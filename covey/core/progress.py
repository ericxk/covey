"""A running turn's progress worked out in one place (``progress``), for every reader that says how a turn is going: the
task card's status line and 任务详情 (core/longtask.py, core/status_line.py, cards/streaming.py), the pinned board's
status cards (app/status_cards.py), 手上的事 (app/overview.py), 早晚汇总 (app/digest.py), the butler's snapshot
(app/butler.py) and the web page (app/web_sync.py, topic.data ``progress``).

``progress(record, checkpoint=..., trace=..., history=..., now=...)`` is a pure function of a job record, the turn's
checkpoint and its execution trace (a core/trace.py ToolTrace or its snapshot), and the 用时 of the topic's earlier
turns (``turn_seconds``). It answers {phase, plan_done, plan_total, last_activity_at, last_activity, eta_seconds,
slow_step, maybe_stuck, command}:

- ``phase``: the checkpoint's phase, '' while the model has written none (core/turn_words.py UNSET_PHASES);
- ``plan_done``, ``plan_total``: the checkpoint's steps done and listed (core/checkpoint.py checkpoint_steps), None
  without steps;
- ``last_activity_at``, ``last_activity``: when the turn last moved (epoch seconds) and what it did then in a few words
  (``activity_words``: 在跑测试, 在读 app/runs.py, 改了 cards.py; never a command line): the trace's newest step, or
  what the task monitor kept on the record (``last_activity_at``, ``last_activity``, written by core/longtask.py with
  streamed text and the model's requests counted too); a record of an older task runner has neither, and its last
  progress is its last health check less ``last_progress_age_seconds``. None and '' when nothing is known;
- ``eta_seconds``: ``estimate``, by the pace of the steps this turn did (``pace``: when the Bridge saw each step end,
  core/checkpoint.py clock_steps, the record's ``step_times``; steps already done when it first saw the plan, carried
  from an earlier turn, are not this turn's pace): the average step times the steps left, less what the current step
  has had since the last one ended, so it goes down while the turn works on and never grows with the wait; put halfway
  towards the topic's usual turn (the median of ``history``) when there are MIN_HISTORY turns and the usual one is
  longer than this one so far. None without a step done this turn and one left, before MIN_ELAPSED, from one step done
  with fewer turns of history, without the steps' times, and once the current step has taken longer than the average
  one (``slow_step`` says that instead): better nothing than a wrong figure. To the minute, so a reader that pushes on
  a change (the web page) does not push every second;
- ``slow_step``: the current step once it has taken longer than this turn's average step, {number (its place in the
  steps, from 1), since (epoch seconds: when the step before it ended), seconds (it has taken so far), usual (the
  average step)}, seconds to the minute; else None. The readers say it instead of an estimate (``slow_words``);
- ``maybe_stuck``: a turn running (not queued, stopping or waiting for an answer) whose last activity is known and
  ``stuck_seconds`` ago or more (COVEY_PROGRESS_STUCK_MINUTES, 30 by default), unless it waits for a command;
- ``command``: {since, seconds (to the minute), long (it has run ``stuck_seconds`` or more)} while the newest step is a
  command still running (``running_command``; from the trace, else from the record's ``command_since`` that the task
  monitor keeps): that turn is never ``maybe_stuck``, the readers say it waits for the command (``command_words``). Not held
  once the Bridge knows the backend process has exited (the record's ``process_alive`` False: a command whose end was never
  recorded would else hold the turn for ever), nor when the record's last health check is older than COMMAND_FRESH (the task
  runner is gone), nor for a turn waiting for an answer or not running; the key is left out otherwise (a result without it is as before: the web page's progress).

The words the readers show it with are here too (``ago_words``, ``eta_words``, ``finish_words``, ``slow_words``,
``activity_line``, ``stuck_words``, ``board_words``), each to the minute: a card is written only when what it shows changes (AGENTS.md rule 3)."""
from __future__ import annotations

import datetime
import html
import re
import statistics
import time

from covey.core import env as cenv
from covey.core.checkpoint import checkpoint_steps, current_step
from covey.core.metrics import run_seconds, run_start, timings_of
from covey.core.state import epoch_seconds
from covey.core.text import command_kind
from covey.core.turn_words import UNSET_PHASES

STUCK_ENV = "COVEY_PROGRESS_STUCK_MINUTES"
STUCK_MINUTES = 30
STUCK_RANGE = (1, 1440)
MIN_HISTORY = 3  # Earlier turns of the topic the estimate is put towards (``estimate``).
MIN_DONE = 2  # Steps done for an estimate without that history.
MIN_ELAPSED = 60  # No estimate in a turn's first minute.
WORDS_CHARS = 40  # ``last_activity`` at most.
COMMAND_FRESH = 600  # A record's health check older than this (seconds) no longer vouches that its command runs (``progress``).
FIELDS = ("phase", "plan_done", "plan_total", "last_activity_at", "last_activity", "eta_seconds", "slow_step",
          "maybe_stuck")  # And ``command`` while one is waited for.

# A trace step's coarse kind (core/text.py step_action, command_kind) as a verb and its object: 在跑测试 while it runs,
# 跑了测试 once it ended, 跑测试失败 when it failed.
VERBS = {"运行测试": ("跑", "测试"), "查看代码": ("看", "代码"), "编辑文件": ("改", "文件"), "提交代码": ("提交", "代码"),
         "查阅网页": ("查", "网页"), "运行命令": ("跑", "命令"), "分配子任务": ("派", "子任务"), "更新计划": ("更新", "计划"),
         "更新检查点": ("更新", "进度"), "等待回答": ("等", "回答"), "调用工具": ("调用", "工具")}
TITLE_KINDS = {"读取文件": "查看代码", "查找文件": "查看代码", "搜索文件": "查看代码", "浏览目录": "查看代码",
               "修改文件": "编辑文件", "写入文件": "编辑文件", "edit": "编辑文件", "读取网页": "查阅网页",
               "搜索网页": "查阅网页", "等待回答": "等待回答", "分配子任务": "分配子任务", "更新计划": "更新计划",
               "更新检查点": "更新检查点"}
COMMAND_TITLES = {"执行命令", "后台命令", "启动后台命令"}
FILE_TITLES = {"读取文件": "读", "修改文件": "改", "写入文件": "写", "edit": "改"}
AGENT_WORDS = {"running": "开始了", "pendingInit": "开始了", "completed": "做完了", "errored": "出错了",
               "interrupted": "中断了", "cancelled": "中断了"}
RUNNING_STATUSES = ("running",)  # A record's status that can be stuck: not queued, stopping or waiting.


def stuck_seconds(environ=None) -> int:
    """COVEY_PROGRESS_STUCK_MINUTES (STUCK_MINUTES when unset or not a whole number in STUCK_RANGE) in seconds."""
    raw = str(cenv.get(STUCK_ENV, "", environ) or "").strip()
    try:
        minutes = int(raw)
    except ValueError:
        minutes = STUCK_MINUTES
    if not STUCK_RANGE[0] <= minutes <= STUCK_RANGE[1]:
        minutes = STUCK_MINUTES
    return minutes * 60


def _number(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _plain(text) -> str:
    """Trace text (escaped for Markdown by core/text.py safe_text) as plain words on one line."""
    return " ".join(html.unescape(re.sub(r"\\([`*_\[\]\\])", r"\1", str(text or ""))).split())


def _cut(text, limit) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _short_path(path) -> str:
    """A path's last two parts (app/runs.py), its first file of a list of several."""
    path = _plain(path).split(", ")[0].rstrip("/")
    return "/".join(path.split("/")[-2:]) if path else ""


def activity_words(step) -> str:
    """What a trace ``step`` did in a few words: 在跑测试 while it runs, 跑了测试 once it ended, 读 app/runs.py with the file a
    file tool named, the model's own purpose for a command whose kind says no more (在跑命令：装依赖); never the command.
    A note is 更新了进展说明 (its title)."""
    if not isinstance(step, dict):
        return ""
    status = step.get("status")
    title = str(step.get("title") or "")
    if status == "note":
        return "更新了" + (title or "进展说明")
    detail = _plain(step.get("detail"))
    purpose = ""
    if title in COMMAND_TITLES:
        said, separated, command = detail.partition(" · ")
        kind = command_kind(command if separated else detail)
        purpose = said if separated and kind == "运行命令" else ""
        verb, thing = VERBS[kind]
    elif title in FILE_TITLES and detail:
        verb, thing = FILE_TITLES[title], " " + _short_path(detail)
    else:
        verb, thing = VERBS[TITLE_KINDS.get(title, "调用工具")]
    if status == "running":
        words = f"在{verb}{thing}"
    elif status == "failed":
        words = f"{verb}{thing}失败"
    else:
        words = f"{verb}了{thing}"
    if purpose:
        words += "：" + purpose
    return _cut(words, WORDS_CHARS)


def _parts(node):
    """(steps, agents, children) of a trace node: a ToolTrace or its snapshot."""
    if isinstance(node, dict):
        return node.get("steps") or {}, node.get("agents") or {}, node.get("children") or {}
    return getattr(node, "steps", None) or {}, getattr(node, "agents", None) or {}, getattr(node, "children", None) or {}


def newest_activity(trace, now=None):
    """(epoch seconds, words) of the trace's newest step anywhere in it, a sub-task's start or end too: a step's start or,
    once it ended, its end; (None, '') without one. A sub-task's own step names it (子任务「标注」在跑测试)."""
    stamp, words, _ = _newest(trace, now)
    return stamp, words


def running_command(trace, now=None):
    """When the newest step of the trace (``newest_activity``'s) started, if it is a command (a shell or background
    command step) still running: the turn then waits for a process, which is not being stuck. None otherwise: another
    step is newer, the command ended, or its end was never recorded and the turn has ended (the trace then reads the
    step unconfirmed, core/trace.py close)."""
    _, _, step = _newest(trace, now)
    if isinstance(step, dict) and step.get("status") == "running" and step.get("title") in COMMAND_TITLES:
        return _number(step.get("started"))
    return None


def _newest(trace, now=None):
    """(epoch seconds, words, the step) of ``newest_activity``; the step is None when an agent's state change is newest."""
    now = time.time() if now is None else now

    def plausible(stamp):
        return stamp is not None and -60 <= now - stamp < 7 * 86400

    nodes, seen, agents = [(trace, None)], {id(trace)}, {}
    for node, _ in nodes:
        _, own, children = _parts(node)
        agents.update(own if isinstance(own, dict) else {})
        for sid, child in (children.items() if isinstance(children, dict) else ()):
            if id(child) not in seen:
                seen.add(id(child))
                nodes.append((child, sid))

    def who(sid):
        name = (agents.get(sid) or {}).get("name") if isinstance(agents.get(sid), dict) else None
        return f"子任务「{_cut(_plain(name), 20)}」" if name else "子任务"

    best = (None, "", None)
    for sid, agent in agents.items():
        if not isinstance(agent, dict):
            continue
        for stamp, state in ((_number(agent.get("started_at")), "开始了"),
                             (_number(agent.get("ended_at")), AGENT_WORDS.get(agent.get("status"), "做完了"))):
            if plausible(stamp) and (best[0] is None or stamp >= best[0]):
                best = (stamp, who(sid) + state, None)
    for node, owner in nodes:
        steps, _, _ = _parts(node)
        for key, step in (steps.items() if isinstance(steps, dict) else ()):
            if not isinstance(step, dict) or step.get("agent_id") is not None or key in agents:
                continue
            ended = _number(step.get("ended"))
            stamp = ended if plausible(ended) else _number(step.get("started"))
            if plausible(stamp) and (best[0] is None or stamp >= best[0]):
                words = activity_words(step)
                best = (stamp, words if owner is None else who(owner) + words, step)
    return best


def turn_seconds(records, exclude=None) -> list:
    """The 用时 (core/metrics.py run_seconds) of the turns in ``records`` that ended done, oldest first, not job
    ``exclude`` (the turn running): what ``estimate`` puts an estimate towards."""
    found = []
    for record in sorted((r for r in records if isinstance(r, dict)), key=lambda r: int(r.get("job_id") or 0)):
        if record.get("status") != "done" or (exclude is not None and str(record.get("job_id")) == str(exclude)):
            continue
        timings = timings_of(record)
        took = run_seconds(timings, now=_number(timings.get("execution_finished")))
        if took is not None and _number(timings.get("execution_finished")) is not None and took > 0:
            found.append(took)
    return found


def pace(steps, times):
    """(steps done this turn, the seconds each took on average, when the last of them ended) from ``times``, the
    Bridge's clock of ``steps`` (core/checkpoint.py clock_steps: {title: {started, ended}}); None without a step done
    after the first time the Bridge saw the plan. The steps already done then (their end is that first sight: carried
    from an earlier turn, or done before the plan was written) and the time before it are not counted."""
    times = times if isinstance(times, dict) else {}
    stamps = [_number(value) for clocked in times.values() if isinstance(clocked, dict) for value in clocked.values()]
    stamps = [stamp for stamp in stamps if stamp is not None]
    if not stamps:
        return None
    first = min(stamps)
    clocked = [times.get(step["title"]) for step in steps if step["state"] == "done"]
    ends = [_number(each.get("ended")) for each in clocked if isinstance(each, dict)]
    ends = [end for end in ends if end is not None and end > first]
    if not ends:
        return None
    last = max(ends)
    return len(ends), (last - first) / len(ends), last


def _minute(seconds) -> int:
    return max(60, int(round(seconds / 60)) * 60)


def estimate(elapsed, steps, times, now, history=()) -> tuple:
    """(``eta_seconds``, ``slow_step``) of a turn ``elapsed`` seconds in at ``now`` with ``steps`` clocked by ``times``
    (see the module); (None, None) when there is no telling."""
    total = len(steps)
    done = sum(step["state"] == "done" for step in steps)
    found = pace(steps, times) if total and done < total else None
    if elapsed is None or elapsed < MIN_ELAPSED or found is None:
        return None, None
    counted, each, since = found
    taken = max(0.0, now - since)  # The current step's so far: since the last step ended.
    if taken > each:
        doing = current_step(steps)
        number = doing[0] if doing else next(index for index, step in enumerate(steps, 1) if step["state"] != "done")
        return None, {"number": number, "since": int(since), "seconds": _minute(taken), "usual": _minute(each)}
    left = each * (total - done) - taken
    samples = [value for value in history if _number(value) is not None and value > 0]
    if len(samples) >= MIN_HISTORY:
        usual = statistics.median(samples)
        if usual > elapsed:
            left = (left + usual - elapsed) / 2
    elif counted < MIN_DONE:
        return None, None
    return _minute(left), None


def _record_activity(record):
    """(when, words) the record says the turn last moved: the task monitor's ``last_activity_at`` (core/longtask.py),
    else, a record of an older task runner, its last health check less ``last_progress_age_seconds``."""
    at = _number(record.get("last_activity_at"))
    if at is not None:
        return at, str(record.get("last_activity") or "")
    checked = epoch_seconds(record.get("health_checked_at"))
    age = _number(record.get("last_progress_age_seconds"))
    if checked is not None and age is not None:
        return checked - age, ""
    return None, ""


def progress(record, *, checkpoint=None, trace=None, history=(), now=None, stuck_after=None, waiting=False) -> dict:
    """The progress of the turn of job ``record`` (see the module). ``checkpoint``: the turn's checkpoint, read by the
    caller (else the steps and phase the task monitor kept on the record; the steps' times, ``step_times``, are always
    the record's); ``trace``: its execution trace or None;
    ``history``: ``turn_seconds`` of its topic; ``stuck_after``: seconds (``stuck_seconds`` when None); ``waiting``:
    the turn waits for the owner's answer, which is never stuck."""
    record = record if isinstance(record, dict) else {}
    now = time.time() if now is None else now
    if isinstance(checkpoint, dict) and checkpoint:
        phase, steps = str(checkpoint.get("phase") or ""), checkpoint_steps(checkpoint)
    else:
        phase, steps = str(record.get("phase") or ""), checkpoint_steps({"steps": record.get("steps")})
    phase = " ".join(phase.split())
    phase = "" if phase in UNSET_PHASES else phase
    total = len(steps) or None
    done = sum(step["state"] == "done" for step in steps) if total else None
    at, words = _record_activity(record)
    if trace is not None:
        stamp, said = newest_activity(trace, now)
        if stamp is not None and (at is None or stamp >= at):
            at, words = stamp, said
    begun = run_start(timings_of(record))
    elapsed = max(0.0, now - begun) if begun is not None else None
    status = str(record.get("status") or "")
    stuck_after = stuck_seconds() if stuck_after is None else stuck_after
    command = _command(record, trace, now, stuck_after) if status in RUNNING_STATUSES and not waiting else None
    # Only from a known last activity: a record the task monitor never wrote says nothing of it. Never while a command runs.
    stuck = bool(status in RUNNING_STATUSES and not waiting and command is None and at is not None
                 and now - at >= stuck_after)
    eta, slow = (estimate(elapsed, steps, record.get("step_times"), now, history) if status in RUNNING_STATUSES
                 else (None, None))
    found = {"phase": phase, "plan_done": done, "plan_total": total,
             "last_activity_at": int(at) if at is not None else None, "last_activity": _cut(words, WORDS_CHARS),
             "eta_seconds": eta, "slow_step": slow, "maybe_stuck": stuck}
    if command is not None:  # Only then: a result without it is as before (the web page's progress, docs/web.md).
        found["command"] = command
    return found


def _command(record, trace, now, stuck_after):
    """``progress``'s ``command``: the newest step a command still running, its process not known to be gone."""
    since = running_command(trace, now) if trace is not None else _number(record.get("command_since"))
    if since is None or not -60 <= now - since < 7 * 86400 or record.get("process_alive") is False:
        return None
    checked = epoch_seconds(record.get("health_checked_at"))
    if checked is not None and now - checked > COMMAND_FRESH:
        return None
    seconds = max(0, now - since)
    return {"since": int(since), "seconds": int(seconds // 60 * 60), "long": seconds >= stuck_after}


# -- words -------------------------------------------------------------------------------------------------------------


def minutes_words(seconds) -> str:
    """A span to the minute: "1 分钟" (under two), "40 分钟", "1 小时 5 分钟", "3 小时"."""
    minutes = max(1, int(seconds // 60))
    hours, minutes = divmod(minutes, 60)
    if not hours:
        return f"{minutes} 分钟"
    return f"{hours} 小时" + (f" {minutes} 分钟" if minutes else "")


def ago_words(seconds) -> str:
    """How long ago, to the minute: "刚刚" under a minute, else "2 分钟前", "1 小时 5 分钟前"."""
    return "刚刚" if seconds < 60 else minutes_words(seconds) + "前"


def eta_words(seconds) -> str:
    """"预计还要约 40 分钟": the minutes exact under ten, then to five, from an hour to ten; '' for None."""
    if seconds is None:
        return ""
    minutes = max(1, int(round(seconds / 60)))
    if minutes >= 60:
        minutes = int(round(minutes / 10)) * 10
    elif minutes >= 10:
        minutes = int(round(minutes / 5)) * 5
    return f"预计还要约 {minutes_words(minutes * 60)}"


def slow_words(found, *, clock=None) -> str:
    """The ``slow_step`` of a ``progress`` result: "第 3 步已做 25 分钟，此前平均每步约 6 分钟"; with ``clock`` (a time
    of day from epoch seconds, for a panel written only now and then) "第 3 步 16:33 开始，此前平均每步约 6 分钟". ''
    without one."""
    slow = found.get("slow_step") if isinstance(found, dict) else None
    if not isinstance(slow, dict):
        return ""
    began = f" {clock(slow['since'])} 开始" if clock is not None else f"已做 {minutes_words(slow['seconds'])}"
    return f"第 {slow['number']} 步{began}，{usual_words(found)}"


def usual_words(found) -> str:
    """"此前平均每步约 6 分钟" of a ``progress`` result's ``slow_step``, for a line that says the step's own minutes
    already (the task card's status line: 本步 25 分钟); '' without one."""
    slow = found.get("slow_step") if isinstance(found, dict) else None
    return f"此前平均每步约 {minutes_words(slow['usual'])}" if isinstance(slow, dict) else ""


def activity_line(found, now) -> str:
    """"2 分钟前在跑测试" of a ``progress`` result; "2 分钟前有动静" without words; '' with nothing known."""
    at = found.get("last_activity_at") if isinstance(found, dict) else None
    if at is None:
        return ""
    return ago_words(max(0, now - at)) + (found.get("last_activity") or "有动静")


COMMAND_WAIT = "在等一条命令跑完"  # What a turn whose newest step is a running command says instead of 可能卡住.


def stuck_words(stuck_after=None) -> str:
    """The line of a turn that may be stuck: "超过 30 分钟没有动静，可能卡住了"."""
    minutes = (stuck_seconds() if stuck_after is None else stuck_after) // 60
    return f"超过 {minutes_words(minutes * 60)}没有动静，可能卡住了"


def command_words(found) -> str:
    """"在等一条命令跑完 · 已 38 分钟" of a ``progress`` result's ``command``, to the minute; '' without one."""
    command = found.get("command") if isinstance(found, dict) else None
    if not isinstance(command, dict):
        return ""
    return f"{COMMAND_WAIT} · 已 {minutes_words(command['seconds'])}"


def plan_words(found) -> str:
    """"步骤 3/5" of a ``progress`` result, '' without steps."""
    if not isinstance(found, dict) or not found.get("plan_total"):
        return ""
    return f"步骤 {found.get('plan_done') or 0}/{found['plan_total']}"


FINISH_STEP = 300  # ``finish_words`` rounds the time it would end at to five minutes.


def finish_words(found, now) -> str:
    """The estimate as a time of day, "预计 15:20 左右做完" (to FINISH_STEP; "预计明天 09:10 左右做完" another day), for
    a card that writes times of day, not durations (手上的事, the pinned status cards): it changes only when the estimate
    does. '' without one."""
    seconds = found.get("eta_seconds") if isinstance(found, dict) else None
    if seconds is None:
        return ""
    end = round((now + seconds) / FINISH_STEP) * FINISH_STEP
    days = (datetime.date(*time.localtime(end)[:3]) - datetime.date(*time.localtime(now)[:3])).days
    head = "预计 " if days <= 0 else "预计明天 " if days == 1 else time.strftime("预计 %m-%d ", time.localtime(end))
    return f"{head}{time.strftime('%H:%M', time.localtime(end))} 左右做完"


def board_words(found, now, *, clock=False) -> list:
    """What a board's row of a running turn adds to its phase (each reader has its own phase or step words): 可能卡住了,
    else, once a command has run as long as it takes to be stuck, that the turn waits for it (``COMMAND_WAIT``), else that the current step is slower than the ones before (``slow_step``: 第 3 步比此前几步慢), else the estimate
    (``eta_words``, or with ``clock`` ``finish_words``); never a command, a last activity or a step's minutes, which
    would change the row every minute. Empty parts are left out."""
    if not isinstance(found, dict):
        return []
    if found.get("maybe_stuck"):
        return ["可能卡住了"]
    if isinstance(found.get("command"), dict) and found["command"].get("long"):
        return [COMMAND_WAIT]  # Without its minutes, as for a slow step: the row does not change every minute.
    if isinstance(found.get("slow_step"), dict):
        return [f"第 {found['slow_step']['number']} 步比此前几步慢"]
    words = finish_words(found, now) if clock else eta_words(found.get("eta_seconds"))
    return [words] if words else []
