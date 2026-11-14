"""The memory watchdog's alerts, sent by the Bridge (service/memguard.py writes each to <data_dir>/outbox/).

An alert names the task whose marker the process carried (COVEY_TASK_DIR, core/task_processes.py): one of
this Bridge's tasks goes to that task's topic; one without a task to the owner's private chat; another Bridge's
is left for it. A file is claimed by renaming it, so of two Bridges on one machine only one sends it. At most
PER_HOUR are sent; the next one sent says how many were not. alerts.jsonl beside the outbox keeps every alert.
"""
from __future__ import annotations

import collections
import json
import os
import time
from pathlib import Path

from covey.core.jobs import find_job_record
from covey.core.loops import every
from covey.core.state import read_object
from covey.service import memguard

POLL = 5
PER_HOUR = 6
MAX_AGE = 24 * 3600  # Unsent this long (no owner chat known yet, a Bridge gone): dropped, alerts.jsonl has it.


def outbox_dir() -> Path | None:
    """Where the installed watchdog leaves its alerts; None while it is not installed."""
    config = read_object(memguard.CONFIG)
    if config.get("owner") != memguard.OWNER or not config.get("data_dir"):
        return None
    return Path(config["data_dir"]) / "outbox"


class MemguardAlerts:
    def __init__(self, runtime, messenger, voice) -> None:
        self.runtime, self.messenger, self.voice = runtime, messenger, voice
        self.sent = collections.deque()
        self.suppressed = 0

    def threads(self):
        """Its worker, always: it finds no outbox while the watchdog is not installed (app/unified.py worker_targets)."""
        return (("memguard", self.run),)

    def run(self, ctx) -> None:
        every(self.runtime, POLL, self.send_pending, args=(ctx,))

    def send_pending(self, ctx) -> None:
        folder = outbox_dir()
        if folder is None:
            return
        try:
            names = sorted(entry.name for entry in os.scandir(folder) if entry.name.endswith(".json"))
        except OSError:
            return
        for name in names:
            self.take(ctx, folder / name)

    def target(self, ctx, alert) -> tuple[str, str, str] | None:
        """(chat, message replied to, task line) for ``alert``; None while it is not this Bridge's to send now."""
        task_dir = alert.get("task_dir")
        if not task_dir:
            chat = self.runtime.access.notify_chat()
            return (chat, "", "") if chat else None
        task = Path(os.path.realpath(task_dir))
        if len(task.parents) < 2 or task.parents[1] != Path(os.path.realpath(ctx.state_dir)):
            return None
        record = find_job_record(ctx.state_dir, task.name) or {}
        if record.get("chat_id"):
            return record["chat_id"], str(record.get("reply_to_message_id") or ""), f"任务：#{record.get('job_id')}"
        chat = self.runtime.access.notify_chat()
        return (chat, "", f"任务：{task.name}（记录已不在）") if chat else None

    def take(self, ctx, path: Path) -> None:
        try:
            alert = json.loads(path.read_text())
            if not isinstance(alert, dict) or not isinstance(alert.get("text"), str):
                raise ValueError("not an alert")
        except FileNotFoundError:
            return
        except (OSError, ValueError, UnicodeDecodeError):
            self.drop(path, "unreadable")
            return
        target = self.target(ctx, alert)
        if target is None:
            try:
                if time.time() - path.stat().st_mtime > MAX_AGE:
                    self.drop(path, "expired")
            except OSError:
                pass  # Taken meanwhile.
            return
        claimed = path.with_name(f"{path.name}.{os.getpid()}.claimed")
        try:
            os.rename(path, claimed)  # Another Bridge on this machine may be taking it too.
        except FileNotFoundError:
            return
        try:
            self.send(ctx, alert, *target)
        finally:
            claimed.unlink(missing_ok=True)

    def send(self, ctx, alert, chat, reply_to, task_line) -> None:
        now = time.time()
        while self.sent and now - self.sent[0] > 3600:
            self.sent.popleft()
        if len(self.sent) >= PER_HOUR:
            self.suppressed += 1
            self.runtime.log(f"memory watchdog alert not sent (over {PER_HOUR} an hour) rule={alert.get('rule')}")
            return
        text = alert["text"] + (f"\n{task_line}" if task_line else "")
        if self.suppressed:
            text += f"\n另有 {self.suppressed} 条告警因限流没有发送，见看门狗数据目录的 alerts.jsonl。"
        self.sent.append(now)
        key = self.voice.safe_key(f"memguard-{alert.get('id') or os.urandom(6).hex()}")
        if self.messenger.send_message(ctx.profile, chat, text, key, reply_to_message_id=reply_to, voice=self.voice):
            self.suppressed = 0
            self.runtime.log(f"memory watchdog alert sent rule={alert.get('rule')} topic={'yes' if reply_to else 'no'}")
        else:
            self.runtime.log(f"memory watchdog alert not sent: the message failed rule={alert.get('rule')}")

    def drop(self, path: Path, why: str) -> None:
        try:
            path.unlink()
        except OSError:
            return
        self.runtime.log(f"memory watchdog alert dropped ({why})")
