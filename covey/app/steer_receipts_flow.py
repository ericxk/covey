"""What became of a supplement, on its receipt card (cards/steer_receipts.py draws it): repainting the card that shows
it (show_steer_receipt) and its buttons, 检查状态, 重新排队 and 撤回 (steer_receipt_action, withdraw_steer).
SteerReceiptFlow is a part of app/steering.py's Steering."""
from __future__ import annotations

import time

from covey.cards.steer_receipts import REQUEUE_KIND, receipt_card, stage_line
from covey.core.deliveries import REQUEUE, offers, read_delivery, reconcile, update_delivery
from covey.core.jobs import find_job_record
from covey.core.statuses import RUNNING
from covey.core.locks import resource_lock
from covey.app.card_lanes import after_ack

REQUEUE_UNCONFIRMED = "重新排队已提交，结果未确认；不会重复排队，需要时请直接在本话题再发送一次"
REQUEUE_IN_FLIGHT = "正在重新排队，不会重复排队"


class SteerReceiptFlow:
    """Steering's receipt cards and their clicks (its harness's voice, runtime, messenger, cards and queue)."""

    def show_steer_receipt(self, ctx, record):
        """Repaint the card that shows this supplement; False when there is none or it failed. Paints of one
        supplement go one at a time, each from its record as saved now: a paint that comes late (the
        click that queued it, after the task already took it) still shows where it is."""
        token = str(record.get("token") or "")
        if len(token) != 64:
            return self._paint_steer_receipt(ctx, record)
        path = ctx.state_dir / "interaction-deliveries" / (token + ".json")
        with resource_lock("steer-receipt", str(path)):
            return self._paint_steer_receipt(ctx, read_delivery(path) or record)

    def _paint_steer_receipt(self, ctx, record):
        receipt = record.get("receipt") or {}
        if receipt.get("kind") == "job":
            card_record = find_job_record(ctx.state_dir, str(receipt.get("key") or ""))
            if not card_record:
                return False
            source = f"来源：#{card_record['job_id']} 排队的消息，已转为补充，不再单独执行。"
            return self.cards.replace_record_card(ctx, card_record, receipt_card(record, source=source))
        if receipt.get("kind") == "browse" and receipt.get("source"):
            return self.cards.repaint_browse_card(ctx, record.get("chat_id", ""), f"steer-receipt-{time.time_ns()}",
                                            receipt["source"], receipt_card(record))
        return False

    def withdraw_steer(self, ctx, chat, raw, mid):
        token = str(raw.get("token") or "")
        if len(token) != 64 or any(c not in "0123456789abcdef" for c in token):
            raise ValueError("补充标识无效")
        record = read_delivery(ctx.state_dir / "interaction-deliveries" / (token + ".json"))
        if record is None or record.get("topic_id") != ctx.topic_id or record.get("chat_id") != chat:
            raise ValueError("补充记录不属于当前话题")
        job = self.runtime.jobs.find(int(record["job_id"])) if record.get("job_id") is not None else None
        if job is not None:
            with job.held_lock:
                if job.held_steers.pop(record.get("mid"), None) is not None:
                    self.settle_steer(ctx, job, record["mid"], status="withdrawn", withdrawn_at=time.time(), error="")
                    return
        # Already sent, refused or ended: show where it is now.
        if not self.show_steer_receipt(ctx, record):
            self.messenger.reply(ctx, chat, f"补充任务 #{record.get('job_id')}：{stage_line(record)}。",
                                 self.voice.key("steer-withdraw", mid), voice=self.voice)

    def steer_receipt_action(self, ctx, chat, kind, raw, mid):
        token = str(raw.get("token") or "")
        if len(token) != 64 or any(c not in "0123456789abcdef" for c in token):
            raise ValueError("补充标识无效")
        path = ctx.state_dir / "interaction-deliveries" / (token + ".json")
        with resource_lock("question", path):
            record = read_delivery(path)
            if record is None or record.get("topic_id") != ctx.topic_id or record.get("chat_id") != chat:
                raise ValueError("补充记录不属于当前话题")
            record = reconcile(path, record)
            target = find_job_record(ctx.state_dir, str(record.get("job_key") or "")) or {}
            if record.get("status") in {"submitting", "uncertain"} and target.get("status") not in RUNNING:
                # The task ended without any acknowledgement: what it did with the text cannot be known now.
                record = update_delivery(path, status="uncertain", target_ended=True,
                                         error=f"任务 #{record.get('job_id')} 已结束，无法确认是否收到这条补充") or record
            elif (record.get("status") == "sent" and record.get("reads") and not record.get("merged")
                  and not record.get("target_ended") and target.get("status") not in RUNNING):
                # The task ended outside its worker (runner lost, Harness retired, closed at a restart): it
                # reports reads, and this one was never read.
                record = update_delivery(path, target_ended=True, ended_at=time.time(),
                                         error=f"任务 #{record.get('job_id')} 在模型读到这条补充之前就结束了") or record
            if kind == REQUEUE_KIND:
                if token in self.requeuing:
                    raise ValueError(REQUEUE_IN_FLIGHT)
                if record.get("requeue_state") in {"submitting", "uncertain"} and not record.get("requeued_job_id"):
                    raise ValueError(REQUEUE_UNCONFIRMED)
                # The receipt's own button, so an old card's click is refused; requeue_state counts only through
                # the two refusals above (a "queued" one always has requeued_job_id, which offers refuses).
                if REQUEUE not in offers({**record, "requeue_state": ""}):
                    raise ValueError("这条补充不能重新排队，或已经排过队")
                # The intent is saved under the lock; queuing sends cards and notices, so it runs outside (rule 1).
                record = update_delivery(path, requeue_state="submitting", requeue_at=time.time()) or record
                self.requeuing.add(token)
        if kind == REQUEUE_KIND:
            # The new task's card and the receipt took 2.9 s in the click (2026-10-26): after the acknowledgement.
            after_ack(lambda: self._requeue_and_show(ctx, chat, path, record, mid))
            return
        self._show_receipt(ctx, chat, record, mid)

    def _requeue_and_show(self, ctx, chat, path, record, mid):
        try:
            record = self._requeue(ctx, chat, path, record)
        finally:
            self.requeuing.discard(record["token"])
        self._show_receipt(ctx, chat, record, mid)

    def _show_receipt(self, ctx, chat, record, mid):
        if not self.show_steer_receipt(ctx, record):
            self.messenger.reply(ctx, chat, f"补充任务 #{record.get('job_id')}：{stage_line(record)}。",
                                 self.voice.key("steer-status", mid), voice=self.voice)

    def _requeue(self, ctx, chat, path, record, *, message_id=""):
        """Queue the supplement's message as a task of its own, then record the job. An error may
        have queued it: it is unconfirmed and never queued again by itself. ``message_id``: the new task's, the
        owner's message itself for a butler one (app/steering.py queue_lost); else its own requeue key."""
        again, token = record["requeue"], record["token"]
        try:
            job = self.enqueue_chat_job(ctx, again["text"], chat, message_id or "requeue-" + token[:24],
                                        prompt_content=again.get("prompt") or again["text"],
                                        **({"sender_id": again["sender_id"]} if again.get("sender_id") else {}))
        except Exception:
            unconfirmed = update_delivery(path, requeue_state="uncertain")
            if unconfirmed:
                try:
                    self.show_steer_receipt(ctx, unconfirmed)  # Its button goes: a second click is refused.
                except Exception as exc:  # noqa: BLE001 - the refusal still holds
                    self.runtime.log(f"steer receipt not repainted: {type(exc).__name__}")
            raise
        if job is None:
            # Refused before anything was queued (its reason was sent): it may be tried again.
            update_delivery(path, requeue_state="")
            raise ValueError("重新排队未成功，请直接在本话题再发送一次")
        return update_delivery(path, requeue_state="queued", requeued_job_id=job.job_id) or record
