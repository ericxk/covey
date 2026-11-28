"""The write gate of a memory entry (docs/butler.md 记忆): what is refused or rewritten, what is only flagged.

Hard refusals (or a rewrite offered) are kept to two things that go wrong whatever the context: a time reference that
stops being true (``TIME_WORDS``: 今天、刚才、目前…) and an explicit live state (``DYNAMIC_RES``: 现网第 N 代 and the
like; such a state goes nowhere in memory, the entry says where to look). Vague pointers (``SOFT_WORDS``: 这个、这类、
那个、上面) and ordinary commit hashes or clock times are flagged only, never refused. Memory entries are
written through it (docs/butler.md 记忆); ``check`` is a pure function.

``write`` and ``revise`` are the one way into core/memstore.py's writes that WAVE12 C8 asks for (``Proof`` what the
owner's words were, ``Proven`` that app/memory_proof.py checked them, ``Written`` what came of it). The owner's words reach
the memory through ``write`` alone (T908 M3a): 记忆自己长 (app/memory_learn.py), a turn's checkpoint (app/memory_collect.py,
app/knowledge_collect.py) and the butler's memory_save (app/butler_knowledge.py). ``write`` checks the words (``check``;
``accept`` takes the rewrite offered, ``strict`` hands the refusal back), counts the entry the owner's only with his words
proven, keeps a learned rule that would let a turn skip asking him (``grants``) out of force until he accepts it, and puts
the question whether a rule is for every topic (``wider``) in 待审核 while it holds where it was said. What it writes is
byte for byte what the same core/memstore.py call writes.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from covey.core import ask_kinds, project_gates, turn_source

TIME_WORDS = ("今天", "明天", "昨天", "刚才", "刚刚", "目前", "现在", "本周", "这周", "今晚", "今早", "上周", "下周")
DYNAMIC_RES = (re.compile(r"现网(?:是|在|为)?第\s*\d+\s*代"), re.compile(r"(?:当前|现在)(?:版本|部署)(?:是|为)\s*\S+"))
SOFT_WORDS = ("这个", "这类", "那个", "上面")
FLAGS = ("vague", "dynamic", "repeat", "old_repo")


@dataclass(frozen=True)
class Verdict:
    ok: bool  # False: refused; ``text`` is the rewrite offered, '' when there is none.
    text: str
    flags: tuple = ()  # Of FLAGS.
    reason: str = ""


_FROM = "|".join(map(re.escape, TIME_WORDS))
# A leading time word that can go without the sentence breaking: 从今天起 / 今天开始 / 目前，/ 现在，/ 目前 and 现在 (adverbs) before words.
_PREFIX = re.compile(rf"^(?:从\s*(?:{_FROM})\s*(?:起|开始)|(?:{_FROM})\s*(?:起|开始)|(?:目前|现在)(?:[，,、：:\s]+|(?=只|都|先|不|要|必须|统一|默认|一律)))[，,、：:\s]*")
MIN_REWRITE = 4  # A rewrite shorter than this is no sentence: the verdict gives none.


def check(text: str, *, kind: str, title: str = "") -> Verdict:
    """The verdict on a new entry's ``text`` of ``kind`` (pure: no state is read).

    A live state (``DYNAMIC_RES``) is refused with no rewrite; a time word (``TIME_WORDS``) is refused with the sentence
    rewritten when a leading 从今天起 / 目前 / 现在 can simply go, else with no rewrite. Vague pointers (``SOFT_WORDS``) are
    only flagged ``vague``, a live state in a decision only ``dynamic``; a decision (kind ``decision``: the question and the
    answer as given) is never refused. Commit hashes and clock times are neither flagged nor refused. ``title`` is read with
    the text for the flags only."""
    body = str(text or "").strip()
    seen = f"{title or ''} {body}"
    flags = []
    if any(word in seen for word in SOFT_WORDS):
        flags.append("vague")
    dynamic = any(pattern.search(body) for pattern in DYNAMIC_RES)
    if dynamic:
        flags.append("dynamic")
    if kind == "decision":
        return Verdict(True, body, tuple(flags))
    if dynamic:
        return Verdict(False, "", tuple(flags), "含会变的现状说法，别把现状写进记忆，写成去哪里查或按什么原则办")
    hit = next((word for word in TIME_WORDS if word in body), "")
    if not hit:
        return Verdict(True, body, tuple(flags))
    rewritten = _PREFIX.sub("", body, count=1).strip()
    if rewritten != body and len(rewritten) >= MIN_REWRITE and not any(word in rewritten for word in TIME_WORDS):
        return Verdict(False, rewritten, tuple(flags), f"含会过时的说法“{hit}”，已改成不依赖时间的写法")
    return Verdict(False, "", tuple(flags), f"含会过时的说法“{hit}”，写成不依赖时间的说法")


def accept(text: str, *, kind: str, title: str = "") -> tuple[str, Verdict]:
    """(what an automatic write keeps, the verdict): the text when it passes, the rewrite the verdict offers when that one
    passes in its turn, '' when neither does (the write is dropped)."""
    verdict = check(text, kind=kind, title=title)
    if verdict.ok:
        return verdict.text, verdict
    if verdict.text:
        again = check(verdict.text, kind=kind, title=title)
        if again.ok:
            return again.text, again
    return "", verdict


@dataclass(frozen=True)
class Proof:
    """What a write says shows the owner's words (WAVE12 C8)."""

    message_id: str = ""  # The owner's own message (om_…, web-…), or the one a hand-on carried.
    question: str = ""  # The token of a question the owner answered himself.
    quote: str = ""  # A stretch of the owner's words, as he said them.
    via: str = ""  # "" | "butler" | "lead" | "web" | "card"


@dataclass(frozen=True)
class Proven:
    """A Proof app/memory_proof.py checked: only it makes one (tests/test_structure.py MemoryWriteTest)."""

    proof: Proof
    level: str  # core/turn_source.py LEVEL_OWNER / LEVEL_AGENT
    said: str = ""  # The owner's words the check found.


@dataclass(frozen=True)
class Written:
    """What a write or a revision did."""

    entry: object | None  # The core/memstore.py Entry; None when it went to review instead, or was refused.
    review: str = ""  # The review item's key: the item it went to, or the one asking whether it is to hold 全局.
    said: str = ""  # Whose the entry is counted as (core/memstore.py SAID).
    scope: str = ""  # The scope it went to.
    reason: str = ""  # Why it was refused or narrowed: the gate's words, or GRANT.
    rewrite: str = ""  # A refused text's rewrite the gate offers (``strict``).
    flags: tuple = ()  # Of FLAGS.
    done: str = ""  # core/memstore.py put's NEW, CONFIRMED or SAME.
    fresh: bool = False  # The review item is new (one there already is not said to wait again).
    grant: str = ""  # What the words name of the five kinds or the gate words (``grants``).


GRANT = "grant"  # Written.reason of a learned rule that waits in 待审核: its words name what only the owner says yes to.
PENDING = "pending"  # Written.reason of a write ``pending`` asked for: it waits in 待审核, written once he accepts it.
GLOBAL = "global"
# The owner's words that say a rule is for every topic: the butler writes one 全局 only with one of them in his words (T908 M3a).
EVERYWHERE = ("所有", "以后都", "处处", "全局")


def grants(text, title="") -> str:
    """What a rule saying ``text`` (``title``) would let a turn do without asking the owner: the key of the first of the five
    kinds its words name (core/ask_kinds.py classify, the one list), else the first gate word (core/project_gates.py words:
    secrets, quota…); '' for none. The lists lean to hits: one costs the owner a click in 待审核."""
    seen = f"{title or ''} {text or ''}"
    return ask_kinds.classify(seen) or next(iter(project_gates.words(seen)), "")


def everywhere(quote) -> bool:
    """Whether the owner's ``quote`` says the rule is for every topic (``EVERYWHERE``)."""
    return any(word in str(quote or "") for word in EVERYWHERE)


DECISION_FIELDS = ("question", "answer", "question_ref", "topic_key", "chosen", "category", "polarity", "status")
REVISIONS = {"update": "edit", "supersede": "supersede", "settle": "done", "archive": "archived", "void": "void"}
OLD_REVISIONS = {"retire": "settle"}  # The tools' old word: a decision settled, anything else archived.


def _said(proof) -> str:
    return "owner" if isinstance(proof, Proven) and proof.level == turn_source.LEVEL_OWNER else "summary"


def _origin(proof) -> dict:
    """The parts of a proof an entry keeps of where it came from."""
    given = proof.proof if isinstance(proof, Proven) else None
    return {key: getattr(given, key) for key in ("message_id", "question", "quote", "via") if given and getattr(given, key)}


def _because(source, place="") -> dict:
    return {key: value for key, value in (("source", source), ("place", place)) if value}


def _suggest(kind, scope, owner_key, **more) -> dict:
    where = {"project_key": owner_key} if scope == "project" else {"owner_key": owner_key} if scope == "topic" else {}
    return {"kind": kind, "scope": scope, **where, **more}


def _review(state_dir, *, kind, text, title, suggest, proof, origin, flags, items=(), now=None) -> tuple[str, bool]:
    """(the key, whether it is new) of a 待审核 item of type learn (core/memory_review.py add) proposing ``text``."""
    import time

    from covey.core import memory_review, memstore

    given = proof.proof if isinstance(proof, Proven) else None
    where = {key: origin[key] for key in ("topic_key", "topic_number") if origin.get(key) not in (None, "")}
    return memory_review.add(state_dir, memory_review.ReviewItem(
        key="", type="learn", title=title or memstore.auto_title(text, kind), text=text, suggest=suggest,
        created_at=time.time() if now is None else now, quote=getattr(given, "quote", "") or "", origin=where,
        items=tuple(items), flags=tuple(flags)))


def write(state_dir, *, kind, scope, owner_key="", text, title="", why="", repos=(), proof: Proven | None, by: str,
          source: str, place: str = "", decision: dict | None = None, supersedes: str = "", redacted=False,
          origin: dict | None = None, strict: bool = False, learned: bool = False, wider: str = "", pending: bool = False,
          now=None) -> Written:
    """A new entry, through to core/memstore.py: ``put`` (``add_decision`` for kind ``decision``, with ``decision``'s
    fields of DECISION_FIELDS), counted as the owner's when ``proof`` is his level, else as a summary by ``by``; its origin
    ``origin`` (where: topic_key, job, topic_number) with the proof's parts over it, its log line's ``because`` ``source``
    and ``place``. ``supersedes``: the ref of an entry it replaces (core/memstore.py supersede).

    The words go through the gate first (a decision's never refused): refused, nothing is written and ``reason`` says why,
    with the rewrite it offers in ``rewrite`` when ``strict`` (the butler's: it writes again), else the rewrite is written
    (``accept``) when there is one. ``learned``: a rule a model took from the owner's words (记忆自己长, a checkpoint's): one
    whose words name what only he says yes to (``grants``) is not written; it waits in 待审核 (type learn, its scope and
    ``grant`` suggested; reason ``GRANT``) and holds once he accepts it, never before, so a learned rule never lets a turn
    skip asking him. ``wider`` "global": a rule the model says is for every topic, written where it was said, and 待审核
    asks whether it is to hold 全局 (``review``; the item's ``items`` the line written, which an accept moves there); never
    for a line from a topic with redaction on. ``pending``: nothing is written, the entry waits in 待审核 at its scope (reason
    ``PENDING``; the butler's 全局 the owner's words did not ask for). ValueError from core/memstore.py says why nothing was
    written."""
    from covey.core import memstore

    said, because = _said(proof), _because(source, place)
    origin = {**{key: value for key, value in dict(origin or {}).items() if value not in (None, "")}, **_origin(proof)}
    flags = ()
    if kind != "decision":
        verdict = check(text, kind=kind, title=title)
        flags = verdict.flags
        if not verdict.ok:
            kept = "" if strict else accept(text, kind=kind, title=title)[0]
            if not kept:
                return Written(entry=None, scope=scope, reason=verdict.reason or "gate", rewrite=verdict.text, flags=flags)
            text = kept
        grant = grants(text, title) if learned and kind == "rule" else ""
        if grant or pending:
            target = GLOBAL if wider == GLOBAL and not redacted else scope
            more = {"grant": grant} if grant else {}
            key, fresh = _review(state_dir, kind=kind, text=text, title=title, proof=proof, origin=origin, flags=flags,
                                 suggest=_suggest(kind, target, "" if target == GLOBAL else owner_key, **more), now=now)
            return Written(entry=None, review=key, said=said, scope=target, reason=GRANT if grant else PENDING, flags=flags,
                           fresh=fresh, grant=grant)
    if kind == "decision":
        fields = {key: value for key, value in dict(decision or {}).items() if key in DECISION_FIELDS}
        entry = memstore.add_decision(state_dir, scope=scope, owner_key=owner_key, text=text, title=title, said=said, by=by,
                                      origin=origin, redacted=redacted, who=by, because=because, now=now, **fields)
        done = memstore.NEW
    else:
        entry, done = memstore.put(state_dir, kind=kind, scope=scope, owner_key=owner_key, text=text, title=title, why=why,
                                   said=said, by=by, origin=origin, repos=repos, redacted=redacted, who=by, because=because,
                                   now=now)
    if supersedes:
        memstore.supersede(state_dir, supersedes, entry.ref, who=by, because=because, now=now)
    key, fresh = "", False
    if wider == GLOBAL and kind == "rule" and entry.scope != GLOBAL and not entry.redacted and entry.status == "active":
        key, fresh = _review(state_dir, kind=kind, text=entry.text, title=title, proof=proof, origin=origin, flags=flags,
                             suggest=_suggest(kind, GLOBAL, ""), items=(entry.ref,), now=now)
    return Written(entry=entry, review=key, said=entry.said, scope=entry.scope, flags=flags, done=done, fresh=fresh)


def revise(state_dir, ref, action, *, proof: Proven | None, by: str, source: str, text="", why="", by_ref="") -> Written:
    """The entry ``ref`` revised, through to core/memstore.py: ``update`` edits its words (``text``, ``why``);
    ``supersede`` replaces it by ``by_ref``, or by a new entry of its kind and scope saying ``text``; ``settle`` (done),
    ``archive``, ``void``; ``retire``, the tools' old word, settles a decision and archives anything else. ``proof``
    is not read yet (W1-0). ValueError for an action it does not know, or from core/memstore.py."""
    from covey.core import memstore

    because = _because(source)
    if action in OLD_REVISIONS:
        entry = memstore.find(state_dir, ref)
        action = "settle" if entry is not None and entry.kind == "decision" else "archive"
    if action not in REVISIONS:
        raise ValueError(f"不认识的改法：{action}")
    if action == "update":
        entry = memstore.edit(state_dir, ref, text=text or None, why=why or None, who=by, because=because)
    elif action == "supersede":
        if not by_ref:
            old = memstore.find(state_dir, ref)
            if old is None:
                raise ValueError("这条记忆已经不在了")
            by_ref = write(state_dir, kind=old.kind, scope=old.scope, owner_key=old.owner_key, text=text, why=why,
                           repos=old.repos, proof=proof, by=by, source=source, place="").entry.ref
        memstore.supersede(state_dir, ref, by_ref, who=by, because=because)
        entry = memstore.find(state_dir, by_ref)
    else:
        entry = {"settle": memstore.settle, "archive": memstore.archive, "void": memstore.void}[action](
            state_dir, ref, who=by, because=because)
    return Written(entry=entry, said=getattr(entry, "said", ""), scope=getattr(entry, "scope", ""))
