"""The Claude CLI's process and its JSONL transport: one frame per row each way, bounded (MAX_FRAME), writes never
replayed, and the transport failures by their reason in the run's diagnostics (TRANSPORT_FAILURES)."""

from __future__ import annotations

import collections
import json
import os
import queue
import re
import selectors
import signal
import subprocess
import threading
import time
import uuid

from covey.core.harness import JobCancelled
from covey.core.process import cleanup_group, device_of, process_identity, spawn_process, stop_group


STDERR_LINES = 40  # The CLI's last lines of stderr kept for the failure log (``ClaudeProcess.stderr_tail``).
STDERR_LINE_CHARS = 400
# Credentials a line of the CLI's stderr may quote, hidden before it is kept: well-known key shapes, ``name=value`` pairs
# whose name says secret, bearer tokens and any long unbroken token (a session's UUID has hyphens: it stays); plus the values of the child's own secret-named
# environment variables (``scrub``).
_SECRETS = re.compile(
    r"(?i)(sk-[a-z0-9_\-]{8,}|bearer\s+[a-z0-9._~+/=\-]{8,}"
    r"|(?:api[_-]?key|token|secret|passw(?:or)?d|authorization|credential)[\"']?\s*[=:]\s*[\"']?(?:bearer\s+)?[^\s\"',;]+"
    r"|[a-z0-9_+/=]{32,})")
_SECRET_NAME = re.compile(r"(?i)key|token|secret|passw|auth|credential")


def scrub(text, env=None) -> str:
    """``text`` with credentials hidden (the failure log's stderr lines): the values of ``env``'s secret-named variables
    wherever they appear, then what looks like one (``_SECRETS``); cut to STDERR_LINE_CHARS."""
    text = str(text)
    for name, value in (env or {}).items():
        if isinstance(value, str) and len(value) >= 6 and _SECRET_NAME.search(str(name)):
            text = text.replace(value, "***")
    text = _SECRETS.sub("***", text)
    return text if len(text) <= STDERR_LINE_CHARS else text[:STDERR_LINE_CHARS - 1] + "…"


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
    "device_offline": "Claude's device connection is down; the input was not sent",
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


def failure_event(outcome, details, stage, process, rc):
    """The ``backend_failure`` event's data for a run that did not complete (core/protocol.py): what failed and how the CLI
    ended, for the Bridge's log; None for a run that completed or was stopped. ``details``: ``transport_failure_details``
    of the exception that ended it, if one did. Public fields only: types, codes, the exit code and the CLI's last lines
    of stderr with credentials hidden (``scrub``), never the exception's own message (it may name the input) or the
    prompt."""
    if outcome is None or outcome.status in ("completed", "cancelled"):
        return None
    data = {"status": outcome.status, "code": outcome.error_code, "stage": stage}
    data.update({key: value for key, value in (details or {}).items() if key != "stage"})
    if "process_exit_code" not in data and type(rc) is int:
        data["exit_code"] = rc
    tail = getattr(process, "stderr_tail", None)
    data["stderr"] = list(tail) if isinstance(tail, (collections.deque, list, tuple)) else []
    return data


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
    device = False  # True for a process on a device (ctx.device), see __init__.

    def __init__(self, argv, ctx, env, *, cancel=None, observe=None):
        self.cancel = cancel or threading.Event()
        self.deadline = time.monotonic() + ctx.timeout if ctx.timeout > 0 else None
        self.events = queue.Queue(maxsize=128)
        self.write_lock = threading.RLock()
        self.stop = threading.Event()
        self.prelude = []
        # On a device (ctx.device) the process is a DeviceProcess: no pid, no file descriptor on stdin.
        self.device = bool(device_of(ctx))
        self.process = spawn_process(
            argv,
            ctx=ctx,
            cwd=str(ctx.workdir),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            # Read by ``_drain_stderr`` into ``stderr_tail`` (what the failure log says); a device's has none.
            stderr=subprocess.DEVNULL if self.device else subprocess.PIPE,
        )
        self.stderr_tail = collections.deque(maxlen=STDERR_LINES)
        self.stderr_reader = None
        if not self.device:
            self.stderr_reader = threading.Thread(target=self._drain_stderr, args=(env,), daemon=True, name="claude-stderr")
            self.stderr_reader.start()
        self.identity = None if self.device else process_identity(self.process.pid)
        self.policy_stop = False
        if not self.device:
            os.set_blocking(self.process.stdin.fileno(), False)
        self.reader = threading.Thread(target=self._read, daemon=True, name="claude-jsonl")
        self.reader.start()
        try:
            (observe or (lambda _: None))(self.process)
        except BaseException:
            self.close()
            raise

    def _drain_stderr(self, env):
        """Keep the CLI's last STDERR_LINES lines of stderr, credentials hidden, and never let its pipe fill."""
        try:
            for line in iter(lambda: self.process.stderr.readline(4096), b""):
                text = scrub(line.decode("utf-8", "replace").rstrip(), env)
                if text:
                    self.stderr_tail.append(text)
        except (OSError, ValueError):
            pass
        finally:
            try:
                self.process.stderr.close()
            except (OSError, ValueError):
                pass

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
        if self.device:
            return self._send_device(data, deadline)
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

    def _send_device(self, data, deadline):
        """One line to the CLI on the device: written once, the shim's ack awaited like the local write. The answer to
        the CLI's own request (a control_response) is kept while the link is down and sent when it is back; any
        other input is refused then (``device_offline``, nothing was sent). Never sent twice by this side."""
        from covey.core.device_link import DeviceClosed, DeviceOffline, DeviceSendUncertain
        with self.write_lock:
            self.check()
            if self.stop.is_set() or self.process.poll() is not None or self.process.stdin.closed:
                raise transport_error("process_closed")
            try:
                self.process.send_frame(data.decode().rstrip("\n"), deadline, check=self.check)
            except DeviceOffline as exc:
                raise transport_error("device_offline") from exc
            except DeviceClosed as exc:
                raise transport_error("process_closed") from exc
            except DeviceSendUncertain as exc:
                raise transport_error("input_write_timeout") from exc
            self.write_offset = len(data)

    def receive(self, timeout=0.1):
        self.check()
        try:
            return self.events.get(timeout=timeout)
        except queue.Empty:
            return None

    def initialize(self, hooks=None):
        """The initialize control request and its answer; ``hooks`` (backends/claude/hooks.py registration), when
        given, registers the turn's SDK hook callbacks."""
        rid = str(uuid.uuid4())
        self.send(
            {
                "type": "control_request",
                "request_id": rid,
                "request": {"subtype": "initialize", "forwardSubagentText": True, **({"hooks": hooks} if hooks else {})},
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
        if self.device:
            return self._close_device()
        if self.policy_stop:
            # The CLI can exit on TERM while a child ignores it with stdout closed.
            # Match the original private session/group and start ticks before killing it.
            cleanup_group(self.identity)
        self.reader.join(timeout=2)
        if self.stderr_reader is not None:
            self.stderr_reader.join(timeout=1)  # Its last lines are the failure log's (``failure_event``).
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

    def _close_device(self):
        """close() for a process on a device: the group was stopped through the device; there is no group of ours
        to sweep. After an official refusal the device's task is reaped, not trusted to have exited."""
        if self.policy_stop:
            self.process.reap()
        self.reader.join(timeout=2)
        confirmed = self.process.close()
        self.reader.join(timeout=2)
        return confirmed and self.process.poll() is not None and not self.reader.is_alive()
