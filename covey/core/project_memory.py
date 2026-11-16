"""The memory of a project (docs/butler.md 专项记忆): what the owner decided and what holds for the project for good, in
``<state>/projects/<id>/memory.json``. Entries are {id, text, kind, source, at, deleted}: ``kind`` one of ``KINDS``,
``source`` who wrote it (``lead``, ``owner_answer``, ...), ``deleted`` 0 or the time of a delete. A delete writes a
tombstone and keeps the text (the lead's pack lists the last 30 days of them, so it does not note again what the owner
struck out); tombstones go after ``TOMB_DAYS``.

Limits: a text of at most ``TEXT`` characters, ``MAX_ENTRIES`` live entries, ``MAX_BYTES`` for the file; past one the
oldest tombstone goes first, then the oldest entry that is not a style line.

Older releases kept the entries in the project record's ``memory`` field (what the card never showed). They are still read
(``entries`` merges both); a write moves them into the file and leaves the field out of the record, and nothing writes the
field again. Every write runs under the record's own lock (core/projects.py ``update``), so the lead's tools, the owner's
click on 删 and the Bridge's own notes never overwrite one another; a read takes no lock (the file is replaced whole).

Standard library and core only: the MCP server imports core/projects.py, which this does."""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from pathlib import Path

from covey.core import projects
from covey.core.state import read_json, write_json

KINDS = ("decision", "constraint", "contact", "fact", "style")
LEGACY_KINDS = {"practice": "constraint", "pitfall": "constraint"}  # What the lead's tool called two of them before.
TEXT = 200
MAX_ENTRIES = 40
MAX_BYTES = 6 * 1024
MAX_TOMBSTONES = 20
TOMB_DAYS = 30
FILE = "memory.json"
SCHEMA = 1


def memory_path(state_dir, project_id) -> Path:
    return projects.folder(state_dir) / project_id / FILE


def kind_of(kind) -> str:
    """``kind`` as one of ``KINDS`` (the two older names mapped, anything unknown a fact)."""
    kind = LEGACY_KINDS.get(kind, kind)
    return kind if kind in KINDS else "fact"


def clean(text) -> str:
    return " ".join(str(text or "").split())[:TEXT]


def _entry(row, index=0):
    """``row`` of a file or of the record's list as an entry; None for one with no text."""
    if isinstance(row, str):
        text = clean(row)
        if not text:
            return None
        return {"id": "s" + hashlib.sha1(text.encode()).hexdigest()[:6], "text": text, "kind": "fact", "source": "owner",
                "at": 0, "deleted": 0}
    if not isinstance(row, dict) or not clean(row.get("text")):
        return None
    at, deleted = row.get("at"), row.get("deleted")
    return {"id": str(row.get("id") or f"i{index}"), "text": clean(row.get("text")), "kind": kind_of(row.get("kind")),
            "source": str(row.get("source") or "lead")[:30], "at": at if isinstance(at, (int, float)) else 0,
            "deleted": deleted if isinstance(deleted, (int, float)) and not isinstance(deleted, bool) and deleted else 0}


def _file_rows(path) -> list:
    try:
        data = read_json(path)
    except (OSError, ValueError):
        return []
    rows = data.get("entries") if isinstance(data, dict) else data
    return [item for item in (_entry(row, index) for index, row in enumerate(rows if isinstance(rows, list) else ())) if item]


def stored(state_dir, project_id) -> list:
    """The entries of the file alone (the maintenance rule of the memory's age reads them), the oldest first."""
    return _merge(_file_rows(memory_path(state_dir, project_id)), [])


def _legacy_rows(record) -> list:
    rows = record.get("memory") if isinstance(record, dict) else None
    return [item for item in (_entry(row, index) for index, row in enumerate(rows if isinstance(rows, list) else ())) if item]


def _merge(file_rows, legacy_rows) -> list:
    seen = {row["id"] for row in file_rows}
    merged = [*file_rows, *(row for row in legacy_rows if row["id"] not in seen)]
    return sorted(merged, key=lambda row: row["at"])  # Stable: entries of one time keep the order they came in.


def entries(state_dir, record) -> list:
    """Every entry of the project of ``record`` (live ones and tombstones), the oldest first: the file's and, until a write
    moves them, the record's own."""
    pid = record.get("id") if isinstance(record, dict) else None
    if not projects.valid_id(pid):
        return []
    return _merge(_file_rows(memory_path(state_dir, pid)), _legacy_rows(record))


def live(state_dir, record, *, style=True) -> list:
    """The entries not deleted (the style line left out with ``style=False``), the oldest first."""
    return [row for row in entries(state_dir, record) if not row["deleted"] and (style or row["kind"] != "style")]


def tombstones(state_dir, record, now=None, days=TOMB_DAYS) -> list:
    """The entries the owner or the lead deleted in the last ``days`` days, the oldest first."""
    cutoff = (time.time() if now is None else now) - days * 86400
    return [row for row in entries(state_dir, record) if row["deleted"] and row["deleted"] >= cutoff]


def dumps(rows) -> str:
    return json.dumps({"schema_version": SCHEMA, "entries": rows}, ensure_ascii=False, indent=1)


def _fit(rows, now) -> list:
    """``rows`` inside the limits: tombstones past ``TOMB_DAYS`` gone, then the oldest ones past ``MAX_TOMBSTONES``, then the
    oldest tombstone and the oldest non-style entry until the count and the size fit."""
    cutoff = now - TOMB_DAYS * 86400
    rows = [row for row in rows if not row["deleted"] or row["deleted"] >= cutoff]
    while sum(1 for row in rows if row["deleted"]) > MAX_TOMBSTONES:
        rows.remove(next(row for row in rows if row["deleted"]))
    while sum(1 for row in rows if not row["deleted"]) > MAX_ENTRIES or len(dumps(rows).encode()) > MAX_BYTES:
        victim = (next((row for row in rows if row["deleted"] and row["kind"] != "style"), None)
                  or next((row for row in rows if row["kind"] != "style"), None))
        if victim is None:
            break
        rows.remove(victim)
    return rows


def _edit(state_dir, project_id, edit, also=None, now=None):
    """Run ``edit(rows)`` (it changes the merged list in place and returns what the caller wants) under the record's lock,
    write the file when it differs, take the legacy field out of the record, and run ``also(record)`` on the record in the
    same write. KeyError for a project with no record."""
    stamp = time.time() if now is None else now
    box = []

    def change(record):
        path = memory_path(state_dir, project_id)
        before = _merge(_file_rows(path), [])
        rows = _merge([dict(row) for row in before], _legacy_rows(record))
        box.append(edit(rows))
        rows = _fit(rows, stamp)
        if rows != before:
            write_json(path, {"schema_version": SCHEMA, "entries": rows}, ensure_ascii=False, indent=1)
        record.pop("memory", None)
        if also is not None:
            also(record)
    projects.update(state_dir, project_id, change)
    return box[0]


def add(state_dir, project_id, text, kind="fact", source="lead", *, now=None, also=None) -> tuple:
    """(the entry, whether it is new): ``text`` as an entry of ``kind``. The same text of the same kind that is still
    there is the one returned, not noted twice. ``also(record)``: more to change in the project's record in the same write."""
    stamp = time.time() if now is None else now
    wanted = {"id": secrets.token_hex(3), "text": clean(text), "kind": kind_of(kind), "source": str(source or "lead")[:30],
              "at": stamp, "deleted": 0}
    if not wanted["text"]:
        raise ValueError("text 不能为空")

    def edit(rows):
        same = next((row for row in rows if not row["deleted"] and row["kind"] == wanted["kind"] and row["text"] == wanted["text"]), None)
        if same is not None:
            return same, False
        rows.append(wanted)
        return wanted, True
    return _edit(state_dir, project_id, edit, also, stamp)


def set_style(state_dir, project_id, text, *, now=None, also=None) -> dict:
    """The one style line of the project: ``text`` replaces the one before (the last word the owner gave)."""
    stamp = time.time() if now is None else now
    wanted = {"id": secrets.token_hex(3), "text": clean(text), "kind": "style", "source": "lead", "at": stamp, "deleted": 0}

    def edit(rows):
        rows[:] = [row for row in rows if row["kind"] != "style"]
        rows.append(wanted)
        return wanted
    return _edit(state_dir, project_id, edit, also, stamp)


def delete(state_dir, project_id, entry_id, *, now=None) -> bool:
    """Whether ``entry_id`` was a live entry: it becomes a tombstone (``deleted`` is the time)."""
    stamp = time.time() if now is None else now

    def edit(rows):
        row = next((row for row in rows if row["id"] == str(entry_id) and not row["deleted"]), None)
        if row is None:
            return False
        row["deleted"] = stamp
        return True
    return _edit(state_dir, project_id, edit, None, stamp)


def forget(state_dir, project_id, before) -> int:
    """How many entries (tombstones too) written before ``before`` (an epoch; one with no time is kept) the file lost: the
    maintenance rule of the memory's age. Style lines stay."""
    def edit(rows):
        gone = [row for row in rows if row["kind"] != "style" and 0 < row["at"] < before]
        rows[:] = [row for row in rows if row not in gone]
        return len(gone)
    try:
        return _edit(state_dir, project_id, edit)
    except KeyError:
        return 0
