"""The 管家 settings card (app/main_thread_panel.py), in the bot menu panels' style: the owner changes the main
thread's settings (core/main_thread_settings.py) on it, in two pages, each a tab of the 设置 card
(cards/settings_hub.py MAIN_PAGES): 私聊默认 (the private chat mode, then the main thread's own Harness, fixed to
Claude Code, 供应商, 模型, 思考预算, 脱敏 and when to compact) and 新话题默认 (what a topic the main thread opens runs
with: Harness, 供应商, 模型, 思考预算, 运行方式, 脱敏, 工作区, and 子 Agent, which every Claude Code turn the Bridge
starts follows, the main thread's too (backends/claude/subagents.py); then 直接开工, the three start tiers). The Harness,
供应商, 模型 and 思考预算 rows of both offer what the launch card's do (core/menu_choice.py tiers, budget_options).
记住的工作区 (the trusted kinds, each removable, ``trusted_section``) and each workspace's 用途 are on the 设置 card's
工作区 tab (cards/workspace_manager.py, drawn by app/workspace_manager.py); the page 其他 that held them and 直接开工
is retired (2026-11-09). A card of its own (from before the 设置 card, gen 286) shows the pages one under the other,
then 记住的工作区 and 工作区用途 (``other_page``).

No select: a choice is a settings-list row (cards/launch_config.py setting_row) whose options open under it in
the client; a tap records the option and redraws the whole card. Every value says the state it sets, so a click
on an older card or revision still does what it says. The fold IDs take the revision, so a redraw closes the row.
"""
from __future__ import annotations

from covey.core.text import literal
from covey.cards.elements import (CARD_BYTES, CARD_NODES, fits, grey, item, md, note, panel_header, plain,
                                            row, section, styled_panel, tag, text_button)
from covey.cards.launch_config import (DEFAULT, Setting, harness_setting, known, listed,
                                                redaction_setting, run_mode_setting, setting_row, settings_block,
                                                tier_rows, workspace_setting)
from covey.core.menu_choice import default_entry, entry_of, models_of, redacts, tiers
from covey.cards.settings_hub import ICONS, MAIN, MAIN_PAGES as PAGES, TITLES, TOPIC, WORKSPACE
from covey.core.main_thread_settings import SUBAGENTS as SUBAGENT_MODES, TIERS
from covey.core.workspaces import RUN_MODES

ENABLE = 'bridge_main_enable'
MODEL = 'bridge_main_model'
EFFORT = 'bridge_main_effort'
PROVIDER = 'bridge_main_provider'
REDACTION = 'bridge_main_redaction'
COMPACT = 'bridge_main_compact'
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
ROWS = frozenset({MODEL, EFFORT, PROVIDER, REDACTION, COMPACT, TOPIC_HARNESS, TOPIC_PROVIDER, TOPIC_MODEL, TOPIC_EFFORT, TOPIC_RUN_MODE,
                  TOPIC_REDACTION, TOPIC_WORKSPACE, SUBAGENTS})  # Settings-list rows: the value's ``option`` is the choice.
KINDS = ROWS | {ENABLE, START, UNTRUST, NOTES}
# The page each button is on: a click on a card drawn before the pages (all on one), or on the retired 其他, redraws
# the page of its row now (直接开工 on 新话题, 记住的工作区 and 用途 on 工作区).
PAGE_OF = {**dict.fromkeys((ENABLE, MODEL, EFFORT, PROVIDER, REDACTION, COMPACT), MAIN),
           **dict.fromkeys((TOPIC_HARNESS, TOPIC_PROVIDER, TOPIC_MODEL, TOPIC_EFFORT, TOPIC_RUN_MODE, TOPIC_REDACTION,
                            TOPIC_WORKSPACE, SUBAGENTS, START), TOPIC),
           **dict.fromkeys((UNTRUST, NOTES), WORKSPACE)}

TITLE = '管家'  # A card of its own; on the 设置 card each page's title and icon (cards/settings_hub.py TITLES, ICONS).
ICON = 'robot_outlined'
CLAUDE = 'claude'  # The main thread is a Claude Code session (docs/main-thread.md).
COMPACT_CHOICES = (0, 100_000, 200_000, 300_000)
TIER_LABELS = {'readonly': '只读的', 'requested': '你在消息里亲口要的', 'trusted': '你记住过的'}
SUBAGENT_LABELS = {'light': '轻量（Sonnet，搜索用 Haiku）', 'same': '跟主模型'}  # core/main_thread_settings.py SUBAGENTS.
PICK = '由管家判断'
FOLLOW = {'last': '跟随上次', 'bridge': 'Bridge 默认'}  # app/main_topics.py LAST, BRIDGE.
# The 模型 row's: no 默认 beside the catalog's own "Default (recommended)" (ACCEPT-325 问题 2).
MODEL_FOLLOW = {**FOLLOW, 'bridge': '跟随 Bridge'}
NOTE_LENGTH = 100  # Characters of a workspace's line.
OPTIONS = 12  # The most options a row lists at first; fewer when the card would not fit.
SHORT = 6  # Rows are cut to this many options before the workspaces' lines are.
# The 新话题默认 模型 row lists up to this many, the whole list of most catalogs (13 models in one catalog, 2026-11-08), and is
# cut last: the owner checks it against the launch card's (T444: 跟话题区选模型对齐).
MODELS = 24
NODES, BYTES = CARD_NODES - 10, CARD_BYTES - 2000  # The header is outside.
NOTE = '只有你能改这张卡；管家按这些设置开话题，自己改不了。'  # 新话题默认, and a card of its own.
# Each page's last line (ACCEPT-325 问题 3): 私聊默认 is what the main thread itself runs with, 新话题默认 how and when
# its topics start.
NOTES_OF = {MAIN: '只有你能改这张卡；这些是管家自己用的设置，它自己改不了。',
            TOPIC: '只有你能改这张卡；管家按这些开话题、决定哪些直接开工，自己改不了。'}
# The 工作区 tab's (cards/workspace_manager.py) under 记住的工作区.
WORKSPACE_NOTE = '记住的工作区和用途只有你能改；管家按用途挑工作区，记住的直接开工。'
NOTES_HINT = '一句话说明这个工作区做什么'  # A 用途 input's placeholder.
# Why the main thread's Harness is not a choice: its tools (core/main_mcp.py) are an MCP server of Claude Code's.
HARNESS_NOTE = '管家靠 Claude Code 的工具开话题、查记录，Harness 只能是 Claude Code。'
TOPIC_NOTE = '管家开的话题按这些设置；空着的一行跟随你上次在启动卡上的选择。'
SUBAGENT_NOTE = '子 Agent 对所有 Claude Code 话题和管家都生效；改文件和拿主意仍由主模型做。'
START_NOTE = '其余的话题出一张预填好的启动卡，等你点开始任务。'
TRUSTED_EMPTY = '还没有：管家问你“以后这类直接开工吗”，你同意后记在这里。'
NOTES_EMPTY = '还没有工作区。'
# The private chat modes (docs/main-thread.md 私聊模式): the first row and the subtitle. True is 管家.
MODES = {True: ('管家', 'green', '聊天区的消息交给管家', '改成纯话题'),
         False: ('纯话题', 'neutral', '私聊顶层一条消息开一个话题', '改成管家')}


def compact_text(tokens):
    return '关' if tokens == 0 else f'{tokens / 10000:g} 万'


def trust_text(key):
    """A trust_key as the card names it: the workspace bold, then 只读 or 改文件."""
    workspace, _, kind = key.rpartition('|')
    return f"**{literal(workspace)}** · {'只读' if kind == 'read' else '改文件'}"


def follow_text(parts, names=FOLLOW):
    """What an empty 新话题默认 row resolves to: ``parts`` (text, source) as app/main_topics.py resolve found them
    with the row and the rows under it empty, each run of one source after its name (``names``), '跟随上次：opus ·
    max' or 'Bridge 默认：sonnet · medium'."""
    shown, head = [], ''
    for text, source in parts:
        name = names.get(source, '')
        shown.append(f'{name}：{text}' if name and name != head else text)
        head = name or head
    return ' · '.join(shown)


def main_settings(settings, choices, limit):
    """The main thread's own rows after its fixed Harness (``harness_line``): 供应商, 模型 and 思考预算 as a new
    topic's of Claude Code (tier_rows, with '' the default model, whose budgets are default_entry's), 脱敏 where
    Claude Code can redact, 压缩阈值."""
    model = settings.model
    entry = entry_of(choices, CLAUDE, model) if model else default_entry(choices, CLAUDE)
    rows = tier_rows(choices, CLAUDE, model, settings.effort, entry, keys=('provider', 'model', 'effort'),
                     kinds=(PROVIDER, MODEL, EFFORT), limit=limit, native=False)
    if settings.redaction or redacts(choices, CLAUDE):
        rows.append(redaction_setting('redact', REDACTION, 'on' if settings.redaction else 'off'))
    compact = settings.compact_tokens
    rows.append(Setting('compact', '压缩阈值', COMPACT, str(compact),
                        known([(str(t), compact_text(t)) for t in COMPACT_CHOICES], str(compact), compact_text(compact))))
    return rows


def harness_line(choices):
    """The main thread's Harness, a line of the settings list that opens nothing (HARNESS_NOTE says why)."""
    title = tiers(choices, CLAUDE)[0].get(CLAUDE) or 'Claude Code'
    return {**md(f"{grey('Harness')}　{title}"), 'margin': '7px 0px 13px 0px'}


def topic_settings(defaults, choices, workspaces, limit, follows=None, models=None, fallback=None):
    """``follows``: {row key: text} of what an empty row resolves to now (follow_text); 默认 without. ``models``: the
    模型 row's own limit (``limit`` without). ``fallback``: the menu entry an empty 模型 row runs now, whose budgets
    the 思考预算 row offers (none without, as with no Harness). The Harness row, then, with one chosen, tier_rows."""
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
    space = workspace_setting('t_space', TOPIC_WORKSPACE, defaults.workspace, workspaces, first=[('', PICK)])
    rows.append(space._replace(options=listed(known(space.options, defaults.workspace, defaults.workspace),
                                              defaults.workspace, limit)))
    return rows


def subagent_setting(settings):
    """The 子 Agent row (core/main_thread_settings.py SUBAGENTS), bot-wide: every Claude Code turn's subagents."""
    return Setting('subagent', '子 Agent', SUBAGENTS, settings.subagents,
                   [(mode, SUBAGENT_LABELS[mode]) for mode in SUBAGENT_MODES])


def main_thread_panel_card(token, view, settings, choices, workspaces, top=(), follows=None, page=None,
                           fallback=None):
    """The whole card for ``view`` (revision, notice) over ``settings`` (core/main_thread_settings.py Settings),
    ``choices`` (app/launch_models.py available_models) and ``workspaces``, the saved workspace names: ``page``
    (PAGES) on the 设置 card, the three one under the other on a card of its own (None). ``top``: elements above
    the body (the 设置 card's tab row), within the same budget; ``follows`` and ``fallback`` as topic_settings."""
    revision = view['revision']
    stamp = f'r{revision % 10000}'
    pages = PAGES if page is None else (page,)
    headed = len(pages) > 1  # A section title each page; one page has its title in the title bar.

    def value(kind, **more):
        return {'kind': kind, 'token': token, 'revision': revision, **more}

    def draw(limit, shown, models):
        elements = list(top) + ([md(view['notice'])] if view.get('notice') else [])
        if MAIN in pages:
            on = settings.enabled
            name, colour, line, switch = MODES[on]
            elements += [section('私聊模式'), item(
                md(f"{tag(name, colour)} {line}"),
                text_button(switch, value(ENABLE, on=not on), tone='danger' if on else 'primary'))]
            elements += [section('管家'), settings_block([harness_line(choices)] + [
                setting_row(s, token=token, stamp=stamp) for s in main_settings(settings, choices, limit)]),
                note(HARNESS_NOTE)]
        if TOPIC in pages:
            # 子 Agent only on the 设置 card's page: a card of its own (gen 286) is full already.
            bot_wide = [subagent_setting(settings)] if page == TOPIC else []
            elements += [section('新话题默认')] if headed else []
            elements += [settings_block([
                setting_row(s, token=token, stamp=stamp)
                for s in topic_settings(settings.topic_defaults, choices, workspaces, limit, follows, models,
                                        fallback) + bot_wide]),
                note(TOPIC_NOTE)] + ([note(SUBAGENT_NOTE)] if bot_wide else [])
            elements += start_section(settings, value)
        if page is None:
            elements += other_page(settings, workspaces[:shown], len(workspaces), value)
        elements.append(note(NOTE if page is None else NOTES_OF[page]))
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


def start_section(settings, value):
    """直接开工: the three start tiers, each turned on or off (新话题默认's last part)."""
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
    harness = tiers(choices, defaults.harness)[0].get(defaults.harness, defaults.harness or DEFAULT)
    if page is None:
        return ' · '.join([mode, f'压缩 {compact_text(settings.compact_tokens)}', f'新话题 {harness}'])
    if page == MAIN:
        model = models_of(choices, CLAUDE).get(settings.model, settings.model) if settings.model else '默认模型'
        return ' · '.join([mode, model] + (['脱敏'] if settings.redaction else [])
                          + [f'压缩 {compact_text(settings.compact_tokens)}'])
    model = models_of(choices, defaults.harness).get(defaults.model, defaults.model) if defaults.model else ''
    return ' · '.join([harness] + ([model] if model else []) + [RUN_MODES[defaults.run_mode]]
                      + (['脱敏'] if defaults.redaction else []) + [f'直接开工 {len(settings.start)}/{len(TIERS)} 类'])
