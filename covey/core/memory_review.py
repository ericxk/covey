"""The one review queue of the memory (待审核, ``memory/review.json``): what someone other than the owner proposes waits here.

What the memory's own learning would make reach past its topic, a conflict, a tidy proposal and the third same-class
question (class3) wait here for the owner's word; a lead's rule does not (it holds in its project at once, 负责人记的).
Items older releases put here (a lead's rule, an evening candidate, a Harness's import, a lead's line the migration
carried) stay readable and decidable until they are decided or expire: an open item EXPIRE_DAYS old is closed as expired
by the daily upkeep (core/memstore.py upkeep), kept, never deleted. Only the owner's submit moves an item out otherwise; a
lead answering for him never writes a rule (docs/butler.md 记忆, docs/operations.md 记忆的存储与迁移). The queue itself is
core/memstore.py's (memory/review.json); this module is what an item means when decided.
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field

from covey.core import memory_usage, memstore

TYPES = ("learn", "candidate", "lead", "import", "conflict", "tidy", "class3", "migrate")


@dataclass(frozen=True)
class ReviewItem:
    key: str
    type: str  # TYPES
    title: str
    text: str
    suggest: dict  # {kind, scope, project_key?}
    created_at: float
    quote: str = ""
    origin: dict = field(default_factory=dict)  # {topic_key?, topic_number?}
    conflict_with: str = ""
    items: tuple = ()  # A tidy proposal's group of refs.
    flags: tuple = ()  # core/memory_gate.py FLAGS


EXPIRE_DAYS = memstore.REVIEW_DAYS  # An open item older than this is ``expired`` (kept, never deleted).
# A scope's rules past either number are over the cap: (entries, characters).
RULE_CAPS = {"global": (15, 1200), "project": (10, 1000), "topic": (5, 400)}
ACTIONS = ("accept", "reject", "keep_left", "keep_right", "keep_both")
_BY = {"learn": "learn", "lead": "lead", "candidate": "bridge", "class3": "bridge", "migrate": "bridge"}
_FIELDS = ("key", "type", "title", "text", "suggest", "created_at", "quote", "origin", "conflict_with", "items", "flags")


def _item(row: dict) -> ReviewItem:
    return ReviewItem(**{name: (tuple(row[name]) if name in ("items", "flags") else row[name]) for name in _FIELDS if name in row})


def items(state_dir) -> list[ReviewItem]:
    """The open items, oldest first."""
    return [_item(row) for row in memstore.review_items(state_dir)]


def add(state_dir, item: ReviewItem) -> tuple[str, bool]:
    """Put ``item`` in the queue (one per proposal, ``memstore.review_fp``): (its key, whether it is new). One that is there
    already (open, turned down or accepted; not one that expired) gives the existing key and False: the caller does not say
    待审核 again for it."""
    row = {name: getattr(item, name) for name in _FIELDS if name != "key" or item.key}
    return memstore.review_put(state_dir, row)


def _need(state_dir, key: str) -> dict:
    row = next((row for row in memstore.review_items(state_dir, states=None) if row.get("key") == key), None)
    if row is None:
        raise ValueError("待审核里没有这一项")
    if row.get("state", "open") != "open":
        raise ValueError("这一项已经处理过了")
    return row


def _where(row: dict) -> tuple[str, str]:
    """(scope, owner key) an item's ``suggest`` says, a topic's key from its origin."""
    suggest = row.get("suggest") if isinstance(row.get("suggest"), dict) else {}
    scope = str(suggest.get("scope") or "global")
    owner = str(suggest.get("project_key") or suggest.get("owner_key") or "")
    if scope == "topic" and not owner:
        owner = str((row.get("origin") or {}).get("topic_key") or "")
    return scope, ("" if scope == "global" else owner)


def _add(state_dir, row: dict, who, token, now, *, by: str, text: str = "") -> list[str]:
    suggest = row.get("suggest") if isinstance(row.get("suggest"), dict) else {}
    scope, owner = _where(row)
    entry = memstore.add(state_dir, kind=str(suggest.get("kind") or "rule"), scope=scope, owner_key=owner,
                         text=text or row.get("text") or "", title=row.get("title") or "", said="owner", by=by,
                         origin=dict(row.get("origin") or {}), who=who, token=token, now=now)
    return [entry.ref]


def _conflict(state_dir, row: dict, action: str, who, token, now) -> list[str]:
    old = memstore.find(state_dir, row.get("conflict_with") or "")
    if action == "keep_left":
        return [old.ref] if old else []
    if old is None:
        return _add(state_dir, row, who, token, now, by="bridge") if action in ("keep_right", "keep_both") else []
    if action == "keep_right":  # The old entry takes the new words: one edit, one undo.
        return [memstore.edit(state_dir, old.ref, text=row.get("text") or "", who=who, token=token, now=now).ref]
    suggest = {"kind": old.kind, "scope": old.scope, "owner_key": old.owner_key}  # keep_both: the new words an entry of their own.
    return _add(state_dir, {**row, "suggest": suggest}, who, token, now, by="bridge")


def _written(state_dir, row: dict, action: str, who, token, now) -> list[str]:
    """What the owner's ``action`` on item ``row`` writes; the refs written."""
    kind, refs = row["type"], [str(ref) for ref in row.get("items") or ()]
    suggest = row.get("suggest") if isinstance(row.get("suggest"), dict) else {}
    if kind == "conflict":
        return _conflict(state_dir, row, {"accept": "keep_right", "reject": "keep_left"}.get(action, action), who, token, now)
    if action not in ("accept", "reject"):
        raise ValueError("这一项只能采纳或不要")
    if kind == "migrate" and suggest.get("scope") == "project":  # The guardrail's: a lead's line carried as a rule.
        written = []
        for ref in refs:
            if action == "accept":
                written.append(memstore.confirm(state_dir, ref, who=who, token=token, now=now).ref)
            else:
                written.append(memstore.set_kind(state_dir, ref, "note", who=who, token=token, now=now).ref)
        return written
    if kind == "tidy":
        written = []
        if action == "reject":
            for ref in refs:
                written.append(memstore.restore(state_dir, ref, who=who, token=token, now=now).ref)
        return written
    if action == "reject":
        return []
    return _add(state_dir, row, who, token, now, by=_BY.get(kind, "bridge"))


def decide(state_dir, key: str, action: str, *, who: str = "owner", token: str = "", now: float | None = None) -> dict:
    """The owner's ``action`` (``ACTIONS``) on item ``key``, one memstore token over every write so one ``memstore.undo`` takes
    it all back: {ok, key, state (accepted/rejected), refs written, undo_token}. ValueError (a Chinese reason) for a key that
    is not there or was decided already. Only the owner's submit gets here; a lead answering for him never writes a rule.
    Each entry written counts as a strong sign of use (``memory_usage.signal`` confirm)."""
    if action not in ACTIONS:
        raise ValueError("不认识这个操作")
    row = _need(state_dir, key)
    token = token or "u-" + secrets.token_hex(6)
    stamp = time.time() if now is None else now
    refs = _written(state_dir, row, action, who, token, stamp)
    if refs:
        memory_usage.signal(state_dir, refs, "confirm")
    state = "rejected" if action in ("reject", "keep_left") else "accepted"
    # The token on the item too: an undo of it (app/web_memory.py reopen) puts the item back in 待审核.
    memstore.review_set(state_dir, key, state, by=who, now=stamp, refs=refs, token=token)
    return {"ok": True, "key": key, "state": state, "refs": refs, "undo_token": token}


def decide_all(state_dir, action: str, *, keys: list[str] | None = None, who: str = "owner", token: str = "",
               now: float | None = None) -> dict:
    """``accept_all`` / ``reject_all`` on ``keys`` (every open item when empty), under one token: {ok, decided, failed [{key,
    error}], refs, undo_token}."""
    if action not in ("accept_all", "reject_all"):
        raise ValueError("不认识这个操作")
    token = token or "u-" + secrets.token_hex(6)
    wanted = list(keys) if keys else [item.key for item in items(state_dir)]
    out = {"ok": True, "decided": [], "failed": [], "refs": [], "undo_token": token}
    for key in wanted:
        try:
            done = decide(state_dir, key, action.split("_")[0], who=who, token=token, now=now)
        except (ValueError, OSError) as exc:
            out["failed"].append({"key": key, "error": str(exc)})
            continue
        out["decided"].append(key)
        out["refs"] += done["refs"]
    return out


def expire(state_dir, *, now: float | None = None) -> int:
    """Open items older than ``EXPIRE_DAYS`` marked ``expired`` (kept, never deleted); how many. The daily upkeep does it
    (core/memstore.py upkeep, the same ``review_expire``)."""
    return memstore.review_expire(state_dir, days=EXPIRE_DAYS, now=now)
