"""The write gate of a memory entry (docs/butler.md 记忆): what is refused or rewritten, what is only flagged.

Hard refusals (or a rewrite offered) are kept to two things that go wrong whatever the context: a time reference that
stops being true (``TIME_WORDS``: 今天、刚才、目前…) and an explicit live state (``DYNAMIC_RES``: 现网第 N 代 and the
like; such a state goes nowhere in memory, the entry says where to look). Vague pointers (``SOFT_WORDS``: 这个、这类、
那个、上面) and ordinary commit hashes or clock times are flagged only, never refused. Memory entries are
written through it (docs/butler.md 记忆); ``check`` is a pure function.

``write`` and ``revise`` are the one way into core/memstore.py's writes that WAVE12 C8 asks for (``Proof`` what the
owner's words were, ``Proven`` that app/memory_proof.py checked them, ``Written`` what came of it). Readers first (W1-0):
they pass the write through to core/memstore.py as it is, the files written byte for byte what the same call there writes,
and no caller uses them yet; the rules of whose words make an entry the owner's, of the scope and of review come with the
package that moves the callers here (W1-D).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from covey.core import turn_source

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

    entry: object | None  # The core/memstore.py Entry; None when it went to review instead.
    review: str = ""  # The review item's key, then.
    said: str = ""  # Whose the entry is counted as (core/memstore.py SAID).
    scope: str = ""  # The scope it went to.
    reason: str = ""  # Why it was refused or narrowed.


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


def write(state_dir, *, kind, scope, owner_key="", text, title="", why="", repos=(), proof: Proven | None, by: str,
          source: str, place: str, decision: dict | None = None, supersedes: str = "", redacted=False,
          now=None) -> Written:
    """A new entry, through to core/memstore.py: ``add`` (``add_decision`` for kind ``decision``, with ``decision``'s
    fields of DECISION_FIELDS), counted as the owner's when ``proof`` is his level, else as a summary by ``by``; its origin
    the proof's parts, its log line's ``because`` ``source`` and ``place``. ``supersedes``: the ref of an entry it
    replaces (core/memstore.py supersede). ValueError from core/memstore.py says why nothing was written."""
    from covey.core import memstore

    said, origin, because = _said(proof), _origin(proof), _because(source, place)
    if kind == "decision":
        fields = {key: value for key, value in dict(decision or {}).items() if key in DECISION_FIELDS}
        entry = memstore.add_decision(state_dir, scope=scope, owner_key=owner_key, text=text, title=title, said=said, by=by,
                                      origin=origin, redacted=redacted, who=by, because=because, now=now, **fields)
    else:
        entry = memstore.add(state_dir, kind=kind, scope=scope, owner_key=owner_key, text=text, title=title, why=why,
                             said=said, by=by, origin=origin, repos=repos, redacted=redacted, who=by, because=because,
                             now=now)
    if supersedes:
        memstore.supersede(state_dir, supersedes, entry.ref, who=by, because=because, now=now)
    return Written(entry=entry, said=entry.said, scope=entry.scope)


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
