"""Durable questions: the Questions component, answers and delivery recovery.

Questions is the one API for a Harness's questions. Its parts are components that call each other
through the instance's parts (``questions.native``, ``.checkpoints``, ``.display``) and its public
methods through the instance, so an override there (tests do) still intercepts them: native
questions in native_questions.py, checkpoint questions in checkpoint_questions.py, question cards
in question_display.py. What an answer form means and the message an answer becomes are in
answers.py; the messages held while questions wait are HeldNotes (held_notes.py)."""

from __future__ import annotations

import copy
import dataclasses
import functools
import json
import time
from pathlib import Path

from covey.core.checkpoint import read_checkpoint, release_user_wait
from covey.app import butler_reactions, device_files, project_context
from covey.app.card_lanes import after_ack
from covey.core.turn_words import retry_hint
from covey.app.outbox import AUDIT_REFUSED_NOTE
from covey.core.jobs import find_job_record, transition_job_record, upsert_job_record, list_job_records
from covey.core import background, triage
from covey.core.locks import resource_lock
from covey.core.providers import retired_provider, retired_provider_notice
from covey.core.run_dir import run_dir, runner_alive
from covey.cards.elements import md
from covey.cards.question_cards import (FOLDED, SET_ASIDE, SET_ASIDE_ANSWER, SET_ASIDE_HELD, SET_ASIDE_SETTLED,
                                        answer_notice_card, aside_offered, folded, folded_refusal)
from covey.app.answers import (ATTACHMENTS, answer_message, answer_title, open_id_of, question_feedback_fields,
                               set_aside_answers, set_aside_message, submitted_answer)
from covey.app.checkpoint_questions import CheckpointQuestions
from covey.app.held_notes import HeldNotes
from covey.app.native_questions import NativeQuestions, turn_output
from covey.app.question_display import QuestionDisplay, question_card_lock
from covey.app.triage import Triage
from covey.core.state import write_private_text
from covey.core.history_storage import append_history
from covey.core.store import directory_store, transition_question, update_question
from covey.core.statuses import NOT_ENDED, OPEN_QUESTIONS, QUESTION_TRANSITIONS, TERMINAL
from covey.app.detached import live_keys
from covey.app.runner_ipc import forward_answer


def question_path(ctx, token):
    """The record file a click's token names; ValueError for a bad token, before any lock is taken on it."""
    if len(token) != 16 or any(c not in '0123456789abcdef' for c in token):
        raise ValueError('问题标识无效')
    return ctx.state_dir / 'questions' / (token + '.json')


def scoped_question(ctx, chat, token):
    """The question record a click in this topic names; ValueError for a bad token or another topic's."""
    record = json.loads(question_path(ctx, token).read_text())
    if record['topic_id'] != ctx.topic_id or record['chat_id'] != chat:
        raise ValueError('问题不属于当前话题')
    return record


class QuestionFolded(ValueError):
    """An answer to a question folded under a newer one of its topic: nothing is sent."""


class Questions:
    """One Harness's questions to the user: a backend's native questions and a checkpoint's
    question, their standalone cards, the answers (and feedback) submitted from them, and the
    messages held while several questions wait (``notes``, a HeldNotes). Native questions
    (``native``), checkpoint questions (``checkpoints``) and the question cards (``display``) are
    components behind the methods here that name them.

    - ``harness``: whose questions (records name it) and the voice of their cards.
    - ``runtime``: the live jobs an answer goes to, and the log.
    - ``messenger``: question cards, answer notices and held-note cards.
    - ``cards``: the task card's question element and the record panels redrawn with it.
    - ``enqueue_chat_job``: submits the job that carries an answer on, as a topic message would be
      (late-bound: submitting a job acknowledges the answered question, so it sits above Questions).
    """

    def __init__(self, harness, *, runtime, messenger, cards, enqueue_chat_job) -> None:
        self.harness = harness
        self.runtime = runtime
        self.messenger = messenger
        self.voice = harness.voice
        self.cards = cards
        self.enqueue_chat_job = enqueue_chat_job
        self.answering = set()  # Questions whose saved answer is being queued after its click was acknowledged.
        self.notes = HeldNotes(harness, messenger=messenger, cards=cards, questions=self)
        self.display = QuestionDisplay(harness, runtime=runtime, messenger=messenger, cards=cards, questions=self)
        self.native = NativeQuestions(harness, runtime=runtime, cards=cards, questions=self)
        self.checkpoints = CheckpointQuestions(runtime=runtime, cards=cards, questions=self)
        self.triage = Triage(runtime=runtime, cards=cards)  # A project's topic's choice question goes to the lead first (app/triage.py).

    def reconcile_checkpoint_questions(self, ctx, chat=None, *, backend=None):
        return self.checkpoints.reconcile(ctx, chat, backend=backend)

    def defer_checkpoint_question(self, ctx, job, questions):
        return self.checkpoints.defer(ctx, job, questions)

    def checkpoint_result_delivered(self, ctx, record):
        return self.checkpoints.result_delivered(ctx, record)

    def dispatch_question_event(self, ctx, job, kind, data, rpc):
        return self.native.dispatch(ctx, job, kind, data, rpc)

    def retry_live_questions(self, ctx, chat_id, job):
        return self.native.retry_live(ctx, chat_id, job)

    def show_handed_on(self, ctx, job, record):
        """A live question of ``job`` the project's lead handed on to the owner, shown by this process (app/triage.py
        show_here; a task runner's inbox message runner_ipc.SHOW_QUESTION). Whether its card went out."""
        from covey.app.triage import show_here

        def written():
            with job.trace_lock:
                return turn_output(job)
        return show_here(self, ctx, job, record, written)

    def deliver_checkpoint_questions(self, ctx, job, use_result_card=False):
        return self.checkpoints.deliver(ctx, job, use_result_card)

    def has_native_questions(self, ctx, job_id):
        return bool(directory_store(ctx.state_dir / "questions").query(job_id=job_id))

    def has_open_native_questions(self, ctx, job_id):
        """The job ``job_id`` asked a native question (the backend's own, not a checkpoint question) that
        is not settled yet (core/statuses.py ``OPEN_QUESTIONS``). Answered ones do not count: answered
        AskUserQuestion forms must not keep a turn-end checkpoint question from being asked."""
        rows = directory_store(ctx.state_dir / "questions").query(job_id=job_id)
        return any(row.get("status", "pending") in OPEN_QUESTIONS
                   and not str(row.get("request_id") or "").startswith("checkpoint-") for _, row in rows)

    def pending_questions(self, ctx, chat):
        rows = directory_store(ctx.state_dir / "questions").query(topic_id=ctx.topic_id, chat_id=chat, status="pending")
        return sorted((row for _, row in rows if not row.get("display_pending") and (not row.get("body_delivery_required") or
            (row.get("body_delivery_confirmed") and self.checkpoints.result_delivered(ctx, row)))), key=lambda r: r.get("created_at", 0))

    def answer_in_flight(self, ctx, chat):
        """An answer of this topic is being sent: saved, its continuation or native answer not settled."""
        return bool(directory_store(ctx.state_dir / "questions").query(topic_id=ctx.topic_id, chat_id=chat,
                                                                       status="submitting"))

    def paint_question_state(self, ctx, record):
        return self.display.paint(ctx, record)

    def fold_question(self, ctx, path, newer, job):
        """Fold the pending question at ``path`` under ``newer``, a later question of its topic whose card
        is shown (fold_older_questions): its card says so and points there. ``job``, the record of the
        task that asked, goes on as continued if it still waits for the answer, as reconcile does for a
        duplicate. Whether it was folded (False: an answer or a stop changed it first)."""
        with question_card_lock(path):  # A click's answer or a paint of this card that came first, first.
            record = transition_question(path, {"pending"}, status="superseded", superseded_by=newer["token"],
                                         superseded_reason=FOLDED, superseded_by_job=newer["job_id"])
            if record is None:
                return False
            self.paint_question_state(ctx, record)  # Nothing for one never shown (display_pending).
        self.runtime.log(f"question folded token={record['token']} job=#{record['job_id']} by=#{newer['job_id']}")
        said = f"这组问题已收起：后面 #{newer['job_id']} 又提了问题"
        if (job and transition_job_record(ctx.state_dir, job["key"], {"waiting_user"}, status="continued",
                                          status_message=said, superseded_question_by=newer["token"])
                and job.get("card_id")):
            try:
                self.cards.update_record_panel(ctx, record["chat_id"], "question-folded-" + record["token"],
                    {"message_id": job.get("card_message_id", "")}, job,
                    md(said + "。", element_id=self.cards.stream.element_id))
            except Exception as exc:  # noqa: BLE001 - the question's own card says it
                self.runtime.log(f"Folded question task display failed: {type(exc).__name__}")
        return True

    def submit_answer(self, ctx, chat, token, fields, event_id, *, top="", by=None):
        """``fields`` as the answer to question ``token``: into the task that asks it when it still runs, else as its
        continuation, queued after the click is acknowledged. ``top``: the message the answer was typed in, quoted from
        outside the topic's thread (app/butler_reactions.py top_message): DONE on it once the running task took the
        answer, else the continuation's reactions. ``by``: who submitted it, when known: the open_id of a card's click, or
        an AnswerBy (app/answers.py), of which its open_id is read: only the owner's own submit writes the rule its extra
        question proposes (app/question_dedup.py settle)."""
        by = open_id_of(by)
        if forward_answer(self.runtime, ctx, chat, token, fields, event_id, by=by):
            self._took(ctx, top)  # The task runner that asked the question took the answer.
            return None
        return self._submit_answer(ctx, chat, token, fields, event_id, **({"top": top} if top else {}), by=by)

    def _took(self, ctx, top):
        """DONE on ``top`` (none: nothing) for an answer or note the running task took. Never raises."""
        if top:
            butler_reactions.top_took(self.messenger, ctx.profile, top, self.runtime.log)

    def submit_question_feedback(self, ctx, chat, token, text, event_id, attachments="", *, top="", by=None):
        record = scoped_question(ctx, chat, token)
        fields = question_feedback_fields(record["questions"], text)
        if attachments:
            fields[ATTACHMENTS] = attachments
        return self.submit_answer(ctx, chat, token, fields, event_id, **({"top": top} if top else {}), by=by)

    def resume_saved_answer(self, ctx, chat, token, mid):
        """核查已保存回答 on a question whose saved answer's delivery is uncertain: submitted again once no
        task of the topic runs, or marked answered when its continuation was queued after all."""
        path = question_path(ctx, token)
        # The card lock across both steps: the fold of older questions takes it too, so it never finds
        # this answer pending between being written back and resubmitted.
        with question_card_lock(path):
            with resource_lock("question", path):
                record = scoped_question(ctx, chat, token)
                if record['status'] != 'uncertain' or not record.get('answers'):
                    raise ValueError('该回答无需恢复，或已进入后续任务')
                if self.runtime.jobs.live_job(ctx.topic_id, chat):
                    raise ValueError('本话题仍有任务运行，请等待结束后核查')
                # Refused before the record turns pending: a pending answer no one can submit holds the topic.
                if record.get('native_live_question') is True:
                    raise ValueError('原生问题已失去原执行上下文')  # Its task has ended; only that task could take it.
                queued = find_job_record(ctx.state_dir, 'answer-' + token)
                if not queued:
                    self.require_delivered_question(ctx, record)
                    update_question(path, lambda current: {**current, 'status': 'pending', 'reconcile_saved': True})
                    fields = {f'answer_{i}':'；'.join(record['answers'][q['id']]['answers']) for i,q in enumerate(record['questions'])}
            if queued:
                # Its continuation was queued after all (a task runner stopped waiting for the Bridge's
                # receipt): that task has the answer. Another under the same key would share its record.
                answered = transition_question(path, {'uncertain'}, status='answered', delivery_state='queued',
                                               continuation_job_id=queued.get('job_id'))
                if answered:
                    self.paint_question_state(ctx, answered)
                    notes = self.notes
                    taken = [n for n in notes.held_notes(ctx, chat, 'attached')
                             if n.get('answer_token') == token and not n.get('job_id')]
                    notes.settle_notes(ctx, chat, taken, queued.get('job_id'))
                return
            self.submit_answer(ctx, chat, token, fields, mid)

    def require_delivered_question(self, ctx, record):
        if record.get("display_pending") or (record.get("body_delivery_required") and (
                not record.get("body_delivery_confirmed") or not self.checkpoints.result_delivered(ctx, record))):
            raise ValueError(f"完整结果或问题控件尚未送达，请先{retry_hint()} 重新投递，不能提前确认")

    def owner_submitted(self, by) -> bool:
        """Whether ``by`` (an open_id, None when not known) is the owner's: app/access.py is_owner. Only such an answer is
        noted as the owner's decision (app/project_context.py remember_answer)."""
        access = getattr(self.runtime, "access", None)
        try:
            return bool(by) and access is not None and bool(access.is_owner(by))
        except Exception:  # noqa: BLE001 - not known to be his
            return False

    def _submit_answer(self, ctx, chat, token, fields, event_id, *, top="", by=None):
        path = question_path(ctx, token)
        # The card lock first: an answer clicked while its form is still being recorded waits for it.
        with question_card_lock(path), resource_lock("question", path):
            record = scoped_question(ctx, chat, token)
            if record["status"] == "cancelled":
                raise ValueError("该问题已取消；如需继续，请在原话题提出新的要求")
            if folded(record):
                raise QuestionFolded(folded_refusal(record))
            self.require_delivered_question(ctx, record)
            if record["status"] == "submitting" and token in self.answering:
                raise ValueError("回答已提交，正在创建后续任务；不会重复发送")
            if record["status"] in {"submitting", "uncertain"}:
                raise ValueError("上次回答已保存，但送达状态未确认；请先核查任务，不会重复发送")
            if record["status"] == SET_ASIDE:
                raise ValueError(SET_ASIDE_ANSWER)
            if record["status"] != "pending":
                raise ValueError("该问题已经提交，不能重复回答；可在话题中补充新的要求")
            if record.get("native_live_question") is True:
                active = self.runtime.jobs.by_id.get(record["job_id"])
                control = active.backend_control if active is not None else None
                if control is None:
                    raise ValueError("原生问题已失去原执行上下文")
                with control.control_lock:
                    self.native.live_control(ctx, active, record)
            elif self.runtime.jobs.live_job(ctx.topic_id, chat, record["job_id"]) is None:
                # The answer would start a task on the original job's model: one of a removed provider is
                # refused before anything is saved, so the question stays open until 换配置 cancels it.
                removed = retired_provider((self.harness.topic_record(ctx, chat, record["job_id"]) or {}).get("model"))
                if removed:
                    raise ValueError(retired_provider_notice(removed))
            answers, response = submitted_answer(record, fields)
            # Persist intent first. Never retry an ambiguous write as another task.
            record = transition_question(path, {"pending"}, status="submitting", answers=answers, answer_event=event_id,
                                         **response)
        project_context.remember_answer(ctx.state_dir, ctx.topic_id, record, self.runtime.log,  # A project's topic: a decision noted.
                                        owners=self.owner_submitted(by))
        try:
            return self._deliver_answer(ctx, chat, token, record, answers, event_id, path, **({"top": top} if top else {}))
        except Exception:
            self._answer_unconfirmed(ctx, path)
            raise

    def _answer_unconfirmed(self, ctx, path):
        uncertain = transition_question(path, {"submitting"}, status="uncertain")
        if uncertain:
            self.paint_question_state(ctx, uncertain)
            try:
                self.notes.answer_unconfirmed(ctx, uncertain["chat_id"], uncertain["token"])
            except Exception as exc:  # noqa: BLE001 - the question card already says it
                self.runtime.log(f"held notes not repainted: {type(exc).__name__}")

    def _deliver_answer(self, ctx, chat, token, record, answers, event_id, path, *, top=""):
        feedback = record.get("feedback", "") if record.get("response_kind") == "feedback" else ""
        original = self.harness.topic_record(ctx, chat, record['job_id']) or {}
        if original.get('checkpoint_path'):
            checkpoint = Path(original['checkpoint_path'])
            cp = read_checkpoint(checkpoint)
            cp['saved_user_answers'] = {'questions':record['questions'],'answers':answers,'delivery':'unconfirmed'}
            if feedback:
                cp["saved_user_answers"].update(response_kind="feedback", feedback=feedback)
            write_private_text(checkpoint, json.dumps(cp, ensure_ascii=False))
        self.paint_question_state(ctx, record)
        job = self.runtime.jobs.live_job(ctx.topic_id, chat, record["job_id"])
        if record.get("native_live_question") is True and job is None:
            raise ValueError("原生问题已失去原执行上下文，不会新建续答任务")
        if job:
            if job.cancel_event.is_set():
                # Stopped before its backend closed this question: the card painted 正在发送 above says
                # cancelled now, as cancel_questions would have (it skips a question no longer pending).
                with question_card_lock(path):
                    cancelled = transition_question(path, {"submitting"}, status="cancelled")
                    if cancelled:
                        self.paint_question_state(ctx, cancelled)
                raise ValueError("任务已停止，本次回答未发送")
            notes = self.notes.claim_held(ctx, chat, token, "feedback" if feedback else "answer")
            # `shown` is what the user wrote, for cards; `text` adds the attachments' local paths for the model.
            shown, text, delivered = answer_message(record, answers, feedback, notes)
            sent = text
            if getattr(job, "device", ""):  # Its attachments are put on the device first (app/device_files.py).
                sent, delivered = self._device_message(ctx, chat, job, token, record, text, delivered)
            if record.get("native_live_question") is True:
                control = job.backend_control
                with control.control_lock:
                    self.native.live_control(ctx, job, record)
                    record["delivery_state"] = "awaiting_ack"
                    transition_question(path, {"submitting"}, delivery_state="awaiting_ack")
                    control.answer(record["request_id"], delivered)
            elif record["request_id"] in job.backend_control.user_requests:
                record["delivery_state"] = "awaiting_ack"
                transition_question(path, {"submitting"}, delivery_state="awaiting_ack")
                job.backend_control.answer(record["request_id"], delivered)
            else:
                job.backend_control.steer(sent, event_id)
                record["delivery_state"] = "delivered"
            if record["blocking"]:
                release_user_wait(Path(job.checkpoint_path))
            append_history(ctx.history_path, "user", text)
        else:
            from covey.core.model_settings import recorded_work_mode, strict_recorded_model_settings

            model, effort = strict_recorded_model_settings(original, native_default_model=self.harness.spec.native_default_model)
            ctx = dataclasses.replace(ctx, model=model, effort=effort, work_mode=recorded_work_mode(original))
            prior = read_checkpoint(Path(original["checkpoint_path"])) if original.get("checkpoint_path") else None
            if original.get("paused_question_delivery") is not None:
                prior = {**(prior or {}), "native_question_continuation": {"token": token, "source_key": original["key"]}}
            title = answer_title(record, answers, feedback)
            # A retry on an older model after a safety refusal goes on as one: its subagents on that model, and
            # never retried again (app/policy_retry.py).
            retried = original.get("policy_retry") if isinstance(original.get("policy_retry"), dict) else None
            # The answered task goes on as the task of whoever sent its message (app/tool_limits.py READ).
            sender = str(original.get("sender_id") or "")
            # The answer is saved and its card says so: the click is acknowledged. The new task's card
            # and the notices take seconds; they follow, in this topic's order.
            self.answering.add(token)
            after_ack(functools.partial(self._queue_continuation, ctx, chat, token, record, path, answers, feedback,
                                        prior, title, retried, sender, **({"top": top} if top else {})))
            return
        self._commit_answer(ctx, chat, token, record, path, notes, feedback)
        self._took(ctx, top)  # The running task has the answer.

    def _device_message(self, ctx, chat, job, token, record, text, delivered):
        """(text, delivered) of an answer to a running task on a device with the files they name put there: the answer
        text, and the riders on the last answer of a native question (the attachments, the 补充说明 and the held notes)."""
        say = lambda line: self.messenger.reply(ctx, chat, line, self.voice.key("device-answer-files", token),  # noqa: E731
                                                voice=self.voice)

        def stage(value):
            return device_files.stage_message(ctx, job, value, log=self.runtime.log, say=say)
        text = stage(text)
        if delivered is not None and record.get("questions"):
            last = record["questions"][-1]["id"]
            given = (delivered.get(last) or {}).get("answers")
            if isinstance(given, list):
                delivered = {**delivered, last: {**delivered[last], "answers": [stage(item) if isinstance(item, str) else item
                                                                                  for item in given]}}
        return text, delivered

    def _queue_continuation(self, ctx, chat, token, record, path, answers, feedback, prior, title, policy_retry=None,
                            sender_id="", *, top=""):
        try:
            # The held notes as they are now: one withdrawn or kept for the next round since the click stays out.
            notes = self.notes.claim_held(ctx, chat, token, "feedback" if feedback else "answer",
                                          again=bool(record.get('reconcile_saved')))
            shown, text, _ = answer_message(record, answers, feedback, notes)
            append_history(ctx.history_path, "user", text)
            if record.get('reconcile_saved'):
                text = '上次回答送达状态未确认。先核查后端会话、已有产物和外部操作是否已完成，避免重复执行。\n' + text
            continuation = self.enqueue_continuation(ctx, text, chat, "answer-" + token, prior, title=title,
                                                     display=shown, policy_retry=policy_retry, sender_id=sender_id,
                                                     **({"top": top} if top else {}))
            if continuation is None:
                raise ValueError("回答已保存，但后续任务未能入队；请检查模型配置后再核查提交状态")
            record["continuation_job_id"] = continuation.job_id
            record["delivery_state"] = "queued"
            self._commit_answer(ctx, chat, token, record, path, notes, feedback)
        except Exception:
            self._answer_unconfirmed(ctx, path)  # It may have been queued: never sent again by itself.
            raise
        finally:
            self.answering.discard(token)

    def _commit_answer(self, ctx, chat, token, record, path, notes, feedback):
        with question_card_lock(path):  # A repeated click repaints from the record after this paint, not before.
            committed = transition_question(path, {"submitting"}, status="answered",
                delivery_state=record["delivery_state"], **({"continuation_job_id": record["continuation_job_id"]} if "continuation_job_id" in record else {}))
            if committed:
                self.paint_question_state(ctx, committed)
        if committed:
            self.reconcile_checkpoint_questions(ctx, chat)
        if record.get("answer_notice_message_id"):
            # The earlier "not submitted yet" card now says the answer went through.
            notice = "问题反馈已提交，原问题未获回答或授权" if feedback else "回答已提交"
            self.cards.repaint_browse_card(ctx, chat, f"answer-notice-done-{token}", record["answer_notice_message_id"],
                                     answer_notice_card(f"✓ **{notice}**（{time.strftime('%H:%M')}）"))
        try:
            self.notes.settle_notes(ctx, chat, notes, record.get("continuation_job_id") or record["job_id"],
                              response_kind="feedback" if feedback else "answer")
        except Exception as exc:  # noqa: BLE001 - the answer itself is already delivered
            self.runtime.log(f"held notes not settled: {type(exc).__name__}")

    def show_answer_notice(self, ctx, chat, token, text, mid):
        return self.display.show_answer_notice(ctx, chat, token, text, mid)

    def queue_ask(self, ctx, chat, shown, text, message_id, *, title="", prior=None, sender=""):
        """A 先问一句 in ctx's topic, from a 代发草稿 card (app/owner_send.py): ``text`` (what the model
        reads; ``shown`` the owner's words, which its card shows) written to the topic's history, then queued the way a
        message of the topic is (app/submission.py enqueue_chat_job with ``steer``): into the task the topic runs, as
        a supplement, when it takes one, else as the topic's next turn. ``message_id`` starts with ASK_PREFIX, so the
        turn does not take the task that waits for an answer for one that went on (app/task_controls.py). ``prior``: the
        checkpoint it goes on from. The job, or None when it was not queued (the topic was told why)."""
        append_history(ctx.history_path, "user", text, {"message_id": message_id})
        return self.enqueue_chat_job(ctx, shown, chat, message_id, prompt_content=text, recovery_context=prior,
                                     title=title, sender_id=sender, steer=True)

    def set_aside(self, ctx, chat, token, event_id, *, by=""):
        """不处理 on question ``token``'s form (T604 #15): every question 不回答此题 with nothing added (as a 补充说明 alone
        would skip them, without it), saved and nothing sent; the record turns SET_ASIDE, so it leaves 等你 everywhere (core/board_read.py
        asks reads pending ones only) and a message of the topic is no answer to it. The topic's next turn takes it as its
        answer (``take_set_aside``); until then ``restore`` (撤销) opens the form again. Its task waits as it did.
        ValueError when it can no longer be set aside; False when it already is (the same click again)."""
        path = question_path(ctx, token)
        with question_card_lock(path):
            record = scoped_question(ctx, chat, token)
            if folded(record):
                raise QuestionFolded(folded_refusal(record))
            if record.get("status") == SET_ASIDE:
                return False
            if record.get("status") != "pending":
                raise ValueError(SET_ASIDE_SETTLED)
            if not aside_offered(record):
                raise ValueError(SET_ASIDE_HELD)
            self.require_delivered_question(ctx, record)
            record = transition_question(path, {"pending"}, status=SET_ASIDE, response_kind=SET_ASIDE,
                                         answers=set_aside_answers(record["questions"]), set_aside_at=time.time(),
                                         set_aside_by=by, set_aside_event=event_id)
            if record is None:
                raise ValueError(SET_ASIDE_SETTLED)
            self.paint_question_state(ctx, record)
        self.runtime.log(f"question set aside token={token[:10]} job=#{record.get('job_id')}")
        return True

    def restore(self, ctx, chat, token, event_id):
        """撤销 on a question set aside (``set_aside``): pending again, its form open, as it was before; refused once the
        topic's next turn took it (the model has it then) or it was cancelled. False when it is pending already."""
        path = question_path(ctx, token)

        def back(current):
            for key in ("answers", "response_kind", "set_aside_at", "set_aside_by", "set_aside_event", "answered_at"):
                current.pop(key, None)
            return {**current, "status": "pending", "restored_at": time.time(), "restore_event": event_id}
        with question_card_lock(path):
            record = scoped_question(ctx, chat, token)
            if record.get("status") == "pending":
                return False
            restored = update_question(path, back, expected={SET_ASIDE}) if record.get("status") == SET_ASIDE else None
            if restored is None:
                current = scoped_question(ctx, chat, token)
                self.paint_question_state(ctx, current)  # Shows where it went instead.
                taken = current.get("continuation_job_id") if current.get("status") == "answered" else None
                raise ValueError(f"这组题已经随 #{taken} 告诉了模型，撤不回了；有话直接在话题里说" if taken
                                 else "这组问题已经取消了，撤不回；有话直接在话题里说")
            self.paint_question_state(ctx, restored)
        self.runtime.log(f"question set aside undone token={token[:10]} job=#{restored.get('job_id')}")
        return True

    def take_set_aside(self, ctx, job):
        """The topic's next turn takes the questions set aside there (不处理, ``set_aside``) as their answer: each record
        is answered by ``job`` (``continuation_job_id``) with its saved answers, and ``job``'s request starts with them
        (app/answers.py set_aside_message, the held notes too, as an answer takes them), so the model hears it once and 撤销
        ends. Called for every job a topic submits (app/task_controls.py acknowledge_waiting_answer, the topic's lock held):
        records only here, the cards painted on a thread of their own. The Bridge's alone. The records taken."""
        if self.runtime.in_runner:
            return []
        topic, chat = getattr(job, "topic_id", "") or ctx.topic_id, job.chat_id
        rows = directory_store(ctx.state_dir / "questions").query(topic_id=topic, chat_id=chat, status=SET_ASIDE)
        taken = []
        for path, _ in sorted(rows, key=lambda row: row[1].get("created_at") or 0):
            record = transition_question(path, {SET_ASIDE}, status="answered", delivery_state="delivered",
                                         continuation_job_id=job.job_id)
            if record is not None:
                taken.append(record)
        if not taken:
            return []
        scoped = dataclasses.replace(ctx, topic_id=topic)
        notes = self.notes.claim_held(scoped, chat, taken[-1]["token"], "answer")
        text = set_aside_message(taken, notes)
        job.prompt_text = text + "\n\n" + (job.prompt_text or job.user_text)
        append_history(ctx.history_path, "user", text)
        self.runtime.log(f"questions set aside taken by job=#{job.job_id}: {','.join(r['token'][:10] for r in taken)}")
        background.start(background.CARD_UPDATES, self._show_taken, scoped, chat, taken, notes, job.job_id)
        return taken

    def _show_taken(self, ctx, chat, taken, notes, job_id):
        """``take_set_aside``'s cards: each question's, saying the turn took it, and the held notes it took. Never raises."""
        for record in taken:
            try:
                with question_card_lock(question_path(ctx, record["token"])):
                    self.paint_question_state(ctx, scoped_question(ctx, chat, record["token"]))
            except Exception as exc:  # noqa: BLE001 - the record says where it went
                self.runtime.log(f"set aside question not repainted: {type(exc).__name__}")
        try:
            self.notes.settle_notes(ctx, chat, notes, job_id)
        except Exception as exc:  # noqa: BLE001 - the notes went with the turn
            self.runtime.log(f"held notes not settled: {type(exc).__name__}")

    def enqueue_continuation(self, ctx, text, chat, message_id, recovery_context, title="", display="",
                             policy_retry=None, sender_id="", top=""):
        """Queue the job that carries an answer on; a task runner asks its bridge to do this.

        `display` is the request its card shows when `text` carries lines meant only for the model; `policy_retry`
        the answered job's mark of a retry after a safety refusal (core/jobs.py), which the continuation keeps, and
        `sender_id` who sent the answered job's message ('' none known). `top`: the message whose reactions say how it
        stands (app/butler_reactions.py top_message); the Bridge's alone (a task runner's answers are clicks)."""
        if self.runtime.runner is not None:
            return self.runtime.runner.enqueue(self.harness.spec.name, ctx, text, chat, message_id, recovery_context,
                                               title=title, display=display, policy_retry=policy_retry,
                                               sender_id=sender_id)
        return self.enqueue_chat_job(ctx, display or text, chat, message_id, prompt_content=text,
                                     recovery_context=recovery_context, title=title, policy_retry=policy_retry,
                                     sender_id=sender_id, **({"top": top} if top else {}))

    def retire_orphaned_live_questions(self, ctx, chat):
        """The topic's pending live questions (``native_live_question``) whose task is gone: no task runner or job of this
        Bridge has it and its record is not running. Nothing can answer one any more (only that task held the backend
        control) and it would hold every later message of the topic, so it is cancelled. A task that ended before its
        backend's cancel_questions reached a question it never showed left one (T488 #5528, T620). The ones cancelled."""
        if self.runtime.in_runner:
            return []
        retired = []
        for path, row in directory_store(ctx.state_dir / "questions").query(topic_id=ctx.topic_id, chat_id=chat,
                                                                            status="pending"):
            if row.get("native_live_question") is not True or triage.held(row):
                continue
            key = str(row.get("job_key") or "")
            if not key or _still_running(self.runtime, ctx.state_dir, key, find_job_record(ctx.state_dir, key)):
                continue
            with question_card_lock(path):
                record = transition_question(path, {"pending"}, status="cancelled", cancelled_reason="task_ended")
            if record:
                self.paint_question_state(ctx, record)  # Nothing for one never shown.
                self.runtime.log(f"orphaned live question cancelled token={record['token'][:10]} job=#{record.get('job_id')}")
                retired.append(record)
        return retired

    def undelivered_checkpoint_jobs(self, ctx, chat):
        return self.checkpoints.undelivered_jobs(ctx, chat)

    def hold_note(self, ctx, chat, text, mid, pending, attachments="", *, top=""):
        return self.notes.hold_note(ctx, chat, text, mid, pending, attachments=attachments,
                                    **({"top": top} if top else {}))

    def note_action(self, ctx, chat, raw, mid):
        return self.notes.note_action(ctx, chat, raw, mid)

    def publish_question_card(self, ctx, record, *, visible=None):
        return self.display.publish(ctx, record, visible=visible)

    def paint_standalone_question(self, ctx, record):
        return self.display.paint_standalone(ctx, record)

    def copy_card(self, ctx, record):
        """The top-level copy of ``record``'s card with its turn's 产物 (app/question_display.py), as a click redraws
        it."""
        return self.display.copy_card(ctx, record)

    def repaint_copy(self, ctx, token):
        """The top-level copy of the question ``token`` repainted as its record is now (app/artifacts.py ``copies``)."""
        return self.display.repaint_copy(ctx, token)

    def update_continuation_card(self, ctx, job, status):
        return self.display.update_continuation(ctx, job, status)


# The follow-up states a settled question's card names (cards/question_cards.py); a follow-up that
# went on past them (``continued``: it asked and was answered in turn) leaves the last one shown.
CONTINUATION_STATES = TERMINAL | {"queued", "running", "waiting_user"}


def _settle_question(path, change):
    """update_question on a record a restart left half-done: (before, after), or (None, None) for a
    record that is gone."""
    with resource_lock("question", path):
        try:
            before = json.loads(path.read_text())
        except (OSError, ValueError):
            return None, None
        return before, update_question(path, change)


def _unconfirmed(record):
    """A send the restart cut off before its receipt, on a record that may still turn uncertain:
    a question cancelled after its 提交回答 keeps that submit's awaiting_ack and stays cancelled."""
    return ((record.get("status") == "submitting" or record.get("delivery_state") == "awaiting_ack")
            and "uncertain" in QUESTION_TRANSITIONS.get(record.get("status", "pending"), ()))


def _lookup(jobs, field):
    """``match(value)``: the jobs of ``jobs`` whose ``field`` == value, in their order, from one pass over them instead
    of a scan of all of them for each question (1500 questions x 5900 jobs at a start, T648). An unhashable value (a
    malformed record) is looked for by that scan."""
    found = {}
    for job in jobs:
        try:
            found.setdefault(job.get(field), []).append(job)
        except TypeError:
            pass  # A list or a dict: equal to no hashable value; an unhashable one is scanned for below.

    def match(value):
        try:
            return found.get(value, ())
        except TypeError:
            return [job for job in jobs if job.get(field) == value]
    return match


def _in_scope(jobs, record):
    """The first of ``jobs`` in the topic and chat of the question ``record``, None when none is."""
    return next((j for j in jobs if j.get("topic_id") == record["topic_id"] and j.get("chat_id") == record["chat_id"]),
                None)


def recover_question_delivery(ctx, harness_questions, log):
    """Startup, before any job runs: settle the question records a restart left half-done
    (every Harness's: their records share the state directory), then let each Harness's
    Questions retire its own duplicate checkpoint questions. A card that still says 正在发送
    is repainted with what the record says now (a restart between the 提交回答 ack
    and its follow-up would leave 正在发送 on the card with no task behind it)."""
    # A job still running in its task runner keeps its questions; that runner settles them.
    live = live_keys(ctx.state_dir)
    jobs = list_job_records(ctx.state_dir, limit=0)
    by_key, by_message, by_number = (_lookup(jobs, field) for field in ("key", "message_id", "job_id"))
    settled = []
    for job in jobs:
        if job.get("key") in live:
            continue
        if (job.get("checkpoint_question_deferred") and job.get("checkpoint_question_registration_pending")
                and not job.get("checkpoint_question_stopped")
                and job.get("status") not in {"continued", "cancelled", "done"}):
            upsert_job_record(ctx.state_dir, job["key"], status="failed", question_delivery_ok=False,
                checkpoint_question_error=job.get("checkpoint_question_error") or "问题登记尚未完成",
                status_message=f"问题登记尚未完成；{retry_hint()} 先重投完整结果，再恢复问题控件")
    for path, record in directory_store(ctx.state_dir / "questions").query():
        if record.get("job_key") in live:
            continue
        if record.get("body_delivery_required") and record.get("display_pending") and not triage.held(record):
            original = _in_scope(by_key(record.get("job_key")), record)
            if original and original.get("execution_status") == "waiting_user":
                # Restart does not publish a form or assume an interrupted send succeeded.
                upsert_job_record(ctx.state_dir, original["key"], status="failed", question_delivery_ok=False,
                    status_message=AUDIT_REFUSED_NOTE if original.get("delivery_error") == "audit_rejected"
                    else f"完整结果或问题控件尚未送达；{retry_hint()} 可重新投递")
        continuation = _in_scope(by_message('answer-' + record['token']), record) if jobs else None
        if continuation and record.get('status') in {'submitting','uncertain','answered'}:
            def answered(current, job=continuation):
                if current.get('status') not in {'submitting', 'uncertain', 'answered'}:
                    return current
                current.update(status='answered', continuation_job_id=job['job_id'])
                if job['status'] in CONTINUATION_STATES:
                    current['delivery_state'] = job['status']
                return current
            if answered(copy.deepcopy(record)) == record:
                # Already as the follow-up says (almost every answered question at every start): the settle below
                # would write nothing and settle nothing, only lock and read the file twice, ~12 ms each on NAS (T648).
                # The store has just caught up with the directory, and while the Bridge starts nothing else writes a
                # question of a job without a live runner.
                continue
            before, after = _settle_question(path, answered)
            if before and before.get('status') != 'answered' and after.get('status') == 'answered':
                settled.append(after)
            continue
        if _unconfirmed(record):
            def unconfirmed(current):
                if _unconfirmed(current):
                    current["status"] = "uncertain"
                return current
            before, after = _settle_question(path, unconfirmed)
            if before and before.get("status") != "uncertain" and after.get("status") == "uncertain":
                settled.append(after)
                log(f"question answer requires reconciliation token={record['token']}")
        elif record.get('status') == 'pending':
            _settle_question(path, lambda current: {**current, 'restarted': True}
                             if current.get('status') == 'pending' else current)
    for questions in harness_questions:
        questions.reconcile_checkpoint_questions(ctx, backend=questions.harness.name)
    owners = {questions.harness.name: questions for questions in harness_questions}
    for record in settled:
        job = _in_scope(by_number(record.get('job_id')), record)
        questions = owners.get((job or {}).get('backend'))
        if questions is None:
            continue  # A Harness no longer in the roster: its cards are retired.
        scoped = dataclasses.replace(ctx, topic_id=record['topic_id'])
        questions.paint_question_state(scoped, record)  # 送达未确认 and its 核查 button, or the answer's task.
        if record['status'] == 'uncertain':
            try:
                questions.notes.answer_unconfirmed(scoped, record['chat_id'], record['token'])
            except Exception as exc:  # noqa: BLE001 - one note card never blocks startup
                log(f"held notes not repainted: {type(exc).__name__}")
    fold_older_questions(ctx, harness_questions)


# The questions whose card was shown that fold the older ones of their topic (not cancelled: its task stopped).
SHOWN = frozenset({"pending", "submitting", "uncertain", "answered"})


def _order(record):
    """Which of a topic's questions came later: the task's number, then when it asked."""
    return int(record.get("job_id") or 0), record.get("created_at") or 0


def _shown(record):
    """The question's form went out: on its card, and after its result where the result comes first."""
    return (record.get("status") in SHOWN and not record.get("display_pending")
            and (not record.get("body_delivery_required") or record.get("body_delivery_confirmed") is True)
            and bool(record.get("question_message_id") or record.get("card_id")))


def _still_running(runtime, state_dir, key, job):
    """The task that asked has not ended: its question may be the one it waits on (a live native question
    folded would leave it waiting for ever), and while its task runner lives, its cards are that runner's."""
    with runtime.jobs.lock:
        if key in runtime.jobs.by_message_id:
            return True
    return (job or {}).get("status") in NOT_ENDED - {"waiting_user"} or runner_alive(run_dir(state_dir, key))


def fold_older_questions(ctx, harness_questions, *, topic=None):
    """Once a newer question card of a topic is shown, fold every older question there that still waits
    (pending), so the one card left open is the one to answer: an older card left open is answered
    by mistake, and the newer questions then hold the owner's next message for hours. Not a question whose task is still on (``_still_running``), nor one
    being answered (submitting, uncertain); one never shown folds without a card.

    The Bridge does it, never a task runner: it redraws other tasks' cards (rule 8). It runs at startup,
    in the outbox loop (a card shown by a task runner folds the older ones within a poll) and before a
    topic's message is routed. ``topic``: (topic_id, chat_id), to look at that topic only. A question is
    folded by the Questions of its task's Harness, and left while that one is not in ``harness_questions``."""
    owners = {questions.harness.name: questions for questions in harness_questions}
    runtime = next(iter(owners.values())).runtime if owners else None
    if runtime is None or runtime.in_runner:
        return
    store = directory_store(ctx.state_dir / "questions")
    scope = {"topic_id": topic[0], "chat_id": topic[1]} if topic else {}
    for topic_id, chat in sorted({(r["topic_id"], r["chat_id"]) for _, r in store.query(**scope, status="pending")}):
        try:
            _fold_in_topic(ctx, owners, runtime, store, topic_id, chat)
        except Exception as exc:  # noqa: BLE001 - never stops startup or a message; the next poll tries again
            runtime.log(f"question fold failed topic={topic_id[:12]}: {type(exc).__name__}")


def _fold_in_topic(ctx, owners, runtime, store, topic_id, chat):
    """fold_older_questions in one topic."""
    rows = store.query(topic_id=topic_id, chat_id=chat)
    newest = max((r for _, r in rows if _shown(r)), key=_order, default=None)
    older = sorted(((path, r) for path, r in rows if newest and r.get("status") == "pending"
                    and _order(r) < _order(newest)), key=lambda row: _order(row[1]))
    if not older:
        return
    jobs = {r.get("key"): r for _, r in directory_store(ctx.state_dir / "jobs").query(topic_id=topic_id, chat_id=chat)}
    scoped = dataclasses.replace(ctx, topic_id=topic_id)
    gone = {}  # By Harness: the Questions that folded some, and those questions' tokens and tasks.
    for path, record in older:
        job = jobs.get(record.get("job_key"))
        name = (job or {}).get("backend") or record.get("harness_id")
        if (name not in owners or not record.get("job_key")
                or _still_running(runtime, ctx.state_dir, record["job_key"], job)):
            continue
        if owners[name].fold_question(scoped, path, newest, job):
            gone.setdefault(name, (owners[name], {}))[1][record["token"]] = record["job_id"]
    for questions, tokens in gone.values():
        try:
            questions.notes.wait_instead(scoped, chat, tokens)
        except Exception as exc:  # noqa: BLE001 - the questions are folded; only a note card is not redrawn
            runtime.log(f"held notes not moved: {type(exc).__name__}")
