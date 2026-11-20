"""What Harness, model and thinking budget a topic the butler opens runs with (docs/butler.md 开话题): ``resolve`` takes
each field from the call, else the 设置 card's 新话题 defaults, else the chat's last 开始任务, else the Bridge's own, and
says which of them it was (``sources``). It reads the menu app/launch_models.py ``available_models`` lists and the last
launch app/launch.py ``launch_defaults`` saved, and turns a budget the way 开始任务 does (core/menu_choice.py,
app/launch_models.py ``run_choice``); it does not draw a card (app/preselection.py is the launch card's own order of
preference). Used by the butler's topic tools (app/butler_topics.py) and the 管家 card's preview (app/butler_panel.py).
"""
from __future__ import annotations

from pathlib import Path

from covey.app.butler_env import arg_text
from covey.app.launch import launch_defaults
from covey.app.launch_models import available_models, run_choice
from covey.core.defaults import RECOMMENDED
from covey.core.menu_choice import budget_effort, find_choice
from covey.core.roster import NAMES

# Where a field topic_start opens with came from, in the order resolve looks.
CALL, SETTINGS, LAST, BRIDGE = "call", "settings", "last", "bridge"


def last_launch(state_dir, chat):
    """(Harness, menu key, budget option) of the chat's last 开始任务 (app/launch.py launch_defaults: what the
    launch card preselects), or None."""
    try:
        saved = launch_defaults(Path(state_dir), chat) if chat else None
    except (OSError, ValueError):
        return None  # No launch yet, here or anywhere.
    if not isinstance(saved, dict):
        return None
    key = str(saved.get("model_choice") or "")
    harness, _, model = key.partition(":")
    if not harness or not model:
        return None
    return harness, key, saved["effort"] if isinstance(saved.get("effort"), str) else ""


def _named(choices, listed, name, model):
    """The menu key of ``model`` as a call or the settings name it (its id or its menu name); None."""
    found = find_choice(choices, f"{name}:{model}")
    if found:
        return found[0]
    return next((k for k, entry in listed.items()
                 if model.lower() in (str(entry.get("display_name") or "").lower(),
                                      str(entry.get("label") or "").lower())), None)


def _plain(text):
    """``text`` lower-cased without spaces and hyphens: "Claude Code", "claude-code" and "claudecode" are one."""
    return "".join(ch for ch in text.lower() if ch not in " -_\t")


def _harness_id(topics, value):
    """The Harness id a call's ``harness`` names: an id, or a Harness's display name (``spec.title``, "Claude Code")
    compared without case, spaces and hyphens. A value naming none comes back lower-cased, for the refusal."""
    lowered = value.lower()
    if lowered in NAMES or not lowered:
        return lowered
    plain = _plain(value)
    for name, app in topics.apps.items():
        if plain in (_plain(name), _plain(app.spec.title)):
            return name
    return lowered


def resolve(topics, state_dir, chat, defaults, args=None, choices=None):
    """The Harness, model and budget a topic the butler opens runs with, each field on its own (REVIEW 11-06:
    a Harness alone in the settings had dropped the last launch's model): the call's value, else the settings'
    default (core/butler_settings.py TopicDefaults; its model and budget only for its Harness), else the
    chat's last 开始任务 (its model and budget only for its Harness, a budget only one the model offers), else the
    Bridge's own default. A dict, ``sources`` saying where each came from (CALL, SETTINGS, LAST or BRIDGE); a str
    saying why not. ``choices``: available_models, when the caller has it."""
    args = args or {}
    last = last_launch(state_dir, chat)
    own = defaults.harness.lower()
    sources, name = {}, ""
    for value, source in ((_harness_id(topics, arg_text(args.get("harness"))), CALL), (own, SETTINGS),
                          (last[0] if last else "", LAST), (topics.default, BRIDGE)):
        if value:
            name, sources["harness"] = value, source
            break
    if name not in NAMES or name not in topics.apps:
        known = "、".join(topics.apps)
        return (f"没有 Harness「{name}」，可选：{known}。" if name not in NAMES
                else f"本机器人没有启用 {name}，可选：{known}。")
    app = topics.apps[name]
    base = app.models.configured_context(app.harness.context)
    choices = available_models(topics, base) if choices is None else choices
    listed = {key: entry for key, entry in choices.items() if entry.get("harness", key.partition(":")[0]) == name}
    follows = last is not None and last[0] == name
    own = own == name  # The settings' model and budget belong to the settings' Harness.
    model = key = ""
    for value, source in ((arg_text(args.get("model")), CALL), (defaults.model if own else "", SETTINGS)):
        if not value:
            continue
        key = _named(choices, listed, name, value) or ""
        if not key and listed:
            names = "、".join(k.partition(":")[2] for k in list(listed)[:12])
            return f"{app.spec.title} 没有模型「{value}」，可选：{names}。"
        model, sources["model"] = (key.partition(":")[2] if key else value), source
        break
    if not model and follows:
        found = find_choice(choices, last[1])  # A model gone from the menu since: the Bridge's.
        if found and found[0] in listed:
            key = found[0]
            model, sources["model"] = key.partition(":")[2], LAST
    if not model:
        model, sources["model"] = base.model, BRIDGE
    key = key or (f"{name}:{model}" if f"{name}:{model}" in choices else "")
    choice = choices.get(key)
    levels = (choice or {}).get("levels") or []
    asked = arg_text(args.get("effort"))
    if asked and asked != "default" and levels and asked not in levels:
        return f"{model or '这个模型'} 没有思考预算「{asked}」，可选：{'、'.join(levels)}。"

    def offered(option):
        """A budget the launch card offers the model (core/menu_choice.py effort_options): a level, 推荐, 默认."""
        return not levels or option in levels or option == "default" or (option == RECOMMENDED and bool(choice))

    effort = ""
    for value, source in ((asked, CALL), (defaults.effort if own else "", SETTINGS),
                          (last[2] if follows else "", LAST)):
        if value and offered(value):
            effort, sources["effort"] = value, source
            break
    else:
        effort, sources["effort"] = base.effort, BRIDGE
    # "default" is the model's own default budget, 推荐 the launch card's recommended one.
    effort = "" if effort == "default" else budget_effort(choice, effort) if effort == RECOMMENDED else effort
    label = str((choice or {}).get("display_name") or model)
    # As 开始任务 does: a budget-preset model runs as the preset of its budget. One with budget presets only has no plain
    # preset, so a call that names no budget gets the card's own preselection.
    if choice:
        if not asked and choice.get("presets") is not None and effort not in choice["presets"]:
            effort = budget_effort(choice, RECOMMENDED)
        try:
            model, effort = run_choice(choice, effort)
        except ValueError as exc:
            return f"{model}：{exc}。"
    # The launch card offers only its menu: a model not on it preselects the Harness's first entry there.
    return {"harness": name, "app": app, "base": base, "model": model, "label": label,
            "model_key": key or next(iter(listed), ""), "effort": effort, "sources": sources}
