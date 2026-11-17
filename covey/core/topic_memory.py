"""The memory of one topic (COVEY_MEMORY=1, docs/butler.md 记忆): what the owner said in this topic that holds for the
topic and nowhere wider ("这个不要问了"), in ``<state>/topics/<id>/memory.json``, the file format of a project's memory
(core/project_memory.py: {id, text, kind, source, at, deleted, redacted?}). Only the Bridge writes it
(app/memory_collect.py, after the turn), always as the owner's word (source ``owner``), at most ``MAX_ENTRIES`` live
ones (past it the oldest goes). A delete keeps the entry as a tombstone. Every write takes the file's shared lock
(core/locks.py); a read takes none. A file that cannot be parsed is read as empty and set aside before the next write.
Standard library and core only."""
from __future__ import annotations

import secrets
import time
from pathlib import Path

from covey.core import project_memory
from covey.core.locks import shared_file_lock
from covey.core.state import write_json
from covey.core.topics import topic_file

FILE = "memory.json"
MAX_ENTRIES = 20


def memory_path(state_dir, topic_id) -> Path:
    return topic_file(Path(state_dir), str(topic_id)).with_name(FILE)


def live(state_dir, topic_id) -> list:
    """The topic's entries not deleted, the oldest first; [] without a topic or a file."""
    if not topic_id:
        return []
    return [row for row in project_memory.rows_at(memory_path(state_dir, topic_id)) if not row["deleted"]]


def _write(path, rows) -> None:
    write_json(path, {"schema_version": project_memory.SCHEMA, "entries": rows}, ensure_ascii=False, indent=1)


def add(state_dir, topic_id, text, *, source="owner", redacted=False, now=None) -> tuple:
    """(the entry, whether it is new): ``text`` as a constraint of the topic. The same text still there is the one returned."""
    stamp = time.time() if now is None else now
    wanted = {"id": secrets.token_hex(3), "text": project_memory.clean(text), "kind": "constraint",
              "source": str(source)[:30], "at": stamp, "deleted": 0, **({"redacted": True} if redacted else {})}
    if not wanted["text"]:
        raise ValueError("text 不能为空")
    path = memory_path(state_dir, topic_id)
    with shared_file_lock(path):
        rows = project_memory.rows_at(path, writing=True)
        same = next((row for row in rows if not row["deleted"] and row["text"] == wanted["text"]), None)
        if same is not None:
            return same, False
        rows.append(wanted)
        while sum(1 for row in rows if not row["deleted"]) > MAX_ENTRIES:
            rows.remove(next(row for row in rows if not row["deleted"]))
        _write(path, rows)
    return wanted, True


def delete(state_dir, topic_id, entry_id, *, now=None) -> bool:
    """Whether ``entry_id`` was a live entry of the topic: it becomes a tombstone (``deleted`` is the time)."""
    path = memory_path(state_dir, topic_id)
    with shared_file_lock(path):
        rows = project_memory.rows_at(path, writing=True)
        row = next((row for row in rows if row["id"] == str(entry_id) and not row["deleted"]), None)
        if row is None:
            return False
        row["deleted"] = time.time() if now is None else now
        _write(path, rows)
    return True
