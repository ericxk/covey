"""网页上发来的操作怎么执行 (docs/butler.md 网页工作台, COVEY_WEB=1): ``Ops.run`` does one operation of the page's outbox (core/web_sync.py
KINDS) as the owner's own and says (state, message) for the ack.

- ``say``: the words are the owner's, in the topic: a running task takes them as a supplement (app/steering.py deliver_steer, as
  app/butler.py supplement does), else they are queued as the topic's next turn (app/triage.py ``correct`` does the same: alias the message
  ``web-<op_id>`` to the topic, event_context, append_history, enqueue_chat_job). To a topic that waits for the owner's answer the words
  are its 只发补充说明 (app/answers.py skip_all_fields through the question's own submit path): its questions unanswered, the words sent
  as the note, as the card's button does; with several questions waiting, or one the project's lead holds, they are refused. To the butler's key (core/butler.py butler_topic_id) the words are a message the owner wrote at the top level of
  the private chat: ``hear`` (app/unified.py web_message) takes it through the entry of a Feishu message, access, the duplicate check,
  the extensions and the butler, so the butler answers, opens a topic or hands on as ever. The message id ``web-<op_id>`` is no Feishu
  message: nothing quotes it, replies in its thread or reacts on it (core/topics.py context_for_topic gives the turn no reply target,
  core/placement.py quiet and ``marked``); the butler's words are new top-level messages.
  To a project's lead (core/projects.py lead_record; COVEY_PROJECTS=1) the words are the owner's quoted reply to the project card: the lead's
  topic is spoken to as any (a running turn takes them as a supplement), the project's own word wake-ups aside; a project that is over, or
  a lead that cannot be read, is ``failed``.
- ``forget``: one entry of a project's memory is voided as 删 on the project card does it (core/project_memory.py delete); ``done`` says 已作废 with
  the start of its text, an entry that is not there is ``failed``.
- ``seen``: the topic's latest result is marked as seen, as the owner's read receipt in Feishu does (core/topic_seen.py record_reads), so
  the board, the Pins and 手上的事 and the next push (``unseen`` false) follow. Asked again it says done and writes nothing.
- ``answer``: the question's own submit path (app/questions.py submit_answer) with the fields of app/answers.py answer_fields as the owner's
  own answer (``owner``: a question without options takes the text written). ``skip`` true: 只发补充说明, its ``note`` required.
- ``stop``: the topic's task stops as a click on its card's 停止 does: forwarded to its task runner when it has one (app/runner_ipc.py), else
  stopped here (app/task_controls.py cancel_reply_job_from_card), as app/butler_tracks.py Tracks.stop does. A question the topic waits on is
  closed too, as a project's lead stops a member topic (app/binding.py retire_open_questions: cancelled with the reason ``web_stop``, the
  turn waiting on it cancelled); a topic with neither is ``failed``.
- ``reopen``: a project that is folded away or paused opens again by the path the owner's own turn takes (app/project_loop.py reopen).
- ``tidy``: 整理专项 (app/project_tidy.py start, COVEY_PROJECT_TIDY on): ``scope`` "" for everything, a project id or a word; ``done`` says
  已开始整理，结果发到私聊 (the 整理专项 card goes to the private chat), or 正在整理（12/40） while one runs; off, or too few topics, is ``failed``.
The topic or project has to be the owner's private chat's, known and not over; anything else is ``failed`` with the reason.

Rules (AGENTS.md): nothing is sent from under a record's lock; a write to a task is not replayed (the ledger, core/web_sync.py, keeps an
operation from running twice); a task process is not touched except through the click it already takes."""
from __future__ import annotations

import dataclasses
import json
import re
import time
from pathlib import Path

from covey.app import project_loop
from covey.app.answers import AnswerIncomplete, answer_fields, skip_all_fields
from covey.app.runtime import job_stage
from covey.core import board_read, project_memory, projects, triage, web_sync
from covey.core.attachments import message_words
from covey.core.butler import is_butler_root
from covey.core.history_storage import append_history
from covey.core.jobs import JOB_STOP_ACTION_KIND, topic_job_records
from covey.core.state import read_object
from covey.core.steer import SteerRefused, SteerUncertain
from covey.core.topics import remember_topic_message, topic_file
from covey.cards.question_cards import SUPPLEMENT
from covey.feishu.events import REPLY_CONTEXT

DONE, FAILED = "done", "failed"
SAY_CHARS = 20000
WEB_STOP_NOTE = "你在网页上停了这个话题"
PREFIX = web_sync.PREFIX  # The message id of the owner's words from the page: ``web-<op_id>``.
TOPIC_ID = re.compile(r"[0-9a-f]{16,128}")
TOKEN = re.compile(r"[0-9A-Za-z_-]{6,128}")


class Ops:
    """The page's operations done in this Bridge. ``env``: app/butler_env.py ButlerEnv (runtime, topics, state_dir). ``hear(event)``: takes
    the owner's top-level message ``event`` as the Feishu consumer would give it and answers why it did not take it ('' when it did);
    None: the butler cannot be spoken to from the page."""

    def __init__(self, env, hear=None) -> None:
        self.env = env
        self.hear = hear

    @property
    def runtime(self):
        return self.env.runtime

    def run(self, row) -> tuple:
        """(state, message) of one outbox row; never raises."""
        kind = str(row.get("kind") or "")
        try:
            handler = {"say": self.say, "answer": self.answer, "stop": self.stop, "reopen": self.reopen, "seen": self.seen,
                       "forget": self.forget, "tidy": self.tidy}.get(kind)
            if handler is None:
                raise ValueError(f"不认识的操作：{kind[:20]}")
            chat = self.runtime.access.owner_chat()
            if not chat:
                raise ValueError("Covey 还不知道你的私聊，先在飞书里和机器人说句话")
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            return DONE, handler(Path(self.env.state_dir), chat, row, payload) or "已执行"
        except ValueError as exc:
            return FAILED, str(exc)
        except Exception as exc:  # noqa: BLE001 - the page is told, the Bridge goes on
            self.runtime.log(f"web op {kind} failed: {type(exc).__name__}")
            return FAILED, f"执行出错：{type(exc).__name__}"

    def topic(self, state, chat, key):
        """(app, ctx) of the owner's topic ``key``; ValueError when it is not one (unknown, another chat's, retired, the butler's)."""
        found = self.env.topics.target(state, str(key or ""), chat)
        if found is None:
            raise ValueError("这个话题不认识，或不在你的私聊里，或已经结束")
        app, ctx, _ = found
        return app, dataclasses.replace(ctx, topic_id=str(key))

    # -- say ---------------------------------------------------------------------------------------------------------------

    def say(self, state, chat, row, payload) -> str:
        topic, text = str(row.get("topic_key") or ""), str(payload.get("text") or "").strip()
        if not text:
            raise ValueError("话是空的")
        if len(text) > SAY_CHARS:
            raise ValueError(f"话太长（超过 {SAY_CHARS} 字）")
        if is_butler_root(topic, chat):
            return self.tell_butler(chat, row, text)
        lead = projects.lead_record(state, topic, chat)
        app = self.lead_app(state, lead) if lead else self.topic(state, chat, topic)[0]
        if self.asking(state, chat, topic):
            if lead:
                raise ValueError("负责人正在等你答题，请在题目里回答")
            return self.note_to_question(state, chat, topic, row, text)
        message = PREFIX + str(row.get("op_id"))
        owner = self.runtime.owner_open_id or ""
        remember_topic_message(state, topic, chat, message)
        where = app.submission.event_context(app.harness.context, {"chat_id": chat, "message_id": message})
        if where.topic_id != topic:
            raise ValueError("这个话题现在不能收话")
        project_loop.heard(state, topic, text)  # The owner's word in a project's thread: a receipt, as a Feishu message makes.
        steering = app.steering
        jobs = sorted(self.runtime.jobs.topic_jobs(topic, chat), key=lambda job: int(job.job_id))
        live = next((job for job in reversed(jobs) if steering.steer_stage(job) != "ended"), None)
        if live is not None and steering.steer_stage(live) != "paused":
            try:
                requeue = {"text": text, "prompt": text, **({"sender_id": owner} if owner else {})}
                steering.deliver_steer(where, live, text, message, asked=message_words(text), requeue=requeue)
                return "已作为补充交给在做的任务"
            except SteerUncertain:
                return "补充可能已经送到，没有重发；请到任务里看一眼"
            except SteerRefused as exc:
                self.runtime.log(f"web say queued instead job=#{live.job_id}: {exc}")
        append_history(where.history_path, "user", text, {"message_id": message, "chat_id": chat, **({"sender_id": owner} if owner else {})})
        queued = app.submission.enqueue_chat_job(where, text, chat, message, prompt_content=text, sender_id=owner)
        if queued is None:
            raise ValueError("话题现在排不上这一轮，话已记进历史但没有开始")
        return "已开下一轮"

    def lead_app(self, state, record):
        """The Harness app of the project's lead, whose topic is the page's say target as the project card's quoted reply is in Feishu
        (app/projects.py lead_target); ValueError for a project that is over or whose lead cannot be spoken to."""
        from covey.app.projects import lead_target
        if record.get("state") in (projects.DONE, projects.CANCELLED):
            raise ValueError("这个专项已经结束，负责人不再收话")
        found = lead_target(self.env, record)
        if found is None:
            raise ValueError("这个专项的负责人话题读不到，话没有发出")
        return found[0]

    def tell_butler(self, chat, row, text) -> str:
        """``text`` as the owner's message at the top level of the private chat (``hear``); the butler's route is the one a Feishu
        message takes. ValueError (said to the page) when the butler is off or cannot take it."""
        owner = self.runtime.owner_open_id or ""
        if self.hear is None or not owner:
            raise ValueError("管家现在收不了网页上的话")
        message = PREFIX + str(row.get("op_id"))
        event = {"message_id": message, "chat_id": chat, "chat_type": "p2p", "sender_id": owner, "sender_type": "user",
                 "message_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False),
                 REPLY_CONTEXT: True, "create_time": str(int(time.time() * 1000))}
        why = self.hear(event)
        if why:
            raise ValueError(why)
        return "已交给管家"

    def note_to_question(self, state, chat, topic, row, text) -> str:
        """The owner's words to a topic waiting on a question: its 只发补充说明 (every question unanswered, the words the note)."""
        from covey.core.store import directory_store
        waiting = sorted((record for _, record in directory_store(state / "questions").query(chat_id=chat, topic_id=topic, status="pending")
                          if record.get("status", "pending") == "pending" and not record.get("display_pending")),
                         key=lambda record: record.get("created_at") or 0)
        if any(triage.held(record) for record in waiting):
            raise ValueError("这个问题还在专项负责人手上，到时间会转给你")
        if len(waiting) != 1:
            raise ValueError("有几道题在等你，请在题目里回答，或在题目里只发补充说明")
        record = waiting[0]
        app, ctx = self.topic(state, chat, topic)
        fields = skip_all_fields(record.get("questions") or [], {SUPPLEMENT: text})
        app.questions.submit_answer(ctx, chat, str(record.get("token")), fields, PREFIX + str(row.get("op_id")))
        return "已作为补充说明交给等你回答的任务（题目算没答）"

    @staticmethod
    def asking(state, chat, topic) -> bool:
        """Whether a question of ``topic`` is shown to the owner and waits for the answer."""
        from covey.core.store import directory_store
        return any(row.get("status", "pending") == "pending" and not row.get("display_pending")
                   for _, row in directory_store(state / "questions").query(chat_id=chat, topic_id=topic, status="pending"))

    # -- answer ------------------------------------------------------------------------------------------------------------

    def answer(self, state, chat, row, payload) -> str:
        token, topic = str(payload.get("token") or ""), str(row.get("topic_key") or "")
        if not TOKEN.fullmatch(token):
            raise ValueError("没有问题编号")
        record = read_object(state / "questions" / (token + ".json"))
        if not record or record.get("chat_id") != chat or (topic and record.get("topic_id") != topic):
            raise ValueError("这个问题不认识，或不属于你的私聊")
        if record.get("status", "pending") != "pending":
            raise ValueError("这个问题已经有回答，或已经取消")
        if triage.held(record):
            raise ValueError("这个问题还在专项负责人手上，到时间会转给你")
        topic = str(record.get("topic_id") or topic)
        if is_butler_root(topic, chat):
            raise ValueError("管家的问题请在飞书里回答")
        app, ctx = self.topic(state, chat, topic)
        note = payload.get("note")
        note = str(note).strip() if isinstance(note, str) else ""
        if payload.get("skip") is True:
            try:
                fields = skip_all_fields(record.get("questions") or [], {SUPPLEMENT: note})
            except AnswerIncomplete:
                raise ValueError("只发补充说明要先写上补充说明") from None
            app.questions.submit_answer(ctx, chat, token, fields, PREFIX + str(row.get("op_id")))
            return "补充说明已交给任务（题目算没答）"
        fields = answer_fields(record, payload.get("answers"), note, owner=True)
        app.questions.submit_answer(ctx, chat, token, fields, PREFIX + str(row.get("op_id")))
        return "答案已交给任务"

    # -- seen --------------------------------------------------------------------------------------------------------------

    def seen(self, state, chat, row, payload) -> str:
        topic = str(row.get("topic_key") or "")
        if not TOPIC_ID.fullmatch(topic):
            raise ValueError("没有话题编号")
        data = read_object(topic_file(state, topic))
        if not data or data.get("chat_id") != chat:
            raise ValueError("这个话题不认识，或不在你的私聊里")
        if is_butler_root(topic, chat):
            return "管家的对话没有“没看”的结果"
        return "已记为看过" if board_read.mark_seen(state, chat, topic, time.time()) else "已经记为看过"

    # -- forget ------------------------------------------------------------------------------------------------------------

    def forget(self, state, chat, row, payload) -> str:
        """One entry of a project's memory voided, as 删 on the project card does it (core/project_memory.py delete: a tombstone the lead's
        pack keeps for 30 days, so it is not noted again)."""
        pid = str(row.get("project_key") or payload.get("project_key") or "")
        entry = str(payload.get("entry_id") or "")
        if not projects.valid_id(pid):
            raise ValueError("没有专项编号")
        record = projects.read_record(state, pid)
        if not record or record.get("chat_id") != chat:
            raise ValueError("这个专项不认识，或不在你的私聊里")
        found = next((each for each in project_memory.live(state, record) if each["id"] == entry), None)
        if found is None or not project_memory.delete(state, pid, entry):
            raise ValueError("这条记忆已经不在了")
        return "已作废：" + found["text"][:40]

    # -- stop --------------------------------------------------------------------------------------------------------------

    def stop(self, state, chat, row, payload) -> str:
        from covey.app import runner_ipc
        topic = str(row.get("topic_key") or "")
        app, ctx = self.topic(state, chat, topic)
        jobs = sorted(self.runtime.jobs.topic_jobs(topic, chat), key=lambda job: int(job.job_id))
        job = next((job for job in reversed(jobs) if job_stage(job) != "ended"), None)
        waiting = self.asking(state, chat, topic) or any(
            record.get("status") == "waiting_user" for record in topic_job_records(state, topic, chat_id=chat))
        if job is None and not waiting:
            raise ValueError("这个话题现在没有在做、排队或等你回答的任务")
        said = []
        if job is not None:
            message = PREFIX + str(row.get("op_id"))
            remember_topic_message(state, topic, chat, message)
            stop_ctx = app.submission.event_context(app.harness.context, {"chat_id": chat, "message_id": message})
            action = {"kind": JOB_STOP_ACTION_KIND, "job_id": str(job.job_id), "message_id": job.message_id, "by": "web"}
            click = {"chat_id": chat, "message_id": message, "event_id": f"web-stop-{row.get('op_id')}",
                     "operator_id": self.runtime.owner_open_id or "", "action_value": action}
            if runner_ipc.forward_card_action(self.runtime, stop_ctx, click, action):
                said.append(f"已转给任务进程停止 #{job.job_id}")
            else:
                app.controls.cancel_reply_job_from_card(stop_ctx, action, chat, message)
                said.append(f"已请求停止 #{job.job_id}")
        if waiting:
            from covey.app import binding
            binding.retire_open_questions(self.env.topics, app, ctx, chat, reason="web_stop", job_status="cancelled", by="web",
                                          message=WEB_STOP_NOTE)
            said.append("等你回答的题已收掉")
        return "；".join(said)

    # -- tidy --------------------------------------------------------------------------------------------------------------

    def tidy(self, state, chat, row, payload) -> str:
        """整理专项 from the page (app/project_tidy.py start): ``scope`` empty for all, else a project id or a word. done with
        已开始整理，结果发到私聊, or with how far the one running has got (正在整理（12/40））; off, ``failed``."""
        from covey.core import project_proposals

        tidy = getattr(self.env, "tidy", None)
        if tidy is None or not project_proposals.enabled():
            raise ValueError("整理专项没有开（COVEY_PROJECT_TIDY）")
        words = tidy.start(origin="web", scope=" ".join(str(payload.get("scope") or "").split())[:40])
        if words != project_proposals.STARTED_WORDS and not words.startswith(project_proposals.BUSY_PREFIX):
            raise ValueError(words)
        return words

    # -- reopen ------------------------------------------------------------------------------------------------------------

    def reopen(self, state, chat, row, payload) -> str:
        pid = str(row.get("project_key") or "")
        if not projects.valid_id(pid):
            raise ValueError("没有专项编号")
        record = projects.read_record(state, pid)
        if not record or record.get("chat_id") != chat:
            raise ValueError("这个专项不认识，或不在你的私聊里")
        if record.get("state") in (projects.DONE, projects.CANCELLED):
            raise ValueError("这个专项已经结束，不能重开")
        if record.get("state") == projects.ACTIVE:
            return "这个专项已经在做了"
        if record.get("state_reason") not in projects.RESUMABLE:
            raise ValueError("这个专项现在的停住原因不能由网页重开，请在飞书里处理")
        projects.update(state, pid, project_loop.reopen)
        return "已重新展开"
