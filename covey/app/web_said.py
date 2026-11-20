"""Who said a topic's owner line and the words the web page shows for it (docs/butler.md 网页工作台, data contract v11 ``by``
and ``display``). A topic's history keeps each line as its model was sent it: the Bridge's headings ("用户补充回答：", "【主人
编辑了刚才的消息】……") and lines the owner never wrote (work the butler or a project's lead handed on in its own words, an answer
the butler or the lead gave for him) read there as his. The page shows them as the cards do: who said it, and the words.

- ``by``: ``owner``, ``butler`` or ``lead`` (BY); ``via``: ``butler`` or ``lead`` for the owner's own words one of them handed on.
- ``display``: the words without the headings, only when they differ from the line; ``edited`` for an edit of his message;
  an answer's line also gets its ``answers`` ({question, choice}) when the question record did not give them, and ``note``,
  what was written beside the choices.

``Origins`` reads the butler's request records (core/butler_opened.py) and job records once per message id; the rest is pure."""
from __future__ import annotations

import re
from pathlib import Path

from covey.app.answers import ANSWER_START, FEEDBACK_START, SKIPPED, answer_parts
from covey.app.source_watch import EDIT_HEAD, RECALL_HEAD
from covey.core import butler_opened, projects, triage
from covey.core.jobs import find_job_record
from covey.core.text import cut

BY = ("owner", "butler", "lead")
NOT_ANSWERED = "没答"
HANDED = "main-"  # The message ids of the butler's and the leads' hand-ons (app/butler_topics.py, app/butler_projects.py project_send).
LEAD_NOTE = "（负责人按专项记忆替主人答的）"  # app/triage.py NOTE: an answer the lead gave.
BUTLER_NOTE = "（管家按主人的话代答）"  # app/butler_tools.py PROXY_NOTE: an answer the butler gave on the owner's words.
FIX = re.compile(r"^（主人纠正负责人替答的那一条：(.*?)）", re.S)  # app/triage.py correct: the owner corrects the lead's answer.
CHOICE_CHARS = 60


class Origins:
    """Who wrote a hand-on (a ``main-`` message): {message id: (by, via)}, read once each. Its request record names the
    butler job or the lead's job that made it; that job answering a message of the owner's means the words handed on are
    his (app/butler_handoff.py shown), else they are the job's own."""

    def __init__(self, state, chat) -> None:
        self.state, self.chat = Path(state), str(chat or "")
        self.known = {}

    def of(self, message_id) -> tuple:
        message_id = str(message_id or "")
        if message_id not in self.known:
            if len(self.known) > 2048:
                self.known.clear()
            self.known[message_id] = self.read(message_id)
        return self.known[message_id]

    def read(self, message_id) -> tuple:
        from covey.app.relay_targets import butler_job_owner_message

        record = butler_opened.by_token(self.state, message_id[len(HANDED):])
        key = str(record.get("job_key") or "")
        job = find_job_record(self.state, key) if key else None
        if not job:
            return "owner", ""  # Its records are gone: the line stays the owner's, as the page had it.
        role = "lead" if projects.is_lead_topic(self.state, str(job.get("topic_id") or ""), job.get("chat_id") or self.chat) else "butler"
        return ("owner", role) if butler_job_owner_message(self.state, key) else (role, "")


def said(row, record=None, origins=None) -> dict:
    """The fields the page reads of the owner line ``row`` (a history row): {by, via?, display?, edited?, answers?, note?}.
    ``record``: the question record an answer's line answers (``answer_record``), None when it is not one or none matched;
    ``origins``: an ``Origins`` for the hand-ons (without it, a hand-on is the owner's)."""
    body = str(row.get("display_content") or row.get("content") or "")
    message_id = str(row.get("message_id") or "")
    text = body.lstrip()
    out = {"by": "owner"}
    if text.startswith(ANSWER_START):
        out.update(answer_view(text, record))
    elif text.startswith(FEEDBACK_START):
        out["display"] = "没答，补充：" + text[len(FEEDBACK_START):].strip()
        out["by"] = answered_by(text, record)
    elif text.startswith(EDIT_HEAD):
        out["display"], out["edited"] = text[len(EDIT_HEAD):].strip(), True
    elif text.startswith(RECALL_HEAD):
        out["display"] = "撤回了刚才那条补充" + (("：" + text.split("：", 1)[1]) if "：" in text else "")
    elif FIX.match(text):
        found = FIX.match(text)
        out["display"] = f"纠正负责人替你答的「{cut(found[1], CHOICE_CHARS)}」：{text[found.end():].strip()}"
    elif message_id.startswith(HANDED) and origins is not None:
        by, via = origins.of(message_id)
        out["by"] = by
        if via:
            out["via"] = via
    if "display" not in out and EDIT_HEAD in text:
        # A line that carries the owner's message and, after it, his edit of it (a hand-on of a message he edited while the
        # butler read it, T584 2026-11-20): the page shows the edited text alone, marked edited; the line itself is what the
        # model was sent and is not changed.
        out["display"], out["edited"] = text.split(EDIT_HEAD, 1)[1].strip(), True
    if out.get("display") == body:
        out.pop("display")
    return out


def answer_view(text, record) -> dict:
    """An answer's line: who answered, its choices as one line (``display``), and what was written beside them (``note``)."""
    questions = [str(q.get("question") or "") for q in (record or {}).get("questions") or () if isinstance(q, dict)]
    parts = answer_parts(text, questions)
    pairs, notes, question = [], [], ""
    for kind, words in parts:
        if kind == "question":
            question = words
        elif kind == "answer":
            given = [part for part in words.split("；") if part.strip() and part.strip() != SKIPPED]
            pairs.append({"question": question[:500], "choice": "、".join(given)[:500] or NOT_ANSWERED})
            question = ""
        elif kind == "note":
            words = words.replace(LEAD_NOTE, "").replace(BUTLER_NOTE, "").strip()
            if words:
                notes.append(words)
    out = {"by": answered_by(text, record)}
    if pairs:
        choices = [pair["choice"] for pair in pairs]
        given = [choice for choice in choices if choice != NOT_ANSWERED]
        line = (f"答了 {len(pairs)} 题：" + "；".join(cut(choice, CHOICE_CHARS) for choice in choices) if given
                else "没答" if notes else f"{len(pairs)} 题都没答")
        out["answers"] = pairs
    else:
        line = "回答了问题"
    if notes:
        out["note"] = "\n\n".join(notes)
    out["display"] = line + (("，补充：" if line == "没答" else "\n补充：") + out["note"] if notes else "")
    return out


def answered_by(text, record) -> str:
    """Who answered: the lead (the question triage, app/triage.py), the butler (question_answer, on the owner's words), or
    the owner; the question record says it when there is one, else the note the answer carries."""
    record = record or {}
    event = str(record.get("answer_event") or "")
    if triage.state(record) == triage.ANSWERED or event.startswith("lead-") or LEAD_NOTE in text:
        return "lead"
    if event.startswith(HANDED) or str(record.get("supplement") or "").startswith(BUTLER_NOTE) or BUTLER_NOTE in text:
        return "butler"
    return "owner"


def answer_record(row, questions, taken=None):
    """The question record an answer's (or feedback's) line ``row`` answers, of the topic's ``questions``: the newest one
    asked before the line whose first question the line begins with; None for another line or none matched. ``taken``:
    the tokens matched to earlier lines (a set this adds to), so two answers to the same questions get one record each."""
    from covey.core.state import epoch_seconds

    text = str(row.get("content") or "").lstrip()
    if text.startswith(ANSWER_START):
        first = text[len(ANSWER_START):].lstrip("\n").split("\n", 1)[0].strip()
    elif text.startswith(FEEDBACK_START):
        first = ""
    else:
        return None
    at = epoch_seconds(row.get("time")) or 0
    found = None
    for record in questions:
        asked = record.get("questions") or ()
        head = str(asked[0].get("question") or "").strip() if asked and isinstance(asked[0], dict) else ""
        if not record.get("answers") or (taken is not None and record.get("token") in taken):
            continue
        if first and head.split("\n", 1)[0].strip() != first:
            continue
        if not first and record.get("response_kind") != "feedback":
            continue
        made = float(record.get("created_at") or 0)
        if at and made > at + 1:
            continue
        if found is None or made >= float(found.get("created_at") or 0):
            found = record
    if found is not None and taken is not None:
        taken.add(found.get("token"))
    return found
