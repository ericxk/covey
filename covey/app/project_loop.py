"""Continuing runs and guards of a project (docs/architecture.md 专项, phase 2's P2-6, COVEY_PROJECTS=1): what wakes the lead
when something happened since it last looked, and the mechanical checks that stop a project that runs on by itself.

- ``ProjectLoop`` (app/unified.py PARTS ``project_loop``, a feature object: ``threads()``, none without COVEY_PROJECTS=1): a
  thread that looks at every active project each ``TICK`` seconds. A project with receipts nobody has read (the inbox,
  app/project_lead.py) and no lead turn queued, running or waiting for the owner gets one wake-up turn, queued like any
  message of the lead's topic (``enqueue_chat_job``, message id ``lead-<project id>-<n>``, ``n`` kept in the record's
  ``wakes``). A ``start`` receipt always wakes; the others only with COVEY_PROJECT_AUTOWAKE=1. After a restart the same
  look finds the receipts on disk again (``take_receipts`` gives back what a turn that did not end well had taken).
- Guards, any of which pauses the project with ``state_reason`` ``tokens``, ``wakes`` or ``stalled`` (core/projects.py
  GUARD_REASONS) and wakes it no more: the lead's and the threads' tokens over COVEY_PROJECT_TOKENS (``tokens_used``, added
  once per task by ``after_runner``); 30 wake-ups in a row with nobody stepping in, or 12 in the last hour; no task and no
  receipt for 30 minutes, which first wakes the lead with a ``stalled`` receipt and, as long again with nothing moving,
  pauses. The owner's own turn in the lead's topic (``owner_turn``) clears the counts and starts a project a guard or the
  owner's stop paused (a ``tokens`` pause gets a new budget from there, ``token_floor``).
- Receipts: ``after_runner`` (the Bridge, once a task's runner is gone: rule 8 keeps a task process to its own card and
  record) writes ``thread_end`` for a thread's turn that ended and counts the tokens; ``heard`` (the Bridge, on the owner's
  message in a thread's topic) writes ``owner_said``; the loop writes ``stalled``. ``question`` is phase 2's P2-5.
- ``silent`` (app/worker.py TurnDelivery): a wake-up turn's reply is not sent to the owner, unless the lead recorded a
  report note in it that its style lets through (``project_lead.reportable``); then it goes as the lead's reply does.

Rules 1 and 5 (AGENTS.md): records are written under their own lock, nothing is sent to Feishu from under it, and the only
reply that goes out is the saved, authorized one."""
from __future__ import annotations

import time
from pathlib import Path

from covey.app import project_lead
from covey.core import board_read, projects
from covey.core.history_storage import append_history
from covey.core.jobs import find_job_record, job_field, job_key, topic_job_records
from covey.core.loops import every
from covey.core.state import read_object
from covey.core.statuses import ENDED, NOT_ENDED
from covey.core.text import cut
from covey.core.topics import load_topic, remember_topic_message, topic_label
from covey.core.turn_words import RESULTS

HARNESS = "claude"  # A lead runs on Claude Code (app/main_thread.py HARNESS: not imported, it imports project_lead).
TICK = 10  # Seconds between two looks.
START_WAIT = 20  # After the Bridge starts: its own recovery of interrupted turns goes first.
HOUR = 3600
WAKE_TEXT = ("[Bridge 唤醒] 专项状态里的收件箱有新的回执，主人不在场：按回执判断下一步。"
             "要告诉主人的用 project_note 并标 report，这一轮的回复不会发给主人。")
TOKEN_JOBS_KEPT = 300  # Task keys the record remembers it has counted (``tokens_used``).
COUNTED = ENDED | {"waiting_user"}  # A turn waiting for the owner's answer has used its tokens too.
RECEIPT_ENDS = ("done", "failed", "cancelled")  # What a thread's receipt says; an interrupted turn goes on by itself.
SAID = 300  # Characters of the owner's words in an ``owner_said`` receipt.


def wakes_of(record) -> dict:
    """The record's ``wakes`` ({count, last_at, streak, recent}) with each missing or invalid part at none."""
    given = record.get("wakes") if isinstance(record.get("wakes"), dict) else {}
    number = lambda value: value if type(value) is int and value >= 0 else 0  # noqa: E731
    recent = [float(t) for t in given.get("recent") or () if isinstance(t, (int, float)) and not isinstance(t, bool)]
    return {"count": number(given.get("count")), "streak": number(given.get("streak")), "recent": recent,
            "last_at": float(given["last_at"]) if isinstance(given.get("last_at"), (int, float)) else 0.0}


def hold(record, now) -> str:
    """The guard a project is over now ('' for none): ``tokens``, or ``wakes`` (30 in a row, or 12 in the last hour)."""
    floor = record.get("token_floor") if type(record.get("token_floor")) is int else 0
    used = record.get("tokens_used") if type(record.get("tokens_used")) is int else 0
    if used - floor >= projects.token_limit():
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


def unread(state_dir, project_id) -> list:
    """The inbox's receipts no lead turn has read (or one that did not end well took), oldest first."""
    found = []
    try:
        paths = sorted(project_lead.inbox_dir(state_dir, project_id).glob("*.json"))
    except OSError:
        return found
    for path in paths:
        item = read_object(path)
        if item.get("id") and project_lead.took_it(state_dir, item.get("taken_by"), ""):
            found.append(item)
    return found


def open_turns(state_dir, topics) -> bool:
    """Whether any of ``topics`` has a task not ended: queued, running, or waiting for the owner."""
    return any(record.get("status") in NOT_ENDED for topic in topics for record in topic_job_records(state_dir, topic))


def last_activity(state_dir, record, receipts) -> float:
    """When something last happened in the project that was not the lead's own wake-up: a thread's task or the lead's
    turn the owner started ending, a receipt other than ``stalled`` coming in, the project beginning."""
    lead = str(record.get("lead_topic_id") or "")
    times = [float(record.get("created_at") or 0)]
    for topic in (lead, *(record.get("threads") or ())):
        for job in topic_job_records(state_dir, topic):
            if topic == lead and projects.is_wake(job.get("message_id")):
                continue
            times.append(board_read.ended_at(job))
    times += [float(item.get("at") or 0) for item in receipts if item.get("kind") != "stalled"]
    return max(times)


def reported(state_dir, topic_id, key) -> bool:
    """Whether the turn ``key`` of the lead topic ``topic_id`` recorded a report note its project's style lets through (the
    reply of a wake-up turn is sent then, as an owner's turn's is)."""
    found = projects.topic_project(state_dir, str(topic_id or ""))
    record = projects.read_record(state_dir, found.id) if found is not None else {}
    return any(isinstance(row, dict) and row.get("job") == key and row.get("report") is True
               and project_lead.reportable(record.get("style"), row.get("kind"))
               for row in record.get("notes") or ())


def silent(delivery) -> bool:
    """Whether the reply of the turn ``delivery`` (app/worker.py TurnDelivery) is not to be sent: the turn is a wake-up
    (message id ``lead-…``), done with its saved reply, and the lead recorded no report note in it. Its reply stays saved
    and counts as delivered, so nothing redelivers it and no recovery card offers it again. Never raises."""
    job, turn = delivery.job, delivery.turn
    try:
        if not projects.enabled() or not projects.is_wake(job_field(job, "message_id")):
            return False
        if turn.status != "done" or getattr(turn, "verified_delivery", None) is None \
                or getattr(turn.verified_outcome, "status", "") != "completed":
            return False
        return not reported(delivery.ctx.state_dir, job.topic_id, job_key(job.message_id, job.job_id))
    except Exception as exc:  # noqa: BLE001 - the reply goes out as an owner's turn's does
        delivery.runtime.log(f"wake-up reply not held back job=#{job_field(job, 'job_id')}: {type(exc).__name__}")
        return False


# What the Bridge sees.

def owner_turn(state_dir, topic_id, chat_id, message_id) -> None:
    """``enqueue_chat_job`` of a turn in a lead's topic the owner started (or the butler for them: any message id that is
    not a wake-up's): the counts of wake-ups start again, and a project a guard (or the owner's stop) paused is active
    again. A ``tokens`` pause starts a new budget at the tokens used so far. Never raises."""
    try:
        if not projects.enabled() or projects.is_wake(message_id) or not projects.is_lead_topic(state_dir, topic_id, chat_id):
            return
        found = projects.topic_project(state_dir, topic_id)

        def change(record):
            record["wakes"] = {**wakes_of(record), "streak": 0, "recent": []}
            record.pop("stall", None)
            if record.get("state") == projects.PAUSED and record.get("state_reason") in projects.RESUMABLE:
                if record.get("state_reason") == "tokens":
                    record["token_floor"] = int(record.get("tokens_used") or 0)
                record["state"], record["state_reason"] = projects.ACTIVE, ""
        projects.update(state_dir, found.id, change)
    except (KeyError, ValueError, OSError):
        return


def heard(state_dir, topic_id, text) -> None:
    """The owner wrote ``text`` in the topic ``topic_id`` (app/app.py handle_event): a thread of a project gets an
    ``owner_said`` receipt, and the wake-ups in a row start again (someone stepped in). Never raises."""
    try:
        if not projects.enabled() or not projects.is_thread_topic(state_dir, topic_id):
            return
        found = projects.topic_project(state_dir, topic_id)
        record = projects.read_record(state_dir, found.id)
        if record.get("state") not in (projects.ACTIVE, projects.PAUSED):
            return
        label = topic_label(Path(state_dir), topic_id) or topic_id[:8]
        project_lead.add_receipt(state_dir, found.id, "owner_said", f"主人在 {label} 里说：{cut(text, SAID)}", thread=topic_id)
        projects.update(state_dir, found.id,
                        lambda data: data.__setitem__("wakes", {**wakes_of(data), "streak": 0, "recent": []}))
    except (KeyError, ValueError, OSError):
        return


def after_runner(log, state_dir, job) -> None:
    """The Bridge, once ``job``'s run has ended (app/main_relay.py after_runner): a task of a project's lead or one of its
    threads has its tokens added once to ``tokens_used``, and a thread's turn that ended says so in a ``thread_end`` receipt
    (what became of it in a sentence, its status and number). Never raises."""
    try:
        topic, chat = str(job_field(job, "topic_id")), str(job_field(job, "chat_id"))
        if not projects.enabled() or not topic:
            return
        found = projects.topic_project(state_dir, topic)
        if found is None or (found.role == projects.LEAD and not projects.is_lead_topic(state_dir, topic, chat)):
            return
        key = job_key(str(job_field(job, "message_id")), job_field(job, "job_id"))
        record = find_job_record(Path(state_dir), key) or {}
        status = str(record.get("status") or "")
        if status not in COUNTED:
            return
        first, used = [], record_tokens(record)

        def change(data):
            counted = data.get("token_jobs") if isinstance(data.get("token_jobs"), list) else []
            if key in counted:
                return
            counted.append(key)
            data["token_jobs"] = counted[-TOKEN_JOBS_KEPT:]
            data["tokens_used"] = int(data.get("tokens_used") or 0) + used
            first.append(True)
        projects.update(state_dir, found.id, change)
        if first and found.role == projects.THREAD and status in RECEIPT_ENDS:
            said = project_lead.last_line(record.get("final_text"))
            label = topic_label(Path(state_dir), topic) or topic[:8]
            text = f"{label} 的任务 #{record.get('job_id')} {RESULTS.get(status, status)}" + (f"：{cut(said, 160)}" if said else "")
            project_lead.add_receipt(state_dir, found.id, "thread_end", text, thread=topic)
    except KeyError:
        return  # The project's record is gone.
    except Exception as exc:  # noqa: BLE001 - the task's end does not depend on it
        log(f"project receipt after job #{job_field(job, 'job_id')} failed: {type(exc).__name__}")


# The loop.

class ProjectLoop:
    """The feature object: the thread that wakes leads and applies the guards. ``env``: app/main_env.py MainEnv (the Harness
    apps, the state directory)."""
    name = "project_loop"

    def __init__(self, runtime, env) -> None:
        self.runtime, self.env = runtime, env

    def threads(self):
        return ((self.name, self.run),) if projects.enabled() else ()

    def run(self, ctx) -> None:
        if self.runtime.shutdown.wait(START_WAIT):
            return
        every(self.runtime, TICK, self.tick, isolate=self.name)

    def tick(self, now=None) -> None:
        """One look at every active project."""
        if not projects.enabled() or not self.env.state_dir:
            return
        now = time.time() if now is None else now
        for record in projects.records(Path(self.env.state_dir)):
            if record.get("state") != projects.ACTIVE:
                continue
            try:
                self.look(record, now)
            except Exception as exc:  # noqa: BLE001 - the others are looked at all the same
                self.runtime.log(f"project loop failed project={str(record.get('id'))[:8]}: {type(exc).__name__}")

    def pause(self, record, reason) -> None:
        try:
            projects.set_state(self.env.state_dir, record["id"], projects.PAUSED, reason)
        except (KeyError, ValueError):
            return  # Moved on already (the owner stopped it, the lead finished it).
        self.runtime.log(f"project paused project={record['id'][:8]} reason={reason}")

    def look(self, record, now) -> None:
        state, pid, lead = Path(self.env.state_dir), record["id"], str(record.get("lead_topic_id") or "")
        if hold(record, now) == "tokens":
            return self.pause(record, "tokens")
        pending = unread(state, pid)
        if pending:
            due = autowake_due(pending)
            if due and not open_turns(state, [lead]):
                reason = hold(record, now)
                return self.pause(record, reason) if reason else self.wake(record, now)
            return
        if projects.autowake_enabled():
            self.stall(record, state, now, lead)

    def stall(self, record, state, now, lead) -> None:
        """No task, no receipt: a reminder after STALL_SECONDS of it (a ``stalled`` receipt, which wakes the lead), and as long
        again with nothing moving the project is paused."""
        if open_turns(state, [lead, *(record.get("threads") or ())]):
            return
        pid = record["id"]
        try:  # Every receipt, read or not: a thread's end the lead has read is activity all the same.
            receipts = [read_object(path) for path in project_lead.inbox_dir(state, pid).glob("*.json")]
        except OSError:
            receipts = []
        activity = last_activity(state, record, receipts)
        marker = record.get("stall") if isinstance(record.get("stall"), dict) else {}
        if marker and activity > float(marker.get("activity") or 0):
            marker = {}  # Something moved since the reminder: a new idle begins.
            projects.update(state, pid, lambda data: data.pop("stall", None))
        if not marker:
            if now - activity > projects.STALL_SECONDS:
                minutes = int((now - activity) // 60)
                project_lead.add_receipt(state, pid, "stalled", f"专项已经 {minutes} 分钟没有任务在做，也没有新的回执。", at=now)
                projects.update(state, pid, lambda data: data.__setitem__("stall", {"at": now, "activity": activity}))
            return
        if now - float(marker.get("at") or now) > projects.STALL_SECONDS:
            self.pause(record, "stalled")

    def wake(self, record, now) -> None:
        """One wake-up turn queued (its number counted first: a lost one is looked for again, never doubled)."""
        pid, mine = record["id"], []

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
        app = self.env.topics.apps.get(data.get("harness_id") or HARNESS)
        if app is None:
            return False
        remember_topic_message(state, topic, chat, message)
        ctx = app.submission.event_context(app.harness.context, {"chat_id": chat, "message_id": message})
        if ctx.topic_id != topic:
            return False
        append_history(ctx.history_path, "user", WAKE_TEXT, {"message_id": message})
        return app.submission.enqueue_chat_job(ctx, WAKE_TEXT, chat, message, prompt_content=WAKE_TEXT) is not None


def autowake_due(pending) -> bool:
    """Whether ``pending`` receipts wake the lead: a ``start`` always does, the others with COVEY_PROJECT_AUTOWAKE=1."""
    return any(item.get("kind") == "start" for item in pending) or projects.autowake_enabled()
