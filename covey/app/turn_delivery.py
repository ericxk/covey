"""Delivering one finished turn (``TurnDelivery``, app/worker.py ``_deliver_turn`` makes it): its result on the task
card and in the topic, its question forms and the card's closing state, with the small helpers only delivery uses
(``show_outcome``, ``outgoing_markdown``, ``close_streaming_reply_async``). The answer card sent on its own is
app/answer_delivery.py AnswerParts, a quiet butler turn's top-level notices app/quiet_delivery.py QuietDelivery."""
from __future__ import annotations

import dataclasses
import time
from pathlib import Path
from typing import Any

from covey.core.locks import card_lock
from covey.core.turn_words import retry_hint
from covey.core.checkpoint import QUESTION_ONLY_REPLY, checkpoint_reply_display, read_checkpoint
from covey.core.diff import append_diff_summary_note, finalize_job_diff
from covey.core.saved_results import display_saved_reply, saved_delivery_kind, paused_question_delivery_key
from covey.core.jobs import job_key
from covey.core import background, failure, placement
from covey.core.metrics import mark
from covey.feishu.outgoing_images import prepare_outgoing_markdown_images
from covey.core.topics import proxy_topic
from covey.cards.review import answer_lead, outcome_line
from covey.core.proxy import NO_REPLY_MARK, SKIP_MARK
from covey.core.history_storage import append_history
from covey.core.text import safe_text
from covey.core.butler import traits_of, worded
from covey.core.paths import tool_root
from covey.feishu.transport import last_failure, set_failure
from covey.app.outbox import AUDIT_REFUSED_NOTE, record_attempt
from covey.app import auto_retry, butler_reply, project_loop, revisits, saved_send, topic_links
from covey.app.answer_delivery import AnswerParts
from covey.app.quiet_delivery import QuietDelivery

# A finished card's body when its reply was sent apart from it as a file, or gave no lead to show.
DELIVERED_ELSEWHERE = "回答已单独发送在下方。"
# A turn that asked, without words of its own to lead with (a native question, a reply sent as a file):
# what it said and its questions are below the card, also once they are answered (repo review U1).
ASKED_ELSEWHERE = "正文和问题已单独发送在下方。"


def close_streaming_reply_async(ctx: Any, progress: Any, cards: Any) -> None:
    def close_and_mark_disabled() -> None:
        cards.close_streaming_reply(ctx, progress)
        with card_lock(progress):
            progress.disabled, progress.failed_off = True, False

    # Named so a task runner waits for it before exiting (app/runner.py BACKGROUND): an unnamed
    # thread died with the runner and left the card streaming, shown as [生成中...] in the chat list.
    background.start(background.CARD_CLOSE, close_and_mark_disabled)


def show_outcome(cards: Any, ctx: Any, job: Any, status: str, runtime: Any) -> None:
    """The turn's outcome in the task card's status line, with the process panel's end; best effort,
    it never costs the delivery."""
    try:
        cards.show_outcome(ctx, job, outcome_line(status), stopped=status == "cancelled", timed=True)
    except Exception as exc:  # noqa: BLE001 - the answer below still says how the turn ended.
        runtime.log("outcome status update failed: " + type(exc).__name__)


def outgoing_markdown(text: str, ctx: Any) -> str:
    """``text`` with the local images it shows made uploadable (feishu/outgoing_images.py). A device topic's text stays
    as it is: its image paths are the device's, and one that also exists here is not the file meant (the upload of
    device files is not built yet; docs/architecture.md 设备)."""
    if getattr(ctx, "device", ""):
        return text
    return prepare_outgoing_markdown_images(text, profile=ctx.profile, base_dirs=media_base_dirs(ctx))


def media_base_dirs(ctx: Any) -> list[Path]:
    """Where a reply's local image paths may point: the job's workdir and this tool's repository."""
    return [ctx.workdir, tool_root()]


@dataclasses.dataclass
class _Sent:
    """What ``TurnDelivery.send`` did so far: whether the result was delivered, and whether the card shows it."""
    delivered: bool = False
    updated: bool = False


class TurnDelivery(AnswerParts, QuietDelivery):
    """Delivering one finished turn: its result on the task card and in the topic, its question
    forms, and the card's closing state. ``retry``: the automatic retry (app/auto_retry.py
    ``Planned``) the turn gets once its failure is delivered. ``relay``: called with the delivery, whether it was
    delivered and the run's status once the card is finished (app/butler_relay.py after_turn). ``react``: puts a
    reaction on the owner's message of the job (app/butler_reactions.py finish), for a quiet butler turn. Its
    answer card sent on its own is AnswerParts (app/answer_delivery.py), a quiet turn's top-level notices QuietDelivery
    (app/quiet_delivery.py). ``retry_promised``: its text promises a retry on an older model (app/policy_retry.py
    at_stop): the record keeps the result as the card shows it, for the card to take the promise back if it does
    not run. ``ledger``: called like ``relay`` once the result is delivered, to save what a track of the butler
    answered (app/track_digest.py record; never raises)."""

    def __init__(self, job, ctx, turn, *, prefix, cards, runtime, artifacts, questions, steering,
                 record_job_state, send_text, retry=None, relay=None, react=None, retry_promised=False, ledger=None):
        self.job, self.ctx, self.turn, self.prefix = job, ctx, turn, prefix
        self.cards, self.runtime, self.artifacts, self.questions, self.steering = cards, runtime, artifacts, questions, steering
        self.record_job_state, self.send_text, self.retry = record_job_state, send_text, retry
        self.relay, self.react, self.retry_promised, self.ledger = relay, react, retry_promised, ledger
        self.reacted = False  # A quiet butler turn's end reaction is on the owner's message (butler_reply.handed_quietly).
        self.cut = False  # The card took the result cut short (send): the full text went elsewhere.
        self.in_place = False  # A butler turn's verified reply is on its task card, no answer card (butler_reply).
        self.silent = False  # A project lead's wake-up turn: its saved reply is not sent (app/project_loop.py silent).
        self.quiet_revisit = False  # A revisit turn that found nothing new: not sent, not relayed (app/revisits.py silent).
        self.listed = False  # A stopped or refused butler turn's card (or top-level notice) lists its supplements.
        self.display_text = None  # The reply as its card shows it, once deliver has it.
        # The answer card sent on its own (_answer_apart), saved as the job's answer_message_id: the pinned board
        # pins it for a 话题区's result (app/pins.py).
        self.answer_message = ""
        self.audit_file = False  # Feishu's audit refused the saved reply: it went as a file (AnswerParts.refused_as_file).

    def deliver(self, snapshot):
        """Record the finished turn, then show and send its result."""
        job, ctx, turn = self.job, self.ctx, self.turn
        cards, record_job_state = self.cards, self.record_job_state
        job.answer_finalized = True
        mark(job, 'execution_finished')
        # The original public reply is authoritative for history, retry and files.
        # Display-only decoration must never replace that durable result.
        record_job_state(ctx, job, "running", "正在发送本轮结果", final_text=turn.final_text,
                             execution_status=turn.status, delivery_ok=False, native_resume_allowed=turn.allow_resume,
                             checkpoint_question_deferred=job.checkpoint_question_deferred,
                             checkpoint_question_error=job.checkpoint_question_error,
                             checkpoint_question_registration_pending=job.checkpoint_question_registration_pending,
                             **({"policy_stop": turn.outcome.policy_event, "auto_retry_state": "cancelled"}
                                if turn.outcome and turn.outcome.policy_event else {}),
                             **self.failure_fields())
        display_text, delivery_text, question_omitted = self.texts(snapshot)
        # Above a 话题区's question copied to the top level (butler_relay.after_turn); none for a question alone.
        self.display_text = "" if display_text == QUESTION_ONLY_REPLY else display_text
        final_card = cards.render_stream_final(job, self.card_text(display_text), status=turn.status)
        standalone_verified = turn.verified_delivery is not None
        standalone_question = turn.status == "waiting_user" and job.paused_question_delivery is not None
        self.in_place = standalone_verified and butler_reply.answers_in_place(self, final_card, display_text)
        if standalone_verified and not self.in_place:
            # A quiet butler turn with no card that only handed work on: its Get reaction says it (D1).
            self.in_place = butler_reply.handed_quietly(self)
        self.quiet_revisit = standalone_verified and not self.in_place and revisits.silent(self)
        self.silent = self.quiet_revisit or standalone_verified and not self.in_place and project_loop.silent(self)
        self.note_standalone_result(standalone_verified and not self.in_place and not self.silent, standalone_question)
        mark(job, 'delivery_started')
        set_failure(None)
        delivered, updated = self.send(final_card, display_text, delivery_text, standalone_verified,
                                       standalone_question)
        # The task the line answers: the web page's turn finds its record by it (app/web_collect.py pair), not by guessing.
        append_history(ctx.history_path, "assistant", delivery_text, {"job_id": job.job_id})
        execution_status = turn.status
        delivery_failure = last_failure()
        if delivered and execution_status != 'waiting_user':
            mark(job, 'delivered')
        if not delivered:
            refused = delivery_failure is not None and delivery_failure.category == 'audit_rejected'
            turn.status, turn.status_message = "failed", (AUDIT_REFUSED_NOTE if refused else
                                                          f"结果发送失败；{retry_hint()} 可重新投递")
            self.retry = None  # An undelivered failure is redelivered, not retried.
        elif self.retry is not None:
            turn.status_message = auto_retry.status_line(*self.retry)
        awaiting_form = delivered and execution_status == "waiting_user" and job.checkpoint_question_deferred
        record_job_state(ctx, job, "running" if awaiting_form else turn.status,
                             "正文已送达，正在发送问题控件" if awaiting_form else turn.status_message,
                             final_text=turn.final_text, execution_status=execution_status, delivery_ok=bool(delivered),
                             delivery_error=delivery_failure.category if delivery_failure and not delivered else '',
                             delivery_retry_after=delivery_failure.retry_after if delivery_failure and not delivered else 0,
                             **({"answer_message_id": self.answer_message} if self.answer_message else {}),
                             **({"policy_retry_card": final_card} if self.retry_promised and updated else {}),
                             **auto_retry.schedule_fields(self.retry))
        if standalone_verified and (execution_status != 'waiting_user' or not delivered):
            record_attempt(ctx.state_dir, job_key(job.message_id, job.job_id), delivery_failure, delivered=bool(delivered))
        elif standalone_question and not delivered:
            record_attempt(ctx.state_dir, job_key(job.message_id, job.job_id), delivery_failure)
        delivered = self.deliver_questions(delivered, updated, execution_status, final_card, delivery_text,
                                      question_omitted, standalone_verified, standalone_question)
        self.finish_card(delivered, execution_status, final_card, standalone_verified, standalone_question)
        if self.relay is not None and not self.quiet_revisit:
            # A turn the butler started or handed on: its outcome back at the top level (after_turn never raises).
            self.relay(self, delivered, execution_status)
        if self.ledger is not None:
            self.ledger(self, delivered, execution_status)

    def failure_fields(self):
        """Why the turn did not succeed, for its record (core/failure.py failure_view reads them); {} when it did."""
        turn = self.turn
        outcome = getattr(turn, "outcome", None)
        policy = getattr(outcome, "policy_event", None) is not None
        if not (turn.status == "failed" or (turn.status == "cancelled" and policy)) or turn.verified_delivery is not None:
            return {}  # Done, or stopped by the owner: no reason to give.
        code = str(getattr(outcome, "error_code", "") or "root_failed") if outcome is not None else "root_failed"
        return {"failure_code": code, "failure_detail": str(getattr(turn, "failure_detail", "") or "")}

    def card_text(self, display_text):
        """The body of a failed task card: the reason and what comes next in one line (core/failure.py), its old
        words in 技术详情 under it (app/auto_retry.py failed_controls). Not a policy stop's: its words say who was
        refused doing what already, and a retry's promise in them is taken back there (app/policy_retry.py)."""
        job, turn = self.job, self.turn
        if (turn.status != "failed" or turn.verified_delivery is not None
                or getattr(getattr(turn, "outcome", None), "policy_event", None) is not None or not job.progress):
            return display_text
        view = failure.failure_view({"status": turn.status, "topic_id": job.topic_id, "job_id": job.job_id,
                                     "status_message": turn.status_message, **self.failure_fields(),
                                     **auto_retry.schedule_fields(self.retry)}, now=time.time())
        return failure.card_body(view) if view else display_text

    def texts(self, snapshot):
        """The text shown for the result and the text kept as delivered, and whether the shown one
        leaves out a question that its form carries."""
        job, ctx, turn, runtime = self.job, self.ctx, self.turn, self.runtime
        question = str(read_checkpoint(Path(job.checkpoint_path)).get("question") or "")
        display_text = checkpoint_reply_display(turn.final_text, question,
            turn.status == "waiting_user" and job.checkpoint_question_deferred)
        question_omitted = display_text != turn.final_text
        delivery_text = turn.final_text
        if turn.verified_delivery is not None and turn.verified_outcome.status != "completed":
            display_text = display_saved_reply(turn.verified_delivery, turn.verified_outcome)
            delivery_text = display_text
        display_text = outgoing_markdown(display_text, ctx)
        if traits_of(job).linkify_body:
            # The owner taps a T number on the butler's card to open that topic (display only:
            # delivery_text, the saved reply, stays as it was; app/topic_links.py).
            display_text = topic_links.linkify(display_text, ctx.state_dir, job.chat_id, profile=ctx.profile,
                                               skip=(job.topic_id,))
        display_text = butler_reply.unfollowed(self, display_text)  # #25: put off with no topic to bring it back.
        if snapshot is not None:
            try:
                # Local artifacts only: diffs are never published to Feishu automatically.
                diff = finalize_job_diff(snapshot, ctx.state_dir, job)
                job.diff_artifact_path, job.diff_json_path = diff.artifact_path, diff.json_path
                display_text = append_diff_summary_note(display_text, diff)
            except Exception as exc:  # noqa: BLE001 - isolate backend and delivery failures per job.
                runtime.log(f"local diff recording failed: {exc}")
        return display_text, delivery_text, question_omitted

    def note_standalone_result(self, standalone_verified, standalone_question):
        """A result or question sent apart from the card: say so on the card first."""
        job, ctx, turn = self.job, self.ctx, self.turn
        cards, runtime, steering = self.cards, self.runtime, self.steering
        if (standalone_verified or standalone_question) and job.progress:
            try:
                note = "回答已保存，正在单独发送。"
                if standalone_question:
                    note = "本轮需要补充信息，问题正文和控件将在话题中投递。"
                elif turn.verified_outcome.error_code == "background_timeout":
                    note = "已到任务的时间上限，本轮最后一条完整回复将单独发出。"
                elif turn.verified_outcome.status == "cancelled":
                    note = "任务已停止，停止前最后一条完整回复将单独发出。"
                elif turn.verified_outcome.status != "completed":
                    note = "本轮未全部完成，已保存的主任务回复将单独投递；原生会话不可续接。"
                self._paint(cards.render_stream_final(job, note, status=turn.status, answer_elsewhere=True),
                            "saved-result-status")
                steering.close_task_controls(ctx, job)
                cards.finalize_trace(ctx, job, stopped=turn.status == "cancelled")
            except Exception as exc:
                runtime.log("saved result status update failed: " + type(exc).__name__)

    def _paint(self, card, tag):
        """Write ``card`` over the task card at once, never as a replacement card; ``tag`` names the write in its
        idempotency key (``<prefix>-<tag>``). Whether the card took it."""
        return self.cards.update_job_streaming_reply(self.ctx, self.job, card, force=True,
                                                     idempotency_prefix=f"{self.prefix}-{tag}", allow_replacement=False)

    def send(self, final_card, display_text, delivery_text, standalone_verified, standalone_question):
        """Send the result: on the card, as a file when it is too long, or as its own message.
        (delivered, updated): updated when the card shows it. One way each (the strategies below), tried in this
        order; each sets ``sent`` as it goes, so what it did before an exception is what is returned."""
        job = self.job
        sent = _Sent()
        if self.in_place:
            return True, True  # The saved reply is the card's body already (deliver).
        if self.silent:
            self.runtime.log(f"wake-up reply kept, not sent job=#{job.job_id}")
            return True, False  # Saved and counted as delivered: nothing redelivers it (app/project_loop.py silent).
        try:
            sent.updated = not (standalone_verified or standalone_question) and bool(job.progress) and self._paint(final_card, "final")
            if sent.updated:
                self._on_card(sent, final_card, display_text, delivery_text)
            elif standalone_question:
                self._question_apart(sent)
            elif standalone_verified:
                self._answer_apart(sent, delivery_text)
            else:
                self._fallback(sent, final_card, display_text, delivery_text)
        except Exception as exc:  # noqa: BLE001 - isolate backend and delivery failures per job.
            self.runtime.log(f"final delivery failed: {exc}")
        return sent.delivered, sent.updated

    def _on_card(self, sent, final_card, display_text, delivery_text):
        """The card took the result: delivered unless it was cut short, then the whole text as a file or a message."""
        job, ctx, turn, prefix, cards = self.job, self.ctx, self.turn, self.prefix, self.cards
        # A truncated card is not full delivery, including when file upload raises. The card cuts
        # by characters, or by its size, which the write that took the body knows.
        self.cut = cards.exceeds_stream_card_limit(final_card) or cards.body_cut(job)
        sent.delivered = not self.cut
        self.steering.close_task_controls(ctx, job)
        if not cards.finalize_trace(ctx, job, stopped=turn.status == "cancelled"):
            self.runtime.log("Final process panel update failed; preserving existing trace")
        if self.cut:
            sent.delivered = self.artifacts.send_result_file(ctx, job, delivery_text) or self.send_text(ctx, job.chat_id, display_text, f"{prefix}-result-full-{job.message_id}")

    def _question_apart(self, sent):
        """A turn paused on a question: its words as a message (a file when long), the key of its paused question."""
        job, ctx, turn, prefix = self.job, self.ctx, self.turn, self.prefix
        # As each redelivery sends them (app/redelivery.py redeliver_paused_question).
        sent.delivered = saved_send.send_paused(
            ctx, self.top_ctx() if self.bare() else ctx, job, turn.final_text,
            paused_question_delivery_key(prefix, job_key(job.message_id, job.job_id), job.paused_question_delivery),
            send=self.send_text, artifacts=self.artifacts)

    def _fallback(self, sent, final_card, display_text, delivery_text):
        """No card took it and nothing was saved apart: a file when too long for a card, else a message, else a file."""
        job, ctx, prefix, artifacts = self.job, self.ctx, self.prefix, self.artifacts
        if self.bare():
            self._quiet_fallback(sent, final_card, display_text, delivery_text)
            return
        sent.delivered = (artifacts.send_result_file(ctx, job, delivery_text) if self.cards.exceeds_stream_card_limit(final_card) else False) or self.send_text(ctx, job.chat_id, display_text, f"{prefix}-result-{job.message_id}")
        if not sent.delivered:
            sent.delivered = artifacts.send_result_file(ctx, job, delivery_text)

    def deliver_questions(self, delivered, updated, execution_status, final_card, delivery_text, question_omitted,
                          standalone_verified, standalone_question):
        """A delivered turn that waits for an answer gets its question forms; when they fail, the
        original text is restored where it can be. Whether the result still counts as delivered."""
        job, ctx, turn, prefix, cards = self.job, self.ctx, self.turn, self.prefix, self.cards
        runtime, artifacts, questions, record_job_state, send_text = self.runtime, self.artifacts, self.questions, self.record_job_state, self.send_text
        if delivered and execution_status == "waiting_user":
            try:
                question_ok = questions.deliver_checkpoint_questions(ctx, job, bool(updated and not self.cut))
            except Exception as exc:  # A failed form must not cause model execution on retry.
                question_ok = False
                runtime.log(f"checkpoint question delivery failed: {exc}")
            if question_ok:
                mark(job, 'delivered')
            if standalone_question or standalone_verified:
                record_attempt(ctx.state_dir, job_key(job.message_id, job.job_id), last_failure(), delivered=bool(question_ok))
            if not question_ok:
                turn.status = "failed"
                turn.status_message = (f"问题登记失败；{retry_hint()} 可重新投递" if job.checkpoint_question_error
                                  else f"问题控件发送失败；{retry_hint()} 可重新投递")
                # If exact-question de-duplication assumed a form, restore the original
                # text when that form fails. The sole question must remain accessible.
                if question_omitted and turn.final_text.strip():
                    def paint():
                        original_card = cards.render_stream_final(job, turn.final_text, status="failed")
                        return bool(job.progress and not cards.exceeds_stream_card_limit(original_card)
                                    and self._paint(original_card, "original-result")
                                    and not cards.body_cut(job))
                    # As a redelivery restores it (app/redelivery.py), which has no task card to paint.
                    delivered = saved_send.restore_original(
                        ctx, job, turn.final_text, delivery_text, key=f"{prefix}-original-result-{job.message_id}",
                        send=send_text, artifacts=artifacts, log=runtime.log, paint=paint)
                try:
                    send_text(ctx, job.chat_id, worded(f"任务 #{job.job_id} 的问题控件未能发送；原始回复已保留，请{retry_hint()} 重新投递。",
                                                       job),
                                     f"{prefix}-question-delivery-failed-{job.message_id}")
                except Exception as exc:
                    runtime.log(f"question delivery notice failed: {exc}")
            record_job_state(ctx, job, turn.status, turn.status_message, delivery_ok=bool(delivered),
                                 question_delivery_ok=bool(question_ok))
        return delivered

    def delivered_note(self, fallback=DELIVERED_ELSEWHERE):
        """The card's body once the verified reply is confirmed sent apart from it: ↓ and that reply's
        lead (its first heading or sentence, ``answer_lead``), so the card says what came of the turn
        (not only "回答已单独发送在下方…"), a
        turn that asked too. Taken from the
        authorized text that was sent, nothing else. A reply sent as a file, or without words to take,
        keeps the fixed sentence (``fallback``). (body, lead): the lead in bold ("**↓ lead**", read first
        once the body moves to the top), on a dashboard card under a bold 结论 (its body is at the top
        already), with the plain lead, or a fixed sentence with "": the
        fallback and a 代答 turn's "↓ 不代你回复…" lines, which the topic's ⏸ card or line explains."""
        reply, outcome = self.turn.verified_delivery, self.turn.verified_outcome
        # A reply the audit refused went as a file (AnswerParts.refused_as_file): its words stay off the card too.
        lead = (answer_lead(reply.text) if reply is not None and saved_delivery_kind(reply, outcome) != "file"
                and not self.audit_file else "")
        if lead and proxy_topic(self.ctx):
            # A 代答 turn that sends nothing leads with its control mark, which the topic's ⏸ card or line
            # below says in words (repo review U5: "↓ [不回复]").
            text = reply.text.strip()
            if text.startswith(SKIP_MARK):
                return "↓ 不代你回复：原因见下方这一轮的回答", ""
            if text.startswith(NO_REPLY_MARK):
                return "↓ 不用回：原因见下方这一轮的回答", ""
        if not lead:
            return fallback, ""
        if getattr(self.job.progress, "dashboard", False):
            # Under the status line and the tiles already: a 「结论」 heading over it (the A 仪表盘 sample).
            return "**结论**\n" + safe_text(lead, None), lead
        # safe_text escapes the lead's own * and _, so the bold cannot be closed early.
        return "**↓ " + safe_text(lead, None) + "**", lead

    def show_lead(self, lead, shown):
        """A body that is the sent reply's lead: moved to the top of the card (``shown``: the body took
        it), and the chat list's preview of the card once it closes (Cards.set_lead_summary, which computes
        it: rule 3). Best effort: the card keeps its order and its header."""
        job, cards, runtime = self.job, self.cards, self.runtime
        cards.set_lead_summary(job, self.turn.status)
        if shown:
            try:
                cards.lead_first(self.ctx, job)
            except Exception as exc:
                runtime.log("lead move failed: " + type(exc).__name__)

    def finish_card(self, delivered, execution_status, final_card, standalone_verified, standalone_question):
        """The task card's last words: delivery receipt, failure notice and result controls, then
        it stops streaming."""
        job, ctx, turn, prefix = self.job, self.ctx, self.turn, self.prefix
        cards, runtime, record_job_state, send_text = self.cards, self.runtime, self.record_job_state, self.send_text
        if turn.status != execution_status:
            # Delivery failed after the run's outcome was shown: the top first, so the label below need not repeat it.
            show_outcome(cards, ctx, job, turn.status, runtime)
        if not delivered and turn.status == "failed" and self.bare():
            self.undelivered_notice(execution_status)
        if job.progress and delivered and (standalone_verified or standalone_question) and turn.status != "failed" \
                and not self.in_place:
            # Only a confirmed delivery (including question controls) may say sent.
            note, lead = self.delivered_note(ASKED_ELSEWHERE if turn.status == "waiting_user" else DELIVERED_ELSEWHERE)
            if self.answer_message and turn.status != "waiting_user" and \
                    self.headed(placement.plan(ctx.state_dir, job).row):
                # A 话题区's answer card in its thread is the turn's result, its status atop it: the task card keeps
                # its status and 输入/输出, no 结论.
                note, lead = " ", ""
            shown = False
            try:
                shown = self._paint(cards.render_stream_final(job, note, status=turn.status, answer_elsewhere=True), "result-delivered")
            except Exception as exc:
                runtime.log("delivery receipt update failed: " + type(exc).__name__)
            if lead and job.progress:
                self.show_lead(lead, shown is True)
        elif job.progress and delivered and standalone_verified and execution_status == "failed":
            try:
                self._paint(cards.render_stream_final(job,
                        "本轮未全部完成；已保存的主任务回复已送达，原生会话不可续接。",
                        status="failed"), "result-delivered")
            except Exception as exc:
                runtime.log("partial delivery receipt update failed: " + type(exc).__name__)
        # A quiet butler turn's refusal is said at the top level as its failure is (D1).
        refused = execution_status != "failed" and self.refused() and placement.quiet(ctx.state_dir, job)
        if (job.progress and delivered and not (self.cut or cards.exceeds_stream_card_limit(final_card))
                and (execution_status == "failed" or refused)):
            try:
                notice_text = (f"任务 #{job.job_id}：任务失败。已保存的主任务回复见本话题正文或文件。" if standalone_verified
                               else f"任务 #{job.job_id}：任务失败。{auto_retry.status_line(*self.retry)}。" if self.retry
                               else f"任务 #{job.job_id}：任务失败。请查看本话题的结果卡片。")
                if refused or placement.quiet(ctx.state_dir, job):
                    # The card is in the owner's message's thread: a failure or a refusal is said at the top level too
                    # (D1); the card lists the supplements a refusal took (butler_reply.show_dropped).
                    notified = self.top_notice(worded(self.notice_words(), job), f"{prefix}-terminal-{job.message_id}")
                else:
                    notified = send_text(ctx, job.chat_id, worded(notice_text, job),
                                         f"{prefix}-terminal-{job.message_id}")
            except Exception as exc:  # noqa: BLE001 - notification failure must never rerun completed edits.
                notified = False
                runtime.log(f"terminal notification failed: {exc}")
            record_job_state(ctx, job, turn.status, turn.status_message, notification_delivered=bool(notified))
        if job.progress and turn.status == "failed" and (not delivered or execution_status == "waiting_user"):
            # Best-effort failure labelling is not proof of full result delivery.
            notice = worded(f"完整结果尚未成功发送，原始回复已保留。请{retry_hint()} 重新投递。" if not delivered
                            else f"正文已送达，但问题控件未能发送。请{retry_hint()} 重试问题交付。",
                            job)
            try:
                failed_body = notice if standalone_verified else notice + "\n\n" + turn.final_text
                failed_display = cards.render_stream_final(job, failed_body, status="failed")
                self._paint(failed_display, "delivery-failed")
            except Exception as exc:
                runtime.log(f"delivery failure label unavailable: {exc}")
        # A butler turn stopped or refused: its card lists the supplements it took (a bare turn's top-level
        # notice did already, _quiet_fallback).
        self.listed = butler_reply.show_dropped(self) or self.listed
        if job.progress and (delivered or turn.status == "failed"):
            try:
                cards.finish_result_controls(ctx, job, turn.status)
            except Exception as exc:
                runtime.log(f"result controls unavailable: {exc}")
        if job.progress and delivered:
            butler_reply.say_outcome(self)  # A butler card says what the turn did (V6), not 已完成 · 用时.
        if job.progress:
            # Closing disables further updates; finish delivery labels and controls first.
            close_streaming_reply_async(ctx, job.progress, cards)
