"""The 设置 card's tab row (app/settings_hub.py): 配置 | 仓库 | 脱敏 | 群聊 (| 记忆) (| 设备) | 诊断 at
the top of the body of the panel the card shows now, the panel's own card under it. 配置 is the butler's settings on
one page (app/butler_panel.py): how a new topic (the launch card's and the butler's alike), the butler and 独立复核 run,
models and all, which of the butler's topics start at once, the private chat mode and 子 Agent; the pages 模型, 新话题 and
管家 it took the place of are retired (RETIRED). 仓库
is the home and the repository map, read only (app/workspace_manager.py; the web page's 仓库 page edits it), 脱敏 the redaction term list
(app/redaction_panel.py), 记忆 the owner's memory by scope and what the next turn carries (app/memory_panel.py, only with
COVEY_MEMORY=1), 诊断 service/doctor.py's report with, under it, 能做什么 (what this deployment can do and what it
lacks, app/about_panel.py; the 能做什么 tab is retired into it) (app/doctor_panel.py, read only). 代答 is the bot menu's own item again (app/proxy.py): no
tab, but a record still on it (an
older card) is drawn there. The page 其他 is retired: an older card's 其他 or 工作区 tab lands on 仓库
(RETIRED). Small buttons side by side in one row, each as wide as its short label: the current tab solid blue, the others
text buttons, no spacing between them, so they fit in one row in a topic's side panel (about 300px; bordered ones have
a minimum width there and wrapped to two rows). No select, no confirmation: a tap redraws the
whole card as the other panel. A tap on the tab shown folds the card to its title bar and this row (``folded_hub``);
another unfolds it."""
from __future__ import annotations

from covey.cards.elements import callback_button, folded_panel, menu_item

TAB = 'bridge_settings_tab'
KINDS = frozenset({TAB})
# 配置 (app/butler_panel.py): how a new topic, the butler and 独立复核 run, models and all, on one page.
CONFIG = 'config'
# The butler's pages before 配置, retired into it: 管家 (BUTLER keeps the key of the one 管家 tab before the pages), 新话题
# and 模型. An older card's tab, a record still on one and a button that names one land on 配置. The web page still has
# 新话题 and 管家 (app/web_settings.py), by these keys and LABELS.
BUTLER, TOPIC, MODELS = 'main', 'topic', 'models'
WORKSPACE, REDACTION, PROXY, GROUPS = 'workspace', 'redaction', 'proxy', 'groups'
REPOS = 'repos'  # 仓库 (app/workspace_manager.py): the home and the repository map, in the 工作区 tab's place (WORKSPACE, retired into it).
NODES = 'nodes'  # 设备 (app/node_panel.py), only with COVEY_NODE_LISTEN.
MEMORY = 'memory'  # 记忆 (app/memory_panel.py), only with COVEY_MEMORY=1.
ABOUT = 'about'  # 能做什么 (app/about_panel.py): the lower part of 诊断 now (RETIRED); an older card's tab and record name it.
DOCTOR = 'doctor'  # 诊断 (app/doctor_panel.py): service/doctor.py's report, read-only.
OTHER = 'other'  # The retired page 其他: an older card's tab and record name it.
RETIRED = {OTHER: REPOS, WORKSPACE: REPOS, BUTLER: CONFIG, TOPIC: CONFIG, MODELS: CONFIG, ABOUT: DOCTOR}  # A retired tab -> where it lands.
# The butler's page (app/butler_panel.py) and the retired ones its buttons still act on. 配置 first: a record without a tab.
BUTLER_PAGES = (CONFIG, BUTLER, TOPIC, MODELS)
# In the row's order. 代答 last: no tab any more (app/menu_panel.py hub_tabs), only an older record's.
LABELS = {CONFIG: '配置', MODELS: '模型', TOPIC: '新话题', BUTLER: '管家', REPOS: '仓库', WORKSPACE: '工作区', REDACTION: '脱敏', GROUPS: '群聊', MEMORY: '记忆', NODES: '设备',
          ABOUT: '能做什么', DOCTOR: '诊断', PROXY: '代答'}
# Each panel's title (cards/butler_panel.py TITLES, cards/workspace_manager.py (仓库), cards/redaction_panel.py,
# cards/proxy_panel.py, cards/group_manager.py, cards/memory_panel.py, cards/about_panel.py) and icon.
TITLES = {CONFIG: '配置', MODELS: '模型', BUTLER: '管家', TOPIC: '新话题', REPOS: '仓库', WORKSPACE: '工作区', NODES: '设备', REDACTION: '敏感词脱敏', PROXY: '代答',
          GROUPS: '群聊', MEMORY: '记忆', ABOUT: '能做什么', DOCTOR: '诊断'}
ICONS = {CONFIG: 'robot_outlined', MODELS: 'robot_outlined', BUTLER: 'robot_outlined', TOPIC: 'launch', REPOS: 'workspaces', WORKSPACE: 'workspaces', NODES: 'computer_outlined', REDACTION: 'redaction',
         PROXY: 'proxy',
         GROUPS: 'groups', MEMORY: 'member_outlined', ABOUT: 'setting_outlined', DOCTOR: 'setting_outlined'}
SPACING = '0px'  # Between two tabs: the text buttons' own padding parts them; 8px wrapped 群聊 in a side panel.


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
