"""What the launch card and the 换配置 card preselect when they are drawn: pure functions of what the callers read.

launch_selection is app/launch.py build_launch_card's order of preference (the binding, the chat's last launch,
its latest selection, the card's own draft, then what the menu still offers; a budget the 设置卡's 新话题默认 names
comes before the chat's usual one); switch_selection is
app/harness_switch.py HarnessSwitch.open's (the topic's binding, what a redacting topic may keep, the chat's
usual budgets). The callers read the catalog, the chat's selections, the saved launch and the workspace list,
and write the form record and the card from the result. The two stay apart: the cards differ on purpose, and
their clicks (launch.py, harness_switch.py) are reduced separately.
"""
from __future__ import annotations

from typing import NamedTuple

from covey.app import model_usage
from covey.core.menu_choice import effort_options, find_choice, preset_choice, work_modes
from covey.core.defaults import RECOMMENDED
from covey.core.redaction import TOPIC_KEY as REDACTION_KEY


class LaunchSelection(NamedTuple):
    choices: dict  # The menu in the selects' order: the preselected model first, then the chat's recent ones.
    model: str  # Its menu entry ('unavailable' on an empty menu).
    effort: str  # Its budget option.
    work_mode: str
    workspace: str
    names: list  # The workspace select's options, most recently used first.
    notice: str


def launch_selection(choices, usage, config, *, model, workspace, work_mode, fresh, saved=None, draft=None,
                     notice='', topic_budget=('', '')):
    """The launch card's preselection. ``choices`` available_models' menu, ``usage`` the chat's selections
    (model_usage.recent), ``config`` the workspace config; ``model`` (harness:model), ``workspace`` and
    ``work_mode`` the topic's binding; ``fresh`` whether it is the topic's first binding, the only one
    preferences prefill, with ``saved`` the chat's last launch (launch_defaults; anything but a dict is
    none); ``draft`` a rebuilt card's own choices. ``topic_budget`` (Harness, budget): the 设置卡's 新话题默认
    budget (core/main_thread_settings.py TopicDefaults; its Harness '' for any), ('', '') without one."""
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
            workspace = next((name for name, path in config['workspaces'].items()
                              if path == saved.get('workdir')), workspace)
        latest = None if first else model_usage.last_selection(choices, usage)
        if latest:  # No saved launch: the latest selection.
            (model, budget, mode), first = latest, latest[0]
    # The preselected model first, then the ones the chat chose most lately; a rebuilt card keeps this order.
    choices = model_usage.ranked(choices, usage, first)
    if workspace not in config['workspaces']:
        workspace = config['default']
    draft = draft or {}
    found = find_choice(choices, draft.get('model'))
    if found:
        model, budget = found[0], found[1] or draft.get('effort') or ''
    elif draft.get('effort'):
        budget = draft['effort']
    mode = draft.get('work_mode') or mode
    if model not in choices:
        model = next(iter(choices), 'unavailable')
    names = sorted(config['workspaces'], key=lambda name: -config.get('recent', {}).get(name, 0))
    if draft.get('workspace') in config['workspaces']:
        workspace = draft['workspace']
    elif isinstance(draft.get('workspace'), str) and draft['workspace']:
        # Chosen on a card sent before its removal: it stays chosen, so 开始任务 reports it instead of
        # starting elsewhere, and the card, redrawn with it, says so.
        workspace, names = draft['workspace'], [*names, draft['workspace']]
        notice = '\n'.join(filter(None, [notice, f'⚠️ 工作区「{workspace}」已从列表移除，请换一个工作区。']))
    options, default_effort = effort_options(choices.get(model, {}))
    # A budget preset's or the card's budget; else the 新话题默认 budget where its Harness is the model's and the
    # model offers it (owner 2026-11-10: the card follows it, so a lowered default reaches chats that always took
    # the recommended one); else the one the chat chose most often with the model; else, for the last launch's
    # model, that launch's.
    harness, default_budget = topic_budget
    preset = default_budget if default_budget in dict(options) and harness in ('', model.split(':', 1)[0]) else ''
    effort = (budget or preset or model_usage.usual_effort(usage, model, dict(options))
              or (last[1] if last[0] == model else '') or RECOMMENDED)
    if effort not in dict(options):
        effort = default_effort
    mode = work_modes(choices.get(model, {}), mode, known=False)[1]
    return LaunchSelection(choices, model, effort, mode, workspace, names, notice)


class SwitchSelection(NamedTuple):
    choices: dict  # The menu in the selects' order: the topic's model first, then the chat's recent ones.
    key: str  # The topic's menu entry (its route key when the menu has none).
    selected: str  # The preselected menu entry.
    effort: str  # Its budget option.
    usual: dict  # Menu entry → the budget option the chat usually gives it, where it has one.
    redacting: bool
    workspace: str
    names: list  # The workspace select's options, the topic's first when the config no longer lists it.


def switch_selection(choices, usage, topic, names):
    """The 换配置 card's preselection. ``choices`` available_models' menu (not empty), ``usage`` the chat's
    selections, ``topic`` the topic's record, ``names`` the configured workspaces, most recently used first."""
    model = str(topic.get("model") or "")
    # A topic bound to a budget preset shows its model, with that preset's budget.
    key, budget = preset_choice(choices, topic["harness_id"], model) or (
        topic["harness_id"] + ":" + model, topic.get("effort") or "")
    redacting = topic.get(REDACTION_KEY) is True
    # The topic's model first, then the ones the chat chose most lately (app/model_usage.py); a model
    # chosen here starts from the budget the chat usually gives it.
    choices = model_usage.ranked(choices, usage, key)
    usual = {name: model_usage.usual_effort(usage, name, dict(effort_options(entry)[0])) for name, entry in choices.items()}
    usual = {name: value for name, value in usual.items() if value}
    # A redacting topic preselects a model that keeps redacting (switch_topic refuses any other).
    offered = [name for name, entry in choices.items() if entry.get("redaction") or not redacting]
    selected = key if key in offered else next(iter(offered), next(iter(choices)))
    options, default_effort = effort_options(choices[selected])
    effort = budget if selected == key and budget in dict(options) else usual.get(selected) or default_effort
    workspace = topic.get("workspace_name") or str(topic.get("workdir") or "")
    names = names if workspace in names else [workspace, *names]
    return SwitchSelection(choices, key, selected, effort, usual, redacting, workspace, names)
