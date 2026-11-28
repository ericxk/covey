"""What a turn reads of the owner's memory (COVEY_MEMORY=1, core/memstore.py, docs/butler.md 记忆), for app/runs.py: the rules in
the turn's rules (``block``), the rest at the tail of its message (``tail``), the codes of both in its job record (``record``).

``block`` is the 规矩 alone: 全局 ones on the owner's own turn (``owner_turn()``), the project's in an ordinary topic of a project,
the topic's own; the owner's first, then as they were written; past a scope's room (RULE_CAPS: 全局 15 and 1,200 characters,
专项 10 and 1,000, 话题 5 and 400, given to the owner's first and then the newest: ``pick_rules``) only their titles, never one
dropped. It is the same every turn of a topic while its rules
are, so the rules stay one cacheable prefix. ``tail`` is what changes turn by turn: the decisions in progress (the topic's own
until it ends or the same question has a newer answer; another topic's of the project while that topic is going and has not
delivered a commit since, and at most IN_PROGRESS_HOURS old otherwise: ``in_progress``), the notes that the turn's words, the
topic's title and its 话题要点 find (core/memory_search.py, at most NOTES with sim ≥ DEFAULT_TAU and NOTE_CHARS characters),
then the titles of the related entries not carried. ``preview`` is the same reckoning for the web page (data contract v43
MemoryPreview).

Who reads what is as it was: a 代答 topic nothing; a line from a topic with redaction on only one with it on; 待审核 never.
Nothing is written but the job record's ``memory_ids`` (carried whole: {n, ref, scope, kind, by}) and ``memory_listed``
(titles only: {n, ref}) — app/memory_observe.py counts them once the turn has ended — and nothing raises into the turn.
``fix_rule`` says whether the turn's rules tell the model it may write ``memory`` and ``memory_void`` into its checkpoint
(app/memory_collect.py reads them once the turn has ended)."""
from __future__ import annotations

import re
import shlex
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from covey.app import assist, knowledge_turn, project_context
from covey.app.prompts import memory_rules_block, memory_tail_block
from covey.core import (knowledge, memory, memory_review, memory_search, memory_usage, memstore, principles, projects,
                        topic_gist, topic_memory)
from covey.core import turn_source
from covey.core.butler import is_butler_topic
from covey.core.butler_opened import handed_on
from covey.core.jobs import job_key, topic_job_records, upsert_job_record
from covey.core.paths import entry_script
from covey.core.process import device_of
from covey.core.redaction import topic_redaction
from covey.core.state import epoch_seconds
from covey.core.topics import load_topic, proxy_topic, topic_number

WORDS = memory.CORRECTION_WORDS  # The owner's words that start a correction.
SAVED_WORDS = ("记一下", "记住", "记下来")  # COVEY_KNOWLEDGE's own (app/knowledge_collect.py): it takes those.
RULE_CAPS = memory_review.RULE_CAPS  # (entries, characters) of a scope's rules: past them only titles (体检 says over_cap).
DECISION_CHARS = 600
DECISION_LINE = 240
NOTES = 5
NOTE_CHARS = 800
LISTED = 10
LISTED_CHARS = 300
IN_PROGRESS_HOURS = 48
DECISION_DAYS = 14  # Another topic's decisions older than this are not in progress: their topic's records are not read.
QUERY_CHARS = 2000  # Of the owner's words a turn's notes are looked up by.


@dataclass
class Plan:
    """What one turn carries of the memory."""

    rules: list = field(default_factory=list)  # memstore Entry, carried whole in the rules
    left: list = field(default_factory=list)  # rules past their scope's room: titles only
    decisions: list = field(default_factory=list)  # carried whole at the tail
    notes: list = field(default_factory=list)  # (Entry, sim) carried whole at the tail
    listed: list = field(default_factory=list)  # related hits not carried: dicts of core/memory_search.py
    project: str = ""
    project_name: str = ""
    numbers: dict = field(default_factory=dict)  # topic id -> its T number, of the decisions' topics
    names: dict = field(default_factory=dict)  # project id -> its name, of the notes' projects


def owner_words(record, state_dir=None) -> str:
    """The owner's own words of the turn of job ``record``: its ``user_text``, but of an answer's turn (app/answers.py
    answer_message) only what he answered and the notes he added, and of a feedback only the feedback; the question's own
    words, written by the Bridge or a model, are never his. ``state_dir``: where the question record is, to tell an answer of
    several lines from its question exactly (app/answer_cards.py asked_questions)."""
    from covey.app.answer_cards import asked_questions
    from covey.app.answers import FEEDBACK_START, answer_parts
    said = str(record.get("user_text") or "")
    if said.startswith(FEEDBACK_START):
        return said[len(FEEDBACK_START):]
    asked = asked_questions(state_dir, str(record.get("message_id") or "")) if state_dir is not None else []
    parts = answer_parts(said, asked)
    if parts:
        return "\n".join(text for kind, text in parts if kind in ("answer", "note"))
    return said


def active(ctx) -> bool:
    return memory.enabled() and not proxy_topic(ctx)


def redacted(ctx, topic_id) -> bool:
    """Whether the topic has redaction on; off when it cannot be read (the lines that need it are then left out)."""
    return project_context.marked(ctx.state_dir, topic_id)


# ------------------------------------------------------------------------------------------------------------ the rules


def _readable(entry, redacted_on) -> bool:
    return not (entry.redacted and not redacted_on)


def _rule_order(entry):
    return (entry.said != "owner", entry.at, entry.code)


def _room_order(entry):
    return (entry.said != "owner", -entry.at, entry.code)


def pick_rules(found, scope) -> tuple[list, list]:
    """(carried, titles only) of one scope's rules ``found``: the room (RULE_CAPS) goes to the owner's first, then the
    newest, so a store past its room lists only its oldest rules not the owner's; both are then in the written order
    (``_rule_order``), the prefix the turn's rules make staying the same while the rules do. app/web_memory.py marks the
    same ones ``over_cap``."""
    count, chars = RULE_CAPS[scope]
    picked, over, used = [], [], 0
    for entry in sorted(found, key=_room_order):
        if len(picked) < count and used + len(entry.text) <= chars:
            picked.append(entry)
            used += len(entry.text)
        else:
            over.append(entry)
    return sorted(picked, key=_rule_order), sorted(over, key=_rule_order)


def rules_of(state_dir, *, topic="", project="", owner=False, redacted_on=False) -> tuple[list, list]:
    """(carried, titles only) of the rules a turn of ``topic`` in ``project`` reads (the module's docstring)."""
    carried, left = [], []
    scopes = [("global", "")] if owner else []
    scopes += [("project", project)] if project else []
    scopes += [("topic", topic)] if topic else []
    for scope, key in scopes:
        found = [entry for entry in memstore.entries(state_dir, scope=scope, owner_key=key, kinds=("rule",))
                 if _readable(entry, redacted_on)]
        picked, over = pick_rules(found, scope)
        carried += picked
        left += over
    return carried, left


# -------------------------------------------------------------------------------------------------------- the decisions


def _asked_in(entry) -> str:
    return str((entry.origin or {}).get("topic_key") or "")


def _record_at(record) -> float:
    for key in ("finished_at", "updated_at", "created_at"):
        found = epoch_seconds(record.get(key)) if isinstance(record.get(key), str) else None
        if found:
            return found
    return 0.0


def _topic_state(state_dir, topic_id, cache) -> tuple[float, list]:
    """(the time of its last turn, the times it delivered a commit) of ``topic_id``, from its job records; read once."""
    if topic_id not in cache:
        last, commits = 0.0, []
        try:
            records = topic_job_records(Path(state_dir), topic_id)
        except (OSError, ValueError):
            records = []
        for record in records:
            at = _record_at(record)
            last = max(last, at)
            delivery = record.get("delivery")
            if isinstance(delivery, dict) and str(delivery.get("commit") or "").strip():
                commits.append(at)
        cache[topic_id] = (last, commits)
    return cache[topic_id]


def in_progress(state_dir, entry, topic, *, now, cache=None) -> bool:
    """Whether the active decision ``entry`` is in progress for a turn of ``topic`` (docs/butler.md 记忆): the topic's own
    always (it is done when the topic ends or the same question is answered anew: the store says so); another topic's while
    that topic has not delivered a commit since it was decided and either it was decided within IN_PROGRESS_HOURS or that
    topic had a turn within them. A delivery ends it for the others only."""
    cache = {} if cache is None else cache
    asked = _asked_in(entry)
    if entry.status != "active":
        return False
    if (topic and asked == topic) or (topic and entry.scope == "topic" and entry.owner_key == topic):
        return True
    if now - entry.at > DECISION_DAYS * 86400:
        return False
    recent = now - entry.at <= IN_PROGRESS_HOURS * 3600
    if not asked:
        return recent
    last, commits = _topic_state(state_dir, asked, cache)
    if any(at >= entry.at for at in commits):
        return False
    return recent or now - last <= IN_PROGRESS_HOURS * 3600


def decisions_of(state_dir, *, topic="", project="", redacted_on=False, now=None, cache=None) -> list:
    """The decisions in progress a turn of ``topic`` in ``project`` carries, the newest first, a question once. ``cache``:
    the asking topics' job records as ``in_progress`` read them, shared by the caller (the web page's previews of one build)."""
    now = time.time() if now is None else now
    found = []
    if topic:
        found += memstore.entries(state_dir, scope="topic", owner_key=topic, kinds=("decision",))
    if project:
        found += memstore.entries(state_dir, scope="project", owner_key=project, kinds=("decision",))
    cache = {} if cache is None else cache
    seen, out = set(), []
    for entry in sorted(found, key=lambda e: -e.at):
        if not _readable(entry, redacted_on) or not in_progress(state_dir, entry, topic, now=now, cache=cache):
            continue
        question = (entry.owner_key, _asked_in(entry), entry.title)
        if question in seen:
            continue  # The same question answered again: the newer answer holds.
        seen.add(question)
        out.append(entry)
    return out


# ------------------------------------------------------------------------------------------------------------ the notes


_SENTENCES = re.compile(r"[。！？!?；;\n]+")
SENTENCE_MIN = 6  # Characters of a sentence of the owner's words looked up on its own.
SENTENCES = 8  # Of them, the first.


def queries_of(state_dir, topic, words="", *, title="", repos=()) -> list:
    """What a turn's notes are looked up by, each on its own (one long text is covered by no entry): the topic's title, its
    话题要点 (essence, goal, involves) with the repos it uses, and the first SENTENCES sentences of the owner's words."""
    found = [title]
    record = {}
    if topic:
        try:
            record = load_topic(state_dir, topic)
        except (OSError, ValueError):
            record = {}
        if not title:
            found.append(str(record.get("title") or ""))
        gist = topic_gist.gist_of(topic_gist.read(state_dir, topic))
        found += [str(gist.get(key) or "") for key in ("essence", "goal", "involves")]
    names = [str(item.get("name") if isinstance(item, dict) else item) for item in (*(record.get("repos") or ()), *repos)]
    if names:
        found.append(" ".join(names))
    sentences = [part.strip() for part in _SENTENCES.split(str(words or "")[:QUERY_CHARS]) if len(part.strip()) >= SENTENCE_MIN]
    found += sentences[:SENTENCES]
    return [query for query in dict.fromkeys(found) if query.strip()]


def _note_like(item, topic, project) -> bool:
    """A hit a turn may carry as 资料: a note, or another project's (or topic's) rule, active."""
    if item["status"] != "active":
        return False
    if item["kind"] == "note":
        return True
    if item["kind"] != "rule":
        return False
    return item["scope"] != "global" and item["owner_key"] != topic and not (project and item["project"] == project)


def notes_of(state_dir, queries, *, topic="", project="", owner=False, redacted_on=False, carried=()) -> tuple[list, list]:
    """((Entry, sim) carried as 资料, the related hits only listed) for ``queries`` (``queries_of``)."""
    if not queries:
        return [], []
    context = {"topic": topic, "project": project, "owner": owner, "redacted": redacted_on, "tau": memory_search.DEFAULT_TAU}
    hits = memory_search.rank_many(memory_search.index(state_dir), queries, context)
    taken, notes, listed, used = set(carried), [], [], 0
    for item in hits:
        if item["ref"] in taken:
            continue
        if len(notes) < NOTES and _note_like(item, topic, project) and used + len(item["text"]) <= NOTE_CHARS:
            entry = memstore.find(state_dir, item["ref"])
            if entry is not None:
                notes.append((entry, item["sim"]))
                used += len(item["text"])
                taken.add(item["ref"])
                continue
        if item["kind"] == "rule" and item["status"] == "active" and (item["scope"] == "global" or item["owner_key"] == topic
                                                                     or (project and item["project"] == project)):
            continue  # A rule of the turn's own scopes: in the rules already, or only its title is (``left``).
        listed.append(item)
        taken.add(item["ref"])
    return notes, listed


# -------------------------------------------------------------------------------------------------------------- the plan


_plans: dict = {}
_PLANS_KEPT = 64


def make_plan(state_dir, *, topic="", project="", owner=False, redacted_on=False, words="", title="", repos=(),
              now=None, cache=None) -> Plan:
    """What a turn of ``topic`` in ``project`` carries (``owner``: the owner's own turn; ``words``: the owner's words of
    it). The same arguments and memory give the same plan."""
    now = time.time() if now is None else now
    state_dir = Path(state_dir)
    rules, left = rules_of(state_dir, topic=topic, project=project, owner=owner, redacted_on=redacted_on)
    decisions = decisions_of(state_dir, topic=topic, project=project, redacted_on=redacted_on, now=now, cache=cache)
    kept, shown = [], 0
    for entry in decisions:
        size = min(len(entry.text), DECISION_LINE)
        if shown + size <= DECISION_CHARS:
            kept.append(entry)
            shown += size
    spare = [entry for entry in decisions if entry not in kept]
    queries = queries_of(state_dir, topic, words, title=title, repos=repos)
    carried = [entry.ref for entry in (*rules, *left, *kept)]
    notes, listed = notes_of(state_dir, queries, topic=topic, project=project, owner=owner, redacted_on=redacted_on,
                             carried=carried)
    listed = [{"ref": e.ref, "code": e.code, "kind": e.kind, "title": e.title} for e in spare] + listed
    trimmed, size = [], 0
    for item in listed:
        line = len(item.get("code") or "") + len(item["title"]) + 2
        if len(trimmed) >= LISTED or size + line > LISTED_CHARS:
            break
        trimmed.append(item)
        size += line
    name = ""
    if project:
        name = str((projects.read_record(state_dir, project) or {}).get("name") or "")
    numbers = {}
    for entry in kept:
        asked = _asked_in(entry) or (entry.owner_key if entry.scope == "topic" else "")
        if asked and asked not in numbers:
            try:
                numbers[asked] = topic_number(state_dir, asked)
            except (OSError, ValueError):
                numbers[asked] = 0
    names = {}
    for entry, _ in notes:
        owner_project = entry.owner_key if entry.scope == "project" else ""
        if owner_project and owner_project != project and owner_project not in names:
            names[owner_project] = str((projects.read_record(state_dir, owner_project) or {}).get("name") or "")
    return Plan(rules=rules, left=left, decisions=kept, notes=notes, listed=trimmed, project=project, project_name=name,
                numbers=numbers, names=names)


def _turn_project(ctx, job) -> str:
    found = project_context.member_record(ctx, job)
    return found[0] if found else ""


def plan(ctx, job, owner_turn) -> Plan:
    """``make_plan`` of ``job``'s turn, worked out once a turn (its rules, its tail and its record read the same one)."""
    topic = str(getattr(job, "topic_id", "") or ctx.topic_id or "")
    key = (str(ctx.state_dir), topic, str(getattr(job, "message_id", "")), str(getattr(job, "job_id", "")))
    stamp = memory_search.stamp(ctx.state_dir)
    found = _plans.get(key)
    if found is not None and found[0] == stamp and stamp[0] is not None:
        return found[1]  # Without the sentinel (memory/.rev) a change cannot be seen: worked out again.
    words = owner_words({"user_text": getattr(job, "user_text", ""), "message_id": getattr(job, "message_id", "")},
                        ctx.state_dir) if turn_source.behavior(job).owner_words else ""
    made = make_plan(ctx.state_dir, topic=topic, project=_turn_project(ctx, job), owner=bool(owner_turn()),
                     redacted_on=redacted(ctx, topic), words=words)
    if len(_plans) >= _PLANS_KEPT:
        _plans.pop(next(iter(_plans)))
    _plans[key] = (stamp, made)
    return made


def search_command(ctx, job, owner) -> str:
    """The command line a turn looks its memory up with (bin/covey_memory.py search, core/memory_search.py): this
    deployment's state and the turn's topic, ``--owner`` on the owner's own turn (his 全局 lines), ``--redacted`` in a topic
    with redaction on (the lines only such a topic reads); '' on a device, whose files are not this machine's."""
    if device_of(ctx):
        return ""
    topic_id = str(getattr(job, "topic_id", "") or ctx.topic_id or "")
    try:
        hidden = bool(topic_redaction(ctx.state_dir, topic_id))
    except ValueError:
        hidden = False  # Its redacted lines are then not looked up, as the block leaves them out.
    words = [sys.executable, "-B", str(entry_script("covey_memory.py")), "search", "--state", str(ctx.state_dir),
             "--topic", topic_id, *(["--owner"] if owner else []), *(["--redacted"] if hidden else [])]
    return " ".join(shlex.quote(word) for word in words) + ' "<一句话>"'


def block(ctx, job, *, owner_turn) -> str:
    """The memory's part of ``job``'s rules: its 规矩 (app/prompts.py memory_rules_block); '' with COVEY_MEMORY off, for a
    代答 topic, or with none."""
    try:
        if not active(ctx):
            return ""
        made = plan(ctx, job, owner_turn)
        return memory_rules_block(made.rules, made.left, project_name=made.project_name,
                                  search=search_command(ctx, job, owner_turn()))
    except Exception:  # noqa: BLE001 - the turn goes on without it
        return ""


def tail(ctx, job, owner_turn) -> str:
    """The memory's dynamic block of ``job``'s turn, at the prompt's tail after the checkpoint note (app/prompts.py
    memory_tail_block): the decisions in progress, this turn's relevant notes and the titles of the related ones not
    carried. Never in the rules, which stay one cacheable prefix. '' with nothing to say."""
    try:
        if not active(ctx):
            return ""
        made = plan(ctx, job, owner_turn)
        return memory_tail_block(made.decisions, made.notes, made.listed, numbers=made.numbers, project=made.project,
                                 names=made.names)
    except Exception:  # noqa: BLE001 - the turn goes on without it
        return ""


def ids_of(made: Plan) -> tuple[list, list]:
    """(memory_ids, memory_listed) of a plan: what went in whole, what only by its title."""
    whole = [*made.rules, *made.decisions, *(entry for entry, _ in made.notes)]
    ids = [{"n": entry.code, "ref": entry.ref, "scope": entry.scope, "kind": entry.kind, "by": entry.by} for entry in whole]
    listed = [{"n": entry.code, "ref": entry.ref} for entry in made.left]
    listed += [{"n": item.get("code") or "", "ref": item["ref"]} for item in made.listed]
    return ids, listed


def record(ctx, job, owner_turn, log=None) -> None:
    """Write the codes of the entries the turn carries into its job record (``memory_ids``, ``memory_listed``)."""
    try:
        if not active(ctx):
            return
        ids, listed = ids_of(plan(ctx, job, owner_turn))
        upsert_job_record(ctx.state_dir, job_key(job.message_id, job.job_id), memory_ids=ids, memory_listed=listed)
    except Exception as exc:  # noqa: BLE001 - the record is a convenience for the correction after the turn
        if log is not None:
            log(f"memory ids not recorded: {type(exc).__name__}")


def preview(state_dir, topic_key: str = "", *, project_key: str = "", title: str = "", query: str = "",
            repos: tuple = (), cache: dict | None = None) -> dict:
    """What a turn of ``topic_key`` would carry, as the web page shows it (data contract v43 MemoryPreview): {topic_key, title,
    chars, rules, decisions, notes: [{code, sim}], listed}; the owner's own turn, the topic's redaction as it is set."""
    state_dir = Path(state_dir)
    topic = str(topic_key or "")
    project = str(project_key or "")
    if topic and not project:
        found = projects.topic_project(state_dir, topic)
        project = found.id if found is not None and found.role == projects.MEMBER else ""
    hidden = project_context.marked(state_dir, topic) if topic else False
    shown = title
    if topic and not shown:
        try:
            shown = str(load_topic(state_dir, topic).get("title") or "")
        except (OSError, ValueError):
            shown = ""
    made = make_plan(state_dir, topic=topic, project=project, owner=True, redacted_on=hidden, words=query, title=title,
                     repos=tuple(repos or ()), cache=cache)
    text = memory_rules_block(made.rules, made.left, project_name=made.project_name, search="")
    text += memory_tail_block(made.decisions, made.notes, made.listed, numbers=made.numbers, project=made.project,
                              names=made.names)
    ids, listed = ids_of(made)
    return {"topic_key": topic, "title": shown, "chars": len(text), "rules": [entry.code for entry in made.rules],
            "decisions": [entry.code for entry in made.decisions],
            "notes": [{"code": entry.code, "sim": round(float(sim), 4)} for entry, sim in made.notes],
            "listed": [row["n"] for row in listed if row["n"]]}


def build(ctx, job, owner_turn) -> tuple[list, int]:
    """The older block's items (core/memory.py select of the principles, the knowledge entries and the project's and the
    topic's lines), for the 设置 card's 记忆 tab (app/memory_panel.py) until it reads the store; a turn no longer carries
    them (``block``, ``tail``)."""
    topic_id = getattr(job, "topic_id", "") or ctx.topic_id
    owner = owner_turn()
    state = ctx.state_dir
    mine, space = principles.for_turn(state, ctx.workspace_name)
    entries = knowledge.for_turn(state, ctx.workspace_name) if knowledge.enabled() else ((), ())
    project_id, rows = project_context.memory_rows(ctx, job)
    used = memory_usage.last_used(state) if memory.learning() else None
    return memory.select(memory.items(principles=(mine if owner else [], space), entries=(entries[0] if owner else [], entries[1]),
                                      project=rows, topic=topic_memory.live(state, topic_id), project_id=project_id,
                                      redacted=redacted(ctx, topic_id), drafts=((), ()), used=used))


def triggered(text, *, knowledge_takes=False) -> bool:
    """Whether the owner's words of the turn hold one of ``WORDS``; ``knowledge_takes``: COVEY_KNOWLEDGE's 记住 and the like
    are its own (the entry goes there, once)."""
    text = str(text or "")
    return any(word in text for word in WORDS if not (knowledge_takes and word in SAVED_WORDS))


def learner_reads(ctx) -> bool:
    """Whether the Bridge reads the owner's words after the turn (app/memory_learn.py): the memory learning and the 辅助模型 not set
    to 不用模型 (the learner then asks nothing, and the checkpoint rule is the one path left to note a correction)."""
    try:
        return memory.learning() and not assist.settings_of(ctx.state_dir).assist.off
    except Exception:  # noqa: BLE001 - unreadable settings: the learner is taken to be there, as before
        return memory.learning()


def fix_rule(ctx, job, owner_turn) -> bool:
    """Whether the rules of this turn carry the memory correction rule: memory on, the owner's own turn in a topic (not the
    butler's, which has its tools; not one the butler handed on), and their words hold one of ``WORDS``."""
    try:
        if not active(ctx) or learner_reads(ctx) or not owner_turn() or handed_on(getattr(job, "message_id", "")):
            return False  # With the learner reading the owner's words after the turn (app/memory_learn.py), the rule is left out.
        if not turn_source.behavior(job).owner_words:
            return False  # A 协作 turn's words are not the owner's (core/turn_source.py): nothing of them is his to correct.
        if is_butler_topic(getattr(job, "topic_id", "") or ctx.topic_id, getattr(job, "chat_id", "")):
            return False
        takes = knowledge_turn.remembers(ctx, job, owner_turn=owner_turn)
        said = owner_words({"user_text": getattr(job, "user_text", ""), "message_id": getattr(job, "message_id", "")},
                           ctx.state_dir)
        return triggered(said, knowledge_takes=takes)
    except Exception:  # noqa: BLE001
        return False
