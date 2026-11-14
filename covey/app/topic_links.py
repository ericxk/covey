"""Links that open a topic from a card (docs/main-thread.md 话题链接; the owner, 2026-11-07 02:00: 卡上的“转给 T404”
要能点，直接跳到话题，不要让我去看板翻; 2026-11-08 06:40: 点 T439 要到那张答案卡上，不只是话题).

Feishu's applinks open inside its clients: a message in a thread with ``client/thread/open`` (open_chat_id,
open_thread_id, thread_position: the message's thread_message_position), a place in a chat with ``client/chat/open``
(openChatId, position). A T number opens one card of its topic: a relay's, the card of the turn it relays (its
question card when it asks); any other, the topic's newest card. A threaded topic opens at that card in its thread;
a card that cannot be read opens the thread at its root message, as before 2026-11-08. Neither id is in topic.json
(topics were never told their thread_id): a message is read once through the messages API
(feishu/message_read.py fetch_message_detail, outside every lock, rules 1 and 2) and what it says is kept in
<state>/topic-links.json, under its topic (``messages``, the newest KEPT). A topic not found, or a read that fails,
gets no link: its T number stays plain text, as before.

``linkify`` turns the T numbers in a card's Markdown into links, outside code; only the main thread's cards and the
relay's call it (app/main_reply.py, app/main_relay.py).
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from urllib.parse import quote

from covey.core.jobs import topic_job_records
from covey.core.locks import resource_lock
from covey.core.state import read_object, write_json

LINKS = "topic-links.json"
THREAD = "https://applink.feishu.cn/client/thread/open?open_chat_id={chat}&open_thread_id={thread}"
MESSAGE = "https://applink.feishu.cn/client/chat/open?openChatId={chat}&position={position}"
KEPT = 8  # Messages read for one topic that topic-links.json keeps (its newest cards and its root).
FETCHES = 12  # At most this many messages are read for one text; the rest of its T numbers stay plain.
BUDGET = 5.0  # Seconds one text may spend reading messages (in parallel).
# A T number on its own: not inside a word or a link, nor one continued by a dot or dash and a letter or digit
# ("T444·330", "T444.md", "T12-3": ACCEPT-330 #2 read "T444·330 验收结束" as a link to T444); "T444 · 01:27" still is.
_NUMBER = re.compile(r"(?<![\w\[/#])(\*\*)?T(\d{2,6})(\*\*)?(?![\w\]])(?!\**[·・.\-][0-9A-Za-z])")
_CODE = re.compile(r"```.*?```|`[^`\n]*`|\[[^\]\n]*\]\([^)\n]*\)", re.S)
_index_cache: dict = {}


def thread_link(chat_id, thread_id, position="") -> str:
    """The thread, opened at the message whose thread_message_position is ``position`` ('' for the thread itself;
    -1 is its root message)."""
    url = THREAD.format(chat=quote(str(chat_id), safe=""), thread=quote(str(thread_id), safe=""))
    return f"{url}&thread_position={quote(str(position), safe='')}" if str(position) else url


def message_link(chat_id, position) -> str:
    return MESSAGE.format(chat=quote(str(chat_id), safe=""), position=quote(str(position), safe=""))


def _path(state_dir) -> Path:
    return Path(state_dir) / LINKS


def _save(state_dir, topic_id, message_id, where) -> None:
    """Keep what reading ``message_id`` of ``topic_id`` said: its topic's ``messages``, the newest KEPT."""
    path = _path(state_dir)
    with resource_lock("record", path.absolute()):
        data = read_object(path)
        entry = data.get(topic_id) if isinstance(data.get(topic_id), dict) else {}
        messages = entry.get("messages") if isinstance(entry.get("messages"), dict) else {}
        messages[message_id] = where
        newest = sorted(messages, key=lambda mid: float((messages[mid] or {}).get("at") or 0), reverse=True)[:KEPT]
        data[topic_id] = {**entry, "messages": {mid: messages[mid] for mid in newest}}
        write_json(path, data, ensure_ascii=False, indent=1)


RECENT = 14 * 86400  # Only topics active this recently link: an old "T13" in a text is more often another bot's.


def _active(folder, now) -> bool:
    """Whether the topic in ``folder`` had a turn within RECENT (its history.jsonl, else topic.json, was written)."""
    for name in ("history.jsonl", "topic.json"):
        try:
            return now - (folder / name).stat().st_mtime <= RECENT
        except OSError:
            continue
    return False


def topic_numbers(state_dir, chat_id) -> dict:
    """{T number: topic id} of ``chat_id``'s topics active within RECENT (``_active``), read again when a topic is
    added (the topics directory's mtime changes) or an hour on. A main-thread answer quotes other bots' topics too
    (2026-11-07: 小娜's "T13" linked to this chat's month-old T13)."""
    base = Path(state_dir) / "topics"
    try:
        stamp = base.stat().st_mtime
    except OSError:
        return {}
    key, now = (str(base), str(chat_id)), time.time()
    cached = _index_cache.get(key)
    if cached and cached[0] == stamp and now - cached[2] < 3600:
        return cached[1]
    numbers = {}
    for path in base.glob("*/topic.json"):
        data = read_object(path)
        if (data.get("chat_id") == chat_id and isinstance(data.get("topic_number"), int)
                and _active(path.parent, now)):
            numbers[data["topic_number"]] = path.parent.name
    _index_cache[key] = (stamp, numbers, now)
    return numbers


def _newest_card(state_dir, topic_id) -> str:
    records = [r for r in topic_job_records(Path(state_dir), topic_id) if r.get("card_message_id")]
    newest = max(records, key=lambda r: int(r.get("job_id") or 0), default=None)
    return str((newest or {}).get("card_message_id") or "")


def _read_message(message_id, profile, fetch) -> dict:
    from covey.feishu.message_read import message_item

    item = message_item(fetch(message_id, profile), message_id) or {}

    def field(name):  # A thread's first reply is at thread_message_position 0.
        value = item.get(name)
        return "" if value is None else str(value)
    return {"thread_id": field("thread_id"), "position": field("message_position"),
            "thread_position": field("thread_message_position")}


def _where(state_dir, topic_id, message_id, profile, fetch) -> dict:
    """{thread_id, position, thread_position} of ``message_id``, read once and kept (``_save``); {} when it cannot
    be read. An entry from before 2026-11-08 (one message a topic, no thread_position) still answers for its message."""
    if not str(message_id).startswith("om_"):
        return {}
    entry = read_object(_path(state_dir)).get(topic_id) or {}
    known = (entry.get("messages") or {}).get(message_id) if isinstance(entry.get("messages"), dict) else None
    if known is None and entry.get("message_id") == message_id:
        known = entry
    if known and (known.get("thread_id") or known.get("position")):
        return known
    try:
        where = _read_message(message_id, profile, fetch)
    except Exception:  # noqa: BLE001 - no link, the T number stays plain
        return {}
    if not (where["thread_id"] or where["position"]):
        return {}
    try:
        _save(state_dir, topic_id, message_id, {**where, "at": time.time()})
    except OSError:
        pass
    return where


def place_of(state_dir, topic_id, message_id, profile=None, fetch=None) -> dict:
    """{thread_id, position, thread_position} of the message ``message_id`` of ``topic_id``, read once and kept
    (``_where``); {} when it cannot be read."""
    return _where(state_dir, topic_id, message_id, profile, fetch)


def topic_thread_link(state_dir, topic_id, *, at=(), profile=None, fetch=None) -> tuple[str, str]:
    """(the applink, how) that opens ``topic_id``'s thread, the thread of its root message (topic.json ``root_id``):
    at the first of ``at`` (messages of one turn: its card, its answer) that is in that thread ("thread_at"), else at
    the thread itself ("thread"); with no thread of the root to be read, the root's place in the chat ("chat");
    ("", "") when neither can be read (the owner, 2026-11-10: 你问、你答点了都跳到那个话题串). Never raises."""
    try:
        if fetch is None:
            from covey.feishu.message_read import fetch_message_detail as fetch
        topic = read_object(Path(state_dir) / "topics" / str(topic_id) / "topic.json")
        chat = str(topic.get("chat_id") or "")
        if not chat:
            return "", ""
        root = _where(state_dir, topic_id, str(topic.get("root_id") or ""), profile, fetch)
        thread = root.get("thread_id")
        for message in at:
            where = _where(state_dir, topic_id, str(message or ""), profile, fetch)
            if where.get("thread_id") and where.get("thread_position") and where["thread_id"] == (thread or where["thread_id"]):
                return thread_link(chat, where["thread_id"], where["thread_position"]), "thread_at"
        if thread:
            return thread_link(chat, thread), "thread"
        return (message_link(chat, root["position"]), "chat") if root.get("position") else ("", "")
    except Exception:  # noqa: BLE001 - no link; the caller says so
        return "", ""


def link_for(state_dir, topic_id, *, card="", profile=None, fetch=None) -> str:
    """The applink that opens ``topic_id`` at ``card`` (one of its cards), else at its newest card; '' when it
    cannot be told."""
    if fetch is None:
        from covey.feishu.message_read import fetch_message_detail as fetch
    topic = read_object(Path(state_dir) / "topics" / str(topic_id) / "topic.json")
    chat = str(topic.get("chat_id") or "")
    if not chat:
        return ""
    card = card or _newest_card(state_dir, topic_id)
    at = _where(state_dir, topic_id, card, profile, fetch)
    if at.get("thread_id") and at.get("thread_position"):
        return thread_link(chat, at["thread_id"], at["thread_position"])
    root = _where(state_dir, topic_id, str(topic.get("root_id") or ""), profile, fetch)
    if root.get("thread_id"):
        return thread_link(chat, root["thread_id"])
    return message_link(chat, root["position"]) if root.get("position") else ""


def linkify(text, state_dir, chat_id, *, profile=None, fetch=None, skip=(), cards=None) -> str:
    """``text`` (card Markdown) with each T number of a topic in ``chat_id`` as a link to it, outside code and
    links already there; ``skip``: topic ids left plain (the card's own topic); ``cards``: {topic id: the card its
    T number opens} (a relay's own turn), any other opens at its topic's newest card (``link_for``). The messages of up to FETCHES topics
    are read at once, in parallel, for at most BUDGET seconds (a status list of eleven T numbers added six seconds
    one by one). Never raises."""
    if not text or "T" not in text:
        return text
    try:
        numbers = topic_numbers(state_dir, chat_id)
        parts, last = [], 0
        for code in _CODE.finditer(text):
            parts.append((False, text[last:code.start()]))
            parts.append((True, code.group(0)))
            last = code.end()
        parts.append((False, text[last:]))
        wanted = []
        for is_code, part in parts:
            if is_code:
                continue
            for match in _NUMBER.finditer(part):
                topic = numbers.get(int(match.group(2)))
                if topic and topic not in skip and topic not in wanted:
                    wanted.append(topic)
        wanted = wanted[:FETCHES]
        if not wanted:
            return text
        links = _links(state_dir, wanted, profile, fetch, cards or {})

        def replace(match):
            url = links.get(numbers.get(int(match.group(2))) or "")
            if not url:
                return match.group(0)
            label = f"T{match.group(2)}"
            if match.group(1) and match.group(3):
                return f"[**{label}**]({url})"
            return f"{match.group(1) or ''}[{label}]({url}){match.group(3) or ''}"

        return "".join(part if is_code else _NUMBER.sub(replace, part) for is_code, part in parts)
    except Exception:  # noqa: BLE001 - a card without links rather than no card
        return text


def _links(state_dir, topics, profile, fetch, cards) -> dict:
    """{topic id: link} for ``topics``, read in parallel within BUDGET; a topic not done in time has none."""
    from concurrent.futures import ThreadPoolExecutor, wait

    pool = ThreadPoolExecutor(max_workers=min(6, len(topics)), thread_name_prefix="topic-link")
    try:
        futures = {pool.submit(link_for, state_dir, topic, card=cards.get(topic) or "", profile=profile,
                               fetch=fetch): topic for topic in topics}
        done, _ = wait(futures, timeout=BUDGET)
        return {futures[future]: future.result() for future in done if not future.exception()}
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
