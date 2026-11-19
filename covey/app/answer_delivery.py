"""TurnDelivery's (app/worker.py) answer card sent on its own: ``AnswerParts`` sends the verified reply as its card
where the placement table puts it, with its 产物 and 输入/输出 (app/answer_cards.py makes their lines and keeps the
cards for the upload) and the line or "你问" block atop it, through app/saved_send.py, as each redelivery sends it."""
from __future__ import annotations

from covey.app import answer_cards, saved_send
from covey.cards.review import answer_lead
from covey.core import placement
from covey.core.failure import HEADLINE_CHARS
from covey.core.jobs import find_job_record, job_key
from covey.core.saved_results import saved_delivery_key, saved_delivery_kind


class AnswerParts:
    """TurnDelivery's (app/worker.py) answer card sent on its own: the verified reply as its card where the placement
    table puts it (``_answer_apart``), the 产物 and 输入/输出 under it (``answer_tail``; on a small card after a file)
    and the line or "你问" block atop it (``answer_head``, ``ask_block``). It reads the job, context, turn and senders
    of the TurnDelivery it is part of."""

    def _answer_apart(self, sent, delivery_text):
        """The verified reply as its answer card (a file when it is one), where the placement table puts it
        (core/placement.py ``answer``), then its 产物 and 输入/输出 when the card could not carry them. What goes out
        is kept first (app/saved_send.py keep): a redelivery under the same key sends the same card."""
        job, ctx, turn = self.job, self.ctx, self.turn
        # Use the same immutable route and key on first send and every retry.
        # Do not switch transports after an ambiguous send result.
        tail, files, status = self.answer_tail()
        key = saved_delivery_key(self.prefix, job_key(job.message_id, job.job_id), turn.verified_delivery)
        # The butler's card is in the owner's message's thread (core/placement.py Row.answer TOP): its
        # answer goes to the top level as a message of its own, a "你问" link to that message above it.
        row = placement.plan(ctx.state_dir, job).row
        top = row.answer == placement.TOP
        # In a 话题区's thread it leads with the turn's status, T number and title instead (D3, 2026-11-08).
        head = saved_send.heads(top, not top and self.headed(row), self.ask_block,
                                lambda: self.answer_head(answer_lead(turn.verified_delivery.text, HEADLINE_CHARS)))
        outgoing = saved_send.plan(
            ctx, job.job_id, turn.status, turn.verified_delivery, delivery_text, key=key,
            file=saved_delivery_kind(turn.verified_delivery, turn.verified_outcome) == "file", top=top, tail=tail,
            head=head, bare=self.bare(), files=files, upload_status=status)
        saved_send.keep(ctx.state_dir, job_key(job.message_id, job.job_id), outgoing, self.runtime.log)
        sent.delivered, self.answer_message = saved_send.send_saved(
            ctx, job, delivery_text, outgoing, send=self.send_text, artifacts=self.artifacts, log=self.runtime.log)

    def process_snapshot(self):
        """The turn's process snapshot (core/trace.py; ``deliver``'s snapshot is the workdir's, for the diff):
        the answer card's 输出 line, its relay's too (app/butler_relay.py). None without a trace."""
        trace = getattr(self.job, "tool_trace", None)
        try:
            return trace.snapshot() if trace is not None else None
        except Exception:  # noqa: BLE001 - the line goes without its figures
            return None

    def answer_tail(self):
        """(elements, whether they show 产物, the upload's status they show) under an answer sent on its own
        (app/saved_send.py tail_parts): its 产物 as the upload stands now and the 输入/输出 box. Never raises: without
        them the answer goes as before."""
        job, ctx = self.job, self.ctx
        try:
            record = find_job_record(ctx.state_dir, job.message_id) or {}
            return saved_send.tail_parts(ctx.state_dir, record, ctx.topic_id, self.artifacts.voice, job,
                                         self.cards.harness, self.process_snapshot(), ctx)
        except Exception as exc:  # noqa: BLE001 - the answer goes all the same
            self.runtime.log(f"answer card tail unavailable: {type(exc).__name__}: {exc}")
            return [], False, None

    def headed(self, row) -> bool:
        """Whether this turn's answer card in its thread leads with its status line and its task card keeps no 结论
        (``row.answer_head``, D3): only in the owner's private chats and the personal groups (app/saved_send.py
        owner_headed). Another group's topic is in the same row of the table, its cards as before; so is any turn when
        the private chat cannot be told."""
        return saved_send.owner_headed(row, self.runtime, self.job.chat_id)

    def answer_head(self, lead=""):
        """What leads this turn's answer card in its thread, its conclusion band over the reply's ``lead`` (app/saved_send.py
        status_head); None when it cannot be told."""
        try:
            return saved_send.status_head(self.ctx.state_dir, self.ctx.topic_id, self.turn.status, lead)
        except Exception as exc:  # noqa: BLE001 - the answer goes without it
            self.runtime.log(f"answer card status line unavailable: {type(exc).__name__}")
            return None

    def ask_block(self):
        """The "你问" block of this turn (cards/answer_card.py ask_block); None when it cannot be told."""
        try:
            # The butler's own turn: the owner's message in the chat (answer_cards.ask_url).
            return answer_cards.asked(self.ctx.state_dir, self.job, profile=self.ctx.profile, log=self.runtime.log)
        except Exception as exc:  # noqa: BLE001 - the answer goes without it
            self.runtime.log(f"answer card 你问 unavailable: {type(exc).__name__}")
            return None
