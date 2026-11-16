"""Continuing runs and guards of a project (docs/architecture.md 专项, phase 2's P2-6, COVEY_PROJECTS=1): what wakes the lead
when something happened since it last looked, and the mechanical checks that stop a project that runs on by itself.

- ``ProjectLoop`` (app/unified.py PARTS ``project_loop``, a feature object: ``threads()``, none without COVEY_PROJECTS=1): a
  thread that looks at every active project each ``TICK`` seconds. A project with receipts nobody has read (the inbox,
  app/project_lead.py) and no lead turn queued, running or waiting for the owner gets one wake-up turn, queued like any
  message of the lead's topic (``enqueue_chat_job``, message id ``lead-<project id>-<n>``, ``n`` kept in the record's
  ``wakes``). A ``start`` receipt always wakes; the others only with COVEY_PROJECT_AUTOWAKE=1. After a restart the same
  look finds the receipts on disk again (``take_receipts`` gives back what a turn that did not end well had taken).
- Guards, any of which pauses the project with ``state_reason`` ``tokens``, ``wakes`` or ``stalled`` (core/projects.py
  GUARD_REASONS) and wakes it no more: the lead's and the members' tokens over COVEY_PROJECT_BUDGET (``tokens_used``, added
  once per task by ``after_runner``); 30 wake-ups in a row with nobody stepping in, or 12 in the last hour; no task and no
  receipt for 30 minutes, which first wakes the lead with a ``stalled`` receipt and, as long again with nothing moving,
  pauses. The owner's own turn in the lead's topic (``owner_turn``) clears the counts and starts a project a guard or the
  owner's stop paused (a ``tokens`` pause gets a new budget from there, ``token_floor``).
- With COVEY_PROJECT_ASK=1 a pause a guard made is also put to the owner (``pause``): the record gets ``pause_ask`` ({reason,
  why, at, choice, card}), app/project_cards.py sends the card (继续 / 收尾 / 停) and ``answer_pause`` is what a click does
  to the record. A ``tokens`` pause also stops the member topics' running tasks; a ``wakes`` pause leaves them running; a
  ``stalled`` one has none. 继续 and 收尾 start the project again (``reopen``, what the owner's own turn does too) and leave an
  ``owner_choice`` receipt, which always wakes the lead.
- Receipts: ``after_runner`` (the Bridge, once a task's runner is gone: rule 8 keeps a task process to its own card and
  record) writes ``thread_end`` (the receipt kind keeps its name on disk) for a member's turn that ended and counts the tokens; ``heard`` (the Bridge, on the owner's
  message in a member's topic) writes ``owner_said``; the loop writes ``stalled``; the Bridge's sweep of the held questions (app/triage.py, COVEY_PROJECT_TRIAGE=1) writes ``question``.
- ``silent`` (app/worker.py TurnDelivery): a wake-up turn's reply is not sent to the owner, unless the lead recorded a
  report note in it that its style lets through (``project_lead.reportable``); then it goes as the lead's reply does.

Rules 1 and 5 (AGENTS.md): records are written under their own lock, nothing is sent to Feishu from under it, and the only
reply that goes out is the saved, authorized one."""
from __future__ import annotations

import time
from pathlib import Path

from covey.app import project_context, project_lead, triage as triage_flow
from covey.core import board_read, projects, triage
from covey.core.history_storage import append_history
from covey.core.jobs import find_job_record, job_field, job_key, topic_job_records
from covey.core.loops import every
from covey.core.state import read_object
from covey.core.statuses import ENDED, NOT_ENDED
from covey.core.text import cut
from covey.core.topics import load_topic, remember_topic_message, topic_label
from covey.core.turn_words import RESULTS

HARNESS = "claude"  # A lead runs on Claude Code (app/butler.py HARNESS: not imported, it imports project_lead).
TICK = 10  # Seconds between two looks.
START_WAIT = 20  # After the Bridge starts: its own recovery of interrupted turns goes first.
HOUR = 3600
WAKE_TEXT = ("[Bridge 唤醒] 专项状态里的收件箱有新的回执，主人不在场：按回执判断下一步。"
             "要告诉主人的用 project_note 并标 report，这一轮的回复不会发给主人。")
TOKEN_JOBS_KEPT = 300  # Task keys the record remembers it has counted (``tokens_used``).
COUNTED = ENDED | {"waiting_user"}  # A turn waiting for the owner's answer has used its tokens too.
RECEIPT_ENDS = ("done", "failed", "cancelled")  # What a member's receipt says; an interrupted turn goes on by itself.
SAID = 300  # Characters of the owner's words in an ``owner_said`` receipt.
ALWAYS_WAKE = ("start", "owner_choice", "question")  # The receipts that wake the lead without COVEY_PROJECT_AUTOWAKE=1.
CHOICE_RECEIPT = {
    "continue": "主人看了停住的原因（{why}），选了继续：专项已经重新开始，叫醒次数清零。接着按状态包判断下一步。",
    "finish": "主人看了停住的原因（{why}），选了收尾：这个专项不再开新的话题。请总结做完了什么、还有什么没做完，"
              "用 project_note 标 report 告诉主人，然后调 project_finish（还有话题在做时它会拒绝，等它们结束再收尾）。",
}


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
    """When something last happened in the project that was not the lead's own wake-up: a member's task or the lead's
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
        projects.update(state_dir, found.id, reopen)
    except (KeyError, ValueError, OSError):
        return


def reopen(record) -> None:
    """Edit ``record`` in place (``projects.update``'s ``change``): the counts of wake-ups start again, the stall reminder is
    forgotten, and a project a guard (or the owner's stop) paused is active again; a ``tokens`` pause gets a new budget from
    the tokens used so far."""
    record["wakes"] = {**wakes_of(record), "streak": 0, "recent": []}
    record.pop("stall", None)
    if record.get("state") == projects.PAUSED and record.get("state_reason") in projects.RESUMABLE:
        if record.get("state_reason") == "tokens":
            record["token_floor"] = int(record.get("tokens_used") or 0)
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
            reopen(record)
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
    project_lead.add_receipt(state_dir, project_id, "owner_choice", text)


def idle_words(seconds) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60} 分钟" if seconds >= 120 else f"{seconds} 秒"


def pause_words(record, reason, now, idle=0) -> str:
    """Why a guard paused ``record`` in a sentence for the owner (the pause card, cards/project_card.py ask_card)."""
    if reason == "tokens":
        return f"token 用到上限 {projects.count_words(projects.token_limit())}"
    if reason == "wakes":
        wakes = wakes_of(record)
        if wakes["streak"] >= projects.DEFAULT_WAKES:
            return f"负责人连续叫醒 {wakes['streak']} 次没有你的消息"
        return f"负责人一小时内叫醒了 {sum(1 for t in wakes['recent'] if now - t < HOUR)} 次"
    return f"{idle_words(idle)}没有进展"


def heard(state_dir, topic_id, text) -> None:
    """The owner wrote ``text`` in the topic ``topic_id`` (app/app.py handle_event): a member of a project gets an
    ``owner_said`` receipt, and the wake-ups in a row start again (someone stepped in). Never raises."""
    try:
        if not projects.enabled() or not projects.is_member_topic(state_dir, topic_id):
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
    """The Bridge, once ``job``'s run has ended (app/butler_relay.py after_runner): a task of a project's lead or one of its
    threads has its tokens added once to ``tokens_used``, and a member's turn that ended says so in a ``thread_end`` receipt
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
        if first and found.role == projects.MEMBER and status in RECEIPT_ENDS:
            said = project_lead.last_line(record.get("final_text"))
            label = topic_label(Path(state_dir), topic) or topic[:8]
            text = f"{label} 的任务 #{record.get('job_id')} {RESULTS.get(status, status)}" + (f"：{cut(said, 160)}" if said else "")
            project_lead.add_receipt(state_dir, found.id, "thread_end", text, thread=topic)
        if first and found.role == projects.MEMBER:
            project_context.turn_to_library(state_dir, found.id, topic, record, log)  # COVEY_PROJECT_MEMORY=1: its artifacts and delivery.
    except KeyError:
        return  # The project's record is gone.
    except Exception as exc:  # noqa: BLE001 - the task's end does not depend on it
        log(f"project receipt after job #{job_field(job, 'job_id')} failed: {type(exc).__name__}")


# The loop.

class ProjectLoop:
    """The feature object: the thread that wakes leads and applies the guards. ``env``: app/butler_env.py ButlerEnv (the Harness
    apps, the state directory)."""
    name = "project_loop"

    def __init__(self, runtime, env, stop=None) -> None:
        """``stop``: app/project_cards.py ``stop_tasks`` (it imports this module, so the wiring hands it over); none, no task is stopped."""
        self.runtime, self.env, self.stop = runtime, env, stop
        triage_flow.attach(env)  # The 专项卡's 不对 click reaches the member topic through it (app/triage.py correct).

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
        if triage.enabled():  # COVEY_PROJECT_TRIAGE=1: receipts for the held questions, the owner's turn for the late ones.
            triage_flow.sweep(self.env, now)
        for record in projects.records(Path(self.env.state_dir)):
            if record.get("state") != projects.ACTIVE:
                continue
            try:
                self.look(record, now)
            except Exception as exc:  # noqa: BLE001 - the others are looked at all the same
                self.runtime.log(f"project loop failed project={str(record.get('id'))[:8]}: {type(exc).__name__}")

    def pause(self, record, reason, now=None, idle=0) -> None:
        """The project paused by a guard (``reason``: tokens, wakes, stalled). With COVEY_PROJECT_ASK=1 the same write keeps
        ``pause_ask`` for app/project_cards.py to put to the owner, and a ``tokens`` pause stops the member topics' tasks."""
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
            app = self.env.topics.apps.get(load_topic(state, lead).get("harness_id") or HARNESS)
            if app is None:
                return
            message = f"project-pause-{record['id']}"
            remember_topic_message(state, lead, chat, message)
            ctx = app.submission.event_context(app.harness.context, {"chat_id": chat, "message_id": message})
            members = [topic for topic in record.get("threads") or () if isinstance(topic, str)]
            stopped = self.stop(app.controls, ctx, members, chat, message)

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
            if due and not open_turns(state, [lead]):
                reason = hold(record, now)
                return self.pause(record, reason, now) if reason else self.wake(record, now)
            return
        if projects.autowake_enabled():
            self.stall(record, state, now, lead)

    def stall(self, record, state, now, lead) -> None:
        """No task, no receipt: a reminder after ``projects.stall_seconds()`` of it (a ``stalled`` receipt, which wakes the lead), and as long
        again with nothing moving the project is paused."""
        if open_turns(state, [lead, *(record.get("threads") or ())]):
            return
        pid = record["id"]
        try:  # Every receipt, read or not: a member's end the lead has read is activity all the same.
            receipts = [read_object(path) for path in project_lead.inbox_dir(state, pid).glob("*.json")]
        except OSError:
            receipts = []
        activity = last_activity(state, record, receipts)
        marker = record.get("stall") if isinstance(record.get("stall"), dict) else {}
        if marker and activity > float(marker.get("activity") or 0):
            marker = {}  # Something moved since the reminder: a new idle begins.
            projects.update(state, pid, lambda data: data.pop("stall", None))
        if not marker:
            if now - activity > projects.stall_seconds():
                minutes = int((now - activity) // 60)
                project_lead.add_receipt(state, pid, "stalled", f"专项已经 {minutes} 分钟没有任务在做，也没有新的回执。", at=now)
                projects.update(state, pid, lambda data: data.__setitem__("stall", {"at": now, "activity": activity}))
            return
        if now - float(marker.get("at") or now) > projects.stall_seconds():
            self.pause(record, "stalled", now, now - activity)

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
    """Whether ``pending`` receipts wake the lead: a ``start``, the owner's choice on a pause card or a ``question`` (a held
    question waits only 180 seconds, app/triage.py) always does, the others with COVEY_PROJECT_AUTOWAKE=1."""
    return any(item.get("kind") in ALWAYS_WAKE for item in pending) or projects.autowake_enabled()
