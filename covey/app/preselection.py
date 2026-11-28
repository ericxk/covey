"""What the launch card and the 换配置 card preselect when they are drawn: pure functions of what the callers read.

launch_selection is app/launch.py build_launch_card's order of preference (the binding, the chat's last launch,
its latest selection, then what the 设置 card's 新话题 page names, the card's own draft, then what the menu still
offers; a budget the 新话题 page names comes before the chat's usual one); switch_selection is
app/harness_switch.py HarnessSwitch.open's (the topic's binding, the chat's
usual budgets). The callers read the catalog, the chat's selections, the saved launch and the workspace list,
and write the form record and the card from the result. The two stay apart: the cards differ on purpose, and
their clicks (launch.py, harness_switch.py) are reduced separately.
"""
from __future__ import annotations

from typing import NamedTuple

from covey.app import model_usage
from covey.core.menu_choice import (NATIVE, default_entry, effort_options, explicit, find_choice, harness_of,
                                    named_budget, preset_choice, work_modes)
from covey.core.defaults import RECOMMENDED


class LaunchSelection(NamedTuple):
    choices: dict  # The menu in the selects' order: the preselected model first, then the chat's recent ones.
    model: str  # Its menu entry ('unavailable' on an empty menu).
    effort: str  # Its budget option.
    work_mode: str
    device: str  # The 执行设备 row's choice: a devices.json alias, '' for 本机.
    notice: str


def device_fit(choices, model, declared):
    """The menu entry a device workspace starts from: ``model`` itself when its Harness is one the device declares
    (devices.json ``harnesses``; ``declared`` empty or None: no device, no limit), else the first model, in the menu's
    order, of the first declared Harness the menu offers (the hub's enabled ones only); ``model`` again when the device
    declares none the menu has, which 开始任务 then refuses with the reason."""
    if not declared or choices.get(model, {}).get('harness', model.split(':', 1)[0]) in declared:
        return model
    for name in declared:
        for key, entry in choices.items():
            if entry.get('harness', key.split(':', 1)[0]) == name:
                return key
    return model


def harness_model(choices, harness, model, current):
    """(key, budget) of the menu entry the 新话题 page's ``harness`` and ``model`` name: that model; with the Harness
    alone, ``current`` (the last launch's) where it is that Harness's, else its default entry (core/menu_choice.py
    default_entry), else its first on the menu. None with no Harness named, or none of it on the menu. With the
    Harness alone it is never Claude Code's "Default (recommended)" (menu_choice.explicit), which says no model: a
    chat's first launch card had preselected it, and its topics read "Default (recommended)" (T698, T718). Still on
    the menu, for an owner who picks it."""
    if not harness:
        return None
    if model:
        found = find_choice(choices, f'{harness}:{model}')
        if found:
            return found
    named = explicit(choices)
    if current in named and harness_of(current, named[current]) == harness:
        return current, ''
    entry = default_entry(named, harness)
    keys = ([key for key, value in named.items() if harness_of(key, value) == harness]
            or [key for key, value in choices.items() if harness_of(key, value) == harness])
    key = next((key for key in keys if choices[key] is entry), keys[0] if keys else None)
    return (key, '') if key else None


def launch_selection(choices, usage, *, model, work_mode, fresh, saved=None, draft=None, notice='',
                     topic_budget=('', ''), device_harnesses=None, topic_model=('', '')):
    """The launch card's preselection (no workspace or run mode to choose: a topic starts in the home). ``choices``
    available_models' menu, ``usage`` the chat's selections (model_usage.recent); ``model`` (harness:model) and
    ``work_mode`` the topic's binding; ``fresh`` whether it is the topic's first binding, the only one
    preferences prefill, with ``saved`` the chat's last launch (launch_defaults; anything but a dict is
    none); ``draft`` a rebuilt card's own choices (its ``device``: the 执行设备 row's alias). ``topic_budget`` (Harness, budget):
    the 设置卡's 新话题默认 budget (core/butler_settings.py TopicDefaults; its Harness '' for any), ('', '') without one.
    ``topic_model`` (Harness, model): what that page names, '' for 跟上次一样 (the page preselects every new topic): a fresh
    card starts from its model (with a Harness alone, the last launch's model where it is that Harness's, else the
    Harness's default one, ``harness_model``). ``device_harnesses`` (device alias → the Harnesses it declares, devices on
    only): a card that nobody has chosen a model on (no ``draft`` model) whose chosen device is such a one does not start
    from a Harness the device lacks but from its first declared one, with that model's own budget (``device_fit``); a
    model the owner chose stays and is refused at 开始任务."""
    # A budget comes with the model only from a budget preset key, saved or sent before one entry per model.
    model, budget = find_choice(choices, model) or (model, '')
    mode, first, last = work_mode, None, ('', '')
    # Preferences only prefill a form. Rendering never changes a topic binding.
    if fresh:
        if isinstance(saved, dict):
            found = find_choice(choices, saved.get('model_choice'))
            if found:
                # The chat's last launch. Its budget is the model's only while the chat has no recorded
                # selection of it (the usual budget, below); one the model doesn't offer falls back below.
                (model, budget), first = found, found[0]
                last = (model, saved['effort'] if isinstance(saved.get('effort'), str) else '')
                mode = saved.get('work_mode') if isinstance(saved.get('work_mode'), str) else ''
        latest = None if first else model_usage.last_selection(choices, usage)
        if latest:  # No saved launch: the latest selection.
            (model, budget, mode), first = latest, latest[0]
        named = harness_model(choices, *topic_model, model)
        if named and named[0] != model:  # The 新话题 page's model over the last one.
            (model, budget), first, mode = named, named[0], ''
    # The preselected model first, then the ones the chat chose most lately; a rebuilt card keeps this order.
    choices = model_usage.ranked(choices, usage, first)
    draft = draft or {}
    found = find_choice(choices, draft.get('model'))
    if found:
        model, budget = found[0], found[1] or draft.get('effort') or ''
    elif draft.get('effort'):
        budget = draft['effort']
    mode = draft.get('work_mode') or mode
    if model not in choices:
        model = next(iter(choices), 'unavailable')
    device = draft.get('device') if isinstance(draft.get('device'), str) and draft['device'] in (device_harnesses or {}) else ''
    if not draft.get('model'):
        fitted = device_fit(choices, model, (device_harnesses or {}).get(device))
        if fitted != model:
            model, budget = fitted, ''
            choices = model_usage.ranked(choices, usage, model)
    if not found and model.partition(':')[2] == NATIVE:
        # Claude Code's "Default (recommended)" names no model: the card lists it no more (docs/ui.md 13 U5), so a last
        # launch or the chat's latest selection of it starts from its Harness's model that names itself.
        named = harness_model(choices, harness_of(model, choices.get(model, {})), '', '')
        if named and named[0] != model:
            last = (named[0], last[1]) if last[0] == model else last  # The budget chosen with it comes along.
            model, budget = named[0], ''
            choices = model_usage.ranked(choices, usage, model)
    options, default_effort = effort_options(choices.get(model, {}))
    # A budget preset's or the card's budget; else the 新话题默认 budget where its Harness is the model's and the
    # model offers it (the card follows it, so a lowered default reaches chats that always took
    # the recommended one); else the one the chat chose most often with the model; else, for the last launch's
    # model, that launch's.
    harness, default_budget = topic_budget
    preset = default_budget if default_budget in dict(options) and harness in ('', model.split(':', 1)[0]) else ''
    effort = (budget or preset or model_usage.usual_effort(usage, model, dict(options))
              or (last[1] if last[0] == model else '') or RECOMMENDED)
    if effort not in dict(options):
        effort = default_effort
    # By the level it runs: 推荐 and 模型原生默认 are on the card no more (docs/ui.md 13 U5); one the card's own draft has
    # (tapped on a card sent before) stays as it was.
    if not (effort == draft.get('effort') and effort in dict(options)):
        effort = named_budget(choices.get(model, {}), effort)
    mode = work_modes(choices.get(model, {}), mode, known=False)[1]
    return LaunchSelection(choices, model, effort, mode, device, notice)


class SwitchSelection(NamedTuple):
    choices: dict  # The menu in the selects' order: the topic's model first, then the chat's recent ones.
    key: str  # The topic's menu entry (its route key when the menu has none).
    selected: str  # The preselected menu entry.
    effort: str  # Its budget option.
    usual: dict  # Menu entry → the budget option the chat usually gives it, where it has one.
    workspace: str
    names: list  # The workspace select's options, the topic's first when the config no longer lists it.


def switch_selection(choices, usage, topic, names):
    """The 换配置 card's preselection. ``choices`` available_models' menu (not empty), ``usage`` the chat's
    selections, ``topic`` the topic's record, ``names`` the configured workspaces, most recently used first."""
    model = str(topic.get("model") or "")
    # A topic bound to a budget preset shows its model, with that preset's budget.
    key, budget = preset_choice(choices, topic["harness_id"], model) or (
        topic["harness_id"] + ":" + model, topic.get("effort") or "")
    # The topic's model first, then the ones the chat chose most lately (app/model_usage.py); a model
    # chosen here starts from the budget the chat usually gives it.
    choices = model_usage.ranked(choices, usage, key)
    usual = {name: model_usage.usual_effort(usage, name, dict(effort_options(entry)[0])) for name, entry in choices.items()}
    usual = {name: named_budget(choices[name], value) for name, value in usual.items() if value}
    selected = key if key in choices else next(iter(choices))
    options, default_effort = effort_options(choices[selected])
    effort = budget if selected == key and budget in dict(options) else usual.get(selected) or default_effort
    # By the level it runs (docs/ui.md 13 U5); the topic's own budget as it is bound.
    if not (selected == key and effort == budget):
        effort = named_budget(choices[selected], effort)
    workspace = topic.get("workspace_name") or str(topic.get("workdir") or "")
    names = names if workspace in names else [workspace, *names]
    return SwitchSelection(choices, key, selected, effort, usual, workspace, names)
