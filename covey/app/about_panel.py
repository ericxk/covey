"""The 设置 card's 能做什么 part (cards/about_panel.py): what this deployment can do and what it lacks, read only (no
button, no model, no Feishu call). Shown always, under the report of the 诊断 tab (app/doctor_panel.py; the 能做什么 tab is
retired into 诊断, cards/settings_hub.py RETIRED), so a colleague who did not install it can see what it has.

What it reads, here and now, through each feature's own switch function (never a copy of the variable's rule): the
Harnesses this Bridge enables and those its availability watch found unavailable (app/welcome.py ``harnesses``), the
features of FEATURES and the enabled extension packages (core/extensions.py). FEATURES are in three groups by what the
owner does with them (GROUPS, T647 D6): 交给它办事, 不断、不丢, 看得全、记得住; each says first what it does for him, then,
on, the switch it reads, off, how to get it (管家用的 Harness 没启用 → 没有管家, app/welcome.py ``butler_state``). What is on
in every deployment (a task in a process of its own, 换配置 with its handoff) and what ./install turns on (core/switches.py
INSTALL: 看板, 独立复核, 到点回来看, 记忆, the project's four) are listed with the rest. docs/configuration.md 开关速查 is the same
list for the deployer.

It has no record of its own: ``elements`` reads the facts at each draw of the 诊断 tab, so a tap on the tab (it folds,
then unfolds afresh) shows the deployment as it is then.
"""
from __future__ import annotations

from types import SimpleNamespace

import platform

from covey.app import butler_harness, welcome
from covey.app.overview import Overview
from covey.cards.about_panel import about_elements
from covey.cards.welcome import BUTLER_WORDS
from covey.core import extensions, knowledge, memory, nodes, project_proposals, projects
from covey.core import butler_settings, butler_tracks

OWNER_ONLY = '⚠️ 仅机器人主人可以看这一页。'
CLAUDE = "claude"  # The Harness redaction and 回退 need (the butler and the small model's judgments run on others too).
WORK, SAFE, SEE = "work", "safe", "see"
GROUPS = ((WORK, "交给它办事"), (SAFE, "不断、不丢"), (SEE, "看得全、记得住"))


def _butler_state(probe) -> str:
    """welcome.butler_state on the Harness the butler runs on (app/butler_harness.py name_of)."""
    return welcome.butler_state(probe.apps, butler_harness.name_of(butler_settings.load(probe.state_dir), probe.apps))


def _butler(probe):
    state = _butler_state(probe)
    if not state:
        return True, "", "COVEY_BUTLER"
    why, how = BUTLER_WORDS[state]
    return False, f"{why}；{how}", "COVEY_BUTLER"


def _tracks(probe):
    if butler_tracks.enabled() and not _butler_state(probe):
        return True, "", butler_tracks.ENV
    if butler_tracks.enabled():
        return False, "开着，但要先有管家", butler_tracks.ENV
    return False, "设 COVEY_BUTLER_TRACKS=1（要有管家）", butler_tracks.ENV


def _projects(probe):
    if projects.enabled():
        return True, "", projects.ENV
    return False, "设 COVEY_PROJECTS=1", projects.ENV


def _push(probe):
    """The project's four (./install turns them on, core/switches.py INSTALL): on when all are, else which are not."""
    from covey.core import triage
    names = (("COVEY_PROJECT_AUTOWAKE", projects.autowake_enabled), ("COVEY_PROJECT_ASK", projects.ask_enabled),
             ("COVEY_PROJECT_MEMORY", projects.memory_enabled), ("COVEY_PROJECT_TRIAGE", triage.enabled))
    if not projects.enabled():
        return False, "先开专项（COVEY_PROJECTS=1），再设这四个开关为 1：" + "、".join(name for name, _ in names), ""
    off = [name for name, on in names if not on()]
    if not off:
        return True, "", "COVEY_PROJECT_AUTOWAKE 等四个"
    if len(off) < len(names):
        return True, "还没开：" + "、".join(off), "COVEY_PROJECT_AUTOWAKE 等四个"
    return False, "设 " + "、".join(f"{name}=1" for name in off), ""


def _tidy(probe):
    if not project_proposals.enabled():
        return False, ("设 COVEY_PROJECT_TIDY=suggest" if projects.enabled()
                       else "先开专项（COVEY_PROJECTS=1），再设 COVEY_PROJECT_TIDY=suggest"), project_proposals.ENV
    from covey.app import assist
    ok, why = assist.available(SimpleNamespace(apps=probe.apps, default=getattr(probe, "default", ""),
                                               runtime=getattr(probe, "runtime", None)), state=probe.state_dir)
    if not ok:
        return False, "开着，但" + why, project_proposals.ENV
    return True, "", project_proposals.ENV


def _revisit(probe):
    from covey.core import revisits
    return (True, "", revisits.SWITCH) if revisits.enabled() else (False, "设 COVEY_REVISIT=1", revisits.SWITCH)


def _review(probe):
    from covey.app import review
    return (True, "", review.ENV) if review.enabled() else (False, "设 COVEY_REVIEW=1", review.ENV)


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
        return True, "菜单「代答」管", "COVEY_PROXY_PROFILE"
    return False, "有发送器：在菜单「代答」里开", "COVEY_PROXY_PROFILE"


def _always(probe):
    """On in every deployment: no switch."""
    return True, "", ""


def _rewind(probe):
    if CLAUDE in probe.apps:
        return True, "", ""
    return False, "要 Claude Code：这台机器没启用它", ""


def _retry(probe):
    from covey.app import auto_retry
    if auto_retry.long_ladder():
        return True, "", auto_retry.SWITCH
    return False, "设回 COVEY_AUTO_RETRY_LONG=1（现在限流只重试两次，临时故障不重试）", auto_retry.SWITCH


def _source_watch(probe):
    from covey.app import source_watch
    if source_watch.enabled():
        return True, "", source_watch.SWITCH
    return False, "设回 COVEY_SOURCE_WATCH=1", source_watch.SWITCH


def _steer(probe):
    from covey.core import deliveries
    if deliveries.topic_steer():
        return True, "", deliveries.TOPIC_STEER
    return False, "设回 COVEY_TOPIC_STEER=1（现在排成下一轮）", deliveries.TOPIC_STEER


def _redaction(probe):
    if CLAUDE in probe.apps:
        return True, "词表在「脱敏」，话题开了才替换", ""
    return False, "要 Claude Code：这台机器没启用它", ""


def _nodes(probe):
    if nodes.enabled():
        return True, "", nodes.LISTEN_ENV
    return False, "设 COVEY_NODE_LISTEN=设备能连进来的监听地址", nodes.LISTEN_ENV


def _digest(probe):
    from covey.app.digest import ENV, configured
    times = configured()
    if not times:
        return False, "设 COVEY_DIGEST_TIMES=09:30,21:30（按北京时间）", ENV
    words = "、".join(f"{minute // 60:02d}:{minute % 60:02d}" for minute in times)
    return True, f"每天 {words}", ENV


def _overview(probe):
    from covey.app import pins
    if not Overview.enabled():
        return False, "设 COVEY_PINNED_OVERVIEW=1，再设 COVEY_PINNED_TOPICS=1 话题也自动 Pin 上", "COVEY_PINNED_OVERVIEW"
    if pins.laid():
        return True, "", "COVEY_PINNED_TOPICS"
    return True, "再设 COVEY_PINNED_TOPICS=1 话题也自动 Pin 上", "COVEY_PINNED_OVERVIEW"


def _dispatch(probe):
    from covey.app import dispatch_cards
    if dispatch_cards.enabled():
        return True, "", dispatch_cards.ENV
    return False, "设 COVEY_DISPATCH_CARD=1", dispatch_cards.ENV


def _memory(probe):
    """记忆, one word for what the owner asked to be kept (docs/glossary.md): his lasting asks (COVEY_MEMORY) and what he says
    记一下 of (COVEY_KNOWLEDGE, the knowledge entries)."""
    asks, notes = memory.enabled(), knowledge.enabled()
    if asks and notes:
        return True, "", f"{memory.ENV}、{knowledge.ENV}"
    if asks:
        return True, "“记一下”的事还没开：设 COVEY_KNOWLEDGE=1", memory.ENV
    if notes:
        return True, "长期要求还没开：设 COVEY_MEMORY=1", knowledge.ENV
    return False, "设 COVEY_MEMORY=1 和 COVEY_KNOWLEDGE=1", f"{memory.ENV}、{knowledge.ENV}"


def _history(probe):
    if knowledge.history_cmd():
        return True, "", knowledge.HISTORY_ENV
    return False, "设 COVEY_HISTORY_CMD=搜索命令的绝对路径（命令不在本仓库）", knowledge.HISTORY_ENV


def _gists(probe):
    from covey.app import workspace_gists
    count = workspace_gists.count()
    if count and project_proposals.enabled():
        return True, f"最近 {count} 个", workspace_gists.ENV
    if count:
        return False, "开着，但话题要点要整理专项开着才会写", workspace_gists.ENV
    return False, "设 COVEY_WORKSPACE_GISTS=3（话题要点要整理专项开着才会写）", workspace_gists.ENV


def _web(probe):
    from covey.core import web_sync
    if web_sync.enabled():
        return True, "", web_sync.ENV
    return False, "要一个配套的网页应用，再设 COVEY_WEB=1 和 COVEY_WEB_CONFIG", web_sync.ENV


# (name, group, what it does for the owner, check): ``check(probe)`` is (on, a line: on, a detail of it, else how to get it;
# the switch it reads, '' for none). In each group what everyone has first, as the deployer meets them (docs/configuration.md
# 开关速查).
FEATURES = (
    ("管家", WORK, "私聊里说一句就有人接：能答的直接答，要做的开成话题去做", _butler),
    ("同时做几件事", WORK, "管家在忙一件事时，你的下一句也马上有人接", _tracks),
    ("专项", WORK, "跨几天、要拆成几个话题的事，交给一位负责人拆、派、跟进、收尾", _projects),
    ("负责人自己往下推", WORK, "话题做完一轮就叫醒负责人接着派，停住了来问你，能答的选择题先替你答", _push),
    ("整理专项", WORK, "话题多了提议建专项、归进专项，你点了才做", _tidy),
    ("到点回来看", WORK, "要等 CI、训练、部署的话题，Covey 按约定的时间自动回看，不用你盯", _revisit),
    ("独立复核", WORK, "改了代码的一轮，点一下就换一家的模型只读复核", _review),
    ("代答", WORK, "替你回白名单里的人", _proxy),
    ("重启不打断", SAFE, "任务在自己的进程里跑，机器人重启、升级都不打断在跑的任务", _always),
    ("换配置不断上下文", SAFE, "话题做到一半换 Harness、模型或工作区，原模型写交接摘要给新模型接着做", _always),
    ("回退一步", SAFE, "Claude Code 的话题能退回上一轮，对话和改过的文件一起退", _rewind),
    ("限流后自动继续", SAFE, "碰上限流或临时故障，到点自动接着做，任务卡上写着几点继续", _retry),
    ("撤回和编辑跟着变", SAFE, "撤回一条消息，它开的任务跟着停；编辑了，任务收到改后的话", _source_watch),
    ("在跑时补一句", SAFE, "任务在跑时在话题里补一句，直接送进去，不另开一轮", _steer),
    ("敏感词脱敏", SAFE, "发给模型服务前换掉词表里的词，只支持 Claude 的模型", _redaction),
    ("设备与机器状况", SAFE, "设备连进来报机器状况，内存紧、盘满、失联会告警", _nodes),
    ("看板", SEE, "菜单「看板」随时看等你、停住了、在做的事", _always),
    ("早晚汇总", SEE, "到点在私聊发一张汇总卡", _digest),
    ("手上的事", SEE, "私聊置顶一张「手上的事」，等你、在做、有结果的话题也能自动 Pin 上", _overview),
    ("派发卡", SEE, "管家把事转给已有话题后，你的提问下面挂一张实时刷新的卡", _dispatch),
    ("记忆", SEE, "你说过的长期要求、让它“记一下”的事，以后每轮都带上；设置卡「记忆」里看、删、升为全局", _memory),
    ("历史搜索", SEE, "管家和话题能搜以前做过的事", _history),
    ("新话题带上最近话题的要点", SEE, "同一工作区新开的话题，先知道最近几个话题在做什么", _gists),
    ("网页工作台", SEE, "网页上看私聊里的话题、专项和设置", _web),
)


class Probe:
    """What the checks read: ``state_dir``, ``apps`` (the Harness apps by name) and ``runtime``."""

    def __init__(self, state_dir, apps, runtime):
        self.state_dir, self.apps, self.runtime = state_dir, apps, runtime


def features(probe) -> list:
    """[{name, group, good, on, line, switch}] of FEATURES; a check that fails says so on its line instead of losing the
    tab."""
    rows = []
    for name, group, good, check in FEATURES:
        try:
            on, line, switch = check(probe)
        except Exception as exc:  # noqa: BLE001 - one feature's reading must not lose the page
            on, line, switch = False, f"读不出来（{type(exc).__name__}）", ""
        rows.append({"name": name, "group": group, "good": good, "on": bool(on), "line": line, "switch": switch})
    return rows


def facts(state_dir, apps, runtime) -> dict:
    """The tab's view now (cards/about_panel.py): the machine, the Harnesses, the groups and their features, the
    extensions and the state directory the switches' command names."""
    titles, down = welcome.harnesses(apps, runtime)
    try:
        packages = extensions.enabled()
    except extensions.ExtensionError:
        packages = []
    return {"host": platform.node(), "harnesses": titles, "down": down, "groups": [list(group) for group in GROUPS],
            "features": features(Probe(state_dir, apps, runtime)), "extensions": list(packages),
            "state_dir": str(state_dir)}


class AboutPanel:
    """``topics``: the Bridge's Topics (its Harness apps)."""

    def __init__(self, runtime, messenger, voice, topics):
        self.runtime, self.messenger, self.voice, self.topics = runtime, messenger, voice, topics

    def elements(self, ctx):
        """The part's card elements under the 诊断 report, from the deployment as it is now."""
        found = facts(ctx.state_dir, self.topics.apps, self.runtime)
        return about_elements(found, heading=True)
