"""The 设置 card's 能做什么 tab (cards/about_panel.py): what this deployment can do and what it lacks, read only (no
button, no model, no Feishu call). Shown always, so a colleague who did not install it can see what it has.

What it reads, here and now, through each feature's own switch function (never a copy of the variable's rule): the
Harnesses this Bridge enables and those its availability watch found unavailable (app/welcome.py ``harnesses``), the
features of FEATURES, each on or off with one line (on: what it does and the switch it reads; off: how to get it, e.g.
管家用的 Harness 没启用 → 没有管家, app/welcome.py ``butler_state``), and the enabled extension packages (core/extensions.py).
docs/configuration.md 开关速查 is the same list for the deployer.

The tab's record is the 设置 card's (app/settings_hub.py, app/menu_panel.py); its view holds no facts (they are read at
each draw), so a tap on the tab (it folds, then unfolds afresh) shows the deployment as it is then.
"""
from __future__ import annotations

import platform

from covey.app import welcome
from covey.app.menu_panel import MenuPanel, hub_top
from covey.app.overview import Overview
from covey.cards.about_panel import about_card
from covey.cards.question_cards import STANDING_ENV, standing_enabled
from covey.cards.settings_hub import ABOUT
from covey.cards.welcome import BUTLER_WORDS
from covey.core import extensions, knowledge, memory, nodes, patrol, project_proposals, projects
from covey.core import butler_settings, butler_tracks

OWNER_ONLY = '⚠️ 仅机器人主人可以看这一页。'
CLAUDE = "claude"  # The Harness the butler, the small model's judgments and redaction need.


def _digest(probe):
    from covey.app.digest import ENV, configured
    times = configured()
    if not times:
        return False, "设 COVEY_DIGEST_TIMES=09:30,21:30（按北京时间）", ENV
    words = "、".join(f"{minute // 60:02d}:{minute % 60:02d}" for minute in times)
    return True, f"每天 {words} 在私聊发汇总卡；菜单有「看板」", ENV


def _butler(probe):
    state = welcome.butler_state(probe.apps, butler_settings.load(probe.state_dir).butler_harness)
    if not state:
        return True, "私聊顶层的话先交给管家，能答的直接答，要做的开话题", "COVEY_BUTLER"
    why, how = BUTLER_WORDS[state]
    return False, f"{why}；{how}", "COVEY_BUTLER"


def _tracks(probe):
    if butler_tracks.enabled() and not welcome.butler_state(probe.apps, butler_settings.load(probe.state_dir).butler_harness):
        return True, "管家在做一件事时，你的下一句可以并进去或另起一件", butler_tracks.ENV
    if butler_tracks.enabled():
        return False, "开着，但要先有管家", butler_tracks.ENV
    return False, "设 COVEY_BUTLER_TRACKS=1（要有管家）", butler_tracks.ENV


def _projects(probe):
    if projects.enabled():
        return True, "要分几个话题、跨几天做的事开成专项，有负责人跟进", projects.ENV
    return False, "设 COVEY_PROJECTS=1", projects.ENV


def _tidy(probe):
    if not project_proposals.enabled():
        return False, ("设 COVEY_PROJECT_TIDY=suggest" if projects.enabled()
                       else "先开专项（COVEY_PROJECTS=1），再设 COVEY_PROJECT_TIDY=suggest"), project_proposals.ENV
    if CLAUDE not in probe.apps:
        return False, "开着，但要 Claude Code 来判断，这台机器没启用它", project_proposals.ENV
    return True, "话题多了会提议建专项、归进专项，你点了才做", project_proposals.ENV


def _memory(probe):
    if memory.enabled():
        return True, "你在话题里说的长期要求记下来，每轮带上；设置卡有「记忆」", memory.ENV
    return False, "设 COVEY_MEMORY=1", memory.ENV


def _knowledge(probe):
    if knowledge.enabled():
        return True, "说“记一下”的事记成条目，新话题都带上", knowledge.ENV
    return False, "设 COVEY_KNOWLEDGE=1", knowledge.ENV


def _history(probe):
    if knowledge.history_cmd():
        return True, "管家和你的话题能搜以前做过的事", knowledge.HISTORY_ENV
    return False, "设 COVEY_HISTORY_CMD=搜索命令的绝对路径（命令不在本仓库）", knowledge.HISTORY_ENV


def _gists(probe):
    from covey.app import workspace_gists
    count = workspace_gists.count()
    if count and project_proposals.enabled():
        return True, f"同一工作区新开的话题带上最近 {count} 个话题的要点", workspace_gists.ENV
    if count:
        return False, "开着，但话题要点要整理专项开着才会写", workspace_gists.ENV
    return False, "设 COVEY_WORKSPACE_GISTS=3（话题要点要整理专项开着才会写）", workspace_gists.ENV


def _overview(probe):
    from covey.app import pins
    if not Overview.enabled():
        return False, "设 COVEY_PINNED_OVERVIEW=1", "COVEY_PINNED_OVERVIEW"
    if pins.laid():
        return True, "私聊置顶「手上的事」，等你、在做、有结果的话题也自动 Pin 上", "COVEY_PINNED_TOPICS"
    return True, "私聊置顶一张「手上的事」；再设 COVEY_PINNED_TOPICS=1 话题也自动 Pin 上", "COVEY_PINNED_OVERVIEW"


def _dispatch(probe):
    from covey.app import dispatch_cards
    if dispatch_cards.enabled():
        return True, "管家把事交给话题后，私聊多一张实时刷新的卡", dispatch_cards.ENV
    return False, "设 COVEY_DISPATCH_CARD=1", dispatch_cards.ENV


def _standing(probe):
    if standing_enabled():
        return True, "每题都有推荐项的问题卡下面有这个按钮，点了记成原则", STANDING_ENV
    return False, "设 COVEY_STANDING_ANSWERS=1", STANDING_ENV


def _redaction(probe):
    if CLAUDE in probe.apps:
        return True, "话题开了脱敏才替换，只支持 Claude 的模型；词表在「脱敏」", ""
    return False, "要 Claude Code：这台机器没启用它", ""


def _proxy(probe):
    from covey.app import proxy_send
    from covey.core import proxy as records
    if not proxy_send.available():
        return False, "要一个带代答发送器的扩展（核心没有），再设 COVEY_PROXY_PROFILE", "COVEY_PROXY_PROFILE"
    try:
        on = bool(records.read_settings(probe.state_dir).get("enabled"))
    except (OSError, ValueError, AttributeError):
        on = False
    if on:
        return True, "替你回白名单里的人；菜单「代答」管", "COVEY_PROXY_PROFILE"
    return False, "有发送器：在菜单「代答」里开", "COVEY_PROXY_PROFILE"


def _web(probe):
    from covey.core import web_sync
    if web_sync.enabled():
        return True, "网页上能看私聊里的话题、专项和设置", web_sync.ENV
    return False, "要一个配套的网页应用，再设 COVEY_WEB=1 和 COVEY_WEB_CONFIG", web_sync.ENV


def _nodes(probe):
    if nodes.enabled():
        return True, "设备连进来报机器状况，内存、盘满、失联会告警；设置卡有「设备」", nodes.LISTEN_ENV
    return False, "设 COVEY_NODE_LISTEN=设备能连进来的监听地址", nodes.LISTEN_ENV


def _patrol(probe):
    if patrol.enabled():
        return True, "只记不打扰：管家本来会提醒你的事，汇总里多一块", patrol.ENV
    return False, "设 COVEY_PATROL=shadow（只记不打扰）", patrol.ENV


# (name, check): ``check(probe)`` is (on, the line, the switch it reads). In the order the deployer meets them
# (docs/configuration.md 开关速查): what everyone has first.
FEATURES = (
    ("管家", _butler), ("同时做几件事", _tracks), ("早晚汇总和看板", _digest), ("手上的事", _overview),
    ("专项", _projects), ("整理专项", _tidy), ("记忆", _memory), ("知识条目", _knowledge), ("历史搜索", _history),
    ("新话题带上最近话题的要点", _gists), ("以后这类按推荐走", _standing), ("派发卡", _dispatch), ("敏感词脱敏", _redaction),
    ("代答", _proxy), ("网页工作台", _web), ("设备与机器状况", _nodes), ("我本来想找你的事", _patrol),
)


class Probe:
    """What the checks read: ``state_dir``, ``apps`` (the Harness apps by name) and ``runtime``."""

    def __init__(self, state_dir, apps, runtime):
        self.state_dir, self.apps, self.runtime = state_dir, apps, runtime


def features(probe) -> list:
    """[{name, on, line, switch}] of FEATURES; a check that fails says so on its line instead of losing the tab."""
    rows = []
    for name, check in FEATURES:
        try:
            on, line, switch = check(probe)
        except Exception as exc:  # noqa: BLE001 - one feature's reading must not lose the page
            on, line, switch = False, f"读不出来（{type(exc).__name__}）", ""
        rows.append({"name": name, "on": bool(on), "line": line, "switch": switch})
    return rows


def facts(state_dir, apps, runtime) -> dict:
    """The tab's view now (cards/about_panel.py): the machine, the Harnesses, the features and the extensions."""
    titles, down = welcome.harnesses(apps, runtime)
    try:
        packages = extensions.enabled()
    except extensions.ExtensionError:
        packages = []
    return {"host": platform.node(), "harnesses": titles, "down": down,
            "features": features(Probe(state_dir, apps, runtime)), "extensions": list(packages)}


class AboutPanel:
    """``topics``: the Bridge's Topics (its Harness apps)."""

    def __init__(self, runtime, messenger, voice, topics):
        self.runtime, self.messenger, self.voice, self.topics = runtime, messenger, voice, topics
        self.menu = MenuPanel(self, 'about-panel', 'about-cards', 'about-card', tab=ABOUT)

    def path(self, ctx, token):
        return self.menu.path(ctx, token)

    @staticmethod
    def tab_view(token):
        return {'token': token, 'revision': 1, 'hub': True}

    @staticmethod
    def tab_fields(ctx):
        return {}

    def tab_card(self, ctx, token, record):
        return self.draw(ctx, token, record['view'])

    def tab_shown(self, ctx, token):
        self.runtime.log(f'about tab shown token={token[:8]}')

    def refuse(self, ctx, path, event):
        return self.menu.refuse(ctx, path, event, OWNER_ONLY, lambda view, record: self.draw(ctx, path.stem, view))

    def draw(self, ctx, token, view):
        found = facts(ctx.state_dir, self.topics.apps, self.runtime)
        return about_card(token, {**found, 'notice': view.get('notice') or ''}, top=hub_top(ABOUT, token, view))
