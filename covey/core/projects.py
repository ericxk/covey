"""专项 (docs/architecture.md 专项, phase 2, in development, off by default): a long job the main thread's butler hands to
one lead topic (the 负责人) that dispatches and follows several thread topics. This module is the foundation and nothing
else: the record <state>/projects/<id>.json, its state machine (``PROJECT_TRANSITIONS``), the lead topic's id and the
topic.json ``project`` field ({id, role}) that says a topic is a project's lead or one of its threads. No entry builds a
record yet (upgrade, the card and the lead's runs are later packages); with COVEY_PROJECTS unset every predicate here
is False and ``cells`` is empty, so nothing else in the product changes.

Roles: a ``lead`` topic belongs to the main thread's family (core/main_thread.py ``is_main_topic`` counts it, ``is_main_root``
does not; its Traits row is LEAD); a ``thread`` topic is an ordinary topic that core/placement.py gives its own role
(project_thread). The lead's topic id comes from the chat and the project (``lead_topic_id``), so it is never a
message's alias and no event routes to it by accident.

Fields written for phase 3 and read by nothing now: ``direction``, ``leader``, ``claims``, ``agreements``, ``place``.

Standard library and core only: the MCP server imports core/main_thread.py, which imports this."""
from __future__ import annotations

import copy
import hashlib
import re
import threading
import time
import uuid
from collections import namedtuple
from pathlib import Path

from covey.core import env as cenv
from covey.core.locks import resource_lock
from covey.core.state import read_object, write_json

ENV = "COVEY_PROJECTS"  # ``1``: on; anything else, unset too: off.
FOLDER = "projects"
SCHEMA = 1

ACTIVE, PAUSED, DONE, CANCELLED = "active", "paused", "done", "cancelled"
# Rule 4: the only way a project's state moves. done and cancelled are final.
PROJECT_TRANSITIONS = {ACTIVE: {PAUSED, DONE, CANCELLED}, PAUSED: {ACTIVE, DONE, CANCELLED}, DONE: set(), CANCELLED: set()}
STATES = frozenset(PROJECT_TRANSITIONS)

LEAD, THREAD = "lead", "thread"  # topic.json project.role.
ROLES = (LEAD, THREAD)

# The owner's defaults of 2026-11-16 for the guards of a later package (the lead's runs, the loop): not read here.
DEFAULT_TOKENS = 10_000_000  # One project's token budget.
DEFAULT_WAKES = 30  # Wake-ups in a row with no one stepping in.
MAX_THREADS = 4  # Threads of one project at the same time.
MAX_ACTIVE = 3  # Projects active at the same time.

NAME_CHARS = 60
GOAL_CHARS = 2000
ID_RE = re.compile(r"[0-9a-z]{6,32}")
TOPIC_RE = re.compile(r"[0-9a-f]{16,128}")


def enabled() -> bool:
    """Whether COVEY_PROJECTS=1 (only ``1``; read on every call). The predicates below are all False without it."""
    return str(cenv.get(ENV, "")).strip() == "1"


def valid_id(project_id) -> bool:
    return isinstance(project_id, str) and ID_RE.fullmatch(project_id) is not None


def new_id() -> str:
    """A fresh project id: 12 hex digits."""
    return uuid.uuid4().hex[:12]


def lead_topic_id(chat_id, project_id) -> str:
    """The topic id of project ``project_id``'s lead in ``chat_id``: hex like every topic id (app/topics.py Topics.target
    takes [0-9a-f]{16,128} only), and no message's alias. The digest's input names the kind, so it cannot meet the
    main thread's (core/main_tracks.py) or a track's. Stable: the same chat and project always give the same id."""
    return hashlib.sha256(f"project-lead\0{project_id}\0{chat_id or ''}".encode()).hexdigest()[:32]


# The record.

def folder(state_dir) -> Path:
    return Path(state_dir) / FOLDER


def record_path(state_dir, project_id) -> Path:
    if not valid_id(project_id):
        raise ValueError(f"project id {project_id!r} is not 6 to 32 of 0-9a-z")
    return folder(state_dir) / f"{project_id}.json"


def new_record(*, chat_id, name, goal="", project_id=None, origin=None, now=None) -> dict:
    """A fresh active record of a project in ``chat_id``. ``origin`` says how it began (a mapping, e.g. the dispatch card
    it grew from); ``steps`` is for the upgrade to tick off what it has done (idempotence), ``card`` for the project's
    card (sequence continues across writes, core/card_sequence.py)."""
    project_id = project_id or new_id()
    if not valid_id(project_id):
        raise ValueError(f"project id {project_id!r} is not 6 to 32 of 0-9a-z")
    stamp = time.time() if now is None else now
    return {
        "schema_version": SCHEMA, "id": project_id, "chat_id": str(chat_id or ""),
        "name": " ".join(str(name or "").split())[:NAME_CHARS], "goal": str(goal or "")[:GOAL_CHARS],
        "state": ACTIVE, "state_reason": "", "lead_topic_id": lead_topic_id(chat_id, project_id), "threads": [],
        "origin": dict(origin) if isinstance(origin, dict) else {}, "steps": {},
        "card": {"card_id": "", "message_id": "", "sequence": 0},
        "created_at": stamp, "updated_at": stamp,
        # For phase 3 (docs/architecture.md 专项); nothing reads them yet.
        "direction": None, "leader": {"kind": "temp", "profile": None}, "claims": [], "agreements": [],
        "place": {"chat_id": str(chat_id or "")},
    }


def transition(record, to) -> dict:
    """``record`` moved to state ``to`` when ``PROJECT_TRANSITIONS`` allows it; ValueError when it does not."""
    state = record.get("state") or ACTIVE
    if to not in PROJECT_TRANSITIONS.get(state, ()):
        raise ValueError(f"project state {state} -> {to} is not allowed")
    record["state"] = to
    return record


def _lock(path):
    # "record" is a shared (cross-process) namespace: the Bridge of an old and a new release may overlap while a deploy
    # runs. Held around reading and writing the one file only: no network call, no other lock (rule 1).
    return resource_lock("record", path)


def read_record(state_dir, project_id) -> dict:
    """The record of ``project_id``; {} when there is none or it cannot be read (or the id is not one)."""
    if not valid_id(project_id):
        return {}
    return read_object(record_path(state_dir, project_id))


def records(state_dir, chat_id=None) -> list:
    """Every record (of ``chat_id`` alone, when given), the oldest first. One that cannot be read is left out."""
    found = []
    try:
        paths = sorted(folder(state_dir).glob("*.json"))
    except OSError:
        return found
    for path in paths:
        if path.is_symlink() or not valid_id(path.stem):
            continue
        record = read_object(path)
        if record.get("id") == path.stem and (chat_id is None or record.get("chat_id") == chat_id):
            found.append(record)
    return sorted(found, key=lambda record: (record.get("created_at") or 0, record["id"]))


def create(state_dir, record) -> tuple:
    """(the record on disk, whether this call wrote it): written only when there is none under its id, so a repeated
    call (the upgrade runs its steps again after a restart) finds the first one."""
    path = record_path(state_dir, record["id"])
    with _lock(path):
        saved = read_object(path)
        if saved.get("id") == record["id"]:
            return saved, False
        write_json(path, record, ensure_ascii=False, indent=2)
        return record, True


def update(state_dir, project_id, change) -> dict:
    """Read the record, let ``change(record)`` edit it in place (no I/O in it: the lock is held), write it, return it.
    KeyError for a record that does not exist. A change of ``state`` must be one ``PROJECT_TRANSITIONS`` allows (ValueError,
    nothing written); ``updated_at`` is set on every write; nothing is written when ``change`` changed nothing."""
    path = record_path(state_dir, project_id)
    with _lock(path):
        record = read_object(path)
        if record.get("id") != project_id:
            raise KeyError(project_id)
        before = copy.deepcopy(record)
        state = record.get("state") or ACTIVE
        change(record)
        after = record.get("state") or ACTIVE
        if after != state:
            record["state"] = state
            transition(record, after)
        if record == before:
            return record
        record["updated_at"] = time.time()
        write_json(path, record, ensure_ascii=False, indent=2)
        return record


def set_state(state_dir, project_id, to, reason="") -> dict:
    """``update`` of the state alone: ``to`` with ``reason`` (kept in ``state_reason``)."""
    def change(record):
        record["state"], record["state_reason"] = to, str(reason or "")[:200]
    return update(state_dir, project_id, change)


def add_thread(state_dir, project_id, topic_id) -> dict:
    """``topic_id`` in the record's ``threads``, once (the list keeps the order they came in)."""
    if not isinstance(topic_id, str) or not TOPIC_RE.fullmatch(topic_id):
        raise ValueError(f"topic id {topic_id!r} is not a topic id")

    def change(record):
        threads = record.get("threads")
        if not isinstance(threads, list):
            threads = record["threads"] = []
        if topic_id not in threads:
            threads.append(topic_id)
    return update(state_dir, project_id, change)


def lead_record(state_dir, topic_id, chat_id=None) -> dict:
    """The record of the project whose lead topic is ``topic_id`` ({} for any other topic, and without COVEY_PROJECTS):
    what the board's cell of a project (``cells``) and a click on its card (app/project_cards.py) are looked up by."""
    if not is_lead_topic(state_dir, topic_id, chat_id):
        return {}
    found = topic_project(state_dir, topic_id)
    return read_record(state_dir, found.id) if found is not None else {}


def card_message(record) -> str:
    """The message id of the project's card ('' while it has none): the board pins it (app/pins.py aimed)."""
    card = record.get("card") if isinstance(record, dict) else None
    message = card.get("message_id") if isinstance(card, dict) else ""
    return message if isinstance(message, str) else ""


def update_card(state_dir, project_id, **fields) -> dict:
    """``update`` of the record's ``card`` alone: ``fields`` merged into it (``card_id``, ``message_id``, ``sequence``, and
    what app/project_cards.py keeps there), a field given as None removed."""
    def change(record):
        card = record.get("card") if isinstance(record.get("card"), dict) else {}
        card = {**{"card_id": "", "message_id": "", "sequence": 0}, **card}
        for key, value in fields.items():
            if value is None:
                card.pop(key, None)
            else:
                card[key] = value
        record["card"] = card
    return update(state_dir, project_id, change)


def cells(state_dir, chat_id) -> dict:
    """thread topic id -> its project's lead topic id, for ``chat_id``'s projects that are not cancelled (a cancelled
    one's threads are ordinary topics again): the board's cell of a thread is its lead's (core/board_read.py groups).
    {} without COVEY_PROJECTS, reading nothing."""
    if not enabled() or not state_dir:
        return {}
    found = {}
    for record in records(state_dir, chat_id):
        if record.get("state") == CANCELLED or not valid_id(record.get("id")):
            continue
        lead = lead_topic_id(chat_id, record["id"])
        for topic_id in record.get("threads") or ():
            if isinstance(topic_id, str) and topic_id and topic_id != lead:
                found.setdefault(topic_id, lead)
    return found


# topic.json's ``project`` field, read by a stat and kept until the file changes.

TopicProject = namedtuple("TopicProject", "id role chat_id")
_CACHE_MAX = 8192
_cache: dict = {}  # (state dir, topic id) -> (the file's stamp, TopicProject or None)
_cache_lock = threading.Lock()


def topic_file(state_dir, topic_id) -> Path:
    """<state>/topics/<topic_id>/topic.json (core/topics.py topic_file: a test keeps the two the same)."""
    return Path(state_dir) / "topics" / topic_id / "topic.json"


def project_field(project_id, role) -> dict:
    """The value of topic.json's ``project``."""
    if not valid_id(project_id) or role not in ROLES:
        raise ValueError(f"not a project field: {project_id!r} {role!r}")
    return {"id": project_id, "role": role}


def topic_project(state_dir, topic_id):
    """``TopicProject`` of the topic whose topic.json has a well formed ``project`` field, else None (no file, no field,
    a field that is not {id, role}). One stat a call: the file is read again only once its mtime, size or inode changed
    (every write replaces the file, so the inode moves). Not gated by COVEY_PROJECTS: the predicates are."""
    if not state_dir or not isinstance(topic_id, str) or not topic_id or "/" in topic_id:
        return None
    path = topic_file(state_dir, topic_id)
    key = (str(state_dir), topic_id)
    try:
        stat = path.stat()
    except OSError:
        with _cache_lock:
            _cache.pop(key, None)
        return None
    stamp = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
    with _cache_lock:
        kept = _cache.get(key)
    if kept is not None and kept[0] == stamp:
        return kept[1]
    data = read_object(path)
    field = data.get("project")
    found = None
    if isinstance(field, dict) and valid_id(field.get("id")) and field.get("role") in ROLES:
        found = TopicProject(field["id"], field["role"], str(data.get("chat_id") or ""))
    with _cache_lock:
        if len(_cache) >= _CACHE_MAX:
            _cache.clear()
        _cache[key] = (stamp, found)
    return found


def is_lead_topic(state_dir, topic_id, chat_id=None) -> bool:
    """Whether ``topic_id`` is a project's lead topic: COVEY_PROJECTS on, its topic.json says ``project.role`` lead, and
    its id is the lead's of that project in its chat (``lead_topic_id``; ``chat_id`` when given, else the file's), so
    a topic.json copied into another chat's topic is none. False when anything is missing."""
    if not enabled():
        return False
    found = topic_project(state_dir, topic_id)
    return (found is not None and found.role == LEAD
            and lead_topic_id(chat_id or found.chat_id, found.id) == topic_id)


def is_thread_topic(state_dir, topic_id) -> bool:
    """Whether ``topic_id`` is one of a project's threads: COVEY_PROJECTS on and its topic.json says ``project.role``
    thread."""
    if not enabled():
        return False
    found = topic_project(state_dir, topic_id)
    return found is not None and found.role == THREAD


def active_count(state_dir) -> int:
    """How many projects are on right now (``ACTIVE``; a stopped one, ``PAUSED``, uses no turns and does not count): the
    owner's limit is ``MAX_ACTIVE``. Read-only: the upgrade and a project opened directly ask it before they write
    (app/projects.py). 0 without COVEY_PROJECTS."""
    if not enabled() or not state_dir:
        return 0
    return sum(1 for record in records(state_dir) if (record.get("state") or ACTIVE) == ACTIVE)


# Continuing runs and their guards (phase 2's P2-6, app/project_loop.py): the switches, the owner's numbers, the id of a
# wake-up turn and the words of a stop. Pure, and nothing here reads a record.

AUTOWAKE_ENV = "COVEY_PROJECT_AUTOWAKE"  # ``1``: every receipt wakes the lead; else only a project's first one (start) does.
TOKENS_ENV = "COVEY_PROJECT_TOKENS"  # One project's token budget; DEFAULT_TOKENS when unset or not a positive integer.
WAKE_PREFIX = "lead-"  # A wake-up turn's message id is ``lead-<project id>-<n>`` (``wake_id``).
MAX_WAKES_HOUR = 12  # Wake-ups in the last hour, whoever stepped in or not.
STALL_SECONDS = 30 * 60  # Idle this long: one reminder; as long again with nothing moving: the project is paused.
# state_reason of a project a guard stopped, and what the owner's own turn may start again (a click on 停止专项 too).
GUARD_REASONS = ("tokens", "wakes", "stalled")
OWNER_STOP = "主人停止"
RESUMABLE = (*GUARD_REASONS, OWNER_STOP)
REASON_WORDS = {"tokens": "token 用完了", "wakes": "连续唤醒次数到上限", "stalled": "很久没有动静"}


def autowake_enabled() -> bool:
    """Whether every kind of receipt wakes the lead (COVEY_PROJECTS=1 and COVEY_PROJECT_AUTOWAKE=1)."""
    return enabled() and str(cenv.get(AUTOWAKE_ENV, "")).strip() == "1"


def token_limit() -> int:
    """One project's token budget: COVEY_PROJECT_TOKENS, else DEFAULT_TOKENS (also for a value that is not a positive integer)."""
    try:
        value = int(str(cenv.get(TOKENS_ENV, "")).strip())
    except ValueError:
        return DEFAULT_TOKENS
    return value if value > 0 else DEFAULT_TOKENS


def wake_id(project_id, number) -> str:
    """The message id of project ``project_id``'s ``number``-th wake-up turn."""
    return f"{WAKE_PREFIX}{project_id}-{int(number)}"


def is_wake(message_id) -> bool:
    """Whether a job's message id is a wake-up turn's (the Bridge woke the lead, the owner did not speak)."""
    return isinstance(message_id, str) and message_id.startswith(WAKE_PREFIX)


LEAD_ALIAS_PREFIX = "project-lead-"  # The alias the butler's project_send addresses a lead by (app/projects.py lead_target).


def own_message(message_id) -> bool:
    """Whether ``message_id`` is one the Bridge made up for a project's lead, a wake-up turn's (``wake_id``) or the alias
    the butler's ``project_send`` hands it a message under: Feishu has no such message, so no reply quotes it (core/topics.py
    context_for_topic) and no "你问" block shows its words (app/answer_cards.py asked)."""
    return isinstance(message_id, str) and message_id.startswith((WAKE_PREFIX, LEAD_ALIAS_PREFIX))


def reason_words(record) -> str:
    """What the card says of a project a guard paused ('' for any other record): the guard's words."""
    if not isinstance(record, dict) or record.get("state") != PAUSED:
        return ""
    words = REASON_WORDS.get(record.get("state_reason"))
    return f"停住 · {words}" if words else ""
