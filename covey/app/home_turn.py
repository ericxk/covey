"""The home's part of a turn: the static home block of the rules and what the Harness's environment adds.

``static_block`` is what app/prompts.py bridge_rules puts where the workspace block was, for a topic that started in
the home (core/topics.py ``home_start``): the home, the repo map's path, the topic's outputs directory when
``home.json`` names one, and the rules of working from the home. It never changes within a topic, so the rules stay
one cacheable prefix; what changes turn by turn is app/repos_turn.py's. A topic from before the home gets '' and keeps
its workspace block. ``env`` is what every Harness process gets (core/home.py ``env``: ``COVEY_HOME``, ``COVEY_TOPIC``,
``COVEY_STATE``, ``<数据根>/bin`` first on ``PATH``), and the cache variables in the run's directory when ``home.json``
says ``cache_env`` (docs/configuration.md 主目录与仓库地图).

A topic the butler or a lead opened with ``local`` false (topic.json ``origin.local``, app/butler_topics.py bind_topic)
keeps off this machine's repositories and files: its home block is ``EXTERNAL`` (its outputs directory, the line that it
works from what it was given and from outside, ``covey repo use`` for when the owner names local code), never the repo
map, the home's AGENTS.md or the write command; and its turns carry ``external`` (core/harness.py EXTERNAL), with which
each Harness leaves the home's own instruction files out. Decided once when it opens, so its rules never change. A topic
without the field (every one before it) is local.

With COVEY_DEVICES on, a local topic's home block ends with ``DEVICE_RULE``: how to use another device (core/device_access.py,
``covey device``). The devices themselves are listed at the end of ``repos.md``, read on demand, so a change of the list
changes no rules; an EXTERNAL topic is not told (``covey device`` still works there when the owner names a device).
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

from covey.core import devices, home, repomap, topic_repos
from covey.core.harness import EXTERNAL as EXTERNAL_OPTION
from covey.core.state import read_json
from covey.core.topics import home_start

RULES = ("主目录的规矩：\n"
         "- 不要在主目录根递归搜索（rg、grep -r、find、ls -R、du），先查仓库地图，只搜具体的仓库或子目录。\n"
         "- 进一个仓库干活之前先读它的 AGENTS.md 或 CLAUDE.md，照它的规矩做。\n"
         "- 要改仓库：covey repo use <仓库> --write，在它打印的目录里改（按话题建 worktree，不动仓库的主检出；"
         "地图里写着原地改的除外）。只读：covey repo use <仓库>；本话题用到的：covey repo status。")
# The home block of a topic that keeps off this machine's files (``local``), after its outputs directory's line.
EXTERNAL = ("本话题不用本机的仓库和工作区：只用任务给的材料和外部来源；主人没要求时，不翻本地仓库，也不把结论往本地项目上引。\n"
            "主人点名要看本地代码时，先 covey repo use <仓库>（不知道名字用 covey repo list），按它打印的目录和规范读。\n"
            "不要递归搜索主目录或家目录这类大目录。")
OUTPUTS_LINE = "本话题产物目录：{}（输出、日志、临时文件都放这里，每次运行在它下面另建子目录）"
# The home block's line on devices (core/device_access.py), with COVEY_DEVICES on; never in an EXTERNAL topic's block.
DEVICE_RULE = ("- 要用别的设备：设备和设备上的常用路径在仓库地图末尾，用 covey device（covey device --help 看用法）；"
               "只读设备只能看，不能改。")


def _topic(ctx) -> dict:
    try:
        data = read_json(ctx.history_path.parent / "topic.json")
    except (OSError, ValueError, AttributeError):
        return {}
    return data if isinstance(data, dict) else {}


def local(data) -> bool:
    """Whether a topic (its topic.json ``data``) may use this machine's repositories and files: false only for one opened
    with ``local`` false (``origin.local``, written once when it opened); one without the field is local."""
    origin = data.get("origin") if isinstance(data, dict) else None
    return not (isinstance(origin, dict) and origin.get("local") is False)


def outputs_dir(ctx, data=None) -> str:
    """The topic's outputs directory (core/home.py ``topic_dir``) when ``home.json`` is there; '' else."""
    if not home.configured(ctx.state_dir):
        return ""
    data = _topic(ctx) if data is None else data
    number = data.get("topic_number") if type(data.get("topic_number")) is int else 0
    return home.topic_dir(home.read(ctx.state_dir), home.of(ctx), number=number, topic_id=ctx.topic_id,
                          title=data.get("slug") or "", started=data.get("started_at"))


def static_block(ctx) -> str:
    """The home block of ``ctx``'s rules: '' for a topic that did not start in the home, or one on a device (its
    workspace block, as before; app/prompts.py home_section)."""
    if getattr(ctx, "device", ""):
        return ""
    data = _topic(ctx)
    if not home_start(data):
        return ""
    if not local(data):  # Its outputs directory and EXTERNAL: no repo map, no home AGENTS.md, no write command.
        return OUTPUTS_LINE.format(outputs_of(ctx, data)) + "\n" + EXTERNAL
    where = home.of(ctx)
    rendered = repomap.render(ctx.state_dir, where) or repomap.rendered_path(ctx.state_dir, where)
    lines = [f"主目录：{where}（本话题从这里启动，先遵守这里的 AGENTS.md）",
             f"仓库地图：{rendered}（要动的代码先在地图里找仓库）"]
    outputs = outputs_dir(ctx, data)
    if outputs:
        lines.append(OUTPUTS_LINE.format(outputs))
    # The switch, not the list: the line stays the same while devices.json changes, so the rules do too.
    return "\n".join(lines) + "\n" + RULES + ("\n" + DEVICE_RULE if devices.enabled() else "")


def outputs_of(ctx, data=None) -> str:
    """The topic's outputs directory: the home's (``outputs_dir``), else ``<state>/topics/<id>/outputs`` (as
    app/tool_limits.py outputs_of)."""
    found = outputs_dir(ctx, data)
    return found or str(Path(ctx.state_dir) / "topics" / (ctx.topic_id or "topic") / "outputs")


def external(ctx, data=None, *, make=True) -> dict:
    """The ``options[EXTERNAL]`` (core/harness.py) of a turn of ``ctx``'s topic when it keeps off this machine's files: it
    started in the home with ``local`` false, on this machine. {"home": the home, "dir": its outputs directory (made, with
    ``make``; '' when it cannot be)}; {} for any other topic, and on a device (its block is the device's workspace)."""
    if getattr(ctx, "device", ""):
        return {}
    data = _topic(ctx) if data is None else data
    if not home_start(data) or local(data):
        return {}
    folder = outputs_of(ctx, data)
    if make:
        try:
            Path(folder).mkdir(parents=True, exist_ok=True)
        except OSError:
            folder = ""
    return {"home": str(home.of(ctx)), "dir": folder}


def isolated(ctx):
    """``ctx`` with ``external`` in its options when its topic keeps off this machine's files; ``ctx`` itself otherwise."""
    found = external(ctx)
    return dataclasses.replace(ctx, options={**ctx.options, EXTERNAL_OPTION: found}) if found else ctx


def env(ctx) -> dict:
    """The variables the Harness process of ``ctx``'s turn gets on top of the Bridge's (core/home.py ``env``), with the
    cache variables of the topic's run directory when ``home.json`` says ``cache_env`` and the directory is writable."""
    found = home.env(ctx)
    if not found or not home.configured(ctx.state_dir):
        return found
    config = home.read(ctx.state_dir)
    if not config.cache_env:
        return found
    data = _topic(ctx)
    if not home_start(data):
        return found
    number = data.get("topic_number") if type(data.get("topic_number")) is int else 0
    task = topic_repos.task_dir_of(ctx.state_dir)
    run = home.run_dir_for(config, home.of(ctx), number=number, topic_id=ctx.topic_id, title=data.get("slug") or "",
                           started=data.get("started_at"), task_key=task.name if task else "")
    return {**found, **home.cache_env(run)}
