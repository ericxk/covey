"""The 补充 forms of a task: a 补充 click (steer_action routes the receipt's buttons to app/steer_receipts_flow.py),
the form (steer_form_card), a submitted form (submit_steer_form; a queued message's old form, submit_merge_form and
merge_target) and the forms closed when the task ends (close_steer_forms, close_task_controls). SteerForms is a
part of app/steering.py's Steering."""
from __future__ import annotations

import hashlib
from types import SimpleNamespace

from covey.cards.steer_receipts import CHECK_KIND, INTERRUPT_KIND, REQUEUE_KIND, WITHDRAW_KIND, receipt_card, stage_line
from covey.core.deliveries import delivery_path, read_delivery, reconcile, topic_steer
from covey.cards.steer_form import closed_card, steer_form
from covey.app.card_actions import action_value
from covey.core.jobs import upsert_job_record
from covey.core.butler import traits, traits_of, worded
from covey.core.state import read_json
from covey.core.steer import SteerRefused
from covey.app.detached import update_queued

STEER_CLOSED = "任务已结束，不能再补充"


class SteerForms:
    """Steering's 补充 forms (its harness's voice, runtime, messenger and cards)."""

    def steer_action(self, ctx, chat, event, raw, fields, mid):
        kind = raw.get("kind")
        if kind in {CHECK_KIND, REQUEUE_KIND, INTERRUPT_KIND}:
            return self.steer_receipt_action(ctx, chat, kind, raw, mid)
        if kind == WITHDRAW_KIND:
            return self.withdraw_steer(ctx, chat, raw, mid)
        if raw.get("merge"):
            return self.submit_merge_form(ctx, chat, event, raw, fields, mid)
        if fields.get("supplement"):
            return self.submit_steer_form(ctx, chat, event, raw, fields, mid)
        job, stage = self.steer_target(ctx, chat, raw.get("job_id"))
        if stage == "queued" and (job.steer_target_job_id or
                                  (job.steer_target_job_id is None and self.runtime.jobs.ahead_of(job))):
            return self.steer_queued_job(ctx, chat, job, mid)  # The queued message itself, attachments and all.
        if stage == "paused":
            raise ValueError(f"任务 #{job.job_id} 正在等待回答，请先回答问题，之后可以再补充")
        if job is None:
            raise ValueError(worded(f"任务 #{raw.get('job_id')} 已结束，不能补充；直接在本话题发送消息即可",
                                    traits(ctx.topic_id, chat)))
        # With nothing ahead, a card that still says 已入队 is the task about to start: it takes the supplement.
        self.messenger.publish_card(ctx, chat, mid, self.steer_form_card(job, raw), on_sent=job.steer_forms.append, voice=self.voice)

    def dispatch_steer(self, ctx, event):
        """📝 补充 on a 派发卡 (cards/dispatch_card.py): the 补充 form of the job the card named, a card of its own
        (that card is redrawn, so it holds no field), then as a click on the job's own 补充."""
        raw = action_value(event)
        if raw.get("topic_id") != ctx.topic_id:
            raise ValueError("操作不属于当前话题")
        try:
            job_id = int(raw.get("job_id"))
        except (TypeError, ValueError):
            raise ValueError("这件事还没有可补充的任务") from None
        self.steer_action(ctx, str(event.get("chat_id") or ""), event,
                          {"kind": "bridge_steer", "topic_id": ctx.topic_id, "job_id": job_id}, {},
                          str(event.get("event_id") or event.get("message_id") or ""))
        return True

    def project_steer(self, ctx, event):
        """📝 补充 on a 专项卡 (cards/project_card.py): the 补充 form of the lead's running turn, a card of its own (the project's
        card is redrawn, so it holds no field). The lead with no turn running has nothing to steer: the owner is told to
        quote the card, which routes to the lead (app/project_cards.py), and no task is started here."""
        from covey.app import project_clicks

        job_id = project_clicks.steer_job(self, ctx, event)
        if not job_id:
            return project_clicks.say(self, ctx, event, project_clicks.NO_LEAD_JOB, "project-steer-idle")
        self.steer_action(ctx, str(event.get("chat_id") or ""), event,
                          {"kind": "bridge_steer", "topic_id": ctx.topic_id, "job_id": int(job_id)}, {},
                          str(event.get("event_id") or event.get("message_id") or ""))
        return True

    def steer_form_card(self, job, raw, *, text="", notice=""):
        """The 补充 form names the task it feeds; it is replaced by a receipt or closed when the task ends. It says
        where a longer message goes: into the task as it arrives (COVEY_TOPIC_STEER; the butler's always)."""
        return steer_form(job.job_id, " · ".join(x for x in (job.topic_label, f"#{job.job_id}") if x), raw,
                          text=text, notice=notice, arrives=topic_steer() or traits_of(job).lists_supplements)

    def merge_target(self, ctx, chat, queued, mid, form="", *, arrival=False):
        """The task a queued message's supplement goes to, bound once to the one before it; None, said on the
        queued card (or the ``form`` it was written in), when that task ended, was stopped or waits for an
        answer: it is never sent to another. ``arrival``: the message as it arrived (Steering.steer_on_arrival), its
        card saying why in one line."""
        ahead = self.runtime.jobs.ahead_of(queued)
        with queued.execution_lock:
            if queued.steer_target_job_id is None:
                # Legacy queued cards bind once to the nearest preceding task, not the first active one.
                queued.steer_target_job_id = ahead[-1].job_id if ahead else 0
                upsert_job_record(ctx.state_dir, queued.message_id, steer_target_job_id=queued.steer_target_job_id)
                update_queued(ctx.state_dir, queued)
        target_id = queued.steer_target_job_id
        target = next((j for j in ahead if j.job_id == target_id), None)
        stage = self.steer_stage(target) if target is not None else "ended"
        if target is not None and stage in {"paused", "ended"}:
            target = None
        if target is None and arrival:
            why = "它在等回答" if stage == "paused" else "它已经结束"
            self.queued_card_notice(ctx, chat, queued, f"没有补充到 #{target_id}：{why}", mid, arrival=True)
        elif target is None:
            notice = (f"上一任务 #{target_id} 已结束、已取消或正在等待回答，未发送补充；本条继续排队，不会改投其他任务"
                      if target_id else "没有可补充的上一任务；本条继续排队，按顺序执行")
            if not (form and self.close_merge_form(ctx, chat, queued, form, notice)):
                self.queued_card_notice(ctx, chat, queued, notice, mid)
        return target

    def close_merge_form(self, ctx, chat, queued, source, why):
        """A queued message's 补充 form that can send nothing now, closed saying ``why``."""
        closed = closed_card(f"**这条没有作为补充发出**\n{why}。", "补充没有发出")
        return self.cards.repaint_browse_card(ctx, chat, f"merge-closed-{queued.job_id}", source, closed)

    def submit_merge_form(self, ctx, chat, event, raw, fields, mid):
        """A queued message's 补充 form submitted. Releases 8ec6c66 to 2a08179 opened one, filled in with the
        message's text only, so an edited one lost its attachments; a click now sends the message itself
        (``steer_queued_job``). Forms already sent still work: what it says goes to the task before the message,
        which no longer runs by itself; the receipt is on this form, the queued card says where it went."""
        source = event.get("message_id", "")
        mapping = ctx.state_dir / "browse-cards" / (self.voice.safe_key(source) + ".json")
        saved_card = read_json(mapping) if source and mapping.exists() else {}
        if saved_card.get("topic_id") != ctx.topic_id or saved_card.get("chat_id") != chat:
            raise ValueError("补充卡片已失效，请从排队消息的卡片重新打开")
        text = str(fields.get("supplement") or "").strip()
        if not text:
            raise ValueError("补充内容为空")
        queued, stage = self.steer_target(ctx, chat, raw.get("job_id"))
        if queued is None or stage != "queued":
            why = f"#{raw.get('job_id')} 已经开始执行、已取消或已结束"
            if not self.close_merge_form(ctx, chat, SimpleNamespace(job_id=raw.get("job_id")), source, why):
                raise ValueError(why + "，这条没有作为补充发出")
            return
        self.steer_queued_job(ctx, chat, queued, mid, text=text, form=source, raw=raw)

    def submit_steer_form(self, ctx, chat, event, raw, fields, mid):
        source = event.get("message_id", "")
        mapping = ctx.state_dir / "browse-cards" / (self.voice.safe_key(source) + ".json")
        saved_card = read_json(mapping) if source and mapping.exists() else {}
        if saved_card.get("topic_id") != ctx.topic_id or saved_card.get("chat_id") != chat:
            raise ValueError("补充卡片已失效，请从当前任务卡重新打开")
        delivery_id = "steer-form-" + source
        path = delivery_path(ctx.state_dir, ctx.topic_id, delivery_id)
        later = []  # The task card's note of the supplement, after the form shows its receipt (S37: 0.35 s).
        saved = read_delivery(path)
        saved = reconcile(path, saved) if saved else None
        text = str(fields.get("supplement") or "").strip()
        if saved is None or saved.get("status") == "refused":
            job, stage = self.steer_target(ctx, chat, raw.get("job_id"))
            if stage == "queued" and self.runtime.jobs.ahead_of(job):
                job = None  # Its own queued card turns it into a supplement to the task before it.
            if not job:
                closed = closed_card(f"**任务 #{raw.get('job_id')} 已结束，这条补充没有发出**\n"
                                     + worded("需要继续时，直接在本话题发送消息。", traits(ctx.topic_id, chat)),
                                     "任务已结束，补充没有发出", unsent=text)
                if not self.cards.repaint_browse_card(ctx, chat, mid, source, closed):
                    self.messenger.reply(ctx, chat, "任务已结束，这条补充没有发出。", self.voice.key("steer", delivery_id), voice=self.voice)
                return
            try:
                saved = self.deliver_steer(ctx, job, text, delivery_id, receipt={"kind": "browse", "source": source},
                                           later=later)
            except SteerRefused as exc:
                # Nothing was sent: the same form takes the text again.
                if not self.cards.repaint_browse_card(ctx, chat, mid, source, self.steer_form_card(
                        job, raw, text=text, notice=f"⚠️ 没有发出：{exc}。内容已保留，可直接再次提交。")):
                    raise ValueError(f"补充没有发出：{exc}；可在原卡片再次提交") from None
                return
            except Exception:  # noqa: BLE001 - an unconfirmed delivery shows its receipt below
                saved = read_delivery(path)
                if saved is None:
                    raise
        if saved.get("job_id") != int(raw["job_id"]) or saved.get("topic_id") != ctx.topic_id:
            raise ValueError("补充记录不属于当前任务")
        if not self.cards.repaint_browse_card(ctx, chat, mid, source, receipt_card(saved)):
            self.messenger.reply(ctx, chat, f"补充任务 #{saved['job_id']}：{stage_line(saved)}。",
                                 self.voice.key("steer", delivery_id), voice=self.voice)
        for note in later:
            try:
                note()
            except Exception as exc:  # noqa: BLE001 - the supplement is delivered and its receipt shown
                self.runtime.log(f"latest supplement not shown: {type(exc).__name__}")

    def close_steer_forms(self, ctx, job, *, held=True):
        """补充 forms still open when their task ends become a read-only note; supplements still waiting
        for the task to take them are marked as never sent (``held``)."""
        if held:
            self.drop_held_steers(ctx, job)
        forms, job.steer_forms = job.steer_forms, []
        closed = closed_card(f"**{STEER_CLOSED}**\n" + worded("需要继续时，直接在本话题发送消息。", job),
                             STEER_CLOSED)
        for source in forms:
            token = hashlib.sha256(f"{ctx.topic_id}-steer-form-{source}".encode()).hexdigest()
            sent = ctx.state_dir / "interaction-deliveries" / (token + ".json")
            if sent.exists() and read_json(sent).get("status") != "refused":
                continue  # That card already shows its receipt.
            try:
                self.messenger.publish_card(ctx, job.chat_id, "steer-closed-" + source, closed, source_message=source, update_only=True, voice=self.voice)
            except Exception as exc:  # noqa: BLE001 - a stale form still refuses late submissions
                self.runtime.log(f"steer form not closed: {type(exc).__name__}")

    def close_task_controls(self, ctx, job):
        """The job's controls end: its 补充 forms close, then its card's action row goes."""
        if not job.progress:
            return False
        self.close_steer_forms(ctx, job)
        return self.cards.delete_stop_action(ctx, job)
