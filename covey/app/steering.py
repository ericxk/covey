"""Supplements (补充) to a task of this topic: which task takes one, and how it gets there.

``steer_stage`` is the one reading of a task's flags, and each stage decides what a supplement
does: a queued task takes it into its request, a starting one keeps it until its backend can
take it, a running one is sent it now, and a task waiting for an answer or ended takes none.
What became of each supplement is its delivery record (core/deliveries.py). The 补充 forms are in
app/steer_forms.py and the receipt cards and their buttons in app/steer_receipts_flow.py, both parts of
Steering. A topic's message sent while the topic's task runs goes into it as it arrives (``steer_on_arrival``,
COVEY_TOPIC_STEER), the way a 补充 click on its queued card sends it; once it is in, DONE goes on his message too
(``acknowledge_arrival``), beside the receipt card.
"""

from __future__ import annotations

import dataclasses
import functools
import threading
import time

from covey.cards.steer_receipts import CHECK_KIND, INTERRUPT_KIND, REQUEUE_KIND, WITHDRAW_KIND
from covey.core.deliveries import (QUEUED_STEER, REQUEUE, delivery_path, delivery_token, end_unread, handed, in_topic,
                                   internal, lost, marked, offers, on_arrival, read_delivery, reconcile, save_delivery,
                                   shown_preview, topic_steer, update_delivery)
from covey.cards.steer_form import closed_card, latest_request
from covey.core.jobs import upsert_job_record, find_job_record
from covey.core import background, timing
from covey.core.butler import traits_of
from covey.core.harness import backend_capabilities
from covey.core.locks import resource_lock
from covey.core.history_storage import append_history
from covey.core.text import literal
from covey.core.topics import load_topic
from covey.core.steer import SteerNotReady, SteerRefused, SteerUncertain
from covey.app import device_files
from covey.app import butler_reactions
from covey.app.butler_reactions import DONE
from covey.app.butler import SUPPLEMENT_LOST, SUPPLEMENT_UNCERTAIN
from covey.app.runtime import job_stage
from covey.app.submission import queue_hint, queued_line
from covey.app.detached import update_queued
from covey.app.steer_forms import SteerForms
from covey.app.steer_receipts_flow import SteerReceiptFlow
from covey.app.steer_interrupt import SteerInterrupt


STEER_WATCH_SECONDS = 120
STEER_KINDS = frozenset({"bridge_steer", CHECK_KIND, REQUEUE_KIND, WITHDRAW_KIND, INTERRUPT_KIND})


def switched(found):
    """The (path, record) pairs of ``found`` (core/deliveries.py lost) the Bridge queues now: a topic's message that went in
    as it arrived only with COVEY_TOPIC_STEER on (core/deliveries.py topic_steer), the others whatever it says."""
    steer = topic_steer()
    return [(path, record) for path, record in found if steer or not on_arrival(record)]


def refusal_reason(text) -> str:
    """What a refusal (SteerRefused) says of the task, without its advice ("；请在本轮结束后发送下一轮要求", "，请先回答"):
    a message that stays queued is the next round already."""
    reason = str(text or "").split("；", 1)[0]
    return reason.split("，请", 1)[0] or "任务现在不能接收补充"


class Steering(SteerForms, SteerReceiptFlow, SteerInterrupt):
    """One Harness's supplements (补充): the forms on a task card, where each supplement goes
    for the task's stage, its delivery record and receipt card, and closing the forms when
    the task ends.

    - ``harness``: the voice of the forms, receipts and notices.
    - ``runtime``: the live and queued jobs a supplement goes to, and the log.
    - ``messenger``: forms, receipts and notices.
    - ``cards``: the task card a supplement is shown on.
    - ``record_job_state``: records a queued task that took a supplement into its request.
    - ``enqueue_chat_job``: queues a refused or withdrawn supplement again as its own task.
    - ``event_context``: the context a message starts a turn in (app/submission.py), for a butler
      supplement its turn never took, queued as a turn of its own (``queue_lost``), and a topic's
      message that went in as it arrived, in its topic (``requeue_context``).
    - ``hold``: words handed to a topic (``handed``) that its turn never took, before they are queued as its next turn:
      kept as a held note when the topic waits on a question (app/app.py workspace_command, ``note_only``), so a turn of
      their own never answers it (WAVE12 W2-B); truthy when they were. None: queued as they are.
    They are late-bound: they sit above Steering.
    """

    def __init__(self, harness, *, runtime, messenger, cards, record_job_state, enqueue_chat_job,
                 event_context=None, hold=None) -> None:
        self.harness = harness
        self.runtime = runtime
        self.messenger = messenger
        self.voice = harness.voice
        self.cards = cards
        self.record_job_state = record_job_state
        self.enqueue_chat_job = enqueue_chat_job
        self.event_context = event_context
        self.hold = hold
        self.requeuing = set()  # Supplements whose 重新排队执行 was acknowledged and is being queued.
        self.queuing_lost = threading.Lock()  # One ``queue_lost`` at a time: the lost messages queue in order.

    def steer_stage(self, job):
        """Where a task is for a supplement: queued (registered, not started), starting (taken by a
        worker, no backend control yet), running, paused (waiting for an answer) or ended."""
        return job_stage(job)

    def steer_target(self, ctx, chat, job_id):
        """This topic's task #job_id and its stage; (None, "ended") once it is gone."""
        try:
            job = self.runtime.jobs.find(int(job_id))
        except (TypeError, ValueError):
            job = None
        if job is None or job.topic_id != ctx.topic_id or job.chat_id != chat:
            return None, "ended"
        stage = self.steer_stage(job)
        if stage == "queued" and self.runtime.in_runner:
            stage = "starting"  # A task runner's only job is its own, about to start: never a queued one.
        return (None, stage) if stage == "ended" else (job, stage)

    def queued_card_notice(self, ctx, chat, queued, notice, mid, *, arrival=False):
        """Say it on the queued message's own card: the card changes as the message moves on, where a
        separate text would stay behind. ``arrival``: the card of a message that did not go into the task before it as
        it arrived (``steer_on_arrival``) gets back its status line, where it said it was being sent
        (app/submission.py queued_line)."""
        line = queued_line(queued) if arrival else ""
        with queued.execution_lock:
            if (queued.progress and not queued.execution_started and self.cards.update_job_streaming_reply(
                    ctx, queued, self.cards.render_stream_status(queued, "排队中", [f"⚠️ {notice}", queue_hint(queued)]),
                    force=True, idempotency_prefix=self.voice.key("queued-notice", mid), allow_replacement=False)):
                if line:
                    self.cards.update_card_activity(ctx, queued, line)
                return
        # No card to say it on: the line says where the message goes as well.
        self.messenger.reply(ctx, chat, notice + (f"；{line}" if line else "") + "。", self.voice.key("queued-notice", mid),
                             voice=self.voice)

    def steer_on_arrival(self, ctx, chat, queued, mid):
        """A topic's message, just queued behind the task it is bound to while that task runs (COVEY_TOPIC_STEER,
        app/submission.py steers_on_arrival), goes into it the way a click on its queued card's 补充 sends it
        (``steer_queued_job``): the message with its attachments and the topic's context, one delivery record, the card
        the receipt, marked ``auto`` (core/deliveries.py on_arrival). What sends nothing (that task waits for an answer,
        is closing or has ended, its backend refuses) leaves it queued as the next round, its card saying why in one
        line; it never runs as well once it may have gone in (rule 6). Whether it went in (or may have)."""
        try:
            taken = self.steer_queued_job(ctx, chat, queued, mid, auto=True)
        except Exception as exc:  # noqa: BLE001 - the message thread goes on; what did not go in stays queued
            # It started (the task before it ended) or was stopped meanwhile, or its record could not be read.
            self.runtime.log(f"topic message not steered on arrival job=#{queued.job_id}: {type(exc).__name__}: {exc}")
            with queued.execution_lock:
                waiting = not (queued.execution_started or queued.cancelled_before_start or queued.cancel_event.is_set())
            if waiting:
                why = f"：{exc}" if isinstance(exc, (ValueError, SteerUncertain)) and str(exc) else ""
                self.queued_card_notice(ctx, chat, queued, f"没有补充到 #{queued.steer_target_job_id}{why}", mid,
                                        arrival=True)
            return False
        if taken:
            timing.outcome("supplement")
            self.acknowledge_arrival(ctx, queued, mid)
        return taken

    def acknowledge_arrival(self, ctx, queued, mid) -> bool:
        """DONE on the owner's message once its supplement is in the running task (delivery sent or read), as a receipt
        beside the card: nothing for a held, refused or uncertain one. Called with no lock held; a failure is a log
        line, the supplement is in either way."""
        if getattr(queued, "top_message", ""):
            return False  # A message quoted from outside the topic's thread: steer_queued_job said it (top_steered).
        saved = read_delivery(delivery_path(ctx.state_dir, queued.topic_id, QUEUED_STEER + mid)) or {}
        if saved.get("status") not in ("sent", "read"):
            return False
        try:
            if self.messenger.react(ctx.profile, mid, DONE):
                return True
        except Exception as exc:  # noqa: BLE001 - the card is the receipt
            self.runtime.log(f"supplement reaction failed job=#{queued.job_id}: {type(exc).__name__}")
            return False
        self.runtime.log(f"supplement reaction not added job=#{queued.job_id} emoji={DONE}")
        return False

    def steer_queued_job(self, ctx, chat, queued, mid, *, text=None, form="", raw=None, auto=False):
        """Supplement the preceding task bound to this card, whether queued or already running, with the queued
        message, or ``text`` written for it in its 补充 form (``form``, the form's message: the receipt goes
        there, and a refusal). ``auto``: as it arrived, no click (``steer_on_arrival``). Whether it went in (or may
        have: it never runs then)."""
        target = self.merge_target(ctx, chat, queued, mid, form, arrival=auto)
        if target is None:
            return False
        # The message as written keeps its attachments' lines (prompt_text); changed, what the form says.
        sent = (queued.prompt_text or queued.user_text) if text is None or text == queued.user_text.strip() else text
        shown = queued.user_text if text is None else text
        delivery_id = QUEUED_STEER + queued.message_id
        # Holding the lock keeps the worker from starting this message while it is
        # delivered. If nothing is sent, the message stays queued and runs in order.
        refused = ""
        with queued.execution_lock:
            if queued.execution_started or queued.cancelled_before_start or queued.cancel_event.is_set():
                raise ValueError(f"任务 #{queued.job_id} 已开始或已取消，未作为补充发送")
            # The message is already in the topic history from when it arrived.
            # One that went in as it arrived becomes a round of its own should its task never take it
            # (``requeue_lost``): queued as its sender's, as the message would have run.
            # A message quoted from outside the topic's thread (``top``) keeps its reactions on a turn of its own too.
            again = {"text": shown, "prompt": sent,
                     **({"sender_id": queued.sender_id} if auto and getattr(queued, "sender_id", "") else {}),
                     **({"top": queued.top_message} if getattr(queued, "top_message", "") else {}),
                     # Its 轮次来源, should it run as a turn of its own again (WAVE12 C1; the requeue reads it from W2-B).
                     **({"source": dict(queued.turn_source)} if getattr(queued, "turn_source", None) else {})}
            try:
                saved = self.deliver_steer(
                    ctx, target, sent, delivery_id, record_history=False,
                    receipt=({"kind": "browse", "source": form, "text": shown} if form else
                             {"kind": "job", "key": queued.message_id, "job_id": queued.job_id, "text": shown,
                              **({"auto": True} if auto else {})}),
                    requeue=again)
            except SteerRefused as exc:
                refused = str(exc)
            except SteerUncertain:
                # It may already be in the target task: running it again could repeat the work.
                saved = read_delivery(delivery_path(ctx.state_dir, target.topic_id, delivery_id))
                if saved is None:
                    raise
                self.watch_steer(ctx, delivery_path(ctx.state_dir, target.topic_id, delivery_id))
            if not refused:
                if saved.get("job_id") != target.job_id:
                    raise ValueError("本条补充已绑定其他任务，未重复发送；请检查送达记录")
                queued.cancelled_before_start = True
                queued.cancel_event.set()
        if refused:
            # Nothing was sent: the message stays queued, and its card says why; a form takes the text again.
            if auto:  # Its card's first line says it runs on its own; this one says why, in its own words.
                self.queued_card_notice(ctx, chat, queued, f"没有补充到 #{target.job_id}：{refusal_reason(refused)}", mid,
                                        arrival=True)
                return False
            notice = f"补充没有发出：{refused}；本条继续排队，按顺序执行"
            if not (form and self.cards.repaint_browse_card(ctx, chat, mid, form, self.steer_form_card(
                    target, raw, text=text or "", notice=f"⚠️ {notice}。"))):
                self.queued_card_notice(ctx, chat, queued, notice, mid)
            return False
        note = {"sent": f"已作为补充送入正在执行的任务 #{target.job_id}，不再单独执行",
                "read": f"已作为补充送入正在执行的任务 #{target.job_id}，不再单独执行",
                "held": f"已转为任务 #{target.job_id} 的补充，任务准备好后自动送入；不再单独执行"}.get(
            saved.get("status"), f"已转为任务 #{target.job_id} 的补充，送达未确认；不再单独执行")
        if saved.get("merged"):
            note = f"已并入上一任务 #{target.job_id}，开始后会一并处理；本条不再单独执行"
        self.record_job_state(ctx, queued, "cancelled", note, merged_into_job_id=target.job_id)
        if queued.progress:
            self.cards.close_streaming_reply(ctx, queued.progress)  # Then the whole card becomes the receipt.
        self.runtime.unregister_reply_job(queued)
        if getattr(queued, "top_message", ""):
            # Its card is in the topic's thread: the message itself says it went in (or waits to).
            butler_reactions.top_steered(self.messenger, ctx.profile, queued.top_message, saved.get("status"),
                                         self.runtime.log, auto=auto)
        if form:
            # The receipt is on the form; the queued card says where its message went.
            record = find_job_record(ctx.state_dir, queued.message_id)
            if record and queued.progress:
                self.cards.replace_record_card(ctx, record, closed_card(
                    f"**#{queued.job_id} {note}**\n送达状态见下方补充卡上的回执。", f"#{queued.job_id} 已转为补充"))
            if not self.show_steer_receipt(ctx, saved):
                self.messenger.reply(ctx, chat, note + "。", self.voice.key("queued-steer-ack", mid), voice=self.voice)
            return True
        if not (queued.progress and self.show_steer_receipt(ctx, saved)):
            self.messenger.reply(ctx, chat, note + "。", self.voice.key("queued-steer-ack", mid), voice=self.voice)
        return True

    def watch_steer(self, ctx, path, seconds=STEER_WATCH_SECONDS):
        """Keep reading a task runner's late acknowledgement and repaint the receipt once it settles."""
        def watch():
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline and not self.runtime.shutdown.wait(2):
                with resource_lock("question", path):
                    record = read_delivery(path)
                    if record is None or record.get("status") not in {"submitting", "uncertain"}:
                        return
                    record = reconcile(path, record)
                if record.get("status") not in {"submitting", "uncertain"}:
                    self.show_steer_receipt(ctx, record)
                    top = butler_reactions.steered_top(record)
                    if top and record.get("status") in ("sent", "read"):
                        # Confirmed late: a message quoted from outside the topic's thread says it went in.
                        butler_reactions.put(self.messenger, ctx.profile, top, DONE, "top confirmed", self.runtime.log)
                    return
        background.start(background.CARD_UPDATES, watch)

    def mark_steer_read(self, ctx, job, event_id):
        if not event_id:
            return
        record = update_delivery(delivery_path(ctx.state_dir, job.topic_id, event_id),
                                 status="read", read_at=time.time(), error="")
        if record and record.get("receipt"):
            # Off the event thread: a card update must not hold back the task's next events.
            background.start(background.CARD_UPDATES, self.show_steer_receipt, ctx, record)

    def note_latest_request(self, ctx, job, text, mid="", *, after=""):
        """The task card names the newest supplement it received, above the original request, in the
        words of its delivery record: what the user wrote, not the background sent with it. ``after``: what became of
        it since (app/steer_interrupt.py: the command it waited on was stopped)."""
        if not job.progress:
            return
        record = read_delivery(delivery_path(ctx.state_dir, job.topic_id, mid)) if mid else None
        preview = " ".join(shown_preview(record if record else text).split())
        preview = preview[:80] + ("…" if len(preview) > 80 else "")
        at = time.strftime('%H:%M', time.localtime(float((record or {}).get("sent_at") or time.time())))
        self.cards.set_task_element(ctx, job, latest_request(f"📝 **最新补充** {at} · {literal(preview)}"
                                                             + (f" · {after}" if after else "")),
                                    before="current_question")

    def deliver_steer(self, ctx, job, text, mid, *, record_history=True, receipt=None, requeue=None, later=None,
                      shown="", asked=""):
        """Send a supplement once. Returns its delivery record, "held" while the task is still starting
        (sent by itself once it can take it); SteerRefused means nothing was sent, SteerUncertain that it
        may have arrived (it is never sent again automatically). A list ``later`` gets the task card's
        note of it instead of the note being written now: the caller shows its receipt first. ``shown``: what the
        card and the topic's history say instead of ``text`` (the butler's hand-on, app/butler_handoff.py). ``asked``:
        the owner's words of it, which a turn held for them notes for its "你问" block once it takes it in
        (``merge_before_start``); '' (a forward, an image, a link with no words) notes nothing. ``requeue``: what it is
        queued as should the turn never take it (``requeue_lost``): ``text``, ``prompt``, ``sender_id``, ``top`` and its
        轮次来源 ``source`` (WAVE12 C1: none for the owner's message). A supplement is the owner's
        words (a source that is not his never goes into a running task, core/turn_source.py): one that went, or may have
        gone, into a project lead's wake-up turn makes it a turn he is in (app/project_loop.py owner_steered, W0-2)."""
        try:
            saved = self._deliver_steer(ctx, job, text, mid, record_history=record_history, receipt=receipt,
                                        requeue=requeue, later=later, shown=shown, asked=asked)
        except SteerUncertain:
            self._owner_in(ctx, job)
            raise
        if isinstance(saved, dict) and (saved.get("status") in ("sent", "read", "held") or saved.get("merged")):
            self._owner_in(ctx, job)
        return saved

    def _owner_in(self, ctx, job):
        from covey.app import project_loop

        project_loop.owner_steered(ctx.state_dir, job, self.runtime.log)

    def _deliver_steer(self, ctx, job, text, mid, *, record_history=True, receipt=None, requeue=None, later=None,
                       shown="", asked=""):
        stage = self.steer_stage(job)
        if stage == "ended":
            raise SteerRefused("任务已结束或已取消")
        if stage == "paused":
            # A paused Codex turn takes no input; sending one would fail the question it waits on.
            raise SteerRefused("任务正在等待回答问题，请先回答")
        control = job.backend_control
        if control is not None and not getattr(control, "supports_steer", True):
            raise SteerRefused("此后端暂不支持执行中补充，请发送下一轮要求")
        path = delivery_path(ctx.state_dir, job.topic_id, mid)
        with resource_lock("question", path):
            saved = read_delivery(path)
            if saved is not None:
                saved = reconcile(path, saved)
                if saved.get("status") in {"sent", "read", "held"} or saved.get("requeue_auto"):
                    return saved  # requeue_auto: a turn of its own already (requeue_lost), the same message again.
                if saved.get("status") in {"submitting", "uncertain"}:
                    raise SteerUncertain("这条补充之前已提交，送达还没确认；不会重复发送，可点“检查送达状态”核对")
            saved = {"status": "submitting", "text": text, "topic_id": job.topic_id, "chat_id": job.chat_id,
                     "job_id": job.job_id, "job_key": job.message_id, "token": delivery_token(job.topic_id, mid),
                     "mid": mid, "saved_at": time.time(), **({"receipt": receipt} if receipt else {}),
                     **({"requeue": requeue} if requeue else {}), **({"reads": True} if self.reports_reads(ctx) else {}),
                     **({"stops": True} if self.stops_commands(ctx) else {}),
                     **({"shown": shown} if shown else {})}
            save_delivery(path, saved)
        merged = self.merge_before_start(ctx, job, text, mid, path, asked=asked)
        if merged is not None:
            if record_history:
                append_history(ctx.history_path, "user", shown or text, {"message_id": mid})
            return merged
        # A task on a device cannot read this machine's incoming_media: its files go there first, the text names the copies
        # (the delivery record and the history keep what was written). A queued task read none of it: its request does.
        sent = self.device_text(ctx, job, text, mid)
        try:
            if job.cancel_event.is_set() or job.answer_finalized:
                raise SteerRefused("任务已结束或已取消")
            if control is None:
                raise SteerNotReady("任务刚开始，后端还没准备好接收补充")
            outcome = control.steer(sent, mid)
            timing.mark("delivered")
        except SteerNotReady as exc:
            outcome, reason = "held", str(exc)
            if not getattr(control, "remote", False):
                self.hold_steer(ctx, job, sent, mid, saved=lambda: self._mark_held(path, reason))
        except SteerRefused as exc:
            update_delivery(path, status="refused", error=str(exc))
            raise
        except Exception as exc:
            error = str(exc) if isinstance(exc, (ValueError, SteerUncertain)) else "后端没有确认收到"
            op, run_dir = getattr(exc, "op", ""), getattr(exc, "run_dir", "")
            update_delivery(path, status="uncertain", error=error, op=op, run_dir=run_dir)
            raise SteerUncertain(error, op=op, run_dir=run_dir) from exc
        else:
            reason = "任务刚开始，还没准备好接收补充"
        if outcome == "held":
            current = self._mark_held(path, reason)
            if record_history:
                append_history(ctx.history_path, "user", shown or text, {"message_id": mid})
            return current
        # Claude's replay may already have marked it read.
        current = update_delivery(path, status="sent", error="") or read_delivery(path) or saved
        if record_history:
            append_history(ctx.history_path, "user", shown or text, {"message_id": mid})
        if not getattr(control, "remote", False):  # A task runner notes it on its own card, and watches it.
            note = functools.partial(self.note_latest_request, ctx, job, text, mid)
            if later is None:
                note()
            else:
                later.append(note)
            self.watch_unread_safely(ctx, job, mid)
        return current

    def device_text(self, ctx, job, text, mid):
        """``text`` for the running task of ``job``: for a task on a device, with the files it names put there
        (app/device_files.py ``stage_message``); the line about files left out is a reply in the topic."""
        if not (getattr(job, "device", "") or getattr(ctx, "device", "")):
            return text
        return device_files.stage_message(
            ctx, job, text, log=self.runtime.log,
            say=lambda line: self.messenger.reply(ctx, job.chat_id, line, self.voice.key("device-steer-files", mid),
                                                  voice=self.voice))

    def merge_before_start(self, ctx, job, text, mid, path, *, asked=""):
        """A task that has not started reads its request when it starts: the supplement joins the request.
        Returns the delivery record, or None once the task has started (it is sent the usual way). A turn held for
        the owner's words adds ``asked`` (his words of it, never its attachments block or quoted message) to its
        ``asked_text``."""
        lock = job.execution_lock
        if lock is None or self.steer_stage(job) != "queued":
            return None
        with lock:  # The worker or the hand-off to a task runner takes it before the request is read.
            if job.execution_started or job.cancelled_before_start or job.cancel_event.is_set():
                return None
            job.prompt_text = (job.prompt_text or job.user_text) + "\n\n用户在任务开始前补充：\n" + text
            fields = {}
            if asked.strip() and job.start_held_until:
                # A turn held for the owner's words (app/butler.py hold_for): these are what he asked.
                job.asked_text = "\n".join(part for part in (job.asked_text, asked.strip()) if part)
                fields = {"asked_text": job.asked_text}
            upsert_job_record(ctx.state_dir, job.message_id, prompt_text=job.prompt_text, **fields)
            update_queued(ctx.state_dir, job)  # A restart before it starts queues the request with it.
            current = update_delivery(path, status="sent", merged=True, error="") or read_delivery(path) or {}
            try:
                self.note_latest_request(ctx, job, text, mid)  # Under the lock: the card is still this process's.
            except Exception as exc:  # noqa: BLE001 - the supplement is in the request
                self.runtime.log(f"latest supplement not shown: {type(exc).__name__}")
        return current

    @staticmethod
    def _mark_held(path, reason):
        """Saved, not sent yet; a sender that got there first (sent, read) is never overwritten."""
        return update_delivery(path, status="held", held_at=time.time(), error=reason) or read_delivery(path) or {}

    def hold_steer(self, ctx, job, text, mid, *, saved=None):
        """Keep a supplement the task cannot take yet and try it once a second until it can. Only a
        not-ready refusal (nothing sent) is tried again, so it is never sent twice."""
        with job.held_lock:
            job.held_steers[mid] = text
            if saved is not None:
                saved()  # Recorded as held before the sender can mark it sent.
            thread = job.held_thread
            if thread is None or not thread.is_alive():
                job.held_thread = threading.Thread(target=self._send_held, args=(ctx, job), name="steer-hold",
                                                   daemon=True)
                job.held_thread.start()

    def _send_held(self, ctx, job):
        while not self.runtime.shutdown.wait(1):
            with job.held_lock:
                if not job.held_steers:
                    return
                if self.runtime.jobs.find(job.job_id) is not job:
                    self.drop_held_steers(ctx, job)
                    return
                if self.steer_stage(job) != "running":
                    continue  # Still starting, or waiting for an answer: nothing is sent until it can take it.
                control = job.backend_control
                if not getattr(control, "supports_steer", True):
                    self.drop_held_steers(ctx, job, "此后端暂不支持执行中补充，这条补充没有发出")
                    return
                for mid, text in list(job.held_steers.items()):
                    try:
                        outcome = control.steer(text, mid)
                    except SteerNotReady:
                        break  # Still starting: try again in a second.
                    except SteerRefused as exc:
                        job.held_steers.pop(mid)
                        self.settle_steer(ctx, job, mid, status="refused", error=str(exc))
                    except Exception:  # noqa: BLE001 - it may have arrived: never sent again
                        job.held_steers.pop(mid)
                        self.settle_steer(ctx, job, mid, status="uncertain", error="后端没有确认收到")
                    else:
                        job.held_steers.pop(mid)
                        if outcome == "held":
                            continue  # The task runner keeps it now and records what becomes of it.
                        self.settle_steer(ctx, job, mid, status="sent", error="")
                        try:
                            self.note_latest_request(ctx, job, text, mid)
                        except Exception as exc:  # noqa: BLE001 - the supplement is delivered
                            self.runtime.log(f"latest supplement not shown: {type(exc).__name__}")
                        self.watch_unread_safely(ctx, job, mid)

    def settle_steer(self, ctx, job, mid, **changes):
        record = update_delivery(delivery_path(ctx.state_dir, job.topic_id, mid), **changes)
        top = butler_reactions.steered_top(record)
        if top:
            # Held for its task (排队中 on the message): sent, or not (a turn of its own puts its reactions on after this).
            butler_reactions.top_settled(self.messenger, ctx.profile, top, record.get("status"), self.runtime.log)
        if record and record.get("receipt"):
            if record.get("status") == "refused" and on_arrival(record):
                # Taken off the queue as it arrived, then never sent: a round of its own, marked before it is shown.
                self.requeue_lost(ctx, job, [record])
            background.start(background.CARD_UPDATES, self.show_steer_receipt, ctx, record)
        elif not record or internal(record):
            return  # The Bridge's own reminder: nobody's words, nothing to queue or say.
        elif record.get("status") in {"refused", "uncertain"} and (handed(record) or not traits_of(job).lists_supplements):
            # Words handed to the topic, refused late (held, then never sent): its next turn, as the turn's end has it
            # (``end_unread_steers``). Uncertain may have arrived (rule 6), and one with nothing to queue: a line in its thread.
            if not (record.get("status") == "refused" and self.requeue_lost(ctx, job, [record])):
                background.start(background.CARD_UPDATES, self.tell_topic_lost, ctx, job, record)
        elif record.get("status") in {"refused", "uncertain"} and traits_of(job).lists_supplements:
            # Refused was never sent: a turn of its own. Uncertain may have arrived (rule 6): only said.
            if not (record.get("status") == "refused" and self.requeue_lost(ctx, job, [record])):
                background.start(background.CARD_UPDATES, self.tell_butler_lost, ctx, job, record)

    def requeue_lost(self, ctx, job, records):
        """Butler supplements the turn ``job`` certainly never took (refused: never sent; or ended unread on a
        harness that reports reads) become turns of their own, as if the owner had just sent them: each is marked
        ``requeue_auto`` (core/deliveries.py marked) and queued by ``queue_lost``, here at once, from a task runner by
        its Bridge once the runner is done (app/detached.py), or at the Bridge's next start when none was there to
        (``queue_stranded``). A topic's messages that went in as they arrived (``on_arrival``) the same, marked
        ``requeue_next``, each queued as its receipt's 重新排队 would queue it. Not after a stop (the owner stopped the turn and what
        came with it) or for a record from before its ``requeue`` was saved: those are said as before. Returns the
        records it marked."""
        if job.cancel_event.is_set() and not getattr(job, "hung_stop", None):
            return []  # The owner's stop. The Bridge's of a hung turn (app/unstick.py) is for what waited to run.
        found = []
        for record in records:
            if not record.get("requeue") or not record.get("mid") or internal(record):
                continue
            path = delivery_path(ctx.state_dir, record.get("topic_id") or job.topic_id, record["mid"])
            # A topic's message that went in as it arrived, and words handed to a topic, have a mark of their own
            # (core/deliveries.py marked): queued in the topic, never as the butler's.
            current = update_delivery(path, **{"requeue_next" if in_topic(record) else "requeue_auto": True})
            if current and REQUEUE in offers(current):
                found.append(current)
        if found and not self.runtime.in_runner:
            background.start(background.CARD_UPDATES, self.queue_lost, ctx, job)
        return found

    def queue_lost(self, ctx, job):
        """Queue the butler supplements of ``job`` marked ``requeue_auto`` as turns of their own, oldest first,
        each once: its message id is the new turn's, so its reactions, card and answer are a new message's. The
        Bridge's only (a task runner never queues a job). The intent is saved under the record's lock and queuing
        runs outside it (rule 1), the same ``requeue_state`` a 重新排队执行 click keeps: one that went is never
        queued again, after a restart neither. Returns the jobs queued."""
        if self.runtime.in_runner:
            return []
        since = min((value for value in (getattr(job, "timings", None) or {}).values()
                     if isinstance(value, (int, float)) and value > 0), default=0)
        return self._queue_marked(ctx, switched(lost(ctx.state_dir, job.message_id, since)), job.chat_id, f"#{job.job_id}")

    def queue_stranded(self, ctx, running, *, found=None, butler=True):
        """At the Bridge's start (app/detached.py queue_stranded): the marked supplements whose turn ended while no
        Bridge watched it, its runner gone while the Bridge was down or the Bridge stopped before its own
        ``queue_lost`` ran, queued as ``queue_lost`` does, oldest first across turns. Those of a turn still
        ``running`` (an adopted or live runner's job key) or in the registry wait for its end. ``found``: the marked
        records (core/deliveries.py lost), read once for every Harness; this one takes the butler's when it is the
        butler's (``butler``) and those of the topics on its Harness that went in as they arrived (``on_arrival``).
        Returns the jobs queued."""
        if self.runtime.in_runner:
            return []
        found = [(path, record) for path, record in switched(lost(ctx.state_dir) if found is None else found)
                 if record.get("job_key") not in running and not self.runtime.jobs.settling(record.get("job_key"))
                 and (self.topic_harness(ctx, record) == self.harness.spec.name if in_topic(record) else butler)]
        return self._queue_marked(ctx, found, "", "the restart") if found else []

    @staticmethod
    def topic_harness(ctx, record) -> str:
        """The Harness the topic of ``record`` is bound to now (its topic.json); '' when it cannot be read."""
        try:
            return str(load_topic(ctx.state_dir, str(record.get("topic_id") or "")).get("harness_id") or "")
        except (OSError, ValueError, AttributeError):
            return ""

    def requeue_context(self, ctx, record):
        """The context a topic's message that went in as it arrived (``on_arrival``), or words handed to it (``handed``),
        is queued again in: its topic's, as a message in its thread (app/topics.py topic_target), on this Harness; None
        when the topic is gone or on another Harness now (换配置): then it is not queued, and its receipt offers 重新排队
        again. Handed words name the message that addresses their topic (``address``: a project's lead's alias, its
        topic.json having no root message), else its root."""
        try:
            topic = load_topic(ctx.state_dir, str(record.get("topic_id") or ""))
        except (OSError, ValueError):
            return None
        address = str((record.get("requeue") or {}).get("address") or "") if handed(record) else ""
        address = address or str(topic.get("root_id") or "")
        if topic.get("harness_id") != self.harness.spec.name or not address or self.event_context is None:
            return None
        target = self.event_context(self.harness.context or ctx,
                                    {"chat_id": record.get("chat_id"), "message_id": address})
        return target if target.topic_id == record.get("topic_id") else None

    def held_for_question(self, target, chat, path, record) -> bool:
        """Words handed to a topic (``handed``) whose turn never took them, while the topic waits on a question: kept as
        a held note for its answer (``hold``: app/interaction.py, the words are not the owner's answer, W0-1 and C2),
        never a turn of their own that would answer it (WAVE12 W2-B); its record says so (``requeue_state`` held).
        Whether they were."""
        if self.hold is None or not handed(record):
            return False
        again = record.get("requeue") or {}
        mid = "requeue-" + str(record.get("token") or "")[:24]
        if not self.hold(target, again.get("text") or "", chat, mid, again.get("source")):
            return False
        update_delivery(path, requeue_state="held", requeue_next=False)
        return True

    def _queue_marked(self, ctx, found, chat, after):
        """Queue each (path, record) of ``found`` still marked and not tried, in order. An error may have queued it
        (``_requeue``: requeue_state uncertain): never queued again (rule 6), and said once under the owner's
        message, after the queuing. A topic's message that went in as it arrived (``on_arrival``) is queued as
        重新排队 queues it (a message id of its own, ``requeue-…``, the topic's thread), and its receipt says so."""
        queued, unsure = [], []
        with self.queuing_lost:
            for path, _ in found:
                with resource_lock("question", path):
                    record = read_delivery(path)
                    if not record or not marked(record) or REQUEUE not in offers(record):
                        continue
                    record = update_delivery(path, requeue_state="submitting", requeue_at=time.time()) or record
                mid, chat_id = str(record.get("mid") or ""), str(record.get("chat_id") or chat)
                topic = in_topic(record)
                try:
                    if topic:
                        target = self.requeue_context(ctx, record)
                        if target is None:
                            raise ValueError("话题已不在这个 Harness 上")
                        if self.held_for_question(target, chat_id, path, record):
                            self.runtime.log(f"handed supplement held for a question message={mid[-10:]} after={after}")
                            continue
                        record = self._requeue(target, chat_id, path, record)
                    else:
                        event = {"chat_id": chat_id, "message_id": mid}
                        target = (self.event_context(self.harness.context, event) if self.event_context is not None
                                  else dataclasses.replace(ctx, reply_to_message_id=mid))
                        record = self._requeue(target, chat_id, path, record, message_id=mid)
                except ValueError as exc:  # Refused before it was queued, its reason sent: not tried again.
                    current = update_delivery(path, **{"requeue_next" if topic else "requeue_auto": False})
                    if topic:  # Its receipt offers 重新排队 again.
                        current = update_delivery(path, requeue_state="") if (current or {}).get(
                            "requeue_state") == "submitting" else current
                        self._show_safely(ctx, current)
                    self.runtime.log(f"{'topic' if topic else 'main thread'} supplement not queued "
                                     f"message={mid[-10:]}: {exc}")
                    continue
                except Exception as exc:  # noqa: BLE001 - it may be queued: requeue_state uncertain, never again
                    self.runtime.log(f"{'topic' if topic else 'main thread'} supplement queue unconfirmed "
                                     f"message={mid[-10:]}: {type(exc).__name__}")
                    if not topic:  # A topic's receipt says it (``_requeue`` repainted it); no message to say it under.
                        unsure.append((chat_id, mid))
                    continue
                queued.append(record.get("requeued_job_id"))
                self.runtime.log(f"{'topic' if topic else 'main thread'} supplement queued as its own turn "
                                 f"message={mid[-10:]} job=#{record.get('requeued_job_id')} after={after}")
                if topic:
                    self._show_safely(target, record)
        for chat_id, mid in unsure:
            self.tell_requeue_unsure(ctx, chat_id, mid)
        return queued

    def _show_safely(self, ctx, record):
        """``show_steer_receipt`` that only logs what fails: the record says where the supplement is."""
        if not record:
            return
        try:
            self.show_steer_receipt(ctx, record)
        except Exception as exc:  # noqa: BLE001 - the next paint of the receipt shows it
            self.runtime.log(f"steer receipt not repainted: {type(exc).__name__}")

    def tell_requeue_unsure(self, ctx, chat, mid):
        """A lost supplement whose queuing raised may be a turn of its own already: never queued again (rule 6), so
        the owner hears it once under the message, as one the turn may have taken (app/butler.py)."""
        if not mid or not chat:
            return
        try:
            self.messenger.reply(dataclasses.replace(ctx, reply_to_message_id=mid), chat, SUPPLEMENT_UNCERTAIN,
                                 self.voice.key("main-supplement-requeue", mid), voice=self.voice)
        except Exception as exc:  # noqa: BLE001 - the record says uncertain; nothing is queued again
            self.runtime.log(f"main thread supplement notice not sent message={mid[-10:]}: {type(exc).__name__}")

    def tell_topic_lost(self, ctx, job, record):
        """A topic's supplement with no receipt card that its turn did not take and that is not queued again (words handed
        before W2-B, the owner's stop, one that may have arrived: rule 6): said in a line in the topic's thread, never sent
        again by itself. A topic with no thread of its own (a project's lead) says nothing: its card lists them."""
        if not traits_of(job).has_thread:
            return
        preview = " ".join(shown_preview(record).split())
        preview = preview[:40] + ("…" if len(preview) > 40 else "")
        state = ("可能没有送到（送达没确认，不会重发）" if record.get("status") == "uncertain"
                 else "在它读到之前任务就结束了，没有自动接着做")
        self.messenger.reply(ctx, job.chat_id, f"有一条补充没赶上任务 #{job.job_id}：{state}"
                             + (f"（「{literal(preview)}」）" if preview else "") + "；需要的话再说一次。",
                             self.voice.key("topic-supplement-lost", str(record.get("mid") or "")), voice=self.voice)

    def tell_butler_lost(self, ctx, job, record):
        """The butler's supplements have no receipt card (app/butler.py): one the turn did not take is
        said in a line under the owner's message, which is never sent again by itself (rule 6)."""
        mid = str(record.get("mid") or "")
        if not mid:
            return
        self.messenger.reply(dataclasses.replace(ctx, reply_to_message_id=mid), job.chat_id, SUPPLEMENT_LOST,
                             self.voice.key("main-supplement-lost", mid), voice=self.voice)

    def stops_commands(self, ctx):
        """Whether this Harness's tasks can have the command they wait on stopped for a supplement: Claude's on this
        machine can (app/steer_interrupt.py)."""
        return getattr(backend_capabilities(getattr(self.harness, "backend", None), ctx), "command_stop", False) is True

    def reports_reads(self, ctx):
        """Whether this Harness's tasks report a supplement read (steer_read): Claude's do."""
        return getattr(backend_capabilities(getattr(self.harness, "backend", None), ctx), "steer_reads", False) is True

    def end_unread_steers(self, ctx, job, *, listed=False):
        """The task ended. On a harness that reports reads (Claude), a supplement still "sent" was never
        read: its receipt says so and offers 重新排队执行. Others never report reads: theirs stay as they are.
        The butler's have no receipt: they become turns of their own (``requeue_lost``), and so do a topic's
        messages that went in as they arrived (``on_arrival``), their receipts saying it. The fourth way (WAVE12
        W2-B): words handed to a topic (``handed``: the butler's, a lead's, an edit's) become its next turn with the
        轮次来源 they came with, held for a question it waits on instead (``held_for_question``); a topic's other
        supplements with no receipt get a line in its thread (``tell_topic_lost``). The Bridge's own (``internal``)
        neither. ``listed``: a stopped or refused butler turn's card listed its supplements (app/butler_reply.py), so
        they get neither a turn nor a line of their own under the owner's message."""
        since = (getattr(job, "timings", None) or {}).get("backend_requested", 0)
        main, arrived, passed, untold = [], [], [], []
        family = traits_of(job).lists_supplements
        for record in end_unread(ctx.state_dir, job.message_id, since):
            if internal(record):
                continue
            if on_arrival(record):
                arrived.append(record)
            elif record.get("receipt"):
                background.start(background.CARD_UPDATES, self.show_steer_receipt, ctx, record)
            elif family and listed:
                continue  # Its card listed them.
            elif handed(record):
                passed.append(record)
            elif family:
                main.append(record)
            else:
                untold.append(record)
        if arrived:
            # A topic's messages that went in as they arrived: rounds of their own, marked before they are shown.
            self.requeue_lost(ctx, job, arrived)
            for record in arrived:
                background.start(background.CARD_UPDATES, self.show_steer_receipt, ctx, record)
        requeued = {record.get("mid") for record in self.requeue_lost(ctx, job, main)} if main else set()
        for record in main:
            if record.get("mid") not in requeued:
                background.start(background.CARD_UPDATES, self.tell_butler_lost, ctx, job, record)
        requeued = {record.get("mid") for record in self.requeue_lost(ctx, job, passed)} if passed else set()
        for record in [*(record for record in passed if record.get("mid") not in requeued), *untold]:
            background.start(background.CARD_UPDATES, self.tell_topic_lost, ctx, job, record)

    def settle_held_at_end(self, ctx, job):
        """The task ``job`` left the registry (app/app.py process_reply_job; a task runner's end once more after its
        inbox stops, app/runner.py): the supplements it still held were never sent (one that came after its result
        is held, the backend no longer taking any) and are settled at once (``drop_held_steers``: the butler's
         become turns of their own, marked for the Bridge in a task runner), not on the steer-hold
        thread's next tick, which a task runner's exit cut off: they stayed held and the owner heard nothing
        (ACCEPT-344-A). Never raises."""
        reason = "任务已经在收尾，这条补充没有发出" if job.execution_started else "任务在准备好之前就结束了，这条补充没有发出"
        try:
            self.drop_held_steers(ctx, job, reason, listed=job.supplements_listed)
        except Exception as exc:  # noqa: BLE001 - the job has ended all the same
            self.runtime.log(f"held supplements not settled job=#{job.job_id}: {type(exc).__name__}")

    def drop_held_steers(self, ctx, job, reason="任务在准备好之前就结束了，这条补充没有发出", *, listed=False):
        """The task ended before it could take them: they were never sent. ``listed``: a stopped or refused
        butler turn's card listed them (app/butler_reply.py show_dropped): no turn and no line of their own,
        as ``end_unread_steers`` has it."""
        with job.held_lock:
            held, job.held_steers = dict(job.held_steers), {}
        for mid in held:  # In order: the butler's queue as turns of their own in the order they came.
            if listed and traits_of(job).lists_supplements:
                update_delivery(delivery_path(ctx.state_dir, job.topic_id, mid), status="refused", error=reason)
                continue
            self.settle_steer(ctx, job, mid, status="refused", error=reason)
