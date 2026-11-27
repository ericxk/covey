"""专项的问题分流 (docs/projects.md 专项的问题分流, COVEY_PROJECTS=1 and COVEY_PROJECT_TRIAGE=1; phase 2's P2-5): a member topic's choice
question goes to the project's lead before the owner, and the owner sees it only when the lead cannot answer it.

- ``Triage.route`` (a part of app/questions.py Questions; asked by app/native_questions.py ``show`` and app/checkpoint_questions.py
  ``deliver`` before they send a card, in the task process too): ``owner`` (send the card as always) or ``lead`` (send nothing:
  the question is held). Held means the question's own record carries ``triage`` {state: held, deadline: now + 180 s} (core/triage.py);
  its ``status`` stays ``pending``, ``display_pending`` stays true (so no list of pending questions has it, and the card the owner
  would click is not there), and the task's ``question_delivery_ok`` is true: nothing is "undelivered", nothing offers a retry.
  All of these must hold, or it is the owner's: the switch, a member topic of an active project, a question that blocks, every
  question a choice (free text is the owner's), and no gate word (core/project_gates.py).
- The receipt (kind ``question``) in the project's inbox and the timeout are the Bridge's (``sweep``, from app/project_loop.py): a task
  process writes only its own question's record (AGENTS.md rule 8). The sweep sends the question on to the owner (``escalate``: the
  card the held question did not get, through the same two doors) when its 180 seconds are up, the project is no longer active,
  or the lead says so (project_escalate).
- The lead answers with ``answer`` (project_answer): the question's own submit path (app/questions.py ``submit_answer``) with its
  note saying the lead answered, so the topic goes on as if the owner had clicked. Every answer and hand-back is a row of the
  project's ``triage`` list (the 专项卡's 分流记录); an answer's row had a 不对 button once (retired, T898: the owner says it in the member topic).
- With 协作 on (COVEY_COLLAB=1), or whenever the lead names them, an answer stands on grounds the Bridge checks (``grounds``,
  docs/projects.md 分流的依据; by id or short code, the decisions of decisions.jsonl too): entries of the project's memory that the owner said and that still hold, or the owner's own
  messages in the project; the lead's notes (待核对参考) never alone, a line voided, done, superseded or only for that once
  never. The grounds go into the answer's note and its 分流记录 row.

Rules 1 and 4 (AGENTS.md): a record is changed under its own lock (``update_question``) and nothing is sent from under it; the
triage state moves only along core/triage.py TRIAGE_TRANSITIONS."""
from __future__ import annotations

import dataclasses
import re
import time
from pathlib import Path

from covey.app import project_lead, question_dedup
from covey.app.triage_view import held_questions, lead_held
from covey.app.answers import AnswerIncomplete, answer_fields
from covey.cards.question_cards import triage_element
from covey.core import memory as core_memory
from covey.core import project_gates, project_memory, projects, triage, turn_source
from covey.core.jobs import find_job_record, saved_job, upsert_job_record
from covey.core.state import keyed_json_path, read_object
from covey.core.store import directory_store, update_question
from covey.core.text import cut
from covey.core.topics import topic_label
from covey.core.trace import stage_notes, with_stage_notes
from covey.core.trace_store import load_trace

LEAD, OWNER = "lead", "owner"
NOTE = "（负责人按专项记忆替主人答的）"  # The 补充说明 of an answer the lead gave.
LOG_KEPT = 40  # Rows of a project's 分流记录.
ROW = 120  # Characters of a row's question and answer.
RECEIPT = "question"  # The inbox receipt's kind.
RESHOW_EVERY = 60  # Seconds between the sweep's tries at the card of a question handed on that never went (``reshow``).
RESHOW_TRIES = 5  # The sweep's tries (a live question: the owner's message in the topic asks once more each time).
FULL_SWEEP = 600  # Seconds between the sweep's looks at the held questions of every status; the others look at the pending ones.
_swept = {}  # state directory -> {"full": monotonic time of the last full look, "answering": {token: path}} (``swept``).


def question_path(state_dir, token) -> Path:
    return keyed_json_path(state_dir, "questions", token)


class Triage:
    """The question side: the verdict of ``route``. Part of Questions (``questions.triage``); ``runtime`` is for its log."""

    def __init__(self, *, runtime, cards) -> None:
        self.runtime = runtime
        self.cards = cards

    def route(self, ctx, job, record) -> str:
        """``lead`` when the question is kept from the owner for the project's lead, else ``owner``. Never raises: whatever
        goes wrong, the owner is asked as they would have been."""
        if not triage.enabled():
            return OWNER
        try:
            return self._route(ctx, record)
        except Exception as exc:  # noqa: BLE001 - the owner's card is the safe answer
            self.runtime.log(f"question triage failed token={str(record.get('token'))[:8]}: {type(exc).__name__}")
            return OWNER

    def _route(self, ctx, record) -> str:
        state, path = Path(ctx.state_dir), question_path(ctx.state_dir, record["token"])
        current = read_object(path) or record
        if triage.of(current):
            if triage.held(current) and not triage.expired(current, time.time()):
                return LEAD
            if triage.held(current):  # Held past its time (a redelivery came late): the owner's now.
                update_question(path, lambda row: triage.moved(row, triage.ESCALATED, why="3 分钟没人处理", at=time.time())
                                if triage.state(row) == triage.HELD else None)
            return OWNER
        project = self.project_of(state, current)
        if (project is None or current.get("status", "pending") != "pending" or current.get("blocking") is not True
                or not project_gates.choice_only(current.get("questions")) or project_gates.hit(current.get("questions"))):
            return OWNER
        now = time.time()

        def hold(row):
            if row.get("status", "pending") != "pending" or row.get("triage"):
                return None
            return {**row, "triage": {"state": triage.HELD, "at": now, "deadline": now + triage.DEADLINE, "project": project["id"],
                                      "receipt": False}}
        update_question(path, hold)
        return LEAD if triage.held(read_object(path)) else OWNER

    def leads(self, ctx, record) -> bool:
        """Whether ``record``'s question (one not saved yet: app/native_questions.py ``dedup`` asks before it registers) is
        one ``route`` would hold for the project's lead: the same conditions, nothing written. Never raises (False)."""
        try:
            if not triage.enabled():
                return False
            project = self.project_of(Path(ctx.state_dir), record)
            return bool(project is not None and record.get("status", "pending") == "pending" and record.get("blocking") is True
                        and project_gates.choice_only(record.get("questions")) and not project_gates.hit(record.get("questions")))
        except Exception:  # noqa: BLE001 - the owner's, as route says when it fails
            return False

    @staticmethod
    def project_of(state_dir, record):
        """The active project whose member topic asked ``record``'s question (in the chat it names), or None."""
        topic, chat = str(record.get("topic_id") or ""), str(record.get("chat_id") or "")
        found = projects.topic_project(state_dir, topic)
        if found is None or found.role != projects.MEMBER:
            return None
        project = projects.read_record(state_dir, found.id)
        if project.get("state") != projects.ACTIVE or (chat and project.get("chat_id") != chat) or not project.get("lead_topic_id"):
            return None
        return project

    def note(self, ctx, job, record) -> None:
        """The task card says the question is with the lead (the question's element, core/triage.py held). Best effort."""
        try:
            self.cards.set_task_element(ctx, job, triage_element(read_object(question_path(ctx.state_dir, record["token"])) or record))
        except Exception as exc:  # noqa: BLE001 - the question is held all the same
            self.runtime.log(f"question triage note failed: {type(exc).__name__}")


# What the Bridge sees: the held questions, their receipts and timeouts, the two tools and the owner's correction.

def log_row(state_dir, project_id, text, *, token="", topic="", kind="", evidence=()) -> None:
    """One row of the project's 分流记录 (its ``triage`` list); never raises (the record may be gone). ``evidence``: the grounds
    of an answer ({ref, kind, level, layer, part}, ``grounds``), kept only when there are some."""
    row = {"at": time.time(), "text": cut(text, 300), **({"token": token} if token else {}), **({"topic": topic} if topic else {}),
           **({"kind": kind} if kind else {}), **({"evidence": list(evidence)} if evidence else {})}

    def change(data):
        rows = data.get("triage") if isinstance(data.get("triage"), list) else []
        rows.append(row)
        data["triage"] = rows[-LOG_KEPT:]
    try:
        projects.update(state_dir, project_id, change)
    except (KeyError, ValueError, OSError):
        return


def summary(record, answered=None) -> str:
    """One question record as a row says it: the first question, and the answers when there are some."""
    first = (record.get("questions") or [{}])[0]
    line = cut(str(first.get("question") or ""), ROW)
    if answered:
        said = "；".join("、".join((answered.get(q.get("id")) or {}).get("answers", [])) for q in record.get("questions") or ())
        line += " → " + cut(said, ROW)
    return line


def escalate(env, record, why, *, by="lead", context="", recommend="", if_not="") -> bool:
    """The held question sent on to the owner: its triage moves to ``escalated`` (with ``why``, which its card says), and the card
    goes out through the door the question would have used. True when the card was sent; a card that could not go is made good by
    the topic's 重新投递 (the task's record says its question was not delivered) or, sooner, by the sweep (``stranded``, ``reshow``). A question no longer held changes nothing (False).
    ``context`` (a line of background), ``recommend`` (the option the lead recommends) and ``if_not`` (what not taking it costs) go
    into the triage record when given and on the card (cards/question_cards.py ``lead_notes``)."""
    state = Path(env.state_dir)
    path = question_path(state, record["token"])
    moved = update_question(path, lambda row: {**triage.moved(row, triage.ESCALATED, why=cut(why, 120), by=by, at=time.time(),
                                                **{k: cut(v, 120) for k, v in (("context", context), ("recommend", recommend), ("if_not", if_not)) if v}),
                                               "display_pending": True} if triage.held(row) else None)
    if moved is None:
        return False
    found = triage.of(moved)
    label = topic_label(state, moved.get("topic_id")) or str(moved.get("topic_id"))[:8]
    if found.get("project"):
        log_row(state, found["project"], f"转给你：{label} · {summary(moved)}" + (f"（{cut(why, 60)}）" if why else ""),
                topic=moved.get("topic_id"), kind="escalated")
    ok = False
    try:
        ok = show(env, moved)
    except Exception as exc:  # noqa: BLE001 - the 重新投递 below is the way back
        env.runtime.log(f"question triage escalation failed token={str(moved.get('token'))[:8]}: {type(exc).__name__}")
    if not ok:
        env.runtime.log(f"question triage escalation card not sent token={str(moved.get('token'))[:8]}: the sweep asks again")
    if not ok and moved.get("native_live_question") is True:
        # The turn still runs and waits on it: its record is not failed (a terminal status its runner could not leave, rule
        # 4); the card is asked for again by the sweep and by the owner's next message (``reshow``).
        upsert_job_record(state, moved.get("job_key"), question_delivery_ok=False)
    elif not ok:
        upsert_job_record(state, moved.get("job_key"), status="failed", question_delivery_ok=False,
                          status_message="问题要主人答，但问题控件还没送到；请重新投递")
    return ok


def unshown(record) -> bool:
    """A live question handed on to the owner whose card never went: pending, ``display_pending``, escalated. Its turn waits on it
    while nobody sees it (the task runner did not acknowledge ``show_question`` in time, or its card failed)."""
    return (isinstance(record, dict) and record.get("status", "pending") == "pending" and record.get("native_live_question") is True
            and record.get("display_pending") is True and triage.state(record) == triage.ESCALATED)


def stranded(record) -> bool:
    """A checkpoint question (the card of a finished turn's form, not a live one) handed on to the owner whose card never went:
    pending, ``display_pending``, escalated. Nothing else would send it: the task's record says 重新投递, and only a click on
    the topic's task card runs that (T752: a card that failed at the hand-over stayed unsent for a day)."""
    return (isinstance(record, dict) and record.get("status", "pending") == "pending" and record.get("native_live_question") is not True
            and record.get("body_delivery_required") is True and record.get("display_pending") is True
            and triage.state(record) == triage.ESCALATED)


def reshow_due(record, now) -> bool:
    """Whether the sweep asks for ``record``'s card again: unshown (live) or stranded (a checkpoint question; with the switch on),
    fewer than RESHOW_TRIES tries, RESHOW_EVERY seconds since the last one (or the hand-over)."""
    found = triage.of(record)
    last = float(found.get("reshown_at") or found.get("at") or 0)
    return ((unshown(record) or (stranded(record) and triage.enabled()))
            and int(found.get("reshows") or 0) < RESHOW_TRIES and now - last >= RESHOW_EVERY)


def reshow(env, record, now=None) -> bool:
    """The card of an ``unshown`` or ``stranded`` question asked for again (``show``: of the task runner that waits on a live one,
    sent from here for a checkpoint one), the try counted in its triage (``reshows``, ``reshown_at``) first. Whether it went out;
    a question no longer unshown or stranded changes nothing (False). Never raises. A task that is gone has no runner to ask:
    its live question is retired instead (Questions.retire_orphaned_live_questions). Each failed try is logged with its reason, the
    last one says the sweep gave up (the task's record stays failed: the board's 停住了 and its task card's 重试 are the way on);
    a try that goes through puts a task the hand-over marked failed back to waiting for the owner (``mend``)."""
    state, now = Path(env.state_dir), time.time() if now is None else now
    stamped = update_question(question_path(state, record["token"]), lambda row: {
        **row, "triage": {**triage.of(row), "reshown_at": now, "reshows": int(triage.of(row).get("reshows") or 0) + 1}}
        if unshown(row) or stranded(row) else None)
    if stamped is None:
        return False
    token, tries = str(record.get("token"))[:8], triage.of(stamped)["reshows"]
    ok = False
    try:
        ok = show(env, stamped)
    except Exception as exc:  # noqa: BLE001 - the next try, or the owner's next message, asks again
        env.runtime.log(f"question triage card again failed token={token}: {type(exc).__name__}")
    env.runtime.log(f"question triage card again token={token} try={tries} ok={ok}")
    if ok and stranded(stamped):
        mend(env, stamped)
    elif not ok and stranded(stamped) and tries >= RESHOW_TRIES:
        env.runtime.log(f"question triage card gave up token={token} after {tries} tries: the task's 重试 button is the way on")
    return ok


def mend(env, record) -> None:
    """The card of a ``stranded`` question went out: the task the hand-over (or a restart) marked failed for it waits for the owner's
    answer again, as 重新投递 leaves it (app/redelivery.py). Only a task still failed on this question's form; never raises."""
    state = Path(env.state_dir)
    try:
        job = find_job_record(state, record.get("job_key")) or {}
        if job.get("status") == "failed" and job.get("question_delivery_ok") is False and job.get("execution_status") == "waiting_user":
            upsert_job_record(state, job["key"], status="waiting_user", delivery_ok=True, question_delivery_ok=True,
                              status_message="问题卡已补发")
    except Exception as exc:  # noqa: BLE001 - the card is out
        env.runtime.log(f"question triage task not mended: {type(exc).__name__}")


def show(env, record) -> bool:
    """The card of ``record`` (a question that was held and is the owner's now) sent, as its own Harness's app sends one."""
    from covey.app.redelivery import delivery_context
    from covey.app.runner_ipc import show_in_runner  # Here: runner_ipc's inbox reaches back to show_here.
    state, topic, chat = Path(env.state_dir), str(record.get("topic_id")), str(record.get("chat_id"))
    token = str(record.get("token"))[:8]
    found = env.topics.target(state, topic, chat)
    if found is None:
        env.runtime.log(f"question triage card not sent token={token}: topic not addressable")
        return False
    app, ctx, _ = found
    ctx = dataclasses.replace(ctx, topic_id=topic)
    if record.get("native_live_question") is True:
        # The turn still runs and waits on it. In a task runner (the usual case) only that process holds the live
        # control the answer goes to, and what the turn has written so far: it shows the card and its top-level copy
        # (show_here). T488 #5528, 2026-11-21: this Bridge's RemoteControl has none of it, so the card never went.
        handed = show_in_runner(app.questions.runtime, state, record)
        if handed is not None:
            return handed
        job = app.questions.runtime.jobs.live_job(topic, chat, record.get("job_id"))
        return bool(job is not None and app.questions.show_handed_on(ctx, job, record))
    original = find_job_record(state, record.get("job_key")) or {}
    if not original:
        env.runtime.log(f"question triage card not sent token={token}: task record gone")
        return False
    rebuilt = saved_job(original, chat, topic, original.get("reply_to_message_id") or ctx.reply_to_message_id,
                        checkpoint_question_deferred=original.get("checkpoint_question_deferred", False))
    sent = bool(app.questions.deliver_checkpoint_questions(delivery_context(ctx, original, rebuilt), rebuilt, False))
    if not sent:
        try:
            waits = not app.questions.checkpoints.result_delivered(ctx, record)
        except Exception:  # noqa: BLE001 - only the reason in the log
            waits = False
        env.runtime.log(f"question triage card not sent token={token}: "
                        + ("the task's result is not delivered yet" if waits else "the card did not go out"))
    if sent:
        try:
            app.questions.display.paint_task_card(ctx, read_object(question_path(state, record["token"])))
        except Exception as exc:  # noqa: BLE001 - the card is out; the task card keeps its older line
            env.runtime.log(f"question triage task card not repainted: {type(exc).__name__}")
        # The card above went to the topic's thread. The turn ended while the question was held, so its top-level
        # copy (app/butler_relay.py mirror_questions) was not made then: made now, with what the turn wrote above it,
        # so the owner sees it in the private chat.
        try:
            job = find_job_record(state, record.get("job_key")) or None
            notes = stage_notes((load_trace(state, job) or {}).get("steps")) if job else []
            app.questions.display.mirror(ctx, read_object(question_path(state, record["token"])),
                                         with_stage_notes((job or {}).get("final_text") or "", notes), job=job)
        except Exception as exc:  # noqa: BLE001 - the card is in its thread and on the board
            env.runtime.log(f"question triage top-level copy failed: {type(exc).__name__}")
    return sent


def show_here(questions, ctx, job, record, written) -> bool:
    """The live question ``record`` (held, the owner's now) shown by the process that runs ``job``, the turn waiting on it
    (``questions``: its Harness's Questions, app/questions.py show_handed_on; in a task runner its inbox's
    runner_ipc.SHOW_QUESTION): its card (``retry_live_questions``, which checks the question is still the backend's), the
    task card's line, and the copy at the private chat's top level with what the turn wrote before it asked (``written()``,
    app/native_questions.py turn_output, as a question that was never held gets it). Whether the card went out; never
    raises."""
    state, token = Path(ctx.state_dir), str(record.get("token"))
    try:
        message = questions.retry_live_questions(ctx, record.get("chat_id"), job)
    except Exception as exc:  # noqa: BLE001 - the task's record says the question was not delivered
        questions.runtime.log(f"question triage live card failed token={token[:8]}: {type(exc).__name__}")
        return False
    if not (message and message.startswith("原生问题控件已重新投递")):
        questions.runtime.log(f"question triage live card not sent token={token[:8]}: {message}")
        return False
    current = read_object(question_path(state, token)) or record
    try:
        questions.display.paint_task_card(ctx, current)
    except Exception as exc:  # noqa: BLE001 - the card is out; the task card keeps its older line
        questions.runtime.log(f"question triage task card not repainted: {type(exc).__name__}")
    try:
        questions.display.mirror(ctx, current, written(), job=job)
    except Exception as exc:  # noqa: BLE001 - the card is in its thread and on the board
        questions.runtime.log(f"question triage top-level copy failed: {type(exc).__name__}")
    return True


def swept(state) -> list:
    """[(path, record)] the sweep looks at, oldest first: the held questions that are pending, and those the lead's answer was on its
    way to last time (read again by name: the answer landing takes them out of ``pending``). Every FULL_SWEEP seconds, and on the
    first look of a process, the held ones of every status instead (``lead_held``). The questions directory has thousands of records
    on a live deployment: looking at all of them every TICK seconds was most of the project loop's time (T726)."""
    seen = _swept.setdefault(str(state), {"full": None, "answering": {}})
    now = time.monotonic()
    if seen["full"] is None or now - seen["full"] >= FULL_SWEEP:
        rows, seen["full"] = lead_held(state), now
    else:
        rows = held_questions(state)
        known = {row.get("token") for _, row in rows}
        for token, path in list(seen["answering"].items()):
            if token not in known:
                record = read_object(path)
                if triage.state(record) == triage.HELD:
                    rows.append((path, record))
        rows.sort(key=lambda row: row[1].get("created_at") or 0)
    seen["answering"] = {row.get("token"): path for path, row in rows
                         if row.get("status", "pending") == "pending" and triage.of(row).get("answering")}
    return rows


def sweep(env, now=None, going=None) -> None:
    """The Bridge's look at the held questions (app/project_loop.py ProjectLoop.tick): a receipt for each one the lead has not been
    told of, and the owner's turn for each one that waited its 180 seconds, whose project stopped, or that was handed back. An
    answer the lead sent to a task process is settled here too: ``answered_by_lead`` once the question record shows it landed,
    the owner's turn when it did not within ANSWERING seconds. An answer on its way (ANSWERING seconds) leaves its question alone.
    With the switch off nobody handles a held question: all of them are the owner's (``closed``; the Bridge's start does it once).
    ``going()``, when given, is asked before each question: False stops the sweep (app/project_loop.py: this look was left behind
    as stuck, and a newer one does the work)."""
    if not getattr(env, "state_dir", None):
        return
    state, now = Path(env.state_dir), time.time() if now is None else now
    closed, handed = not triage.enabled(), set()  # ``handed``: the questions this look sent on, whose card has its minute first.
    for path, record in (lead_held(state) if closed else swept(state)):
        if going is not None and not going():
            return
        try:
            found = triage.of(record)
            answering = float(found.get("answering") or 0)
            if record.get("status", "pending") != "pending":
                if answering and triage.state(record) == triage.HELD:  # The lead's answer landed (or something else ended the question).
                    update_question(path, lambda row: triage.moved(row, triage.ANSWERED, by="lead", at=now, answering=0)
                                    if triage.state(row) == triage.HELD and row.get("status", "pending") != "pending" else None)
                continue
            if answering and now - answering < triage.ANSWERING:
                continue  # The lead's answer is being sent: neither the pause, nor the time, nor the switch takes the question.
            handed.add(record.get("token"))
            if closed:
                escalate(env, record, "问题分流已关闭", by="switch")
            elif answering:
                escalate(env, record, "负责人的回答没有送到", by="error")
            elif projects.read_record(state, found.get("project") or "").get("state") != projects.ACTIVE:
                escalate(env, record, "专项现在没有在做", by="project")
            elif triage.expired(record, now):
                escalate(env, record, "3 分钟没人处理", by="timeout")
            else:
                handed.discard(record.get("token"))
                if not found.get("receipt"):
                    receipt(state, projects.read_record(state, found.get("project") or ""), record, now)
        except Exception as exc:  # noqa: BLE001 - the others are looked at all the same
            env.runtime.log(f"question triage sweep failed token={str(record.get('token'))[:8]}: {type(exc).__name__}")
    for _, record in directory_store(state / "questions").query(status="pending", where=lambda row: reshow_due(row, now)):
        if going is not None and not going():
            return
        if record.get("token") not in handed:
            reshow(env, record, now)


def settle_closed(env) -> None:
    """The Bridge's start with the question triage off: the questions an earlier run held go to the owner, once (ProjectLoop runs
    only with COVEY_PROJECTS=1, and the questions' directory is not a thing to scan every few seconds)."""
    if not triage.enabled():
        sweep(env)


def owner_spoke(env, topic, chat) -> None:
    """The owner wrote in ``topic``: the questions held there for the lead go to them now (their message is then the answer, or a
    note, as with any question card of the topic). A question whose answer is on its way stays with the lead. A card that cannot be
    sent leaves the question held and the topic's old refusal (app/interaction.py). Never raises. Nothing is sent from under a lock."""
    try:
        now = time.time()
        for _, record in held_questions(Path(env.state_dir), {topic}):
            if record.get("chat_id") == chat and not (triage.of(record).get("answering") and now - float(triage.of(record)["answering"]) < triage.ANSWERING):
                escalate(env, record, "你在这个话题里说话了", by="owner")
    except Exception as exc:  # noqa: BLE001 - the topic's own refusal says what to do
        env.runtime.log(f"question triage hand-over failed: {type(exc).__name__}")


def receipt(state_dir, project, record, now) -> None:
    """The ``question`` receipt of a held question, once (marked on the record after: a crash between the two writes it again,
    which wakes the lead no more than once more)."""
    label = topic_label(Path(state_dir), record.get("topic_id")) or str(record.get("topic_id"))[:8]
    first = (record.get("questions") or [{}])[0]
    before = question_dedup.lead_line(record)  # The owner's answer to the same question before (app/question_dedup.py).
    project_lead.add_receipt(state_dir, project["id"], RECEIPT,
                             f"{label} 的任务 #{record.get('job_id')} 问：{cut(str(first.get('question') or ''), 160)}"
                             + (f"（{before}）" if before else "")
                             + "（交给你：能答就 project_answer，不能就 project_escalate；3 分钟没处理会转给主人）",
                             thread=str(record.get("topic_id") or ""), at=now)
    update_question(question_path(state_dir, record["token"]),
                    lambda row: {**row, "triage": {**triage.of(row), "receipt": True}} if triage.held(row) else None)


# The grounds of an answer the lead gives in the owner's place (``grounds``; project_answer's ``evidence``).

MSG = "msg:"  # A ground that names the owner's own message in the project: msg:<message id>.
MESSAGE_ID = re.compile(r"[0-9A-Za-z_\-]{4,128}")
SPLIT = re.compile(r"[,，、;；\s]+")  # Grounds given as one string (the schema asks for a list).
GROUNDS_MAX = 6
GROUND_TEXT = 40  # Characters of a ground's words in the answer's note.
NO_GROUNDS = ("要写 evidence：你依据的专项记忆条目 id（状态包专项记忆每行开头〔〕里的）或主人原话 msg:<消息 id>；"
              "没有依据就用 project_escalate 转给主人。")
ESCALATE = "依据不够就用 project_escalate 转给主人（写清你推荐哪个），不要换个说法再答。"
NOT_OWNER = ("这些依据都不是主人说的：待核对参考是你整理的口径，（无出处）的旧记录条件可能不全，都不能单独作为替主人答题的依据。"
             "用 project_escalate 转给主人，写清你推荐哪个、为什么。")
WHO = {turn_source.LEVEL_OWNER: "你说的", turn_source.LEVEL_AGENT: "它总结的"}


def ground_id(text) -> str:
    """An evidence as the lead wrote it, the decorations it may have copied from the pack taken off (〔〕, a ref's
    ``project:<id>:`` or ``decision:<id>:`` head)."""
    text = " ".join(str(text or "").split()).strip("〔〕[]【】 ")
    return text.rsplit(":", 1)[-1] if text.startswith(("project:", "decision:")) else text


def ground_rows(state_dir, project) -> dict:
    """entry id -> line of the project's memory a ground may name: its memory.json's and its decisions.jsonl's
    (app/project_context.py decision_rows)."""
    from covey.app.project_context import decision_rows

    rows = project_memory.entries(Path(state_dir), project)
    return {row["id"]: row for row in [*rows, *decision_rows(Path(state_dir), project["id"], rows)]}


def coded(state_dir, project, ref, rows=()) -> tuple:
    """(the entry id, its short code) a ground names by a short code (core/memstore.py CODE_RE), or (ref, '') for another (a
    line's own id among ``rows`` first: an old six-hex id can read like a code); ('', code) for a code of no line of this project."""
    from covey.core import memstore

    if ref in rows or ref.startswith(MSG) or not memstore.is_code(ref.upper()):
        return ref, ""
    entry = memstore.find(Path(state_dir), ref)
    if entry is None or entry.scope != "project" or entry.owner_key != project.get("id"):
        return "", ref.upper()
    return entry.ref.rsplit(":", 1)[-1], entry.code


def owner_message(env, project, message_id) -> dict:
    """The job record of ``message_id`` when it is the owner's own words in ``project`` (its lead's topic or a member topic, its
    chat): a message the Bridge took in from him (project_lead.owner_message), sent by him (app/access.py owner_turn); else
    {}."""
    record = (find_job_record(Path(env.state_dir), message_id) or {}) if MESSAGE_ID.fullmatch(message_id) else {}
    topics = {str(project.get("lead_topic_id") or ""), *(topic for topic in project.get("threads") or () if isinstance(topic, str))}
    access = getattr(getattr(env, "runtime", None), "access", None)
    if (record.get("message_id") != message_id or record.get("topic_id") not in topics or record.get("chat_id") != project.get("chat_id")
            or not project_lead.owner_message(record) or access is None
            or not access.owner_turn(str(record.get("chat_id") or ""), str(record.get("sender_id") or ""))):
        return {}
    return record


def grounds(env, project, evidence) -> list:
    """The grounds the lead gave for answering in the owner's place (project_answer's ``evidence``), each checked here and
    never taken from what the model says of it: [{ref, kind (memory or message), level, layer, part, text}]. An entry id is a
    line of the project's memory (core/project_memory.py entries; its level and layer core/memory.py describe's); msg:<message
    id> (or the bare id) the owner's own message in the project (``owner_message``). ValueError says what is wrong, and the
    answer is not sent: none given, one that is not there (made up), a style line, one voided, done, superseded or only for
    that once (``ONCE``), or none of the owner's words among them (the lead's notes, 待核对参考, and the owner's older lines
    without an origin are never the ground alone)."""
    given = evidence if isinstance(evidence, list) else SPLIT.split(evidence) if isinstance(evidence, str) else []
    given = list(dict.fromkeys(ground_id(item) for item in given if ground_id(item)))
    if not given:
        raise ValueError(NO_GROUNDS)
    if len(given) > GROUNDS_MAX:
        raise ValueError(f"依据最多 {GROUNDS_MAX} 条，这次写了 {len(given)} 条：只留真正依据的那几条。")
    rows = ground_rows(env.state_dir, project)
    found, problems = [], []
    for ref in given:
        ref, code = coded(env.state_dir, project, ref, rows)
        if not ref:
            problems.append(f"{code} 不是这个专项的记忆（短码抄状态包专项记忆每行开头〔〕里的）")
            continue
        message = ref[len(MSG):] if ref.startswith(MSG) else ref if ref.startswith(project_lead.OWNER_PREFIXES) else ""
        if message:
            record = owner_message(env, project, message)
            if not record:
                problems.append(f"{ref} 不是主人在这个专项里说的话（消息 id 抄状态包“主人在专项里说过的话”里的）")
                continue
            found.append({"ref": MSG + message, "kind": "message", "level": turn_source.LEVEL_OWNER, "layer": "", "part": "",
                          "text": cut(record.get("user_text"), 200)})
            continue
        row = rows.get(ref)
        if row is None:
            problems.append(f"{ref} 不在这个专项的记忆里（短码抄状态包专项记忆每行开头〔〕里的）")
            continue
        code = code or str(row.get("code") or "")
        facts = core_memory.describe(row)
        if row.get("kind") == "style":
            problems.append(f"{ref} 是汇报风格，不是答题的依据")
        elif row.get("deleted") or facts["valid"] == "void":
            problems.append(f"{ref} 已作废，不再适用")
        elif facts["layer"] == core_memory.HISTORY:
            problems.append(f"{ref} 已经做完或被替代，进了历史记录，不再适用")
        elif facts["applies"] == project_memory.ONCE:
            problems.append(f"{ref} 只限当次，不能拿来答别的题")
        else:
            found.append({"ref": ref, "kind": "memory", "level": facts["level"], "layer": facts["layer"],
                          "part": facts["part"] or "", "text": cut(row.get("text"), 200), "applies": facts["applies"],
                          **({"code": code} if code else {})})
    if problems:
        raise ValueError("；".join(problems) + "。" + ESCALATE)
    if not any(item["level"] == turn_source.LEVEL_OWNER for item in found):
        raise ValueError(NOT_OWNER)
    return found


def ground_tag(item) -> str:
    """Who said a ground and which layer it is, short: 你说的·当前决定, 主人原话, 它总结的·待核对参考."""
    if item["kind"] == "message":
        return "主人原话"
    return WHO.get(item["level"], "它总结的") + "·" + core_memory.PART_WORDS.get(item["part"], core_memory.LAYER_WORDS.get(item["layer"], ""))


def ground_note(found) -> str:
    """The 补充说明 of an answer the lead gave on ``found`` (``grounds``): who answered and on what, for the topic and the owner to
    check; NOTE without grounds."""
    if not found:
        return NOTE
    return "（负责人替主人答的，依据：" + "；".join(f"{item.get('code') or item['ref']} {ground_tag(item)}「{cut(item['text'], GROUND_TEXT)}」"
                                              for item in found) + "）"


def ground_row(found) -> str:
    """The head of an answer's 分流记录 line with its grounds, short enough to show before the line is clipped."""
    if not found:
        return "答了："
    tags = list(dict.fromkeys(ground_tag(item) for item in found))
    return f"答了（依据 {'、'.join(tags[:2])}{f' 等 {len(found)} 条' if len(found) > 2 else ''}）："


def answer(env, app, ctx, chat, project, answers, event_id, found=()) -> dict:
    """project_answer: the newest held question of the topic ``ctx`` answered as the owner would, with NOTE as its 补充说明
    (``ground_note`` when it stands on grounds, ``found``: ``grounds``'s, also kept on its 分流记录 row). ValueError says what
    does not fit (the answer is not sent then, and the question stays held)."""
    state = Path(env.state_dir)
    rows = held_questions(state, {ctx.topic_id})
    if not rows:
        raise ValueError("这个话题现在没有交给你的问题（可能已经转给主人，或超过 3 分钟了）。")
    path, record = rows[-1]
    fields = answer_fields(record, answers, ground_note(found))
    now = time.time()
    taken = update_question(path, lambda row: {**row, "display_pending": False, "triage": {**triage.of(row), "answering": now}}
                            if triage.held(row) else None)
    if taken is None:
        raise ValueError("这个问题已经不在你手上了（转给了主人，或已经有回答）。")
    try:
        app.questions.submit_answer(ctx, chat, record["token"], fields, event_id)
    except Exception as exc:
        escalate(env, taken, "负责人的回答没有送出：" + cut(str(exc), 80), by="error")
        if isinstance(exc, AnswerIncomplete):
            raise ValueError(f"没有提交：{exc}") from exc
        raise ValueError(f"回答没有送出，问题已转给主人：{cut(str(exc), 80)}") from exc
    saved = read_object(path)
    if saved.get("status", "pending") == "pending":
        # The task process that asked has the answer in its inbox and has not said it took it: the question stays the lead's
        # (``answering``), and the sweep settles it, or hands it to the owner when it never landed.
        given = {str(key): {"answers": [str(a) for a in (value if isinstance(value, list) else [value])]} for key, value in (answers or {}).items()}
    else:
        given = saved.get("answers") if isinstance(saved.get("answers"), dict) else {}
        # What the answer's own path made of its status by now (submitting, answered): the hold is the lead's, and ends here.
        update_question(path, lambda row: triage.moved(row, triage.ANSWERED, by="lead", at=time.time(), answering=0)
                        if triage.state(row) == triage.HELD else None)
    label = topic_label(state, ctx.topic_id) or ctx.topic_id[:8]
    line = f"{label} · {summary(record, given)}"
    log_row(state, project["id"], ground_row(found) + line, token=record["token"], topic=ctx.topic_id, kind="answered",
            evidence=[{key: item[key] for key in ("ref", "kind", "level", "layer", "part")} for item in found])
    return {"label": label, "status": saved.get("status"), "line": line}


ENV = None  # app/butler_env.py ButlerEnv, set by ProjectLoop: non-None once the project loop is wired (app/interaction.py reads it).


def attach(env) -> None:
    global ENV
    ENV = env
