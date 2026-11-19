"""What the owner's private chat's topics are doing, as the records say it: one read of the indexes of questions/ and
jobs/ (no record lock, no Feishu call) and the topic-seen and board-priority files (``read``), and the pure
functions its readers judge the records with. Its readers: the pinned board (app/pins.py board, look), 手上的事
(app/overview.py view), 早晚汇总 (app/digest.py collect), the butler's snapshot (app/butler.py
snapshot_groups) and its tool topics_list (app/butler_tools.py topic_row, from ``topic_jobs`` and ``topic_questions``:
every job of the chat, not a window).

What waits for the owner (等我) is one count too, ``waits``: every reader above, the 看板 (app/digest.py board_view),
the project card (``asks``) and the web page (app/web_sync.py) take it, and each 等我 carries the card it is acted on
(the question card, the launch card), so whatever lists it can open it (the owner, 2026-11-19: 汇总说 6 个、看板
3 个、管家 4 个，在私聊里也找不到).

A reader that also needs the board's ``taken off`` reads once with the board record's ``keep`` (app/pins.py kept)
and hands the read to the board's look; the jobs it judges itself are those ``fresh`` picks, as read without it. So
a butler turn reads jobs/ once (before 2026-11-10: three times, 手上的事's running ones, the board's, and its
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
is unseen. The group is named ``result`` everywhere (the butler's snapshot and topics_list: 有结果未看, only
the unseen ones; the board and 手上的事: 有结果, with the ones seen within KEEP_SEEN). The windows of RESULT_AGE and
TOOLS_AGE left elsewhere are for what was done lately (topics_list's recent, 早晚汇总's 做完 since the digest
before), never for what is unseen.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

from covey.core.jobs import jobs_dir, select_job_records
from covey.core import look, projects
from covey.core.butler import is_butler_topic, traits
from covey.core.metrics import run_start, timings_of
from covey.core.state import epoch_seconds, read_object
from covey.core.statuses import OPEN_FORMS, RUNNING
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
START = "waiting_workspace"  # A job waiting for the owner to press 开始 on its topic's launch card.


@dataclasses.dataclass(frozen=True)
class Read:
    """One ``read`` of a chat: ``questions`` its pending questions shown (not display_pending), in the index's
    order; ``jobs`` its jobs running, written within RESULT_AGE or of a topic in ``keep``, newest first
    (core/jobs.py select_job_records); both without the butler's topic (Traits.listed). ``seen``
    topic-seen.json, ``priority`` board-priority.json. ``cells``: a project's thread topic -> its lead's topic id
    (core/projects.py cells), {} without COVEY_PROJECTS; ``groups`` takes it, so a project is one cell."""
    questions: list
    jobs: list
    seen: dict
    priority: dict
    keep: frozenset = frozenset()
    cells: dict = dataclasses.field(default_factory=dict)


def listed(topic_id, chat) -> bool:
    """Whether ``topic_id`` is on the board and 手上的事: not the butler's, which talks at the top level."""
    return traits(topic_id, chat).listed


def fresh(record, now) -> bool:
    """Whether ``read`` takes job ``record`` without ``keep``: running, or written within RESULT_AGE (a job that
    ended since was written since)."""
    return record.get("status") in RUNNING or (epoch_seconds(record.get("updated_at")) or now) >= now - RESULT_AGE


def may_be_read(fields, now, keep, chat) -> bool:
    """Whether a job record with these indexed and carried ``fields`` (core/store.py) can be one ``read`` takes: false
    only when they show it is not (``fresh``, or its topic in ``keep``, and ``listed``), so ``read`` leaves such a
    record unread. True when a field it needs is not there: it is not indexed, or the .index is from a release
    before ``updated_at`` was carried; the record then decides."""
    status, updated, topic = fields.get("status"), fields.get("updated_at"), fields.get("topic_id")
    if topic is None:
        return True
    if status is not None and updated is not None and not (fresh(fields, now) or topic in keep):
        return False
    return listed(topic, chat)


def pending_questions(state, chat) -> list:
    """``chat``'s pending questions shown, of the topics ``listed``, in the index's order."""
    return [row for _, row in directory_store(Path(state) / "questions").query(chat_id=chat, status="pending")
            if not row.get("display_pending") and listed(row.get("topic_id"), chat)]


def top_questions(state, chat) -> list:
    """``chat``'s pending questions shown of the topics not ``listed``: the butler's and the project leads', which ask
    at the chat's top level. 要你定的事 waits under 等你 whoever asks (docs/ui.md 5.3), so 手上的事 and the 看板 list
    these too, under their speaker (``speaker_of``)."""
    return [row for _, row in directory_store(Path(state) / "questions").query(chat_id=chat, status="pending")
            if row.get("topic_id") and not row.get("display_pending") and not listed(row.get("topic_id"), chat)]


def speaker_of(state, topic_id, chat) -> str:
    """Who speaks for ``topic_id`` at ``chat``'s top level (core/look.py ``speaker``): 管家, 专项「名」· 负责人, or the
    topic's T number and title; '' for the butler's topic with the butler off (its answers are a topic's then)."""
    role = traits(topic_id, chat).name
    if role == "butler":
        return look.speaker("butler")
    if role == "lead":
        return look.speaker("lead", name=projects.lead_record(state, topic_id, chat).get("name") or "")
    if role in {"butler_off", "lead_off"}:
        return ""
    label, title = label_title(state, topic_id)
    return look.speaker("topic", label=label, title=title)


def running_jobs(state, chat) -> list:
    """``chat``'s jobs in RUNNING of the topics ``listed``, newest first; only those records are read."""
    return [record for record in select_job_records(state, None, chat_id=chat, status=frozenset(RUNNING))
            if listed(record.get("topic_id"), chat)]


def read(state, chat, now, keep=frozenset()) -> Read:
    """``chat`` now, one query of each index: ``Read``. ``keep``: topics whose older jobs are wanted too (the board
    record's ``results``: one on the board may have ended before RESULT_AGE). A job record whose indexed and carried
    fields (core/store.py) show it is none of those is not read (``may_be_read``): in a new process, a few hundred
    of the thousands."""
    keep = frozenset(keep)
    cells = projects.cells(state, chat)
    # A project's cell is its lead's id (``groups``): the board record keeps results by cell, the older jobs wanted are its threads'.
    wanted = keep | frozenset(topic for topic, lead in cells.items() if lead in keep) if cells else keep
    questions = pending_questions(state, chat)
    jobs = select_job_records(state, lambda record: listed(record.get("topic_id"), chat) and (
        fresh(record, now) or record.get("topic_id") in wanted), chat_id=chat,
        narrow=lambda fields: may_be_read(fields, now, wanted, chat))
    return Read(questions, jobs, read_seen(state), times(Path(state) / PRIORITY), keep, cells)


def asks(record) -> bool:
    """Whether question ``record`` waits for the owner (等我): pending; shown (not ``display_pending``, so not one held
    for a project's lead, app/triage.py, nor one whose result must come first and has not); its card sent (the card
    every reader opens). The one test of a question every reader of 等我 takes (``waits``, and the web page's word)."""
    return (record.get("status", "pending") == "pending" and not record.get("display_pending")
            and (not record.get("body_delivery_required") or record.get("body_delivery_confirmed") is True)
            and bool(record.get("question_message_id")) and bool(record.get("topic_id")))


def launch_card(state, topic_id, chat) -> str:
    """The message id of ``topic_id``'s newest launch card that can still start it (OPEN_FORMS), '' when none."""
    rows = directory_store(Path(state) / "launch-forms").query(topic_id=topic_id, chat_id=chat)
    cards = [(row.get("updated_at") or row.get("created_at") or "", str(row.get("message_id") or ""))
             for _, row in rows if row.get("status") in OPEN_FORMS and row.get("message_id")]
    return max(cards, key=lambda item: str(item[0]))[1] if cards else ""


@dataclasses.dataclass(frozen=True)
class Wait:
    """One thing that waits for the owner: ``kind`` "question" (``record`` the question's, ``card`` its question card)
    or "start" (``record`` the job's, waiting for 开始 on its topic's launch card, ``card`` that card); ``since`` when
    it began to wait, epoch seconds."""
    topic: str
    kind: str
    record: dict
    since: float
    card: str


@dataclasses.dataclass(frozen=True)
class Waits:
    """``waits``: ``live`` asked within QUESTION_AGE, ``stale`` before it, each newest first."""
    live: list
    stale: list

    @property
    def all(self) -> list:
        return [*self.live, *self.stale]

    def count(self) -> int:
        """The number of 等我 every reader says: the live ones and the old ones (the 早晚汇总's 等你 since 2026-11-09)."""
        return len(self.live) + len(self.stale)


def waits(state, chat, now, off=()) -> Waits:
    """等我 of ``chat`` now: the one count of what waits for the owner, which the 早晚汇总, the 看板, 手上的事, the
    butler's snapshot and the web page all take (the owner, 2026-11-19: 汇总 6 个、看板 3 个、管家 4 个，对不上也找不到).

    Per topic one: its newest question that ``asks`` (the butler's and the leads' at the top level too: 要你定的事
    waits under 等你 whoever asks), else its newest job waiting for 开始 (START) whose topic has a launch card that can
    still start it (``launch_card``). Each has the card the owner acts on (``Wait.card``), so a reader links right to
    it; one without such a card is not his to act on and not counted (a launch card that never reached Feishu: the
    stale closer's, core/stale.py). ``off``: topics the owner took off the board (app/pins.py dismissed), not counted.
    One that has waited over QUESTION_AGE is ``stale``: listed apart as one line, still counted. Index queries only (the
    pending questions, the jobs in START, and the launch forms of those topics): no record lock, no Feishu call."""
    found = {}
    for _, row in directory_store(Path(state) / "questions").query(chat_id=chat, status="pending"):
        topic, at = row.get("topic_id") or "", row.get("created_at")
        if (asks(row) and topic not in off and isinstance(at, (int, float))
                and at > (found[topic].since if topic in found else -1)):
            found[topic] = Wait(topic, "question", row, float(at), str(row["question_message_id"]))
    starts = {}
    for record in select_job_records(state, None, chat_id=chat, status=frozenset({START})):
        topic = record.get("topic_id") or ""
        if topic and topic not in found and topic not in off and listed(topic, chat):
            at = started_at(record)
            if at > (starts[topic].since if topic in starts else -1):
                starts[topic] = Wait(topic, "start", record, at, "")
    for topic, wait in starts.items():
        card = launch_card(state, topic, chat)
        if card:
            found[topic] = dataclasses.replace(wait, card=card)
    rows = sorted(found.values(), key=lambda wait: wait.since, reverse=True)
    return Waits([wait for wait in rows if now - wait.since <= QUESTION_AGE],
                 [wait for wait in rows if now - wait.since > QUESTION_AGE])


def topic_jobs(state, chat) -> dict:
    """Every job record of ``chat``'s topics by topic id, each topic's oldest first; not the butler's."""
    topics = {}
    for _, record in directory_store(jobs_dir(state)).query(chat_id=chat):
        topic_id = record.get("topic_id")
        if isinstance(topic_id, str) and topic_id and not is_butler_topic(topic_id, chat):
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
    """topic_id -> its pending question records that wait for the owner (``asks``), oldest first."""
    found = {}
    rows = directory_store(Path(state) / "questions").query(chat_id=chat, status="pending")
    for _, row in sorted(rows, key=lambda item: item[1].get("created_at") or 0):
        if asks(row):  # As 等我 counts a question (``waits``).
            found.setdefault(row["topic_id"], []).append(row)
    return found


def topic_label(state, topic_id):
    """(T number, title) of ``topic_id`` as 手上的事 (app/overview.py Overview.topic) and the butler's snapshot
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
    ``seen`` (topic-seen.json) over the topics there (the butler's)."""
    return max((value for topic, value in seen.items() if traits(topic, chat).top_level), default=0.0)


def seen_at(record, seen, read_top=0.0, inline=None):
    """When the owner has seen the result of ``record`` (a job record that ended), None when not yet: its topic read
    at or after its end (``seen``), or, for an answer written on a top-level card (a conclusion an older release
    wrote on the butler's card: ``inline``, a job record -> when, or None), the top
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
    relayed: object = None  # A job record -> whether its done conclusion went to the top level (app/butler_relay.py).
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


def groups(questions, jobs, seen, now, rules=Rules(), cells=None) -> Groups:
    """The board's three groups of ``questions`` and ``jobs`` (a ``read``'s), a topic in the first it is in.

    ``cells`` (a ``read``'s: a project's thread topic -> its lead's topic id, core/projects.py): the threads of one project
    are one topic here, the lead's, so the board has a cell for the project and not one for each thread. In a cell the
    newest question waits, the running job the board shows is the one running before one queued and then the latest
    (``order``; job ids are the Bridge's one count, so they order jobs of different topics too), and the result is the
    one of the cell's threads' latest jobs (as each would be a topic's) that the owner has not seen, the one that
    ended last of them, else the one that ended last: an older result not seen is not lost behind a newer one seen,
    but only one is pinned. With ``cells`` empty or none of a topic's, a topic is its own cell and every rule is as it
    was before cells."""
    cell = (lambda topic: cells.get(topic, topic)) if cells else (lambda topic: topic)
    waiting, running, results = {}, {}, {}
    for record in questions:
        topic, at = cell(record.get("topic_id") or ""), record.get("created_at")
        if (topic and asks(record) and isinstance(at, (int, float)) and now - at <= QUESTION_AGE
                and at > (waiting.get(topic) or {}).get("created_at", -1)):
            waiting[topic] = record
    latest = {}
    for record in jobs:
        member = record.get("topic_id") or ""
        topic = cell(member)
        if (member and not unrun(record)
                and int(record.get("job_id") or 0) > int(latest.get(member, {}).get("job_id") or 0)):
            latest[member] = record
        if (topic and topic not in waiting and record.get("status") in RUNNING and record.get("card_message_id")
                and order(record) > order(running.get(topic, {}))):
            running[topic] = record
    members = {}
    for member, record in latest.items():
        members.setdefault(cell(member), []).append(record)
    for topic, records in members.items():
        if topic in waiting or topic in running:
            continue
        found = []
        for record in records:
            result = board_result(topic, record, seen, now, since=rules.since, relayed=rules.relayed,
                                  read_top=rules.read_top, keep_since=rules.keep_since, results=rules.results,
                                  stopped_since=rules.stopped_since, inline=rules.inline)
            if result is not None:
                found.append((record, *result))
        if found:
            results[topic] = max(found, key=lambda item: (item[2] is None, item[1], int(item[0].get("job_id") or 0)))
    return Groups(waiting, running, results)


def unseen(found: Groups, off=()) -> dict:
    """有结果未看: topic -> the job record of its result the owner has not seen (``found``'s results first seen
    never), not a topic in ``off`` (taken off the board: app/pins.py dismissed). The one rule of all its readers."""
    return {topic: record for topic, (record, _, first) in found.results.items() if first is None and topic not in off}
