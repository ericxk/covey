"""知识条目 (docs/butler.md 知识库): short entries the owner asked to be remembered ("记一下"), kept in one place so
every Harness reads them (app/prompts.py knowledge_block: a new topic's first turn carries them) and the butler
searches and changes them (app/butler_knowledge.py knowledge_save, knowledge_update, knowledge_drop, knowledge_search).
Off unless COVEY_KNOWLEDGE=1: without it nothing here is called, no directory is made and no prompt, tool list
or digest changes.

<state>/knowledge/::

    owner.jsonl, owner.md                    # 通用: events appended / the current entries, generated
    workspaces/<workspace>.jsonl, .md        # one group per workspace
    owner-brief.md                           # generated: the injected summary (KNOWLEDGE_CHARS), all groups

An event is one JSON line, never rewritten::

    {"v":1,"op":"add","id":"k1a2b3c","at":"2026-11-12T10:00:00+08:00","by":"main",
     "source":{"topic":"T483","job_key":"…","job_id":12},"workspace":"",
     "entry":{"kind":"decision","text":"≤200 字","why":""}}

``op`` is ``add``; ``update`` (a new ``id`` with ``replaces``: the old one turns superseded); ``drop`` (``id`` is the
entry dropped, ``reason`` is required). ``by`` is main (the butler's tool), topic (a topic's turn, collected by
the Bridge from its checkpoint's ``remember``: app/knowledge_collect.py) or import. A group is read by replaying its
file from the top: every entry ends current, superseded or dropped. A line that is not an event is skipped; a file
that cannot be read at all reads as empty and the first write after it keeps it as <file>.bad-<epoch>.

A write appends one line under the group file's shared lock (core/locks.py, across processes), then replaces the
generated .md files; the lock is never held over a Feishu call or a subprocess. A generated file that cannot be
written does not undo the event. Readers take no lock. An entry that says the same as a current one of its group
(``same_key``) is not added again. Standard library only (with core/locks.py and core/state.py).
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import secrets
import time
from pathlib import Path

from covey.core import env as cenv
from covey.core.locks import shared_file_lock
from covey.core.state import epoch_seconds, write_private_text

ENV = "COVEY_KNOWLEDGE"
HISTORY_ENV = "COVEY_HISTORY_CMD"
DIR = "knowledge"
VERSION = 1
OWNER, WORKSPACE = "owner", "workspace"
SCOPES = (OWNER, WORKSPACE)
KINDS = {"decision": "决定", "practice": "做法", "pitfall": "坑", "fact": "事实", "question": "待定"}
BY = ("main", "topic", "import")
TEXT_LIMIT = 200  # Characters of one entry (and of its reason ``why``).
KNOWLEDGE_CHARS = 1500  # Of the lines injected into a turn (app/prompts.py knowledge_block) and of owner-brief.md.
SEARCH_LIMIT = 20  # Rows of one search.
ID = re.compile(r"k[0-9a-f]{6}")
_END = "。.；;，,！!、 "
CURRENT, SUPERSEDED, DROPPED = "current", "superseded", "dropped"
STATE_WORDS = {CURRENT: "当前", SUPERSEDED: "已取代", DROPPED: "已作废"}


def enabled(environ=None) -> bool:
    """Whether COVEY_KNOWLEDGE=1: the owner's own deployment only (read on every call)."""
    return str(cenv.get(ENV, "", environ)).strip() == "1"


def history_cmd(environ=None) -> str:
    """The history search command (COVEY_HISTORY_CMD): its absolute path; '' when not set or not absolute."""
    value = str(cenv.get(HISTORY_ENV, "", environ)).strip()
    return value if os.path.isabs(value) else ""


def root(state_dir) -> Path:
    return Path(state_dir) / DIR


def clean_text(text) -> str:
    """One line: whitespace runs, newlines too, become one space."""
    return " ".join(str(text or "").split())


def same_key(text) -> str:
    """What two entries that say the same thing share: the text without spaces, case or ending punctuation."""
    return "".join(clean_text(text).casefold().split()).rstrip(_END)


def slug(name) -> str:
    """A file name for workspace ``name``: itself when it is plain, else made plain with a short hash after it."""
    name = clean_text(name)
    plain = re.sub(r"[^\w.\-]", "_", name)[:60].lstrip(".")
    if plain and plain == name:
        return plain
    return (plain or "ws") + "-" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:6]


def group_file(state_dir, scope, workspace="") -> Path:
    base = root(state_dir)
    return base / "owner.jsonl" if scope == OWNER else base / "workspaces" / (slug(workspace) + ".jsonl")


def md_path(state_dir, scope, workspace="") -> Path:
    return group_file(state_dir, scope, workspace).with_suffix(".md")


def brief_path(state_dir) -> Path:
    return root(state_dir) / "owner-brief.md"


def _now():
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def epoch(at) -> float:
    return epoch_seconds(at) or 0.0


def _events(file: Path):
    """(events, bad): the lines of ``file`` that are events; ``bad`` when it is there but cannot be read."""
    try:
        text = file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [], False
    except (OSError, UnicodeDecodeError):
        return [], True
    found = []
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("op") in ("add", "update", "drop") and isinstance(event.get("id"), str):
            found.append(event)
    return found, False


def _entry_of(event, scope, workspace):
    body = event.get("entry") if isinstance(event.get("entry"), dict) else {}
    kind = body.get("kind") if body.get("kind") in KINDS else "fact"
    text = clean_text(body.get("text"))[:TEXT_LIMIT]
    if not ID.fullmatch(event["id"]) or not text:
        return None
    source = event.get("source") if isinstance(event.get("source"), dict) else {}
    return {"id": event["id"], "scope": scope, "workspace": workspace, "kind": kind, "text": text,
            "why": clean_text(body.get("why"))[:TEXT_LIMIT], "at": str(event.get("at") or ""),
            "by": event.get("by") if event.get("by") in BY else "main", "source": dict(source),
            "state": CURRENT, "replaces": "", "superseded_by": "", "reason": "", "ended_at": ""}


def replay(events, scope, workspace="") -> dict:
    """id -> entry (oldest first) after ``events`` in order: an update turns the entry it replaces superseded, a drop
    turns a current one dropped (its reason and time kept); anything that names no entry changes nothing."""
    entries = {}
    for event in events:
        op = event["op"]
        if op == "drop":
            entry = entries.get(event["id"])
            if entry is not None and entry["state"] == CURRENT:
                entry.update(state=DROPPED, reason=clean_text(event.get("reason")), ended_at=str(event.get("at") or ""))
            continue
        entry = _entry_of(event, scope, workspace)
        if entry is None or entry["id"] in entries:
            continue
        if op == "update":
            old = entries.get(str(event.get("replaces") or ""))
            entry["replaces"] = str(event.get("replaces") or "")
            entry["reason"] = clean_text(event.get("reason"))
            if old is not None and old["state"] == CURRENT:
                old.update(state=SUPERSEDED, superseded_by=entry["id"], ended_at=entry["at"])
        entries[entry["id"]] = entry
    return entries


def newest_first(rows) -> list:
    """``rows`` (entries in the order they were written) newest first; entries written in the same second keep
    the later-written first."""
    return sorted(reversed(list(rows)), key=lambda e: -epoch(e["at"]))


def oldest_first(rows) -> list:
    return sorted(rows, key=lambda e: epoch(e["at"]))


def _workspace_of(events, file: Path) -> str:
    for event in events:
        if isinstance(event.get("workspace"), str) and event["workspace"]:
            return event["workspace"]
    return file.stem


def _files(state_dir):
    """[(scope, file)] of the groups that have a file now: 通用 first, then each workspace by file name."""
    base = root(state_dir)
    found = [(OWNER, base / "owner.jsonl")] if (base / "owner.jsonl").is_file() else []
    try:
        names = sorted(path for path in (base / "workspaces").iterdir() if path.suffix == ".jsonl" and path.is_file())
    except OSError:
        names = []
    return found + [(WORKSPACE, path) for path in names]


def groups(state_dir) -> list[tuple[str, str, dict]]:
    """[(scope, workspace, {id: entry})] of every group with a file; 通用 has workspace ''."""
    found = []
    for scope, file in _files(state_dir):
        events, _ = _events(file)
        workspace = "" if scope == OWNER else _workspace_of(events, file)
        found.append((scope, workspace, replay(events, scope, workspace)))
    return found


def entries(state_dir, *, states=(CURRENT,), workspace=None) -> list[dict]:
    """Entries in ``states`` of every group (``workspace`` given: 通用 and that workspace's only), the newest first."""
    rows = [entry for scope, name, group in groups(state_dir) for entry in group.values()
            if entry["state"] in states and (workspace is None or scope == OWNER or name == workspace)]
    return newest_first(rows)


def for_turn(state_dir, workspace_name) -> tuple[list, list]:
    """(通用, the workspace's) current entries a turn in ``workspace_name`` reads, each oldest first."""
    owner, space = [], []
    for scope, name, group in groups(state_dir):
        if scope == OWNER or (workspace_name and name == workspace_name):
            (owner if scope == OWNER else space).extend(e for e in group.values() if e["state"] == CURRENT)
    return oldest_first(owner), oldest_first(space)


def find(state_dir, kid):
    """The entry with id ``kid`` in any state, or None."""
    kid = str(kid or "").strip()
    for _, _, group in groups(state_dir):
        if kid in group:
            return group[kid]
    return None


def collected(state_dir, job_key) -> bool:
    """Whether a topic's turn ``job_key`` already wrote entries (any group): its checkpoint is read once."""
    if not job_key:
        return False
    for scope, file in _files(state_dir):
        for event in _events(file)[0]:
            source = event.get("source")
            if event.get("by") == "topic" and isinstance(source, dict) and source.get("job_key") == job_key:
                return True
    return False


def _new_id(state_dir):
    taken = {kid for _, _, group in groups(state_dir) for kid in group}
    while True:
        kid = "k" + secrets.token_hex(3)
        if kid not in taken:
            return kid


def _line(event):
    return json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"


def _append(file: Path, event, bad, now):
    file.parent.mkdir(parents=True, exist_ok=True)
    if bad and file.exists():
        os.replace(file, file.with_name(f"{file.name}.bad-{int(now)}"))
    glue = ""
    try:
        with open(file, "rb") as stream:
            stream.seek(0, os.SEEK_END)
            if stream.tell():
                stream.seek(-1, os.SEEK_END)
                glue = "" if stream.read(1) == b"\n" else "\n"  # A torn last line must not swallow this event.
    except FileNotFoundError:
        pass
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    fd = os.open(file, flags, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as stream:
        stream.write(glue + _line(event))
        stream.flush()
        os.fsync(stream.fileno())


def _check(scope, kind, text, why, workspace):
    text, why, workspace = clean_text(text), clean_text(why), clean_text(workspace)
    if scope not in SCOPES:
        raise ValueError("scope 只能是 owner（通用）或 workspace（某个工作区）")
    if kind not in KINDS:
        raise ValueError("kind 只能是 " + "、".join(f"{k}（{v}）" for k, v in KINDS.items()))
    if not text:
        raise ValueError("条目的正文是空的")
    if len(text) > TEXT_LIMIT:
        raise ValueError(f"一条最多 {TEXT_LIMIT} 字，这条 {len(text)} 字：概括成一句再记")
    if len(why) > TEXT_LIMIT:
        raise ValueError(f"原因最多 {TEXT_LIMIT} 字，这条 {len(why)} 字")
    if scope == WORKSPACE and not workspace:
        raise ValueError("工作区的条目要写是哪个工作区（workspaces_list 里的名字）")
    return text, why, workspace


def _write(state_dir, file, scope, workspace, make, now):
    """``make(group)`` -> (event, result), event None for nothing to append: the event is appended under the group's
    lock, the generated files redrawn after it."""
    file.parent.mkdir(parents=True, exist_ok=True)
    with shared_file_lock(file):
        events, bad = _events(file)
        group = replay(events, scope, workspace)
        event, result = make(group)
        if event is not None:
            _append(file, event, bad, now)
    if event is not None:
        refresh(state_dir)
    return result


def save(state_dir, scope, kind, text, *, workspace="", why="", source=None, by="main", now=None) -> tuple[dict, bool]:
    """(the entry, whether it is new). One that says the same as a current one of its group is not added again: that
    one comes back, not new. ValueError says why nothing was written."""
    text, why, workspace = _check(scope, kind, text, why, workspace)
    if by not in BY:
        raise ValueError("by 只能是 " + "、".join(BY))
    workspace = workspace if scope == WORKSPACE else ""
    now = time.time() if now is None else now
    kid = _new_id(state_dir)
    key = same_key(text)

    def make(group):
        for entry in group.values():
            if entry["state"] == CURRENT and same_key(entry["text"]) == key:
                return None, (dict(entry), False)
        event = {"v": VERSION, "op": "add", "id": kid, "at": _now(), "by": by, "source": dict(source or {}),
                 "workspace": workspace, "entry": {"kind": kind, "text": text, "why": why}}
        return event, (_entry_of(event, scope, workspace), True)

    return _write(state_dir, group_file(state_dir, scope, workspace), scope, workspace, make, now)


def update(state_dir, kid, text, *, kind=None, why=None, reason, source=None, by="main", now=None) -> dict:
    """Replace the current entry ``kid`` by a new one (its new id; ``kind`` and ``why`` stay as they were when not
    given). ValueError says why nothing was written."""
    old = find(state_dir, kid)
    if old is None:
        raise ValueError(f"没有 id 为 {str(kid)[:20]} 的条目：用 knowledge_search 找")
    if old["state"] != CURRENT:
        raise ValueError(f"{old['id']} 已经{STATE_WORDS[old['state']]}，不能再改"
                         + (f"：改用 {old['superseded_by']}" if old["superseded_by"] else ""))
    reason = clean_text(reason)
    if not reason:
        raise ValueError("要写改的原因（reason）")
    text, why, _ = _check(OWNER, kind or old["kind"], text, old["why"] if why is None else why, "")
    kind = kind or old["kind"]
    now = time.time() if now is None else now
    new_id = _new_id(state_dir)
    key = same_key(text)

    def make(group):
        current = group.get(old["id"])
        if current is None or current["state"] != CURRENT:
            raise ValueError(f"{old['id']} 刚被别处改动，用 knowledge_search 重新找")
        for entry in group.values():
            if entry["state"] == CURRENT and entry["id"] != old["id"] and same_key(entry["text"]) == key:
                raise ValueError(f"已有相同的条目 {entry['id']}，不重复记；要合并就作废其中一条")
        event = {"v": VERSION, "op": "update", "id": new_id, "replaces": old["id"], "at": _now(), "by": by,
                 "source": dict(source or {}), "workspace": old["workspace"], "reason": reason,
                 "entry": {"kind": kind, "text": text, "why": why}}
        return event, _entry_of(event, old["scope"], old["workspace"])

    result = _write(state_dir, group_file(state_dir, old["scope"], old["workspace"]), old["scope"], old["workspace"],
                    make, now)
    result["replaces"], result["reason"] = old["id"], reason
    return result


def drop(state_dir, kid, reason, *, source=None, by="main", now=None) -> dict:
    """Mark the current entry ``kid`` dropped: it, as it was. ValueError says why nothing was written."""
    old = find(state_dir, kid)
    if old is None:
        raise ValueError(f"没有 id 为 {str(kid)[:20]} 的条目：用 knowledge_search 找")
    if old["state"] != CURRENT:
        raise ValueError(f"{old['id']} 已经{STATE_WORDS[old['state']]}")
    reason = clean_text(reason)
    if not reason:
        raise ValueError("要写作废的原因（reason）")
    now = time.time() if now is None else now

    def make(group):
        current = group.get(old["id"])
        if current is None or current["state"] != CURRENT:
            raise ValueError(f"{old['id']} 刚被别处改动，用 knowledge_search 重新找")
        event = {"v": VERSION, "op": "drop", "id": old["id"], "at": _now(), "by": by, "source": dict(source or {}),
                 "reason": reason[:TEXT_LIMIT]}
        return event, dict(current)

    return _write(state_dir, group_file(state_dir, old["scope"], old["workspace"]), old["scope"], old["workspace"],
                  make, now)


def remember(state_dir, items, *, job_key, source, workspace) -> list[dict]:
    """A topic's turn's ``remember`` list (app/knowledge_collect.py) written as ``by=topic`` entries: at most five,
    each {kind, text, why, scope}; a slip (a kind not known, a text too long) is cut to what fits, an item that is
    nothing is left out. Nothing when ``job_key`` wrote entries before. The entries written (the new ones)."""
    if collected(state_dir, job_key):
        return []
    written = []
    for item in (items if isinstance(items, list) else [])[:5]:
        if not isinstance(item, dict):
            continue
        text = clean_text(item.get("text"))[:TEXT_LIMIT]
        kind = item.get("kind") if item.get("kind") in KINDS else "fact"
        scope = OWNER if item.get("scope") == OWNER or not workspace else WORKSPACE
        if not text:
            continue
        entry, new = save(state_dir, scope, kind, text, workspace=workspace, why=clean_text(item.get("why"))[:TEXT_LIMIT],
                          source=source, by="topic")
        if new:
            written.append(entry)
    return written


# Showing them.

def where_words(entry) -> str:
    return "通用" if entry["scope"] == OWNER else f"工作区 {entry['workspace']}"


def row(entry, *, state=False) -> str:
    """One line for the butler: id · group · kind · date · text · (why) · source T number."""
    parts = [entry["id"], where_words(entry), KINDS[entry["kind"]], entry["at"][:10] or "-"]
    text = entry["text"] + (f"（原因：{entry['why']}）" if entry["why"] else "")
    parts.append(text)
    if entry["source"].get("topic"):
        parts.append("来源 " + str(entry["source"]["topic"]))
    if state and entry["state"] != CURRENT:
        why = f"：{entry['reason']}" if entry["reason"] else ""
        parts.append(STATE_WORDS[entry["state"]] + (f"（改为 {entry['superseded_by']}）" if entry["superseded_by"] else "") + why)
    return " · ".join(parts)


def search(state_dir, query, *, workspace=None, history=False) -> list[dict]:
    """Entries matching ``query`` (words split by spaces; one is enough, more hits rank higher, a text, reason, id,
    kind word or source T number holding it), the current ones, with ``history`` the superseded and dropped too;
    ``workspace`` given: 通用 and that workspace's only. The best first, at most SEARCH_LIMIT; an empty query lists
    the newest."""
    words = [word.casefold() for word in clean_text(query).split()]
    rows = []
    for entry in entries(state_dir, states=tuple(STATE_WORDS) if history else (CURRENT,), workspace=workspace):
        hay = " ".join((entry["id"], entry["text"], entry["why"], KINDS[entry["kind"]], entry["kind"], entry["reason"],
                        str(entry["source"].get("topic") or ""))).casefold()
        hits = sum(word in hay for word in words)
        if hits or not words:
            rows.append((hits, entry))
    rows.sort(key=lambda item: -item[0])  # Stable: the newest first among equals.
    return [entry for _, entry in rows[:SEARCH_LIMIT]]


def changes(state_dir, since, until) -> list[dict]:
    """What was written in (``since``, ``until``] (epoch seconds), the newest first: {op, id, scope, workspace, text,
    at} for each add, update and drop (a drop with the text of the entry it dropped)."""
    found = []
    for scope, file in _files(state_dir):
        events, _ = _events(file)
        workspace = "" if scope == OWNER else _workspace_of(events, file)
        group = replay(events, scope, workspace)
        for event in events:
            at = epoch(event.get("at"))
            if not since < at <= until:
                continue
            entry = group.get(event["id"])
            if entry is None:
                continue
            found.append({"op": event["op"], "id": event["id"], "scope": scope, "workspace": workspace,
                          "text": entry["text"], "at": at})
    return sorted(found, key=lambda c: (c["at"], c["id"]), reverse=True)


def added_since(state_dir, workspace_name, since) -> tuple[list, list]:
    """for_turn's current entries that were added or rewritten after ``since`` (epoch seconds)."""
    owner, space = for_turn(state_dir, workspace_name)
    return [e for e in owner if epoch(e["at"]) > since], [e for e in space if epoch(e["at"]) > since]


# The generated files.

def entry_line(entry) -> str:
    return f"- [{KINDS[entry['kind']]}] {entry['text']}" + (f"（原因：{entry['why']}）" if entry["why"] else "")


def trim(rows, limit=KNOWLEDGE_CHARS) -> tuple[list, int]:
    """(``rows`` newest first up to ``limit`` characters of lines, how many were left out)."""
    kept, used = [], 0
    for entry in rows:
        size = len(entry_line(entry)) + 1
        if used + size > limit:
            break
        kept.append(entry)
        used += size
    return kept, len(rows) - len(kept)


def group_markdown(scope, workspace, group) -> str:
    title = "通用" if scope == OWNER else f"工作区 {workspace}"
    lines = [f"# 知识条目 · {title}", "", "> 由 Bridge 生成，不要手改；事件记在同名的 .jsonl 里。", ""]
    current = newest_first(e for e in group.values() if e["state"] == CURRENT)
    for kind, word in KINDS.items():
        rows = [e for e in current if e["kind"] == kind]
        if rows:
            lines += [f"## {word}", ""]
            for e in rows:
                tail = f" · 来源 {e['source']['topic']}" if e["source"].get("topic") else ""
                lines.append(f"- {e['id']} · {e['at'][:10]} · {e['text']}"
                             + (f"（原因：{e['why']}）" if e["why"] else "") + tail)
            lines.append("")
    if not current:
        lines.append("（现在没有当前条目）")
    return "\n".join(lines).rstrip() + "\n"


def brief_markdown(all_groups) -> str:
    rows = newest_first(e for _, _, group in all_groups for e in group.values() if e["state"] == CURRENT)
    kept, left = trim(rows)
    lines = ["# 主人的知识条目（摘要）", "", "> 由 Bridge 生成，不要手改；全文见同目录的 owner.md 和 workspaces/*.md。", ""]
    for e in kept:
        lines.append(entry_line(e) + ("" if e["scope"] == OWNER else f" ·{e['workspace']}"))
    if left:
        lines.append(f"另有 {left} 条较早的未列出。")
    if not kept:
        lines.append("（现在没有当前条目）")
    return "\n".join(lines) + "\n"


def refresh(state_dir) -> None:
    """Regenerate every group's .md and owner-brief.md from the event files. A file that cannot be written is left
    as it was: the events are the record."""
    try:
        everything = groups(state_dir)
        for scope, workspace, group in everything:
            write_private_text(md_path(state_dir, scope, workspace), group_markdown(scope, workspace, group))
        write_private_text(brief_path(state_dir), brief_markdown(everything))
    except OSError:
        pass
