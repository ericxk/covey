"""Where a topic turn's outcome goes back to at the main thread's top level (app/main_relay.py, docs/main-thread.md
结论回到顶层): which turns came from the main thread (``relay_target``: a request's opened record, a supplement it
gave, or an automatic retry of either; ``area_target``: the latest request that touched a 话题区), the message a relay
quotes (``relay_quote``, ``main_job_owner_message``), its "你问" block (``ask_for``) and the card its T number opens
(``turn_card``). Reads records only: nothing here sends or writes."""
from __future__ import annotations

from pathlib import Path

from covey.app import answer_cards
from covey.cards.relay_card import WAITING
from covey.core import main_opened, main_thread, placement
from covey.core.jobs import find_job_record, job_field, job_key
from covey.core.store import directory_store
from covey.core.topics import topic_label

PREFIX = "main-"  # A main-thread tool's synthetic message id (app/main_topics.py).
RETRY_HOPS = 5


def _opened(state_dir, message_id, job_id, topic_id):
    """(token, kind, the main-thread/opened record) the job came from, or None. A job topic_send supplemented
    more than once: the latest owner message."""
    if message_id.startswith(PREFIX):
        token = message_id[len(PREFIX):]
        record = main_opened.by_token(state_dir, token)
        if record:
            return token, main_opened.kind_of(record), record
    found = main_opened.steered(state_dir, job_id, topic_id)
    return None if found is None else (found[0], "steer", found[1])


def _origin_job(state_dir, job):
    """The job's (message_id, job_id): its own, or the one its automatic retry chain started from."""
    message_id, job_id = str(job_field(job, "message_id")), job_field(job, "job_id")
    for _ in range(RETRY_HOPS):
        record = find_job_record(Path(state_dir), job_key(message_id, job_id)) or {}
        previous = find_job_record(Path(state_dir), str(record.get("retry_of") or "")) if record.get("retry_of") else None
        if not previous or previous.get("auto_retry_state") != "queued":
            break
        message_id, job_id = str(previous.get("message_id") or ""), previous.get("job_id") or ""
    return message_id, job_id


def main_job_owner_message(state_dir, main_job_key) -> str:
    """The owner's top-level message the main-thread job ``main_job_key`` answers (the one its card quotes); ''
    when its record is gone or names no Feishu message."""
    main_job = find_job_record(Path(state_dir), str(main_job_key or "")) or {}
    reply_to = str(main_job.get("reply_to_message_id") or "")
    if not reply_to.startswith("om_"):
        reply_to = str(main_job.get("message_id") or "")
    return reply_to if reply_to.startswith("om_") else ""


def relay_quote(state_dir, main_job_key) -> str:
    """What a relay of the main-thread job ``main_job_key``'s hand-on quotes: its card at the top level (``relay``),
    else the owner's message it answers; '' when neither is known."""
    main_job = find_job_record(Path(state_dir), str(main_job_key or "")) or {}
    card = str(main_job.get("card_message_id") or "")
    return card if card.startswith("om_") else main_job_owner_message(state_dir, main_job_key)


def areas_of(state_dir, main_job_key) -> set:
    """The topics the main-thread job ``main_job_key`` opened, forked or handed words to (main-thread/opened/), done."""
    found = set()
    for _, record in main_opened.by_job(state_dir, main_job_key) if main_job_key else ():
        topic = main_opened.topic_of(record)
        if topic and main_opened.answered(record):
            found.add(str(topic))
    return found


def relays(state_dir, topic_id, chat) -> bool:
    """Whether the turns of ``topic_id`` relay at all: its row of core/placement.py has a conclusion. A topic's does;
    none with the main thread off."""
    return placement.plan(state_dir, main_thread.traits(topic_id, chat)).row.conclusion != placement.NONE


def area_target(state_dir, topic_id, chat=""):
    """Where a turn of the 话题区 ``topic_id`` relays when no request of the main thread started it: the latest one
    (main-thread/opened/) that opened, forked or handed on to it, as ``relay_target`` gives it, kind "follow"; None
    for a topic no request touched."""
    if not topic_id or not relays(state_dir, topic_id, chat):
        return None
    best = main_opened.latest_for_topic(state_dir, topic_id)
    if best is None:
        return None
    reply_to = relay_quote(state_dir, best[1].get("job_key"))
    if not reply_to:
        return None
    return {"chat_id": str(best[1].get("chat_id") or chat), "reply_to": reply_to,
            "label": topic_label(Path(state_dir), topic_id) or "话题", "token": best[0], "kind": "follow",
            "topic_id": topic_id, "main_key": str(best[1].get("job_key") or "")}


def ask_for(state_dir, job, target, profile=None, *, at="", log=None):
    """The "你问"/"你答" block of the topic turn ``job`` its relay or question copy carries (app/answer_cards.py
    asked): the words of the owner's message the main thread took it from (its job record, target ``main_key``), or,
    for a turn the owner started in the topic (kind "follow"), the turn's own words; None when it cannot be told.
    Never raises. Whatever the kind, the block opens the topic's thread (target ``topic_id``; the owner, 2026-11-10:
    都能跳转到话题上), at ``at`` (the relay's turn card or question card, ``turn_card``) or the turn's own card when
    they are in it: a second topic one message opened has its own root, the topic card (S6), so its own thread."""
    try:
        record = None
        if target.get("kind") != "follow" and target.get("main_key"):
            record = find_job_record(Path(state_dir), str(target["main_key"]))
        from covey.app.answer_cards import turn_messages

        return answer_cards.asked(state_dir, record or job, profile=profile, log=log,
                                  topic_id=str(target.get("topic_id") or job_field(job, "topic_id") or ""),
                                  at=[m for m in (at, *turn_messages(job)) if m])
    except Exception:  # noqa: BLE001 - the relay goes without it
        return None


def relay_target(state_dir, job):
    """Where a topic turn the main thread started or handed on relays to: {chat_id, reply_to (the main-thread card,
    ``relay_quote``), label (its T number), token (the request's), kind (start, send, steer, fork), topic_id, main_key
    (the main-thread job's key)}; None for any other turn, the main thread's own, or one whose main-thread job is not
    known."""
    topic_id, chat = str(job_field(job, "topic_id")), str(job_field(job, "chat_id"))
    if not relays(state_dir, topic_id, chat):
        return None
    message_id, job_id = _origin_job(state_dir, job)
    found = _opened(state_dir, message_id, job_id, topic_id)
    if found is None:
        return area_target(state_dir, topic_id, chat)
    token, kind, record = found
    reply_to = relay_quote(state_dir, record.get("job_key"))
    if not reply_to:
        return None
    label = topic_label(Path(state_dir), topic_id) or "话题"
    return {"chat_id": str(record.get("chat_id") or chat), "reply_to": reply_to, "label": label, "token": token,
            "kind": kind, "topic_id": topic_id, "main_key": str(record.get("job_key") or "")}


def turn_card(state_dir, job, outcome) -> str:
    """The card of ``job``'s topic that its relay's T number opens: the question card a waiting turn asks on (its
    newest question record's question_message_id), else the turn's task card."""
    if outcome == WAITING:
        rows = directory_store(Path(state_dir) / "questions").query(job_id=job_field(job, "job_id"),
                                                                    topic_id=job_field(job, "topic_id"))
        for _, row in sorted(rows, key=lambda pair: pair[1].get("created_at") or 0, reverse=True):
            if str(row.get("question_message_id") or "").startswith("om_"):
                return str(row["question_message_id"])
    return str(job_field(job, "card_message_id") or "")
