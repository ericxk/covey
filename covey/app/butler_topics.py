"""The butler's tools that change topics (docs/butler.md 开话题、转交、分叉): topic_start, topic_send and
topic_fork, handlers as app/butler_env.py describes them. Only in the owner's private chat.

- ``topic_start`` sends the topic card (cards/topic_card.py) at the chat's top level, opens the topic on it as
  app/forks.py opens a fork on its root, and replaces the card once with the topic's number. Every new topic starts at
  once in the home with the repositories it names (``plan_start``, ``declared_repos``; docs/configuration.md 主目录与仓库地图),
  is bound as a fork is and its first turn is submitted; one an older release saved to wait (独立 worktree) waits on its
  launch card, prefilled from ``origin.launch`` in its topic.json (app/launch.py build_launch_card), with the prompt as
  the waiting turn. An older call's workspace on a device opens nothing (主人 D2Y4T, ``device_refusal``): a topic uses a
  device from the home (``covey device``).
- ``topic_send`` gives the owner's words to a topic: a supplement to its turn while one runs (app/steering.py
  deliver_steer), else its next turn, headed by the topic's title. A line in the topic's thread says the butler
   passed the owner's words on. A topic waiting on its question takes them as a held note for the owner's answer, never
   as the answer (``note_only``: the butler's or a lead's words are not the owner's, W0-1); the answer is told how to
   answer instead (``held_next``).

What each topic's model gets is app/butler_handoff.py's: the Bridge's note, the butler's task, the owner's words
from the butler turn's record and its attachments, carried into the topic's incoming_media; its card and
history show the owner's words. A call that names one of the turn's messages (``message``: 1 the turn's own, then each it
took) hands on that message alone, and the topic it opens hangs under it (``said_message``, T957). A project's lead's call (``lead_of``) hands on its task and the files it names, its card
and history show that (``handed``), and the topic it opens hangs under no message of the owner's (``place``).
- ``topic_fork`` forks a topic's last finished turn, on whichever Harness it ran (app/forks.py create_fork), and submits
  the new topic's first.
- The 派发卡 (``dispatch_card``, COVEY_DISPATCH_CARD=1) is topic_send's alone, in the thread of the owner's question
  (app/dispatch_cards.py created): a topic topic_start or topic_fork opens shows at the top level already, its thread
  under the owner's question or its own card there.

Which Harness, model and budget a new topic gets is app/butler_model_choice.py's (``resolve``, used by ``plan_start``).

Each request runs once (rule 6): core/butler_requests.py claim gives it to one caller, what it did is kept in
main-thread/opened/<request>.json (core/butler_opened.py: ``record_path``, ``save``, ``done``), every send's key and every synthetic message id derive from the request id, and
the same request again gets the saved answer. The handlers hold no lock across a Feishu call (rules 1 and 2) but
the one a card's write needs for its sequence (rule 3).
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from covey.app import config_basis, dispatch_cards, butler_handoff, memory_picks, repo_peers
from covey.app.forks import FINISHED, fork_token
from covey.app.harnesses import chat_records
from covey.app.butler_env import arg_text, reply
from covey.app.butler_model_choice import resolve
from covey.app.launch_models import available_models
from covey.cards.topic_card import topic_card
from covey.app.relay_targets import said_message as said_message_of
from covey.core import criteria, devices, butler, butler_settings, dispatch_records, projects, triage, turn_source
from covey.core.store import directory_store
from covey.core.attachments import attachment_block
from covey.core.card_sequence import file_card, reserve
from covey.core.jobs import find_job_record
from covey.core.statuses import RUNNING
from covey.core.locks import LOCK, topic_lock
from covey.core.butler_opened import done, once_a_turn, record_path, request_token, save
from covey.core.butler import is_butler_topic, is_lead
from covey.core.state import read_object, write_json
from covey.core.history_storage import append_history
from covey.core.steer import SteerRefused, SteerUncertain
from covey.core.topics import (claim_area_root, load_topic, mark_home, message_topic, read_topic, topic_label,
                                       retired_topic, topic_title, workspace_locked)
from covey.core.workspaces import RUN_MODES, config_path, load_config, topic_context, touch_workspace

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


def source_of(env, request, owner) -> dict | None:
    """The 轮次来源 of the turn a call ``request`` hands a topic (core/turn_source.py ``stamp``, WAVE12 C1): a project's lead's
    (``lead_of``) ``lead`` from its topic; the butler's ``relay``, said by the owner with his message in its origin when the
    butler turn answers one of his (``owner``: butler_handoff.asked's ``message_id``), else the butler's. None when stamping
    is off (as before W1-B)."""
    if lead_of(env, request) is not None:
        job = find_job_record(Path(env.state_dir), request.job_key) or {}
        topic = str(job.get("topic_id") or "")
        return turn_source.stamp(turn_source.LEAD, origin={"topic": topic} if topic else None, via="lead")
    if owner.message_id:
        return turn_source.stamp(turn_source.RELAY, asserted_by=turn_source.PARTY_OWNER,
                                 origin={"message_id": owner.message_id}, via="butler")
    return turn_source.stamp(turn_source.RELAY, via="butler")


def dispatch_card(env, request, topic_id, **how):
    """The 派发卡 of the work topic_send ``request`` handed to the topic ``topic_id`` that was there (app/dispatch_cards.py:
    a reply in the thread of the owner's question), only with COVEY_DISPATCH_CARD=1: without it nothing is called and the
    topic's own cards are all there is. None for a project's lead's call: the project's card is the one (phase 2's P2-4).
    topic_start and topic_fork call none: the topic they open hangs its thread under the owner's question, or has its own
    card at the chat's top level (a second topic of the question, a question not known), and a 派发卡 said the same again."""
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
    if is_lead(found, chat):
        # A project's lead is of the butler's family but is not "the butler itself": say whose topic it is and where to go.
        project = projects.topic_project(Path(env.state_dir), found)
        name = (projects.read_record(Path(env.state_dir), project.id) or {}).get("name") if project else ""
        return (f"{text} 是专项{f'「{name}」' if name else ''}的负责人话题，管家不直接动它：要对负责人说话用 project_send；"
                "要停整个专项，请主人在专项卡上点“停”，或告诉主人。")
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


GRANTING = ("project_topic_start", "project_topic_send")  # The lead's tools that check an approval (app/project_tools.py approved).


def granted(request) -> str:
    """The approval line app/project_tools.py put in a lead's hand-on after checking it (``_approval_line``); '' for any other
    call: only those two tools set it, from the record, after dropping any the model wrote (handed_args)."""
    if getattr(request, "tool", "") not in GRANTING:
        return ""
    line = request.args.get("_approval_line")
    return line.strip() if isinstance(line, str) else ""


def handed(env, request, ctx):
    """(the owner's message the request's butler turn answers, the attachments of it the call hands ``ctx``'s
    topic, carried into its incoming_media). A project's lead's call hands on its own words alone (Asked with ``lead``
    and nothing said) and the files of its turn it names (``attachments``), none unnamed."""
    lead = lead_of(env, request)
    if lead is not None:
        # The lead runs the project: what the owner said in its turn (a reply to its card, his supplements) is the lead's
        # to act on, and one turn of it often opens several topics and reminds another of something else. Its task says
        # what each is to do; the owner's words are never handed on as theirs (T856, 2026-11-27: T831, handed a reminder,
        # got the owner's whole reply about two other documents, and the threads T852 to T855 opened in that turn and the
        # next showed his words in place of their task). His files go to the topic the lead names them for (the owner:
        # 转不转给话题由负责人决定); none when it names none.
        chosen = request.args.get("attachments")
        grant = granted(request)
        if not chosen or chosen == "none":
            return butler_handoff.Asked(lead=lead[1] or "专项", grant=grant), []
        turn = butler_handoff.asked(env.state_dir, request.job_key)
        owner = butler_handoff.Asked(attachments=turn.attachments, roots=turn.roots, lead=lead[1] or "专项", grant=grant)
        items = butler_handoff.picked(owner.attachments, chosen)
        return owner, butler_handoff.carry(items, media_dir(ctx), owner.roots) if items else []
    # A call that names one of the turn's messages (``message``, T957) hands on that message's words and files alone.
    owner, items = butler_handoff.chosen(butler_handoff.asked(env.state_dir, request.job_key), request.args.get("message"),
                                         request.args.get("attachments"))
    return owner, butler_handoff.carry(items, media_dir(ctx), owner.roots) if items else []


# topic_start ---------------------------------------------------------------------------------------------------

# A workspace on a device (``gpu1:/home/me/proj``, or the name of a saved one): no topic is opened there any more (主人 D2Y4T,
# core/devices.py START_RETIRED), whether 多设备 is on or off. The call is refused before anything is opened and told the way
# instead: open the topic in the home and use the device from there (``covey device``).
DEVICE_RETIRED = ("没有开话题：工作区「{workspace}」是设备 {alias} 上的目录。{retired}。开话题时不写 workspace（要用的仓库写进 "
                  "repos），开好后话题里用 covey device 访问 {alias}。")


def device_refusal(workspace, value) -> str:
    """Why a call naming ``workspace`` (its saved value ``value``, '' when none is saved) opens no topic: it is on a device
    (``alias:path`` either way, the switch on or off: core/devices.py device_shaped); '' for any other."""
    where = value if devices.device_shaped(value) else workspace if devices.device_shaped(workspace) else ""
    if not where:
        return ""
    return DEVICE_RETIRED.format(workspace=workspace, alias=str(where).split(":", 1)[0],
                                 retired=devices.START_RETIRED.split("；", 1)[0])


REPOS_MAX = 10
REPO_NAME_CHARS = 64
REPO_MODES = ("read", "write")
NO_WRITE = {"frozen": "已冻结", "readonly": "只读"}  # core/repomap.py STATUSES a topic may not write to.


def declared_repos(state_dir, value, *, workspace="", now=None) -> list:
    """The repositories a new topic says it uses (topic_start's and project_topic_start's ``repos``, the web page's launch,
    docs/configuration.md 主目录与仓库地图) as ``topic.json`` keeps them (core/topic_repos.py TopicRepo's keys): each
    ``{name, path, mode, source: "declared", first_at}``, by the repo map's names (core/repomap.py), or a saved
    workspace's while the map has none of that name. ``workspace``: an older call's argument, one more read. A name
    twice is one, its write kept. ValueError says what is wrong: not a list of at most REPOS_MAX ``{name, mode}``, a
    name neither has (with the names to choose from), a write to a repository the map holds frozen or read-only."""
    if value in (None, "", []) and not workspace:
        return []
    if value in (None, ""):
        value = []
    if not isinstance(value, list) or len(value) > REPOS_MAX:
        raise ValueError(f"repos 要是最多 {REPOS_MAX} 项的列表，每项 {{name, mode: read|write}}")
    asked = [{"name": workspace, "mode": "read"}] if workspace else []
    for item in value:
        name = str(item.get("name") or "").strip() if isinstance(item, dict) else ""
        mode = item.get("mode", "read") if isinstance(item, dict) else ""
        if not name or len(name) > REPO_NAME_CHARS or mode not in REPO_MODES:
            raise ValueError(f"repos 的每一项要有 name（{REPO_NAME_CHARS} 字内）和 mode（read 或 write）")
        asked.append({"name": name, "mode": mode})
    from covey.core import repomap
    repos = repomap.read(state_dir)
    spaces = read_object(config_path(Path(state_dir))).get("workspaces")  # Read only: a missing list is made by no one here.
    spaces = spaces if isinstance(spaces, dict) else {}
    now = time.time() if now is None else now
    found, on_device = {}, {}
    for item in asked:
        name, mode = item["name"], item["mode"]
        repo = repomap.find(repos, name)
        if repo is not None:
            if mode == "write" and repo.status in NO_WRITE:
                raise ValueError(f"仓库「{repo.name}」{NO_WRITE[repo.status]}，不能改；只读就把 mode 写成 read")
            name, path = repo.name, repo.path
            if repo.device:
                on_device[name] = repo.device  # Recorded with its device; core/topic_repos.py reads no such entry as a path here.
        elif name in spaces and not devices.is_device_value(spaces[name]):
            path = str(Path(spaces[name]).expanduser())
        else:
            known = [repo.name for repo in repos] or [space for space, where in spaces.items() if not devices.is_device_value(where)]
            raise ValueError(f"仓库地图里没有「{name[:40]}」，可选：{'、'.join(known[:20]) or '（没有）'}；用 repos_list 看")
        kept = found.get(name)
        if kept is None or mode == "write":
            found[name] = {"name": name, "path": path, "mode": mode if kept is None else "write", "source": "declared",
                           "first_at": now, **({"device": on_device[name]} if name in on_device else {})}
    return list(found.values())


def plan_start(env, args, settings):
    """What topic_start opens: the Harness, model and budget as ``resolve`` finds them; the home, where every new topic
    starts (docs/configuration.md 主目录与仓库地图, app/launch_submit.py home_binding: its ``workspace`` and ``workdir`` as
    before, so an older release reads them), ``home_start`` and the ``repos`` it uses (``declared_repos``). Every new topic starts at once in 原目录:
    the run mode 独立 worktree is gone (a repository to change gets its worktree from ``covey repo use --write``). A str
    saying why not. A call's ``redaction`` (an older butler's) is ignored.

    An older call's ``workspace`` on a device (``alias:path``, or a saved workspace written so) opens nothing (主人 D2Y4T,
    ``device_refusal``); the settings' new-topic workspace, which no card sets any more, is not read. Any other ``workspace``
    is one more repository read."""
    defaults = settings.topic_defaults
    found = resolve(env.topics, env.state_dir, env.chat(), defaults, args)
    if isinstance(found, str):
        return found
    base = found["base"]
    named = arg_text(args.get("workspace"))
    if named:
        config = load_config(Path(env.state_dir), base.default_workdir)
        refused = device_refusal(named, str(config["workspaces"].get(named, "")))
        if refused:
            return refused
    try:
        repos = declared_repos(env.state_dir, args.get("repos"), workspace=named)
    except ValueError as exc:
        return f"没有开话题：{exc}"
    from covey.app.launch_submit import home_binding
    workspace, home = home_binding(base)
    plan = {"harness": found["harness"], "model": found["model"], "model_key": found["model_key"],
            "effort": found["effort"], "work_mode": getattr(base, "work_mode", "") or "", "workspace": workspace,
            "workdir": str(home.workdir), "run_mode": "direct", "repos": repos, "home_start": True}
    return plan


def config_line(env, plan):
    """The topic card's configuration: Harness · 模型 · 思考预算 · 主目录 (· 仓库 …) (· 按「…」), or for a topic on a
    device (or an older plan) 工作区 (· 设备) · 运行方式 in place of 主目录 and its repositories; 设备 the title of the
    device a workspace there is on, as the launch card names it; 按「…」 what the call named its Harness, model or
    budget on (app/config_basis.py), when it named one."""
    app = env.topics.apps[plan["harness"]]
    model = app.cards.model_label(app.harness.context, plan["model"]) if plan["model"] else ""
    model = model.split(" · ", 1)[-1] if model else (plan["model"] or "默认模型")
    if plan.get("home_start"):
        names = [f"{repo['name']}{'（改）' if repo.get('mode') == 'write' else ''}" for repo in plan.get("repos") or ()]
        where = ["主目录", *(["仓库 " + "、".join(names)] if names else [])]
    else:
        where = ["工作区 " + plan["workspace"], *(["设备 " + plan["device_title"]] if plan.get("device_title") else []),
                 RUN_MODES[plan["run_mode"]]]
    return (app.spec.title, model, "思考预算 " + (plan["effort"] or unnamed_budget(env, app, plan)),
            *where,
            *([config_basis.line(plan["basis"])] if plan.get("basis") else []))


def unnamed_budget(env, app, plan):
    """How the topic card names a budget ``plan`` leaves '': 模型自动管理 for a model without levels (resolve names
    every other one, T718), else 模型默认 (a model not on the menu now)."""
    entry = available_models(env.topics, app.harness.context).get(f"{plan['harness']}:{plan['model']}")
    return "模型自动管理" if entry is not None and not entry.get("levels") else "模型默认"


def launch_draft(plan):
    """The launch card's prefill (app/preselection.py launch_selection's ``draft``)."""
    return {"model": plan["model_key"] or f"{plan['harness']}:{plan['model']}", "effort": plan["effort"],
            "work_mode": plan["work_mode"], "workspace": plan["workspace"], "run_mode": plan["run_mode"]}


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
    if message_refusal(env, request):
        return reply(False, message_refusal(env, request))
    if args.get("project") is True and getattr(env, "projects", None) is not None and projects.enabled() and lead_of(env, request) is None:
        return env.projects.start(request, args)  # 直接开专项: a project, not a topic (COVEY_PROJECTS=1; the butler's call alone).
    # The same topic asked for again in this turn: the first call's answer, nothing opened twice (W2-B).
    return once_a_turn(env.state_dir, request, "topic_start", title, prompt,
                       lambda: _start(env, request, chat, args, title, prompt, why), rest=rest_of(args, "title", "prompt"))


def rest_of(args, *texts) -> dict:
    """A call's arguments but its ``texts`` (title, prompt): what else must be the same for a call to be the same one
    (core/butler_opened.py once_a_turn)."""
    return {key: value for key, value in args.items() if key not in texts}


def _start(env, request, chat, args, title, prompt, why):
    """topic_start's work once its arguments are checked and no same call came first: its plan, then ``_open``."""
    path = record_path(env.state_dir, request)
    record = read_object(path)
    if record.get("answer"):
        return record["answer"]  # The same request: the same topic, nothing sent again.
    if not record.get("plan"):
        shown, lead = "", lead_of(env, request)
        if config_basis.named(args) and arg_text(args.get("basis")):
            # What the model's call named its Harness, model or budget on (app/butler_tools.py refused one without, T786).
            shown = config_basis.check(env.state_dir, request.job_key, args.get("basis"),
                                       project_id=lead[0] if lead else "")[0]
        settings = butler_settings.load(env.state_dir)
        plan = plan_start(env, args, settings)
        if isinstance(plan, str):
            return reply(False, plan)
        if shown:
            plan["basis"] = shown
        # Every new topic starts at once (a plan saved by an older release before a restart keeps its own ``start``).
        plan["start"] = plan["run_mode"] == "direct" and not plan.get("worktree_wanted")
        # No read-only first turn (T956): the butler's and a lead's ``readonly`` was a model's guess that held nothing back
        # the owner's words and the rules did not, and stopped work he asked for (T950). A plan an older release saved with
        # ``read_only`` is opened as any other.
        record = {"request": request.id, "job_key": request.job_key, "chat_id": chat, "title": title,
                  "why": why, "plan": plan, "at": time.time(), "sequence": 0, **said_by(lead)}
        save(path, record)
    return _open(env, request, path, record, chat, prompt, args)


def said_by(lead) -> dict:
    """What a request's record says of whose words it handed on, for a project's lead's call (``lead``, lead_of's): its
    own (``handed``), so the page shows them as the lead's (app/web_said.py Origins) whatever its turn answers. {} for the
    butler's: the owner's words when its turn answers his message. A lead's record from before T856 has none, and handed
    on the owner's words when its turn answered his message."""
    return {"by": "lead"} if lead is not None else {}


def said_in(env, request) -> dict:
    """{"message_id": the owner's message the request's butler turn answers} for a topic's ``origin`` in its topic.json: where
    its turns' results go back to (app/relay_targets.py), read from the topic itself, never only from main-thread/opened,
    which is cleared after a week (WAVE12 W2-B). {} for a project's lead's call (its threads hang under no message of the
    owner's, ``place``) and when the turn answers none (a page's topic: app/web_launch.py)."""
    if lead_of(env, request) is not None or not getattr(request, "job_key", ""):
        return {}
    owner = said_message(env, request)
    return {"message_id": owner} if owner else {}


def said_message(env, request, job_key=None) -> str:
    """The owner's top-level message a butler call ``request`` is for: the one its ``message`` names when that is a supplement
    of the turn he sent in Feishu (its own id: the topic it opens hangs under it, T957), else the one the butler turn answers
    (``butler_job_owner_message``, its first). ``job_key``: that turn's, when not the request's. '' when none is known."""
    return said_message_of(env.state_dir, request.job_key if job_key is None else job_key,
                           (getattr(request, "args", None) or {}).get("message"))


def message_refusal(env, request) -> str:
    """Why the call's ``message`` names none of the owner's messages of its turn ('' when it does, or names none): checked
    before anything is opened or sent. A project's lead's call has no such argument (its words are its own)."""
    value = request.args.get("message")
    if value is None or lead_of(env, request) is not None:
        return ""
    return butler_handoff.message_error(value, butler_handoff.asked(env.state_dir, request.job_key))


def picker(lead) -> str:
    """Who picks what a call's ``memory`` names (app/memory_picks.py): a project's lead (``lead``, lead_of's) or the butler."""
    return "lead" if lead is not None else "butler"


def carry(env, ctx, raw, lead, *, steered=False) -> str:
    """The call's ``memory`` ``raw`` (app/memory_picks.py check) added to the topic ``ctx``'s ``memory_carried``, under LOCK
    with no Feishu call; the words the answer adds ('' when it names none; ``steered``: the words go into a turn running
    now, which has its message already). Read with what it carries already; a project's
    lead may name its project's too."""
    if not memory_picks.codes_of(raw):
        return ""
    project = lead[0] if lead is not None else ""
    have = memory_picks.entries_of(env.state_dir, read_topic(ctx), project=project)
    taken, refused = memory_picks.check(env.state_dir, raw, project=project, carried=have)
    if taken:
        with LOCK:
            data = read_topic(ctx)
            memory_picks.remember(data, taken, picker(lead))
            write_json(ctx.history_path.parent / "topic.json", data, ensure_ascii=False, indent=2)
    return memory_picks.said(taken, refused, steered=steered)


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
    named = f"{label}「{title}」" if label else f"「{title}」"
    where = ("，话题串挂在主人这条提问下面" if record.get("area_root")
             else "，话题串的根是顶层一张小卡（主人这条提问下面已经有一个话题串）" if record.get("area_taken") else "")
    twins = repo_peers.duplicates(env.state_dir, record["topic_id"], plan.get("workdir"), title)
    on = f"，在设备 {plan['device_title']} 上" if plan.get("device_title") else ""
    text = (f"已开 {named}：直接开工{on}{where}" if plan["start"]
            else f"已开 {named}：启动卡在话题里，等主人点开始{on}{where}"
            + ("（运行方式是独立 worktree，由启动卡创建）" if plan["run_mode"] == "worktree" else "")
            + ("（设备上的工作区只能用原目录运行，设置里的独立 worktree 用不了，启动卡预选了原目录）"
               if plan.get("worktree_wanted") else ""))
    text += f"。可能重复：{'、'.join(twins)} 也在同一个仓库做相近的事（照开，没拦）" if twins else ""
    said = str(record.get("memory_said") or "")
    text += ("。" + said.strip()) if said else ""
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
              "user_requested": args.get("user_requested") is True,
              # Uses this machine's repositories and files (app/home_turn.py local): false only when the call said so.
              "local": args.get("local") is not False,
              "title": title, "at": record["at"], **said_in(env, request)}
    lead = lead_of(env, request)
    if not record.get("topic_id"):
        # 派活时挑带 (app/memory_picks.py): checked before the lock.
        taken, refused = memory_picks.check(env.state_dir, args.get("memory"), project=lead[0] if lead is not None else "")
        record["memory_said"] = memory_picks.said(taken, refused)
        with topic_lock(target), LOCK:
            data = read_topic(target)
            data.update(harness_id=plan["harness"], binding_revision=1, origin=origin)
            memory_picks.remember(data, taken, picker(lead))
            items = criteria.clean(args.get("criteria"))
            if items:  # 完成标准 (core/criteria.py): each turn is told them, its answer card shows those not met.
                data[criteria.KEY] = items
            if lead is not None:  # Written with the binding: the thread's first turn is queued only after it.
                data["project"] = projects.project_field(lead[0], projects.MEMBER)
            if plan.get("home_start"):  # 主目录启动 (core/topic_repos.py): where it started (once, with its slug) and what it uses.
                mark_home(data, title=title, now=record["at"])
            if plan.get("repos"):
                data["repos"] = [dict(repo) for repo in plan["repos"]]
            if plan["start"]:
                data.update(model=plan["model"], effort=plan["effort"], model_settings_snapshot=True,
                            workspace_name=plan["workspace"], workdir=plan["workdir"],
                            workspace_locked=True, harness_locked=True)
                if plan["work_mode"]:
                    data["work_mode"] = plan["work_mode"]
            else:
                data["origin"]["launch"] = launch_draft(plan)
            write_json(target.history_path.parent / "topic.json", data, ensure_ascii=False, indent=2)
        record["topic_id"] = target.topic_id
        save(path, record)
        if plan["start"] and not plan.get("home_start"):  # The home is in no workspace list (launch_submit.home_binding).
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
    sent = butler_handoff.handoff_prompt(butler_handoff.START, label=label, title=record["title"], task=prompt, owner=owner,
                                       items=items)
    append_history(ctx.history_path, "user", content, {"message_id": mid})
    source = source_of(env, request, owner)
    return app.submission.enqueue_chat_job(ctx, content, chat, mid, prompt_content=sent, title=record["title"],
                                           **({"source": source} if source else {})) is None


def place(env, request, chat) -> tuple:
    """(area root, taken) of a topic ``request`` opens, decided once: a 话题区 (docs/butler.md 话题), its thread
    hanging under the owner's question the request answers, the area root; a message has one thread, so a second
    topic opened on the same question gets a topic card at the top level as its root (area root '', taken), and so does
    one whose question's thread holds the 派发卡 of a topic_send before it (core/dispatch_records.py threaded). ('',
    False) with the switch off or that message not known: the topic card and its thread, as before; and for a project's
    lead's call, whatever its turn answers: the owner's message there is his to the lead (a reply to its card), which
    keeps it, not the question of a topic it opens (T856, 2026-11-27: T852 took his reply about two other documents as
    its root, its thread hung under it and the message's alias moved to it)."""
    if not butler.enabled() or lead_of(env, request) is not None:
        return "", False
    owner = said_message(env, request)  # The supplement the call names (``message``), else the turn's first message.
    if not owner:
        return "", False
    held = message_topic(Path(env.state_dir), chat, owner)
    free = (not held or is_butler_topic(held, chat)) and not dispatch_records.threaded(env.state_dir, chat, owner)
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
        # A lead's words are its own (``handed``): the line goes as text, never a card opening a message of the owner's.
        back = (ask_url(env.state_dir, butler_record, profile=env.profile, log=env.runtime.log)
                if butler_record and not owner.lead else "")
    except Exception as exc:  # noqa: BLE001 - the line as text
        env.runtime.log(f"handoff line link unavailable: {type(exc).__name__}")
        back = ""

    def keep(message_id):
        record["thread_message_id"] = message_id
    env.messenger.reply(ctx, chat, butler_handoff.thread_line(kind, owner, text, items), env.voice.key("main-send", token),
                        voice=env.voice, card=butler_handoff.thread_card(kind, owner, text, items, back), on_card=keep)


def set_criteria(env, ctx, raw):
    """The topic's completion criteria replaced by the call's ``criteria`` (core/criteria.py clean; an empty list takes
    them away), under LOCK, no Feishu call; the new ones, None when the call gave none."""
    items = criteria.clean(raw)
    if items is None:
        return None
    with LOCK:
        data = read_topic(ctx)
        if items:
            data[criteria.KEY] = items
        else:
            data.pop(criteria.KEY, None)
        write_json(ctx.history_path.parent / "topic.json", data, ensure_ascii=False, indent=2)
    return items


def topic_send(env, request):
    chat = env.chat()
    if not chat:
        return reply(False, NO_CHAT)
    text, note = arg_text(request.args.get("text")), arg_text(request.args.get("note"), 100)
    if not text:
        return reply(False, "text 不能为空：写这次交给它的事。")
    if butler_handoff.choice_error(request.args.get("attachments")):
        return reply(False, butler_handoff.choice_error(request.args.get("attachments")))
    if message_refusal(env, request):
        return reply(False, message_refusal(env, request))
    found = find_topic(env, chat, request.args.get("topic"))
    if isinstance(found, str):
        return reply(False, found)
    app, ctx, topic = found
    label = topic_label(Path(env.state_dir), ctx.topic_id) or arg_text(request.args.get("topic"))
    lead = lead_of(env, request)
    if projects.enabled() and lead is None and projects.is_member_topic(Path(env.state_dir), ctx.topic_id):
        # A project's thread takes words through the project's lead (project_send), which knows what the others are doing.
        return reply(False, f"{label or '这个话题'} 是专项的话题，不能直接转话：用 projects_list 查专项的 id，"
                            "再用 project_send 把话交给负责人，由它决定要不要再开话题。")
    path, token = record_path(env.state_dir, request), request_token(request)
    mid, words = "main-" + token, composed(text, note)
    record = read_object(path)
    if record.get("answer"):
        return record["answer"]
    record = {"request": request.id, "job_key": request.job_key, "chat_id": chat, "topic_id": ctx.topic_id,
              "at": time.time(), **({"note": note} if note else {}), **said_by(lead)}  # The note: the card's 转给 line.
    save(path, record)
    data = {"topic_id": ctx.topic_id, "label": label}
    changed = set_criteria(env, ctx, request.args.get("criteria"))
    if changed is not None:
        data["criteria"] = changed
    title = arg_text(request.args.get("title"), 40) or topic_title(Path(env.state_dir), ctx.topic_id)
    owner, items = handed(env, request, ctx)
    # Words handed to a topic are its words' sender's (the butler's, a lead's: WAVE12 C1), as a supplement or a next turn.
    source = source_of(env, request, owner)
    job = live_turn(app, ctx, chat)
    if job is not None and app.steering.steer_stage(job) == "paused":
        # Its turn waits on a live question (app/runtime.py job_stage): words handed on are never its answer (C2).
        return done(path, record, reply(False, waiting_line(label, job, lead), **data, job_id=job.job_id, waiting=True))
    if job is None and not workspace_locked(ctx):
        return done(path, record, reply(False, f"{label} 还在等主人点启动卡上的开始，没有转；等它开工后再转。", **data))
    # 派活时挑带 (app/memory_picks.py): what the call's ``memory`` names, carried from the turn it starts, or the next one.
    picked = carry(env, ctx, request.args.get("memory"), lead, steered=job is not None)

    def end(answer):
        return done(path, record, {**answer, "text": answer["text"] + picked} if picked else answer)

    if job is not None:
        steer = butler_handoff.handoff_prompt(butler_handoff.STEER, label=label, title=title, task=text, owner=owner,
                                            items=items, extra=note)
        # Should its turn never take them (it ends unread, or refuses them late): the topic's next turn, as the stopped
        # topic gets below, with their 轮次来源 (app/steering.py end_unread_steers, the fourth way: W2-B).
        again = next_turn(text=butler_handoff.shown(owner, text, items), title=title, source=source,
                          prompt=butler_handoff.handoff_prompt(butler_handoff.NEXT, label=label, title=title, task=text,
                                                               owner=owner, items=items, extra=note))
        try:
            saved = app.steering.deliver_steer(ctx, job, steer, mid, shown=butler_handoff.shown(owner, text, items),
                                               requeue=again)
        except SteerUncertain as exc:
            return end(reply(False, f"补充可能已送到 {label} 的任务 #{job.job_id}，送达没有确认（{exc}）；"
                                             "不会重发，别再转一次。", **data, job_id=job.job_id, uncertain=True))
        except SteerRefused as exc:  # Nothing was sent (rule 6).
            return end(reply(False, f"没有送进 {label} 正在跑的任务 #{job.job_id}：{exc}。没有发出；"
                                             "等这一轮结束后再转，或者告诉主人。", **data, job_id=job.job_id))
        hand_line(env, ctx, chat, request, record, butler_handoff.STEER, owner, text, items, token)
        dispatch_card(env, request, ctx.topic_id, mid=job.message_id, **project_hint(request))
        return end(reply(True, f"已转给 {label}：{steered_words(job, saved)}。",
                                         **data, job_id=job.job_id, steered=True))
    hand_line(env, ctx, chat, request, record, butler_handoff.NEXT, owner, text, items, token)
    # A topic waiting on its question keeps these words for the owner's answer (a held note, never the answer: W0-1, the
    # words are the butler's or a lead's, their source says so: WAVE12 C2), and a topic whose result or question card has
    # not arrived takes nothing. A question the project's lead holds (the triage, never shown to the owner) keeps them too
    # (P1-7), for its answer, the lead's or the owner's.
    waiting = len(app.questions.pending_questions(ctx, chat))
    triaged = any(triage.held(row) for _, row in directory_store(Path(env.state_dir) / "questions").query(
        topic_id=ctx.topic_id, chat_id=chat, status="pending"))
    if app.workspace_command(ctx, words, chat, mid, "\n".join(attachment_block(items)), note_only=True,
                             **({"source": source} if source else {})):
        if waiting:
            return end(reply(True, f"已转给 {label}，但没有开下一轮，也没有当成回答：它在等主人回答问题，"
                                             f"这条作为补充暂存，主人回答时一起带给它（话题里有暂存卡）。{held_next(lead)}",
                                             **data, steered=False, held=True))
        if triaged:
            return end(reply(True, f"已转给 {label}，但没有开下一轮，也没有当成回答：它的问题正在分流"
                                             f"（交给负责人，主人还没看到），这条作为补充暂存，这道题答了时一起带给它。{held_next(lead)}",
                                             **data, steered=False, held=True))
        return end(reply(False, f"{label} 没有开下一轮：它的结果或问题卡还没送达，话题里有说明。", **data))
    if not (find_job_record(Path(env.state_dir), mid) or app.runtime.jobs.settling(mid)):
        content = butler_handoff.shown(owner, text, items)
        append_history(ctx.history_path, "user", content, {"message_id": mid})
        sent = butler_handoff.handoff_prompt(butler_handoff.NEXT, label=label, title=title, task=text, owner=owner,
                                           items=items, extra=note)
        # Its card is headed by the topic's title, not the owner's words about where it goes.
        if app.submission.enqueue_chat_job(ctx, content, chat, mid, prompt_content=sent, title=title,
                                           **({"source": source} if source else {})) is None:
            return end(reply(False, f"{label} 的下一轮没有提交：模型配置无法确认。", **data))
    dispatch_card(env, request, ctx.topic_id, **project_hint(request))
    return end(reply(True, f"已转给 {label}：它已经停了，开了下一轮。", **data, steered=False))


def next_turn(*, text, prompt, title, source, address="") -> dict:
    """The ``requeue`` of words handed to a topic's running turn (core/deliveries.py handed): what the topic's next turn
    is queued as should the turn never take them, with their 轮次来源 (``source``; none when stamping is off) and the
    title its card is headed by. ``address``: the message whose context is the topic, for one with no root message (a
    project's lead, app/butler_projects.py lead_target)."""
    return {"text": text, "prompt": prompt, "topic": True, **({"title": title} if title else {}),
            **({"source": dict(source)} if source else {}), **({"address": address} if address else {})}


def waiting_line(label, job, lead) -> str:
    """What the caller of topic_send or project_send is told when the turn there waits on a live question (WAVE12 W2-B):
    nothing went in, and the words are not its answer; the butler answers with question_answer once the owner has said
    it, a project's lead with project_answer or project_escalate when the question was handed to it."""
    head = f"{label or '它'} 的任务 #{job.job_id} 正停在一道实时问题上等主人回答，这次没有送进去，也没有当成回答。"
    if lead is not None:
        return head + ("不要用加话代替回答：问题交给你分流时用 project_answer 替主人答（要有依据）或 project_escalate 转给主人，"
                       "否则等主人答完这道题再转。")
    return head + "主人这句话就是在答那道题，就用 question_answer 交上去；不是的话，等它答完这道题再转。"


def held_next(lead) -> str:
    """What the caller of ``topic_send`` is told to do about the question its words were held for: a project's lead
    (``lead``: lead_of's (id, name), None for the butler) answers or hands on a question the triage gave it, never by sending words; the butler
    waits for the owner, or answers with question_answer once the owner has said the answer himself."""
    if lead is not None:
        return ("不要再用加话代替回答：问题交给你分流时用 project_answer 替主人答（要有依据）或 project_escalate 转给主人，"
                "否则等主人自己回答。")
    return "不要再用加话代替回答：主人说了答案再用 question_answer 交上去，否则等主人在话题里回答。"


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
    if message_refusal(env, request):
        return reply(False, message_refusal(env, request))
    found = find_topic(env, chat, request.args.get("topic"))
    if isinstance(found, str):
        return reply(False, found)
    app, ctx, _ = found
    label = topic_label(Path(env.state_dir), ctx.topic_id) or arg_text(request.args.get("topic"))
    if projects.enabled() and projects.is_member_topic(Path(env.state_dir), ctx.topic_id):
        # A fork of a project's thread would be no thread of it, out of its lead's sight (W2-B): the lead decides.
        return reply(False, f"{label or '这个话题'} 是专项的话题，不能分叉：用 projects_list 查专项的 id，再用 project_send 把话交给"
                            "负责人，由它决定要不要再开话题。")
    path, token = record_path(env.state_dir, request), request_token(request)
    record = read_object(path)
    if record.get("answer"):
        return record["answer"]
    return once_a_turn(env.state_dir, request, "topic_fork", title, prompt,
                       lambda: _fork(env, request, chat, app, ctx, label, path, token, record, title, prompt),
                       rest={**rest_of(request.args, "title", "prompt"), "source": ctx.topic_id})


def _fork(env, request, chat, app, ctx, label, path, token, record, title, prompt):
    """topic_fork's work once the topic is found and no same call came first: the fork and its first turn."""
    # Its turns on every Harness: one 🔁 换配置 moved to another has its finished turns on the one before.
    records = chat_records(env.state_dir, ctx.topic_id, chat)
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
                                   "user_requested": False, "title": title,
                                   "at": record["at"], "fork_of": ctx.topic_id, **said_in(env, request)}
            write_json(Path(env.state_dir) / "topics" / fork["topic_id"] / "topic.json", new_topic,
                       ensure_ascii=False, indent=2)
    if "memory_said" not in record:  # 派活时挑带 (app/memory_picks.py), once: before the fork's first turn.
        fork_ctx = env.topics.apps[new_topic["harness_id"]].submission.event_context(
            env.topics.apps[new_topic["harness_id"]].harness.context, {"chat_id": chat, "message_id": fork["root_id"]})
        record["memory_said"] = carry(env, fork_ctx, request.args.get("memory"), lead_of(env, request))
        save(path, record)
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
        stamped = source_of(env, request, owner)  # Not ``source``: that is the turn the fork is made from.
        target_app.submission.enqueue_chat_job(target, content, chat, mid, prompt_content=sent, title=title,
                                               **({"source": stamped} if stamped else {}))
    data = {"topic_id": fork["topic_id"], "label": new_label, "root_message_id": fork["root_id"],
            "source_topic_id": ctx.topic_id, "source_job_id": source["job_id"]}
    return done(path, record, reply(True, f"已从 {label} 的任务 #{source['job_id']} 分叉出 {new_label}「{title}」，"
                                     "第一轮已提交。" + str(record.get("memory_said") or ""), **data))


TOOLS = {"topic_start": topic_start, "topic_send": topic_send, "topic_fork": topic_fork}
