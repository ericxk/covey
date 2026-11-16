"""The 工作区 card (app/workspace_manager.py), the bot menu's 工作区 panel and the launch card's
⚙️ 添加或管理工作区…: the saved list PAGE_SIZE a page under a search box and a filter, a row each with
its state as coloured tags and 设为默认 / 移除 text buttons; ＋ 添加工作区… shows the directory browser
of cards/directory.py under BROWSE_HOST. One form: every button submits the search with it.

On the 设置 card's 工作区 tab (``hub``) the tab row is on top and there is no 完成管理; with the butler's settings
(``notes``) each row has its 用途 input (note_<i>) and the form 保存用途, and ``after`` (记住的工作区,
cards/butler_panel.py trusted_section) comes under the form."""
from __future__ import annotations

import hashlib
import json
import time

from covey.cards.directory import Host, label
from covey.core import devices
from covey.core.text import literal
from covey.cards.butler_panel import NOTE_LENGTH, NOTES_HINT
from covey.cards.elements import (CARD_BYTES, CARD_NODES, fits, folded_panel, form_button, item, markdown_panel,
                                            md, menu_item, note, notice_card, panel_header, plain, row, styled_panel, tag, text_submit)
from covey.core.workspaces import missing_workspaces

MANAGER_PREFIX = 'bridge_wsm_'
TITLE = '工作区'
PAGE_SIZE = 8  # A row is 10 card nodes (its lines, two buttons): a page is ~140 with the rest.
NODE_LIMIT, SIZE_LIMIT = CARD_NODES - 10, CARD_BYTES - 2000  # The header is outside.
LIST_LIMIT = 60  # Lines of the removal details.
FILTERS = {'all': '全部', 'missing': '目录不存在', 'stale': '30 天以上未使用', 'unused': '从未用于任务'}
STALE_SECONDS = 30 * 86400
BUSY_NOTE = '⏳ 正在处理…'
REMOVED_TITLE = '查看移除详情'
QUERY_PLACEHOLDER = '搜索名称或路径'
ADD_LABEL = '＋ 添加工作区…'
NOTE = '添加只登记已有目录，名称取目录名；移除只改列表，不删除目录；已开始的话题不受影响。'
# A row's buttons: bridge_wsm_<token>:<revision>:<verb>-<key>, the key naming the workspace (row_key).
ROW_VERBS = ('remove', 'default')
BROWSE_VERBS = 'dir-'  # The browser's buttons: bridge_wsm_<token>:<revision>:dir-<verb>[<row or page>].
BROWSE_HOST = Host(MANAGER_PREFIX, BROWSE_VERBS, '添加工作区', '添加', '添加此目录', '返回管理页', True)


def row_key(name):
    """A row button's key: the name's hash, so a click acts on its row's workspace however the list
    moved since it was drawn (a task's start reorders it)."""
    return hashlib.sha256(name.encode('utf-8')).hexdigest()[:12]


def ordered_names(config):
    """The launch card's menu order: most recently used first."""
    return sorted(config['workspaces'], key=lambda name: -config.get('recent', {}).get(name, 0))


def list_entries(config, status):
    """The card's order: the default first, then the launch card's."""
    names = sorted(ordered_names(config), key=lambda name: name != config['default'])
    return [{'name': name, 'path': config['workspaces'][name], 'state': status.get(config['workspaces'][name]),
             'used': config.get('recent', {}).get(name),
             # A device's workspace (alias:path) says so; none exist unless devices are on.
             **({'device': config['workspaces'][name].partition(':')[0]}
                if devices.is_device_value(config['workspaces'][name]) else {})} for name in names]


def visible(entries, view, now=None):
    """The entries a view's search (name or full path) and filter select."""
    now = time.time() if now is None else now
    query, kind = view.get('query', '').strip().casefold(), view.get('filter', 'all')
    return [e for e in entries if (query in e['name'].casefold() or query in e['path'].casefold()) and (
        kind == 'all' or (kind == 'missing' and e['state'] is False) or (kind == 'unused' and not e['used'])
        or (kind == 'stale' and bool(e['used']) and now - e['used'] > STALE_SECONDS))]


def capped(items, budget, size=lambda one: len(json.dumps(one, ensure_ascii=False).encode('utf-8'))):
    """At most LIST_LIMIT items within budget bytes."""
    kept, used = [], 0
    for one in items[:LIST_LIMIT]:
        used += size(one)
        if used > budget:
            break
        kept.append(one)
    return kept


def markdown_lines(lines, budget):
    kept = capped(lines, budget, lambda text: len(text.encode('utf-8')) + 1)
    return '\n'.join(kept) + (f'\n……其余 {len(lines) - len(kept)} 项未列出' if len(kept) < len(lines) else '')


def tail(path, size=120):
    return path if len(path) <= size else '…' + path[-(size - 1):]


def entry_tags(entry, default):
    return ([tag('默认', 'blue')] if entry['name'] == default else []) + (
        [tag('设备 ' + entry['device'], 'blue')] if entry.get('device') else []) + (
        [tag('目录不存在', 'red')] if entry['state'] is False else [tag('未确认', 'orange')] if entry['state'] is None else [])


def manager_card(token, view, config, status, *, standalone, hub=False, top=(), notes=None, after=(), device_lines=()):
    """The whole card under the menu panels' title bar; while a change is applied every action except 刷新
    is disabled. ``standalone`` (opened from the menu) ends with 完成管理, in a launch card's place with
    返回启动卡 and 结束，不启动任务; ``hub`` (the 设置 card's tab, ``top`` its tab row) with 刷新 only. ``notes``:
    (name -> 用途, ``save(names)`` the 保存用途 button of the rows listed) to edit each row's 用途; ``after``: elements
    under the form, within the same budget. ``device_lines``: a line on each device the chat may use (its last
    self-check, core/devices.py ``status_text``), above the form. Its form: the list's head (``list_head``), a row for each workspace
    remembered (``entry_rows``), pages and 移除不存在的 (``list_foot``), and the buttons (``end_buttons``)."""
    busy = bool(view.get('processing'))

    def name(verb):
        return f"{MANAGER_PREFIX}{token}:{view['revision']}:{verb}"

    def button(text, verb, primary=False, disabled=False, small=False):
        element = form_button(name(verb), text, primary=primary, disabled=bool(disabled or busy and verb != 'refresh'))
        if small:
            element['size'] = 'small'
        return element

    def text_button(text, verb, tone=''):
        return text_submit(name(verb), text, tone=tone, disabled=busy)

    default = config['default']
    entries = list_entries(config, status)
    missing = missing_workspaces(config, status)
    gone, unknown = [e for e in entries if e['state'] is False], [e for e in entries if e['state'] is None]
    subtitle = f'共 {len(entries)} 个 · 默认 {label(default)}'
    tags = ([(f'{len(gone)} 个目录不存在', 'red')] if gone else []) + (
        [(f'{len(unknown)} 个未确认', 'orange')] if unknown else [])
    elements = []
    notice = BUSY_NOTE if busy else view.get('notice')
    if notice:
        elements.append(md(notice))
    if view.get('details') and not busy:
        elements.append(markdown_panel('wsm_removed', REMOVED_TITLE, view['details']))
    elements += [note(literal(line)) for line in device_lines]

    shown = visible(entries, view)
    pages = max(1, -(-len(shown) // PAGE_SIZE))
    page = min(max(int(view.get('page') or 0), 0), pages - 1)
    head = list_head(view, shown, button, text_button)
    rows, names = entry_rows(shown[page * PAGE_SIZE:(page + 1) * PAGE_SIZE], default, notes, text_button)
    foot = list_foot(page, pages, missing, button, text_button)

    def form(small):
        ends = end_buttons(button, standalone=standalone, hub=hub, notes=notes, names=names[:len(rows)])
        # In the topic side panel "stretch" gave every control a line of its own and "bisect" cut 结束，不启动任务
        # to "…" (2026-11-02 review): the buttons keep their width and wrap ("flow").
        return {'tag': 'form', 'name': 'workspace_manager_form', 'vertical_spacing': '6px',
                'elements': head + rows + foot + [note(small), row(*((end, None) for end in ends), flex='flow')]}

    count = len(rows)
    top, after = list(top), list(after)
    while len(rows) > 1 and not fits(top + elements + [form(NOTE)] + after, NODE_LIMIT, SIZE_LIMIT):
        rows.pop()
    elements.append(form(NOTE + (f'本页只列出前 {len(rows)} 个，可搜索缩小范围。' if len(rows) < count else '')))
    return styled_panel(panel_header('workspaces', TITLE, subtitle, tags), top + elements + after, TITLE)


def list_head(view, shown, button, text_button):
    """The list's head: the search box, the filter and 筛选, then the count and ＋ 添加工作区…."""
    query, kind = view.get('query', ''), view.get('filter', 'all')
    scope = (f'搜索“{literal(query)}” · ' if query else '') + f'{FILTERS[kind]} {len(shown)} 个'
    # The search box takes what the filter and 筛选 leave ("none" keeps them on one line); ＋ 添加工作区… sits
    # by the count, as wide as its label.
    return [row(({'tag': 'input', 'name': 'query', 'width': 'fill', 'max_length': 100, 'default_value': query,
                  'placeholder': plain(QUERY_PLACEHOLDER)}, 3),
                ({'tag': 'select_static', 'name': 'filter', 'width': 'fill', 'initial_option': kind,
                  'options': [{'text': plain(text), 'value': key} for key, text in FILTERS.items()]}, 2),
                (button('筛选', 'filter'), None), flex='none', align='center'),
            item(note(scope), text_button(ADD_LABEL, 'browse', 'primary'))]


def entry_rows(listed, default, notes, text_button):
    """(a row for each remembered workspace of the page ``listed``: its name and tags, path and last use, 设为默认
    and 移除, and with ``notes`` its 用途 box; the workspaces' names), a line saying so when none is listed."""
    rows, names = [], []
    for i, entry in enumerate(listed):
        used = time.strftime('最近 %m-%d', time.localtime(entry['used'])) if entry['used'] else '未用于任务'
        line = md(' '.join([f"**{literal(label(entry['name']))}**", *entry_tags(entry, default)]))
        key = row_key(entry['name'])
        actions = ([text_button('设为默认', f'default-{key}', 'primary')]
                   if entry['name'] != default and entry['state'] is not False else []) + (
            [text_button('移除', f'remove-{key}', 'danger')] if entry['name'] != default else [])
        listing = item(line, *actions)
        listing['columns'][0].update(vertical_spacing='2px', elements=[line, note(f"{literal(tail(entry['path']))} · {used}")])
        if notes is not None:
            listing['columns'][0]['elements'].append(
                {'tag': 'input', 'name': f'note_{i}', 'width': 'fill', 'max_length': NOTE_LENGTH,
                 'default_value': notes[0].get(entry['name'], ''), 'placeholder': plain('用途：' + NOTES_HINT)})
        rows.append(listing)
        names.append(entry['name'])
    if not rows:
        rows.append(note('没有符合条件的工作区。'))
    return rows, names


def list_foot(page, pages, missing, button, text_button):
    """Under the rows: the page and its buttons when there are more, 移除不存在的 when a directory is gone."""
    foot = []
    if pages > 1:
        foot.append(row((note(f'第 {page + 1}/{pages} 页'), 2),
                        (button('上一页', f'page{max(page - 1, 0)}', disabled=page == 0, small=True), 1),
                        (button('下一页', f'page{min(page + 1, pages - 1)}', disabled=page == pages - 1, small=True), 1),
                        flex='none', align='center'))
    if missing:
        foot.append(item(note('目录已不存在的工作区可一次移除'),
                         text_button(f'移除不存在的（{len(missing)}）', 'prune', 'danger')))
    return foot


def end_buttons(button, *, standalone, hub, notes, names):
    """The form's buttons: 刷新, then 完成管理 (``standalone``), or 返回启动卡 and 结束，不启动任务 (a launch
    card's), none more on the 设置 card (``hub``); 保存用途 first when ``notes`` and the rows ``names`` listed."""
    ends = ([button('刷新', 'refresh')] if hub else
            [button('刷新', 'refresh'), button('完成管理', 'done', primary=True)] if standalone else
            [button('刷新', 'refresh'), button('返回启动卡', 'back', primary=True), button('结束，不启动任务', 'end')])
    if notes is not None and names:
        ends.insert(0, notes[1](names))
    return ends


def closed_card(text):
    return notice_card(text, '工作区管理已结束')


def folded_card():
    """The menu's card folded in place (app/workspace_manager.py, a second menu click)."""
    return folded_panel('workspaces', TITLE, menu_item('工作区'))
