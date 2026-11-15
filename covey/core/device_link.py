"""DeviceProcess: a Harness process that runs on a device, dressed as the ``subprocess.Popen`` the backends use.

``core/process.py spawn_process`` returns one when the turn's context names a device (``ctx.device``); otherwise it
is a plain ``Popen`` and nothing here is imported. The backends (``ClaudeProcess``, Codex's ``AppServer``) read its
``stdout`` line by line and write its ``stdin`` as they do a local process; underneath, the shim on the device
(``core/device_shim.py``) owns the real process and a **link thread** carries the bytes over ``attach``:

- **spawn**: the device runs the Agent under a supervisor that outlives ssh (``spawn``, idempotent per task key); the
  hub then ``attach``es from a byte offset into the task's journal. ``pid`` is ``None`` (a pid of the hub means
  nothing there, and ``os.killpg(0, ...)`` would signal our own group): code that signals or inspects a pid fails
  loudly; ``signal_group`` and ``send_signal`` go to the device.
- **output**: the journal's ``out`` records are written to a pipe the ``stdout`` file reads (EOF when the Agent ends
  or is lost). A record is processed once: frames whose offset is below the processed one are dropped, a frame past
  it (a gap) closes the connection and attaches again from the processed offset, so a killed attach loses and
  repeats nothing. The offset, the sent input numbers and the request ledger are saved to ``<run dir>/device.json``
  every 50 records or 2 seconds.
- **input**: ``send_frame`` numbers each line (``seq`` from 1, consecutive: the shim delivers in order and drops
  repeats) and waits for the shim's ``ack``. While the link is down a *durable* frame (an answer to a request the
  Agent made) is kept and sent once it is back; any other raises ``DeviceOffline``, and nothing was sent
  (``SteerNotReady`` for a supplement). A frame the link accepted is never invented again, only resent as it was.
- **requests**: the Agent's own requests to the hub (Claude ``control_request``, a Codex server request) are kept
  in a ledger by request id. A replayed request that is no longer open is not handed to the reader again; an answer
  is accepted only for an open request.
- **patience**: the link retries 1, 2, 4 ... 15 seconds apart. Outages add up (a link that stayed up for five
  minutes starts the count again); past ``patience`` seconds the process is **lost** (``returncode`` ``LOST``, EOF on
  its pipes, ``lost`` true) and the caller settles the turn as a lost device; while ``hold()`` is true (a question
  waits for the user) the longer ``waiting_patience`` counts instead. A lost process is not stopped: ``reap()`` does
  it once the device is back (the app registers a pending reap).

Only core modules are imported; ``covey/core/process.py`` imports this one on first use."""
from __future__ import annotations

import base64
import dataclasses
import io
import json
import logging
import os
import re
import secrets
import select
import signal
import subprocess
import threading
import time
from pathlib import Path

from covey.core import device_transport as dt
from covey.core import devices
from covey.core import env as cenv
from covey.core.state import read_object, write_json, write_private_text
from covey.core.task_processes import TASK_DIR_ENV

log = logging.getLogger(__name__)

LOST = -1000  # returncode of a process whose device stayed away past the patience (the Agent may still run).
GONE = -1001  # returncode of a task the device reports gone (rebooted, supervisor lost) or no longer has.
BACKOFF = (1, 2, 4, 8, 15)  # Seconds between attach attempts; the last one repeats.
SILENCE = 25.0  # No frame at all (the shim's heartbeat comes every 5 s) for this long: the connection is dead.
STABLE = 300.0  # Seconds up that start the outage count again.
SEND_WAIT = 10.0  # How long send_frame waits for the shim's ack.
SAVE_RECORDS, SAVE_SECONDS = 50, 2.0
LEDGER_MAX = 1000
READER_FILE = Path(__file__).resolve().parents[1] / "backends" / "codex" / "workspace_reader.py"
SECRET_NAME = re.compile(r"TOKEN|KEY|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH", re.I)
PATH_NAME = re.compile(r"(PATH|DIR|HOME|FILE|ROOT)$")
ENV_PREFIXES = ("CLAUDE_CODE_", "ANTHROPIC_", "CLAUDE_AUTOCOMPACT")
ENV_NAMES = ("LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM", "NO_COLOR", "ENABLE_TOOL_SEARCH", "DISABLE_AUTOUPDATER",
             "DISABLE_TELEMETRY", "DISABLE_ERROR_REPORTING")
TOOLS = ("claude", "codex")


class DeviceSpawnError(OSError):
    """The process could not be started on the device (like the OSError a local Popen raises)."""


class DeviceOffline(OSError):
    """Nothing was sent: the link to the device is down."""


class DeviceClosed(OSError):
    """The process has ended, or was lost: nothing more can be sent."""


class DeviceSendUncertain(OSError):
    """The input was queued but not acknowledged in time. It stays queued: it reaches the Agent once the device is
    reachable (the same frame, once), so a caller must not send it again."""


class DeviceRequestClosed(ValueError):
    """An answer to a request that is not open any more (answered, cancelled): not sent."""


# ---------------------------------------------------------------- resolving a device

@dataclasses.dataclass
class Link:
    """What a process needs of its device: the transport, the device's config, its profile (the last self check),
    the patience in seconds and the hub's state directory."""
    alias: str
    transport: object
    device: object
    profile: dict
    patience: float
    waiting_patience: float
    state_dir: Path


_TRANSPORTS: dict = {}
_TRANSPORTS_LOCK = threading.Lock()


def resolve(ctx):
    """The ``Link`` of ``ctx.device``: devices.json, one ssh transport per (state directory, device) for the whole
    hub, the profile on disk. Raises ``devices.UnknownDevice`` / ``DeviceConfigError`` when it cannot."""
    alias = ctx.device
    config = devices.current()
    device = config.device(alias)
    state_dir = Path(ctx.state_dir)
    with _TRANSPORTS_LOCK:
        transport = _TRANSPORTS.get((str(state_dir), alias))
        if transport is None:
            transport = _TRANSPORTS[(str(state_dir), alias)] = devices.open_transport(config, alias, state_dir)
    return Link(alias, transport, device, devices.read_profile(state_dir, alias), config.patience_minutes * 60.0,
                config.waiting_patience_hours * 3600.0, state_dir)


def read_record(run_dir):
    """``<run dir>/device.json`` as saved (``{}`` when there is none)."""
    try:
        return read_object(Path(run_dir) / "device.json")
    except (OSError, ValueError):
        return {}


# ---------------------------------------------------------------- the spawn request

def spawn_env(env, extra):
    """``(variables, dropped)``: what of the hub's environment a device process gets, and the names left out (names
    only: a value may be a credential). Only the names Claude Code and Codex are configured with (``CLAUDE_CODE_*``,
    ``ANTHROPIC_*`` except credentials, locale and a few switches) pass, and none that names a path of the hub;
    ``extra`` (the device's profile and devices.json ``env``: proxies) goes over them."""
    kept, dropped = {}, []
    for name, value in (env or {}).items():
        if not isinstance(value, str) or "\0" in value or not name or "=" in name:
            dropped.append(name)
        elif (name in ENV_NAMES or name.startswith(ENV_PREFIXES)) and not SECRET_NAME.search(name) \
                and not PATH_NAME.search(name):
            kept[name] = value
        else:
            dropped.append(name)
    kept.update(extra or {})
    return kept, sorted(dropped)


def device_argv(argv, ctx, profile):
    """``argv`` with the hub's Harness command (``ctx.backend_cmd``) replaced by the one the device's profile found
    (``tools.claude.path``), or by the bare name for the device's PATH."""
    prefix = list(getattr(ctx, "backend_cmd", None) or [])
    argv = list(argv)
    if not prefix or argv[:len(prefix)] != prefix:
        return argv
    name = os.path.basename(prefix[0])
    if name not in TOOLS:
        return argv
    info = ((profile or {}).get("tools") or {}).get(name) or {}
    path = info.get("path") if info.get("found") and isinstance(info.get("path"), str) and info.get("path") else name
    return [path, *argv[1:]]


def task_key(run_dir):
    """A device task key: the run directory's name and a random suffix (a second process of one run is a new task)."""
    stem = re.sub(r"[^A-Za-z0-9._-]", "-", Path(run_dir).name if run_dir else "adhoc").strip("-._") or "task"
    return "%s-%s" % (stem[:100], secrets.token_hex(4))


# ---------------------------------------------------------------- the files a backend reads and writes

class _Stdin:
    """The process's standard input: whole lines are sent as frames (``send_frame``) when flushed. It has no file
    descriptor (``fileno`` raises), so code that polls or sets it non-blocking fails at once."""

    def __init__(self, owner, text):
        self._owner, self._text = owner, text
        self._buffer = ""
        self._closed = False
        self._lock = threading.Lock()

    @property
    def closed(self):
        return self._closed

    def fileno(self):
        raise io.UnsupportedOperation("a device process's stdin has no file descriptor")

    def writable(self):
        return not self._closed

    def write(self, data):
        if self._closed:
            raise ValueError("I/O operation on closed file")
        if isinstance(data, (bytes, bytearray)):
            data = bytes(data).decode("utf-8", "replace")
        with self._lock:
            self._buffer += data
        return len(data)

    def flush(self):
        if self._closed:
            raise ValueError("I/O operation on closed file")
        with self._lock:
            *lines, self._buffer = self._buffer.split("\n")
        for line in lines:
            if line:
                self._owner.send_frame(line, time.monotonic() + SEND_WAIT)

    def close(self):
        if self._closed:
            return
        try:
            if self._buffer.strip():
                self.flush()
        finally:
            self._closed = True
            self._owner._input_closed()


def _pipe_reader(fd, text):
    if text:
        return io.TextIOWrapper(io.BufferedReader(io.FileIO(fd, "r")), encoding="utf-8", errors="replace")
    return io.BufferedReader(io.FileIO(fd, "r"))


# ---------------------------------------------------------------- the process

class DeviceProcess:
    """A Harness process on a device. ``argv`` is the hub's command line (``ctx.backend_cmd`` first); ``ctx`` the
    turn's ``BridgeContext`` (``device``, ``remote_workdir``, ``state_dir``); ``env`` the hub's environment for the
    child (filtered, see ``spawn_env``). ``stdin``/``stdout``/``stderr`` as for Popen: ``subprocess.PIPE`` gives the
    file, anything else none. ``run_dir`` is the task's run directory (default: ``COVEY_TASK_DIR`` of this process;
    without one nothing is saved). ``checkpoint`` is ``{"path": <on the device>, "hub_path": <on the hub>}``: the
    device mirrors the file and the link writes every change to ``hub_path``; ``files`` are seed files for the task
    (``{"path", "text" or "b64", "mode"}``). ``link`` replaces ``resolve(ctx)`` (tests)."""

    def __init__(self, argv, *, ctx, cwd=None, env=None, stdin=None, stdout=None, stderr=None, text=False,
                 bufsize=-1, run_dir=None, checkpoint=None, files=None, link=None):
        self.args = list(argv)
        self.pid = None
        self.returncode = None
        self.exit_info = None
        self.reason = ""
        self._ctx = ctx
        self.alias = getattr(ctx, "device", "")
        if not isinstance(self.alias, str) or not self.alias:
            raise ValueError("DeviceProcess needs a context that names a device")
        self.remote_workdir = str(getattr(ctx, "remote_workdir", "") or "")
        if not self.remote_workdir:
            raise ValueError("a device turn has no remote working directory")
        try:
            self.link = link or resolve(ctx)
        except (devices.DeviceConfigError, devices.UnknownDevice, OSError) as exc:
            raise DeviceSpawnError("device %s: %s" % (self.alias, exc)) from exc
        self.transport = self.link.transport
        self._cond = threading.Condition(threading.RLock())
        self._stop = threading.Event()
        self._thread = None
        self._dirty = False
        self._hold = lambda: False
        self._att = None
        self._state = "connecting"
        self._offset = 0
        self._records = 0
        self._saved_at = time.monotonic()
        self._next_seq = 1
        self._acked = 0
        self._frames = {}  # seq -> the line, until acknowledged
        self._failed = {}  # seq -> the shim's refusal
        self._ledger = {}  # request id -> {"o", "state"}
        self._pending_sigs = set()
        self._eof = False
        self._stdin_closed = False
        self._down_total = 0.0
        self._down_at = time.monotonic()
        self._up_at = None
        self.lost = False
        self.gone = ""
        self.cleanup_confirmed = False
        self.reaped = False
        self.task_dir = ""
        self.pgid = None
        self.run_dir = Path(run_dir) if run_dir else self._run_dir_from_env()
        options = getattr(ctx, "options", None)
        extra = options.get("device_spawn") if isinstance(options, dict) else None
        extra = extra if isinstance(extra, dict) else {}  # What the app puts on the context for the spawn.
        checkpoint = checkpoint or extra.get("checkpoint")
        files = files or extra.get("files")
        self.checkpoint = dict(checkpoint) if isinstance(checkpoint, dict) and checkpoint.get("path") else None
        self._tasks_before = [key for key in read_record(self.run_dir).get("tasks", []) if isinstance(key, str)] \
            if self.run_dir else []
        self.task = task_key(self.run_dir)
        self.shim_path = self.transport.shim_path
        self._text = text
        self._pipes = {}
        self.stdin = _Stdin(self, text) if stdin == subprocess.PIPE else None
        for name, mode in (("stdout", stdout), ("stderr", stderr)):
            reader = None
            if mode == subprocess.PIPE:
                read_fd, write_fd = os.pipe()
                os.set_blocking(write_fd, False)
                self._pipes[name] = write_fd
                reader = _pipe_reader(read_fd, text)
            setattr(self, name, reader)
        try:
            self._spawn(cwd, env, files)
            self._connect_first()
        except BaseException:
            self._close_pipes()
            raise
        self._thread = threading.Thread(target=self._main, daemon=True, name="device-link")
        self._thread.start()

    # -- starting
    def _run_dir_from_env(self):
        value = cenv.get(TASK_DIR_ENV)
        return Path(value) if value else None

    def _spec(self, cwd, env, files):
        device, profile = self.link.device, self.link.profile
        wanted, dropped = spawn_env(env, devices.effective_env(device, profile))
        if dropped:
            log.info("device %s: left %d variables out of the task's environment: %s%s", self.alias, len(dropped),
                     ", ".join(dropped[:40]), " ..." if len(dropped) > 40 else "")
        spec = {"argv": device_argv(self.args, self._ctx, profile), "cwd": self.remote_workdir, "env": wanted,
                "shell": "login" if getattr(device, "login_shell", False) else "", "files": list(files or [])}
        if self.checkpoint and self.checkpoint.get("path"):
            spec["checkpoint"] = {"path": str(self.checkpoint["path"]), "poll": float(self.checkpoint.get("poll", 1.0))}
        return spec

    def _spawn(self, cwd, env, files):
        spec = self._spec(cwd, env, files)
        try:
            devices.push_file(self.transport, dt.shim_bytes())
            tries = 0
            while True:
                try:
                    answer = self.transport.call("spawn", task=self.task, patience=30.0, shim_path=self.shim_path, **spec)
                    break
                except dt.Unreachable:
                    raise
                except dt.LinkError:  # spawn is idempotent for this task key: asking again is safe.
                    tries += 1
                    if tries > 2:
                        raise
                    time.sleep(0.5)
        except dt.DeviceError as exc:
            raise DeviceSpawnError("device %s: %s" % (self.alias, exc)) from exc
        self.task_dir = str(answer.get("task_dir") or "")
        self.pgid = answer.get("pgid")
        self._save(force=True)

    def _connect_first(self):
        """The first attach, before the process is handed out: one that fails is an outage the link thread retries."""
        try:
            attachment = self._attach()
        except (dt.NoTask, dt.BadOffset) as exc:
            raise DeviceSpawnError("device %s: %s" % (self.alias, exc)) from exc
        except (dt.DeviceError, OSError) as exc:
            log.info("device %s: first attach failed: %s", self.alias, exc)
            return
        self._mark_up(attachment)
        self._first = attachment

    _first = None

    # -- Popen's face
    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        end = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            while self.returncode is None:
                remaining = None if end is None else end - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise subprocess.TimeoutExpired(self.args, timeout)
                self._cond.wait(0.2 if remaining is None else min(0.2, remaining))
            return self.returncode

    def send_signal(self, signum):
        self.signal_group(signum)

    def terminate(self):
        self.signal_group(signal.SIGTERM)

    def kill(self):
        self.signal_group(signal.SIGKILL)

    def signal_group(self, signum):
        """TERM (any signal but KILL) or KILL to the Agent's process group on the device. Nothing once it has
        ended; while the link is down the signal waits for it."""
        if self.returncode is not None:
            return
        name = "KILL" if signum == signal.SIGKILL else "TERM"
        with self._cond:
            attachment = self._att if self._state == "up" else None
            if attachment is None:
                self._pending_sigs.add(name)
                return
        try:
            attachment.send({"op": "sig", "sig": name})
        except (dt.Closed, dt.BadFrame):
            with self._cond:
                self._pending_sigs.add(name)

    # -- the link's face
    @property
    def offline(self):
        """The link to the device is not up (or the process was lost): a supplement cannot be sent now."""
        return self._state in ("connecting", "down", "lost", "gone", "closed")

    def state(self):
        """``connecting``, ``up``, ``down``, ``exited``, ``lost`` or ``gone``."""
        return self._state

    def details(self):
        """The state with what the card says: the seconds the link has been down now and in all, the offset."""
        with self._cond:
            return {"state": self._state, "down_seconds": round(self._down_seconds(), 1), "offset": self._offset,
                    "acked": self._acked, "seq": self._next_seq - 1, "lost": self.lost, "gone": self.gone,
                    "reason": self.reason, "alias": self.alias, "task": self.task}

    def set_hold(self, hold):
        """``hold()`` is true while the process waits for the user (a question): the longer patience counts."""
        self._hold = hold if callable(hold) else (lambda: False)

    def workspace_reader(self, root, extra_roots=()):
        """``(command, args)`` of the read-only workspace MCP server on the device for Codex: ``workspace_reader.py``
        pushed next to the shim (by content), run with the device's Python."""
        data = READER_FILE.read_bytes()
        try:
            _, path = devices.push_file(self.transport, data, "workspace_reader.py")
        except dt.DeviceError as exc:
            raise DeviceSpawnError("device %s: %s" % (self.alias, exc)) from exc
        home = self.home
        if path.startswith("~/") and home:
            path = home + path[1:]
        profile = self.link.profile or {}
        command = profile.get("python_exe") or self.link.device.python
        args = ["-B", path, "--root", root]
        for extra in extra_roots:
            args += ["--extra-root", extra]
        return command, args

    @property
    def home(self):
        """The device user's home, read from the task directory's path (``<home>/.covey/tasks/...``), else the profile."""
        head, sep, _ = self.task_dir.partition("/.covey/tasks/")
        if sep and head.startswith("/"):
            return head
        return str((self.link.profile or {}).get("home") or "")

    def reap(self, grace=5.0):
        """Stop whatever of the task is left on the device and make sure of it (``reap``): whether nothing is left.
        False when the device cannot be reached or refuses (the group is not provably the task's)."""
        try:
            answer = self.transport.call("reap", task=self.task, grace=grace, patience=grace + 30.0,
                                         shim_path=self.shim_path)
        except dt.DeviceError as exc:
            log.info("device %s: reap of %s failed: %s", self.alias, self.task, exc)
            return False
        confirmed = not answer.get("after") and not answer.get("refused")
        if confirmed:
            with self._cond:
                self.reaped = self.cleanup_confirmed = True
        return confirmed

    def close(self):
        """End the link: stop the thread, close the connection (detaching from the shim: the Agent is not touched),
        give the pipes their EOF. Whether the device is known to hold nothing of the task (it ended and left nothing,
        or ``reap`` confirmed). One that ended leaving processes behind is reaped here."""
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
            attachment, self._att = self._att, None
        if attachment is not None:
            attachment.close()
        if self._thread is not None and threading.current_thread() is not self._thread:
            self._thread.join(timeout=5)
        confirmed = self.cleanup_confirmed
        if self.exit_info and self.exit_info.get("left") and not self.reaped:
            confirmed = self.reap(2.0)
        with self._cond:
            if self._state not in ("exited", "lost", "gone"):
                self._state = "closed"
            self._dirty = True
        self._save(force=True)
        self._close_pipes()
        return confirmed

    # -- input
    def send_frame(self, data, deadline=None, *, durable=None, check=None):
        """Send one line to the Agent's stdin: ``"acked"`` once the shim wrote it to the Agent, ``"queued"`` for a
        durable frame while the link is down. ``deadline`` (monotonic) bounds the wait for the ack; ``check`` is
        called while waiting (it may raise to give up). ``durable`` defaults to true for the answer to a request of the
        Agent. Raises ``DeviceOffline`` (nothing sent), ``DeviceClosed``, ``DeviceRequestClosed`` (nothing sent) and
        ``DeviceSendUncertain`` (queued, not acknowledged in time: it will still be delivered)."""
        if "\n" in data:
            raise ValueError("a frame is one line")
        parsed = _json(data)
        answered = _answer_id(parsed)
        if durable is None:
            durable = answered is not None
        with self._cond:
            if self.returncode is not None:
                raise DeviceClosed("the process on %s has ended" % self.alias)
            if self._stdin_closed:
                raise ValueError("I/O operation on closed file")
            if answered is not None:
                entry = self._ledger.get(answered)
                if entry is not None and entry["state"] != "open":
                    raise DeviceRequestClosed("request %s is %s" % (answered, entry["state"]))
            offline = self._state != "up"
            if offline and not durable:
                raise DeviceOffline("the connection to %s is down; nothing was sent" % self.alias)
            seq = self._next_seq
            self._next_seq += 1
            self._frames[seq] = data
            if answered is not None and answered in self._ledger:
                self._ledger[answered]["state"] = "answered"
            self._save(force=True)
            attachment = None if offline else self._att
        if attachment is not None:
            try:
                attachment.send({"op": "send", "seq": seq, "d": data})
            except dt.Closed:
                pass  # The link thread notices, attaches again and resends this frame.
            except dt.BadFrame as exc:
                with self._cond:
                    self._frames.pop(seq, None)
                raise ValueError("the input does not fit one frame: %s" % exc) from exc
        if offline:
            return "queued"
        return self._wait_ack(seq, deadline, check)

    def _wait_ack(self, seq, deadline, check):
        while True:
            with self._cond:
                if self._acked >= seq:
                    return "acked"
                if seq in self._failed:
                    raise DeviceClosed("the device refused the input: %s" % self._failed[seq])
                if self.returncode is not None:
                    raise DeviceClosed("the process on %s ended before it took the input" % self.alias)
                if deadline is not None and time.monotonic() >= deadline:
                    raise DeviceSendUncertain("input %d to %s was not acknowledged in time" % (seq, self.alias))
                self._cond.wait(0.1)
            if check is not None:
                check()

    def _input_closed(self):
        """stdin was closed: the Agent gets end of input once everything sent before it was taken."""
        with self._cond:
            self._stdin_closed = True
            self._eof = True
            attachment = self._att if self._state == "up" else None
        if attachment is not None:
            try:
                attachment.send({"op": "sig", "sig": "EOF"})
            except (dt.Closed, dt.BadFrame):
                pass

    # -- the thread
    def _main(self):
        try:
            self._loop()
        except BaseException as exc:  # A bug here must not leave the reader waiting for ever.
            log.exception("device link of %s failed", self.alias)
            self._finish("failed", LOST, why="link_error:%s" % type(exc).__name__)
        finally:
            attachment = self._att
            if attachment is not None:
                attachment.close(detach=False)
            self._save(force=True)

    def _loop(self):
        attempt = 0
        attachment, self._first = self._first, None
        while not self._stop.is_set() and self.returncode is None:
            if attachment is None:
                self._mark_down()
                try:
                    attachment = self._attach()
                except dt.NoTask:
                    self._finish("gone", GONE, why="no_task")
                    return
                except dt.BadOffset:
                    self._finish("failed", GONE, why="bad_offset")
                    return
                except (dt.DeviceError, OSError) as exc:
                    log.info("device %s: attach failed (%s)", self.alias, exc)
                    delay = BACKOFF[min(attempt, len(BACKOFF) - 1)]
                    attempt += 1
                    self._rest(delay)
                    continue
                self._mark_up(attachment)
            began = time.monotonic()
            try:
                self._read(attachment)
            except (dt.LinkError, OSError, ValueError) as exc:
                log.info("device %s: link broke (%s)", self.alias, exc)
            finally:
                attachment.close(detach=False)
                with self._cond:
                    if self._att is attachment:
                        self._att = None
            attachment = None
            if time.monotonic() - began >= 10.0:
                attempt = 0
            if self.returncode is None and not self._stop.is_set():
                self._mark_down()
                self._rest(BACKOFF[min(attempt, len(BACKOFF) - 1)])
                attempt += 1

    def _rest(self, seconds):
        """Wait ``seconds`` (a closing process cuts it short), checking the patience every quarter second."""
        end = time.monotonic() + seconds
        while not self._stop.is_set() and self.returncode is None:
            if self._lose_if_patience_ran_out():
                return
            left = end - time.monotonic()
            if left <= 0:
                return
            self._stop.wait(min(0.25, left))

    def _attach(self):
        return self.transport.attach(self.task, self._offset, patience=15.0, shim_path=self.shim_path)

    def _read(self, attachment):
        last = time.monotonic()
        while not self._stop.is_set() and self.returncode is None:
            try:
                frame = attachment.read_frame(timeout=0.5)
            except TimeoutError:
                if time.monotonic() - last > SILENCE:
                    attachment.kill()
                    return
                self._save()
                continue
            if frame is None:
                return
            last = time.monotonic()
            if not self._handle(frame):
                return

    def _handle(self, frame):
        """One frame from the device; False ends this connection (a gap: attach again from the processed offset)."""
        kind = frame.get("k")
        if kind == "hb":
            return True
        if kind == "r":
            self._reply(frame)
            return True
        start, end = frame.get("o"), frame.get("e")
        if isinstance(start, int) and isinstance(end, int) and not isinstance(start, bool):
            if start < self._offset:
                return True  # Seen before (the attach was repeated).
            if start > self._offset:
                log.info("device %s: a gap in the journal (%d, wanted %d): attaching again", self.alias, start,
                         self._offset)
                return False
        else:
            return True  # Not a journal record: nothing this hub knows.
        if kind == "out":
            self._output(frame)
        elif kind == "ack":
            self._ack(frame)
        elif kind == "cp":
            self._checkpoint(frame)
        elif kind == "exit":
            with self._cond:
                self._offset = end
            self._exited(frame)
            return False
        elif kind == "gone":
            with self._cond:
                self._offset = end
            self.gone = str(frame.get("why") or "gone")
            self._finish("gone", GONE, why=self.gone)
            return False
        with self._cond:
            self._offset = end
            self._records += 1
            self._dirty = True
        self._save()
        return True

    def _output(self, frame):
        if isinstance(frame.get("b64"), str):
            data = base64.b64decode(frame["b64"])
        else:
            data = str(frame.get("d", "")).encode("utf-8")
        if not frame.get("more"):
            data += b"\n"
        if self._request_seen(data, frame.get("o", 0)) is False:
            return
        self._pump("stdout", data)

    def _request_seen(self, data, offset):
        """Keep the Agent's requests in the ledger. False for a line that must not reach the reader again: a request
        already answered or cancelled (a replayed journal)."""
        if b'"control_request"' not in data and b'"control_cancel_request"' not in data and not (
                b'"method"' in data and b'"id"' in data):
            return True
        row = _json(data.decode("utf-8", "replace"))
        if not isinstance(row, dict):
            return True
        if row.get("type") == "control_cancel_request" and isinstance(row.get("request_id"), str):
            with self._cond:
                entry = self._ledger.get(row["request_id"])
                if entry is not None and entry["state"] == "open":
                    entry["state"] = "cancelled"
            return True
        rid = _request_id(row)
        if rid is None:
            return True
        with self._cond:
            entry = self._ledger.get(rid)
            if entry is not None:
                return entry["state"] == "open"
            self._ledger[rid] = {"o": offset, "state": "open"}
            if len(self._ledger) > LEDGER_MAX:
                for old in [k for k, v in self._ledger.items() if v["state"] != "open"][:len(self._ledger) - LEDGER_MAX]:
                    del self._ledger[old]
        return True

    def _pump(self, name, data):
        """Write to a pipe the reader holds, without blocking for ever on a reader that has gone away."""
        fd = self._pipes.get(name)
        if fd is None:
            return
        view = memoryview(data)
        while view and not self._stop.is_set():
            try:
                written = os.write(fd, view)
                view = view[written:]
            except BlockingIOError:
                select.select([], [fd], [], 0.2)
            except OSError:
                return  # The reader closed its end.

    def _ack(self, frame):
        seq = frame.get("seq")
        if isinstance(seq, int) and not isinstance(seq, bool):
            with self._cond:
                if seq > self._acked:
                    self._acked = seq
                    for number in [n for n in self._frames if n <= seq]:
                        del self._frames[number]
                    self._cond.notify_all()

    def _reply(self, frame):
        if frame.get("op") != "send" or frame.get("ok") is not False:
            return
        seq = frame.get("seq")
        if isinstance(seq, int):
            with self._cond:
                self._failed[seq] = frame.get("err") or "refused"
                self._frames.pop(seq, None)
                self._cond.notify_all()

    def _checkpoint(self, frame):
        target = (self.checkpoint or {}).get("hub_path")
        if not target or frame.get("over") or frame.get("gone"):
            return  # Too large to mirror, or removed: the hub's copy stays as it was.
        if isinstance(frame.get("b64"), str):
            try:
                text = base64.b64decode(frame["b64"]).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                return
        else:
            text = str(frame.get("d", ""))
        try:
            json.loads(text)
        except ValueError:  # Caught mid-write on the device (empty or cut short): the next look brings all of it.
            log.info("device %s: a checkpoint read mid-write was skipped (%d chars)", self.alias, len(text))
            return
        try:
            write_private_text(Path(target), text)
        except OSError as exc:
            log.warning("device %s: the checkpoint could not be written: %s", self.alias, exc)

    def _exited(self, frame):
        code = frame.get("code")
        sig = frame.get("sig")
        if not isinstance(code, int) or isinstance(code, bool):
            code = -int(sig) if isinstance(sig, int) and sig else -signal.SIGKILL
        self.exit_info = {key: frame.get(key) for key in ("code", "sig", "reason", "left", "error") if key in frame}
        self._finish("exited", code, why=str(frame.get("reason") or ""))

    # -- state
    def _mark_up(self, attachment):
        with self._cond:
            now = time.monotonic()
            if self._down_at is not None:
                self._down_total += now - self._down_at
                self._down_at = None
            self._up_at = now
            self._att = attachment
            self._state = "up"
            resend = [(seq, self._frames[seq]) for seq in sorted(self._frames) if seq > self._acked]
            sigs, eof = sorted(self._pending_sigs), self._eof
            self._pending_sigs.clear()
            self._cond.notify_all()
        try:
            for seq, data in resend:
                attachment.send({"op": "send", "seq": seq, "d": data})
            if eof:
                attachment.send({"op": "sig", "sig": "EOF"})
            for name in sigs:
                attachment.send({"op": "sig", "sig": name})
        except (dt.Closed, dt.BadFrame):
            pass  # The read loop sees the connection end; the frames stay unacknowledged and are sent again.
        self._save(force=True)

    def _mark_down(self):
        with self._cond:
            if self._state in ("exited", "lost", "gone"):
                return
            if self._down_at is None:
                self._down_at = time.monotonic()
            if self._state != "down":
                self._state = "down"
                self._cond.notify_all()
        self._save(force=True)

    def _down_seconds(self):
        now = time.monotonic()
        if self._down_at is not None:
            return self._down_total + (now - self._down_at)
        if self._up_at is not None and now - self._up_at >= STABLE:
            self._down_total = 0.0
        return self._down_total

    def _limit(self):
        try:
            waiting = bool(self._hold())
        except Exception:
            waiting = False
        return self.link.waiting_patience if waiting else self.link.patience

    def _lose_if_patience_ran_out(self):
        with self._cond:
            if self._state != "down" or self._down_seconds() <= self._limit():
                return False
        self.lost = True
        self._finish("lost", LOST, why="device_lost")
        return True

    def _finish(self, state, code, why=""):
        """The process is over for the hub: ``returncode`` first (a reader that sees EOF asks for it), then EOF."""
        with self._cond:
            if self.returncode is not None:
                return
            self.reason = why or self.reason
            self._state = state
            if state == "exited":
                self.cleanup_confirmed = not (self.exit_info or {}).get("left")
            self.returncode = code
            self._cond.notify_all()
        self._save(force=True)
        self._close_pipes()

    def _close_pipes(self):
        for name, fd in list(self._pipes.items()):
            self._pipes.pop(name, None)
            try:
                os.close(fd)
            except OSError:
                pass

    # -- device.json
    def _save(self, force=False):
        """``<run dir>/device.json``: every 50 records or 2 seconds when something changed, or at once with ``force``."""
        if self.run_dir is None:
            return
        now = time.monotonic()
        with self._cond:
            if not force and (not self._dirty or (self._records < SAVE_RECORDS and now - self._saved_at < SAVE_SECONDS)):
                return
            record = {"schema": 1, "device": self.alias, "hub": getattr(self.transport, "hub", ""), "task": self.task,
                      "tasks": [key for key in self._tasks_before if key != self.task] + [self.task],
                      "shim": self.shim_path, "task_dir": self.task_dir, "pgid": self.pgid,
                      "cwd": self.remote_workdir, "state": self._state, "offset": self._offset,
                      "seq": self._next_seq - 1, "acked": self._acked, "requests": self._ledger,
                      "down_seconds": round(self._down_seconds(), 1), "lost": self.lost, "gone": self.gone,
                      "reason": self.reason, "exit": self.exit_info, "updated": time.time()}
            self._records, self._saved_at, self._dirty = 0, now, False
            try:
                write_json(self.run_dir / "device.json", record)
            except OSError as exc:
                log.warning("device %s: device.json not saved: %s", self.alias, exc)


# ---------------------------------------------------------------- reading rows

def _json(text):
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _rpc_id(row):
    value = row.get("id")
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    return "rpc:%s" % value


def _request_id(row):
    """The id (for the ledger) of a request the Agent makes of the hub, or None: Claude's ``control_request`` or a
    JSON-RPC request of Codex's app server (a ``method`` and an ``id``)."""
    if row.get("type") == "control_request" and isinstance(row.get("request_id"), str):
        return row["request_id"]
    if "method" in row and "id" in row:
        return _rpc_id(row)
    return None


def _answer_id(row):
    """The request id an outgoing line answers, or None: Claude's ``control_response`` or a JSON-RPC response."""
    if not isinstance(row, dict):
        return None
    if row.get("type") == "control_response":
        response = row.get("response")
        if isinstance(response, dict) and isinstance(response.get("request_id"), str):
            return response["request_id"]
        return None
    if "id" in row and "method" not in row and ("result" in row or "error" in row):
        return _rpc_id(row)
    return None
