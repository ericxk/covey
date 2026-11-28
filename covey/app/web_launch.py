"""网页开新话题 (docs/web.md, COVEY_WEB=1; data contract v17 ``launch``): what the launch card offers a new topic of the
owner's private chat, pushed for the page's chat box (``catalog``), and the page's ``launch`` that opens one with the choices made there
(``launch``), started at once.

- ``catalog`` is the launch card's own reading: the menu (app/launch_models.py available_models) as the card lists it (without Claude
  Code's "Default (recommended)", core/menu_choice.py offered_models) in its order (app/preselection.py launch_selection: the
  preselected model first, then the chat's recent ones), each model with its Harness and provider and their titles, the budgets the
  card offers (core/menu_choice.py plain_budgets: the levels by name), ``start_budget`` (the budget the card
  preselects when that model is chosen: launch_selection with that model as the draft, so the 新话题 page's budget, the chat's usual one
  and 推荐 come in the card's order), whether it can redact and its work modes; ``devices`` (v43, only with COVEY_DEVICES=1 and devices: 本机
  first, then each device, ``device_rows``), the page's 执行设备 choice; no
  workspaces or run modes any more: a topic starts in the home); and ``preselect``, what a launch card of the private chat
  preselects now (the 新话题 page, the last 开始任务, the chat's usage). The page applies two rules of the card's: a Harness or provider
  choice keeps the model when it is among that one's models, else takes the first in this order (core/menu_choice.py tier_pick); a
  model choice takes its ``start_budget``.
- ``launch``: the choices checked as 开始任务 checks them (the model on the menu, a budget it offers, a work mode it has, 敏感词脱敏 only
  where it can redact, a device's only where it is allowed and declares the Harness; an older page's workspace and run mode as before, never a
  worktree on a device; ``repos``, at most ten, as app/butler_topics.py declared_repos takes them), then the topic opened as the butler opens one that starts at once (app/butler_topics.py: its topic card at the
  top level of the private chat with the configuration line, its topic.json bound, its first turn queued), the turn being the owner's own
  words (no handoff). The choice is kept as the chat's last 开始任务 and in its usage (app/launch_submit.py remember_launch), so the next card
  and the next page preselect it. A worktree takes minutes to check out, which only the launch card does: such a topic gets the launch
  card prefilled with these choices, to start from there (the receipt says so). Each step is kept in <state>/web/launches/<op_id>.json,
  so an operation done again (the inbox's ledger never does it twice) does not open a second topic.
- ``files`` (data contract v25): the first turn's attachments, as a ``say``'s (app/web_files.py): taken from the page before anything is
  opened, then moved into the new topic's incoming_media and listed after the words.
"""
from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

from covey.core.state import keyed_json_path, read_object, write_json

FOLDER = "launches"
TITLE_CHARS = 40
TEXT_CHARS = 20000


def _title(text) -> str:
    line = next((part.strip() for part in str(text).splitlines() if part.strip()), "")
    return line[:TITLE_CHARS] or "网页上开的话题"


def base_app(env):
    topics = env.topics
    app = topics.apps.get(topics.default) or next(iter(topics.apps.values()))
    return app, app.models.configured_context(app.harness.context)


def workspace_config(env, ctx, chat):
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


def _selection(env, choices, reads, draft=None):
    from covey.app.launch import alias_harnesses
    from covey.app.preselection import launch_selection
    _, base = base_app(env)
    defaults = reads["defaults"]
    return launch_selection(choices, reads["usage"], device_harnesses=alias_harnesses(),
                            model=f"{env.topics.default}:{base.model}", work_mode="", fresh=True,
                            saved=reads["saved"], draft=draft, topic_budget=reads["budget"],
                            topic_model=(defaults.harness.lower(), defaults.model))


LOCAL_TITLE = "本机"  # The 执行设备 row's own option, as the launch card has it (cards/launch_config.py LOCAL).


def device_rows(env, ctx, chat) -> list:
    """The data contract v43 ``devices`` of the launch block: [{name, title, dot, harnesses, local?}], the 执行设备 choices of the
    chat as the launch card lists them: 本机 first (``name`` "", ``local`` true, ``harnesses`` [] for every Harness enabled here),
    then each device the chat may use (its alias, its devices.json title); only with COVEY_DEVICES=1 and such a device (else
    empty, and the page has no such choice). ``dot``: the node panel's status word (app/web_panels.py node_row), "gray" where it
    is not known. A page older than 本机's row draws it as a nameless option that means 本机 all the same (no ``device``)."""
    from covey.app.launch import alias_harnesses
    from covey.core import devices
    from covey.core.nodes import LOCAL
    harnesses = alias_harnesses()
    allowed = [alias for alias in harnesses if env.runtime.access.allows_device(chat, alias)]
    if not allowed:
        return []

    def title(alias):  # The card's word for it (app/launch.py device_options).
        try:
            return str(devices.current().device(alias).title or alias)
        except (devices.UnknownDevice, devices.DeviceConfigError, AttributeError):
            return alias
    dots = {}
    try:
        from covey.app.node_panel import facts
        from covey.app.web_panels import node_row
        dots = {row["alias"]: node_row(row)["dot"] for row in facts(Path(ctx.state_dir), "")["rows"]}
    except Exception:  # noqa: BLE001 - a dot the page cannot read is gray, not a launch refused
        pass
    local = {"name": "", "title": LOCAL_TITLE, "dot": dots.get(LOCAL) or "gray", "harnesses": [], "local": True}
    return [local, *({"name": alias, "title": title(alias), "dot": dots.get(alias) or "gray", "harnesses": list(harnesses[alias])}
                     for alias in allowed)]


def catalog(env, ctx, chat) -> dict:
    """The data contract v17 ``launch`` block (see the module's docstring)."""
    from covey.app.launch_models import available_models
    from covey.core.menu_choice import budget_options, harness_of, offered_models, plain_budget, plain_budgets, tiers
    from covey.core.defaults import WORK_MODES
    choices = available_models(env.topics, ctx)
    reads = _reads(env, ctx, chat)
    found = _selection(env, choices, reads)
    # The models the launch card lists (docs/ui.md 13 U5): Claude Code's "Default (recommended)" names none, so it is not
    # one of them; 开始任务's check (model_choice) still takes the whole menu.
    ranked = offered_models(found.choices, found.model)
    models = []
    for key, entry in ranked.items():
        harness = harness_of(key, entry)
        names, providers, _ = tiers(ranked, harness)
        # The budgets by name, as the 设置 card lists them (T615): no 推荐 or 模型原生默认, which do not say what they run;
        # the preselected and default ones are replaced by the level they name. 开始任务's check (model_choice) still
        # takes the card's whole list, a superset.
        options = plain_budgets(entry)
        start = plain_budget(entry, _selection(env, choices, reads, draft={"model": key}).effort)
        default = plain_budget(entry, budget_options(entry)[1])
        models.append({"key": key, "harness": harness, "harness_title": names.get(harness, harness),
                       "provider": entry.get("provider", ""), "provider_title": providers.get(entry.get("provider", ""), "自带"),
                       "label": str(entry.get("display_name") or entry.get("label") or key.partition(":")[2]),
                       "budgets": [{"value": value, "text": text} for value, text in options], "default_budget": default,
                       "start_budget": start, "redaction": bool(entry.get("redaction")),
                       # v36: a project's lead can run on it (its Harness takes the Bridge's tools, app/lead_harness.py).
                       "bridge_tools": bool(entry.get("bridge_tools")),
                       "work_modes": list(entry.get("work_modes") or ()),
                       # The card's words for each mode (core/defaults.py WORK_MODES: name（what it does）).
                       "work_mode_texts": {mode: "{}（{}）".format(*WORK_MODES[mode]) if mode in WORK_MODES else mode
                                           for mode in entry.get("work_modes") or ()}})
    defaults = reads["defaults"]
    devices = device_rows(env, ctx, chat)
    return {"models": models, **({"devices": devices} if devices else {}),
            "preselect": {"model": found.model, "effort": plain_budget(ranked.get(found.model) or {}, found.effort), "work_mode": found.work_mode,
                          "redaction": "on" if defaults.redaction else "off"}}


def record_path(state_dir, op_id) -> Path:
    return keyed_json_path(Path(state_dir) / "web", FOLDER, op_id)


def model_choice(env, ctx, payload) -> tuple:
    """(harness, key, menu entry, budget, work mode, redaction) of the page's choices of a model, checked as 开始任务 checks them (the
    model on the menu, a budget it offers, a work mode it has, 敏感词脱敏 only where it can redact); ValueError says why not. The 换配置
    of the page (app/web_switch.py) checks its model the same way."""
    from covey.app.launch_models import available_models
    from covey.core.menu_choice import budget_options, harness_of
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
    return harness, key, choice, effort, work_mode, redaction


REPOS_MAX = 10


def repos_of(env, ctx, payload) -> list:
    """The page's ``repos`` ([{name, mode: read|write}], at most REPOS_MAX; data contract v43) as app/butler_topics.py declared_repos
    makes them; [] without. ValueError says what is wrong. A release whose butler_topics has no ``declared_repos`` yet gets the
    list as the page sent it, after the same plain checks."""
    value = payload.get("repos")
    if value in (None, "", []):
        return []
    if not isinstance(value, list) or len(value) > REPOS_MAX:
        raise ValueError(f"仓库最多 {REPOS_MAX} 个")
    from covey.app import butler_topics
    declared = getattr(butler_topics, "declared_repos", None)
    if declared is not None:
        return declared(Path(ctx.state_dir), value)
    out = []
    for each in value:
        name = str(each.get("name") or "").strip() if isinstance(each, dict) else ""
        mode = each.get("mode") if isinstance(each, dict) else None
        if not name or len(name) > 64 or mode not in ("read", "write"):
            raise ValueError("仓库要写名字，用途只能是 read 或 write")
        out.append({"name": name, "mode": mode})
    return out


def plan_of(env, ctx, chat, payload) -> dict:
    """The topic's plan from the page's choices, checked as 开始任务 checks them; ValueError says why not.

    No workspace or run mode in the payload: the topic starts in the home (app/launch_submit.py home_binding), on the device the
    optional ``device`` names. A payload that has them (an older page) is checked as before, a worktree going to the launch card.
    The optional ``repos`` are the ones the topic declares (``plan["repos"]``; ``plan["home_start"]`` marks a home start, both
    kept in topic.json by app/butler_topics.py bind_topic)."""
    from covey.app.launch_models import run_choice
    from covey.app.launch_submit import home_binding, home_selection
    from covey.core import devices
    from covey.core.menu_choice import chosen_work_mode
    from covey.core.workspaces import RUN_MODES, resolve_binding
    harness, key, choice, effort, work_mode, redaction = model_choice(env, ctx, payload)
    repos = repos_of(env, ctx, payload)
    workspace, requested = str(payload.get("workspace") or ""), str(payload.get("run_mode") or "")
    run_mode = requested or "direct"
    if run_mode not in RUN_MODES:
        raise ValueError("运行方式只能是原目录或独立 worktree")
    home = not workspace and run_mode == "direct"
    if home:
        device = str(payload.get("device") or "")
        if device and not env.runtime.access.allows_device(chat, device):
            raise ValueError(f"设备 {device} 不允许在这个聊天里使用")
        name, binding = home_binding(ctx, device)
    else:
        if not workspace:  # A run mode alone (an older page): in the home, under the name the workspace list gives it.
            workspace = home_selection(ctx)
        elif workspace not in workspace_config(env, ctx, chat)[0]["workspaces"]:
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
            "redaction": redaction, "start": run_mode == "direct", **({"repos": repos} if repos else {}),
            # A home start on this machine only: one on a device runs in the device's directory, as the butler's and the
            # launch card's do (core/topics.py mark_home).
            **({"home_start": True} if home and not binding.device else {})}


def staged_files(env, op_id, record, payload, fetch) -> list:
    """The launch's ``files`` (data contract v25) taken from the page into <state>/web/launches/<op_id>.files/ before the topic is opened,
    so a file that cannot be taken opens nothing; [{kind, name, path}] kept in the record, the same list when done again."""
    from covey.app import web_files
    if "files" in record:
        return record["files"]
    files = web_files.listed(payload.get("files"))
    if not files:
        return []
    if fetch is None:
        raise ValueError("Covey 现在取不到网页上的附件")
    staging = record_path(env.state_dir, op_id).with_suffix(".files")
    return [{"kind": item.kind, "name": item.name, "path": item.path} for item in web_files.fetch(fetch, staging, files)]


def placed_files(staged, media_dir) -> list:
    """The staged files moved into the new topic's incoming_media (a file moved before, by a launch done again, is found there);
    ListedAttachment each."""
    import os
    from covey.core.attachments import ListedAttachment
    media_dir = Path(media_dir)
    media_dir.mkdir(parents=True, exist_ok=True)
    out = []
    for each in staged:
        source = Path(each["path"])
        dest = media_dir / source.name
        if source.exists():
            os.replace(source, dest)
        out.append(ListedAttachment(each["kind"], each.get("name") or "", str(dest)))
    for folder in {Path(each["path"]).parent for each in staged}:
        try:
            folder.rmdir()  # Empty once every file moved; left alone otherwise.
        except OSError:
            pass
    return out


def launch(env, op_id, chat, payload, fetch=None) -> str:
    """Open the topic (see the module's docstring); the receipt the page shows, with the topic's T number. ``fetch``: the page's
    /openapi request (app/web_ops.py Ops.fetch), for the attachments of the first turn (data contract v25)."""
    from covey.app import butler_topics
    from covey.app.launch_submit import remember_launch
    text = str(payload.get("text") or "").strip()
    if not text:
        raise ValueError("要先写这个话题做什么")
    if len(text) > TEXT_CHARS:
        raise ValueError(f"话太长（超过 {TEXT_CHARS} 字）")
    _, base = base_app(env)
    ctx = SimpleNamespace(state_dir=Path(env.state_dir), profile=env.profile, default_workdir=base.default_workdir)
    path = record_path(env.state_dir, op_id)
    record = read_object(path)
    if record.get("answer"):
        return record["answer"]
    if not record.get("plan"):
        record = {"op_id": op_id, "chat_id": chat, "title": _title(text), "why": "你在网页上开的", "plan": plan_of(env, ctx, chat, payload),
                  "at": time.time(), "sequence": 0}
        record["files"] = staged_files(env, op_id, record, payload, fetch)
        write_json(path, record, ensure_ascii=False)
    plan = record["plan"]
    app = env.topics.apps[plan["harness"]]
    request = SimpleNamespace(id="web-" + str(op_id), job_key="")
    config = butler_topics.config_line(env, plan)
    if not butler_topics.send_root(env, path, record, chat, record["title"], config, "web-" + str(op_id)):
        raise ValueError("话题卡没有发出去，没有开话题；可以稍后再开")
    root = record["root_message_id"]
    event = {"chat_id": chat, "message_id": root, "root_id": root}
    label = butler_topics.bind_topic(env, app, request, path, record, {}, event, config, kind="web")
    message = "web-" + str(op_id)
    from covey.core.history_storage import append_history
    from covey.core.jobs import find_job_record
    if not find_job_record(Path(env.state_dir), message) and not app.runtime.jobs.settling(message):
        target = app.submission.event_context(app.harness.context, event)
        owner = env.runtime.owner_open_id or ""
        if record.get("files"):  # The first turn lists them as a Feishu message's attachments (app/web_files.py with_files).
            from covey.app.web_files import with_files
            text = with_files(text, placed_files(record["files"], target.history_path.parent / "incoming_media"))
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
