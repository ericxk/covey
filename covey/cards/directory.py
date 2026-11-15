"""The directory browser card: one bounded page of a directory, shown in the launch card's place
(选择其他目录) or the workspace manager's (＋ 添加工作区…). Its host names the buttons and the actions."""
from __future__ import annotations

import os
from typing import NamedTuple

from covey.cards.elements import (CARD_BYTES, CARD_NODES, fits, form_button, md, panel_header, plain, row,
                                            styled_panel, text_submit)
from covey.core.text import literal


DIRECTORY_FORM_PREFIX = 'bridge_dir_'
PAGE_SIZE = 15  # A row is 9 card nodes (3 columns, text, 2 buttons with their labels): 15 fit with the rest.
SEARCH_PLACEHOLDER = '搜索子目录名，或粘贴绝对路径'
DEVICE_PLACEHOLDER = '搜索子目录名，或粘贴绝对路径；设备目录写 别名:路径'  # Where the chat may use a device (``offer``).
NODE_LIMIT, SIZE_LIMIT = CARD_NODES - 5, CARD_BYTES - 2000  # The margin leaves room for the status line.


class Host(NamedTuple):
    """Where the browser is shown: its button names are ``prefix + token:revision: + verbs + verb``."""
    prefix: str
    verbs: str
    title: str
    pick: str  # A row's own button.
    use: str  # The bottom one, for the directory shown.
    back: str
    adds: bool  # Rows already listed are marked 已添加 and cannot be chosen again.


LAUNCH = Host(DIRECTORY_FORM_PREFIX, '', '选择工作区目录', '使用', '使用此目录', '返回', False)


def visible_entries(view):
    """(index, entry) of the rows a view lists: search results as found; a listing without hidden
    directories, or narrowed by the query of a card sent before search (筛选)."""
    entries = list(enumerate(view.get('entries', [])))
    if view.get('results'):
        return entries
    query = view.get('query', '').casefold().strip()
    return [(i, e) for i, e in entries
            if (view.get('hidden') or query.startswith('.') or not e['name'].startswith('.'))
            and query in e['name'].casefold()]


def label(name):
    if len(name) <= 60:
        return name
    return '…' + name[-59:] if '/' in name else name[:59] + '…'


def status_line(view, shown):
    query = literal(view.get('query', ''))
    if view.get('loading'):
        return f'正在搜索“{query}”…' if view.get('results') else '正在读取…'
    if view.get('error'):
        return view['error']
    if view.get('results'):
        return (f'搜索“{query}”：{len(shown)} 个结果' + ('，只列出前面的' if view.get('more') else '')
                + (f"；只查了前 {view.get('scanned', 0)} 个目录，可进入子目录再搜" if view.get('truncated') else ''))
    return f'共 {len(shown)} 个子目录' + ('；目录较大，只列出读到的部分，可粘贴完整路径' if view.get('truncated') else '')


def directory_card(token, view, host=LAUNCH, listed=None, top=()):
    """The whole card, one form: every button submits it, so the search box comes with each click. A view with a
    ``device`` shows which device it is on under the folder's name, and one that may go to a device (``offer``) says
    in the search box that ``别名:路径`` can be typed. ``listed`` maps a saved workspace's normalized path to its name; ``top``: elements above the form (the 设置 card's
    tab row), within the same budget."""
    top = list(top)
    listed = listed or {}

    def button(text, verb, primary=False, disabled=False, small=False):
        element = form_button(f"{host.prefix}{token}:{view['revision']}:{host.verbs}{verb}", text,
                              primary=primary, disabled=disabled)
        if small:
            element['size'] = 'small'
        return element

    def link(text, verb, tone='', disabled=False):
        return text_submit(f"{host.prefix}{token}:{view['revision']}:{host.verbs}{verb}", text, tone=tone, disabled=disabled)

    path, query = view['path'], view.get('query', '')
    blocked = bool(view.get('loading') or view.get('error'))
    here = listed.get(os.path.normpath(path))
    shown = visible_entries(view)
    pages = max(1, -(-len(shown) // PAGE_SIZE))
    page = min(max(view.get('page', 0), 0), pages - 1)
    # The folder's own name first, the full path below it in small grey print: a long path does not push
    # the list down, and the name is what tells two places apart.
    folder = os.path.basename(path.rstrip('/')) or '/'
    head = [md('📂 **' + literal(folder) + '**' + (" <font color='grey'>· 已在列表" + (
        '' if here == folder else '：' + literal(here)) + '</font>' if here else '')),
            md(literal(path if not view.get('device') else f"设备 {view.get('device_title') or view['device']}"
                       f"（{view['device']}）· {path}"), size='notation')]
    if view.get('notice'):
        head.append(md(view['notice']))
    status = [(md(status_line(view, shown), size='notation'), 1)]
    if query:
        status.append((button('清除搜索', 'clear', small=True), None))
    head += [row((button('⬆️ 上一级', 'up', disabled=path == '/', small=True), None), (button('🔄 刷新', 'refresh', small=True), None),
                 flex=None),
             row(({'tag': 'input', 'name': 'query', 'width': 'fill', 'max_length': 1000, 'default_value': query,
                   'placeholder': plain(DEVICE_PLACEHOLDER if view.get('device') or view.get('offer') else SEARCH_PLACEHOLDER)}, 1), (button('搜索', 'search'), None), flex=None),
             row(*status, flex=None, align='center')]
    rows = []
    for index, entry in shown[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
        # The name is text, so a long one wraps instead of being cut to "…" in a narrow topic panel; a search
        # result shows where it is below its name.
        known = os.path.normpath(os.path.join(path, entry['name'])) in listed
        parent, _, base = entry['name'].rpartition('/')
        text = ('🔗 ' if entry['link'] else '📁 ') + '**' + literal(label(base)) + '**'
        if known and not host.adds:
            text += " <font color='grey'>· 已在列表</font>"
        if parent:
            text += "\n<font color='grey'>" + literal(label(parent)) + '</font>'
        rows.append(row((md(text), 1), (link('进入', f'enter{index}'), None),
                        (link('已添加' if host.adds and known else host.pick, f'pick{index}', 'primary',
                              disabled=host.adds and known), None), flex=None, align='center'))
    if not rows and not blocked:
        rows.append(md('没有匹配的目录' if view.get('results') else '没有子目录', size='notation'))
    tail = []
    if pages > 1:
        tail.append(row((button('上一页', f'page{max(page - 1, 0)}', disabled=page == 0), None),
                        (md(f'第 {page + 1}/{pages} 页'), None),
                        (button('下一页', f'page{min(page + 1, pages - 1)}', disabled=page == pages - 1), None),
                        flex=None, align='center'))
    listed_here = host.adds and bool(here)
    tail.append(row((button(host.back, 'back'), None),
                    (button('此目录已在列表' if listed_here else host.use, 'use', primary=True,
                            disabled=blocked or listed_here), 1), flex=None))
    count = len(rows)
    while len(rows) > 1 and not fits(top + head + rows + tail, NODE_LIMIT, SIZE_LIMIT):
        rows.pop()
    if len(rows) < count:
        status[0][0]['content'] += f'；本页只显示前 {len(rows)} 行，可搜索缩小范围'
    form = {'tag': 'form', 'name': 'directory_form', 'vertical_spacing': '6px', 'elements': head + rows + tail}
    return styled_panel(panel_header('directory', host.title), [*top, form], host.title)
