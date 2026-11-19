"""The 代答 card (app/proxy_panel.py), the bot menu's 代答 panel: on or off and the group in its title
bar; tiles for the whitelist, the batches being handled and the model; 开启/关闭, web login and refresh;
the model as the launch card's settings rows (Harness, 供应商, 模型, 思考预算; no select); the whitelist, a row each with its state as coloured tags and pause/resume/remove
text buttons (a group's members added together fold into one row with buttons for all of them, and
逐个管理 lists them under it, each with 删除; long lists turn pages; over FOLD_ROWS rows it is a folded
panel); a search for several names at once whose result lists each name (全部添加 adds every unique
match, each candidate of an ambiguous one has its own button, a group can also be added with all its
members after a confirmation; gone from the card once something is added). Every control's value
carries its kind, the panel's token and the revision drawn."""
from __future__ import annotations

from collections import Counter

from covey.cards.ui import receipt
from covey.core.text import literal
from covey.cards.elements import (CARD_BYTES, CARD_NODES, callback_button, collapsible, element_count, fits,
                                            folded_panel, grey, item, json_size, md, menu_item, note, panel_header, plain, row, section,
                                            styled_panel, tag, tiles)
from covey.cards.launch_config import harness_setting, setting_row, settings_block, tier_rows
from covey.core.menu_choice import harness_of
from covey.core.proxy import GROUP_NAME, NAMES, source_of

ENABLE = 'bridge_proxy_enable'
DISABLE = 'bridge_proxy_disable'
# The model's settings rows (model_rows): a tap's ``option`` is in its value. MODEL and BUDGET were the selects of a
# card drawn before them (2026-11-10), whose click has the option beside the value; such a card still acts.
HARNESS = 'bridge_proxy_harness'
PROVIDER = 'bridge_proxy_provider'
TIER_MODEL = 'bridge_proxy_tier_model'
TIER_BUDGET = 'bridge_proxy_tier_budget'
MODEL = 'bridge_proxy_model'
BUDGET = 'bridge_proxy_budget'
FIND = 'bridge_proxy_find'
ADD = 'bridge_proxy_add'
ADD_ALL = 'bridge_proxy_add_all'
MEMBERS = 'bridge_proxy_members'
MEMBERS_ADD = 'bridge_proxy_members_add'
MEMBERS_CANCEL = 'bridge_proxy_members_cancel'
PAUSE = 'bridge_proxy_pause'
RESUME = 'bridge_proxy_resume'
REMOVE = 'bridge_proxy_remove'
SOURCE_PAUSE = 'bridge_proxy_source_pause'
SOURCE_RESUME = 'bridge_proxy_source_resume'
SOURCE_REMOVE = 'bridge_proxy_source_remove'
SOURCE_OPEN = 'bridge_proxy_source_open'
PAGE = 'bridge_proxy_page'
LOGIN = 'bridge_proxy_login'
REFRESH = 'bridge_proxy_refresh'
KINDS = frozenset({ENABLE, DISABLE, HARNESS, PROVIDER, TIER_MODEL, TIER_BUDGET, MODEL, BUDGET, FIND, ADD, ADD_ALL, MEMBERS, MEMBERS_ADD, MEMBERS_CANCEL, PAUSE,
                   RESUME, REMOVE, SOURCE_OPEN, SOURCE_PAUSE, SOURCE_RESUME, SOURCE_REMOVE, PAGE, LOGIN, REFRESH})
QUERY_LIMIT = 1000  # Characters of the search box: the most a card input takes.
# CardKit counts every plain_text as a node (each model on the menu is one). Below NODE_LIMIT and SIZE_LIMIT, less
# the title bar's, the whitelist page takes up to PAGE_NODES, a search's groups and candidates up to RESULT_NODES
# first and then what is left.
NODE_LIMIT = CARD_NODES - 10
PAGE_NODES = 72
RESULT_NODES = 45
SIZE_LIMIT = CARD_BYTES - 2000
MEMBER_NAMES = 20  # Names a group's confirmation shows.
FOLD_ROWS = 5  # A longer whitelist is a folded panel, open after a click on one of its rows.
# The most models the 模型 row lists (the chosen one kept): as the 设置 card's other tabs (OPTIONS), fewer while a
# search's result does not fit beside them.
MODEL_ROWS = (12, 6, 3)
FIRST_CHOICES = 2  # Candidates each name gets before any gets more, when not all fit.
# Nodes a whitelist page keeps however many names a search lists: two rows and their rule. Three before the
# title bar and the tiles (2026-11-03), when five names' first candidates no longer fit beside them.
PAGE_FLOOR = 20
TITLE = '代答'
QUERY_PLACEHOLDER = '人名、邮箱、群名或 ou_/oc_ ID，可写多个'
MODEL_NOTE = '所有联系人话题共用：新话题直接用，已有话题空闲时换过去。'
ADD_NOTE = ('可写多个（空格、逗号、顿号或换行分开，也可粘贴“@张三 @李四”）；'
            '“全部添加”加入唯一匹配，多个匹配的在候选里点“添加”。')
EMPTY = '还没有人：在下面「添加」里查找后加入。'
ICONS = {'model': 'robot_outlined', 'whitelist': 'member_outlined', 'add': 'member-add_outlined'}
KIND_COLORS = {'p2p': 'wathet', 'group': 'violet'}


def kind_label(contact):
    return '私聊' if contact['kind'] == 'p2p' else '群 @我'


def kind_tag(contact):
    return tag(kind_label(contact), KIND_COLORS.get(contact['kind'], 'neutral'))


def state_tags(running, paused, waiting, *, counted=False):
    """A row's state as tags: 运行中 green, 已暂停 grey, 处理中 orange; ``counted`` adds the numbers of a group."""
    tags = []
    if running:
        tags.append(tag(f'运行中 {running}' if counted else '运行中', 'green'))
    if paused:
        tags.append(tag(f'已暂停 {paused}' if counted else '已暂停', 'neutral'))
    if waiting:
        tags.append(tag(f'处理中 {waiting}', 'orange'))
    return ' '.join(tags)


def heading(part, title):
    """A part's heading with its icon, a little apart from the part above."""
    return {**section(title, ICONS[part]), 'margin': '4px 0px 0px 0px'}


def flow(*elements):
    """Buttons side by side, each as wide as its label; on a narrow screen they wrap, never cut."""
    return {'tag': 'column_set', 'flex_mode': 'flow', 'horizontal_spacing': '8px',
            'columns': [{'tag': 'column', 'width': 'auto', 'vertical_align': 'center', 'elements': [element]}
                        for element in elements]}


# compat(73dd0f3): a panel view saved before lookups of several names; retire when no proxy/panels record
# has ``found`` without ``lookup`` (compat_report: panel-lookup)
def legacy_lookup(found):
    """A view's ``found`` from before several names (one query, its candidates) as a lookup."""
    if not isinstance(found, dict):
        return None
    candidates = [c for c in found.get('candidates') or [] if isinstance(c, dict) and c.get('key')]
    return {'id': '', 'text': str(found.get('query') or ''), 'items': [
        {'query': str(found.get('query') or ''), 'state': 'choices' if candidates else 'none', 'candidates': candidates}]}


def lookup_of(view):
    return view.get('lookup') or legacy_lookup(view.get('found'))


def addable(lookup, listed):
    """The unique matches of ``lookup`` not whitelisted yet: what 全部添加 adds."""
    return [i['candidates'][0] for i in (lookup or {}).get('items') or []
            if i.get('state') == 'unique' and i['candidates'][0]['key'] not in listed]


def entries(contacts):
    """The whitelist's rows in the order they were added: a contact added by itself, or the members
    of one group added together ({chat_id: [contacts]} as one entry)."""
    rows, groups = [], {}
    for contact in contacts:
        source = source_of(contact)
        if not source:
            rows.append(contact)
        elif source in groups:
            groups[source].append(contact)
        else:
            groups[source] = [contact]
            rows.append(groups[source])
    return rows


class Controls:
    """The card's buttons and menus: each calls back with the card's token and revision. While ``busy``
    (what a click is still doing) every control except 刷新 is disabled."""

    def __init__(self, token, revision, busy):
        self.token, self.revision, self.busy = token, revision, busy

    def button(self, kind, label, *, primary=False, small=False, form=False, disabled=False, fit=False, tone=None,
               **value):
        """A button; ``tone`` ('' plain, 'primary' blue, 'danger' red) makes it a small borderless one for a row."""
        style = 'primary_filled' if primary else 'default'
        if tone is not None:
            style, small, fit = f'{tone}_text' if tone else 'text', True, True
        element = callback_button(label, {'kind': kind, 'token': self.token, 'revision': self.revision, **value},
                                  style=style, width='default' if fit else 'fill',
                                  disabled=bool(disabled or self.busy and kind != REFRESH),
                                  size='small' if small else None)
        if form:
            element.update(name='find', form_action_type='submit')
        return element

    def rows(self, settings):
        """Settings rows (cards/launch_config.py setting_row) whose taps carry the revision too; their fold IDs take
        it, so a redraw closes the row."""
        return settings_block([setting_row(setting, token=self.token, stamp=f'r{self.revision % 10000}',
                                           value={'revision': self.revision}) for setting in settings])


def columns(*cells):
    return row(*cells, align='center')


def proxy_panel_card(token, view, *, settings, contacts, batches, choices, current, busy='', top=()):
    """The whole card. ``current`` is (model key, budget option) of the settings' model on the menu,
    None when it is not there; while ``busy`` (what a click is still doing) every control except
    刷新 is disabled. ``top``: elements above the body (the 设置 card's tab row), counted in the budget."""
    ui = Controls(token, view['revision'], busy)
    pending = Counter(b.get('contact') for b in batches)
    rows = []
    for entry in entries(contacts):
        if not isinstance(entry, list):
            rows.append(contact_row(ui, entry, pending))
            continue
        opened = source_of(entry[0]) == view.get('source_open')
        rows.append(source_row(ui, entry, pending, opened))
        rows += [member_row(ui, contact, pending) for contact in entry] if opened else []
    add, group_rows, candidate_rows = add_part(ui, view, {c['key'] for c in contacts})
    head = title_bar(settings, contacts)
    above = list(top) + top_part(ui, view, settings, choices, current, contacts, pending)
    # A search's candidates first: the 模型 row lists fewer models (the chosen one kept) when they would not all fit.
    for limit in MODEL_ROWS:
        elements, whole = fitted(ui, view, contacts, head, above + model_part(ui, settings, choices, current, limit),
                                 rows, add, group_rows, candidate_rows)
        if whole:
            break
    return styled_panel(head, elements, f"{TITLE} · {'已开启' if settings.get('enabled') else '已关闭'}")


def title_bar(settings, contacts):
    """On or off as a tag; the group and the whitelist's size under the title."""
    group_name = settings.get('group_name') or GROUP_NAME
    where = f'群「{group_name}」' if settings.get('group_chat_id') else '尚未建群'
    return panel_header('proxy', TITLE, f'{where} · 白名单 {len(contacts)} 个',
                        [('已开启', 'green') if settings.get('enabled') else ('已关闭', 'neutral')])


def model_figure(settings, choices, current):
    """The model tile: the model's name on the menu without its Harness and provider, and the budget."""
    if current:
        entry = choices[current[0]]
        name = entry.get('display_name') or entry['label'].rsplit(' · ', 1)[-1]
    else:
        name = settings.get('model') or ('默认模型' if settings.get('harness') else '未设置')
    return '模型', literal(name), f"思考预算 {literal(settings.get('effort') or '模型默认')}"


def top_part(ui, view, settings, choices, current, contacts, pending):
    """What a click is doing or did, missing setup, the tiles, and 开启/关闭, 网页端登录, 刷新."""
    top = ([md(ui.busy)] if ui.busy else []) + ([receipt(view['notice'])] if view.get('notice') else [])
    warnings = ([] if settings.get('user_profile') else ['⚠️ 未配置以你身份读消息的 lark-cli profile（COVEY_PROXY_PROFILE）'])
    warnings += ['⚠️ ' + problem for problem in settings.get('send_problems') or ()]
    if warnings:
        top.append(note('\n'.join(warnings)))
    paused = sum(1 for c in contacts if c.get('paused'))
    top.append(tiles(('白名单', f'{len(contacts)}', f'运行中 {len(contacts) - paused}' + (f' · 暂停 {paused}' if paused else '')),
                     ('处理中', f'{sum(pending.values())}', '条待回复'), model_figure(settings, choices, current)))
    enabled = bool(settings.get('enabled'))
    top.append(flow(ui.button(DISABLE, '关闭代答', fit=True, small=True) if enabled
                    else ui.button(ENABLE, '开启代答', primary=True, fit=True, small=True),
                    ui.button(LOGIN, '网页端登录', fit=True, small=True), ui.button(REFRESH, '刷新', fit=True, small=True)))
    return top


def model_rows(settings, choices, current, limit=MODEL_ROWS[0]):
    """The Harness, 供应商, 模型 and 思考预算 rows of the settings' model, as the launch card's (cards/launch_config.py
    harness_setting, tier_rows without empty choices): the menu entry ``current`` names (a budget preset's entry with
    its budget), else the saved ids as they are."""
    if current:
        key, budget = current
        entry = choices[key]
        harness, model = harness_of(key, entry), key.partition(':')[2]
    else:
        entry, budget = None, ''
        harness, model = settings.get('harness') or '', settings.get('model') or ''
    return [harness_setting('p_harness', HARNESS, harness, choices),
            *tier_rows(choices, harness, model, budget, entry, keys=('p_provider', 'p_model', 'p_effort'),
                       kinds=(PROVIDER, TIER_MODEL, TIER_BUDGET), limit=limit, empty=False)]


def model_part(ui, settings, choices, current, limit=MODEL_ROWS[0]):
    """The model rows of the 代答 topics (at most ``limit`` models)."""
    part = [heading('model', '代答话题的模型')]
    model, effort = settings.get('model') or '默认模型', settings.get('effort') or '模型默认'
    if not choices:
        part.append(note(f"当前：{literal(settings.get('harness') or '未设置')} / {literal(model)} / {literal(effort)}。"
                         '模型目录准备中，稍后点“刷新”再选。'))
        return part
    part.append(ui.rows(model_rows(settings, choices, current, limit)))
    gone = '当前模型不在可选目录中，请在上面重新选择。' if settings.get('harness') and not current else ''
    part.append(note(gone + MODEL_NOTE + (f"工作区 {literal(settings['workspace'])}。" if settings.get('workspace') else '')))
    return part


# The whitelist: one row a contact, one row (and its buttons) a group's members, in pages. A row that
# starts an entry starts with a rule; the first one of a page is left out (``fitted``).
RULE = {'tag': 'hr'}


def contact_row(ui, contact, pending):
    paused = bool(contact.get('paused'))
    line = (f"**{literal(contact['name'] or contact['target'])}** {kind_tag(contact)} "
            + state_tags(not paused, paused, pending.get(contact['key'], 0)))
    toggle = (ui.button(RESUME, '恢复', tone='primary', contact=contact['key']) if paused
              else ui.button(PAUSE, '暂停', tone='primary', contact=contact['key']))
    return [dict(RULE), item(md(line), toggle, ui.button(REMOVE, '删除', tone='danger', contact=contact['key']))]


def source_row(ui, members, pending, opened=False):
    """A group's members added together: a line with 逐个管理 or 收起, then the buttons for all of them."""
    chat_id, name = source_of(members[0]), members[0]['source'].get('name') or source_of(members[0])
    paused = sum(1 for c in members if c.get('paused'))
    waiting = sum(pending.get(c['key'], 0) for c in members)
    line = (f"**群「{literal(name)}」的成员** {tag(f'{len(members)} 人', 'violet')} "
            + state_tags(len(members) - paused, paused, waiting, counted=True))
    buttons = [ui.button(SOURCE_PAUSE, '暂停全部', tone='primary', source=chat_id)] if paused < len(members) else []
    buttons += [ui.button(SOURCE_RESUME, '恢复全部', tone='primary', source=chat_id)] if paused else []
    buttons += [ui.button(SOURCE_REMOVE, '删除全部', tone='danger', source=chat_id),
                ui.button(MEMBERS, '同步成员', tone='primary', group=chat_id, name=name)]
    toggle = ui.button(SOURCE_OPEN, '收起' if opened else '逐个管理', tone='', source=chat_id)
    return [dict(RULE), item(md(line), toggle), flow(*buttons)]


def member_row(ui, contact, pending):
    """One member of an opened group's row, set in under it: its state and 删除."""
    paused = bool(contact.get('paused'))
    line = (f"{literal(contact['name'] or contact['target'])} "
            + state_tags(not paused, paused, pending.get(contact['key'], 0)))
    shown = item(md(line), ui.button(REMOVE, '删除', tone='danger', contact=contact['key']))
    shown['columns'][0]['padding'] = '0px 0px 0px 16px'
    return [shown]


def whitelist_part(view, contacts, folded, shown):
    """The whitelist's rows (and pager) drawn under its heading: in a folded panel when long."""
    part = [heading('whitelist', f'白名单（{len(contacts)}）' if contacts else '白名单')]
    if not contacts:
        return part + [note(EMPTY)]
    if not folded:
        return part + shown
    paused = sum(1 for c in contacts if c.get('paused'))
    title = f'全部 {len(contacts)} 个' + (f' · 已暂停 {paused}' if paused else '') + ' · 点开管理'
    return part + [collapsible('whitelist', title, shown, expanded=bool(view.get('whitelist_open')))]


def pager(ui, page, count):
    return columns((note(f'第 {page + 1}/{count} 页'), 2),
                   (ui.button(PAGE, '上一页', small=True, disabled=page == 0, page=page - 1), 1),
                   (ui.button(PAGE, '下一页', small=True, disabled=page == count - 1, page=page + 1), 1))


def described(candidate):
    return (f"**{literal(candidate['name'] or candidate['target'])}** · {literal(candidate['detail'])}"
            + (f" · {literal(candidate['email'])}" if candidate.get('email') else ''))


def group_buttons(ui, candidate, listed, lookup):
    known = candidate['key'] in listed
    count = candidate.get('members')
    return [ui.button(ADD, '已添加' if known else '群里@我', tone='primary', disabled=known,
                      candidate=candidate['key'], lookup=lookup['id']),
            ui.button(MEMBERS, f'全员 {count} 人' if count is not None else '全员', tone='primary',
                      group=candidate['target'], name=candidate['name'])]


def add_part(ui, view, listed):
    """The search box, a group's members to confirm, and the search result: a line a name, 全部添加, then
    (returned apart, for ``fitted`` to cut) a row a group, and each ambiguous name's candidates to choose."""
    part = [heading('add', '添加'), note(ADD_NOTE), {'tag': 'form', 'name': 'proxy_find', 'elements': [item(
        {'tag': 'input', 'name': 'query', 'width': 'fill', 'max_length': QUERY_LIMIT, 'input_type': 'multiline_text',
         'rows': 1, 'auto_resize': True, 'max_rows': 4, 'default_value': view.get('query', ''),
         'placeholder': plain(QUERY_PLACEHOLDER)},
        ui.button(FIND, '查找', form=True, fit=True))]}]

    members = view.get('members')
    if members:
        named = '、'.join(literal(m['name'] or m['open_id']) for m in members['add'][:MEMBER_NAMES])
        more = len(members['add']) - MEMBER_NAMES
        part.append(md(
            f"**加入群「{literal(members['name'])}」的全体成员**\n"
            f"共 {members['total']} 人" + (f"（另有机器人 {members['bots']} 个）" if members.get('bots') else '')
            + f"；不加你自己和已在白名单的 {members['skipped']} 人。将以私聊代答加入 **{len(members['add'])} 人**，每人一个话题：\n"
            + named + (f' 等 {len(members["add"])} 人' if more > 0 else '')
            + ('\n⚠️ 飞书只返回了部分成员（群的安全设置），只能加这些人。' if members.get('partial') else '')
            + '\n之后进群的人不会自动加入，可在白名单这个群的一行点“同步成员”补加。'))
        part.append(flow(ui.button(MEMBERS_ADD, f"确认添加 {len(members['add'])} 人", primary=True, fit=True,
                                   pending=members['id']),
                         ui.button(MEMBERS_CANCEL, '取消', fit=True, pending=members['id'])))

    lookup, group_rows, candidate_rows = lookup_of(view), [], []
    if not lookup:
        return part, group_rows, candidate_rows
    lines = []
    for each in lookup['items']:
        query = literal(each['query'])
        if each['state'] == 'unique':
            candidate = each['candidates'][0]
            known = candidate['key'] in listed
            lines.append(described(candidate) + f' · {kind_label(candidate)} '
                         + (tag('已在白名单', 'green') if known else tag('待添加', 'blue')))
            if candidate['kind'] == 'group':
                group_rows.append(item(note(f"群「{literal(candidate['name'])}」："),
                                       *group_buttons(ui, candidate, listed, lookup)))
        elif each['state'] == 'choices':
            count = len(each['candidates']) + each.get('more', 0)
            lines.append(f"**“{query}”** {tag(f'{count} 个匹配', 'orange')} 在下面选")
            block = []
            for candidate in each['candidates']:
                if candidate['kind'] == 'group':
                    block.append(item(md(described(candidate)), *group_buttons(ui, candidate, listed, lookup)))
                else:
                    known = candidate['key'] in listed
                    block.append(item(md(described(candidate) + ' · 私聊'),
                                      ui.button(ADD, '已添加' if known else '添加', tone='primary', disabled=known,
                                                candidate=candidate['key'], lookup=lookup['id'])))
            candidate_rows.append((query, block, each.get('more', 0)))
        elif each['state'] == 'error':
            lines.append(f"**“{query}”** {tag('没查成', 'red')} {literal(each.get('error') or '')}")
        else:
            lines.append(f"**“{query}”** {tag('没找到', 'neutral')}")
    if lookup.get('left'):
        lines.append(grey(f"一次最多查 {NAMES} 个名字，后面 {lookup['left']} 个没有查。"))
    part.append(note(f"“{literal(lookup['text'])}”的查找结果：" if len(lookup['items']) == 1
                     else f"查找结果（{len(lookup['items'])} 个名字）："))
    part.append(md('\n'.join(lines)))
    ready = addable(lookup, listed)
    if ready:
        part.append(ui.button(ADD_ALL, f'全部添加（{len(ready)}）', primary=True, fit=True, lookup=lookup['id']))
    return part, group_rows, candidate_rows


def results(group_rows, candidate_rows, counts):
    """The unique groups' rows, then each ambiguous name's first ``counts`` candidates under its heading."""
    shown = list(group_rows[:counts[0]])
    for (query, block, more), count in zip(candidate_rows, counts[1:]):
        hidden = more + len(block) - count
        title = note(f"“{query}”的匹配" + (f"（另有 {hidden} 个没列出，请写得更具体或用 ID）" if hidden else '') + '：')
        shown += [title, *block[:count]] if count else []
    return shown


def fitted(ui, view, contacts, head, top, rows, add, group_rows, candidate_rows):
    """The card's elements within CardKit's limits, less what the title bar ``head`` takes.

    The parts above always; the whitelist page up to PAGE_NODES of what is left, leaving up to
    RESULT_NODES for a search's groups and candidates, or the room of every name's first FIRST_CHOICES
    when that is more, as long as the page keeps PAGE_FLOOR; the result takes the rest. Pages are cut by
    that room, so a search on the card makes them shorter. Whatever does not fit is said. Returns the elements and
    whether the whole result and page fit."""
    nodes, size = NODE_LIMIT - element_count(head), SIZE_LIMIT - json_size(head)
    folded = len(rows) > FOLD_ROWS
    whole = [len(group_rows)] + [len(block) for _, block, _ in candidate_rows]
    first = whole[:1] + [min(count, FIRST_CHOICES) for count in whole[1:]]
    footer = []

    fixed = top + whitelist_part(view, contacts, folded, []) + add + footer
    left = nodes - element_count(fixed) - element_count(pager(ui, 0, 2))
    room = left - max(min(element_count(results(group_rows, candidate_rows, whole)), RESULT_NODES),
                      min(element_count(results(group_rows, candidate_rows, first)), left - PAGE_FLOOR))
    pages, used = [], 0
    for shown in rows:
        cost = element_count(shown)
        if not pages or pages[-1] and used + cost > min(PAGE_NODES, room):
            pages.append([])
            used = 0
        pages[-1].append(shown)
        used += cost
    page = min(max(int(view.get('page') or 0), 0), len(pages) - 1) if pages else 0
    page_rows = list(pages[page]) if pages else []
    paging = [pager(ui, page, len(pages))] if len(pages) > 1 else []

    def body(page_rows, extra):
        drawn = [e for shown in page_rows for e in shown]
        drawn = drawn[1:] if drawn[:1] == [RULE] else drawn  # No rule above a page's first row.
        return top + whitelist_part(view, contacts, folded, drawn + paging) + add + extra + footer

    def room_for(counts):
        return fits(body(page_rows, results(group_rows, candidate_rows, counts)), nodes - 1, size - 400)

    # What fits of the result: the unique groups, then FIRST_CHOICES candidates of each name (one where
    # two do not fit), then the rest in order. 2026-10-27, “zengduju”'s 21 matches got no row at all.
    counts = [0] * len(whole)
    for at in range(len(whole)):
        take = first[at]
        while take and not room_for(counts[:at] + [take] + counts[at + 1:]):
            take -= 1
        counts[at] = take
    for at in range(len(whole)):
        while counts[at] < whole[at] and room_for(counts[:at] + [counts[at] + 1] + counts[at + 1:]):
            counts[at] += 1
    extra = results(group_rows, candidate_rows, counts)
    if counts != whole:
        extra.append(note('候选太多，卡片放不下全部：请把名字写得更具体，或分几次查找。'))
    trimmed = False
    while page_rows and not fits(body(page_rows, extra), nodes, size):
        page_rows.pop()  # A model menu larger than planned for: the page gives way.
        trimmed = True
    if trimmed:
        paging.insert(0, note('卡片放不下这一页的全部，后面的请翻页查看。'))
    return body(page_rows, extra), counts == whole and not trimmed


def folded_card():
    """The menu's own 代答 card folded in place (app/proxy_panel.py, a second click on the menu's 代答)."""
    return folded_panel('proxy', TITLE, menu_item(TITLE))


def expired_card(top=()):
    """``top``: the 设置 card's tab row, which stays usable on it."""
    head = panel_header('proxy', TITLE, '这张卡已过期', [('已过期', 'neutral')])
    return styled_panel(head, [*top, md('只有最新的一张代答卡能操作：请用最新的一张，或点机器人菜单「代答」重新打开。')],
                        f'{TITLE} · 已过期')
