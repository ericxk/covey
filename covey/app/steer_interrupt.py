"""A supplement the model has not read yet: how long it has waited, and stopping the command that holds it back
(docs/architecture.md 补充投递).

Claude Code takes a supplement in only between two tool calls: one sent while a long command ran was read 4 minutes
later (T538, "二维码过期，马上重发"). Once a supplement is in a running task whose Harness reports reads, a watcher
(``watch_unread``) looks again URGENT_SECONDS after it was sent and at each of REPAINT_AT after that while it is unread:
its receipt card says how long it has waited and offers 打断它，马上读; an urgent one (core/steer.py ``is_urgent``) has
the command stopped at the first look (``interrupt``) unless COVEY_STEER_URGENT_SECONDS=0. Stopping is the backend's
(``stop_command``, backends/claude/command_stop.py): only the command the model waits on, never the turn, and never a
push, a deploy or another command ``keeps_running`` names. The process that holds the task's backend does all this: a
task runner for its own task (app/runner_ipc.py), the Bridge for a task it runs itself. SteerInterrupt is a part of
app/steering.py's Steering.
"""
from __future__ import annotations

import threading
import time

from covey.core import env
from covey.core.deliveries import delivery_path, read_delivery, shown_text, update_delivery
from covey.core.steer import is_urgent

SWITCH = "COVEY_STEER_URGENT_SECONDS"
URGENT_SECONDS = 20
# After the first look, the receipt's 已等 is said again at these seconds since it was sent, while it stays unread.
REPAINT_AT = (60, 180, 600, 1800)
# What the model is told, before the command ends, as a supplement of its own.
NOTE = ("（Bridge 说明：用户发来了上面的补充，为了让你马上读到，Bridge 停掉了你正在等的那条命令，它不是自己失败的。"
        "先处理补充；那条命令还需要的话再跑。）")


def urgent_seconds() -> float:
    """How long an urgent supplement waits unread before its command is stopped; 0 turns the stop off."""
    try:
        return max(0.0, float(env.get(SWITCH, str(URGENT_SECONDS)) or 0))
    except ValueError:
        return float(URGENT_SECONDS)


def stop_words(outcome) -> str:
    """What became of a stop, as the receipt's details and a click's reply say it."""
    if outcome.get("stopped"):
        return "已停掉正在跑的命令，模型马上读到"
    if outcome.get("kept"):
        return "正在跑的是推送、部署这类命令，不打断；它结束后模型读到"
    return "没有打断：" + str(outcome.get("reason") or "模型现在没有在等命令")


class SteerInterrupt:
    """Steering's watch over unread supplements (its runtime, cards and receipts)."""

    def watch_unread(self, ctx, job, mid):
        """``mid`` was just sent into ``job``'s running backend, which this process holds: watch it while it is
        unread. Only a Harness that reports reads (``reads`` on the record) and can stop a command (``stops``)."""
        control = job.backend_control
        if control is None or getattr(control, "remote", False) or not callable(getattr(control, "stop_command", None)):
            return
        path = delivery_path(ctx.state_dir, job.topic_id, mid)
        record = read_delivery(path)
        if not record or not record.get("reads") or not record.get("stops") or record.get("merged"):
            return
        threading.Thread(target=self._watch, args=(ctx, job, control, path), daemon=True, name="steer-unread").start()

    def watch_unread_safely(self, ctx, job, mid):
        """``watch_unread`` for a sender that has delivered: a failure is logged, never raised to it."""
        try:
            self.watch_unread(ctx, job, mid)
        except Exception as exc:  # noqa: BLE001 - the supplement is delivered; only its watch is not
            self.runtime.log(f"unread supplement not watched: {type(exc).__name__}")

    def _unread(self, job, control, path):
        """The record while it is unread and its task still runs on ``control``; else None."""
        if self.runtime.jobs.find(job.job_id) is not job or job.backend_control is not control:
            return None
        record = read_delivery(path)
        if not record or record.get("status") != "sent" or record.get("target_ended") or record.get("merged"):
            return None
        return record

    def _watch(self, ctx, job, control, path):
        record = read_delivery(path) or {}
        sent = float(record.get("sent_at") or time.time())
        first = urgent_seconds() or URGENT_SECONDS
        for index, after in enumerate((first, *[at for at in REPAINT_AT if at > first])):
            while (left := sent + after - time.time()) > 0:
                if self.runtime.shutdown.wait(min(left, 1.0)) or self._unread(job, control, path) is None:
                    return
            record = self._unread(job, control, path)
            if record is None:
                return
            try:
                if (index == 0 and urgent_seconds() and is_urgent(shown_text(record))
                        and not record.get("interrupt_at")):
                    self.interrupt(ctx, job, path, by="auto")
                elif record.get("receipt"):
                    self.show_steer_receipt(ctx, record)  # Its 已等 N 秒, and the button.
            except Exception as exc:  # noqa: BLE001 - the supplement is delivered all the same
                self.runtime.log(f"unread supplement watch failed job=#{job.job_id}: {type(exc).__name__}")

    def interrupt(self, ctx, job, path, *, by):
        """Stop the command ``job``'s model waits on, so the supplement of ``path`` is read now; ``by``: "auto" (urgent)
        or "click" (打断它，马上读). Tried once a supplement; the record keeps what became of it. Returns the record."""
        control = job.backend_control
        record = self._unread(job, control, path)
        if record is None:
            return read_delivery(path) or {}
        if record.get("interrupt_at"):
            return record
        record = update_delivery(path, interrupt_at=time.time(), interrupt_by=by) or record
        stop = getattr(control, "stop_command", None)
        outcome = stop(NOTE) if callable(stop) else {"reason": "这个后端不能停命令"}
        stopped = [str(command) for command in outcome.get("stopped") or []]
        changes = {"interrupt": stop_words(outcome)}
        if not stopped and not outcome.get("kept"):
            changes["interrupt_at"] = None  # Nothing to stop now (the model writes, or waits on a subtask): may be tried again.
        if stopped:
            changes["interrupted"] = " ".join(stopped[0].split())[:120]
        record = update_delivery(path, **changes) or read_delivery(path) or record
        self.runtime.log(f"supplement interrupt job=#{job.job_id} by={by} "
                         f"outcome={'stopped' if stopped else 'kept' if outcome.get('kept') else 'none'}")
        if stopped:
            try:
                self.note_latest_request(ctx, job, record.get("text") or "", record.get("mid") or "",
                                         after="已停掉正在跑的命令，马上读到")
            except Exception as exc:  # noqa: BLE001 - the command is stopped; only the card line failed
                self.runtime.log(f"latest supplement not shown: {type(exc).__name__}")
        if record.get("receipt"):
            self.show_steer_receipt(ctx, record)
        return record
