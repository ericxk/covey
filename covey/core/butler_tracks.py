"""多路并行 (docs/butler.md 同时做几件事; off by default): the butler's family of topics, one per
track (路). Slot 0 is the butler's own topic (core/butler.py butler_topic_id); slots 1 to SLOTS are topics of the
same kind, each one a fork of the session of slot 0 that runs a turn of its own at the same time as the others (the
scheduler keys on the topic, so a topic's turns queue in order and two topics' turns run together).

What is here: the two switches (``enabled``, ``limit``), a slot's topic id (``track_topic_id``), and which slot a topic
is (``track_of``); ``family_ids`` lists every topic id the switch lets count as the butler's. The predicates
that use them live in core/butler.py (``is_butler_topic`` the family, ``is_butler_root`` slot 0 only). With the switch
off the family is slot 0 alone, as before this module existed.

Standard library and core/env only: the MCP server imports core/butler.py, which imports this.
"""
from __future__ import annotations

import functools
import hashlib

from covey.core import env as cenv

ENV = "COVEY_BUTLER_TRACKS"  # ``1``: on; anything else, unset too: off.
MAX_ENV = "COVEY_BUTLER_TRACKS_MAX"  # How many tracks may run at once, 2 to 4: see ``limit``.
SLOTS = 4  # Slots 1..SLOTS besides slot 0: the most tracks a deployment can have.
DEFAULT_LIMIT, MIN_LIMIT, MAX_LIMIT = 3, 2, 4


def enabled() -> bool:
    """Whether COVEY_BUTLER_TRACKS=1 (only ``1``; read on every call). Whether the butler itself is on is
    core/butler.py ``enabled``: with it off a slot is still no topic, and nothing starts a track."""
    return str(cenv.get(ENV, "")).strip() == "1"


def limit() -> int:
    """COVEY_BUTLER_TRACKS_MAX: how many tracks may be live at once, DEFAULT_LIMIT unset or unreadable, otherwise held to
    MIN_LIMIT..MAX_LIMIT (1 would be no parallel at all, more than the slots has nowhere to go)."""
    try:
        value = int(str(cenv.get(MAX_ENV, "")).strip())
    except ValueError:
        return DEFAULT_LIMIT
    return max(MIN_LIMIT, min(MAX_LIMIT, value))


def root_topic_id(chat_id) -> str:
    """Slot 0's topic id (what core/butler.py butler_topic_id answers: the same digest, kept in step by a test)."""
    return hashlib.sha256(f"main-thread\0{chat_id}".encode()).hexdigest()[:32]


@functools.lru_cache(maxsize=256)
def _slots(chat_id: str) -> tuple:
    """The topic ids of slots 0..SLOTS in ``chat_id``: hex like every topic id (app/topics.py Topics.target takes
    [0-9a-f]{16,128} only), and no message's alias, so no event routes to one. The digest's input names the slot, so it
    cannot meet slot 0's (a different prefix) or another slot's."""
    return (root_topic_id(chat_id), *(
        hashlib.sha256(f"main-thread-track\0{slot}\0{chat_id}".encode()).hexdigest()[:32]
        for slot in range(1, SLOTS + 1)))


def track_topic_id(chat_id, slot: int) -> str:
    """The topic id of slot ``slot`` (0..SLOTS; 0 is the butler's own) in the private chat ``chat_id``. Stable:
    the same chat and slot always give the same id."""
    if type(slot) is not int or not 0 <= slot <= SLOTS:
        raise ValueError(f"track slot {slot!r} is not 0..{SLOTS}")
    return _slots(str(chat_id or ""))[slot]


def family_ids(chat_id) -> tuple:
    """The topic ids that count as the butler's in ``chat_id`` now: slots 0..SLOTS with the switch on, slot 0 alone
    with it off."""
    ids = _slots(str(chat_id or ""))
    return ids if enabled() else ids[:1]


def track_of(topic_id, chat_id):
    """The slot of ``topic_id`` in ``chat_id``, 0..SLOTS, or None when it is none of the family's (with the switch off
    only slot 0 is). Slot 0 is the butler's own topic."""
    if not topic_id or not isinstance(topic_id, str):
        return None
    ids = _slots(str(chat_id or ""))
    if topic_id == ids[0]:
        return 0
    if not enabled():
        return None
    try:
        return ids.index(topic_id)
    except ValueError:
        return None


def owner_stop_drops_reply(topic_id, chat_id, cancel_requested_by) -> bool:
    """Whether the owner's stop (``cancel_requested_by`` set on the job: the card's 停止 or a stop word) of a task of
    the family, with the switch on, also drops the reply a stopped Claude turn kept (backends/claude/runtime.py
    ``keep_last_reply``: stopped while only background tasks ran, the last complete reply is still sent). Everywhere
    else that reply goes out as before: a stop of a task of an ordinary topic, or with the switch off, is unchanged."""
    return bool(cancel_requested_by) and enabled() and track_of(topic_id, chat_id) is not None
