"""The 管家 settings card (app/butler_panel.py), in the bot menu panels' style: the owner changes the butler's
 settings (core/butler_settings.py) on it, on one page, the 设置 card's tab 配置 (cards/settings_hub.py CONFIG; the pages
管家, 新话题 and 模型 it took the place of are retired into it), a section a use (``config_page``): 新话题 (what every
new topic is preselected with, the launch card's and the topics the butler opens alike: line one Harness, 供应商, 模型,
思考预算, line two 脱敏), 管家 (line one the same four, line two 脱敏, when to compact; then the
private chat mode on a line of its own, MODE_CHOICES: 每条直接开一个话题 asks once more, ``mode_confirm``, before it is saved),
专项负责人 (``lead_setting_list``, with COVEY_PROJECTS=1: what a new project's
lead runs on) and under 更多 子 Agent, which every Claude Code turn the Bridge starts
follows, the butler's too (backends/claude/subagents.py). The Harness, 供应商, 模型 and 思考预算 rows offer what the launch card's do
(core/menu_choice.py tiers, budget_options). There is no 工作区 row, 运行方式 row or 用途 input any more (a new topic starts in the home;
the repositories are the 设置 card's 仓库 tab and the web page's): the old buttons are OFFLINE_KINDS, a click on one says so. A card of its own
(from before the 设置 card, gen 286) shows the pages one under the other.

No select: a choice is a settings-list row (cards/launch_config.py setting_row) whose options open under it in
the client, two tiles to a line (``tile_lines``, setting_line); a tap records the option and redraws the whole card. Every value says the state it sets, so a click
on an older card or revision still does what it says. The fold IDs take the revision, so a redraw closes the row.
"""
from __future__ import annotations

from covey.cards import ui
from covey.core.text import literal
from covey.cards.elements import (CARD_BYTES, CARD_NODES, collapsible, fits, grey, item, md, note, panel_header,
                                            section, shown_width, styled_panel, tag, text_button)
from covey.cards.launch_config import (DEFAULT, Setting, tap, harness_setting, known,
                                                redaction_setting, setting_line, setting_row, settings_block,
                                                tier_rows)
from covey.core.menu_choice import butler_harnesses, default_entry, entry_of, models_of, redacts, tiers
from covey.cards.settings_hub import BUTLER, CONFIG, ICONS, TITLES, successor
from covey.core.butler_settings import COMPACT_TOKENS, SUBAGENTS as SUBAGENT_MODES
from covey.core.workspaces import RUN_MODES

ENABLE = 'bridge_main_enable'
MODEL = 'bridge_main_model'
EFFORT = 'bridge_main_effort'
PROVIDER = 'bridge_main_provider'
REDACTION = 'bridge_main_redaction'
COMPACT = 'bridge_main_compact'
HARNESS = 'bridge_main_harness'  # The butler's own Harness (core/butler_settings.py Settings.harness).
SPACE = 'bridge_main_workspace'  # Offline (OFFLINE_KINDS): the butler's own workspace (Settings.workspace, kept, no row).
TOPIC_HARNESS = 'bridge_main_topic_harness'
TOPIC_PROVIDER = 'bridge_main_topic_provider'
TOPIC_MODEL = 'bridge_main_topic_model'
TOPIC_EFFORT = 'bridge_main_topic_effort'
TOPIC_RUN_MODE = 'bridge_main_topic_run_mode'  # Offline (OFFLINE_KINDS).
TOPIC_REDACTION = 'bridge_main_topic_redaction'
TOPIC_WORKSPACE = 'bridge_main_topic_workspace'  # Offline (OFFLINE_KINDS).
SUBAGENTS = 'bridge_main_subagents'
# Retired (docs/legacy.md 直接开工): a click on an older card's button says so (RETIRED_NOTICE) and changes nothing.
START = 'bridge_main_start'
UNTRUST = 'bridge_main_untrust'
START_ROW = 'bridge_main_start_row'
RETIRED_KINDS = frozenset({START, START_ROW, UNTRUST})
NOTES = 'bridge_main_notes'  # Offline (OFFLINE_KINDS): 保存用途 of the retired 工作区 tab.
MODE = 'bridge_main_mode'  # The private chat mode's row (MODE_CHOICES); ENABLE is its 确定改 (and an older card's button).
SECTION = 'bridge_main_section'  # 配置's 展开 of a closed section (SECTIONS): the view's ``open``, no setting.
# 专项负责人 (core/butler_settings.py LeadModel): what a new project's lead runs on.
LEAD_HARNESS = 'bridge_main_lead_harness'
LEAD_PROVIDER = 'bridge_main_lead_provider'
LEAD_MODEL = 'bridge_main_lead_model'
LEAD_EFFORT = 'bridge_main_lead_effort'
# 辅助模型 (core/butler_settings.py AssistChoice, app/assist.py): which Harness the background model calls go through.
ASSIST = 'bridge_main_assist_harness'
# With a Harness named there, its 供应商, 模型 and 思考预算 as the sections above (T924: one setting for every background call).
ASSIST_PROVIDER = 'bridge_main_assist_provider'
ASSIST_MODEL = 'bridge_main_assist_model'
ASSIST_EFFORT = 'bridge_main_assist_effort'
ASSIST_AUTO, ASSIST_NONE = 'auto', 'none'  # app/assist.py AUTO, NONE: the row's two choices that are not a Harness.
ASSIST_WORDS = {ASSIST_AUTO: '自动（跟随管家）', ASSIST_NONE: '不用模型（只按规则）'}
ROWS = frozenset({ASSIST, HARNESS, MODEL, EFFORT, PROVIDER, REDACTION, COMPACT, TOPIC_HARNESS, TOPIC_PROVIDER, TOPIC_MODEL, TOPIC_EFFORT,
                  TOPIC_REDACTION, SUBAGENTS, MODE, LEAD_HARNESS, LEAD_PROVIDER, LEAD_MODEL, LEAD_EFFORT, ASSIST_PROVIDER, ASSIST_MODEL, ASSIST_EFFORT})  # Settings-list rows: the value's ``option`` is the choice.
# The rows and the 用途 button the 工作区 rows' removal took off the cards: a click on an older card's says OFFLINE_NOTICE and changes nothing
# (the kinds stay: an older card is still answered, app/card_actions.py RETIRED_TEXT says the same for a click no panel took).
OFFLINE_KINDS = frozenset({SPACE, TOPIC_WORKSPACE, TOPIC_RUN_MODE, NOTES})
KINDS = ROWS | RETIRED_KINDS | OFFLINE_KINDS | {ENABLE, SECTION}
# 配置's sections that open one at a time (the card has room for one's options, CardKit's element budget): the view's
# ``open`` names it, OPEN_FIRST when it names none. A closed one is its heading, 展开 and one grey line of what it holds.
TOPIC_PART, BUTLER_PART, LEAD_PART, ASSIST_PART = 'topic', 'butler', 'lead', 'assist'
SECTIONS = {TOPIC_PART: '新话题', BUTLER_PART: '管家', LEAD_PART: '专项负责人', ASSIST_PART: '辅助模型'}
OPEN_FIRST = TOPIC_PART
# The section a row is in: a click on it opens that section (a card drawn before the sections names any row).
PART_OF = {**dict.fromkeys((TOPIC_HARNESS, TOPIC_PROVIDER, TOPIC_MODEL, TOPIC_EFFORT, TOPIC_RUN_MODE, TOPIC_REDACTION,
                            TOPIC_WORKSPACE, *RETIRED_KINDS), TOPIC_PART),
           **dict.fromkeys((HARNESS, PROVIDER, MODEL, EFFORT, REDACTION, SPACE, COMPACT, MODE, ENABLE), BUTLER_PART),
           **dict.fromkeys((LEAD_HARNESS, LEAD_PROVIDER, LEAD_MODEL, LEAD_EFFORT), LEAD_PART),
           **dict.fromkeys((ASSIST, ASSIST_PROVIDER, ASSIST_MODEL, ASSIST_EFFORT), ASSIST_PART)}
# The page each button is on: a click on a card drawn before the pages, on one of the retired pages 管家, 新话题 and 模型
# or on the retired 其他 or 工作区 redraws the page of its row now (配置).
PAGE_OF = dict.fromkeys(KINDS, CONFIG)

TITLE = '管家'  # A card of its own; on the 设置 card each page's title and icon (cards/settings_hub.py TITLES, ICONS).
ICON = 'robot_outlined'
CLAUDE = 'claude'  # The butler's Harness when its settings name none and Claude Code is enabled (core/butler.py HARNESS).
COMPACT_CHOICES = (0, 100_000, 200_000, 300_000)
SUBAGENT_LABELS = {'light': '轻量（Sonnet，搜索用 Haiku）', 'same': '跟主模型'}  # core/butler_settings.py SUBAGENTS.
FOLLOW = {'last': '跟上次一样', 'bridge': '系统默认'}  # app/butler_topics.py LAST, BRIDGE.
# The 模型 row's: no 默认 beside the catalog's own "Default (recommended)" (ACCEPT-325 问题 2).
MODEL_FOLLOW = FOLLOW
# The private chat mode (docs/butler.md 私聊模式) as a row: True 管家.
MODE_CHOICES = {'agent': (True, '交给管家'), 'topics': (False, '每条直接开一个话题')}
MODE_CONFIRM = '改了以后，管家不再接私聊顶层的消息，话题的结论也不再回到聊天区。已开的话题照常在各自的话题串里继续。'
MODE_OFF = '现在私聊顶层每条消息直接开一个话题。改回交给管家，管家的设置才用得上。'
NOTE_LENGTH = 100  # Characters of a workspace's line.
OPTIONS = 12  # The most options a row lists at first; fewer when the card would not fit.
SHORT = 6  # Rows are cut to this many options.
# The 新话题默认 模型 row lists up to this many, the whole list of most catalogs (13 models in one catalog), and is
# cut last: the owner checks it against the launch card's.
MODEL_ROWS = 24
NODES, BYTES = CARD_NODES - 10, CARD_BYTES - 2000  # The header is outside.
NOTE = '只有你能改这张卡；管家按这些设置开话题，自己改不了。'  # A card of its own (gen 286).
# The web page's old 工作区 page's last line (app/web_settings.py, for a page that still draws it).
WORKSPACE_NOTE = '管家按用途挑工作区。'
# 新话题's first line: the page preselects every new topic (app/preselection.py, app/butler_topics.py).
TOPIC_NOTE = '你自己开的、管家开的话题都照这些预选；启动卡上还能改。'
SUBAGENT_NOTE = '子 Agent 对所有 Claude Code 话题和管家都生效；改文件和拿主意仍由主模型做。'
# 专项负责人's Harness row's empty choice, and the line under its rows (app/lead_harness.py binding).
LEAD_FOLLOW = '跟管家一样'
LEAD_NOTE = ('新建的专项，负责人用这里选的；没选就跟管家一样。已有的专项在专项卡上点「换配置」。'
             '只列能接 Bridge 工具的 Harness（负责人用它们派话题，在哪个上都只读）；管家开着脱敏时只列能脱敏的。')
# 辅助模型's line under its rows (app/assist.py), then what it runs on now (app/assist.py describe).
ASSIST_NOTE = ('分流、整理专项、记忆自己长这些后台活都用这一个；自动就和管家一样（Harness、模型、思考预算），'
               '选了哪个就只用哪个，用不了就不调模型并说明原因。')
RETIRED_NOTICE = '直接开工已下线：管家开的话题一律直接开工，这一项不用再设。'
OFFLINE_NOTICE = '这个选项已下线：新话题一律从主目录开始，仓库在网页设置的「仓库」页管。'
# 管家's line under its rows while 脱敏 keeps a Harness that cannot redact off its Harness row.
REDACT_FIRST = '开着脱敏时，Harness 只能选能脱敏的；要换别的，先把脱敏关掉。'
# The pages' last line where the butler cannot change them (ACCEPT-325 问题 3), once a page.
LOCKED = '这些只有你能改，管家自己改不了。'
# The private chat modes (docs/butler.md 私聊模式): the first row and the subtitle. True is 管家.
MODES = {True: ('管家', 'green', '聊天区的消息交给管家', '改成纯话题'),
         False: ('纯话题', 'neutral', '私聊顶层一条消息开一个话题', '改成管家')}


def compact_text(tokens):
    return '关' if tokens == 0 else f'{tokens / 10000:g} 万'


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
    redact; the current one kept), then 供应商, 模型 and 思考预算 as a new topic's of that Harness (tier_rows without
    empty choices: each names what it runs; a model saved before, '', is drawn 未选 and its budgets are
    default_entry's), 脱敏 where it can redact and 压缩阈值 where the Bridge compacts it. ``workspaces``
    is no row's any more (the 工作区 row is gone; the butler starts in the home); the argument stays for the callers."""
    harness, model = settings.butler_harness, settings.model
    offered = butler_harnesses(choices, redacting=settings.redaction)
    rows = [Setting('harness', 'Harness', HARNESS, harness,
                    known(list(offered.items()), harness, tiers(choices, harness)[0].get(harness) or harness))]
    entry = entry_of(choices, harness, model) if model else default_entry(choices, harness)
    rows += tier_rows(choices, harness, model, settings.effort, entry, keys=('provider', 'model', 'effort'),
                      kinds=(PROVIDER, MODEL, EFFORT), limit=limit, empty=False)
    if settings.redaction or redacts(choices, harness):
        rows.append(redaction_setting('redact', REDACTION, 'on' if settings.redaction else 'off'))
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


def mode_setting(settings):
    """The private chat mode's row (MODE_CHOICES)."""
    return Setting('mode', '私聊顶层的消息', MODE, 'agent' if settings.enabled else 'topics',
                   [(key, text) for key, (_, text) in MODE_CHOICES.items()])


def harness_line(settings, choices):
    """The butler's Harness, a line of the settings list that opens nothing, on a card of its own (gen 286)."""
    harness = settings.butler_harness
    title = tiers(choices, harness)[0].get(harness) or ('Claude Code' if harness == CLAUDE else harness)
    return {**md(f"{grey('Harness')}　{title}"), 'margin': '7px 0px 13px 0px'}


def topic_settings(defaults, choices, workspaces, limit, follows=None, models=None, fallback=None, butler=True):
    """``follows``: {row key: text} of what an empty row resolves to now (follow_text); 默认 without. ``models``: the
    模型 row's own limit (``limit`` without). ``fallback``: the menu entry an empty 模型 row runs now, whose budgets
    the 思考预算 row offers (none without, as with no Harness). ``workspaces`` and ``butler`` are no row's any more (no 工作区 or
    运行方式 row). The Harness row, then, with one chosen, tier_rows, then 脱敏."""
    follows = follows or {}
    harness, model = defaults.harness, defaults.model
    rows = [harness_setting('t_harness', TOPIC_HARNESS, harness, choices,
                            first=[('', follows.get('t_harness') or DEFAULT)])]
    if harness:
        rows += tier_rows(choices, harness, model, defaults.effort, entry_of(choices, harness, model) if model else fallback,
                          keys=('t_provider', 't_model', 't_effort'), kinds=(TOPIC_PROVIDER, TOPIC_MODEL, TOPIC_EFFORT),
                          limit=models or limit, follows=follows)
    # 脱敏 as on the launch card: only where the Harness can redact (no Harness chosen: the Bridge's may).
    if not harness or defaults.redaction or redacts(choices, harness):
        rows.append(redaction_setting('t_redact', TOPIC_REDACTION, 'on' if defaults.redaction else 'off'))
    return rows


def subagent_setting(settings):
    """The 子 Agent row (core/butler_settings.py SUBAGENTS), bot-wide: every Claude Code turn's subagents."""
    return Setting('subagent', '子 Agent', SUBAGENTS, settings.subagents,
                   [(mode, SUBAGENT_LABELS[mode]) for mode in SUBAGENT_MODES])


def assist_setting(assist):
    """辅助模型's Harness row (app/assist.py offered, current): 自动（跟随管家）, each Harness that answers one question by
    itself, 不用模型."""
    options = list(assist.get('options') or ())
    current = str(assist.get('current') or ASSIST_AUTO)
    return Setting('assist', 'Harness', ASSIST, current, known(options, current, ASSIST_WORDS.get(current, current)))


def assist_setting_list(settings, choices, limit, assist):
    """辅助模型's rows (core/butler_settings.py AssistChoice): its Harness row (``assist_setting``), and with a Harness named
    its 供应商, 模型 and 思考预算 as 专项负责人's (tier_rows without empty choices: each names what it runs)."""
    rows = [assist_setting(assist)]
    choice = settings.assist
    if choice.harness and not choice.off:
        entry = entry_of(choices, choice.harness, choice.model) if choice.model else default_entry(choices, choice.harness)
        rows += tier_rows(choices, choice.harness, choice.model, choice.effort, entry,
                          keys=('a_provider', 'a_model', 'a_effort'), kinds=(ASSIST_PROVIDER, ASSIST_MODEL, ASSIST_EFFORT),
                          limit=limit, empty=False)
    return rows


def assist_note(assist):
    """The line under 辅助模型's row: ASSIST_NOTE, then what it runs on now."""
    words = str(assist.get('words') or '')
    return note(ASSIST_NOTE + (words if words else ''))


def lead_setting_list(settings, choices, limit):
    """专项负责人's rows (core/butler_settings.py LeadModel): its Harness, LEAD_FOLLOW first (the butler's), then the Harnesses
    that can run a lead (those that can run the butler, butler_harnesses: they take the Bridge's tools; with the butler's
    脱敏 on, only those that redact too, as the lead keeps it, app/lead_harness.py; the saved one kept), and with one chosen
    its 供应商, 模型 and 思考预算 as the butler's (tier_rows without empty choices: each names what it runs)."""
    lead = settings.lead
    offered = butler_harnesses(choices, redacting=settings.redaction)
    rows = [Setting('l_harness', 'Harness', LEAD_HARNESS, lead.harness,
                    known([('', LEAD_FOLLOW), *offered.items()], lead.harness,
                          tiers(choices, lead.harness)[0].get(lead.harness) or lead.harness))]
    if lead.harness:
        entry = entry_of(choices, lead.harness, lead.model) if lead.model else default_entry(choices, lead.harness)
        rows += tier_rows(choices, lead.harness, lead.model, lead.effort, entry,
                          keys=('l_provider', 'l_model', 'l_effort'), kinds=(LEAD_PROVIDER, LEAD_MODEL, LEAD_EFFORT),
                          limit=limit, empty=False)
    return rows


# The rows of a section in the order the owner set, two tiles to a line (``tile_lines``): Harness, 供应商 / 模型, 思考预算 / then
# the rest (新话题: 脱敏; 管家: 脱敏, 压缩阈值, 私聊顶层的消息). A choice whose words are long
# (``wide``) takes a line of its own, as wide as the card.
MODEL_KEYS = frozenset({'harness', 'provider', 'model', 'effort', 't_harness', 't_provider', 't_model', 't_effort',
                        'l_harness', 'l_provider', 'l_model', 'l_effort',
                        'assist', 'a_provider', 'a_model', 'a_effort'})
WIDE = 16  # Display width (a CJK character is two) of a choice past which its tile is a line of its own.


def shown_value(setting):
    """What a settings row shows as chosen, as setting_row draws it closed."""
    return dict(setting.options).get(setting.current, setting.current or '未选')


def wide(setting):
    """Whether ``setting``'s choice is too long for half a card (a workspace's name, 子 Agent's words)."""
    return shown_width(shown_value(setting)) > WIDE


def lines(settings_):
    """``settings_`` as three lines, the web page's (app/web_settings.py rows_of): the model's rows, the others, the mode."""
    found = [[s for s in settings_ if s.key in MODEL_KEYS], [s for s in settings_ if s.key not in MODEL_KEYS | {'mode'}],
             [s for s in settings_ if s.key == 'mode']]
    return [line for line in found if line]


def tile_lines(settings_):
    """``settings_`` as the lines of tiles 配置 draws, (rows, full) each: the model's rows (MODEL_KEYS) first and the rest
    after them, each group two to a line in the order given; a ``wide`` row alone and full; a last row alone keeps its half."""
    found = []
    for group in ([s for s in settings_ if s.key in MODEL_KEYS], [s for s in settings_ if s.key not in MODEL_KEYS]):
        pending = []
        for setting in group:
            if wide(setting):
                found += [(pending, False)] if pending else []
                pending = []
                found.append(([setting], True))
                continue
            pending.append(setting)
            if len(pending) == 2:
                found.append((pending, False))
                pending = []
        found += [(pending, False)] if pending else []
    return found


# What a closed section's line leaves out: the choices that are the usual ones (the open section shows them).
USUAL = {'redact': 'off', 't_redact': 'off', 'mode': 'agent',
         'compact': str(COMPACT_TOKENS)}


def summary(settings_):
    """A closed section's grey line, short: the chosen values in order, '·' between, no labels (the values say what they
    are: the Harness, the model, 思考预算 as 'xhigh'); no 供应商 (the model says it); a choice that is the usual one
    (USUAL: 脱敏 关, compaction at its default, the mode 交给管家) left out, one that
    is not named by its label where the value alone would not say it (压缩, 脱敏): 'Antigravity · Gemini 4 Neon · xhigh'."""
    parts = []
    for s in settings_:
        if s.label == '供应商' or USUAL.get(s.key) == s.current:
            continue
        shown = literal(str(shown_value(s)))
        parts.append(f"{'压缩' if s.label == '压缩阈值' else s.label} {shown}" if s.label in ('压缩阈值', '脱敏') else shown)
    return ' · '.join(parts)


# The rows nobody changed in 30 days (U4, docs/ui.md 13 U15), each at the end of its open section in a fold of its own,
# 更多, closed (``view['more']`` opens it: the click was on one of them, MORE_KINDS). 脱敏 on and 每条直接开一个话题 are no
# usual choice (USUAL) and stay in sight.
FOLDED = frozenset({'provider', 't_provider', 'l_provider', 'redact', 't_redact', 'l_model', 'l_effort', 'mode'})
MORE_KINDS = frozenset({PROVIDER, TOPIC_PROVIDER, LEAD_PROVIDER, REDACTION, TOPIC_REDACTION, LEAD_MODEL, LEAD_EFFORT, MODE})


def folded(setting):
    """Whether ``setting``'s row goes in its section's 更多: one of FOLDED, at its usual choice where it has one (USUAL)."""
    return setting.key in FOLDED and USUAL.get(setting.key, setting.current) == setting.current


def more_fold(part, settings_, rows, expanded=False, stamp=''):
    """A section's 更多: a closed fold titled with what it holds, '更多：供应商、脱敏', its rows inside (``rows`` draws them);
    its ID takes the revision (``stamp``) as the rows' do, so a redraw draws it as ``expanded`` says."""
    return collapsible(f'more_{part}_{stamp}' if stamp else f'more_{part}', '更多：' + '、'.join(dict.fromkeys(s.label for s in settings_)), rows(settings_),
                       expanded=expanded, quiet=True)


def heading(title):
    """A section's heading in 配置, a little space above it to part it from the section before."""
    return {**section(title), 'margin': '6px 0px 0px 0px'}


def closed_section(part, line, click):
    """A section that is not open: its heading with 展开 beside it and ``line`` (``summary``) under, one tappable frame
    (a tap calls back ``click``, SECTION)."""
    return tap(click, [md(f"**{SECTIONS[part]}**　<font color='blue'>展开</font>"), note(line)], '8px 10px 8px 10px', border=True)


def config_page(settings, choices, workspaces, limit, follows, models, fallback, rows, value, confirm, *,
                butler_allowed, butler_on, open_=OPEN_FIRST, lead_on=False, assist=None, more=False, stamp=''):
    """配置's body, a section a use (``rows`` draws a section's rows, ``value`` a button's value): 新话题 (topic_settings,
    with 直接开工 and 记住的工作区 while ``butler_on``); 管家 while ``butler_allowed`` (the deployment allows the butler):
    its own rows (butler_setting_list) and the private chat mode, the mode's row alone in 纯话题, ``confirm`` asking
    before 每条直接开一个话题 is saved (mode_confirm, the section open); 专项负责人 (lead_setting_list) while ``lead_on``
    (COVEY_PROJECTS=1); 辅助模型 (``assist``: app/assist.py's row
    data; with the butler off too: the background calls run without it); under 更多 子 Agent. Only the section
    ``open_`` (SECTIONS) shows its rows, those nobody changes (``folded``) in its 更多 at the end (``more_fold``, open with
    ``more`` or ``confirm``); each other is its heading with 展开 (SECTION) and a grey line of what it holds
    (``summary``)."""
    parts = {TOPIC_PART: topic_settings(settings.topic_defaults, choices, workspaces, limit, follows, models, fallback,
                                        butler=butler_on)}
    if butler_allowed:
        parts[BUTLER_PART] = ([mode_setting(settings)] if not settings.enabled
                              else butler_setting_list(settings, choices, limit, workspaces) + [mode_setting(settings)])
    if lead_on:
        parts[LEAD_PART] = lead_setting_list(settings, choices, limit)
    if assist:
        parts[ASSIST_PART] = assist_setting_list(settings, choices, limit, assist)
    open_ = BUTLER_PART if confirm and BUTLER_PART in parts else open_ if open_ in parts else OPEN_FIRST
    elements = []
    for part, settings_ in parts.items():
        if part != open_:
            elements.append(closed_section(part, summary(settings_), value(SECTION, open=part)))
            continue
        rare = [s for s in settings_ if folded(s)]
        elements += [heading(SECTIONS[part]), *rows([s for s in settings_ if not folded(s)])]
        if rare:
            elements.append(more_fold(part, rare, rows, expanded=bool(more or confirm), stamp=stamp))
        if part == TOPIC_PART:
            elements.append(note(TOPIC_NOTE))
        elif part == BUTLER_PART:
            if not settings.enabled:  # 纯话题: nothing else of the butler's counts.
                elements.append(note(MODE_OFF))
            elif redact_first(settings, choices):
                elements.append(note(REDACT_FIRST))
            if confirm:
                elements += mode_confirm(value)
        elif part == LEAD_PART:
            elements.append(note(LEAD_NOTE))
        elif part == ASSIST_PART:
            elements.append(assist_note(assist))
    elements += [heading('更多'), *rows([subagent_setting(settings)]), note(SUBAGENT_NOTE)]
    if butler_on:
        elements.append(note(LOCKED))
    return elements


def butler_panel_card(token, view, settings, choices, workspaces, top=(), follows=None, page=None,
                           fallback=None, butler_on=True, butler_allowed=True, lead_on=False, assist=None):
    """The whole card for ``view`` (revision, notice) over ``settings`` (core/butler_settings.py Settings),
    ``choices`` (app/launch_models.py available_models) and ``workspaces``, the saved workspace names: ``page``
    (cards/settings_hub.py BUTLER_PAGES, a retired one as 配置) on the 设置 card, a card of its own (None, gen 286) as it
    was. ``top``: elements above the body (the 设置 card's tab row), within the same budget; ``follows`` and
    ``fallback`` as topic_settings. ``butler_on``: whether the butler serves the private chat now (its mode 管家 and the
    deployment allowing it, ``butler_allowed``): without it 新话题 has no 直接开工. ``lead_on``: whether projects are on
    (COVEY_PROJECTS=1), for 专项负责人's;
    ``assist``: 辅助模型's row (app/assist.py: options, current, words), None without it.
    ``view['confirm']``: 管家 asks to confirm 每条直接开一个话题
    (``mode_confirm``), ``view['open']`` the section of 配置 that shows its rows (SECTIONS), ``view['more']`` its 更多 open. 整理专项 is not here: the
    看板's alone (cards/board.py)."""
    page = successor(page) if page else None
    revision = view['revision']
    stamp = f'r{revision % 10000}'

    def value(kind, **more):
        return {'kind': kind, 'token': token, 'revision': revision, **more}

    def rows(settings_):
        return [setting_line(line, token=token, stamp=stamp, full=full) for line, full in tile_lines(settings_)]

    def draw(limit, models):
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
            elements += [section('新话题默认'), settings_block(rows(topic_settings(settings.topic_defaults, choices, workspaces, limit,
                                                                    follows, models, fallback)))]
            elements.append(note(NOTE))
            return elements
        return elements + config_page(settings, choices, workspaces, limit, follows, models, fallback, rows, value,
                                      view.get('confirm'), butler_allowed=butler_allowed, butler_on=butler_on,
                                      open_=view.get('open') or OPEN_FIRST, lead_on=lead_on,
                                      assist=assist, more=view.get('more'), stamp=stamp)

    # Too much for one card: fewer options a row down to SHORT; then the new topic's model list down to SHORT, then fewer options.
    def tries():
        yield from ((limit, MODEL_ROWS) for limit in range(OPTIONS, SHORT - 1, -1))
        yield from ((SHORT, models) for models in range(MODEL_ROWS - 1, SHORT - 1, -1))
        yield from ((limit, limit) for limit in range(SHORT - 1, 0, -1))

    for limit, models in tries():
        elements = draw(limit, models)
        if fits(elements, NODES, BYTES):
            break
    title = TITLE if page is None else TITLES[page]
    line = (config_subtitle(settings, choices, butler_on, butler_allowed, lead_on) if page == CONFIG
            else subtitle(settings, choices, page))
    return styled_panel(panel_header(ICON if page is None else ICONS[page], title, line),
                        elements, title + ' · 设置')


def mode_confirm(value):
    """What 每条直接开一个话题 asks before it is saved: its consequence, 取消 (the row's 交给管家, which changes nothing)
    and 确定改 (ENABLE off)."""
    return [md(MODE_CONFIRM), item(md(''), text_button('取消', value(MODE, option='agent')),
                                   text_button('确定改', value(ENABLE, on=False), tone='danger'))]


def config_subtitle(settings, choices, butler_on, butler_allowed=True, lead_on=False):
    """配置's title bar: each use's model in a word, '新话题 Claude Code · 管家 Opus · 负责人 跟管家'; '管家没开' in 纯话题."""
    def named(harness, model, empty):
        if model:
            return models_of(choices, harness).get(model, model)
        return tiers(choices, harness)[0].get(harness) or harness if harness else empty
    defaults = settings.topic_defaults
    parts = [f"新话题 {named(defaults.harness, defaults.model, FOLLOW['last'])}"]
    if butler_on:
        parts.append(f"管家 {named(settings.butler_harness, settings.model, '')}".strip())
    elif butler_allowed:
        parts.append('管家没开')
    if lead_on:
        parts.append(f"负责人 {named(settings.lead.harness, settings.lead.model, '跟管家')}")
    return ' · '.join(parts)


def subtitle(settings, choices, page):
    """The title bar's line: the mode, compaction and the new topics' Harness on a card of its own; the state of the
    retired page 管家 (BUTLER) or 新话题 (any other), as the web page's 新话题 and 管家 still say it (app/web_settings.py)."""
    mode, defaults = MODES[settings.enabled][0], settings.topic_defaults
    if page is None:
        harness = tiers(choices, defaults.harness)[0].get(defaults.harness, defaults.harness or DEFAULT)
        return ' · '.join([mode, f'压缩 {compact_text(settings.compact_tokens)}', f'新话题 {harness}'])
    if page == BUTLER:
        if not settings.enabled:
            return '没开：每条消息直接开一个话题'
        harness = settings.butler_harness
        title = tiers(choices, harness)[0].get(harness) or ('Claude Code' if harness == CLAUDE else harness)
        model = models_of(choices, harness).get(settings.model, settings.model) if settings.model else '模型未选'
        return ' · '.join([title, model] + (['脱敏'] if settings.redaction else []))
    harness = tiers(choices, defaults.harness)[0].get(defaults.harness, defaults.harness or FOLLOW['last'])
    model = models_of(choices, defaults.harness).get(defaults.model, defaults.model) if defaults.model else ''
    return ' · '.join([harness] + ([model] if model else []) + [RUN_MODES[defaults.run_mode]]
                      + (['脱敏'] if defaults.redaction else []))
