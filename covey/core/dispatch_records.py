"""The records of the 派发活卡片 (docs/main-thread.md 派发活卡片): <state>/main-thread/dispatch/<token>.json, one per
card the main thread's topic_start, topic_send or topic_fork put at the chat's top level (app/dispatch_cards.py), and
the sidecar <token>.relay.json a task runner leaves when that card's conclusion went out (``mark_relayed``).

The record is written by the Bridge alone: first by the tool that sends the card, then by the refresh thread. The task
runner never writes it (rule 3: one writer for a card); what the runner has to say, the relay's end, goes in the
sidecar, one small file written once, which the refresh thread reads.

``state`` only goes live -> settled (``DISPATCH_TRANSITIONS``, rule 4); a settled card is never refreshed again.
``rounds``, ``upgrade_hint`` and ``project_id`` are for a later phase (a long job becoming a project): written now,
read by nothing yet. Standard library and core only (a task runner imports it)."""
from __future__ import annotations

import re
import time
from pathlib import Path

from covey.core.state import read_object, write_json

FOLDER = Path("main-thread") / "dispatch"
LIVE, SETTLED = "live", "settled"
# Rule 4: the only way a record's state moves.
DISPATCH_TRANSITIONS = {LIVE: {SETTLED}, SETTLED: set()}
SCHEMA = 1
LONG_WORDS = re.compile("长活|长期|接着做")  # What the owner says of a job that goes on: upgrade_hint.long_word.
RELAY_SUFFIX = ".relay.json"
REQUEST_CHARS = 200  # Of the owner's request kept in the record; the card's header shows the first 30.


def folder(state_dir) -> Path:
    return Path(state_dir) / FOLDER


def record_path(state_dir, token) -> Path:
    return folder(state_dir) / f"{token}.json"


def relay_note_path(state_dir, token) -> Path:
    return folder(state_dir) / f"{token}{RELAY_SUFFIX}"


def new_record(*, token, chat_id, topic_id, job_key, mid, kind, owner_message, request, starts, rounds=1, now=None) -> dict:
    """A fresh live record. ``job_key`` the main-thread job that sent the card, ``mid`` the key or message id of the
    topic's job it follows, ``kind`` start, send or fork, ``starts`` whether the topic starts at once (else it waits
    on its launch card)."""
    request = " ".join(str(request or "").split())
    return {"schema_version": SCHEMA, "token": token, "chat_id": chat_id, "topic_id": topic_id, "job_key": job_key,
            "mid": mid, "kind": kind, "owner_message": owner_message, "request": request[:REQUEST_CHARS],
            "starts": bool(starts), "card_id": "", "card_message_id": "", "sequence": 0, "state": LIVE,
            "started_at": time.time() if now is None else now,
            # For the phase after this one; nothing reads them yet.
            "rounds": rounds, "upgrade_hint": {"long_word": bool(LONG_WORDS.search(request))}, "project_id": None}


def transition(record, to) -> dict:
    """``record`` moved to state ``to`` when ``DISPATCH_TRANSITIONS`` allows it; ValueError when it does not."""
    state = record.get("state") or LIVE
    if to not in DISPATCH_TRANSITIONS.get(state, ()):
        raise ValueError(f"dispatch card state {state} -> {to} is not allowed")
    record["state"] = to
    return record


def live_paths(state_dir) -> list[Path]:
    """The records' files, those a refresh may still look at (a settled one is told apart by reading it)."""
    try:
        return sorted(path for path in folder(state_dir).glob("*.json") if not path.name.endswith(RELAY_SUFFIX))
    except OSError:
        return []


def rounds_for(state_dir, owner_message, topic_id) -> int:
    """How many times the same question of the owner dispatched to ``topic_id``, this one counted."""
    count = 0
    for path in live_paths(state_dir):
        saved = read_object(path)
        if saved.get("owner_message") == owner_message and saved.get("topic_id") == topic_id:
            count += 1
    return count + 1


def mark_relayed(state_dir, token, *, outcome, state, message_id="", reason="") -> bool:
    """The relay of the topic's turn that ``token`` dispatched has been sent (``state`` "sent", ``message_id`` the
    conclusion) or skipped ("skipped": the owner stopped it himself): the sidecar the refresh thread reads. Only when
    the card has a record, once (a second call keeps the first). Whether it wrote."""
    if not token or "/" in str(token):
        return False
    if not record_path(state_dir, token).exists():
        return False
    path = relay_note_path(state_dir, token)
    if path.exists():
        return False
    write_json(path, {"token": token, "outcome": outcome, "state": state, "message_id": message_id,
                      "reason": str(reason or "")[:200], "at": time.time()}, ensure_ascii=False)
    return True


def relay_note(state_dir, token) -> dict:
    return read_object(relay_note_path(state_dir, token))
