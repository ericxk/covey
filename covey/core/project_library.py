"""The library of a project (docs/projects.md 专项记忆与资料库, COVEY_PROJECT_MEMORY=1): the files the owner gave the project and a line
for what its topics delivered, in ``<state>/projects/<id>/library/`` (the files) and ``library.json`` (the index).

An entry is {id, name, kind, ref, sha256, size, source, topic_id, at, purged} and, when it says, ``redacted``. ``kind`` is
``file`` (a copy in library/, ``ref`` its name there), ``path`` or ``link`` (an artifact of a topic: only where it is, nothing is
copied) or ``delivery`` (a checkpoint's delivery in one line, ``ref`` the line). ``source`` is ``owner_file`` (the owner sent it
to the lead), ``artifact``, ``delivery`` or ``result`` (a topic's turn; ``result`` a member turn's whole reply, C7). ``redacted``
(true): it came from a topic with redaction on, so only a reader with redaction on lists it (``index_lines``). ``purged`` is
the time the maintenance rule removed a file after the project was done: the entry, with its hash, stays.

Limits: one file up to ``FILE_LIMIT`` is copied (a larger one is only noted by its path), ``PROJECT_LIMIT`` bytes of files in
all, ``MAX_ENTRIES`` entries (the oldest of the topics' go with their files first, the owner's files last). ``index_lines`` is what the lead's pack and a topic's first
turn read: at most ``INDEX_BYTES`` bytes, at most ``RESULT_LINES`` of them the topics' whole replies, each topic named by its number,
a delivery's fields cut one by one (``line_of``).

Every write runs under the project record's lock (core/projects.py ``update``), the copy itself before it, so no lock is held for
a long copy. Standard library and core only."""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import sys
import time
from pathlib import Path

from covey.core import projects
from covey.core.checkpoint import DELIVERY_LABELS, delivery_line
from covey.core.text import stamp
from covey.core.state import read_json, write_json
from covey.core.topics import topic_label

FILE = "library.json"
FOLDER = "library"
SCHEMA = 1
KINDS = ("file", "path", "link", "delivery")
SOURCES = {"owner_file": "主人发的", "artifact": "话题的产物", "delivery": "话题的交付", "result": "话题的结论"}
MAX_ENTRIES = 100
INDEX_BYTES = 2048
FILE_LIMIT = 25 * 1024 * 1024
PROJECT_LIMIT = 200 * 1024 * 1024
NAME_CHARS = 80
REF_CHARS = 300
LINE_CHARS = 160
DELIVERY_LINE_CHARS = LINE_CHARS + 40  # Its fields are cut one by one (core/checkpoint.py delivery_line): this only bounds an old line.
RESULT_LINES = 3  # Of the topics' whole replies in the index, the newest: one a member turn, they would push out the owner's files.


def library_dir(state_dir, project_id) -> Path:
    return projects.folder(state_dir) / project_id / FOLDER


def index_path(state_dir, project_id) -> Path:
    return projects.folder(state_dir) / project_id / FILE


def _entry(row):
    if not isinstance(row, dict) or row.get("kind") not in KINDS or not str(row.get("ref") or "").strip():
        return None
    at, size, purged = row.get("at"), row.get("size"), row.get("purged")
    return {"id": str(row.get("id") or ""), "name": str(row.get("name") or "")[:NAME_CHARS], "kind": row["kind"],
            "ref": str(row["ref"])[:REF_CHARS], "sha256": str(row.get("sha256") or ""),
            "size": size if type(size) is int and size >= 0 else 0, "source": str(row.get("source") or "")[:20],
            "topic_id": str(row.get("topic_id") or "")[:64], "at": at if isinstance(at, (int, float)) else 0,
            "purged": purged if isinstance(purged, (int, float)) and not isinstance(purged, bool) and purged else 0,
            **({"redacted": row["redacted"]} if isinstance(row.get("redacted"), bool) else {})}


def _rows(state_dir, project_id, *, writing=False) -> list:
    """The index's entries, the oldest first. A read that fails is [] (a reader shows nothing); with ``writing`` an index that cannot
    be read raises its OSError (the write fails, the entries stay), and one that is there but cannot be parsed is moved aside
    (``library.json.bad-<time>``, a line in the log) and read as empty."""
    path = index_path(state_dir, project_id)
    try:
        data = read_json(path)
        rows = data.get("entries") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise ValueError("not a library index")
    except FileNotFoundError:
        return []
    except ValueError:
        if writing:
            aside = path.with_name(f"{path.name}.bad-{time.strftime('%Y%m%d-%H%M%S')}")
            os.replace(path, aside)
            print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} project file unreadable, kept as {aside.name}", file=sys.stderr, flush=True)
        return []
    except OSError:
        if writing:
            raise
        return []
    found = [row for row in map(_entry, rows) if row and row["id"]]
    return sorted(found, key=lambda row: row["at"])


def entries(state_dir, project_id) -> list:
    """The index's entries, the oldest first; [] when there is none or it cannot be read."""
    return _rows(state_dir, project_id)


def _write(state_dir, project_id, rows) -> None:
    write_json(index_path(state_dir, project_id), {"schema_version": SCHEMA, "entries": rows}, ensure_ascii=False, indent=1)


def _first_out(rows) -> int:
    """The index in ``rows`` of the entry to drop first: the oldest of the topics' (artifacts, deliveries); the owner's files
    only when nothing else is left."""
    return next((i for i, row in enumerate(rows) if row["source"] != "owner_file"), 0)


def _edit(state_dir, project_id, edit):
    box = []

    def change(record):
        rows = _rows(state_dir, project_id, writing=True)
        before = [dict(row) for row in rows]
        box.append(edit(rows))
        rows.sort(key=lambda row: row["at"])
        while len(rows) > MAX_ENTRIES:
            _unlink(state_dir, project_id, rows.pop(_first_out(rows)))
        if rows != before:
            _write(state_dir, project_id, rows)
    projects.update(state_dir, project_id, change)  # KeyError for a project with no record; the record itself is not written.
    return box[0]


def _unlink(state_dir, project_id, row) -> None:
    if row["kind"] == "file" and not row["purged"]:
        try:
            (library_dir(state_dir, project_id) / row["ref"]).unlink()
        except OSError:
            pass


def used_bytes(state_dir, project_id, committed=False) -> int:
    """The bytes of the files in library/ (one level); with ``committed`` the copies still being made (``.incoming-*``) left out."""
    total = 0
    try:
        with os.scandir(library_dir(state_dir, project_id)) as found:
            for item in found:
                if item.is_file(follow_symlinks=False) and not (committed and item.name.startswith(".incoming-")):
                    total += item.stat(follow_symlinks=False).st_size
    except OSError:
        return total
    return total


def safe_name(name) -> str:
    """``name`` as one plain file name: no folder, no leading dot, at most NAME_CHARS."""
    name = str(name or "").replace("\\", "/").rsplit("/", 1)[-1].strip().lstrip(".")
    name = "".join(ch for ch in name if ch.isprintable() and ch not in '<>:"|?*')
    return name[:NAME_CHARS].strip() or "file"


def digest(path) -> tuple:
    """(sha256 hex, size) of the file at ``path``."""
    sha, size = hashlib.sha256(), 0
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            sha.update(block)
            size += len(block)
    return sha.hexdigest(), size


def _new(kind, name, ref, source, topic_id, now, **more) -> dict:
    return {"id": secrets.token_hex(3), "name": safe_name(name) if kind == "file" else str(name or "")[:NAME_CHARS],
            "kind": kind, "ref": str(ref)[:REF_CHARS], "sha256": "", "size": 0, "source": source,
            "topic_id": str(topic_id or ""), "at": time.time() if now is None else now, "purged": 0, **more}


def add_file(state_dir, project_id, source_path, *, name="", source="owner_file", topic_id="", now=None) -> tuple:
    """(the entry, whether it is new): the file at ``source_path`` copied into library/ under its own name (another file of that
    name already there: the name with the hash's first 8 digits), its sha256 noted. The same content twice is one entry. A file
    over FILE_LIMIT, or one that would take the library over PROJECT_LIMIT, is not copied: it is noted as a ``path`` entry
    (where it is). ValueError for a source that is not a readable file."""
    src = Path(source_path)
    if not src.is_file() or src.is_symlink():
        raise ValueError("资料不是一个文件")
    label = safe_name(name or src.name)
    folder = library_dir(state_dir, project_id)
    if src.stat().st_size > FILE_LIMIT:  # Not read: only where it is.
        sha, size = digest(src)
        return add_ref(state_dir, project_id, "path", str(src), name=label, source=source, topic_id=topic_id, now=now, sha256=sha, size=size)
    folder.mkdir(parents=True, exist_ok=True)
    partial = folder / f".incoming-{secrets.token_hex(4)}"
    # The copy is what is judged and hashed: the source may change while it is read.
    sha, size = hashlib.sha256(), 0
    try:
        with open(src, "rb") as reader, open(partial, "wb") as writer:
            for block in iter(lambda: reader.read(1 << 20), b""):
                size += len(block)
                if size > FILE_LIMIT:
                    break
                sha.update(block)
                writer.write(block)
        sha = sha.hexdigest()
        if size > FILE_LIMIT:
            partial.unlink(missing_ok=True)
            return add_ref(state_dir, project_id, "path", str(src), name=label, source=source, topic_id=topic_id, now=now)

        def edit(rows):
            same = next((row for row in rows if row["sha256"] == sha and row["kind"] == "file" and not row["purged"]), None)
            if same is not None:
                return same, False
            if used_bytes(state_dir, project_id, committed=True) + size > PROJECT_LIMIT:  # Judged under the lock, on the copy's size.
                wanted = _new("path", label, str(src), source, topic_id, now, sha256=sha, size=size)
                same = next((row for row in rows if row["kind"] == "path" and row["ref"] == wanted["ref"]
                             and row["topic_id"] == wanted["topic_id"]), None)
                if same is not None:
                    return same, False
                rows.append(wanted)
                return wanted, True
            target = folder / label
            if target.exists():
                target = folder / f"{Path(label).stem[:60]}-{sha[:8]}{Path(label).suffix[:20]}"
            os.replace(partial, target)
            wanted = _new("file", label, target.name, source, topic_id, now, sha256=sha, size=size)
            rows.append(wanted)
            return wanted, True
        return _edit(state_dir, project_id, edit)
    finally:
        partial.unlink(missing_ok=True)  # Still there: the copy was not kept (not new, over the limit, no project, an error).


def add_ref(state_dir, project_id, kind, ref, *, name="", source="artifact", topic_id="", now=None, **more) -> tuple:
    """(the entry, whether it is new) of a ``path``, ``link`` or ``delivery`` that is only noted. The same kind and ``ref`` of the
    same topic is one entry."""
    if kind not in KINDS or kind == "file":
        raise ValueError(f"kind {kind!r} is not a reference")
    ref = " ".join(str(ref or "").split())[:REF_CHARS]
    if not ref:
        raise ValueError("ref 不能为空")
    wanted = _new(kind, name or Path(ref).name or ref, ref, source, topic_id, now, **more)

    def edit(rows):
        same = next((row for row in rows if row["kind"] == kind and row["ref"] == ref and row["topic_id"] == wanted["topic_id"]), None)
        if same is not None:
            return same, False
        rows.append(wanted)
        return wanted, True
    return _edit(state_dir, project_id, edit)


def delete(state_dir, project_id, entry_id) -> bool:
    """Whether ``entry_id`` was an entry: it and its file (a ``file`` entry's copy) are gone."""
    def edit(rows):
        row = next((row for row in rows if row["id"] == str(entry_id)), None)
        if row is None:
            return False
        rows.remove(row)
        _unlink(state_dir, project_id, row)
        return True
    return _edit(state_dir, project_id, edit)


def purge_files(state_dir, project_id, now=None) -> int:
    """The files of the library removed, their entries kept (``purged``: when): the maintenance rule of a project that is done.
    How many files went. Files in library/ that no entry names go too."""
    stamp = time.time() if now is None else now

    def edit(rows):
        count = 0
        for row in rows:
            if row["kind"] == "file" and not row["purged"]:
                _unlink(state_dir, project_id, row)
                row["purged"] = stamp
                count += 1
        named = {row["ref"] for row in rows if row["kind"] == "file"}
        try:
            for item in os.scandir(library_dir(state_dir, project_id)):
                if item.is_file(follow_symlinks=False) and item.name not in named:
                    Path(item.path).unlink(missing_ok=True)
                    count += 1
        except OSError:
            pass
        return count
    try:
        return _edit(state_dir, project_id, edit)
    except KeyError:
        return 0


def row_label(row) -> str:
    """One entry in a few words for the project card's 资料库 fold."""
    source = SOURCES.get(row["source"], "")
    if row["kind"] == "delivery":
        return "交付：" + row["ref"]
    return row["name"] + (f"（{source}）" if source else "") + ("（文件已清理）" if row["purged"] else "")


def trim(state_dir, project_id, limit=None, now=None) -> int:
    """The oldest files removed, the topics' before the owner's (their entries kept, ``purged``) until the library's files are at most ``limit`` bytes: how
    many went. The maintenance rule behind PROJECT_LIMIT, for a library that grew by hand."""
    stamp = time.time() if now is None else now
    limit = PROJECT_LIMIT if limit is None else limit

    def edit(rows):
        count = 0
        for row in sorted(rows, key=lambda each: each["source"] == "owner_file"):  # The topics' files first, then the owner's (oldest first in each).
            if used_bytes(state_dir, project_id) <= limit:
                break
            if row["kind"] == "file" and not row["purged"]:
                _unlink(state_dir, project_id, row)
                row["purged"] = stamp
                count += 1
        return count
    try:
        return _edit(state_dir, project_id, edit)
    except KeyError:
        return 0


def delivery_words(ref) -> str:
    """A delivery's line as ``turn_to_library`` wrote it (app/project_context.py: "提交 …；部署 …；验收 …"), each field cut
    alone (core/checkpoint.py delivery_line); a line not of those fields as it is."""
    fields = {label: name for name, label in DELIVERY_LABELS}
    found = {}
    for part in re.split("；(?=(?:" + "|".join(fields) + ") )", str(ref or "")):
        label, _, value = part.partition(" ")
        if label not in fields or fields[label] in found:
            return str(ref or "")
        found[fields[label]] = value
    return delivery_line(found)


def line_of(state_dir, project_id, row, label="") -> str:
    """One entry in a line (no leading dash) for the lead's pack and a topic's first turn: what it is, where, whose (its
    topic's number, ``label``, unless its name begins with it), when. A delivery's fields are cut one by one
    (``delivery_words``: 7 digits of at most 3 commits and how many more, the deployment 40 characters, the check 60)."""
    source = SOURCES.get(row["source"], row["source"] or "资料")
    label = "" if label and row["name"].startswith(label + " ") else label
    when = f"{source}，{stamp(row['at'], '%m-%d')}" + (f"，{label}" if label else "")
    if row["kind"] == "delivery":
        return f"交付（{when}）：{delivery_words(row['ref'])}"[:DELIVERY_LINE_CHARS]
    if row["kind"] == "file":
        where = "（文件已清理）" if row["purged"] else f"：{library_dir(state_dir, project_id) / row['ref']}"
        return f"{row['name']}（{when}）{where}"[:LINE_CHARS + 120]
    return f"{row['name']}（{when}）：{row['ref']}"[:LINE_CHARS + 120]


def index_lines(state_dir, project_id, limit=INDEX_BYTES, labels=None, *, redacted_reader=False) -> list:
    """The index in lines, the newest first, the lines together at most ``limit`` bytes, at most RESULT_LINES of them whole
    replies (``result``); a last line says how many did not fit. ``labels``: {topic id: its short label} for a line's topic
    (None: its number, T12, core/topics.py topic_label). ``redacted_reader``: whether the reader is a topic with redaction
    on; an entry from such a topic (``redacted``) is for it alone, left out (and not counted) for any other reader."""
    rows = [row for row in reversed(entries(state_dir, project_id)) if redacted_reader or not row.get("redacted")]
    older = {row["id"] for row in [row for row in rows if row["source"] == "result"][RESULT_LINES:]}
    listed = [row for row in rows if row["id"] not in older]
    if labels is None:
        labels = {topic: topic_label(Path(state_dir), topic) or topic[:8] for topic in {row["topic_id"] for row in listed} if topic}
    lines, used = [], 0
    for row in listed:
        line = line_of(state_dir, project_id, row, labels.get(row["topic_id"], ""))
        size = len(line.encode()) + 3  # "- " and the line break the caller adds
        if used + size > limit - 60:
            break
        lines.append(line)
        used += size
    if len(lines) < len(rows):
        lines.append(f"另有 {len(rows) - len(lines)} 条较早的未列出")
    return lines
