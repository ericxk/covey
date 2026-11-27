"""The owner's principles (docs/butler.md 主人的原则): long-standing rules and preferences the owner stated once,
so that every topic's turn and the butler's read them again (app/prompts.py principles_block) and the owner
need not repeat "按照之前的设计理念". The butler records them with its tools (app/butler_tools.py principle_save,
principle_delete, principles_list); the 设置 card's 记忆 tab (app/memory_panel.py, COVEY_MEMORY=1) shows them,
deletes them (``delete``) and marks them 常驻 (``pin``).

<state>/principles.json::

    {"version": 1,
     "owner": [principle, ...],                      # 通用: every topic and the butler
     "workspaces": {"<workspace name>": [principle, ...]}}  # a topic in that workspace, the butler in its own

    principle: {"id": "p1a2b3c", "text": "≤200 字，一行", "at": "2026-11-08T12:00:00Z",
                "source": {"job_key": "<管家任务键>", "job_id": 12}, "pinned": true, "edited": "2026-11-24T14:00:00Z"}

Each group keeps at most GROUP_LIMIT, oldest first. ``pinned`` (常驻, absent: not) is the owner's mark from the 设置
card's 记忆 tab (``pin``, app/memory_panel.py): a pinned principle is never left out of a turn for want of room
(core/memory.py select, app/prompts.py principles_block); at most PINNED_LIMIT in a group. ``edited`` (absent: never) is when the
owner last changed its words on the web page (``edit``, app/web_memory.py); ``at`` stays when it was first noted.

A write reads, changes and replaces the whole file (core/state.py write_json) under its shared file lock (core/locks.py,
across processes): the Bridge's butler tools and the 设置 card. Nothing inside the lock sends to Feishu. Readers take no lock: a replace is
atomic. A file that cannot be read, or holds anything else, reads as empty; the first write over it keeps it as
principles.json.bad-<epoch> rather than lose it. Standard library only.
"""
from __future__ import annotations

import os
import re
import secrets
import time
from pathlib import Path

from covey.core import memory_rev
from covey.core.locks import shared_file_lock
from covey.core.state import Unreadable, read_json, utc_now_iso, write_json

FILE = "principles.json"
VERSION = 1
OWNER, WORKSPACE = "owner", "workspace"
SCOPES = (OWNER, WORKSPACE)
TEXT_LIMIT = 200  # Characters of one principle.
GROUP_LIMIT = 30  # Principles in one group (通用, or one workspace).
PINNED_LIMIT = 8  # Pinned (常驻) principles in one group: they are carried whatever their room.
ID = re.compile(r"p[0-9a-f]{6}")
_END = "。.；;，,！!、 "


def path(state_dir) -> Path:
    return Path(state_dir) / FILE


def clean_text(text) -> str:
    """One line: whitespace runs, newlines too, become one space."""
    return " ".join(str(text or "").split())


def same_key(text) -> str:
    """What two principles that say the same thing share: the text without spaces, case or ending punctuation."""
    return "".join(clean_text(text).casefold().split()).rstrip(_END)


def _item(value):
    if not isinstance(value, dict):
        return None
    text = clean_text(value.get("text"))
    pid = value.get("id")
    if not text or not isinstance(pid, str) or not ID.fullmatch(pid):
        return None
    source = value.get("source") if isinstance(value.get("source"), dict) else {}
    item = {"id": pid, "text": text[:TEXT_LIMIT], "at": str(value.get("at") or ""), "source": dict(source)}
    if value.get("pinned") is True:
        item["pinned"] = True
    if isinstance(value.get("edited"), str) and value["edited"]:
        item["edited"] = value["edited"]
    return item


def _group(values):
    return [item for item in map(_item, values if isinstance(values, list) else []) if item]


def _parse(data):
    if not isinstance(data, dict):
        raise ValueError("not an object")
    spaces = data.get("workspaces") if isinstance(data.get("workspaces"), dict) else {}
    return {"version": VERSION, "owner": _group(data.get("owner")),
            "workspaces": {str(name): group for name, group in ((n, _group(g)) for n, g in spaces.items()) if group}}


def _empty():
    return {"version": VERSION, "owner": [], "workspaces": {}}


def _read(state_dir, *, writing=False):
    """(data, bad): ``bad`` when the file is there but cannot be read as principles. With ``writing`` (a write under the
    lock follows) a file that cannot be read at all raises core/state.py Unreadable, so a network disk's passing error
    leaves it as it is; one that reads but is no principles file is ``bad`` (moved aside by ``_write``)."""
    try:
        return _parse(read_json(path(state_dir), None) or _empty()), False
    except OSError as exc:
        if writing:
            raise Unreadable(*exc.args) from exc
        return _empty(), True
    except ValueError:
        return _empty(), True


def readable(state_dir) -> bool:
    """Whether principles.json is missing or reads as principles (core/memstore.py sync_legacy deletes nothing from a bad one)."""
    return not _read(state_dir)[1]


def load(state_dir) -> dict:
    """{"version", "owner": [...], "workspaces": {name: [...]}}, each group oldest first; empty for a bad file."""
    return _read(state_dir)[0]


def for_turn(state_dir, workspace_name) -> tuple[list, list]:
    """(通用, the workspace's) principles a turn in ``workspace_name`` reads."""
    data = load(state_dir)
    return data["owner"], list(data["workspaces"].get(str(workspace_name or ""), [])) if workspace_name else []


def entries(state_dir) -> list[dict]:
    """Every principle with its group, 通用 first then each workspace by name: {"scope", "workspace", "id", "text",
    "at", "source"}. For principles_list and the settings card."""
    data = load(state_dir)
    rows = [{"scope": OWNER, "workspace": "", **item} for item in data["owner"]]
    for name in sorted(data["workspaces"]):
        rows += [{"scope": WORKSPACE, "workspace": name, **item} for item in data["workspaces"][name]]
    return rows


def _write(state_dir, data, bad, now):
    file = path(state_dir)
    if bad and file.exists():
        os.replace(file, file.with_name(f"{FILE}.bad-{int(now)}"))
    write_json(file, data, ensure_ascii=False, indent=1)
    memory_rev.touch(state_dir, now, create=False)


def _new_id(data):
    taken = {item["id"] for group in (data["owner"], *data["workspaces"].values()) for item in group}
    while True:
        pid = "p" + secrets.token_hex(3)
        if pid not in taken:
            return pid


def save(state_dir, scope, text, *, workspace="", source=None, now=None) -> tuple[dict, bool]:
    """(the principle, whether it is new). One that says the same as one in its group (``same_key``) is not added
    again: that one comes back, not new. ValueError says why nothing was written."""
    text = clean_text(text)
    workspace = clean_text(workspace)
    if scope not in SCOPES:
        raise ValueError("scope 只能是 owner（通用）或 workspace（某个工作区）")
    if not text:
        raise ValueError("原则的正文是空的")
    if len(text) > TEXT_LIMIT:
        raise ValueError(f"一条原则最多 {TEXT_LIMIT} 字，这条 {len(text)} 字：概括成一句再记")
    if scope == WORKSPACE and not workspace:
        raise ValueError("工作区的原则要写是哪个工作区（workspaces_list 里的名字）")
    now = time.time() if now is None else now
    with shared_file_lock(path(state_dir)):
        data, bad = _read(state_dir, writing=True)
        group = data["owner"] if scope == OWNER else data["workspaces"].setdefault(workspace, [])
        key = same_key(text)
        for item in group:
            if same_key(item["text"]) == key:
                return dict(item), False
        if len(group) >= GROUP_LIMIT:
            where = "通用" if scope == OWNER else f"工作区 {workspace} "
            raise ValueError(f"{where}的原则已有 {GROUP_LIMIT} 条：先问主人删掉哪条（principle_delete），再记新的")
        item = {"id": _new_id(data), "text": text, "at": utc_now_iso(), "source": dict(source or {})}
        group.append(item)
        _write(state_dir, data, bad, now)
    return dict(item), True


def pin(state_dir, pid, on, *, now=None) -> dict | None:
    """Mark the principle ``pid`` 常驻 (``on``) or not: it, with its "scope" and "workspace", as it is now; None when
    there is none. ValueError says why nothing was written (PINNED_LIMIT in its group already)."""
    pid = str(pid or "").strip()
    with shared_file_lock(path(state_dir)):
        data, bad = _read(state_dir, writing=True)
        groups = [(OWNER, "", data["owner"]), *((WORKSPACE, name, group) for name, group in data["workspaces"].items())]
        for scope, name, group in groups:
            for item in group:
                if item["id"] != pid:
                    continue
                if bool(item.get("pinned")) != bool(on):
                    if on and sum(1 for other in group if other.get("pinned")) >= PINNED_LIMIT:
                        where = "通用" if scope == OWNER else f"工作区 {name} "
                        raise ValueError(f"{where}的常驻已有 {PINNED_LIMIT} 条：先取消一条再标")
                    if on:
                        item["pinned"] = True
                    else:
                        item.pop("pinned", None)
                    _write(state_dir, data, bad, time.time() if now is None else now)
                return {"scope": scope, "workspace": name, **item}
    return None


def edit(state_dir, pid, text, *, base=None, now=None) -> dict | None:
    """New words for the principle ``pid`` (its id, group, 常驻 and ``at`` stay; ``edited`` is now): it, with its "scope" and
    "workspace", as it is now; None when there is none. ``base``, when given, is the text the editor started from: a principle that
    no longer reads so was changed elsewhere meanwhile and is left as it is. Words that are already its own write nothing.
    ValueError says why nothing was written."""
    text = clean_text(text)
    if not text:
        raise ValueError("原则的正文是空的")
    if len(text) > TEXT_LIMIT:
        raise ValueError(f"一条原则最多 {TEXT_LIMIT} 字，这条 {len(text)} 字：概括成一句再记")
    pid = str(pid or "").strip()
    with shared_file_lock(path(state_dir)):
        data, bad = _read(state_dir, writing=True)
        groups = [(OWNER, "", data["owner"]), *((WORKSPACE, name, group) for name, group in data["workspaces"].items())]
        for scope, name, group in groups:
            for item in group:
                if item["id"] != pid:
                    continue
                if base is not None and clean_text(base) != item["text"]:
                    raise ValueError("这条刚在别处改过，先看看现在的内容再改")
                if item["text"] != text:
                    key = same_key(text)
                    if any(other["id"] != pid and same_key(other["text"]) == key for other in group):
                        raise ValueError("这一组已有说同样意思的一条，不重复记")
                    item["text"], item["edited"] = text, utc_now_iso()
                    _write(state_dir, data, bad, time.time() if now is None else now)
                return {"scope": scope, "workspace": name, **item}
    return None


def delete(state_dir, pid, *, now=None) -> dict | None:
    """Remove the principle ``pid``: it, with its "scope" and "workspace", or None when there is none."""
    pid = str(pid or "").strip()
    with shared_file_lock(path(state_dir)):
        data, bad = _read(state_dir, writing=True)
        groups = [(OWNER, "", data["owner"]), *((WORKSPACE, name, group) for name, group in data["workspaces"].items())]
        for scope, name, group in groups:
            for index, item in enumerate(group):
                if item["id"] == pid:
                    del group[index]
                    if scope == WORKSPACE and not group:
                        del data["workspaces"][name]
                    _write(state_dir, data, bad, time.time() if now is None else now)
                    return {"scope": scope, "workspace": name, **item}
    return None
