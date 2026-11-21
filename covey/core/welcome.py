"""The welcome card's record and a new deployment's first answer, as the Bridge (app/welcome.py) and ./install
(service/installer.py) read them: <state>/welcome.json.

- ``settle``, at the Bridge's start before any event, writes the record once: PENDING for a new deployment, SKIPPED for
  one with ``history`` (a job, a topic, or the owner's private chat saved), so a deployment used before this release
  sees no change. From then on the record alone decides: a restart, a reinstall or an upgrade never sends the card
  again. A send that was going out when the Bridge stopped (SENDING) may have landed: it becomes UNCERTAIN (rule 1).
- The states: PENDING (to be sent), SENDING (the intent, written just before the message goes out), SENT, UNCERTAIN
  (never sent again), SKIPPED.
- ``first_answer``: how far the owner's private chat has got, from its job records, for ./install's wait
  (docs/install.md 第一个回答).

Standard library and core only (./install reads it).
"""
from __future__ import annotations

from pathlib import Path

from covey.core.locks import resource_lock
from covey.core.state import read_object, utc_now_iso, write_json
from covey.core.statuses import ENDED

RECORD = "welcome.json"
PENDING, SENDING, SENT, UNCERTAIN, SKIPPED = "pending", "sending", "sent", "uncertain", "skipped"
OWNER_CHAT = "owner-chat.json"  # app/access.py OWNER_CHAT: the owner's private chat, once he wrote or the bot opened it.
# first_answer's stages, in the order a first message goes through them.
NONE, RECEIVED, LAUNCH, STOPPED, ANSWERED = "none", "received", "launch", "stopped", "answered"
REPLIED = frozenset({"done", "waiting_user", "continued"})  # A turn that ended with its reply (or its question) sent.


def path(state_dir) -> Path:
    return Path(state_dir) / RECORD


def lock(state_dir):
    """The record's lock: held only to read and write it, never across a Feishu call (rule 1)."""
    return resource_lock("welcome", str(path(state_dir)))


def read(state_dir) -> dict:
    """The record; {} before ``settle`` wrote one (or when it cannot be read)."""
    return read_object(path(state_dir))


def update(state_dir, change) -> dict:
    """The record after ``change(record)``, which returns the new record, or None to leave it as it is."""
    with lock(state_dir):
        record = read(state_dir)
        changed = change(record)
        if changed is None:
            return record
        write_json(path(state_dir), changed, ensure_ascii=False)
        return changed


def history(state_dir) -> bool:
    """Whether the deployment has been used: a job or a topic (core/butler_settings.py settle_mode asks the same), or
    the owner's private chat saved (he wrote to the bot, or its menu opened the chat)."""
    state = Path(state_dir)
    jobs, topics = state / "jobs", state / "topics"
    return ((jobs.is_dir() and any(jobs.glob("*.json")))
            or (topics.is_dir() and any(p.name != "aliases" and p.is_dir() for p in topics.iterdir()))
            or (state / OWNER_CHAT).exists())


def settle(state_dir) -> str:
    """The record written once (the module's docstring); the state it holds now."""
    with lock(state_dir):
        record = read(state_dir)
        state = record.get("state")
        if state == SENDING:
            state = UNCERTAIN
            write_json(path(state_dir), {**record, "state": state, "uncertain_at": utc_now_iso()}, ensure_ascii=False)
        if state:
            return state
        state = SKIPPED if history(state_dir) else PENDING
        write_json(path(state_dir), {"state": state, "decided_at": utc_now_iso(),
                                     **({"why": "history"} if state == SKIPPED else {})}, ensure_ascii=False)
        return state


def first_answer(state_dir) -> str:
    """How far the owner's private chat has got: ANSWERED once a turn of it sent its reply (a question card counts: the
    bot did answer); else by its latest task, LAUNCH while its topic waits on the launch card, STOPPED when it ended
    without a reply, RECEIVED while it is queued or runs; NONE before any task of the chat."""
    from covey.core.store import directory_store

    state = Path(state_dir)
    chat = read_object(state / OWNER_CHAT).get("chat_id")
    if not isinstance(chat, str) or not chat or not (state / "jobs").is_dir():
        return NONE
    records = [record for _, record in directory_store(state / "jobs").query(chat_id=chat)]
    if any(r.get("delivery_ok") is True and r.get("status") in REPLIED for r in records):
        return ANSWERED
    if not records:
        return NONE

    def number(record):
        try:
            return int(record.get("job_id") or 0)
        except (TypeError, ValueError):
            return 0
    status = max(records, key=number).get("status")
    return LAUNCH if status == "waiting_workspace" else STOPPED if status in ENDED else RECEIVED
