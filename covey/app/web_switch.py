"""网页换配置 (docs/web.md, COVEY_WEB=1; data contract v21 ``config`` and ``switch``): a topic's configuration as the page's chat box shows it
(``config``), and the page's ``switch`` that changes it as 🔁 换配置's 确认更换 does (``switch``).

- ``config``: what a topic runs with, in the words of the ``launch`` block (app/web_launch.py catalog): the model's menu key (a topic bound to a
  budget preset is that model with the preset's budget, as the 换配置 card preselects it, app/preselection.py switch_selection), its budget, work
  mode, workspace, run mode and 敏感词脱敏. Read from topic.json alone, so a push does not ask anything else.
- ``switch``: the page's choices checked as 开始任务 checks a model (app/web_launch.py model_choice); a place (workspace, run mode) other than the
  topic's checked as the switch card checks it, but a worktree the Bridge would have to make now is left to the card (minutes on the NAS, which
  the inbox thread does not wait); then the rebind of the switch card (app/binding.py switch_topic: refused while a task is queued or runs, and
  for nothing changed). The model's choice is kept in the chat's usage as the card keeps it, and the topic's thread in Feishu gets the
  card's 已换配置 words, so Feishu shows the switch the page made. The receipt says from what to what.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from covey.core.redaction import TOPIC_KEY as REDACTION_KEY

WORKTREE_ON_CARD = "新建独立 worktree 要在飞书的换配置卡上做"


def _worktree_prefix() -> str:
    from covey.core.workspaces import worktree_root
    return str(worktree_root()).rstrip("/") + "/"


def run_mode(workdir, prefix=None) -> str:
    """"worktree" for a topic in a Bridge worktree, else "direct" (app/binding.py run_mode_of without resolving the path, which a push
    must not wait on)."""
    workdir = str(workdir or "")
    return "worktree" if workdir.startswith(prefix or _worktree_prefix()) else "direct"


def topic_config(topic, choices=None, prefix=None):
    """The data contract v21 ``config`` of a topic's record (topic.json); None without a Harness. ``choices``: the menu (available_models),
    to find a budget preset's model; None or {}: the topic's own model and budget as they are."""
    from covey.core.menu_choice import preset_choice
    harness = str(topic.get("harness_id") or "")
    if not harness:
        return None
    model = str(topic.get("model") or "")
    key, budget = preset_choice(choices or {}, harness, model) or (f"{harness}:{model}", str(topic.get("effort") or ""))
    return {"model": key, "effort": budget, "work_mode": str(topic.get("work_mode") or ""),
            "workspace": str(topic.get("workspace_name") or ""), "run_mode": run_mode(topic.get("workdir"), prefix),
            "redaction": "on" if topic.get(REDACTION_KEY) is True else "off"}


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
        if not env.runtime.access.allows_device(chat, binding.device):
            raise ValueError(f"设备 {binding.device} 不允许在这个聊天里使用")
        refused = devices.undeclared_refusal(binding.device, harness, choice.get("title") or harness)
        if refused:
            raise ValueError(refused.replace("任务尚未启动", "话题配置没有改变"))
        return {"workspace": name, "workdir": binding.value, "run_mode": "direct", "branch": ""}
    return {"workspace": name, "workdir": str(binding.workdir), "run_mode": "direct", "branch": ""}


def _receipt(apps, before, after, redaction, was) -> str:
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
    if redaction != was:
        parts.append("脱敏 " + ("关 → 开" if redaction else "开 → 关"))
    return "已换配置：" + "；".join(parts or ["已更新"]) + "，下一句话起由新会话接着做"


def switch(env, op_id, chat, topic_id, payload) -> str:
    """Change the topic's configuration (see the module's docstring); the receipt the page shows. ValueError says why nothing changed."""
    from covey.app import model_usage
    from covey.app.binding import busy_reason, switch_topic
    from covey.app.harness_switch import done_text
    from covey.app.launch_models import available_models, run_choice
    from covey.app.web_launch import base_app, model_choice
    from covey.core import devices
    from covey.core.menu_choice import chosen_work_mode
    from covey.core.topics import topic_label
    state = Path(env.state_dir)
    found = env.topics.target(state, str(topic_id or ""), chat)
    if found is None:
        raise ValueError("这个话题不认识，或不在你的私聊里，或已经结束")
    app, target, topic = found
    _, base = base_app(env)
    ctx = SimpleNamespace(state_dir=state, profile=env.profile, default_workdir=base.default_workdir)
    choices = available_models(env.topics, ctx)
    current = topic_config(topic, choices)
    harness, key, choice, budget, mode, redaction = model_choice(env, ctx, payload)
    workspace = str(payload.get("workspace") or current["workspace"])
    place = _place(env, ctx, chat, current, workspace, str(payload.get("run_mode") or current["run_mode"]), harness, choice)
    if (key, budget, mode, redaction, place) == (current["model"], current["effort"], current["work_mode"],
                                                 current["redaction"] == "on", None):
        raise ValueError("配置没有变化")
    reason = busy_reason(env.topics, target, chat, action="换配置")
    if reason:
        raise ValueError(reason.replace("在任务卡上停止", "停止"))
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
    was = topic.get(REDACTION_KEY) is True
    form = {"harness_id": topic["harness_id"], "binding_revision": topic.get("binding_revision", 1), "job_id": None}
    record = {"topic": topic_label(state, target.topic_id), "workspace": current["workspace"],
              "current": {"label": _label(app, choices, current["model"], topic), "work_mode": current["work_mode"],
                          "workspace": current["workspace"], "run_mode": current["run_mode"], "workdir": str(topic.get("workdir") or "")}}
    switched = switch_topic(env.topics, target, chat, form, harness, model, effort, work_mode, place=place, redaction=redaction)
    if switched is not None:
        try:
            model_usage.record(state, chat, key, budget, work_mode, "switch", form="web-" + str(op_id))
        except OSError as exc:
            env.runtime.log(f"model usage save failed: {type(exc).__name__}; the switch is done")
    text = done_text(record, choice, effort, work_mode, place, redaction, was, switched)
    now = env.topics.apps.get(harness, app)
    try:  # Feishu's thread says it too; the switch is done whether this goes out or not.
        now.messenger.reply(target, chat, text, now.key("switch-web", str(op_id)), voice=now.voice)
    except Exception as exc:  # noqa: BLE001 - the page has its receipt
        env.runtime.log(f"web switch note not sent topic={str(topic_id)[:12]}: {type(exc).__name__}")
    if switched is None:
        return "已打开脱敏，会话不变" if redaction else "已关闭脱敏，会话不变"
    return _receipt(env.topics.apps, *switched, redaction, was)
