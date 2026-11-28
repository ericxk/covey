"""The one read port of the owner's memory (COVEY_MEMORY=1, docs/butler.md 记忆): the principles (core/principles.py), the
knowledge entries (core/knowledge.py), a project's memory (core/project_memory.py) and a topic's (core/topic_memory.py) become
one list of items, and one block of a turn's rules (app/prompts.py memory_block) says them. An item is
{ref, scope, by, text, at, redacted, pinned} and, of a project, what ``describe`` says of its line too:

- ``ref`` names the entry in its own store ("principle:<id>", "knowledge:<id>", "project:<project id>:<id>", "topic:<id>").
- ``scope`` is how far it reaches: 全局 (``GLOBAL``), 工作区 (``WORKSPACE``), 专项 (``PROJECT``), 话题 (``TOPIC``).
- ``by`` is who said it: ``OWNER`` (你说的: a principle and a knowledge entry are only noted when the owner asked; a project
  line the owner asserted) or ``LEAD`` (它总结的: the lead's own note, or any other).
- Of a project's line (``describe``, docs/architecture.md 参与者、交付与来源): who said it (``asserted_by``) and who wrote it down
  (``recorded_by``), from its meta or, for a line without, its source (``SOURCE_PARTIES``), never from what a model says; its
  ``origin``; its ``level``, only ever core/turn_source.py provenance's (the owner's with an origin the Bridge took in, else an
  agent's); its ``layer`` (``RULE`` 长期约定, ``DECISION`` 当前决定, ``HISTORY`` 历史记录), ``applies`` and ``valid``; and the
  ``part`` a turn carries it in, each with its own room (``PART_LIMITS``): 长期约定 (the owner's rules), 当前决定 (the owner's
  decisions: an answer is one, for what that question asked), 待核对参考 (``REFERENCE``: what anybody but the owner noted, below
  the owner's words and never by itself the ground of an answer in the owner's place). A line of the history is not carried.
- ``redacted``: it came from a topic with redaction on; only such a topic reads it.
- ``pinned``: a principle the owner marked 常驻 (core/principles.py ``pin``).

``select`` keeps, per scope, what fits the scope's character limit (``LIMITS``; a project's three parts each their own, the room
one leaves unused lent to the others, the rules first, then the decisions, the 待核对参考 last and up to a cap): the pinned ones first whatever their room (never
crowded out), then the owner's words first, a whole entry or none (no entry is cut in the middle), the newest first within each
(of the lead's notes, its decisions and constraints before its facts and contacts); the entries kept get a short number "M123"
that is the same every turn (a hash of the ref; one taken by another bumps to the next). ``fill`` is the same choice scope by
scope, with the room used and what was left out: the 设置 card's 记忆 tab says it (app/memory_panel.py) rather than leave it to
the one line the block ends with. ``project_view`` is a project's memory as every reader takes it, a member topic's turn, the
lead's state pack and the web page alike: the same items, parts and choice, its version, and the lines voided since the reader's
previous turn. Nothing here reads a file but through those stores, and nothing is written. Standard library and core only.

With COVEY_MEMORY_LEARN=1 as well (``learning``, docs/butler.md 记忆自己长) two more things count. ``drafts``: what the Bridge
distilled from the owner's repeated words and has not been confirmed or declined (an older release's evening candidates;
none are made any more), an item of 它总结的 of 全局 or 工作区 (``ref`` "candidate:<id>"), always after his own words. ``used``: when a turn last used an
item (core/memory_usage.py, ref -> epoch seconds): an item's ``seen`` is the later of its time and that, and "the newest first"
is by ``seen``. Without either the choice is the one above, word for word."""
from __future__ import annotations

import hashlib
import os

from covey.core import env as cenv
from covey.core.process_env import env_flag
from covey.core import knowledge, project_memory, turn_source
from covey.core.state import epoch_seconds

ENV = "COVEY_MEMORY"
LEARN_ENV = "COVEY_MEMORY_LEARN"  # 记忆自己长 (``learning``): only with ENV on too.
GLOBAL, WORKSPACE, PROJECT, TOPIC = "global", "workspace", "project", "topic"
SCOPES = (GLOBAL, WORKSPACE, PROJECT, TOPIC)
WORDS = {GLOBAL: "全局", WORKSPACE: "这个工作区", PROJECT: "这个专项", TOPIC: "这个话题"}
RULE, DECISION, HISTORY = project_memory.LAYERS  # A project line's layer (docs/web.md 三层记忆).
LAYER_WORDS = {RULE: "长期约定", DECISION: "当前决定", HISTORY: "历史记录"}
REFERENCE = "reference"  # The part of what anybody but the owner noted: 待核对参考.
PARTS = (RULE, DECISION, REFERENCE)  # What a turn carries of a project, each in its own room, in the order a block lists them.
PART_WORDS = {RULE: "长期约定", DECISION: "当前决定", REFERENCE: "待核对参考"}
PART_LIMITS = {RULE: 1000, DECISION: 1000, REFERENCE: 800}  # Characters of a project's lines, a part each.
SPARE_ORDER = (RULE, DECISION, REFERENCE)  # Who takes, in turn, the room the parts left unused: never the room a part uses itself.
REFERENCE_CAP = PART_LIMITS[REFERENCE] * 5 // 4  # 待核对参考 (1000) never takes more than this with lent room: the owner's decisions come first.
LIMITS = {GLOBAL: 1200, WORKSPACE: 800, PROJECT: sum(PART_LIMITS.values()), TOPIC: 800}  # Characters of a scope's lines.
OWNER, LEAD = "owner", "lead"
# The owner's words that start a correction (app/memory_turn.py, app/memory_learn.py).
CORRECTION_WORDS = ("以后", "每次", "别再", "不要再", "记住", "再也不要", "从现在起")
OWNER_WORDS = {OWNER: "你说的", LEAD: "它总结的"}
OWNER_SOURCES = project_memory.OWNERS  # Of a project line: the owner's own words.
ANSWER_SOURCES = project_memory.ANSWERS  # Of a project line: the owner's answer to a question.
# Who said a project line of a source and who wrote it down, for a line whose meta does not say (docs/architecture.md
# 参与者、交付与来源): the owner's words the Bridge noted, or the lead's (any other source too).
SOURCE_PARTIES = {source: (turn_source.PARTY_OWNER, turn_source.PARTY_BRIDGE) for source in OWNER_SOURCES}
LEAD_PARTIES = (turn_source.PARTY_LEAD, turn_source.PARTY_LEAD)
HELD_KINDS = ("decision", "constraint")  # Of the lead's notes: what the project holds to, carried before its facts and contacts.


def enabled(environ=None) -> bool:
    """Whether the memory is on: COVEY_MEMORY unset or anything but 0/off/false/no, under either spelling (core/env.py get:
    a Manager from before the rename starts the Bridge with only the manifest's LARK_BRIDGE_MEMORY; read on every call)."""
    return env_flag(ENV, True, environ)


def learning(environ=None) -> bool:
    """Whether the memory learns by itself (docs/butler.md 记忆自己长): COVEY_MEMORY_LEARN=1 with the memory on. Off: every
    reader and writer of the memory does what it did before it."""
    env = os.environ if environ is None else environ
    return enabled(env) and cenv.get(LEARN_ENV, "", env) == "1"


def by_source(source) -> str:
    return OWNER if str(source or "") in OWNER_SOURCES else LEAD


def describe(row) -> dict:
    """What a project's line ``row`` (an entry as core/project_memory.py entries gives it, with its meta) is to its readers:
    {asserted_by, recorded_by, origin, level, layer, part, applies, valid, superseded_by}. The two parties are its meta's when it
    has both (only a correction of the owner's writes them, core/project_memory.py fix), else its source's (``SOURCE_PARTIES``);
    ``level`` is core/turn_source.py provenance's of them and its origin. The layer is its meta's, else a 当前决定 for an answer
    and for a decision of the lead's, a 长期约定 for the rest; a line done or superseded is history. The part a turn carries it in
    is the layer's for the owner's words and 待核对参考 for anybody else's (a tombstone's is the one it had); none for history."""
    parties = (row.get("asserted_by"), row.get("recorded_by"))
    if not all(party in turn_source.PARTIES for party in parties):
        parties = SOURCE_PARTIES.get(str(row.get("source") or ""), LEAD_PARTIES)
    origin = dict(row.get("origin") or {})
    valid = "void" if row.get("deleted") else row.get("valid") or "active"
    layer = row.get("layer") or (DECISION if row.get("source") in ANSWER_SOURCES
                                 or (parties[0] != turn_source.PARTY_OWNER and row.get("kind") == "decision") else RULE)
    if valid in ("done", "superseded"):
        layer = HISTORY
    part = None if layer == HISTORY else layer if parties[0] == turn_source.PARTY_OWNER else REFERENCE
    return {"asserted_by": parties[0], "recorded_by": parties[1], "origin": origin,
            "level": turn_source.provenance(parties[0], parties[1], origin)["level"], "layer": layer, "part": part,
            "applies": str(row.get("applies") or ""), "valid": valid, "superseded_by": str(row.get("superseded_by") or "")}


def _at(value) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return float(epoch_seconds(value) or 0.0)


def make(ref, scope, by, text, at=0, redacted=False, pinned=False) -> dict:
    """An item; one of a project also has what ``describe`` says (made here without a row: the owner's words a 长期约定 at the
    owner's level, anybody else's a 待核对参考), and its ``kind``."""
    item = {"ref": ref, "scope": scope, "by": by, "text": " ".join(str(text or "").split()), "at": _at(at),
            "redacted": bool(redacted), "pinned": bool(pinned)}
    if scope == PROJECT:
        item.update(asserted_by=by, recorded_by=turn_source.PARTY_BRIDGE if by == OWNER else by, origin={},
                    level=turn_source.LEVEL_OWNER if by == OWNER else turn_source.LEVEL_AGENT, layer=RULE,
                    part=RULE if by == OWNER else REFERENCE, applies="", valid="active", superseded_by="", kind="")
    return item


def items(*, principles=((), ()), entries=((), ()), project=(), topic=(), project_id="", redacted=False, drafts=((), ()),
          used=None) -> list:
    """The items one turn may read: ``principles`` and ``entries`` are (全局, 工作区) lists as core/principles.py and
    core/knowledge.py for_turn give them (the caller leaves 全局 out of a turn that is not the owner's), ``project`` and ``topic``
    the live rows of those memories. A redacted line is only for a turn with ``redacted`` on. ``drafts`` (全局, 工作区): the
    evening candidates an older release carried ({id, text, at}; none now), 它总结的; ``used``: ref -> when a turn last used it
    (core/memory_usage.py last_used), each item's ``seen``."""
    found = []
    for scope, rows in zip((GLOBAL, WORKSPACE), principles):
        found += [make(f"principle:{row['id']}", scope, OWNER, row["text"], row.get("at"), pinned=row.get("pinned") is True)
                  for row in rows]
    for scope, rows in zip((GLOBAL, WORKSPACE), drafts):
        found += [make(f"candidate:{row['id']}", scope, LEAD, row["text"], row.get("at")) for row in rows]
    for scope, rows in zip((GLOBAL, WORKSPACE), entries):
        for row in rows:
            text = f"{knowledge.KINDS[row['kind']]}：{row['text']}" + (f"（原因：{row['why']}）" if row.get("why") else "")
            found.append(make(f"knowledge:{row['id']}", scope, OWNER, text, row.get("at")))
    for row in project:
        facts = describe(row)
        item = make(f"project:{project_id}:{row['id']}", PROJECT, OWNER if facts["asserted_by"] == turn_source.PARTY_OWNER else LEAD,
                    row["text"], row.get("at"), row.get("redacted"))
        found.append({**item, **facts, "kind": str(row.get("kind") or "")})
    for row in topic:
        found.append(make(f"topic:{row['id']}", TOPIC, by_source(row.get("source")), row["text"], row.get("at"), row.get("redacted")))
    for item in found if used else ():
        item["seen"] = max(item["at"], _at(used.get(item["ref"])))
    return [item for item in found if item["text"] and (redacted or not item["redacted"])]


def seen(item) -> float:
    """When ``item`` was last of use: its ``seen`` (``items`` with ``used``), else its own time."""
    return item.get("seen", item["at"])


def number(ref, taken) -> str:
    """The short number of ``ref``: "M" and three digits from its hash, the next free one when ``taken`` has it."""
    value = 10 + int(hashlib.sha1(ref.encode()).hexdigest()[:8], 16) % 990
    while f"M{value}" in taken:
        value = 10 + (value - 9) % 990
    return f"M{value}"


def line_size(item) -> int:
    """The characters of ``item``'s line, about: the text and the number, the scope and the speaker around it (a project's
    part, an answer's day and a line's conditions too)."""
    extra = 0
    if item.get("part") in PART_WORDS:
        extra = len(PART_WORDS[item["part"]]) + 3 + (8 if item["part"] == DECISION else 0) + (
            len(item["applies"]) + 5 if item.get("applies") else 0) + (5 if item.get("level") == turn_source.LEVEL_AGENT and item["by"] == OWNER else 0)
    return len(item["text"]) + 24 + extra


def _order(part=""):
    """The order items are taken in: pinned first, the owner's words first, the newest first (``seen``: the later of its
    time and its last use); of the 待核对参考 of a project (``REFERENCE``), the decisions and constraints before the facts and
    contacts, the newest first within each."""
    if part == REFERENCE:
        return lambda item: (item.get("kind") not in HELD_KINDS, -seen(item), item["ref"])
    return lambda item: (not item.get("pinned"), item["by"] != OWNER, -seen(item), item["ref"])


def _take(items, limit, order) -> dict:
    used, kept, left = 0, [], []
    for item in sorted(items, key=order):
        if not item.get("pinned") and used + line_size(item) > limit:
            left.append(item)
            continue
        used += line_size(item)
        kept.append(item)
    return {"kept": kept, "left": left, "used": used, "limit": limit}


def fill(found) -> dict:
    """Scope -> {kept, left (the items), used (characters, about), limit}: per scope up to its ``LIMITS``, the pinned ones first
    whatever their room, then the owner's words first, the newest first within each; an item that does not fit is left out and a
    smaller one after it may still fit. A project's lines are three choices of their own, one a part up to its ``PART_LIMITS``
    (``parts``: part -> the same four), so one part never takes the room another uses; what the three leave unused goes to the
    lines they left out, part after part in ``SPARE_ORDER`` (rules, then the owner's decisions, the 待核对参考 last, and never above ``REFERENCE_CAP``
    in all: what nobody but the owner noted never takes the room of a decision).
    A line of the history is in none."""
    scopes = {}
    for scope in SCOPES:
        mine = [item for item in found if item["scope"] == scope]
        if scope != PROJECT:
            scopes[scope] = _take(mine, LIMITS[scope], _order())
            continue
        parts = {part: _take([item for item in mine if item.get("part") == part], PART_LIMITS[part], _order(part)) for part in PARTS}
        spare = sum(max(0, PART_LIMITS[part] - parts[part]["used"]) for part in PARTS)
        for part in SPARE_ORDER:
            left, parts[part]["left"] = parts[part]["left"], []
            for item in left:
                cap = REFERENCE_CAP - parts[part]["used"] if part == REFERENCE else spare
                if line_size(item) <= min(spare, cap):
                    spare -= line_size(item)
                    parts[part]["used"] += line_size(item)
                    parts[part]["kept"].append(item)
                else:
                    parts[part]["left"].append(item)
        scopes[scope] = {"kept": [item for part in PARTS for item in parts[part]["kept"]],
                         "left": [item for part in PARTS for item in parts[part]["left"]],
                         "used": sum(parts[part]["used"] for part in PARTS), "limit": LIMITS[PROJECT], "parts": parts}
    return scopes


def numbered(kept) -> list:
    """``kept`` with their numbers (``n``), listed by scope (全局 to 话题), a project's by part, within one the owner's words
    first, the oldest first."""
    taken = set()
    for item in sorted(kept, key=lambda item: item["ref"]):
        item["n"] = number(item["ref"], taken)
        taken.add(item["n"])
    order = {scope: index for index, scope in enumerate(SCOPES)}
    parts = {part: index for index, part in enumerate(PARTS)}
    return sorted(kept, key=lambda item: (order[item["scope"]], parts.get(item.get("part"), 0), item["by"] != OWNER,
                                          item["at"], item["ref"]))


def select(found) -> tuple[list, int]:
    """(the items to carry, how many were left out): ``fill``'s, the carried ones numbered (``n``) and listed as ``numbered``
    says."""
    scopes = fill(found)
    kept = numbered([item for scope in SCOPES for item in scopes[scope]["kept"]])
    return kept, sum(len(scopes[scope]["left"]) for scope in SCOPES)


def project_view(rows, project_id, *, redacted=False, since=None, used=None) -> dict:
    """A project's memory as every reader takes it (a member topic's turn, the lead's pack, the web page): ``rows`` is every
    entry of it, tombstones too (core/project_memory.py entries). {version (core/project_memory.py version), items (the live
    lines but the style line, as ``items`` makes them), kept, left, parts (``fill``'s of the project), voided, new}. ``since``,
    when the reader's previous turn began (None: it had none, or reads it whole): ``voided`` the lines deleted after it,
    as items, ``new`` the refs of the kept ones written after it. ``used`` (``items``): when a turn last used each line, so the
    choice is the one the turn's own memory block makes (app/memory_turn.py) and what both say was left out is the same count. A line from a topic with redaction on is only in a view
    with ``redacted``."""
    live = [row for row in rows if not row["deleted"] and row["kind"] != "style"]
    found = items(project=live, project_id=project_id, redacted=redacted, used=used)
    room = fill(found)[PROJECT]
    gone = [row for row in rows if row["deleted"] and row["kind"] != "style" and since is not None and row["deleted"] > since]
    voided = items(project=gone, project_id=project_id, redacted=redacted)
    new = {item["ref"] for item in room["kept"] if since is not None and item["at"] > since}
    return {"version": project_memory.version(rows), "items": found, "kept": room["kept"], "left": room["left"],
            "parts": room["parts"], "voided": voided, "new": new}


def ids_of(kept) -> list:
    """What the job record's ``memory_ids`` says of the items a turn carried."""
    return [{"n": item["n"], "ref": item["ref"], "scope": item["scope"], "by": item["by"]} for item in kept]
