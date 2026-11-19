"""网页开新话题 (docs/butler.md 网页工作台, COVEY_WEB=1; data contract v17 ``launch``): what the launch card offers a new topic of the
owner's private chat, pushed for the page's chat box (``catalog``), and the page's ``launch`` that opens one with the choices made there
(``launch``), started at once (the owner, 2026-11-19 T554: 网页上选好就直接开工).

- ``catalog`` is the launch card's own reading: the menu (app/launch_models.py available_models) in the order the card lists it
  (app/preselection.py launch_selection: the preselected model first, then the chat's recent ones), each model with its Harness and
  provider and their titles, the budgets the card offers (core/menu_choice.py budget_options), ``start_budget`` (the budget the card
  preselects when that model is chosen: launch_selection with that model as the draft, so the 新话题 page's budget, the chat's usual one
  and 推荐 come in the card's order), whether it can redact and its work modes; the workspaces the private chat may use, most recently
  used first, a device's with its title and the Harnesses it declares; and ``preselect``, what a launch card of the private chat
  preselects now (the 新话题 page, the last 开始任务, the chat's usage). The page applies two rules of the card's: a Harness or provider
  choice keeps the model when it is among that one's models, else takes the first in this order (core/menu_choice.py tier_pick); a
  model choice takes its ``start_budget``.
- ``launch``: the choices checked as 开始任务 checks them (the model on the menu, a budget it offers, a work mode it has, 敏感词脱敏 only
  where it can redact, a workspace of the list the chat may use, a device's only where it is allowed and declares the Harness, never a
  worktree on a device), then the topic opened as the butler opens one that starts at once (app/butler_topics.py: its topic card at the
  top level of the private chat with the configuration line, its topic.json bound, its first turn queued), the turn being the owner's own
  words (no handoff). The choice is kept as the chat's last 开始任务 and in its usage (app/launch.py remember_launch), so the next card
  and the next page preselect it. A worktree takes minutes to check out, which only the launch card does: such a topic gets the launch
  card prefilled with these choices, to start from there (the receipt says so). Each step is kept in <state>/web/launches/<op_id>.json,
  so an operation done again (the inbox's ledger never does it twice) does not open a second topic.
"""
from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

from covey.core.state import read_object, write_json

FOLDER = "launches"
TITLE_CHARS = 40
TEXT_CHARS = 20000


def _title(text) -> str:
    line = next((part.strip() for part in str(text).splitlines() if part.strip()), "")
    return line[:TITLE_CHARS] or "网页上开的话题"


def _base(env):
    topics = env.topics
    app = topics.apps.get(topics.default) or next(iter(topics.apps.values()))
    return app, app.models.configured_context(app.harness.context)


def _config(env, ctx, chat):
    from covey.app.launch import device_harnesses, device_titles
    from covey.core.workspaces import load_config, visible_workspaces
    config = load_config(ctx.state_dir, ctx.default_workdir)
    config = visible_workspaces(config, lambda alias: env.runtime.access.allows_device(chat, alias))
    return config, device_titles(config), device_harnesses(config)


def _reads(env, ctx, chat) -> dict:
    """What a launch card of ``chat`` reads before it preselects: the chat's usage, its last 开始任务, the 新话题 page."""
    from covey.app import model_usage
    from covey.app.launch import launch_defaults, topic_budget, topic_defaults
    defaults = topic_defaults(ctx.state_dir)
    try:
        saved = launch_defaults(ctx.state_dir, chat)
    except (OSError, ValueError):
        saved = None
    return {"usage": model_usage.recent(ctx.state_dir, chat), "saved": saved, "budget": topic_budget(ctx.state_dir),
            "defaults": defaults}


def _selection(env, choices, config, harnesses, reads, draft=None):
    from covey.app.preselection import launch_selection
    _, base = _base(env)
    defaults = reads["defaults"]
    return launch_selection(choices, reads["usage"], config, device_harnesses=harnesses,
                            model=f"{env.topics.default}:{base.model}", workspace=config["default"], work_mode="", fresh=True,
                            saved=reads["saved"], draft=draft, topic_budget=reads["budget"],
                            topic_model=(defaults.harness.lower(), defaults.model), topic_workspace=defaults.workspace)


def catalog(env, ctx, chat) -> dict:
    """The data contract v17 ``launch`` block (see the module's docstring)."""
    from covey.app.launch_models import available_models
    from covey.core.menu_choice import budget_options, harness_of, tiers
    from covey.core.defaults import WORK_MODES
    from covey.core.workspaces import RUN_MODES
    choices = available_models(env.topics, ctx)
    config, titles, harnesses = _config(env, ctx, chat)
    reads = _reads(env, ctx, chat)
    found = _selection(env, choices, config, harnesses, reads)
    ranked = found.choices
    models = []
    for key, entry in ranked.items():
        harness = harness_of(key, entry)
        names, providers, _ = tiers(ranked, harness)
        options, default = budget_options(entry)
        start = _selection(env, choices, config, harnesses, reads, draft={"model": key}).effort
        models.append({"key": key, "harness": harness, "harness_title": names.get(harness, harness),
                       "provider": entry.get("provider", ""), "provider_title": providers.get(entry.get("provider", ""), "自带"),
                       "label": str(entry.get("display_name") or entry.get("label") or key.partition(":")[2]),
                       "budgets": [{"value": value, "text": text} for value, text in options], "default_budget": default,
                       "start_budget": start, "redaction": bool(entry.get("redaction")),
                       "work_modes": list(entry.get("work_modes") or ()),
                       # The card's words for each mode (core/defaults.py WORK_MODES: name（what it does）).
                       "work_mode_texts": {mode: "{}（{}）".format(*WORK_MODES[mode]) if mode in WORK_MODES else mode
                                           for mode in entry.get("work_modes") or ()}})
    defaults = reads["defaults"]
    workspaces = [{"name": name, "device": titles.get(name, ""), "harnesses": list(harnesses.get(name) or ())}
                  for name in found.names]
    return {"models": models, "workspaces": workspaces,
            "run_modes": [{"value": key, "text": text} for key, text in RUN_MODES.items()],
            "preselect": {"model": found.model, "effort": found.effort, "work_mode": found.work_mode,
                          "redaction": "on" if defaults.redaction else "off", "workspace": found.workspace,
                          "run_mode": defaults.run_mode if defaults.run_mode in RUN_MODES else "direct"}}


def record_path(state_dir, op_id) -> Path:
    return Path(state_dir) / "web" / FOLDER / (str(op_id) + ".json")


def plan_of(env, ctx, chat, payload) -> dict:
    """The topic's plan from the page's choices, checked as 开始任务 checks them; ValueError says why not."""
    from covey.app.launch_models import available_models, run_choice
    from covey.core import devices
    from covey.core.menu_choice import budget_options, chosen_work_mode, harness_of
    from covey.core.workspaces import RUN_MODES, resolve_binding
    choices = available_models(env.topics, ctx)
    key = str(payload.get("model") or "")
    choice = choices.get(key)
    if choice is None:
        raise ValueError("这个模型已不在可选目录中，请重新选")
    harness = harness_of(key, choice)
    if harness not in env.topics.apps:
        raise ValueError(f"本机器人没有启用 {harness}")
    effort = str(payload.get("effort") or "")
    if effort not in dict(budget_options(choice)[0]):
        raise ValueError("这个模型没有这一档思考预算，请重新选")
    work_mode = str(payload.get("work_mode") or "")
    if work_mode and work_mode not in (choice.get("work_modes") or ()):
        raise ValueError("这个模型没有这个工作模式")
    redaction = payload.get("redaction") == "on"
    if redaction and not choice.get("redaction"):
        raise ValueError("这个模型不能脱敏")
    run_mode = str(payload.get("run_mode") or "direct")
    if run_mode not in RUN_MODES:
        raise ValueError("运行方式只能是原目录或独立 worktree")
    config, _, _ = _config(env, ctx, chat)
    workspace = str(payload.get("workspace") or "")
    if workspace not in config["workspaces"]:
        raise ValueError(f"没有登记工作区「{workspace}」，请重新选")
    name, binding = resolve_binding(Path(ctx.state_dir), ctx.default_workdir, workspace)
    if binding.device:
        if not env.runtime.access.allows_device(chat, binding.device):
            raise ValueError(f"设备 {binding.device} 不允许在这个聊天里使用")
        refused = devices.undeclared_refusal(binding.device, harness, choice.get("title") or harness)
        if refused:
            raise ValueError(refused)
        if run_mode == "worktree":
            raise ValueError("设备上的工作区只能用原目录运行")
    model, run_effort = run_choice(choice, effort)
    return {"harness": harness, "model": model, "model_key": key, "effort": run_effort, "preference_effort": effort,
            "label": str(choice.get("label") or model), "work_mode": chosen_work_mode(choice, work_mode),
            "workspace": name, "workdir": binding.value if binding.device else str(binding.workdir), "run_mode": run_mode,
            "redaction": redaction, "start": run_mode == "direct"}


def launch(env, op_id, chat, payload) -> str:
    """Open the topic (see the module's docstring); the receipt the page shows, with the topic's T number."""
    from covey.app import butler_topics
    from covey.app.launch import remember_launch
    text = str(payload.get("text") or "").strip()
    if not text:
        raise ValueError("要先写这个话题做什么")
    if len(text) > TEXT_CHARS:
        raise ValueError(f"话太长（超过 {TEXT_CHARS} 字）")
    _, base = _base(env)
    ctx = SimpleNamespace(state_dir=Path(env.state_dir), profile=env.profile, default_workdir=base.default_workdir)
    path = record_path(env.state_dir, op_id)
    record = read_object(path)
    if record.get("answer"):
        return record["answer"]
    if not record.get("plan"):
        record = {"op_id": op_id, "chat_id": chat, "title": _title(text), "why": "你在网页上开的", "plan": plan_of(env, ctx, chat, payload),
                  "at": time.time(), "sequence": 0}
        write_json(path, record, ensure_ascii=False)
    plan = record["plan"]
    app = env.topics.apps[plan["harness"]]
    request = SimpleNamespace(id="web-" + str(op_id), job_key="")
    config = butler_topics.config_line(env, plan)
    if not butler_topics._root(env, path, record, chat, record["title"], config, "web-" + str(op_id)):
        raise ValueError("话题卡没有发出去，没有开话题；可以稍后再开")
    root = record["root_message_id"]
    event = {"chat_id": chat, "message_id": root, "root_id": root}
    label = butler_topics._bind(env, app, request, path, record, {}, event, config, kind="web")
    message = "web-" + str(op_id)
    from covey.core.history_storage import append_history
    from covey.core.jobs import find_job_record
    if not find_job_record(Path(env.state_dir), message) and not app.runtime.jobs.settling(message):
        target = app.submission.event_context(app.harness.context, event)
        owner = env.runtime.owner_open_id or ""
        append_history(target.history_path, "user", text, {"message_id": message, "chat_id": chat, **({"sender_id": owner} if owner else {})})
        if app.submission.enqueue_chat_job(target, text, chat, message, prompt_content=text, title=record["title"],
                                           sender_id=owner) is None:
            raise ValueError(f"{label or '话题'}已开，但第一轮没有排上：模型配置无法确认（见话题里的说明）")
    if plan["start"] and not record.get("remembered"):
        remember_launch(env.state_dir, chat, plan, form="web-" + str(op_id), log=env.runtime.log)
        record["remembered"] = True
        write_json(path, record, ensure_ascii=False)
    named = f"{label}「{record['title']}」" if label else f"「{record['title']}」"
    answer = (f"已开 {named}：直接开工" if plan["start"]
              else f"已开 {named}：独立 worktree 要由启动卡创建，启动卡已按你的选择填好，在飞书话题里点开始任务")
    record["answer"] = answer
    write_json(path, record, ensure_ascii=False)
    return answer
