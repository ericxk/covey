"""Long-task status: the thread that watches a running turn (TaskMonitor): elapsed time, real subprocess health and
durable milestones, drawn as the status line by core/status_line.py."""

from __future__ import annotations

import datetime as dt
import threading
import time
from pathlib import Path

from covey.core.checkpoint import checkpoint_delivery, checkpoint_steps, clock_steps, read_checkpoint
from covey.core.metrics import run_start
from covey.core.turn_words import START_PHASE, UNSET_PHASES
from covey.core import turn_words
from covey.core.state import utc_now_iso
from covey.core.status_line import (REASONING, THINKING, WAITING_FOR_MODEL, Facts, status_summary,
                                             status_text, step_text, trace_progress)


# The phase of the checkpoint a turn starts with (app/worker.py): until the model writes its own, the status line
# names no phase (core/turn_words.py UNSET_PHASES).
SEED_PHASE = turn_words.SEED_PHASE


class TaskMonitor:
    def __init__(
        self,
        job,
        checkpoint: Path,
        update,
        persist,
        notify,
        log,
        end_stream,
        clock=time.monotonic,
        long_after=180,
        long_interval=60,
        wall=time.time,
        starting="",
        state=None,
        images=None,
    ):
        """``state``: told (phase, asking) when the checkpoint's phase ("" before one) or a model's question
        waiting for its answer changes, for the card's chat-list preview. ``images``: told the checkpoint each time
        it changes and once more as the monitor stops (app/turn_images.py: the images it lists to show at once)."""
        self.job, self.checkpoint = job, checkpoint
        self.state, self.last_state = state, None
        self.images = images
        self.update, self.persist, self.notify, self.log = update, persist, notify, log
        self.end_stream, self.clock, self.wall = end_stream, clock, wall
        self.long_after, self.long_interval = long_after, long_interval
        self.started = clock()
        self.last_progress = self.started
        self.progress_seen = False  # A checkpoint change or progress text, not just the start.
        self.output_at = None  # Wall time of the main task's latest streamed answer text.
        self.reasoning_at = None  # Wall time the backend last said the model began reasoning.
        # Until the backend says its first model call started, the status line shows this (if set).
        self.starting = starting
        self.requested_at = None
        self.phase = START_PHASE
        self.last_event = "尚无阶段进展"
        self.stopped = threading.Event()
        self.lock = threading.RLock()
        self.thread = threading.Thread(target=self.run, daemon=True, name="task-monitor")
        self.frame = 0
        self.last_ui = float("-inf")
        self.last_checkpoint = None
        self.long_mode = False
        self.last_retry = None
        # The checkpoint's steps and delivery (core/checkpoint.py), and when the Bridge saw each step start and end,
        # kept on the job's record once the checkpoint had them (``step_fields``).
        self.steps, self.step_times, self.delivery, self.stepped = [], {}, {}, False

    def output(self):
        """Streamed answer text is progress. Lock-free: tick holds the lock across card I/O."""
        self.output_at = self.wall()

    def reasoning(self):
        """The model began reasoning that streams no text: progress, and no longer waiting. Lock-free too."""
        self.reasoning_at = self.wall()

    def request(self):
        """The backend's first model call started: no longer starting. Lock-free too."""
        if self.requested_at is None:
            self.requested_at = self.wall()

    def tick(self):
        with self.lock:
            if self.stopped.is_set():
                return  # A progress event or retry note after the run: the line has its last words.
            now = self.clock()
            facts, changed = self.facts(now)
            summary = status_summary(facts)
            shown = (facts.phase if facts.phase not in UNSET_PHASES else "",
                     bool(facts.waiting and facts.native_question_active))
            if self.state is not None and shown != self.last_state:
                self.last_state = shown  # Not retried when its write fails: the card's close writes its end.
                self.state(*shown)
            retry = facts.retry
            retry_changed = retry != self.last_retry
            self.last_retry = dict(retry)
            self.persist(
                elapsed_seconds=int(facts.elapsed),
                last_progress_age_seconds=int(facts.quiet),
                process_alive=facts.alive,
                health_checked_at=utc_now_iso(),
                phase=self.phase,
                long_task_summary=summary,
                checkpoint_path=str(self.checkpoint),
                **self.step_fields(),
            )
            if facts.elapsed >= self.long_after and not self.long_mode:
                self.end_stream()
                self.long_mode = True
            interval = self.long_interval if self.long_mode else 2
            if changed or retry_changed or now - self.last_ui >= interval:
                self.last_ui = now
                self.update(status_text(facts))
                self.frame += 1

    def step_fields(self):
        """The record's ``steps``, ``step_times`` and ``delivery``: nothing for a checkpoint that never had them, so
        a record without them is as before; written on, emptied, once it had them."""
        fields = {"steps": self.steps, "step_times": self.step_times} if self.stepped else {}
        if self.delivery:
            fields["delivery"] = self.delivery
        return fields

    def facts(self, now):
        """(Facts, whether the checkpoint changed): the checkpoint, the backend process and the trace at
        ``now`` (``clock``). The caller holds the lock. 已运行 counts from the turn's run start, as its 用时 does
        (core/metrics.py run_start: the backend's request), else from the monitor's start."""
        begun = run_start(getattr(self.job, "timings", None))
        elapsed = max(0.0, self.wall() - begun) if begun is not None else now - self.started
        cp, changed = self._checkpoint_progress(now)
        process = self.job.backend_process
        returncode = process.poll() if process is not None else None
        alive = returncode is None if process is not None else None
        quiet = now - self.last_progress
        wall = self.wall()
        activity = self.trace_activity(wall) if self.job.tool_trace is not None else None
        if activity is not None:
            # With a trace, progress is what the backend reported, sub-tasks included.
            ages = [wall - stamp for stamp in (activity[1], self.output_at, self.reasoning_at, self.requested_at)
                    if stamp is not None]
            quiet = max(0, min(ages + [quiet] if self.progress_seen or not ages else ages))
        question = str(cp.get("question") or "").strip()
        retry = self.job.backend_retry
        steps, step_doing = step_text(self.steps, self.step_times, wall)
        return Facts(
            elapsed=elapsed, quiet=quiet, alive=alive, finished=alive is False and returncode == 0,
            phase=self.phase, last_event=self.last_event,
            checked=dt.datetime.now().astimezone().strftime('%m-%d %H:%M:%S'),
            question=question, waiting=cp.get("requires_user") is True and bool(question),
            native_question_active=bool(self.job.native_question_active), activity=activity,
            traced=self.job.tool_trace is not None, wall=wall,
            stirred=(self.progress_seen or self.output_at is not None or self.reasoning_at is not None
                     or self.requested_at is not None),
            retry=retry, epoch=time.time(), long_mode=self.long_mode or elapsed >= self.long_after,
            frame=self.frame, steps=steps, step_doing=step_doing,
        ), changed

    def _checkpoint_progress(self, now):
        """The checkpoint, and whether it changed beyond its timestamp (which counts as progress)."""
        cp = read_checkpoint(self.checkpoint)
        material = {k: v for k, v in cp.items() if k != "updated_at"}
        changed = bool(material and material != self.last_checkpoint)
        if changed:
            self.last_checkpoint = material
            self.phase = str(cp.get("phase") or self.phase)[:300]
            self.last_event = "阶段记录已更新：" + self.phase
            self.last_progress = now
            self.progress_seen = True
            self.steps = checkpoint_steps(cp)
            self.stepped = self.stepped or bool(self.steps)
            self.step_times = clock_steps(self.steps, self.step_times, int(self.wall()))
            self.delivery = checkpoint_delivery(cp) or self.delivery
            if self.images is not None:
                self.images(cp)
        return cp, changed

    def trace_activity(self, now):
        """(current, newest progress stamp, its phrase) from the trace, so a long command or a busy
        sub-task reads as work, not as a hang."""
        import contextlib
        trace = self.job.tool_trace
        if not getattr(trace, "steps", None) and not getattr(trace, "children", None):
            # Without steps (a backend with no steps, an answer with no tool call), a first token, token usage or streamed
            # output says the model has started; a reasoning start, that it is at work. After a reasoning
            # start the first token may be that start (Codex): then only output or tokens move on.
            started = (self.output_at is not None or bool(getattr(trace, "output_tokens", None))
                       or (getattr(trace, "ttft_seconds", None) is not None and self.reasoning_at is None))
            current = (THINKING if started else REASONING if self.reasoning_at is not None
                       else self.starting if self.starting and self.requested_at is None else WAITING_FOR_MODEL)
            return current, None, None
        with self.job.trace_lock or contextlib.nullcontext():
            return trace_progress(trace, now)

    def run(self):
        while not self.stopped.is_set() and not self.job.cancel_event.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - UI failure must not stop computation.
                self.log(f"task monitor update failed: {exc}")
            if self.stopped.wait(15 if self.long_mode else 2):
                break

    def start(self):
        self.thread.start()

    def stop(self):
        """No more ticks, including one under way on another thread. The status line keeps its text
        until the worker writes the run's outcome over it (app/worker.py), so it is never blank."""
        self.stopped.set()
        self.thread.join()
        with self.lock:
            if self.images is not None:  # One written as the turn ended: its last tick may have been before it.
                self.images(read_checkpoint(self.checkpoint))
