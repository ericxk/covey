"""到点回来看 at the Bridge (COVEY_REVISIT=1, docs/usage.md 到点回来看): a topic that waits on something outside (a
training, CI, a deploy) ends its turn with ``revisit`` in its checkpoint, and the Bridge queues one turn of that topic
when the time comes, instead of a turn that polls for minutes holding a slot. The record is core/revisits.py's. Off: no rule line, no
thread, no record, nothing read.

- Asked for (``after_runner``, the Bridge once a turn's runner is gone, from app/butler_relay.py after_runner; rule 8 keeps
  a task process to its own card and record): a turn that ended done with ``revisit`` ({minutes, look}) in its checkpoint
  sets its topic's record, replacing the one before (one at a time a topic). An ordinary topic's only: not the butler's
  family, not a project's, not a 代答 topic (``REVISIT_RULE`` is in no other turn's message, app/runs.py).
- Queued (the ``revisits`` thread, every TICK seconds): a PENDING record past its ``due_at`` whose topic has nothing
  queued, running or waiting for the owner is marked FIRED with its turn's message id first, then the turn is queued as
  a message of the topic (``enqueue_chat_job``, as app/project_loop.py wakes a lead; ``WAKE`` is its text). One that
  could not be queued is PENDING again RETRY seconds later. A FIRED record whose turn has no job record LOST seconds on
  is queued again under the next try; one whose turn ended without ``after_runner`` seeing it (the Bridge was down) is
  settled by the thread the same way. The records are on disk: a restart goes on where it was.
- The revisit turn ended (``after_runner``): its own ``revisit`` sets the next one (``round`` one more, at most
  core/revisits.py MAX_ROUND in a row), else the record goes; a turn that did not end done ends the watch, unless
  an automatic retry (app/auto_retry.py: a rate limit, a server fault) is scheduled for it: the watch waits for that
  retry and goes on with its end (``lineage``), a retry that is cancelled or runs out ends it as before.
- Quiet (``silent``, app/turn_delivery.py TurnDelivery, in the task runner): a revisit turn that found nothing new
  (``"unchanged": true``) and asks for the next look is not told: its saved reply is kept and counts as delivered (no
  answer card, no relay to the top level), its task card shows its first sentence, and the 看板 keeps the topic as seen
  when its result before was (``keep_seen``). The look that ends the watch (no next ``revisit``, or MAX_ROUND reached)
  always reports.
- Seen and taken back (the 看板, cards/board.py REVISIT_KIND, app/digest.py claim): each record of the chat is a row
  (``board_rows``) with 取消, the owner's only (``cancel``).
"""
from __future__ import annotations

import time
from pathlib import Path

from covey.core import revisits as records
from covey.core.board_read import ended_at, unrun
from covey.core.butler import is_butler_topic
from covey.core.checkpoint import read_checkpoint
from covey.core.history_storage import append_history
from covey.core.jobs import find_job_record, job_field, job_key, select_job_records, topic_job_records
from covey.core.loops import every
from covey.core.projects import topic_project
from covey.core.statuses import ENDED, NOT_ENDED
from covey.core.topic_seen import read_seen, record_reads
from covey.core.topics import label_title, load_topic, remember_topic_message

TICK = 30
START_WAIT = 20  # After the Bridge starts: its own recovery of interrupted turns goes first.
RETRY = 60  # Seconds before a turn that could not be queued is tried again.
LOST = 120  # Seconds a FIRED record's turn may have no job record before it is queued again.
MAX_TRIES = 5  # Turns of one revisit queued (each lost or refused one tried again) before it is given up.
# The turn's message: what it is and what to do, the look the turn before asked for in it.
WAKE = ("[Bridge 叫醒] 到了上一轮约好回来看的时间，要看的是：{look}\n"
        "先看现在的状态。和上次比没有变化：检查点写 \"unchanged\": true，回复一句话。有变化：照常回复。"
        "还要接着等，就再写 revisit。")
# Put after the checkpoint file in an ordinary topic's turns (app/runs.py _revisit_note), only with the switch on.
REVISIT_RULE = ("到点回来看：要等外部的事（训练、CI、部署）出结果时，不要在这一轮里反复查：在检查点写 "
                "revisit：{\"minutes\":30,\"look\":\"回来看什么\"}，回复里说几点回来看，然后结束这一轮，Bridge 到点再叫醒这个话题一次。"
                "被叫醒的那一轮没有变化就写 \"unchanged\": true（不打扰主人）。只有主人明说要定期看时，才在每次看完后再写 revisit。")


def ordinary(state_dir, topic_id, chat_id) -> bool:
    """Whether a topic may ask for a revisit: not the butler's family (its leads too), not a project's, not 代答's."""
    if not topic_id or is_butler_topic(topic_id, chat_id) or topic_project(state_dir, topic_id) is not None:
        return False
    try:
        return not load_topic(Path(state_dir), topic_id).get("proxy")
    except (OSError, ValueError):
        return False


def after_runner(log, state_dir, job) -> None:
    """The Bridge, once ``job``'s run has ended: its topic's revisit set, followed on or ended. Never raises."""
    try:
        if not records.enabled() or not state_dir:
            return
        settle(log, Path(state_dir), find_job_record(Path(state_dir), job_key(
            str(job_field(job, "message_id")), job_field(job, "job_id"))) or {})
    except Exception as exc:  # noqa: BLE001 - the task's end does not depend on it
        log(f"revisit after job #{job_field(job, 'job_id')} failed: {type(exc).__name__}")


HOPS = 8  # Automatic retries followed from a revisit turn at most.


def lineage(state, record):
    """(the message of the revisit turn ``record`` belongs to or its own, the record that settles it, whether it is
    still to come): a revisit turn that failed has an automatic retry scheduled, queued or running (app/auto_retry.py),
    and that retry's end is the turn's; ``record`` may be the turn or one of its retries. Without a revisit turn in its
    lineage, (its own message, ``record``, False)."""
    origin = record
    for _ in range(HOPS):
        before = find_job_record(state, origin["retry_of"]) if origin.get("retry_of") else None
        if before is None:
            break
        origin = before
    message = str(origin.get("message_id") or "")
    if not records.is_revisit(message):
        return str(record.get("message_id") or ""), record, False
    current = origin
    for _ in range(HOPS):
        if current.get("auto_retry_state") in ("pending", "submitting"):
            return message, current, True
        if current.get("auto_retry_state") != "queued":
            break
        found = select_job_records(state, lambda r, key=current["key"]: r.get("retry_of") == key)
        if not found or found[0].get("status") not in ENDED:
            return message, current, True  # Queued or running: its end is the turn's.
        current = found[0]
    return message, current, False


def settle(log, state, record, now=None) -> None:
    """What the turn of job ``record`` (ended) does to its topic's revisit, decided under the record's lock: the Bridge's
    after_runner and its thread may both see a revisit turn end, and only the first one acts."""
    message, record, waiting = lineage(state, record)
    if waiting:
        return  # A retry of a revisit turn that failed is to come: the watch waits for it.
    topic, chat = str(record.get("topic_id") or ""), str(record.get("chat_id") or "")
    status = str(record.get("status") or "")
    if not topic or status not in ENDED or not ordinary(state, topic, chat):
        return
    revisit_turn = records.is_revisit(message)
    checkpoint = read_checkpoint(Path(record["checkpoint_path"])) if record.get("checkpoint_path") else {}
    want = records.wanted(checkpoint) if status == "done" else None
    now = time.time() if now is None else now
    said, quiet = [], []

    def edit(data):
        mine = revisit_turn and data.get("message") == message
        if revisit_turn and not mine:
            return data or None  # Settled already, replaced or cancelled: not this turn's to change.
        round_ = int(data.get("round") or 0) + 1 if mine else 1
        if want is not None and round_ <= records.MAX_ROUND:
            said.append(f"revisit set topic={topic[-8:]} minutes={want['minutes']:g} round={round_}")
            quiet.extend([True] if mine and records.unchanged(checkpoint) else [])  # The turn ``silent`` kept back.
            return records.armed(topic, chat, want, by=job_key(message, record.get("job_id")), round_=round_, now=now)
        if not mine:
            return data or None  # An owner's turn that asked for none: the revisit set before stays.
        said.append(f"revisit ended topic={topic[-8:]} status={status} round={round_ - 1}"
                    + (" (the last one in a row)" if want is not None else ""))
        return None
    records.change(state, topic, edit)
    for line in said:
        log(line)
    if quiet:
        keep_seen(state, topic, chat, record)


def keep_seen(state, topic, chat, record) -> None:
    """A quiet revisit turn's result read as seen when the topic's result before it was: the 看板 does not call it 有结果没看."""
    end = ended_at(record)
    before = max((ended_at(other) for other in topic_job_records(state, topic, chat_id=chat)
                  if other.get("job_id") != record.get("job_id") and other.get("status") in ENDED and not unrun(other)
                  and ended_at(other) <= end), default=0.0)
    if read_seen(state).get(topic, 0.0) >= before:
        record_reads(state, {topic: end})


def silent(delivery) -> bool:
    """Whether the reply of the turn ``delivery`` (app/turn_delivery.py TurnDelivery) is kept, not sent: a revisit turn,
    done with its saved reply, that found nothing new and asks for the next look within MAX_ROUND. Never raises."""
    job, turn = delivery.job, delivery.turn
    try:
        if not records.enabled() or not records.is_revisit(job_field(job, "message_id")):
            return False
        if turn.status != "done" or getattr(turn, "verified_delivery", None) is None \
                or getattr(turn.verified_outcome, "status", "") != "completed":
            return False
        checkpoint = read_checkpoint(Path(job.checkpoint_path)) if job.checkpoint_path else {}
        current = records.read(delivery.ctx.state_dir, job.topic_id)
        if current.get("message") != job.message_id:
            return False
        return (records.unchanged(checkpoint) and records.wanted(checkpoint) is not None
                and int(current.get("round") or 0) + 1 <= records.MAX_ROUND)
    except Exception as exc:  # noqa: BLE001 - the reply goes out as any turn's does
        delivery.runtime.log(f"revisit reply not held back job=#{job_field(job, 'job_id')}: {type(exc).__name__}")
        return False


def board_rows(state_dir, chat_id, clock) -> list:
    """The 看板's rows (cards/board.py revisit_blocks): each revisit of ``chat_id``, the soonest first, as {topic, label,
    title, at (``clock`` of its due time), look, set_at, fired}; [] with the switch off."""
    if not records.enabled():
        return []
    rows = []
    for record in records.records(state_dir):
        if record.get("chat_id") != chat_id:
            continue
        label, title = label_title(state_dir, record["topic_id"], "（话题未知）")
        rows.append({"topic": record["topic_id"], "label": label, "title": title, "at": clock(float(record["due_at"])),
                     "look": str(record.get("look") or ""), "set_at": record.get("set_at"),
                     "fired": record.get("state") == records.FIRED})
    return rows


class Revisits:
    """The feature object (app/unified.py PARTS ``revisits``): its thread, none with the switch off. ``topics``:
    app/topics.py Topics (the Harness apps by name)."""
    name = "revisits"

    def __init__(self, runtime, topics) -> None:
        self.runtime, self.topics = runtime, topics

    def threads(self):
        return ((self.name, self.run),) if records.enabled() else ()

    def run(self, ctx) -> None:
        if self.runtime.shutdown.wait(START_WAIT):
            return
        every(self.runtime, TICK, self.tick, args=(ctx,), isolate=self.name)

    def tick(self, ctx, now=None) -> None:
        """One look at every revisit."""
        if not records.enabled():
            return
        now = time.time() if now is None else now
        state = Path(ctx.state_dir)
        for record in records.records(state):
            try:
                self.look(state, record, now)
            except Exception as exc:  # noqa: BLE001 - the others are looked at all the same
                self.runtime.log(f"revisit failed topic={str(record.get('topic_id'))[-8:]}: {type(exc).__name__}")

    def look(self, state, record, now) -> None:
        topic, chat = record["topic_id"], str(record.get("chat_id") or "")
        if record.get("state") == records.FIRED:
            job = find_job_record(state, str(record.get("message") or ""))
            if job is None and now - float(record.get("fired_at") or 0) > LOST:
                self.again(state, record, now)  # Marked, never queued (the Bridge stopped in between).
            elif job is not None and job.get("status") in ENDED:
                settle(self.runtime.log, state, job)  # Ended while no Bridge saw its runner go.
            return
        if float(record.get("due_at") or 0) > now or self.busy(state, topic, chat):
            return
        self.fire(state, record, now)

    def busy(self, state, topic, chat) -> bool:
        """Whether the topic has a turn queued, running or waiting for the owner: the revisit waits for it."""
        return bool(self.runtime.jobs.open_in_topic(topic, chat)) or any(
            job.get("status") in NOT_ENDED for job in topic_job_records(state, topic, chat_id=chat))

    def again(self, state, record, now) -> None:
        set_at = record.get("set_at")
        records.change(state, record["topic_id"], lambda data: {**data, "state": records.PENDING, "due_at": now}
                       if data.get("set_at") == set_at else data or None)

    def fire(self, state, record, now) -> None:
        """The revisit turn queued: marked FIRED with its message id first, then queued outside the lock."""
        set_at, mine = record.get("set_at"), []

        def mark(data):
            if data.get("set_at") != set_at or data.get("state") != records.PENDING:
                return data or None  # Replaced or cancelled meanwhile.
            if int(data.get("tries") or 0) >= MAX_TRIES:
                mine.append(None)
                return None  # Its topic cannot take a turn (its Harness is gone): the watch ends.
            data = {**data, "state": records.FIRED, "fired_at": now, "tries": int(data.get("tries") or 0) + 1}
            data["message"] = records.message_id(data)
            mine.append(data)
            return data
        records.change(state, record["topic_id"], mark)
        if mine == [None]:
            self.runtime.log(f"revisit dropped topic={record['topic_id'][-8:]}: not queued {MAX_TRIES} times")
        if not mine or mine == [None]:
            return
        marked = mine[0]
        if self.enqueue(state, marked):
            self.runtime.log(f"revisit queued topic={marked['topic_id'][-8:]} message={marked['message']}")
            return
        self.runtime.log(f"revisit not queued topic={marked['topic_id'][-8:]} message={marked['message']}")
        records.change(state, marked["topic_id"], lambda data: {**data, "state": records.PENDING, "due_at": now + RETRY}
                       if data.get("message") == marked["message"] else data or None)

    def enqueue(self, state, record) -> bool:
        """The revisit turn queued as a message of its topic (an alias makes the message the topic's)."""
        topic, chat, message = record["topic_id"], str(record.get("chat_id") or ""), record["message"]
        try:
            data = load_topic(state, topic)
        except (OSError, ValueError):
            return False
        app = self.topics.apps.get(data.get("harness_id") or "")
        if app is None:
            return False
        remember_topic_message(state, topic, chat, message)
        ctx = app.submission.event_context(app.harness.context, {"chat_id": chat, "message_id": message})
        if ctx.topic_id != topic:
            return False
        text = WAKE.format(look=record.get("look") or "上一轮说要回来看的事")
        append_history(ctx.history_path, "user", text, {"message_id": message})
        return app.submission.enqueue_chat_job(ctx, text, chat, message, prompt_content=text) is not None
