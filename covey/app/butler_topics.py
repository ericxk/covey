"""The butler's tools that change topics (docs/butler.md 开话题、转交、分叉): topic_start, topic_send and
topic_fork, handlers as app/butler_env.py describes them. Only in the owner's private chat.

- ``topic_start`` sends the topic card (cards/topic_card.py) at the chat's top level, opens the topic on it as
  app/forks.py opens a fork on its root, and replaces the card once with the topic's number. A topic that
  starts at once (core/butler_settings.py starts_at_once) is bound as a fork is and its first turn is
  submitted; any other waits on its launch card, prefilled from ``origin.launch`` in its topic.json
  (app/launch.py build_launch_card), with the prompt as the waiting turn.
- ``topic_send`` gives the owner's words to a topic: a supplement to its turn while one runs (app/steering.py
  deliver_steer), else its next turn, headed by the topic's title. A line in the topic's thread says the butler
   passed the owner's words on.

What each topic's model gets is app/butler_handoff.py's: the Bridge's note, the butler's task, the owner's words
from the butler turn's record and its attachments, carried into the topic's incoming_media; its card and
history show the owner's words.
- ``topic_fork`` forks a topic's last finished turn (app/forks.py create_fork) and submits the new topic's first.

Which Harness, model and budget a new topic gets is app/butler_model_choice.py's (``resolve``, used by ``plan_start``).

Each request runs once (rule 6): core/butler_requests.py claim gives it to one caller, what it did is kept in
main-thread/opened/<request>.json (core/butler_opened.py: ``record_path``, ``save``, ``done``), every send's key and every synthetic message id derive from the request id, and
the same request again gets the saved answer. The handlers hold no lock across a Feishu call (rules 1 and 2) but
the one a card's write needs for its sequence (rule 3).
"""
from __future__ import annotations

import dataclasses
import re
import time
from pathlib import Path

from covey.app import dispatch_cards, butler_handoff
from covey.app.forks import FINISHED, fork_token
from covey.app.butler_env import arg_text, reply
from covey.app.butler_model_choice import resolve
from covey.cards.topic_card import topic_card
from covey.app.relay_targets import butler_job_owner_message
from covey.core import devices, butler, butler_settings, projects
from covey.core.attachments import attachment_block
from covey.core.card_sequence import file_card, reserve
from covey.core.harness import backend_capabilities
from covey.core.jobs import find_job_record
from covey.core.statuses import RUNNING
from covey.core.locks import LOCK, topic_lock
from covey.core.butler_opened import done, record_path, request_token, save
from covey.core.butler import is_butler_topic
from covey.core.redaction import TOPIC_KEY as REDACTION_KEY
from covey.core.state import read_object, write_json
from covey.core.history_storage import append_history
from covey.core.steer import SteerRefused, SteerUncertain
from covey.core.topics import (claim_area_root, load_topic, message_topic, read_topic, topic_label,
                                       retired_topic, topic_title, workspace_locked)
from covey.core.workspaces import RUN_MODES, load_config, topic_context, touch_workspace

NO_CHAT = "还不知道主人和机器人的私聊（主人还没在私聊里发过消息），没有执行。"


def lead_of(env, request):
    """The project a lead's call ``request`` is for, as (project id, its name); None when its job is not a project's lead's
    (COVEY_PROJECTS off: never). The lead's topic_start and topic_send open and feed the project's threads."""
    if not projects.enabled():
        return None
    job = find_job_record(Path(env.state_dir), request.job_key) or {}
    topic = str(job.get("topic_id") or "")
    if not topic or not butler.is_lead(topic, job.get("chat_id")):
        return None
    found = projects.topic_project(Path(env.state_dir), topic)
    return (found.id, projects.read_record(Path(env.state_dir), found.id).get("name") or "") if found else None


def dispatch_card(env, request, topic_id, **how):
    """The 派发卡 of the work ``request`` handed to ``topic_id`` (app/dispatch_cards.py), only with
    COVEY_DISPATCH_CARD=1: without it nothing is called and the topic's own cards are all there is. None for a
    project's lead's call: the project's card is the one (phase 2's P2-4)."""
    if dispatch_cards.enabled() and lead_of(env, request) is None:
        token = dispatch_cards.created(env, request, topic_id, request.job_key, token=request_token(request), **how)
        if token and getattr(env, "projects", None) is not None and projects.enabled():
            env.projects.consider(token)  # A quick job that is a long one becomes a project (app/projects.py).


def project_hint(request) -> dict:
    """{"project": True} when the butler called topic_send with ``project`` (this is a long job) and COVEY_PROJECTS is on: the 派发卡's
    record keeps it for the upgrade (app/dispatch_cards.py created)."""
    return {"project": True} if projects.enabled() and request.args.get("project") is True else {}


def find_topic(env, chat, name):
    """(app, context, topic.json) of the owner's topic ``name`` (T12, 412 or its id); a str saying why not."""
    found = topic_ref(env, chat, name)
    return found if isinstance(found, str) else found[1]


def topic_ref(env, chat, name):
    """(topic id, (app, context, topic.json)) of the owner's topic ``name`` as ``find_topic`` finds it; a str saying why not."""
    text = arg_text(name)
    found = None
    number = re.fullmatch(r"[Tt]?(\d+)", text)
    if number:
        for path in (Path(env.state_dir) / "topics").glob("*/topic.json"):
            data = read_object(path)
            if data.get("topic_number") == int(number.group(1)) and data.get("chat_id") == chat:
                found = data.get("topic_id")
                break
    elif re.fullmatch("[0-9a-f]{16,128}", text):
        found = text
    if not found:
        return f"没有找到话题 {text or '（空）'}：用 topics_list 查 T 号。"
    if is_butler_topic(found, chat):
        return "这是管家自己，不是一个话题。"
    if retired_topic(Path(env.state_dir), found):
        return (f"{text} 是已下线的前台话题（11-07 的旧布局），不再接新消息，也不能分叉：这件事你自己在这一轮里做，"
                "或者用 topic_start 开一个新话题。")
    target = env.topics.target(Path(env.state_dir), found, chat)
    if target is None:
        return f"话题 {text} 不在主人私聊里，或者它用的 Harness 本机器人没有启用。"
    if target[2].get("proxy"):
        return f"{text} 是代答话题，管家不往里转话。"
    return found, target


def composed(text, note):
    """What a topic waiting on its question takes as a message written in it: the butler's words, and its note
    on its own line (a next turn or a supplement gets butler_handoff.handoff_prompt instead)."""
    return text + (f"\n（管家：{note}）" if note else "")


def media_dir(ctx) -> Path:
    """The topic's incoming_media, where a message's attachments are saved (app/app.py) and its model reads them."""
    return ctx.history_path.parent / "incoming_media"


def handed(env, request, ctx):
    """(the owner's message the request's butler turn answers, the attachments of it the call hands ``ctx``'s
    topic, carried into its incoming_media)."""
    owner = butler_handoff.asked(env.state_dir, request.job_key)
    lead = lead_of(env, request)
    if lead is not None:
        # The words of a lead's turn are the Bridge's wake-up message unless the owner spoke in it (its message is the
        # owner's, not a synthetic one): never quoted to a thread as the owner's.
        owner = dataclasses.replace(owner if butler_job_owner_message(env.state_dir, request.job_key) else butler_handoff.Asked(),
                                    lead=lead[1] or "专项")
    items = butler_handoff.picked(owner.attachments, request.args.get("attachments"))
    return owner, butler_handoff.carry(items, media_dir(ctx), owner.roots) if items else []


# topic_start ---------------------------------------------------------------------------------------------------

def plan_start(env, args, settings):
    """What topic_start opens: the Harness, model and budget as ``resolve`` finds them; the settings' workspace
    (a workspace there is always the one), else the call's; the settings' run mode and redaction. A str saying
    why not."""
    defaults = settings.topic_defaults
    found = resolve(env.topics, env.state_dir, env.chat(), defaults, args)
    if isinstance(found, str):
        return found
    app, base = found["app"], found["base"]
    config = load_config(Path(env.state_dir), base.default_workdir)
    workspace = defaults.workspace or arg_text(args.get("workspace"))
    if workspace not in config["workspaces"]:
        return f"没有登记工作区「{workspace}」，可选：{'、'.join(config['workspaces'])}。"
    if devices.is_device_value(config["workspaces"][workspace]):
        # Not built yet: the plan's workdir is resolved here, and the topic would be on a path that does not exist.
        return f"工作区「{workspace}」在设备上，管家本期还不能把话题开在设备上；请让主人在启动卡里选它。"
    return {"harness": found["harness"], "model": found["model"], "model_key": found["model_key"],
            "effort": found["effort"], "work_mode": getattr(base, "work_mode", "") or "", "workspace": workspace,
            "workdir": str(Path(config["workspaces"][workspace]).expanduser().resolve()),
            "run_mode": defaults.run_mode if defaults.run_mode in RUN_MODES else "direct",
            "redaction": bool(defaults.redaction and backend_capabilities(app.backend, base).redaction)}


def config_line(env, plan):
    """The topic card's configuration: Harness · 模型 · 思考预算 · 工作区 · 运行方式 (· 脱敏)."""
    app = env.topics.apps[plan["harness"]]
    model = app.cards.model_label(app.harness.context, plan["model"]) if plan["model"] else ""
    model = model.split(" · ", 1)[-1] if model else (plan["model"] or "默认模型")
    return (app.spec.title, model, "思考预算 " + (plan["effort"] or "模型默认"), "工作区 " + plan["workspace"],
            RUN_MODES[plan["run_mode"]], *(["脱敏"] if plan["redaction"] else []))


def launch_draft(plan):
    """The launch card's prefill (app/preselection.py launch_selection's ``draft``)."""
    return {"model": plan["model_key"] or f"{plan['harness']}:{plan['model']}", "effort": plan["effort"],
            "work_mode": plan["work_mode"], "workspace": plan["workspace"], "run_mode": plan["run_mode"],
            "redaction": "on" if plan["redaction"] else "off"}


def topic_start(env, request):
    chat = env.chat()
    if not chat:
        return reply(False, NO_CHAT)
    args = request.args
    title, prompt, why = arg_text(args.get("title"), 40), arg_text(args.get("prompt")), arg_text(args.get("why"), 200)
    if not title or not prompt:
        return reply(False, "title 和 prompt 都不能为空。")
    if butler_handoff.choice_error(args.get("attachments")):
        return reply(False, butler_handoff.choice_error(args.get("attachments")))
    if args.get("project") is True and getattr(env, "projects", None) is not None and projects.enabled() and lead_of(env, request) is None:
        return env.projects.start(request, args)  # 直接开专项: a project, not a topic (COVEY_PROJECTS=1; the butler's call alone).
    path = record_path(env.state_dir, request)
    record = read_object(path)
    if record.get("answer"):
        return record["answer"]  # The same request: the same topic, nothing sent again.
    if not record.get("plan"):
        settings = butler_settings.load(env.state_dir)
        plan = plan_start(env, args, settings)
        if isinstance(plan, str):
            return reply(False, plan)
        # A worktree takes minutes to check out; the launch card creates it and survives a restart.
        plan["start"] = plan["run_mode"] == "direct" and butler_settings.starts_at_once(
            settings, workspace=plan["workspace"], readonly=args.get("readonly") is True,
            user_requested=args.get("user_requested") is True)
        # Judged read-only and started without the owner's click: its first turn says so and is held to it
        # (app/tool_limits.py LOOK). A plan saved before it has none: as before.
        plan["read_only"] = bool(plan["start"]) and args.get("readonly") is True
        record = {"request": request.id, "job_key": request.job_key, "chat_id": chat, "title": title,
                  "why": why, "plan": plan, "at": time.time(), "sequence": 0}
        save(path, record)
    return _open(env, request, path, record, chat, prompt, args)


def _open(env, request, path, record, chat, prompt, args):
    """topic_start's steps, each kept in the request's record so a second try does the same: where the topic goes
    (``_place``), its root message (``send_root``), its topic.json (``bind_topic``), its first turn (``_enqueue``)."""
    plan, token, title = record["plan"], request_token(request), record["title"]
    app = env.topics.apps.get(plan["harness"])
    if app is None:
        return reply(False, f"本机器人没有启用 {plan['harness']}。")
    config = config_line(env, plan)
    _place(env, request, path, record, chat, token)
    if not send_root(env, path, record, chat, title, config, token):
        return reply(False, "话题卡没有发出去，没有开话题；可以稍后再开。")
    root = record["root_message_id"]
    event = {"chat_id": chat, "message_id": root, "root_id": root}
    label = bind_topic(env, app, request, path, record, args, event, config)
    failed = _enqueue(env, app, request, record, chat, prompt, event, token, label)
    if failed:
        return reply(False, f"{label or '话题'}已开，但第一轮没有提交：模型配置无法确认（见话题里的说明）。",
                     topic_id=record["topic_id"], label=label, root_message_id=root, started=False)
    dispatch_card(env, request, record["topic_id"], kind="start", starts=bool(plan["start"]))
    named = f"{label}「{title}」" if label else f"「{title}」"
    where = ("，话题串挂在主人这条提问下面" if record.get("area_root")
             else "，话题串的根是顶层一张小卡（主人这条提问下面已经有一个话题串）" if record.get("area_taken") else "")
    text = (f"已开 {named}：直接开工{where}" if plan["start"]
            else f"已开 {named}：启动卡在话题里，等主人点开始{where}"
            + ("（运行方式是独立 worktree，由启动卡创建）" if plan["run_mode"] == "worktree" else ""))
    return done(path, record, reply(True, text, topic_id=record["topic_id"], label=label, root_message_id=root,
                                     started=bool(plan["start"])))


def _place(env, request, path, record, chat, token):
    """Where the topic goes (``place``), decided once, and the root it takes from that: the owner's question for a
    话题区."""
    if "area_root" not in record:  # Decided once: a second try opens the topic the same way.
        record["area_root"], record["area_taken"] = place(env, request, chat)
        save(path, record)
    if record.get("area_root") and not record.get("root_message_id"):
        # A 话题区: its thread hangs under the owner's question (core/topics.py claim_area_root).
        claim_area_root(env.state_dir, chat, record["area_root"])
        record["root_message_id"] = record["area_root"]
        save(path, record)


def send_root(env, path, record, chat, title, config, token) -> bool:
    """The topic card at the top level as the topic's root, when ``_place`` gave it none; whether it has a root."""
    if record.get("root_message_id"):
        return True
    def created(card_id):
        record["card_id"] = card_id  # Kept first: a second try sends the same card.
        save(path, record)
    card_id, root = env.messenger.create_and_send_card(
        env.profile, chat, topic_card(title, why=record["why"], config=config), env.voice.key("main-topic", token),
        voice=env.voice, card_id=record.get("card_id", ""), on_created=created)
    if not root:
        return False
    record.update(card_id=card_id, root_message_id=root)
    save(path, record)
    return True


def bind_topic(env, app, request, path, record, args, event, config, kind="main") -> str:
    """The topic's topic.json: its Harness, where it came from (``kind``: "main" the butler's, "web" the page's,
    app/web_launch.py), and the model settings when it starts at once; then its topic card numbered. Its label ('' when
    it has no number yet)."""
    plan, title = record["plan"], record["title"]
    target = topic_context(app.harness.context, event, app.store.sessions_dir)
    origin = {"kind": kind, "request": request.id, "job_key": request.job_key, "why": record["why"],
              "readonly": args.get("readonly") is True, "user_requested": args.get("user_requested") is True,
              "title": title, "at": record["at"]}
    lead = lead_of(env, request)
    if not record.get("topic_id"):
        with topic_lock(target), LOCK:
            data = read_topic(target)
            data.update(harness_id=plan["harness"], binding_revision=1, origin=origin)
            if lead is not None:  # Written with the binding: the thread's first turn is queued only after it.
                data["project"] = projects.project_field(lead[0], projects.MEMBER)
            if plan["start"]:
                data.update(model=plan["model"], effort=plan["effort"], model_settings_snapshot=True,
                            workspace_name=plan["workspace"], workdir=plan["workdir"],
                            workspace_locked=True, harness_locked=True)
                if plan["work_mode"]:
                    data["work_mode"] = plan["work_mode"]
                if plan["redaction"]:
                    data[REDACTION_KEY] = True
            else:
                data["origin"]["launch"] = launch_draft(plan)
            write_json(target.history_path.parent / "topic.json", data, ensure_ascii=False, indent=2)
        record["topic_id"] = target.topic_id
        save(path, record)
        if plan["start"]:
            touch_workspace(target.state_dir, target.default_workdir, plan["workspace"])
    label = topic_label(Path(env.state_dir), record["topic_id"])
    if label and not record.get("numbered") and record.get("card_id"):
        record["numbered"] = number_card(env, path, record, topic_card(title, label=label, why=record["why"],
                                                                         config=config))
    return label


def _enqueue(env, app, request, record, chat, prompt, event, token, label) -> bool:
    """The topic's first turn queued once (its message id "main-" + the request's token): the owner's words on its
    card, the butler's ``prompt`` handed on to its model (app/butler_handoff.py); whether the queue refused it
    (the model settings could not be told)."""
    mid = "main-" + token
    if find_job_record(Path(env.state_dir), mid) or app.runtime.jobs.settling(mid):
        return False
    ctx = app.submission.event_context(app.harness.context, event)
    owner, items = handed(env, request, ctx)
    content = butler_handoff.shown(owner, prompt, items)
    read_only = record["plan"].get("read_only") is True
    sent = butler_handoff.handoff_prompt(butler_handoff.START, label=label, title=record["title"], task=prompt, owner=owner,
                                       items=items, read_only=read_only)
    append_history(ctx.history_path, "user", content, {"message_id": mid})
    return app.submission.enqueue_chat_job(ctx, content, chat, mid, prompt_content=sent, title=record["title"],
                                           read_only=read_only) is None


def place(env, request, chat) -> tuple:
    """(area root, taken) of a topic ``request`` opens, decided once: a 话题区 (docs/butler.md 话题), its thread
    hanging under the owner's question the request answers, the area root; a message has one thread, so a second
    topic opened on the same question gets a topic card at the top level as its root (area root '', taken). ('',
    False) with the switch off or that message not known: the topic card and its thread, as before."""
    if not butler.enabled():
        return "", False
    owner = butler_job_owner_message(env.state_dir, request.job_key)
    if not owner:
        return "", False
    held = message_topic(Path(env.state_dir), chat, owner)
    free = not held or is_butler_topic(held, chat)
    return owner if free else "", not free


def number_card(env, path, record, card):
    """Replace the topic card whole with its number (one write, its sequence reserved before it goes, never
    reused); whether CardKit confirmed. Unconfirmed, the card keeps its title alone."""
    handle = file_card(path, record, shared=False)
    with handle.lock:
        sequence = reserve(handle)
        result = env.messenger.put_card(env.profile, record["card_id"], card, sequence=sequence,
                                        key=f"main-topic-{path.stem[:24]}-{sequence}", timeout=3)
    if result is None:
        env.runtime.log(f"topic card not numbered topic={record['topic_id'][:12]}")
    return result is not None


# topic_send ----------------------------------------------------------------------------------------------------

def live_turn(app, ctx, chat):
    """The topic's task here a supplement goes to (app/runtime.py supplement_target): the one that runs, else the one
    queued; None when it has none."""
    return app.runtime.jobs.supplement_target(ctx.topic_id, chat)


def steered_words(job, saved) -> str:
    """Where a supplement went, as the sender is told: the task that took it, by its number, and how."""
    if saved.get("merged"):
        return f"并进排队中的任务 #{job.job_id}，它开始时一起处理"
    if saved.get("status") == "held":
        return f"送进刚开始的任务 #{job.job_id}，它准备好后自动送入"
    return f"作为补充送入正在跑的任务 #{job.job_id}，已送达"


def hand_line(env, ctx, chat, request, record, kind, owner, text, items, token):
    """The line the topic's thread gets when topic_send hands it the owner's words (app/butler_handoff.py thread_line):
    a card whose whole block opens the owner's message at its place in the chat (thread_card), as text when that place cannot be read. The card's message id is
    the request record's ``thread_message_id``: the hook under the owner's message opens the thread at it
    (app/butler_reply.py hook_topics). Its key is the one the text always had, the card's that key's "-card"."""
    from covey.app.answer_cards import ask_url

    try:
        butler_record = find_job_record(Path(env.state_dir), request.job_key) or {}
        back = (ask_url(env.state_dir, butler_record, profile=env.profile, log=env.runtime.log)
                if butler_record and not (owner.lead and not owner.said) else "")
    except Exception as exc:  # noqa: BLE001 - the line as text
        env.runtime.log(f"handoff line link unavailable: {type(exc).__name__}")
        back = ""

    def keep(message_id):
        record["thread_message_id"] = message_id
    env.messenger.reply(ctx, chat, butler_handoff.thread_line(kind, owner, text, items), env.voice.key("main-send", token),
                        voice=env.voice, card=butler_handoff.thread_card(kind, owner, text, items, back), on_card=keep)


def topic_send(env, request):
    chat = env.chat()
    if not chat:
        return reply(False, NO_CHAT)
    text, note = arg_text(request.args.get("text")), arg_text(request.args.get("note"), 100)
    if not text:
        return reply(False, "text 不能为空：写这次交给它的事。")
    if butler_handoff.choice_error(request.args.get("attachments")):
        return reply(False, butler_handoff.choice_error(request.args.get("attachments")))
    found = find_topic(env, chat, request.args.get("topic"))
    if isinstance(found, str):
        return reply(False, found)
    app, ctx, topic = found
    label = topic_label(Path(env.state_dir), ctx.topic_id) or arg_text(request.args.get("topic"))
    if projects.enabled() and lead_of(env, request) is None and projects.is_member_topic(Path(env.state_dir), ctx.topic_id):
        # A project's thread takes words through the project's lead (project_send), which knows what the others are doing.
        return reply(False, f"{label or '这个话题'} 是专项的话题，不能直接转话：用 projects_list 查专项的 id，"
                            "再用 project_send 把话交给负责人，由它决定要不要再开话题。")
    path, token = record_path(env.state_dir, request), request_token(request)
    mid, words = "main-" + token, composed(text, note)
    record = read_object(path)
    if record.get("answer"):
        return record["answer"]
    record = {"request": request.id, "job_key": request.job_key, "chat_id": chat, "topic_id": ctx.topic_id,
              "at": time.time(), **({"note": note} if note else {})}  # The note: the card's 转给 line.
    save(path, record)
    data = {"topic_id": ctx.topic_id, "label": label}
    title = arg_text(request.args.get("title"), 40) or topic_title(Path(env.state_dir), ctx.topic_id)
    owner, items = handed(env, request, ctx)
    job = live_turn(app, ctx, chat)
    if job is not None:
        steer = butler_handoff.handoff_prompt(butler_handoff.STEER, label=label, title=title, task=text, owner=owner,
                                            items=items, extra=note)
        try:
            saved = app.steering.deliver_steer(ctx, job, steer, mid, shown=butler_handoff.shown(owner, text, items))
        except SteerUncertain as exc:
            return done(path, record, reply(False, f"补充可能已送到 {label} 的任务 #{job.job_id}，送达没有确认（{exc}）；"
                                             "不会重发，别再转一次。", **data, job_id=job.job_id, uncertain=True))
        except SteerRefused as exc:  # Nothing was sent (rule 6).
            return done(path, record, reply(False, f"没有送进 {label} 正在跑的任务 #{job.job_id}：{exc}。没有发出；"
                                             "等这一轮结束后再转，或者告诉主人。", **data, job_id=job.job_id))
        hand_line(env, ctx, chat, request, record, butler_handoff.STEER, owner, text, items, token)
        dispatch_card(env, request, ctx.topic_id, kind="send", mid=job.message_id, **project_hint(request))
        return done(path, record, reply(True, f"已转给 {label}：{steered_words(job, saved)}。",
                                         **data, job_id=job.job_id, steered=True))
    if not workspace_locked(ctx):
        return done(path, record, reply(False, f"{label} 还在等主人点启动卡上的开始，没有转；等它开工后再转。", **data))
    hand_line(env, ctx, chat, request, record, butler_handoff.NEXT, owner, text, items, token)
    # As a message written in the topic: its open question takes it, and a topic whose result or question card
    # has not arrived takes nothing.
    waiting = len(app.questions.pending_questions(ctx, chat))
    if app.workspace_command(ctx, words, chat, mid, "\n".join(attachment_block(items))):
        if waiting:
            return done(path, record, reply(True, f"已转给 {label}：它在等主人回答问题，这条没有开下一轮，按话题里的消息处理"
                                             "（只有一道题时作为回答，否则留给下一次回答），结果看话题。", **data, steered=False))
        return done(path, record, reply(False, f"{label} 没有开下一轮：它的结果或问题卡还没送达，话题里有说明。", **data))
    if not (find_job_record(Path(env.state_dir), mid) or app.runtime.jobs.settling(mid)):
        content = butler_handoff.shown(owner, text, items)
        append_history(ctx.history_path, "user", content, {"message_id": mid})
        sent = butler_handoff.handoff_prompt(butler_handoff.NEXT, label=label, title=title, task=text, owner=owner,
                                           items=items, extra=note)
        # Its card is headed by the topic's title, not the owner's words about where it goes.
        if app.submission.enqueue_chat_job(ctx, content, chat, mid, prompt_content=sent, title=title) is None:
            return done(path, record, reply(False, f"{label} 的下一轮没有提交：模型配置无法确认。", **data))
    dispatch_card(env, request, ctx.topic_id, kind="send", **project_hint(request))
    return done(path, record, reply(True, f"已转给 {label}：它已经停了，开了下一轮。", **data, steered=False))


# topic_fork ----------------------------------------------------------------------------------------------------

def topic_fork(env, request):
    chat = env.chat()
    if not chat:
        return reply(False, NO_CHAT)
    title, prompt = arg_text(request.args.get("title"), 40), arg_text(request.args.get("prompt"))
    if not title or not prompt:
        return reply(False, "title 和 prompt 都不能为空。")
    if butler_handoff.choice_error(request.args.get("attachments")):
        return reply(False, butler_handoff.choice_error(request.args.get("attachments")))
    found = find_topic(env, chat, request.args.get("topic"))
    if isinstance(found, str):
        return reply(False, found)
    app, ctx, _ = found
    label = topic_label(Path(env.state_dir), ctx.topic_id) or arg_text(request.args.get("topic"))
    path, token = record_path(env.state_dir, request), request_token(request)
    record = read_object(path)
    if record.get("answer"):
        return record["answer"]
    records = app.harness.topic_records(ctx, chat)
    if not record.get("fork_topic_id") and (live_turn(app, ctx, chat) is not None
                                            or any(r.get("status") in RUNNING for r in records)):
        return reply(False, f"{label} 正在跑，现在不能分叉；要把话交给它，用 topic_send 作为补充。")
    source = next((r for r in records if r.get("status") in FINISHED), None)
    if source is None:
        return reply(False, f"{label} 还没有结束的任务，不能分叉。")
    record.update(request=request.id, job_key=request.job_key, chat_id=chat, source=ctx.topic_id,
                  job_id=source["job_id"], at=record.get("at") or time.time())
    save(path, record)
    click = "main-" + token
    if "area_root" not in record:  # Decided once, as topic_start's (_open).
        record["area_root"], record["area_taken"] = place(env, request, chat)
        save(path, record)
    forks = app.forks.create_fork(ctx, chat, source, click, area=record.get("area_root") or "")
    fork = next(f for f in forks if f.get("token") == fork_token(ctx.topic_id, click))
    record.update(fork_topic_id=fork["topic_id"], root_message_id=fork["root_id"])
    save(path, record)
    new_topic = load_topic(Path(env.state_dir), fork["topic_id"])
    if not (new_topic.get("origin") or {}).get("request"):
        with LOCK:
            new_topic = load_topic(Path(env.state_dir), fork["topic_id"])
            new_topic["origin"] = {"kind": "main", "request": request.id, "job_key": request.job_key, "why": "",
                                   "readonly": False, "user_requested": False, "title": title,
                                   "at": record["at"], "fork_of": ctx.topic_id}
            write_json(Path(env.state_dir) / "topics" / fork["topic_id"] / "topic.json", new_topic,
                       ensure_ascii=False, indent=2)
    new_label = topic_label(Path(env.state_dir), fork["topic_id"])
    target_app = env.topics.apps[new_topic["harness_id"]]
    mid = "main-" + token
    if not (find_job_record(Path(env.state_dir), mid) or target_app.runtime.jobs.settling(mid)):
        target = target_app.submission.event_context(
            target_app.harness.context, {"chat_id": chat, "message_id": fork["root_id"]})
        owner, items = handed(env, request, target)
        content = butler_handoff.shown(owner, prompt, items)
        sent = butler_handoff.handoff_prompt(butler_handoff.FORK, label=new_label, title=title, task=prompt, owner=owner,
                                           items=items, source=label)
        append_history(target.history_path, "user", content, {"message_id": mid})
        target_app.submission.enqueue_chat_job(target, content, chat, mid, prompt_content=sent, title=title)
    dispatch_card(env, request, fork["topic_id"], kind="fork")
    data = {"topic_id": fork["topic_id"], "label": new_label, "root_message_id": fork["root_id"],
            "source_topic_id": ctx.topic_id, "source_job_id": source["job_id"]}
    return done(path, record, reply(True, f"已从 {label} 的任务 #{source['job_id']} 分叉出 {new_label}「{title}」，"
                                     "第一轮已提交。", **data))


TOOLS = {"topic_start": topic_start, "topic_send": topic_send, "topic_fork": topic_fork}
