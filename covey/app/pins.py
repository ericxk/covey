"""The pinned board: the owner's private chat's topics pinned (Pin 消息) in the order the Bridge keeps, under
手上的事 (app/overview.py), which stays the earliest pin and lists what has no pin. Off unless both
COVEY_PINNED_TOPICS=1 and COVEY_PINNED_OVERVIEW=1 (docs/configuration.md): 手上的事's loop runs it, after
its own card is pinned, so that card is below every topic's.

Feishu lists a chat's pins newest pin first (tried in a group): a message unpinned and pinned again
goes to the top; ``pins create`` on a message pinned already changes nothing and answers with its original
create_time, on one that was unpinned it pins it again with a new one. A pin adds no message, no unread and no chat
list preview; a recall would leave "撤回了一条消息", so nothing here recalls. A pinned card opens its topic at that
card, so each topic's pin is its current card: the question card, the task card, or a 话题区's answer card. The
private chat's ``pins list`` answers empty, so what is pinned is this module's record only.

The board (``board``, from the indexes of questions/ and jobs/: no record lock, no Feishu call; the read, the groups
and the result rule are core/board_read.py's, read, groups and board_result, as are ended_at, unrun, seen_at,
top_read, KEEP_SEEN and RESULT_AGE below; its results not seen are 有结果未看 for every reader, ``board_results``),
top to bottom:

- waiting (等你回答, the owner's 等我): the newest pending question shown in the topic, asked within QUESTION_AGE;
  without one, its task waiting for 开始 on a launch card that can still start it (core/board_read.py starts), come
  in within QUESTION_AGE too: every other reader of 等我 counts it (core/board_read.py waits), and a topic opened by
  a reply to a card that is no topic's (a node's alert) has no other card the owner can find it by. Newest first.
- running (进行中, 在做): a job in RUNNING with a task card, the topic's running one before its queued next; started
  last first. Above the results (等我 → 在做 → 做完).
- result (有结果, the owner's 做完): the topic's latest job (by job_id; not one cancelled before it ran,
  ``unrun``) done, failed, interrupted or stopped (cancelled: by the owner's 停止, a refusal's stop, the task's time
  limit; all kept, its state word 已停止, core/turn_words.py STATES), ended
  (``ended_at``) after the board first ran (the record's ``since``: when it is first switched on, three days of
  results the owner has long dealt with would fill it), until the owner has seen it and KEEP_SEEN more seconds have
  passed (as a phone keeps a notification seen; one not seen stays, however old). Seen
  (``seen_at``): <state>/topic-seen.json (topic_id -> when the owner last read the bot's messages there, epoch
  seconds) at or after its end: the answer card in its thread, or its conclusion sent back to the top level as a
  message of its own (app/butler_relay.py, the topic's message too). An answer written on a top-level card already
  read brings no read receipt: for a top-level topic's, the owner's last read of any top-level topic's message
  (the butler's) counts too, once at or after the answer was there (its ``delivered``, so a read seconds before its relay went out does not count). When it was first seen
  is kept (the record's ``results``), so a later read does not start KEEP_SEEN again. A result that ended before
  these rules (the record's ``keep_since``, set by the first round with them) is on the board as before: within
  RESULT_AGE, not seen in its topic, and not a relayed one done; a stopped one that
  ended before the record's ``stopped_since`` (set by the first round that keeps them) is not on it, as
  before. Ended last first.

What each topic's pin is (``aimed``, by the placement table's ``status_card``, core/placement.py): in the 话题区
layout (管家), a topic's own cards in its thread: its
question card while it waits (its launch card while it waits for 开始), its task card while it runs, the answer
card its round sent into its thread once
done or stopped (the job's ``answer_message_id``: a stopped turn's last full reply, under ⏹ 已停止; its task card
when the answer is on it or went otherwise, or a stopped turn had none). In the other
layouts a topic with a thread is pinned by its status card (app/status_cards.py: its number and title, state and
one line), the same message in every group, sent into its thread and kept up to date by each
round. A status card pinned before the 话题区
stopped using them is not held in place (``buffered``) and is unpinned as the board is laid again; it is never
recalled.

A topic is in one group, the first of these it is in. <state>/board-priority.json (topic_id -> epoch seconds; none
without the file) puts its topics first in their group, the latest first. The first MAX_PINS that the owner did not
take off are pinned (``choose``), the results seen giving way first, the earliest seen first; 手上的事 lists the
rest.

Taken off (``dismissed``): a topic the owner unpinned by hand, or took off with 手上的事's 这些都不用回答了 or
through the butler's topic_priority (``dismiss``). It is not pinned, and neither 手上的事 nor the butler's
snapshot counts it, until it has something new: a question asked or a result ended after the time it was taken off
as of (``active_dismissed``). A topic with no row and no pending question left is forgotten.

Where a topic is pinned lags one way: one pinned under 等你回答 or 有结果 whose topic now runs keeps that pin, in
its place, until it has run DEMOTE seconds (``buffered``: otherwise a topic asking a question after every one or two minute
run re-pins every pin above it each time). A new question or result is pinned at once; the counts
are the board's own.

``plan`` turns the order wanted and the record's into unpins, moves (unpin, pin: to the top) and pins. A move first
pins the message as it is: the create_time recorded back means it is still pinned; a new one means the owner took
it off, so it is unpinned again and its topic is ``dismissed`` as of the row it showed. The record,
<state>/pinned-topics.json: {schema_version, chat_id, since: when the board first ran, keep_since: when its
results first stayed once seen, stopped_since: when its stopped results were first kept, index_message_id: the 手上的事 message the pins were laid over, pins: [{message_id,
topic_id, group, at, create_time}] top to bottom, dismissed: {topic_id: {group, at}}, results: {topic_id: {at: when
its result ended, seen: when it was first seen, or null}}}, written after each step, keeping what ``dismiss`` wrote
meanwhile; a record without keep_since, stopped_since or results reads as one whose rules start now. In older
records a dismissed topic's value was its group (off while it stayed there): such a value is read so once. A new
手上的事 card is pinned above them all: every pin moves once more, on the next round. A round runs only when the
order differs, at most once every GAP seconds, longer after rounds that failed.
"""
from __future__ import annotations

import functools
import time
from pathlib import Path

from covey.core import env as cenv
from covey.app import butler_relay
from covey.app.status_cards import StatusCards, card_message
from covey.core import board_read, placement, projects
from covey.core.board_read import seconds, started_at, top_read
from covey.core.locks import resource_lock
from covey.core.butler import traits
from covey.core.state import read_object, write_json
from covey.core.topic_seen import enabled

RECORD = "pinned-topics.json"
GROUP_BOARDS = frozenset()  # Chats with a board of their own besides the owner's (configure_boards: the personal groups).
OVERVIEW_ENV = "COVEY_PINNED_OVERVIEW"  # app/overview.py ENV: it imports this module.
MAX_PINS = 10
QUESTION_AGE = board_read.QUESTION_AGE  # A question asked longer ago is not waiting on it.
GAP = 60
BACKOFF = 5  # Rounds that failed in a row double GAP, up to 2**BACKOFF times.
OLD_PIN = 60  # A pin created this long before the round: the message was pinned already.
DEMOTE = 300  # A pinned topic that drops to running keeps its pin this long.
GROUPS = ("waiting", "running", "result")  # Top to bottom.
RANK = {group: index for index, group in enumerate(GROUPS)}
NEWS = frozenset({"waiting", "result"})  # Such a row newer than its topic's dismissal brings the topic back.


def configure_boards(chats):
    """``chats`` (the personal groups, app/access.py) each keep a board of their own, in its own record
    (``record_path``), beside the owner's private chat's in RECORD; set where the harness config is read, in the
    Bridge and its task runners alike."""
    global GROUP_BOARDS
    GROUP_BOARDS = frozenset(chats)


def panel_message(state, chat, message) -> bool:
    """Whether ``message`` is a card the Bridge itself keeps at ``chat``'s top level: 手上的事 (app/overview.py), one
    of the last 早晚汇总 cards (app/digest.py) or an alert it sent there (``remember_notice``). No topic sent it, so a
    reply to it is the butler's (app/butler.py app_for: 回复谁的消息就交给谁, docs/ui.md 5.3), not a new topic opened
    on it. Never raises."""
    if not message:
        return False
    for name in ("pinned-overview.json", "digest.json", NOTICES):  # app/overview.py RECORD, app/digest.py RECORD
        record = read_object(record_path(state, chat, name))
        if record.get("message_id") == message or (isinstance(record.get("sent"), dict) and message in record["sent"]):
            return True
    return False


NOTICES = "top-notices.json"  # The alerts the Bridge sent at a chat's top level: {sent: {message id: when}}.
NOTICES_KEPT = 200  # The newest of them the record keeps.


def remember_notice(state, chat, message) -> bool:
    """``message``, an alert the Bridge sent at ``chat``'s top level (a node's, app/node_alerts.py; the memory
    watchdog's, app/memguard_alerts.py), kept as one of its own cards there (``panel_message``): a reply to it is the
    butler's, which starts on it as its settings say, not a topic opened on the alert to wait for 开始 on a launch card
    (2026-11-21 T622, a reply to a disk alert, waited so for 21 minutes). The newest NOTICES_KEPT are kept. The
    message is out already: False when it could not be kept, never raised."""
    if not (state and chat and str(message or "").startswith("om_")):
        return False
    path = record_path(state, chat, NOTICES)
    try:
        with resource_lock("top-notices", str(path)):
            record = read_object(path)
            sent = dict(record["sent"]) if isinstance(record.get("sent"), dict) else {}
            sent[str(message)] = time.time()
            newest = sorted(sent.items(), key=lambda item: seconds(item[1], 0.0))[-NOTICES_KEPT:]
            write_json(path, {"schema_version": 1, "sent": dict(newest)})
    except (OSError, TypeError, ValueError):
        return False
    return True


def record_path(state, chat, name=RECORD):
    """The board's record ``name`` (RECORD, app/overview.py's) for ``chat``: a chat of GROUP_BOARDS has its own
    (``pinned-topics.<chat_id>.json``), every other the one file as ever."""
    if chat in GROUP_BOARDS:
        stem, dot, suffix = name.rpartition(".")
        return Path(state) / f"{stem}.{chat}{dot}{suffix}"
    return Path(state) / name


def laid():
    """Whether the board is laid: COVEY_PINNED_TOPICS=1 and COVEY_PINNED_OVERVIEW=1 (手上的事's loop runs
    it, app/overview.py); its record's rules and results hold only then."""
    return enabled() and cenv.get(OVERVIEW_ENV, "").strip() == "1"


def kept(state, chat):
    """The topics the record keeps a result of for ``chat`` (its ``results``): the ``keep`` of the read ``look``
    judges (core/board_read.py read)."""
    record = read_object(record_path(state, chat))
    table = record.get("results") if record.get("chat_id") == chat else None
    return frozenset(table if isinstance(table, dict) else ())


def board(questions, jobs, seen, priority, now, since=0.0, relayed=None, *, read_top=0.0, keep_since=None,
          results=None, inline=None, stopped_since=None, cells=None, starts=None):
    """The topics as they would be pinned, top to bottom, each {topic, group, message_id, at, record}, a result's
    with ``seen`` (when the owner first saw it, None: not yet): ``at`` is when it was asked, ended or started;
    ``record`` the question's or job's record. The groups are core/board_read.py groups with these ``Rules``: a
    result that ended before ``since`` is not on it, nor one seen KEEP_SEEN ago (``seen_at``, ``read_top``,
    ``inline``; first seen as ``results``, the record's, says when it holds the same end). One that ended before
    ``keep_since`` (None: none did) is as in older releases: within RESULT_AGE, not seen in its topic, and not done at
    the top level (``relayed``, a job record -> bool: app/butler_relay.py relayed). A stopped one that
    ended before ``stopped_since`` (None: none did) is not on it: older releases left them out.
    ``cells``: a project's threads are one topic, the lead's (core/board_read.py groups). ``starts``: the topics
    waiting for 开始 on a launch card (core/board_read.py starts), under 等你回答 with that card."""
    return rows_of(board_read.groups(questions, jobs, seen, now, board_read.Rules(
        since, keep_since, stopped_since, results or {}, read_top, relayed, inline), cells, starts), priority)


def rows_of(found, priority):
    """The board's rows of ``found`` (core/board_read.py Groups), ranked."""
    rows = [entry(topic, "waiting", record["question_message_id"], record["created_at"], record)
            for topic, record in found.waiting.items()]
    # A task waiting for 开始 is 等我 as a question is (core/board_read.py starts): its launch card is the pin.
    rows += [entry(topic, "waiting", wait.card, wait.since, wait.record) for topic, wait in found.starting.items()]
    rows += [entry(topic, "running", record["card_message_id"], started_at(record), record)
             for topic, record in found.running.items()]
    rows += [{**entry(topic, "result", record["card_message_id"], at, record), "seen": first}
             for topic, (record, at, first) in found.results.items()]
    return ranked(rows, priority)


def ranked(rows, priority):
    """``rows`` by group; in each, prioritised topics first, the latest first; then the latest asked, ended or
    started."""
    return sorted(rows, key=lambda row: (-RANK[row["group"]], row["topic"] in priority, priority.get(row["topic"], 0),
                                         row["at"]), reverse=True)


def status_pinned(state, chat, topic) -> bool:
    """Whether ``topic`` (in ``chat``) is pinned by its status card (core/placement.py Row.status_card): not in the
    话题区 layout, nor one without a thread (app/status_cards.py root)."""
    return placement.plan(state, {"topic_id": topic, "chat_id": chat}).row.status_card


def aimed(state, chat, row):
    """``row`` with the message its topic's pin is: a result's, for a topic with a thread pinned by no status card
    (a 话题区's), the answer card its round sent into its thread (the job's answer_message_id), else its task card;
    every other row as ``board`` has it (the question card, the task card). A project's cell (core/projects.py cells: the
    row's topic is its lead's) is aimed by ``project_aimed``."""
    lead = projects.lead_record(state, row["topic"], chat)
    if lead:
        return project_aimed(state, chat, row, lead)
    answer = str((row.get("record") or {}).get("answer_message_id") or "")
    if (row["group"] != "result" or not answer.startswith("om_") or not traits(row["topic"], chat).has_thread
            or status_pinned(state, chat, row["topic"])):
        return row
    return {**row, "message_id": answer}


def project_aimed(state, chat, row, project):
    """A project's one cell: at work the project's card (the thread's task card is one of several), waiting the
    question card, done the conclusion (the answer card of the thread that ended, else the project's card); the
    row is as it was, with the lead's topic id, when the project has no card yet (the thread's own)."""
    card = projects.card_message(project)
    if row["group"] == "waiting" or (not card and row["group"] != "result"):
        return row
    if row["group"] == "running":
        return {**row, "message_id": card}
    thread = str((row.get("record") or {}).get("topic_id") or "")
    answer = str((row.get("record") or {}).get("answer_message_id") or "")
    if answer.startswith("om_") and thread and traits(thread, chat).has_thread and not status_pinned(state, chat, thread):
        return {**row, "message_id": answer}
    return {**row, "message_id": card} if card else row


def entry(topic, group, message_id, at, record):
    return {"topic": topic, "group": group, "message_id": message_id, "at": float(at), "record": record}


def choose(rows, dismissed, limit=MAX_PINS):
    """(the rows pinned, the rest): the first ``limit`` whose topic the owner did not take off; with more of them,
    the results the owner has seen give way first, the earliest seen first."""
    kept = [row for row in rows if row["topic"] not in dismissed]
    seen = sorted((row for row in kept if row.get("seen") is not None), key=lambda row: row["seen"])
    out = {row["topic"] for row in seen[:max(0, len(kept) - limit)]}
    pinned = [row for row in kept if row["topic"] not in out][:limit]
    return pinned, [row for row in rows if row not in pinned]


def active_dismissed(dismissed, rows, asking=frozenset()):
    """The topics of ``dismissed`` (the record's) still taken off, {topic: {group, at}}: not one with a question or
    result on ``rows`` newer than its ``at``, nor one without a row that is not in ``asking`` (the topics with a
    pending question). An older record's value, a group, holds while the topic is in it, as of its row there."""
    shown = {row["topic"]: row for row in rows}
    news = {}
    for row in rows:
        if row["group"] in NEWS:
            news[row["topic"]] = max(news.get(row["topic"], row["at"]), row["at"])
    kept = {}
    for topic, value in (dismissed if isinstance(dismissed, dict) else {}).items():
        row = shown.get(topic)
        if isinstance(value, str):
            if row and row["group"] == value:
                kept[topic] = {"group": value, "at": row["at"]}
            continue
        at = value.get("at") if isinstance(value, dict) else None
        if (not isinstance(at, (int, float)) or isinstance(at, bool) or news.get(topic, at) > at
                or (row is None and topic not in asking)):
            continue
        kept[topic] = value
    return kept


def buffered(rows, pins, priority, now):
    """``rows`` as they are pinned: a topic pinned under 等你回答 or 有结果 that has run less than DEMOTE seconds
    keeps that pin (its message, group and ``at``), so its place."""
    held = {pin.get("topic_id"): pin for pin in pins if pin.get("group") in NEWS and pin.get("message_id")
            and isinstance(pin.get("at"), (int, float)) and not isinstance(pin.get("at"), bool)}
    placed, kept = [], False
    for row in rows:
        pin = held.get(row["topic"])
        if row["group"] == "running" and pin and now - row["at"] < DEMOTE:
            row, kept = {**row, "group": pin["group"], "message_id": pin["message_id"], "at": float(pin["at"])}, True
        placed.append(row)
    return ranked(placed, priority) if kept else rows


def dismiss(state, chat, topics, at, group="", by=""):
    """Take ``topics`` off ``chat``'s board as of ``at`` (epoch seconds), under 等你回答 etc. when ``group`` says:
    the board's record changes, no job or question does. The topics that were not taken off already. ``by`` "card":
    the owner's 不管了 click (``undismiss`` and ``last_click`` know such an entry; a hand unpin or the butler's is not)."""
    path = record_path(state, chat)
    with resource_lock("pinned-topics", str(path)):
        record = read_object(path)
        dismissed = record.get("dismissed") if record.get("chat_id") == chat else None
        dismissed = dict(dismissed) if isinstance(dismissed, dict) else {}
        new = [topic for topic in topics if topic not in dismissed]
        dismissed.update({topic: {"group": group, "at": float(at), **({"by": by} if by else {})} for topic in topics})
        write_json(path, {**record, "schema_version": 1, "chat_id": chat, "dismissed": dismissed})
    return new


UNDO_AGE = 86400  # 手上的事 offers 撤销 for the owner's latest 不管了 this long.


def last_click(dismissed, now):
    """{topic: at} of the owner's latest 不管了 click still in ``dismissed`` (the entries ``dismiss`` wrote with
    by="card", the newest ``at`` and the topics sharing it: one click took them off together), within UNDO_AGE of
    ``now``; {} when there is none. Not what he unpinned by hand or the butler took off."""
    clicks = {topic: value["at"] for topic, value in (dismissed if isinstance(dismissed, dict) else {}).items()
              if isinstance(value, dict) and value.get("by") == "card" and isinstance(value.get("at"), (int, float))
              and not isinstance(value.get("at"), bool)}
    if not clicks or now - max(clicks.values()) >= UNDO_AGE:
        return {}
    last = max(clicks.values())
    return {topic: at for topic, at in clicks.items() if at == last}


def undismiss(state, chat, taken):
    """Put back what one 不管了 took off: each topic of ``taken`` ({topic: the ``at`` it was taken off at}) whose entry
    is still that one (``at`` the same). One taken off again since (another ``at``), or already back (its question
    was answered or retired, a newer one brought it back: ``active_dismissed``) is left as it is. The topics put back."""
    path = record_path(state, chat)
    with resource_lock("pinned-topics", str(path)):
        record = read_object(path)
        dismissed = record.get("dismissed") if record.get("chat_id") == chat else None
        if not isinstance(dismissed, dict):
            return []
        back = [topic for topic, at in taken.items() if isinstance(dismissed.get(topic), dict)
                and isinstance(dismissed[topic].get("at"), (int, float)) and abs(dismissed[topic]["at"] - at) < 0.001]
        if back:
            write_json(path, {**record, "dismissed": {topic: value for topic, value in dismissed.items() if topic not in back}})
    return back


def plan(wanted, current, stale=frozenset()):
    """The steps from the pins ``current`` (message ids, top to bottom) to ``wanted``: (op, message id) with op
    unpin, move (to the top) or pin. Those not wanted go first. The longest end of ``wanted`` that ``current``
    holds in the same order, none of it ``stale``, stays; the rest goes to the top one by one, bottom first."""
    wanted_set = set(wanted)
    steps = [("unpin", message) for message in current if message not in wanted_set]
    left = [message for message in current if message in wanted_set]
    keep = len(wanted)
    for message in reversed(left):
        if keep and message == wanted[keep - 1] and message not in stale:
            keep -= 1
    held = set(left)
    return steps + [("move" if message in held else "pin", message) for message in reversed(wanted[:keep])]


class PinnedTopics:
    def __init__(self, runtime, messenger, voice=None):
        self.runtime, self.messenger = runtime, messenger
        # A topic with a thread is pinned by its status card (app/status_cards.py) outside the 话题区 layout
        # (``status_pinned``); none without a voice to send.
        self.status = StatusCards(runtime, messenger, voice)
        self.rows = []  # The last look's board, top to bottom.
        self.ran_at = 0.0
        self.failures = 0
        self.since = None  # When the board first ran: the record's, else this process's first look.
        self.keep_since = None  # When results first stayed once seen: the record's, else this process's first look.
        self.stopped_since = None  # When stopped results were first kept: the record's, else this first look.
        self.results = {}  # The last look's results, {topic: {at, seen}}: the record's ``results`` once saved.
        self.dismissed = {}  # The last look's topics taken off, {topic: {group, at}}.
        self.asking = frozenset()  # The last look's topics with a pending question.
        self.unseen = {}  # The last look's 有结果未看, topic -> job record (core/board_read.py unseen).
        self.priority = {}
        self.read = {}  # The record's dismissed as the round read it: what ``dismiss`` wrote since is kept.

    enabled = staticmethod(enabled)

    def look(self, state, chat, now, read=None, *, board=True):
        """The board for ``chat`` now, kept for ``tick``: its rows, and the topics the record has a pin of in
        ``chat``; ``dismissed`` is then the topics taken off, ``unseen`` its results not seen yet
        (core/board_read.py unseen). ``read``: core/board_read.py read already done for ``chat`` at ``now``, used
        when its ``keep`` is the record's (``kept``), else read again. ``board`` False: as a deployment without the
        board would have it (board_read.Rules defaults: the record's rules, results and topics taken off unused), for
        ``board_results``."""
        record = read_object(record_path(state, chat))
        ours = record.get("chat_id") == chat
        if self.since is None:
            self.since = seconds(record.get("since"), now) if board else 0.0
        if self.keep_since is None:
            self.keep_since = seconds(record.get("keep_since") if ours else None, now) if board else None
        if self.stopped_since is None:
            self.stopped_since = seconds(record.get("stopped_since") if ours else None, now) if board else None
        table = record.get("results") if board and ours and isinstance(record.get("results"), dict) else {}
        if read is None or read.keep != frozenset(table):
            read = board_read.read(state, chat, now, keep=table)
        questions, self.priority = read.questions, read.priority
        found = board_read.groups(questions, read.jobs, read.seen, now, board_read.Rules(
            self.since, self.keep_since, self.stopped_since, table, top_read(read.seen, chat),
            functools.partial(butler_relay.relayed, state), functools.partial(butler_relay.inline_at, state)), read.cells,
            read.starts)
        self.rows = [aimed(state, chat, row) for row in rows_of(found, self.priority)]
        self.results = {row["topic"]: {"at": row["at"], "seen": row["seen"]} for row in self.rows
                        if row["group"] == "result"}
        self.asking = frozenset(row.get("topic_id") for row in questions if row.get("topic_id"))
        # An old record's topics taken off filter nothing once the board is off (r4 review P4).
        self.dismissed = active_dismissed(record.get("dismissed") if board and ours else None, self.rows, self.asking)
        self.unseen = board_read.unseen(found, self.dismissed)
        pins = record.get("pins") if ours and isinstance(record.get("pins"), list) else []
        return self.rows, {pin.get("topic_id") for pin in pins if isinstance(pin, dict) and pin.get("message_id")}

    def tick(self, ctx, chat, index, now):
        """One round toward the board ``look`` found, once 手上的事 (``index``, its record) is pinned in ``chat``."""
        if index.get("chat_id") != chat or not index.get("pinned_at"):
            return
        path = record_path(ctx.state_dir, chat)
        record = read_object(path)
        pins = [pin for pin in record.get("pins") or [] if isinstance(pin, dict) and pin.get("message_id")]
        dismissed = record.get("dismissed") if record.get("chat_id") == chat else None
        self.read = dict(dismissed) if isinstance(dismissed, dict) else {}
        dismissed = active_dismissed(dismissed, self.rows, self.asking)
        state = ctx.state_dir
        # A status card its topic no longer uses (the 话题区) keeps no place: its turn's card goes.
        held = [pin for pin in pins if status_pinned(state, chat, pin.get("topic_id") or "")
                or pin["message_id"] != card_message(state, pin.get("topic_id") or "")]
        pinned, _ = choose(buffered(self.rows, held, self.priority, now), dismissed)
        # A status card says where its topic is now, not where ``buffered`` keeps its pin.
        now_rows = {row["topic"]: row for row in self.rows}
        cards = self.status.ensure(ctx, chat, [{**now_rows.get(row["topic"], row),
                                                "started": started_at(now_rows.get(row["topic"], row).get("record") or {})}
                                               for row in pinned if status_pinned(state, chat, row["topic"])], now)
        pinned = [{**row, "message_id": cards[row["topic"]]} if row["topic"] in cards else row for row in pinned]
        groups = {row["message_id"]: row for row in pinned}
        current = [pin["message_id"] for pin in pins]
        laid = record.get("index_message_id") == index["message_id"]
        steps = plan([row["message_id"] for row in pinned], current, frozenset() if laid else frozenset(current))
        saved = {"schema_version": 1, "chat_id": chat, "since": now if self.since is None else self.since,
                 "keep_since": now if self.keep_since is None else self.keep_since,
                 "stopped_since": now if self.stopped_since is None else self.stopped_since, "results": self.results,
                 "index_message_id": record.get("index_message_id"),
                 "pins": [{**pin, "group": groups[pin["message_id"]]["group"], "at": groups[pin["message_id"]]["at"]}
                          if pin["message_id"] in groups else pin for pin in pins], "dismissed": dismissed}
        if not steps:
            if not laid or saved != record:
                self.save(path, {**saved, "index_message_id": index["message_id"]})
            return
        if now - self.ran_at < GAP * 2 ** min(self.failures, BACKOFF):
            return
        self.ran_at = now
        for op, message in steps:
            if not self.step(ctx, path, saved, op, message, groups.get(message), now):
                self.failures += 1
                self.runtime.log(f"pinned board failed op={op} message={message[-8:]} failures={self.failures}")
                return
        self.failures = 0
        self.save(path, {**saved, "index_message_id": index["message_id"]})

    def step(self, ctx, path, saved, op, message, row, now):
        """One step, ``saved`` brought up to date and written once it is done; False when a call failed."""
        pins = saved["pins"]
        held = next((pin for pin in pins if pin["message_id"] == message), None)
        topic = (row or {}).get("topic") or (held or {}).get("topic_id") or ""
        group = (row or {}).get("group") or (held or {}).get("group") or ""
        at = (row or {}).get("at", (held or {}).get("at"))
        if op == "unpin":
            if not self.messenger.unpin(ctx.profile, message):
                return False
            pins.remove(held)
            self.save(path, saved)
            self.log("unpin", message, topic, group)
            return True
        if op == "move":
            probe = self.messenger.pin_info(ctx.profile, message)
            if probe is None:
                return False
            if held.get("create_time") and str(probe.get("create_time") or "") != held["create_time"]:
                # Pinned anew: the owner had taken it off. Off again, until its topic has something newer than
                # what the pin showed. Saved first, so a round after an unpin that failed unpins it.
                pins.remove(held)
                pins.insert(0, {**held, "create_time": str(probe.get("create_time") or "")})
                saved["dismissed"][topic] = {"group": group, "at": float(now if at is None else at)}
                self.save(path, saved)
                if not self.messenger.unpin(ctx.profile, message):
                    return False
                pins.pop(0)
                self.save(path, saved)
                self.log("dismissed", message, topic, group)
                return True
            return self.repin(ctx, path, saved, held, message, topic, group, at)
        pin = self.messenger.pin_info(ctx.profile, message)
        if pin is None:
            return False
        created = str(pin.get("create_time") or "")
        held = {"message_id": message, "topic_id": topic, "group": group, "at": at, "create_time": created}
        pins.insert(0, held)
        self.save(path, saved)
        if created.isdigit() and int(created) / 1000 < now - OLD_PIN:
            # Pinned already (by hand, or by a round whose record was lost): where it was, so to the top.
            return self.repin(ctx, path, saved, held, message, topic, group, at)
        self.log("pin", message, topic, group)
        return True

    def repin(self, ctx, path, saved, held, message, topic, group, at):
        """Unpin and pin ``message`` again, to the top."""
        pins = saved["pins"]
        if not self.messenger.unpin(ctx.profile, message):
            return False
        pins.remove(held)
        self.save(path, saved)
        pin = self.messenger.pin_info(ctx.profile, message)
        if pin is None:
            return False
        pins.insert(0, {"message_id": message, "topic_id": topic, "group": group, "at": at,
                        "create_time": str(pin.get("create_time") or "")})
        self.save(path, saved)
        self.log("move", message, topic, group)
        return True

    def log(self, op, message, topic, group):
        self.runtime.log(f"pinned board {op} message={message[-8:]} topic={topic} group={group}")

    def save(self, path, record):
        """``record`` written, with what ``dismiss`` wrote since the round read the record (added to ``record``
        too, for the round's next writes)."""
        with resource_lock("pinned-topics", str(path)):
            disk = read_object(path)
            theirs = disk.get("dismissed") if disk.get("chat_id") == record["chat_id"] else None
            for topic, value in (theirs if isinstance(theirs, dict) else {}).items():
                if isinstance(value, dict) and self.read.get(topic) != value:
                    record["dismissed"][topic] = value
            for topic in list(record["dismissed"]):
                if topic in self.read and topic not in (theirs if isinstance(theirs, dict) else {}):
                    record["dismissed"].pop(topic)  # Put back (``undismiss``) since the round read it.
            write_json(path, record)


def board_results(state, chat, now, read=None):
    """(有结果未看 of ``chat`` now, topic -> job record; the topics the owner took off the board, {topic: {group,
    at}}), as the board's own look finds them (PinnedTopics.look: the same record, rules and ``active_dismissed``;
    ``read`` as there), for a reader without the board's loop (app/overview.py, app/digest.py, app/butler.py,
    app/butler_tools.py). Without the board's loop (``laid``) its rules' defaults. Only reads: no Feishu call, no
    write."""
    looked = PinnedTopics(None, None)
    looked.look(state, chat, now, read, board=laid())
    return looked.unseen, looked.dismissed


def taken_off(state, chat, now, read=None):
    """The topics of ``chat`` the owner took off the board, now, {topic: {group, at}} (``board_results``)."""
    return board_results(state, chat, now, read)[1]
