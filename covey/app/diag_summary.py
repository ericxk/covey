"""复制诊断 next to 看原因 under a turn that did not succeed (cards/recovery.py result_actions, kind REPORT_KIND): one message in
the topic's thread with a plain block the owner copies to whoever helps him, so nobody has to ask again which version,
which turn and what it did last.

``summary`` reads, and changes nothing: the version (this Bridge's release and its Manager generation, the release the
turn's task runner ran when another), the optional switches the Bridge has set (core/switches.py), the topic and the task (label, ids, Harness and model, status), when it started and
ended, the last step that succeeded (its process record, core/trace_store.py), its checkpoint, the retries along its chain
(app/auto_retry.py KINDS' counts, the task it retried), and the error: core/failure.py's reason and technical detail, the
record's own error fields and the start of its failed answer, each with what looks like a secret hidden (core/text.py
redact) and cut short. No model is asked and no task runs.

``DiagSummary.copy_action`` takes the click on the Bridge (bot_only: the turn has ended, and its runner may be gone), the
owner's only (the block names ids and errors: in a group, a member's click is refused on the card's topic with a line).
The block is read and sent after the click is acknowledged (card_lanes.after_ack), keyed by the click (Feishu delivers it
at least once: one message).
"""
from __future__ import annotations

import datetime as _dt
import subprocess
import time
from pathlib import Path

from covey.app import auto_retry
from covey.app.card_actions import action_value
from covey.app.card_lanes import after_ack
from covey.core import failure
from covey.core import progress as turn_progress
from covey.core import switches
from covey.core.checkpoint import read_checkpoint
from covey.core.jobs import find_job_record
from covey.core.metrics import run_seconds, run_start, timings_of, took_text
from covey.core.run_dir import run_dir, runner_info
from covey.core.text import cut, redact
from covey.core.topics import topic_label
from covey.core.trace_store import load_trace

FIELD_CHARS = 300  # Of each error field, after redact.
ANSWER_CHARS = 300  # Of a failed answer's start.
GIT_SECONDS = 3
TITLE = "Covey 诊断摘要"
NOT_OWNER = "只有机器人的主人能复制诊断摘要。"


def masked(value, limit=FIELD_CHARS) -> str:
    """``value`` on one line, what looks like a secret hidden, at most ``limit`` characters."""
    return cut(" ".join(redact(value).split()), limit, beyond=True)


def clock(stamp) -> str:
    """A record's time (epoch seconds, or an ISO string as created_at is) as MM-DD HH:MM:SS, '' without one."""
    if isinstance(stamp, str) and stamp:
        try:
            stamp = _dt.datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return ""
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or stamp <= 0:
        return ""
    return time.strftime("%m-%d %H:%M:%S", time.localtime(stamp))


def code_version(release) -> str:
    """A code folder as a version: its name (a release's commit), with the commit it has checked out when it is a git
    checkout of another name (./install's)."""
    release = Path(release)
    name = release.name
    if not (release / ".git").exists():
        return name
    try:
        done = subprocess.run(["git", "-C", str(release), "rev-parse", "--short=12", "HEAD"], capture_output=True,
                              text=True, timeout=GIT_SECONDS, check=False, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return name
    commit = done.stdout.strip() if done.returncode == 0 else ""
    return f"{name} @ {commit}" if commit and not name.startswith(commit[:8]) else name


def manager_generation(state_dir) -> str:
    """The Manager generation this Bridge runs in (service/manager.py generation), '-' when it is not managed."""
    from covey.service import manager
    try:
        return str(manager.generation(Path(state_dir)))
    except (OSError, ValueError, KeyError, TypeError):
        return "-"


def last_done_step(snapshot):
    """The latest step of the main task that ended well, as core/progress.py activity_words says it, with its time;
    '' without one."""
    best = None
    for step in ((snapshot or {}).get("steps") or {}).values():
        if not isinstance(step, dict) or step.get("agent_id") is not None or step.get("status") != "done":
            continue
        at = step.get("ended") if isinstance(step.get("ended"), (int, float)) else step.get("started")
        if isinstance(at, (int, float)) and not isinstance(at, bool) and (best is None or at >= best[0]):
            best = (at, step)
    if best is None:
        return ""
    return f"{time.strftime('%H:%M:%S', time.localtime(best[0]))} {turn_progress.activity_words(best[1])}".strip()


def retries(state_dir, record) -> str:
    """The automatic retries along ``record``'s chain and the task it retried, '' when neither."""
    parts = [f"{spec.action} {int(record.get(spec.count) or 0)} 次（{spec.reason}）"
             for spec in auto_retry.KINDS.values() if int(record.get(spec.count) or 0)]
    if record.get("policy_retry_state"):
        parts.append(f"安全拒绝换模型重试：{record['policy_retry_state']}")
    before = record.get("retry_of")
    if before:
        found = find_job_record(Path(state_dir), str(before)) or {}
        parts.append(f"接着任务 #{found.get('job_id') or '?'} 做")
    if record.get("retried_by"):
        found = find_job_record(Path(state_dir), str(record["retried_by"])) or {}
        parts.append(f"已由任务 #{found.get('job_id') or '?'} 接着做")
    return "；".join(parts)


def error_lines(record) -> list:
    """The error, a line each: the reason core/failure.py gives with its technical detail, the record's own error
    fields, and the start of a failed answer; secrets hidden."""
    lines = []
    view = failure.failure_view(record)
    if view:
        lines.append("原因：" + masked(view["title"]))
        if view.get("detail"):
            lines.append("详情：" + masked(view["detail"]))
    elif record.get("status_message"):
        lines.append("状态：" + masked(record["status_message"]))
    for field, name in (("failure_code", "错误码"), ("runner_start_error", "任务进程启动"),
                        ("delivery_error", "投递"), ("last_card_error", "任务卡")):
        value = record.get(field)
        if value and not (field == "failure_code" and view and f"错误码 {value}" in view.get("detail", "")):
            lines.append(f"{name}：{masked(value)}")
    if record.get("execution_status") == "failed" and record.get("final_text"):
        lines.append("回答开头：" + masked(record["final_text"], ANSWER_CHARS))
    return lines


def summary(state_dir, record, *, bridge_release, generation="-") -> str:
    """The block of ``record``'s task (the module docstring), plain lines under TITLE."""
    state_dir = Path(state_dir)
    runner = runner_info(run_dir(state_dir, str(record["key"]))) if record.get("key") else None
    ran = Path(str(runner["release"])).name if isinstance(runner, dict) and runner.get("release") else ""
    version = f"Bridge {bridge_release}（第 {generation} 代）"
    if ran and ran != Path(str(bridge_release).split(" @ ")[0]).name:
        version += f"；这一轮的任务进程 {ran}"
    topic = record.get("topic_id") or ""
    label = topic_label(state_dir, topic) if topic else ""
    model = " ".join(str(record.get(field) or "") for field in ("model", "effort")).strip()
    status = " / ".join(str(record.get(field)) for field in ("status", "execution_status") if record.get(field))
    lines = [TITLE, "版本：" + version, "开关：" + (switches.words(switches.shown()) or "没设可选开关"),
             "话题：" + " ".join(part for part in (label, f"（{topic}）" if topic else "") if part) if topic else "",
             "任务：" + " · ".join(part for part in (f"#{record.get('job_id')}", str(record.get("harness_id") or ""),
                                                   model, status) if part)]
    try:
        snapshot = load_trace(state_dir, record) if record.get("key") else record.get("trace_snapshot")
    except (OSError, ValueError, KeyError, TypeError):
        snapshot = None
    timings = timings_of(record)
    started = clock(run_start(timings) or record.get("created_at"))
    ended = clock(timings.get("execution_finished") or (snapshot or {}).get("ended_at"))
    took = run_seconds(timings, snapshot) if ended else None
    if started or ended:
        lines.append("时间：" + " → ".join(part for part in (started, ended) if part)
                     + (f"（用时 {took_text(took)}）" if took is not None else ""))
    step = last_done_step(snapshot)
    lines.append("最近成功的一步：" + (step or "没有记下"))
    point = read_checkpoint(Path(record["checkpoint_path"])) if record.get("checkpoint_path") else {}
    if point.get("phase") or point.get("completed"):
        done = point.get("completed") if isinstance(point.get("completed"), list) else []
        lines.append("检查点：" + "；".join(part for part in (
            f"停在 {masked(point['phase'], 80)}" if point.get("phase") else "", f"已完成 {len(done)} 项" if done else "")
            if part))
    said = retries(state_dir, record)
    lines.append("重试：" + (said or "没有"))
    lines.extend(error_lines(record))
    return "\n".join(line for line in lines if line)


def message_text(block, job_id) -> str:
    """The message: one line of what it is, the block in a code fence (one tap copies it whole on a computer)."""
    return f"📋 任务 #{job_id} 的诊断摘要，整段复制给帮你排障的人（像密钥的字样已打码）：\n```text\n{block}\n```"


class DiagSummary:
    """One Harness's 复制诊断 (the module). ``harness``: its job records and voice; ``messenger``: the reply."""

    def __init__(self, harness, *, runtime, messenger) -> None:
        self.harness, self.runtime, self.messenger = harness, runtime, messenger
        self.voice = harness.voice

    def copy_action(self, ctx, event):
        raw = action_value(event)
        if not ctx.topic_id or raw.get("topic_id") != ctx.topic_id:
            return True
        chat = str(event.get("chat_id") or "")
        event_id = str(event.get("event_id") or event.get("message_id") or "")
        if not self.runtime.access.is_owner(str(event.get("operator_id") or "")):
            raise ValueError(NOT_OWNER)
        try:
            job_id = int(raw.get("job_id") or 0)
        except (TypeError, ValueError):
            return True
        record = self.harness.topic_record(ctx, chat, job_id)
        if not record:
            raise ValueError("当前话题找不到这一轮任务")
        after_ack(lambda: self.send(ctx, chat, record, event_id))
        return True

    def send(self, ctx, chat, record, event_id):
        """Read the block and reply with it; a block that cannot be read says so instead."""
        try:
            block = summary(ctx.state_dir, record, bridge_release=code_version(Path(self.runtime.script_path).resolve().parents[1]),
                            generation=manager_generation(ctx.state_dir))
            text = message_text(block, record.get("job_id"))
        except Exception as exc:  # noqa: BLE001 - a summary that failed says so; nothing else is touched
            self.runtime.log(f"diag summary failed job=#{record.get('job_id')}: {type(exc).__name__}")
            text = "诊断摘要这次没读出来（读记录时出错），可以在服务器上运行 covey_doctor.py 看整体状态。"
        self.messenger.reply(ctx, chat, text, self.voice.key("diag-summary", event_id), voice=self.voice)
        self.runtime.log(f"diag summary job=#{record.get('job_id')} sent")
