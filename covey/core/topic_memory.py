"""The memory of one topic (COVEY_MEMORY=1, docs/butler.md 记忆): what the owner said in this topic that holds for the
topic and nowhere wider ("这个不要问了"), in ``<state>/topics/<id>/memory.json``, the file format of a project's memory
(core/project_memory.py: {id, text, kind, source, at, deleted}). Only the Bridge writes it
(app/memory_collect.py after the turn, as the owner's word, source ``owner``; core/memstore.py for the memory center), at
most ``MAX_ENTRIES`` live ones (past it the oldest becomes a tombstone and the change is logged, T908 M3b). A delete keeps
the entry as a tombstone. Every write takes the file's shared lock (core/locks.py), lists the topic among those with memory and touches the memory's sentinel
(core/memstore.py note_topic, touch_rev); a read takes none. A file that cannot be parsed is read as empty and set aside
before the next write. The fields an older release does not know lie beside it in ``memory-v2.json`` (core/memstore.py).
Standard library and core only."""
from __future__ import annotations

import time
from pathlib import Path

from covey.core import memstore, project_memory
from covey.core.locks import shared_file_lock
from covey.core.state import write_json
from covey.core.topics import topic_file

FILE = "memory.json"
MAX_ENTRIES = memstore.TOPIC_MAX


def memory_path(state_dir, topic_id) -> Path:
    return topic_file(Path(state_dir), str(topic_id)).with_name(FILE)


def live(state_dir, topic_id) -> list:
    """The topic's entries not deleted, the oldest first; [] without a topic or a file."""
    if not topic_id:
        return []
    return [row for row in project_memory.rows_at(memory_path(state_dir, topic_id)) if not row["deleted"]]


def _write(path, rows) -> None:
    write_json(path, {"schema_version": project_memory.SCHEMA, "entries": rows}, ensure_ascii=False, indent=1)


def _changed(state_dir, topic_id, now=None) -> None:
    """Every write: the topic listed among those with memory (core/memstore.py topics) and the memory's sentinel touched."""
    memstore.note_topic(state_dir, topic_id, now=now)
    memstore.touch_rev(state_dir, now)


def add(state_dir, topic_id, text, *, source="owner", now=None, kind="constraint") -> tuple:
    """(the entry, whether it is new): ``text`` as a line of the topic (a constraint unless ``kind`` says otherwise: a note
    of core/memstore.py is a fact). The same text still there is the one returned (core/memstore.py topic_add)."""
    return memstore.topic_add(state_dir, topic_id, text, source=source, now=now, kind=kind)


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
    _changed(state_dir, topic_id, now)
    return True


def restore(state_dir, topic_id, entry_id) -> bool:
    """Whether the tombstone ``entry_id`` of the topic is a live entry again (app/memory_collect.py take_back: a line the
    learner put out of the way because the owner contradicted it, and he said 不对); False for one that is not a tombstone."""
    path = memory_path(state_dir, topic_id)
    with shared_file_lock(path):
        rows = project_memory.rows_at(path, writing=True)
        row = next((row for row in rows if row["id"] == str(entry_id) and row["deleted"]), None)
        if row is None:
            return False
        row["deleted"] = 0
        _write(path, rows)
    _changed(state_dir, topic_id)
    return True


def rewrite(state_dir, topic_id, entry_id, *, text=None, kind=None, deleted=None) -> dict | None:
    """The Bridge's own change of the line ``entry_id`` (core/memstore.py topic_rewrite): new words, a kind, or ``deleted``."""
    return memstore.topic_rewrite(state_dir, topic_id, entry_id, text=text, kind=kind, deleted=deleted)
