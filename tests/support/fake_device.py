"""A fake device for tests: the real shim (covey/core/device_shim.py) run under a temporary HOME through
``LocalTransport`` (core/device_transport.py: the same command lines as over ssh, real supervisors, setsid and
signals), plus faults a flaky link would cause.

    device = FakeDevice()                      # pushes the shim into its temporary HOME, like a first connection
    device.spawn("t1", device.harness("--count", "5"))
    link = device.transport.attach("t1")       # an Attachment; frames from the fake Agent
    device.kill_attaches()                     # ssh dies under the attach
    device.faults.latency = 0.2                # every call and every frame waits 0.2 s
    device.faults.drop(2)                      # the next 2 record frames never reach the hub (the offsets show a gap)
    device.faults.fail(1)                      # the next call or attach fails as if ssh could not connect
    left = device.cleanup()                    # reaps every task, closes every attach; the pids it had to kill

Test modules call ``cleanup`` last and assert that nothing was left (``[]``).
"""
from __future__ import annotations

import os
import signal
import sys
import tempfile
import time
from pathlib import Path

from covey.core import devices
from covey.core.device_transport import Attachment, LocalTransport, Unreachable, shim_bytes

from support import REPO
from support.shim_client import DeviceShim

HARNESS = REPO / "tests" / "support" / "fake_harness.py"


class Faults:
    """What the link does wrong; the transport and its attachments read it on every call and frame."""

    def __init__(self):
        self.latency = 0.0  # Seconds slept before each call, attach and received frame.
        self.drop_count = 0
        self.drop_kinds = ("out",)
        self.fail_count = 0
        self.failed = 0  # How many calls were failed so far.
        self.dropped = 0

    def drop(self, count=1, kinds=("out",)):
        """The next ``count`` frames whose ``k`` is in ``kinds`` are lost on their way to the hub."""
        self.drop_count, self.drop_kinds = count, tuple(kinds)

    def fail(self, count=1):
        """The next ``count`` calls or attaches raise ``Unreachable``."""
        self.fail_count = count


class FaultyAttachment(Attachment):
    faults = None

    def filter(self, frame):
        faults = self.faults
        if faults is None:
            return frame
        if faults.latency:
            time.sleep(faults.latency)
        if faults.drop_count > 0 and frame.get("k") in faults.drop_kinds and "o" in frame:
            faults.drop_count -= 1
            faults.dropped += 1
            return None
        return frame


class FaultyTransport(LocalTransport):
    """``LocalTransport`` with ``faults`` and a list of the attachments it opened."""

    attachment_class = FaultyAttachment

    def __init__(self, home, faults, **kwargs):
        super().__init__(home, **kwargs)
        self.faults = faults
        self.attachments = []
        self.log = []  # (command, fields) of every ``call``: what the hub asked the device.

    def before(self, what):
        if self.faults.latency:
            time.sleep(self.faults.latency)
        if self.faults.fail_count > 0:
            self.faults.fail_count -= 1
            self.faults.failed += 1
            raise Unreachable("injected: ssh could not connect (%s)" % what)

    def call(self, cmd, **fields):
        self.log.append((cmd, dict(fields)))
        return super().call(cmd, **fields)

    def attach(self, task, offset=0, **kwargs):
        connection = super().attach(task, offset, **kwargs)
        connection.faults = self.faults
        self.attachments.append(connection)
        return connection


class FakeDevice:
    """A device (temporary HOME) with the shim pushed, a ``transport`` to it and its ``faults``."""

    def __init__(self, hub="hub1", *, push=True, env=None):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name).resolve()
        self.faults = Faults()
        self.transport = FaultyTransport(self.home, self.faults, hub=hub, env=env)
        self.shim = DeviceShim(self.home, hub)  # Only for the scan of stray processes and the task directories.
        self.tasks = []
        if push:
            devices.push_file(self.transport, shim_bytes())

    @property
    def hub(self):
        return self.transport.hub

    def harness(self, *flags):
        """argv of the scripted fake Harness (tests/support/fake_harness.py)."""
        return [sys.executable, str(HARNESS), *flags]

    def task_dir(self, task):
        return self.transport.task_dir(task)

    def spawn(self, task, argv, **fields):
        fields.setdefault("cwd", str(self.home))
        answer = self.transport.call("spawn", task=task, argv=argv, **fields)
        self.tasks.append(task)
        return answer

    def kill_attaches(self):
        """ssh dying under every attach that is open: SIGKILL, no goodbye. How many were alive."""
        killed = 0
        for connection in self.transport.attachments:
            if connection.alive:
                connection.kill()
                killed += 1
        return killed

    def cleanup(self, grace=1.0):
        """Close the attaches, reap every task spawned through ``spawn``, kill what is left and remove the HOME; the
        pids that needed killing (empty when the test left nothing behind)."""
        self.faults.latency, self.faults.fail_count = 0.0, 0
        for connection in self.transport.attachments:
            connection.close(detach=False)
        for task in self.tasks:
            try:
                self.transport.call("reap", task=task, grace=grace, patience=30)
            except Exception:  # A task that never started, or whose directory the test removed.
                pass
        deadline = time.time() + 3
        while self.shim.stray_processes() and time.time() < deadline:
            time.sleep(0.1)
        left = self.shim.stray_processes()
        for pid in left:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        self.temp.cleanup()
        return left
