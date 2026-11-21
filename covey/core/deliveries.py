"""Supplements sent into a running task: one delivery record each.

``interaction-deliveries/<token>.json`` keeps one supplement's progress:
submitting (saved, being sent) → sent (the backend took it) → read (the model
took it in; Claude reports this by replaying the input). ``held`` is saved for a
task still starting; ``withdrawn`` was taken back before that. ``refused`` means
nothing was sent and the same content may be submitted again; ``uncertain``
means it may have arrived, so it is never sent again automatically. A ``sent``
record whose task ended unread, on a harness that reports reads, stays ``sent``
and gains ``target_ended`` (``end_unread``); ``reads`` on a record says its task
reports reads, so a 检查送达状态 click on one still "sent" after its task ended
marks it the same way (``app/steering.py``). A butler supplement (no receipt) that its turn
certainly never took, refused or ended unread, gains ``requeue_auto``: the Bridge queues it as a
turn of its own once (``lost``, app/steering.py ``queue_lost``; at the Bridge's start, those whose turn ended
while no Bridge watched it, ``queue_stranded``), through the same ``requeue_state``
and ``requeued_job_id`` as a 重新排队执行 click. A topic's message that went into its task on arrival
(COVEY_TOPIC_STEER, app/steering.py steer_on_arrival; ``on_arrival``: its receipt, the queued card's, says ``auto``)
gains ``requeue_next`` the same way (``marked``) and is queued as 重新排队 queues it. Every change
after the record is created goes through ``update_delivery``. The receipt card
that shows a record is rendered by ``cards/steer_receipts.py``, with the buttons ``offers`` names.
"""
from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path

from covey.core.locks import resource_lock
from covey.core.process_env import env_flag
from covey.core.run_dir import ACKS, EXIT, INBOX, OP
from covey.core.state import keyed_json_path, read_json, write_json

# A topic's message sent while the topic's task runs goes into that task as a supplement (app/submission.py
# steers_on_arrival), unless 0: then it is queued as the next round, as before (docs/configuration.md).
TOPIC_STEER = "COVEY_TOPIC_STEER"


def topic_steer() -> bool:
    """Whether a topic's message goes into the task running there (TOPIC_STEER, on unless 0)."""
    return env_flag(TOPIC_STEER, True)


def delivery_token(topic_id, mid):
    return hashlib.sha256(f"{topic_id}-{mid}".encode()).hexdigest()


def delivery_path(state_dir, topic_id, mid):
    return keyed_json_path(state_dir, "interaction-deliveries", delivery_token(topic_id, mid))


def read_delivery(path):
    try:
        return read_json(Path(path))
    except (OSError, ValueError):
        return None


def save_delivery(path, record):
    write_json(Path(path), record, ensure_ascii=False)


# Where each status may go next. Senders race (a task runner, Claude's replay, a late
# acknowledgement), so a change the record has already moved past is dropped, never applied:
# "held" never replaces "sent", "sent" never replaces "read". read, refused and withdrawn are
# final; a refused supplement submitted again starts a new record.
NEXT = {
    "submitting": {"held", "sent", "read", "refused", "uncertain"},
    "held": {"sent", "read", "refused", "uncertain", "withdrawn"},
    "uncertain": {"sent", "read", "refused", "uncertain"},
    "sent": {"read"},
}


def update_delivery(path, **changes):
    """Change a saved record under its lock; returns it, or None when it is missing or has moved past
    the requested status. Reaching sent or read stamps sent_at once."""
    with resource_lock("question", path):
        record = read_delivery(path)
        status = changes.get("status")
        if record is None or (status and status not in NEXT.get(record.get("status"), ())):
            return None
        record.update(changes)
        if status in {"sent", "read"}:
            record["sent_at"] = record.get("sent_at") or time.time()
        if status == "read":
            record.pop("target_ended", None)  # A late read wins over the task's end (end_unread).
        save_delivery(path, record)
        return record


# How far a record's mtime may lag the turn's start (the state directory can be on a NAS with its own clock).
CLOCK_SLACK = 600


def end_unread(state_dir, job_key, since=0.0):
    """The task ``job_key`` ended: its supplements still "sent" were never read. Each keeps its status (sent
    only becomes read) and gains ``target_ended``, which offers 重新排队执行; returns those records.
    A record turns "sent" only while the task runs: files last written before ``since`` (the turn's start,
    less CLOCK_SLACK) are skipped unread, so the scan does not read every topic's supplements."""
    def unread(record):  # A merged one is in the task's request: it was read with it.
        return (isinstance(record, dict) and record.get("job_key") == job_key and record.get("status") == "sent"
                and not record.get("merged") and not record.get("target_ended"))
    ended = []
    since = float(since or 0) - CLOCK_SLACK
    for path in sorted((Path(state_dir) / "interaction-deliveries").glob("*.json")):
        try:
            if path.stat().st_mtime < since:
                continue
        except OSError:
            continue
        if not unread(read_delivery(path)):
            continue  # Checked again under the lock: only this task's records are locked.
        with resource_lock("question", path):
            record = read_delivery(path)
            if not unread(record):
                continue
            record.update(target_ended=True, ended_at=time.time(),
                          error=f"任务 #{record.get('job_id')} 在模型读到这条补充之前就结束了")
            save_delivery(path, record)
        ended.append(record)
    return ended


def marked(record) -> bool:
    """Whether ``record`` waits to be queued as a turn of its own: a butler supplement's ``requeue_auto``, or a topic's
    message's that went in as it arrived, ``requeue_next`` (``on_arrival``; its own name, so a release before it, which
    reads every ``requeue_auto`` as the butler's, leaves it to its receipt's 重新排队)."""
    return bool(record.get("requeue_next") if on_arrival(record) else record.get("requeue_auto"))


def lost(state_dir, job_key=None, since=0.0):
    """The butler supplements of the task ``job_key`` (None: of every task, a Bridge's start) its turn never took
    and that wait to be queued as turns of their own (``marked``, not tried yet: ``offers`` still has REQUEUE),
    oldest first, as (path, record); a topic's messages that went in as they arrived too. Files last written before
    ``since`` (less CLOCK_SLACK) are skipped unread, as in ``end_unread``."""
    found = []
    since = float(since or 0) - CLOCK_SLACK
    for path in (Path(state_dir) / "interaction-deliveries").glob("*.json"):
        try:
            if path.stat().st_mtime < since:
                continue
        except OSError:
            continue
        record = read_delivery(path)
        if (isinstance(record, dict) and (job_key is None or record.get("job_key") == job_key)
                and marked(record) and REQUEUE in offers(record)):
            found.append((path, record))
    return sorted(found, key=lambda item: float(item[1].get("saved_at") or 0))


def _late_ack(record):
    """A task runner's acknowledgement that arrived after the bridge stopped waiting."""
    op, run_dir = record.get("op"), record.get("run_dir")
    if not op or not run_dir or not OP.fullmatch(str(op)):
        return None
    try:
        return read_json(Path(run_dir) / ACKS / (op + ".json"))
    except (OSError, ValueError):
        pass
    directory = Path(run_dir)
    if (directory / EXIT).exists() and (directory / INBOX / (op + ".json")).exists():
        return {"ok": False, "refused": True, "error": "任务已结束，补充没有发出"}
    return None


def reconcile(path, record):
    """Settle an unconfirmed delivery from what the runner left on disk; returns the record."""
    if record.get("status") not in {"submitting", "uncertain"}:
        return record
    ack = _late_ack(record)
    if ack is None:
        return record
    if ack.get("ok"):
        change = {"status": "sent", "error": ""}
    elif ack.get("refused"):
        change = {"status": "refused", "error": ack.get("error") or "补充没有发出"}
    else:
        change = {"status": "uncertain", "error": ack.get("error") or record.get("error") or ""}
    return update_delivery(path, **change) or read_delivery(path) or record


# What a receipt offers: its buttons (cards/steer_receipts.py), and the 重新排队执行 clicks app/steering.py takes.
WITHDRAW, CHECK, REQUEUE, INTERRUPT = "withdraw", "check", "requeue", "interrupt"


def on_arrival(record) -> bool:
    """Whether ``record`` is of a topic's message that went into the task before it as it arrived (COVEY_TOPIC_STEER,
    app/steering.py steer_on_arrival), not by a click: its receipt, the message's queued card, is marked ``auto``."""
    receipt = record.get("receipt") if isinstance(record, dict) else None
    return isinstance(receipt, dict) and receipt.get("kind") == "job" and receipt.get("auto") is True


def unread(record) -> bool:
    """In a running task that reports reads (``reads``), sent and not read yet: not in its request, its task not ended."""
    return (record.get("status") == "sent" and bool(record.get("reads")) and not record.get("merged")
            and not record.get("target_ended"))


def offers(record):
    """What the record's receipt offers now. WITHDRAW while held. CHECK while unconfirmed, and while
    "sent" and unread on a harness that reports reads: a check after its task ended elsewhere (a task
    runner lost, a Harness retired, a restart) marks it unread, so it can be queued again. REQUEUE once
    refused, withdrawn or its task ended, when it can be queued and no requeue was tried. INTERRUPT (打断它，马上读) while
    ``unread`` in a task whose command can be stopped (``stops``), until a stop was tried (``interrupt_at``: one that
    found nothing to stop clears it, app/steer_interrupt.py)."""
    status = record.get("status", "submitting")
    offered = set()
    if status == "held":
        offered.add(WITHDRAW)
    if status in {"submitting", "uncertain"} or unread(record):
        offered.add(CHECK)
    if unread(record) and record.get("stops") and not record.get("interrupt_at"):
        offered.add(INTERRUPT)
    if (record.get("requeue") and not record.get("requeued_job_id") and not record.get("requeue_state")
            and (status in {"refused", "withdrawn"} or record.get("target_ended"))):
        offered.add(REQUEUE)
    return frozenset(offered)


def shown_text(record):
    """What the user wrote. A queued message is sent with its topic background, the butler's hand-on with the
    Bridge's note (``shown``, app/butler_handoff.py); cards show only this."""
    return (record.get("receipt") or {}).get("text") or record.get("shown") or record.get("text") or ""


# The context a message that replies to another is sent to the model with (feishu/forward.py): a preview leaves it out
# (else 最新补充 shows "回复上下文： - parent_id: `om_…`").
REPLY_CONTEXT = re.compile(r"\n[ \t]*(?:回复上下文|话题起始消息（背景参考）)[：:]")


def shown_preview(record_or_text) -> str:
    """``shown_text`` of a delivery record (or a text) without the reply context after it: what a card's one-line
    preview of a supplement shows."""
    text = shown_text(record_or_text) if isinstance(record_or_text, dict) else str(record_or_text or "")
    return REPLY_CONTEXT.split(text, 1)[0]
