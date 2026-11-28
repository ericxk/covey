"""The launch card: its form and the model-scoped reasoning choices on it, and the settings list it shares
with the 换配置 card (cards/switch_form.py); the 设置 card's 私聊默认, 新话题默认 and 代答 tabs draw their Harness,
供应商, 模型 and 思考预算 rows with the same rows (harness_setting, tier_rows).

The look: the chosen model large with its Harness and
provider as tags and the rest of the choice in one grey line; the settings folded under 调整模型、预算和工作区
as a list, a grey label left of the choice; then 其他目录… and 开始任务. Each choice redraws the card
(app/model_picker.py) unfolded, so the summary says what 开始任务 would start.

No select: on iOS Feishu a select opens a full-screen picker
whose 取消 lies under the status bar, so the only way out was the option already ticked, and the options fill
the top of an empty page. A row is a fold whose options, one per row and the current one ✓, open under it in
the client; a tap on an option records it as the select did and redraws the card. The first form of this
(84caa8a) turned the card into a row's options page, a redraw each way: every change waited for two round
trips of about a second each.
"""
from __future__ import annotations

from typing import NamedTuple

from covey.cards.elements import CARD_BYTES, CARD_NODES, fits, fold, grey, md, note, panel_header, styled_panel
from covey.core.defaults import WORK_MODES
from covey.core.menu_choice import (budget_options, effort_text, explicit, model_provider, model_tiers, models_of,
                                    offered_models, named_budget, plain_budgets, tiers, work_modes)
from covey.core.workspaces import RUN_MODES

MODEL_ACTION = 'bridge_launch_model'
# The Harness and provider selects above the model's.
HARNESS_ACTION = 'bridge_launch_harness'
PROVIDER_ACTION = 'bridge_launch_provider'
BUDGET_ACTION = 'bridge_launch_budget'
WORKSPACE_ACTION = 'bridge_launch_workspace'
MODE_ACTION = 'bridge_launch_mode'
DEVICE_ACTION = 'bridge_launch_device'  # The 执行设备 row (COVEY_DEVICES=1 with devices): an alias, '' for 本机.
WORK_MODE_ACTION = 'bridge_launch_work_mode'
# The last workspace-menu entry opens the workspace manager instead of choosing one.
MANAGE_OPTION = '__bridge_manage_workspaces__'
SUBMIT_PREFIX = 'bridge_launch_'
PICKER_PREFIX = 'bridge_pick_'
ADJUST_TITLE = '调整模型、预算和工作区'
DEFAULT = '默认'  # An empty choice of tier_rows' rows without a ``follows`` text.
# A row's tap on a card sent with options pages (an older release, before the fold rows): its ``page``, or
# "" from a page's ‹ 返回. Now a redraw with that row open, or none open; the rows open in the client.
PAGE_ACTION = 'bridge_launch_page'
PAGES = frozenset({'', 'harness', 'provider', 'model', 'effort', 'work_mode', 'workspace', 'run_mode', 'device'})
LOCAL = '本机'  # The 执行设备 row's own option (its value is '').
HOME_ADJUST_TITLE = '调整模型和预算'


class Setting(NamedTuple):
    """A settings-list row and the options under it. ``key`` names the row (and the select it replaced),
    ``kind`` is what a choice calls back, with the option as the select's callback had it; ``options`` are
    (value, text) pairs, ``current`` the chosen value."""
    key: str
    label: str
    kind: str
    current: str
    options: list

    @property
    def text(self):
        return dict(self.options).get(self.current, '未选')


def run_mode_setting(key, kind, current):
    """A 运行方式 row: 原目录 or 独立 worktree (core/workspaces.py RUN_MODES)."""
    return Setting(key, '运行方式', kind, current, list(RUN_MODES.items()))


def workspace_setting(key, kind, current, names, *, first=(), last=(), labels=None):
    """A 工作区 row: the saved workspaces ``names``, each its own value, between the card's own ``first`` and ``last``
    options. ``labels``: the text of a name that is not its own (a device's workspace: 名字 · 设备标题)."""
    labels = labels or {}
    return Setting(key, '工作区', kind, current, [*first, *((name, labels.get(name, name)) for name in names), *last])


def tap(value, elements, padding, border=False):
    """A tappable container around ``elements``; a tap calls back ``value``."""
    return {'tag': 'interactive_container', 'width': 'fill', 'padding': padding, 'corner_radius': '6px',
            **({'has_border': True, 'border_color': 'grey'} if border else {}), 'behaviors': [{'type': 'callback', 'value': value}], 'elements': elements}


def setting_row(setting, *, token, open_key='', changed=False, stamp='', value=None, cell=False):
    """A settings-list row: a fold whose title is the label, grey (blue and bold when ``changed``), then the
    choice, and whose options open under it in the client (``open_key``: the row drawn open). Its element ID
    takes ``stamp`` (else the card's token, new on each launch card): the client keeps a fold open or closed
    across a redraw of the same ID (the web client kept a row open after its choice), so a choice's
    redraw closes the row. An option is
    one tappable line, two card nodes (a card holds 200), the current one blue and bold after ✓, the others
    after a full-width space so the names line up. ``cell``: a row of a line (setting_line) as a small unit, its title the label
    in grey then the choice on one line. ``value``: more fields of each option's callback (the 代答 tab's
    revision)."""
    label = f"**<font color='blue'>{setting.label}</font>**" if changed else grey(setting.label)
    lines = [tap({'kind': setting.kind, 'token': token, **(value or {}), 'option': option},
                 [md(f"✓ **<font color='blue'>{text}</font>**" if option == setting.current else '\u3000 ' + text)],
                 '7px 4px 7px 4px')
             for option, text in setting.options]
    options = {'tag': 'column_set', 'flex_mode': 'none', 'columns': [
        {'tag': 'column', 'width': 'weighted', 'weight': 1, 'background_style': 'bg-white',
         'padding': '2px 8px 2px 8px', 'vertical_spacing': '0px', 'elements': lines}]}
    title = md(f'{label}　{setting.text}')
    if cell:  # A tile: its own frame, the label then the choice on one line, the chevron at its right edge.
        return fold(f'set_{setting.key}_{stamp or token[:4]}', title, [options], expanded=setting.key == open_key,
                    padding='0px 4px 4px 4px', spacing='0px', title_padding='3px 2px 3px 8px', border=TILE_BORDER)
    return fold(f'set_{setting.key}_{stamp or token[:4]}', title, [options],
                expanded=setting.key == open_key, padding='0px 0px 6px 0px', spacing='0px',
                title_padding='7px 0px 7px 0px')


# A line of settings (setting_line): at most two tiles (setting_row ``cell``) side by side, the label then the choice on one line, each
# still a fold whose options open under its tile. 'stretch': two columns of equal width where the card is wide, one
# under the other where it is narrow (the 话题侧栏, a phone), so a choice is never cut into "Claude 自 / 带" and the tiles'
# edges and chevrons line up from one line to the next; a setting whose words are long (``wide`` in cards/butler_panel.py)
# is a line of its own, as wide as the card. ('bisect' kept two columns at any width; 'flow' with auto widths left the
# tiles ragged.)
LINE_FLEX = 'stretch'
TILE_BORDER = {'color': 'grey', 'corner_radius': '8px'}


def setting_line(settings, *, token, stamp='', value=None, full=False):
    """``settings`` as one line of tiles (setting_row ``cell``) in a column_set; ``full``: one tile as wide as the card
    (a long choice), else a tile alone keeps its half, the other column empty, so the tiles stay on one grid."""
    cells = [setting_row(s, token=token, stamp=stamp, value=value, cell=True) for s in settings]
    if full and len(cells) == 1:
        return cells[0]
    columns = [{'tag': 'column', 'width': 'weighted', 'weight': 1, 'vertical_align': 'top', 'elements': [cell]}
               for cell in cells]
    if len(columns) == 1:
        columns.append({'tag': 'column', 'width': 'weighted', 'weight': 1, 'elements': []})
    return {'tag': 'column_set', 'flex_mode': LINE_FLEX, 'horizontal_spacing': '8px', 'columns': columns}


def model_settings(choices, model, *, kinds, revision, effort, work_mode='', limit=None, keep=()):
    """The Harness, 供应商 and 模型 settings (model_tiers) around ``model``, its 思考预算 (values as the budget
    select's, ``revision``:value) and, for a model with work modes, 工作模式; ``kinds`` the five callback
    kinds. ``limit``: the most models the 模型 row lists (fitted), ``model`` and those of ``keep`` among them all
    the same; None, every one."""
    # Only what says what it runs (docs/ui.md 13 U5): no "Default (recommended)" (but the selected one), no 推荐 or
    # 模型原生默认; a budget saved as one of them is drawn as the level it runs.
    harness, provider, harnesses, providers, models = model_tiers(offered_models(choices, model), model)
    choice = choices.get(model, {})
    options, every = plain_budgets(choice), dict(budget_options(choice)[0])
    if effort in every and effort not in dict(options):
        options = [*options, (effort, every[effort])]  # Chosen before (a topic's binding, a card sent before): as it was.
    elif effort not in every:
        effort = named_budget(choice, effort)
    settings = [Setting('harness', 'Harness', kinds[0], harness, list(harnesses.items())),
                Setting('provider', '供应商', kinds[1], provider, list(providers.items())),
                Setting('model', '模型', kinds[2], model, shortened(list(models.items()), limit, {model, *keep})),
                Setting('effort', '思考预算', kinds[3], f'{revision}:{effort}',
                        [(f'{revision}:{value}', label) for value, label in options])]
    modes, mode = work_modes(choice, work_mode)
    if modes:
        settings.append(Setting('work_mode', '工作模式', kinds[4], mode,
                                [(mode, '{}（{}）'.format(*WORK_MODES[mode])) for mode in modes]))
    return settings


def effort_choices(entry, default=DEFAULT, empty=True):
    """The 设置 card's 思考预算 options of the model ``entry`` (a menu entry, or None): '' ``default`` first where
    ``empty``, then core/menu_choice.py plain_budgets (the levels by name, no 推荐 or 模型原生默认)."""
    return ([('', default)] if empty else []) + (plain_budgets(entry) if entry else [])


def listed(options, current, limit):
    """At most ``limit`` options, the current one kept."""
    shown = options[:limit]
    if current not in dict(shown):
        shown += [option for option in options if option[0] == current]
    return shown


# The launch and 换配置 cards' 模型 row lists every model of the chosen provider, two card nodes each (setting_row). A
# catalog too long for one card (a model gateway that serves dozens of routes as Codex's own: 62 of them took the 换配置
# card to 204 nodes, CardKit refused every redraw with 300305 and Codex could not be chosen at all) is cut from its end
# to what fits, the chosen model and the topic's own kept; the row's last line says so (SHORTENED_NOTE).
SHORTENED_NOTE = '模型太多，一张卡放不下：这里列了 {shown} 个（共 {total} 个，最近用过的在前），其余的在网页工作台里选。'


def shortened(options, limit, keep):
    """At most ``limit`` of ``options`` (None: all of them) in their order, then those of ``keep`` it left out."""
    if limit is None:
        return options
    return options[:limit] + [option for option in options[limit:] if option[0] in keep]


def model_count(choices, model):
    """How many models the 模型 row around ``model`` would list uncut (model_settings)."""
    return len(model_tiers(offered_models(choices, model), model)[4])


def cut_rows(settings, rows, total):
    """``rows``, one a setting of ``settings``, with SHORTENED_NOTE under the 模型 row's where that row lists fewer than
    ``total`` models."""
    at = next((i for i, setting in enumerate(settings) if setting.key == 'model'), None)
    if at is None or len(settings[at].options) >= total:
        return rows
    return [*rows[:at + 1], note(SHORTENED_NOTE.format(shown=len(settings[at].options), total=total)), *rows[at + 1:]]


def fitted(draw, total):
    """The card ``draw(limit)`` draws, its 模型 row listing the most of its ``total`` models that one card holds
    (cards/elements.py CARD_NODES, CARD_BYTES): ``draw(None)`` where they all fit, else ``limit`` down one at a time,
    to a single model at the least."""
    card, limit = draw(None), total
    while limit > 1 and not fits(card, CARD_NODES, CARD_BYTES):
        limit -= 1
        card = draw(limit)
    return card


def known(options, current, text):
    """``options`` with ``current`` added when it is not among them (a model gone from the catalog, a hand edit)."""
    return options if current in dict(options) else options + [(current, text)]


def harness_setting(key, kind, harness, choices, *, first=()):
    """A Harness row: every Harness on the menu (core/menu_choice.py tiers) after the card's own ``first`` options;
    ``harness`` kept where the menu no longer has it."""
    return Setting(key, 'Harness', kind, harness,
                   known([*first, *tiers(choices, harness)[0].items()], harness, harness or '未设置'))


# A row's key goes into its fold's element ID, set_<key>_r<revision>: CardKit takes 20 characters, so a key takes 10.
def tier_rows(choices, harness, model, effort, entry, *, keys, kinds, limit, follows=None, empty=True):
    """The 供应商, 模型 and 思考预算 rows of ``harness`` (chosen) for the settings that save a Harness, a model id and a
    budget: the 设置 card's 私聊默认, 新话题默认 and 代答 (``keys`` and ``kinds`` theirs, in that order). Each as the
    launch card's (core/menu_choice.py tiers), with an empty choice first where ``empty``, named by ``follows`` (row
    key -> text) or 默认. Every other choice names what it runs (T615): the 模型 row has no "Default (recommended)"
    (explicit) and the 思考预算 row no 推荐 or 模型原生默认 (effort_choices); a saved one of them is still drawn, chosen,
    by its old name until another replaces it. A Harness of several providers (Codex: its own, Acme) chooses a
    provider before a model, and its 模型 row lists that provider's models only (at most ``limit``). The 思考预算 row
    offers the budgets of ``entry`` (the model's menu entry, or what an empty 模型 runs; no row without either).
    Without ``empty`` (管家, 代答: always a model, as the launch card) a row has no empty choice, and an empty
    current choice is drawn 未选."""
    follows = follows or {}

    def blank(key):
        return [('', follows.get(key) or DEFAULT)] if empty else []

    def kept(options, current, names):
        return known(options, current, names.get(current, current)) if current or empty else options

    providers = tiers(choices, harness)[1]
    several = len(providers) > 1
    provider = model_provider(harness, model, choices)
    rows = []
    if providers or model:
        rows.append(Setting(keys[0], '供应商', kinds[0], provider, kept(
            (blank(keys[0]) if several or not providers else []) + list(providers.items()), provider, {})))
    if model or not several:
        shown = provider if several else None
        rows.append(Setting(keys[1], '模型', kinds[1], model, listed(kept(
            ([] if several else blank(keys[1])) + list(models_of(explicit(choices), harness, shown).items()),
            model, models_of(choices, harness, shown)), model, limit)))
    if model or entry:
        budgets = effort_choices(entry, follows.get(keys[2]) or DEFAULT, empty=empty)
        # A model without levels runs 模型自动管理 with none saved ('').
        effort = effort or ('default' if not empty and entry and not entry.get('levels') else '')
        rows.append(Setting(keys[2], '思考预算', kinds[2], effort,
                            kept(budgets, effort, dict(budget_options(entry)[0]) if entry else {})))
    return rows


def settings_block(rows):
    """Setting rows on one grey block."""
    return {'tag': 'column_set', 'flex_mode': 'none', 'columns': [
        {'tag': 'column', 'width': 'weighted', 'weight': 1, 'background_style': 'grey-50',
         'padding': '2px 12px 2px 12px', 'vertical_spacing': '0px', 'elements': rows}]}


def summary_block(choice, lines):
    """The chosen model large with its Harness and provider as tags, then ``lines`` (the rest of the
    choice) in grey small print: what 开始任务 starts, at a glance."""
    tags = ''.join(f"<text_tag color='{color}'>{text}</text_tag> " for text, color in (
        (choice.get('title') or choice.get('harness') or '', 'blue'),
        (choice.get('provider_title') or '自带', 'neutral')) if text)
    name = choice.get('display_name') or choice.get('label') or ''
    return {'tag': 'column_set', 'flex_mode': 'none', 'columns': [
        {'tag': 'column', 'width': 'weighted', 'weight': 1, 'background_style': 'grey-50',
         'padding': '10px 12px 10px 12px', 'vertical_spacing': '4px', 'elements': [
             md(f'**{name}**　{tags}'.rstrip(), size='heading'), md(grey(' · '.join(lines)), size='notation')]}]}


def device_setting(key, kind, current, options):
    """A 执行设备 row: 本机, then each device (alias, title) of ``options``."""
    return Setting(key, '执行设备', kind, current, [('', LOCAL), *options])


def launch_form_card(token, *, model, choices, effort, workspace=None, names=(), run_mode='direct', topic_tag='', notice='',
                     work_mode='', expanded=False, page='', devices=None, device='', device_options=()):
    """The launch card under the panels' title bar: the summary (summary_block), the settings list folded under
    ADJUST_TITLE (``expanded`` after a choice on it; ``page``, a row drawn open), then 其他目录… and 开始任务 in the
    form. Its token routes every callback to the record build_launch_card saved. ``devices``: the title of the device
    each of the ``names`` that is a device's workspace is on; the summary and the 工作区 row name it.

    A topic starts in the home (docs/configuration.md 主目录与常用路径): the card has no 工作区 or 运行方式 row and no 其他目录…, and
    ``device_options`` ((alias, title) of each device the chat may use; COVEY_DEVICES=1 with devices only) draws one 执行设备
    row, ``device`` its choice. A card with a ``workspace`` is one rebuilt from an older card's choices (a butler's
    worktree launch, a card sent before): it draws the 工作区 and 运行方式 rows and 其他目录… as before."""
    devices = devices or {}
    legacy = workspace is not None
    title = '开始任务'
    choice = choices.get(model, {})
    mode = work_modes(choice, work_mode)[1]
    total = model_count(choices, model) if choice else 0

    def draw(limit):  # The card with its 模型 row cut to ``limit`` (fitted).
        settings = model_settings(choices, model, kinds=(HARNESS_ACTION, PROVIDER_ACTION, MODEL_ACTION, BUDGET_ACTION,
                                                         WORK_MODE_ACTION), revision=1, effort=effort, work_mode=mode,
                                  limit=limit) if choice else []
        if legacy:
            settings += [workspace_setting('workspace', WORKSPACE_ACTION, workspace, names,
                                           last=[(MANAGE_OPTION, '⚙️ 添加或管理工作区…')],
                                           labels={name: f'{name} · {title}' for name, title in devices.items()}),
                         run_mode_setting('run_mode', MODE_ACTION, run_mode)]
        elif device_options:
            settings.append(device_setting('device', DEVICE_ACTION, device, device_options))
        rows = cut_rows(settings, [setting_row(setting, token=token, open_key=page) for setting in settings], total)
        facts = [f'思考预算 {effort_text(choice, effort)}', *(['工作模式 {}（{}）'.format(*WORK_MODES[mode])] if mode else []),
                 *([f'工作区 {workspace}' + (f' · 设备 {devices[workspace]}' if workspace in devices else ''),
                    RUN_MODES['direct'] + '运行' if run_mode == 'direct' else RUN_MODES['worktree']] if legacy else
                   [f'执行设备 {dict(device_options).get(device, device)}'] if device else [])]
        summary = summary_block(choice, facts) if choice else md('模型目录准备中，请稍后重新打开配置。')
        adjust = fold('launch_adjust', md(grey(ADJUST_TITLE if legacy else HOME_ADJUST_TITLE)), rows, expanded=bool(expanded or page), background='grey-50',
                      padding='0px 12px 0px 12px', spacing='0px')
        buttons = [{'tag': 'button', 'name': prefix + token, 'form_action_type': 'submit', 'type': style, 'width': 'fill',
                    'text': {'tag': 'plain_text', 'content': label}}
                   for prefix, label, style in [*([(PICKER_PREFIX, '其他目录…', 'default')] if legacy else []),
                                                (SUBMIT_PREFIX, title, 'primary_filled')]]
        head = panel_header('launch', title, f'话题 {topic_tag}' if topic_tag else '')
        return styled_panel(head, [
            *([md(notice)] if notice else []),
            summary,
            adjust,
            # The form holds the buttons only: each choice is recorded as made, and a directory not in the
            # list is pasted into 其他目录…'s browser (the path input went).
            {'tag': 'form', 'name': 'launch_form', 'elements': [
                {'tag': 'column_set', 'horizontal_spacing': '8px', 'columns': [
                    {'tag': 'column', 'width': 'weighted', 'weight': 1, 'elements': [button]} for button in buttons]},
            ]},
        ], title + (' · 模型、思考预算、工作区、运行方式' if legacy else ' · 模型、思考预算'))

    return fitted(draw, total)
