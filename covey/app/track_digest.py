"""Memory merge for 多路并行 (docs/butler.md): what the butler's tracks answered, from the track's turn to the
butler's next one (COVEY_BUTLER_TRACKS=1; the record is core/track_ledger.py).

- ``record``: TurnDelivery's ``ledger`` hook, once a turn's result is delivered. A turn of a track (slot 1..) that ended
  (not one that waits for the owner's answer: its answer turn writes) saves its record; the answer is the delivered,
  authorized reply and nothing else (rule 5). Never raises: a failure is a log line, the delivery stands.
- ``block``: the text a turn's message carries after the snapshot. The butler's own turn (slot 0) gets what it has
  not been given yet and marks it given (``mark``), once the text is made; a track's turn gets the same block without
  marking, as the butler has not read it. The turn that took it ending badly gives it again (``settled``).
"""
from __future__ import annotations

import time

from covey.core import butler_opened
from covey.core import butler_tracks as core
from covey.core import track_ledger as ledger
from covey.core.jobs import job_key
from covey.core.state import read_object
from covey.core.topics import topic_file

ENDED = frozenset({"done", "failed", "cancelled"})  # The statuses a track's turn ends in with its result delivered.


def opened_numbers(state_dir, key) -> list:
    """The T numbers (T12) of the topics the butler job ``key`` opened, forked or handed on to, by number."""
    numbers = set()
    for _, entry in butler_opened.by_job(state_dir, key):
        topic = butler_opened.topic_of(entry)
        if butler_opened.answered(entry) and topic:
            number = read_object(topic_file(state_dir, topic)).get("topic_number")
            if type(number) is int:
                numbers.add(number)
    return [f"T{number}" for number in sorted(numbers)]


def record(delivery, delivered, status, *, now=None) -> None:
    """Save the ledger record of ``delivery``'s turn when it is a track's and ended (see the module's docstring)."""
    job, turn = delivery.job, delivery.turn
    try:
        track = core.track_of(job.topic_id, job.chat_id)
        if not track or turn.status not in ENDED:
            return  # Slot 0's answers are in its own session; a question waiting for an answer is not an answer yet.
        state_dir = delivery.ctx.state_dir
        key = job_key(job.message_id, job.job_id)
        reply = turn.verified_delivery
        ledger.write(state_dir, ledger.make(
            key, job.chat_id, track, job.message_id, job.user_text or job.prompt_text,
            reply.text if delivered and reply is not None else "", turn.status,
            time.time() if now is None else now, getattr(job, "session_id", ""), opened_numbers(state_dir, key)))
    except Exception as exc:  # noqa: BLE001 - the ledger never fails a delivered turn
        log = getattr(getattr(delivery, "runtime", None), "log", None)
        if log is not None:
            log(f"track ledger not written job=#{getattr(job, 'job_id', '?')}: {type(exc).__name__}")


def block(state_dir, chat_id, key, settled, *, mark, log=None, now=None) -> str:
    """The block of what the other tracks answered for the turn ``key``, "" when there is none (or the ledger cannot
    be read). ``mark``: the butler's own turn, which is then given them (``digested_by`` set to ``key``);
    ``settled(turn key)``: whether that turn ended well enough to have read what it was given."""
    try:
        rows, more = ledger.pending(state_dir, chat_id, key, time.time() if now is None else now, settled)
        text = ledger.render(rows, more)
        if text and mark:
            ledger.claim(state_dir, rows, key, settled)
        return text
    except Exception as exc:  # noqa: BLE001 - the owner's message goes on without it
        if log is not None:
            log(f"track ledger block unavailable: {type(exc).__name__}")
        return ""
