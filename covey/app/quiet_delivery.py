"""The quiet butler turn's side of delivering a result (core/placement.py Row.quiet): a turn whose task card has
not gone out (``bare``) says its failure, refusal or stop in a few words at the chat's top level with its "你问"
block (``top_notice``), and an answer that could not be sent there too (``undelivered_notice``). ``QuietDelivery``
is a part of app/worker.py TurnDelivery, which it reads its job, context, turn and senders from."""
from __future__ import annotations

import dataclasses

from covey.app import auto_retry, butler_reply
from covey.app.outbox import AUDIT_REFUSED_NOTE, REDELIVER_LABEL, retry_label
from covey.cards.recovery import result_actions
from covey.cards.review import answer_card, answer_lead
from covey.core import placement
from covey.core.jobs import find_job_record
from covey.core.butler import worded
from covey.core.saved_results import saved_delivery_kind
from covey.core.text import safe_text
from covey.feishu.message_request import optimize_markdown


UNFINISHED = "这一轮没做完。"  # A quiet butler turn's failure notice begins so (TurnDelivery.notice_words).


class QuietDelivery:
    """TurnDelivery's methods for a quiet butler turn (``bare``) and its top-level notices."""

    def bare(self) -> bool:
        """A quiet butler turn (core/placement.py Row.quiet) whose card has not gone out: nothing of it may reply
        to the owner's message, which would open its thread."""
        return not self.job.progress and placement.quiet(self.ctx.state_dir, self.job)

    def top_ctx(self):
        """This turn's context sending a new message at the chat's top level, quoting nothing."""
        return dataclasses.replace(self.ctx, reply_to_message_id="")

    def refused(self) -> bool:
        """Whether the turn was stopped by an official refusal (its outcome's ``policy_event``): its status is
        cancelled, as a stop's is, but the owner did not stop it."""
        return bool(getattr(getattr(self.turn, "outcome", None), "policy_event", None))

    def notice_words(self) -> str:
        """The words of a quiet turn's top-level message on its failure or refusal (``top_notice``): that it failed
        and where its saved reply went when one was sent as a message of its own (``saved_words``), the automatic
        retry's line when one follows, else the first line of the turn's text. No task number: the owner saw no card
        with it (B4 review)."""
        saved = "" if self.refused() else self.saved_words()  # A refusal says its own first line.
        if saved:
            return f"{UNFINISHED}{saved}"
        if self.retry:
            return f"{UNFINISHED}{auto_retry.status_line(*self.retry)}。"
        first = (self.turn.final_text or "").strip().splitlines()
        return safe_text(first[0], 300) if first else UNFINISHED

    def saved_words(self) -> str:
        """Where this turn's saved, authorized reply went once it was sent apart from its card (``_answer_apart``),
        in a failure notice's words; "" when there is none or the card shows it. Said instead of the reply's first
        line, which the answer card above has already (B4 review): placement's ``answer`` cell, the top level for a
        quiet turn."""
        turn = self.turn
        if getattr(turn, "verified_delivery", None) is None or self.in_place:
            return ""
        place = ("聊天区" if placement.plan(self.ctx.state_dir, self.job).row.answer == placement.TOP
                 else "你提问的话题串里")
        if saved_delivery_kind(turn.verified_delivery, turn.verified_outcome) == "file":
            return f"回答太长，作为文件发在{place}。"
        return f"回答已经单独发在{place}，这里不重复。"

    def _quiet_fallback(self, sent, final_card, display_text, delivery_text):
        """``_fallback`` of a ``bare`` turn: a failure's or a refusal's few words (``notice_words``; a refusal's with
        the supplements it took, butler_reply.dropped_lines) at the top level with its "你问" block (``top_notice``); a
        stop says only the supplements it took, there the same way, and with none nothing (its reaction goes,
        app/butler_reactions.py). Supplements listed there get no line of their own (``listed``): a reply under each
        would open its thread (D1)."""
        job, turn = self.job, self.turn
        refused = self.refused()
        stopped = turn.status == "cancelled" and not refused
        if stopped or refused:
            try:
                dropped = butler_reply.dropped_lines(self.ctx.state_dir, job, refused)
            except Exception as exc:  # noqa: BLE001 - the notice without the list
                self.runtime.log(f"main thread supplements not listed job=#{job.job_id}: {type(exc).__name__}")
                dropped = ""
        else:
            dropped = ""
        if stopped and not dropped:
            self.runtime.log(f"main thread turn stopped with no card job=#{job.job_id}: nothing sent")
            sent.delivered = True
            return
        if stopped:
            words = dropped  # Its first line says the turn stopped.
        else:
            words = worded(self.notice_words(), job)
            words = words + "\n\n" + dropped if dropped else words
        sent.delivered = self.top_notice(words, f"{self.prefix}-result-{job.message_id}")
        self.listed = bool(dropped) and bool(sent.delivered)
        if stopped:
            sent.delivered = True  # A stop has no result to deliver: the list is all it says.

    def undelivered_notice(self, execution_status):
        """A ``bare`` turn whose answer could not be sent (its delivery failed, ``deliver``): no card says it nor
        offers 重新投递结果 (finish_card's labels need one), so a few words at the top level with its "你问" block and,
        when its record offers one (app/outbox.py retry_label), the button. Never raises."""
        job, ctx = self.job, self.ctx
        try:
            refused = self.turn.status_message == AUDIT_REFUSED_NOTE
            record = find_job_record(ctx.state_dir, job.message_id) or {}
            label = retry_label(record) if record else ""
            text = ("飞书内容审核拒收了这一轮的回答，打码后仍未通过；原文保存在本地任务记录。" if refused
                    else "这一轮的回答没有发出去，原文已保留。")
            if label == REDELIVER_LABEL:
                text += "点下面的按钮重新投递，不会重新执行。"
            elif label:
                text += "点下面的按钮检查并继续。"
            actions = [result_actions(job.topic_id, job.job_id, label)] if label else []
            ask = self.ask_block()
            built = answer_card(optimize_markdown(text, step_headings=False), f"⚠️ 没发出去：{answer_lead(text)}",
                                actions, [ask] if ask else [])
            if not self.send_text(self.top_ctx(), job.chat_id, text, f"{self.prefix}-undelivered-{job.message_id}",
                                  card=built):
                self.runtime.log(f"main thread undelivered notice not sent job=#{job.job_id}")
        except Exception as exc:  # noqa: BLE001 - the record keeps the failure and its retry
            self.runtime.log(f"main thread undelivered notice failed job=#{job.job_id}: {type(exc).__name__}")

    def notice_preview(self, text) -> str:
        """The chat list's preview of a ``top_notice``: how the turn ended (as its reaction says it,
        app/butler_reactions.py: a refusal is a failure, not a stop) and the notice's lead, no task number."""
        status = self.turn.status
        head = ("❌ 被拒" if self.refused() else "⏹ 已停止" if status == "cancelled"
                else "⚠️ 没做完" if status in {"failed", "timed_out"} else "")
        rest = text.strip().removeprefix(UNFINISHED).strip()
        lead = answer_lead(rest) if rest else ""
        if not head:
            return lead or answer_lead(text)
        return f"{head}：{lead}" if lead else head

    def top_notice(self, text, key) -> bool:
        """``text``, a few words the Bridge wrote (a failure, a refusal, a timeout), as a card at the chat's top level
        with this turn's "你问" block above it (core/placement.py Row.quiet); whether it went."""
        job = self.job
        ask = self.ask_block()
        built = answer_card(optimize_markdown(text, step_headings=False), self.notice_preview(text), (),
                            [ask] if ask else [])
        return bool(self.send_text(self.top_ctx(), job.chat_id, text, key, card=built))
