"""网页上发来的操作怎么执行 (docs/web.md, COVEY_WEB=1): ``Ops.run`` does one operation of the page's outbox (core/web_sync.py
KINDS) as the owner's own and says (state, message) for the ack.

- ``say``: the words are the owner's, in the topic: a running task takes them as a supplement (app/steering.py deliver_steer, as
  app/butler.py supplement does), else they are queued as the topic's next turn (app/triage.py ``correct`` does the same: alias the message
  ``web-<op_id>`` to the topic, event_context, append_history, enqueue_chat_job). To a topic that waits for the owner's answer the words
  are taken as a message in its thread is in Feishu (app/interaction.py _interactive_command): with one question waiting they are its
  answer, written (``answer_0``, through the question's own submit path); with several waiting, or one the project's lead holds, they are
  refused, to be answered on the question, and the question is never closed by them (a 补充说明 alone, the page's handiest box,
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
  own answer (``owner``: a question without options takes the text written, ``{"skip": true}`` is 不回答此题, and with ``note`` a question
  left out is 不回答此题, as 提交回答 has it since v29). ``skip`` true, and ``ask`` true from a page before v29: the ``note`` alone, which it
  requires. 不处理 beside 提交回答 (cards/question_cards.py aside_offered) by its own path (app/questions.py): ``aside`` true, and
  ``restore`` true is its 撤销. A question that no longer waits (``settled``):
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
- ``say`` with ``to``, ``integrator`` or ``quote`` (data contract v31, COVEY_COLLAB=1, docs/web.md 协作与三层记忆): without ``to`` of two or
  more and without ``quote.collab`` the words go as above, to the row's topic. With them core/collab.py ``route`` says who owns the
  delivery: the butler or the one picked as before; the butler once, told whom he must find the owner among (the lead alone); a
  collaboration started through ``Collaboration.start`` (``env.collab``: the integrator asks each helper, the owner's words are its
  root ``web-<op_id>``); a quote of a collaboration asks each one picked again (``clarify``), or, none picked, goes to its integrator's
  topic. Off, or without the part, they are ``failed`` (协作没开); the single-target say is the same either way.
- ``stop`` with ``collab``: only that collaboration is stopped (``Collaboration.stop``), the integrator's own task and every helper's work
  stay; off it is ``failed``, never a plain stop of the integrator's task.
- ``recover`` (data contract v33): a button of a turn that did not succeed, ``job_id`` its task, done as the task card's button does it
  (``recover``): 检查并继续 redelivers a saved result or question when the record says so, never running the model again for it.
- ``login`` (self-hosted page, docs/web.md 网页登录确认): a browser asks to sign in, payload ``{request_id, code, browser, os, at}``; the 网页登录确认
  card goes to the private chat (app/web_login.py) and the owner's 允许 / 不是我 is told to the page as a ``login`` item; ``done`` says the card was sent
  (not that the owner agreed), a request over 5 minutes old or without the part is ``failed``.
- ``project_stop``, ``project_finish`` (data contract v34): 停止 as the 专项卡's 停止专项 (or the pause card's 停) does it, the tasks of the
  lead and the member topics asked to stop; 收尾 as the pause card's 收尾, or the owner's choice the lead finishes from, the lead's own gates
  (a member topic running or waiting, the 目标检查表) checked first and their reason said, nothing done.
- ``memory_undo`` (data contract v34): 不对 on a turn's "记下了" line as on its answer card (app/memory_collect.py take_back); a line taken
  back already says so, ``done``.
- ``memory_edit`` with ``v: 43`` (the page's 记忆, the owner's only): add, edit, move, archive, restore, void, accept, reject,
  accept_all, reject_all, keep_left, keep_right, keep_both, promote, undo of the one store (app/web_memory.py edit_v43; import
  and ignore are gone since v45), the operation's id the change's ``undo_token``. v31's ``memory_fix`` and v37's ``memory_edit``
  without ``v`` are gone (T908 M3b): an older page's is refused as an operation not known.
- ``revisit_cancel`` (data contract v35): 取消 on a topic's 定时回看, ``payload.set_at`` the time it was asked for (core/revisits.py cancel);
  gone already says so, ``done``; one Covey is looking at now, or a newer one that replaced it, is ``failed`` and stays.
The topic or project has to be the owner's private chat's, known and not over; anything else is ``failed`` with the reason.

Rules (AGENTS.md): nothing is sent from under a record's lock; a write to a task is not replayed (the ledger, core/web_sync.py, keeps an
operation from running twice); a task process is not touched except through the click it already takes."""
from __future__ import annotations

import dataclasses
import json
import re
import time
from pathlib import Path

from covey.app import auto_retry, memory_collect, project_clicks, project_lead, project_loop, project_tools
from covey.app.answers import WEB, AnswerBy, AnswerIncomplete, answer_fields, skip_all_fields, submitted_answer
from covey.app.binding import stale_job_card
from covey.app.runtime import job_stage, supplement_target_of
from covey.app import quota_watch, self_upgrade, web_files, web_memory, web_quota
from covey.app.web_settings import WebSettings
from covey.app.web_turns import STOP_AUTO_RETRY
from covey.core import board_read, inbound_ledger, collab, collab_records, failure, project_memory, projects, revisits, triage, turn_source, web_sync
from covey.core.attachments import message_words
from covey.core.butler import is_butler_root
from covey.core.history_storage import append_history
from covey.core.jobs import JOB_STOP_ACTION_KIND, find_job_record, topic_job_records
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
# ``recover``'s actions (data contract v33): core/failure.py's continue, retry, diagnose and split, and 停止自动重试.
RECOVER_ACTIONS = (failure.CONTINUE, failure.RETRY, failure.DIAGNOSE, failure.SPLIT, STOP_AUTO_RETRY)
# The message id of the turn a ``recover`` starts: ``recover-<op_id>``, as a card click's is its event id. Not ``web-``: those are the
# owner's own words (app/project_lead.py OWNER_PREFIXES, app/butler_asks.py, app/collab_flow.py), and a retry or a 看原因 says none.
RECOVER_PREFIX = "recover-"
# 收尾 from the page on a project that is on (no pause put to the owner): the lead's ``owner_choice`` receipt, as the pause card's.
WEB_FINISH = ("主人在网页上选了收尾：这个专项不再开新的话题。请总结做完了什么、还有什么没做完，用 project_note 标 report 告诉主人，"
              "然后调 project_finish（还有话题在做时它会拒绝，等它们结束再收尾）。")
RECOVER_DONE = {failure.DIAGNOSE: "已开一轮只读排查：看看这一轮为什么没成功，不改文件、不重跑",
                failure.SPLIT: "已开始拆小再试：把这件事拆成更小的几步重新做"}


class Ops:
    """The page's operations done in this Bridge. ``env``: app/butler_env.py ButlerEnv (runtime, topics, state_dir). ``hear(event)``: takes
    the owner's top-level message ``event`` as the Feishu consumer would give it and answers why it did not take it ('' when it did);
    None: the butler cannot be spoken to from the page."""

    def __init__(self, env, hear=None, fetch=None, login=None) -> None:
        self.env = env
        self.web_login = login  # app/web_login.py WebLogin: the 网页登录确认 card of ``login``; None: ``login`` is refused.
        self.hear = hear
        self.fetch = fetch  # request(method, path) of the page's /openapi (app/web_push_client.py Client.request): a say's files; None: none taken.
        self.web_settings = WebSettings(env)  # The 设置 page's snapshot (app/web_sync.py) and its changes (``settings``).
        self._refresher = None  # web_quota.Refresher of ``quota_refresh``, made on the first one (it needs the state directory).
        self._upgrader = None  # self_upgrade, made once the state directory is known (build_context fills it in before any thread).

    @property
    def runtime(self):
        return self.env.runtime

    @property
    def self_upgrade(self):
        """版本与更新 (``upgrade``, v39, app/self_upgrade.py): its checks and the upgrade it starts, one for the Bridge (app/web_sync.py
        pushes its item and runs its thread); None before the state directory is known."""
        if self._upgrader is None and self.env.state_dir:
            self._upgrader = self_upgrade.shared(self.env.state_dir, self.runtime.log)
        return self._upgrader

    @property
    def quota_refresher(self):
        """账号与额度's 刷新 (``quota_refresh``, v42, app/web_quota.py Refresher): one for the Bridge, so its cooldown is one."""
        if self._refresher is None and self.env.state_dir:
            self._refresher = web_quota.Refresher(  # The apps are read when asked: they come after this object.
                self.env.state_dir, log=self.runtime.log,
                command=lambda: web_quota.agy_command(getattr(getattr(self.env, "topics", None), "apps", None)))
        return self._refresher

    def run(self, row) -> tuple:
        """(state, message) of one outbox row; never raises."""
        kind = str(row.get("kind") or "")
        try:
            handler = {"say": self.say, "answer": self.answer, "stop": self.stop, "reopen": self.reopen, "seen": self.seen,
                       "forget": self.forget, "tidy": self.tidy, "settings": self.settings,
                       "dismiss": self.dismiss, "tidy_decide": self.tidy_decide, "launch": self.launch,
                       "switch": self.switch, "recover": self.recover,
                       "login": self.login, "project_finish": self.project_finish, "project_stop": self.project_stop,
                       "memory_undo": self.memory_undo, "revisit_cancel": self.revisit_cancel,
                       "memory_edit": self.memory_edit, "upgrade": self.upgrade,
                       "quota_refresh": self.quota_refresh}.get(kind)
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
        if self.collaborating(topic, payload):
            return self.say_collab(state, chat, row, payload)
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
                inbound_ledger.note(state, message, "supplement", topic_id=topic, job_id=live.job_id,
                                    event={"chat_id": chat, "content": text})
                return (f"已并进排队中的任务 #{live.job_id}，它开始时一起处理" if (saved or {}).get("merged")
                        else f"已作为补充交给在做的任务 #{live.job_id}")
            except SteerUncertain:
                return "补充可能已经送到，没有重发；请到任务里看一眼"
            except SteerRefused as exc:
                self.runtime.log(f"web say queued instead job=#{live.job_id}: {exc}")
        append_history(where.history_path, "user", text, {"message_id": message, "chat_id": chat, **({"sender_id": owner} if owner else {})})
        queued = app.submission.enqueue_chat_job(where, text, chat, message, prompt_content=text, sender_id=owner)
        inbound_ledger.note(state, message, "job" if queued is not None else "not_queued", topic_id=topic,
                            job_id=getattr(queued, "job_id", None), event={"chat_id": chat, "content": text})
        if queued is None:
            raise ValueError("话题现在排不上这一轮，话已记进历史但没有开始")
        return "已开下一轮"

    # -- say to several, or to a collaboration ------------------------------------------------------------------------------

    @staticmethod
    def picked(payload) -> list:
        """The participants the say named (``to``), in the order picked, each once; [] for none or a field that is no list."""
        raw = payload.get("to")
        return list(dict.fromkeys(str(key) for key in raw if isinstance(key, str) and key)) if isinstance(raw, list) else []

    @staticmethod
    def quoted_collab(payload) -> str:
        quote = payload.get("quote")
        return str(quote.get("collab") or "") if isinstance(quote, dict) else ""

    def collaborating(self, topic, payload) -> bool:
        """Whether the say goes through ``route`` (several picked, one that is not the page's topic, or a quote of a collaboration). A say
        to one, the topic it is sent to, is as it was before the page knew of collaborations."""
        picked = self.picked(payload)
        if self.quoted_collab(payload) or len(picked) > 1:
            return True  # Off, or without the part, ``say_collab`` says so: nothing goes to one of them instead.
        return len(picked) == 1 and payload.get("room") is not True and picked[0] != topic and turn_source.enabled() and self.collab is not None

    @property
    def collab(self):
        """The Bridge's ``collab`` part (app/collab_flow.py Collaboration; ButlerEnv.collab), None without one."""
        return getattr(self.env, "collab", None)

    def collab_ready(self):
        """The part, when COVEY_COLLAB is on and the Bridge has it; ValueError (said to the page) otherwise."""
        from covey.app import collab_checks
        if not turn_source.enabled():
            raise ValueError(collab_checks.OFF)
        if self.collab is None:
            raise ValueError("这个 Bridge 没有协作部件")
        return self.collab

    def collab_ctx(self, state, chat, topic=""):
        """The context ``Collaboration`` is called with: the integrator's own when it is a topic, else the default app's."""
        found = self.env.topics.target(state, topic, chat) if topic else None
        if found is not None:
            return found[1]
        from covey.app.web_launch import base_app
        return base_app(self.env)[1]

    def say_collab(self, state, chat, row, payload) -> str:
        """The page's say with several picked or a quote of a collaboration: ``route`` (core/collab.py) decides, as it does in Feishu."""
        from covey.app import collab_checks as checks
        topic, text = str(row.get("topic_key") or ""), str(payload.get("text") or "").strip()
        room = payload.get("room") is True
        picked, quoted = self.picked(payload), self.quoted_collab(payload)
        gateway = self.collab_ready()

        def who(key):
            found = checks.participant(state, chat, key)
            if found is None:
                raise ValueError(f"{str(key)[:12]} 不能参与协作（不在你的私聊里，或是代答话题）")
            return found
        mentions = tuple(who(key) for key in picked)
        here = None if room else who(topic)
        quote = None
        raw = payload.get("quote") if isinstance(payload.get("quote"), dict) else {}
        if quoted:
            record = collab_records.read(state, quoted)
            if not record or record.get("chat_id") != chat:
                raise ValueError("找不到被引用的这次协作")
            lead = who(str((record.get("integrator") or {}).get("key") or ""))
            quote = collab.Quote(of=(who(str(raw.get("topic_key"))) if raw.get("topic_key") else lead), collab=quoted, integrator=lead)
        elif raw.get("topic_key"):
            quoted_by = checks.participant(state, chat, str(raw.get("topic_key")))
            quote = collab.Quote(of=quoted_by) if quoted_by is not None else None
        integrator = who(str(payload.get("integrator"))) if payload.get("integrator") else None
        route = collab.route(collab.Entry(where=collab.ROOM if room else collab.IN_TOPIC, here=here, mentions=mentions, quote=quote,
                                          integrator=integrator))

        def label(key):
            return checks.label(state, chat, who(key))
        plain = {key: value for key, value in payload.items() if key not in ("to", "integrator", "quote")}
        if route.to == collab.REFUSED:
            raise ValueError(route.reason)
        if route.to == collab.DESK:
            from covey.core.butler import butler_topic_id
            return self.say(state, chat, {**row, "topic_key": butler_topic_id(chat)}, plain)
        if route.to == collab.DIRECT:
            return self.say(state, chat, {**row, "topic_key": route.owner}, plain)
        if route.to == collab.ROUTER:
            from covey.core.butler import butler_topic_id
            names = "、".join(label(key) for key in route.helpers)
            note = f"（主人在网页上点了 {names}，想请他们办这件事：先找出这件事归谁，再按他的意思办。）\n"
            return self.tell_butler(chat, row, self.attach(state, butler_topic_id(chat), note + text, web_files.listed(payload.get("files"))))
        ctx = self.collab_ctx(state, chat, route.owner)
        if route.collab:
            if not picked:  # His words to the collaboration, no one picked: its integrator takes them as it takes his words.
                return self.say(state, chat, {**row, "topic_key": route.owner}, plain)
            said, refused = [], []
            for key in picked:
                done = gateway.clarify(ctx, route.collab, key, text)
                if done.get("refused"):
                    refused.append(f"{label(key)}：{done['refused']}")
                else:
                    said.append(label(key))
            if not said:
                raise ValueError("没有问出去：" + "；".join(refused))
            return "已再问 " + "、".join(said) + ("；没问成：" + "；".join(refused) if refused else "")
        made = gateway.start(ctx, chat, root={"message_id": PREFIX + str(row.get("op_id")), "words": text},
                             integrator=route.owner, asks=[{"to": key, "question": text} for key in route.helpers],
                             owner_named=list(picked), asserted_by=turn_source.PARTY_OWNER)
        if made.get("refused"):
            raise ValueError(str(made["refused"]))
        asks = "、".join(label(key) for key in route.helpers)
        return (f"这次协作之前已经发起过：{label(route.owner)} 汇总，问 {asks}" if made.get("again")
                else f"已发起协作：{label(route.owner)} 汇总，问 {asks}")

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
        if is_butler_root(topic, chat):
            raise ValueError("管家的配置在设置的「配置」里改")
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

    def web_by(self, row):
        """The AnswerBy (app/answers.py) of an answer given on the web page: the owner's (the page is his), through the web,
        its op id the message it came in by (WAVE12 C2)."""
        return AnswerBy(party=turn_source.PARTY_OWNER, channel=WEB, open_id=self.runtime.owner_open_id or "",
                        message_id=PREFIX + str(row.get("op_id")))

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
        app.questions.submit_answer(ctx, chat, str(record.get("token")), {"answer_0": text}, PREFIX + str(row.get("op_id")),
                                     by=self.web_by(row))  # The page is the owner's.
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
        # ``skip``, and ``ask`` from a page before v29 (先问一句 folded into 提交回答): the 补充说明 alone.
        if payload.get("skip") is True or payload.get("ask") is True:
            try:
                fields = skip_all_fields(record.get("questions") or [], {SUPPLEMENT: note})
            except AnswerIncomplete:
                raise ValueError("补充说明是空的：写上你想说的再提交") from None
            app.questions.submit_answer(ctx, chat, token, fields, PREFIX + str(row.get("op_id")), by=self.web_by(row))
            return "补充说明已交给任务（题目算没答）"
        fields = answer_fields(record, payload.get("answers"), note, owner=True)
        app.questions.submit_answer(ctx, chat, token, fields, PREFIX + str(row.get("op_id")), by=self.web_by(row))
        if note and not any(key.startswith(("option_", "answer_")) for key in fields):
            return "补充说明已交给任务（题目算没答）"
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
        return web_launch.launch(self.env, row.get("op_id"), chat, payload, fetch=self.fetch)

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
        if payload.get("collab"):
            return self.stop_collab(state, chat, str(payload.get("collab")))
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

    def stop_collab(self, state, chat, collab_id) -> str:
        """Only the collaboration ``collab_id`` stopped (``Collaboration.stop``): its asks waiting or running and its continuation turn;
        the integrator's own task and everything a helper does stay. Never the plain stop of the integrator's topic."""
        gateway = self.collab_ready()
        record = collab_records.read(state, collab_id)
        if not record or record.get("chat_id") != chat:
            raise ValueError("找不到这次协作")
        if record.get("state") != collab.OPEN:
            return "这次协作已经" + ("停了" if record.get("state") == collab.CANCELLED else "做完了") + "，没有要停的"
        live = [ask for ask in record.get("asks") or () if ask.get("state") in (collab.CHECKING, collab.QUEUED, collab.TAKEN)]
        reply = any(each.get("state") == "queued" for each in record.get("replies") or ())
        gateway.stop(self.collab_ctx(state, chat, str((record.get("integrator") or {}).get("key") or "")), collab_id, by="web")
        parts = [f"{len(live)} 次咨询" if live else "", "汇总方的续接轮" if reply else ""]
        return "已停止这次协作" + (f"（停了 {'、'.join(part for part in parts if part)}）" if live or reply else "") + "：各话题手上别的工作没有动"

    # -- recover -----------------------------------------------------------------------------------------------------------

    def login(self, state, chat, row, payload) -> str:
        if self.web_login is None:
            raise ValueError("这个 Covey 不能发网页登录确认卡")
        return self.web_login.send(state, chat, self.env.profile, payload)

    def recover(self, state, chat, row, payload) -> str:
        """检查并继续 (``action`` continue, and retry: 现在重试), 看原因 (diagnose), 拆小再试 (split) and 停止自动重试 (stop_auto_retry) of
        the task ``job_id`` of the row's topic, as the buttons of its task card do (data contract v33): the same calls
        (app/task_controls.py retry_topic, which redelivers a saved result or question instead of running the model again when the
        record says so; app/submission.py queue_follow_up; app/task_controls.py stop_auto_retry), in the context an automatic retry
        runs in (app/auto_retry.py retry_context: a topic's, the butler's or a lead's). Only what the record allows now
        (core/failure.py failure_view's actions; continue and retry also while an automatic retry is pending: it runs at once and
        the scheduled one is dropped, never both), and continue not on a task from before the topic's latest 换配置 (a card's
        click is refused the same way, app/card_routing.py job_route). Done when something was started or sent, else failed with
        why not, nothing done: the topic has a task at work, the task went on already, a second click (the op ledger keeps an
        op_id from running twice, and a task once retried is not retried again)."""
        topic, action = str(row.get("topic_key") or ""), str(payload.get("action") or "")
        if action not in RECOVER_ACTIONS:
            raise ValueError(f"不认识的恢复操作：{action[:20]}")
        job_id = str(payload.get("job_id") or "").strip()
        if not job_id.isdigit():
            raise ValueError("没有任务编号")
        if not TOPIC_ID.fullmatch(topic):
            raise ValueError("没有话题编号")
        data = read_object(topic_file(state, topic))
        if not data or data.get("chat_id") != chat or data.get("retired"):
            raise ValueError("这个话题不认识，或不在你的私聊里，或已经结束")
        record = next((each for each in topic_job_records(state, topic, chat_id=chat) if str(each.get("job_id")) == job_id), None)
        if record is None:
            raise ValueError(f"这个话题里没有任务 #{job_id}")
        app = getattr(self.env.topics, "apps", {}).get(record.get("backend"))
        if app is None:
            raise ValueError("这个任务的 Harness 现在不可用")
        try:
            ctx = auto_retry.retry_context(app, record, auto_retry.record_topic(app, record))
        except (OSError, ValueError, KeyError, TypeError):
            raise ValueError("这个话题现在读不到，没有执行") from None
        if ctx.topic_id != topic:
            raise ValueError("这个话题现在读不到，没有执行")
        message, pending = RECOVER_PREFIX + str(row.get("op_id")), auto_retry.pending(record)
        if action == STOP_AUTO_RETRY:
            if not pending:
                raise ValueError(f"任务 #{job_id} 没有待执行的自动重试")
            app.controls.stop_auto_retry(ctx, chat, int(job_id))
            if (find_job_record(state, record["key"]) or {}).get("auto_retry_state") != "cancelled":
                raise ValueError("自动重试已经开始，没有停下；要停在跑的那一轮，点它的停止")
            return "已停止自动重试；要接着做时点「检查并继续」"
        offered = (failure.failure_view(record) or {}).get("actions") or []
        if action in (failure.CONTINUE, failure.RETRY):
            if record.get("retried_by"):  # 检查并继续 on its card, an automatic retry or another click went first.
                moved = find_job_record(state, str(record["retried_by"])) or {}
                raise ValueError(f"这一轮已经接着做了（任务 #{moved.get('job_id') or '?'}），没有再开一次")
            if not pending and not {failure.CONTINUE, failure.RETRY} & set(offered):
                raise ValueError("这一轮现在不能接着做" + ("：结果已经发出" if record.get("delivery_ok") else ""))
            if stale_job_card(data, record):
                raise ValueError("话题之后换过配置，这一轮不能再接着做；在话题里说一句，就按新配置开下一轮")
            said = app.controls.retry_topic(ctx, chat, message, job_id=int(job_id))
            after = find_job_record(state, record["key"]) or {}
            moved = after.get("retried_by") and after.get("retried_by") != record.get("retried_by")
            sent = any(after.get(name) is True and record.get(name) is not True for name in ("delivery_ok", "question_delivery_ok"))
            if moved or sent:
                return said
            raise ValueError(said)
        if action not in offered:
            raise ValueError("这一轮现在没有这一步可做")
        said, started = app.submission.follow_up_job(ctx, chat, message, int(job_id), action)
        if said or started is None:
            raise ValueError(said or "这一步没有排上，话题里有说明")
        return f"{RECOVER_DONE[action]}（任务 #{started}）"

    # -- tidy --------------------------------------------------------------------------------------------------------------

    def tidy(self, state, chat, row, payload) -> str:
        """整理专项 from the page (app/project_tidy.py start): ``scope`` empty for all, else a project id or a word. done with
        已开始整理，进度和结果显示在整理面板上 (the panel follows it, app/tidy_web.py), or with how far the one running has got
        (正在整理（12/40））; off, ``failed``."""
        from covey.core import project_proposals

        tidy = getattr(self.env, "tidy", None)
        if tidy is None or not project_proposals.enabled():
            raise ValueError("整理专项没有开（COVEY_PROJECT_TIDY）")
        words = tidy.start(origin="web", scope=" ".join(str(payload.get("scope") or "").split())[:40])
        if words not in (project_proposals.STARTED_WORDS, project_proposals.WEB_STARTED_WORDS) \
                and not words.startswith(project_proposals.BUSY_PREFIX):
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

    # -- 专项的收尾、停止 (data contract v34) -------------------------------------------------------------------------------

    def owned_project(self, state, chat, row, what) -> dict:
        """The record of the row's project, the owner's (app/access.py is_owner) and in his chat; ValueError says why not."""
        pid = str(row.get("project_key") or "")
        if not projects.valid_id(pid):
            raise ValueError("没有专项编号")
        record = projects.read_record(state, pid)
        if not record or record.get("chat_id") != chat:
            raise ValueError("这个专项不认识，或不在你的私聊里")
        if self.runtime.access.is_owner(self.runtime.owner_open_id or "") is not True:
            raise ValueError(f"只有主人能{what}专项")
        return record

    def project_stop(self, state, chat, row, payload) -> str:
        """停止 of a project from the page: what 停止专项 on its 专项卡 does to one that is on (``ACTIVE``: app/project_clicks.py
        stop_project) and what 停 on its pause card does to one a guard paused and put to the owner (``project_loop.answer_pause``):
        停住 as the owner's stop, the lead's and the member topics' queued and running tasks asked to stop (stop source ``web``, the
        owner's own). Any other state, or the pause card's choice made already, says why and does nothing."""
        record = self.owned_project(state, chat, row, "停止")
        pid, name = str(record["id"]), str(record.get("name") or "专项")[:30]
        ask = record.get("pause_ask") if isinstance(record.get("pause_ask"), dict) else None
        if record.get("state") == projects.ACTIVE:
            try:
                projects.set_state(state, pid, projects.PAUSED, projects.OWNER_STOP)
            except ValueError:  # The lead, a guard or a click moved it first.
                raise ValueError(f"专项「{name}」刚变了状态，没有停：刷新后再看") from None
        elif record.get("state") == projects.PAUSED and ask is not None and not ask.get("choice"):
            outcome = project_loop.answer_pause(state, pid, "stop")
            if outcome != "ok":
                raise ValueError(project_clicks.ASK_REFUSED[outcome].format(name=name))
        else:
            raise ValueError(f"专项「{name}」已经是{'做完' if record.get('state') == projects.DONE else '停住'}的，没有要停的")
        lead, stopped = str(record.get("lead_topic_id") or ""), 0
        try:
            app, ctx = self.topic(state, chat, lead)
            stopped = project_clicks.stop_tasks(app.controls, ctx, [lead, *(record.get("threads") or ())], chat,
                                                PREFIX + str(row.get("op_id") or ""), by="web")
        except ValueError:  # No lead topic to stop from (gone): the project is 停住 all the same.
            pass
        return f"已停住专项「{name}」，已请 {stopped} 个任务停下（在跑的命令可能要过一会儿才停）。要继续，点「继续」"

    def project_finish(self, state, chat, row, payload) -> str:
        """收尾 of a project from the page: the owner's choice of 收尾 its lead acts on, as on the pause card (``answer_pause`` and the
        lead's ``owner_choice`` receipt, ``pause_receipt``) when a guard's pause was put to him, else the same receipt in words
        of his own choosing on a project that is on (the lead sums up and calls project_finish). The lead's own gates go first
        and refuse with their reason, nothing done: a member topic running or waiting for the owner (app/project_lead.py
        live_members), the 目标检查表 or 协作 gaps not dealt with (app/project_tools.py checklist_gate)."""
        record = self.owned_project(state, chat, row, "收尾")
        pid, name = str(record["id"]), str(record.get("name") or "专项")[:30]
        if record.get("state") not in (projects.ACTIVE, projects.PAUSED):
            raise ValueError(f"专项「{name}」已经{'做完' if record.get('state') == projects.DONE else '取消'}了，不用再收尾")
        live = project_lead.live_members(state, record)
        if live:
            raise ValueError(f"还有话题在做或等你（{'、'.join(live)}），不能收尾：等它们结束，或先处理掉")
        unmet, _, _ = project_tools.checklist_gate(state, record)
        if unmet:  # The reason as the lead gets it, without the steps the lead is told to take.
            raise ValueError(unmet.removesuffix(project_tools.FINISH_NEXT) + "等负责人补齐，或在专项里告诉它哪些不要了")
        ask = record.get("pause_ask") if isinstance(record.get("pause_ask"), dict) else None
        if record.get("state") == projects.PAUSED and ask is not None and not ask.get("choice"):
            outcome = project_loop.answer_pause(state, pid, "finish")
            if outcome != "ok":
                raise ValueError(project_clicks.ASK_REFUSED[outcome].format(name=name))
            project_loop.pause_receipt(state, pid, "finish", (projects.read_record(state, pid) or {}).get("pause_ask") or {})
        else:
            projects.update(state, pid, project_loop.owner_spoke)  # His word: on again if it was folded or stopped.
            project_lead.add_receipt(state, pid, "owner_choice",
                                     WEB_FINISH + (project_loop.CHOICE_CHECKLIST if turn_source.enabled() else ""))
        return f"已请负责人收尾专项「{name}」：它会总结做完了什么、还有什么没做完，再结束这个专项"

    # -- 记忆的「不对」 (data contract v34) ------------------------------------------------------------------------------------

    def memory_undo(self, state, chat, row, payload) -> str:
        """不对 on a "记下了" line of a turn on the page: what the line's 不对 on its answer card does (app/memory_collect.py
        take_back, the card's lines redrawn); a line taken back already says so, done."""
        topic = str(row.get("topic_key") or "")
        job_id, entry_id = str(payload.get("job_id") or "").strip(), str(payload.get("entry_id") or "").strip()
        if not TOPIC_ID.fullmatch(topic):
            raise ValueError("没有话题编号")
        if not job_id.isdigit() or not entry_id:
            raise ValueError("没有任务编号或记忆编号")
        if self.runtime.access.is_owner(self.runtime.owner_open_id or "") is not True:
            raise ValueError("只有主人能撤销记忆")
        app, ctx = self.topic(state, chat, topic)
        record = next((each for each in topic_job_records(state, topic, chat_id=chat) if str(each.get("job_id")) == job_id), None)
        if record is None:
            raise ValueError(f"这个话题里没有任务 #{job_id}")
        note = next((each for each in record.get("memory_notes") or () if isinstance(each, dict) and each.get("entry_id") == entry_id), None)
        if note is None:
            raise ValueError("这一轮没有记下这条记忆")
        notes = memory_collect.take_back(state, topic, record, entry_id)
        if notes is None:
            return "这条记忆已经撤销过了"
        try:
            memory_collect.show(ctx, app.controls.messenger, record, notes, self.runtime.log)
        except Exception as exc:  # noqa: BLE001 - taken back all the same; the card's lines follow on its next redraw
            self.runtime.log(f"web memory_undo card not redrawn: {type(exc).__name__}")
        return "已撤销：" + " ".join(str(note.get("text") or "").split())[:40]

    def upgrade(self, state, chat, row, payload) -> str:
        """``upgrade`` (v39, docs/web.md 版本与更新): ``check`` looks for a newer version now, ``run`` starts the upgrade in a process of
        its own (app/self_upgrade.py). Only with an upgrader set up; never with a topic or a project."""
        if row.get("topic_key") or row.get("project_key"):
            raise ValueError("upgrade 不带话题和专项")
        action = str(payload.get("action") or "")
        if action not in ("check", "run"):
            raise ValueError("upgrade 的 action 只能是 check 或 run")
        upgrader = self.self_upgrade
        if upgrader is None or not upgrader.on():
            raise ValueError("这台 Covey 没配网页上升级：在终端里按安装说明升级")
        if action == "run":
            return upgrader.start(row.get("op_id") or "")
        status = upgrader.check()
        if status.get("error"):
            raise ValueError(f"没查成：{status['error']}")
        if status.get("blocked"):
            return f"已检查：现在不能升级（{status['blocked']}）"
        count = status.get("count") or 0
        if self_upgrade.available(status):
            return f"已检查：有 {count} 个更新" if count else "已检查：网页要更新到和 Bridge 同一版"
        return "已检查：已经是最新"

    def quota_refresh(self, state, chat, row, payload) -> str:
        """``quota_refresh`` (v42, docs/web.md 账号与额度): read every quota source and ask agy's /usage of every account now, in a
        thread of its own; one in COOLDOWN (inside it the page gets the snapshots as they are and is told it just did). Only the
        owner; never with a topic or a project, and only with a source to read."""
        if row.get("topic_key") or row.get("project_key"):
            raise ValueError("quota_refresh 不带话题和专项")
        if self.runtime.access.is_owner(self.runtime.owner_open_id or "") is not True:
            raise ValueError("只有主人能刷新额度")
        if not quota_watch.wanted():
            raise ValueError("没有可读的额度来源（COVEY_QUOTA 关着，或没有配来源）")
        refresher = self.quota_refresher
        if refresher is None:
            raise ValueError("Covey 还不知道状态目录")
        return refresher.ask()

    def memory_edit(self, state, chat, row, payload) -> str:
        """The page's memory, the owner's only (``v`` 43, data contracts v43/v45): one of the 15 actions through core/memstore.py
        (app/web_memory.py edit_v43), the operation's id the change's undo token (the receipt says so in the log the page reads).
        An older page's 记忆 tab (v37, no ``v``) is refused: it only read since T908 M3b."""
        if not web_memory.enabled():
            raise ValueError("记忆没开（COVEY_MEMORY）")
        if self.runtime.access.is_owner(self.runtime.owner_open_id or "") is not True:
            raise ValueError("只有主人能改记忆")
        if payload.get("v") != web_memory.V43:
            raise ValueError("这个页面版本太旧，刷新网页后到「记忆」里改")
        done = web_memory.edit_v43(state, payload, token=web_memory.token_of(row.get("op_id")), op_id=str(row.get("op_id") or ""))
        if done["undo_token"]:
            self.runtime.log(f"web memory_edit {payload.get('action')} refs={len(done['refs'])} token={done['undo_token']}")
        return done["message"]

    # -- 定时回看的「取消」 (data contract v35) ------------------------------------------------------------------------------

    def revisit_cancel(self, state, chat, row, payload) -> str:
        """取消 on a topic's 定时回看 (the 看板's 取消, core/revisits.py cancel): the record asked for at ``payload.set_at`` is removed, a newer one
        stays. Done when it is gone already (taken back or run); ValueError, nothing removed, when Covey is looking at it now (FIRED) or
        a newer one replaced it. The owner's, on a topic of his private chat. The collector looks at the topic at once (web_sync.py)."""
        topic = str(row.get("topic_key") or "")
        if not TOPIC_ID.fullmatch(topic):
            raise ValueError("没有话题编号")
        set_at = payload.get("set_at")
        if isinstance(set_at, bool) or not isinstance(set_at, (int, float)) or set_at != set_at or set_at <= 0:
            raise ValueError("没有预约时刻 set_at")
        if self.runtime.access.is_owner(self.runtime.owner_open_id or "") is not True:
            raise ValueError("只有主人能取消定时回看")
        self.topic(state, chat, topic)
        found = revisits.read(state, topic) if revisits.enabled() else {}
        if not found or found.get("chat_id") != chat:
            return "这条定时回看已经没有了（已经回看过，或已取消）"
        if abs(float(found.get("set_at") or 0) - float(set_at)) >= 0.001:
            raise ValueError("这条定时回看已经换成了新的，没有取消，请看页面上最新的")
        if found.get("state") == revisits.FIRED:
            raise ValueError("Covey 已经在回看了，这一次取消不了")
        if not revisits.cancel(state, topic, set_at):
            return "这条定时回看已经没有了（已经回看过，或已取消）"
        self.runtime.log(f"revisit cancelled from the page topic={topic[-8:]}")
        return "已取消定时回看"

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
