"""Continuing runs and guards of a project (docs/architecture.md 专项, phase 2's P2-6, COVEY_PROJECTS=1): what wakes the lead
when something happened since it last looked, and the mechanical checks that stop a project that runs on by itself.

- ``ProjectLoop`` (app/unified.py PARTS ``project_loop``, a feature object: ``threads()``, none without COVEY_PROJECTS=1): a
  thread that looks at every active project each ``TICK`` seconds. A project with receipts nobody has read (the inbox,
  app/project_lead.py) and no lead turn queued, running or waiting for the owner (``lead_busy``: a turn that only waits for
  the answer to its project_ask does not count, W1-A C4 ③) gets one wake-up turn, queued like any message of the lead's
  topic (``enqueue_chat_job``, message id ``lead-<project id>-<n>``, ``n`` kept in the record's ``wakes``) with the source
  ``wake`` (WAVE12 C1: it answers no question the lead waits on, C4 ①). A ``start`` receipt always wakes; the others only
  with COVEY_PROJECT_AUTOWAKE=1. After a restart the same look finds the receipts on disk again (``take_receipts`` gives
  back what a turn that did not end well had taken). ``lead_free`` tells app/triage.py whether the lead can take a member's
  question now; when it cannot, the question goes to the owner at once.
- Guards, any of which pauses the project with ``state_reason`` ``tokens``, ``wakes`` or ``stalled`` (core/projects.py
  GUARD_REASONS) and wakes it no more: the lead's and the members' tokens over COVEY_PROJECT_BUDGET, only when a deployment
  sets it (``tokens_used`` is added once per task by ``after_runner`` all the same, the 专项卡 shows it); 30 wake-ups in a row with nobody stepping in, or 12 in the last hour; no task and no
  receipt for 30 minutes, which first wakes the lead with a ``stalled`` receipt and, as long again with nothing moving,
  pauses (only a project with something to do, ``has_work``, can stall). The owner's own turn in the lead's topic (``owner_turn``) clears the counts and starts a project a guard or the
  owner's stop paused (a ``tokens`` pause gets a new budget from there, ``token_floor``).
- A project with nothing to do (``has_work``: no task queued, running or waiting for the owner in the lead's or a member's topic, no
  question waiting, no receipt unread) and nothing said or done for COVEY_PROJECT_IDLE_DAYS days (default 7, ``fold``) is
  folded away: PAUSED with ``state_reason`` ``idle`` (``projects.IDLE``) and ``idle`` ({at, days}); no pause card, no question. The owner's own
  turn in the lead's topic, his word in a member's topic (``heard``) or the butler's ``project_send`` (all go through ``reopen``)
  open it again; ``idle_reopened_at`` keeps it from folding again before something has happened. With MAX_ACTIVE projects
  on, a new one (the butler's start, an upgrade, 整理专项's 建 and 拆) first folds the quietest one that rests and has been
  quiet a day (``make_room``) instead of being refused; only when none can be folded is it refused, naming the quietest.
- A project its lead finished (``done``) opens again when the owner speaks to it (``owner_spoke``, ``unfinish``): his turn in
  the lead's topic, the butler's ``project_send``, the web page; there is more to do. His word in a member's topic
  (``heard``) is that topic's and leaves a finished project finished (W2-C; one folded away still opens). ``owner_at``
  keeps when he last spoke in the project, ``owner_lead_at`` when he last spoke to it (app/project_tools.py finish_gate).
- With COVEY_PROJECT_ASK=1 a pause a guard made is also put to the owner (``pause``): the record gets ``pause_ask`` ({reason,
  why, at, choice, card}), app/project_cards.py sends the card (继续 / 收尾 / 停) and ``answer_pause`` is what a click does
  to the record. A ``tokens`` pause also stops the member topics' running tasks; a ``wakes`` pause leaves them running; a
  ``stalled`` one has none. 继续 and 收尾 start the project again (``reopen``, what the owner's own turn does too) and leave an
  ``owner_choice`` receipt, which always wakes the lead.
- Receipts: ``after_runner`` (the Bridge, once a task's runner is gone: rule 8 keeps a task process to its own card and
  record) writes ``thread_end`` (the receipt kind keeps its name on disk) for a member's turn that ended and counts the tokens; ``heard`` (the Bridge, on the owner's
  message in a member's topic) writes ``owner_said``; the loop writes ``stalled``; the Bridge's sweep of the held questions (app/triage.py, COVEY_PROJECT_TRIAGE=1) writes ``question``.
- A lead that a wake would only see fail on a limit is not woken (``limit_wait``): its latest turn failed on a rate limit,
  a spent quota or a passing server fault and its automatic retry is to come (app/auto_retry.py, at the reset when one is
  known), or none is to come and it ended less than LIMIT_REST ago, or its model draws on a gateway quota that is held
  (core/quota_hold.py), or its latest turns did not start (``backend_down``: its Harness's login check or program failed,
  its task runner did not start), or failed as the next would (``stuck``: a refusal, a model the account cannot use, a
  login, or nothing done at all), and the latest ended less than DOWN_LADDER says ago. The receipts wait in the inbox; the
  retry, or the wake after the wait, reads them all in one turn, as any lead turn takes them. A receipt of the owner's own
  doing that came after that turn (``start``, ``owner_choice``) still wakes it, once. A wake-up that does not start after
  one that did not either tells the owner nothing (``quiet_start``): he was told when the first did not.
- ``silent`` (app/turn_delivery.py TurnDelivery): the reply of a wake-up turn the owner was not in (``woken``) is not sent,
  done or waiting for the answer to what it asked (its question cards go all the same); its report notes that its style
  lets through (``project_lead.reportable``) go instead, as one message under who says it (``report_out``, W1-A C4: the
  three channels are project_ask, report notes, and the reply only when the owner is there). A member's turn the owner
  waits for (the lead handed it on while he was there) has its result sent to him by its relay (app/relay_targets.py
  awaited_target): a wake-up turn that read only such receipts (its relay read when the lead reports: on its way, sent or
  maybe sent, ``receipt_told``; and consults' ``collab_answered`` ones, which are the integrator's to deliver) sends none of
  its ``thread`` or ``note`` notes (``told_already``): one result, told once; one whose relay failed is the report's to tell.
- 主人在场 (WAVE12 C5) is read in one place: ``woken`` (core/projects.py woken on the job and on its record, ``present``):
  a wake-up is one by its ``wake`` source (or, queued before the stamp, its message id), and the owner is in it once a
  supplement of his is about to go into it (``owner_steered``, written before the supplement is sent and taken back when it
  surely did not go, P1-8) or its source carries his message.
- With 协作 on (COVEY_COLLAB=1, docs/projects.md 负责人对照目标跟进): ordinary receipts that come close together wake the lead
  once (``settling``: it waits until none has come for ``batch_seconds``, at most BATCH_STRETCH times as long; a ``start``,
  the owner's choice, a held question or a 协作 gap wakes at once), and a consult's turn (``collab_ask``, in its 咨询通道)
  has its tokens counted in the project of its collaboration's integrator, else of its helper (``consult_project``), once,
  so consults spend the same budget (COVEY_PROJECT_BUDGET). It still leaves no ``thread_end`` and adds nothing to the library.

Rules 1 and 5 (AGENTS.md): records are written under their own lock, nothing is sent to Feishu from under it, and the only
reply that goes out is the saved, authorized one."""
from __future__ import annotations

import re
import threading
import time
from collections import namedtuple
from contextlib import contextmanager
from pathlib import Path

from covey.app import auto_retry, butler_harness, project_context, project_lead, triage as triage_flow
from covey.app.artifacts import write_result_file
from covey.core import board_read, collab, collab_records, failure, project_checklist, projects, quota_hold, triage, turn_source
from covey.core import env as cenv
from covey.core.checkpoint import SUMMARY_CHARS, read_checkpoint, turn_summary
from covey.core.history_storage import append_history
from covey.core.jobs import (find_job_record, job_field, job_key, job_path, read_job_record, topic_job_records,
                              update_job_record_if)
from covey.core.loops import Stuck, Worker
from covey.core.state import read_object
from covey.core.statuses import ENDED, NOT_ENDED, RUNNING
from covey.core.store import directory_store, one_scan
from covey.core.text import cut
from covey.core.topics import load_topic, remember_topic_message, topic_label
from covey.core.turn_words import RESULTS

TICK = 10  # Seconds between two looks.
# A look runs in a Worker (core/loops.py). One with no progress (the sweep's next question, a project looked at) in STUCK_SECONDS
# is left behind: an NFS open can hold its thread for good, and with it every wake-up and every hand-over to the owner (T726,
# 2026-11-24: 12:13:14 the sweep wrote a held question's receipt, then nothing until the Bridge restarted at 12:35: no wake,
# and the 12:16:03 deadline passed). The next look runs in a new thread and leaves out the project the old one was stuck on
# while that thread lives; a left-behind look that comes back changes nothing (``behind``).
STUCK_SECONDS = 60
SLOW_SECONDS = 10  # A look that took longer is logged (``project loop slow``), at most once a SLOW_LOG seconds.
SLOW_LOG = 300
MAX_LEFT = 3  # Left-behind looks still stuck: with this many the next waits for one to end, at most LEFT_WAIT seconds.
LEFT_WAIT = 300
START_WAIT = 20  # After the Bridge starts: its own recovery of interrupted turns goes first.
HOUR = 3600
WAKE_TEXT = ("[Bridge 唤醒] 专项状态里的收件箱有新的回执，主人不在场：按回执判断下一步。"
             "要主人定的事用 project_ask 问他（出问题卡，任何汇报风格都发）；进展用 project_note 标 report，Bridge 按汇报风格把笔记原文发给主人。"
             "这一轮的回复不发给主人（主人中途插话时才发），要他知道的写进 project_note 或 project_ask。")
TOKEN_JOBS_KEPT = projects.TOKEN_JOBS_KEPT
EXECUTING = frozenset({"running", "cancel_requested"})
COUNTED = ENDED | {"waiting_user"}  # A turn waiting for the owner's answer has used its tokens too.
RECEIPT_ENDS = ("done", "failed", "cancelled")  # What a member's receipt says; an interrupted turn goes on by itself.
SAID = 300  # Characters of the owner's words in an ``owner_said`` receipt.
RECEIPT_CHARS = 500  # Of a receipt's text: app/project_lead.py add_receipt keeps no more (a member's receipt keeps its path).
RETOLD = ("thread", "note")  # The report kinds that would tell a result the owner was sent again; a milestone or finish goes on.
ALWAYS_WAKE = ("start", "owner_choice", "question")  # The receipts that wake the lead without COVEY_PROJECT_AUTOWAKE=1.
CHOICE_RECEIPT = {
    "continue": "主人看了停住的原因（{why}），选了继续：专项已经重新开始，叫醒次数清零。接着按状态包判断下一步。",
    "finish": "主人看了停住的原因（{why}），选了收尾：这个专项不再开新的话题。请总结做完了什么、还有什么没做完，"
              "用 project_note 标 report 告诉主人，然后调 project_finish（还有话题在做时它会拒绝，等它们结束再收尾）。",
}
# With 协作 on the 收尾 receipt says how the 目标检查表 is left: the owner chose to stop, not that the rest was met. A drop names
# his word for it (W2-C, core/project_checklist.py BASIS): on the pause card that is his choice (CHOICE_BASIS).
CHOICE_CHECKLIST = ("目标检查表里没满足的条目标 dropped，gap 写“主人选了收尾，没做完：还差什么”，evidence 写主人的依据；"
                    "没处理的协作缺口用 close_gaps 写同样的话。不要把没做完的标成 done。")
CHOICE_BASIS = "这次的依据是主人在停住卡上选的收尾：evidence 写 {ref}。"
BATCH_ENV = "COVEY_PROJECT_BATCH_SECONDS"  # 协作 on: how long ordinary receipts are gathered before they wake the lead.
BATCH_SECONDS = 20
BATCH_STRETCH = 4  # The oldest receipt of a batch waits at most this many windows, however many keep coming.
LIMIT_REST = 600  # A lead turn that failed on a limit with no retry to come: the next wake waits this long after it ended.
OWN_WAKES = ("start", "owner_choice")  # The owner's own doing: wakes the lead once even while it waits out a limit.
RETRY_COMING = ("pending", "submitting")  # app/auto_retry.py AUTO_RETRY_TRANSITIONS: the retry has not been queued yet.
DOWN_LADDER = (60, 180, 600)  # The lead's turns that did not start, 1, 2, 3 or more in a row: the next wake waits this long.
# A lead's turn that failed in a way the next one fails the same (``stuck``): a refusal of its content, a model its
# account cannot use, a login or an account block. T872, 2026-11-27: a lead on a model its account had lost woke every
# 10 s, 12 failed turns in two minutes each time, until the wake guard paused the project.
WONT_HEAL = frozenset({"policy_refusal", "model_unavailable", "authentication_error", "model_blocked"})


def wakes_of(record) -> dict:
    """The record's ``wakes`` ({count, last_at, streak, recent}) with each missing or invalid part at none."""
    given = record.get("wakes") if isinstance(record.get("wakes"), dict) else {}
    number = lambda value: value if type(value) is int and value >= 0 else 0  # noqa: E731
    recent = [float(t) for t in given.get("recent") or () if isinstance(t, (int, float)) and not isinstance(t, bool)]
    return {"count": number(given.get("count")), "streak": number(given.get("streak")), "recent": recent,
            "last_at": float(given["last_at"]) if isinstance(given.get("last_at"), (int, float)) else 0.0}


def hold(record, now) -> str:
    """The guard a project is over now ('' for none): ``tokens`` (only with a budget, COVEY_PROJECT_BUDGET), or ``wakes`` (30
    in a row, or 12 in the last hour)."""
    floor = record.get("token_floor") if type(record.get("token_floor")) is int else 0
    used = record.get("tokens_used") if type(record.get("tokens_used")) is int else 0
    limit = projects.token_limit()
    if limit and used - floor >= limit:
        return "tokens"
    wakes = wakes_of(record)
    if wakes["streak"] >= projects.DEFAULT_WAKES or sum(1 for t in wakes["recent"] if now - t < HOUR) >= projects.MAX_WAKES_HOUR:
        return "wakes"
    return ""


def record_tokens(record) -> int:
    """A task's tokens: its input (cache included) and output, and its subtasks' (core/trace.py ``turn_usage`` counts them
    so); a count that is not a non-negative int adds nothing."""
    report = record.get("subtask_usage") if isinstance(record.get("subtask_usage"), dict) else {}
    levels = report.get("levels") if report.get("count") and isinstance(report.get("levels"), list) else []
    parts = [record, *(level for level in levels if isinstance(level, dict))]
    return sum(value for part in parts for name in ("input_tokens", "output_tokens")
               if type(value := part.get(name)) is int and value >= 0)


# Receipts that no longer change: taken by a lead turn that ended well and is over (project_lead.take_receipts never takes such a
# one again). Read once per process instead of every TICK: an inbox keeps every receipt, a hundred and more each.
_settled = {}  # inbox directory -> {file name: receipt}
_settled_lock = threading.Lock()
SETTLED = project_lead.GOOD_ENDS & ENDED


def inbox(state_dir, project_id) -> list:
    """[(receipt, unread)] of every receipt of the project's inbox, oldest first; ``unread``: no lead turn has read it (or one that
    did not end well took it). The settled ones (``_settled``) are not read again."""
    folder = project_lead.inbox_dir(state_dir, project_id)
    try:
        paths = sorted(folder.glob("*.json"))
    except OSError:
        return []
    with _settled_lock:
        known = dict(_settled.get(str(folder)) or {})
    found, settled = [], {}
    for path in paths:
        item = known.get(path.name)
        if item is not None:
            settled[path.name] = item
            found.append((item, False))
            continue
        item = read_object(path)
        taker = item.get("taken_by")
        status = (read_job_record(job_path(state_dir, taker)) or {}).get("status") if taker else None
        done = bool(item.get(project_lead.RECEIPT_DONE))  # Settled by the Bridge (project_lead.settle_receipt): read by nobody now.
        if status in SETTLED or done:
            settled[path.name] = item
        # project_lead.took_it with no turn of its own: untaken, or taken by a turn that did not end well.
        found.append((item, bool(item.get("id")) and not done
                      and (not taker or (status not in project_lead.GOOD_ENDS and status not in RUNNING))))
    with _settled_lock:
        _settled[str(folder)] = settled  # Only the files still there.
    return found


def unread(state_dir, project_id) -> list:
    """The inbox's receipts no lead turn has read (or one that did not end well took), oldest first."""
    return [item for item, fresh in inbox(state_dir, project_id) if fresh]


_memo = threading.local()  # .jobs: {topic: its job records} while ``remembered`` holds (one project's look).


@contextmanager
def remembered():
    """Inside, each topic's job records are read once (``jobs_of``): a project's look asks for the same topics up to five times."""
    before = getattr(_memo, "jobs", None)
    _memo.jobs = {} if before is None else before
    try:
        yield
    finally:
        _memo.jobs = before


def jobs_of(state_dir, topic) -> list:
    """``topic``'s job records, once per ``remembered`` block."""
    cache = getattr(_memo, "jobs", None)
    if cache is None:
        return topic_job_records(state_dir, topic)
    if topic not in cache:
        cache[topic] = topic_job_records(state_dir, topic)
    return cache[topic]


def open_turns(state_dir, topics) -> bool:
    """Whether any of ``topics`` has a task not ended: queued, running, or waiting for the owner."""
    return any(record.get("status") in NOT_ENDED for topic in topics for record in jobs_of(state_dir, topic))


ASK_ORIGIN = "project_ask"  # A question record's ``origin`` when the lead asked it with project_ask (app/project_tools.py).


def asks_owner(state_dir, record) -> bool:
    """Whether the lead's task ``record`` only waits for the owner's answer to what it asked with project_ask (W1-A C4 ③): it
    is ``waiting_user`` and a question of it still pending says ``origin`` ASK_ORIGIN. Such a turn is over as far as the lead
    goes: it neither holds a wake-up back nor keeps a member's question from the lead."""
    if record.get("status") != "waiting_user":
        return False
    key = str(record.get("key") or job_key(record.get("message_id"), record.get("job_id")))
    return any(True for _ in directory_store(Path(state_dir) / "questions").query(
        status="pending", where=lambda row: row.get("job_key") == key and row.get("origin") == ASK_ORIGIN))


def lead_busy(state_dir, lead) -> bool:
    """Whether the lead's topic ``lead`` has a turn that keeps a wake-up back: one queued, running or waiting for the owner,
    but not one that only waits for the answer to a project_ask question (``asks_owner``): the owner may take his time, and
    the receipts and the members' questions meanwhile go to the lead as ever (W1-A C4 ③)."""
    return any(record.get("status") in NOT_ENDED and not asks_owner(state_dir, record) for record in jobs_of(state_dir, lead))


def lead_free(state_dir, record, now=None) -> bool:
    """Whether the lead of the project ``record`` can take a member's question now (app/triage.py ``_route``, W1-A C4 ③): no
    turn of it busy (``lead_busy``), no guard over it (``hold``) and no wait for a limit or a backend down (``limit_wait``).
    When it cannot, the question goes to the owner at once instead of waiting its three minutes for a lead that will not
    come. Never raises: False when the record or the lead's turns cannot be read (the owner's card is the safe answer); a
    wait that cannot be worked out (the lead's topic.json unreadable) holds nothing, as before W1-A (the three minutes do)."""
    try:
        now = time.time() if now is None else now
        lead = str(record.get("lead_topic_id") or "")
        if not lead or record.get("state") != projects.ACTIVE or lead_busy(state_dir, lead) or hold(record, now):
            return False
    except Exception:  # noqa: BLE001 - see above
        return False
    try:
        return not limit_wait(state_dir, record, [], now)[0]
    except Exception:  # noqa: BLE001 - see above
        return True


triage_flow.LEAD_FREE = lead_free  # app/triage.py _route asks it (it may not import this module: no import cycle).


def has_question(state_dir, topics) -> bool:
    """Whether any of ``topics`` has a question card shown and not answered."""
    return any(True for topic in topics for _ in directory_store(Path(state_dir) / "questions").query(topic_id=topic, status="pending"))


def has_work(state_dir, record, pending) -> bool:
    """Whether ``record``'s project has something to do: a task queued, running or waiting for the owner in the lead's or a member's
    topic, a question waiting for an answer, or a receipt (``pending``: the unread ones) other than the stall reminder."""
    topics = [str(record.get("lead_topic_id") or ""), *(topic for topic in record.get("threads") or () if isinstance(topic, str))]
    return open_turns(state_dir, topics) or has_question(state_dir, topics) or any(item.get("kind") != "stalled" for item in pending)


def movable(state_dir, topics, pending) -> bool:
    """Whether the project has work the lead or the Bridge can move: a task queued, or a receipt (``pending``: the unread ones)
    other than the stall reminder. A task or question waiting for the owner is work, so the project does not fold away, but it is
    the owner's to move, not the lead's: a project that only waits for the owner never stalls (the owner sees the question card)."""
    if any(item.get("kind") != "stalled" for item in pending):
        return True
    return any(record.get("status") == "queued" for topic in topics for record in jobs_of(state_dir, topic))


def running_turns(state_dir, topics) -> bool:
    """Whether any of ``topics`` has a task executing (one still queued or waiting for the owner's answer is work that is not moving)."""
    return any(record.get("status") in EXECUTING for topic in topics for record in jobs_of(state_dir, topic))


def last_activity(state_dir, record, receipts) -> float:
    """When something last happened in the project that was not the lead's own wake-up: a member's task or the lead's
    turn the owner started ending, a receipt other than ``stalled`` coming in, the project beginning."""
    lead = str(record.get("lead_topic_id") or "")
    times = [float(record.get("created_at") or 0)]
    for topic in (lead, *(record.get("threads") or ())):
        for job in jobs_of(state_dir, topic):
            if topic == lead and projects.woken(job):
                continue
            times.append(board_read.ended_at(job))
    times += [float(item.get("at") or 0) for item in receipts if item.get("kind") != "stalled"]
    return max(times)


def receipts(state_dir, project_id) -> list:
    """Every receipt of the project's inbox, read or not: a member's end the lead has read is activity all the same."""
    return [item for item, _ in inbox(state_dir, project_id)]


def resting(state_dir, record, pending) -> bool:
    """Whether ``record``'s project has nothing the lead or the Bridge could move: nothing to do, or only the owner's answer
    awaited (``ProjectLoop.look`` folds such a one away after the idle days, ``rest``)."""
    topics = [str(record.get("lead_topic_id") or ""), *(record.get("threads") or ())]
    return not has_work(state_dir, record, pending) or not (running_turns(state_dir, topics) or movable(state_dir, topics, pending))


def quiet_since(state_dir, record) -> float:
    """When something last happened in the project, its opening again after a fold counted (what the idle days run from)."""
    return max(last_activity(state_dir, record, receipts(state_dir, record["id"])), float(record.get("idle_reopened_at") or 0))


# Making room for a new project when MAX_ACTIVE are on (``make_room``): the quietest active project that has rested this
# long is folded away now instead of after the idle days, so a project that has gone still does not keep a new one out.
ROOM_IDLE = 86400
QUIETEST_SHOWN = 3  # The quietest projects a refusal names, for the owner to wrap up or stop.
Room = namedtuple("Room", "ok folded words")  # ok: a new project may begin; folded: the name of the one folded for it; words: why not.


def quietest(state_dir, now=None) -> list:
    """[(seconds quiet, record, whether it rests)] of the active projects (``quiet_since``, ``resting``), the quietest
    first: what ``make_room`` folds and what a refusal names."""
    now = time.time() if now is None else now
    found = []
    for record in projects.records(state_dir):
        if projects.state_of(record) != projects.ACTIVE:
            continue
        found.append((now - quiet_since(state_dir, record), record, resting(state_dir, record, unread(state_dir, record["id"]))))
    return sorted(found, key=lambda item: item[0], reverse=True)


def room_possible(state_dir, now=None) -> bool:
    """Whether a new project could begin, without changing anything: fewer than MAX_ACTIVE are on, or one of them could be
    folded away for it (what a proposal of 整理专项 asks before it is shown)."""
    if projects.active_count(state_dir) < projects.MAX_ACTIVE:
        return True
    return any(rests and quiet >= ROOM_IDLE for quiet, _, rests in quietest(state_dir, now))


def make_room(state_dir, log=None, now=None) -> Room:
    """Whether a new project may begin. Fewer than MAX_ACTIVE on: yes. Else the quietest active project that rests and has been
    quiet ROOM_IDLE at least is folded away as the idle fold does it (PAUSED, ``idle`` with ``by`` "room" and the days it
    rested; its card becomes 收起 and new work opens it again), and its name is in ``folded``. None to fold: ``words`` says
    so and names the quietest ones, for the owner to wrap up or stop. Records only (rule 1): the caller writes the new
    project after."""
    if not projects.enabled() or projects.active_count(state_dir) < projects.MAX_ACTIVE:
        return Room(True, "", "")
    now = time.time() if now is None else now
    ranked = quietest(state_dir, now)
    for quiet, record, rests in ranked:
        if not rests or quiet < ROOM_IDLE:
            continue
        days, done = max(1, int(quiet // 86400)), []

        def change(data):
            if data.get("state") != projects.ACTIVE:
                return
            data["state"], data["state_reason"] = projects.PAUSED, projects.IDLE
            data["idle"] = {"at": now, "days": days, "by": "room"}
            data.pop("stall", None)
            done.append(True)
        try:
            projects.update(state_dir, record["id"], change)
        except (KeyError, ValueError, OSError):
            continue
        if done:
            if log is not None:
                log(f"project folded away to make room project={str(record['id'])[:8]} days={days}")
            return Room(True, cut(record.get("name"), 30) or "专项", "")
    named = "、".join(f"「{cut(record.get('name'), 20) or '专项'}」（{quiet_words(quiet)}没有动静）"
                     for quiet, record, _ in ranked[:QUIETEST_SHOWN])
    return Room(False, "", f"已经有 {projects.MAX_ACTIVE} 个专项在做，都还有事在做或刚动过，没有能收起腾位置的"
                           + (f"；最久没动静的是{named}，收尾或停掉一个就能再开" if named else ""))


def quiet_words(seconds) -> str:
    """How long a project has been quiet, in the owner's words: N 天, else N 小时, else 不到 1 小时."""
    seconds = max(0, int(seconds))
    if seconds >= 86400:
        return f"{seconds // 86400} 天"
    return f"{seconds // 3600} 小时" if seconds >= 3600 else "不到 1 小时"


def room_words(room) -> str:
    """The sentence a new project's answer adds when ``make_room`` folded a project away for it; '' when none was."""
    return f"为腾位置收起了最久没动静的专项「{room.folded}」，有新的事它会自动展开。" if room.folded else ""


def report_rows(state_dir, project_id, record, key) -> list:
    """The report notes of the lead's turn ``key`` that go to the owner (``report_out``): ``report`` true and of a kind the
    project's style lets through (project_lead.reportable), the RETOLD ones left out when the owner was sent the member's
    result itself (``told_already``): one result, told once."""
    rows = [row for row in record.get("notes") or ()
            if isinstance(row, dict) and row.get("job") == key and row.get("report") is True
            and project_lead.reportable(record.get("style"), row.get("kind")) and str(row.get("text") or "").strip()]
    if any(row.get("kind") in RETOLD for row in rows) and told_already(state_dir, project_id, key):
        rows = [row for row in rows if row.get("kind") not in RETOLD]
    return rows


def reported(state_dir, topic_id, key) -> bool:
    """Whether the turn ``key`` of the lead topic ``topic_id`` recorded a report note that goes to the owner (``report_rows``):
    its project's style lets it through and, a ``thread`` or ``note`` one (RETOLD), the owner was not sent the member's result
    itself (``told_already``). What a wake-up turn changed (``acted``) no longer counts (W1-A C4): the owner hears of a
    wake-up only through its report notes (``report_out``) and its questions (project_ask). The web page shows such a turn's
    reply by it (app/web_collect.py)."""
    found = projects.topic_project(state_dir, str(topic_id or ""))
    record = projects.read_record(state_dir, found.id) if found is not None else {}
    return found is not None and bool(report_rows(state_dir, found.id, record, key))


def present(state_dir, job) -> bool:
    """Whether the owner's words went into ``job`` (a job or its record) as it ran (``projects.OWNER_PRESENT``, on the job
    or, as a task runner's job has it, on its record: ``owner_steered`` writes it in the Bridge, before the supplement
    goes, P1-8), or its record is of a turn he is in all the same (``projects.woken`` false: his message in a wake-up's
    source, WAVE12 C5). Never raises."""
    if job_field(job, projects.OWNER_PRESENT) is True:
        return True
    try:
        record = read_job_record(job_path(Path(state_dir), job_key(job_field(job, "message_id"), job_field(job, "job_id"))))
    except (OSError, ValueError):
        return False
    return isinstance(record, dict) and (record.get(projects.OWNER_PRESENT) is True
                                         or projects.wake_turn(record) and not projects.woken(record))


def woken(state_dir, job) -> bool:
    """Whether ``job`` is a lead's wake-up turn the owner was not in (``projects.woken``, with its record read: ``present``):
    the one read of 主人在场 (WAVE12 C5) for ``silent``, app/web_collect.py and, on the record, app/relay_targets.py."""
    return projects.woken(job) and not present(state_dir, job)


def owner_steered(state_dir, job, log=None) -> bool:
    """The owner's words go into ``job``, a supplement it is about to take (app/steering.py deliver_steer: his message in the
    topic, the butler's project_send, the web page). A project lead's wake-up turn is his now (W0-2): its record says
    ``owner_present``, so its reply is sent (``silent``) and what it hands out relays as his turn's does, and the wake-ups
    count afresh (``owner_spoke``), as his own turn's would. Written before the supplement is sent (P1-8: a turn that took it
    and ended at once was judged a wake-up the owner was not in, its reply held back and no new turn queued); one that then
    does not go is taken back (``owner_unsteered``). Whether this call wrote the mark (False for any other job, or one marked
    already). Never raises."""
    try:
        message, topic = str(job_field(job, "message_id")), str(job_field(job, "topic_id"))
        if not projects.enabled() or not projects.wake_turn(job) or job_field(job, projects.OWNER_PRESENT) is True:
            return False
        if not projects.is_lead_topic(state_dir, topic, str(job_field(job, "chat_id")) or None):
            return False
        if present(state_dir, job):
            return False
        update_job_record_if(Path(state_dir), job_key(message, job_field(job, "job_id")), lambda current: True,
                             **{projects.OWNER_PRESENT: True})
        if not isinstance(job, dict):
            setattr(job, projects.OWNER_PRESENT, True)
        found = projects.topic_project(state_dir, topic)
        if found is not None:
            projects.update(state_dir, found.id, owner_spoke)
        return True
    except (KeyError, ValueError, OSError, AttributeError, TypeError) as exc:
        if log is not None:
            log(f"owner presence not noted job=#{job_field(job, 'job_id')}: {type(exc).__name__}")
        return False


def owner_unsteered(state_dir, job, log=None) -> None:
    """Take back the mark ``owner_steered`` wrote for a supplement that then surely did not go into ``job`` (refused, not
    ready: rule 6, nothing was sent). The wake-up counts it reset stay reset: the owner did speak. Never raises."""
    try:
        update_job_record_if(Path(state_dir), job_key(str(job_field(job, "message_id")), job_field(job, "job_id")),
                             lambda current: current.get(projects.OWNER_PRESENT) is True, **{projects.OWNER_PRESENT: False})
        if not isinstance(job, dict):
            setattr(job, projects.OWNER_PRESENT, False)
    except (KeyError, ValueError, OSError, AttributeError, TypeError) as exc:
        if log is not None:
            log(f"owner presence not taken back job=#{job_field(job, 'job_id')}: {type(exc).__name__}")


QUIET_ENDS = ("done", "waiting_user")  # The ends of a wake-up turn whose reply is held back (``silent``).


def silent(delivery) -> bool:
    """Whether the reply of the turn ``delivery`` (app/turn_delivery.py TurnDelivery) is not to be sent (W1-A C4 ④): the turn
    is a wake-up the owner was not in (``woken``: none of his supplements went into it, W0-2 and P1-8), done with its saved
    reply or waiting for the answer to what it asked (project_ask: the questions still go out as cards, through
    TurnDelivery.deliver_questions, and a card that does not go says so as any does). Its reply stays saved and counts as
    delivered, so nothing redelivers it and no recovery card offers it again; what the owner is to hear of it are its report
    notes (``report_out``). A wake-up that failed is not hidden; nor one paused on a native question of its Harness, whose
    words carry that question. Never raises."""
    job, turn = delivery.job, delivery.turn
    try:
        if not projects.enabled() or not woken(delivery.ctx.state_dir, job):
            return False
        if turn.status not in QUIET_ENDS or getattr(turn, "verified_delivery", None) is None \
                or getattr(turn.verified_outcome, "status", "") != "completed":
            return False
        return not (turn.status == "waiting_user" and getattr(job, "paused_question_delivery", None) is not None)
    except Exception as exc:  # noqa: BLE001 - the reply goes out as an owner's turn's does
        delivery.runtime.log(f"wake-up reply not held back job=#{job_field(job, 'job_id')}: {type(exc).__name__}")
        return False


REPORT_HEAD = "专项「{name}」负责人："  # What the lead's report notes of a wake-up turn go out under (``report_out``).
ACTION_WORDS = {"start": "开了话题", "send": "给话题加了话", "stop": "停了话题", "answer": "替主人答了题",
                "escalate": "把问题转给了主人", "ask": "问了主人"}
WITH_ACTIONS = ("each_thread", "verbose")  # The styles whose reports say, in a line, what the turn did (``acted``).


def acted_whats(record, key) -> list:
    """What the lead's turn ``key`` did (the record's ``acted``: {job, whats} rows, an older release's {job, what}), once each,
    in the order it did them."""
    found = []
    for row in record.get("acted") or ():
        if not isinstance(row, dict) or row.get("job") != key:
            continue
        whats = row.get("whats") if isinstance(row.get("whats"), list) else [row.get("what")]
        found += [what for what in whats if isinstance(what, str) and what and what not in found]
    return found


def report_text(record, rows, key) -> str:
    """The message of a wake-up turn's report notes ``rows``: REPORT_HEAD and each note as the lead wrote it (several, one a
    line), and under a style of WITH_ACTIONS a line of what the turn did."""
    texts = [str(row.get("text") or "").strip() for row in rows]
    body = texts[0] if len(texts) == 1 else "\n" + "\n".join(f"- {text}" for text in texts)
    text = REPORT_HEAD.format(name=str(record.get("name") or "")) + body
    whats = [ACTION_WORDS[what] for what in acted_whats(record, key) if what in ACTION_WORDS]
    if whats and project_lead.style_of(record)[project_lead.REPORT] in WITH_ACTIONS:
        text += "\n这一轮：" + "、".join(whats) + "。"
    return text


def report_out(delivery) -> bool:
    """A wake-up turn whose reply is held back (``silent``) sends its report notes (``report_rows``) to the owner as one
    message, where the reply would have gone, under REPORT_HEAD (W1-A C4: the report channel), once (key
    ``lead-report-<任务键>``). Nothing when there is none: a turn that only did things tells nobody. Called by
    app/turn_delivery.py TurnDelivery.deliver outside any lock (rule 1). Whether a message went; never raises."""
    job, ctx = delivery.job, delivery.ctx
    try:
        if not projects.enabled():
            return False
        found = projects.topic_project(ctx.state_dir, str(job.topic_id or ""))
        if found is None or found.role != projects.LEAD:
            return False
        key = job_key(job.message_id, job.job_id)
        record = projects.read_record(ctx.state_dir, found.id)
        rows = report_rows(ctx.state_dir, found.id, record, key)
        if not rows:
            return False
        from covey.core import placement

        top = placement.plan(ctx.state_dir, job).row.answer == placement.TOP
        where = delivery.top_ctx() if top else ctx
        sent = bool(delivery.send_text(where, job.chat_id, report_text(record, rows, key), f"lead-report-{key}"))
        delivery.runtime.log(f"lead report {'sent' if sent else 'not sent'} job=#{job.job_id} notes={len(rows)}")
        return sent
    except Exception as exc:  # noqa: BLE001 - the notes stay on the 专项卡
        delivery.runtime.log(f"lead report failed job=#{job_field(job, 'job_id')}: {type(exc).__name__}")
        return False


# What the Bridge sees.

def owner_turn(state_dir, topic_id, chat_id, message_id) -> None:
    """``enqueue_chat_job`` of a turn in a lead's topic the owner started (or the butler for them: any message id that is
    not a wake-up's): the counts of wake-ups start again, and a project a guard (or the owner's stop) paused is active
    again, and so is one its lead finished (``owner_spoke``). A ``tokens`` pause starts a new budget at the tokens used so
    far. Never raises."""
    try:
        if not projects.enabled() or projects.is_wake(message_id) or not projects.is_lead_topic(state_dir, topic_id, chat_id):
            return
        found = projects.topic_project(state_dir, topic_id)
        projects.update(state_dir, found.id, owner_spoke)
    except (KeyError, ValueError, OSError):
        return


def owner_spoke(record) -> None:
    """Edit ``record`` in place: the owner spoke to the project (his turn in the lead's topic or a supplement of his to its
    turn, the butler's ``project_send``, the web page, his click on its pause card; not his word in a member's topic,
    ``heard``). ``owner_at`` and ``owner_lead_at`` say when (app/project_approvals.py spoke_since, app/project_tools.py
    finish_gate), a project its lead finished is open again (``unfinish``), and whatever ``reopen`` opens opens."""
    record["owner_at"] = record["owner_lead_at"] = time.time()
    unfinish(record)
    reopen(record)


REOPENS_KEPT = 10  # The record's ``reopened`` rows kept.


def unfinish(record) -> None:
    """Edit ``record`` in place: a project its lead finished (``done``) is active again (a finished
    project the owner speaks in opens again, as a folded one does). ``reopened`` keeps when and what the finish had said, for the
    lead's next turn (app/project_lead.py state_pack); the finish's turn and conclusion are forgotten, and the card that had
    shrunk to one line is drawn whole again in place (app/project_cards.py). A record in any other state is left alone."""
    if record.get("state") != projects.DONE:
        return
    rows = record.get("reopened")
    rows = record["reopened"] = rows if isinstance(rows, list) else []
    rows.append({"at": time.time(), "job": str(record.get("state_job") or ""), "summary": cut(str(record.get("state_reason") or ""), 200)})
    del rows[:-REOPENS_KEPT]
    for name in ("state_job", "conclusion_message_id"):
        record.pop(name, None)
    record["state"], record["state_reason"] = projects.ACTIVE, ""
    card = record.get("card")
    if isinstance(card, dict) and card.get("closed"):
        card["closed"], card["digest"] = False, ""
        card.pop("settled_at", None)


def reopen(record) -> None:
    """Edit ``record`` in place (``projects.update``'s ``change``): the counts of wake-ups start again, the stall reminder is
    forgotten, and a project a guard (or the owner's stop) paused is active again; a ``tokens`` pause gets a new budget from
    the tokens used so far."""
    record["wakes"] = {**wakes_of(record), "streak": 0, "recent": []}
    record.pop("stall", None)
    if record.get("state") == projects.PAUSED and record.get("state_reason") in projects.RESUMABLE:
        if record.get("state_reason") == "tokens":
            record["token_floor"] = int(record.get("tokens_used") or 0)
        if record.get("state_reason") == projects.IDLE:
            record.pop("idle", None)
            record["idle_reopened_at"] = time.time()
        record["state"], record["state_reason"] = projects.ACTIVE, ""


def answer_pause(state_dir, project_id, choice, message="", now=None) -> str:
    """What a click on the pause card (``pause_ask``, cards/project_card.py ask_card) does to the record, once and under the
    record's lock: 'ok' with the choice kept in ``pause_ask`` (``continue`` and ``finish`` start the project again,
    ``reopen``; ``stop`` leaves it paused with the owner's stop as the reason, and after a ``tokens`` pause the new budget it will start with), else why not: 'none' (no pause was put to the
    owner), 'answered' (a choice was made already), 'stale' (the click is on another pause's card), 'moved' (the project
    is not paused for that reason any more: the owner's own turn, the lead, a stop got there first)."""
    outcome = []

    def change(record):
        ask = record.get("pause_ask") if isinstance(record.get("pause_ask"), dict) else None
        card = ask.get("card") if ask and isinstance(ask.get("card"), dict) else {}
        if ask is None:
            return outcome.append("none")
        if ask.get("choice"):
            return outcome.append("answered")
        if message and card.get("message_id") and card["message_id"] != message:
            return outcome.append("stale")
        if record.get("state") != projects.PAUSED or record.get("state_reason") != ask.get("reason"):
            return outcome.append("moved")
        ask["choice"], ask["chosen_at"] = choice, time.time() if now is None else now
        if choice == "stop":
            record["state_reason"] = projects.OWNER_STOP
            if ask.get("reason") == "tokens":
                record["token_floor"] = int(record.get("tokens_used") or 0)  # Started again later, it has a budget (as ``reopen`` gives).
        else:
            owner_spoke(record)  # His click: a project 整理专项 made may be finished from here (project_finish's gate).
        outcome.append("ok")
    projects.update(state_dir, project_id, change)
    return outcome[0]


def pause_receipt(state_dir, project_id, choice, ask) -> None:
    """The lead's receipt of the owner's choice on the pause card ``ask`` (``pause_ask``): ``owner_choice``, which wakes it; 继续 and
    收尾 only, a stop leaves none. A ``tokens`` pause that asked the member topics' tasks to stop says so, and that 继续 does not
    start them again: the lead hands them their work again."""
    if choice not in CHOICE_RECEIPT:
        return
    text = CHOICE_RECEIPT[choice].format(why=str(ask.get("why") or ""))
    if ask.get("reason") == "tokens" and type(ask.get("halted")) is int and ask["halted"] > 0:
        text += f"停住时有 {ask['halted']} 个话题的任务被请停了，不会自己接着做；要接着做的用 project_topic_send 再交给它们。"
    if choice == "finish" and turn_source.enabled():
        ref = project_checklist.pause_ref(ask)
        text += CHOICE_CHECKLIST + (CHOICE_BASIS.format(ref=ref) if ref else "")
    project_lead.add_receipt(state_dir, project_id, "owner_choice", text)


def resume(state_dir, project_id, chat_id="") -> str:
    """继续 from outside the pause card (the 看板's block of a project a guard paused, cards/board.py PROJECT_GO_KIND): what the
    pause card's 继续 does. With the pause put to the owner and not answered (``pause_ask``, COVEY_PROJECT_ASK=1) it is that
    answer (``answer_pause``: the card then becomes "你选了继续"), else the owner's word (``owner_spoke``: active again, the
    wake-ups counted afresh, a ``tokens`` pause with a new budget); either way the lead's ``owner_choice`` receipt wakes it.
    'ok', else why not: 'none' (no such project in ``chat_id``), 'moved' (no guard's pause to go on from any more: the owner's
    own turn, the lead or a stop got there first)."""
    record = projects.read_record(state_dir, str(project_id or ""))
    if not record or (chat_id and record.get("chat_id") != chat_id):
        return "none"
    if not projects.halted(record):
        return "moved"
    ask = record.get("pause_ask") if isinstance(record.get("pause_ask"), dict) else None
    try:
        if ask is not None and not ask.get("choice") and ask.get("reason") == record.get("state_reason"):
            outcome = answer_pause(state_dir, project_id, "continue")
            if outcome == "ok":
                pause_receipt(state_dir, project_id, "continue",
                              (projects.read_record(state_dir, project_id) or {}).get("pause_ask") or ask)
            return outcome if outcome in ("ok", "none") else "moved"
        reason, moved = str(record.get("state_reason") or ""), []

        def change(data):
            if not projects.halted(data):
                return moved.append(True)
            owner_spoke(data)
            if data.get("state") == projects.PAUSED:  # A reason ``reopen`` does not know (a newer release's guard): his click.
                data["state"], data["state_reason"] = projects.ACTIVE, ""
        projects.update(state_dir, project_id, change)
    except KeyError:  # Gone since it was read.
        return "none"
    except ValueError:  # Moved on to a state its pause cannot go on from.
        return "moved"
    if moved:
        return "moved"
    pause_receipt(state_dir, project_id, "continue", {"reason": reason, "why": projects.REASON_WORDS.get(reason, reason)})
    return "ok"


def idle_words(seconds) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60} 分钟" if seconds >= 120 else f"{seconds} 秒"


def pause_words(record, reason, now, idle=0) -> str:
    """Why a guard paused ``record`` in a sentence for the owner (the pause card, cards/project_card.py ask_card)."""
    if reason == "tokens":
        limit = projects.token_limit()  # 0 once the budget is gone again: an older pause is still worded.
        return f"token 用到上限 {projects.count_words(limit)}" if limit else "token 用到上限"
    if reason == "wakes":
        wakes = wakes_of(record)
        if wakes["streak"] >= projects.DEFAULT_WAKES:
            return f"负责人连续叫醒 {wakes['streak']} 次没有你的消息"
        return f"负责人一小时内叫醒了 {sum(1 for t in wakes['recent'] if now - t < HOUR)} 次"
    return f"{idle_words(idle)}没有进展"


def heard(state_dir, topic_id, text) -> None:
    """The owner wrote ``text`` in the topic ``topic_id`` (app/app.py handle_event): a member of a project gets an
    ``owner_said`` receipt, the wake-ups in a row start again (someone stepped in), and a project folded away is open again.
    His word is that topic's (W2-C): a project its lead finished stays finished (it opened again before, and its lead's
    next wake-up finished it once more; the receipt waits for the lead's next turn if he opens it), and the lead's finish
    gate does not take it as his word to the project (``owner_lead_at``). Never raises."""
    try:
        if not projects.enabled() or not projects.is_member_topic(state_dir, topic_id):
            return
        found = projects.topic_project(state_dir, topic_id)
        record = projects.read_record(state_dir, found.id)
        if record.get("state") not in (projects.ACTIVE, projects.PAUSED, projects.DONE):
            return
        label = topic_label(Path(state_dir), topic_id) or topic_id[:8]
        project_lead.add_receipt(state_dir, found.id, "owner_said", f"主人在 {label} 里说：{cut(text, SAID)}", thread=topic_id)
        def change(data):
            data["owner_at"] = time.time()
            if projects.folded(data):
                reopen(data)  # A project folded away for being idle: his word opens it again.
            data["wakes"] = {**wakes_of(data), "streak": 0, "recent": []}
        projects.update(state_dir, found.id, change)
    except (KeyError, ValueError, OSError):
        return


def count_tokens(state_dir, project_id, key, used) -> bool:
    """``used`` tokens of the task ``key`` added to the project's ``tokens_used``, once a task (``token_jobs``); whether this
    was the first time."""
    first = []

    def change(data):
        counted = data.get("token_jobs") if isinstance(data.get("token_jobs"), list) else []
        if key in counted:
            return
        counted.append(key)
        data["token_jobs"] = counted[-TOKEN_JOBS_KEPT:]
        data["tokens_used"] = int(data.get("tokens_used") or 0) + used
        first.append(True)
    projects.update(state_dir, project_id, change)
    return bool(first)


def consult_project(state_dir, record) -> str:
    """The project a consult's turn (``collab_ask``, ``record`` its job record) spends the budget of: its collaboration's
    integrator's, else the helper it asked's, as they are now (core/projects.py topic_project; a lead's topic only when it is
    the lead's); '' for none (the collaboration is gone, neither is in a project)."""
    source = turn_source.of(record) or {}
    found = collab_records.read(state_dir, str(source.get("collab") or ""))
    if not found:
        return ""
    ask = collab_records.ask_of(found, str(source.get("ask") or ""))
    chat = str(found.get("chat_id") or "")
    for key in ((found.get("integrator") or {}).get("key"), ask.get("to")):
        place = projects.topic_project(state_dir, str(key or ""))
        if place is not None and (place.role != projects.LEAD or projects.is_lead_topic(state_dir, key, chat)):
            return place.id
    return ""


def consult_tokens(log, state_dir, job) -> None:
    """A consult's turn that ended (协作 ``collab_ask``, in a 咨询通道): its tokens counted once in its project
    (``consult_project``), so asking one another spends the same budget as working. No receipt, nothing in the library:
    its collaboration tells the lead (app/collab_flow.py)."""
    key = job_key(str(job_field(job, "message_id")), job_field(job, "job_id"))
    record = find_job_record(Path(state_dir), key) or {}
    if str(record.get("status") or "") not in COUNTED or turn_source.kind(record) != turn_source.ASK:
        return
    project = consult_project(state_dir, record)
    if project and count_tokens(state_dir, project, key, record_tokens(record)):
        log(f"project tokens of a consult counted project={project[:8]} job=#{record.get('job_id')}")


def after_runner(log, state_dir, job) -> None:
    """The Bridge, once ``job``'s run has ended (app/butler_relay.py after_runner): a task of a project's lead or one of its
    threads has its tokens added once to ``tokens_used``, and a member's turn that ended says so in a ``thread_end`` receipt
    ("T12 的任务 #41 做完：<turn_summary>（全文 <path>）": its number, status, what became of it, where its whole reply is).
    That whole reply, however short, is kept first (C7: app/artifacts.py write_result_file, ``result-files/<key>.md``, an
    over-long one its runner sent as a file stays as it is) and noted in the library (app/project_context.py
    result_to_library). A consult's turn of 协作 has its tokens counted in its collaboration's project
    (``consult_tokens``) and nothing else. Never raises."""
    try:
        topic, chat = str(job_field(job, "topic_id")), str(job_field(job, "chat_id"))
        if not projects.enabled() or not topic:
            return
        if collab.is_channel(topic):
            return consult_tokens(log, state_dir, job)
        found = projects.topic_project(state_dir, topic)
        if found is None or (found.role == projects.LEAD and not projects.is_lead_topic(state_dir, topic, chat)):
            return
        key = job_key(str(job_field(job, "message_id")), job_field(job, "job_id"))
        record = find_job_record(Path(state_dir), key) or {}
        status = str(record.get("status") or "")
        if status not in COUNTED or not turn_source.behavior(record).own_delivery:
            return  # Nor a 协作 ask's turn: its collaboration writes the lead's receipt (app/collab_flow.py), no thread_end.
        ended = found.role == projects.MEMBER and status in RECEIPT_ENDS
        whole = kept_reply(log, state_dir, found.id, topic, key, record) if ended else ""  # Whether or not it is the first look.
        first = count_tokens(state_dir, found.id, key, record_tokens(record))
        if first and ended:
            checkpoint = str(record.get("checkpoint_path") or "")
            head = f"{topic_label(Path(state_dir), topic) or topic[:8]} 的任务 #{record.get('job_id')} {RESULTS.get(status, status)}"
            tail = f"（全文 {whole}）" if whole else ""
            said = turn_summary(record, read_checkpoint(Path(checkpoint)) if checkpoint else {},
                                max(40, min(SUMMARY_CHARS, RECEIPT_CHARS - len(head) - len(tail) - 1)))
            project_lead.add_receipt(state_dir, found.id, "thread_end", head + (f"：{said}" if said else "") + tail,
                                     thread=topic, relayed=relayed_to_owner(state_dir, key))
        if first and found.role == projects.MEMBER:
            project_context.turn_to_library(state_dir, found.id, topic, record, log)  # COVEY_PROJECT_MEMORY=1: its artifacts and delivery.
    except KeyError:
        return  # The project's record is gone.
    except Exception as exc:  # noqa: BLE001 - the task's end does not depend on it
        log(f"project receipt after job #{job_field(job, 'job_id')} failed: {type(exc).__name__}")


def kept_reply(log, state_dir, project_id, topic, key, record) -> str:
    """Where the member turn ``key``'s whole reply is kept (C7: app/artifacts.py write_result_file, written once; an
    over-long one its runner sent as a file stays as it is), noted in the project's library; '' without a reply or when it
    could not be written (logged)."""
    try:
        path = write_result_file(state_dir, key, record.get("final_text"))
    except Exception as exc:  # noqa: BLE001 - the receipt goes all the same, without the path
        log(f"project result file of job #{record.get('job_id')} failed: {type(exc).__name__}")
        return ""
    if path is None:
        return ""
    project_context.result_to_library(state_dir, project_id, topic, record, path, log)
    return str(path)


# The states of a member turn's relay (app/relay_send.py) in which its result reaches the owner, or may have: on its way
# (``pending`` in the merge window, ``submitting``), ``sent``, or ``uncertain`` (it may have arrived and is never sent again
# by itself). ``failed`` and ``unsent`` did not: the lead's report of it is then the owner's way to hear it (§5.4 R3).
RELAY_TOLD = frozenset({"pending", "submitting", "sent", "uncertain"})
RECEIPT_JOB = re.compile(r"的任务 #(\d+) ")  # The task number in a ``thread_end`` receipt's head (``after_runner``).


def relay_state(state_dir, key) -> str:
    """The state of the member turn ``key``'s relay record ('' for none)."""
    from covey.app.relay_send import relay_path

    return str(read_object(relay_path(state_dir, key)).get("state") or "")


def relayed_to_owner(state_dir, key) -> bool:
    """Whether the member's turn ``key`` has its conclusion or stop going, or gone, to the owner's top level (RELAY_TOLD):
    by its task runner before the runner ended, or by the Bridge after it (app/butler_relay.py: a turn the owner waits for,
    app/relay_targets.py awaited_target; a merged relay waits ``pending`` a moment first)."""
    return relay_state(state_dir, key) in RELAY_TOLD


def receipt_job(state_dir, item) -> str:
    """The task key of the member turn a ``thread_end`` receipt ``item`` is of: its ``job`` when it has one, else its topic's
    task of the number its head names ('' when none is found)."""
    given = item.get("job")
    if isinstance(given, str) and given:
        return given
    found, thread = RECEIPT_JOB.search(str(item.get("text") or "")), str(item.get("thread") or "")
    if found is None or not thread:
        return ""
    number = int(found.group(1))
    for record in topic_job_records(Path(state_dir), thread):
        if str(record.get("job_id") or "") == str(number):
            return str(record.get("key") or job_key(record.get("message_id"), record.get("job_id")))
    return ""


def receipt_told(state_dir, item) -> bool:
    """Whether the owner gets the result of the member turn the ``thread_end`` receipt ``item`` is of from its relay: its relay
    record read now (RELAY_TOLD), not what it said when the receipt was written (§5.4 R3: a relay merged a moment later, or
    the Bridge's own once the runner is gone; one that failed is not); without a record, what the receipt says (``relayed``)."""
    key = receipt_job(state_dir, item)
    state = relay_state(state_dir, key) if key else ""
    return state in RELAY_TOLD if state else item.get("relayed") is True


def told_already(state_dir, project_id, key) -> bool:
    """Whether every receipt the lead's turn ``key`` read is of a member's turn whose result the owner gets from its relay
    (``receipt_told``, read when the lead reports: W1-A C4), or a consult's (``collab_answered``: its integrator delivers what
    came of it, the lead only reads it): a ``thread`` or ``note`` report of that turn would tell him the same again, so it is
    not sent (``report_rows``)."""
    taken = project_lead.taken_receipts(state_dir, project_id, key)
    return bool(taken) and all(item.get("kind") == "thread_end" and receipt_told(state_dir, item)
                               or item.get("kind") == collab.RECEIPT_ANSWERED for item in taken)


# The loop.

class ProjectLoop:
    """The feature object: the thread that wakes leads and applies the guards. ``env``: app/butler_env.py ButlerEnv (the Harness
    apps, the state directory)."""
    name = "project_loop"

    def __init__(self, runtime, env, stop=None) -> None:
        """``stop``: app/project_clicks.py ``stop_tasks`` (it imports this module, so the wiring hands it over); none, no task is stopped."""
        self.runtime, self.env, self.stop = runtime, env, stop
        triage_flow.attach(env)  # app/interaction.py tells a wired project loop by it (app/triage.py ENV).
        self.worker, self.left = None, []  # The Worker the looks run in; the left-behind ones still stuck.
        self.stuck = {}  # project id -> the thread of a left-behind look stuck on it: the project is left out while it lives.
        self.generation = 0  # Moves on when a look is left behind; a look of an older one changes nothing (``behind``).
        self.mine = threading.local()  # .generation of the look this thread runs.
        self.at, self.moved, self.slowest, self.slow_logged = "", 0.0, ("", 0.0), 0.0
        self.waiting = {}  # project id -> (why, until) of the wake held back last (``waits``): logged once.

    def threads(self):
        return ((self.name, self.run),) if projects.enabled() else ()

    def run(self, ctx) -> None:
        if self.runtime.shutdown.wait(START_WAIT):
            return
        while not self.runtime.shutdown.is_set():
            self.step()
            self.runtime.shutdown.wait(TICK)

    def step(self, now=None) -> None:
        """One look (``tick``) in the Worker, left behind when it has not moved in STUCK_SECONDS. Never raises."""
        if self.worker is None:
            self.worker = Worker(f"{self.name}-step")
        generation, began = self.generation, time.monotonic()
        self.moved, self.at, self.slowest = began, "", ("", 0.0)
        try:
            self.worker.call(lambda: self.tick(now, generation), STUCK_SECONDS, lambda: self.moved)
        except Stuck:
            return self.leave_behind()
        except Exception as exc:  # noqa: BLE001 - the next look runs all the same
            self.runtime.log(f"{self.name} step failed: {type(exc).__name__}")
        took = time.monotonic() - began
        if took >= SLOW_SECONDS and time.monotonic() - self.slow_logged >= SLOW_LOG:
            self.slow_logged = time.monotonic()
            where, longest = self.slowest
            self.runtime.log(f"project loop slow: a look took {took:.0f} s, the longest part {longest:.0f} s at {where or '?'}")

    def leave_behind(self) -> None:
        """The look stuck: its thread is left to it, the project it was at left out while that thread lives, and the next look runs
        in a new Worker (after waiting for one of MAX_LEFT left-behind ones to end, LEFT_WAIT seconds at most)."""
        old, at = self.worker, self.at
        self.generation += 1
        self.left = [worker for worker in (*self.left, old) if worker.thread.is_alive()]
        self.stuck = {pid: thread for pid, thread in self.stuck.items() if thread.is_alive()}
        if at.startswith("project:"):
            self.stuck[at.split(":", 1)[1]] = old.thread
        self.runtime.log(f"project loop stuck: a look has not moved in {STUCK_SECONDS} s, at: {at or 'its start'}; "
                         f"left behind ({len(self.left)} so far)")
        began = time.monotonic()
        while len(self.left) >= MAX_LEFT and time.monotonic() - began < LEFT_WAIT and not self.runtime.shutdown.wait(10):
            self.left = [worker for worker in self.left if worker.thread.is_alive()]
        if len(self.left) >= MAX_LEFT:
            self.runtime.log(f"project loop: {len(self.left)} left-behind looks still stuck after {LEFT_WAIT} s; going on in a new thread")
        self.worker = Worker(f"{self.name}-step")

    def behind(self) -> bool:
        """Whether this thread's look was left behind (``leave_behind``): what it read is old, a newer look does the work."""
        return getattr(self.mine, "generation", self.generation) != self.generation

    def going(self, at) -> bool:
        """A part of the look done: progress for the Worker's watch (``moved``), and whether to go on (not ``behind``). A left-behind
        look touches nothing of the current one's."""
        if self.behind():
            return False
        now = time.monotonic()
        if now - self.moved > self.slowest[1]:
            self.slowest = (self.at, now - self.moved)
        self.moved, self.at = now, at
        return not self.behind()

    def tick(self, now=None, generation=None) -> None:
        """One look at every active project (``generation``: the look's, ``step``; none, the current one)."""
        if not projects.enabled() or not self.env.state_dir:
            return
        self.mine.generation = self.generation if generation is None else generation
        now = time.time() if now is None else now
        with one_scan():
            if triage.enabled():  # COVEY_PROJECT_TRIAGE=1: receipts for the held questions, the owner's turn for the late ones.
                triage_flow.sweep(self.env, now, going=lambda: self.going("sweep"))
            if not self.going("projects"):
                return
            for record in projects.records(Path(self.env.state_dir)):
                if record.get("state") != projects.ACTIVE:
                    continue
                pid = str(record.get("id"))
                if pid in self.stuck and self.stuck[pid].is_alive():
                    continue  # A left-behind look is stuck on it: this one does not get stuck there too.
                if not self.going(f"project:{pid}"):
                    return
                try:
                    with remembered():
                        self.look(record, now)
                except Exception as exc:  # noqa: BLE001 - the others are looked at all the same
                    self.runtime.log(f"project loop failed project={pid[:8]}: {type(exc).__name__}")
            self.going("")

    def pause(self, record, reason, now=None, idle=0) -> None:
        """The project paused by a guard (``reason``: tokens, wakes, stalled). With COVEY_PROJECT_ASK=1 the same write keeps
        ``pause_ask`` for app/project_cards.py to put to the owner, and a ``tokens`` pause stops the member topics' tasks."""
        if self.behind():
            return
        ask = projects.ask_enabled()
        now = time.time() if now is None else now
        kept = []

        def change(data):
            fresh = data.get("state") == projects.ACTIVE
            data["state"], data["state_reason"] = projects.PAUSED, str(reason)[:200]
            if ask and fresh:
                data["pause_ask"] = {"reason": reason, "why": pause_words(data, reason, now, idle), "at": now, "choice": "",
                                     "halted": 0, "card": {}}
                kept.append(True)
        try:
            projects.update(self.env.state_dir, record["id"], change)
        except (KeyError, ValueError):
            return  # Moved on already (the owner stopped it, the lead finished it).
        self.runtime.log(f"project paused project={record['id'][:8]} reason={reason}")
        if kept and reason == "tokens":
            self.halt_members(record)

    def halt_members(self, record) -> None:
        """A ``tokens`` pause the owner is asked about: the member topics' queued and running tasks are stopped as a stop on their
        cards does (``stop``), so the budget is not spent on while the card waits. Never raises."""
        if self.stop is None:
            return
        try:
            state, chat, lead = Path(self.env.state_dir), str(record.get("chat_id") or ""), str(record["lead_topic_id"])
            app = lead_app(self.env, state, load_topic(state, lead))
            if app is None:
                return
            message = f"project-pause-{record['id']}"
            remember_topic_message(state, lead, chat, message)
            ctx = app.submission.event_context(app.harness.context, {"chat_id": chat, "message_id": message})
            members = [topic for topic in record.get("threads") or () if isinstance(topic, str)]
            stopped = self.stop(app.controls, ctx, members, chat, message, by="guard")

            def count(data):
                if isinstance(data.get("pause_ask"), dict):
                    data["pause_ask"]["halted"] = stopped
            projects.update(state, record["id"], count)
            self.runtime.log(f"project pause stopped {stopped} tasks project={record['id'][:8]}")
        except Exception as exc:  # noqa: BLE001 - the pause stands; the tasks can be stopped from their cards
            self.runtime.log(f"project pause could not stop its tasks project={str(record.get('id'))[:8]}: {type(exc).__name__}")

    def look(self, record, now) -> None:
        state, pid, lead = Path(self.env.state_dir), record["id"], str(record.get("lead_topic_id") or "")
        if hold(record, now) == "tokens":
            return self.pause(record, "tokens", now)
        pending = unread(state, pid)
        if pending:
            due = autowake_due(pending)
            if due and settling(pending, now):
                return  # 协作 on: more ordinary receipts may come in a moment (members ending together): one turn reads them all.
            if due and not lead_busy(state, lead):
                if self.waits(record, state, pending, now):
                    return  # A wake would only fail (a limit, a backend down); no wake, so nothing for the wakes guard to count either.
                reason = hold(record, now)
                return self.pause(record, reason, now) if reason else self.wake(record, now)
            if not due:
                # Without COVEY_PROJECT_AUTOWAKE=1 these wake no one: the lead reads them in its next turn, which the owner
                # starts. Nothing else moving, the project rests and folds away after the idle days as one with nothing to
                # do (T647 P0-6: it said 在做 and never folded); the owner's next word opens it again.
                if not projects.autowake_enabled() and resting(state, record, []):
                    return self.rest(record, state, now)
                return
        if resting(state, record, pending):
            # Nothing to do, or only the owner's answer awaited (the lead can move nothing): no stall, and it folds away
            # after the idle days all the same (the question card stays; the owner's answer opens the project again).
            return self.rest(record, state, now)
        if projects.autowake_enabled():
            self.stall(record, state, now, lead, pending)

    def inbox(self, state, pid) -> list:
        return receipts(state, pid)

    def rest(self, record, state, now) -> None:
        """Nothing to do: folded away once ``projects.idle_seconds()`` have passed since anything happened (a task ended, a receipt came, the
        owner spoke, the project was opened or opened again). The pause card is not sent and nothing is asked."""
        pid = record["id"]
        if self.behind():
            return
        self.unstall(record, state)  # Nothing left to do: a reminder of a stall is over.
        if now - quiet_since(state, record) < projects.idle_seconds():
            return
        days = projects.idle_days()

        def change(data):
            if data.get("state") != projects.ACTIVE:
                return
            data["state"], data["state_reason"] = projects.PAUSED, projects.IDLE
            data["idle"] = {"at": now, "days": days}
            data.pop("stall", None)
        try:
            projects.update(state, pid, change)
        except (KeyError, ValueError):
            return
        self.runtime.log(f"project folded away project={str(pid)[:8]} days={days}")

    def unstall(self, record, state) -> None:
        """Drop the stall reminder's marker of a project that has nothing the lead could move any more."""
        if isinstance(record.get("stall"), dict):
            try:
                projects.update(state, record["id"], lambda data: data.pop("stall", None))
            except (KeyError, ValueError):
                pass

    def stall(self, record, state, now, lead, pending=()) -> None:
        """Something to do and nothing moving: a reminder after ``projects.stall_seconds()`` of it (a ``stalled`` receipt, which wakes the lead),
        and as long again with nothing moving the project is paused. A task running is movement; a project with nothing to do never stalls,
        nor one that only waits for the owner's answer (``movable``: the owner, not the lead, moves it)."""
        topics = [lead, *(record.get("threads") or ())]
        if running_turns(state, topics) or self.behind():
            return
        if not movable(state, topics, pending):
            return self.unstall(record, state)  # Only the owner's answer is awaited now: no stall, and none left over.
        pid = record["id"]
        receipts = self.inbox(state, pid)
        activity = last_activity(state, record, receipts)
        marker = record.get("stall") if isinstance(record.get("stall"), dict) else {}
        if marker and activity > float(marker.get("activity") or 0):
            marker = {}  # Something moved since the reminder: a new idle begins.
            projects.update(state, pid, lambda data: data.pop("stall", None))
        if not marker:
            if now - activity > projects.stall_seconds():
                minutes = int((now - activity) // 60)
                project_lead.add_receipt(state, pid, "stalled", f"专项还有要做的事，已经 {minutes} 分钟没有任务在跑，也没有新的回执。", at=now)
                projects.update(state, pid, lambda data: data.__setitem__("stall", {"at": now, "activity": activity}))
            return
        if now - float(marker.get("at") or now) > projects.stall_seconds():
            self.pause(record, "stalled", now, now - activity)

    def waits(self, record, state, pending, now) -> bool:
        """Whether the lead's wake is held back (``limit_wait``): a wake would only fail. Logged when it begins
        or what it waits for changes, not every look."""
        pid = record["id"]
        why, until = limit_wait(state, record, pending, now)
        if not why or self.behind():
            return bool(why)
        if self.waiting.get(pid) != (why, until):
            self.waiting[pid] = (why, until)
            self.runtime.log(f"project wake held project={pid[:8]} why={why} until={time.strftime('%H:%M:%S', time.localtime(until))}")
        return True

    def wake(self, record, now) -> None:
        """One wake-up turn queued (its number counted first: a lost one is looked for again, never doubled)."""
        pid, mine = record["id"], []
        if self.behind():
            return
        self.waiting.pop(pid, None)  # A wait that begins again later is logged again.

        def change(data):
            wakes = wakes_of(data)
            data["wakes"] = {"count": wakes["count"] + 1, "streak": wakes["streak"] + 1, "last_at": now,
                             "recent": [t for t in wakes["recent"] if now - t < HOUR][-projects.MAX_WAKES_HOUR:] + [now]}
            mine.append(data["wakes"]["count"])
        projects.update(self.env.state_dir, pid, change)
        message = projects.wake_id(pid, mine[0])
        if self.enqueue(record, message):
            self.runtime.log(f"project woken project={pid[:8]} message={message}")
        else:
            self.runtime.log(f"project wake not queued project={pid[:8]} message={message}")

    def enqueue(self, record, message) -> bool:
        """The wake-up turn queued as a message in the lead's topic (an alias makes the message the topic's)."""
        state, chat, topic = Path(self.env.state_dir), str(record.get("chat_id") or ""), str(record["lead_topic_id"])
        data = load_topic(state, topic)
        app = lead_app(self.env, state, data)
        if app is None:
            return False
        remember_topic_message(state, topic, chat, message)
        ctx = app.submission.event_context(app.harness.context, {"chat_id": chat, "message_id": message})
        if ctx.topic_id != topic:
            return False
        append_history(ctx.history_path, "user", WAKE_TEXT, {"message_id": message})
        # The Bridge's wake-up (WAVE12 C1 ``wake``): it answers no question the lead still waits on (C4 ①), its words are
        # not the owner's, and it sends nothing as his (core/turn_source.py BEHAVIOR).
        source = turn_source.stamp(turn_source.WAKE)
        return app.submission.enqueue_chat_job(ctx, WAKE_TEXT, chat, message, prompt_content=WAKE_TEXT,
                                               **({"source": source} if source else {})) is not None


def lead_app(env, state, data):
    """The app a lead's turns run on: the Harness its topic.json (``data``) is bound to, else, before the lead has one, the
    butler's (app/butler_harness.py: the settings', else Claude Code while it can, else the first enabled Harness that takes
    the Bridge's tools). None when that Harness is not enabled here: a lead's session is its Harness's own."""
    apps = env.topics.apps
    return apps.get(data.get("harness_id") or butler_harness.chosen(state, apps)[0])


def batch_seconds() -> float:
    """How long ordinary receipts are gathered before they wake the lead: with 协作 on (COVEY_COLLAB=1)
    COVEY_PROJECT_BATCH_SECONDS, else BATCH_SECONDS (also for a value that is not a number); 0 turns it off. Off with 协作 off:
    each wakes at the next look, as before."""
    if not turn_source.enabled():
        return 0.0
    raw = str(cenv.get(BATCH_ENV, "")).strip()
    try:
        value = float(raw) if raw else float(BATCH_SECONDS)
    except ValueError:
        return float(BATCH_SECONDS)
    return value if 0 <= value < 3600 else float(BATCH_SECONDS)  # Also NaN and infinity.


def urgent(item) -> bool:
    """Whether the receipt ``item`` wakes the lead at once, never gathered: a ``start``, the owner's choice on a pause card,
    a held question (it waits only 180 seconds), a 协作 gap (core/collab.py RECEIPT_WAKES)."""
    return item.get("kind") in ALWAYS_WAKE or collab.RECEIPT_WAKES.get(item.get("kind")) is True


def settling(pending, now) -> bool:
    """Whether the lead's wake-up waits a moment for more receipts (``batch_seconds``, with 协作 on): none of ``pending`` is
    ``urgent``, the newest ordinary one came less than the window ago, and the oldest has waited less than BATCH_STRETCH
    windows. Topics that end within seconds of one another are then read in one turn, not one turn each. A consult's
    receipt (``collab_answered``) wakes nobody and counts for neither."""
    window = batch_seconds()
    if window <= 0 or any(urgent(item) for item in pending):
        return False
    times = [float(item.get("at") or 0) for item in pending if item.get("kind") not in collab.RECEIPT_WAKES]
    return bool(times) and now - max(times) < window and now - min(times) < window * BATCH_STRETCH


def autowake_due(pending) -> bool:
    """Whether ``pending`` receipts wake the lead: a ``start``, the owner's choice on a pause card or a ``question`` (a held
    question waits only 180 seconds, app/triage.py) always does, the others with COVEY_PROJECT_AUTOWAKE=1. 协作's own
    (core/collab.py RECEIPT_WAKES): a gap the integrator cannot close always wakes, an answer never does."""
    kinds = [item.get("kind") for item in pending]
    if any(kind in ALWAYS_WAKE or collab.RECEIPT_WAKES.get(kind) for kind in kinds):
        return True
    return projects.autowake_enabled() and any(kind not in collab.RECEIPT_WAKES for kind in kinds)


def limit_wait(state_dir, record, pending, now) -> tuple:
    """(why, until when) a lead that ``pending`` receipts are due to wake is not woken now, or ('', 0.0): a wake would
    only fail, on a limit or a backend down. T99, 2026-11-25 21:59–22:11: three accounts spent; each failed wake gave
    its receipts back (``unread``), the next look woke the lead again, about every 20 s (65 failed turns), and each wake,
    a new message in the topic, cancelled the retry the failed turn had at the account's reset.

    - ``retry``: the lead's latest turn failed on a limit (app/auto_retry.py OTHER_CODES: a rate limit, a spent quota, a
      passing server fault) and its automatic retry is to come: that turn takes the inbox as any lead turn does, every
      receipt that came meanwhile in one turn. One overdue by LIMIT_REST holds no more (its tick may be gone).
    - ``rest``: it failed so with no retry to come (the ladder ran out, its Harness retries none): until LIMIT_REST
      after it ended.
    - ``quota``: the lead's model draws on a gateway quota that is held (core/quota_hold.py ``blocks``): its turn would
      fail before it starts. Until the reset; the hold itself is checked again RECHECK after it was last found spent.
    - ``down``: the lead's latest turn did not start, or failed as the next one would (``stuck``; ``backend_down``,
      2026-11-26 10:57: a Claude Code on the remote
      machine that its login check could not reach, a wake every 10 s, each telling the owner 后端暂不可用 with a
      recovery card). Until DOWN_LADDER after it ended, by how many of its latest turns in a row did not; the wake then
      is the try whether it starts again, and the first that does reads every receipt.

    A receipt of the owner's own doing (OWN_WAKES) that came after the latest turn ended wakes the lead all the same:
    once, as the turn it wakes is then the latest. A retry cancelled (the owner's 停止自动重试, a model switch) holds
    nothing."""
    lead = str(record.get("lead_topic_id") or "")
    turns = sorted(jobs_of(state_dir, lead), key=lambda job: int(job.get("job_id") or 0), reverse=True)
    latest = turns[0] if turns else {}
    ended = board_read.ended_at(latest) if latest else 0.0
    if any(item.get("kind") in OWN_WAKES and float(item.get("at") or 0) > ended for item in pending):
        return "", 0.0
    if latest and stuck(latest):
        row = next((n for n, job in enumerate(turns) if not stuck(job)), len(turns))
        until = ended + DOWN_LADDER[min(row, len(DOWN_LADDER)) - 1]
        if now < until:
            return "down", until
    if latest and failure.failure_code(latest) in auto_retry.OTHER_CODES:
        state = latest.get("auto_retry_state")
        due = float(latest.get("auto_retry_at") or 0) or ended
        if state in RETRY_COMING and now - due < LIMIT_REST:
            return "retry", due
        if not state and now - ended < LIMIT_REST:
            return "rest", ended + LIMIT_REST
    data = load_topic(Path(state_dir), lead)
    found = quota_hold.blocks(state_dir, data.get("harness_id"), data.get("model"), now)
    return ("quota", float(found[1]["reset_at"])) if found is not None else ("", 0.0)


def backend_down(job) -> bool:
    """Whether nothing of ``job``'s turn ran as its Harness could not start it: its login check or its program failed, or
    its start-up raised (app/job_dispatch.py, core/failure.py STARTUP_FAILED), or its task runner did not start
    (START_FAILED)."""
    if job.get("status") == "failed" and str(job.get("status_message") or "").startswith(failure.STARTUP_FAILED):
        return True
    return failure.failure_code(job) == failure.START_FAILED


def stuck(job) -> bool:
    """Whether the next turn of ``job``'s lead would fail as it did: it did not start (``backend_down``), or it failed
    as one of WONT_HEAL, or with nothing done at all (no tool step, no token) for a reason it did not name."""
    if backend_down(job):
        return True
    code = failure.failure_code(job)
    if code in WONT_HEAL:
        return True
    idle = not int(job.get("tool_step_count") or 0) and not int(job.get("input_tokens") or 0)
    return code in ("root_failed", "process_exit", "root_incomplete") and idle


def quiet_start(state_dir, job, now=None) -> bool:
    """Whether the owner is told nothing of ``job``, a turn whose Harness did not start (app/job_dispatch.py: no
    后端暂不可用, no recovery card): it is a wake-up, so he is not there, and the lead's turn before it did not start
    either, less than an hour ago: he was told of that one, and the loop tries again DOWN_LADDER apart (``limit_wait``).
    Never raises: on any doubt he is told, as before."""
    now = time.time() if now is None else now
    try:
        if not projects.enabled() or not woken(state_dir, job):
            return False
        number = int(job_field(job, "job_id") or 0)
        earlier = [record for record in topic_job_records(Path(state_dir), job_field(job, "topic_id"))
                   if int(record.get("job_id") or 0) < number]
        before = max(earlier, key=lambda record: int(record.get("job_id") or 0), default=None)
        return before is not None and backend_down(before) and now - board_read.ended_at(before) < HOUR
    except Exception:  # noqa: BLE001 - on the failure path of a turn: he is told, as before
        return False
