"""A message sent while several questions wait for answers is kept, not refused.

It is saved in ``held-notes/<token>.json`` and shown on a small card. By default
it goes with the next submitted answer, in the same delivery: the follow-up
task's prompt, or the last answer of a live native question. 留作下一轮 queues it as its own
task after the answer; 撤回 drops it. Nothing is sent before the user answers.

A note goes out once or not at all. Every change of its status is a compare-and-set under
the note's lock. An answer takes the notes still held at the moment it is sent (``claim_held``),
not those held at its click: since 提交回答 is acknowledged before its continuation is queued,
a 撤回 or 留作下一轮 clicked in between must win or lose as a whole.

The records, their cards and HeldNotes, which holds, attaches and queues them, are here;
Questions (app/questions.py) owns a HeldNotes and settles its notes when an answer goes through.
"""
from __future__ import annotations

import functools
import hashlib
import json
import time

from covey.app.card_lanes import after_ack
from covey.cards.held_note import ANSWER_REQUIRED, feedback_chooser, note_card, note_parts, waiting_text
from covey.core.locks import resource_lock
from covey.core.state import keyed_json_path, write_private_text
from covey.core.history_storage import append_history
from covey.core.store import directory_store

IN_FLIGHT = "回答正在发送，稍后再操作这条补充"


def waiting_for(pending):
    """What a held note waits for, saved with it: per task, when it first asked and how many questions
    wait (two forms of one task are one entry)."""
    groups = {}
    for q in pending:
        group = groups.setdefault(q.get("job_id"), {"job_id": q.get("job_id"), "created_at": q.get("created_at"), "count": 0})
        group["count"] += len(q.get("questions") or [])
        if isinstance(q.get("created_at"), (int, float)) and q["created_at"] < (group["created_at"] or float("inf")):
            group["created_at"] = q["created_at"]
    return list(groups.values())





class HeldNotes:
    """The messages a topic holds while its questions wait for answers.

    - ``harness``: the voice of the note cards.
    - ``messenger``: note cards, and the reply when one cannot be sent.
    - ``cards``: repaints a note card when its status changes.
    - ``questions``: the Questions that owns these notes: which questions wait, the feedback a
      note can become, and the job that carries a queued note on.
    """

    def __init__(self, harness, *, messenger, cards, questions) -> None:
        self.messenger = messenger
        self.voice = harness.voice
        self.cards = cards
        self.questions = questions

    @staticmethod
    def note_path(ctx, token):
        return keyed_json_path(ctx.state_dir, "held-notes", token)

    def held_notes(self, ctx, chat, *statuses):
        rows = directory_store(ctx.state_dir / "held-notes").query(topic_id=ctx.topic_id, chat_id=chat)
        return sorted((r for _, r in rows if r.get("status") in statuses), key=lambda r: r.get("created_at", 0))

    def hold_note(self, ctx, chat, text, mid, pending, attachments=""):
        token = hashlib.sha256(f"{ctx.topic_id}-{mid}".encode()).hexdigest()[:32]
        path = self.note_path(ctx, token)
        with resource_lock("question", path):
            if path.exists():
                return  # A redelivered event.
            record = {"schema_version": 1, "token": token, "topic_id": ctx.topic_id, "chat_id": chat, "text": text,
                      "message_id": mid, "status": "held", "created_at": time.time(),
                      "question_tokens": [q["token"] for q in pending], "waiting": waiting_for(pending),
                      **({"attachments": attachments} if attachments else {})}
            write_private_text(path, json.dumps(record, ensure_ascii=False))

        def remember(card_message):
            with resource_lock("question", path):
                current = json.loads(path.read_text())
                current["card_message_id"] = card_message
                write_private_text(path, json.dumps(current, ensure_ascii=False))
        if not self.messenger.publish_card(ctx, chat, mid, note_card(record), on_sent=remember, voice=self.voice):
            self.messenger.reply(ctx, chat, f"这条消息还没发给模型：{waiting_text(record)}还没答，提交回答时会一起带上。"
                                 + ANSWER_REQUIRED,
                                 self.voice.key("note-held", mid), voice=self.voice)

    def change_note(self, ctx, token, expected=None, **changes):
        """Apply ``changes`` under the note's lock; with ``expected``, only while its status is one of
        them. The record after the change, or None when another action changed the note first."""
        path = self.note_path(ctx, token)
        with resource_lock("question", path):
            record = json.loads(path.read_text())
            if expected is not None and record.get("status") not in expected:
                return None
            record.update(changes)
            write_private_text(path, json.dumps(record, ensure_ascii=False))
        return record

    def repaint_note(self, ctx, record):
        if record.get("card_message_id"):
            self.cards.repaint_browse_card(ctx, record["chat_id"], f"note-{record['token']}-{time.time_ns()}",
                                     record["card_message_id"], note_card(record))

    def update_note(self, ctx, token, expected=None, **changes):
        """change_note, then the note's card shows the result; with nothing to change it just repaints."""
        record = self.change_note(ctx, token, expected, **changes)
        if record is not None:
            self.repaint_note(ctx, record)
        return record

    def note_action(self, ctx, chat, raw, mid):
        token = str(raw.get("token") or "")
        if len(token) != 32 or any(c not in "0123456789abcdef" for c in token):
            raise ValueError("补充标识无效")
        path = self.note_path(ctx, token)
        record = json.loads(path.read_text())
        if record.get("topic_id") != ctx.topic_id or record.get("chat_id") != chat:
            raise ValueError("补充不属于当前话题")
        if record.get("status") not in {"held", "next_round"}:
            self.update_note(ctx, token)  # Already settled: just show it.
            return
        verb = raw.get("verb")
        if verb == "withdraw":
            changed = self.update_note(ctx, token, {"held", "next_round"}, status="withdrawn")
        elif verb == "attach":
            changed = self.update_note(ctx, token, {"next_round"}, status="held")
        elif verb == "next":
            changed = self.change_note(ctx, token, {"held"}, status="next_round")
            if changed is not None and not (self.questions.pending_questions(ctx, chat)
                                            or self.questions.answer_in_flight(ctx, chat)):
                # Nothing left to answer: it runs now, in order; its task card follows the acknowledgement.
                after_ack(functools.partial(self.queue_notes, ctx, chat, [changed]))
            elif changed is not None:
                # An answer being sent queues it once it is through (settle_notes), behind that answer.
                self.repaint_note(ctx, changed)
        elif verb == "feedback":
            if self.questions.answer_in_flight(ctx, chat):
                # The answer being sent may take this note with it; it cannot also become feedback.
                raise ValueError(IN_FLIGHT)
            pending = [q for q in self.questions.pending_questions(ctx, chat) if q["token"] in record["question_tokens"]]
            if raw.get("question_token"):
                pending = [q for q in pending if q["token"] == raw["question_token"]]
            if len(pending) != 1:
                if not pending:
                    raise ValueError("原问题已处理，请在本话题继续说明新要求")
                # Several forms can be from independent native requests. Let the
                # user choose the request, never release all of them implicitly.
                self.messenger.publish_card(ctx, chat, mid, feedback_chooser(ctx.topic_id, token, pending),
                                            voice=self.voice)
                return
            changed = record
            if record["status"] == "next_round":
                changed = self.update_note(ctx, token, {"next_round"}, status="held")
            if changed is not None:
                said, paths = note_parts(record)
                self.questions.submit_question_feedback(ctx, chat, pending[0]["token"], said, mid, attachments=paths)
        else:
            raise ValueError("未知操作")
        if changed is None:
            self.update_note(ctx, token)  # Another action took it first: show where it went.

    def claim_held(self, ctx, chat, token, response_kind, *, again=False):
        """The notes going with question ``token``'s answer, taken as it is sent: each one still held
        becomes attached (its job follows in settle_notes). A note withdrawn or kept for the next round
        before this stays out; a click on a taken note shows it taken. ``again``: the answer is being
        resubmitted (核查已保存回答并继续), so the notes an earlier attempt took without a job go with it
        too: that attempt queued nothing, or no one knows."""
        claimed = [note for note in self.held_notes(ctx, chat, "attached")
                   if again and note.get("answer_token") == token and not note.get("job_id")]
        for note in self.held_notes(ctx, chat, "held"):
            record = self.change_note(ctx, note["token"], {"held"}, status="attached", answer_token=token,
                                      response_kind=response_kind)
            if record is not None:
                claimed.append(record)
        return claimed

    def wait_instead(self, ctx, chat, folded):
        """Questions this topic's notes waited for were folded under newer ones (``folded``: their tokens
        and tasks; questions.fold_older_questions). Each such note waits for the questions pending now
        instead: its first line names them, a line says which were folded, and its buttons act on them.
        With none pending a note is left as it was (the next answer still takes it)."""
        pending = self.questions.pending_questions(ctx, chat)
        if not pending:
            return
        for note in self.held_notes(ctx, chat, "held", "next_round"):
            tokens = set(note.get("question_tokens") or []) & set(folded)
            if not tokens:
                continue
            jobs = {folded[token] for token in tokens}
            record = self.change_note(ctx, note["token"], {"held", "next_round"},
                                      question_tokens=[q["token"] for q in pending], waiting=waiting_for(pending),
                                      folded=[g for g in note.get("waiting") or []
                                              if isinstance(g, dict) and g.get("job_id") in jobs])
            if record is not None:
                self.repaint_note(ctx, record)

    def answer_unconfirmed(self, ctx, chat, token):
        """The answer that took these notes may not have gone: their cards stop offering 撤回."""
        for note in self.held_notes(ctx, chat, "attached"):
            if note.get("answer_token") == token and not note.get("job_id"):
                self.repaint_note(ctx, note)

    def queue_notes(self, ctx, chat, notes):
        for note in notes:
            # Taken first: a note queued by a click and by an answer's settle at once runs once.
            note = self.change_note(ctx, note["token"], {"next_round"}, status="queued")
            if note is None:
                continue
            said, paths = note_parts(note)
            text = said + ("\n" + paths if paths else "")
            append_history(ctx.history_path, "user", text, {"message_id": note["message_id"]})
            try:
                job = self.questions.enqueue_continuation(ctx, text, chat, "note-" + note["token"], None, display=said)
            except Exception:
                self.repaint_note(ctx, note)  # It may have been queued: never sent again by itself.
                raise
            if job is None:
                self.update_note(ctx, note["token"], {"queued"}, status="next_round")
                continue
            self.update_note(ctx, note["token"], {"queued"}, job_id=job.job_id)

    def settle_notes(self, ctx, chat, attached, job_id, *, response_kind="answer"):
        """After an answer: the notes it took (claim_held) show its job; notes left for the next
        round are queued behind it once no question waits or is being answered."""
        for note in attached:
            self.update_note(ctx, note["token"], {"attached"}, job_id=job_id, response_kind=response_kind)
        if not self.questions.pending_questions(ctx, chat) and not self.questions.answer_in_flight(ctx, chat):
            self.queue_notes(ctx, chat, self.held_notes(ctx, chat, "next_round"))
