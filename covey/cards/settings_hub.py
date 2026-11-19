"""The 设置 card's tab row (app/settings_hub.py): 新话题 | 管家 | 工作区 | 脱敏 | 群聊 (| 设备) at the top of the body of
the panel the card shows now, the panel's own card under it (the owner, 2026-11-19, T546). 新话题 and 管家 are the
butler's settings in two pages (app/butler_panel.py): 新话题 what every new topic is preselected with (the launch card's
and the butler's alike) and which of the butler's start at once, 管家 the butler's own and the private chat mode. 工作区
is the workspace list with each workspace's 用途 (app/workspace_manager.py), 脱敏 the redaction term list
(app/redaction_panel.py). 代答 is the bot menu's own item again (app/proxy.py): no tab, but a record still on it (an
older card) is drawn there. The page 其他 is retired (2026-11-09): an older card's 其他 tab and buttons land on 工作区
(RETIRED). Small buttons side by side in one row, each as wide as its short label: the current tab solid blue, the others
text buttons, no spacing between them, so they fit in one row in a topic's side panel (about 300px; bordered ones have
a minimum width there and wrapped to two rows, 2026-11-08 screenshots). No select, no confirmation: a tap redraws the
whole card as the other panel. A tap on the tab shown folds the card to its title bar and this row (``folded_hub``);
another unfolds it."""
from __future__ import annotations

from covey.cards.elements import callback_button, folded_panel, menu_item

TAB = 'bridge_settings_tab'
KINDS = frozenset({TAB})
# BUTLER (管家) keeps the key of the one 管家 tab before it (2026-11-08): an older card's 管家 tab is its first page.
BUTLER, TOPIC, WORKSPACE, REDACTION, PROXY, GROUPS = 'main', 'topic', 'workspace', 'redaction', 'proxy', 'groups'
NODES = 'nodes'  # 设备 (app/node_panel.py), only with COVEY_NODE_LISTEN.
OTHER = 'other'  # The retired page 其他 (2026-11-08 to 11-09): an older card's tab and record name it.
RETIRED = {OTHER: WORKSPACE}  # A retired tab -> the tab its click and its record land on.
BUTLER_PAGES = (BUTLER, TOPIC)  # The butler's pages (app/butler_panel.py): 管家 only with it, 新话题 always.
# In the row's order. 代答 last: no tab any more (app/menu_panel.py hub_tabs), only an older record's.
LABELS = {TOPIC: '新话题', BUTLER: '管家', WORKSPACE: '工作区', REDACTION: '脱敏', GROUPS: '群聊', NODES: '设备', PROXY: '代答'}
# Each panel's title (cards/butler_panel.py TITLES, cards/workspace_manager.py, cards/redaction_panel.py,
# cards/proxy_panel.py, cards/group_manager.py) and icon.
TITLES = {BUTLER: '管家', TOPIC: '新话题', WORKSPACE: '工作区', NODES: '设备', REDACTION: '脱敏看板', PROXY: '代答',
          GROUPS: '群聊'}
ICONS = {BUTLER: 'robot_outlined', TOPIC: 'launch', WORKSPACE: 'workspaces', NODES: 'computer_outlined', REDACTION: 'redaction',
         PROXY: 'proxy',
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
