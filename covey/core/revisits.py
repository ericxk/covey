"""到点回来看 (COVEY_REVISIT=1, docs/usage.md 到点回来看): a topic ends its turn with ``revisit`` in its checkpoint (when, and
what to look at) and the Bridge queues one turn of that topic at that time, instead of the turn polling for minutes.

The record, one per topic, ``revisits/<topic_id>.json`` (only the Bridge writes it, under the ``revisit`` lock of its
topic; no Feishu call under it): ``topic_id``, ``chat_id``, ``state`` (PENDING until its time, FIRED once its turn is
queued), ``due_at``, ``minutes``, ``look`` (what the turn is to look at), ``set_at`` and ``set_by`` (the job key of the
turn that asked for it), ``round`` (1 for one an owner's turn asked for, one more for each revisit turn that asked for
the next), ``tries`` and, once fired, ``message`` (the revisit turn's message id, ``message_id``) and ``fired_at``. A
newer request replaces the older one; the owner's 取消 on the 看板 removes it. Off, nothing reads or writes the folder.
Standard library only.
"""
from __future__ import annotations

import time
from pathlib import Path

from covey.core import env as cenv
from covey.core.locks import resource_lock
from covey.core.state import read_object, write_json

SWITCH = "COVEY_REVISIT"
FOLDER = "revisits"
PREFIX = "revisit-"  # A revisit turn's message id: ``revisit-<topic id head>-<set_at>-<try>`` (``message_id``).
PENDING, FIRED = "pending", "fired"
MIN_MINUTES, MAX_MINUTES = 3, 7 * 24 * 60
MAX_ROUND = 24  # Revisit turns in a row that may ask for the next one: past it the watch ends and its reply goes out.
LOOK_CHARS = 200


def enabled() -> bool:
    """Whether COVEY_REVISIT=1 (only ``1``; read on every call)."""
    return str(cenv.get(SWITCH, "")).strip() == "1"


def path(state_dir, topic_id) -> Path:
    return Path(state_dir) / FOLDER / f"{topic_id}.json"


def message_id(record) -> str:
    """The message id of the turn ``record`` queues now (a lost one is queued again under the next ``tries``)."""
    return f"{PREFIX}{str(record['topic_id'])[:12]}-{int(record['set_at'])}-{int(record.get('tries') or 0)}"


def is_revisit(message) -> bool:
    """Whether a job's message id is a revisit turn's: the Bridge queued it, nobody spoke."""
    return isinstance(message, str) and message.startswith(PREFIX)


def wanted(checkpoint) -> dict | None:
    """The checkpoint's ``revisit`` as {minutes, look}: minutes a number (or its digits), kept within MIN_MINUTES and
    MAX_MINUTES; look its words on one line, at most LOOK_CHARS. None without one, or with no number of minutes."""
    raw = checkpoint.get("revisit") if isinstance(checkpoint, dict) else None
    if not isinstance(raw, dict) or isinstance(raw.get("minutes"), bool):
        return None
    try:
        minutes = float(raw.get("minutes"))
    except (TypeError, ValueError):
        return None
    if minutes != minutes:  # NaN
        return None
    look = " ".join(str(raw.get("look") or "").split())[:LOOK_CHARS]
    return {"minutes": min(max(minutes, MIN_MINUTES), MAX_MINUTES), "look": look}


def unchanged(checkpoint) -> bool:
    """Whether a revisit turn said it found nothing new (``"unchanged": true``)."""
    return isinstance(checkpoint, dict) and checkpoint.get("unchanged") is True


def read(state_dir, topic_id) -> dict:
    record = read_object(path(state_dir, topic_id))
    return record if record.get("topic_id") == topic_id else {}


def records(state_dir) -> list:
    """Every topic's record, the soonest due first."""
    folder = Path(state_dir) / FOLDER
    try:
        names = sorted(p.stem for p in folder.glob("*.json"))
    except OSError:
        return []
    found = [record for record in (read(state_dir, name) for name in names) if record]
    return sorted(found, key=lambda record: float(record.get("due_at") or 0))


def change(state_dir, topic_id, edit):
    """``edit(record)`` under the topic's lock: the record it returns is written (None removes it). What was written."""
    target = path(state_dir, topic_id)
    with resource_lock("revisit", topic_id):
        current = read(state_dir, topic_id)
        new = edit(dict(current))
        if new is None:
            try:
                target.unlink()
            except FileNotFoundError:
                pass
            return None
        target.parent.mkdir(parents=True, exist_ok=True)
        write_json(target, {"schema_version": 1, **new}, ensure_ascii=False)
        return new


def armed(topic_id, chat_id, want, *, by, round_, now=None) -> dict:
    """The record of a revisit of ``topic_id`` ``want['minutes']`` from ``now``, asked for by the turn ``by``."""
    now = time.time() if now is None else now
    return {"topic_id": topic_id, "chat_id": chat_id, "state": PENDING, "due_at": now + want["minutes"] * 60,
            "minutes": want["minutes"], "look": want["look"], "set_at": now, "set_by": by, "round": int(round_),
            "tries": 0}


def cancel(state_dir, topic_id, set_at) -> bool:
    """The owner's 取消: the revisit of ``topic_id`` asked for at ``set_at`` removed (a newer one stays); whether it was."""
    gone = []

    def edit(record):
        if record and abs(float(record.get("set_at") or 0) - float(set_at)) < 0.001:
            gone.append(True)
            return None
        return record or None
    try:
        change(state_dir, topic_id, edit)
    except (TypeError, ValueError):
        return False
    return bool(gone)
