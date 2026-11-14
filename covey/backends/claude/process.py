"""The Claude CLI's process and its JSONL transport: one frame per row each way, bounded (MAX_FRAME), writes never
replayed, and the transport failures by their reason in the run's diagnostics (TRANSPORT_FAILURES)."""

from __future__ import annotations

import json
import os
import queue
import selectors
import signal
import subprocess
import threading
import time
import uuid

from covey.core.harness import JobCancelled
from covey.core.process import cleanup_group, process_identity, stop_group


MAX_FRAME = 8 * 1024 * 1024
IMAGE_FRAME = MAX_FRAME - 4096  # What the prompt and its base64 images may take in the one input frame.


# The transport failures of the CLI's conversation by their reason in the run's diagnostics. Each is a
# RuntimeError with this message: the diagnostics read the reason back from it (transport_failure_details).
TRANSPORT_FAILURES = {
    "process_closed": "Claude process is closed",
    "input_write_timeout": "Claude input delivery is uncertain; do not replay",
    "input_delivery_uncertain": "Claude input delivery is uncertain",
    "initialize_rejected": "Claude initialization failed",
    "initialize_unexpected_event": "Unexpected Claude initialization event",
    "initialize_timeout": "Claude initialization timed out",
}


_REASONS = {message: reason for reason, message in TRANSPORT_FAILURES.items()}


def transport_error(reason):
    """The RuntimeError of a transport failure (TRANSPORT_FAILURES)."""
    return RuntimeError(TRANSPORT_FAILURES[reason])


def transport_failure_details(error, stage, process):
    """Private, allowlisted transport metadata; never include input or stderr."""
    result = {
        "stage": stage,
        "exception": type(error).__name__,
        "reason": _REASONS.get(str(error), "os_error" if isinstance(error, OSError) else "runtime_error"),
    }
    errno = getattr(error, "errno", None)
    if type(errno) is int:
        result["errno"] = errno
    if process is not None:
        code = process.process.poll()
        if type(code) is int:
            result["process_exit_code"] = code
        for name in ("write_size", "write_offset"):
            value = getattr(process, name, None)
            if type(value) is int:
                result[name] = value
    return result


def save_transport_details(state, details):
    """Add ``details`` to the run's diagnostics. Only a diagnosis: failing to save them never changes
    how the run ended (it would have turned a transport failure into the job's own exception)."""
    if not details:
        return
    from covey.core.state import write_private_text
    path = state.ctx.state_dir / "claude-diagnostics" / (state.run_id + ".json")
    try:
        if not path.is_file():
            return
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            return
        data["transport"] = details
        write_private_text(path, json.dumps(data, ensure_ascii=False))
    except (OSError, ValueError, TypeError):
        pass


class ClaudeProcess:
    def __init__(self, argv, ctx, env, *, cancel=None, observe=None):
        self.cancel = cancel or threading.Event()
        self.deadline = time.monotonic() + ctx.timeout if ctx.timeout > 0 else None
        self.events = queue.Queue(maxsize=128)
        self.write_lock = threading.RLock()
        self.stop = threading.Event()
        self.prelude = []
        self.process = subprocess.Popen(
            argv,
            cwd=str(ctx.workdir),
            env=env,
            start_new_session=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self.identity = process_identity(self.process.pid)
        self.policy_stop = False
        os.set_blocking(self.process.stdin.fileno(), False)
        self.reader = threading.Thread(target=self._read, daemon=True, name="claude-jsonl")
        self.reader.start()
        try:
            (observe or (lambda _: None))(self.process)
        except BaseException:
            self.close()
            raise

    def _put(self, row):
        while not self.stop.is_set():
            try:
                self.events.put(row, timeout=0.1)
                return
            except queue.Full:
                pass

    def _read(self):
        try:
            while not self.stop.is_set():
                line = self.process.stdout.readline(MAX_FRAME + 1)
                if not line:
                    break
                if len(line) > MAX_FRAME:
                    raise ValueError("Claude event exceeds size limit")
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("Invalid Claude event")
                row["_bridge_received_at"] = time.time()
                self._put(row)
        except (OSError, ValueError):
            self._put({"type": "bridge_error"})
        finally:
            self._put({"type": "bridge_eof"})

    def check(self):
        if self.cancel.is_set():
            raise JobCancelled("Claude task cancelled")
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise subprocess.TimeoutExpired("claude", 0)

    def send(self, row):
        data = (json.dumps(row, ensure_ascii=False) + "\n").encode()
        self.write_size, self.write_offset = len(data), 0
        if len(data) > MAX_FRAME:
            raise ValueError("Claude input exceeds size limit")
        deadline = time.monotonic() + 10
        with self.write_lock, selectors.DefaultSelector() as selector:
            self.check()
            if self.stop.is_set() or self.process.poll() is not None or self.process.stdin.closed:
                raise transport_error("process_closed")
            selector.register(self.process.stdin, selectors.EVENT_WRITE)
            offset = 0
            while offset < len(data):
                self.check()
                if time.monotonic() >= deadline:
                    raise transport_error("input_write_timeout")
                if not selector.select(0.1):
                    continue
                try:
                    offset += os.write(self.process.stdin.fileno(), data[offset:])
                    self.write_offset = offset
                except BlockingIOError:
                    continue

    def receive(self, timeout=0.1):
        self.check()
        try:
            return self.events.get(timeout=timeout)
        except queue.Empty:
            return None

    def initialize(self):
        rid = str(uuid.uuid4())
        self.send(
            {
                "type": "control_request",
                "request_id": rid,
                "request": {"subtype": "initialize", "forwardSubagentText": True},
            }
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            row = self.receive()
            if row is None:
                continue
            if row.get("type") == "control_response":
                response = row.get("response") or {}
                if response.get("request_id") != rid:
                    raise ValueError("Claude initialization response identity mismatch")
                data = response.get("response")
                if response.get("subtype") != "success" or not isinstance(data, dict):
                    raise transport_error("initialize_rejected")
                return data
            # No user input has been submitted; a result or tool request here
            # cannot be attributed to this job.
            if row.get("type") not in {"system", "rate_limit_event"}:
                raise transport_error("initialize_unexpected_event")
            self.prelude.append(row)
            if len(self.prelude) > 64:
                raise ValueError("Too many Claude initialization events")
        raise transport_error("initialize_timeout")

    def end_input(self):
        with self.write_lock:
            if not self.process.stdin.closed:
                self.process.stdin.close()

    def close(self):
        self.stop.set()
        self.end_input()
        stop_group(self.process, 3)
        if self.policy_stop:
            # The CLI can exit on TERM while a child ignores it with stdout closed.
            # Match the original private session/group and start ticks before killing it.
            cleanup_group(self.identity)
        self.reader.join(timeout=2)
        # A descendant may still hold stdout. Never block on the buffered
        # reader's lock while trying to close it from a different thread.
        if self.reader.is_alive():
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.reader.join(timeout=2)
        if not self.reader.is_alive():
            self.process.stdout.close()
        return self.process.poll() is not None and not self.reader.is_alive()
