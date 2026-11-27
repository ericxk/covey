"""看看: a read-only look at a turn that may be stuck (core/progress.py maybe_stuck), from the button under its task card's
status line (cards/task_status.py look_element, kind LOOK_KIND, Cards.show_stuck), answered in the topic's thread.

``StuckCheck.look_action`` takes the click on the Bridge (the kind is bot_only: never forwarded to the task runner, which
may be the stuck one) and looks in a thread of its own (``look``): the job's task runner (core/run_dir.py runner.json),
the processes under it and which of them used the CPU over SAMPLE_SECONDS (``processes``: names and states, never their
command lines), when its runner.log was last written and its last lines, the trace's latest steps and the turn's
progress. ``verdict`` says in one sentence what that means (still working, waiting for a command, waiting for the
model, its runner gone), by the rules alone: no model is asked (T898: the model only reworded that sentence, and 看看 was
never clicked). The reply is that sentence and the facts it rests on. Nothing is stopped, changed or started, and no task
runs.

The stops route's 看原因 (a turn that ended) is the same kind of read-only look; the two are to be merged.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from covey.app.card_actions import action_value
from covey.core import progress as turn_progress
from covey.core import procfs
from covey.core.checkpoint import read_checkpoint
from covey.core.run_dir import LOG, run_dir, runner_alive, runner_info
from covey.core.statuses import RUNNING
from covey.core.text import redact
from covey.core.trace_store import load_trace

SAMPLE_SECONDS = 1.0
LOG_BYTES = 4096
LOG_LINES = 8
SHOWN = 5  # Processes and trace steps listed at most.


def _ticks(pid):
    """utime + stime of ``pid`` in clock ticks (fields 14 and 15 of /proc/<pid>/stat), None when it is gone."""
    try:
        raw = (Path("/proc") / str(pid) / "stat").read_text()
        fields = raw[raw.rindex(")") + 1:].split()
        return int(fields[11]) + int(fields[12])
    except (OSError, ValueError, IndexError):
        return None


def descendants(root):
    """[(pid, depth from ``root``)] of the processes under ``root``, from one scan of /proc's stat files."""
    parents = {}
    for entry in os.scandir("/proc"):
        if entry.name.isdigit():
            stat = procfs.stat(int(entry.name))
            if stat is not None:
                parents.setdefault(stat.ppid, []).append(int(entry.name))
    found, level = [], [root]
    for depth in range(1, 16):
        level = [child for pid in level for child in parents.get(pid, ())]
        if not level:
            break
        found += [(pid, depth) for pid in level]
    return found


def processes(root, *, sample=SAMPLE_SECONDS, sleep=time.sleep):
    """[{name, state, depth, busy}] of the processes under ``root``: ``busy`` when it used the CPU over ``sample``
    seconds. Depth 1 is the Harness (the backend), deeper ones the commands it runs."""
    found = descendants(root)
    before = {pid: _ticks(pid) for pid, _ in found}
    sleep(sample)
    rows = []
    for pid, depth in found:
        stat, after = procfs.stat(pid), _ticks(pid)
        if stat is None or stat.state == "Z":
            continue
        busy = after is not None and before.get(pid) is not None and after > before[pid]
        rows.append({"name": stat.name, "state": stat.state, "depth": depth, "busy": busy})
    return rows


def log_tail(path, now):
    """(seconds since ``path`` was written, its last LOG_LINES lines with what looks like a secret hidden), (None, [])
    without it."""
    try:
        info = path.stat()
        with path.open("rb") as stream:
            stream.seek(max(0, info.st_size - LOG_BYTES))
            text = stream.read().decode("utf-8", "replace")
    except OSError:
        return None, []
    lines = [redact(line.strip())[:200] for line in text.splitlines() if line.strip()][-LOG_LINES:]
    return max(0.0, now - info.st_mtime), lines


def recent_steps(snapshot):
    """The trace's latest steps of the main task, newest last, as core/progress.py activity_words says them with their
    times."""
    steps = (snapshot or {}).get("steps") or {}
    rows = []
    for step in steps.values():
        if not isinstance(step, dict) or step.get("agent_id") is not None:
            continue
        at = step.get("ended") if isinstance(step.get("ended"), (int, float)) else step.get("started")
        if isinstance(at, (int, float)) and not isinstance(at, bool):
            rows.append((at, turn_progress.activity_words(step)))
    rows.sort()
    return [{"at": int(at), "did": words} for at, words in rows[-SHOWN:]]


def facts(state_dir, record, now=None, *, procs=processes) -> dict:
    """What the look found of job ``record``: its progress, runner, processes, log and latest steps (the module)."""
    now = time.time() if now is None else now
    state_dir = Path(state_dir)
    key = str(record.get("key") or "")
    directory = run_dir(state_dir, key) if key else None
    info = runner_info(directory) if directory is not None else None
    alive = bool(directory is not None and runner_alive(directory))
    rows = procs(info["pid"]) if alive and isinstance(info, dict) and isinstance(info.get("pid"), int) else []
    quiet, lines = log_tail(directory / LOG, now) if directory is not None else (None, [])
    snapshot = None
    try:
        snapshot = load_trace(state_dir, record) if key else None
    except (OSError, ValueError, KeyError, TypeError):
        snapshot = None
    point = read_checkpoint(Path(record["checkpoint_path"])) if record.get("checkpoint_path") else {}
    found = turn_progress.progress(record, checkpoint=point, trace=snapshot, now=now)
    return {"progress": found, "runner": alive, "detached": info is not None, "processes": rows,
            "log_quiet": quiet, "log": lines, "steps": recent_steps(snapshot)}


def _names(rows):
    names = list(dict.fromkeys(row["name"] for row in rows))
    return "、".join(names[:3]) + (" 等" if len(names) > 3 else "")


def verdict(found, now=None) -> str:
    """The rules' sentence of what ``found`` (``facts``) means."""
    now = time.time() if now is None else now
    progress = found["progress"]
    last = turn_progress.activity_line(progress, now) or "没有记下的动静"
    if found["detached"] and not found["runner"]:
        return f"任务进程已经不在了，这一轮不会再往下走（{last}）；任务卡稍后会变成中断，到时可以点「检查并继续」接着做。"
    commands = [row for row in found["processes"] if row["depth"] >= 2]
    backend = [row for row in found["processes"] if row["depth"] == 1]
    if any(row["busy"] for row in commands):
        return (f"还在干活：{_names([row for row in commands if row['busy']])} 正在用 CPU，只是一直没有新的执行过程"
                f"（{last}），多半是一条耗时的命令，可以再等等。")
    if commands:
        return (f"在等命令 {_names(commands)} 返回，但它这一秒没在用 CPU（{last}）：像是在等什么。"
                "再过一会儿还不动，可以停止后重试，或者补充一句让它说说在等什么。")
    if any(row["busy"] for row in backend):
        return f"后端进程在用 CPU，可能在处理很长的输出（{last}），可以再等等。"
    if found["runner"] or backend:
        return f"没有在跑的命令，像是在等模型回复（{last}）；模型这么久没回，可能是上游慢或连接卡住了，可以停止后重试。"
    return f"这台机器上看不到这一轮的进程（{last}），可能在设备上跑，或者已经结束。"


def facts_lines(found, now=None) -> list:
    """The facts under the sentence, a line each."""
    now = time.time() if now is None else now
    progress = found["progress"]
    lines = []
    if progress.get("phase"):
        lines.append(f"阶段：{progress['phase']}" + (f"（{turn_progress.plan_words(progress)}）"
                                                    if progress.get("plan_total") else ""))
    if progress.get("last_activity_at") is not None:
        lines.append("最近动静：" + turn_progress.activity_line(progress, now))
    if found["detached"]:
        lines.append("任务进程：" + ("还在" if found["runner"] else "不在了"))
    if found["processes"]:
        shown = [f"{row['name']}（{'在用 CPU' if row['busy'] else '空闲'}）" for row in found["processes"][:SHOWN]]
        more = len(found["processes"]) - SHOWN
        lines.append("下面的进程：" + "、".join(shown) + (f" 等 {len(found['processes'])} 个" if more > 0 else ""))
    if found["log_quiet"] is not None:
        lines.append(f"日志：{turn_progress.ago_words(found['log_quiet'])}写过")
    if found["steps"]:
        lines.append("最近几步：" + "；".join(f"{time.strftime('%H:%M', time.localtime(row['at']))} {row['did']}"
                                           for row in found["steps"][-3:]))
    return lines


def reply_text(found, sentence, job_id, now=None) -> str:
    """The reply in the thread: the sentence, then its facts."""
    return "\n".join([f"🔍 看了一下任务 #{job_id}：{sentence}", *(f"- {line}" for line in facts_lines(found, now))])


class StuckCheck:
    """One Harness's 看看 (the module). ``harness``: its job records, its name and backend; ``messenger``: the reply."""

    def __init__(self, harness, *, runtime, messenger) -> None:
        self.harness = harness
        self.runtime = runtime
        self.messenger = messenger
        self.voice = harness.voice
        self.looking = set()  # Job keys looked at now: a second click meanwhile adds nothing.
        self.lock = threading.Lock()

    def look_action(self, ctx, event):
        raw = action_value(event)
        if not ctx.topic_id or raw.get("topic_id") != ctx.topic_id:
            return True
        chat = str(event.get("chat_id") or "")
        event_id = str(event.get("event_id") or event.get("message_id") or "")
        record = self.harness.topic_record(ctx, chat, int(raw.get("job_id") or 0))
        if not record:
            raise ValueError("当前话题找不到这一轮任务")
        if record.get("status") not in RUNNING:
            self.reply(ctx, chat, f"任务 #{record.get('job_id')} 已经结束了，不用再看。", "stuck-look-ended", event_id)
            return True
        key = str(record.get("key") or "")
        with self.lock:
            if key in self.looking:
                return True
            self.looking.add(key)
        threading.Thread(target=self.look, args=(ctx, chat, record, event_id), daemon=True, name="stuck-look").start()
        return True

    def look(self, ctx, chat, record, event_id):
        """Look and reply; never raises."""
        key = str(record.get("key") or "")
        try:
            found = facts(ctx.state_dir, record)
            self.reply(ctx, chat, reply_text(found, verdict(found), record.get("job_id")), "stuck-look", event_id)
            self.runtime.log(f"stuck look job=#{record.get('job_id')} runner={found['runner']} "
                             f"processes={len(found['processes'])}")
        except Exception as exc:  # noqa: BLE001 - a look that failed says so, the task is not touched
            self.runtime.log(f"stuck look failed job=#{record.get('job_id')}: {type(exc).__name__}")
            try:
                self.reply(ctx, chat, "这次没看成（读进程或记录时出错），任务本身没有动。", "stuck-look-failed", event_id)
            except Exception:  # noqa: BLE001
                pass
        finally:
            with self.lock:
                self.looking.discard(key)

    def reply(self, ctx, chat, text, name, event_id):
        if chat:
            self.messenger.reply(ctx, chat, text, self.voice.key(name, event_id), voice=self.voice)
