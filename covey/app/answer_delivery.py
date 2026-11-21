"""TurnDelivery's (app/turn_delivery.py) answer card sent on its own: ``AnswerParts`` sends the verified reply as its card
where the placement table puts it, with its 产物 and 输入/输出 (app/answer_cards.py makes their lines and keeps the
cards for the upload) and the line or "你问" block atop it, through app/saved_send.py, as each redelivery sends it. A reply
Feishu's audit refuses even masked goes as a file of the same saved text, with one line why (``refused_as_file``)."""
from __future__ import annotations

import dataclasses

from covey.app import answer_cards, revisit_hint, saved_send
from covey.cards.review import answer_lead
from covey.core import placement
from covey.core.failure import HEADLINE_CHARS
from covey.app.switch_handoff import topic_controls
from covey.core.jobs import find_job_record, job_key, quick_held
from covey.core.saved_results import saved_delivery_key, saved_delivery_kind
from covey.feishu.transport import last_failure, set_failure

# The line after a saved reply Feishu's audit refused even masked (feishu/delivery.py), sent as a file instead (#24).
AUDIT_FILE_NOTE = "飞书内容审核拦下了这条回答（打码后也没通过），原文作为文件发在上面。"


class AnswerParts:
    """TurnDelivery's (app/turn_delivery.py) answer card sent on its own: the verified reply as its card where the placement
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
        if quick_held(job):
            tail = [*tail, *self.topic_controls()]  # A quick answer has no task card to carry them.
        key = saved_delivery_key(self.prefix, job_key(job.message_id, job.job_id), turn.verified_delivery)
        # The butler's card is in the owner's message's thread (core/placement.py Row.answer TOP): its
        # answer goes to the top level as a message of its own, a "你问" link to that message above it.
        row = placement.plan(ctx.state_dir, job).row
        top = row.answer == placement.TOP
        # In a 话题区's thread it leads with the turn's status, T number and title instead.
        headed = not top and self.headed(row)
        head = saved_send.heads(top, headed, self.ask_block,
                                lambda: self.answer_head(answer_lead(turn.verified_delivery.text, HEADLINE_CHARS)))
        outgoing = saved_send.plan(
            ctx, job.job_id, turn.status, turn.verified_delivery, delivery_text, key=key,
            file=saved_delivery_kind(turn.verified_delivery, turn.verified_outcome) == "file", top=top, tail=tail,
            head=head, bare=self.bare(), files=files, upload_status=status,
            said=saved_send.band_lead(top, headed, head, turn.verified_delivery))
        saved_send.keep(ctx.state_dir, job_key(job.message_id, job.job_id), outgoing, self.runtime.log)
        sent.delivered, self.answer_message = saved_send.send_saved(
            ctx, job, delivery_text, outgoing, send=self.send_text, artifacts=self.artifacts, log=self.runtime.log)
        refused = last_failure()
        if not sent.delivered and not outgoing.file and refused is not None and refused.category == "audit_rejected":
            sent.delivered = self.refused_as_file(outgoing, delivery_text, tail, files, status)

    def refused_as_file(self, outgoing, delivery_text, tail, files, status) -> bool:
        """Feishu's audit refused the saved reply even masked (feishu/delivery.py): the same saved text as a file where
        the answer would have gone, its 产物, 改动 and 输入/输出 on the small card after it as a long answer's go (``tail``,
        app/saved_send.py; under a key of its own, without the head, whose band quotes the reply), then one line why
        (AUDIT_FILE_NOTE), #24. Only the saved text is sent, the model is not run again (rule 5). Whether the file went;
        when it did not, the audit's refusal stays the turn's failure."""
        job, ctx, turn = self.job, self.ctx, self.turn
        refused = last_failure()
        fallback = saved_send.plan(ctx, job.job_id, turn.status, turn.verified_delivery, delivery_text,
                                   key=f"{outgoing.key}-audit-file", file=True, top=outgoing.top, tail=tail, head=(),
                                   bare=self.bare(), files=files, upload_status=status)
        delivered, _ = saved_send.send_saved(ctx, job, delivery_text, fallback, send=self.send_text,
                                             artifacts=self.artifacts, log=self.runtime.log)
        if not delivered:
            set_failure(refused)
            return False
        where = dataclasses.replace(ctx, reply_to_message_id="") if outgoing.top else ctx
        self.audit_file = True
        self.runtime.log(f"answer of job #{job.job_id} refused by Feishu's audit even masked; sent as a file")
        try:
            self.send_text(where, job.chat_id, AUDIT_FILE_NOTE, f"{self.prefix}-audit-file-{job.message_id}")
        except Exception as exc:  # noqa: BLE001 - the file is out; the line is an extra
            self.runtime.log(f"audit file note failed: {type(exc).__name__}")
        set_failure(None)
        return True

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
            snapshot = self.process_snapshot()
            turn = self.turn
            hint = revisit_hint.elements(
                ctx.state_dir, topic_id=ctx.topic_id, chat_id=job.chat_id, job_id=job.job_id,
                text=getattr(turn.verified_delivery, "text", ""), done=turn.status == "done"
                and getattr(turn.verified_outcome, "status", "") == "completed",
                checkpoint_path=str(job.checkpoint_path or ""), snapshot=snapshot)  # Display only: the saved reply stays.
            return saved_send.tail_parts(ctx.state_dir, record, ctx.topic_id, self.artifacts.voice, job,
                                         self.cards.harness, snapshot, ctx, hint)
        except Exception as exc:  # noqa: BLE001 - the answer goes all the same
            self.runtime.log(f"answer card tail unavailable: {type(exc).__name__}: {exc}")
            return [], False, None

    def topic_controls(self):
        """换配置 and 回退 at the bottom of a quick answer's card (追问只发答案卡, docs/cards.md): its task card never went
        out (core/jobs.py quick_held), and they are the turn's (回退 takes only the topic's latest turn). [] when the
        topic has none; never raises: the answer goes without them."""
        try:
            panel = topic_controls(self.cards.harness, self.ctx, self.job.job_id)
        except Exception as exc:  # noqa: BLE001 - the answer goes all the same
            self.runtime.log(f"answer card controls unavailable: {type(exc).__name__}")
            return []
        return [panel] if panel else []

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

    def ask_block(self, doing="answer"):
        """The "你问" block of this turn (cards/answer_card.py ask_block), the speaker saying ``doing`` (``says``); None
        when it cannot be told."""
        try:
            # The butler's own turn: the owner's message in the chat (answer_cards.ask_url).
            return answer_cards.asked(self.ctx.state_dir, self.job, profile=self.ctx.profile, log=self.runtime.log,
                                      doing=doing)
        except Exception as exc:  # noqa: BLE001 - the answer goes without it
            self.runtime.log(f"answer card 你问 unavailable: {type(exc).__name__}")
            return None
