"""The family ledger of 多路并行 (docs/butler.md 记忆合并): what the butler's other tracks (core/butler_tracks.py,
slots 1..) answered, for the butler's next turn to read. A track is a fork of the butler's session, so what it answered
is not in the butler's own session: each turn of a track that ended writes one record here, and the butler's
next turn (slot 0) is given the ones it has not read yet, in one block of its message.

main-thread/tracks/<job key>.json: {key, chat_id, track (slot), owner_message (the owner's message id), request (at
most REQUEST_CHARS), answer (at most ANSWER_CHARS, the delivered reply and only that: "" when none went out), status
(the turn's), ended_at, session_id (the track's session: maintenance removes its file, core/maintenance_rules.py),
opened (the T numbers of the topics the turn opened or handed on to), digested_by (the key of the butler turn that
was given it, "" while none was)}. Written once, when the turn's result is delivered (app/track_digest.py); the butler's
 turn then only sets ``digested_by``, by compare-and-set under the record lock (``claim``).

Standard library and core only: nothing here talks to Feishu or holds a lock across anything but its own file.
"""
from __future__ import annotations

from pathlib import Path

from covey.core.text import cut, stamp

from covey.core.locks import resource_lock
from covey.core.state import read_object, write_json

FOLDER = Path("main-thread") / "tracks"
REQUEST_CHARS, ANSWER_CHARS = 200, 600  # What a record keeps.
SHOW_CHARS = 300  # How much of the answer the butler's block shows.
WINDOW = 12 * 3600  # Seconds: a record older than this is not given any more.
MAX_ROWS = 8  # Rows in the block.
HEADER = "[另外几件事这段时间答了什么]"


def folder(state_dir) -> Path:
    return Path(state_dir) / FOLDER


def path(state_dir, key) -> Path:
    return folder(state_dir) / f"{key}.json"


def make(key, chat_id, track, owner_message, request, answer, status, ended_at, session_id, opened) -> dict:
    """One record, its text cut to what a record keeps."""
    return {"key": key, "chat_id": chat_id, "track": track, "owner_message": owner_message,
            "request": cut(request, REQUEST_CHARS), "answer": cut(answer, ANSWER_CHARS), "status": status,
            "ended_at": float(ended_at), "session_id": session_id or "", "opened": list(opened), "digested_by": ""}


def write(state_dir, record) -> None:
    """Save ``record`` (a new one: ``digested_by`` stays "")."""
    target = path(state_dir, record["key"])
    target.parent.mkdir(parents=True, exist_ok=True)
    with resource_lock("record", target):
        write_json(target, record, ensure_ascii=False)


def records(state_dir) -> list:
    """Every record of the folder that reads as one."""
    where = folder(state_dir)
    found = [read_object(p) for p in sorted(where.glob("*.json"))] if where.is_dir() else []
    return [r for r in found if isinstance(r.get("key"), str) and r.get("key")]


def pending(state_dir, chat_id, key, now, settled) -> tuple:
    """(the rows to give, how many more there were): the chat's records ended within WINDOW that the turn ``key`` was
    not given yet, the newest MAX_ROWS of them, oldest first. Not given yet: ``digested_by`` is empty, or names a turn
    that did not end well (``settled(turn key)`` false: it did not read them), or is ``key`` itself (run again). The
    turn's own record is none of them."""
    rows = []
    for record in records(state_dir):
        ended = float(record.get("ended_at") or 0)
        by = str(record.get("digested_by") or "")
        if (record.get("chat_id") != chat_id or record["key"] == key or not now - WINDOW <= ended <= now + 60
                or (by and by != key and settled(by))):
            continue
        rows.append(record)
    rows.sort(key=lambda r: (float(r.get("ended_at") or 0), r["key"]))
    return rows[-MAX_ROWS:], max(0, len(rows) - MAX_ROWS)


def render(rows, more=0) -> str:
    """The block of ``rows`` (``pending``'s): one line each, the request → the answer's first SHOW_CHARS → the T
    numbers; "" for none."""
    if not rows:
        return ""
    lines = [HEADER + "（它们在各自的会话里答的，不在你的会话里；按完成先后，答案只列前"
             f" {SHOW_CHARS} 字，要细节用 topics_search、topic_read）"]
    for record in rows:
        ended = stamp(record.get("ended_at"), "%H:%M", "--:--", lenient=True)
        status = {"done": "", "failed": "（失败）", "cancelled": "（已停止）"}.get(record.get("status"), f"（{record.get('status')}）")
        answer = cut(record.get("answer"), SHOW_CHARS) or "没有送达的回答"
        opened = "、".join(str(t) for t in record.get("opened") or ())
        lines.append(f"- {ended}{status} 请求「{cut(record.get('request'), REQUEST_CHARS)}」→ {answer}"
                     + (f" → {opened}" if opened else ""))
    if more:
        lines.append(f"（更早的还有 {more} 条没列）")
    return "\n".join(lines)


def claim(state_dir, rows, key, settled) -> int:
    """Mark ``rows`` given to the turn ``key``: ``digested_by`` set under the record lock, when it still is what
    ``pending`` saw (empty, ``key`` or a turn that did not end well). How many were set."""
    done = 0
    for row in rows:
        target = path(state_dir, row["key"])
        with resource_lock("record", target):
            current = read_object(target)
            by = str(current.get("digested_by") or "")
            if current.get("key") != row["key"] or (by and by != key and settled(by)):
                continue
            if by != key:
                write_json(target, {**current, "digested_by": key}, ensure_ascii=False)
            done += 1
    return done
