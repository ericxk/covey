"""The one read port of the owner's memory (COVEY_MEMORY=1, docs/butler.md 记忆): the principles (core/principles.py), the
knowledge entries (core/knowledge.py), a project's memory (core/project_memory.py) and a topic's (core/topic_memory.py) become
one list of items, and one block of a turn's rules (app/prompts.py memory_block) says them. An item is
{ref, scope, by, text, at, redacted}:

- ``ref`` names the entry in its own store ("principle:<id>", "knowledge:<id>", "project:<project id>:<id>", "topic:<id>").
- ``scope`` is how far it reaches: 全局 (``GLOBAL``), 工作区 (``WORKSPACE``), 专项 (``PROJECT``), 话题 (``TOPIC``).
- ``by`` is who said it: ``OWNER`` (你说的: a principle and a knowledge entry are only noted when the owner asked; a project
  line of source owner, owner_answer or owner_fix) or ``LEAD`` (它总结的: the lead's own note, source lead or any other).
- ``redacted``: it came from a topic with redaction on; only such a topic reads it.

``select`` keeps, per scope, what fits the scope's character limit (``LIMITS``), the owner's words first, a whole entry or none (no
entry is cut in the middle), the newest first within each; the entries kept get a short number "M123" that is the same every
turn (a hash of the ref; one taken by another bumps to the next). Nothing here reads a file but through those stores, and nothing is
written. Standard library and core only."""
from __future__ import annotations

import hashlib
import os

from covey.core import knowledge
from covey.core.state import epoch_seconds

ENV = "COVEY_MEMORY"
GLOBAL, WORKSPACE, PROJECT, TOPIC = "global", "workspace", "project", "topic"
SCOPES = (GLOBAL, WORKSPACE, PROJECT, TOPIC)
WORDS = {GLOBAL: "全局", WORKSPACE: "这个工作区", PROJECT: "这个专项", TOPIC: "这个话题"}
LIMITS = {GLOBAL: 1200, WORKSPACE: 800, PROJECT: 2000, TOPIC: 800}  # Characters of a scope's lines.
OWNER, LEAD = "owner", "lead"
OWNER_WORDS = {OWNER: "你说的", LEAD: "它总结的"}
OWNER_SOURCES = ("owner", "owner_answer", "owner_fix")  # Of a project line: the owner's own words.


def enabled(environ=None) -> bool:
    return (os.environ if environ is None else environ).get(ENV) == "1"


def by_source(source) -> str:
    return OWNER if str(source or "") in OWNER_SOURCES else LEAD


def _at(value) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return float(epoch_seconds(value) or 0.0)


def make(ref, scope, by, text, at=0, redacted=False) -> dict:
    return {"ref": ref, "scope": scope, "by": by, "text": " ".join(str(text or "").split()), "at": _at(at),
            "redacted": bool(redacted)}


def items(*, principles=((), ()), entries=((), ()), project=(), topic=(), project_id="", redacted=False) -> list:
    """The items one turn may read: ``principles`` and ``entries`` are (全局, 工作区) lists as core/principles.py and
    core/knowledge.py for_turn give them (the caller leaves 全局 out of a turn that is not the owner's), ``project`` and ``topic``
    the live rows of those memories. A redacted line is only for a turn with ``redacted`` on."""
    found = []
    for scope, rows in zip((GLOBAL, WORKSPACE), principles):
        found += [make(f"principle:{row['id']}", scope, OWNER, row["text"], row.get("at")) for row in rows]
    for scope, rows in zip((GLOBAL, WORKSPACE), entries):
        for row in rows:
            text = f"{knowledge.KINDS[row['kind']]}：{row['text']}" + (f"（原因：{row['why']}）" if row.get("why") else "")
            found.append(make(f"knowledge:{row['id']}", scope, OWNER, text, row.get("at")))
    for row in project:
        found.append(make(f"project:{project_id}:{row['id']}", PROJECT, by_source(row.get("source")), row["text"], row.get("at"),
                          row.get("redacted")))
    for row in topic:
        found.append(make(f"topic:{row['id']}", TOPIC, by_source(row.get("source")), row["text"], row.get("at"), row.get("redacted")))
    return [item for item in found if item["text"] and (redacted or not item["redacted"])]


def number(ref, taken) -> str:
    """The short number of ``ref``: "M" and three digits from its hash, the next free one when ``taken`` has it."""
    value = 10 + int(hashlib.sha1(ref.encode()).hexdigest()[:8], 16) % 990
    while f"M{value}" in taken:
        value = 10 + (value - 9) % 990
    return f"M{value}"


def line_size(item) -> int:
    return len(item["text"]) + 24  # The number, the scope and the speaker around the text, about.


def select(found) -> tuple[list, int]:
    """(the items to carry, how many were left out): per scope up to its ``LIMITS``, the owner's words first, the newest first
    within each; the carried ones numbered (``n``) and listed by scope (全局 to 话题), within one the owner's words first, the oldest
    first."""
    kept, left = [], 0
    for scope in SCOPES:
        used = 0
        mine = sorted((item for item in found if item["scope"] == scope), key=lambda item: (item["by"] != OWNER, -item["at"], item["ref"]))
        for item in mine:
            if used + line_size(item) > LIMITS[scope]:
                left += 1
                continue
            used += line_size(item)
            kept.append(item)
    taken = set()
    for item in sorted(kept, key=lambda item: item["ref"]):
        item["n"] = number(item["ref"], taken)
        taken.add(item["n"])
    order = {scope: index for index, scope in enumerate(SCOPES)}
    kept.sort(key=lambda item: (order[item["scope"]], item["by"] != OWNER, item["at"], item["ref"]))
    return kept, left


def ids_of(kept) -> list:
    """What the job record's ``memory_ids`` says of the items a turn carried."""
    return [{"n": item["n"], "ref": item["ref"], "scope": item["scope"], "by": item["by"]} for item in kept]
