"""The 设置 card's tab row (app/settings_hub.py): 私聊 | 新话题 | 工作区 | 脱敏 | 代答 | 群聊 at the top of the body of
the panel the card shows now, the panel's own card under it. The first two are the main thread's settings in two
pages (app/main_thread_panel.py: 私聊默认, 新话题默认), 工作区 the workspace list (app/workspace_manager.py) with the
main thread's 记住的工作区 and each workspace's 用途, 脱敏 the redaction term list (app/redaction_panel.py). The page 其他 is
retired (2026-11-09, the owner): its 直接开工 is on 新话题, the rest on 工作区; an older card's 其他 tab and buttons land
there (RETIRED). Small buttons side by side in one row, each as wide as its short label: the current tab solid blue,
the others text buttons, no spacing between them, so the six fit in one row in a topic's side panel (about 300px; bordered ones have a minimum
width there and wrapped to two rows, 2026-11-08 screenshots). No select, no confirmation: a tap redraws the whole
card as the other panel. A tap on the tab shown folds the card to its title bar and this row (``folded_hub``);
another unfolds it."""
from __future__ import annotations

from covey.cards.elements import callback_button, folded_panel, menu_item

TAB = 'bridge_settings_tab'
KINDS = frozenset({TAB})
# MAIN (私聊默认) keeps the key of the one 主线程 tab before it (2026-11-08): an older card's 主线程 tab is its first page.
MAIN, TOPIC, WORKSPACE, REDACTION, PROXY, GROUPS = 'main', 'topic', 'workspace', 'redaction', 'proxy', 'groups'
OTHER = 'other'  # The retired page 其他 (2026-11-08 to 11-09): an older card's tab and record name it.
RETIRED = {OTHER: WORKSPACE}  # A retired tab -> the tab its click and its record land on.
MAIN_PAGES = (MAIN, TOPIC)  # The main thread's pages (app/main_thread_panel.py), shown with it only.
# In the row's order.
LABELS = {MAIN: '私聊', TOPIC: '新话题', WORKSPACE: '工作区', REDACTION: '脱敏', PROXY: '代答', GROUPS: '群聊'}
# Each panel's title (cards/main_thread_panel.py TITLES, cards/workspace_manager.py, cards/redaction_panel.py,
# cards/proxy_panel.py, cards/group_manager.py) and icon.
TITLES = {MAIN: '私聊默认', TOPIC: '新话题默认', WORKSPACE: '工作区', REDACTION: '脱敏看板', PROXY: '代答', GROUPS: '群聊'}
ICONS = {MAIN: 'robot_outlined', TOPIC: 'launch', WORKSPACE: 'workspaces', REDACTION: 'redaction', PROXY: 'proxy',
         GROUPS: 'groups'}
SPACING = '0px'  # Between two tabs: the text buttons' own padding parts them; 8px wrapped 群聊 in a side panel (2026-11-09).


def successor(tab):
    """``tab`` as it is now: a retired one's successor."""
    return RETIRED.get(tab, tab)


def tab_row(current, token, tabs):
    """The tab row of the card whose record is ``current``'s panel's ``token``: a button for each of ``tabs``,
    LABELS' keys, and ``current`` always."""
    current = successor(current)
    shown = [tab for tab in LABELS if tab in tabs or tab == current]
    return {'tag': 'column_set', 'flex_mode': 'flow', 'horizontal_spacing': SPACING, 'margin': '0px 0px 4px 0px',
            'columns': [{'tag': 'column', 'width': 'auto', 'vertical_align': 'center', 'elements': [callback_button(
                LABELS[tab], {'kind': TAB, 'tab': tab, 'from': current, 'token': token},
                style='primary_filled' if tab == current else 'text', size='small')]} for tab in shown]}


def folded_hub(current, token, tabs):
    """The 设置 card folded in place on ``current``'s tab: that panel's title bar, the tab row and one grey line, none
    of the panel's body."""
    current = successor(current)
    return folded_panel(ICONS[current], TITLES[current], f'「{LABELS[current]}」或{menu_item("设置")}',
                        top=[tab_row(current, token, tabs)])
