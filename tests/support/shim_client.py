"""A hub's side of the device shim for tests: one-shot commands and attach connections to a shim run for real
under a temporary HOME (real supervisors, setsid and signals)."""
from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from support import REPO

SHIM = REPO / "covey" / "core" / "device_shim.py"


class Reply:
    """What a command answered: ``head`` (the JSON line), ``body`` (the bytes after a get's header), ``junk``
    (what came before the hi frame), ``hi`` and the process's exit code."""

    def __init__(self, head, body=b"", hi=None, code=0, stderr=b""):
        self.head, self.body, self.hi, self.code, self.stderr = head, body, hi, code, stderr

    def __getitem__(self, key):
        return self.head[key]

    def get(self, key, default=None):
        return self.head.get(key, default)

    @property
    def ok(self):
        return self.head.get("ok") is True

    @property
    def err(self):
        return self.head.get("err")


class Frames:
    """An attach connection: the shim's process with a reader thread collecting its lines."""

    def __init__(self, process):
        self.process = process
        self.lines = queue.Queue()
        self.seen = []
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for raw in iter(self.process.stdout.readline, b""):
            self.lines.put(raw)
        self.lines.put(None)

    def next(self, timeout=10.0):
        """The next frame (a dict), or None at EOF; raises queue.Empty after ``timeout``."""
        raw = self.lines.get(timeout=timeout)
        if raw is None:
            return None
        frame = json.loads(raw)
        self.seen.append(frame)
        return frame

    def until(self, predicate, timeout=20.0):
        """Frames up to and including the first that satisfies ``predicate``; None when the stream ended first."""
        found, deadline = [], time.time() + timeout
        while True:
            frame = self.next(max(0.05, deadline - time.time()))
            if frame is None:
                return None
            found.append(frame)
            if predicate(frame):
                return found

    def records(self, timeout=20.0):
        """Every record frame (those with an offset) up to the end of the stream."""
        found, deadline = [], time.time() + timeout
        while True:
            frame = self.next(max(0.05, deadline - time.time()))
            if frame is None:
                return found
            if "o" in frame and frame.get("k") != "hb":
                found.append(frame)

    def send(self, frame):
        self.process.stdin.write(json.dumps(frame).encode() + b"\n")
        self.process.stdin.flush()

    def kill(self):
        """Like ssh dying: SIGKILL, no goodbye."""
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except OSError:
            pass
        self.process.wait(10)

    def close(self):
        if self.process.poll() is None:
            self.kill()
        for stream in (self.process.stdin, self.process.stdout):
            try:
                stream.close()
            except OSError:
                pass


class DeviceShim:
    """A shim copy under ``home`` (a temporary directory standing in for a device's HOME)."""

    def __init__(self, home: Path, hub: str = "hub1", shim: Path = SHIM):
        self.home, self.hub, self.shim = Path(home), hub, Path(shim)
        self.tasks: list[str] = []
        self.attaches: list[Frames] = []
        self.env = {"HOME": str(self.home), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    "PYTHONDONTWRITEBYTECODE": "1", "LANG": "C"}

    @property
    def tasks_root(self) -> Path:
        return self.home / ".covey" / "tasks" / self.hub

    def task_dir(self, task: str) -> Path:
        return self.tasks_root / task

    def argv(self, cmd):
        return [sys.executable, "-B", str(self.shim), cmd]

    def call(self, cmd, data: bytes = b"", client_timeout=60.0, **fields) -> Reply:
        """Run one command: its JSON line (plus ``data`` and a newline for a put) on stdin, the answer parsed.
        ``client_timeout`` is the test's patience; the request's own ``timeout`` field goes through ``fields``."""
        fields = {"cmd": cmd, "id": "t-%d" % time.time_ns(), **fields}
        if cmd in ("spawn", "attach", "send", "status", "stop", "reap") and "pgid" not in fields:
            fields.setdefault("hub", self.hub)
        stdin = json.dumps(fields).encode() + b"\n" + (data + b"\n" if cmd == "put" else b"")
        done = subprocess.run(self.argv(cmd), input=stdin, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env=self.env, timeout=client_timeout, start_new_session=True)
        out = done.stdout
        hi_line, _, rest = out.partition(b"\n")
        head_line, _, body = rest.partition(b"\n")
        head = json.loads(head_line) if head_line else {}
        if "len" in head and cmd == "get":
            body = body[:head["len"]]
        if cmd == "spawn" and fields.get("task"):
            self.tasks.append(fields["task"])
        return Reply(head, body, json.loads(hi_line), done.returncode, done.stderr)

    def spawn(self, task, argv, **fields) -> Reply:
        fields.setdefault("cwd", str(self.home))
        return self.call("spawn", task=task, argv=argv, **fields)

    def attach(self, task, offset=0) -> Frames:
        process = subprocess.Popen(self.argv("attach"), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, env=self.env, start_new_session=True)
        frames = Frames(process)
        frames.send({"cmd": "attach", "id": "a", "hub": self.hub, "task": task, "from": offset})
        self.attaches.append(frames)
        return frames

    def wait_exit(self, task, timeout=20.0):
        """The task's exit.json once it exists."""
        path = self.task_dir(task) / "exit.json"
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                return json.loads(path.read_text())
            except (OSError, ValueError):
                time.sleep(0.05)
        raise TimeoutError("no exit.json for %s" % task)

    def stray_processes(self) -> list[int]:
        """Processes of this device still running: any whose command line or environment names its HOME."""
        mine, found = str(self.home).encode(), []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit() or int(entry.name) == os.getpid():
                continue
            try:
                state = (entry / "stat").read_text().rsplit(")", 1)[1].split()[0]
                if state in "ZX":
                    continue
                if mine in (entry / "cmdline").read_bytes() or mine in (entry / "environ").read_bytes():
                    found.append(int(entry.name))
            except (OSError, IndexError):
                continue
        return found

    def cleanup(self, grace=1.0) -> list[int]:
        """Close the attaches, reap every task this shim spawned, kill what is left; the pids that needed that."""
        for frames in self.attaches:
            frames.close()
        for task in self.tasks:
            try:
                self.call("reap", task=task, grace=grace, client_timeout=30)
            except (subprocess.SubprocessError, ValueError):
                pass
        deadline = time.time() + 3
        while self.stray_processes() and time.time() < deadline:
            time.sleep(0.1)
        left = self.stray_processes()
        for pid in left:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        return left
