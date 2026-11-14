"""The task card's status line as pure functions of what one TaskMonitor tick saw (core/longtask.py): ``Facts`` in,
``status_text`` out, with the words for the current step (step_text) and the trace's newest progress (trace_progress)."""

from __future__ import annotations

from typing import NamedTuple

from covey.core.checkpoint import current_step, step_words
from covey.core.metrics import clock_text, duration, step_age, took_text
from covey.core.text import safe_text, step_event, step_label
from covey.core.turn_words import UNSET_PHASES


THINKING = "模型思考或生成回复中"
# The backend says the model began reasoning that streams no text (Codex): at work, nothing to show yet.
REASONING = "模型思考中"
# No sign from the model yet. "等待模型开始输出" read as if nothing were happening while Codex
# reasoned for 29 s under it (S28 #850).
WAITING_FOR_MODEL = "已提交给模型，等待首个输出"
# A backend that says when its first model call starts (``model_requests``) is still setting up
# before it: one SDK took about 8 s (S29 #863). Formatted with the backend's name.
STARTING = "正在启动 {}…"
BACKEND_ALIVE = "后端进程仍在运行"
# A backend that exited normally: the worker is taking its result, and the outcome follows.
COLLECTING = "正在整理本轮结果"
# Progress more recent than this goes without its age on the status line ("最近进展 1 秒前" said nothing).
QUIET_SHOWN = 10


def step_text(steps, times, wall):
    """(the status line's words for the checkpoint's ``steps``, whether a step is under way): "第 3/5 步：<title> ·
    本步 40 分钟" with the time since the Bridge first saw it doing (``times``, core/checkpoint.py clock_steps), else
    step_words's; ('', False) without steps."""
    doing = current_step(steps)
    words = step_words(steps)
    seen = times.get(doing[1]["title"]) if doing is not None and isinstance(times, dict) else None
    started = seen.get("started") if isinstance(seen, dict) else None
    if isinstance(started, (int, float)) and not isinstance(started, bool):
        words += f" · 本步 {step_age(wall - started)}"
    return words, doing is not None


AGENT_STATES = {"completed": "已完成", "errored": "出错", "interrupted": "已中断", "cancelled": "已中断"}


def trace_progress(trace, now):
    """Current activity and the newest real progress anywhere in the trace tree.

    Progress is a step start or finish, a note or a sub-task state change, of the main task or
    any sub-task; a running process alone is not progress. Returns (current, stamp, phrase):
    stamp is None when no timestamped progress exists, phrase is None for main-task steps."""

    def plausible(stamp):
        return isinstance(stamp, (int, float)) and not isinstance(stamp, bool) and -60 <= now - stamp < 7 * 86400

    # A nested sub-task's trace is linked from the root and from its parent; visit it once.
    nodes, agents, seen = [(trace, None)], {}, {id(trace)}
    for node, _owner in nodes:
        agents.update(node.agents)
        for sid, child in node.children.items():
            if id(child) not in seen:
                seen.add(id(child))
                nodes.append((child, sid))
    running = [(sid, agent) for sid, agent in trace.agents.items() if agent.get("status") in {"running", "pendingInit"}]
    main = [step for key, step in trace.steps.items()
            if step.get("status") == "running" and step.get("agent_id") is None and key not in agents]
    focus = None
    if main:
        current = step_label(max(main, key=lambda step: step.get("started") if plausible(step.get("started")) else 0))
    elif len(running) == 1:
        focus, agent = running[0]
        current = (f"子任务「{safe_text(agent['name'], 30)}」" if agent.get("name") else "子任务") + "运行中"
    elif running:
        current = f"{len(running)} 个子任务运行中"
    else:
        current = THINKING

    def who(sid):
        name = (agents.get(sid) or {}).get("name")
        return "子任务" if sid == focus or not name else f"子任务「{safe_text(name, 30)}」"
    newest = (None, None)
    for sid, agent in agents.items():
        for stamp, state in ((agent.get("started_at"), "已启动"), (agent.get("ended_at"), AGENT_STATES.get(agent.get("status"), "已结束"))):
            if plausible(stamp) and (newest[0] is None or stamp >= newest[0]):
                newest = (stamp, who(sid) + state)
    for node, owner in nodes:
        for key, step in node.steps.items():
            if step.get("agent_id") is not None or key in agents:
                continue  # A sub-task's own row; its state changes are counted above.
            stamp = step.get("ended") if plausible(step.get("ended")) else step.get("started")
            if plausible(stamp) and (newest[0] is None or stamp >= newest[0]):
                newest = (stamp, None if owner is None else who(owner) + step_event(step))
    return current, newest[0], newest[1]


class Facts(NamedTuple):
    """What one TaskMonitor.tick saw: all the status line is made of (``status_text``)."""
    elapsed: float
    quiet: float  # Seconds since the last progress.
    alive: bool | None  # The backend process runs; None without one.
    finished: bool  # It exited normally.
    phase: str
    last_event: str
    checked: str  # The local time of this check, "11-05 14:03:09".
    question: str  # The checkpoint's question.
    waiting: bool  # The checkpoint waits for the user's answer to it.
    native_question_active: bool
    activity: tuple | None  # trace_activity's (current, newest, phrase); None without a trace.
    traced: bool  # The job has a trace (TaskMonitor.trace_activity).
    wall: float
    stirred: bool  # Some progress, streamed output, a reasoning start or a model request was seen.
    retry: dict | None  # The backend's model-request retry (job.backend_retry).
    epoch: float  # time.time() for the retry's times.
    long_mode: bool  # The update after this tick is a long task's.
    frame: int
    steps: str = ""  # The checkpoint's steps (``step_text``), '' without.
    step_doing: bool = False  # One of them is under way: ``steps`` stands for the phase.


def plain_summary(facts):
    """Phase, last progress and process health, for a task without a trace."""
    health = (
        BACKEND_ALIVE if facts.alive is True else COLLECTING if facts.finished
        else ("后端进程已退出" if facts.alive is False else "未取得后端进程状态")
    )
    summary = (
        f"执行中 · 已运行 {took_text(facts.elapsed)}\n当前阶段：{facts.phase}\n"
        + (f"当前步骤：{facts.steps}\n" if facts.steps else "") +
        f"最近进展：{duration(facts.quiet)}前 · {facts.last_event}\n"
        f"最近检查：{facts.checked} · {health}"
    )
    if facts.quiet >= 900:
        summary += "\n较长时间没有新进展，可能正在等待外部任务；尚不能据此判定失败。"
    return summary


def trace_summary(facts):
    """What the trace says the task is doing now and when it last progressed."""
    current, newest, phrase = facts.activity
    quiet = facts.quiet
    summary = f"执行中 · 已运行 {took_text(facts.elapsed)}"
    # The step under way says where the task is better than its phase, and keeps the line one line (2026-11-08).
    if facts.phase not in UNSET_PHASES and not facts.step_doing:
        summary += f" · 阶段：{facts.phase}"
    if facts.steps:
        summary += f" · {facts.steps}"
    if facts.finished:
        # S27 #843 read "等待模型开始输出 · … 后端进程已退出，正在收尾" for the second before its ✅ 已完成.
        return summary + "\n当前：" + COLLECTING
    # The latest step only: a sub-task's newest event when it is the latest progress, else what runs
    # now. T182 #1128 wrapped "子任务「…」运行中，已 2 分 18 秒 · 最近进展 1 秒前 · 子任务运行命令结束：…"
    # over three lines; the sub-task's own time is on its entry in 执行过程.
    line = "当前：" + (phrase if phrase and facts.wall - newest <= quiet + 1 else current)
    if (newest is not None or facts.stirred) and quiet >= QUIET_SHOWN:
        line += f" · 最近进展 {clock_text(quiet)}前"
    if quiet >= 900 and facts.alive is True:
        line += " · 进程仍在运行"
    summary += "\n" + line
    if quiet >= 900:
        summary += "\n较长时间没有新进展，可能正在等待外部任务；尚不能据此判定失败。"
    if facts.alive is False:
        summary += "\n后端进程已退出，正在收尾。"
    return summary


def retry_summary(retry, elapsed, epoch):
    """The model request is being retried: which attempt, when the next one starts, for how long so far
    and at most (when the backend says), and why."""
    wait = max(0, int((retry.get('next') or 0) / 1000 - epoch))
    suffix = f"约 {wait} 秒后再次尝试" if wait else "正在再次尝试"
    if retry.get("since"):
        limit = f"，最长 {int(retry['limit']) // 60} 分钟" if retry.get("limit") else ""
        suffix += f"（已持续 {clock_text(epoch - retry['since'] / 1000)}{limit}）"
    reason = f"上游返回：{safe_text(retry['reason'], 200)}\n" if retry.get("reason") else ""
    return (f"模型请求重试中 · 第 {int(retry.get('attempt') or 0)} 次 · 已运行 {took_text(elapsed)}\n"
            f"本次模型请求尚未成功，{suffix}。\n{reason}可使用停止按钮结束等待。")


def status_summary(facts):
    """The status line in full (the record's ``long_task_summary``): a retry, else the model's question
    waiting for its answer, else what the trace says, else phase and process health."""
    if facts.retry and not facts.waiting:
        return retry_summary(facts.retry, facts.elapsed, facts.epoch)
    if facts.waiting:
        return ("需要你回答：\n" + facts.question if facts.native_question_active
                else "正在整理本轮结果，完成后将展示需要确认的内容。")
    if facts.activity is not None:
        # A live process only proves the backend is up; show what it last did instead.
        return trace_summary(facts)
    return plain_summary(facts)


def status_text(facts):
    """The status line a tick writes to the card (TaskMonitor.update): the summary, or, for a short task
    without a trace, its last checkpoint event or a moving ellipsis."""
    if facts.retry or facts.long_mode or facts.waiting or facts.traced:
        return status_summary(facts)
    if facts.last_event != "尚无阶段进展":
        return facts.last_event
    return "正在处理本轮消息" + " ·" * (facts.frame % 3 + 1)
