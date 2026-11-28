"""The one memory store (docs/operations.md 记忆的存储与迁移): every entry, whatever file holds it, as one ``Entry`` with a short code.

Where each part lies (docs/operations.md 状态目录):

- 全局: ``memory/global.json`` ({version, entries}), written whole under its file lock. The principles
  (``principles.json``) and the knowledge entries (``knowledge/``) of older releases are taken into it by the
  migration (core/migrate.py ``memory_v2``), each with its old ref among its ``aliases``; the old files stay as they
  are and nothing here writes them (``export_legacy`` does, on request, for a rollback).
- 专项: the lines of ``projects/<id>/memory.json`` (core/project_memory.py: the file every release reads and writes)
  and, beside it, ``projects/<id>/memory-v2.json`` (``sidecar_path``): what an older release does not know (the title,
  the kind of the three, who said it, the repos…), keyed by the line's id. An older release rewrites memory.json and
  memory-meta.json with its own keys only, so nothing new goes into them: rolling back loses no field.
- 话题: ``topics/<id>/memory.json`` (core/topic_memory.py) and its ``memory-v2.json`` the same way.
- 决定记录: the owner's answers. Those an older release noted are the ``owner_answer`` lines of a project's memory.json;
  new ones are appended to the project's or the topic's ``decisions.jsonl`` (an ``add`` event, then ``set`` events).
- Short codes (``CODE_RE``): ``memory/codes.json`` holds every code given and the ref it names (and the old refs, the
  aliases, of the entries that have them). A line of memory.json gets its code the first time something reads it (lazy:
  nothing walks every topic directory), from a hash of its ref, longer only when that one is taken.
- ``memory/review.json`` (待审核), ``memory/log.jsonl`` (every change, with what it was before and an ``undo_token``),
  ``memory/topics.json`` (the topics that have memory, so the readers of every topic need no walk) and ``memory/usage.json``
  (core/memory_usage.py) lie beside them; ``memory/imports.json``, what an older release offered of the Harnesses' own
  memory, is left as it is and read by nothing.

Every write touches the one sentinel ``memory/.rev`` (``touch_rev``), so the web sync stats a single file. Reads take
no lock; a write takes the lock of the file it writes (core/locks.py shared_file_lock), never the global ``LOCK``.
Standard library and core only.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from covey.core import knowledge, memory_rev, principles, project_memory, projects, turn_source
from covey.core.locks import shared_file_lock
from covey.core.state import epoch_seconds, read_json, read_object, write_json, write_private_text
from covey.core.state import read_for_write as state_read_for_write

KINDS = ("rule", "decision", "note")
SCOPES = ("global", "project", "topic")
STATUSES = ("active", "pending", "done", "archived", "void")
GONE = ("archived", "void")  # An entry no longer in force that is not a decision done: what 待审核 about it closes on.
SAID = ("owner", "summary")  # 你定的 (the owner's words or confirmed by him) / 别人总结的.
BY = ("owner", "lead", "butler", "learn", "import", "bridge", "web")

TITLE_CHARS = 40
TEXT_CHARS = 200  # A rule's and a note's.
DECISION_CHARS = 600  # A decision's: the question and the answer as given.

CODE_LETTERS = {"rule": "R", "decision": "D", "note": "N"}
CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32: no I, L, O, U.
CODE_RE = re.compile(r"^[RDN][0-9A-HJKMNP-TV-Z]{4,6}$")
# What the turns and the tools wrote before: a principle's p + 6 hex, a knowledge entry's k + 6 hex, a memory number M12.
LEGACY_RE = re.compile(r"^(?:p[0-9a-f]{6}|k[0-9a-f]{6}|M\d{2,3})$")

MEMORY_DIR = memory_rev.MEMORY_DIR  # "memory"
GLOBAL_FILE = "global.json"
REVIEW_FILE = "review.json"
LOG_FILE = "log.jsonl"
USAGE_FILE = "usage.json"
SYNONYMS_FILE = "synonyms.json"
REV_FILE = memory_rev.REV_FILE  # ".rev"
SIDECAR_FILE = "memory-v2.json"  # Beside a project's or a topic's memory.json.
DECISIONS_FILE = "decisions.jsonl"
CODES_FILE = "codes.json"
TOPICS_FILE = "topics.json"

VERSION = 1
REVIEW_STATES = ("open", "accepted", "rejected", "expired")
REVIEW_DAYS = 14  # An item of 待审核 left open this long is closed as expired (review_expire, the daily upkeep).
REVIEW_HELD = ("open", "accepted", "rejected")  # An item in these keeps the same proposal out (review_put); an expired one does not.
# The types of 待审核 (core/memory_review.py TYPES): learn (推断全局 and 放行类, T908 M3b), and the one-time sources, a rollback's
# conflict and an older release's workspace line (migrate). An open item of a type older releases put there (a lead's rule, an
# evening candidate, a Harness's import, a tidy proposal, the third same-class question) reads as expired: kept, never written.
REVIEW_TYPES = ("learn", "conflict", "migrate")
LOG_DAYS = 90  # Of memory/log.jsonl (core/maintenance_rules.py MemoryLog).
VOID_DAYS = 30  # A void decision's events go from decisions.jsonl after this (core/maintenance_rules.py Decisions).
_SNAP = ("title", "text", "why", "kind", "said", "status", "superseded_by", "repos")
_ID = re.compile(r"^[0-9A-Za-z][0-9A-Za-z_\-]{0,80}$")
_SENTENCE = re.compile(r"[。！？?；\n]")  # Not the half-width ! and ; (``!248``, ``a; b``: inside a sentence).
TITLE_MIN = 8  # A first sentence shorter than this (主人定（11-23）) takes the next one too.


@dataclass(frozen=True)
class Entry:
    """One memory entry, whatever store it came from."""

    ref: str  # global:<id> / project:<pid>:<id> / topic:<tid>:<id> / decision:<pid|tid>:<id>
    code: str  # CODE_RE; given once, kept.
    kind: str  # KINDS
    scope: str  # SCOPES
    owner_key: str  # The project's id, the topic's id, '' for global.
    title: str  # ≤ TITLE_CHARS
    text: str  # ≤ TEXT_CHARS, a decision's ≤ DECISION_CHARS
    status: str  # STATUSES
    said: str  # SAID
    by: str  # BY
    origin: dict = field(default_factory=dict)  # {at, topic_key?, topic_number?, job?, message_id?, question?, quote?, file?, harness?}
    why: str = ""
    repos: tuple = ()  # A note's: the repo map names it is about.
    device: str = ""
    verified_at: float = 0.0  # A fact's; 0 not verified.
    superseded_by: str = ""
    aliases: tuple = ()  # The refs it had before (principle:p…, knowledge:k…, candidate:c…).
    at: float = 0.0
    edited_at: float = 0.0


# ---------------------------------------------------------------------------------------------------------------- paths


def memory_dir(state_dir) -> Path:
    return Path(state_dir) / MEMORY_DIR


def global_path(state_dir) -> Path:
    return memory_dir(state_dir) / GLOBAL_FILE


def review_path(state_dir) -> Path:
    return memory_dir(state_dir) / REVIEW_FILE


def log_path(state_dir) -> Path:
    return memory_dir(state_dir) / LOG_FILE


def usage_path(state_dir) -> Path:
    return memory_dir(state_dir) / USAGE_FILE


def rev_path(state_dir) -> Path:
    return memory_dir(state_dir) / REV_FILE


def codes_path(state_dir) -> Path:
    return memory_dir(state_dir) / CODES_FILE


def topics_path(state_dir) -> Path:
    return memory_dir(state_dir) / TOPICS_FILE


def _owner_dir(state_dir, scope: str, owner_key: str) -> Path:
    if scope not in ("project", "topic"):
        raise ValueError(f"no sidecar for scope {scope!r}")
    return Path(state_dir) / ("projects" if scope == "project" else "topics") / owner_key


def sidecar_path(state_dir, scope: str, owner_key: str) -> Path:
    """``projects/<id>/memory-v2.json`` or ``topics/<id>/memory-v2.json``: the new fields an older release does not know."""
    return _owner_dir(state_dir, scope, owner_key) / SIDECAR_FILE


def decisions_path(state_dir, scope: str, owner_key: str) -> Path:
    """A project's or a topic's ``decisions.jsonl`` (append only): the owner's answers, out of memory.json's 120 lines."""
    if scope not in ("project", "topic"):
        raise ValueError(f"no decisions for scope {scope!r}")
    return _owner_dir(state_dir, scope, owner_key) / DECISIONS_FILE


def topic_memory_path(state_dir, topic_id) -> Path:
    """Where a topic's lines are (core/topic_memory.py, which imports this module, writes them through ``topic_add``)."""
    return Path(state_dir) / "topics" / str(topic_id) / project_memory.FILE


TOPIC_MAX = 20  # A topic's live lines (core/topic_memory.py MAX_ENTRIES): past it the oldest becomes a tombstone.


def _topic_write(path, rows) -> None:
    write_json(path, {"schema_version": project_memory.SCHEMA, "entries": rows}, ensure_ascii=False, indent=1)


def topic_add(state_dir, topic_id, text, *, source="owner", now=None, kind="constraint") -> tuple:
    """(the line, whether it is new): ``text`` as a line of the topic's memory.json in the format every release reads
    (core/topic_memory.py add). The same text still there is the one returned; past ``TOPIC_MAX`` live lines the oldest becomes
    a tombstone (its words kept, logged as a void the 记录 shows and an undo puts back; T908 M3b), never deleted."""
    stamp = time.time() if now is None else now
    wanted = {"id": secrets.token_hex(3), "text": project_memory.clean(text), "kind": project_memory.kind_of(kind),
              "source": str(source)[:30], "at": stamp, "deleted": 0}
    if not wanted["text"]:
        raise ValueError("text 不能为空")
    path = topic_memory_path(state_dir, topic_id)
    with shared_file_lock(path):
        rows = project_memory.rows_at(path, writing=True)
        same = next((row for row in rows if not row["deleted"] and row["text"] == wanted["text"]), None)
        if same is not None:
            return same, False
        rows.append(wanted)
        gone = []
        while sum(1 for row in rows if not row["deleted"]) > TOPIC_MAX:
            oldest = next(row for row in rows if not row["deleted"])
            oldest["deleted"] = stamp
            gone.append(oldest["id"])
        _topic_write(path, rows)
    note_topic(state_dir, topic_id, now=stamp)
    for line in gone:
        _crowded_out(state_dir, f"topic:{topic_id}:{line}", stamp)
    touch_rev(state_dir, stamp)
    return wanted, True


def _crowded_out(state_dir, ref, now) -> None:
    """The topic's line ``ref`` that a new one crowded out (``TOPIC_MAX``): its void logged, 待审核 about it closed."""
    try:
        entry = find(state_dir, ref)
    except (OSError, ValueError):
        entry = None
    if entry is None:
        return
    token = log_change(state_dir, who="bridge", action="void", refs=[ref], title=entry.title,
                       before={ref: {**snapshot(entry), "status": "active"}}, after={ref: snapshot(entry)},
                       because={"source": "topic_max"}, now=now)
    review_close(state_dir, ref, token=token, now=now)


def topic_rewrite(state_dir, topic_id, entry_id, *, text=None, kind=None, deleted=None, now=None) -> dict | None:
    """The Bridge's own change of the topic's line ``entry_id`` (live or a tombstone): new words, a kind, or ``deleted`` (a
    time: a tombstone; 0: live again). The line as it is now; None when there is no such line."""
    path = topic_memory_path(state_dir, topic_id)
    with shared_file_lock(path):
        rows = project_memory.rows_at(path, writing=True)
        row = next((row for row in rows if row["id"] == str(entry_id)), None)
        if row is None:
            return None
        if text is not None and project_memory.clean(text):
            row["text"] = project_memory.clean(text)
        if kind is not None:
            row["kind"] = project_memory.kind_of(kind)
        if deleted is not None:
            row["deleted"] = deleted if isinstance(deleted, (int, float)) and not isinstance(deleted, bool) else 0
        _topic_write(path, rows)
    note_topic(state_dir, topic_id, now=now)
    touch_rev(state_dir, now)
    return dict(row)


def is_code(text) -> bool:
    return bool(CODE_RE.match(str(text or "")))


def is_legacy(text) -> bool:
    return bool(LEGACY_RE.match(str(text or "")))


def touch_rev(state_dir, now: float | None = None) -> None:
    """Mark that some memory changed (``memory/.rev``'s mtime); never raises (core/memory_rev.py touch)."""
    memory_rev.touch(state_dir, now)


# -------------------------------------------------------------------------------------------------------- small helpers


def clean(text, limit=None) -> str:
    """One line: whitespace runs, newlines too, become one space; cut to ``limit``."""
    text = " ".join(str(text or "").split())
    return text[:limit] if limit else text


def limit_of(kind: str) -> int:
    return DECISION_CHARS if kind == "decision" else TEXT_CHARS


def auto_title(text, kind: str = "") -> str:
    """A title for an entry that has none: an answer's question (``主人答：Q→A``), else the first sentence (the next ones too
    while it is shorter than TITLE_MIN), ≤ TITLE_CHARS."""
    text = clean(text)
    if text.startswith("主人答："):
        text = text[len("主人答："):].split("→", 1)[0]
    head = text
    for end in _SENTENCE.finditer(text):
        head = text[:end.start() + (1 if end.group() in "？?" else 0)].strip()
        if len(head.rstrip("，,、：: 。！？?；")) >= TITLE_MIN:
            break
    else:
        head = text
    head = re.sub(r"\s*\n\s*", " ", head).strip() or text
    head = head.rstrip("，,、：: ")
    return head if len(head) <= TITLE_CHARS else head[:TITLE_CHARS - 1] + "…"


def _number(value) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return float(epoch_seconds(value) or 0.0)


def _strings(values, limit=64) -> tuple:
    return tuple(dict.fromkeys(clean(v, limit) for v in values or () if isinstance(v, str) and clean(v)))


def _refs(values) -> tuple:
    """Refs as kept: a topic's are long (its id is 64 characters), never cut."""
    return _strings(values, 160)


def same_key(text) -> str:
    """What two entries that say the same thing share (core/principles.py ``same_key``)."""
    return principles.same_key(text)


def rev_of(entry: Entry) -> str:
    """The entry's rev: changes whenever what a reader sees of it changes (the web's ``base`` for an edit)."""
    seen = [entry.code, entry.kind, entry.scope, entry.owner_key, entry.title, entry.text, entry.why, entry.status, entry.said,
            entry.by, list(entry.repos), entry.device, entry.verified_at, entry.superseded_by]
    return hashlib.sha1(json.dumps(seen, ensure_ascii=False).encode()).hexdigest()[:8]


def as_dict(entry: Entry) -> dict:
    """The entry as JSON: tuples as lists, its ``rev`` added."""
    out = asdict(entry)
    out["repos"], out["aliases"], out["rev"] = list(entry.repos), list(entry.aliases), rev_of(entry)
    return out


def snapshot(entry: Entry | None) -> dict | None:
    """What a change of ``entry`` changes (``_SNAP``), for the log's before/after and ``undo``."""
    if entry is None:
        return None
    out = {key: getattr(entry, key) for key in _SNAP}
    out["repos"] = list(entry.repos)
    return out


def _append_line(path: Path, row: dict) -> None:
    """One JSON line at the end of ``path`` (fsynced); a torn last line does not swallow it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    glue = ""
    try:
        with open(path, "rb") as stream:
            stream.seek(0, os.SEEK_END)
            if stream.tell():
                stream.seek(-1, os.SEEK_END)
                glue = "" if stream.read(1) == b"\n" else "\n"
    except FileNotFoundError:
        pass
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as stream:
        stream.write(glue + json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _lines(path: Path) -> list:
    """The JSON objects of the lines of ``path``; [] when there is none. A line that is not one is skipped."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    found = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            found.append(row)
    return found


# -------------------------------------------------------------------------------------------------------- kind mapping

# Which old store an entry came from (``kind3_of``'s ``source``).
PRINCIPLE, KNOWLEDGE, PROJECT, TOPIC, CANDIDATE = "principle", "knowledge", "project", "topic", "candidate"
LEAD_HELD = ("constraint", "decision")  # Of the lead's lines: what the project holds to (core/memory.py HELD_KINDS).
CARRIED_DAYS = 7  # A lead's line carried this recently stays a rule after the migration (docs/operations.md 记忆的存储与迁移).


def kind3_of(entry: dict, meta: dict | None = None, *, source: str = "") -> tuple[str, str, str]:
    """(kind, said, status) of an old entry (docs/operations.md 记忆的存储与迁移).

    ``source`` is the store it came from (``PRINCIPLE``, ``KNOWLEDGE``, ``PROJECT``, ``TOPIC``, ``CANDIDATE``); '' guesses it
    from the entry. ``meta`` is a project line's memory-meta.json part (merged into ``entry`` by core/project_memory.py
    entries already: either works). A principle is a rule of the owner's; a knowledge entry's decision and practice are
    rules, the rest notes; a project's line of the owner's (or one he confirmed, ``asserted_by`` owner) is a rule, one he
    marked 只限当次 a decision, an answer to a question a decision; the lead's line a note of 别人总结的, but a constraint or
    a decision of the lead's that the turns carried in the last ``CARRIED_DAYS`` days (``meta["carried"]`` true, set by
    the migration) stays a rule, still 别人总结的, until the owner decides (it is put in 待审核 too). A topic's line is
    a rule; an open candidate pending. A tombstone is void; a line done or superseded is archived (a decision done)."""
    row = {**(entry or {}), **(meta or {})}
    if not source:
        if "state" in row and row.get("kind") in knowledge.KINDS:
            source = KNOWLEDGE
        elif "source" in row:
            source = PROJECT
        else:
            source = PRINCIPLE
    if source == PRINCIPLE:
        return "rule", "owner", "active"
    if source == KNOWLEDGE:
        kind = "rule" if row.get("kind") in ("decision", "practice") else "note"
        status = {knowledge.SUPERSEDED: "archived", knowledge.DROPPED: "void"}.get(row.get("state"), "active")
        return kind, "owner", status
    if source == CANDIDATE:
        return "rule", "summary", "pending"
    origin = str(row.get("source") or "")
    owner = origin in project_memory.OWNERS or row.get("asserted_by") == turn_source.PARTY_OWNER
    if source == TOPIC:
        kind, said = "rule", "owner" if origin in ("", "owner") or owner else "summary"
    elif origin in project_memory.ANSWERS:
        kind, said = "decision", "owner"
    elif owner:
        kind, said = ("decision" if row.get("layer") == "decision" else "rule"), "owner"
    elif row.get("kind") in LEAD_HELD and row.get("carried"):
        kind, said = "rule", "summary"
    else:
        kind, said = "note", "summary"
    if row.get("deleted"):
        status = "void"
    elif row.get("valid") in project_memory.SETTLED:
        status = "done" if kind == "decision" else "archived"
    else:
        status = "active"
    return kind, said, status


# -------------------------------------------------------------------------------------------------------- short codes

_cache: dict = {}


def _cached(path: Path, parse):
    """``parse(data)`` of the JSON file at ``path``, read again only when its mtime or size changed."""
    try:
        stat = path.stat()
    except OSError:
        return parse({})
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    hit = _cache.get(str(path))
    if hit is not None and hit[0] == key:
        return hit[1]
    try:
        data = read_json(path)
    except (OSError, ValueError):
        return parse({})  # Not kept: a network disk's passing error must not stand for the file until it changes (RT-6).
    value = parse(data if isinstance(data, dict) else {})
    _cache[str(path)] = (key, value)
    return value


def _codes_parse(data) -> dict:
    codes = data.get("codes") if isinstance(data.get("codes"), dict) else {}
    aliases = data.get("aliases") if isinstance(data.get("aliases"), dict) else {}
    codes = {str(code): str(ref) for code, ref in codes.items() if is_code(code) and isinstance(ref, str)}
    return {"codes": codes, "aliases": {str(k): str(v) for k, v in aliases.items() if isinstance(v, str)},
            "refs": {ref: code for code, ref in codes.items() if ref}}


def codes(state_dir) -> dict:
    """{codes: code -> ref, aliases: old ref -> ref, refs: ref -> code} of every code given (memory/codes.json)."""
    return _cached(codes_path(state_dir), _codes_parse)


def _base32(digest: bytes, size: int) -> str:
    number = int.from_bytes(digest, "big")
    out = []
    for _ in range(size):
        number, rest = divmod(number, 32)
        out.append(CODE_ALPHABET[rest])
    return "".join(out)


def _candidates(kind: str, seed: str):
    letter = CODE_LETTERS.get(kind, "N")
    digest = hashlib.sha1(seed.encode("utf-8")).digest()
    for size in (4, 5, 6):
        yield letter + _base32(digest, size)
    while True:
        yield letter + _base32(secrets.token_bytes(8), 6)


def _write_codes(state_dir, data) -> None:
    write_json(codes_path(state_dir), {"version": VERSION, "codes": data["codes"], "aliases": data["aliases"]},
               ensure_ascii=False, separators=(",", ":"))


def assign(state_dir, pairs, *, aliases=None) -> dict:
    """ref -> code of each (ref, kind) of ``pairs``: the one given before, else a new one (from a hash of the ref, longer
    while it is taken), written into memory/codes.json with ``aliases`` (old ref -> ref). A registry that cannot be written
    gives each missing one a provisional code (six letters of its hash) that is not kept."""
    pairs = [(str(ref), kind) for ref, kind in pairs if ref]
    known = codes(state_dir)
    missing = [(ref, kind) for ref, kind in pairs if ref not in known["refs"]]
    new_aliases = {str(k): str(v) for k, v in (aliases or {}).items() if k and v and known["aliases"].get(k) != v}
    if not missing and not new_aliases:
        return {ref: known["refs"][ref] for ref, _ in pairs}
    try:
        with shared_file_lock(codes_path(state_dir)):
            data = _codes_parse(read_for_write(codes_path(state_dir)))
            for ref, kind in missing:
                if ref in data["refs"]:
                    continue
                code = next(code for code in _candidates(kind, ref) if code not in data["codes"])
                data["codes"][code], data["refs"][ref] = ref, code
            data["aliases"].update(new_aliases)
            _write_codes(state_dir, data)
            _cache.pop(str(codes_path(state_dir)), None)
        touch_rev(state_dir)  # A code given is a change a reader of the memory sees.
        return {ref: data["refs"][ref] for ref, _ in pairs}
    except OSError:
        return {ref: known["refs"].get(ref) or CODE_LETTERS.get(kind, "N") + _base32(hashlib.sha1(ref.encode()).digest(), 6)
                for ref, kind in pairs}


def rebind(state_dir, old: str, new: str) -> None:
    """The code of ``old`` names ``new`` from now on (an entry moved), ``old`` an alias of it."""
    with shared_file_lock(codes_path(state_dir)):
        data = _codes_parse(read_for_write(codes_path(state_dir)))
        code = data["refs"].pop(old, None)
        if code:
            data["codes"][code] = new
            data["refs"][new] = code
        data["aliases"][old] = new
        _write_codes(state_dir, data)
        _cache.pop(str(codes_path(state_dir)), None)
    touch_rev(state_dir)


def code_for(state_dir, kind: str) -> str:
    """A new short code of ``kind`` not given before, reserved in memory/codes.json (naming no entry yet)."""
    if kind not in KINDS:
        raise ValueError(f"kind {kind!r}")
    with shared_file_lock(codes_path(state_dir)):
        data = _codes_parse(read_for_write(codes_path(state_dir)))
        code = next(code for code in _candidates(kind, secrets.token_hex(8)) if code not in data["codes"])
        data["codes"][code] = ""
        _write_codes(state_dir, data)
        _cache.pop(str(codes_path(state_dir)), None)
    touch_rev(state_dir)
    return code


# -------------------------------------------------------------------------------------------------------- topic index


def topics(state_dir) -> dict:
    """topic id -> when its memory last changed, of every topic that has memory (memory/topics.json); no walk."""
    found = read_object(topics_path(state_dir)).get("topics")
    return {str(k): _number(v) for k, v in (found.items() if isinstance(found, dict) else ()) if _ID.match(str(k))}


def note_topic(state_dir, topic_id, *, now=None, gone=False) -> None:
    """Mark that the memory of ``topic_id`` changed now (core/topic_memory.py writes, decisions); never raises."""
    topic_id = str(topic_id or "")
    if not _ID.match(topic_id):
        return
    try:
        with shared_file_lock(topics_path(state_dir)):
            data = read_for_write(topics_path(state_dir))
            found = data.get("topics") if isinstance(data.get("topics"), dict) else {}
            if gone:
                found.pop(topic_id, None)
            else:
                found[topic_id] = time.time() if now is None else now
            write_json(topics_path(state_dir), {**data, "version": VERSION, "topics": found}, ensure_ascii=False,
                       separators=(",", ":"))
    except OSError:
        pass


def seed_topics(state_dir, *, now=None) -> int:
    """memory/topics.json made again from the topic directories that hold a memory.json or a decisions.jsonl: the one walk
    of every topic, done by the daily maintenance (core/maintenance_rules.py MemoryTopics), never at start. How many."""
    root = Path(state_dir) / "topics"
    found, started = {}, time.time()
    try:
        names = sorted(entry.name for entry in os.scandir(root) if entry.is_dir() and _ID.match(entry.name))
    except OSError:
        names = []
    for name in names:
        stamps = []
        for file in (root / name / project_memory.FILE, root / name / DECISIONS_FILE):
            try:
                stamps.append(file.stat().st_mtime)
            except OSError:
                pass
        if stamps:
            found[name] = max(stamps)
    with shared_file_lock(topics_path(state_dir)):
        data = read_for_write(topics_path(state_dir))
        noted = data.get("topics") if isinstance(data.get("topics"), dict) else {}
        found.update({tid: at for tid, at in noted.items() if _number(at) >= started})  # Written during the walk.
        if noted != found and (found or data):  # Written only when it says something new.
            write_json(topics_path(state_dir), {**data, "version": VERSION, "topics": found,
                                                "seeded_at": time.time() if now is None else now},
                       ensure_ascii=False, separators=(",", ":"))
    return len(found)


# -------------------------------------------------------------------------------------------------------- sidecars


def sidecar(state_dir, scope: str, owner_key: str) -> dict:
    """entry id -> its v2 fields, of a project's or a topic's memory-v2.json; {} when there is none."""
    found = read_object(sidecar_path(state_dir, scope, owner_key)).get("entries")
    return {str(k): dict(v) for k, v in (found.items() if isinstance(found, dict) else ()) if isinstance(v, dict)}


def _put_sidecar(state_dir, scope, owner_key, changes: dict) -> None:
    """entry id -> fields merged into its record (a value None takes the key out), under the file's lock."""
    path = sidecar_path(state_dir, scope, owner_key)
    with shared_file_lock(path):
        data = read_for_write(path)
        found = data.get("entries") if isinstance(data.get("entries"), dict) else {}
        for entry_id, fields in changes.items():
            row = dict(found.get(entry_id) or {})
            for key, value in fields.items():
                if value is None:
                    row.pop(key, None)
                else:
                    row[key] = value
            found[entry_id] = row
        write_json(path, {"version": VERSION, "entries": found}, ensure_ascii=False, indent=1)


# -------------------------------------------------------------------------------------------------------- reading


def _origin(row: dict, at: float) -> dict:
    origin = dict(row.get("origin") or {}) if isinstance(row.get("origin"), dict) else {}
    if "topic" in origin and "topic_key" not in origin:
        origin["topic_key"] = origin.pop("topic")
    origin.setdefault("at", at)
    return origin


def _make(ref, kind, scope, owner_key, text, status, said, by, *, row, extra, code, at, aliases=()) -> Entry:
    """An Entry of an old line with its sidecar fields (``extra``) laid over what its old store says."""
    kind = extra.get("kind") if extra.get("kind") in KINDS else kind
    said = extra.get("said") if extra.get("said") in SAID else said
    if extra.get("status") == "pending" and status == "active":
        status = "pending"
    elif extra.get("status") in ("archived", "done") and status == "active":
        status = extra["status"]
    text = clean(text, limit_of(kind))
    return Entry(ref=ref, code=code or "", kind=kind, scope=scope, owner_key=owner_key,
                 title=clean(extra.get("title"), TITLE_CHARS) or auto_title(text, kind), text=text, status=status, said=said,
                 by=extra.get("by") if extra.get("by") in BY else by, origin=_origin(row, at), why=clean(extra.get("why"), TEXT_CHARS),
                 repos=_strings(extra.get("repos")), device=clean(extra.get("device"), 64),
                 verified_at=_number(extra.get("verified_at")), superseded_by=str(extra.get("superseded_by") or row.get("superseded_by") or ""),
                 aliases=tuple(dict.fromkeys((*aliases, *_refs(extra.get("aliases"))))),
                 at=at, edited_at=_number(extra.get("edited_at")))


def _global_rows(state_dir) -> list:
    found = read_object(global_path(state_dir)).get("entries")
    return [dict(row) for row in (found if isinstance(found, list) else ()) if isinstance(row, dict) and _ID.match(str(row.get("id") or ""))]


def _global_entry(row) -> Entry:
    kind = row.get("kind") if row.get("kind") in KINDS else "rule"
    return Entry(ref=f"global:{row['id']}", code=str(row.get("code") or ""), kind=kind, scope="global", owner_key="",
                 title=clean(row.get("title"), TITLE_CHARS) or auto_title(row.get("text"), kind),
                 text=clean(row.get("text"), limit_of(kind)), status=row.get("status") if row.get("status") in STATUSES else "active",
                 said=row.get("said") if row.get("said") in SAID else "owner", by=row.get("by") if row.get("by") in BY else "owner",
                 origin=dict(row.get("origin") or {}), why=clean(row.get("why"), TEXT_CHARS), repos=_strings(row.get("repos")),
                 device=clean(row.get("device"), 64), verified_at=_number(row.get("verified_at")),
                 superseded_by=str(row.get("superseded_by") or ""), aliases=_refs(row.get("aliases")),
                 at=_number(row.get("at")), edited_at=_number(row.get("edited_at")))


def _global(state_dir) -> list:
    return [_global_entry(row) for row in _global_rows(state_dir)]


def _project_rows(state_dir, pid) -> list:
    """(row, archived?) of every line of the project's memory: memory.json's (tombstones too, with their meta) and what a
    full memory let go (memory-archive.json)."""
    record = projects.read_record(state_dir, pid)
    rows = project_memory.entries(state_dir, record) if record else project_memory.stored(state_dir, pid)
    gone = project_memory.archived(state_dir, pid)
    return [(row, False) for row in rows] + [({**row, **(row.get("meta") or {})}, True) for row in gone]


def _decision_events(state_dir, scope, owner_key) -> dict:
    """id -> the decision as decisions.jsonl's events leave it (an ``add``, then ``set`` events)."""
    found = {}
    for event in _lines(decisions_path(state_dir, scope, owner_key)):
        entry_id = str(event.get("id") or "")
        if not _ID.match(entry_id):
            continue
        if event.get("op") == "add" and entry_id not in found:
            found[entry_id] = dict(event)
        elif event.get("op") == "set" and entry_id in found and isinstance(event.get("fields"), dict):
            found[entry_id].update(event["fields"])
    return found


def _decision_entry(row, scope, owner_key) -> Entry:
    text = clean(row.get("text") or (f"{clean(row.get('q'))}→{clean(row.get('a'))}" if row.get("q") else row.get("a")), DECISION_CHARS)
    origin = dict(row.get("origin") or {}) if isinstance(row.get("origin"), dict) else {}
    origin.setdefault("at", _number(row.get("at")))
    for key in ("question_ref", "topic_key"):
        if row.get(key) and key not in origin:
            origin["question" if key == "question_ref" else key] = str(row[key])
    return Entry(ref=f"decision:{owner_key}:{row['id']}", code=str(row.get("code") or ""), kind="decision", scope=scope,
                 owner_key=owner_key, title=clean(row.get("title"), TITLE_CHARS) or auto_title(row.get("q") or text, "decision"),
                 text=text, status=row.get("status") if row.get("status") in STATUSES else "active",
                 said=row.get("said") if row.get("said") in SAID else "owner", by=row.get("by") if row.get("by") in BY else "owner",
                 origin=origin, why=clean(row.get("why"), TEXT_CHARS), superseded_by=str(row.get("superseded_by") or ""),
                 at=_number(row.get("at")), edited_at=_number(row.get("edited_at")))


def _project(state_dir, pid) -> list:
    extra = sidecar(state_dir, "project", pid)
    made = []
    for row, gone in _project_rows(state_dir, pid):
        side = extra.get(row["id"], {})
        if side.get("mirror_of"):
            continue  # A decision export_legacy wrote back for an older release: decisions.jsonl has it.
        if row.get("kind") == "style":
            continue  # project_style's line (汇报风格：…): the lead's own settings, read where they always were, never a rule.
        kind, said, status = kind3_of(row, {"carried": bool((side.get("guard") or {}).get("kept"))}, source=PROJECT)
        if row.get("superseded_by") and ":" not in str(row["superseded_by"]):
            row = {**row, "superseded_by": f"project:{pid}:{row['superseded_by']}"}
        if gone and status == "active":
            status = "done" if kind == "decision" else "archived"
        scope_ref = "decision" if kind == "decision" and row.get("source") in project_memory.ANSWERS else "project"
        made.append((f"{scope_ref}:{pid}:{row['id']}", kind, row, side, said, status))
    decisions = _decision_events(state_dir, "project", pid)
    pairs = [(ref, side.get("kind") or kind) for ref, kind, row, side, *_ in made]
    pairs += [(f"decision:{pid}:{key}", "decision") for key, row in decisions.items() if not row.get("code")]
    given = assign(state_dir, pairs)
    out = []
    for ref, kind, row, side, said, status in made:
        by = "owner" if said == "owner" and row.get("source") != "lead" else "lead"
        out.append(_make(ref, kind, "project", pid, row["text"], status, said, by, row=row, extra=side, code=given.get(ref),
                         at=_number(row.get("at"))))
    for key, row in decisions.items():
        entry = _decision_entry({**row, "id": key}, "project", pid)
        out.append(entry if entry.code else replace(entry, code=given.get(entry.ref, "")))
    return out


def _topic(state_dir, tid) -> list:
    extra = sidecar(state_dir, "topic", tid)
    rows = project_memory.rows_at(topic_memory_path(state_dir, tid))
    decisions = _decision_events(state_dir, "topic", tid)
    pairs = [(f"topic:{tid}:{row['id']}", (extra.get(row["id"]) or {}).get("kind") or "rule") for row in rows]
    pairs += [(f"decision:{tid}:{key}", "decision") for key, row in decisions.items() if not row.get("code")]
    given = assign(state_dir, pairs)
    out = []
    for row in rows:
        kind, said, status = kind3_of(row, None, source=TOPIC)
        ref = f"topic:{tid}:{row['id']}"
        out.append(_make(ref, kind, "topic", tid, row["text"], status, said, "owner" if said == "owner" else "bridge", row=row,
                         extra=extra.get(row["id"], {}), code=given.get(ref), at=_number(row.get("at"))))
    for key, row in decisions.items():
        entry = _decision_entry({**row, "id": key}, "topic", tid)
        out.append(entry if entry.code else replace(entry, code=given.get(entry.ref, "")))
    return out


def _project_ids(state_dir) -> list:
    return [record["id"] for record in projects.records(state_dir) if projects.valid_id(record.get("id"))]


def entries(state_dir, *, scope: str = "", owner_key: str = "", kinds=KINDS, statuses=("active",)) -> list[Entry]:
    """The entries of ``scope``/``owner_key`` (all scopes when ''; every project or every topic with memory when the key is
    ''), of ``kinds`` and ``statuses`` (None: any), oldest first within a store. A topic is read only when named or listed
    in memory/topics.json: nothing walks every topic directory. The short codes of lines that had none are given now."""
    if scope and scope not in SCOPES:
        raise ValueError(f"scope {scope!r}")
    found = []
    if scope in ("", "global"):
        found += _global(state_dir)
    if scope in ("", "project"):
        for pid in [owner_key] if (scope == "project" and owner_key) else _project_ids(state_dir):
            if projects.valid_id(pid):
                found += _project(state_dir, pid)
    if scope in ("", "topic"):
        for tid in [owner_key] if (scope == "topic" and owner_key) else sorted(topics(state_dir)):
            if _ID.match(str(tid)):
                found += _topic(state_dir, tid)
    kinds = set(kinds or KINDS)
    return [entry for entry in found if entry.kind in kinds and (statuses is None or entry.status in statuses)]


_REF = re.compile(r"^(global|project|topic|decision|principle|knowledge|candidate):(?:([0-9A-Za-z_\-]+):)?([0-9A-Za-z_\-]+)$")


def parse_ref(ref) -> tuple[str, str, str]:
    """(store, owner key, id) of a ref: store one of global, project, topic, decision (owner a project's or a topic's id),
    or the old principle, knowledge, candidate. ValueError for anything else."""
    found = _REF.match(str(ref or ""))
    if not found or (found.group(1) in ("project", "topic", "decision") and not found.group(2)):
        raise ValueError(f"not a memory ref: {ref!r}")
    return found.group(1), found.group(2) or "", found.group(3)


def _owner_scope(owner_key: str) -> str:
    """A decision's owner: a project's id (6 to 32 of 0-9a-z), else a topic's."""
    return "project" if projects.valid_id(owner_key) else "topic"


def _exact(state_dir, key: str) -> Entry | None:
    """The entry whose ref is ``key`` (an answer read as a decision by its project ref too); None for any other key."""
    try:
        store, owner, entry_id = parse_ref(key)
    except ValueError:
        return None
    if store == "global":
        pool = _global(state_dir)
    elif store in ("project", "decision") and projects.valid_id(owner):
        pool = _project(state_dir, owner)
    elif store in ("topic", "decision"):
        pool = _topic(state_dir, owner)
    else:
        return next((entry for entry in _global(state_dir) if key in entry.aliases), None)
    if store in ("project", "decision"):  # An answer read as a decision is still found by the project ref a turn named it by.
        return next((entry for entry in pool if entry.ref == key
                     or (entry.owner_key == owner and entry.ref.rsplit(":", 1)[-1] == entry_id)), None)
    return next((entry for entry in pool if entry.ref == key), None)


def find(state_dir, key: str) -> Entry | None:
    """The entry a ref, a short code or an old id (``LEGACY_RE``'s p…/k…, an alias) names; None when none does. A ref
    that is an entry's own names that entry (one moved away is still there, archived); a ref no entry has, the entry that
    has it as an alias (memory/codes.json). A memory number of a turn (``M12``) names an entry only with that turn's
    ``memory_ids``: ``find_in_turn``."""
    key = str(key or "").strip()
    if not key:
        return None
    known = codes(state_dir)
    if is_code(key.upper()):
        key = known["codes"].get(key.upper()) or ""
        if not key:
            return None
    elif re.fullmatch(r"p[0-9a-f]{6}", key):
        key = f"principle:{key}"
    elif re.fullmatch(r"k[0-9a-f]{6}", key):
        key = f"knowledge:{key}"
    found = _exact(state_dir, key)
    if found is None and known["aliases"].get(key):
        found = _exact(state_dir, known["aliases"][key])
    return found


def find_in_turn(state_dir, key: str, memory_ids) -> Entry | None:
    """``find``, an old memory number (``M12``) read through the ``memory_ids`` of the turn that carried it."""
    key = str(key or "").strip()
    if re.fullmatch(r"M\d{2,3}", key):
        row = next((row for row in memory_ids or () if isinstance(row, dict) and row.get("n") == key), None)
        return find(state_dir, str(row.get("ref") or "")) if row else None
    return find(state_dir, key)


# -------------------------------------------------------------------------------------------------------- writing


def _check(kind, scope, owner_key, title, text, why, said, by, status):
    if kind not in KINDS:
        raise ValueError("kind 只能是 rule（规矩）、decision（决定）、note（资料）")
    if scope not in SCOPES:
        raise ValueError("scope 只能是 global、project、topic")
    if scope != "global" and not _ID.match(str(owner_key or "")):
        raise ValueError("专项、话题范围要写是哪一个")
    if scope == "project" and not projects.valid_id(owner_key):
        raise ValueError("专项 id 不对")
    if said not in SAID or by not in BY or status not in STATUSES:
        raise ValueError("said、by 或 status 不对")
    text, title, why = clean(text), clean(title), clean(why)
    if not text:
        raise ValueError("正文是空的")
    if len(text) > limit_of(kind):
        raise ValueError(f"一条最多 {limit_of(kind)} 字，这条 {len(text)} 字")
    if len(title) > TITLE_CHARS:
        raise ValueError(f"标题最多 {TITLE_CHARS} 字")
    if len(why) > TEXT_CHARS:
        raise ValueError(f"原因最多 {TEXT_CHARS} 字")
    return text, title or auto_title(text, kind), why


def _done(state_dir, now, refs=(), scope="", owner_key="") -> None:
    if scope == "topic" and owner_key:
        note_topic(state_dir, owner_key, now=now)
    touch_rev(state_dir, now)


def _legacy_kind(kind):
    return "constraint" if kind == "rule" else "fact"


NEW, CONFIRMED, SAME = "new", "confirmed", "same"  # What ``put`` did.


def add(state_dir, **fields) -> Entry:
    """A new entry: ``put``'s, without saying what it did."""
    return put(state_dir, **fields)[0]


def put(state_dir, *, kind: str, scope: str, owner_key: str = "", text: str, title: str = "", why: str = "",
        said: str = "owner", by: str = "owner", origin: dict | None = None, repos=(), device: str = "",
        verified_at: float = 0.0, status: str = "active", aliases=(), who: str = "",
        because: dict | None = None, token: str = "", log: bool = True, now: float | None = None) -> tuple[Entry, str]:
    """(the entry, what was done: ``NEW``, ``CONFIRMED`` or ``SAME``): a new entry (a decision goes through
    ``add_decision``). 全局 into memory/global.json; a project's or a topic's into its memory.json, the store every release
    reads, with the rest in its memory-v2.json. The same text of the same kind still live in that store is the one returned,
    as it was (``SAME``); but when it is somebody else's summary and this one is the owner's (``said`` owner), that line is
    confirmed where it is (同文就地确认, T908 M3a): 你定的 from now on, the owner's origin laid over its own, logged as a
    confirm (``CONFIRMED``). Logged (``token`` names the change for ``undo``). ValueError says why nothing was written."""
    if kind == "decision":
        raise ValueError("决定记录用 add_decision")
    stamp = time.time() if now is None else now
    text, title, why = _check(kind, scope, owner_key, title, text, why, said, by, status)
    extra = {"title": title, "kind": kind, "said": said, "by": by, "why": why or None, "repos": list(_strings(repos)) or None,
             "device": clean(device, 64) or None, "verified_at": float(verified_at) or None,
             "aliases": list(_refs(aliases)) or None, "status": status if status != "active" else None, "edited_at": None}
    if scope == "global":
        with shared_file_lock(global_path(state_dir)):
            data = read_for_write(global_path(state_dir))
            rows = data.get("entries") if isinstance(data.get("entries"), list) else []
            key = same_key(text)
            row = next((row for row in rows if isinstance(row, dict) and row.get("kind") == kind
                        and row.get("status") in ("active", "pending") and same_key(row.get("text")) == key), None)
            if row is not None and (said != "owner" or row.get("said", "owner") == "owner"):
                return _global_entry(row), SAME
            before = None
            if row is not None:  # The owner says what somebody else summed up: that line is his now, where it is.
                before = _global_entry(dict(row))
                row.update(said="owner", origin={**dict(row.get("origin") or {}), **dict(origin or {})})
            else:
                taken = {str(r.get("id")) for r in rows if isinstance(r, dict)}
                gid = next(gid for gid in ("g" + secrets.token_hex(3) for _ in iter(int, 1)) if gid not in taken)
                row = {"id": gid, "text": text, "at": stamp, "origin": {"at": stamp, **dict(origin or {})}, "status": "active"}
                row.update({k: v for k, v in extra.items() if v is not None})
                rows.append(row)
                ref = f"global:{gid}"
                row["code"] = assign(state_dir, [(ref, kind)], aliases={a: ref for a in _refs(aliases)})[ref]
            write_json(global_path(state_dir), {**data, "version": VERSION, "entries": rows}, ensure_ascii=False, indent=1)
        entry = _global_entry(row)
        if before is not None:
            if log:
                log_change(state_dir, who=who or by, action="confirm", refs=[entry.ref], title=entry.title,
                           before={entry.ref: snapshot(before)}, after={entry.ref: snapshot(entry)}, because=because,
                           token=token, now=stamp)
            _done(state_dir, stamp)
            return entry, CONFIRMED
    elif scope == "project":
        source = "owner" if said == "owner" else "lead"
        try:
            row, new = project_memory.add(state_dir, owner_key, text, _legacy_kind(kind), source, now=stamp,
                                          origin=_legacy_origin(origin))
        except KeyError:
            raise ValueError("专项不存在") from None
        if not new:
            return _same(state_dir, f"project:{owner_key}:{row['id']}", said, who or by, because, token, stamp, log)
        _put_sidecar(state_dir, "project", owner_key, {row["id"]: extra})
        ref = f"project:{owner_key}:{row['id']}"
        assign(state_dir, [(ref, kind)], aliases={a: ref for a in _refs(aliases)})
        entry = find(state_dir, ref)
    else:
        row, new = topic_add(state_dir, owner_key, text, source="owner" if said == "owner" else "lead",
                             now=stamp, kind=_legacy_kind(kind))
        if not new:
            return _same(state_dir, f"topic:{owner_key}:{row['id']}", said, who or by, because, token, stamp, log)
        _put_sidecar(state_dir, "topic", owner_key, {row["id"]: extra})
        ref = f"topic:{owner_key}:{row['id']}"
        assign(state_dir, [(ref, kind)], aliases={a: ref for a in _refs(aliases)})
        entry = find(state_dir, ref)
    if log:
        log_change(state_dir, who=who or by, action="add", refs=[entry.ref], title=entry.title, before={entry.ref: None},
                   after={entry.ref: snapshot(entry)}, because=because, token=token, now=stamp)
    _done(state_dir, stamp, scope=scope, owner_key=owner_key)
    return entry, NEW


def _same(state_dir, ref, said, who, because, token, now, log=True) -> tuple[Entry, str]:
    """``put``'s answer for a project's or a topic's line whose words were there already: it as it was, or, the owner saying
    what somebody else summed up, confirmed (said owner; core/project_memory.py add laid his origin on a project's line)."""
    entry = find(state_dir, ref)
    if said != "owner" or entry is None or entry.said == "owner":
        return entry, SAME
    return (change(state_dir, ref, action="confirm", who=who, because=because, token=token, log=log, now=now, said="owner"),
            CONFIRMED)


def _legacy_origin(origin) -> dict:
    """An origin as core/project_memory.py keeps one (its ORIGIN_KEYS; ``topic`` for the topic)."""
    origin = dict(origin or {})
    if "topic_key" in origin and "topic" not in origin:
        origin["topic"] = origin.pop("topic_key")
    return {key: value for key, value in origin.items() if key in project_memory.ORIGIN_KEYS or key == "index"}


def add_decision(state_dir, *, scope: str, owner_key: str, question: str = "", answer: str = "", text: str = "",
                 title: str = "", question_ref: str = "", topic_key: str = "", chosen: str = "", category: str = "",
                 polarity: str = "", said: str = "owner", by: str = "owner", origin: dict | None = None, status: str = "active",
                 who: str = "", because: dict | None = None, token: str = "", log: bool = True,
                 now: float | None = None) -> Entry:
    """A decision appended to the project's or the topic's decisions.jsonl: the question and the answer (``text`` when given,
    else ``question→answer``), ≤ DECISION_CHARS. The same ``question_ref`` (a question record's token and the question's
    place, ``<token>-<n>``) noted before is the one returned, not noted twice. Logged."""
    stamp = time.time() if now is None else now
    body = clean(text) or (f"{clean(question)}→{clean(answer)}" if question else clean(answer))
    body, title, _ = _check("decision", scope, owner_key, title or auto_title(question or body, "decision"), body, "", said, by,
                            status)
    path = decisions_path(state_dir, scope, owner_key)
    with shared_file_lock(path):
        found = _decision_events(state_dir, scope, owner_key)
        same = next((key for key, row in found.items() if question_ref and row.get("question_ref") == question_ref), None)
        if same is None:
            entry_id = next(i for i in ("d" + secrets.token_hex(3) for _ in iter(int, 1)) if i not in found)
            ref = f"decision:{owner_key}:{entry_id}"
            code = assign(state_dir, [(ref, "decision")])[ref]
            row = {"v": VERSION, "op": "add", "id": entry_id, "code": code, "at": stamp, "title": title, "text": body,
                   "q": clean(question, DECISION_CHARS), "a": clean(answer, DECISION_CHARS), "question_ref": clean(question_ref, 40),
                   "topic_key": clean(topic_key, 80), "chosen": clean(chosen, 200), "category": clean(category, 20),
                   "polarity": clean(polarity, 10), "status": status, "said": said, "by": by,
                   "origin": {"at": stamp, **dict(origin or {})}}
            _append_line(path, {key: value for key, value in row.items() if value not in ("", None)})
            found[entry_id] = row
        else:
            entry_id = same
    entry = _decision_entry({**found[entry_id], "id": entry_id}, scope, owner_key)
    if log and same is None:
        log_change(state_dir, who=who or by, action="add", refs=[entry.ref], title=entry.title, before={entry.ref: None},
                   after={entry.ref: snapshot(entry)}, because=because, token=token, now=stamp)
    _done(state_dir, stamp, scope=scope, owner_key=owner_key)
    return entry


def decisions(state_dir, scope: str, owner_key: str, *, statuses=None) -> list[Entry]:
    """The decisions of a project or a topic, in every status (``statuses`` to keep some): its old answers and decisions.jsonl."""
    return [entry for entry in entries(state_dir, scope=scope, owner_key=owner_key, kinds=("decision",), statuses=statuses)]


def _update(state_dir, entry: Entry, changes: dict, now: float) -> Entry:
    """Write ``changes`` (of ``_SNAP`` and the other Entry fields) of ``entry`` into the store it lives in; the entry after."""
    store, owner, entry_id = parse_ref(entry.ref)
    changes = {key: value for key, value in changes.items() if value is not None and value != getattr(entry, key, None)}
    if "repos" in changes:
        changes["repos"] = list(_strings(changes["repos"]))
    if not changes:
        return entry
    if store == "global":
        with shared_file_lock(global_path(state_dir)):
            data = read_for_write(global_path(state_dir))
            rows = data.get("entries") if isinstance(data.get("entries"), list) else []
            row = next((row for row in rows if isinstance(row, dict) and str(row.get("id")) == entry_id), None)
            if row is None:
                raise ValueError("这条记忆已经不在了")
            row.update(changes)
            if {"title", "text", "why", "kind"} & set(changes):
                row["edited_at"] = now
            if "status" in changes:
                row["status_at"] = now
            write_json(global_path(state_dir), {**data, "version": VERSION, "entries": rows}, ensure_ascii=False, indent=1)
    elif store == "decision" and entry_id.startswith("d") and _decision_events(state_dir, entry.scope, owner).get(entry_id):
        path = decisions_path(state_dir, entry.scope, owner)
        fields = {key: value for key, value in changes.items() if key in ("title", "text", "status", "superseded_by", "why", "said")}
        if {"title", "text"} & set(fields):
            fields["edited_at"] = now
        if fields.get("status") in ("done", "void"):
            fields["done_at"] = now
        with shared_file_lock(path):
            _append_line(path, {"v": VERSION, "op": "set", "id": entry_id, "at": now, "fields": fields})
    else:
        scope = "topic" if store == "topic" or (store == "decision" and _owner_scope(owner) == "topic") else "project"
        side, legacy = {}, {}
        for key, value in changes.items():
            if key == "text":
                legacy["text"] = value
                side["edited_at"] = now
            elif key == "status":
                legacy.update(_legacy_status(value, entry.kind, scope, now))
                side["status"] = value if value in ("pending",) or (scope == "topic" and value in ("archived", "done")) else None
            elif key == "superseded_by":
                same = value.rsplit(":", 1)[-1] if value.startswith(f"{store}:{owner}:") else ""
                legacy["superseded_by"] = same
                side["superseded_by"] = value or None
            elif key == "kind":
                side["kind"] = value
                if store != "decision":
                    legacy["kind"] = _legacy_kind(value)
            else:
                side[key] = value
                if key == "said" and scope == "project" and store != "decision" and value != entry.said:
                    # Confirmed (or that undone): the lead's tools and 整理记忆 (core/memory.py describe) read it so too.
                    legacy["owner"] = value == "owner"
                if key in ("title", "why"):
                    side["edited_at"] = now
        if legacy:
            if scope == "project":
                project_memory.rewrite(state_dir, owner, entry_id, now=now, **legacy)
            else:
                topic_rewrite(state_dir, owner, entry_id, now=now,
                              **{k: v for k, v in legacy.items() if k in ("text", "kind", "deleted")})
        if side:
            _put_sidecar(state_dir, scope, owner, {entry_id: side})
    after = find(state_dir, entry.ref)
    if after is None:
        raise ValueError("这条记忆已经不在了")
    return after


def _legacy_status(status, kind, scope, now) -> dict:
    """What an older release's store says for ``status``: a tombstone for void, history (valid done) for done and archived."""
    if status == "void":
        return {"deleted": now}
    if scope == "topic":
        return {"deleted": 0}
    if status in ("done", "archived"):
        return {"deleted": 0, "valid": "done"}
    return {"deleted": 0, "valid": ""}


def _need(state_dir, key) -> Entry:
    entry = find(state_dir, key)
    if entry is None:
        raise ValueError("这条记忆已经不在了")
    return entry


def change(state_dir, key: str, *, action: str = "edit", base: str = "", who: str = "", because: dict | None = None,
           token: str = "", log: bool = True, now: float | None = None, **fields) -> Entry:
    """The entry ``key`` names with ``fields`` changed (title, text, why, kind, said, status, superseded_by, repos, device,
    verified_at): the one write every other write goes through. ``base``, when given, is the rev the editor started from
    (``rev_of``): an entry changed meanwhile is left as it is (ValueError). Logged as ``action``."""
    stamp = time.time() if now is None else now
    entry = _need(state_dir, key)
    if base and base != rev_of(entry):
        raise ValueError("这条刚在别处改过，先看看现在的内容再改")
    if "text" in fields and fields["text"] is not None:
        fields["text"] = clean(fields["text"])
        if not fields["text"]:
            raise ValueError("正文是空的")
        if len(fields["text"]) > limit_of(fields.get("kind") or entry.kind):
            raise ValueError(f"一条最多 {limit_of(fields.get('kind') or entry.kind)} 字")
    if fields.get("title") is not None:
        fields["title"] = clean(fields["title"], TITLE_CHARS) or auto_title(fields.get("text") or entry.text)
    for name, allowed in (("kind", KINDS), ("said", SAID), ("status", STATUSES)):
        if fields.get(name) is not None and fields[name] not in allowed:
            raise ValueError(f"{name} 不对")
    after = _update(state_dir, entry, fields, stamp)
    if log and snapshot(after) != snapshot(entry):
        token = log_change(state_dir, who=who, action=action, refs=[entry.ref], title=after.title,
                           before={entry.ref: snapshot(entry)}, after={entry.ref: snapshot(after)}, because=because,
                           token=token, now=stamp)
    if after.status in GONE and entry.status not in GONE:  # The line a 推断全局 holds is gone: its item closes (T908 M3b).
        review_close(state_dir, entry.ref, by=who or "bridge", token=token, now=stamp)
    _done(state_dir, stamp, scope=entry.scope, owner_key=entry.owner_key)
    return after


def edit(state_dir, key: str, *, title=None, text=None, why=None, repos=None, device=None, verified_at=None, base: str = "",
         who: str = "", because=None, token: str = "", now=None) -> Entry:
    """New words (or repos, device, verification time) for the entry ``key``; ``base`` its rev the editor saw."""
    return change(state_dir, key, action="edit", base=base, who=who, because=because, token=token, now=now, title=title,
                  text=text, why=why, repos=repos, device=device, verified_at=verified_at)


def set_status(state_dir, key: str, status: str, *, superseded_by: str | None = None, action: str = "", base: str = "",
               who: str = "", because=None, token: str = "", now=None) -> Entry:
    """The entry ``key`` made ``status`` (``archive``, ``restore``, ``void``, ``settle`` say which)."""
    return change(state_dir, key, action=action or status, base=base, who=who, because=because, token=token, now=now,
                  status=status, superseded_by=superseded_by)


def archive(state_dir, key: str, **kwargs) -> Entry:
    """收起: not carried, still found and restorable."""
    return set_status(state_dir, key, "archived", action="archive", **kwargs)


def restore(state_dir, key: str, **kwargs) -> Entry:
    """放回: active again (one superseded loses the mark)."""
    return set_status(state_dir, key, "active", superseded_by="", action="restore", **kwargs)


def void(state_dir, key: str, **kwargs) -> Entry:
    """作废: a tombstone (an older release's delete for a line of memory.json)."""
    return set_status(state_dir, key, "void", action="void", **kwargs)


def settle(state_dir, key: str, **kwargs) -> Entry:
    """A decision done (办完): kept and searchable, no longer carried."""
    return set_status(state_dir, key, "done", action="settle", **kwargs)


def supersede(state_dir, key: str, by_key: str, *, who: str = "", because=None, token: str = "", now=None) -> Entry:
    """The entry ``key`` replaced by ``by_key`` (another live entry): archived (a decision done) with ``superseded_by``."""
    new = _need(state_dir, by_key)
    old = _need(state_dir, key)
    if new.ref == old.ref:
        raise ValueError("不能用它自己替代自己")
    return set_status(state_dir, key, "done" if old.kind == "decision" else "archived", superseded_by=new.ref,
                      action="supersede", who=who, because=because, token=token, now=now)


def set_kind(state_dir, key: str, kind: str, *, said: str | None = None, who: str = "", because=None, token: str = "",
             now=None) -> Entry:
    """The entry ``key`` counted as ``kind`` from now on (a rule demoted to a note, a note made a rule), ``said`` too."""
    return change(state_dir, key, action="kind", who=who, because=because, token=token, now=now, kind=kind, said=said)


def confirm(state_dir, key: str, *, who: str = "owner", because=None, token: str = "", now=None) -> Entry:
    """The owner confirms the entry ``key``: 你定的 from now on."""
    return change(state_dir, key, action="confirm", who=who, because=because, token=token, now=now, said="owner")


def move(state_dir, key: str, *, scope: str, owner_key: str = "", base: str = "", who: str = "", because=None,
         token: str = "", now=None) -> Entry:
    """The entry ``key`` moved to ``scope``/``owner_key``: a new entry there with its words, fields and code, the old one
    archived with ``superseded_by`` and an alias of the new one. The new entry. A decision does not move."""
    stamp = time.time() if now is None else now
    old = _need(state_dir, key)
    if base and base != rev_of(old):
        raise ValueError("这条刚在别处改过，先看看现在的内容再改")
    if old.kind == "decision":
        raise ValueError("决定记录不挪范围")
    if (scope, owner_key or "") == (old.scope, old.owner_key):
        return old
    new = add(state_dir, kind=old.kind, scope=scope, owner_key=owner_key, text=old.text, title=old.title, why=old.why,
              said=old.said, by=old.by, origin=old.origin, repos=old.repos, device=old.device, verified_at=old.verified_at,
              aliases=(*old.aliases, old.ref), log=False, now=stamp)
    archived = _update(state_dir, old, {"status": "archived", "superseded_by": new.ref}, stamp)
    if old.code and old.ref in new.aliases:  # Made now (not one that said the same there already): it keeps the code.
        try:
            _keep_code(state_dir, old, new)
            new = find(state_dir, new.ref) or new
        except OSError:
            pass
    log_change(state_dir, who=who, action="move", refs=[old.ref, new.ref], title=new.title,
               before={old.ref: snapshot(old), new.ref: None}, after={old.ref: snapshot(archived), new.ref: snapshot(new)},
               because=because, token=token, now=stamp)
    _done(state_dir, stamp, scope=scope, owner_key=owner_key)
    return new


def _keep_code(state_dir, old: Entry, new: Entry) -> None:
    """The moved entry keeps the code it had: the registry's code of ``old`` names ``new``; the one ``new`` got is dropped."""
    with shared_file_lock(codes_path(state_dir)):
        data = _codes_parse(read_for_write(codes_path(state_dir)))
        given = data["refs"].pop(new.ref, None)
        if given and data["codes"].get(given) == new.ref:
            data["codes"][given] = ""
        data["codes"][old.code] = new.ref
        data["aliases"][old.ref] = new.ref
        _write_codes(state_dir, data)
        _cache.pop(str(codes_path(state_dir)), None)
    if new.scope == "global":
        _, _, gid = parse_ref(new.ref)
        with shared_file_lock(global_path(state_dir)):
            data = read_for_write(global_path(state_dir))
            rows = data.get("entries") if isinstance(data.get("entries"), list) else []
            for row in rows:
                if isinstance(row, dict) and str(row.get("id")) == gid:
                    row["code"] = old.code
            write_json(global_path(state_dir), {**data, "entries": rows}, ensure_ascii=False, indent=1)


def promote(state_dir, key: str, *, scope: str, owner_key: str = "", repos=None, who: str = "owner", because=None,
            token: str = "", now=None) -> Entry:
    """升级: ``move`` to a wider scope (a topic's line to its project or to 全局), ``repos`` set on the way."""
    new = move(state_dir, key, scope=scope, owner_key=owner_key, who=who, because=because, token=token, now=now)
    if repos is not None:
        new = change(state_dir, new.ref, action="edit", who=who, token=token, now=now, repos=repos, log=False)
    return new


# -------------------------------------------------------------------------------------------------------- log and undo


def log_change(state_dir, *, who: str, action: str, refs, title: str = "", before: dict | None = None,
               after: dict | None = None, because: dict | None = None, token: str = "", now: float | None = None) -> str:
    """One line of memory/log.jsonl: {at, who, action, refs, title, before, after, because?, undo_token}; the token (``token``
    when given, else a new one). Never raises: a log that cannot be written loses the line, not the change."""
    token = clean(token, 64) or "u-" + secrets.token_hex(6)
    row = {"at": time.time() if now is None else now, "who": clean(who, 40) or "bridge", "action": clean(action, 20),
           "refs": [str(ref) for ref in refs], "title": clean(title, TITLE_CHARS), "before": before or {}, "after": after or {},
           **({"because": dict(because)} if because else {}), "undo_token": token}
    try:
        _append_line(log_path(state_dir), row)
    except OSError:
        pass
    return token


def noted(state_dir, before: Entry | None, ref: str = "", *, who: str, action: str, because: dict | None = None,
          token: str = "", now: float | None = None) -> str:
    """A change some other module wrote to an entry's file itself (a lead's revision of its project's line, the learner's
    supersede, core/project_memory.py and core/topic_memory.py writes) logged as ``change`` logs its own: ``before`` the entry
    as it was (None: made by the change; then ``ref`` names it), the entry now read again. An entry gone by it closes the
    待审核 about it (``review_close``). The token ('' when nothing changed)."""
    stamp = time.time() if now is None else now
    ref = before.ref if before is not None else ref
    try:
        after = find(state_dir, ref) if ref else None
    except (OSError, ValueError):
        after = None
    if after is None or (before is not None and snapshot(before) == snapshot(after)):
        return ""
    token = log_change(state_dir, who=who, action=action, refs=[after.ref], title=after.title,
                       before={after.ref: snapshot(before)}, after={after.ref: snapshot(after)}, because=because, token=token,
                       now=stamp)
    if after.status in GONE and (before is None or before.status not in GONE):
        review_close(state_dir, after.ref, by=who, token=token, now=stamp)
    return token


def log_read(state_dir, *, limit: int = 100, since: float = 0.0) -> list:
    """The latest ``limit`` lines of memory/log.jsonl written after ``since``, newest first."""
    rows = [row for row in _lines(log_path(state_dir)) if _number(row.get("at")) > since]
    return list(reversed(rows))[:limit]


def undo(state_dir, token: str, *, who: str = "owner", now: float | None = None) -> list[str]:
    """Every change logged with ``token`` put back as it was, the newest first: an entry made by it voided, one changed by it
    given its words, kind and status again. ValueError when the token names nothing, was undone already, or one of its
    entries changed since (nothing is put back then). The refs put back."""
    stamp = time.time() if now is None else now
    token = clean(token, 64)
    rows = _lines(log_path(state_dir))
    if not token or any(row.get("undid") == token for row in rows):
        raise ValueError("这一步已经撤销过了" if token else "没有要撤销的")
    mine = [row for row in rows if row.get("undo_token") == token and row.get("action") != "undo"]
    if not mine:
        raise ValueError("找不到这一步")
    plan = []
    for row in reversed(mine):
        for ref in row.get("refs") or ():
            current = find(state_dir, ref)
            after = (row.get("after") or {}).get(ref)
            if after is not None and (current is None or snapshot(current) != after):
                raise ValueError("这条后来又改过了，撤销不了")
            plan.append((ref, current, (row.get("before") or {}).get(ref)))
    done = []
    for ref, current, before in plan:
        if current is None:
            continue
        wanted = {"status": "void"} if before is None else {key: before.get(key) for key in _SNAP}
        if before is not None:
            wanted["repos"] = tuple(before.get("repos") or ())
        _update(state_dir, current, wanted, stamp)
        done.append(ref)
    try:
        title = next((str(row.get("title") or "") for row in reversed(mine) if row.get("title")), "")  # 记录页写撤销了哪条。
        _append_line(log_path(state_dir), {"at": stamp, "who": who, "action": "undo", "refs": done, "undid": token,
                                           **({"title": title} if title else {})})
    except OSError:
        pass
    touch_rev(state_dir, stamp)
    return done


# -------------------------------------------------------------------------------------------------------- review queue


def read_for_write(path) -> dict:
    """The JSON object in ``path`` a write is about to change: core/state.py ``read_for_write`` (kept under this name: callers
    and tests reach it here). {} when there is no file yet; Unreadable (an OSError) when there is one that cannot be read or
    parsed, so the write is refused rather than writing the whole file over with only what it adds (T870 Wave 3.5)."""
    return state_read_for_write(path)


def _review_data(state_dir, *, writing=False) -> dict:
    data = (read_for_write if writing else read_object)(review_path(state_dir))
    items = data.get("items") if isinstance(data.get("items"), list) else []
    return {"version": VERSION, "items": [dict(item) for item in items if isinstance(item, dict) and item.get("key")]}


def review_state(item: dict) -> str:
    """The state of the item ``item`` of 待审核: as stored, but ``expired`` for one still open whose type is no longer one of
    REVIEW_TYPES (an older release's; nothing decides it any more)."""
    state = str(item.get("state") or "open")
    return "expired" if state == "open" and item.get("type") not in REVIEW_TYPES else state


def review_items(state_dir, *, states=("open",)) -> list:
    """The items of 待审核 (memory/review.json) in ``states`` (None: all), oldest first: the fields of core/memory_review.py
    ReviewItem with ``state`` (``review_state``), ``fp`` and, once decided, ``decided_at``, ``decided_by``."""
    out = []
    for item in _review_data(state_dir)["items"]:
        state = review_state(item)
        if states is None or state in states:
            out.append({**item, "state": state} if state != item.get("state", "open") else item)
    return out


def review_fp(item: dict) -> str:
    """What two items that propose the same share: their type, the entry they are about or their words, and where to."""
    suggest = item.get("suggest") if isinstance(item.get("suggest"), dict) else {}
    about = ",".join(item.get("items") or ()) if item.get("type") == "migrate" else same_key(item.get("text"))
    body = "\0".join((str(item.get("type") or ""), about, str(suggest.get("scope") or ""),
                      str(suggest.get("project_key") or suggest.get("owner_key") or "")))
    return hashlib.sha1(body.encode("utf-8")).hexdigest()[:12]


def review_put(state_dir, item: dict, *, now: float | None = None) -> tuple[str, bool]:
    """(its key, whether it is new): ``item`` (ReviewItem's fields; ``key`` and ``created_at`` given here when missing) put in
    待审核 open. One proposing the same (``review_fp``) still open, turned down, or accepted already (``REVIEW_HELD``) is the one
    returned; one that expired unanswered does not hold it back."""
    return review_put_many(state_dir, [item], now=now)[0]


def review_put_many(state_dir, items, *, now: float | None = None) -> list[tuple[str, bool]]:
    """``review_put`` of each of ``items``, in one write of memory/review.json."""
    stamp = time.time() if now is None else now
    wanted = []
    for item in items:
        item = dict(item)
        kind = str(item.get("type") or "")
        if kind not in REVIEW_TYPES:
            raise ValueError(f"review type {kind!r}")
        item["title"] = clean(item.get("title"), TITLE_CHARS) or auto_title(item.get("text"))
        item["text"] = clean(item.get("text"), DECISION_CHARS)
        item["fp"] = review_fp(item)
        wanted.append(item)
    if not wanted:
        return []
    out = []
    with shared_file_lock(review_path(state_dir)):
        data = _review_data(state_dir, writing=True)
        by_fp = {old.get("fp"): old for old in data["items"] if review_state(old) in REVIEW_HELD}
        taken = {old["key"] for old in data["items"]}
        for item in wanted:
            same = by_fp.get(item["fp"])
            if same is not None:
                out.append((same["key"], False))
                continue
            kind = item["type"]
            item["key"] = item.get("key") or next(key for key in (f"review:{kind}:{secrets.token_hex(3)}" for _ in iter(int, 1))
                                                  if key not in taken)
            taken.add(item["key"])
            item.setdefault("created_at", stamp)
            item["state"] = "open"
            row = {key: (list(value) if isinstance(value, tuple) else value) for key, value in item.items()}
            data["items"].append(row)
            by_fp[item["fp"]] = row
            out.append((item["key"], True))
        if any(new for _, new in out):
            write_json(review_path(state_dir), data, ensure_ascii=False, indent=1)
    if any(new for _, new in out):
        touch_rev(state_dir, stamp)
    return out


def review_expire(state_dir, *, days: float = REVIEW_DAYS, now: float | None = None) -> int:
    """The open items of 待审核 older than ``days`` made ``expired`` by the Bridge, in one write (kept, never deleted): how many."""
    stamp = time.time() if now is None else now
    cutoff = stamp - days * 86400
    if not any(_number(item.get("created_at")) < cutoff for item in review_items(state_dir)):
        return 0  # A read and no write, most days.
    with shared_file_lock(review_path(state_dir)):
        data = _review_data(state_dir, writing=True)
        old = [item for item in data["items"] if review_state(item) == "open" and _number(item.get("created_at")) < cutoff]
        for item in old:
            item.update(state="expired", decided_at=stamp, decided_by="bridge")
        if old:
            write_json(review_path(state_dir), data, ensure_ascii=False, indent=1)
    for item in old:
        _review_logged(state_dir, item, "open", by="bridge", now=stamp)
    if old:
        touch_rev(state_dir, stamp)
    return len(old)


# What a change of an item's state is called in memory/log.jsonl (the page's 记录 words: 采纳, 不要, 归档, 放回).
REVIEW_ACTIONS = {"accepted": "accept", "rejected": "reject", "expired": "archive", "open": "restore"}


def _review_logged(state_dir, item: dict, before: str, *, by: str, token: str = "", now: float | None = None) -> None:
    """The change of the item ``item`` of 待审核 from ``before`` to its state now in memory/log.jsonl (T908 M3b: 待审核's state
    changes are in the 记录 too), under ``token`` when the decision has one. Its ref is the item's key and it has no
    ``after``: an undo of the token passes over it (the entries the decision wrote are what an undo puts back)."""
    state = review_state(item)
    if state == before:
        return
    log_change(state_dir, who=by, action=REVIEW_ACTIONS.get(state, state), refs=[str(item.get("key") or "")],
               title=str(item.get("title") or ""), before={str(item.get("key") or ""): None}, token=token, now=now)


def review_set(state_dir, key: str, state: str, *, by: str = "owner", now: float | None = None, **extra) -> dict | None:
    """The item ``key`` made ``state`` (one of REVIEW_STATES) by ``by``, with ``extra`` fields (``refs`` written, ``token``,
    …), logged (``_review_logged``): it as it is now; None when there is none."""
    if state not in REVIEW_STATES:
        raise ValueError(f"review state {state!r}")
    stamp = time.time() if now is None else now
    with shared_file_lock(review_path(state_dir)):
        data = _review_data(state_dir, writing=True)
        item = next((item for item in data["items"] if item["key"] == key), None)
        if item is None:
            return None
        before = review_state(item)
        item.update(extra, state=state, decided_at=stamp if state != "open" else None, decided_by=by if state != "open" else None)
        write_json(review_path(state_dir), data, ensure_ascii=False, indent=1)
    _review_logged(state_dir, item, before, by=by, token=str(extra.get("token") or ""), now=stamp)
    touch_rev(state_dir, stamp)
    return dict(item)


def review_close(state_dir, ref: str, *, by: str = "bridge", token: str = "", now: float | None = None) -> list:
    """The open items of 待审核 about the entry ``ref`` (their ``items`` name it: the line a 推断全局 holds where it was said)
    closed as expired, the entry being gone (voided, put away; T908 M3b): the keys closed. Expired, so the same words said
    again may be proposed again; ``token`` the change's that took the entry away, so its undo opens them again
    (app/web_memory.py reopen)."""
    keys = [str(item["key"]) for item in review_items(state_dir) if ref in (item.get("items") or ())]
    for key in keys:
        review_set(state_dir, key, "expired", by=by, now=now, **({"token": token} if token else {}))
    return keys


# -------------------------------------------------------------------------------------------------------- legacy sync


def _sig(path: Path):
    try:
        stat = path.stat()
    except OSError:
        return None
    return [stat.st_mtime_ns, stat.st_size]


def legacy_sources(state_dir) -> dict:
    """name -> [mtime_ns, size] (None: no file) of what an older release writes that ``sync_legacy`` reads."""
    state = Path(state_dir)
    names = ["principles.json", "memory-candidates.json", "knowledge/owner.jsonl"]
    try:
        names += sorted(f"knowledge/workspaces/{p.name}" for p in (state / "knowledge" / "workspaces").iterdir() if p.suffix == ".jsonl")
    except OSError:
        pass
    return {name: _sig(state / name) for name in names}


def legacy_changed(state_dir) -> bool:
    """Whether an older release wrote a global store since the last ``sync_legacy`` (a few stats, whatever the topics)."""
    synced = read_object(global_path(state_dir)).get("migrated_from")
    return not isinstance(synced, dict) or synced != legacy_sources(state_dir)


def _legacy_global(state_dir) -> list:
    """(alias, kind, said, status, text, why, at, extra) of every global entry an older release keeps: the 通用 principles
    and the 通用 knowledge entries (in any state)."""
    out = []
    for item in principles.load(state_dir)["owner"]:
        out.append((f"principle:{item['id']}", "rule", "owner", "active", item["text"], "", _number(item.get("at")),
                    {"pinned": True} if item.get("pinned") else {}))
    for scope, _, group in knowledge.groups(state_dir):
        if scope != knowledge.OWNER:
            continue
        for row in group.values():
            kind, said, status = kind3_of(row, source=KNOWLEDGE)
            extra = {"superseded_by_alias": f"knowledge:{row['superseded_by']}"} if row.get("superseded_by") else {}
            out.append((f"knowledge:{row['id']}", kind, said, status, row["text"], row.get("why") or "", _number(row.get("at")),
                        extra))
    return out


def _legacy_id(alias: str, taken) -> str:
    digest = hashlib.sha1(alias.encode()).hexdigest()
    for size in (6, 8, 10):
        gid = "g" + digest[:size]
        if gid not in taken:
            return gid
    return "g" + secrets.token_hex(5)


def sync_legacy(state_dir, *, now: float | None = None) -> dict:
    """Take what an older release keeps of the global memory into memory/global.json, again and again without loss:
    a principle or a knowledge entry not taken before becomes an entry (its old ref an alias, a code given); one taken
    before whose words or state the older release changed since (a rollback) has that change laid over it, unless the
    entry was changed here after it was taken (then it is left, and the change goes to 待审核 as a conflict); one the
    older release deleted is archived (restorable). Nothing an older release does not write is touched. The workspace
    groups go to 待审核. {added, updated, archived, reviewed}."""
    stamp = time.time() if now is None else now
    sources = legacy_sources(state_dir)
    found = {"added": 0, "updated": 0, "archived": 0, "reviewed": 0}
    legacy = _legacy_global(state_dir)
    principles_ok = principles.readable(state_dir)
    with shared_file_lock(global_path(state_dir)):
        data = read_for_write(global_path(state_dir))
        rows = [row for row in data.get("entries") or () if isinstance(row, dict)]
        by_alias = {alias: row for row in rows for alias in row.get("aliases") or ()}
        taken = {str(row.get("id")) for row in rows}
        seen = set()
        made = []
        for alias, kind, said, status, text, why, at, extra in legacy:
            seen.add(alias)
            sig = hashlib.sha1(json.dumps([text, why, status, kind]).encode()).hexdigest()[:12]
            row = by_alias.get(alias)
            if row is None:
                gid = _legacy_id(alias, taken)
                taken.add(gid)
                row = {"id": gid, "kind": kind, "title": auto_title(text, kind), "text": clean(text, limit_of(kind)),
                       "why": clean(why, TEXT_CHARS), "status": status, "said": said, "by": "owner",
                       "origin": {"at": at}, "aliases": [alias], "at": at,
                       "legacy": {"alias": alias, "sig": sig, "synced_at": stamp, **extra}}
                rows.append(row)
                by_alias[alias] = row
                made.append(row)
                found["added"] += 1
                continue
            mark = row.get("legacy") if isinstance(row.get("legacy"), dict) else {}
            if mark.get("sig") == sig:
                continue
            if max(_number(row.get("edited_at")), _number(row.get("status_at"))) > _number(mark.get("synced_at")):
                found["reviewed"] += _conflict(state_dir, row, text, kind, stamp)
            else:
                row.update(text=clean(text, limit_of(kind)), why=clean(why, TEXT_CHARS), kind=kind)
                if row.get("status") != "void":
                    row["status"] = status
                found["updated"] += 1
            row["legacy"] = {**mark, "sig": sig, "synced_at": stamp}
        for row in rows:
            mark = row.get("legacy") if isinstance(row.get("legacy"), dict) else {}
            alias = mark.get("alias")
            if alias and alias not in seen and not mark.get("gone") and row.get("status") == "active":
                if alias.startswith("principle:") and not principles_ok:
                    continue  # A principles.json that cannot be read deletes nothing.
                row["status"] = "archived"
                row["legacy"] = {**mark, "gone": stamp}
                found["archived"] += 1
        for row in rows:  # A knowledge entry superseded by another: its successor's ref.
            follow = (row.get("legacy") or {}).get("superseded_by_alias")
            if follow and follow in by_alias and not row.get("superseded_by"):
                row["superseded_by"] = f"global:{by_alias[follow]['id']}"
        given = assign(state_dir, [(f"global:{row['id']}", row.get("kind") or "rule") for row in rows if not row.get("code")],
                       aliases={alias: f"global:{row['id']}" for row in made for alias in row["aliases"]})
        for row in rows:
            row["code"] = row.get("code") or given.get(f"global:{row['id']}", "")
        write_json(global_path(state_dir), {**data, "version": VERSION, "entries": rows, "migrated_from": sources,
                                            "migrated_at": data.get("migrated_at") or stamp, "synced_at": stamp},
                   ensure_ascii=False, indent=1)
    found["reviewed"] += _legacy_reviews(state_dir, stamp)
    if any(found.values()):
        touch_rev(state_dir, stamp)
    return found


def _conflict(state_dir, row, text, kind, stamp) -> int:
    try:
        _, new = review_put(state_dir, {"type": "conflict", "title": auto_title(text, kind), "text": text,
                                        "suggest": {"kind": kind, "scope": "global"}, "conflict_with": f"global:{row['id']}",
                                        "origin": {"source": "rollback"}}, now=stamp)
    except (OSError, ValueError):
        return 0
    return int(new)


def _legacy_reviews(state_dir, stamp) -> int:
    """The workspace principles and knowledge entries put in 待审核 (once each)."""
    made = 0
    proposals = []
    for name, group in principles.load(state_dir)["workspaces"].items():
        for item in group:
            proposals.append({"type": "migrate", "title": auto_title(item["text"]), "text": item["text"],
                              "suggest": {"kind": "rule", "scope": "global"}, "origin": {"workspace": name},
                              "items": [f"principle:{item['id']}"]})
    for scope, name, group in knowledge.groups(state_dir):
        if scope == knowledge.OWNER:
            continue
        for row in group.values():
            if row["state"] != knowledge.CURRENT:
                continue
            kind, _, _ = kind3_of(row, source=KNOWLEDGE)
            proposals.append({"type": "migrate", "title": auto_title(row["text"]), "text": row["text"],
                              "suggest": {"kind": kind, "scope": "global"}, "origin": {"workspace": name},
                              "items": [f"knowledge:{row['id']}"]})
    try:
        made = sum(1 for _, new in review_put_many(state_dir, proposals, now=stamp) if new)
    except (OSError, ValueError):
        pass
    return made


def mark_migrated(state_dir, **fields) -> None:
    """``fields`` merged into memory/global.json's top level (what the migration saw), under its lock."""
    with shared_file_lock(global_path(state_dir)):
        data = read_for_write(global_path(state_dir))
        data.setdefault("version", VERSION)
        data.setdefault("entries", [])
        data.update(fields)
        write_json(global_path(state_dir), data, ensure_ascii=False, indent=1)


def project_sources(state_dir) -> dict:
    """project id -> [mtime_ns, size] of its memory.json (None: none), of every project: a few stats."""
    return {pid: _sig(project_memory.memory_path(state_dir, pid)) for pid in _project_ids(state_dir)}


# -------------------------------------------------------------------------------------------------------- export


def export_legacy(state_dir, *, now: float | None = None) -> dict:
    """What an older release would not see written back into its own stores, for a rollback: the global rules into
    principles.json (one there already updated), the global notes as knowledge entries, the decisions of decisions.jsonl as
    ``owner_answer`` lines of the project's memory.json (marked ``mirror_of`` in the sidecar so they are not read twice). Each
    written back gets the old id as an alias, so a later migration does not take it again. {principles, knowledge, decisions,
    left} (left: what did not fit an older release's limits)."""
    stamp = time.time() if now is None else now
    out = {"principles": 0, "knowledge": 0, "decisions": 0, "left": 0}
    for entry in entries(state_dir, scope="global", statuses=("active",)):
        legacy = [alias for alias in entry.aliases if alias.startswith(("principle:", "knowledge:"))]
        if legacy:
            continue
        try:
            if entry.kind == "rule":
                item, _ = principles.save(state_dir, principles.OWNER, entry.text[:principles.TEXT_LIMIT])
                alias = f"principle:{item['id']}"
                out["principles"] += 1
            else:
                item, _ = knowledge.save(state_dir, knowledge.OWNER, "fact", entry.text[:knowledge.TEXT_LIMIT], why=entry.why,
                                         by="import")
                alias = f"knowledge:{item['id']}"
                out["knowledge"] += 1
        except ValueError:
            out["left"] += 1
            continue
        _set_global(state_dir, entry.ref, aliases=[*entry.aliases, alias], legacy={"alias": alias, "synced_at": stamp})
        assign(state_dir, [], aliases={alias: entry.ref})
    for pid in _project_ids(state_dir):
        for key, row in _decision_events(state_dir, "project", pid).items():
            if row.get("status") not in ("active", None) or row.get("mirrored"):
                continue
            entry = _decision_entry({**row, "id": key}, "project", pid)
            try:
                line, _ = project_memory.add(state_dir, pid, entry.text, "decision", "owner_answer", now=stamp)
            except (KeyError, ValueError):
                out["left"] += 1
                continue
            _put_sidecar(state_dir, "project", pid, {line["id"]: {"mirror_of": entry.ref}})
            with shared_file_lock(decisions_path(state_dir, "project", pid)):
                _append_line(decisions_path(state_dir, "project", pid),
                             {"v": VERSION, "op": "set", "id": key, "at": stamp, "fields": {"mirrored": line["id"]}})
            out["decisions"] += 1
    touch_rev(state_dir, stamp)
    return out


def _set_global(state_dir, ref, **fields) -> None:
    _, _, gid = parse_ref(ref)
    with shared_file_lock(global_path(state_dir)):
        data = read_for_write(global_path(state_dir))
        rows = data.get("entries") if isinstance(data.get("entries"), list) else []
        for row in rows:
            if isinstance(row, dict) and str(row.get("id")) == gid:
                row.update(fields)
        write_json(global_path(state_dir), {**data, "entries": rows}, ensure_ascii=False, indent=1)


# -------------------------------------------------------------------------------------------------------- upkeep


def trim_log(state_dir, before: float) -> int:
    """memory/log.jsonl without its lines older than ``before`` (core/maintenance_rules.py MemoryFiles); how many went."""
    path = log_path(state_dir)
    with shared_file_lock(path):
        rows = _lines(path)
        kept = [row for row in rows if _number(row.get("at")) >= before]
        if len(kept) == len(rows):
            return 0
        _write_lines(path, kept)
    return len(rows) - len(kept)


def compact_decisions(state_dir, scope: str, owner_key: str, before: float) -> int:
    """A decisions.jsonl written again with one line a decision (its events laid together), a void one done before ``before``
    left out; only when that leaves fewer lines. How many lines went."""
    path = decisions_path(state_dir, scope, owner_key)
    with shared_file_lock(path):
        events = _lines(path)
        found = _decision_events(state_dir, scope, owner_key)
        kept = [{**row, "op": "add", "id": key} for key, row in found.items()
                if not (row.get("status") == "void" and _number(row.get("done_at") or row.get("at")) < before)]
        for row in kept:
            row.pop("fields", None)
        if len(kept) >= len(events):
            return 0
        _write_lines(path, kept)
    return len(events) - len(kept)


def _write_lines(path: Path, rows) -> None:
    write_private_text(path, "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows))


def upkeep(state_dir, *, now: float | None = None) -> dict:
    """The daily upkeep of the memory files (core/maintenance_rules.py MemoryFiles): the log past LOG_DAYS trimmed, each
    project's and each indexed topic's decisions.jsonl compacted (a void decision past VOID_DAYS gone), the topic index
    made again (the one walk of the topic directories, never at start), the items of 待审核 left open REVIEW_DAYS closed as
    expired (``review_expire``, core/memory_review.py expire: kept, never deleted). {log, decisions, topics, expired}."""
    stamp = time.time() if now is None else now
    out = {"log": trim_log(state_dir, stamp - LOG_DAYS * 86400), "decisions": 0, "topics": 0,
           "expired": review_expire(state_dir, now=stamp)}
    out["topics"] = seed_topics(state_dir, now=stamp)
    owners = [("project", pid) for pid in _project_ids(state_dir)] + [("topic", tid) for tid in topics(state_dir)]
    for scope, owner in owners:
        if decisions_path(state_dir, scope, owner).is_file():
            out["decisions"] += compact_decisions(state_dir, scope, owner, stamp - VOID_DAYS * 86400)
    if out["log"] or out["decisions"]:
        touch_rev(state_dir, stamp)
    return out
