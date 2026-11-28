"""网页换配置 (docs/web.md, COVEY_WEB=1; data contract v21 ``config`` and ``switch``): a topic's configuration as the page's chat box shows it
(``config``), and the page's ``switch`` that changes it as 🔁 换配置's 确认更换 does (``switch``).

- ``config``: what a topic runs with, in the words of the ``launch`` block (app/web_launch.py catalog): the model's menu key (a topic bound to a
  budget preset is that model with the preset's budget, as the 换配置 card preselects it, app/preselection.py switch_selection), its budget, work
  mode and, as read-only facts for a page that still shows them, workspace and run mode, and the device it works on (``device``,
  "" for 本机; T879). Read from topic.json alone, so a push does not ask anything else. ``switch`` never moves a topic to another machine:
  a payload's ``device`` other than the topic's is refused (DEVICE_FIXED), none or "" leaves it; an older page's workspace on a
  device is taken only for a topic already on one (TO_DEVICE, 主人 D2Y4T).
- ``switch``: the page's choices checked as 开始任务 checks a model (app/web_launch.py model_choice); a place (workspace, run mode) the payload may still carry (an older page; the new one sends none and the topic stays where it is) other than the
  topic's checked as the switch card checks it, but a worktree the Bridge would have to make now is left to the card (minutes on the NAS, which
  the inbox thread does not wait); then the rebind of the switch card (app/binding.py switch_topic: refused while a task is queued or runs, and
  for nothing changed). The model's choice is kept in the chat's usage as the card keeps it, and the topic's thread in Feishu gets the
  card's 已换配置 words, so Feishu shows the switch the page made. The receipt says from what to what.
- A project's lead (data contract v36): its ``config`` is pushed too, and ``switch`` changes it as the 专项卡's 换配置 does
  (app/harness_switch.py): only a model a lead can run on (app/lead_harness.py ``can_lead``), its workspace and run mode as they
  are; the 已换配置 words go as a reply to the 专项卡.
- The repositories a topic uses (data contract v43 §4, ``repos?: {name, mode}[]``; ``asked_repos``): a payload that has ``repos``
  sets them (each checked as a new topic's are, app/butler_topics.py ``declared_repos``; one the topic has keeps its worktree and
  what it read, a new one is declared; ``[]`` clears them), one without leaves them. Repositories alone are a change too: written
  into topic.json as they are, no rebind and no new session (``binding_revision`` stays), still not while a task runs. A lead's
  are the project's, read only: not changed here.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace


WORKTREE_ON_CARD = "新建独立 worktree 要在飞书的换配置卡上做"
LEAD_PLACE = "负责人的工作区和运行方式不能换：只能换 Harness、模型和思考预算"
LEAD_REPOS = "负责人用的仓库跟着专项（只读），不能在这里换"
DEVICE_FIXED = "换配置不能换执行设备，话题留在原来的机器上；要用别的设备，在话题里用 covey device"
# A topic here moved onto a device's workspace by an older page's 换配置 (主人 D2Y4T): only a topic already on a device may still
# switch to one; any other is refused so (core/devices.py START_RETIRED).
TO_DEVICE = "换配置不能把话题换到设备上：执行设备入口已下线；要用别的设备，在话题里用 covey device"
NO_REPOS = "无"


def _named(entries) -> list:
    """The ``repos`` entries with a name, in order (what app/web_turns.py topic_fields reads)."""
    return [entry for entry in entries or () if isinstance(entry, dict) and str(entry.get("name") or "").strip()]


def _pairs(entries) -> list:
    return sorted((str(e["name"]), "write" if e.get("mode") == "write" else "read") for e in _named(entries))


def shown_repos(entries) -> list:
    """The topic's repositories the page was given (app/web_turns.py topic_fields: the first REPOS_KEPT with a name): those a
    switch's ``repos`` speaks of. The others it never saw are kept as they are."""
    from covey.app.web_turns import REPOS_KEPT
    return _named(entries)[:REPOS_KEPT]


def repos_words(entries) -> str:
    """The repositories as a receipt names them: ``covey（改）、web``, 无 for none."""
    return "、".join(name + ("（改）" if mode == "write" else "") for name, mode in _pairs(entries)) or NO_REPOS


def asked_repos(state, payload, topic):
    """The repositories the payload's ``repos`` asks for, [{name, mode} or a new declared entry] (``merged_repos`` applies
    them), or None when it has none (or null) or asks for the ones the page was shown (``shown_repos``: the same names and
    modes; the page sends them back with every switch). Only what changes is checked as a new topic's repositories are
    (app/butler_topics.py ``declared_repos``: a new name must be the repo map's, a write must not be to a frozen or read-only
    one): one the topic has and keeps, or writes no more, is not checked again (the map may have frozen or dropped it
    since). ``[]`` clears them. ValueError says what is wrong."""
    asked = payload.get("repos")
    if asked is None:
        return None
    from covey.app.butler_topics import REPO_MODES, REPO_NAME_CHARS, REPOS_MAX, declared_repos
    if not isinstance(asked, list) or len(asked) > REPOS_MAX:
        raise ValueError(f"repos 要是最多 {REPOS_MAX} 项的列表，每项 {{name, mode: read|write}}")
    wanted = {}
    for item in asked:
        name = str(item.get("name") or "").strip() if isinstance(item, dict) else ""
        mode = item.get("mode", "read") if isinstance(item, dict) else ""
        if not name or len(name) > REPO_NAME_CHARS or mode not in REPO_MODES:
            raise ValueError(f"repos 的每一项要有 name（{REPO_NAME_CHARS} 字内）和 mode（read 或 write）")
        wanted[name] = "write" if mode == "write" or wanted.get(name) == "write" else "read"  # A name twice: its write kept.
    shown = shown_repos(topic.get("repos"))
    if sorted(wanted.items()) == _pairs(shown):
        return None
    had = {str(entry["name"]): entry for entry in shown}
    fresh = [{"name": name, "mode": mode} for name, mode in wanted.items()
             if name not in had or (mode == "write" and had[name].get("mode") != "write")]
    try:
        declared = {repo["name"]: repo for repo in declared_repos(state, fresh)} if fresh else {}
    except ValueError as exc:
        raise ValueError(str(exc).replace("；用 repos_list 看", "")) from None
    return [{"name": name, "mode": mode} if name in had else declared[name] for name, mode in wanted.items()]


def merged_repos(current, wanted) -> list:
    """topic.json's ``repos`` once ``wanted`` (``asked_repos``) is applied to ``current``: those of ``wanted`` in its order (one
    ``current`` has kept whole, with the mode asked), then the entries the page was not shown, as they are; the shown ones
    ``wanted`` leaves out are gone."""
    current = [dict(entry) for entry in current or () if isinstance(entry, dict)]
    shown = {str(entry["name"]) for entry in shown_repos(current)}
    had = {str(entry["name"]): entry for entry in _named(current)}
    names = {repo["name"] for repo in wanted}
    out = [{**had[repo["name"]], "mode": repo["mode"]} if repo["name"] in had else dict(repo) for repo in wanted]
    return out + [entry for entry in current if str(entry.get("name") or "").strip() not in shown | names]


def _worktree_prefix() -> str:
    from covey.core.workspaces import worktree_root
    return str(worktree_root()).rstrip("/") + "/"


def run_mode(workdir, prefix=None) -> str:
    """"worktree" for a topic in a Bridge worktree, else "direct" (app/binding.py run_mode_of without resolving the path, which a push
    must not wait on)."""
    workdir = str(workdir or "")
    return "worktree" if workdir.startswith(prefix or _worktree_prefix()) else "direct"


def device_of(workdir) -> str:
    """The alias of the device a topic works on (its workdir ``alias:path``, core/devices.py device_shaped, which reads nothing), ""
    for 本机."""
    from covey.core.devices import device_shaped
    return str(workdir).strip().split(":", 1)[0] if workdir and device_shaped(workdir) else ""


def topic_config(topic, choices=None, prefix=None):
    """The data contract v21 ``config`` of a topic's record (topic.json); None without a Harness. ``choices``: the menu (available_models),
    to find a budget preset's model; None or {}: the topic's own model and budget as they are. ``device`` (T879): the device it works
    on, "" for 本机, read only (``switch`` does not move a topic to another machine)."""
    from covey.core.menu_choice import preset_choice
    harness = str(topic.get("harness_id") or "")
    if not harness:
        return None
    model = str(topic.get("model") or "")
    key, budget = preset_choice(choices or {}, harness, model) or (f"{harness}:{model}", str(topic.get("effort") or ""))
    return {"model": key, "effort": budget, "work_mode": str(topic.get("work_mode") or ""),
            "workspace": str(topic.get("workspace_name") or ""), "run_mode": run_mode(topic.get("workdir"), prefix),
            "device": device_of(topic.get("workdir"))}


def _label(app, choices, key, topic) -> str:
    entry = choices.get(key) or {}
    return str(entry.get("label") or f"{app.spec.title} · {topic.get('model') or '默认模型'}")


def _place(env, ctx, chat, current, workspace, mode, harness, choice):
    """None for where the topic runs now, else {workspace, workdir, run_mode, branch}; ValueError for one the page may not choose."""
    from covey.app.web_launch import workspace_config
    from covey.core import devices
    from covey.core.workspaces import RUN_MODES, resolve_binding
    if mode not in RUN_MODES:
        raise ValueError("运行方式只能是原目录或独立 worktree")
    if (workspace, mode) == (current["workspace"], current["run_mode"]):
        return None
    if mode == "worktree":
        raise ValueError(WORKTREE_ON_CARD)
    config, _, _ = workspace_config(env, ctx, chat)
    if workspace not in config["workspaces"]:
        raise ValueError(f"没有登记工作区「{workspace}」，请重新选")
    name, binding = resolve_binding(Path(ctx.state_dir), ctx.default_workdir, workspace)
    if binding.device:
        if not current["device"]:
            raise ValueError(TO_DEVICE)  # D2Y4T: only an older topic on a device keeps this way.
        if not env.runtime.access.allows_device(chat, binding.device):
            raise ValueError(f"设备 {binding.device} 不允许在这个聊天里使用")
        refused = devices.undeclared_refusal(binding.device, harness, choice.get("title") or harness)
        if refused:
            raise ValueError(refused.replace("任务尚未启动", "话题配置没有改变"))
        return {"workspace": name, "workdir": binding.value, "run_mode": "direct", "branch": ""}
    return {"workspace": name, "workdir": str(binding.workdir), "run_mode": "direct", "branch": ""}


def _receipt(apps, before, after, lead=False, repos="") -> str:
    from covey.core.model_names import model_name

    def named(side):
        app = apps.get(side["harness_id"])
        title = app.spec.title if app is not None else side["harness_id"]
        return " · ".join(part for part in (title, model_name(side.get("model")) or side.get("model") or "默认模型") if part)
    parts = []
    if [before[k] for k in ("harness_id", "model", "effort", "work_mode")] != [after[k] for k in ("harness_id", "model", "effort", "work_mode")]:
        parts.append(f"{named(before)} → {named(after)}" + (f" · 思考 {after['effort']}" if after.get("effort") else ""))
    if (before.get("workspace"), before.get("run_mode")) != (after.get("workspace"), after.get("run_mode")):
        parts.append(f"工作区 {before.get('workspace') or '?'} → {after.get('workspace') or '?'}")
    if repos:
        parts.append(repos)
    return "已换配置：" + "；".join(parts or ["已更新"]) + ("，负责人下一轮起由新会话接着做" if lead else "，下一句话起由新会话接着做")


def switch(env, op_id, chat, topic_id, payload) -> str:
    """Change the topic's configuration (see the module's docstring); the receipt the page shows. ValueError says why nothing changed."""
    from covey.app import lead_harness, model_usage
    from covey.app.binding import busy_reason, switch_topic
    from covey.app.harness_switch import done_text
    from covey.app.launch_models import available_models, run_choice
    from covey.app.web_launch import base_app, model_choice
    from covey.core import devices
    from covey.core.menu_choice import chosen_work_mode
    from covey.core.topics import topic_label, update_repos
    state = Path(env.state_dir)
    lead = lead_harness.target(env.topics, state, str(topic_id or ""), chat)  # A project's lead (v36), else None.
    found = lead[:3] if lead else env.topics.target(state, str(topic_id or ""), chat)
    if found is None:
        raise ValueError("这个话题不认识，或不在你的私聊里，或已经结束")
    app, target, topic = found
    _, base = base_app(env)
    ctx = SimpleNamespace(state_dir=state, profile=env.profile, default_workdir=base.default_workdir)
    choices = available_models(env.topics, ctx)
    current = topic_config(topic, choices)
    harness, key, choice, budget, mode = model_choice(env, ctx, payload)
    # A page that offered the 执行设备 as a choice (before T879) may send one: never applied here, so not taken in silence either.
    asked_device = str(payload.get("device") or "")
    if asked_device and asked_device != current["device"]:
        raise ValueError(DEVICE_FIXED)
    # The page no longer sends a workspace or run mode (data contract v43: both optional): none, or the topic's own, moves nothing; an
    # older page's other one is checked and applied as before, a lead's refused. ``repos``: the ones the topic uses (``asked_repos``).
    workspace = str(payload.get("workspace") or current["workspace"])
    repos = None if lead else asked_repos(state, payload, topic)
    if lead:
        if (workspace, str(payload.get("run_mode") or current["run_mode"])) != (current["workspace"], current["run_mode"]):
            raise ValueError(LEAD_PLACE)
        asked = payload.get("repos")
        if asked is not None and (not isinstance(asked, list) or _pairs(asked) != _pairs(shown_repos(topic.get("repos")))):
            raise ValueError(LEAD_REPOS)
        if not lead_harness.can_lead(choice):
            raise ValueError(lead_harness.CANNOT_LEAD)
        place = None
    elif not (payload.get("workspace") or payload.get("run_mode")):
        place = None
    else:
        place = _place(env, ctx, chat, current, workspace, str(payload.get("run_mode") or current["run_mode"]), harness, choice)
    same = (key, budget, mode, place) == (current["model"], current["effort"], current["work_mode"], None)
    if same and repos is None:
        raise ValueError("配置没有变化")
    reason = busy_reason(env.topics, target, chat, action="换配置")
    if reason:
        raise ValueError(reason.replace("在任务卡上停止", "停止"))
    moved = f"仓库 {repos_words(shown_repos(topic.get('repos')))} → {repos_words(repos)}" if repos is not None else ""

    def set_repos():
        update_repos(state, target.topic_id, lambda entries: merged_repos(entries, repos))
    if same:  # Only the repositories: topic.json's, no rebind (binding_revision and the session stay).
        set_repos()
        try:
            app.messenger.reply(target, chat, f"🔁 网页上改了用到的仓库：{moved[3:]}（会话不变）", app.key("switch-web", str(op_id)),
                                voice=app.voice)
        except Exception as exc:  # noqa: BLE001 - the page has its receipt
            env.runtime.log(f"web switch note not sent topic={str(topic_id)[:12]}: {type(exc).__name__}")
        return f"已换配置：{moved}，会话不变"
    if key == current["model"] and budget == current["effort"]:
        model, effort = str(topic.get("model") or ""), str(topic.get("effort") or "")  # The topic's own, untouched (a preset, a default).
    else:
        model, effort = run_choice(choice, budget)
    work_mode = chosen_work_mode(choice, mode)
    where = place["workdir"] if place else str(topic.get("workdir") or "")
    if place is None and devices.is_device_value(where):  # A place chosen now was checked in _place.
        refused = devices.undeclared_refusal(where.split(":", 1)[0], harness, choice.get("title") or harness)
        if refused:
            raise ValueError(refused.replace("任务尚未启动", "话题配置没有改变"))
    form = {"harness_id": topic["harness_id"], "binding_revision": topic.get("binding_revision", 1), "job_id": None}
    record = {"topic": topic_label(state, target.topic_id), "workspace": current["workspace"],
              "current": {"label": _label(app, choices, current["model"], topic), "work_mode": current["work_mode"],
                          "workspace": current["workspace"], "run_mode": current["run_mode"], "workdir": str(topic.get("workdir") or "")},
              **({"lead": lead_harness.named(lead[3])} if lead else {})}
    switched = switch_topic(env.topics, target, chat, form, harness, model, effort, work_mode, place=place)
    if repos is not None:  # After the rebind it rode on: one that was refused changed nothing.
        set_repos()
    if switched is not None:
        try:
            model_usage.record(state, chat, key, budget, work_mode, "switch", form="web-" + str(op_id))
        except OSError as exc:
            env.runtime.log(f"model usage save failed: {type(exc).__name__}; the switch is done")
    text = done_text(record, choice, effort, work_mode, place, switched) + (f"\n{moved}" if moved else "")
    now = env.topics.apps.get(harness, app)
    try:  # Feishu's thread says it too; the switch is done whether this goes out or not.
        now.messenger.reply(target, chat, text, now.key("switch-web", str(op_id)), voice=now.voice)
    except Exception as exc:  # noqa: BLE001 - the page has its receipt
        env.runtime.log(f"web switch note not sent topic={str(topic_id)[:12]}: {type(exc).__name__}")
    return _receipt(env.topics.apps, *switched, lead=bool(lead), repos=moved)
