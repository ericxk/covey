"""Ordinary topic-job execution, local records and in-topic final delivery: ``process_bridge_reply_job`` runs a
turn, settles how it ended (app/turn_settle.py) and delivers its result (``TurnDelivery``: the task card, the topic,
question forms, the card's closing state). TurnDelivery's answer card sent on its own is app/answer_delivery.py
AnswerParts, a quiet main-thread turn's top-level notices app/quiet_delivery.py QuietDelivery."""
from __future__ import annotations

from covey.core.trace import task_trace
from covey.core.turn_words import retry_hint

from covey.core.locks import card_lock

import dataclasses
import datetime as _dt
import functools
import json
import subprocess
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

from covey.core.checkpoint import QUESTION_ONLY_REPLY, checkpoint_reply_display, read_checkpoint
from covey.core.diff import append_diff_summary_note, capture_job_diff_snapshot, finalize_job_diff
from covey.core.harness import JobCancelled, backend_capabilities, normalize_run_result
from covey.core.saved_results import (display_saved_reply, reruns_after_withdrawal,
                                               observe_verified_reply, saved_delivery_kind,
                                               paused_question_delivery_key)
from covey.core.jobs import SIGTERM_STATUS, context_for_job, job_key
from covey.core import background, placement, timing
from covey.core.metrics import mark, mark_first_output
from covey.core.longtask import SEED_PHASE, TaskMonitor
from covey.core.status_line import STARTING
from covey.feishu.outgoing_images import prepare_outgoing_markdown_images
from covey.core.topics import proxy_topic
from covey.cards.review import answer_lead, outcome_line, stopped_sentence
from covey.core.proxy import NO_REPLY_MARK, SKIP_MARK
from covey.core.state import write_json
from covey.core.history_storage import append_history
from covey.core.logs import append_private_text
from covey.core.text import safe_text
from covey.core.main_thread import traits_of, worded
from covey.core.paths import tool_root
from covey.feishu.transport import last_failure, set_failure
from covey.app.outbox import AUDIT_REFUSED_NOTE, record_attempt
from covey.app import (auto_retry, main_reactions, main_relay, main_reply, main_tracks, policy_retry, saved_send,
                                topic_links)
from covey.app.answer_delivery import AnswerParts
from covey.app.turn_images import TurnImages
from covey.app.quiet_delivery import QuietDelivery
from covey.app.turn_settle import TurnResult, settle_turn

PROGRESS_START = "正在处理本轮消息"
# A finished card's body when its reply was sent apart from it as a file, or gave no lead to show.
DELIVERED_ELSEWHERE = "回答已单独发送在下方。"
# A turn that asked, without words of its own to lead with (a native question, a reply sent as a file):
# what it said and its questions are below the card, also once they are answered (repo review U1).
ASKED_ELSEWHERE = "正文和问题已单独发送在下方。"
STOPPED_NOTE = "已保存的结果和记录保留在本话题。"  # After "任务 #N 已停止。" (stopped_sentence).


def format_received_progress_event(event: str, received_at: float) -> str:
    timestamp = _dt.datetime.fromtimestamp(received_at).astimezone().strftime("%m-%d %H:%M:%S")
    return f"{timestamp}：{event}"

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


def media_base_dirs(ctx: Any) -> list[Path]:
    """Where a reply's local image paths may point: the job's workdir and this tool's repository."""
    return [ctx.workdir, tool_root()]


class TurnProgress:
    """The progress feed of one turn: each event goes to the job record and job-progress.jsonl; the task
    card shows the turn's trace, not these lines."""

    def __init__(self, job, ctx, *, record_job_state):
        self.job, self.ctx, self.record_job_state = job, ctx, record_job_state

    def __call__(self, event: str, force: bool = False) -> None:
        if not str(event or "").strip():
            return
        event = prepare_outgoing_markdown_images(event, profile=self.ctx.profile, base_dirs=media_base_dirs(self.ctx))
        rendered_event = format_received_progress_event(event, time.time())
        append_private_text(self.ctx.history_path.parent / "job-progress.jsonl", json.dumps(
            {"job_id": self.job.job_id, "message_id": self.job.message_id, "event": rendered_event}, ensure_ascii=False) + "\n")
        self.record_job_state(self.ctx, self.job, "running", event)
        if self.job.progress and not self.job.progress_body_cleared:
            # The queued text leaves the body with the first status line write (Cards.update_card_activity),
            # in one call: the backend starts without waiting for it (S33: 3 s), and 执行中 does not
            # come a call later (S34: 2 s). A streamed answer replaces it first.
            self.job.progress_body_cleared = self.job.body_clear_pending = True


@dataclasses.dataclass
class _Sent:
    """What ``TurnDelivery.send`` did so far: whether the result was delivered, and whether the card shows it."""
    delivered: bool = False
    updated: bool = False


class TurnDelivery(AnswerParts, QuietDelivery):
    """Delivering one finished turn: its result on the task card and in the topic, its question
    forms, and the card's closing state. ``retry``: the automatic retry (app/auto_retry.py
    ``Planned``) the turn gets once its failure is delivered. ``relay``: called with the delivery, whether it was
    delivered and the run's status once the card is finished (app/main_relay.py after_turn). ``react``: puts a
    reaction on the owner's message of the job (app/main_reactions.py finish), for a quiet main-thread turn. Its
    answer card sent on its own is AnswerParts (app/answer_delivery.py), a quiet turn's top-level notices QuietDelivery
    (app/quiet_delivery.py). ``retry_promised``: its text promises a retry on an older model (app/policy_retry.py
    at_stop): the record keeps the result as the card shows it, for the card to take the promise back if it does
    not run."""

    def __init__(self, job, ctx, turn, *, prefix, cards, runtime, artifacts, questions, steering,
                 record_job_state, send_text, retry=None, relay=None, react=None, retry_promised=False):
        self.job, self.ctx, self.turn, self.prefix = job, ctx, turn, prefix
        self.cards, self.runtime, self.artifacts, self.questions, self.steering = cards, runtime, artifacts, questions, steering
        self.record_job_state, self.send_text, self.retry = record_job_state, send_text, retry
        self.relay, self.react, self.retry_promised = relay, react, retry_promised
        self.reacted = False  # A quiet main-thread turn's end reaction is on the owner's message (main_reply.handed_quietly).
        self.cut = False  # The card took the result cut short (send): the full text went elsewhere.
        self.in_place = False  # A main-thread turn's verified reply is on its task card, no answer card (main_reply).
        self.listed = False  # A stopped or refused main-thread turn's card (or top-level notice) lists its supplements.
        self.display_text = None  # The reply as its card shows it, once deliver has it.
        # The answer card sent on its own (_answer_apart), saved as the job's answer_message_id: the pinned board
        # pins it for a 话题区's result (app/pins.py).
        self.answer_message = ""

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
                                if turn.outcome and turn.outcome.policy_event else {}))
        display_text, delivery_text, question_omitted = self.texts(snapshot)
        # Above a 话题区's question copied to the top level (main_relay.after_turn); none for a question alone.
        self.display_text = "" if display_text == QUESTION_ONLY_REPLY else display_text
        final_card = cards.render_stream_final(job, display_text, status=turn.status)
        standalone_verified = turn.verified_delivery is not None
        standalone_question = turn.status == "waiting_user" and job.paused_question_delivery is not None
        self.in_place = standalone_verified and main_reply.answers_in_place(self, final_card, display_text)
        if standalone_verified and not self.in_place:
            # A quiet main-thread turn with no card that only handed work on: its Get reaction says it (D1).
            self.in_place = main_reply.handed_quietly(self)
        self.note_standalone_result(standalone_verified and not self.in_place, standalone_question)
        mark(job, 'delivery_started')
        set_failure(None)
        delivered, updated = self.send(final_card, display_text, delivery_text, standalone_verified,
                                       standalone_question)
        append_history(ctx.history_path, "assistant", delivery_text)
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
        if self.relay is not None:
            # A turn the main thread started or handed on: its outcome back at the top level (after_turn never raises).
            self.relay(self, delivered, execution_status)

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
        display_text = prepare_outgoing_markdown_images(display_text, profile=ctx.profile, base_dirs=media_base_dirs(ctx))
        if traits_of(job).linkify_body:
            # The owner taps a T number on the main thread's card to open that topic (display only:
            # delivery_text, the saved reply, stays as it was; app/topic_links.py).
            display_text = topic_links.linkify(display_text, ctx.state_dir, job.chat_id, profile=ctx.profile,
                                               skip=(job.topic_id,))
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
        # by characters, or by its size, which the write that took the body knows (#1150).
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
        (the 2026-10-27 UI review: all 644 finished cards of a week said only "回答已单独发送在下方…"), a
        turn that asked too (repo review U1: half the turns of the owner's chats). Taken from the
        authorized text that was sent, nothing else. A reply sent as a file, or without words to take,
        keeps the fixed sentence (``fallback``). (body, lead): the lead in bold ("**↓ lead**", read first
        once the body moves to the top, T288), on a dashboard card under a bold 结论 (its body is at the top
        already), with the plain lead, or a fixed sentence with "": the
        fallback and a 代答 turn's "↓ 不代你回复…" lines, which the topic's ⏸ card or line explains."""
        reply, outcome = self.turn.verified_delivery, self.turn.verified_outcome
        lead = answer_lead(reply.text) if reply is not None and saved_delivery_kind(reply, outcome) != "file" else ""
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
                # its status and 输入/输出, no 结论 (D3, 2026-11-08).
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
        # A quiet main-thread turn's refusal is said at the top level as its failure is (D1).
        refused = execution_status != "failed" and self.refused() and placement.quiet(ctx.state_dir, job)
        if (job.progress and delivered and not (self.cut or cards.exceeds_stream_card_limit(final_card))
                and (execution_status == "failed" or refused)):
            try:
                notice_text = (f"任务 #{job.job_id}：任务失败。已保存的主任务回复见本话题正文或文件。" if standalone_verified
                               else f"任务 #{job.job_id}：任务失败。{auto_retry.status_line(*self.retry)}。" if self.retry
                               else f"任务 #{job.job_id}：任务失败。请查看本话题的结果卡片。")
                if refused or placement.quiet(ctx.state_dir, job):
                    # The card is in the owner's message's thread: a failure or a refusal is said at the top level too
                    # (D1); the card lists the supplements a refusal took (main_reply.show_dropped).
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
        # A main-thread turn stopped or refused: its card lists the supplements it took (a bare turn's top-level
        # notice did already, _quiet_fallback).
        self.listed = main_reply.show_dropped(self) or self.listed
        if job.progress and (delivered or turn.status == "failed"):
            try:
                cards.finish_result_controls(ctx, job, turn.status)
            except Exception as exc:
                runtime.log(f"result controls unavailable: {exc}")
        if job.progress and delivered:
            main_reply.say_outcome(self)  # A main-thread card says what the turn did (V6), not 已完成 · 用时.
        if job.progress:
            # Closing disables further updates; finish delivery labels and controls first.
            close_streaming_reply_async(ctx, job.progress, cards)


def _start_turn(ctx, job, *, sessions, runtime, record_job_state):
    """Mark the job started: the topic's native session pointer, a run id, an empty checkpoint and a
    trace (every backend reports structured events). Returns (revision, session_id) of the session."""
    runtime.log(f"starting reply job #{job.job_id} message_id={job.message_id}")
    timing.mark("started")
    record_job_state(ctx, job, "running", "后台 worker 已开始处理")
    revision, session_id = sessions.capture_session(ctx)
    job.session_id = session_id
    job.bridge_run_id = str(uuid.uuid4())
    job.verified_reply = None
    job.checkpoint_path = str(ctx.state_dir / "checkpoints" / (job_key(job.message_id, job.job_id) + ".json"))
    write_json(Path(job.checkpoint_path), {"phase":SEED_PHASE, "completed":[],
        "artifacts":[], "external_tasks":[], "next_step":"检查任务要求与已有状态", "requires_user":False}, ensure_ascii=False)
    # Without a trace until its first event, a status line could say only "<后端> 已开始处理", with no
    # 已运行 (S28 #849), and its progress text showed as a 执行进度 block.
    job.tool_trace = task_trace()
    job.tool_trace.checkpoint = job.checkpoint_path
    return revision, session_id


def _task_monitor(ctx, job, *, cards, runtime, record_job_state, send_text, prefix, starting=""):
    """The job's activity monitor (not started): the card's activity line, the job record, and the
    long-task questions it sends to the topic. ``starting``: the backend's name, when it says when
    its first model call starts (``model_requests``)."""
    # The images the task lists to show at once (app/turn_images.py), in the voice of its task card's writer.
    messenger = getattr(cards, "messenger", None)
    images = (TurnImages(ctx, job, messenger=messenger, voice=cards.voice, log=runtime.log)
              if messenger is not None and getattr(cards, "voice", None) is not None else None)
    return TaskMonitor(job, Path(job.checkpoint_path),
        lambda content: cards.update_card_activity(ctx, job, content),
        lambda **fields: record_job_state(ctx, job, "running", "任务执行中", **fields),
        lambda text, key: send_text(ctx, job.chat_id, text, f"{prefix}-question-{job.message_id}-{key}"),
        runtime.log, lambda: job.progress and cards.close_streaming_reply(ctx, job.progress),
        starting=STARTING.format(starting) if starting else "",
        state=lambda phase, asking: cards.show_running_summary(ctx, job, phase, asking),
        images=images)


def _generate_reply(ctx, job, session_id, progress, *, runs, record_job_state):
    """Run the turn on the backend; its first output is recorded once, every event goes to ``progress``."""
    mark(job, 'backend_requested')
    timing.mark('backend')
    job.tool_trace.started_at = job.timings['backend_requested']
    record_job_state(ctx, job, 'running', '正在启动后端')
    def backend_progress(event, force=False):
        if mark_first_output(job):
            record_job_state(ctx, job, 'running', '后端已输出首条进展')
        progress(event, force)
    return normalize_run_result(runs.generate_reply_for_worker(ctx, job, session_id, backend_progress))


def finished_before(result) -> bool:
    """The backend completed the turn before a SIGTERM reached it: the result stands and is delivered."""
    return result.outcome is not None and result.outcome.status == "completed"


def leave_interrupted(ctx, job, *, harness, runtime, record_job_state) -> None:
    """A turn a SIGTERM ended before its backend finished (``job.interrupted``, app/runner.py): no outcome,
    result or notice. Its candidate reply is withdrawn, as when the model goes on after one, so 检查并继续
    may run the task again. The record stays running and the card as it is: once this process exits the
    Bridge marks the job interrupted and shows its recovery card, as for a runner that died
    (app/detached.py ``finish_detached``, or ``recover_stale_jobs`` when the next Bridge starts)."""
    if job.verified_reply is not None and reruns_after_withdrawal(harness.spec.name, job.native_question_deferred):
        try:
            observe_verified_reply(ctx, job, harness.spec.name, None)
        except (OSError, ValueError) as exc:
            runtime.log(f"candidate reply of interrupted job #{job.job_id} kept: {type(exc).__name__}")
    record_job_state(ctx, job, "running", SIGTERM_STATUS)
    runtime.log(f"reply job #{job.job_id} interrupted by SIGTERM; the Bridge marks it interrupted")


def process_bridge_reply_job(job: Any, ctx: Any, *, harness, runtime, messenger, cards, sessions, runs, artifacts,
                             questions, steering, record_job_state) -> None:
    """Run one queued job of ``harness`` and deliver its result.

    - ``runtime``: the log; ``messenger``: text replies in the Harness's voice.
    - ``sessions``: the topic's native session pointer before and after the turn.
    - ``runs``: the turn on the backend.
    - ``cards``: the job's task card; ``artifacts``: a result too long for it, sent as a file.
    - ``questions``: a checkpoint question delivered with the result.
    - ``steering``: the job's 补充 forms and action row, closed at its end.
    - ``record_job_state``: the Harness's job-state recorder.
    """

    def send_text(ctx, chat_id, text, key, card=None, on_card=None):
        return messenger.reply(ctx, chat_id, text, key, voice=harness.voice, card=card,
                               **({"on_card": on_card} if on_card is not None else {}))

    ctx = context_for_job(ctx, job)
    if job.kind != "chat":
        record_job_state(ctx, job, "cancelled", "旧的专用任务类型已移除，不再执行")
        return
    with job.execution_lock:
        if job.cancelled_before_start:
            return
        job.execution_started = True
    capabilities = backend_capabilities(getattr(harness, "backend", None), ctx)
    revision, session_id = _start_turn(ctx, job, sessions=sessions, runtime=runtime, record_job_state=record_job_state)
    timing.mark("turn")
    turn, snapshot = _run_turn(job, ctx, harness=harness, runtime=runtime, cards=cards, sessions=sessions, runs=runs,
                               record_job_state=record_job_state, send_text=send_text, capabilities=capabilities,
                               revision=revision, session_id=session_id)
    if turn.status != "waiting_user":  # A track of the main thread forgets its session (app/main_tracks.py).
        main_tracks.release_pointer(ctx, job, harness, runtime)
    if turn.status == "interrupted":
        leave_interrupted(ctx, job, harness=harness, runtime=runtime, record_job_state=record_job_state)
        return
    _deliver_turn(job, ctx, turn, snapshot, harness=harness, runtime=runtime, messenger=messenger, cards=cards,
                  artifacts=artifacts, questions=questions, steering=steering, record_job_state=record_job_state,
                  send_text=send_text, capabilities=capabilities)
    runtime.log(f"finished reply job #{job.job_id}")


TRACE_CHARS = 4000  # Of a failed turn's traceback in the log: its last frames, where it was raised.


def failure_trace(exc, job) -> str:
    """One log entry of the exception ``exc`` that failed ``job``'s turn: its type, words and traceback, the end
    kept when long. The words of an exception never hold the turn's content here (a backend's errors name files
    and states), and the log stays on this machine."""
    text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).rstrip()
    if len(text) > TRACE_CHARS:
        text = "…" + text[-TRACE_CHARS:]
    return f"job #{job.job_id} failed with {type(exc).__name__}: {text}"


def _turn_from_exception(exc, job, settling) -> dict:
    """The TurnResult fields of a turn the exception ``exc`` ended (its native session is never resumed then):
    a timeout fails it; a SIGTERM before the backend finished (``settling`` False) leaves it interrupted, whatever
    the backend raised as it stopped; a stop cancels it; anything else fails it, its type named."""
    if isinstance(exc, subprocess.TimeoutExpired):
        return {"status": "failed", "status_message": "任务超时",
                "final_text": worded(f"任务 #{job.job_id} 超时未完成，可在本话题{retry_hint()} 重试。", job)}
    if job.interrupted and not settling:
        return {"status": "interrupted"}
    if isinstance(exc, JobCancelled):
        # Like a finished card since P1: no "直接回复本话题即可继续" (repo review U10).
        return {"status": "cancelled", "status_message": "用户停止了任务",
                "final_text": stopped_sentence(job.job_id) + worded(STOPPED_NOTE, job)}
    name = type(exc).__name__
    return {"status": "failed", "status_message": "任务异常（" + name + "）",
            "final_text": f"任务 #{job.job_id} 处理出错（{name}），未满足结果发布条件。"}


def _run_turn(job, ctx, *, harness, runtime, cards, sessions, runs, record_job_state, send_text, capabilities,
              revision, session_id):
    """The turn on the backend, settled (``settle_turn``) or ended by an exception (``_turn_from_exception``), its
    native session protected unless settling did it and its activity monitor stopped. (turn, the workspace's diff
    snapshot from before it, None when it did not get that far)."""
    prefix = harness.spec.idempotency_prefix
    progress = TurnProgress(job, ctx, record_job_state=record_job_state)
    turn = TurnResult()
    snapshot = None
    activity = None
    settling = False  # The backend finished: a SIGTERM no longer interrupts the turn (``finished_before``).
    try:
        if job.cancel_event.is_set():
            raise JobCancelled("任务已在执行前停止")
        progress(PROGRESS_START, True)
        timing.mark("progress")
        activity = _task_monitor(ctx, job, cards=cards, runtime=runtime, record_job_state=record_job_state,
                                 send_text=send_text, prefix=prefix,
                                 starting=harness.spec.prompt_agent_name if capabilities.model_requests else "")
        job.activity_monitor = activity
        activity.start()
        timing.mark("monitor")
        # After the monitor starts: 执行中 does not wait for git (0.2-0.35 s, S34); the backend does.
        snapshot = capture_job_diff_snapshot(ctx.workdir)
        timing.mark("snapshot")
        result = _generate_reply(ctx, job, session_id, progress, runs=runs, record_job_state=record_job_state)
        if job.interrupted and not finished_before(result):
            raise JobCancelled("任务进程收到 SIGTERM")
        settling = True
        settle_turn(turn, result, ctx, job, sessions=sessions, session_id=session_id, revision=revision,
                    runtime=runtime, prefix=prefix)
    except Exception as exc:  # noqa: BLE001 - isolate backend and delivery failures per job.
        turn.allow_resume = False
        for name, value in _turn_from_exception(exc, job, settling).items():
            setattr(turn, name, value)
        if not isinstance(exc, (subprocess.TimeoutExpired, JobCancelled)):
            # A backend stopped under a SIGTERM may fail in its own way.
            runtime.log(f"interrupted job #{job.job_id} ended in {type(exc).__name__}" if turn.status == "interrupted"
                        else turn.status_message)
            # Where it came from: T480/T481 (2026-11-11) failed at their start with only "任务异常（RuntimeError）".
            runtime.log(failure_trace(exc, job))
    finally:
        if not turn.session_finalized:
            try:
                sessions.finalize_session_from_worker(ctx, job, session_id, job.session_id, revision, allow_resume=False)
            except Exception as exc:
                runtime.log("session protection failed: " + type(exc).__name__)
        if activity is not None:
            activity.stop()
    return turn, snapshot


def _deliver_turn(job, ctx, turn, snapshot, *, harness, runtime, messenger, cards, artifacts, questions, steering,
                  record_job_state, send_text, capabilities):
    """The ended turn's outcome atop its card, its automatic retry planned, its result delivered (TurnDelivery)
    and, once it is out, the supplements the backend never read marked so."""
    # The outcome replaces 执行中 at the top at once; preparing the result (a diff takes seconds) left
    # the top blank until the answer appeared lower down. The execution ended here: the status line's 用时 is the
    # answer card's and the relay's (core/metrics.py run_seconds; TurnDelivery.deliver's mark keeps this one).
    mark(job, 'execution_finished')
    show_outcome(cards, ctx, job, turn.status, runtime)

    retry = auto_retry.plan(harness, ctx, job, turn)
    promised = None
    try:
        # A refusal of cyber or bio: one retry on an older model, said in the stopped turn's text (app/policy_retry.py).
        promised = policy_retry.at_stop(ctx, job, turn)
    except (OSError, ValueError) as exc:
        runtime.log(f"policy retry of job #{job.job_id} not scheduled: {type(exc).__name__}")
    delivery = TurnDelivery(job, ctx, turn, prefix=harness.spec.idempotency_prefix, cards=cards, runtime=runtime,
                            artifacts=artifacts, questions=questions, steering=steering,
                            record_job_state=record_job_state, send_text=send_text, retry=retry,
                            relay=functools.partial(main_relay.after_turn, messenger=messenger, profile=ctx.profile,
                                                    voice=harness.voice),
                            react=functools.partial(main_reactions.finish, messenger, ctx.profile, log=runtime.log),
                            retry_promised=promised is not None)
    try:
        delivery.deliver(snapshot)
        main_reply.react_end(delivery)  # A quiet main-thread turn: OnIt on the owner's message becomes its end (D1).
    finally:
        job.supplements_listed = delivery.listed  # Those still held at its end too (settle_held_at_end).
        if capabilities.steer_reads:  # After the result: the scan never holds it back.
            try:
                # The turn is over: Claude reads nothing more. Supplements its card listed get no line of their own.
                steering.end_unread_steers(ctx, job, listed=delivery.listed)
            except Exception as exc:  # noqa: BLE001 - only the receipts stay as they were
                runtime.log(f"unread supplements not marked: {type(exc).__name__}")
