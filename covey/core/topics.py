"""Durable, chat-scoped Feishu topic -> Harness session bindings, and the readers and writers of a
topic's topic.json."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import threading
from pathlib import Path
from typing import Any

from covey.core import collab, devices, butler, projects, web_sync
from covey.core.history_storage import history_lines
from covey.core.jobs import topic_job_records
from covey.core.locks import LOCK, resource_lock
from covey.core.state import read_json, read_object, write_json, write_private_text
from covey.core.turn_words import request_line

# Held only to write: a new topic (its number from counter.json), the Harness of an old one pinned, a number given to a
# topic from before numbering. Never to read: topic.json and the aliases are replaced whole (core/state.py
# atomic_write_bytes), so a read sees one version. A read under it that hung on the network disk held up every click and
# message of the Bridge for 8 minutes (T680).
_LOCK = threading.RLock()
# (state dir, topic id) -> its T number: given once and never changed, so kept from its first read.
_numbers: dict[tuple[str, str], int] = {}


def _key(chat_id: str, identifier: str) -> str:
    return hashlib.sha256(f"{chat_id}\0{identifier}".encode()).hexdigest()


def _alias_path(base: Path, chat_id: str, identifier: str) -> Path:
    """The alias file of the message or thread ``identifier`` of ``chat_id`` under ``base``, <state>/topics."""
    return base / "aliases" / f"{_key(chat_id, identifier)}.json"


def _alias_lock(path: Path):
    """The lock of one alias file: each of its writers looks at it and writes it under this, and takes no other lock
    inside (the innermost lock, after LOCK and _LOCK). A write hung on the network disk holds up that alias alone."""
    return resource_lock("topic-alias", str(path))


def topic_key(chat_id: str, root_id: str) -> str:
    """The id of the topic a message ``root_id`` of ``chat_id`` opens (context_for_topic), before it exists."""
    return _key(chat_id, root_id)


def _next_topic_number(state_dir: Path) -> int:
    """Caller holds _LOCK. The counter is written first, so a crash leaves a gap, never a repeat."""
    counter = state_dir / "topics" / "counter.json"
    try:
        number = int(json.loads(counter.read_text())["next"])
    except (OSError, ValueError, KeyError, TypeError):
        number = 1
    write_private_text(counter, json.dumps({"next": number + 1}) + "\n")
    return number


def topic_file(state_dir: Path, topic_id: str) -> Path:
    """<state>/topics/<topic_id>/topic.json, the topic's binding."""
    return state_dir / "topics" / topic_id / "topic.json"


def load_topic(state_dir: Path, topic_id: str) -> dict:
    """The topic's topic.json; read_json's errors reach the caller."""
    return read_json(topic_file(state_dir, topic_id))


def topic_origin(state_dir: Path, topic_id: str) -> dict:
    """The ``origin`` of the topic's topic.json (where it came from: the butler's request for it, ``kind`` "main", or the
    web page's, "web"; app/butler_topics.py bind_topic, app/web_launch.py), {} for a topic without one or one whose
    topic.json cannot be read. Reads without a lock (rule 2): the file is replaced whole. The relay of a turn reads it
    before the request ledger (app/relay_targets.py), which daily maintenance clears after 7 days."""
    try:
        data = load_topic(Path(state_dir), str(topic_id or ""))
    except (OSError, ValueError):
        return {}
    origin = data.get("origin") if isinstance(data, dict) else None
    return origin if isinstance(origin, dict) else {}


def read_topic(ctx) -> dict:
    return read_json(ctx.history_path.parent / "topic.json")


def proxy_topic(ctx) -> bool:
    """Whether ctx's topic is a 代答 topic (its topic.json names the contact it answers)."""
    try:
        return bool(read_topic(ctx).get("proxy"))
    except (OSError, ValueError, AttributeError):
        return False


# topic.json's ``retired``: "desk" for a front-desk topic (前台话题, the retired desk_topics layout) that
# core/migrate.py desk-retire froze. Nothing goes into it any more: a message quoting
# its cards goes to the butler (app/butler.py app_for), a click on them gets RETIRED_TEXT
# (app/task_controls.py retired_action), the butler's tools say so (app/butler_topics.py find_topic).
RETIRED_DESK = "desk"
RETIRED_TEXT = "这个前台话题已下线，不再接新消息；在私聊里直接跟管家说要做什么。"


def retired_topic(state_dir, topic_id) -> str:
    """``topic_id``'s topic.json ``retired`` (RETIRED_DESK), '' when it is none's or cannot be read."""
    if not topic_id or not isinstance(topic_id, str) or "/" in topic_id:
        return ""
    retired = read_object(topic_file(Path(state_dir), topic_id)).get("retired")
    return retired if isinstance(retired, str) else ""


def topic_traits(ctx) -> butler.Traits:
    """What ctx's topic does as the butler's or a topic's (core/butler.py Traits): a context has no chat,
    so its topic.json names it. TOPIC when that cannot be read."""
    try:
        data = read_topic(ctx)
    except (OSError, ValueError, AttributeError):
        return butler.TOPIC
    return butler.traits_of(data) if isinstance(data, dict) else butler.TOPIC


def topic_project(state_dir, topic_id):
    """The ``project`` field of ``topic_id``'s topic.json (core/projects.py ``TopicProject``: id, role, chat_id), None
    without one. Read by a stat, kept until the file changes; not gated by COVEY_PROJECTS."""
    return projects.topic_project(state_dir, topic_id)


def set_topic_project(state_dir, topic_id, project_id, role) -> bool:
    """Write ``project`` {id, role} (core/projects.py: ``lead`` or ``thread``) into ``topic_id``'s topic.json, leaving
    the rest of it as it is; whether it changed (False: the field was already that). Under the same locks as
    ``topic_number`` (workspace ``LOCK``, then ``_LOCK``): the caller holds no network call and, if it holds a topic's lock
    (core/locks.py topic_lock), takes these after it, as every topic.json rewrite does. A missing topic.json raises."""
    field = projects.project_field(project_id, role)
    path = topic_file(Path(state_dir), topic_id)
    with LOCK, _LOCK:
        data = read_json(path)
        if data.get("project") == field:
            return False
        data["project"] = field
        write_private_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return True


def clear_topic_project(state_dir, topic_id) -> bool:
    """Take ``project`` out of ``topic_id``'s topic.json (a project turned back into a plain job: 不要专项); whether
    there was one. A missing topic.json raises."""
    path = topic_file(Path(state_dir), topic_id)
    with LOCK, _LOCK:
        data = read_json(path)
        if "project" not in data:
            return False
        del data["project"]
        write_private_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return True


def workspace_locked(ctx) -> bool:
    """The topic's workspace is settled: chosen, or the topic ran before workspace choice existed."""
    data = read_topic(ctx)
    if data.get("workspace_locked") or ctx.session_path.exists():
        return True
    # Preserve every topic that already executed before this feature. The topic's records only:
    # copying all of them took 0.1-0.2 s, several times per message.
    return any(r.get("status") not in {"waiting_workspace", "cancelled"}
               for r in topic_job_records(ctx.state_dir, ctx.topic_id))


def bind_topic(ctx, name: str, directory: Path, *, stored: str = "") -> None:
    """Fix the topic's workspace. ``stored``: the value to write for a device (``gpu1:/home/me/proj``, core/devices.py
    ``Binding.value``), with ``directory`` its stand-in; '' for a directory here."""
    with LOCK:
        path = ctx.history_path.parent / "topic.json"
        data = read_json(path)
        # Legacy topics with a session are already bound.
        if (data.get("workspace_locked") or ctx.session_path.exists()) and (
            data["workdir"] != stored if stored else Path(data["workdir"]).resolve() != directory.resolve()
        ):
            raise ValueError("本话题已绑定工作区，请在其他工作区新开话题")
        data.update(workspace_name=name, workdir=stored or str(directory), workspace_locked=True)
        write_json(path, data, ensure_ascii=False, indent=2)


def home_start(data) -> bool:
    """Whether a topic (its topic.json ``data``) started in the home (core/home.py): its rules show the home block and
    the repositories it uses, not a workspace. A topic from before the home keeps its workspace."""
    return isinstance(data, dict) and data.get("home_start") is True


def mark_home(data: dict, *, title: str = "", now: float | None = None) -> dict:
    """``data`` (a topic.json being written, under LOCK) marked as started in the home: ``home_start``, and once its
    ``started_at`` and ``slug`` (core/home.py ``slug`` of ``title``), which app/home_turn.py ``outputs_dir`` names the
    topic's outputs directory by, so it never moves after (a missing start would be each day's date). Every writer of
    ``home_start`` comes here: ``bind_home``, the butler's and the page's topics (app/butler_topics.py bind_topic), the
    launch card's (app/launch_submit.py), a project's lead (app/projects.py) and a fork (app/forks.py). ``data``."""
    import time

    from covey.core.home import slug

    data["home_start"] = True
    if not isinstance(data.get("started_at"), (int, float)) or isinstance(data.get("started_at"), bool):
        data["started_at"] = time.time() if now is None else now
    if not str(data.get("slug") or "").strip():
        data["slug"] = slug(title)
    return data


def bind_home(ctx, home: Path, *, name: str = "", repos=(), title: str = "", now: float | None = None) -> None:
    """Start the topic in the home ``home``: ``home_start``, its workdir the home (``workspace_name`` ``name`` and the
    workspace fields still written, so an older release reads the topic as before), the repositories it is given
    (``repos``: TopicRepo dicts, ``source`` declared), and, once, its start time and ``slug`` (core/home.py ``slug`` of
    ``title``): its outputs directory's name never changes after (app/home_turn.py ``outputs_dir``)."""
    with LOCK:
        path = ctx.history_path.parent / "topic.json"
        data = read_json(path)
        if (data.get("workspace_locked") or ctx.session_path.exists()) and \
                Path(str(data.get("workdir") or "")).resolve() != Path(home).resolve():
            raise ValueError("本话题已绑定工作区，请在其他工作区新开话题")
        data.update(workspace_name=name or data.get("workspace_name") or ctx.workspace_name, workdir=str(home),
                    workspace_locked=True)
        mark_home(data, title=title, now=now)
        if repos:
            data["repos"] = _merged_repos(data.get("repos"), list(repos))
        write_json(path, data, ensure_ascii=False, indent=2)


def _merged_repos(old, new) -> list:
    """``old`` (a topic.json ``repos``) with each of ``new`` put in by name: an entry there keeps what it has, a write
    wins over a read."""
    merged = {str(item.get("name")): dict(item) for item in old or () if isinstance(item, dict) and item.get("name")}
    for item in new:
        name = str(item.get("name") or "")
        if not name:
            continue
        entry = merged.setdefault(name, dict(item))
        if item.get("mode") == "write":
            entry["mode"] = "write"
    return list(merged.values())


def update_repos(state_dir: Path, topic_id: str, change, *, key="repos") -> list:
    """``change(repos) -> repos`` applied to the topic's topic.json ``repos`` (``key``: another list of dicts, as
    ``devices``, ``update_devices``) under LOCK; the list
    written, [] when the topic is not there. Nothing is written when nothing changed. Like every topic.json writer it
    holds LOCK of its own process only: the read, the change and the write follow each other at once (no git, no
    network in between), so another process's write of the same moment is the one window left, as for the others. Read
    first without LOCK: a change that changes nothing (most turns' end) takes no lock at all (T870 Wave 3.5: the end of
    every turn waited on it before its answer went out)."""
    path = topic_file(Path(state_dir), topic_id)

    def read():
        data = read_json(path)
        return data, [dict(item) for item in data.get(key) or () if isinstance(item, dict)]
    try:
        _, before = read()
    except (OSError, ValueError):
        return []
    after = change([dict(item) for item in before])
    if after == before:
        return after
    with LOCK:
        try:
            data, before = read()
        except (OSError, ValueError):
            return []
        after = change([dict(item) for item in before])
        if after != before:
            data[key] = after
            write_json(path, data, ensure_ascii=False, indent=2)
        return after


def update_devices(state_dir: Path, topic_id: str, change) -> list:
    """``change(devices) -> devices`` on the topic's topic.json ``devices`` (core/device_access.py ``fold``), as
    ``update_repos`` does it."""
    return update_repos(state_dir, topic_id, change, key="devices")


def topic_number(state_dir: Path, topic_id: str) -> int:
    """A short number shown on a topic's cards (T12), so the topic can be named and found.
    Topics created before numbering get theirs when first shown. 0 if there is no topic.

    Read without a lock and kept from then on (a number never changes): every card, the 专项卡 each second for each of
    its topics and the board ask for it. Only a topic without a number yet takes the locks topic.json's writers hold
    (``_give_number``). Under them, a read hung on the network disk once held up every route of the Bridge (T680)."""
    if not topic_id:
        return 0
    key = (str(Path(state_dir)), topic_id)
    number = _numbers.get(key)
    if number is not None:
        return number
    path = topic_file(state_dir, topic_id)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return 0
    if not butler.traits(topic_id, data.get("chat_id")).numbered:
        return 0  # The butler (app/butler.py) is no numbered topic.
    number = data.get("topic_number")
    if type(number) is not int:
        number = _give_number(state_dir, path)
        if not number:
            return 0
    _numbers[key] = number
    return number


def _give_number(state_dir: Path, path: Path) -> int:
    """The number of a topic from before numbering, given once: topic.json read again under the locks of its writers
    (the order of launch submission: workspace LOCK, then _LOCK), so two callers cannot give it two. 0 when unreadable."""
    with LOCK, _LOCK:
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return 0
        if type(data.get("topic_number")) is not int:
            data["topic_number"] = _next_topic_number(state_dir)
            write_private_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        return data["topic_number"]


def topic_label(state_dir: Path, topic_id: str) -> str:
    number = topic_number(state_dir, topic_id)
    return f"T{number}" if number else ""


_titles: dict[tuple[str, str], tuple[tuple[int, int], str]] = {}  # (state dir, topic id) -> (topic.json's mtime and size, its title).
# A half-width colon or comma next to a Chinese character (验收:睡 70 秒) reads as the full-width one.
_HALF_WIDTH = re.compile(r"(?<=[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef])\s*([:,])\s*"
                         r"|\s*([:,])\s*(?=[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef])")
_FULL_WIDTH = {":": "：", ",": "，"}


def shown_title(text: Any) -> str:
    """A title as every card shows it: its whitespace one space, a half-width colon or comma beside a Chinese
    character full-width."""
    text = " ".join(str(text or "").split())
    return _HALF_WIDTH.sub(lambda match: _FULL_WIDTH[match.group(1) or match.group(2)], text)


def topic_title(state_dir: Path, topic_id: str) -> str:
    """The topic's title, the one every card, the board and the butler name it by (C9): the first
    line of the title the butler gave it (topic.json ``origin.title``, app/butler_topics.py), else the first line
    that says something of its first user message (core/turn_words.py request_line: not a heading the Bridge put
    before the words), as ``shown_title`` writes it; "" without either. The caller cuts it to its own length. Kept
    until topic.json changes."""
    if not topic_id:
        return ""
    path = topic_file(state_dir, topic_id)
    try:
        stat = path.stat()
    except OSError:
        return ""
    key, stamp = (str(state_dir), topic_id), (stat.st_mtime_ns, stat.st_size)
    kept = _titles.get(key)
    if kept is not None and kept[0] == stamp:
        return kept[1]
    try:
        origin = json.loads(path.read_text()).get("origin")
    except (OSError, ValueError, AttributeError):
        return ""
    title = origin.get("title") if isinstance(origin, dict) else None
    title = title.strip().split("\n")[0] if isinstance(title, str) else ""
    title = shown_title(title or request_line(_first_user_message(path.parent / "history.jsonl")))
    if title:
        _titles[key] = (stamp, title)  # A topic without one yet is read again: its first message may come.
    return title


def _first_user_message(path: Path) -> str:
    """The text of the first user message the topic's history keeps (among its first 20 rows), '' without one."""
    lines = history_lines(path)
    try:
        for count, line in enumerate(lines):
            if count >= 20:
                break
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("role") == "user":
                return str(row.get("display_content") or row.get("content") or "")
    except OSError:
        return ""
    finally:
        lines.close()
    return ""


@dataclasses.dataclass(frozen=True)
class _Place:
    """Where an event goes (``_place``): its topic's id, topic.json and its contents."""
    topic_id: str
    path: Path
    data: dict
    existing: bool  # topic.json was there when looked at.
    pending: bool  # Something must still be written: the topic made, its Harness pinned or an alias (only when not writing).


def _place(ctx, event, base: Path, legacy_harness_id, *, write: bool, make: bool = True) -> _Place:
    """The topic ``event`` goes to (``context_for_topic``), found by its aliases. ``write`` False: nothing is written, and
    ``pending`` says whether something would be. ``write`` True (the caller holds _LOCK): the topic is made (only when
    ``make``: ``ctx`` is the one a new topic starts from; else it stays pending), its Harness pinned and the aliases written
    as needed."""
    chat_id = str(event.get("chat_id") or "")
    message_id = str(event.get("message_id") or event.get("id") or "")
    root_id = str(event.get("root_id") or "")
    thread_id = str(event.get("thread_id") or "")
    # Root/message aliases also work when a later event omits thread_id.
    # Parent aliases cover replies to a message already seen in the topic.
    parent_id = str(event.get("parent_id") or "")
    identifiers = list(dict.fromkeys(value for value in (root_id, thread_id, message_id) if value))
    topic_id = _key(chat_id, root_id or parent_id or message_id)
    # In a thread, with the butler on, only the quoted message's routes_to counts (the butler's
    #  card is in the owner's message's thread, so quoting it there is the topic it handed on to; the
    # thread is not that topic's). With the butler off (COVEY_BUTLER=0) a thread stays its root's.
    quoted = _routes_to(base, chat_id, parent_id) if not thread_id or butler.enabled() else ""
    if quoted:
        # A reply quoting a message routed to a topic (``route_message``): its chain's root is the owner's
        # message, the butler's, so the quoted message decides, and only this message becomes the topic's
        # alias.
        topic_id, identifiers = quoted, [message_id]
    for identifier in [] if quoted else [*identifiers, parent_id]:
        if not identifier:
            continue
        alias = _alias_path(base, chat_id, identifier)
        if alias.exists():
            topic_id = json.loads(alias.read_text())["topic_id"]
            break
    if not thread_id and butler.is_butler_topic(topic_id, chat_id) and not butler.enabled():
        # The butler off (COVEY_BUTLER=0; its records stay): a top-level reply quoting one of the butler's
        # messages is a new message like any other, its own topic; the butler's messages stay its own.
        root_id = parent_id = ""
        topic_id, identifiers = _key(chat_id, message_id), [message_id]
    path = base / topic_id / "topic.json"
    existing = path.exists()
    if existing:
        data = json.loads(path.read_text())
    elif not write or not make:
        return _Place(topic_id, path, {}, False, True)
    else:
        data = {
            "topic_id": topic_id, "chat_id": chat_id,
            "root_id": root_id or parent_id or message_id, "thread_id": thread_id,
            "workspace_name": ctx.workspace_name, "workdir": devices.stored_value(ctx),
            "topic_number": _next_topic_number(ctx.state_dir),
        }
        write_private_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    if ctx.options.get("harness_id") and not data.get("harness_id"):
        if not write:
            return _Place(topic_id, path, data, existing, True)
        # Unified routing pins old unbound topics to Codex even when this bot's new default changed.
        name = legacy_harness_id if existing and legacy_harness_id else ctx.options["harness_id"]
        data.update(harness_id=name, binding_revision=1)
        write_private_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    for identifier in identifiers:
        alias = _alias_path(base, chat_id, identifier)
        if alias.exists() and json.loads(alias.read_text()).get("topic_id") == topic_id:
            continue
        if not write:
            return _Place(topic_id, path, data, existing, True)
        with _alias_lock(alias):
            if not alias.exists() or json.loads(alias.read_text()).get("topic_id") != topic_id:
                write_private_text(alias, json.dumps({"topic_id": topic_id}) + "\n")
    return _Place(topic_id, path, data, existing, False)


def context_for_topic(ctx: Any, event: dict[str, Any], sessions_dir: Any, *, legacy_harness_id: str | None = None,
                      default=None) -> Any:
    """``ctx`` in the topic of ``event``: found by its aliases, made when it is new. ``default()``, when given, is the
    context a new topic starts from (core/workspaces.py topic_context: the default workspace), asked for only when the
    topic is still to be made. Looked up without a lock first: a message or a click in a topic that is there, its aliases
    written, takes none, so a read hung on the network disk holds up its own thread alone. What must be written is
    written under _LOCK, everything looked up again there (T680)."""
    chat_id = str(event.get("chat_id") or "")
    message_id = str(event.get("message_id") or event.get("id") or "")
    if not chat_id or not message_id:
        return ctx
    base = ctx.state_dir / "topics"
    place = _place(ctx, event, base, legacy_harness_id, write=False)
    defaulted = default is None
    while place.pending:
        if not place.existing and not defaulted:
            # Before _LOCK: it may take LOCK (core/workspaces.py load_config), which comes first.
            ctx, defaulted = default(), True
        with _LOCK:
            place = _place(ctx, event, base, legacy_harness_id, write=True, make=defaulted)
    topic_id, path, data = place.topic_id, place.path, place.data
    # A device value (``gpu1:/home/me/proj``) becomes the stand-in and the device's directory; one for a device that
    # devices.json no longer lists still does (refused by name when a turn starts, app/runs.py).
    binding = devices.bind_workdir(ctx, data["workdir"], strict=False)
    workdir = binding.workdir
    # The butler's topic has no root: each message is answered quoting it, at the chat's top level.
    top_level = butler.traits(topic_id, data.get("chat_id")).top_level
    reply_to = message_id if top_level else data["root_id"]
    if projects.own_message(reply_to) or web_sync.is_web_message(reply_to) or collab.own_message(reply_to):
        # A project's lead woken or handed the butler's words, words typed on the web page, a turn of 协作: Feishu has no
        # such message, so the answer is a new top-level message.
        reply_to = ""
    return dataclasses.replace(
        ctx, topic_id=topic_id, reply_to_message_id=reply_to,
        workspace_name=data["workspace_name"], workdir=workdir,
        **({"device": binding.device, "remote_workdir": binding.remote_workdir}
           if binding.device or getattr(ctx, "device", "") else {}),
        history_path=path.parent / "history.jsonl",
        session_path=path.parent / ctx.session_path.name,
        sessions_dir=sessions_dir(workdir),
        options={**ctx.options, **({"harness_id": data["harness_id"],
                                   "binding_revision": data.get("binding_revision", 1)}
                                  if data.get("harness_id") else {})},
    )


def _routes_to(base: Path, chat_id: str, parent_id: str) -> str:
    """The topic ``parent_id``'s alias routes a quote of it to (``route_message``), or ''."""
    if not parent_id:
        return ""
    try:
        return str(json.loads(_alias_path(base, chat_id, parent_id).read_text()).get("routes_to") or "")
    except (OSError, ValueError, AttributeError):
        return ""


def remember_topic_message(state_dir: Path, topic_id: str, chat_id: str, message_id: str) -> None:
    """Persist outgoing card aliases so callbacks need no network lookup. Written once, under the alias's own lock (one
    already there is left as it is), never _LOCK: each card sent and each message of the butler's writes one (T680)."""
    if not topic_id or not chat_id or not message_id:
        return
    path = _alias_path(state_dir / "topics", chat_id, message_id)
    if path.exists():
        return
    with _alias_lock(path):
        if not path.exists():
            write_private_text(path, json.dumps({"topic_id": topic_id}) + "\n")


def move_topic_message(state_dir: Path, topic_id: str, chat_id: str, message_id: str) -> None:
    """Make the card message ``message_id`` belong to ``topic_id``, whichever topic it belonged to: the owner's
    message a 话题区 takes as its root (``claim_area_root``); clicks on it and replies quoting it go to that topic from then on. Every other
    alias is written once (``remember_topic_message``)."""
    if not topic_id or not chat_id or not message_id:
        return
    path = _alias_path(state_dir / "topics", chat_id, message_id)
    with _alias_lock(path):
        write_private_text(path, json.dumps({"topic_id": topic_id}) + "\n")


def route_message(state_dir: Path, chat_id: str, message_id: str, topic_id: str, *, holder: str = "") -> None:
    """A top-level reply quoting the message ``message_id`` goes to ``topic_id`` (``context_for_topic``,
    app/butler.py app_for), while the message stays whose it was: a reply the Bridge sends to it stays where that
    topic's replies go (feishu/message_request.py reply_in_thread). For the butler's card that handed work to one
    话题区 alone, and a relay quoting it (app/butler_relay.py: 引用直达)."""
    if not topic_id or not chat_id or not message_id:
        return
    path = _alias_path(state_dir / "topics", chat_id, message_id)
    with _alias_lock(path):
        try:
            alias = json.loads(path.read_text())
        except (OSError, ValueError):
            alias = {}
        # A message no topic knew yet is ``holder``'s (the butler's card), else the topic's.
        alias = alias if isinstance(alias, dict) and alias.get("topic_id") else {"topic_id": holder or topic_id}
        if alias.get("routes_to") != topic_id:
            write_private_text(path, json.dumps({**alias, "routes_to": topic_id}) + "\n")


def routed_topic(state_dir: Path, chat_id: str, message_id: str) -> str:
    """The topic a quote of ``message_id`` goes to by ``route_message``, or ''."""
    if not chat_id or not message_id:
        return ""
    return _routes_to(state_dir / "topics", chat_id, message_id)


def message_topic(state_dir: Path, chat_id: str, message_id: str) -> str:
    """The topic a message (or a card the bridge sent) belongs to, or ''."""
    if not chat_id or not message_id:
        return ""
    try:
        return str(json.loads(_alias_path(state_dir / "topics", chat_id, message_id).read_text())
                   .get("topic_id") or "")
    except (OSError, ValueError, AttributeError):
        return ""


def has_topic_message(state_dir: Path, chat_id: str, message_id: str) -> bool:
    return _alias_path(state_dir / "topics", chat_id, message_id).exists()


def label_title(state_dir, topic_id, unknown=""):
    """(T number, title) of ``topic_id`` as topic.json has them now, read only: "" for a topic without a number
    yet (``topic_number`` gives one), ``unknown`` for no title (``topic_title``)."""
    if not topic_id:
        return "", unknown
    if projects.enabled():  # A project's lead is the board's cell of the project (core/projects.py cells): its name.
        record = projects.lead_record(state_dir, topic_id)
        if record:
            return "专项", " ".join(str(record.get("name") or "").split()) or unknown
    number = _numbers.get((str(Path(state_dir)), topic_id))  # Kept by topic_number: a number never changes.
    if number is None:
        number = read_object(topic_file(Path(state_dir), topic_id)).get("topic_number")
    return (f"T{number}" if type(number) is int else ""), topic_title(Path(state_dir), topic_id) or unknown


def claim_area_root(state_dir, chat_id: str, message_id: str) -> str:
    """The owner's message ``message_id`` made the root of a 话题区 (app/butler_topics.py topic_start, app/forks.py
    a fork the butler opens): the butler's message until now, from now on the topic's whose key it makes
    (its first context makes the topic, replies to it go in its thread). That topic's id."""
    topic_id = topic_key(chat_id, message_id)
    move_topic_message(Path(state_dir), topic_id, chat_id, message_id)
    return topic_id
