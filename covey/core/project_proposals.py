"""整理专项's records and switches (docs/butler.md 整理专项, COVEY_PROJECT_TIDY): what was proposed, what the owner decided and what
整理专项 must not do again. app/project_tidy.py writes them, app/project_sorter.py reads them; no model, no Feishu call.

- ``COVEY_PROJECT_TIDY``: ``suggest`` (整理 runs, joins into an existing project are done, a new project is only proposed) or
  ``auto`` (a new project is made too, with 撤回 left); anything else, unset too, is off, and so is every value without
  COVEY_PROJECTS=1. ./install writes ``suggest`` (and COVEY_PROJECTS=1) into a new service's manifest.
- <state>/projects/tidy/state.json (``load``/``change``): the cursor of ended turns looked at (``cursor``, ``seen``), the last
  scheduled time done (``last_slot``), the owner's preferences (``prefs``: ``no_proposals`` 以后别提议, ``auto_create`` 以后直接建,
  ``accepted_in_row`` 建专项 clicked as proposed, ``asked_auto`` once asked), the run going on (``run``) and the owner's reasons
  for a 不用 or a 移出 (``lessons``, the newest LESSONS_KEPT, read by the judge as rules).
- <state>/projects/tidy/proposals/<id>.json: one proposal (``new_proposal``): its ``kind`` (KINDS), the topics and project it is
  about, the name and goal, the judge's one line, its ``state`` (PROPOSAL_TRANSITIONS) and the card it went out on.
- <state>/projects/tidy/topics/<topic_id>.json (``topic_flags``): ``never_auto`` (移出: never joined by itself again), what it
  joined, the moments that already started a real-time 整理 (``fired``) and when it was last judged.

A proposal the owner turned down (``declined``) is not made again until there is new evidence (``declined_like``: EVIDENCE more
topics than the declined one had). A name the owner chose or changed (``name_by`` "owner" in the project's record) is never
renamed by 整理专项. Standard library and core only."""
from __future__ import annotations

import copy
import hashlib
import re
import time
from pathlib import Path

from covey.core import env as cenv
from covey.core import projects
from covey.core.locks import resource_lock
from covey.core.state import read_object, write_json

ENV = "COVEY_PROJECT_TIDY"
OFF, SUGGEST, AUTO = "off", "suggest", "auto"
FOLDER = "tidy"
SCHEMA = 1

CREATE, JOIN, LEAVE, MERGE, SPLIT, RENAME, FOLD = "create", "join", "leave", "merge", "split", "rename", "fold"
KINDS = (CREATE, JOIN, LEAVE, MERGE, SPLIT, RENAME, FOLD)
DIRECT = (JOIN, FOLD)  # Done at once by a tidy, the owner only told (a join only when the judge is sure: JOIN_SURE).
ASKED = (CREATE, MERGE, SPLIT, RENAME, LEAVE)  # Done only once the owner says 好.
OPEN, ACCEPTED, DECLINED, NEVER, DONE, FAILED, EXPIRED = "open", "accepted", "declined", "never", "done", "failed", "expired"
PROPOSAL_TRANSITIONS = {OPEN: {ACCEPTED, DECLINED, NEVER, EXPIRED, DONE, FAILED}, ACCEPTED: {DONE, FAILED},
                        DECLINED: set(), NEVER: set(), DONE: set(), FAILED: {ACCEPTED}, EXPIRED: set()}

EVIDENCE = 2  # A declined proposal is made again once it would take in this many more topics.
MIN_CREATE = 2  # Topics a new project is made of at least (app/project_sorter.py, the 看板 and the proposal cards).
ACCEPTS_TO_ASK = 5  # 建专项 clicked as proposed this many times in a row: the card asks 以后直接建？
LESSONS_KEPT = 30
SEEN_KEPT = 400
NAME_CHARS = 12  # 对象 + 要达成的事, at most this many characters (a CJK one or an ASCII word's letter alike).
JOIN_SURE = 0.85
STARTED_WORDS = "已开始整理，结果发到私聊"  # What whoever asked for a 整理 hears (app/project_tidy.py start).
BUSY_PREFIX = "正在整理"  # ... and while one runs: 正在整理（12/40）.
DATED = re.compile(r"(?:T\d{2,}|\d{4}[-/年.]\d{1,2}(?:[-/月.]\d{1,2}[日号]?)?|\d{1,2}[-/月.]\d{1,2}[日号]?|#\d+|第\s*\d+\s*[期轮次])")

DEFAULTS = {"days": 7, "age_days": 3, "words": 15, "min_topics": 5, "judge_seconds": 90, "read_seconds": 60}


def raw(name, environ=None) -> str:
    """The text of the variable behind ``name`` (DEFAULTS)."""
    return {
        "days": cenv.get("COVEY_PROJECT_TIDY_DAYS", "", environ),
        "age_days": cenv.get("COVEY_PROJECT_TIDY_AGE_DAYS", "", environ),
        "words": cenv.get("COVEY_PROJECT_TIDY_WORDS", "", environ),
        "min_topics": cenv.get("COVEY_PROJECT_TIDY_MIN_TOPICS", "", environ),
        "judge_seconds": cenv.get("COVEY_PROJECT_TIDY_JUDGE_SECONDS", "", environ),
        "read_seconds": cenv.get("COVEY_PROJECT_TIDY_READ_SECONDS", "", environ),
    }[name]


MODEL_ENV = "COVEY_PROJECT_TIDY_MODEL"
DEFAULT_MODEL = "sonnet"
MODEL_RE = re.compile(r"[A-Za-z0-9._:\[\]-]{1,80}")


def model(environ=None) -> str:
    """The Claude Code model that writes a 话题要点 from a whole topic and judges a 整理: COVEY_PROJECT_TIDY_MODEL, else
    DEFAULT_MODEL (more than the small one: both read whole conversations and name what a matter really is)."""
    value = str(cenv.get(MODEL_ENV, "", environ) or "").strip()
    return value if MODEL_RE.fullmatch(value) else DEFAULT_MODEL


def mode(environ=None) -> str:
    """``suggest``, ``auto`` or ``off`` (read on every call; off without COVEY_PROJECTS=1)."""
    if str(cenv.get(projects.ENV, "", environ)).strip() != "1":
        return OFF
    value = str(cenv.get(ENV, "", environ)).strip().lower()
    return value if value in (SUGGEST, AUTO) else OFF


def enabled(environ=None) -> bool:
    return mode(environ) != OFF


def number(name, environ=None):
    """One of DEFAULTS from its variable (``raw``), the default when unset or not a positive number."""
    try:
        value = float(str(raw(name, environ) or "").strip())
    except ValueError:
        return DEFAULTS[name]
    if not 0 < value < 1e7:
        return DEFAULTS[name]
    return int(value) if value == int(value) else value


def folder(state_dir) -> Path:
    return projects.folder(state_dir) / FOLDER


# state.json ----------------------------------------------------------------------------------------------------------

def state_path(state_dir) -> Path:
    return folder(state_dir) / "state.json"


def load(state_dir) -> dict:
    data = read_object(state_path(state_dir))
    data.setdefault("prefs", {})
    data.setdefault("lessons", [])
    return data


def change(state_dir, edit) -> dict:
    """Read state.json, let ``edit(data)`` change it in place (no I/O in it: the lock is held), write it, return it."""
    path = state_path(state_dir)
    with resource_lock("record", str(path)):
        data = load(state_dir)
        before = copy.deepcopy(data)
        edit(data)
        if data != before:
            data["schema_version"] = SCHEMA
            path.parent.mkdir(parents=True, exist_ok=True)
            write_json(path, data, ensure_ascii=False, indent=1)
        return data


def prefs(state_dir) -> dict:
    return dict(load(state_dir).get("prefs") or {})


def set_pref(state_dir, **fields) -> dict:
    def edit(data):
        data["prefs"] = {**(data.get("prefs") or {}), **fields}
    return change(state_dir, edit)["prefs"]


def add_lesson(state_dir, text, *, kind="", now=None) -> None:
    """The owner's reason for a 不用 or a 移出, one sentence: the judge reads the newest as rules."""
    text = " ".join(str(text or "").split())[:200]
    if not text:
        return

    def edit(data):
        data["lessons"] = [*(data.get("lessons") or []), {"text": text, "kind": kind, "at": time.time() if now is None else now}
                           ][-LESSONS_KEPT:]
    change(state_dir, edit)


def lessons(state_dir) -> list:
    return [str(item.get("text")) for item in load(state_dir).get("lessons") or () if isinstance(item, dict) and item.get("text")]


# Proposals -----------------------------------------------------------------------------------------------------------

def proposals_dir(state_dir) -> Path:
    return folder(state_dir) / "proposals"


def proposal_id(kind, topics=(), project="", extra="") -> str:
    """The same proposal (kind, topics, project) has the same id, so a repeated look finds it."""
    basis = "\0".join([kind, ",".join(sorted(topics)), str(project or ""), str(extra or "")])
    return hashlib.sha256(basis.encode()).hexdigest()[:16]


def proposal_path(state_dir, pid) -> Path:
    if not re.fullmatch(r"[0-9a-f]{16}", str(pid or "")):
        raise ValueError(f"not a proposal id: {pid!r}")
    return proposals_dir(state_dir) / f"{pid}.json"


def new_proposal(kind, *, topics=(), project="", projects_=(), name="", goal="", why="", source="tidy", run="",
                 confidence=0.0, groups=(), now=None) -> dict:
    """A fresh open proposal. ``projects_``: the projects a merge joins (the first keeps its name); ``groups``: a split's
    topics for the new project (``topics`` then are the ones that go)."""
    if kind not in KINDS:
        raise ValueError(f"not a proposal kind: {kind!r}")
    topics = [topic for topic in dict.fromkeys(topics) if isinstance(topic, str) and projects.TOPIC_RE.fullmatch(topic)]
    stamp = time.time() if now is None else now
    extra = ",".join(projects_) if kind == MERGE else name if kind == RENAME else ""
    return {"schema_version": SCHEMA, "id": proposal_id(kind, topics, project, extra), "kind": kind, "topics": topics,
            "project": str(project or ""), "projects": [str(item) for item in projects_], "name": clean_name(name),
            "goal": " ".join(str(goal or "").split())[:200], "why": " ".join(str(why or "").split())[:120],
            "confidence": float(confidence or 0), "source": source, "run": str(run or ""), "state": OPEN,
            "created_at": stamp, "decided_at": 0, "card": {}, "groups": [list(group) for group in groups]}


def read_proposal(state_dir, pid) -> dict:
    try:
        return read_object(proposal_path(state_dir, pid))
    except ValueError:
        return {}


def put_proposal(state_dir, record) -> tuple:
    """(the record on disk, whether this call wrote it): an open or decided proposal with the same id stays as it is, an
    expired or failed one is replaced by the new one."""
    path = proposal_path(state_dir, record["id"])
    with resource_lock("record", str(path)):
        saved = read_object(path)
        if saved.get("id") == record["id"] and saved.get("state") not in (EXPIRED, FAILED):
            return saved, False
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json(path, record, ensure_ascii=False, indent=1)
        return record, True


def update_proposal(state_dir, pid, edit) -> dict:
    """``edit(record)`` in place under its lock; a change of ``state`` must be one PROPOSAL_TRANSITIONS allows (ValueError,
    nothing written). KeyError when there is no such proposal."""
    path = proposal_path(state_dir, pid)
    with resource_lock("record", str(path)):
        record = read_object(path)
        if record.get("id") != pid:
            raise KeyError(pid)
        before = copy.deepcopy(record)
        edit(record)
        if record.get("state") != before.get("state") and record.get("state") not in PROPOSAL_TRANSITIONS.get(before.get("state"), ()):
            raise ValueError(f"proposal state {before.get('state')} -> {record.get('state')} is not allowed")
        if record != before:
            write_json(path, record, ensure_ascii=False, indent=1)
        return record


def proposals(state_dir, states=None) -> list:
    """Every proposal (of ``states``, when given), the oldest first."""
    try:
        paths = sorted(proposals_dir(state_dir).glob("*.json"))
    except OSError:
        return []
    found = [read_object(path) for path in paths if not path.is_symlink()]
    found = [record for record in found if record.get("id") and (states is None or record.get("state") in states)]
    return sorted(found, key=lambda record: (record.get("created_at") or 0, record["id"]))


def declined_like(state_dir, kind, topics, project="", projects_=()) -> bool:
    """Whether the owner turned down (不用, 以后别提议) a proposal like this one with no new evidence since: one of the same kind,
    project (and, for a merge, projects) whose topics overlap these and lack fewer than EVIDENCE of them. A proposal about a
    project (改名, 收起, 合并) carries the project's topics when it was made: its topics growing by EVIDENCE is the new evidence."""
    wanted = set(topics)
    for record in proposals(state_dir, (DECLINED, NEVER)):
        if record.get("kind") != kind or str(record.get("project") or "") != str(project or ""):
            continue
        if kind == MERGE and set(record.get("projects") or ()) != set(projects_):
            continue
        old = set(record.get("topics") or ())
        if not wanted and not old:
            return True
        if (old & wanted or not wanted) and len(wanted - old) < EVIDENCE:
            return True
    return False


QUEUE_MAX = 20  # The real-time 整理 waiting in state.json's ``queue`` (app/project_tidy.py drain).


def queue_sweep(state_dir, project, now=None) -> None:
    """A look over the recent topics for ones that belong in ``project`` (just made), for app/project_tidy.py to take next."""
    item = {"project": project, "reason": "project", "at": time.time() if now is None else now}
    change(state_dir, lambda data: data.update(queue=[*(data.get("queue") or []), item][-QUEUE_MAX:]))


def open_with(state_dir, topic) -> list:
    """The open proposals that are about ``topic``."""
    return [record for record in proposals(state_dir, (OPEN,)) if topic in (record.get("topics") or ())]


def free(state_dir, topics) -> list:
    """``topics`` without the ones already in a project (core/projects.py topic_project), each once, in order."""
    return [topic for topic in dict.fromkeys(topics or ()) if projects.topic_project(state_dir, topic) is None]


def waiting(state_dir) -> list:
    """The proposals waiting for the owner, the newest first: the open 新建 ones on a proposal card of their own with at least
    MIN_CREATE topics, the same topics (or some of them) once. The 看板 (app/digest.py), the butler's projects_list
    (app/projects.py) and 整理专项's 还在等你定 say these, so all three say the same."""
    found, seen = [], []
    for record in sorted(proposals(state_dir, (OPEN,)), key=lambda record: record.get("created_at") or 0, reverse=True):
        mine = frozenset(record.get("topics") or ())
        if record.get("kind") != CREATE or len(mine) < MIN_CREATE or not (record.get("card") or {}).get("message_id"):
            continue  # A row of an older 整理专项 card (before 2026-11-18 20:00) stays on that card.
        if any(mine <= other or other <= mine for other in seen):
            continue
        seen.append(mine)
        found.append(record)
    return found


def overlapping(state_dir, topics) -> dict:
    """The proposal waiting for the owner (``waiting``) that is mostly about ``topics``: they share at least half of the
    smaller of the two; {} when none. A new 新建 proposal goes into it instead of a card of its own (the owner, 2026-11-19:
    C队重启后自启 proposed twice)."""
    wanted = set(topics or ())
    for record in waiting(state_dir):
        shared = wanted & set(record.get("topics") or ())
        if shared and 2 * len(shared) >= min(len(wanted), len(set(record.get("topics") or ()))):
            return record
    return {}


# Topics --------------------------------------------------------------------------------------------------------------

def topic_path(state_dir, topic) -> Path:
    if not isinstance(topic, str) or not projects.TOPIC_RE.fullmatch(topic):
        raise ValueError(f"not a topic id: {topic!r}")
    return folder(state_dir) / "topics" / f"{topic}.json"


def topic_flags(state_dir, topic) -> dict:
    try:
        return read_object(topic_path(state_dir, topic))
    except ValueError:
        return {}


def change_topic(state_dir, topic, edit) -> dict:
    path = topic_path(state_dir, topic)
    with resource_lock("record", str(path)):
        data = read_object(path)
        before = copy.deepcopy(data)
        edit(data)
        if data != before:
            data.update(schema_version=SCHEMA, topic_id=topic)
            path.parent.mkdir(parents=True, exist_ok=True)
            write_json(path, data, ensure_ascii=False, indent=1)
        return data


def never_auto(state_dir, topic) -> bool:
    """Whether ``topic`` was taken out of a project (移出): 整理专项 never joins it into one by itself again."""
    return bool(topic_flags(state_dir, topic).get("never_auto"))


# Names ---------------------------------------------------------------------------------------------------------------

def name_width(text) -> int:
    """Characters as the rule counts them: a CJK character 1, an ASCII word 1."""
    return len(re.findall(r"[A-Za-z0-9][A-Za-z0-9_.+-]*|[^\sA-Za-z0-9]", str(text or "")))


def clean_name(name) -> str:
    """A project name by the rule: one line, no numbers of topics, dates or rounds, at most NAME_CHARS (``name_width``)."""
    text = DATED.sub("", " ".join(str(name or "").split()))
    text = re.sub(r"\s{2,}", " ", text).strip(" ·-—:：,，")
    while name_width(text) > NAME_CHARS:
        text = text[:-1].rstrip()
    return text


def owner_named(record) -> bool:
    """Whether the owner chose or changed the project's name: 整理专项 never renames it."""
    return isinstance(record, dict) and record.get("name_by") == "owner"
