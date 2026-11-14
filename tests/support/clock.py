"""Time without waiting: a fake clock, and a shutdown event that counts polling rounds."""
from __future__ import annotations

import threading
from collections import Counter


class FakeClock:
    """``monotonic()`` and ``time()`` that move only on ``sleep()`` or ``advance()``.

    Patch a module's ``time`` with it (``mock.patch.object(module, "time", clock)``)
    so only that module sees the fake time.
    """

    def __init__(self, start: float = 1000.0):
        self.now = start
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    time = monotonic

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        self.now += max(seconds, 0)


class CountingShutdown(threading.Event):
    """A shutdown event whose timed waits last ``tick`` seconds and are counted per thread name.

    A loop like ``while not shutdown.wait(1): ...`` then runs one round per tick,
    and a test can wait for whole rounds instead of sleeping for a fixed time.
    """

    def __init__(self, tick: float = 0.005):
        super().__init__()
        self.tick = tick
        self.rounds: Counter = Counter()
        self._changed = threading.Condition()

    def wait(self, timeout=None):
        with self._changed:
            self.rounds[threading.current_thread().name] += 1
            self._changed.notify_all()
        return super().wait(None if timeout is None else min(timeout, self.tick))

    def wait_rounds(self, thread_name: str, rounds: int = 2, timeout: float = 10) -> bool:
        """True once ``thread_name`` has started ``rounds`` more waits: the loop bodies
        between them ran entirely after this call."""
        with self._changed:
            target = self.rounds[thread_name] + rounds
            return self._changed.wait_for(lambda: self.rounds[thread_name] >= target, timeout)
