"""专项的问题分流 (docs/butler.md 专项的问题分流, COVEY_PROJECTS=1 and COVEY_PROJECT_TRIAGE=1; phase 2's P2-5): a member topic's choice
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
  project's ``triage`` list (the 专项卡's 分流记录); an answer's row has the 不对 button (``correct``).

Rules 1 and 4 (AGENTS.md): a record is changed under its own lock (``update_question``) and nothing is sent from under it; the
triage state moves only along core/triage.py TRIAGE_TRANSITIONS."""
from __future__ import annotations

import dataclasses
import time
from pathlib import Path

from covey.app import project_lead
from covey.app.triage_view import held_questions
from covey.app.answers import AnswerIncomplete, answer_fields
from covey.cards.question_cards import triage_element
from covey.core import project_gates, project_memory, projects, triage
from covey.core.jobs import find_job_record, saved_job, upsert_job_record
from covey.core.state import read_object
from covey.core.store import update_question
from covey.core.text import cut
from covey.core.topics import topic_label

LEAD, OWNER = "lead", "owner"
NOTE = "（负责人按专项记忆替主人答的）"  # The 补充说明 of an answer the lead gave.
LOG_KEPT = 40  # Rows of a project's 分流记录.
ROW = 120  # Characters of a row's question and answer.
RECEIPT = "question"  # The inbox receipt's kind.


def question_path(state_dir, token) -> Path:
    return Path(state_dir) / "questions" / (str(token) + ".json")


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

def log_row(state_dir, project_id, text, *, token="", topic="", kind="") -> None:
    """One row of the project's 分流记录 (its ``triage`` list); never raises (the record may be gone)."""
    row = {"at": time.time(), "text": cut(text, 300), **({"token": token} if token else {}), **({"topic": topic} if topic else {}),
           **({"kind": kind} if kind else {})}

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


def escalate(env, record, why, *, by="lead") -> bool:
    """The held question sent on to the owner: its triage moves to ``escalated`` (with ``why``, which its card says), and the card
    goes out through the door the question would have used. True when the card was sent; a card that could not go is made good by
    the topic's 重新投递 (the task's record says its question was not delivered). A question no longer held changes nothing (False)."""
    state = Path(env.state_dir)
    path = question_path(state, record["token"])
    moved = update_question(path, lambda row: {**triage.moved(row, triage.ESCALATED, why=cut(why, 120), by=by, at=time.time()),
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
        upsert_job_record(state, moved.get("job_key"), status="failed", question_delivery_ok=False,
                          status_message="问题要主人答，但问题控件还没送到；请重新投递")
    return ok


def show(env, record) -> bool:
    """The card of ``record`` (a question that was held and is the owner's now) sent, as its own Harness's app sends one."""
    from covey.app.redelivery import delivery_context
    state, topic, chat = Path(env.state_dir), str(record.get("topic_id")), str(record.get("chat_id"))
    found = env.topics.target(state, topic, chat)
    if found is None:
        return False
    app, ctx, _ = found
    ctx = dataclasses.replace(ctx, topic_id=topic)
    if record.get("native_live_question") is True:
        job = app.questions.runtime.jobs.live_job(topic, chat, record.get("job_id"))
        message = app.questions.retry_live_questions(ctx, chat, job) if job is not None else None
        sent = bool(message and message.startswith("原生问题控件已重新投递"))
    else:
        original = find_job_record(state, record.get("job_key")) or {}
        if not original:
            return False
        rebuilt = saved_job(original, chat, topic, original.get("reply_to_message_id") or ctx.reply_to_message_id,
                            checkpoint_question_deferred=original.get("checkpoint_question_deferred", False))
        sent = bool(app.questions.deliver_checkpoint_questions(delivery_context(ctx, original, rebuilt), rebuilt, False))
    if sent:
        try:
            app.questions.display.paint_task_card(ctx, read_object(question_path(state, record["token"])))
        except Exception as exc:  # noqa: BLE001 - the card is out; the task card keeps its older line
            env.runtime.log(f"question triage task card not repainted: {type(exc).__name__}")
    return sent


def sweep(env, now=None) -> None:
    """The Bridge's look at the held questions (app/project_loop.py ProjectLoop.tick): a receipt for each one the lead has not been
    told of, and the owner's turn for each one that waited its 180 seconds, whose project stopped, or that was handed back."""
    if not triage.enabled() or not getattr(env, "state_dir", None):
        return
    state, now = Path(env.state_dir), time.time() if now is None else now
    for path, record in held_questions(state):
        try:
            found = triage.of(record)
            project = projects.read_record(state, found.get("project") or "")
            if project.get("state") != projects.ACTIVE:
                escalate(env, record, "专项现在没有在做", by="project")
            elif triage.expired(record, now):
                escalate(env, record, "3 分钟没人处理", by="timeout")
            elif not found.get("receipt"):
                receipt(state, project, record, now)
        except Exception as exc:  # noqa: BLE001 - the others are looked at all the same
            env.runtime.log(f"question triage sweep failed token={str(record.get('token'))[:8]}: {type(exc).__name__}")


def receipt(state_dir, project, record, now) -> None:
    """The ``question`` receipt of a held question, once (marked on the record after: a crash between the two writes it again,
    which wakes the lead no more than once more)."""
    label = topic_label(Path(state_dir), record.get("topic_id")) or str(record.get("topic_id"))[:8]
    first = (record.get("questions") or [{}])[0]
    project_lead.add_receipt(state_dir, project["id"], RECEIPT,
                             f"{label} 的任务 #{record.get('job_id')} 问：{cut(str(first.get('question') or ''), 160)}"
                             "（交给你：能答就 project_answer，不能就 project_escalate；3 分钟没处理会转给主人）",
                             thread=str(record.get("topic_id") or ""), at=now)
    update_question(question_path(state_dir, record["token"]),
                    lambda row: {**row, "triage": {**triage.of(row), "receipt": True}} if triage.held(row) else None)


def answer(env, app, ctx, chat, project, answers, event_id) -> dict:
    """project_answer: the newest held question of the topic ``ctx`` answered as the owner would, with NOTE as its 补充说明.
    ValueError says what does not fit (the answer is not sent then, and the question stays held)."""
    state = Path(env.state_dir)
    rows = held_questions(state, {ctx.topic_id})
    if not rows:
        raise ValueError("这个话题现在没有交给你的问题（可能已经转给主人，或超过 3 分钟了）。")
    path, record = rows[-1]
    fields = answer_fields(record, answers, NOTE)
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
    given = saved.get("answers") if isinstance(saved.get("answers"), dict) else {}
    # Whatever the answer's own path made of its status by now (submitting, answered): the hold is the lead's, and ends here.
    update_question(path, lambda row: triage.moved(row, triage.ANSWERED, by="lead", at=time.time(), answering=0)
                    if triage.state(row) == triage.HELD else None)
    label = topic_label(state, ctx.topic_id) or ctx.topic_id[:8]
    line = f"{label} · {summary(record, given)}"
    log_row(state, project["id"], "答了：" + line, token=record["token"], topic=ctx.topic_id, kind="answered")
    return {"label": label, "status": saved.get("status"), "line": line}


# The owner's 不对.

ENV = None  # app/butler_env.py ButlerEnv, set by ProjectLoop: what a click on the 专项卡 needs to reach another topic.
FIX_PREFIX = "triage-fix-"  # The message id of a correction in a member topic (not a wake-up's: core/projects.py is_wake).


def attach(env) -> None:
    global ENV
    ENV = env


def correct(project, token, text) -> str:
    """The owner's correction ``text`` of the answer the lead gave to the question ``token``: queued as a message in the member
    topic (they said it, as 补充), put in the project's decision log (the lead reads the last ten every turn), and marked on the
    row. The line said to the owner. ValueError for a row that is not one of this project's answers or was corrected already."""
    env = ENV
    if env is None:
        raise ValueError("现在不能纠正：Bridge 还没准备好")
    state, pid = Path(env.state_dir), project["id"]
    row = next((r for r in project.get("triage") or () if isinstance(r, dict) and r.get("token") == token and r.get("kind") == "answered"), None)
    if row is None or row.get("corrected"):
        raise ValueError("这一条已经纠正过，或已经不在分流记录里了")
    topic, chat = str(row.get("topic") or ""), str(project.get("chat_id") or "")
    found = env.topics.target(state, topic, chat)
    if found is None:
        raise ValueError("这个话题现在不能收消息，纠正没有发出")
    app, ctx, _ = found
    message = FIX_PREFIX + str(token)[:16]  # One per question: a second click finds the row corrected.
    sent = f"（主人纠正负责人替答的那一条：{row.get('text')}）{text}"
    from covey.core.history_storage import append_history
    from covey.core.topics import remember_topic_message
    remember_topic_message(state, topic, chat, message)
    where = app.submission.event_context(app.harness.context, {"chat_id": chat, "message_id": message})
    if where.topic_id != topic:
        raise ValueError("这个话题现在不能收消息，纠正没有发出")
    append_history(where.history_path, "user", sent, {"message_id": message})
    queued = app.submission.enqueue_chat_job(where, sent, chat, message, prompt_content=sent)
    decision = project_lead.entry(f"主人纠正了分流：{cut(str(row.get('text')), 120)} → {cut(text, 120)}", kind="note")

    def change(data):
        for each in data.get("triage") or ():
            if isinstance(each, dict) and each.get("token") == token and each.get("kind") == "answered":
                each["corrected"] = True
        rows = data.get("decisions") if isinstance(data.get("decisions"), list) else []
        rows.append({**decision, "report": False})
        data["decisions"] = rows[-project_lead.DECISIONS_KEPT:]
    projects.update(state, pid, change)
    if projects.memory_enabled():  # COVEY_PROJECT_MEMORY=1: the owner's word over the lead's answer is a decision of the project too (member topics start with it).
        try:
            project_memory.add(state, pid, f"主人纠正了负责人替答的：{cut(str(row.get('text')), 80)} → {cut(text, 100)}", "decision", "owner_fix")
        except Exception as exc:  # noqa: BLE001 - the correction is sent and in the decision log whether or not the memory took it
            env.runtime.log(f"project memory of a correction failed: {type(exc).__name__}")
    return "已记下：这句话发给了那个话题" + ("" if queued is not None else "（话题现在排不上，没有发出，但已记进决定日志）") + "，也记进专项的决定日志，负责人之后照做。"

