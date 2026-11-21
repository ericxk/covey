"""Backend event dispatch and task-card callbacks; supplements are in steering.py."""

from __future__ import annotations

import time
from pathlib import Path

from covey.app import policy_guard
from covey.app import triage as triage_flow
from covey.core import timing, triage
from covey.core.checkpoint import release_user_wait
from covey.core.trace import CONTEXT_FIELDS, ToolTrace, finish_record_trace, task_trace
from covey.core.turn_words import retry_hint
from covey.core.jobs import job_key, quick_held, upsert_job_record
from covey.core.locks import card_lock
from covey.cards.elements import md
from covey.cards.question_cards import (ASK_EMPTY, ASK_KIND, ASK_SETTLED, PICK, RESTORE_KIND, SET_ASIDE_KIND,
                                        SET_ASIDE_SETTLED, SKIP_ALL_KIND, SUPPLEMENT, folded, folded_refusal,
                                        question_feedback_card)
from covey.app.card_lanes import after_ack
from covey.app.answers import ATTACHMENTS, AnswerIncomplete, picked_fields, skip_all_fields
from covey.app.question_display import standalone_card, top_card
from covey.app.questions import (QuestionFolded, fold_older_questions, question_card_lock, question_path,
                                          scoped_question)
from covey.cards.held_note import NOTE_KIND
from covey.app.steering import STEER_KINDS
from covey.core.store import directory_store, transition_question, update_question
from covey.core.statuses import ENDED
from covey.app.card_actions import action_value, form_fields
from covey.core.text import literal
from covey.cards.trace_updates import panel_json
from covey.cards.trace_view import BRIDGE_NOTE, TraceView
from covey.core.metrics import mark_first_output
from covey.core.trace_store import load_trace


def job_trace(job):
    """The job's execution trace, started at the backend request when the first event creates it."""
    trace = job.tool_trace
    if trace is None:
        trace = job.tool_trace = task_trace()
        trace.started_at = job.timings.get("backend_requested", trace.started_at)
    return trace


def _show_answer_snapshot(cards, ctx, job, data):
    """The answer so far, streamed to the task card until the answer is final. An empty one (Claude
    sends it when a tool starts) leaves the body blank: "正在处理…" there said what the status line
    above already says."""
    if job.answer_finalized:
        return
    text = data.get('text', '')
    if text and job.activity_monitor is not None:
        job.activity_monitor.output()
    def flush_answer():
        with card_lock(job.progress):
            if job.progress and not job.answer_finalized:
                job.body_clear_pending = False  # The answer takes the queued text's place.
                cards.update_streaming_reply(ctx, job.progress, text or ' ', force=True)
    cards.trace_updates(job).submit('answer', flush_answer)


def _cancel_live_question(questions, ctx, job, data):
    """The backend withdrew a live native question: cancel its record; with none pending, the
    checkpoint no longer waits for the user."""
    for path, record in directory_store(ctx.state_dir / "questions").query(job_id=job.job_id, topic_id=job.topic_id):
        if record.get("request_id") == data.get("requestId") and record.get("native_live_question") is True:
            updated = transition_question(path, {"pending", "submitting", "uncertain"}, status="cancelled")
            if updated:
                questions.paint_question_state(ctx, updated)
    remaining = directory_store(ctx.state_dir / "questions").query(
        job_id=job.job_id, topic_id=job.topic_id, chat_id=job.chat_id, status="pending")
    if not remaining:
        release_user_wait(Path(job.checkpoint_path))


def _resolve_live_question(cards, questions, ctx, job, data):
    """The backend took a live question's answer: the answer is delivered, and its card says so."""
    def delivered(record):
        if (record.get("job_id") != job.job_id or record.get("request_id") != data.get("requestId")
                or record.get("delivery_state") != "awaiting_ack"):
            return None
        return {**record, "delivery_state": "delivered"}
    for path, _ in directory_store(ctx.state_dir / "questions").query(job_id=job.job_id):
        record = update_question(path, delivered)
        if record:
            cards.trace_updates(job).submit("question-" + record["token"],
                lambda saved=record: questions.paint_question_state(ctx, saved), urgent=True)


def _record_turn_metric(cards, ctx, job, kind, data):
    """Time to first token, or the turn's token usage (the main task's, or its subtasks'): kept on the
    job record and the process panel; a redacted turn's counts on the process panel only."""
    trace = job_trace(job)
    if not trace.accept(kind, data):
        return
    if kind == "first_token":
        upsert_job_record(ctx.state_dir, job.message_id, ttft_seconds=trace.ttft_seconds,
            ttft_source=trace.ttft_source)
        cards.queue_trace(ctx, job, True)  # Its flush saves the process record.
    elif kind == "subtask_usage":
        # The main task's fields keep their scope (parent_turn); this one adds the subtasks.
        upsert_job_record(ctx.state_dir, job.message_id, subtask_usage=trace.subtask_usage)
        cards.queue_trace(ctx, job, False)
    elif kind == "redaction":
        cards.queue_trace(ctx, job, False)  # The process record keeps it for 任务详情.
    else:
        upsert_job_record(ctx.state_dir, job.message_id, output_tokens=trace.output_tokens,
            input_tokens=getattr(trace, "input_tokens", None), input_tokens_scope="parent_turn",
            cache_read_tokens=getattr(trace, "cache_read_tokens", None),
            cache_write_tokens=getattr(trace, "cache_write_tokens", None),
            output_tokens_scope="parent_turn", measured_output_tokens=trace.measured_output_tokens,
            generation_seconds=trace.generation_seconds,
            **{key: getattr(trace, key) for key in CONTEXT_FIELDS if getattr(trace, key, None)})
        cards.queue_trace(ctx, job, False)


def clicked_card(event, ctx, record, copy_card=top_card):
    """The question's card as the one clicked shows it: its top-level copy (app/question_display.py mirror, drawn by
    ``copy_card``: Questions.copy_card keeps its turn's 产物 on it) or the card in the topic."""
    copy = event.get("message_id") and event.get("message_id") == record.get("mirror_message_id")
    return copy_card(ctx, record) if copy else standalone_card(ctx, record)


class Interaction:
    """One Harness's topic interaction while a task runs or waits: the backend's structured
    events (progress, questions, supplements read), a topic reply while questions wait, and
    the clicks on question, held-note, supplement and process-panel controls.

    - ``harness``: whose backend capabilities and voice.
    - ``runtime``: the live jobs a click or reply goes to, and the log.
    - ``messenger``: notices.
    - ``cards``: the task card and process panels an event or a click updates.
    - ``questions``: answers, feedback and held notes.
    - ``steering``: supplements.
    """

    def __init__(self, harness, *, runtime, messenger, cards, questions, steering) -> None:
        self.harness = harness
        self.runtime = runtime
        self.messenger = messenger
        self.voice = harness.voice
        self.cards = cards
        self.questions = questions
        self.steering = steering

    def structured_harness_event(self, ctx, job, kind, data, rpc, *, from_backend=True):
        # A note from the Bridge itself is shown like progress but is not the backend's first output.
        if from_backend and kind in {'answer_snapshot', 'tool', 'progress_note', 'agent_state', 'turn_finished', 'question'}:
            if mark_first_output(job) and job.execution_started:
                upsert_job_record(ctx.state_dir, job.message_id, timings=job.timings)
        with job.trace_lock:
            return self._dispatch_structured_event(ctx, job, kind, data, rpc)

    def _dispatch_structured_event(self, ctx, job, kind, data, rpc):
        if kind == "steer_read":
            self.steering.mark_steer_read(ctx, job, str(data.get("event_id") or ""))
            return
        # Only the main task's model request is the status line's: a sub-task's wait or work leaves it.
        main = not data.get("child_id")
        retry = data.get("retry") if kind == "progress_note" and main else None
        retrying = bool(job.backend_retry)
        if isinstance(retry, dict):
            job.backend_retry = {k: v for k, v in retry.items()
                                 if (k in {"attempt", "next", "since", "limit"} and type(v) in {int, float})
                                 or (k == "reason" and isinstance(v, str) and v)}
        elif main and kind in {"tool", "answer_snapshot", "progress_note", "model_reasoning", "model_output"}:
            job.backend_retry = {}
        if job.activity_monitor is not None and (isinstance(retry, dict) or retrying and not job.backend_retry):
            # Without this a long task's status line would wait up to 15 s to show or end the retry.
            self.cards.trace_updates(job).submit("backend-retry", job.activity_monitor.tick, urgent=True)
        if kind in {"model_reasoning", "model_output"}:
            # Only the status line hears of these: the trace has nothing to show for them.
            monitor = job.activity_monitor
            if monitor is not None:
                (monitor.reasoning if kind == "model_reasoning" else monitor.output)()
            return
        if kind == 'answer_snapshot':
            _show_answer_snapshot(self.cards, ctx, job, data)
            return
        if kind in {"tool", "progress_note", "agent_state"}:
            trace = job_trace(job)
            if not trace.accept(kind, data):
                return
            if kind == "agent_state":
                job.agents = {**job.agents, data["id"]: {k:v for k,v in data.items() if k != "id"}}
            urgent = (data.get("urgent") is not False if kind == "progress_note"
                      else data.get("status") in {"failed", "errored", "completed"})
            self.cards.queue_trace(ctx, job, urgent)
            return
        if kind == "question_cancelled":
            _cancel_live_question(self.questions, ctx, job, data)
            return
        if kind == "question_resolved":
            _resolve_live_question(self.cards, self.questions, ctx, job, data)
            return
        if kind in {"first_token", "token_usage", "subtask_usage", "redaction"}:
            _record_turn_metric(self.cards, ctx, job, kind, data)
            return
        if kind == "policy_tolerated":
            self._policy_tolerated(ctx, job, data)
            return
        if kind == "trace_finished":
            trace = job.tool_trace
            if trace is not None and trace.steps:
                # Sent as the turn closes, before the worker has its outcome: a stop is already known.
                trace.finish(stopped=job.cancel_event.is_set())
                self.cards.queue_trace(ctx, job, True)
            return
        if kind in {"question", "turn_finished", "cancel_questions"}:
            if kind == "question" and quick_held(job):
                # A topic's follow-up's held card goes out first: its question is asked as on any task card.
                self.cards.reveal_held(ctx, job, "question")
            self.questions.dispatch_question_event(ctx, job, kind, data, rpc)

    def _policy_tolerated(self, ctx, job, data):
        """A subagent's first refusal, let through (docs/policy-stop.md): a line of the Bridge's in 执行过程, and the
        record's ``policy_tolerated`` so the policy guard counts it once (app/policy_guard.py tolerate)."""
        event, text = data.get("event"), str(data.get("text") or "")
        if isinstance(event, dict):
            try:
                policy_guard.tolerate(ctx.state_dir, job_key(job.message_id, job.job_id), event, "stream",
                                      where=str(data.get("where") or ""))
            except (OSError, ValueError) as exc:
                self.runtime.log(f"policy tolerated record failed: {type(exc).__name__}")
        if text:
            at = time.strftime("%H:%M")
            self._dispatch_structured_event(ctx, job, "progress_note", {
                "id": f"policy-tolerated-{time.time_ns()}", "title": BRIDGE_NOTE, "text": f"机器人 {at}：{text}"}, None)

    def interactive_command(self, ctx, content, chat, mid, attachments=""):
        try:
            return self._interactive_command(ctx, content, chat, mid, attachments)
        except (ValueError, OSError, RuntimeError) as exc:
            self.messenger.reply(ctx, chat, f"交互暂未完成：{exc}", self.voice.key("interaction-error", mid), voice=self.voice)
            return True

    def _interactive_command(self, ctx, content, chat, mid, attachments=""):
        self.questions.reconcile_checkpoint_questions(ctx, chat)
        # Before the message is routed: it answers, or waits for, only the newest question card.
        fold_older_questions(ctx, [self.questions], topic=(ctx.topic_id, chat))
        timing.mark("cmd_reconcile")
        rows = directory_store(ctx.state_dir / "questions").query(topic_id=ctx.topic_id, chat_id=chat, status="pending")
        if triage_flow.ENV is not None and any(triage.held(row) for _, row in rows):
            # The owner speaks in a topic whose question the project's lead holds: it is theirs now (the card goes out first, so
            # their message is its answer or a note, not a refusal for a card that was never shown).
            triage_flow.owner_spoke(triage_flow.ENV, ctx.topic_id, chat)
            rows = directory_store(ctx.state_dir / "questions").query(topic_id=ctx.topic_id, chat_id=chat, status="pending")
        timing.mark("cmd_questions")
        undelivered = self.questions.undelivered_checkpoint_jobs(ctx, chat)
        timing.mark("cmd_undelivered")
        if undelivered or any(row.get("display_pending") or (row.get("body_delivery_required")
                and not self.questions.checkpoint_result_delivered(ctx, row)) for _, row in rows):
            self.messenger.reply(ctx, chat, f"原结果或问题控件尚未送达，请先{retry_hint()} 重新投递；本条没有续接或排队。",
                                 self.voice.key("answer-awaiting-result", mid), voice=self.voice)
            return True
        from covey.core.harness import backend_capabilities
        capabilities = backend_capabilities(self.harness.backend, ctx)
        timing.mark("cmd_capabilities")
        if not (capabilities.questions or capabilities.steering):
            return False
        pending = self.questions.pending_questions(ctx, chat)
        timing.mark("cmd_pending")
        blocking = pending
        # An image sent as the answer reaches the model as its downloaded path; cards show only "附件：图片 1 张".
        if len(blocking) == 1 and len(blocking[0]["questions"]) == 1:
            self.questions.submit_answer(ctx, chat, blocking[0]["token"],
                               {"answer_0": content, **({ATTACHMENTS: attachments} if attachments else {})}, mid)
            return True
        if blocking:
            # Several answers are awaited: keep the message for the first submitted answer.
            self.questions.hold_note(ctx, chat, content, mid, blocking, attachments=attachments)
            return True
        return False

    def interaction_action(self, ctx, event):
        """A click on a question's card (提交回答, 只发补充说明, 先问一句, 不处理 and its 撤销, its state, a saved answer's
        check), on a supplement's receipt or a held note's card, or an old process panel's page."""
        raw = action_value(event)
        kind = raw.get("kind")
        if raw.get("topic_id") != ctx.topic_id:
            raise ValueError("操作不属于当前话题")
        chat = event.get("chat_id", "")
        mid = event.get("event_id") or event.get("message_id", "")
        fields = form_fields(event)
        if fields is None:
            raise ValueError("表单数据无效，请重新提交")  # card_action_failed shows a ValueError's text.
        if kind == 'bridge_question_resume':
            self.questions.resume_saved_answer(ctx, chat, str(raw.get('token', '')), mid)
            return True
        token = str(raw.get("token", ""))
        if kind == "bridge_question_status":
            # Read under the card lock: a just-acknowledged answer's follow-up may be painting this card.
            with question_card_lock(question_path(ctx, token)):
                record = scoped_question(ctx, chat, token)
                next_job = self.harness.topic_record(ctx, chat, record.get("continuation_job_id"))
                if next_job and next_job["status"] in {"running","done","failed"}:
                    record["delivery_state"] = next_job["status"]
                self.messenger.publish_card(ctx, chat, mid, clicked_card(event, ctx, record, self.questions.copy_card),
                                            source_message=event.get("message_id", ""), voice=self.voice)
        elif kind == "bridge_trace":
            self.show_trace_page(ctx, chat, mid, event, raw, fields)
        elif kind == "bridge_question":
            record = scoped_question(ctx, chat, token)
            if folded(record):
                self.show_folded(ctx, chat, event, token, mid)
                return True
            if record.get("status") != "pending":
                with question_card_lock(question_path(ctx, token)):
                    self.questions.paint_question_state(ctx, scoped_question(ctx, chat, token))
            if raw.get("mode") == "feedback":
                if record.get("status") != "pending":
                    raise ValueError("该组问题已处理，请在本话题继续说明新要求")
                self.messenger.publish_card(ctx, chat, mid, question_feedback_card(record), voice=self.voice)
                return True
            if raw.get("mode") == "feedback_submit":
                try:
                    self.questions.submit_question_feedback(ctx, chat, raw["token"], fields.get("feedback"), mid)
                except QuestionFolded:  # Folded between the read above and the answer's own check.
                    self.show_folded(ctx, chat, event, token, mid)
                    return True
                if event.get("message_id"):
                    # After the answer's own follow-up: the feedback card shows where it ended.
                    after_ack(lambda: self.cards.repaint_browse_card(ctx, chat, mid, event["message_id"], clicked_card(
                        event, ctx, scoped_question(ctx, chat, raw["token"]), self.questions.copy_card)))
                return True
            try:
                if PICK in raw:  # An option's button on a top-level copy: that option is the answer.
                    fields = picked_fields(record["questions"], fields, raw[PICK])
                self.questions.submit_answer(ctx, chat, raw["token"], fields, mid)
            except AnswerIncomplete as exc:
                self.questions.show_answer_notice(ctx, chat, raw["token"], str(exc), mid)
            except QuestionFolded:
                self.show_folded(ctx, chat, event, token, mid)
        elif kind == SKIP_ALL_KIND:
            self.skip_all(ctx, chat, event, token, fields, mid)
        elif kind == ASK_KIND:
            self.ask_first(ctx, chat, event, token, fields, mid)
        elif kind == SET_ASIDE_KIND:
            self.set_aside(ctx, chat, event, token, mid)
        elif kind == RESTORE_KIND:
            self.questions.restore(ctx, chat, token, mid)
        elif kind in STEER_KINDS:
            self.steering.steer_action(ctx, chat, event, raw, fields, mid)
        elif kind == NOTE_KIND:
            self.questions.note_action(ctx, chat, raw, mid)
        return True

    def skip_all(self, ctx, chat, event, token, fields, mid):
        """“这些题先不答，只发补充说明”: every question 不回答此题 and the 补充说明 sent, through the same answer
        path as 提交回答 (a runner that still waits for the answer gets it). An empty 补充说明 is refused on the
        clicked card, which is redrawn with the refusal under the form; a small notice card when that card
        cannot be redrawn (a form on a task card)."""
        record = scoped_question(ctx, chat, token)
        if folded(record):
            self.show_folded(ctx, chat, event, token, mid)
            return
        if record.get("status") != "pending":
            with question_card_lock(question_path(ctx, token)):
                self.questions.paint_question_state(ctx, scoped_question(ctx, chat, token))
            self.questions.submit_answer(ctx, chat, token, fields, mid)  # Refused there, in its own words.
            return
        try:
            answer = skip_all_fields(record["questions"], fields)
        except AnswerIncomplete as exc:
            self.refuse_on_card(ctx, chat, event, token, str(exc), mid)
            return
        try:
            self.questions.submit_answer(ctx, chat, token, answer, mid)
        except QuestionFolded:
            self.show_folded(ctx, chat, event, token, mid)

    def refuse_on_card(self, ctx, chat, event, token, text, mid, *, elsewhere=None):
        """Say ``text`` under the form of the clicked card, redrawn whole and still open: the card in the topic, or its
        copy at the top level (app/question_display.py). A form on a task card is not redrawn alone: ``elsewhere(text)``
        then, by default the small notice card 提交回答 gets (show_answer_notice)."""
        source = event.get("message_id", "")
        with question_card_lock(question_path(ctx, token)):
            current = scoped_question(ctx, chat, token)
            build = (standalone_card if source == current.get("question_message_id")
                     else self.questions.copy_card if source == current.get("mirror_message_id") else None)
            if (current.get("status") == "pending" and source and build is not None
                    and self.cards.repaint_browse_card(ctx, chat, mid, source, build(ctx, {**current, "refusal": text}))):
                return
        if elsewhere is None:
            self.questions.show_answer_notice(ctx, chat, token, text, mid)
        else:
            elsewhere(text)

    def ask_first(self, ctx, chat, event, token, fields, mid):
        """先问一句 (T604 #13): what the owner wrote in 补充说明 goes to the asking topic as a question about these
        questions (Questions.ask_first), the card stays open and nothing counts as an answer. An empty 补充说明 is
        refused on the clicked card as 只发补充说明's is (``refuse_on_card``; under a task card's form, a line in the
        topic: the notice card 提交回答 gets speaks of an answer). A question settled meanwhile gets its card redrawn."""
        record = scoped_question(ctx, chat, token)
        if folded(record):
            self.show_folded(ctx, chat, event, token, mid)
            return
        if record.get("status") != "pending":
            with question_card_lock(question_path(ctx, token)):
                self.questions.paint_question_state(ctx, scoped_question(ctx, chat, token))
            raise ValueError(ASK_SETTLED)
        words = str(fields.get(SUPPLEMENT) or "").strip()
        if not words:
            self.refuse_on_card(ctx, chat, event, token, ASK_EMPTY, mid, elsewhere=lambda text: self.messenger.reply(
                ctx, chat, text + "。", self.voice.key("question-ask-empty", mid), voice=self.voice))
            return
        try:
            self.questions.ask_first(ctx, chat, token, words, mid, sender=str(event.get("operator_id") or ""))
        except QuestionFolded:  # Folded between the read above and its own check.
            self.show_folded(ctx, chat, event, token, mid)

    def set_aside(self, ctx, chat, event, token, mid):
        """不处理 (T604 #15): every question 不回答此题, off the 等你 lists, nothing sent until the topic's next turn
        (Questions.set_aside). A question settled meanwhile gets its card redrawn and the click is refused."""
        record = scoped_question(ctx, chat, token)
        if folded(record):
            self.show_folded(ctx, chat, event, token, mid)
            return
        if record.get("status") not in ("pending", "set_aside"):
            with question_card_lock(question_path(ctx, token)):
                self.questions.paint_question_state(ctx, scoped_question(ctx, chat, token))
            raise ValueError(SET_ASIDE_SETTLED)
        try:
            self.questions.set_aside(ctx, chat, token, mid, by=str(event.get("operator_id") or ""))
        except QuestionFolded:
            self.show_folded(ctx, chat, event, token, mid)

    def show_folded(self, ctx, chat, event, token, mid):
        """A click on a question's form, or on its feedback card, after the question was folded under a
        newer one while the client still showed the form: the clicked card is redrawn folded, saying where
        to answer (a refusal goes on its card); nothing is sent. A topic message only when that card cannot
        be redrawn (a form on a task card)."""
        with question_card_lock(question_path(ctx, token)):
            record = scoped_question(ctx, chat, token)
            refusal = folded_refusal(record)
            source = event.get("message_id", "")
            if source and self.cards.repaint_browse_card(ctx, chat, mid, source,
                                                         clicked_card(event, ctx, {**record, "refusal": refusal},
                                                                      self.questions.copy_card)):
                return
            self.questions.paint_question_state(ctx, record)
        raise ValueError(refusal)

    def show_trace_page(self, ctx, chat, mid, event, raw, fields):
        """A page picked on a task card sent with page controls (an older release): the process panel redrawn
        as its one page, from its trace file (core/migrate.py wrote an older record's there); a typed page
        that does not exist is noted above the records."""
        started = time.monotonic()
        record = self.harness.topic_record(ctx, chat, int(raw["job_id"]))
        if record is None:
            raise ValueError("过程记录不存在")
        snapshot = load_trace(ctx.state_dir, record)
        if snapshot:
            trace = ToolTrace.restore(snapshot)
        else:  # No process record: an empty panel with the record's figures.
            trace = ToolTrace()
            trace.output_tokens = record.get("output_tokens")
            trace.started_at = time.time()
            trace.ended_at = trace.started_at + record.get("elapsed_seconds", 0)
        if record.get("status") in ENDED | {"waiting_user"}:
            finish_record_trace(trace, record)
        notice = ""
        # compat(dd3ee63): a process panel sent before dd3ee63 has page controls, whose every pick draws the one
        # page; retire when no job record from before then keeps its task card (compat_report: trace-pages)
        if raw.get("verb") == "jump":
            value = str(event.get("option") or event.get("input_value") or fields.get("trace_page", "")).strip()
            limit = int(raw.get("page_count", 0))
            if not value.isascii() or not value.isdecimal() or not 1 <= int(value) <= limit:
                if not event.get("input_value"):
                    raise ValueError(f"请输入 1–{limit} 之间的整数页码")
                # A typed page that does not exist: said above the records; the next pick redraws without it.
                notice = f"⚠️ “{literal(value[:10])}”不是有效页码，请输入 1–{limit} 之间的整数"
        model = TraceView(trace.snapshot()).panel_model(True, opened=True)  # Opened: its page was picked inside it.
        if notice:
            model = model._replace(notice=md(notice, element_id="trace_page_notice", size="notation"))
        self.cards.update_record_panel(ctx, chat, mid, event, record, panel_json(model))
        # page=-1: the newest page, which every pick drew since the panel has one.
        self.runtime.log(f"trace navigation job={record['job_id']} page=-1 elapsed_ms={(time.monotonic()-started)*1000:.0f}")

