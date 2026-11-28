"""The web page's memory: the 记忆 tab of data contract v37 (COVEY_MEMORY=1) and the one-page 记忆 of data contracts v43/v45.

v37 (``view``): the ``settings`` item ``memory`` (app/web_sync.py push_memory), the owner's main memory, 全局 and each
workspace's, read as the 设置 card's 记忆 tab once did: the principles (core/principles.py) and, with COVEY_KNOWLEDGE=1, the
knowledge entries (core/knowledge.py), each group with the room it uses of its scope's limit and which entries the next turn
carries, through core/memory.py ``items``, ``fill`` and ``select`` exactly as a turn does, and the block a new topic's turn in
each workspace would read (app/prompts.py memory_block). Read only: v37's ``memory_edit`` without ``v`` is gone (T908 M3b; the
page writes through v43's).

v45 (``Center``, ``edit_v43``; docs/web.md 数据约定 v45, tests/golden/web_memory_v43.json): the page's ``settings`` items, each
a key of core/web_sync.py ``MEMORY_KEY_RE`` read from the one store (core/memstore.py):

- ``memory:index``, the small key the page polls: the counts (``counts.pending`` is the badge: the open items of 待审核), each
  scope's counts, every other key's ``rev`` and the ⌘K title table (``titles``: [code, title, key], at most TITLES).
- ``memory:global``: the 全局 entries, the open items of 待审核 (``review``) and the latest LOG_SHOWN changes (``log``, without
  the Bridge's own notes of the owner's answers); past KEY_BYTES the log alone is cut, never the review.
- ``memory:topics``: the entries of the topics with memory written in the last TOPIC_DAYS days (core/memstore.py ``topics``:
  no topic directory is walked), ``memory:p:<project id>`` one project's. A rule past its scope's room has ``over_cap`` (the
  turn lists only its title); a decision's origin names the T number of the topic it was asked in.

v43 had 体检 (``health``), the rooms of the rules, memory:previews and memory:import:<page> besides: gone in v45, with the
Harnesses' memory import (memory/imports.json stays as it is; nothing reads it).

What a turn did with an entry (how often it was carried, used) is never in an entry: an entry's ``rev`` moves only when what
the page shows of it does, so a turn does not make every key go again. ``stamps`` stats the one sentinel ``memory/.rev`` that
every memory write touches (core/memory_rev.py). ``edit_v43`` does the 15 actions of a ``memory_edit`` with ``v: 43`` through
core/memstore.py and, for 待审核, core/memory_review.py ``decide``/``decide_all`` (``review_decide``); the operation's id is
the change's ``undo_token``. No model, no Feishu call.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from covey.app import memory_turn
from covey.app.prompts import memory_block
from covey.cards.memory_panel import center_url as memory_card_url
from covey.core import knowledge, memory, memory_review, memory_rev, memory_usage, memstore, principles, projects, web_sync
from covey.core.jobs import job_path, read_job_record
from covey.core.state import read_object
from covey.core.topics import topic_file
from covey.core.workspaces import load_config

GLOBAL, WORKSPACE = memory.GLOBAL, memory.WORKSPACE
PRINCIPLE, KNOWLEDGE = "principle", "knowledge"
PREVIEWS = 24  # Workspaces the next turn is previewed for: the default one and those with entries, the default first.
ORIGIN_WORDS = {"web": "网页上记的", "settings_card": "设置卡里升为全局的", "memory_candidates": "晚间候选里记下的",
                "memory_learn": "自动学习记的"}
QUOTE_SHOWN = 200  # Characters of the owner's own words an entry's origin carries.
NOTE_SHOWN = 60  # Characters of a source's note the page shows after where it came from.


def enabled() -> bool:
    return memory.enabled()


def stamps(state_dir) -> tuple:
    """What a change of the memory shows in, read each step of the pass thread: the one sentinel ``memory/.rev`` (its mtime and
    its ctime, which a touch with a given time still moves), which every memory write touches. Planted on the first look when
    it is not there yet, so the older stores' writes (core/memory_rev.py ``touch`` with ``create`` False) touch it from then on."""
    path = memstore.rev_path(state_dir)
    try:
        found = path.stat()
    except FileNotFoundError:
        memory_rev.touch(state_dir)
        try:
            found = path.stat()
        except OSError:
            return (0, 0)
    except OSError:
        return (0, 0)
    return (found.st_mtime_ns, found.st_ctime_ns)


def workspace_names(state_dir, default_workdir) -> tuple[str, list]:
    """(the default workspace, every workspace's name); ('', []) when the list cannot be read."""
    try:
        config = load_config(state_dir, default_workdir)
    except (OSError, ValueError, TypeError, AttributeError):
        return "", []
    return str(config["default"] or ""), [str(name) for name in config["workspaces"]]


def _job_topic(state_dir, job_key, cache) -> tuple[str, bool]:
    """(the topic number "T83" or '', whether it is the butler's topic) of the job ``job_key``, '' and False when it cannot be read."""
    if job_key not in cache:
        found = ("", False)
        try:
            record = read_job_record(job_path(Path(state_dir), job_key)) or {}
            topic = read_object(topic_file(Path(state_dir), str(record.get("topic_id") or ""))) if record.get("topic_id") else {}
            number = topic.get("topic_number")
            found = (f"T{number}" if type(number) is int else "", topic.get("main_thread") is True)
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        cache[job_key] = found
    return cache[job_key]


def _topic_number(state_dir, topic_id, cache) -> str:
    key = f"topic:{topic_id}"
    if key not in cache:
        try:
            number = read_object(topic_file(Path(state_dir), str(topic_id))).get("topic_number")
        except (OSError, ValueError, TypeError, AttributeError):
            number = None
        cache[key] = f"T{number}" if type(number) is int else ""
    return cache[key]


def origin_of(source, by="", state_dir=None, cache=None) -> dict:
    """Where a principle or an entry came from, by what its ``source`` really says: {origin (a few words, '' when it does not say),
    quote (the owner's own words it was learned from), topic (the topic number "T83" it was written in, or the candidates' topics
    joined)}. ``by``: a knowledge entry's own ``by`` (main: the butler's tool, topic: a topic's turn, import). A ``note`` in the
    source (what a later look at the line found of where it came from) follows the words."""
    found = _origin_bare(source, by, state_dir, cache)
    note = " ".join(str((source if isinstance(source, dict) else {}).get("note") or "").split())[:NOTE_SHOWN]
    if note:
        found["origin"] = f"{found['origin']}；{note}" if found["origin"] else note
    return found


def _origin_bare(source, by="", state_dir=None, cache=None) -> dict:
    source = source if isinstance(source, dict) else {}
    cache = {} if cache is None else cache
    quote = " ".join(str(source.get("quote") or "").split())[:QUOTE_SHOWN]
    for key in ("via", "by"):
        word = ORIGIN_WORDS.get(source.get(key))
        if word is None:
            continue
        topic = ""
        if source.get(key) == "memory_candidates" and state_dir is not None:
            names = [_topic_number(state_dir, tid, cache) for tid in list(source.get("topics") or ())[:6]]
            topic = "、".join(name for name in names if name)
        elif source.get(key) == "memory_learn" and state_dir is not None and source.get("job_key"):
            topic = _job_topic(state_dir, str(source["job_key"]), cache)[0]
        if topic:
            word += f"（话题 {topic}）"
        return {"origin": word, "quote": quote, "topic": topic}
    topic = str(source.get("topic") or "")
    job_key = str(source.get("job_key") or "")
    butler = False
    if not topic and job_key and state_dir is not None:
        topic, butler = _job_topic(state_dir, job_key, cache)
    if source.get("from") and (job_key or topic):  # app/memory_collect.py: a line of a topic's memory raised to 全局.
        return {"origin": f"话题 {topic} 里升为全局的" if topic else "话题里升为全局的", "quote": quote, "topic": topic}
    if by == "topic" or (topic and not butler):
        return {"origin": f"话题 {topic} 里记的" if topic else "话题里记的", "quote": quote, "topic": topic}
    if by == "import":
        return {"origin": "导入的", "quote": quote, "topic": topic}
    if job_key or source.get("job_id"):
        return {"origin": "管家记的", "quote": quote, "topic": ""}
    return {"origin": "", "quote": quote, "topic": ""}


def origin(source, by="", state_dir=None, cache=None) -> str:
    """The words of ``origin_of``."""
    return origin_of(source, by, state_dir, cache)["origin"]


def _origin_facts(source, by, state_dir, cache) -> dict:
    found = origin_of(source, by, state_dir, cache)
    return {"origin": found["origin"], "quote": found["quote"], "topic": found["topic"]}


def _found(state_dir, workspace, knowledge_on, learn, used):
    """The items a new topic's turn in ``workspace`` reads ('' : 全局 alone)."""
    mine, space = principles.for_turn(state_dir, workspace)
    entries = knowledge.for_turn(state_dir, workspace) if knowledge_on else ((), ())
    return memory.items(principles=(mine, space), entries=entries, drafts=((), ()), used=used)


def _row(item, carried, facts) -> dict:
    """An entry of a group: what the item says, and of its store what the page shows besides (kind, origin, knowledge's kind and why).
    ``text`` is what the store holds (a knowledge entry's own words, not the line a turn reads, which adds its kind and why), the
    ``base`` of an edit; ``size`` is its characters as the editor counts them, ``chars`` what the line uses of the group's room
    (the number and the speaker around it count too)."""
    kind, _, rid = item["ref"].partition(":")
    extra = facts.get(item["ref"], {})
    text = extra.get("text", item["text"])
    row = {"key": item["ref"], "kind": kind, "text": text, "by": item["by"], "pinned": bool(item.get("pinned")),
           "pinnable": kind == PRINCIPLE, "editable": kind in (PRINCIPLE, KNOWLEDGE), "carried": item["ref"] in carried,
           "chars": memory.line_size(item), "size": len(text), "at": item["at"], "origin": extra.get("origin", ""),
           "edited": extra.get("edited", ""), "quote": extra.get("quote", ""), "topic": extra.get("topic", "")}
    if kind == KNOWLEDGE:
        row["knowledge_kind"], row["why"] = extra.get("kind", ""), extra.get("why", "")
    return row


def _group(scope, workspace, found, scopes, carried, facts) -> dict:
    info = scopes[scope]
    rows = [_row(item, carried, facts) for item in found if item["scope"] == scope]
    rows.sort(key=lambda row: (not row["pinned"], -row["at"], row["key"]))
    return {"scope": scope, "workspace": workspace, "title": "全局" if scope == GLOBAL else f"工作区 {workspace}",
            "used": info["used"], "limit": info["limit"], "left": len(info["left"]), "entries": rows}


def view(state_dir, default_workdir=None) -> dict:
    """The ``memory`` item's data (docs/web.md 设置条目): {on, knowledge, learn, limits, kinds, default, workspaces, groups,
    candidates, previews}. ``groups``: 全局 first, then each workspace that has entries, by name (an empty one the page adds to
    is listed once it has one). ``previews``: per workspace, the block a new topic's turn there reads (``text``), how many entries
    and characters it carries and how many it leaves out."""
    knowledge_on, learn = knowledge.enabled(), memory.learning()
    used = memory_usage.last_used(state_dir) if learn else None
    default, names = workspace_names(state_dir, default_workdir)
    loaded = principles.load(state_dir)
    facts, cache = {}, {}
    for row in principles.entries(state_dir):
        facts[f"principle:{row['id']}"] = {**_origin_facts(row.get("source"), "", state_dir, cache), "edited": row.get("edited", "")}
    spaces = set(loaded["workspaces"])
    if knowledge_on:
        for scope, name, group in knowledge.groups(state_dir):
            for entry in group.values():
                if entry["state"] == knowledge.CURRENT:
                    facts[f"knowledge:{entry['id']}"] = {**_origin_facts(entry.get("source"), entry.get("by"), state_dir, cache),
                                                          "text": entry["text"], "kind": entry.get("kind", ""), "why": entry.get("why", "")}
                    if scope == knowledge.WORKSPACE and name:
                        spaces.add(name)
    groups, previews = [], {}
    order = list(dict.fromkeys([default, *sorted(spaces)])) if default else sorted(spaces)
    for index, name in enumerate(order or [""]):
        found = _found(state_dir, name, knowledge_on, learn, used)
        scopes = memory.fill(found)
        kept, left = memory.select([dict(item) for item in found])
        carried = {item["ref"] for item in kept}
        if index == 0:  # 全局 reads the same whatever the workspace: its group once, from the first.
            groups.append(_group(GLOBAL, "", found, scopes, carried, facts))
        if name and name in spaces:
            groups.append(_group(WORKSPACE, name, found, scopes, carried, facts))
        if name and len(previews) < PREVIEWS:
            previews[name] = {"count": len(kept), "chars": sum(memory.line_size(item) for item in kept), "left": left,
                              "text": memory_block(kept, left, learn={"search": ""} if learn else None)}
    globals_ = groups[:1]
    groups = globals_ + sorted(groups[1:], key=lambda group: group["workspace"])
    return {"on": True, "knowledge": knowledge_on, "learn": learn,
            "limits": {"text": principles.TEXT_LIMIT, "group": principles.GROUP_LIMIT, "pinned": principles.PINNED_LIMIT,
                       GLOBAL: memory.LIMITS[GLOBAL], WORKSPACE: memory.LIMITS[WORKSPACE]},
            "kinds": [{"value": key, "text": word} for key, word in knowledge.KINDS.items()],
            "default": default, "workspaces": names, "groups": groups, "candidates": [], "previews": previews}


def short(text, limit=30) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


# ================================================================================ data contract v43/v45: the memory center

V43 = 43  # memory_edit's ``v`` (the payload is v43's, its actions fewer since v45).
V45 = 45  # memory:index's ``version``: no 体检, previews, imports or rooms since.
INDEX_KEY, GLOBAL_KEY, TOPICS_KEY = "memory:index", "memory:global", "memory:topics"
PROJECT_PREFIX = "memory:p:"
TOPIC_DAYS = 30  # A topic's memory is in memory:topics while something was written to it this recently.
TITLES = 2000  # Rows of the ⌘K title table.
KEY_BYTES = 256 * 1024  # Of one key's data as sent (UTF-8).
LOG_SHOWN = 100  # Latest changes in memory:global's log.
LOG_READ = 1000  # Latest lines of memory/log.jsonl read for it (the answers' own writes left out, then LOG_SHOWN kept).
REBUILD_SECONDS = 300  # A full pass builds the keys again at most this often while the sentinel did not move.
ORIGIN_TEXT = ("topic_key", "quote", "message_id", "question", "file", "harness")
FLAGS = ("vague", "dynamic", "repeat", "old_repo")
LIVE = ("active", "pending")


def _shown_origin(origin) -> dict:
    """An entry's origin as the contract has it (MemoryEntryOrigin): when, and of where it came from only the words the page shows."""
    origin = origin if isinstance(origin, dict) else {}
    out = {"at": float(memstore._number(origin.get("at")))}
    for key in ORIGIN_TEXT:
        value = " ".join(str(origin.get(key) or "").split())
        if value:
            out[key] = value[:QUOTE_SHOWN] if key == "quote" else value[:200]
    number = origin.get("topic_number")
    if type(number) is int:
        out["topic_number"] = number
    return out


def entry_view(entry, *, over_cap=False, topic_number=None, live=None, asked_number=None) -> dict:
    """One entry as the page reads it (MemoryEntry): never how often a turn carried or used it. ``over_cap``: a rule past its
    scope's room (the turn lists only its title). ``live``: whether a decision is 在办 (``Store.in_progress``), its status alone
    when not given; ``asked_number``: the T number of the topic a decision was asked in, when its origin does not say."""
    origin = _shown_origin(entry.origin)
    if entry.scope == "topic":  # Written in that topic: its own key and number when the line does not say.
        origin.setdefault("topic_key", entry.owner_key)
        if type(topic_number) is int:
            origin.setdefault("topic_number", topic_number)
    out = {"key": entry.ref, "code": entry.code, "kind": entry.kind, "scope": entry.scope, "title": entry.title,
           "text": entry.text, "status": entry.status, "said": entry.said, "by": entry.by, "origin": origin,
           "rev": memstore.rev_of(entry)}
    if entry.scope == "project":
        out["project_key"] = entry.owner_key
    elif entry.scope == "topic":
        out["topic_key"] = entry.owner_key
        if type(topic_number) is int:
            out["topic_number"] = topic_number
    if entry.why:
        out["why"] = entry.why
    if entry.repos:
        out["repos"] = list(entry.repos)
    if entry.device:
        out["device"] = entry.device
    if entry.verified_at:
        out["verified_at"] = entry.verified_at
    if over_cap:
        out["over_cap"] = True
    if entry.superseded_by:
        out["superseded_by"] = entry.superseded_by
    if entry.edited_at:
        out["edited"] = True
    if entry.kind == "decision":
        out["in_progress"] = in_progress(entry) if live is None else bool(live)
        if type(asked_number) is int and origin.get("topic_key"):
            origin.setdefault("topic_number", asked_number)
    return out


def in_progress(entry) -> bool:
    """Whether a decision is active: what 在办 is read as without the job records (``Store.in_progress`` says it with them)."""
    return entry.kind == "decision" and entry.status == "active"


def over_room(entries, scope) -> set:
    """The refs of the rules of one store past its room: the active rules a turn lists only by title as app/memory_turn.py
    ``pick_rules`` gives the room (the owner's first, then the newest); never dropped by use (裁决 A9)."""
    rules = [e for e in entries if e.kind == "rule" and e.status == "active"]
    _, left = memory_turn.pick_rules(rules, scope)
    return {e.ref for e in left}


def _counts(entries, pending=0, migrate=None) -> dict:
    """The active entries by kind and the open items of 待审核 (``pending``); ``migrate``, given, how many of those are what
    memory_v2 carried over (type migrate: rules carried as before, not the owner's to do; the page's badge leaves them out)."""
    found = {"rule": 0, "decision": 0, "note": 0, "pending": pending}
    if migrate is not None:
        found["migrate"] = migrate
    for entry in entries:
        if entry.status == "active":
            found[entry.kind] += 1
    return found


def _topic_facts(state_dir, topic_id, cache) -> dict:
    """{number, title, project, proxy} of a topic's topic.json, read once a build."""
    if topic_id not in cache:
        try:
            data = read_object(topic_file(Path(state_dir), str(topic_id)))
        except (OSError, ValueError, TypeError):
            data = {}
        project = data.get("project") if isinstance(data.get("project"), dict) else {}
        number = data.get("topic_number")
        title = " ".join(str(data.get("title") or data.get("topic_title") or "").split())
        cache[topic_id] = {"number": number if type(number) is int else None, "title": title[:80],
                           "project": str(project.get("id") or "") if projects.valid_id(project.get("id")) else "",
                           "proxy": bool(data.get("proxy"))}
    return cache[topic_id]


class Store:
    """One read of the memory for a build: every entry of 全局, of each project not cancelled and of the topics with memory
    written in the last TOPIC_DAYS days, each store's rules' room, the open items of 待审核."""

    def __init__(self, state_dir, now=None, numbers=None) -> None:
        self.state = Path(state_dir)
        self.now = time.time() if now is None else now
        self.topic_cache = {}
        self.numbers = {} if numbers is None else numbers  # topic id -> its T number (never changes: kept across builds).
        self.globals = memstore.entries(self.state, scope="global", statuses=None)
        self.projects = {}  # pid -> (title, entries)
        for record in projects.records(self.state):
            pid = str(record.get("id") or "")
            if not projects.valid_id(pid) or record.get("state") == projects.CANCELLED:
                continue
            found = memstore.entries(self.state, scope="project", owner_key=pid, statuses=None)
            if found or record.get("state") != projects.DONE:
                self.projects[pid] = (" ".join(str(record.get("name") or "").split())[:60] or pid, found)
        cutoff = self.now - TOPIC_DAYS * 86400
        listed = sorted(((tid, at) for tid, at in memstore.topics(self.state).items() if memstore._number(at) >= cutoff),
                        key=lambda pair: -memstore._number(pair[1]))
        self.topics = {}  # tid -> entries, the latest written first
        for tid, _ in listed:
            found = memstore.entries(self.state, scope="topic", owner_key=tid, statuses=None)
            if found:
                self.topics[tid] = found
        self.review = memstore.review_items(self.state)
        self.live_cache = {}  # app/memory_turn.py in_progress's: each asking topic's job records read once a build.
        self.over = set()
        for found, scope in [(self.globals, "global"), *((found, "project") for _, found in self.projects.values()),
                             *((found, "topic") for found in self.topics.values())]:
            self.over |= over_room(found, scope)

    def all(self) -> list:
        return [*self.globals, *(e for _, found in self.projects.values() for e in found),
                *(e for found in self.topics.values() for e in found)]

    def topic(self, tid) -> dict:
        return _topic_facts(self.state, tid, self.topic_cache)

    def in_progress(self, entry) -> bool:
        """在办 as the turns carry a decision (app/memory_turn.py ``in_progress``, docs/butler.md 记忆): a topic's
        own while it is active; a project's as the project's other topics carry it (14 days at most, ended by the asking
        topic's delivered commit, kept while it was decided or its topic had a turn within IN_PROGRESS_HOURS)."""
        if entry.kind != "decision":
            return False
        topic = entry.owner_key if entry.scope == "topic" else ""
        try:
            return memory_turn.in_progress(self.state, entry, topic, now=self.now, cache=self.live_cache)
        except (OSError, ValueError, TypeError):
            return in_progress(entry)

    def number(self, tid) -> int | None:
        """The T number of the topic ``tid`` (its topic.json read once, then kept)."""
        if tid not in self.numbers:
            self.numbers[tid] = self.topic(tid)["number"]
        return self.numbers[tid]

    def view(self, entry) -> dict:
        number = self.topic(entry.owner_key)["number"] if entry.scope == "topic" else None
        live = self.in_progress(entry) if entry.kind == "decision" else None
        asked = None
        if entry.kind == "decision":  # 上次你答过：…（T12 · 11-26）, the page's search says where it was asked.
            origin = entry.origin if isinstance(entry.origin, dict) else {}
            tid = str(origin.get("topic_key") or "")
            if tid and type(origin.get("topic_number")) is not int:
                asked = self.number(tid)
        return entry_view(entry, over_cap=entry.ref in self.over, topic_number=number, live=live, asked_number=asked)

    def pending(self, kind=None) -> dict:
        """Open items of 待审核 per scope summary key ('global', 'topics', 'p:<id>'), by where each would go; of ``kind`` only
        (an item's type) when given."""
        found = {}
        for item in self.review:
            if kind is not None and item.get("type") != kind:
                continue
            suggest = item.get("suggest") if isinstance(item.get("suggest"), dict) else {}
            pid = str(suggest.get("project_key") or "")
            key = f"p:{pid}" if suggest.get("scope") == "project" and pid in self.projects else \
                "topics" if suggest.get("scope") == "topic" else "global"
            found[key] = found.get(key, 0) + 1
        return found


def review_view(item, views) -> dict:
    """An open item of 待审核 as the page reads it (MemoryReviewItem); ``views`` ref -> entry view, for a group's entries."""
    suggest = item.get("suggest") if isinstance(item.get("suggest"), dict) else {}
    kind = suggest.get("kind") if suggest.get("kind") in memstore.KINDS else "note"
    scope = suggest.get("scope") if suggest.get("scope") in memstore.SCOPES else "global"
    out = {"key": str(item["key"]), "type": str(item.get("type") or ""), "title": memstore.clean(item.get("title"), memstore.TITLE_CHARS),
           "text": memstore.clean(item.get("text"), memstore.DECISION_CHARS), "suggest": {"kind": kind, "scope": scope},
           "created_at": float(memstore._number(item.get("created_at")))}
    if scope == "project" and projects.valid_id(suggest.get("project_key")):
        out["suggest"]["project_key"] = str(suggest["project_key"])
    if str(item.get("quote") or "").strip():
        out["quote"] = memstore.clean(item["quote"], QUOTE_SHOWN)
    origin = item.get("origin") if isinstance(item.get("origin"), dict) else {}
    where = {}
    if str(origin.get("topic_key") or origin.get("topic") or "").strip():
        where["topic_key"] = str(origin.get("topic_key") or origin.get("topic"))
    if type(origin.get("topic_number")) is int:
        where["topic_number"] = origin["topic_number"]
    if where:
        out["from"] = where
    if str(item.get("conflict_with") or "").strip():
        out["conflict_with"] = str(item["conflict_with"])
    if str(suggest.get("grant") or "").strip():  # 放行类: what of the five kinds its words name (core/memory_gate.py grants).
        out["suggest"]["grant"] = str(suggest["grant"])[:40]
    group = [views[ref] for ref in item.get("items") or () if isinstance(ref, str) and ref in views]
    if group:
        out["items"] = group
    flags = [flag for flag in item.get("flags") or () if flag in FLAGS]
    if flags:
        out["flags"] = flags
    return out


def answered(row) -> bool:
    """Whether a change of memory/log.jsonl is the Bridge's own note of what the owner answered on a question card (a decision
    written or settled by itself): not one of the page's (its operation's ``w-`` token), not an undo."""
    refs = [str(ref) for ref in row.get("refs") or ()]
    return (bool(refs) and all(ref.startswith("decision:") for ref in refs) and row.get("action") != "undo"
            and not str(row.get("undo_token") or "").startswith("w-"))


def log_view(rows) -> list:
    """The latest changes (memory/log.jsonl, newest first) as the page reads them (MemoryLogItem), without the answers' own notes
    (``answered``: 记录 is what changed what Covey does, not every question answered): an ``undo_token`` only on a change that
    can still be taken back (not one undone already, not an undo itself)."""
    undone = {str(row.get("undid")) for row in rows if row.get("action") == "undo" and row.get("undid")}
    out = []
    for row in rows:
        if answered(row):
            continue
        item = {"at": float(memstore._number(row.get("at"))), "who": str(row.get("who") or "bridge"),
                "action": str(row.get("action") or ""), "refs": [str(ref) for ref in row.get("refs") or ()],
                "title": memstore.clean(row.get("title"), memstore.TITLE_CHARS)}
        because = row.get("because") if isinstance(row.get("because"), dict) else {}
        why = {}
        if type(because.get("topic_number")) is int:
            why["topic_number"] = because["topic_number"]
        if str(because.get("quote") or "").strip():
            why["quote"] = memstore.clean(because["quote"], QUOTE_SHOWN)
        if why:
            item["because"] = why
        token = str(row.get("undo_token") or "")
        review = bool(item["refs"]) and all(ref.startswith("review:") for ref in item["refs"])
        if token and row.get("action") != "undo" and token not in undone and (not review or row.get("action") in ("accept", "reject")):
            item["undo_token"] = token  # An item of 待审核 the Bridge closed (expired, its line gone) is not undone from here.
        out.append(item)
    return out


def _size(data) -> int:
    return len(json.dumps(data, ensure_ascii=False).encode())


def fit(data, cuts) -> dict:
    """``data`` within KEY_BYTES: the lists named in ``cuts`` (in order) lose their last rows until it fits; what is not named is
    never cut (memory:global's review)."""
    for name in cuts:
        rows = data.get(name)
        while isinstance(rows, list) and rows and _size(data) > KEY_BYTES:
            del rows[max(len(rows) - max(len(rows) // 8, 1), 0):]
    return data


def _shelved_last(views) -> list:
    """Entries with the live ones first, so a key past its size loses the archived and the void ones."""
    return sorted(views, key=lambda view: view["status"] not in LIVE)


# ---------------------------------------------------------------------------------------------- the keys


class Center:
    """The memory center's keys, built by app/web_sync.py push_memory; keeps the T numbers of the topics decisions were asked in
    between builds (they never change)."""

    def __init__(self, state_dir, home=None) -> None:
        self.state = Path(state_dir)
        self.numbers = {}

    def build(self, now=None, *, quick=False) -> dict:
        """key -> data of every memory:* key, memory:index last (its ``revs`` name the others' revs). ``quick`` (the one before
        the page's ack of its own change, app/web_sync.py push_memory_center ``force``) builds the same: there is no preview
        left to keep from the last build."""
        now = time.time() if now is None else now
        return keys(self.state, Store(self.state, now, self.numbers), now=now)


def off_index() -> dict:
    """memory:index with the memory off: the page shows that it is, the other keys are taken back."""
    return {"on": False, "version": V45, "counts": {"rule": 0, "decision": 0, "note": 0, "pending": 0}, "scopes": [], "revs": {},
            "limits": {"title": memstore.TITLE_CHARS, "text": memstore.TEXT_CHARS, "decision_text": memstore.DECISION_CHARS},
            "titles": []}


def keys(state_dir, store, *, now=None) -> dict:
    """Every memory:* key's data of ``store`` (``Store``): memory:global, memory:topics, each memory:p:<id>, memory:index last.
    Since v45 no memory:previews, no memory:import:<page> (app/web_sync.py takes back the ones an older release pushed)."""
    views = {entry.ref: store.view(entry) for entry in store.all()}
    pending = store.pending()
    out = {}
    log = [row for row in memstore.log_read(state_dir, limit=LOG_READ) if not answered(row)][:LOG_SHOWN]
    out[GLOBAL_KEY] = fit({"key": "global", "entries": _shelved_last([views[e.ref] for e in store.globals]),
                           "review": [review_view(item, views) for item in store.review],
                           "log": log_view(log)}, ("log", "entries"))
    topics = []
    for tid, found in store.topics.items():
        facts = store.topic(tid)
        row = {"topic_key": tid, "title": facts["title"] or (f"T{facts['number']}" if facts["number"] else tid[:8]),
               "entries": _shelved_last([views[e.ref] for e in found])}
        if facts["number"] is not None:
            row["topic_number"] = facts["number"]
        if facts["project"]:
            row["project_key"] = facts["project"]
        topics.append(row)
    out[TOPICS_KEY] = fit({"topics": topics}, ("topics",))
    for pid, (title, found) in store.projects.items():
        out[PROJECT_PREFIX + pid] = fit({"key": f"p:{pid}", "title": title,
                                         "entries": _shelved_last([views[e.ref] for e in found])}, ("entries",))
    revs = {key: web_sync.rev(data) for key, data in out.items()}

    migrating = store.pending("migrate")

    def scope(key, title, found, waiting):
        return {"key": key, "title": title, "counts": _counts(found, waiting, migrating.get(key, 0)),
                "rev": revs.get(GLOBAL_KEY if key == "global" else TOPICS_KEY if key == "topics" else "memory:" + key, "")}

    scopes = [scope("global", "全局", store.globals, pending.get("global", 0)),
              scope("topics", "话题", [e for found in store.topics.values() for e in found], pending.get("topics", 0)),
              *(scope(f"p:{pid}", title, found, pending.get(f"p:{pid}", 0)) for pid, (title, found) in store.projects.items())]
    titled = sorted((e for e in store.all() if e.code and e.status != "void"), key=lambda e: (e.status not in LIVE, -e.at))
    index = {"on": True, "version": V45, "counts": _counts(store.all(), len(store.review), sum(migrating.values())), "scopes": scopes,
             "revs": dict(revs),
             "limits": {"title": memstore.TITLE_CHARS, "text": memstore.TEXT_CHARS, "decision_text": memstore.DECISION_CHARS},
             "titles": [[e.code, e.title[:memstore.TITLE_CHARS], e.ref] for e in titled[:TITLES]]}
    index["revs"][INDEX_KEY] = web_sync.rev(index)
    out[INDEX_KEY] = index
    return out


def center_url(code="", scope="") -> str:
    """The web page's 记忆 (``page_url`` + /memory, at the entry ``code`` or the group ``scope``), '' without the page: the
    cards' rule (cards/memory_panel.py center_url, which a card reads without importing app/)."""
    return memory_card_url(code, scope)


def overview(state_dir, now=None) -> dict:
    """What the 设置 card's 记忆 tab and the evening digest show (app/memory_panel.py, app/digest.py): {counts (the active
    rules, decisions and notes, ``pending`` the open items of 待审核), rules_by (the active rules of 全局, of the projects and
    of the topics: {global, project, topic}), scopes [{key, title, counts}] (全局, 话题, each project with memory), review: the
    open items, the oldest first [{key, type, title, text, suggest, items?}] (a 推断全局's held line in ``items``, a 放行类's
    ``suggest.grant``), rules: the active rules [{key, code, title, scope,
    over_cap, project_key, topic_number?}] of 全局, the projects and the topics}."""
    store = Store(state_dir, now)
    pending = store.pending()
    scopes = [{"key": "global", "title": "全局", "counts": _counts(store.globals, pending.get("global", 0))},
              {"key": "topics", "title": "话题", "counts": _counts([e for f in store.topics.values() for e in f],
                                                                     pending.get("topics", 0))},
              *({"key": f"p:{pid}", "title": title, "counts": _counts(found, pending.get(f"p:{pid}", 0))}
                for pid, (title, found) in store.projects.items())]
    rules = []
    for e in store.all():
        if e.kind != "rule" or e.status != "active":
            continue
        row = {"key": e.ref, "code": e.code, "title": e.title, "scope": e.scope, "over_cap": e.ref in store.over,
               "project_key": e.owner_key if e.scope == "project" else ""}
        if e.scope == "topic" and store.topic(e.owner_key)["number"] is not None:
            row["topic_number"] = store.topic(e.owner_key)["number"]
        rules.append(row)
    by = {scope: sum(1 for row in rules if row["scope"] == scope) for scope in memstore.SCOPES}
    held = {}  # The lines a 推断全局 holds where it was said: the tab says so (cards/memory_panel.py review_words).
    for ref in {ref for item in store.review for ref in item.get("items") or () if isinstance(ref, str)}:
        entry = memstore.find(state_dir, ref)
        if entry is not None:
            held[ref] = entry_view(entry)
    return {"counts": _counts(store.all(), len(store.review)), "rules_by": by, "scopes": scopes,
            "review": [review_view(item, held) for item in store.review], "rules": rules}


# ---------------------------------------------------------------------------------------------- 待审核 decided


REVIEW_ACTIONS = ("accept", "reject", "keep_left", "keep_right", "keep_both")
ALL_ACTIONS = ("accept_all", "reject_all")


def _review_item(state_dir, key) -> dict:
    item = next((row for row in memstore.review_items(state_dir) if row.get("key") == key), None)
    if item is None:
        raise ValueError("这条待审核已经处理过了（或不在了），页面会按最新的重画")
    return item


def reopen(state_dir, token, *, who="owner", now=None) -> list:
    """The items of 待审核 decided with ``token`` (or closed by the change it names: their line gone, core/memstore.py
    review_close) open again, as ``undo`` takes their change back: the keys."""
    if not token:
        return []
    done = []
    for item in memstore.review_items(state_dir, states=("accepted", "rejected", "expired")):
        if item.get("token") == token:
            memstore.review_set(state_dir, str(item["key"]), "open", by=who, now=now, token="", refs=[], action="")
            done.append(str(item["key"]))
    return done


def review_decide(state_dir, keys, action, *, edits=None, who="owner", token="", now=None) -> dict:
    """待审核 decided by the owner: ``action`` one of REVIEW_ACTIONS on the item among ``keys`` (keep_*: the review key of the
    pair [the entry, the item]), or ALL_ACTIONS on ``keys`` (empty: every open item). ``edits`` {title, text, kind, scope,
    project_key, topic_key}: what the owner changed before accepting, laid on the item first. Through core/memory_review.py
    ``decide``/``decide_all``. {decided: [keys], refs: [written]}."""
    stamp = time.time() if now is None else now
    edits = {k: v for k, v in dict(edits or {}).items() if v not in (None, "")}
    if action in ALL_ACTIONS:
        wanted = [str(k) for k in keys or () if str(k).startswith("review:")]
        open_keys = [str(item["key"]) for item in memstore.review_items(state_dir)]
        chosen = [k for k in open_keys if not wanted or k in wanted]
        if not chosen:
            raise ValueError("没有要处理的待审核")
        found = memory_review.decide_all(state_dir, action, keys=chosen, who=who, token=token, now=stamp)
        decided = list(found.get("decided", chosen))
        if not decided:
            raise ValueError("这些待审核都已经处理过了")
        return {"decided": decided, "refs": [str(r) for r in found.get("refs") or ()]}
    if action not in REVIEW_ACTIONS:
        raise ValueError("不认识的记忆操作")
    key = next((str(k) for k in keys or () if str(k).startswith("review:")), "")
    if not key:
        raise ValueError("要选一条待审核")
    return {"decided": [key], "refs": _decide_one(state_dir, key, action, edits=edits, who=who, token=token, now=stamp)}


def _decide_one(state_dir, key, action, *, edits, who, token, now) -> list:
    item = _review_item(state_dir, key)
    if edits:  # The owner's words and place go on the item first: decide takes the item as it is.
        suggest = dict(item.get("suggest") or {})
        for name in ("kind", "scope", "project_key"):
            if edits.get(name):
                suggest[name] = edits[name]
        if edits.get("scope") == "topic" and edits.get("topic_key"):
            suggest["owner_key"] = edits["topic_key"]
        memstore.review_set(state_dir, key, "open", by=who, now=now, suggest=suggest,
                            **{name: edits[name] for name in ("title", "text") if edits.get(name)})
    return [str(r) for r in memory_review.decide(state_dir, key, action, who=who, token=token, now=now)["refs"]]


# ---------------------------------------------------------------------------------------------- memory_edit v43


ACTIONS_V43 = ("add", "edit", "move", "archive", "restore", "void", "accept", "reject", "accept_all", "reject_all",
               "keep_left", "keep_right", "keep_both", "promote", "undo")  # web/shared/api.interface.ts (v45: no import, ignore)
DONE_WORDS = {"archive": "已归档", "restore": "已放回", "void": "已作废", "accept": "已采纳", "reject": "不要了", "keep_left": "留了原来的",
              "keep_right": "换成了新的", "keep_both": "两条都留了", "accept_all": "全部采纳", "reject_all": "全部不要"}


def token_of(op_id) -> str:
    """The change's undo token of the page's operation ``op_id`` (core/memstore.py log_change: ≤64)."""
    return ("w-" + "".join(ch for ch in str(op_id or "") if ch.isalnum() or ch in "-_"))[:64] if op_id else ""


def _owner_of(payload, scope) -> str:
    if scope == "global":
        return ""
    found = str(payload.get("project_key") if scope == "project" else payload.get("topic_key") or "").strip()
    if not found:
        raise ValueError("要选记在哪个专项" if scope == "project" else "要选记在哪个话题")
    return found


def _keys(payload) -> list:
    keys = payload.get("keys")
    return [str(key) for key in keys if isinstance(key, str) and key.strip()] if isinstance(keys, list) else []


def _one(state_dir, payload):
    keys = _keys(payload)
    if not keys:
        raise ValueError("要选一条记忆")
    entry = memstore.find(state_dir, keys[0])
    if entry is None:
        raise ValueError("这条记忆已经不在了（可能刚在别处改掉），页面会按最新的重画")
    return entry


def _short_entry(entry) -> str:
    return short(entry.title or entry.text)


def edit_v43(state_dir, payload, *, token="", now=None) -> dict:
    """One ``memory_edit`` of data contract v43 (``payload["action"]`` one of ACTIONS_V43), as the owner: {message (the
    receipt the page shows), undo_token (``token``, what ``undo`` takes back; '' for an undo, an ignore or nothing written),
    refs (the entries written)}. ValueError says why nothing changed."""
    action = str(payload.get("action") or "")
    if action not in ACTIONS_V43:
        raise ValueError("不认识的记忆操作")
    stamp = time.time() if now is None else now
    who, base = "owner", str(payload.get("base") or "")
    text = payload.get("text") if isinstance(payload.get("text"), str) else None
    title = payload.get("title") if isinstance(payload.get("title"), str) else None
    why = payload.get("why") if isinstance(payload.get("why"), str) else None
    refs, message = [], ""
    if action == "add":
        kind, scope = str(payload.get("kind") or ""), str(payload.get("scope") or "")
        owner = _owner_of(payload, scope) if scope in memstore.SCOPES else ""
        new = True
        if kind == "decision":
            if scope == "global":
                raise ValueError("决定记在专项或话题里")
            entry = memstore.add_decision(state_dir, scope=scope, owner_key=owner, text=text or "", title=title or "",
                                          said="owner", by="web", who=who, token=token, now=stamp)
        else:
            if kind in memstore.KINDS and scope in memstore.SCOPES:  # The store returns the live one that says the same.
                same = memstore.same_key(text or "")
                new = not any(memstore.same_key(e.text) == same for e in memstore.entries(
                    state_dir, scope=scope, owner_key=owner, kinds=(kind,), statuses=LIVE))
            entry = memstore.add(state_dir, kind=kind, scope=scope, owner_key=owner, text=text or "", title=title or "",
                                 why=why or "", said="owner", by="web", repos=payload.get("repos") or (), who=who, token=token,
                                 now=stamp)
        refs, message = [entry.ref], ("已记下：" if new else "已有同样的一条，没再记：") + _short_entry(entry)
        if not new:
            token = ""
    elif action == "edit":
        entry = _one(state_dir, payload)
        kind = payload.get("kind") if payload.get("kind") in memstore.KINDS and payload.get("kind") != entry.kind else None
        after = memstore.change(state_dir, entry.ref, action="edit", base=base, who=who, token=token, now=stamp, title=title,
                                text=text, why=why, kind=kind, repos=payload.get("repos") if isinstance(payload.get("repos"), list) else None)
        if memstore.snapshot(after) == memstore.snapshot(entry):
            return {"message": "没有变化", "undo_token": "", "refs": []}
        refs, message = [after.ref], "已保存：" + _short_entry(after)
    elif action in ("move", "promote"):
        entry = _one(state_dir, payload)
        scope = str(payload.get("scope") or "")
        if scope not in memstore.SCOPES:
            raise ValueError("要选挪到哪个范围")
        owner = _owner_of(payload, scope)
        if action == "move":
            new = memstore.move(state_dir, entry.ref, scope=scope, owner_key=owner, base=base, who=who, token=token, now=stamp)
        else:
            if base and base != memstore.rev_of(entry):
                raise ValueError("这条刚在别处改过，先看看现在的内容再改")
            repos = payload.get("repos") if isinstance(payload.get("repos"), list) else None
            new = memstore.promote(state_dir, entry.ref, scope=scope, owner_key=owner, repos=repos, who=who, token=token, now=stamp)
        if new.ref == entry.ref:
            return {"message": "已经在这个范围了", "undo_token": "", "refs": []}
        refs, message = [entry.ref, new.ref], ("已换范围：" if action == "move" else "已提到更大的范围：") + _short_entry(new)
    elif action in ("archive", "restore", "void"):
        entry = _one(state_dir, payload)
        if action == "restore" and entry.status in LIVE:
            return {"message": "这条本来就在用", "undo_token": "", "refs": []}
        if action == "archive" and entry.status in ("archived", "done"):
            return {"message": "这条已经归档了", "undo_token": "", "refs": []}
        if action == "void" and entry.status == "void":
            return {"message": "这条已经作废了", "undo_token": "", "refs": []}
        because = {"quote": memstore.clean(why, memstore.TEXT_CHARS)} if why and why.strip() else None
        after = getattr(memstore, action)(state_dir, entry.ref, base=base, who=who, because=because, token=token, now=stamp)
        refs, message = [after.ref], f"{DONE_WORDS[action]}：" + _short_entry(after)
    elif action in REVIEW_ACTIONS or action in ALL_ACTIONS:
        edits = {name: payload.get(name) for name in ("title", "text", "kind", "scope", "project_key", "topic_key")
                 if isinstance(payload.get(name), str) and payload.get(name).strip()}
        found = review_decide(state_dir, _keys(payload), action, edits=edits, who=who, token=token, now=stamp)
        refs = found["refs"]
        count = len(found["decided"])
        message = f"{DONE_WORDS[action]} {count} 条" if action in ALL_ACTIONS else DONE_WORDS[action]
    else:  # undo
        wanted = memstore.clean(payload.get("undo_token"), 64)
        if not wanted:
            raise ValueError("没有要撤销的")
        decided = [item for item in memstore.review_items(state_dir, states=("accepted", "rejected")) if item.get("token") == wanted]
        try:
            back = memstore.undo(state_dir, wanted, who=who, now=stamp)
        except ValueError as exc:
            if not decided or "找不到这一步" not in str(exc):
                raise  # A change made since, or undone already: nothing is put back, the item stays decided too.
            back = []  # 不要 on 待审核 wrote nothing: its item alone goes back.
        opened = reopen(state_dir, wanted, who=who, now=stamp)
        words = "、".join(part for part in (f"{len(back)} 条放回原样" if back else "", f"{len(opened)} 条回到待审核" if opened else "") if part)
        return {"message": f"已撤销（{words}）" if words else "已撤销", "undo_token": "", "refs": back}
    return {"message": message, "undo_token": token if refs else "", "refs": refs}
