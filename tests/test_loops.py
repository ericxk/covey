"""core/loops.py: the Bridge's worker threads (supervised) and the periodic loop most of them run (every)."""
from __future__ import annotations

import threading
from types import SimpleNamespace
import unittest

from covey.core.loops import every, supervised


class Shutdown(threading.Event):
    """Set after ``rounds`` timed waits, each recorded and returned at once."""

    def __init__(self, rounds):
        super().__init__()
        self.rounds, self.waits = rounds, []

    def wait(self, timeout=None):
        if timeout is None:
            return super().wait()
        self.waits.append(timeout)
        if len(self.waits) >= self.rounds:
            self.set()
        return self.is_set()


def runtime(rounds):
    logs = []
    return SimpleNamespace(shutdown=Shutdown(rounds), log=logs.append, logs=logs)


def failing(*args):
    raise KeyError("x")


class EveryTests(unittest.TestCase):
    def test_steps_run_in_turn_with_the_arguments_then_the_wait(self):
        rt, calls = runtime(2), []
        every(rt, 7, lambda *a: calls.append(("a", a)), lambda *a: calls.append(("b", a)), args=(1, 2))
        self.assertEqual(calls, [("a", (1, 2)), ("b", (1, 2))] * 2)
        self.assertEqual(rt.shutdown.waits, [7, 7])

    def test_an_isolated_step_that_fails_is_logged_and_the_next_runs(self):
        rt, calls = runtime(1), []
        every(rt, 5, failing, lambda: calls.append(1), isolate="outbox")
        self.assertEqual(rt.logs, [f"outbox step {__name__}.failing failed: KeyError"])
        self.assertEqual(calls, [1])

    def test_without_isolation_a_failure_ends_the_loop(self):
        rt, calls = runtime(5), []
        with self.assertRaises(KeyError):
            every(rt, 5, failing, lambda: calls.append(1))
        self.assertEqual((calls, rt.shutdown.waits, rt.logs), ([], [], []))

    def test_nothing_runs_after_shutdown(self):
        rt = runtime(1)
        rt.shutdown.set()
        every(rt, 5, failing)


class SupervisedTests(unittest.TestCase):
    def test_a_thread_restarts_its_target_after_a_failure(self):
        rt, calls = runtime(3), []

        def target(ctx):
            calls.append((ctx, threading.current_thread().name))
            raise OSError("down")
        thread = supervised(rt, "outbox", target, "CTX")
        thread.join(10)
        self.assertEqual((thread.name, thread.daemon, thread.is_alive()), ("outbox", True, False))
        self.assertEqual(calls, [("CTX", "outbox")] * 3)
        self.assertEqual(rt.logs, ["worker outbox failed: OSError; restarting"] * 3)
        self.assertEqual(rt.shutdown.waits, [5, 5, 5])


if __name__ == "__main__":
    unittest.main()
