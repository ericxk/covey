"""Sending a card as a message of its own and keeping it in a record: the part the 专项卡 (app/project_cards.py) and the 派发卡
(app/dispatch_cards.py) share, and the small helpers both draw their cards with.

- ``publish`` is the send: the card goes out through ``Messenger.create_and_send_card`` with ``on_created`` keeping the card id
  first (a second try sends that same card), then ``record`` stores the ids and the digest, the message is remembered and
  routed to the topic that answers a click or a quote of it, and the log line says so. It takes no record lock itself (rule 1:
  the callers hold none while it sends) and writes the card no more (rule 3: the CardKit sequence starts at the callers').
- ``safely`` runs a sending that must never raise: the card is the one thing missing then, the log says which.
"""
from __future__ import annotations

import hashlib
import json
import time

from covey.core.topics import remember_topic_message, route_message


def digest_of(card) -> str:
    return hashlib.sha256(json.dumps(card, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]


def words(text, limit=200) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def clock(epoch) -> str:
    return time.strftime("%H:%M", time.localtime(float(epoch))) if epoch else ""


def titles_of(topics) -> dict:
    """Harness name -> its title (the card's 输入 line), from the Harness apps."""
    try:
        return {name: app.harness.spec.title for name, app in topics.apps.items()}
    except AttributeError:
        return {}


def safely(env, send, failed: str) -> str:
    """``send()``'s message id; '' after logging ``failed`` and the exception's name when it raises."""
    try:
        return send()
    except Exception as exc:  # noqa: BLE001 - the work is open; only its card is missing
        env.runtime.log(f"{failed}: {type(exc).__name__}")
        return ""


def publish(env, state, chat, card, key, *, card_id, on_created, record, topic, route_chat, not_sent, sent) -> str:
    """Send ``card`` to ``chat`` (not a reply: Feishu would put it into a thread) under the idempotency ``key``, resuming
    ``card_id`` when one was created before. ``record(card_id, message, digest)`` stores the sent card; ``topic`` and
    ``route_chat`` are where a click on it and a quote of it go; ``not_sent`` is the log line when nothing went out, ``sent``
    the start of the one when it did (the message's tail follows). The message id, '' when it did not go out."""
    card_id, message = env.messenger.create_and_send_card(
        env.profile, chat, card, key, voice=env.voice, card_id=card_id, on_created=on_created)
    if not message:
        env.runtime.log(not_sent)
        return ""
    record(card_id, message, digest_of(card))
    remember_topic_message(state, topic, route_chat, message)
    route_message(state, route_chat, message, topic, holder=topic)
    env.runtime.log(f"{sent}{message[-8:]}")
    return message
