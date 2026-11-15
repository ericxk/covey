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

from covey.core import devices, main_thread
from covey.core.history_storage import history_lines
from covey.core.jobs import topic_job_records
from covey.core.locks import LOCK
from covey.core.state import read_json, read_object, write_json, write_private_text
from covey.core.turn_words import request_line

_LOCK = threading.RLock()


def _key(chat_id: str, identifier: str) -> str:
    return hashlib.sha256(f"{chat_id}\0{identifier}".encode()).hexdigest()


def _alias_path(base: Path, chat_id: str, identifier: str) -> Path:
    """The alias file of the message or thread ``identifier`` of ``chat_id`` under ``base``, <state>/topics."""
    return base / "aliases" / f"{_key(chat_id, identifier)}.json"


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


def read_topic(ctx) -> dict:
    return read_json(ctx.history_path.parent / "topic.json")


def proxy_topic(ctx) -> bool:
    """Whether ctx's topic is a 代答 topic (its topic.json names the contact it answers)."""
    try:
        return bool(read_topic(ctx).get("proxy"))
    except (OSError, ValueError, AttributeError):
        return False


# topic.json's ``retired``: "desk" for a front-desk topic (前台话题, the desk_topics layout of 11-07) that
# core/migrate.py desk-retire froze (2026-11-10, the owner: 整块删除). Nothing goes into it any more: a message quoting
# its cards goes to the main thread (app/main_thread.py app_for), a click on them gets RETIRED_TEXT
# (app/task_controls.py retired_action), the main thread's tools say so (app/main_topics.py find_topic).
RETIRED_DESK = "desk"
RETIRED_TEXT = "这个前台话题已下线，不再接新消息；在主线程里直接说要做什么。"


def retired_topic(state_dir, topic_id) -> str:
    """``topic_id``'s topic.json ``retired`` (RETIRED_DESK), '' when it is none's or cannot be read."""
    if not topic_id or not isinstance(topic_id, str) or "/" in topic_id:
        return ""
    retired = read_object(topic_file(Path(state_dir), topic_id)).get("retired")
    return retired if isinstance(retired, str) else ""


def topic_traits(ctx) -> main_thread.Traits:
    """What ctx's topic does as the main thread's or a topic's (core/main_thread.py Traits): a context has no chat,
    so its topic.json names it. TOPIC when that cannot be read."""
    try:
        data = read_topic(ctx)
    except (OSError, ValueError, AttributeError):
        return main_thread.TOPIC
    return main_thread.traits_of(data) if isinstance(data, dict) else main_thread.TOPIC


def workspace_locked(ctx) -> bool:
    """The topic's workspace is settled: chosen, or the topic ran before workspace choice existed."""
    data = read_topic(ctx)
    if data.get("workspace_locked") or ctx.session_path.exists():
        return True
    # Preserve every topic that already executed before this feature. The topic's records only:
    # copying all of them took 0.1-0.2 s, several times per message (2026-10-26).
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


def topic_number(state_dir: Path, topic_id: str) -> int:
    """A short number shown on a topic's cards (T12), so the topic can be named and found.
    Topics created before numbering get theirs when first shown. 0 if there is no topic."""
    if not topic_id:
        return 0
    path = topic_file(state_dir, topic_id)
    # Same lock order as launch submission (workspace LOCK, then _LOCK): topic.json has one writer at a time.
    with LOCK, _LOCK:
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return 0
        if not main_thread.traits(topic_id, data.get("chat_id")).numbered:
            return 0  # The main thread (app/main_thread.py) is no numbered topic.
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
    """The topic's title, the one every card, the board and the main thread name it by (C9, 2026-11-08): the first
    line of the title the main thread gave it (topic.json ``origin.title``, app/main_topics.py), else the first line
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


def context_for_topic(ctx: Any, event: dict[str, Any], sessions_dir: Any, *, legacy_harness_id: str | None = None) -> Any:
    chat_id = str(event.get("chat_id") or "")
    message_id = str(event.get("message_id") or event.get("id") or "")
    root_id = str(event.get("root_id") or "")
    thread_id = str(event.get("thread_id") or "")
    if not chat_id or not message_id:
        return ctx
    # Root/message aliases also work when a later event omits thread_id.
    # Parent aliases cover replies to a message already seen in the topic.
    parent_id = str(event.get("parent_id") or "")
    identifiers = list(dict.fromkeys(value for value in (root_id, thread_id, message_id) if value))
    base = ctx.state_dir / "topics"
    with _LOCK:
        topic_id = _key(chat_id, root_id or parent_id or message_id)
        # In a thread, with the main thread on, only the quoted message's routes_to counts (S8, 2026-11-08: the main
        # thread's card is in the owner's message's thread, so quoting it there is the topic it handed on to; the
        # thread is not that topic's). The mode 纯话题 keeps a thread its root's.
        quoted = _routes_to(base, chat_id, parent_id) if not thread_id or main_thread.enabled() else ""
        if quoted:
            # A reply quoting a message routed to a topic (``route_message``): its chain's root is the owner's
            # message, the main thread's, so the quoted message decides, and only this message becomes the topic's
            # alias.
            topic_id, identifiers = quoted, [message_id]
        for identifier in [] if quoted else [*identifiers, parent_id]:
            if not identifier:
                continue
            alias = _alias_path(base, chat_id, identifier)
            if alias.exists():
                topic_id = json.loads(alias.read_text())["topic_id"]
                break
        if not thread_id and main_thread.is_main_topic(topic_id, chat_id) and not main_thread.enabled():
            # The mode 纯话题 (docs/main-thread.md 私聊模式): a top-level reply quoting one of the main thread's
            # messages is a new message like any other, its own topic; the main thread's messages stay its own.
            root_id = parent_id = ""
            topic_id, identifiers = _key(chat_id, message_id), [message_id]
        path = base / topic_id / "topic.json"
        existing = path.exists()
        if existing:
            data = json.loads(path.read_text())
        else:
            data = {
                "topic_id": topic_id, "chat_id": chat_id,
                "root_id": root_id or parent_id or message_id, "thread_id": thread_id,
                "workspace_name": ctx.workspace_name, "workdir": devices.stored_value(ctx),
                "topic_number": _next_topic_number(ctx.state_dir),
            }
            write_private_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        if ctx.options.get("harness_id") and not data.get("harness_id"):
            # Unified routing pins old unbound topics to Codex even when this bot's new default changed.
            name = legacy_harness_id if existing and legacy_harness_id else ctx.options["harness_id"]
            data.update(harness_id=name, binding_revision=1)
            write_private_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        for identifier in identifiers:
            alias = _alias_path(base, chat_id, identifier)
            if not alias.exists() or json.loads(alias.read_text()).get("topic_id") != topic_id:
                write_private_text(alias, json.dumps({"topic_id": topic_id}) + "\n")
    # A device value (``gpu1:/home/me/proj``) becomes the stand-in and the device's directory; one for a device that
    # devices.json no longer lists still does (refused by name when a turn starts, app/runs.py).
    binding = devices.bind_workdir(ctx, data["workdir"], strict=False)
    workdir = binding.workdir
    # The main thread's topic has no root: each message is answered quoting it, at the chat's top level.
    top_level = main_thread.traits(topic_id, data.get("chat_id")).top_level
    reply_to = message_id if top_level else data["root_id"]
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
    """Persist outgoing card aliases so callbacks need no network lookup."""
    if not topic_id or not chat_id or not message_id:
        return
    path = _alias_path(state_dir / "topics", chat_id, message_id)
    with _LOCK:
        if not path.exists():
            write_private_text(path, json.dumps({"topic_id": topic_id}) + "\n")


def move_topic_message(state_dir: Path, topic_id: str, chat_id: str, message_id: str) -> None:
    """Make the card message ``message_id`` belong to ``topic_id``, whichever topic it belonged to: the owner's
    message a 话题区 takes as its root (``claim_area_root``); clicks on it and replies quoting it go to that topic from then on. Every other
    alias is written once (``remember_topic_message``)."""
    if not topic_id or not chat_id or not message_id:
        return
    path = _alias_path(state_dir / "topics", chat_id, message_id)
    with _LOCK:
        write_private_text(path, json.dumps({"topic_id": topic_id}) + "\n")


def route_message(state_dir: Path, chat_id: str, message_id: str, topic_id: str, *, holder: str = "") -> None:
    """A top-level reply quoting the message ``message_id`` goes to ``topic_id`` (``context_for_topic``,
    app/main_thread.py app_for), while the message stays whose it was: a reply the Bridge sends to it stays where that
    topic's replies go (feishu/message_request.py reply_in_thread). For the main thread's card that handed work to one
    话题区 alone, and a relay quoting it (app/main_relay.py: the owner, 2026-11-07, 引用直达)."""
    if not topic_id or not chat_id or not message_id:
        return
    path = _alias_path(state_dir / "topics", chat_id, message_id)
    with _LOCK:
        try:
            alias = json.loads(path.read_text())
        except (OSError, ValueError):
            alias = {}
        # A message no topic knew yet is ``holder``'s (the main thread's card), else the topic's.
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
    number = read_object(topic_file(Path(state_dir), topic_id)).get("topic_number")
    return (f"T{number}" if type(number) is int else ""), topic_title(Path(state_dir), topic_id) or unknown


def claim_area_root(state_dir, chat_id: str, message_id: str) -> str:
    """The owner's message ``message_id`` made the root of a 话题区 (app/main_topics.py topic_start, app/forks.py
    a fork the main thread opens): the main thread's message until now, from now on the topic's whose key it makes
    (its first context makes the topic, replies to it go in its thread). That topic's id."""
    topic_id = topic_key(chat_id, message_id)
    move_topic_message(Path(state_dir), topic_id, chat_id, message_id)
    return topic_id
