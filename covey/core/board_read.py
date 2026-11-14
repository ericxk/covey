"""What the owner's private chat's topics are doing, as the records say it: one read of the indexes of questions/ and
jobs/ (no record lock, no Feishu call) and the topic-seen and board-priority files (``read``), and the pure
functions its readers judge the records with. Its readers: the pinned board (app/pins.py board, look), 手上的事
(app/overview.py view), 早晚汇总 (app/digest.py collect), the main thread's snapshot (app/main_thread.py
snapshot_groups) and its tool topics_list (app/main_tools.py topic_row, from ``topic_jobs`` and ``topic_questions``:
every job of the chat, not a window).

A reader that also needs the board's ``taken off`` reads once with the board record's ``keep`` (app/pins.py kept)
and hands the read to the board's look; the jobs it judges itself are those ``fresh`` picks, as read without it. So
a main-thread turn reads jobs/ once (before 2026-11-10: three times, 手上的事's running ones, the board's, and its
own results), as do 手上的事 with the board on and 早晚汇总 (each twice before).

"A result the owner has not seen" (有结果未看) is one rule, the board's (the owner, 2026-11-10: 以看板为准), and
all five readers take it from ``groups`` and ``unseen`` below: the same topics, at the same time, from the same
records. ``groups`` splits a read as the board does: waiting (the newest pending question shown, asked within
QUESTION_AGE), running (a job in RUNNING with a task card) and results (``board_result``): the topic's latest job
that ran (``unrun``) ended done, failed, interrupted or stopped (BOARD_RESULTS, cancelled among them) with a task
card; seen once its topic was read at or after its end, or for a top-level topic's answer the top level read once
it was there (``seen_at``, ``top_read``), and once seen it stays seen (the board record's ``results``); one not seen
stays however old (the board record keeps its topic in the read: ``keep``), one seen stays KEEP_SEEN from when it
was first seen; one that ended before the board's rules (keep_since) as before them: within RESULT_AGE, not seen in
its topic, not done at the top level. ``unseen``: the results not seen yet, without the topics the owner took off
the board. The board's record gives the rules (``rules``, app/pins.py board_rules); without the board
(COVEY_PINNED_TOPICS unset) there is no record and no read receipt: every result ``read`` reaches (RESULT_AGE)
is unseen. The group is named ``result`` everywhere (the main thread's snapshot and topics_list: 有结果未看, only
the unseen ones; the board and 手上的事: 有结果, with the ones seen within KEEP_SEEN). The windows of RESULT_AGE and
TOOLS_AGE left elsewhere are for what was done lately (topics_list's recent, 早晚汇总's 做完 since the digest
before), never for what is unseen.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

from covey.core.jobs import jobs_dir, select_job_records
from covey.core.main_thread import main_topic_id, traits
from covey.core.metrics import run_start, timings_of
from covey.core.state import epoch_seconds, read_object
from covey.core.statuses import RUNNING
from covey.core.store import directory_store
from covey.core.topic_seen import read_seen
from covey.core.topics import label_title

PRIORITY = "board-priority.json"
RESULT_AGE = 3 * 86400  # What ``read`` reaches back; a board result that ended before keep_since: on it this long.
KEEP_SEEN = 12 * 3600  # A board result the owner has seen stays this long from when it was first seen (11-07).
BOARD_RESULTS = frozenset({"done", "failed", "interrupted", "cancelled"})
QUESTION_AGE = 86400  # A pending question asked longer ago is not the board's waiting (手上的事 lists it apart).
STOPPED = "cancelled"  # A board result only since the board record's stopped_since (the owner, 2026-11-09).
RAN = ("backend_requested", "backend_started", "execution_finished")  # Timings of a job whose run began.
TOOLS_AGE = 3 * 86400  # topics_list's recent: what moved in the last three days (not its unseen: ``unseen``).


@dataclasses.dataclass(frozen=True)
class Read:
    """One ``read`` of a chat: ``questions`` its pending questions shown (not display_pending), in the index's
    order; ``jobs`` its jobs running, written within RESULT_AGE or of a topic in ``keep``, newest first
    (core/jobs.py select_job_records); both without the main thread's topic (Traits.listed). ``seen``
    topic-seen.json, ``priority`` board-priority.json."""
    questions: list
    jobs: list
    seen: dict
    priority: dict
    keep: frozenset = frozenset()


def listed(topic_id, chat) -> bool:
    """Whether ``topic_id`` is on the board and 手上的事: not the main thread's, which talks at the top level."""
    return traits(topic_id, chat).listed


def fresh(record, now) -> bool:
    """Whether ``read`` takes job ``record`` without ``keep``: running, or written within RESULT_AGE (a job that
    ended since was written since)."""
    return record.get("status") in RUNNING or (epoch_seconds(record.get("updated_at")) or now) >= now - RESULT_AGE


def pending_questions(state, chat) -> list:
    """``chat``'s pending questions shown, of the topics ``listed``, in the index's order."""
    return [row for _, row in directory_store(Path(state) / "questions").query(chat_id=chat, status="pending")
            if not row.get("display_pending") and listed(row.get("topic_id"), chat)]


def running_jobs(state, chat) -> list:
    """``chat``'s jobs in RUNNING of the topics ``listed``, newest first; only those records are read."""
    return [record for record in select_job_records(state, None, chat_id=chat, status=frozenset(RUNNING))
            if listed(record.get("topic_id"), chat)]


def read(state, chat, now, keep=frozenset()) -> Read:
    """``chat`` now, one query of each index: ``Read``. ``keep``: topics whose older jobs are wanted too (the board
    record's ``results``: one on the board may have ended before RESULT_AGE)."""
    keep = frozenset(keep)
    questions = pending_questions(state, chat)
    jobs = select_job_records(state, lambda record: listed(record.get("topic_id"), chat) and (
        fresh(record, now) or record.get("topic_id") in keep), chat_id=chat)
    return Read(questions, jobs, read_seen(state), times(Path(state) / PRIORITY), keep)


def topic_jobs(state, chat) -> dict:
    """Every job record of ``chat``'s topics by topic id, each topic's oldest first; not the main thread's."""
    main, topics = main_topic_id(chat), {}
    for _, record in directory_store(jobs_dir(state)).query(chat_id=chat):
        topic_id = record.get("topic_id")
        if isinstance(topic_id, str) and topic_id and topic_id != main:
            topics.setdefault(topic_id, []).append(record)
    for records in topics.values():
        records.sort(key=job_number)
    return topics


def job_number(record):
    try:
        return int(record.get("job_id") or 0)
    except (TypeError, ValueError):
        return 0


def topic_questions(state, chat) -> dict:
    """topic_id -> its pending question records shown to the owner, oldest first."""
    found = {}
    rows = directory_store(Path(state) / "questions").query(chat_id=chat, status="pending")
    for _, row in sorted(rows, key=lambda item: item[1].get("created_at") or 0):
        if not row.get("display_pending") and row.get("topic_id"):
            found.setdefault(row["topic_id"], []).append(row)
    return found


def topic_label(state, topic_id):
    """(T number, title) of ``topic_id`` as 手上的事 (app/overview.py Overview.topic) and the main thread's snapshot
    write it: "" without a number yet, and then （话题未知） for no title (core/topics.py label_title)."""
    label, title = label_title(state, topic_id)
    return (label, title) if label else ("", title or "（话题未知）")


def ended_at(record):
    """When a job ended: its run finished, else its answer delivered, else its record's last write. The run's end
    comes before the answer is sent, so the owner's read of the answer card (topic-seen.json) is never earlier than
    it, even with the topic open as the answer arrives (``delivered`` is marked after Feishu has answered)."""
    timings = timings_of(record)
    for stage in ("execution_finished", "delivered"):
        if isinstance(timings.get(stage), (int, float)):
            return float(timings[stage])
    return epoch_seconds(record.get("updated_at")) or 0.0


def unrun(record):
    """Whether ``record`` is a job cancelled before it ran: one taken out of the queue, merged into the task
    running (its message a supplement there, app/steering.py), or never started. No turn of its topic: the topic's
    latest is the one before it."""
    if record.get("status") != STOPPED:
        return False
    timings = timings_of(record)
    return bool(record.get("merged_into_job_id") or record.get("never_started")) or not any(
        isinstance(timings.get(stage), (int, float)) and not isinstance(timings.get(stage), bool) for stage in RAN)


def started_at(record):
    """When a job's run started (core/metrics.py run_start, the start of its 用时), else when it came in."""
    timings = timings_of(record)
    begun = run_start(timings)
    if begun is not None:
        return begun
    if isinstance(timings.get("received"), (int, float)):
        return float(timings["received"])
    return epoch_seconds(record.get("created_at")) or 0.0


def seconds(value, default):
    """``value`` as epoch seconds when it is a number, else ``default``."""
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def times(path):
    """A topic_id -> epoch seconds file, without the entries that are not."""
    return {str(key): float(value) for key, value in read_object(path).items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)}


def top_read(seen, chat):
    """The owner's last read of a message at ``chat``'s top level, epoch seconds (0.0: none known): the latest of
    ``seen`` (topic-seen.json) over the topics there (the main thread's)."""
    return max((value for topic, value in seen.items() if traits(topic, chat).top_level), default=0.0)


def seen_at(record, seen, read_top=0.0, inline=None):
    """When the owner has seen the result of ``record`` (a job record that ended), None when not yet: its topic read
    at or after its end (``seen``), or, for an answer written on a top-level card (a conclusion an older release
    wrote on the main thread's card: ``inline``, a job record -> when, or None), the top
    level read (``read_top``) once it was there, never before its end."""
    topic, at = record.get("topic_id") or "", ended_at(record)
    if topic in seen and seen[topic] >= at:
        return seen[topic]
    if not read_top:
        return None
    if traits(topic, record.get("chat_id")).top_level:
        shown = seconds(timings_of(record).get("delivered"), at)
    else:
        shown = inline(record) if inline is not None else None
        if shown is None:
            return None
    return read_top if read_top >= max(at, shown) else None


def board_result(topic, record, seen, now, *, since, relayed, read_top, keep_since, results, stopped_since,
                 inline=None):
    """The board's rule (app/pins.py board, its arguments): whether ``record``, ``topic``'s latest job that ran, is
    a result on the board, as (when it ended, when the owner first saw it, None: not yet), else None."""
    at = ended_at(record)
    if (record.get("status") not in BOARD_RESULTS or not record.get("card_message_id") or at < since
            or (record.get("status") == STOPPED and stopped_since is not None and at < stopped_since)):
        return None
    if keep_since is not None and at < keep_since:
        top = record.get("status") == "done" and relayed is not None and relayed(record)
        if now - at > RESULT_AGE or seen.get(topic, 0) >= at or top:
            return None
        return at, None
    kept = (results or {}).get(topic)
    kept = kept if isinstance(kept, dict) and kept.get("at") == at else {}
    first = kept.get("seen")
    if not isinstance(first, (int, float)) or isinstance(first, bool):
        first = seen_at(record, seen, read_top, inline)
    if first is not None and now - first >= KEEP_SEEN:
        return None
    return at, first


@dataclasses.dataclass(frozen=True)
class Rules:
    """The board's rules for its results, as its record (<state>/pinned-topics.json, app/pins.py) has them:
    ``board_result``'s arguments. The defaults are a deployment without the board: every result read, none seen
    but by ``seen``."""
    since: float = 0.0
    keep_since: float | None = None
    stopped_since: float | None = None
    results: dict = dataclasses.field(default_factory=dict)
    read_top: float = 0.0
    relayed: object = None  # A job record -> whether its done conclusion went to the top level (app/main_relay.py).
    inline: object = None  # A job record -> when its conclusion was written on a top-level card, or None.


def rules(record, chat, now, seen, *, board=True, relayed=None, inline=None) -> Rules:
    """``Rules`` from the board's ``record`` for ``chat`` at ``now`` as the board's first look takes them (a time
    not in the record: ``now``, its rules start now); ``board`` False: the board is off (``Rules`` defaults)."""
    if not board:
        return Rules(read_top=top_read(seen, chat), relayed=relayed, inline=inline)
    ours = record.get("chat_id") == chat
    table = record.get("results") if ours and isinstance(record.get("results"), dict) else {}
    return Rules(seconds(record.get("since"), now), seconds(record.get("keep_since") if ours else None, now),
                 seconds(record.get("stopped_since") if ours else None, now), table, top_read(seen, chat),
                 relayed, inline)


@dataclasses.dataclass(frozen=True)
class Groups:
    """A read split as the board splits it, topic -> record: ``waiting`` the newest question, ``running`` the job
    its pin shows (``order``), ``results`` (record, when it ended, when the owner first saw it or None)."""
    waiting: dict
    running: dict
    results: dict


def order(record):
    """Which of a topic's running jobs the board shows: the one running before one queued, then the latest."""
    return (record.get("status") not in (None, "queued"), int(record.get("job_id") or 0)) if record else (False, -1)


def groups(questions, jobs, seen, now, rules=Rules()) -> Groups:
    """The board's three groups of ``questions`` and ``jobs`` (a ``read``'s), a topic in the first it is in."""
    waiting, running, results = {}, {}, {}
    for record in questions:
        topic, at = record.get("topic_id") or "", record.get("created_at")
        if (topic and record.get("question_message_id") and isinstance(at, (int, float)) and now - at <= QUESTION_AGE
                and at > (waiting.get(topic) or {}).get("created_at", -1)):
            waiting[topic] = record
    latest = {}
    for record in jobs:
        topic = record.get("topic_id") or ""
        if (topic and not unrun(record)
                and int(record.get("job_id") or 0) > int(latest.get(topic, {}).get("job_id") or 0)):
            latest[topic] = record
        if (topic and topic not in waiting and record.get("status") in RUNNING and record.get("card_message_id")
                and order(record) > order(running.get(topic, {}))):
            running[topic] = record
    for topic, record in latest.items():
        if topic in waiting or topic in running:
            continue
        result = board_result(topic, record, seen, now, since=rules.since, relayed=rules.relayed,
                              read_top=rules.read_top, keep_since=rules.keep_since, results=rules.results,
                              stopped_since=rules.stopped_since, inline=rules.inline)
        if result is not None:
            results[topic] = (record, *result)
    return Groups(waiting, running, results)


def unseen(found: Groups, off=()) -> dict:
    """有结果未看: topic -> the job record of its result the owner has not seen (``found``'s results first seen
    never), not a topic in ``off`` (taken off the board: app/pins.py dismissed). The one rule of all its readers."""
    return {topic: record for topic, (record, _, first) in found.results.items() if first is None and topic not in off}
