"""等主人的账 (docs/architecture.md 等主人的账): what waits for the owner in a chat, one entry a thing with the card it is
opened on, and the record of the Bridge's 对账 (app/owner_ledger.py): when it last looked, what it counted, and the cards
it found missing from the chat and sent again.

- An entry (``Entry``): ``state`` the core/look.py key it is shown under (waiting 等你, stopped 停住了, stuck 可能卡住, result
  有结果), ``what`` it is (``WHATS``: a question, a task waiting for 开始, a proposal of 整理专项, a topic's result that stopped,
  a project a guard paused, a running task that may be stuck, a result not seen), ``key`` its own and stable
  (``what:id``), ``topic``, ``since`` (epoch seconds), ``card``, the message id of the card that opens it ('' when it has
  none of its own: a project's card may be off), and ``missing``: its card is known not to have gone to the chat, which
  is what 对账 sends again; it is not counted until its card is there.
- The record, <state>/owner-ledger.json (``RECORD``), written by the Bridge alone under its lock (``update``): ``chats``
  {chat: {at, counts, missing: [key, …]}} as the last 对账 found them, ``resends`` {key: {what, topic, chat, tries,
  first_at, last_at, ok, why}} and ``totals`` {tries, ok, failed}: each try counted before its send (AGENTS rule 1, so a
  send that may have gone is never taken for one that did not) and its outcome after. ``due`` says whether a missing
  card is tried (again): fewer than TRIES, RESEND_EVERY seconds since the last.

Standard library and core only.
"""
from __future__ import annotations

import dataclasses
import time
from pathlib import Path

from covey.core.locks import resource_lock
from covey.core.state import read_for_write, read_object, write_json

RECORD = "owner-ledger.json"
VERSION = 1
# What an entry is, and the core/look.py state it is shown under.
QUESTION, START, PROPOSAL, STOP, PROJECT, STUCK, RESULT = (
    "question", "start", "proposal", "stop", "project", "stuck", "result")
WHATS = {QUESTION: "waiting", START: "waiting", PROPOSAL: "waiting", STOP: "stopped", PROJECT: "stopped",
         STUCK: "stuck", RESULT: "result"}
STATES = ("waiting", "stopped", "stuck", "result")
TRIES = 3  # A missing card is sent again at most this often.
RESEND_EVERY = 600  # Seconds between two tries at one card.
KEPT_RESENDS = 200  # The newest tries kept in the record; older ones go (their totals stay).
WHY_CHARS = 120


@dataclasses.dataclass(frozen=True)
class Entry:
    """One thing that waits for the owner, or is his to see."""
    what: str
    id: str
    topic: str
    since: float
    card: str
    missing: bool = False

    @property
    def key(self) -> str:
        return f"{self.what}:{self.id}"

    @property
    def state(self) -> str:
        return WHATS.get(self.what, "waiting")

    def row(self) -> dict:
        return {"key": self.key, "what": self.what, "state": self.state, "topic": self.topic, "since": self.since,
                "card": self.card, "missing": self.missing}


def counts(entries) -> dict:
    """{state: how many} of ``entries`` not ``missing`` (what the cards count), each of STATES."""
    found = dict.fromkeys(STATES, 0)
    for entry in entries:
        if not entry.missing:
            found[entry.state] = found.get(entry.state, 0) + 1
    return found


def path(state_dir) -> Path:
    return Path(state_dir) / RECORD


def read(state_dir) -> dict:
    """The record as it is ({} without one or when it cannot be read). Never raises."""
    try:
        found = read_object(path(state_dir))
    except (OSError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def update(state_dir, change) -> dict:
    """``change(record)`` → the record to write (None: leave it), under the record's lock; the record written. Only the
    Bridge writes it, never across a Feishu call (AGENTS rules 1 and 2). core/state.py Unreadable (nothing written: the
    counts and tries kept) when the record is there and cannot be read now."""
    target = path(state_dir)
    with resource_lock("owner-ledger", str(target)):
        record = read_for_write(target)
        changed = change({**record, "v": VERSION})
        if changed is None:
            return record
        write_json(target, changed, ensure_ascii=False, indent=2)
        return changed


def due(record, key, now) -> bool:
    """Whether 对账 tries the card of ``key`` now: not sent by an earlier try, fewer than TRIES, RESEND_EVERY seconds
    since the last."""
    found = (record.get("resends") or {}).get(key) if isinstance(record.get("resends"), dict) else None
    if not isinstance(found, dict):
        return True
    return (not found.get("ok") and int(found.get("tries") or 0) < TRIES
            and now - float(found.get("last_at") or 0) >= RESEND_EVERY)


def looked(state_dir, chat, entries, now) -> dict:
    """The record with what 对账 found in ``chat`` now: its counts and the keys of the entries missing a card."""
    missing = [entry.key for entry in entries if entry.missing]

    def change(record):
        chats = dict(record.get("chats") or {}) if isinstance(record.get("chats"), dict) else {}
        chats[chat] = {"at": now, "counts": counts(entries), "missing": missing}
        return {**record, "chats": chats}
    return update(state_dir, change)


def trying(state_dir, chat, entry, now) -> bool:
    """A try at ``entry``'s card counted before it is sent (False: not ``due``, nothing to send)."""
    taken = []

    def change(record):
        if not due(record, entry.key, now):
            return None
        resends = dict(record.get("resends") or {}) if isinstance(record.get("resends"), dict) else {}
        last = resends.get(entry.key) if isinstance(resends.get(entry.key), dict) else {}
        resends[entry.key] = {"what": entry.what, "topic": entry.topic, "chat": chat,
                              "tries": int(last.get("tries") or 0) + 1, "first_at": last.get("first_at") or now,
                              "last_at": now, "ok": None, "why": ""}
        if len(resends) > KEPT_RESENDS:
            for old in sorted(resends, key=lambda key: float(resends[key].get("last_at") or 0))[:len(resends) - KEPT_RESENDS]:
                resends.pop(old, None)
        totals = dict(record.get("totals") or {}) if isinstance(record.get("totals"), dict) else {}
        totals["tries"] = int(totals.get("tries") or 0) + 1
        taken.append(True)
        return {**record, "resends": resends, "totals": totals}
    update(state_dir, change)
    return bool(taken)


def tried(state_dir, entry, ok, why="") -> None:
    """The outcome of the try ``trying`` counted: sent (``ok``) or not, and why not."""
    def change(record):
        resends = dict(record.get("resends") or {}) if isinstance(record.get("resends"), dict) else {}
        last = resends.get(entry.key)
        if not isinstance(last, dict):
            return None
        resends[entry.key] = {**last, "ok": bool(ok), "why": str(why or "")[:WHY_CHARS], "done_at": time.time()}
        totals = dict(record.get("totals") or {}) if isinstance(record.get("totals"), dict) else {}
        name = "ok" if ok else "failed"
        totals[name] = int(totals.get(name) or 0) + 1
        return {**record, "resends": resends, "totals": totals}
    update(state_dir, change)


def summary(record) -> dict:
    """What the 诊断 says of the record: {checked_at, counts, missing, tries, ok, failed, recent: [{key, what, topic,
    tries, ok, why, last_at}, …] newest first, at most 5}."""
    chats = record.get("chats") if isinstance(record.get("chats"), dict) else {}
    newest = max((row for row in chats.values() if isinstance(row, dict)), key=lambda row: float(row.get("at") or 0),
                 default={})
    totals = record.get("totals") if isinstance(record.get("totals"), dict) else {}
    resends = record.get("resends") if isinstance(record.get("resends"), dict) else {}
    recent = sorted(((key, row) for key, row in resends.items() if isinstance(row, dict)),
                    key=lambda item: float(item[1].get("last_at") or 0), reverse=True)[:5]
    return {"checked_at": float(newest.get("at") or 0), "counts": dict(newest.get("counts") or {}),
            "missing": list(newest.get("missing") or ()), "tries": int(totals.get("tries") or 0),
            "ok": int(totals.get("ok") or 0), "failed": int(totals.get("failed") or 0),
            "recent": [{"key": key, "what": row.get("what"), "topic": row.get("topic"), "tries": row.get("tries"),
                        "ok": row.get("ok"), "why": row.get("why") or "", "last_at": row.get("last_at")}
                       for key, row in recent]}
