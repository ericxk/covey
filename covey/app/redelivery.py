"""Sending a recorded task's saved result or question again, without running the model (检查并继续 on a task whose
result was saved, or whose delivery failed; core/recovery.py classify decides which): ``Redelivery`` is a part of
app/task_controls.py TaskControls, which it reads its Harness, senders and questions from."""
from __future__ import annotations

import dataclasses
import functools
import hashlib
from pathlib import Path

from covey.app import saved_send
from covey.app.outbox import PAUSED_REASON, needs_question, record_attempt
from covey.cards.relay_card import STOPPED
from covey.core import placement
from covey.core.checkpoint import checkpoint_reply_display, read_checkpoint
from covey.core.jobs import saved_job as rebuilt_job
from covey.core.jobs import find_job_record, upsert_job_record
from covey.core.saved_results import (authorized_delivery, display_saved_reply,
                                               paused_question_delivery, paused_question_delivery_key,
                                               saved_delivery_key, saved_delivery_kind)
from covey.core.turn_words import retry_hint
from covey.feishu.delivery import card_tried, markdown_tried
from covey.feishu.transport import Failure, last_failure, set_failure


def delivery_context(ctx, record, saved_job):
    """The context ``record``'s saved result goes out again in: its own reply target, or, where the placement table
    puts the answer at the top level (core/placement.py ``answer`` TOP: the 话题区 layout's butler, whose answers
    are new messages in the chat, D1), none."""
    top = placement.plan(ctx.state_dir, record).row.answer == placement.TOP
    return dataclasses.replace(ctx, reply_to_message_id="" if top else saved_job.reply_to_message_id)


REDELIVERED_STATUS = "结果已重新投递"
REDELIVERY_UNCONFIRMED = "结果可能已经送达，但飞书没有确认；请先查看本话题。再点“重新投递结果”只会重发同一条，不会多发一份。"
# A card its first send tried, which was not kept to send again as it was (Redelivery.saved_outgoing).
TRIED_NOT_KEPT = "这条结果之前已经发过一次，可能已经送达；请先查看本话题。不会另发一张卡，也不会重新执行任务。"


def unconfirmed_send():
    """The last send on this thread failed in a way that may have delivered it (rule 6)."""
    failure = last_failure()
    return failure is not None and (failure.ambiguous or failure.category == "reconciliation_required")


def redelivery_key(voice, key, tag, text):
    """The send key of a redelivered result (``tag``) of the job ``key``: the same for every click."""
    return voice.key(tag, f"{key}-{hashlib.sha256(text.encode('utf-8')).hexdigest()[:24]}")




class Redelivery:
    """TaskControls' redelivery of a saved result, a paused native question or a result whose delivery failed."""

    def send_message(self, ctx, chat_id, text, key, card=None, on_card=None):
        """A message in ``ctx`` in the Harness's voice (app/saved_send.py's ``send``, as the first send's)."""
        return self.messenger.reply(ctx, chat_id, text, key, voice=self.voice,
                                    **({"card": card} if card is not None else {}),
                                    **({"on_card": on_card} if on_card is not None else {}))

    def attempted(self, ctx, key, failure, *, delivered=False, text=""):
        """app/outbox.py record_attempt of a redelivery of job ``key``, then the top level told (TaskControls'
        ``delivered_relay`` and ``settle_relay``, app/task_controls.py): a result that just went, or 停住了 with
        PAUSED_REASON once the outbox gives up (paused) on a send that surely did not go: not one that may have landed
        (``failure`` ambiguous, reconciliation_required, or none to tell), whose relay would say what is not known.
        The outbox state."""
        state = record_attempt(ctx.state_dir, key, failure, delivered=delivered)
        if state == "delivered":
            self.delivered_relay(ctx, key, text)
        elif state == "paused" and failure is not None and not failure.ambiguous \
                and failure.category != "reconciliation_required":
            self.settle_relay(ctx, {**(find_job_record(ctx.state_dir, key) or {}), "key": key}, STOPPED, PAUSED_REASON)
        return state

    def saved_outgoing(self, ctx, record, saved_job, reply, outcome, text):
        """What ``record``'s saved reply goes out as again: what its first send kept under the same key, as it was
        (the same key never goes with other content), when its digest is that of ``text``, the reply authorized now
        (rule 5); drawn from the record as the first send draws it when none was kept (app/saved_send.py). None when
        a card was already tried under the key: one drawn again is other content, which may follow one that landed
        (rules 1 and 5: not sent; the transport would refuse it as ``idempotency_conflict``). A reply already tried as
        Markdown under the key (a release before an answer too large for one card went on several, cards/pages.py)
        goes so again, whole, under the same keys, never as cards (``paged`` False): its 产物 and 输入/输出 on the
        small card after it."""
        key = saved_delivery_key(self.harness.spec.idempotency_prefix, record["key"], reply)
        outgoing = saved_send.kept(ctx.state_dir, record["key"], key, text)
        if outgoing is not None:
            return outgoing
        file = saved_delivery_kind(reply, outcome) == "file"
        cards = not file and self.voice.message_format() == "markdown"
        route = dict(text=text, idempotency_key=key, chunk_limit=self.voice.chunk_limit(),
                     fallback_key_prefix=self.voice.key_prefix) if cards else {}
        if cards and card_tried(**route):
            return None
        return saved_send.rebuilt(ctx, record, saved_job, reply, text, key=key, file=file,
                                  bare=not record.get("card_message_id") and placement.quiet(ctx.state_dir, record),
                                  harness=self.harness, cards=getattr(self, "cards", None), runtime=self.runtime,
                                  voice=self.voice, paged=not cards or not markdown_tried(**route))

    def redeliver_verified_result(self, ctx, chat_id, record):
        set_failure(None)
        try:
            with authorized_delivery(ctx, record, self.harness.spec.name, chat_id) as (reply, outcome, record):
                if record.get("delivery_ok") is True and not needs_question(record):
                    return "该结果已送达；不会重新执行任务或恢复原生会话。"
                saved_job = rebuilt_job(record, chat_id, ctx.topic_id,
                                        record.get("reply_to_message_id") or ctx.reply_to_message_id)
                delivery_ctx = delivery_context(ctx, record, saved_job)
                text = display_saved_reply(reply, outcome)
                answer_message = ""
                if record.get('delivery_ok') is True:
                    sent = True
                else:
                    # The card its first send sent, under the same key (app/saved_send.py).
                    outgoing = self.saved_outgoing(ctx, record, saved_job, reply, outcome, text)
                    if outgoing is None:
                        self.runtime.log(f"saved result not sent again job={record['key'][-10:]}: "
                                         "its first send tried a card that was not kept")
                        self.attempted(ctx, record['key'], Failure('reconciliation_required'))
                        return TRIED_NOT_KEPT
                    sent, answer_message = saved_send.send_saved(
                        dataclasses.replace(ctx, reply_to_message_id=saved_job.reply_to_message_id), saved_job,
                        text, outgoing, send=self.send_message, artifacts=self.artifacts, log=self.runtime.log)
                    if sent and outgoing.files and hasattr(self.artifacts, "refresh_answer_cards"):
                        # Drawn as the upload stood at the first send: its 产物 as they are now (never raises).
                        self.artifacts.refresh_answer_cards(ctx, record)
                if not sent:
                    self.attempted(ctx, record['key'], last_failure())
                    return "保存的结果仍未能发送，请稍后重试；不会重新执行任务。"
                upsert_job_record(ctx.state_dir, record["key"], delivery_ok=True,
                    status='waiting_user' if needs_question(record) else outcome.job_status,
                    status_message="保存的结果已重新投递；执行事实与续接限制不变",
                    **({"answer_message_id": answer_message} if answer_message else {}))
                if needs_question(record):
                    if not self.questions.deliver_checkpoint_questions(delivery_ctx, saved_job, False):
                        self.attempted(ctx, record['key'], last_failure())
                        return '正文已送达，问题控件仍未送达；不会重新执行任务。'
                    upsert_job_record(ctx.state_dir, record['key'], question_delivery_ok=True, status='waiting_user')
                self.attempted(ctx, record['key'], None, delivered=True, text=text)
                return "保存的结果已重新投递；没有调用模型或恢复原生会话。"
        except Exception as exc:
            self.runtime.log("saved result redelivery refused: " + type(exc).__name__)
            self.attempted(ctx, record['key'], Failure('reconciliation_required'))
            return "保存结果未通过发布条件或完整性检查，暂不能投递；不会重新执行任务。"

    def redeliver_paused_question(self, ctx, chat_id, record):
        set_failure(None)
        try:
            with paused_question_delivery(ctx, record, self.harness.spec.name, chat_id) as current:
                if current.get("delivery_ok") is True and current.get("question_delivery_ok") is True:
                    return "原生问题已送达，请回答问题；不会重新执行任务。"
                saved_job = rebuilt_job(current, chat_id, ctx.topic_id,
                                        current.get("reply_to_message_id") or ctx.reply_to_message_id)
                delivery_ctx = delivery_context(ctx, record, saved_job)
                text = current["final_text"]
                if current.get("delivery_ok") is not True:
                    # Where its first send puts it (app/turn_delivery.py TurnDelivery._question_apart): at the top level
                    # only for a quiet butler turn that sent no card, a long one as a file where it replies.
                    replying = dataclasses.replace(ctx, reply_to_message_id=saved_job.reply_to_message_id)
                    bare = not current.get("card_message_id") and placement.quiet(ctx.state_dir, current)
                    sent = saved_send.send_paused(
                        replying, dataclasses.replace(ctx, reply_to_message_id="") if bare else replying, saved_job,
                        text, paused_question_delivery_key(self.harness.spec.idempotency_prefix, current["key"],
                                                           current["paused_question_delivery"]),
                        send=self.send_message, artifacts=self.artifacts)
                    if not sent:
                        self.attempted(ctx, current['key'], last_failure())
                        return "问题正文仍未送达，请稍后重试；不会执行任务。"
                    upsert_job_record(ctx.state_dir, current["key"], delivery_ok=True)
                if not self.questions.deliver_checkpoint_questions(delivery_ctx, saved_job, False):
                    upsert_job_record(ctx.state_dir, current["key"], status="failed", question_delivery_ok=False)
                    self.attempted(ctx, current['key'], last_failure())
                    return "正文已送达，问题控件仍未送达；请稍后重试。"
                upsert_job_record(ctx.state_dir, current["key"], status="waiting_user", delivery_ok=True,
                                  question_delivery_ok=True, status_message="原生问答已重新投递")
                self.attempted(ctx, current['key'], None, delivered=True)
                return "原生问答已重新投递；没有重新执行任务。"
        except Exception as exc:
            self.runtime.log("native question redelivery refused: " + type(exc).__name__)
            self.attempted(ctx, record['key'], Failure('reconciliation_required'))
            return "原生问答暂停或投递状态未通过核对；不会重新执行任务。"

    def redeliver_failed_delivery(self, ctx, chat_id, message_id, record):
        """Send again a result (or its question forms) whose delivery failed; the model does not run again."""
        saved_job = rebuilt_job(record, chat_id, ctx.topic_id,
            record.get("reply_to_message_id") or ctx.reply_to_message_id,
            checkpoint_path=record.get("checkpoint_path") or "",
            checkpoint_question_error=record.get("checkpoint_question_error") or "",
            checkpoint_question_registration_pending=record.get("checkpoint_question_registration_pending", False),
            checkpoint_question_deferred=record.get("checkpoint_question_deferred", False))
        delivery_ctx = delivery_context(ctx, record, saved_job)
        sent = record.get("delivery_ok") is True
        text = record["final_text"]
        cp = read_checkpoint(Path(saved_job.checkpoint_path)) if saved_job.checkpoint_path else {}
        visible = checkpoint_reply_display(text, str(cp.get("question") or ""),
            saved_job.checkpoint_question_deferred)
        # A restart may retain successful body delivery but precede the form.
        # Conservatively restore canonical text if that later form fails.
        display_only_sent = bool(sent and visible != text)
        # One key per record and text, not per click: a send that timed out and may have landed is the
        # same message when clicked again, never a second copy (rule 6).
        key = functools.partial(redelivery_key, self.voice, record["key"])
        set_failure(None)
        if not sent:
            if self.cards.exceeds_stream_card_limit(visible):
                sent = self.artifacts.send_result_file(delivery_ctx, saved_job, text)
            if not sent:
                sent = self.messenger.reply(delivery_ctx, chat_id, visible, key("redeliver", visible), voice=self.voice)
                display_only_sent = bool(sent and visible != text)
            if not sent and not unconfirmed_send():  # One that may have landed is not sent again as a file.
                sent = self.artifacts.send_result_file(delivery_ctx, saved_job, text)

        def restore_original_if_needed():
            # A failed form must not leave the sole question removed from
            # the visible reply. Files always contain the canonical text.
            if not display_only_sent or not text.strip():
                return True
            # As the first send restores it (app/turn_delivery.py deliver_questions); no task card here to paint.
            return saved_send.restore_original(delivery_ctx, saved_job, text, text, key=key("redeliver-original", text),
                                               send=self.send_message, artifacts=self.artifacts,
                                               log=self.runtime.log)
        if not sent:
            return REDELIVERY_UNCONFIRMED if unconfirmed_send() else "结果仍未能发送，请稍后重试。"
        execution_status = record.get("execution_status") or "done"
        # Keep the failed delivery status until the deferred form is also sent.
        upsert_job_record(ctx.state_dir, record["key"], delivery_ok=True)
        if execution_status == "waiting_user" and (saved_job.checkpoint_question_error
                or saved_job.checkpoint_question_registration_pending):
            try:
                cp = read_checkpoint(Path(saved_job.checkpoint_path))
                questions = cp.get("questions") or [{"id": "answer", "header": "补充信息", "question": cp.get("question", "")}]
                self.questions.defer_checkpoint_question(delivery_ctx, saved_job, questions)
                saved_job.checkpoint_question_error = ""
                upsert_job_record(ctx.state_dir, record["key"], checkpoint_question_error="",
                                  checkpoint_question_registration_pending=False)
            except Exception as exc:
                original_ok = restore_original_if_needed()
                upsert_job_record(ctx.state_dir, record["key"], status="failed", question_delivery_ok=False,
                    delivery_ok=original_ok, checkpoint_question_error="问题登记失败（" + type(exc).__name__ + "）",
                    status_message=f"问题登记失败；原始回复已保留，{retry_hint()} 可重新投递")
                return ("正文已重新投递，问题仍未能登记，请稍后重试。" if original_ok
                        else f"原始回复仍未能完整发送，问题登记也未完成；请稍后{retry_hint()}。")
        try:
            question_ok = execution_status != "waiting_user" or self.questions.deliver_checkpoint_questions(delivery_ctx, saved_job, False)
        except Exception as exc:
            question_ok = False
            self.runtime.log("question redelivery failed: " + type(exc).__name__)
        if not question_ok:
            original_ok = restore_original_if_needed()
            upsert_job_record(ctx.state_dir, record["key"], status="failed", question_delivery_ok=False,
                              delivery_ok=original_ok, status_message=f"问题控件发送失败；原始回复已保留，{retry_hint()} 可重新投递")
            return ("正文已重新投递，问题控件仍未能发送，请稍后重试。" if original_ok
                    else f"原始回复仍未能完整发送，问题控件也未送达；请稍后{retry_hint()}。")
        upsert_job_record(ctx.state_dir, record["key"], status=execution_status,
                          status_message=REDELIVERED_STATUS, delivery_ok=True, question_delivery_ok=True)
        self.delivered_relay(ctx, record["key"], text)  # The top level's 做完了 or 等你, which the first send never sent.
        return REDELIVERED_STATUS + "。"
