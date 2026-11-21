"""The 管家 settings card (app/butler_panel.py), in the bot menu panels' style: the owner changes the butler's
 settings (core/butler_settings.py) on it, in two pages, each a tab of the 设置 card
(cards/settings_hub.py BUTLER_PAGES): 新话题 (what every new topic is preselected with,
the launch card's and the topics the butler opens alike: Harness, 供应商, 模型, 思考预算, 运行方式, 脱敏, 工作区; then
直接开工, one row of START_CHOICES, with 记住的工作区 under it while it counts, ``trusted_section``; and under 更多 子
Agent, which every Claude Code turn the Bridge starts follows, the butler's too, backends/claude/subagents.py) and 管家
(the butler's own Harness, 供应商, 模型, 思考预算, 脱敏 and 工作区: a Harness that can run it, ``butler_setting_list``; under
更多 when to compact and the private chat mode, MODE_CHOICES: 每条直接开一个话题 asks once more, ``mode_confirm``, before it
is saved). The Harness, 供应商, 模型 and 思考预算 rows of both offer what the launch card's do (core/menu_choice.py
tiers, budget_options). Each workspace's 用途 is on the 设置 card's 工作区 tab (cards/workspace_manager.py, drawn by
app/workspace_manager.py); the page 其他 that held it is retired. A card of its own (from before the 设置 card, gen 286) shows the pages one under the other,
then 记住的工作区 and 工作区用途 (``other_page``).

No select: a choice is a settings-list row (cards/launch_config.py setting_row) whose options open under it in
the client; a tap records the option and redraws the whole card. Every value says the state it sets, so a click
on an older card or revision still does what it says. The fold IDs take the revision, so a redraw closes the row.
"""
from __future__ import annotations

from covey.cards import ui
from covey.core.text import literal
from covey.cards.elements import (CARD_BYTES, CARD_NODES, fits, grey, item, md, note, panel_header, plain,
                                            row, section, styled_panel, tag, text_button)
from covey.cards.launch_config import (DEFAULT, Setting, harness_setting, known, listed,
                                                redaction_setting, run_mode_setting, setting_row, settings_block,
                                                tier_rows, workspace_setting)
from covey.core.menu_choice import butler_harnesses, default_entry, entry_of, models_of, redacts, tiers
from covey.cards.settings_hub import ICONS, BUTLER, TITLES, TOPIC, WORKSPACE
from covey.core.butler_settings import SUBAGENTS as SUBAGENT_MODES, TIERS
from covey.core.workspaces import RUN_MODES

ENABLE = 'bridge_main_enable'
MODEL = 'bridge_main_model'
EFFORT = 'bridge_main_effort'
PROVIDER = 'bridge_main_provider'
REDACTION = 'bridge_main_redaction'
COMPACT = 'bridge_main_compact'
HARNESS = 'bridge_main_harness'  # The butler's own Harness (core/butler_settings.py Settings.harness).
SPACE = 'bridge_main_workspace'  # The butler's own workspace (Settings.workspace).
TOPIC_HARNESS = 'bridge_main_topic_harness'
TOPIC_PROVIDER = 'bridge_main_topic_provider'
TOPIC_MODEL = 'bridge_main_topic_model'
TOPIC_EFFORT = 'bridge_main_topic_effort'
TOPIC_RUN_MODE = 'bridge_main_topic_run_mode'
TOPIC_REDACTION = 'bridge_main_topic_redaction'
TOPIC_WORKSPACE = 'bridge_main_topic_workspace'
SUBAGENTS = 'bridge_main_subagents'
START = 'bridge_main_start'
UNTRUST = 'bridge_main_untrust'
NOTES = 'bridge_main_notes'
MODE = 'bridge_main_mode'  # The private chat mode's row (MODE_CHOICES); ENABLE is its 确定改 (and an older card's button).
START_ROW = 'bridge_main_start_row'  # 直接开工's row (START_CHOICES); START is an older card's one tier.
ROWS = frozenset({HARNESS, MODEL, EFFORT, PROVIDER, REDACTION, SPACE, COMPACT, TOPIC_HARNESS, TOPIC_PROVIDER, TOPIC_MODEL, TOPIC_EFFORT, TOPIC_RUN_MODE,
                  TOPIC_REDACTION, TOPIC_WORKSPACE, SUBAGENTS, MODE, START_ROW})  # Settings-list rows: the value's ``option`` is the choice.
KINDS = ROWS | {ENABLE, START, UNTRUST, NOTES}
# The page each button is on: a click on a card drawn before the pages (all on one), or on the retired 其他, redraws
# the page of its row now (直接开工 on 新话题, 记住的工作区 and 用途 on 工作区).
PAGE_OF = {**dict.fromkeys((ENABLE, MODE, HARNESS, MODEL, EFFORT, PROVIDER, REDACTION, SPACE, COMPACT), BUTLER),
           **dict.fromkeys((TOPIC_HARNESS, TOPIC_PROVIDER, TOPIC_MODEL, TOPIC_EFFORT, TOPIC_RUN_MODE, TOPIC_REDACTION,
                            TOPIC_WORKSPACE, SUBAGENTS, START, START_ROW, UNTRUST), TOPIC),
           NOTES: WORKSPACE}

TITLE = '管家'  # A card of its own; on the 设置 card each page's title and icon (cards/settings_hub.py TITLES, ICONS).
ICON = 'robot_outlined'
CLAUDE = 'claude'  # The butler's Harness when its settings name none (core/butler.py HARNESS).
COMPACT_CHOICES = (0, 100_000, 200_000, 300_000)
TIER_LABELS = {'readonly': '只读的', 'requested': '你在消息里亲口要的', 'trusted': '你记住过的'}
SUBAGENT_LABELS = {'light': '轻量（Sonnet，搜索用 Haiku）', 'same': '跟主模型'}  # core/butler_settings.py SUBAGENTS.
# The 工作区 row's empty choice: the launch card follows the last launch, the butler picks by 用途.
PICK, LAST = '跟上次一样（管家按用途挑）', '跟上次一样'
FOLLOW = {'last': '跟上次一样', 'bridge': '系统默认'}  # app/butler_topics.py LAST, BRIDGE.
# The 模型 row's: no 默认 beside the catalog's own "Default (recommended)" (ACCEPT-325 问题 2).
MODEL_FOLLOW = FOLLOW
# 直接开工 (core/butler_settings.py TIERS) as one row: a choice -> (the tiers it sets, its text). Any other set (a card
# of the older three switches) is shown as it is (``start_setting``) until a choice replaces it.
START_CHOICES = {'all': (('readonly', 'requested', 'trusted'), '都直接开工'),
                 'safe': (('readonly', 'trusted'), '只读的直接开工，改文件的等你点'),
                 'none': ((), '都等你点开始')}
# The private chat mode (docs/butler.md 私聊模式) as a row: True 管家.
MODE_CHOICES = {'agent': (True, '交给管家'), 'topics': (False, '每条直接开一个话题')}
MODE_CONFIRM = '改了以后，管家不再接私聊顶层的消息，话题的结论也不再回到聊天区。已开的话题照常在各自的话题串里继续。'
MODE_OFF = '现在私聊顶层每条消息直接开一个话题。改回交给管家，管家的设置才用得上。'
NOTE_LENGTH = 100  # Characters of a workspace's line.
OPTIONS = 12  # The most options a row lists at first; fewer when the card would not fit.
SHORT = 6  # Rows are cut to this many options before the workspaces' lines are.
# The 新话题默认 模型 row lists up to this many, the whole list of most catalogs (13 models in one catalog), and is
# cut last: the owner checks it against the launch card's.
MODELS = 24
NODES, BYTES = CARD_NODES - 10, CARD_BYTES - 2000  # The header is outside.
NOTE = '只有你能改这张卡；管家按这些设置开话题，自己改不了。'  # A card of its own (gen 286).
# The 工作区 tab's (cards/workspace_manager.py) last line.
WORKSPACE_NOTE = '管家按用途挑工作区。'
NOTES_HINT = '一句话说明这个工作区做什么'  # A 用途 input's placeholder.
# 新话题's first line: the page preselects every new topic (app/preselection.py, app/butler_topics.py).
TOPIC_NOTE = '你自己开的、管家开的话题都照这些预选；启动卡上还能改。'
SUBAGENT_NOTE = '子 Agent 对所有 Claude Code 话题和管家都生效；改文件和拿主意仍由主模型做。'
START_NOTE = '直接开工只管管家开的话题；其余的出一张预填好的启动卡，等你点开始任务。'
# 管家's line under its rows while 脱敏 keeps a Harness that cannot redact off its Harness row.
REDACT_FIRST = '开着脱敏时，Harness 只能选能脱敏的；要换别的，先把脱敏关掉。'
# 管家's 工作区 row's empty choice.
BUTLER_SPACE = '默认工作区'
# The pages' last line where the butler cannot change them (ACCEPT-325 问题 3), once a page.
LOCKED = '这些只有你能改，管家自己改不了。'
TRUSTED_EMPTY = '还没有：管家问你“以后这类直接开工吗”，你同意后记在这里。'
NOTES_EMPTY = '还没有工作区。'
# The private chat modes (docs/butler.md 私聊模式): the first row and the subtitle. True is 管家.
MODES = {True: ('管家', 'green', '聊天区的消息交给管家', '改成纯话题'),
         False: ('纯话题', 'neutral', '私聊顶层一条消息开一个话题', '改成管家')}


def compact_text(tokens):
    return '关' if tokens == 0 else f'{tokens / 10000:g} 万'


def trust_text(key):
    """A trust_key as the card names it: the workspace bold, then 只读 or 改文件."""
    workspace, _, kind = key.rpartition('|')
    return f"**{literal(workspace)}** · {'只读' if kind == 'read' else '改文件'}"


def follow_text(parts, names=FOLLOW):
    """What an empty 新话题默认 row resolves to: ``parts`` (text, source) as app/butler_topics.py resolve found them
    with the row and the rows under it empty, each run of one source after its name (``names``), '跟随上次：opus ·
    max' or 'Bridge 默认：sonnet · medium'."""
    shown, head = [], ''
    for text, source in parts:
        name = names.get(source, '')
        shown.append(f'{name}：{text}' if name and name != head else text)
        head = name or head
    return ' · '.join(shown)


def butler_setting_list(settings, choices, limit, workspaces=()):
    """The butler's own rows: its Harness (those that can run it, butler_harnesses: with 脱敏 on, only those that can
    redact; the current one kept), then 供应商, 模型 and 思考预算 as a new topic's of that Harness (tier_rows, with ''
    the default model, whose budgets are default_entry's), 脱敏 where it can redact, 工作区 (``workspaces``, the saved
    names; '' the default) and 压缩阈值 where the Bridge compacts it."""
    harness, model = settings.butler_harness, settings.model
    offered = butler_harnesses(choices, redacting=settings.redaction)
    rows = [Setting('harness', 'Harness', HARNESS, harness,
                    known(list(offered.items()), harness, tiers(choices, harness)[0].get(harness) or harness))]
    entry = entry_of(choices, harness, model) if model else default_entry(choices, harness)
    rows += tier_rows(choices, harness, model, settings.effort, entry, keys=('provider', 'model', 'effort'),
                      kinds=(PROVIDER, MODEL, EFFORT), limit=limit, native=False)
    if settings.redaction or redacts(choices, harness):
        rows.append(redaction_setting('redact', REDACTION, 'on' if settings.redaction else 'off'))
    space = workspace_setting('space', SPACE, settings.workspace, workspaces, first=[('', BUTLER_SPACE)])
    rows.append(space._replace(options=listed(known(space.options, settings.workspace, settings.workspace),
                                              settings.workspace, limit)))
    compact = settings.compact_tokens
    if compacts(choices, harness):
        rows.append(Setting('compact', '压缩阈值', COMPACT, str(compact),
                            known([(str(t), compact_text(t)) for t in COMPACT_CHOICES], str(compact), compact_text(compact))))
    return rows


def compacts(choices, harness):
    """Whether the Bridge compacts the butler's session on ``harness`` (app/launch_models.py ``compaction``); a menu
    without the field (an older cache) says yes for Claude Code, as it always did."""
    entries = [entry for key, entry in choices.items() if (entry.get('harness') or key.partition(':')[0]) == harness]
    return any(entry.get('compaction') for entry in entries) or harness == CLAUDE and not any(
        'compaction' in entry for entry in entries)


def redact_first(settings, choices):
    """Whether 管家's page says REDACT_FIRST: 脱敏 is on and some Harness that could run the butler is left off for it."""
    return settings.redaction and len(butler_harnesses(choices)) > len(butler_harnesses(choices, redacting=True))


def start_choice(start):
    """The START_CHOICES key of the tiers ``start``, '' when it is none of them."""
    return next((key for key, (tiers_, _) in START_CHOICES.items() if set(tiers_) == set(start)), '')


def start_setting(settings):
    """直接开工 as one row: START_CHOICES, and the tiers as they are when they are none of them."""
    current = start_choice(settings.start) or 'custom'
    options = [(key, text) for key, (_, text) in START_CHOICES.items()]
    if current == 'custom':
        options.append(('custom', '、'.join(TIER_LABELS[t] for t in settings.start) + '直接开工'))
    return Setting('start', '直接开工', START_ROW, current, options)


def mode_setting(settings):
    """The private chat mode's row (MODE_CHOICES)."""
    return Setting('mode', '私聊顶层的消息', MODE, 'agent' if settings.enabled else 'topics',
                   [(key, text) for key, (_, text) in MODE_CHOICES.items()])


def trusted_shown(settings):
    """Whether 记住的工作区 counts now: its tier on, the others not all on."""
    return 'trusted' in settings.start and start_choice(settings.start) != 'all'


def harness_line(settings, choices):
    """The butler's Harness, a line of the settings list that opens nothing, on a card of its own (gen 286)."""
    harness = settings.butler_harness
    title = tiers(choices, harness)[0].get(harness) or ('Claude Code' if harness == CLAUDE else harness)
    return {**md(f"{grey('Harness')}　{title}"), 'margin': '7px 0px 13px 0px'}


def topic_settings(defaults, choices, workspaces, limit, follows=None, models=None, fallback=None, butler=True):
    """``follows``: {row key: text} of what an empty row resolves to now (follow_text); 默认 without. ``models``: the
    模型 row's own limit (``limit`` without). ``fallback``: the menu entry an empty 模型 row runs now, whose budgets
    the 思考预算 row offers (none without, as with no Harness). ``butler``: whether the butler is on (the 工作区 row's
    empty choice says it picks). The Harness row, then, with one chosen, tier_rows."""
    follows = follows or {}
    harness, model = defaults.harness, defaults.model
    rows = [harness_setting('t_harness', TOPIC_HARNESS, harness, choices,
                            first=[('', follows.get('t_harness') or DEFAULT)])]
    if harness:
        rows += tier_rows(choices, harness, model, defaults.effort, entry_of(choices, harness, model) if model else fallback,
                          keys=('t_provider', 't_model', 't_effort'), kinds=(TOPIC_PROVIDER, TOPIC_MODEL, TOPIC_EFFORT),
                          limit=models or limit, follows=follows)
    rows.append(run_mode_setting('t_run_mode', TOPIC_RUN_MODE, defaults.run_mode))
    # 脱敏 as on the launch card: only where the Harness can redact (no Harness chosen: the Bridge's may).
    if not harness or defaults.redaction or redacts(choices, harness):
        rows.append(redaction_setting('t_redact', TOPIC_REDACTION, 'on' if defaults.redaction else 'off'))
    space = workspace_setting('t_space', TOPIC_WORKSPACE, defaults.workspace, workspaces,
                              first=[('', PICK if butler else LAST)])
    rows.append(space._replace(options=listed(known(space.options, defaults.workspace, defaults.workspace),
                                              defaults.workspace, limit)))
    return rows


def subagent_setting(settings):
    """The 子 Agent row (core/butler_settings.py SUBAGENTS), bot-wide: every Claude Code turn's subagents."""
    return Setting('subagent', '子 Agent', SUBAGENTS, settings.subagents,
                   [(mode, SUBAGENT_LABELS[mode]) for mode in SUBAGENT_MODES])


def butler_panel_card(token, view, settings, choices, workspaces, top=(), follows=None, page=None,
                           fallback=None, butler_on=True):
    """The whole card for ``view`` (revision, notice) over ``settings`` (core/butler_settings.py Settings),
    ``choices`` (app/launch_models.py available_models) and ``workspaces``, the saved workspace names: ``page``
    (cards/settings_hub.py BUTLER_PAGES) on the 设置 card, the three one under the other on a card of its own (None). ``top``: elements above
    the body (the 设置 card's tab row), within the same budget; ``follows`` and ``fallback`` as topic_settings.
    ``butler_on``: whether the butler serves the private chat now (its mode 管家 and the deployment allowing it): without
    it 新话题 has no 直接开工. ``view['confirm']``: 管家 asks to confirm 每条直接开一个话题 (``mode_confirm``). 整理专项 is
    not here: the 看板's alone (cards/board.py)."""
    revision = view['revision']
    stamp = f'r{revision % 10000}'

    def value(kind, **more):
        return {'kind': kind, 'token': token, 'revision': revision, **more}

    def rows(settings_):
        return settings_block([setting_row(s, token=token, stamp=stamp) for s in settings_])

    def draw(limit, shown, models):
        elements = list(top) + ([ui.receipt(view['notice'])] if view.get('notice') else [])
        if page is None:  # A card of its own (gen 286): as it was, the mode a button.
            on = settings.enabled
            name, colour, line, switch = MODES[on]
            elements += [section('私聊模式'), item(
                md(f"{tag(name, colour)} {line}"),
                text_button(switch, value(ENABLE, on=not on), tone='danger' if on else 'primary'))]
            # As it was: the Harness a line that opens nothing, no 工作区 row (the 设置 card's 管家 tab has both).
            elements += [section('管家用的模型'), settings_block([harness_line(settings, choices)] + [
                setting_row(s, token=token, stamp=stamp) for s in butler_setting_list(settings, choices, limit)
                if s.key not in ('harness', 'space')])]
            elements += [section('新话题默认'), rows(topic_settings(settings.topic_defaults, choices, workspaces, limit,
                                                                    follows, models, fallback))]
            elements += start_section(settings, value)
            elements += other_page(settings, workspaces[:shown], len(workspaces), value)
            elements.append(note(NOTE))
            return elements
        if page == BUTLER and not settings.enabled:
            # 纯话题: nothing else on the page counts.
            return elements + [rows([mode_setting(settings)]), note(MODE_OFF)]
        if page == BUTLER:
            own = butler_setting_list(settings, choices, limit, workspaces)
            elements += [rows([s for s in own if s.key != 'compact'])]
            if redact_first(settings, choices):
                elements.append(note(REDACT_FIRST))
            elements += [section('更多'), rows([s for s in own if s.key == 'compact'] + [mode_setting(settings)])]
            if view.get('confirm'):
                elements += mode_confirm(value)
            elements.append(note(LOCKED))
            return elements
        on = butler_on
        elements += [note(TOPIC_NOTE), rows(topic_settings(
            settings.topic_defaults, choices, workspaces, limit, follows, models, fallback, butler=on)
            + ([start_setting(settings)] if on else []))]
        if on:
            elements.append(note(START_NOTE))
            if trusted_shown(settings):
                elements += trusted_section(settings, value)
        elements += [section('更多'), rows([subagent_setting(settings)]), note(SUBAGENT_NOTE)]
        if on:
            elements.append(note(LOCKED))
        return elements

    # Too much for one card: fewer options a row down to SHORT; then fewer workspaces' lines, where that keeps the new
    # topic's whole model list; else that list down to SHORT, then fewer workspaces' lines, then fewer options.
    every = len(workspaces)

    def tries():
        yield from ((limit, every, MODELS) for limit in range(OPTIONS, SHORT - 1, -1))
        yield from ((SHORT, shown, MODELS) for shown in range(every - 1, 0, -1))
        yield from ((SHORT, every, models) for models in range(MODELS - 1, SHORT - 1, -1))
        yield from ((SHORT, shown, SHORT) for shown in range(every - 1, 0, -1))
        yield from ((limit, 1, limit) for limit in range(SHORT - 1, 0, -1))

    for limit, shown, models in tries():
        elements = draw(limit, shown, models)
        if fits(elements, NODES, BYTES):
            break
    title = TITLE if page is None else TITLES[page]
    return styled_panel(panel_header(ICON if page is None else ICONS[page], title, subtitle(settings, choices, page)),
                        elements, title + ' · 设置')


def mode_confirm(value):
    """What 每条直接开一个话题 asks before it is saved: its consequence, 取消 (the row's 交给管家, which changes nothing)
    and 确定改 (ENABLE off)."""
    return [md(MODE_CONFIRM), item(md(''), text_button('取消', value(MODE, option='agent')),
                                   text_button('确定改', value(ENABLE, on=False), tone='danger'))]


def start_section(settings, value):
    """直接开工 on a card of its own (gen 286): the three start tiers, each turned on or off."""
    elements = [section('直接开工')]
    for tier in TIERS:
        started = tier in settings.start
        elements.append(item(
            md(f"{TIER_LABELS[tier]} {tag('直接开工', 'green') if started else tag('等你点开始', 'neutral')}"),
            text_button('关闭' if started else '开启', value(START, tier=tier, on=not started),
                        tone='danger' if started else 'primary')))
    elements.append(note(START_NOTE))
    return elements


def trusted_section(settings, value):
    """记住的工作区: the trusted kinds, each with 删除 (``value(kind, **more)`` its button's value)."""
    elements = [section(f'记住的工作区（{len(settings.trusted)}）' if settings.trusted else '记住的工作区')]
    return elements + ([item(md(trust_text(key)), text_button('删除', value(UNTRUST, key=key), tone='danger'))
                        for key in settings.trusted] or [note(TRUSTED_EMPTY)])


def notes_button(value, named):
    """保存用途: submits the form the 用途 inputs (note_<i>, ``named``'s order) are in."""
    return {**text_button('保存用途', value(NOTES, workspaces=named), tone='primary'), 'name': 'main_notes',
            'form_action_type': 'submit'}


def other_page(settings, named, every, value):
    """A card of its own's last part: 记住的工作区 and 工作区用途 (``named``: the workspaces listed, of ``every``)."""
    elements = trusted_section(settings, value)
    elements.append(section('工作区用途'))
    if named:
        inputs = [row((md(f'**{literal(name)}**'), 1),
                      ({'tag': 'input', 'name': f'note_{i}', 'width': 'fill', 'max_length': NOTE_LENGTH,
                        'default_value': settings.note(name), 'placeholder': plain(NOTES_HINT)}, 3),
                      flex='none', align='center') for i, name in enumerate(named)]
        save = notes_button(value, named)
        more = [note(f'只列出前 {len(named)} 个工作区。')] if len(named) < every else []
        elements.append({'tag': 'form', 'name': 'main_notes_form', 'vertical_spacing': '6px',
                         'elements': inputs + more + [save]})
    else:
        elements.append(note(NOTES_EMPTY))
    return elements


def subtitle(settings, choices, page):
    """The title bar's line: the mode, compaction and the new topics' Harness on a card of its own; a page's own
    state on the 设置 card."""
    mode, defaults = MODES[settings.enabled][0], settings.topic_defaults
    if page is None:
        harness = tiers(choices, defaults.harness)[0].get(defaults.harness, defaults.harness or DEFAULT)
        return ' · '.join([mode, f'压缩 {compact_text(settings.compact_tokens)}', f'新话题 {harness}'])
    if page == BUTLER:
        if not settings.enabled:
            return '没开：每条消息直接开一个话题'
        harness = settings.butler_harness
        title = tiers(choices, harness)[0].get(harness) or ('Claude Code' if harness == CLAUDE else harness)
        model = models_of(choices, harness).get(settings.model, settings.model) if settings.model else '默认模型'
        return ' · '.join([title, model] + (['脱敏'] if settings.redaction else []))
    harness = tiers(choices, defaults.harness)[0].get(defaults.harness, defaults.harness or FOLLOW['last'])
    model = models_of(choices, defaults.harness).get(defaults.model, defaults.model) if defaults.model else ''
    return ' · '.join([harness] + ([model] if model else []) + [RUN_MODES[defaults.run_mode]]
                      + (['脱敏'] if defaults.redaction else []))
