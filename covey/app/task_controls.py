"""Task-card stop and retry controls, and showing a task's recovery card (app/recovery_cards.py draws it); redelivering
a saved result or question is app/redelivery.py Redelivery, a part of TaskControls."""

from __future__ import annotations

import dataclasses
import functools
from pathlib import Path

from covey.core import placement, recovery, timing
from covey.core.main_thread import traits, worded
from covey.core.checkpoint import read_checkpoint
from covey.core.harness import BridgeContext
from covey.core.jobs import BridgeReplyJob as ReplyJob
from covey.core.jobs import carry_device, context_for_job, parse_job_stop_action_value, upsert_job_record
from covey.core.model_settings import recorded_work_mode, strict_recorded_model_settings
from covey.core.model_policy import retried_line
from covey.core.state import utc_now_iso
from covey.core.history_storage import recent_history
from covey.core.topics import RETIRED_TEXT as TOPIC_RETIRED, remember_topic_message, retired_topic
from covey.app.card_actions import RETIRED_TEXT, action_value
from covey.app.card_lanes import after_ack
from covey.cards.review import recent_user_questions
from covey.core.saved_results import has_saved_reply, load_authorized_reply
from covey.core.locks import LOCK, topic_lock
from covey.core.store import directory_store
from covey.app import auto_retry
from covey.app.recovery_cards import paint_answered, recovery_card
from covey.app.task_card import card_failure_notice
from covey.app.redelivery import Redelivery


RETRY_REFUSALS = {
    recovery.GUARDED: "本轮因官方拒绝或降级已停止。停止通知由后台重投，不会重新执行任务。请检查任务内容后提出调整后的要求。",
    recovery.POLICY_STOPPED: "本轮因官方拒绝或降级已停止，不会重跑原请求。请检查任务内容，在本话题提出调整后的要求。",
    recovery.NATIVE_LOST: "原生问题已失去原执行上下文；不会重新投递问题、发送答案或新建续答任务。",
    recovery.ANSWERED_ONCE: "该回答已提交过，不会重复执行；请检查已有任务状态。",
    recovery.WAIT_CANCELLED: "本轮等待已取消；如需继续，请在本话题发送新的要求。原始回复仍保存在本地记录中。",
}


# A record whose card a quiet main-thread turn's recovery card is, sent at the top level (TaskControls.show_recovery_card).
TOP_CARD = "card_top_level"


@dataclasses.dataclass
class _Marked:
    """A record as app/main_reactions.py takes a job."""
    record: dict

    @property
    def job_id(self):
        return self.record.get("job_id")

    @property
    def message_id(self):
        return self.record.get("message_id")

    @property
    def reaction_message(self):
        return self.record.get("reaction_message")


class TaskControls(Redelivery):
    """One Harness's task controls: the stop and retry buttons of a task card, retrying a
    failed task or redelivering its saved result or question, the recovery card of an
    interrupted task, and the acknowledgement of an answered task's card.

    - ``harness``: whose jobs (records name it) and the voice of the notices.
    - ``runtime``: the live and queued jobs and the scheduler's queue, and the log.
    - ``messenger``: notices and redelivered results.
    - ``cards``: task and record cards.
    - ``questions``: questions a redelivered result still owes, and live ones to show again.
    - ``artifacts``: a long result sent as a file.
    - ``models``: the effort preset a retried job runs with.
    - ``steering``: a stopped task's 补充 forms close with its controls.
    - ``record_job_state``: the Harness's job-state recorder (late-bound: it sits above).
    """

    def __init__(self, harness, *, runtime, messenger, cards, questions, artifacts, models, steering,
                 record_job_state) -> None:
        self.harness = harness
        self.runtime = runtime
        self.messenger = messenger
        self.voice = harness.voice
        self.cards = cards
        self.questions = questions
        self.artifacts = artifacts
        self.models = models
        self.steering = steering
        self.record_job_state = record_job_state

    def asked_card(self, ctx, record, card):
        """``card`` with the 你问 block of ``record`` on top when its card is one of a quiet main-thread turn's at the
        top level (``TOP_CARD``, ``show_recovery_card``): it quotes nothing, the block says what it answers."""
        if not record.get(TOP_CARD):
            return card
        from covey.app.answer_cards import asked
        try:
            block = asked(ctx.state_dir, record, profile=ctx.profile, log=self.runtime.log)
        except Exception as exc:  # noqa: BLE001 - the card without it
            self.runtime.log(f"recovery card 你问 unavailable: {type(exc).__name__}")
            block = None
        if block:
            card["body"]["elements"].insert(0, block)
        return card

    def repaint_recovery_card(self, ctx, record):
        """Redraw a recorded task's card as its recovery card, in place; whether CardKit took it."""
        return self.cards.replace_record_card(ctx, record, self.asked_card(ctx, record, recovery_card(
            record, state_dir=ctx.state_dir)))

    def show_recovery_card(self, ctx, record):
        """``record``'s card redrawn as its recovery card, else a new one. A quiet main-thread turn with no card yet
        (core/placement.py Row.quiet, D1 2026-11-08: 提问不开串) gets it at the top level, quoting nothing, with its
        你问 block (``TOP_CARD``), and the OnIt on the owner's message comes off (a resume puts it back,
        ``requeue``)."""
        card = self.asked_card(ctx, record, recovery_card(record, state_dir=ctx.state_dir))
        if self.cards.replace_record_card(ctx, record, card):
            return True
        top = not record.get("card_message_id") and placement.quiet(ctx.state_dir, record)
        if top:
            record = {**record, TOP_CARD: True}
            card = self.asked_card(ctx, record, recovery_card(record, state_dir=ctx.state_dir))
        card_id, outgoing = self.messenger.create_and_send_card(
            ctx.profile,
            record["chat_id"],
            card,
            self.voice.key("recover-card", record["key"]),
            reply_to_message_id="" if top else record.get("reply_to_message_id") or "",
            **({"in_thread": False} if top else {}),
            voice=self.voice,
        )
        if outgoing:
            remember_topic_message(ctx.state_dir, record["topic_id"], record["chat_id"], outgoing)
            upsert_job_record(ctx.state_dir, record["key"], card_id=card_id, card_message_id=outgoing,
                              **({TOP_CARD: True} if top else {}))
        if top and placement.marked(ctx.state_dir, record):
            from covey.app import main_reactions
            main_reactions.finish(self.messenger, ctx.profile, _Marked(record), "", self.runtime.log)
        return bool(outgoing)

    def report_unfinished(self, ctx, record, notice, key, *, keep_question_forms, restart=True):
        """Tell the user ``record``'s job ended unfinished: its card offers 检查并继续, or ``notice`` goes to its
        topic under ``key`` when no card could be written. ``restart``: it continues once by itself
        (auto_retry.schedule_restart), unless it has native questions. ``keep_question_forms``: a job with
        native questions gets nothing at all, so its durable question form stays usable after reconnect. The
        Bridge's start asks that; a runner's end still shows the recovery card (the owner kept both ways,
        2026-11-05)."""
        questions = (keep_question_forms or restart) and self.questions.has_native_questions(ctx, record["job_id"])
        if questions and keep_question_forms:
            return
        if restart and not questions:
            record = auto_retry.schedule_restart(ctx, record)
        if not self.show_recovery_card(ctx, record):
            self.messenger.send_message(ctx.profile, str(record.get("chat_id") or ""), notice, key,
                                        reply_to_message_id=str(record.get("reply_to_message_id") or ""), voice=self.voice)

    def task_action(self, ctx, event):
        """检查并继续 and 停止自动重试 on a task's card."""
        raw = action_value(event)
        if not ctx.topic_id or raw.get("topic_id") != ctx.topic_id:
            return True
        chat_id = str(event.get("chat_id") or "")
        event_id = str(event.get("event_id") or event.get("message_id") or "")
        try:
            job_id = int(raw.get("job_id") or 0)
        except (ValueError, TypeError):
            return True

        def reply(text):
            self.messenger.reply(ctx, chat_id, text, self.voice.key("retry-action", event_id), voice=self.voice)
        if raw["kind"] == auto_retry.CANCEL_KIND:
            text = self.stop_auto_retry(ctx, chat_id, job_id)
            after_ack(lambda: reply(text))
        else:
            # The new task's card, the old card's update and the reply took 3.5 s in the click, over
            # Feishu's 3 s (2026-10-26): all after the acknowledgement. A repeated click's follow-up
            # comes after this one and finds the task queued.
            after_ack(lambda: reply(self.retry_topic(ctx, chat_id, event_id, job_id=job_id)))
        return True

    def retired_action(self, ctx, event):
        """A button taken off the cards (app/card_actions.py RETIRED_TEXT) on a card sent before: says what
        replaces it, and does nothing else. Any click or message in a frozen front-desk topic (ctx's topic, core/topics.py
        retired_topic; app/topics.py refuse_retired) gets core/topics.py RETIRED_TEXT instead, as a message of its own
        at the chat's top level, quoting nothing (the topic has no thread to answer in)."""
        chat_id, event_id = str(event.get("chat_id") or ""), str(event.get("event_id") or event.get("message_id") or "")
        if retired_topic(ctx.state_dir, getattr(ctx, "topic_id", "")):
            if chat_id:
                self.messenger.send_message(ctx.profile, chat_id, TOPIC_RETIRED,
                                            self.voice.key("retired-topic", event_id), voice=self.voice)
            return True
        text = RETIRED_TEXT.get(action_value(event).get("kind"))
        if text and chat_id:
            self.messenger.reply(ctx, chat_id, text, self.voice.key("retired-button", event_id), voice=self.voice)
        return True

    def stop_auto_retry(self, ctx, chat_id, job_id):
        """停止自动重试 on a failed task card: cancel its pending automatic retry."""
        record = next((r for r in self.harness.topic_records(ctx, chat_id) if r.get("job_id") == job_id), None)
        if record is not None and auto_retry.cancel(self, ctx, record, "stopped"):
            return f"已停止任务 #{job_id} 的自动重试。需要时点任务卡上的“检查并继续”。"
        if (record or {}).get("auto_retry_state") in {"submitting", "queued"}:
            return "自动重试已经开始；要停止它，在新的任务卡上点“停止”。"
        return f"任务 #{job_id} 没有待执行的自动重试。"

    def acknowledge_waiting_answer(self, ctx, job):
        """``job`` continues the topic's task that waits for an answer: that task's record says so now,
        and ``job`` takes its checkpoint. Returns what repaints that task's card (None: nothing to
        paint), for the caller to run once ``job`` is queued: before it, its two writes held the
        continuation 1.2 s (S37)."""
        previous = next(iter(self.harness.topic_records(ctx, job.chat_id, status="waiting_user")), None)
        timing.mark("ack_query")
        if not previous:
            return None
        if job.recovery_context is None and previous.get("checkpoint_path"):
            job.recovery_context = read_checkpoint(Path(previous["checkpoint_path"]))
        upsert_job_record(
            ctx.state_dir,
            previous["key"],
            status="continued",
            status_message="已收到回答，后续任务继续处理",
            resumed_by=job.message_id,
        )
        timing.mark("ack_upsert")
        if not previous.get("card_id"):
            return None
        return functools.partial(self.paint_answered, ctx, job.chat_id, job.job_id, previous)

    def paint_answered(self, ctx, chat, job_id, previous):
        paint_answered(self.cards, self.runtime.log, ctx, chat, job_id, previous)

    def stop_action(self, ctx, event):
        """停止 on a running task's card; False when its value names no job (a retired card's click)."""
        action = parse_job_stop_action_value(event.get("action_value"))
        chat_id = str(event.get("chat_id") or "")
        if not (action and chat_id):
            return False
        self.cancel_reply_job_from_card(ctx, action, chat_id, str(event.get("event_id") or event.get("message_id") or ""))
        return True

    def cancel_reply_job_from_card(
        self,
        ctx: BridgeContext,
        action: dict[str, str],
        chat_id: str,
        message_id: str,
    ) -> bool:
        try:
            job_id = int(action.get("job_id") or "0")
        except ValueError:
            job_id = 0
        if job_id <= 0:
            return False
        job = self.runtime.jobs.find(job_id, action.get("message_id") or "")
        if job is None:
            self.messenger.reply(
                ctx,
                chat_id,
                f"任务 #{job_id} 当前不在运行或队列中，可能已经结束。",
                self.voice.key("job-stop-stale", message_id),
                voice=self.voice,
            )
            return True
        if job.chat_id != chat_id or (ctx.topic_id and job.topic_id != ctx.topic_id):
            self.runtime.log(f"ignored job stop action for mismatched chat job=#{job.job_id} chat={chat_id}")
            return True
        ctx = context_for_job(ctx, job)
        if job.cancel_event.is_set():
            self.messenger.reply(
                ctx,
                chat_id,
                f"任务 #{job.job_id} 已经收到停止请求，正在结束。",
                self.voice.key("job-stop-duplicate", message_id),
                voice=self.voice,
            )
            return True
        with job.execution_lock:
            queued_cancel = not job.execution_started
            if queued_cancel:
                job.cancelled_before_start = True
            job.cancel_event.set()
        if queued_cancel:
            self.record_job_state(ctx, job, "cancelled", "已取消排队，未执行")
            self.close_unstarted_job(ctx, job, "已取消排队", "本条消息不会执行；其他任务继续。",
                                     self.voice.key("cancel-queued", message_id))
            self.messenger.reply(ctx, chat_id, f"已取消排队任务 #{job.job_id}，其他任务继续。",
                                 self.voice.key("cancel-queued-ack", message_id), voice=self.voice)
            return True
        job.cancel_requested_at = utc_now_iso()
        job.cancel_requested_by = "card"
        # What the user looks at changes first: the status line and the buttons at the top of the card.
        shown = bool(job.progress) and self.cards.show_stopping(ctx, job)
        self.record_job_state(ctx, job, "cancel_requested", "用户点击卡片停止按钮")
        if job.progress:
            if not shown:  # A card without the status line (or a failed write) says it in its body.
                content = self.cards.render_stream_status(job, "正在停止", ["已收到停止请求，正在中断后台任务。"])
                self.cards.update_job_streaming_reply(
                    ctx,
                    job,
                    content,
                    force=True,
                    idempotency_prefix=self.voice.key("job-stop", message_id),
                    allow_replacement=False,
                )
            self.steering.close_task_controls(ctx, job)
        else:
            self.messenger.reply(
                ctx,
                chat_id,
                f"已请求停止任务 #{job.job_id}，后台会尽快结束。",
                self.voice.key("job-stop", message_id),
                voice=self.voice,
            )
        self.runtime.log(f"job stop requested job=#{job.job_id} message_id={job.message_id}")
        return True

    def close_unstarted_job(self, ctx: BridgeContext, job: ReplyJob, status: str, line: str, key: str) -> None:
        """Finish the card of a queued job that will never run and drop its registration."""
        if job.progress:
            self.cards.show_outcome(ctx, job, "⏹ " + status)  # The top first, with the buttons' removal.
            self.cards.update_job_streaming_reply(ctx, job, self.cards.render_stream_status(job, status, [line]),
                force=True, idempotency_prefix=key, allow_replacement=False)
            self.steering.close_task_controls(ctx, job)
            self.cards.close_streaming_reply(ctx, job.progress)
        self.runtime.unregister_reply_job(job)

    def retry_topic(self, ctx: BridgeContext, chat_id: str, message_id: str, *, job_id: int | None = None,
                    automatic: int = 0) -> str:
        """检查并继续 on the topic's failed task (``job_id``); ``automatic``: the n-th automatic retry
        of the kind the task's record schedules (app/auto_retry.py), 0 for a click."""
        with topic_lock(ctx):  # One retry at a time; its Feishu calls delay only this topic.
            return self._retry_topic(ctx, chat_id, message_id, job_id=job_id, automatic=automatic)

    def _retry_topic(self, ctx: BridgeContext, chat_id: str, message_id: str, *, job_id: int | None = None,
                     automatic: int = 0) -> str:
        active = self.runtime.jobs.topic_jobs(ctx.topic_id, chat_id)
        if active:
            if len(active) == 1 and (job_id is None or active[0].job_id == job_id):
                message = self.questions.retry_live_questions(ctx, chat_id, active[0])
                if message is not None:
                    return message
            return "本话题还有运行或排队的任务，请完成或停止后再重试。"
        records = self.harness.topic_records(ctx, chat_id)
        record = next((r for r in records if r.get("status") not in {"running", "queued"} and (r.get("status") in {"failed", "interrupted"}
            or ((r.get("status") == "cancelled" or job_id is not None) and (has_saved_reply(ctx, r) or r.get("policy_stop"))))
            and (job_id is None or r.get("job_id") == job_id) and not r.get("retried_by")), None)
        if not record:
            return "本话题没有可重试的失败任务。"
        decided = recovery.classify(record, saved=lambda: has_saved_reply(ctx, record), native_live_question=lambda: any(
            row.get("native_live_question") is True for _, row in directory_store(ctx.state_dir / "questions").query(
                job_id=record["job_id"], topic_id=ctx.topic_id, chat_id=chat_id)))
        kind = decided.kind
        if kind == recovery.SAVED_QUESTION:
            try:
                # Workflow waiting_user is separate from the completed native turn.
                _, outcome = load_authorized_reply(ctx, {**record, "execution_status": "done"}, self.harness.spec.name, chat_id)
                if outcome.status != "completed" or not outcome.resume_allowed:
                    raise ValueError("normal question completion is not authorized")
            except Exception:
                return "保存的回复或问答权限未通过核对；不会重新执行任务。"
            kind = decided.after
        if kind in RETRY_REFUSALS:
            return worded(RETRY_REFUSALS[kind], traits(ctx.topic_id, chat_id))
        if kind == recovery.REDELIVER_QUESTION:
            return self.redeliver_paused_question(ctx, chat_id, record)
        if kind == recovery.REDELIVER_SAVED:
            return self.redeliver_verified_result(ctx, chat_id, record)
        if kind == recovery.REDELIVER_FAILED:
            return self.redeliver_failed_delivery(ctx, chat_id, message_id, record)
        return self.requeue(ctx, chat_id, message_id, record, automatic=automatic)

    def requeue(self, ctx, chat_id, message_id, record, *, automatic=0, fallback=None):
        """Queue the recorded task again with its recorded model settings, under the topic's
        current binding (a click on a stale card was refused before this). ``fallback``: a retry after a safety
        refusal (app/policy_retry.py; {"of", "category", "model", "name"}): the same request on that model, as a new
        turn, not 检查并继续: no recovery context, the refused turn left out of the recent conversation."""
        history = recent_history(ctx.history_path)
        kind = auto_retry.kind_of(record) if automatic else ""
        counts = auto_retry.chain_counts(record, automatic)
        try:
            model, effort = strict_recorded_model_settings(record, native_default_model=self.harness.spec.native_default_model)
            if fallback:
                model = fallback["model"]
                history = [row for row in history if row.get("message_id") != record.get("message_id")
                           and not (row.get("role") == "assistant" and row.get("content") == record.get("final_text"))]
            model, effort = self.models.effort_preset(ctx, model, effort)
        except ValueError as exc:
            return "无法重试：" + str(exc)
        job = ReplyJob(job_id=self.runtime.jobs.next_id(), user_text=str(record.get("user_text") or ""),
                       model=model, effort=effort, model_settings_snapshot=True, work_mode=recorded_work_mode(record),
                       prompt_text=str(record.get("prompt_text") or "") or None,
                       recovery_context=None if fallback else {
                           "previous_status":record.get("status"),"previous_result":record.get("final_text"),
                           "checkpoint":read_checkpoint(Path(record["checkpoint_path"])) if record.get("checkpoint_path") else (record.get("recovery_context") or {}).get("checkpoint", {}),
                           "workdir":record.get("workdir")},
                       chat_id=chat_id, message_id=message_id, timeout=ctx.timeout,
                       topic_id=ctx.topic_id, reply_to_message_id=ctx.reply_to_message_id,
                       workspace_name=ctx.workspace_name, workdir=ctx.workdir, history_path=ctx.history_path,
                       session_path=ctx.session_path, sessions_dir=ctx.sessions_dir, history_snapshot=history,
                       recent_questions=recent_user_questions(history, str(record.get("user_text") or "")),
                       **counts)
        carry_device(job, ctx)
        if fallback:
            job.policy_retry = {name: fallback[name] for name in ("of", "category", "model")}
        elif record.get("policy_retry"):
            # A continuation of such a retry (检查并继续, an automatic retry): still on its older model, its subagents
            # too, and never retried after a refusal (app/policy_retry.py eligible).
            job.policy_retry = dict(record["policy_retry"])
        moved = retried_line(fallback["category"], fallback["name"]) if fallback else ""
        queued = moved or (auto_retry.queued_line(kind, automatic) if automatic else "重试已入队")
        # A quiet main-thread turn's (core/placement.py Row.quiet, D1): no card of its own yet (one at the top level
        # is its recovery card), its card held back as the first turn's was, its reactions on the owner's message.
        quiet = ((not record.get("card_message_id") or bool(record.get(TOP_CARD)))
                 and placement.quiet(ctx.state_dir, record))
        if quiet:
            job.reaction_message = placement.owner_message(record)
            job.card_deferred = True
        # The topic's binding, as context_for_topic read it: its controls and a later retry check it,
        # as for a job from a message.
        job.binding_revision = int(ctx.options.get("binding_revision") or 0)
        with LOCK:
            self.runtime.jobs.register(job)  # Idle maintenance sees it before its card is sent.
        try:
            job.progress = None if quiet else self.cards.create_streaming_reply(
                ctx, job, self.cards.render_stream_status(job, queued))
            # Read now: later calls on this thread replace the failure (app/submission.py says it the same way).
            card_notice = card_failure_notice(job) if not quiet and not job.progress and self.cards.streaming else ""
            self.record_job_state(ctx, job, "queued", queued, retry_of=record["key"],
                                  **({"reaction_message": job.reaction_message} if quiet else {}),
                                  **{name: count for name, count in counts.items() if count})
            self.runtime.enqueue_reply_job(job, self.harness.name)
        except BaseException:
            self.runtime.unregister_reply_job(job)
            raise
        upsert_job_record(ctx.state_dir, record["key"], retried_by=job.message_id)
        if quiet:
            from covey.app import main_reactions
            main_reactions.start(self.messenger, ctx.state_dir, ctx.profile, job, self.runtime.log, again=True)
        self.cards.replace_record_card(ctx, record, self.asked_card(ctx, record, recovery_card(
            record, state_dir=ctx.state_dir, moved_to=job.job_id, automatic=kind, moved=moved)))
        queued_line = f"本话题任务已重新入队，任务 #{job.job_id}。"
        return f"{queued_line}\n{card_notice}" if card_notice else queued_line
