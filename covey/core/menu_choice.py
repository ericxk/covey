"""A model menu's entries and the choices on them: the pure functions the launch card, 换配置, the 设置 card's pages and
代答 share (cards/ draws them, app/ records the clicks).

``choices`` is app/launch_models.py available_models: route key (``<harness>:<model>``) -> menu entry, in menu order
(the chat's latest first). An entry names its Harness (``harness``, else its key's), provider (``provider``, '' for an
entry from before the field, named 自带), ``display_name``, budgets (``levels``, ``recommended``, a budget-preset model's
``presets``) and work modes (``work_modes``).
"""
from __future__ import annotations

from covey.core.defaults import EFFORT_ORDER, RECOMMENDED, WORK_MODES, recommended_effort, stored_work_mode
from covey.core.providers import provider_of

AUTO_EFFORT_LABEL = '模型自动管理（无可选预算）'
NATIVE = 'default'  # Claude Code's catalog entry of the model it runs without --model (default_entry).


def harness_of(key, entry):
    """A menu entry's Harness: its own field, else its route key's (an entry from before that field)."""
    return entry.get('harness') or key.partition(':')[0]


def tiers(choices, harness, provider=None):
    """What the Harness → 供应商 → 模型 rows offer for ``harness``: (harnesses, providers, models), each value → name
    in menu order (the chat's latest first): every Harness, ``harness``'s providers and its models of ``provider``
    (all of them when None). The launch and 换配置 cards' rows and the 设置 card's 私聊默认, 新话题默认 and 代答 rows
    come from here (cards/launch_config.py tier_rows). An entry from before the provider fields has none,
    named 自带."""
    harnesses = {harness_of(key, entry): entry.get('title') or harness_of(key, entry) for key, entry in choices.items()}
    providers = {entry.get('provider', ''): entry.get('provider_title') or entry.get('provider') or '自带'
                 for key, entry in choices.items() if harness_of(key, entry) == harness}
    models = {key: entry.get('display_name') or entry.get('label') or key for key, entry in choices.items()
              if harness_of(key, entry) == harness and (provider is None or entry.get('provider', '') == provider)}
    return harnesses, providers, models


def model_tiers(choices, model):
    """What the Harness → 供应商 → 模型 selects of the launch and 换配置 cards offer around ``model``: (harness, provider,
    harnesses, providers, models), as ``tiers`` for its Harness and provider."""
    choice = choices.get(model, {})
    harness, provider = harness_of(model, choice), choice.get('provider', '')
    return (harness, provider, *tiers(choices, harness, provider))


def tier_pick(choices, selected, harness=None, provider=None, prefer=None):
    """The model a Harness or provider choice selects among that Harness's models, or the selected one's Harness's
    models of that provider (``harness`` given too: that Harness's); None when there is none. The launch card keeps
    ``selected`` where it is among them (None) and else takes the first in menu order. 换配置 gives ``prefer``, the
    topic's own model: that one where it is among them, else the first; None when that is ``selected``."""
    current = harness_of(selected, choices.get(selected, {}))
    pool = [key for key, entry in choices.items()
            if harness_of(key, entry) == (current if harness is None else harness)
            and (provider is None or entry.get('provider', '') == provider)]
    if not pool:
        return None
    if prefer is None:
        return None if selected in pool else pool[0]
    key = prefer if prefer in pool else pool[0]
    return None if key == selected else key


def parse_budget(option, revision, choice):
    """The budget option (effort_options) a 思考预算 row's ``option`` chose: the rows' values are
    ``<revision>:<option>`` (model_settings), so a tap on a card drawn before the menu entry's revision ``revision``
    is None, as is an option the entry ``choice`` no longer offers."""
    prefix = f'{revision}:'
    if not option.startswith(prefix):
        return None
    option = option[len(prefix):]
    return option if option in dict(effort_options(choice)[0]) else None


def effort_options(choice):
    levels = choice.get('levels', [])
    if not levels:
        return [('default', AUTO_EFFORT_LABEL)], 'default'
    try:
        recommended = choice.get('recommended') or recommended_effort(levels)
    except ValueError:
        recommended = ''
    options = [(RECOMMENDED, '推荐 · ' + recommended)] if recommended else []
    ordered = sorted(levels, key=lambda level: EFFORT_ORDER.index(level) if level in EFFORT_ORDER else -1,
                     reverse=True)
    options += [(level, level) for level in ordered]
    # A budget-preset model's native default is its plain preset; a model with budget presets only has none.
    options += [('default', '模型原生默认（不指定）')] if '' in choice.get('presets', {'': ''}) else []
    if recommended:
        return options, RECOMMENDED
    # A model with a single budget (a budget preset runs only its own) needs no choice.
    return options, levels[0] if len(levels) == 1 else ''


def budget_options(choice):
    """The 思考预算 row's (options, default): effort_options as the settings lists name them (推荐 high)."""
    options, default = effort_options(choice)
    return [(value, label.replace('推荐 · ', '推荐 ')) for value, label in options], default


def effort_text(choice, selected):
    """How the summary names the budget ``selected`` (an effort_options value)."""
    if not choice.get('levels'):
        return '模型自动管理'
    options, default = budget_options(choice)
    labels = dict(options)
    return labels.get(selected if selected in labels else default, '未选')


def work_modes(choice, selected, *, known=True):
    """(modes, mode): the work modes of the menu entry ``choice`` (``known``: only those core/defaults.py WORK_MODES
    names) and the one ``selected`` keeps, else the first ('' for a model without modes)."""
    modes = [mode for mode in choice.get('work_modes') or () if not known or mode in WORK_MODES]
    return modes, selected if selected in modes else modes[0] if modes else ''


def chosen_work_mode(choice, selected):
    """The work mode a launch or switch binds for a menu entry: the card's choice ('' its first) where the
    model has modes, stored as core/defaults.stored_work_mode keeps it; '' for a model without modes."""
    modes = choice.get('work_modes') or ()
    if not modes:
        return ''
    mode = selected or modes[0]
    if mode not in modes:
        raise ValueError('该模型不支持此工作模式')
    return stored_work_mode(mode)


def budget_effort(choice, option):
    """The effort a budget option of the launch card's menu runs with ('' the model's own default)."""
    if option == RECOMMENDED:
        return choice.get('recommended') or recommended_effort(choice['levels'])
    return '' if option == 'default' else option


def preset_choice(choices, harness, model):
    """(key, budget) of the menu entry that runs ``harness``'s preset ``model``, 'default' for the plain
    preset, or None. A budget preset saved before one entry per model, or bound to a topic, is found this way."""
    for key, entry in choices.items():
        if entry.get('harness') == harness:
            budget = next((b for b, preset in (entry.get('presets') or {}).items() if preset == model), None)
            if budget is not None:
                return key, budget or 'default'
    return None


def find_choice(choices, key):
    """(key, budget) of a route key from a saved default, a draft or an older card: a menu entry, with the
    card's own default budget (''), or a budget preset with its budget; None when neither is on the menu."""
    if not isinstance(key, str):
        return None
    if key in choices:
        return key, ''
    harness, _, model = key.partition(':')
    return preset_choice(choices, harness, model)


def entry_of(choices, harness, model):
    """The menu entry of ``harness``'s ``model``, else None (none chosen, or gone from the catalog)."""
    return choices.get(f'{harness}:{model}') if harness and model else None


def default_entry(choices, harness):
    """The menu entry of the model ``harness`` runs with none chosen: the butler's topic.json then has no model
    and Claude Code runs without --model (backends/claude/argv.py), its catalog's NATIVE entry, "Default
    (recommended)"; else the one its catalog names recommended; None when there is neither."""
    entry = entry_of(choices, harness, NATIVE)
    return entry if entry is not None else next(
        (entry for key, entry in choices.items() if harness_of(key, entry) == harness
         and 'recommended' in str(entry.get('display_name') or '').lower()), None)


def provider_key(choices, harness, model):
    """The provider of ``harness``'s ``model``: its menu entry's, else the one its id's prefix names
    (core/providers.py provider_of: acme/gpt-6-astra is Acme's), so a saved model id says its provider."""
    entry = entry_of(choices, harness, model)
    if entry is not None:
        return entry.get('provider', '')
    found = provider_of(harness, model)
    return found.key if found else ''


def model_provider(harness, model, choices):
    """The 供应商 row's choice: ``model``'s provider; with no model, the Harness's one provider, or '' (as the last
    launch, the Bridge's or Claude Code's default) when it has several."""
    providers = tiers(choices, harness)[1]
    if model:
        return provider_key(choices, harness, model)
    return next(iter(providers)) if len(providers) == 1 else ''


def redacts(choices, harness):
    """Whether a model of ``harness`` offers 敏感词脱敏 (app/launch_models.py: a Harness that can redact)."""
    return any(entry.get('redaction') for key, entry in choices.items() if harness_of(key, entry) == harness)


def models_of(choices, harness, provider=None):
    """{model id: name} of ``harness``'s models (of ``provider``, all when None) as the launch card's 模型 row lists
    them (tiers): the same filter, order and names."""
    return {key.partition(':')[2]: name for key, name in tiers(choices, harness, provider)[2].items()}
