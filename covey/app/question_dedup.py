"""Before a question reaches the owner, the same question answered before (docs/architecture.md 问题与回答).

The check runs in app/native_questions.py ``NativeQuestions.dispatch``, strictly before ``register`` (a registered
question that is never shown holds the topic). ``check`` finds, for each question, a decision of the topic or of its
project (core/memstore.py) whose question says the same (``same_question``): core/memory_search.py ``eval_search`` finds it
from the question's words with its one-way similarity at least ``DEFAULT_TAU_DUP`` (what that threshold was set on: a
question → the decision that answered it), and the old question's words and the new one's are alike both ways too
(``mutual_similarity`` at least ``MUTUAL_DUP``, measured on the blind set's 20 asked-again questions and 3 that must not
match); two classes that say different things (``category``, neither of them ``other``) never match.
While core/memory_search.py has neither of the two, with COVEY_MEMORY off, or in a 代答 topic, nothing is matched. What a
hit does is the dispatch's:

- a project's question that triage hands to its lead (app/triage.py ``leads``): no turn of its own, the old answer goes
  with it to the lead (``lead_line``, in the receipt);
- one of the five high-risk classes (``HIGH_RISK``, or a gate word of core/project_gates.py): only the grey line, never held;
- on a backend that takes a refused question back in the same turn (``in_turn``: its Harness ``refuses_questions`` and keeps
  the turn open on a question): the question is refused with the old answer (``reject_text``), at most ``MAX_REJECTS`` time
  per (topic, question hash) and per old answer, counted in the topic's job records (``JOB_FIELD``: the task's own record,
  so a restart of the Bridge does not reset it), and the entry gets a ``dedup`` signal (core/memory_usage.py);
- on any other (a refusal that ends the run, a question that pauses the turn, a checkpoint's question): the card shows a grey
  line 上次你答过：…（Txx） (``hint``) and is never held.

The third same-class answer (``class3``): when the project (an ordinary topic: every ordinary topic) has two decisions of the
same class (``category`` and a similar question) answered the same way (``polarity``) in ``CLASS_DAYS`` days, the card adds a
question of its own, kept in the record's ``extra_questions`` and never in ``questions`` (``live_question_proof`` hashes
those): drawn on the card by ``drawn``, its answer read by ``extra_answers``, nothing of it in what the model gets back or in
the decision noted. ``settle``: only the owner's own submit writes the rule of ``FOLLOW`` (a lead answering never does), and
``KEEP_ASKING`` (or the rule written) keeps the class quiet for ``CLASS3_QUIET_DAYS`` (memory/dedup.json, the Bridge's).
Nothing here raises into the question's path: whatever fails, the question is asked as before."""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from pathlib import Path

from covey.core import ask_kinds

MAX_REJECTS = 1
CLASS3_AFTER = 3
CLASS3_QUIET_DAYS = 30
HIGH_RISK = ask_kinds.HIGH_RISK  # The five kinds the owner is always asked (core/ask_kinds.py: one source, WAVE12 C3).
EXTRA_KEY = "extra_questions"

TAU_DUP = 0.55  # When core/memory_search.py has no DEFAULT_TAU_DUP of its own.
MUTUAL_DUP = 0.25  # The old and the new question alike both ways (T870 Wave 2B, verify/after/dedup-probe.json: 15 of 20 asked again
# found, none wrong, none of the 3 that must not match; at 0.2 the nearest negative, 0.18, is too close).
REFUSE_MUTUAL = 0.5  # A refusal in the turn hands the model the old answer: only for questions this alike both ways (else
# the grey line alone). With few decisions every word weighs about the same: a shared noun phrase alone reached 0.39.
CLASS_TAU = 0.4  # Of a question of the same class.
CLASS_SEARCH_TAU = 0.25  # eval_search's floor for the candidates of a class (CLASS_TAU, both ways, decides).
CLASS_DAYS = 30
CANDIDATES = 10  # eval_search's results looked at for a hit (those of other topics and projects are passed over).
CLASS_CANDIDATES = 20  # For the same class.
MARK_KEY = "dedup"  # The record's: {question id: the hit as the card and the lead's receipt say it}.
EXTRA_ANSWERS = "extra_answers"  # The record's: {extra question id: the label picked}.
JOB_FIELD = "question_dedup"  # A job record's: [{q, ref, request, at}], one a question refused with an old answer.
EXTRA_ID = "__memory_class"
KEEP_ASKING, FOLLOW = "还要问", "以后按这次的做"
STORE = "dedup.json"  # Under memory/: {"quiet": {class key: until}}; the Bridge writes it (an answer's path).
ANSWER_SHOWN = 60  # Characters of an old answer on the card's grey line.

CATEGORIES = (  # The first that matches; ``other`` for none. The five are core/ask_kinds.py's words, in its ORDER (deleting first).
    *((key, ask_kinds.PATTERNS[key]) for key in ask_kinds.ORDER),
    ("merge", re.compile(r"合入|合进|合并|合到|主干|\bmerge\b|\bPR\b|\bMR\b", re.I)),
    ("sample", re.compile(r"样张|样式|外观|配色|颜色|版式|界面")),
)
CLASS_WORDS = {"merge": "合进主干", "sample": "选样张"}
NO_WORDS = re.compile(r"不|别|先等|再等|暂缓|暂停|停|取消|跳过|保持现状|否|算了")
YES_WORDS = re.compile(r"做|开|同意|可以|好|要|合|按推荐|是|继续|直接|照|行|用")


@dataclass(frozen=True)
class Hit:
    ref: str
    code: str
    answer: str
    topic_number: int | None
    at: float
    sim: float
    question: str = ""  # The old question's words.
    mutual: float = 1.0  # The old and the new question alike both ways (REFUSE_MUTUAL for a refusal in the turn).
    risky: bool = False  # The old question or its answer is of the five high-risk classes (``old_risky``): the grey line only.


def q_hash(question: dict) -> str:
    """A question's fingerprint: its text and its options' labels, whitespace folded."""
    text = " ".join(str(question.get("question") or question.get("header") or "").split())
    labels = "\0".join(" ".join(str(option.get("label") or "").split()) for option in question.get("options") or ()
                       if isinstance(option, dict))
    return hashlib.sha1(f"{text}\0{labels}".encode("utf-8")).hexdigest()[:16]


def _words(text) -> str:
    return " ".join(str(text or "").split())


def asked(question) -> str:
    """What a question asks: its header and its words (what is compared with an old question)."""
    if not isinstance(question, dict):
        return ""
    header = _words(question.get("header"))
    text = _words(question.get("question"))
    return f"{header} {text}".strip() if header and header not in text else text


def query_of(question) -> str:
    """What a question is searched by: ``asked`` and its options' labels."""
    labels = " ".join(_words(option.get("label")) for option in (question.get("options") or ()) if isinstance(option, dict))
    return f"{asked(question)} {labels}".strip()


def category(text) -> str:
    """The class of what ``text`` asks about (``CATEGORIES``), ``other`` for none."""
    text = str(text or "")
    return next((name for name, words in CATEGORIES if words.search(text)), "other")


def polarity(text) -> str:
    """``no`` for an answer that holds back (不、先不、等…), ``yes`` for one that goes ahead, '' for neither."""
    text = _words(text)
    if not text:
        return ""
    if NO_WORDS.search(text):
        return "no"
    return "yes" if YES_WORDS.search(text) else ""


def risky(question) -> bool:
    """Whether ``question`` is of the five high-risk classes: only noted, never held, never a class of its own."""
    from covey.core import project_gates

    text = query_of(question) if isinstance(question, dict) else ""
    return category(text) in HIGH_RISK or bool(project_gates.hit([question]))


def engine():
    """(eval_search, mutual_similarity, τ_dup) of core/memory_search.py, or None while it has not both (nothing matched)."""
    try:
        from covey.core import memory_search
    except Exception:  # noqa: BLE001 - no search, no check
        return None
    search, mutual = getattr(memory_search, "eval_search", None), getattr(memory_search, "mutual_similarity", None)
    if not (callable(search) and callable(mutual)):
        return None
    tau = getattr(memory_search, "DEFAULT_TAU_DUP", None)
    tau = float(tau) if isinstance(tau, (int, float)) and not isinstance(tau, bool) and 0 < tau <= 1 else TAU_DUP
    return search, mutual, tau


def enabled(ctx) -> bool:
    """COVEY_MEMORY on and not a 代答 topic."""
    from covey.core import memory as core_memory
    from covey.core.topics import proxy_topic

    if not core_memory.enabled():
        return False
    try:
        return not proxy_topic(ctx)
    except Exception:  # noqa: BLE001 - a topic that cannot be read is not checked
        return False


def project_of(state_dir, topic_id) -> str:
    """The id of the project ``topic_id`` is a topic of (member or lead), '' for an ordinary topic."""
    from covey.core import projects

    try:
        found = projects.topic_project(Path(state_dir), str(topic_id or ""))
    except Exception:  # noqa: BLE001 - an ordinary topic
        return ""
    return found.id if found is not None else ""


def split(entry) -> tuple[str, str]:
    """(question, answer) of a decision's text ("主人答：question→answer", or "question→answer")."""
    text = _words(getattr(entry, "text", ""))
    text = text[len("主人答："):] if text.startswith("主人答：") else text
    question, sep, answer = text.partition("→")
    return (question.strip(), answer.strip()) if sep else ("", text)


def _entry(state_dir, item):
    from covey.core import memstore

    entry = item.get("entry") if isinstance(item, dict) else item
    if entry is not None and hasattr(entry, "ref") and hasattr(entry, "kind"):
        return entry
    ref = item.get("ref") if isinstance(item, dict) else ""
    return memstore.find(state_dir, str(ref or "")) if ref else None


def _number(state_dir, entry) -> int | None:
    from covey.core.topics import topic_number

    origin = getattr(entry, "origin", None) or {}
    number = origin.get("topic_number")
    if isinstance(number, int) and not isinstance(number, bool) and number > 0:
        return number
    topic = origin.get("topic_key") or origin.get("topic") or (entry.owner_key if entry.scope == "topic" else "")
    try:
        number = topic_number(Path(state_dir), str(topic or "")) if topic else 0
    except Exception:  # noqa: BLE001 - no number
        number = 0
    return int(number) if number else None


def old_risky(old, answer) -> bool:
    """Whether an old decision is of the five high-risk classes by its own words (its question and answer: ``category`` or a
    gate word of core/project_gates.py). Such an answer is never handed back as the answer of a new question, however alike:
    a new one that says neither 删 nor 部署 may still ask the same 删掉吗, and what the owner said then is no leave for now."""
    from covey.core import project_gates

    return category(f"{old} {answer}") in HIGH_RISK or bool(project_gates.hit([{"question": old, "options": [{"label": answer}]}]))


def search_context(topic, project, tau, purpose) -> dict:
    """What core/memory_search.py ``eval_search`` is given (its keys: ``topic``, ``project``, ``kinds``, ``statuses``, ``tau``):
    the decisions in use or done, weighed for this topic and project, none of the global store (no decision is there)."""
    return {"topic": topic, "project": project, "owner": False, "kinds": ("decision",), "statuses": ("active", "done"),
            "tau": tau, "purpose": purpose}


def _candidates(state_dir, search, question, context, limit) -> list:
    """[(entry, eval_search's item)] of the decisions ``question`` finds, the best first."""
    try:
        found = search(state_dir, query_of(question), context, limit=limit)
    except Exception:  # noqa: BLE001 - no candidates
        return []
    out = []
    for item in found or ():
        entry = _entry(state_dir, item)
        if (entry is not None and entry.kind == "decision" and entry.status in ("active", "done")
                and not entry.superseded_by):
            out.append((entry, item if isinstance(item, dict) else {}))
    return out


def same_class(one, other) -> bool:
    """Whether two ``category``s may be the same question's: equal, or one of them ``other`` (the words say neither)."""
    return one == other or "other" in (one, other)


def same_question(item, mutual_sim, covered, tau) -> bool:
    """The hit rule of ``check``: eval_search's one-way ``sim`` at least ``tau`` with enough words in common, and the two
    questions alike both ways (``mutual_sim`` at least ``MUTUAL_DUP``, covered)."""
    sim = item.get("sim")
    one_way = float(sim) >= tau if isinstance(sim, (int, float)) else True  # A search that gives no sim: its own threshold.
    return one_way and item.get("covered", True) is not False and covered and float(mutual_sim) >= MUTUAL_DUP


def check(ctx, record: dict, questions) -> dict:
    """{question id: Hit} of ``questions`` answered before in the topic or its project; {} while nothing may be matched."""
    try:
        found = engine()
        if found is None or not enabled(ctx):
            return {}
        search, mutual, tau = found
        state = Path(ctx.state_dir)
        topic = str(record.get("topic_id") or getattr(ctx, "topic_id", "") or "")
        project = project_of(state, topic)
        owners = {key for key in (topic, project) if key}
        hits = {}
        for question in questions or ():
            if not isinstance(question, dict) or not question.get("id") or not asked(question):
                continue
            words, kind = asked(question), category(query_of(question))
            context = search_context(topic, project, tau, "dedup")
            best, best_mutual = None, 0.0
            for entry, item in _candidates(state, search, question, context, CANDIDATES):
                old, answer = split(entry)
                if entry.owner_key not in owners or not old or not answer or not same_class(category(f"{old} {answer}"), kind):
                    continue
                both, covered = mutual(words, old, state_dir=state)
                if same_question(item, both, covered, tau) and (best is None or float(both) > best_mutual):
                    sim = item.get("sim")
                    best, best_mutual = Hit(ref=entry.ref, code=entry.code, answer=answer, topic_number=_number(state, entry),
                                            at=float(entry.at or (entry.origin or {}).get("at") or 0),
                                            sim=float(sim) if isinstance(sim, (int, float)) else float(both), question=old,
                                            mutual=float(both), risky=old_risky(old, answer)), float(both)
            if best is not None:
                hits[str(question["id"])] = best
        return hits
    except Exception:  # noqa: BLE001 - the question is asked as before
        return {}


def _when(hit) -> str:
    parts = [f"T{hit.topic_number}" if hit.topic_number else "", time.strftime("%m-%d", time.localtime(hit.at)) if hit.at else ""]
    return " · ".join(part for part in parts if part)


def _cut(text, limit) -> str:
    text = _words(text)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def hint(hit) -> str:
    """The card's grey line for a question answered before."""
    when = _when(hit)
    return f"上次你答过：{_cut(hit.answer, ANSWER_SHOWN)}" + (f"（{when}）" if when else "")


def lead_line(record) -> str:
    """The old answers a held question's receipt hands the lead (``MARK_KEY``), '' for none."""
    marks = record.get(MARK_KEY) if isinstance(record.get(MARK_KEY), dict) else {}
    lines = [f"主人上次答过：{_cut(mark.get('question'), 60)}→{_cut(mark.get('answer'), 80)}"
             + (f"（{mark['code']}）" if mark.get("code") else "") for mark in marks.values()
             if isinstance(mark, dict) and not mark.get("risky")]  # A high-risk old answer is no ground for the lead either.
    return "；".join(lines)


def reject_text(pairs) -> str:
    """What a refused question hands back in the same turn: each one's old answer, and what to do with it."""
    told = []
    for question, hit in pairs:
        when = _when(hit)
        told.append(f"“{_cut(asked(question), 80)}”主人{('在 ' + when + ' ') if when else '之前'}答过："
                    f"{_cut(hit.question, 80)}→{_cut(hit.answer, 160)}" + (f"（记忆 {hit.code}）" if hit.code else ""))
    return ("这道题主人答过，没有再发给他。" + "；".join(told) + "。范围和那次一样就照那次的回答做，不要再问；"
            "范围确实不同，就把题原样再问一次，并在题里写明和那次哪里不一样。")


def marks(hits) -> dict:
    """What the record keeps of ``hits`` (``MARK_KEY``), {} for none."""
    if not hits:
        return {}
    return {MARK_KEY: {qid: {"ref": hit.ref, "code": hit.code, "answer": hit.answer, "question": hit.question,
                             "topic_number": hit.topic_number, "at": hit.at, "line": hint(hit),
                             **({"risky": True} if getattr(hit, "risky", False) else {})} for qid, hit in hits.items()}}


# Refusing in the same turn: at most MAX_REJECTS, counted in the topic's job records.

def in_turn(harness, rpc) -> bool:
    """Whether a refused question goes back to the model in the same turn: the Harness refuses without ending the run
    (``refuses_questions``) and keeps the turn open on its questions (LIVE_QUESTIONS), and this question is such a one."""
    from covey.core.roster import LIVE_QUESTIONS

    spec = getattr(harness, "spec", None)
    return (spec is not None and getattr(spec, "refuses_questions", False) is True and spec.name in LIVE_QUESTIONS
            and rpc is not None and getattr(rpc, "native_blocking_questions", False) is True
            and callable(getattr(rpc, "reject_question", None)))


def refused(state_dir, topic_id) -> list:
    """Every refusal of the topic's jobs (``JOB_FIELD``'s rows)."""
    from covey.core.store import directory_store

    rows = []
    for _, job in directory_store(Path(state_dir) / "jobs").query(topic_id=str(topic_id or "")):
        rows += [row for row in job.get(JOB_FIELD) or () if isinstance(row, dict)]
    return rows


def may_refuse(rows, question, hit) -> bool:
    """Whether ``question`` may be refused with ``hit``: the old answer not of a high-risk class (``Hit.risky``), the two
    questions alike enough (``REFUSE_MUTUAL``), and neither it (its hash) nor that old answer refused MAX_REJECTS times."""
    digest = q_hash(question)
    if getattr(hit, "risky", False):
        return False  # The old answer is of a high-risk class (``old_risky``): only the grey line.
    return (float(getattr(hit, "mutual", 1.0)) >= REFUSE_MUTUAL and sum(1 for row in rows if row.get("q") == digest) < MAX_REJECTS
            and sum(1 for row in rows if row.get("ref") == hit.ref) < MAX_REJECTS)


def note_refused(state_dir, job, request_id, pairs, now=None) -> None:
    """The refusal kept in the task's own job record, before it is sent."""
    from covey.core.jobs import find_job_record, upsert_job_record

    stamp = time.time() if now is None else now
    record = find_job_record(Path(state_dir), str(job.message_id)) or {}
    rows = [row for row in record.get(JOB_FIELD) or () if isinstance(row, dict)]
    rows += [{"q": q_hash(question), "ref": hit.ref, "request": str(request_id), "at": stamp} for question, hit in pairs]
    upsert_job_record(Path(state_dir), str(job.message_id), **{JOB_FIELD: rows[-20:]})


def signal(state_dir, hits) -> None:
    """A ``dedup`` signal of each entry a question was answered from (core/memory_usage.py); best effort."""
    try:
        from covey.core import memory_usage

        memory_usage.signal(Path(state_dir), [hit.ref for hit in hits], "dedup")
    except Exception:  # noqa: BLE001 - the count is not why a question is or is not refused
        pass


# The third same-class answer.

def _store(state_dir) -> Path:
    from covey.core import memstore

    return memstore.memory_dir(Path(state_dir)) / STORE


def quiet_until(state_dir, key) -> float:
    from covey.core.state import read_object

    try:
        value = (read_object(_store(state_dir)).get("quiet") or {}).get(key)
    except Exception:  # noqa: BLE001 - not quiet
        return 0.0
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def keep_quiet(state_dir, key, now=None) -> None:
    """No class question for ``key`` for CLASS3_QUIET_DAYS (what has passed is let go)."""
    from covey.core.locks import shared_file_lock
    from covey.core.state import read_object, write_json

    stamp = time.time() if now is None else now
    path = _store(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with shared_file_lock(path):
        data = read_object(path)
        quiet = {name: until for name, until in (data.get("quiet") or {}).items()
                 if isinstance(until, (int, float)) and until > stamp}
        quiet[key] = stamp + CLASS3_QUIET_DAYS * 86400
        write_json(path, {**data, "quiet": quiet}, ensure_ascii=False, indent=1)


def class_name(question) -> str:
    kind = category(query_of(question))
    return CLASS_WORDS.get(kind) or _cut(asked(question), 16)


def class3(ctx, record: dict, questions, now=None) -> list:
    """The question the card adds (one at most) when a question of ``questions`` is the third of its class answered the
    same way in CLASS_DAYS days: [] while nothing may be matched, for the high-risk classes, or for a class kept quiet."""
    try:
        found = engine()
        if found is None or not enabled(ctx):
            return []
        search, mutual, _ = found
        stamp = time.time() if now is None else now
        state = Path(ctx.state_dir)
        topic = str(record.get("topic_id") or getattr(ctx, "topic_id", "") or "")
        project = project_of(state, topic)
        for question in questions or ():
            if not isinstance(question, dict) or not question.get("id") or not question.get("options") or risky(question):
                continue
            kind = category(query_of(question))
            key = f"{project or 'global'}:{kind}:{hashlib.sha1(class_name(question).encode()).hexdigest()[:8]}"
            if quiet_until(state, key) > stamp:
                continue
            context = search_context(topic, project, CLASS_SEARCH_TAU, "class")
            if not project:  # An ordinary topic's class is every ordinary topic's (``ours``): read past the topic's own.
                context["owner"] = True
            same = {}
            for entry, _ in _candidates(state, search, question, context, CLASS_CANDIDATES):
                ours = entry.owner_key == project if project else (entry.scope == "topic" and not project_of(state, entry.owner_key))
                if not ours:
                    continue
                if stamp - float(entry.at or 0) > CLASS_DAYS * 86400:
                    continue
                old, answer = split(entry)
                sim, _covered = mutual(asked(question), old, state_dir=state) if old else (0.0, False)
                if not old or category(f"{old} {answer}") != kind or float(sim) < CLASS_TAU or not polarity(answer):
                    continue
                same.setdefault(polarity(answer), set()).add(entry.ref)
            if not any(len(refs) >= CLASS3_AFTER - 1 for refs in same.values()):
                continue
            name = class_name(question)
            return [{"id": EXTRA_ID, "header": "以后", "question": f"以后这类（{name}）还要问你吗？",
                     "options": [{"label": KEEP_ASKING, "description": f"照常问；{CLASS3_QUIET_DAYS} 天内不再问这一句"},
                                 {"label": FOLLOW, "description": "记成一条规矩：以后同类的题按这次的回答直接做"}],
                     "class": {"key": key, "category": kind, "name": name, "for": str(question["id"]),
                               "scope": "project" if project else "global", "project_id": project}}]
        return []
    except Exception:  # noqa: BLE001 - the card goes without it
        return []


# The card and the answer.

def drawn(record) -> dict:
    """``record`` as its card draws it: each question answered before with the grey line under its words, then the
    ``extra_questions`` (cards/question_cards.py ``with_dedup``, which every drawing of the open form goes through). Only for
    drawing: the record's ``questions`` are never changed. ``record`` itself without either."""
    from covey.cards.question_cards import with_dedup

    return with_dedup(record)


def extra_answers(record, fields) -> dict:
    """{extra question id: the label picked} of a submitted card form (the extra questions come after ``questions``)."""
    extra = [q for q in record.get(EXTRA_KEY) or () if isinstance(q, dict) and q.get("id")]
    if not extra or not isinstance(fields, dict):
        return {}
    start, picked = len(record.get("questions") or ()), {}
    for offset, question in enumerate(extra):
        value = fields.get(f"option_{start + offset}")
        value = value[0] if isinstance(value, list) and value else value
        options = [o for o in question.get("options") or () if isinstance(o, dict)]
        try:
            index = int(value)
        except (TypeError, ValueError):
            continue
        if 0 <= index < len(options):
            picked[str(question["id"])] = str(options[index].get("label") or "")
    return picked


def settle(state_dir, topic_id, record, given, *, owner: bool, log=None, now=None) -> None:
    """What the owner picked in the extra questions of the answered ``record``: FOLLOW writes the class's rule (the owner's
    words, by the Bridge, its origin this card) and keeps the class quiet, KEEP_ASKING only keeps it quiet. ``given``: question
    id -> what was answered to it (skipped ones left out). ``owner``: the owner's own submit; anybody else's (the lead's in the
    owner's place) writes nothing."""
    picked = record.get(EXTRA_ANSWERS) if isinstance(record.get(EXTRA_ANSWERS), dict) else {}
    if not owner or not picked:
        return
    from covey.core import memstore

    for question in record.get(EXTRA_KEY) or ():
        try:
            if not isinstance(question, dict) or not isinstance(question.get("class"), dict):
                continue
            label, kind = picked.get(str(question.get("id"))), question["class"]
            if label == FOLLOW:
                answer = _cut(given.get(str(kind.get("for"))) or "", 80)
                if not answer:
                    continue
                scope = "project" if kind.get("scope") == "project" and kind.get("project_id") else "global"
                memstore.add(Path(state_dir), kind="rule", scope=scope, owner_key=kind.get("project_id") if scope == "project" else "",
                             text=_cut(f"{kind.get('name')}这类题直接按“{answer}”做，不用再问", memstore.TEXT_CHARS),
                             title=_cut(f"{kind.get('name')}直接按{answer}做", memstore.TITLE_CHARS), said="owner", by="bridge",
                             origin={"topic_key": str(topic_id or ""), "question": str(record.get("token") or ""),
                                     "job": str(record.get("job_key") or "")},
                             who="owner", now=now)
            if label in (FOLLOW, KEEP_ASKING):
                keep_quiet(state_dir, str(kind.get("key") or ""), now=now)
        except Exception as exc:  # noqa: BLE001 - the answer is delivered whether or not this took
            if log is not None:
                log(f"question class answer not kept: {type(exc).__name__}")
