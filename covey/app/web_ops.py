"""网页上发来的操作怎么执行 (docs/web.md, COVEY_WEB=1): ``Ops.run`` does one operation of the page's outbox (core/web_sync.py
KINDS) as the owner's own and says (state, message) for the ack.

- ``say``: the words are the owner's, in the topic: a running task takes them as a supplement (app/steering.py deliver_steer, as
  app/butler.py supplement does), else they are queued as the topic's next turn (app/triage.py ``correct`` does the same: alias the message
  ``web-<op_id>`` to the topic, event_context, append_history, enqueue_chat_job). To a topic that waits for the owner's answer the words
  are taken as a message in its thread is in Feishu (app/interaction.py _interactive_command): with one question waiting they are its
  answer, written (``answer_0``, through the question's own submit path); with several waiting, or one the project's lead holds, they are
  refused, to be answered on the question, and the question is never closed by them (as its 只发补充说明, the page's handiest box
  would close the question). To the butler's key (core/butler.py butler_topic_id) the words are a message the owner wrote at the top level of
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
  own answer (``owner``: a question without options takes the text written, ``{"skip": true}`` is 不回答此题). ``skip`` true: 只发补充说明,
  its ``note`` required. The form's lesser buttons (cards/question_cards.py extras) by their own paths (app/questions.py): ``ask`` true is
  先问一句 with ``note`` as the words, ``aside`` true is 不处理, ``restore`` true is its 撤销. A question that no longer waits (``settled``):
  the same answer it has is ``done`` and sent nothing; any other is ``failed`` saying who closed it and how (you, the lead, a newer
  question, maintenance, a stop or a switch) and what to do now.
- ``stop``: the topic's task stops as a click on its card's 停止 does: forwarded to its task runner when it has one (app/runner_ipc.py), else
  stopped here (app/task_controls.py cancel_reply_job_from_card), as app/butler_tracks.py Tracks.stop does. A question the topic waits on is
  closed too, as a project's lead stops a member topic (app/binding.py retire_open_questions: cancelled with the reason ``web_stop``, the
  turn waiting on it cancelled); a topic with neither is ``failed``.
- ``reopen``: a project that is folded away or paused opens again by the path the owner's own turn takes (app/project_loop.py reopen).
- ``settings``: one change on the page's 设置 (app/web_settings.py ``change``: a row of 新话题 or 管家, 记住的工作区's 移除, a workspace's
  用途, 脱敏's switches and terms (many at once), a group's 停用 / 启用) through the 设置 card's own code, the owner only (app/access.py ``is_owner``); ``done`` says the
  card's receipt (已保存：之后新开的话题照这个来。), a row the page may not change (私聊顶层的消息, 免@, ...) is ``failed``.
- ``launch``: a new topic with the page's choices, started at once (app/web_launch.py): its topic card at the top level of the
  private chat, its first turn the owner's words; a worktree's gets the launch card prefilled.
- ``switch``: the topic's configuration changed as 🔁 换配置's 确认更换 changes it (app/web_switch.py, data contract v21); its thread in Feishu
  says so too. Refused while a task of it is queued or runs, for nothing changed, and for a worktree that would have to be made now.
- ``say`` with ``files`` (data contract v21): the owner's attachments, uploaded to the page, are taken from it (app/web_files.py, ``fetch``:
  the page's client) into the topic's incoming_media and listed after the words as a Feishu message's are; one that cannot be taken fails the say.
- ``dismiss``: 不管了, the topic taken off the board as the 看板 card's 不管了 does (app/pins.py dismiss); without the board ``failed``.
- ``tidy_decide``: 好 (``choice`` ok) or 不用 (decline) on the 整理专项 panel's suggestion ``proposal`` (data contract v16), through its
  card's own click (app/project_tidy.py web_decide); done says what came of it, failed why nothing was done (decided already, ...).
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
from covey.app.answers import AnswerIncomplete, answer_fields, skip_all_fields, submitted_answer
from covey.app.runtime import job_stage, supplement_target_of
from covey.app import web_files
from covey.app.web_settings import WebSettings
from covey.core import board_read, project_memory, projects, triage, web_sync
from covey.core.attachments import message_words
from covey.core.butler import is_butler_root
from covey.core.history_storage import append_history
from covey.core.jobs import JOB_STOP_ACTION_KIND, topic_job_records
from covey.core.state import read_object
from covey.core.steer import SteerRefused, SteerUncertain
from covey.core.topics import remember_topic_message, topic_file
from covey.cards.question_cards import SET_ASIDE, SUPPLEMENT
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

    def __init__(self, env, hear=None, fetch=None) -> None:
        self.env = env
        self.hear = hear
        self.fetch = fetch  # request(method, path) of the page's /openapi (app/web_push_client.py Client.request): a say's files; None: none taken.
        self.web_settings = WebSettings(env)  # The 设置 page's snapshot (app/web_sync.py) and its changes (``settings``).

    @property
    def runtime(self):
        return self.env.runtime

    def run(self, row) -> tuple:
        """(state, message) of one outbox row; never raises."""
        kind = str(row.get("kind") or "")
        try:
            handler = {"say": self.say, "answer": self.answer, "stop": self.stop, "reopen": self.reopen, "seen": self.seen,
                       "forget": self.forget, "tidy": self.tidy, "settings": self.settings,
                       "dismiss": self.dismiss, "tidy_decide": self.tidy_decide, "launch": self.launch,
                       "switch": self.switch}.get(kind)
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
        files = web_files.listed(payload.get("files"))
        if not text and not files:
            raise ValueError("话是空的")
        if len(text) > SAY_CHARS:
            raise ValueError(f"话太长（超过 {SAY_CHARS} 字）")
        if is_butler_root(topic, chat):
            return self.tell_butler(chat, row, self.attach(state, topic, text, files))
        lead = projects.lead_record(state, topic, chat)
        app = self.lead_app(state, lead) if lead else self.topic(state, chat, topic)[0]
        text = self.attach(state, topic, text, files)
        if self.asking(state, chat, topic):
            if lead:
                raise ValueError("负责人正在等你答题，请在题目里回答")
            return self.answer_with_words(state, chat, topic, row, text)
        if lead and lead.get("state") == projects.DONE:  # His words open a finished project again (a steer reaches no owner_turn).
            projects.update(state, lead["id"], project_loop.owner_spoke)
        message = PREFIX + str(row.get("op_id"))
        owner = self.runtime.owner_open_id or ""
        remember_topic_message(state, topic, chat, message)
        where = app.submission.event_context(app.harness.context, {"chat_id": chat, "message_id": message})
        if where.topic_id != topic:
            raise ValueError("这个话题现在不能收话")
        project_loop.heard(state, topic, text)  # The owner's word in a project's thread: a receipt, as a Feishu message makes.
        steering = app.steering
        # The task that runs, not a message queued behind it (app/runtime.py supplement_target).
        live = supplement_target_of(self.runtime.jobs.topic_jobs(topic, chat), steering.steer_stage)
        if live is not None and steering.steer_stage(live) != "paused":
            try:
                requeue = {"text": text, "prompt": text, **({"sender_id": owner} if owner else {})}
                saved = steering.deliver_steer(where, live, text, message, asked=message_words(text), requeue=requeue)
                return (f"已并进排队中的任务 #{live.job_id}，它开始时一起处理" if (saved or {}).get("merged")
                        else f"已作为补充交给在做的任务 #{live.job_id}")
            except SteerUncertain:
                return "补充可能已经送到，没有重发；请到任务里看一眼"
            except SteerRefused as exc:
                self.runtime.log(f"web say queued instead job=#{live.job_id}: {exc}")
        append_history(where.history_path, "user", text, {"message_id": message, "chat_id": chat, **({"sender_id": owner} if owner else {})})
        queued = app.submission.enqueue_chat_job(where, text, chat, message, prompt_content=text, sender_id=owner)
        if queued is None:
            raise ValueError("话题现在排不上这一轮，话已记进历史但没有开始")
        return "已开下一轮"

    def attach(self, state, topic, text, files) -> str:
        """``text`` with ``files`` taken from the page into the topic's incoming_media and listed after it; ``text`` itself without files."""
        if not files:
            return text
        if self.fetch is None:
            raise ValueError("Covey 现在取不到网页上的附件")
        return web_files.with_files(text, web_files.fetch(self.fetch, state / "topics" / topic / "incoming_media", files))

    def switch(self, state, chat, row, payload) -> str:
        from covey.app import web_switch
        topic = str(row.get("topic_key") or "")
        if is_butler_root(topic, chat) or projects.lead_record(state, topic, chat):
            raise ValueError("管家和负责人的配置不在网页上换")
        if not self.runtime.access.is_owner(self.runtime.owner_open_id or ""):
            raise ValueError("只有主人能换配置")
        return web_switch.switch(self.env, row.get("op_id"), chat, topic, payload)

    def lead_app(self, state, record):
        """The Harness app of the project's lead, whose topic is the page's say target as the project card's quoted reply is in Feishu
        (app/butler_projects.py lead_target); ValueError for a project that was cancelled or whose lead cannot be spoken to (a finished
        one takes the owner's words and opens again, ``say``)."""
        from covey.app.butler_projects import lead_target
        if record.get("state") == projects.CANCELLED:
            raise ValueError("这个专项已经取消，负责人不再收话")
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

    def answer_with_words(self, state, chat, topic, row, text) -> str:
        """The owner's words to a topic waiting on a question, as a message in its thread in Feishu: the answer of the one question waiting
        (written, as the card's answer box takes it); several, or one the lead holds, are refused and stay open."""
        from covey.core.store import directory_store
        waiting = sorted((record for _, record in directory_store(state / "questions").query(chat_id=chat, topic_id=topic, status="pending")
                          if record.get("status", "pending") == "pending" and not record.get("display_pending")),
                         key=lambda record: record.get("created_at") or 0)
        if any(triage.held(record) for record in waiting):
            raise ValueError("这个问题还在专项负责人手上，到时间会转给你")
        if len(waiting) != 1 or len(waiting[0].get("questions") or []) != 1:
            raise ValueError("有几道题在等你，请在题目里逐题回答")
        record = waiting[0]
        app, ctx = self.topic(state, chat, topic)
        app.questions.submit_answer(ctx, chat, str(record.get("token")), {"answer_0": text}, PREFIX + str(row.get("op_id")))
        return "已作为这道题的回答交给任务"

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
        if payload.get("restore") is True or payload.get("aside") is True:
            return self.aside(state, chat, row, record, restore=payload.get("restore") is True)
        if record.get("status", "pending") != "pending":
            return self.settled(state, record, payload)
        if triage.held(record):
            raise ValueError("这个问题还在专项负责人手上，到时间会转给你")
        topic = str(record.get("topic_id") or topic)
        if is_butler_root(topic, chat):
            raise ValueError("管家的问题请在飞书里回答")
        app, ctx = self.topic(state, chat, topic)
        note = payload.get("note")
        note = str(note).strip() if isinstance(note, str) else ""
        if payload.get("ask") is True:
            if not note:
                raise ValueError("先问一句要先写上你想问的")
            if not app.questions.ask_first(ctx, chat, token, note, PREFIX + str(row.get("op_id")), sender=self.runtime.owner_open_id or ""):
                return "这句已经问过了"
            return "已问话题，回答在话题里；题目还开着，看完再答"
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

    def aside(self, state, chat, row, record, *, restore=False) -> str:
        """不处理 (or, ``restore``, its 撤销) on the question ``record`` as its card's button does (app/questions.py set_aside, restore):
        off 等你 with every question 不回答此题, told to the topic's next turn; until then 撤销 opens it again. A question that waits no
        more and was not set aside is refused as an answer to it is (``settled``)."""
        token, topic = str(record.get("token") or ""), str(record.get("topic_id") or "")
        if is_butler_root(topic, chat):
            raise ValueError("管家的问题请在飞书里处理")
        if not restore and record.get("status", "pending") not in ("pending", SET_ASIDE):
            return self.settled(state, record, {"skip": True})
        if not restore and triage.held(record):
            raise ValueError("这个问题还在专项负责人手上，到时间会转给你")
        app, ctx = self.topic(state, chat, topic)
        event = PREFIX + str(row.get("op_id"))
        if restore:
            if not app.questions.restore(ctx, chat, token, event):
                return "这组题已经在等你回答了"
            return "已撤销：题目重新等你回答"
        if not app.questions.set_aside(ctx, chat, token, event, by=self.runtime.owner_open_id or ""):
            return "这组题已经先不管了"
        return "先不管了：每题记为不回答，话题下一轮开始时告诉模型；在那之前可以撤销"

    @staticmethod
    def settled(state, record, payload) -> str:
        """The answer to a question that no longer waits: the same answer the owner gave it already is done (a second click,
        or the page showing the form until its next push), nothing sent again; any other is refused with who closed the
        question and how, and what to do now (app/web_pairing.py settled_words)."""
        from covey.app import web_pairing
        from covey.core.store import directory_store
        topic = str(record.get("topic_id") or "")
        if record.get("status") == "answered" and triage.of(record).get("state") != triage.ANSWERED \
                and record.get("response_kind") != "feedback" and payload.get("skip") is not True:
            try:
                given = submitted_answer(record, answer_fields(record, payload.get("answers"), payload.get("note"), owner=True))[0]
            except (ValueError, KeyError, TypeError):
                given = None
            if given is not None and given == record.get("answers"):
                return "这题你已经答过同样的答案，已经交给任务，这次没有重复发送"
        records = topic_job_records(state, topic) if topic else []
        rows = [row for _, row in directory_store(state / "questions").query(topic_id=topic)] if topic else []
        raise ValueError(web_pairing.settled_words(record, records, rows))

    # -- launch ------------------------------------------------------------------------------------------------------------

    def launch(self, state, chat, row, payload) -> str:
        """A new topic with the choices made on the page (app/web_launch.py launch), the owner's only (app/access.py
        ``is_owner``); ``done`` says 已开 T23「…」：直接开工."""
        from covey.app import web_launch
        if not self.runtime.access.is_owner(self.runtime.owner_open_id or ""):
            raise ValueError("只有机器人主人能开话题")
        return web_launch.launch(self.env, row.get("op_id"), chat, payload)

    def opened_topic(self, row) -> str:
        """The topic a ``launch`` row opened ('' for none): the push builds it on its next step, so the page can open it at once."""
        from covey.app import web_launch
        if row.get("kind") != "launch" or not self.env.state_dir:
            return ""
        return str(read_object(web_launch.record_path(self.env.state_dir, row.get("op_id"))).get("topic_id") or "")

    # -- dismiss -----------------------------------------------------------------------------------------------------------

    def dismiss(self, state, chat, row, payload) -> str:
        """不管了 on the page: the topic taken off the board as 不管了 on the 看板 card does it (app/pins.py ``dismiss``, the
        board's record; no job or question changes), so the 看板, 手上的事 and the next push (``board`` "") all leave it. Without the
        board (app/overview.py ``dismissable``) it is ``failed``: the page keeps it on this device only."""
        from covey.app.overview import dismissable
        from covey.app.pins import dismiss
        topic = str(row.get("topic_key") or "")
        if not TOPIC_ID.fullmatch(topic):
            raise ValueError("没有话题编号")
        data = read_object(topic_file(state, topic))
        if not data or data.get("chat_id") != chat:
            raise ValueError("这个话题不认识，或不在你的私聊里")
        if not dismissable():
            raise ValueError("没开看板（手上的事和 Pin），只记在这台设备上")
        return "已从看板拿掉" if dismiss(state, chat, [topic], time.time(), "waiting") else "已经拿掉了"

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
        from covey.app.launch import close_open_launches
        starts = bool(board_read.launch_card(state, topic, chat))
        if job is None and not waiting and not starts:
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
        if starts and close_open_launches(ctx, chat, getattr(self.env, "messenger", None), WEB_STOP_NOTE):
            said.append("没开工的启动卡已收掉")
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

    def tidy_decide(self, state, chat, row, payload) -> str:
        """好 or 不用 on one suggestion of the 整理专项 panel (data contract v16): ``proposal`` its id, ``choice`` ok or decline, through
        the click of its card (app/project_tidy.py web_decide), which is redrawn the same. done says what came of it (已拆出专项「…」),
        failed why nothing was done."""
        from covey.core import project_proposals

        tidy = getattr(self.env, "tidy", None)
        if tidy is None or not project_proposals.enabled():
            raise ValueError("整理专项没有开（COVEY_PROJECT_TIDY）")
        choice = str(payload.get("choice") or "")
        if choice not in ("ok", "decline"):
            raise ValueError("choice 只能是 ok 或 decline")
        done, words = tidy.web_decide(str(payload.get("proposal") or "")[:64], choice)
        if not done:
            raise ValueError(words)
        return words

    # -- settings ----------------------------------------------------------------------------------------------------------

    def settings(self, state, chat, row, payload) -> str:
        """One change on the page's 设置 (app/web_settings.py ``change``), as the owner app/access.py knows; the card's receipt."""
        return self.web_settings.change(payload, self.runtime.access.is_owner(self.runtime.owner_open_id or ""))

    # -- reopen ------------------------------------------------------------------------------------------------------------

    def reopen(self, state, chat, row, payload) -> str:
        pid = str(row.get("project_key") or "")
        if not projects.valid_id(pid):
            raise ValueError("没有专项编号")
        record = projects.read_record(state, pid)
        if not record or record.get("chat_id") != chat:
            raise ValueError("这个专项不认识，或不在你的私聊里")
        if record.get("state") == projects.CANCELLED:
            raise ValueError("这个专项已经取消，不能重开")
        if record.get("state") == projects.ACTIVE:
            return "这个专项已经在做了"
        if record.get("state") == projects.DONE:
            projects.update(state, pid, project_loop.owner_spoke)
            return "已重新打开，负责人等你的新要求"
        if record.get("state_reason") not in projects.RESUMABLE:
            raise ValueError("这个专项现在的停住原因不能由网页重开，请在飞书里处理")
        projects.update(state, pid, project_loop.reopen)
        return "已重新展开"
