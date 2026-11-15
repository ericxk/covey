"""A Claude Code kept ready for the track judge (app/track_judge.py): one process, started and primed before the message
that needs it, used for exactly one question and thrown away; the next one is started right after.

Why: a cold one-shot spends about a second on starting the CLI and its first request is slower than the ones after it
(measured 2026-11-16, docs/main-thread.md 多路并行). The standby is started in ``stream-json`` mode, answers one fixed,
invented priming message (so its first request and its connection are warm, and no word of the owner is ever in it),
and then waits on standard input. ``take`` hands it out once; ``Standby.ask`` writes the one real question and reads
the answer. Because it answers one question, nothing of an earlier message is in its context.

Safety: the process leads a process group of its own, killed outright (SIGKILL on the group) when it is used, past its
limit, idle for IDLE_S (a timer thread, one per standby, only while one exists), older than MAX_AGE_S when the next
``warm`` comes, at ``shutdown`` (the Bridge's stop, app/unified.py ``stop_children``) and at interpreter exit. A Bridge that
dies hard closes its standard input, which ends the child on its own. Nothing here starts a process unless ``warm`` is
called, and only the track router calls it (COVEY_MAIN_TRACKS=1). Standard library and core/process.py only.
"""
from __future__ import annotations

import atexit
import json
import queue
import signal
import subprocess
import threading
import time

from covey.core.process import signal_group

IDLE_S = 600  # A standby nobody asked for this long is killed; the next message (``warm``) starts another.
MAX_AGE_S = 1800  # ``warm`` replaces one this old (its environment is the Bridge's of the time it started).
PRIME = json.dumps({"message": "顺便查一下今天上海天气", "tasks": [{"index": 1, "phase": "running", "request": "整理缓存淘汰策略对比表"}]},
                   ensure_ascii=False)
STDERR_KEPT = 300


class Failed(Exception):
    """A reason the model's answer cannot be used (``reason`` is what the log line says; track_judge raises the same)."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(reason)
        self.reason, self.detail = reason, detail


def user_line(text: str) -> bytes:
    """One user message in Claude Code's ``--input-format stream-json``."""
    return (json.dumps({"type": "user", "message": {"role": "user", "content": text}}, ensure_ascii=False) + "\n").encode()


class Standby:
    """One started process: ``signature`` says what it was started with (argv and directory)."""

    def __init__(self, argv, env, cwd, signature, idle_s: float = IDLE_S):
        self.signature, self.idle_s = signature, idle_s
        self.started = time.monotonic()
        self.used = False
        self.results: queue.Queue = queue.Queue()  # The ``result`` events, then None when the output ends.
        self.seen = 0  # Results read so far: the first is the priming's.
        self.err = ""
        self.timer: threading.Timer | None = None
        self.lock = threading.Lock()
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                                     cwd=cwd, start_new_session=True)
        threading.Thread(target=self.read_out, name="track-standby-out", daemon=True).start()
        self.err_reader = threading.Thread(target=self.read_err, name="track-standby-err", daemon=True)
        self.err_reader.start()
        try:
            self.proc.stdin.write(user_line(PRIME))
            self.proc.stdin.flush()
        except OSError:
            pass  # It died at once; ``alive`` says so and ``ask`` reports it.
        self.touch()

    def read_out(self) -> None:
        try:
            for line in self.proc.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict) and event.get("type") == "result":
                    self.results.put(event)
        except (OSError, ValueError):
            pass
        finally:
            self.results.put(None)

    def read_err(self) -> None:
        try:
            for line in self.proc.stderr:
                self.err = (self.err + line.decode("utf-8", "replace"))[-STDERR_KEPT:]
        except (OSError, ValueError):
            pass

    def alive(self) -> bool:
        return self.proc.poll() is None

    def touch(self) -> None:
        """Start the idle count again."""
        with self.lock:
            if self.timer is not None:
                self.timer.cancel()
            self.timer = threading.Timer(self.idle_s, self.kill)
            self.timer.daemon = True
            self.timer.start()

    def kill(self) -> None:
        """Gone, with its process group; safe to call again."""
        with self.lock:
            timer, self.timer = self.timer, None
        if timer is not None:
            timer.cancel()
        signal_group(self.proc, signal.SIGKILL)
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        for pipe in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                pipe.close()
            except (OSError, ValueError):
                pass

    def ask(self, text: str, limit: float) -> str:
        """The answer to ``text`` as the JSON of Claude Code's ``result`` event (the shape its one-shot ``json`` output
        has), within ``limit`` seconds; the process is killed afterwards in every case (it answers one question).
        Failed: timeout, or the process ended (exit_nonzero)."""
        deadline = time.monotonic() + limit
        try:
            try:
                self.proc.stdin.write(user_line(text))
                self.proc.stdin.flush()
            except (OSError, ValueError):
                raise Failed("exit_nonzero", self.exit_detail()) from None
            while True:
                try:
                    event = self.results.get(timeout=max(0.0, deadline - time.monotonic()))
                except queue.Empty:
                    raise Failed("timeout") from None
                if event is None:
                    raise Failed("exit_nonzero", self.exit_detail())
                self.seen += 1
                if self.seen > 1:  # The first result answered the priming.
                    return json.dumps(event, ensure_ascii=False)
        finally:
            self.kill()

    def exit_detail(self) -> str:
        try:
            code = self.proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            code = "?"
        self.err_reader.join(timeout=1)
        return f"code={code} " + self.err.strip()


_lock = threading.Lock()
_current: Standby | None = None
_exit_hook = False


def warm(argv, env, cwd, signature, idle_s: float = IDLE_S) -> bool:
    """Make sure a standby is on its way: a live unused one with this ``signature`` is only kept (its idle count starts
    again) unless it is older than MAX_AGE_S; otherwise it is replaced by a new one. True when a process was started.
    A process that cannot start is not an error here (the judge starts a one-shot instead)."""
    global _current, _exit_hook
    stale = None
    with _lock:
        have = _current
        if have is not None and not have.used and have.alive() and have.signature == signature \
                and time.monotonic() - have.started < MAX_AGE_S:
            have.touch()
            return False
        stale, _current = have, None
        try:
            _current = Standby(argv, env, cwd, signature, idle_s)
        except OSError:
            _current = None
        if not _exit_hook:
            _exit_hook = True
            atexit.register(shutdown)
        started = _current is not None
    if stale is not None:
        stale.kill()
    return started


def take(signature) -> Standby | None:
    """The standby, handed out once (it is no longer ``current``); None when there is none that is alive, unused and
    started with ``signature``. One that is not usable is killed."""
    global _current
    with _lock:
        have, _current = _current, None
    if have is None:
        return None
    if have.signature != signature or not have.alive():
        have.kill()
        return None
    have.used = True
    return have


def shutdown() -> None:
    """Kill the standby, if any (the Bridge's stop, interpreter exit)."""
    global _current
    with _lock:
        have, _current = _current, None
    if have is not None:
        have.kill()
