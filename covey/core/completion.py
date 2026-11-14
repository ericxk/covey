"""One post-root deadline, independent of model-request retry policy."""
from __future__ import annotations

import math
import time

DEFAULT_END_WAIT_SECONDS = 3600.0


class CompletionBudget:
    def __init__(self, task_deadline=None, end_wait_seconds=None, *, total_seconds=None,
                 default_wait=DEFAULT_END_WAIT_SECONDS, clock=time.monotonic):
        """`default_wait` applies when neither an explicit wait nor a task deadline is set;
        None waits without limit, as Claude does for background tasks still running."""
        for value in (task_deadline, end_wait_seconds, total_seconds):
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value <= 0):
                raise ValueError("invalid completion deadline")
        self.task_deadline, self.end_wait_seconds, self.clock = task_deadline, end_wait_seconds, clock
        self.total_seconds, self.default_wait = total_seconds, default_wait
        self.started_at = None
        self.deadline = None
        self.hinted = False

    def start(self):
        if self.started_at is not None:
            return
        self.started_at = self.clock()
        self.deadline = self._deadline(self.started_at)

    def renew(self):
        """A new root reply: the wait counts again from now, never past the total or the task deadline."""
        if self.started_at is None:
            self.start()
        else:
            self.deadline = self._deadline(self.clock())

    def _deadline(self, since):
        explicit = self.end_wait_seconds
        if explicit is None and self.task_deadline is None:
            explicit = self.default_wait
        limits = [v for v in (since + explicit if explicit is not None else None, self.task_deadline,
                              self.started_at + self.total_seconds if self.total_seconds is not None else None)
                  if v is not None]
        return min(limits) if limits else None

    def expired(self):
        return self.deadline is not None and self.clock() >= self.deadline

    def needs_hint(self):
        if self.started_at is not None and not self.hinted and self.clock() - self.started_at >= 30:
            self.hinted = True
            return True
        return False
