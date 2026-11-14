"""Process doubles: a Popen stand-in with scripted poll results, waiting for a real process to end, and
what a test or a test module left running (``Leftovers``, ``leftover_check``)."""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace


class StubChild:
    """Stands in for Popen: no real process, signal or pipe is involved."""

    def __init__(self, *, poll_results=None, write_error=None, wait_error=None, returncode=0):
        self.pid = os.getpid()
        self._poll = list(poll_results if poll_results is not None else [returncode])
        self._wait_error = wait_error
        self.returncode = returncode
        self.stdin = SimpleNamespace(
            write=self._write if write_error is None else self._raise(write_error),
            close=lambda: None)
        self.killed = False

    @staticmethod
    def _raise(exc):
        def fail(_data):
            raise exc
        return fail

    def _write(self, _data):
        return len(_data)

    def poll(self):
        return self._poll[0] if len(self._poll) == 1 else self._poll.pop(0)

    def wait(self, timeout=None):
        if self._wait_error is not None:
            raise self._wait_error
        return self.returncode

    def kill(self):
        self.killed = True

    def send_signal(self, _sig):
        return None


def wait_gone(pid_file: Path, timeout: float = 10) -> bool:
    """True once the process whose pid is in ``pid_file`` has ended (gone or a zombie).

    The file must be written by the process itself before it can be signalled;
    a missing file counts as not started yet.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            pid = int(pid_file.read_text())
        except (OSError, ValueError):
            time.sleep(0.01)
            continue
        try:
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        except (OSError, IndexError):
            return True
        if state in {"Z", "X"}:
            return True
        time.sleep(0.01)
    return False


def child_pids() -> set[int]:
    """This process's child processes that still run (zombies left out), from /proc."""
    found = set()
    for stat in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = stat.read_text().rsplit(")", 1)[1].split()
        except (OSError, IndexError):
            continue
        if fields[0] not in {"Z", "X"} and int(fields[1]) == os.getpid():
            found.add(int(stat.parent.name))
    return found


class Leftovers:
    """The threads started after this was made, and with ``processes`` its child processes (a /proc scan)."""

    def __init__(self, processes=False):
        self.before, self.children = set(threading.enumerate()), child_pids() if processes else set()

    def threads(self, timeout: float = 0) -> list[threading.Thread]:
        """Those threads still running after waiting up to ``timeout`` seconds for them to end."""
        deadline = time.monotonic() + timeout
        for thread in self._started():
            thread.join(max(0.0, deadline - time.monotonic()))
        return [thread for thread in self._started() if thread.is_alive()]

    def _started(self):
        # A dummy thread stands for one threading did not start (it cannot be joined and never leaves the list).
        return [thread for thread in threading.enumerate()
                if thread not in self.before and not isinstance(thread, threading._DummyThread)]

    def processes(self, timeout: float = 0) -> set[int]:
        """Those child processes still running after waiting up to ``timeout`` seconds for them to end."""
        deadline = time.monotonic() + timeout
        while (running := child_pids() - self.children) and time.monotonic() < deadline:
            time.sleep(0.05)
        return running


def leftover_check(timeout: float = 1):
    """``setUpModule, tearDownModule = leftover_check()``: the module fails when a thread it started or a
    child process is still running ``timeout`` seconds after its last test. One left behind writes into
    a temporary directory that is already removed, while later modules run."""
    started = []

    def set_up():
        started[:] = [Leftovers(processes=True)]

    def tear_down():
        threads, children = started[0].threads(timeout), started[0].processes(timeout)
        if threads or children:
            names = sorted({thread.name for thread in threads})
            raise AssertionError(f"still running after the module: threads {names}, child processes {sorted(children)}")

    return set_up, tear_down
