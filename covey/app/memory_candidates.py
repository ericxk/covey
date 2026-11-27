"""每天一次的候选整理 (docs/butler.md 记忆, COVEY_MEMORY=1 with 早晚汇总): what the owner corrected more than once in the
day's ended topics, at most SHOWN a day, put in 待审核 (app/digest.py, the day's last digest time) with its source
topics. Nothing takes effect without the owner's word there: 记下 writes a principle (core/principles.py ``save``: the
workspace's when every source topic is of one workspace, else 通用), 不用 sets it aside and the same thing is not offered
again (``declined``). The evening digest no longer lists them; it only says how many wait (记忆：待审核 N 条). By day the memory only takes what the owner says outright (app/memory_collect.py); this only
proposes.

What it reads (``sentences``): the owner's own words (the job record's ``user_text``) of the turns that ended in the 24 hours
before the digest time, of ordinary topics (not the butler's, not a 代答 topic, not a turn the butler handed on), never of a
topic with redaction on or whose setting cannot be read; each message split into sentences, the ones with a correction's word
(CUES) kept, at most PER_TOPIC a topic and MAX_SENTENCES in all. Nothing is read unless two topics or more have such words.

Who groups them (``find``): the rules alone (``by_rules``, no model is called), which group sentences that read nearly the same
(core/topic_gist.py ``same_essence``): a correction said in other words is missed, never invented. A group must span two
topics or more; one that says what a principle already says (core/principles.py ``same_key``) or what the owner declined is
left out.

The record, <state>/memory-candidates.json: {schema_version, rows: [{id, text, topics, labels, workspace, slot, at, state
(open, saved, declined, expired), decided_at, principle, error, carried?, source?}], declined: [same_key, …], ran: [slot, …]};
a digest time is looked at once (``ran``), rows older than KEPT_DAYS go. Written under its own lock, never across a Feishu
call (rule 1). Log lines never hold the owner's words.

What it finds now goes to 待审核 (core/memory_review.py, type candidate, flagged ``repeat``), not into the rows below (the rows
an older release wrote stay readable and can still be decided); ``carried`` gives nothing: a turn does not read an open
candidate as 它总结的 any more. The rest of this paragraph is how the rows of an older release behaved.

With COVEY_MEMORY_LEARN=1 too (core/memory.py learning, docs/butler.md 记忆自己长) what the owner repeated takes effect before
his click: a row offered then is ``carried`` and, while open, every turn reads it as a line of 它总结的 (``carried``,
app/memory_turn.py; 全局 ones on his own turns only), at most CARRIED of them, the newest; 记下 makes it his (a principle), 不用
takes it out for good. An open carried row stays past KEPT_DAYS until he decides it or 整理记忆 lets it go as unused
(app/memory_tidy.py ``expire``: ``expired``). Rows of other ``source``s (``SOURCE_WORDS``) are of older releases: never
carried, and a click on the web page's 记忆 tab decides them (``decide``).

A turn counts only when its words are the owner's (``not_owner_reason``): a revisit, a Bridge wake-up or note, a hand-on, a
review, a 协作 turn are the Bridge's or an agent's words and are never read; of an answer only what he answered.
"""
from __future__ import annotations

import re
import time
from collections.abc import Mapping
from pathlib import Path

from covey.core import memory, memory_review, memory_usage, principles, topic_gist, turn_source
from covey.core.board_read import ended_at
from covey.core.butler import is_butler_topic
from covey.core.butler_opened import handed_on
from covey.core.jobs import select_job_records
from covey.core.locks import resource_lock
from covey.core.redaction import topic_redaction
from covey.core.state import epoch_seconds, read_object, write_json
from covey.core.text import cut
from covey.core.topics import label_title, load_topic

FILE = "memory-candidates.json"
SCHEMA = 1
SHOWN = 3  # Candidates a day.
DAY = 86400
KEPT_DAYS = 14
RAN_KEPT = 30
DECLINED_KEPT = 200
PER_TOPIC = 6  # Sentences of one topic kept.
MAX_SENTENCES = 40
SENTENCE = 120  # Characters of a sentence sent.
TEXT = 80  # Characters of a candidate's rule.
ENDED = ("done", "waiting_user", "failed", "interrupted", "cancelled")
# A correction's words: memory's own (core/memory.py CORRECTION_WORDS) and the ones a repeated correction is said with.
# Only words that say "from now on" (T852): everyday ones (先, 不要, 别, 不是, 应该, 改成, 又, 必须, 记得) are in nearly every message
# and sent a small model after each of them for nothing.
CUES = (*memory.CORRECTION_WORDS, "一律", "永远", "总是", "老是", "说过", "规则是", "默认")
BRIDGE_IDS = ("revisit-", "main-", "review-", "lead-")  # Message ids the Bridge makes up for a turn nobody spoke: 回看, a hand-on, 复核, a lead woken.
BRIDGE_TEXT = ("[Bridge", "【Bridge")  # What the Bridge starts its own messages with (叫醒, 唤醒, 说明, 协作…).
SPLIT = re.compile(r"(?<=[。！？!?；;\n])")
OPEN, SAVED, DECLINED, EXPIRED = "open", "saved", "declined", "expired"
CARRIED = 8  # Open carried rows a turn reads (COVEY_MEMORY_LEARN=1), the newest.
REPEAT = "repeat"  # What the owner repeated in the day's topics (``find``): the rows of every release before ``source``.
LEARN = "learn"  # What the learner (app/memory_learn.py) took of one turn that reaches past its topic: waits for his 记下.
SOURCE_WORDS = {REPEAT: "", "codex": "Codex 自己记的", "claude": "Claude Code 自己记的", LEARN: "自动学习记的"}  # How a row's card line says where it is from.
FULL = f"这一组的记忆已有 {principles.GROUP_LIMIT} 条，先在菜单「设置」→「记忆」里删掉不用的"  # 记下 refused: the group is full.


def path(state_dir) -> Path:
    return Path(state_dir) / FILE


def _at(row) -> float:
    """When ``row`` was offered; 0 for a time that cannot be read (it goes with the old ones)."""
    at = row.get("at")
    return float(at) if isinstance(at, (int, float)) and not isinstance(at, bool) else 0.0


def load(state_dir) -> dict:
    data = read_object(path(state_dir))
    rows = [row for row in data.get("rows") or () if isinstance(row, dict) and isinstance(row.get("id"), str)]
    return {"schema_version": SCHEMA, "rows": rows,
            "declined": [key for key in data.get("declined") or () if isinstance(key, str)],
            "ran": [slot for slot in data.get("ran") or () if isinstance(slot, (int, float)) and not isinstance(slot, bool)]}


def save(state_dir, data) -> None:
    write_json(path(state_dir), data, ensure_ascii=False, indent=1)


def lock(state_dir):
    return resource_lock("memory-candidates", str(path(state_dir)))


def _redacted(state_dir, topic_id) -> bool:
    try:
        return bool(topic_redaction(state_dir, topic_id))
    except ValueError:
        return True


def _proxy(state_dir, topic_id) -> bool:
    try:
        return bool(load_topic(Path(state_dir), topic_id).get("proxy"))
    except (OSError, ValueError, AttributeError):
        return False


def not_owner_reason(*items, text="") -> str:
    """Why the words of the turn of ``items`` (a job and its record) are not the owner's own ('' when they are): the turn's
    source (core/turn_source.py) decides, by its level and whether its words are the owner's, and only his own message or answer passes; a
    turn with none is as before, and then the message id the Bridge makes up (BRIDGE_IDS: a revisit, a hand-on, a review) and,
    as a last resort, the way its text begins (BRIDGE_TEXT) say it is the Bridge's."""
    value = turn_source.of(*items)
    if value is not None:
        found = turn_source.kind(*items)
        asserted = value.get("asserted_by") if isinstance(value, Mapping) else ""
        if (not turn_source.behavior(*items).owner_words or found not in (turn_source.OWNER, turn_source.ANSWER)
                or asserted != turn_source.PARTY_OWNER
                or (found == turn_source.ANSWER and turn_source.level(*items) != turn_source.LEVEL_OWNER)):
            return "source"  # A hand-on, a wake-up, a lead's answer for him, a 协作 turn; an answer of his own passes.
    for item in items:
        message = item.get("message_id") if isinstance(item, Mapping) else getattr(item, "message_id", "")
        if isinstance(message, str) and message.startswith(BRIDGE_IDS):
            return "bridge-id"
    if str(text or "").lstrip().startswith(BRIDGE_TEXT):
        return "bridge-text"
    return ""


def owner_words(record, state_dir=None) -> str:
    """The owner's own words of the turn of job ``record`` (app/memory_turn.py calls it): its ``user_text``, but of an answer's
    turn (app/answers.py
    answer_message) only what he answered and the notes he added, and of a feedback only the feedback; the question's own
    words, written by the Bridge or a model, are never his. ``state_dir``: where the question record is, to tell an answer of
    several lines from its question exactly (app/answer_cards.py asked_questions)."""
    from covey.app.answer_cards import asked_questions
    from covey.app.answers import FEEDBACK_START, answer_parts
    said = str(record.get("user_text") or "")
    if said.startswith(FEEDBACK_START):
        return said[len(FEEDBACK_START):]
    asked = asked_questions(state_dir, str(record.get("message_id") or "")) if state_dir is not None else []
    parts = answer_parts(said, asked)
    if parts:
        return "\n".join(text for kind, text in parts if kind in ("answer", "note"))
    return said


def cued(text) -> list:
    """The sentences of ``text`` with a correction's word, each on one line, cut to SENTENCE characters."""
    found = []
    for part in SPLIT.split(str(text or "")):
        part = " ".join(part.split())
        if len(part) >= 4 and any(word in part for word in CUES):
            found.append(cut(part, SENTENCE))
    return found


def sentences(state_dir, chat, since, until, owner_turn) -> list:
    """[{topic, text}] the day's correction sentences (the module's docstring), the oldest first; ``owner_turn(chat,
    sender)`` is app/access.py's: whether a turn was the owner's own."""
    def fresh(fields):  # A turn that ended in the window was written in it, or after; a record without the time is read.
        at = epoch_seconds(fields.get("updated_at"))
        return at is None or at > since

    records = select_job_records(state_dir, lambda record: record.get("status") in ENDED and since < ended_at(record) <= until,
                                 chat_id=chat, narrow=fresh)
    found, per = [], {}
    for record in sorted(records, key=ended_at):
        topic = str(record.get("topic_id") or "")
        if (not topic or is_butler_topic(topic, chat) or handed_on(record.get("message_id"))
                or not_owner_reason(record, text=record.get("user_text"))  # A revisit, a wake-up, a 协作 turn: not his words.
                or not owner_turn(chat, str(record.get("sender_id") or "")) or _proxy(state_dir, topic)
                or _redacted(state_dir, topic)):
            continue
        for text in cued(owner_words(record, state_dir)):  # An answer's turn: his answer, never the question's words.
            if per.get(topic, 0) >= PER_TOPIC or any(row["topic"] == topic and row["text"] == text for row in found):
                continue
            per[topic] = per.get(topic, 0) + 1
            found.append({"topic": topic, "text": text})
    return found[-MAX_SENTENCES:]


def by_rules(rows) -> list:
    """The groups the rules alone find: sentences of different topics that read nearly the same, the shortest the rule."""
    groups, used = [], set()
    for i, row in enumerate(rows):
        if i in used:
            continue
        mine = [i] + [j for j in range(i + 1, len(rows)) if j not in used and topic_gist.same_essence(row["text"], rows[j]["text"])]
        if len({rows[j]["topic"] for j in mine}) >= 2:
            used.update(mine)
            groups.append((min((rows[j]["text"] for j in mine), key=len), mine))
    return groups


def workspace_of(state_dir, topics) -> str:
    """The workspace every topic of ``topics`` is in, '' when they are not all in one (the rule is then 通用)."""
    names = set()
    for topic in topics:
        try:
            names.add(str(load_topic(Path(state_dir), topic).get("workspace_name") or ""))
        except (OSError, ValueError, AttributeError):
            names.add("")
    return names.pop() if len(names) == 1 else ""


def find(state_dir, chat, slot, *, owner_turn, log=print) -> list:
    """The day's candidates before ``slot``: [{text, topics, labels, workspace}], at most SHOWN, none already a principle
    or declined (the module's docstring). Never raises."""
    try:
        rows = sentences(state_dir, chat, slot - DAY, slot, owner_turn)
        if len({row["topic"] for row in rows}) < 2:
            log(f"memory candidates none: {len({row['topic'] for row in rows})} topics with corrections")
            return []
        for row in rows:
            row["label"] = label_title(state_dir, row["topic"])[0] or "话题"
        existing = [row["text"] for row in principles.entries(state_dir)]
        declined = load(state_dir)["declined"]
        groups = by_rules(rows)
        taken = {principles.same_key(text) for text in existing} | set(declined)
        found = []
        for rule, picked in groups:
            key = principles.same_key(rule)
            if not key or key in taken:
                continue
            taken.add(key)
            topics = list(dict.fromkeys(rows[i]["topic"] for i in picked))
            found.append({"text": rule, "topics": topics, "labels": [rows[i]["label"] for i in picked],
                          "workspace": workspace_of(state_dir, topics)})
            if len(found) >= SHOWN:
                break
        for item in found:
            item["labels"] = list(dict.fromkeys(item["labels"]))
        log(f"memory candidates found={len(found)} sentences={len(rows)}")
        return found
    except Exception as exc:  # noqa: BLE001 - the digest goes out without them
        log(f"memory candidates failed: {type(exc).__name__}")
        return []


def offer(state_dir, chat, slot, finder, *, now=None) -> None:
    """The candidates of the digest time ``slot``: looked for once (``finder()``, outside the lock); what it finds goes to 待审核
    (core/memory_review.py, type candidate, flagged ``repeat``) and no longer into this record's rows."""
    now = time.time() if now is None else now
    with lock(state_dir):
        if slot in load(state_dir)["ran"]:
            return
    found = finder()
    for item in found:
        try:
            memory_review.add(state_dir, memory_review.ReviewItem(
                key="", type="candidate", title="", text=item["text"], suggest={"kind": "rule", "scope": "global"},
                created_at=now, origin={"topics": list(item.get("topics") or ()), **({"workspace": item["workspace"]} if item.get("workspace") else {})},
                flags=("repeat",)))
        except (OSError, ValueError):
            continue  # One that cannot be put in the queue is not offered; the digest goes out without it.
    with lock(state_dir):
        data = load(state_dir)
        if slot not in data["ran"]:
            data["ran"] = [*data["ran"], slot][-RAN_KEPT:]
            data["rows"] = kept_rows(data["rows"], now)
            save(state_dir, data)


def kept_rows(rows, now) -> list:
    """``rows`` without the ones offered over KEPT_DAYS ago, but an open carried one: it counts until it is decided or let go."""
    return [row for row in rows if _at(row) >= now - KEPT_DAYS * DAY or (row.get("carried") and row.get("state") == OPEN)]


def carried(state_dir, workspace_name) -> tuple[list, list]:
    """(通用, the workspace's) open carried rows of the older record, each {id, text, at}, the newest CARRIED in all; ([], [])
    without the record. A turn no longer reads them (app/memory_turn.py carries nothing that waits for the owner): the web
    page's older 记忆 tab lists them, and new candidates wait in 待审核 (core/memory_review.py)."""
    rows = sorted((row for row in load(state_dir)["rows"] if row.get("carried") and row.get("state") == OPEN
                   and isinstance(row.get("text"), str) and row["text"].strip()), key=lambda row: -_at(row))[:CARRIED]
    shaped = [{"id": row["id"], "text": row["text"], "at": _at(row), "workspace": str(row.get("workspace") or "")}
              for row in rows]
    return ([row for row in shaped if not row["workspace"]],
            [row for row in shaped if workspace_name and row["workspace"] == workspace_name])


def open_carried(state_dir) -> list:
    """Every open carried row, {id, at} (app/memory_tidy.py lets the unused ones go)."""
    return [{"id": row["id"], "at": _at(row)} for row in load(state_dir)["rows"]
            if row.get("carried") and row.get("state") == OPEN]


def reopen(state_dir, ids) -> list:
    """整理记忆's 撤销 (app/memory_tidy.py): the rows ``ids`` it let go open and carried again; the ids reopened."""
    wanted, changed = set(ids), []
    with lock(state_dir):
        data = load(state_dir)
        for row in data["rows"]:
            if row["id"] in wanted and row.get("state") == EXPIRED:
                row["state"] = OPEN
                row.pop("decided_at", None)
                changed.append(row["id"])
        if changed:
            save(state_dir, data)
    return changed


def expire(state_dir, ids, *, now=None) -> list:
    """Open carried rows ``ids`` no turn used for long (app/memory_tidy.py): ``expired``, no longer read; the rows changed."""
    now = time.time() if now is None else now
    wanted, changed = set(ids), []
    with lock(state_dir):
        data = load(state_dir)
        for row in data["rows"]:
            if row["id"] in wanted and row.get("state") == OPEN and row.get("carried"):
                row.update(state=EXPIRED, decided_at=now)
                changed.append(dict(row))
        if changed:
            save(state_dir, data)
    return changed


def decide(state_dir, candidate_id, op, *, now=None) -> dict | None:
    """「记下」 (``op`` save: the principle written) or 「不用」 (drop) on candidate ``candidate_id``: the row as it is then;
    None when there is none. A row decided before is left as it is (a click delivered twice, or two cards)."""
    now = time.time() if now is None else now
    with lock(state_dir):
        data = load(state_dir)
        row = next((row for row in data["rows"] if row["id"] == candidate_id), None)
        if row is None or row.get("state") != OPEN or op not in ("save", "drop"):
            return row
        if op == "drop":
            row.update(state=DECLINED, decided_at=now)
            data["declined"] = [*data["declined"], principles.same_key(row["text"])][-DECLINED_KEPT:]
        else:
            scope = principles.WORKSPACE if row.get("workspace") else principles.OWNER
            try:
                item, _ = principles.save(state_dir, scope, row["text"], workspace=row.get("workspace") or "",
                                          source={"by": "memory_candidates", "topics": list(row.get("topics") or ())})
            except ValueError as exc:  # Its words are the butler's (a tool's name in them): the owner reads FULL.
                row["error"] = FULL if f"已有 {principles.GROUP_LIMIT} 条" in str(exc) else str(exc)
            else:
                row.update(state=SAVED, decided_at=now, principle=item["id"])
                memory_usage.carry(state_dir, f"candidate:{row['id']}", f"principle:{item['id']}")
                row.pop("error", None)
        save(state_dir, data)
        return dict(row)


class Candidates:
    """The digest's hook (app/digest.py ``candidates``): ``runtime`` for the owner and the log."""

    def __init__(self, runtime):
        self.runtime = runtime

    def offer(self, ctx, chat, slot) -> None:
        offer(ctx.state_dir, chat, slot, lambda: find(
            ctx.state_dir, chat, slot, owner_turn=self.runtime.access.owner_turn, log=self.runtime.log))
