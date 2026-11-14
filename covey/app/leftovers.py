"""After a turn's backend has closed, the processes its commands left running (core/task_processes.py): the
search tools are ended, the rest are named on the task card for the user, as a line of the Bridge's own."""
from __future__ import annotations

import datetime as _dt
import time
from pathlib import Path

from covey.core import env as cenv
from covey.cards.trace_view import BRIDGE_NOTE
from covey.core.task_processes import TASK_DIR_ENV, stop_leftovers

SHOWN = 5


def _listing(items) -> str:
    shown = [f"{item.name}（PID {item.pid}，{item.rss / 2 ** 30:.1f} GiB）{item.command}" for item in items[:SHOWN]]
    return "；".join(shown) + (f"；等共 {len(items)} 个" if len(items) > SHOWN else "")


def leftover_note(stopped, running, at: str) -> str:
    """The card's line for ``stopped`` search tools and other processes still ``running``."""
    lines = []
    if stopped:
        pending = sum(1 for item in stopped if item.outcome != "stopped")
        lines.append(f"机器人 {at} 结束了本轮命令留下的 {len(stopped)} 个搜索进程：{_listing(stopped)}"
                     + (f"。其中 {pending} 个还没退出，可能卡在 NAS 读写上" if pending else "") + "。")
    if running:
        lines.append(("" if stopped else f"机器人 {at} 检查：")
                     + f"本轮命令留下的 {len(running)} 个进程仍在运行，没有处理：{_listing(running)}。")
    return "\n".join(lines)


def stop_task_leftovers(runtime, interaction, ctx, job) -> None:
    """Only in a task runner, which marks its commands (app/runner.py): a job the Bridge runs in its own process
    (an app built without arguments, in tests) has no marker. Never the turn's failure."""
    run_dir = cenv.get(TASK_DIR_ENV)
    if not run_dir:
        return
    try:
        stopped, running = stop_leftovers(Path(run_dir))
    except Exception as exc:  # noqa: BLE001 - a check after the turn; its text may name paths
        runtime.log(f"task leftovers not checked: {type(exc).__name__}")
        return
    if not stopped and not running:
        return
    runtime.log(f"task leftovers job=#{job.job_id} stopped={sum(item.outcome == 'stopped' for item in stopped)} "
                f"pending={sum(item.outcome != 'stopped' for item in stopped)} running={len(running)} "
                f"pids={','.join(str(item.pid) for item in [*stopped, *running][:20])}")
    if job.tool_trace is None:
        return
    at = _dt.datetime.now().astimezone().strftime("%H:%M")
    try:
        interaction.structured_harness_event(ctx, job, "progress_note", {
            "id": f"bridge-leftovers-{time.time_ns()}", "title": BRIDGE_NOTE,
            "text": leftover_note(stopped, running, at)}, None, from_backend=False)
    except Exception as exc:  # noqa: BLE001 - the processes are handled; only the card line failed
        runtime.log(f"task leftovers not shown: {type(exc).__name__}")
