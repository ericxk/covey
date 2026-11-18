"""Cards: the one place that writes a Harness's task and process cards.

A task card is the streaming CardKit card of one job: created when the job is queued,
updated while it runs (status, process panel, answer), closed when it ends, and
replaced when an update keeps failing (the body, status line, panels and close
through ``self.stream``, a StreamingCard from app/task_card.py). Records keep their
card's address, so the card can be redrawn later from the record alone (process
panel pages, recovery and redelivery cards, result controls). Browse cards
(question history, trace pages) are published through the Messenger and redrawn
in place the same way. Two parts live in their own modules and are Cards' own components:
任务详情 and the process panel (``details``, app/task_details.py) and the writes by record or
saved address (``records``, app/record_cards.py); their methods other components call stay
Cards methods.
"""

from __future__ import annotations

import copy
import datetime as _dt
import functools
import time

from covey.app.task_card import (StreamingCard, StreamingReply, bind_record, body_anchor, bottom_anchor,
                                          count_update_failure, details_anchor, drop_element, io_card, note_element,
                                          status_actions, took_status, under_body, write_live)
from covey.app import answer_cards
from covey.app.launch_models import available_models
from covey.core.menu_choice import find_choice
from covey.core.trace import ToolTrace
from covey.cards.cardkit_actions import (batch_body, delete, element_body, insert, insert_body,
                                                  settings_body, update)
from covey.cards.review import (compact_text, outcome_line, render_stream_final, render_stream_status,
                                         running_summary, task_card_summary)
from covey.cards.elements import grey
from covey.core import butler, projects
from covey.cards.streaming import (ACTION_ROW_ELEMENT_ID, SIDE_CHAT_ELEMENT_ID, answer_element,
                                            card_title_for_question)
from covey.cards.card_cost import exceeds_stream_card_limit
from covey.core.redaction import topic_redaction
from covey.cards.trace_view import TraceView, clock
from covey.cards.trace_updates import incremental_actions, panel_json
from covey.core.harness import BridgeContext, backend_capabilities
from covey.core.jobs import BridgeReplyJob as ReplyJob
from covey.core.jobs import RUN_STAGES, find_job_record, job_key, upsert_job_record
from covey.core.locks import card_lock
from covey.core.metrics import run_seconds, run_start, took_text
from covey.core.topics import remember_topic_message, topic_label
from covey.core.trace_store import save_trace
from covey.app import auto_retry
from covey.app.record_cards import RecordCards
from covey.app.task_details import TaskDetails

STREAM_CARD_ELEMENT_ID = "answer"
STOPPING = "正在停止… · 已收到停止请求，正在中断后台任务。"

# A task card that never went out is sent again from its running task (Cards.resend_missing): this many times,
# this many seconds apart.
CARD_RESENDS = 3
CARD_RESEND_EVERY = 60.0


class Cards:
    """One Harness's card writes.

    - ``harness``: whose voice the cards speak with and whose backend decides their controls.
    - ``runtime``: the job registry (a created card registers its job) and the log.
    - ``messenger``: every CardKit call and published card.
    - ``record_job_state``: the Harness's job-state recorder; a replaced task card is recorded
      through it (late-bound: recording a state repaints cards, so it sits above Cards).
    - ``topics``: the Bridge's Topics, whose model catalogs name a task's model; None for an app built alone.
    """

    def __init__(self, harness, *, runtime, messenger, record_job_state, topics=None) -> None:
        self.harness = harness
        self.runtime = runtime
        self.messenger = messenger
        self.voice = harness.voice
        self.record_job_state = record_job_state
        self.topics = topics
        # Off only in tests: a job then goes on without a card, as one whose card was not created does.
        self.streaming = True
        self.stream = StreamingCard(
            idempotency_prefix=harness.spec.idempotency_prefix,
            default_title=harness.spec.title,
            element_id=STREAM_CARD_ELEMENT_ID,
            progress_factory=StreamingReply,
            log=runtime.log,
            safe_idempotency_key=self.voice.safe_key,
            send_text_message=functools.partial(messenger.send_message, voice=self.voice),
            compact_text=compact_text,
            messenger=messenger,
            send_card=functools.partial(messenger.send_card, voice=self.voice),
        )
        self.details = TaskDetails(self)
        self.records = RecordCards(self)

    def create_streaming_reply(self, ctx: BridgeContext, job: ReplyJob, initial_content: str) -> StreamingReply | None:
        if not self.streaming:
            return None
        self.card_facts(ctx, job)
        progress = self.stream.create(ctx.profile, job, initial_content)
        job.card_missing = progress is None  # Sent again while the task runs (resend_missing).
        if progress:
            remember_topic_message(ctx.state_dir, job.topic_id, job.chat_id, progress.message_id)
            bind_record(progress, ctx.state_dir, job_key(job.message_id, job.job_id))
        else:
            self.runtime.log(f"task card missing job=#{job.job_id}: {job.last_card_error or 'not created'}")
        return progress

    def card_facts(self, ctx: BridgeContext, job: ReplyJob) -> None:
        """What a task card is drawn with besides the job's own fields (app/task_card.py CARD_FACTS); registers
        the job. No request: also set on a butler turn whose card is held back (app/submission.py), so its
        answer card's 输入 (app/answer_cards.py inputs) says what a sent card's would; its task runner reads them
        from the handoff's extras and sets them again when it sends the card (``reveal``)."""
        if hasattr(self.harness.backend, "capabilities"):
            job.harness_capabilities = self.harness.backend.capabilities(ctx)
        job.topic_label = topic_label(ctx.state_dir, job.topic_id)
        job.model_label = self.model_label(ctx, job.model)
        job.workspace_label = ctx.workspace_name  # The 输入/输出 box's 输入 (cards/task_figures.py job_inputs).
        # Whether the turn redacts (the box's 输入, cards/answer_card.py input_parts): None for a Harness that cannot.
        job.redaction = (topic_redaction(ctx.state_dir, job.topic_id)
                         if getattr(getattr(job, "harness_capabilities", None), "redaction", False) else None)
        try:
            # The 输入/输出 box's 输入 (cards/task_figures.py io_box), as the answer card's says it.
            job.input_parts = answer_cards.inputs(job, self.harness, ctx=ctx)
        except Exception as exc:  # noqa: BLE001 - the box from the job's own fields (task_figures.job_inputs)
            self.runtime.log(f"task card 输入 unavailable: {type(exc).__name__}")
        self.runtime.jobs.register(job)

    def model_label(self, ctx: BridgeContext, model: str) -> str:
        """``model`` as the model selects name it (Claude · Sonnet 5.5), read from the cached catalogs
        as they read them; a budget preset by its model's entry (its budget is shown beside it);
        "" when they do not list it."""
        if self.topics is None or not model:
            return ""
        choices = available_models(self.topics, ctx)
        found = find_choice(choices, f"{self.harness.spec.name}:{model}")
        return str(choices[found[0]].get("label") or "") if found else ""

    def update_streaming_reply(
        self, ctx: BridgeContext, progress: StreamingReply, content: str, force: bool = False
    ) -> bool:
        return self.stream.update(ctx.profile, progress, content, force=force)

    def close_streaming_reply(self, ctx: BridgeContext, progress: StreamingReply) -> None:
        self.stream.close(ctx.profile, progress)

    def exceeds_stream_card_limit(self, text: str) -> bool:
        return exceeds_stream_card_limit(text, self.stream)

    @staticmethod
    def body_cut(job: ReplyJob) -> bool:
        """Whether the body the job's card took last is cut short to fit the card's size."""
        return bool(job.progress and getattr(job.progress, "cut", False))

    def delete_stop_action(self, ctx: BridgeContext, job: ReplyJob) -> bool:
        """Remove the task card's action row (its 补充 forms are closed by Steering first)."""
        if not job.progress:
            return False
        if job.action_controls_removed:
            return True  # Stopping removed them; the job's end must not delete them again.
        with card_lock(job.progress):
            if self._write(ctx, job, "DELETE", f"elements/{ACTION_ROW_ELEMENT_ID}", {}, "remove-" + ACTION_ROW_ELEMENT_ID):
                job.action_controls_removed = True
                drop_element(job.progress, ACTION_ROW_ELEMENT_ID)
                return True
        # Cards published before the action row existed keep their buttons beside the status text.
        extra = (["steer_action"] if backend_capabilities(self.harness.backend, ctx).steering else []) + (
            [SIDE_CHAT_ELEMENT_ID] if job.topic_id else [])
        for element in extra:
            with card_lock(job.progress):
                self._write(ctx, job, "DELETE", f"elements/{element}", {}, "remove-" + element)
        job.action_controls_removed = self.stream.delete_stop_action(ctx.profile, job.progress)
        return job.action_controls_removed

    def update_job_streaming_reply(
        self,
        ctx: BridgeContext,
        job: ReplyJob,
        content: str,
        *,
        force: bool = False,
        idempotency_prefix: str | None = None,
        allow_replacement: bool = True,
    ) -> bool:
        def record_replacement(updated_job, reason):
            self.record_job_state(ctx, updated_job, "running", reason,
                                  card_replaced_at=_dt.datetime.now(_dt.timezone.utc).isoformat(),
                                  stream_replacements=updated_job.stream_replacements, last_card_error=reason)

        return self.stream.update_job(ctx.profile, job, content, force=force,
                                      idempotency_prefix=idempotency_prefix or self.harness.spec.idempotency_prefix,
                                      record_replacement=record_replacement, allow_replacement=allow_replacement)

    def render_stream_status(self, job: ReplyJob, status: str, events: list[str] | None = None) -> str:
        """A queued or stopping card's body: on a slim card that has one already (the butler's) one small grey
        line, its status line says the rest; a slim card being created takes the whole text in its status line
        (cards/task_status.py slim_start)."""
        if job.progress and getattr(job.progress, "slim", False):
            return grey(" · ".join([status, *(events or ())]))
        return render_stream_status(job, status, events, queue_depth=self.runtime.jobs.queue.qsize())

    def render_stream_final(self, job: ReplyJob, final_text: str, *, status: str = "done",
                            answer_elsewhere: bool = False) -> str:
        """The result block; it leaves the outcome to the status line when this card's line shows it."""
        shown = bool(job.progress) and getattr(job.progress, "outcome", "") == outcome_line(status)
        return render_stream_final(job, final_text, status=status, answer_elsewhere=answer_elsewhere,
                                   outcome_shown=shown, bare=bool(job.progress) and getattr(job.progress, "slim", False))

    def update_card_activity(self, ctx: BridgeContext, job: ReplyJob, content: str) -> bool:
        """The running status line. After a stop click it only clears: a TaskMonitor tick already
        under way cannot put 执行中 back over 正在停止 (the click sets ``cancel_requested_at``
        before its write takes the card lock). A card held back (``reveal``) goes out here once it is due."""
        if not job.progress:
            return self.reveal(ctx, job, content) or self.resend_missing(ctx, job, content)
        with card_lock(job.progress):
            if content and job.cancel_requested_at:
                return False
            job.progress.outcome = ""  # Whatever this write does, the line may no longer show an outcome.
            if job.body_clear_pending and not job.answer_finalized:
                job.body_clear_pending = False
                if self._start_top(ctx, job, content):
                    return True
                self.update_job_streaming_reply(ctx, job, " ", force=True, allow_replacement=True,
                                                idempotency_prefix=f"{self.harness.spec.idempotency_prefix}-progress")
            return self.stream.update_activity(ctx.profile, job.progress, content)

    def reveal(self, ctx: BridgeContext, job: ReplyJob, content: str) -> bool:
        """The task card of a butler turn held back (``job.card_deferred``, core/placement.py Row.quiet), sent by
        its task runner once the turn has run butler.CARD_AFTER seconds, as any task card is (in the owner's
        message's thread, app/butler.py configure), its status line ``content``; the job record gets its address
        (stop clicks, recovery and the board find it there). Tried once; not for a turn stopping or delivering."""
        if not getattr(job, "card_deferred", False) or job.cancel_requested_at or job.answer_finalized:
            return False
        if projects.is_wake(job.message_id):
            return False  # A lead's wake-up turn (app/project_loop.py): the project's card says what it does.
        begun = run_start(getattr(job, "timings", None))
        if begun is None or time.time() - begun < butler.CARD_AFTER:
            return False
        job.card_deferred = False
        progress = self.create_streaming_reply(ctx, job, render_stream_status(job, content or "执行中"))
        if not progress:
            self.runtime.log(f"main thread card not sent job=#{job.job_id}")
            return False
        job.progress = progress
        try:
            upsert_job_record(ctx.state_dir, job_key(job.message_id, job.job_id), card_id=progress.card_id,
                              card_message_id=progress.message_id)
        except OSError as exc:  # The next state write saves it (core/jobs.py job_record_fields).
            self.runtime.log(f"main thread card not recorded job=#{job.job_id}: {type(exc).__name__}")
        self.runtime.log(f"main thread card sent after {butler.CARD_AFTER}s job=#{job.job_id} "
                         f"card_id={progress.card_id}")
        return True

    def resend_missing(self, ctx: BridgeContext, job: ReplyJob, content: str) -> bool:
        """The task card of a running task that never went out (``job.card_missing``: its send failed, the reply
        and the top-level fallback too, app/task_card.py create), sent again from the status line's ticks, at most
        CARD_RESENDS times CARD_RESEND_EVERY seconds apart, its status line ``content``; the job record gets its
        address as ``reveal`` gives it. Not for a task stopping or delivering, nor one whose card was given up for
        text (StreamingCard.replace_card)."""
        if not getattr(job, "card_missing", False) or job.cancel_requested_at or job.answer_finalized:
            return False
        now = time.monotonic()
        if job.card_resends >= CARD_RESENDS or now < job.card_resend_after:
            return False
        job.card_resends += 1
        job.card_resend_after = now + CARD_RESEND_EVERY
        progress = self.create_streaming_reply(ctx, job, render_stream_status(job, content or "执行中"))
        if not progress:
            return False
        job.progress = progress
        try:
            upsert_job_record(ctx.state_dir, job_key(job.message_id, job.job_id), card_id=progress.card_id,
                              card_message_id=progress.message_id, last_card_error=job.last_card_error)
        except OSError as exc:  # The next state write saves it (core/jobs.py job_record_fields).
            self.runtime.log(f"task card not recorded job=#{job.job_id}: {type(exc).__name__}")
        self.runtime.log(f"task card sent again job=#{job.job_id} try={job.card_resends} card_id={progress.card_id}")
        return True

    def _start_top(self, ctx, job, content):
        """The first status line and the body's queued text cleared, in one batch; whether CardKit took
        it. The caller holds the card lock."""
        progress = job.progress
        if progress.disabled:
            return False
        actions = [*status_actions(content or " ", getattr(progress, "status_rows", False),
                                   getattr(progress, "slim", False), io_card(progress)),
                   update(answer_element(progress.element_id, " "))]
        if self._batch(ctx, job, actions, "job-start"):
            progress.last_content = " "
            progress.update_failures = 0
            took_status(progress, content or " ")
            return True
        count_update_failure(progress)
        return False

    def show_stopping(self, ctx: BridgeContext, job: ReplyJob) -> bool:
        """A stop click's first card write, at the top of the card: the status line says 正在停止…
        and the action row goes, in one batch. A card without that row (sent before it existed)
        or a failed batch gets the two writes one after the other. Whether the status line took it:
        a card without the line needs 正在停止 in its body instead."""
        if not job.progress:
            return False
        with card_lock(job.progress):
            if job.progress.disabled:
                return False
            job.progress.outcome = ""
            if self._replace_top(ctx, job, STOPPING, "job-stopping"):
                return True
            shown = self.stream.update_activity(ctx.profile, job.progress, STOPPING)
        self.delete_stop_action(ctx, job)
        return shown

    def show_outcome(self, ctx: BridgeContext, job: ReplyJob, content: str, stopped: bool = False, *,
                     timed: bool = False) -> bool:
        """The run ended: ``content`` (its outcome, ``outcome_line``) takes the status line's place and
        the action row goes, in one batch, so the top of the card never goes blank while the result
        is prepared below. ``timed``: the outcome is followed by how long the run took (✅ 已完成 · 用时
        1小时47分; T182 #1111 showed it only as 总耗时 6460.3 秒 lower down). The trace ends here
        (``stopped``: the user stopped the task) and a process panel the card shows running ends in that
        batch too: S31 K5 read ✅ 已完成 over 执行过程 · 等待进展 … 正在处理 until the result's write.
        Without the row (a stop click removed it, or the card was sent before it existed) the panel and
        then the status line are written alone; ``delete_stop_action`` removes older buttons."""
        if not job.progress:
            return False
        snapshot = self._end_trace(ctx, job, stopped)
        line = card_line = content
        took = self._took(job, snapshot) if timed else None
        if timed:
            line += f" · 用时 {took_text(took)}" if took is not None else ""
            card_line = self._ended_line(job, content, snapshot) or line
        with card_lock(job.progress):
            # The chat list's preview from now on, written when the card closes (StreamingCard.close): every
            # outcome, not only a reply sent beside the card (worker show_lead), or the topic's row kept saying
            # 执行中 (cards/review.py running_summary).
            job.progress.summary = task_card_summary(self.summary_label(job),
                                                     "done" if content == outcome_line("done") else "", line)
            if job.progress.disabled and not self._revive(job.progress):
                return False
            job.progress.outcome = ""
            panel = self._ended_panel(ctx, job, snapshot)
            figures = self.details.card_figures(job, snapshot, took, ended=True)
            extra = [*(incremental_actions(job.trace_rendered_panel, panel) if panel else ()),
                     *([update(figures)] if figures else ())]
            if self._replace_top(ctx, job, card_line, "job-outcome", extra):
                if panel:
                    job.trace_rendered_panel = panel
                    note_element(job.progress, panel)
                if figures:
                    self.details.took_figures(job, figures)
                shown = True
            else:
                if panel is not None:
                    self.details.set_process_panel(ctx, job, panel, figures=figures)
                elif figures:
                    self.details.write_elements(ctx, job, [], figures)
                shown = self.stream.update_activity(ctx.profile, job.progress, card_line)
            if shown:  # Only a confirmed write: an old card without the line, or a failure, keeps it below.
                job.progress.outcome = content  # The outcome alone: render_stream_final compares it.
                job.progress.timed_outcome = line  # With 用时: the chat list's preview (show_lead).
            return shown

    @staticmethod
    def _ended_line(job, content, snapshot):
        """A dashboard card's status line once its run ended: the outcome and when it ended (✅ 已完成 · 23:41
        结束); its time is in the 用时 tile. "" on a card sent
        before, or without a process record: the line then says 用时 itself."""
        if not getattr(job.progress, "dashboard", False) or getattr(job.progress, "slim", False) or snapshot is None:
            return ""  # A slim card's line says 用时 itself: its tiles are folded.
        # No output speed since 2026-11-04 15:07 (the owner: 冗余的文字有点多): the 输入/输出 box's 输出 has it.
        return " · ".join([content, clock(snapshot.get("ended_at") or time.time()) + " 结束"])

    def say_outcome(self, ctx, job, line) -> bool:
        """A butler card's last status line and chat-list preview: what the turn did, ``line`` (app/butler_reply.py
        outcome_words, V6, 2026-11-08), in the place of "✅ 已完成 · 用时 …" and "#12 · 已完成 · 用时 …". One batch of
        the status elements alone (the 输入/输出 box keeps its 用时); the preview goes with the card's close. Whether
        CardKit took it."""
        progress = job.progress
        if not progress or not line:
            return False
        with card_lock(progress):
            if progress.disabled:
                return False
            progress.summary = line
            actions = status_actions(line, getattr(progress, "status_rows", False), getattr(progress, "slim", False),
                                     io_card(progress))
            if not self._batch(ctx, job, actions, "main-outcome"):
                return False
            took_status(progress, line)
            progress.timed_outcome = line
            return True

    @staticmethod
    def summary_label(job):
        """What a task card's chat-list preview names its task by: the topic (T298), else the task (#12)."""
        return getattr(job, "topic_label", "") or f"#{job.job_id}"

    def set_lead_summary(self, job, status):
        """The chat list's preview of a card whose reply's lead moved to its top (worker show_lead), from when
        the card closes: the topic, the outcome and the time, "T298 · 已完成 · 用时 3 分 39 秒"
        (``task_card_summary``; the answer card sent beside it has the lead)."""
        job.progress.summary = task_card_summary(self.summary_label(job), status,
                                                 getattr(job.progress, "timed_outcome", ""))

    def show_running_summary(self, ctx, job, phase="", asking=False):
        """The task card's chat-list preview while its task runs (cards/review.py running_summary), written when
        it changes: the TaskMonitor reports the checkpoint's phase and a model's question as they come and go.
        Whether the card shows it."""
        progress = job.progress
        if not progress:
            return False
        text = running_summary(self.summary_label(job), card_title_for_question(getattr(job, "title", "") or job.user_text,
                                                                                self.stream),
                               phase, asking)
        with card_lock(progress):
            if progress.disabled:
                return False
            if getattr(progress, "summary", "") == text:
                return True
            if self._write(ctx, job, "PATCH", "settings", settings_body({"summary": {"content": text}}),
                           "running-summary") is None:
                return False
            progress.summary = text
            return True

    def _revive(self, progress):
        """A card that failed writes disabled (not one closed or replaced) is written once more when its
        run ends: its outcome, result and close, so it never stays at 执行中 (#1150). One more failed
        body write disables it again. The caller holds the card lock."""
        if not getattr(progress, "failed_off", False):
            return False
        progress.disabled, progress.failed_off, progress.update_failures = False, False, 2
        self.runtime.log(f"task card writes resumed for the run's end card_id={progress.card_id}")
        return True

    @staticmethod
    def _took(job, snapshot):
        """The run's 用时 (core/metrics.py run_seconds), the 用时 tile's too: its timings, else its process record.
        None when the run never reached the backend (and has no process record)."""
        timings = getattr(job, "timings", None) or {}
        if snapshot is None and not any(stage in timings for stage in RUN_STAGES):
            return None
        return run_seconds(timings, snapshot)

    def _end_trace(self, ctx, job, stopped):
        """End the job's trace and save it; its snapshot, None without one. Ending again keeps the end."""
        trace = job.tool_trace
        if trace is None:
            return None
        with job.trace_lock:
            trace.finish(stopped)
            snapshot = copy.deepcopy(trace.snapshot())
        save_trace(ctx.state_dir, job.message_id, snapshot)
        return snapshot

    def _ended_panel(self, ctx, job, snapshot):
        """The ended trace's process panel, when this card shows the trace's panel; else None (one not
        drawn yet comes with the result, as before). The caller holds the card lock."""
        if snapshot is None or job.trace_rendered_panel is None or job.trace_rendered_card != job.progress.card_id:
            return None
        return panel_json(TraceView(ToolTrace.restore(snapshot).snapshot()).panel_model(running=False))

    def _replace_top(self, ctx, job, content, name, extra=()):
        """``content`` in the status line and the action row deleted, in one batch with ``extra``
        actions, while the row is there; whether CardKit took it. The caller holds the card lock."""
        if job.action_controls_removed:
            return False
        actions = [*status_actions(content, getattr(job.progress, "status_rows", False),
                                   getattr(job.progress, "slim", False), io_card(job.progress)),
                   delete(ACTION_ROW_ELEMENT_ID), *extra]
        if self._batch(ctx, job, actions, name):
            job.action_controls_removed = True
            drop_element(job.progress, ACTION_ROW_ELEMENT_ID)
            took_status(job.progress, content)
            return True
        return False

    def lead_first(self, ctx, job):
        """The body moved up to right under the status line, above 本轮请求, in one batch: the delete
        and the same element added back there (CardKit has no move; a later body write still lands on
        it by its element_id). For a body that is the sent reply's lead (TurnDelivery.delivered_note):
        what the turn came to is then read first, not under the request and 任务详情 (T288). Whether
        CardKit took it; a failed batch is only logged, the card keeps its order (a card sent before
        本轮请求 existed refuses it whole)."""
        progress = job.progress
        if not progress:
            return False
        if getattr(progress, "dashboard", False):
            return True  # Its body is under the status line and the tiles already, the lead under 结论.
        with card_lock(progress):
            if progress.disabled or progress.lead_first:
                return bool(progress.lead_first)
            body = answer_element(progress.element_id, progress.last_content or " ")
            actions = [delete(progress.element_id), insert([body], "current_question", before=True, as_list=True)]
            if not self._batch(ctx, job, actions, "lead-first"):
                self.runtime.log(f"lead not moved to the top card_id={progress.card_id}")
                return False
            progress.lead_first = True
            return True

    def _write(self, ctx, job, method, part, body, name):
        """One write to the job's live task card, keyed by ``name``, the job and its sequence
        (task_card.write_live: once more on CardKit's 300317 only). The caller holds the card lock."""
        return write_live(self.messenger, ctx.profile, job.progress, method, part, body, log=self.runtime.log,
                          key=lambda sequence: self.voice.key(name, f"{job.message_id}-{sequence}"))

    def _batch(self, ctx, job, actions, name):
        """``actions`` in one batch on the job's live task card (``_write``). The caller holds the card lock."""
        return self._write(ctx, job, "POST", "batch_update", batch_body(actions), name)

    def _write_failed(self, job, what):
        """A write to the job's task card failed: counted, and ``what`` is its last card error and logged."""
        count_update_failure(job.progress)
        job.last_card_error = what
        self.runtime.log(what)

    def set_task_element(self, ctx, job, element, *, before="", at_bottom=False, under=False):
        """Put ``element`` on the job's task card: replaced in place when the card has it, else inserted
        after the body, before ``before``, or, ``at_bottom``, after the process panel when this card
        shows one (the 🔀 💬 🔁 buttons of a finished turn: in the middle they cut the result off from
        its artifacts and figures, T182 #1111). On a slim card (task_card.body_anchor) "after the body" and
        the bottom are in its folded 详情; ``under`` and a question (its ID starts q_, cards/question_cards.py)
        go right under the body instead (``under_body``), where they show without opening anything."""
        if not job.progress:
            return False
        with card_lock(job.progress):
            if job.interactive_card_id not in (None, job.progress.card_id):
                job.interactive_elements = set()
            job.interactive_card_id = job.progress.card_id
            present = job.interactive_elements
            eid = element["element_id"]
            after = body_anchor(job.progress)  # Where the body was, once its lead moved up (lead_first).
            if under or eid.startswith("q_"):
                after = under_body(job.progress)
            elif at_bottom:
                after = bottom_anchor(job.progress, job.trace_rendered_panel is not None
                                      and job.trace_rendered_card == job.progress.card_id)
            # 任务详情 is part of the task card as sent when it has words (details_panel; every card sent before
            # 2026-11-09); only dynamically inserted panels are tracked in `present`. Without it, it goes under
            # 本轮请求, where it would have been.
            details = eid == "task_details"
            if eid in present or (details and getattr(job.progress, "details_panel", True)):
                result = self._write(ctx, job, "PUT", "elements/" + eid, element_body(element), eid)
            else:
                if details:
                    before, after = "", details_anchor(job.progress)
                result = self._write(ctx, job, "POST", "elements", insert_body([element], before or after, bool(before)),
                                     eid)
            if result is not None:
                present.add(eid)
                job.interactive_elements = present
                note_element(job.progress, element)
                if details and not getattr(job.progress, "details_panel", True):
                    # Its text by its own ID from now on, as a card sent with it (live_details).
                    job.progress.details_panel = job.progress.live_details = True
                    job.progress.details_text = element["elements"][0]["content"]
            else:
                self._write_failed(job, f"Failed to update task element: {eid}")
            return result is not None


    def finish_result_controls(self, ctx, job, status):
        """A failed task card's 技术详情 and next steps (app/auto_retry.py failed_controls); a policy stop's
        (cancelled) next steps too."""
        if status not in ("failed", "cancelled"):
            return
        record = find_job_record(ctx.state_dir, job.message_id) or {}
        if status == "cancelled" and not record.get("policy_stop"):
            return
        elements = auto_retry.failed_controls(job.topic_id, job.job_id, record)
        if not elements:
            return
        with card_lock(job.progress):
            if self._write(ctx, job, "POST", "elements", insert_body(elements, under_body(job.progress)),
                           "result-actions") is not None:
                for element in elements:
                    note_element(job.progress, element)
    # Parts of Cards in their own modules: what other components call stays a Cards method.

    def trace_updates(self, job):
        return self.details.trace_updates(job)

    def queue_trace(self, ctx, job, urgent=False):
        return self.details.queue_trace(ctx, job, urgent)

    def finalize_trace(self, ctx, job, stopped=False):
        return self.details.finalize_trace(ctx, job, stopped)

    def update_record_panel(self, ctx, chat, mid, event, record, panel):
        return self.records.update_record_panel(ctx, chat, mid, event, record, panel)

    def repaint_browse_card(self, ctx, chat, mid, source, card):
        return self.records.repaint_browse_card(ctx, chat, mid, source, card)

    def update_record_elements(self, ctx, record, elements):
        return self.records.update_record_elements(ctx, record, elements)

    def set_record_summary(self, ctx, record, summary):
        return self.records.set_record_summary(ctx, record, summary)

    def set_record_activity(self, ctx, record, content):
        return self.records.set_record_activity(ctx, record, content)

    def replace_record_card(self, ctx, record, card):
        return self.records.replace_record_card(ctx, record, card)
