"""The hub's side of the device shim's wire protocol (``core/device_shim.py``, docs/architecture.md "设备端小程序").

``SshTransport`` runs ``python3 ~/.covey/shim/<id>/shim.py <cmd>`` on a device through ssh (one shared ControlMaster
connection per device, so a call costs one round trip); ``LocalTransport`` runs the very same remote command line
under a temporary HOME on this machine, so the tests exercise the real shim (real supervisors, setsid, signals)
without ssh. Both give the same interface:

- ``call(cmd, **fields)``: a one-answer command, the ``{"ok":true,...}`` line as a dict; ``get`` / ``put`` (and
  ``get_file`` / ``put_file``, which cut a file into segments) carry data; ``shell`` runs a plain ``sh`` script
  (how the shim itself is pushed); ``attach`` opens the long connection (``Attachment``: frames in, frames out).
- Framing: stdout's first line is the ``hi`` frame; the lines before it (what ``.bashrc`` echoed) are dropped, at most
  64 KiB of them. A line is at most 16 MiB, a get or put segment at most 64 MiB.
- Errors: a shim answer ``{"ok":false,"err":"<code>"}`` becomes the ``ShimError`` subclass ``ERRORS`` names for the
  code; anything wrong with the channel (ssh could not connect, no hi frame, a broken frame, a call out of patience)
  is a ``LinkError``. A ``LinkError`` says nothing about whether the command ran: the caller repeats only what is
  idempotent (docs/architecture.md, the shim's idempotency table) and never invents a new input.

Standard library only; imports nothing of covey (task processes may use it).
"""
from __future__ import annotations

import hashlib
import itertools
import json
import os
import select
import shlex
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

PROTO = 1  # The protocol this hub speaks (the shim's hi frame and hello carry its own).
LINE_MAX = 16 << 20  # A frame line, either direction.
JUNK_MAX = 64 << 10  # What a login shell may print before the hi frame.
DATA_MAX = 64 << 20  # One get or put segment.
STDERR_KEEP = 8192  # The last bytes of ssh's or the shim's stderr kept for messages.
SHIM_FILE = Path(__file__).with_name("device_shim.py")
SHIM_ROOT = "~/.covey/shim"  # On the device; the shim's own directory is <SHIM_ROOT>/<id>/.


def shim_id(data):
    """The directory name of a pushed file: the first 16 hex digits of its SHA-256 (the shim's hi frame reports the
    same for itself)."""
    return hashlib.sha256(data).hexdigest()[:16]


def shim_bytes():
    return SHIM_FILE.read_bytes()


# ---------------------------------------------------------------- errors

class DeviceError(Exception):
    """Base of every error this module raises: ``code`` is the shim's ``err`` or a ``LinkError`` code."""
    code = "error"

    def __init__(self, msg="", code=None):
        super().__init__(msg)
        self.msg = msg
        if code:
            self.code = code

    def __str__(self):
        return "%s: %s" % (self.code, self.msg) if self.msg else self.code


class LinkError(DeviceError):
    """The channel failed; whether the command ran is unknown."""
    code = "link"


class Unreachable(LinkError):
    """ssh could not connect (exit 255 or no output at all)."""
    code = "unreachable"


class NoHi(LinkError):
    """No hi frame: the command line is wrong (no python3, no shim) or a login shell printed too much."""
    code = "no_hi"


class BadFrame(LinkError):
    """A line that is not a JSON object, a body of the wrong length, or one past the size limits."""
    code = "bad_frame"


class LinkTimeout(LinkError):
    """The device did not answer within the caller's patience; the process was killed."""
    code = "link_timeout"


class Closed(LinkError):
    """The attach connection is closed."""
    code = "closed"


class ShimError(DeviceError):
    """The shim answered ``ok: false``."""
    code = "shim"


def _shim_error(name, code):
    return type(name, (ShimError,), {"code": code, "__doc__": "The shim's error code %r." % code})


BadRequest = _shim_error("BadRequest", "bad_request")
BadProto = _shim_error("BadProto", "bad_proto")
NoTask = _shim_error("NoTask", "no_task")
Conflict = _shim_error("Conflict", "conflict")
Exited = _shim_error("Exited", "exited")
SpawnFailed = _shim_error("SpawnFailed", "spawn_failed")
BadCwd = _shim_error("BadCwd", "bad_cwd")
BadOffset = _shim_error("BadOffset", "bad_offset")
NotFound = _shim_error("NotFound", "not_found")
NotFile = _shim_error("NotFile", "not_file")
NotDir = _shim_error("NotDir", "not_dir")
NoPermission = _shim_error("NoPermission", "permission")
TooLarge = _shim_error("TooLarge", "too_large")
ShaMismatch = _shim_error("ShaMismatch", "sha_mismatch")
NoPgid = _shim_error("NoPgid", "no_pgid")
ShimTimeout = _shim_error("ShimTimeout", "timeout")
ShimGone = _shim_error("ShimGone", "gone")
ShimIO = _shim_error("ShimIO", "io")
Internal = _shim_error("Internal", "internal")

ERRORS = {cls.code: cls for cls in (
    BadRequest, BadProto, NoTask, Conflict, Exited, SpawnFailed, BadCwd, BadOffset, NotFound, NotFile, NotDir,
    NoPermission, TooLarge, ShaMismatch, NoPgid, ShimTimeout, ShimGone, ShimIO, Internal)}


def error_for(code, msg=""):
    """The exception for a shim error code (an unknown code, one a newer shim added, is a plain ``ShimError``)."""
    cls = ERRORS.get(code)
    if cls is None:
        return ShimError(msg, code=str(code))
    return cls(msg)


# ---------------------------------------------------------------- framing

def encode(obj):
    """One request or frame line: compact JSON, ASCII only (the shim's locale is not ours), newline-terminated."""
    line = json.dumps(obj, separators=(",", ":")).encode("ascii") + b"\n"
    if len(line) > LINE_MAX:
        raise BadFrame("a frame of %d bytes is past the %d byte limit" % (len(line), LINE_MAX))
    return line


def decode(line):
    """A frame line as a dict; ``BadFrame`` for anything else."""
    try:
        frame = json.loads(line.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise BadFrame("a line of the stream is not JSON: %r" % line[:80])
    if not isinstance(frame, dict):
        raise BadFrame("a line of the stream is not a JSON object: %r" % line[:80])
    return frame


def scan_hi(buf):
    """``(hi, used)`` once ``buf`` holds the hi frame (``used``: the bytes up to and with its line), ``(None, 0)``
    while more data is needed. Lines before it are junk; more than JUNK_MAX bytes of it raises ``NoHi``."""
    pos = 0
    while True:
        nl = buf.find(b"\n", pos)
        if nl < 0:
            if len(buf) > JUNK_MAX:
                raise NoHi("more than %d bytes came before the hi frame" % JUNK_MAX)
            return None, 0
        line = bytes(buf[pos:nl])
        if line[:1] == b"{":
            try:
                frame = json.loads(line.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                frame = None
            if isinstance(frame, dict) and frame.get("k") == "hi":
                return frame, nl + 1
        pos = nl + 1
        if pos > JUNK_MAX:
            raise NoHi("more than %d bytes came before the hi frame" % JUNK_MAX)


def word(text):
    """A shell word for the device's shell; a leading ``~/`` stays unquoted so the shell expands it."""
    if text == "~":
        return "~"
    if text.startswith("~/"):
        return "~/" + shlex.quote(text[2:])
    return shlex.quote(text)


def kill_group(proc):
    """SIGKILL ``proc``'s process group (every ssh and shim of ours starts its own session) and reap it."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass


def tail_text(data):
    return data[-STDERR_KEEP:].decode("utf-8", "replace").strip()


def failure(code, out, err, what):
    """The error for a channel that ended without a hi frame or an answer, told by the exit code and stderr."""
    text = tail_text(err) or tail_text(out)
    if code == 255:  # ssh's own exit code for a failure to connect or authenticate.
        return Unreachable("%s (exit %s): %s" % (what, code, text))
    return NoHi("%s (exit %s): %s" % (what, code, text))


class Answer:
    """One command's answer: the hi frame, the head line and, for get, the body."""

    def __init__(self, hi, head, body=b""):
        self.hi, self.head, self.body = hi, head, body


def parse_answer(out, err, code, *, body=False):
    """An ``Answer`` from everything a one-answer command printed; raises the error a failure maps to."""
    buf = bytearray(out)
    hi, used = scan_hi(buf)
    if hi is None:
        raise failure(code, out, err, "no hi frame from the device")
    nl = out.find(b"\n", used)
    if nl < 0:
        raise failure(code, out, err, "no answer after the hi frame")
    head = decode(out[used:nl])
    if head.get("ok") is not True:
        raise error_for(head.get("err"), str(head.get("msg") or ""))
    data = b""
    if body:
        count = head.get("len")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise BadFrame("a get answer without len")
        data = out[nl + 1:nl + 1 + count]
        if len(data) != count or out[nl + 1 + count:nl + 2 + count] != b"\n":
            raise BadFrame("a get body of %d bytes was announced and %d arrived" % (count, len(data)))
        if head.get("sha256") and hashlib.sha256(data).hexdigest() != head["sha256"]:
            raise ShaMismatch("the bytes that arrived do not match the sha256 the device sent")
    return Answer(hi, head, data)


# ---------------------------------------------------------------- the attach connection

class Attachment:
    """An attach connection: the shim's process (ssh) with its two NDJSON directions.

    ``read_frame(timeout)`` is the next frame from the device (record frames with ``o``/``e``, ``hb``, ``r`` replies,
    ``gone``): a dict, None once the connection ended, ``TimeoutError`` after ``timeout`` seconds with none. A frame
    that cannot be read (garbage, a line past 16 MiB) raises ``BadFrame``: close and attach again from the processed
    offset. ``for frame in attachment`` reads until the end. ``send(frame)`` writes one line to the device
    (``{"op":"send","seq":N,"d":"..."}``, ``{"op":"sig","sig":"TERM"}``, ``{"op":"detach"}``). ``close()`` ends it
    from any thread (a reader blocked in ``read_frame`` returns None); ``kill()`` is ssh dying: SIGKILL, no goodbye.
    """

    def __init__(self, proc, label=""):
        self.proc, self.label = proc, label
        self.hi, self.att = None, None
        self._buf, self._scanned, self._eof = bytearray(), 0, False
        self._closed = False
        self._reading = 0
        self._guard = threading.Lock()
        self._write = threading.Lock()
        self._err = bytearray()
        self._fd = proc.stdout.fileno()
        threading.Thread(target=self._drain, daemon=True, name="attach-stderr").start()

    # -- reading
    def _drain(self):
        try:
            for chunk in iter(lambda: self.proc.stderr.read1(4096), b""):
                self._err += chunk
                del self._err[:-STDERR_KEEP]
        except (OSError, ValueError):
            pass

    def stderr_tail(self):
        return tail_text(bytes(self._err))

    def _fill(self, timeout):
        """True when bytes or the end of the stream arrived within ``timeout``; False when not."""
        if self._eof:
            return True
        if self._closed:
            self._eof = True
            return True
        try:
            ready = select.select([self._fd], [], [], timeout)[0]
            if not ready:
                return False
            chunk = os.read(self._fd, 1 << 20)
        except (OSError, ValueError):
            chunk = b""
        if chunk:
            self._buf += chunk
        else:
            self._eof = True
        return True

    def _line(self, timeout):
        """The next line without its newline; None at the end of the stream (a half line is lost with it)."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            index = self._buf.find(b"\n", self._scanned)
            if index >= 0:
                line = bytes(self._buf[:index])
                del self._buf[:index + 1]
                self._scanned = 0
                return line
            self._scanned = len(self._buf)
            if self._scanned > LINE_MAX:
                raise BadFrame("a line from the device is longer than %d bytes" % LINE_MAX)
            if self._eof or self._closed:
                return None
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise TimeoutError("no frame within %s seconds" % timeout)
            self._fill(0.5 if remaining is None else min(remaining, 0.5))

    def filter(self, frame):
        """Every parsed frame passes here; a subclass (tests/support/fake_device.py) may drop one by returning None."""
        return frame

    def read_frame(self, timeout=None):
        with self._guard:
            self._reading += 1
        try:
            while True:
                line = self._line(timeout)
                if line is None:
                    return None
                if not line.strip():
                    continue
                frame = self.filter(decode(line))
                if frame is not None:
                    return frame
        finally:
            with self._guard:
                self._reading -= 1
                last = self._reading == 0 and self._closed
            if last:
                self._release()

    def __iter__(self):
        while True:
            frame = self.read_frame()
            if frame is None:
                return
            yield frame

    def handshake(self, request, patience):
        """Send the attach request, read the hi frame and the ``att`` reply (or the error that replaced it)."""
        deadline = time.monotonic() + patience
        try:
            self.send_raw(request)
        except Closed:
            # An ssh that cannot connect exits at once, and on a fast machine before the request is written: the
            # reading below meets the end of the stream and judges it by the exit code and stderr, as when the
            # write got in first (seen in CI: Closed instead of Unreachable).
            pass
        while True:
            hi, used = scan_hi(self._buf)
            if hi is not None:
                del self._buf[:used]
                self._scanned = 0
                self.hi = hi
                break
            if self._eof or self._closed:
                self.proc.poll()
                raise failure(self._status(), b"", bytes(self._err), "the attach connection ended before the hi frame")
            if time.monotonic() > deadline:
                raise LinkTimeout("no hi frame within %s seconds" % patience)
            self._fill(0.2)
        while True:
            try:
                line = self._line(max(0.05, deadline - time.monotonic()))
            except TimeoutError:
                raise LinkTimeout("no attach answer within %s seconds" % patience)
            if line is None:
                raise failure(self._status(), b"", bytes(self._err), "the attach connection ended before its answer")
            if line.strip():
                break
        head = decode(line)
        if head.get("ok") is not True:
            raise error_for(head.get("err"), str(head.get("msg") or ""))
        self.att = head
        return head

    def _status(self):
        try:
            return self.proc.wait(2)
        except subprocess.TimeoutExpired:
            return None

    # -- writing
    def send_raw(self, data):
        try:
            with self._write:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
        except (OSError, ValueError):
            raise Closed("the attach connection is closed")

    def send(self, frame):
        self.send_raw(encode(frame))

    # -- ending
    @property
    def alive(self):
        return self.proc.poll() is None

    @property
    def returncode(self):
        return self.proc.poll()

    def kill(self):
        """Like ssh dying: SIGKILL the process group, no detach."""
        kill_group(self.proc)
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            pass

    def close(self, detach=True):
        """End the connection politely (detach, close stdin, a second to leave) and then for sure. Any thread."""
        with self._guard:
            if self._closed:
                return
            self._closed = True
            idle = self._reading == 0
        if detach and self.proc.poll() is None:
            try:
                self.send_raw(encode({"op": "detach"}))
                self.proc.stdin.close()
                self.proc.wait(1.0)
            except (Closed, OSError, ValueError, subprocess.TimeoutExpired):
                pass
        self.kill()
        if idle:
            self._release()

    def _release(self):
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# ---------------------------------------------------------------- the transports

class Transport:
    """What the hub does to one device. Subclasses say how a command line on the device is run (``_popen``)."""

    attachment_class = Attachment

    def __init__(self, alias, *, hub, shim, python="python3"):
        self.alias, self.hub, self.shim, self.python = alias, hub, shim, python
        self.pushed = {}  # (file id, name) -> monotonic time of the last confirmation (core/devices.py push_file)
        self._ids = itertools.count(1)

    # -- command lines
    @property
    def shim_path(self):
        return "%s/%s/shim.py" % (SHIM_ROOT, self.shim)

    def remote(self, cmd, shim_path=None):
        """The command line to run on the device. ``shim_path`` is the shim file a task recorded (``status`` returns
        it): later calls for that task use the shim that started it, whatever this hub pushed since."""
        return "%s %s %s" % (word(self.python), word(shim_path or self.shim_path), cmd)

    def _popen(self, remote, *, stdin):
        raise NotImplementedError

    def before(self, what):
        """A hook run before every call and attach (tests/support/fake_device.py injects delay and loss here)."""

    # -- one-shot commands
    def _run(self, remote, payload, patience):
        proc = self._popen(remote, stdin=subprocess.PIPE)
        try:
            out, err = proc.communicate(payload, timeout=patience)
        except subprocess.TimeoutExpired:
            kill_group(proc)
            proc.communicate()
            raise LinkTimeout("no answer within %s seconds" % patience)
        except BaseException:
            kill_group(proc)
            proc.communicate()
            raise
        return proc.returncode, out, err

    def request(self, cmd, fields):
        body = {"cmd": cmd, "id": "%s-%d" % (self.alias, next(self._ids)), "hub": self.hub}
        body.update(fields)
        return body

    def call(self, cmd, *, patience=30.0, shim_path=None, **fields):
        """Run one command and return the answer's fields (``ok`` and ``id`` included). ``patience`` is how long the
        hub waits; a request's own limit (``exec``'s ``timeout``) goes in ``fields``."""
        self.before(cmd)
        line = encode(self.request(cmd, fields))
        code, out, err = self._run(self.remote(cmd, shim_path), line, patience)
        return parse_answer(out, err, code).head

    def get(self, path, *, offset=0, limit=None, patience=120.0, shim_path=None):
        """One segment of a file: ``(head, bytes)``; ``head["eof"]`` says whether it was the last."""
        self.before("get")
        fields = {"path": path, "offset": offset}
        if limit is not None:
            fields["limit"] = limit
        code, out, err = self._run(self.remote("get", shim_path), encode(self.request("get", fields)), patience)
        answer = parse_answer(out, err, code, body=True)
        return answer.head, answer.body

    def put(self, path, data, *, offset=0, total=None, total_sha256=None, mode=None, patience=120.0, shim_path=None):
        """One segment of a file (``put_file`` does a whole one): the answer's fields."""
        self.before("put")
        if len(data) > DATA_MAX:
            raise TooLarge("a put segment is at most %d bytes" % DATA_MAX)
        fields = {"path": path, "len": len(data), "offset": offset, "sha256": hashlib.sha256(data).hexdigest()}
        if total is not None:
            fields["total"] = total
        if total_sha256 is not None:
            fields["total_sha256"] = total_sha256
        if mode is not None:
            fields["mode"] = mode
        payload = encode(self.request("put", fields)) + data + b"\n"
        code, out, err = self._run(self.remote("put", shim_path), payload, patience)
        return parse_answer(out, err, code).head

    def get_file(self, path, *, max_bytes=None, segment=DATA_MAX, patience=120.0, shim_path=None):
        """A whole file's bytes, in segments of ``segment``; ``TooLarge`` before reading past ``max_bytes``."""
        chunks, offset = [], 0
        while True:
            head, data = self.get(path, offset=offset, limit=segment, patience=patience, shim_path=shim_path)
            if max_bytes is not None and head.get("size", 0) > max_bytes:
                raise TooLarge("%s is %d bytes, the limit is %d" % (path, head["size"], max_bytes))
            chunks.append(data)
            offset += len(data)
            if head.get("eof") or not data:
                return b"".join(chunks)

    def put_file(self, path, data, *, mode=0o600, segment=DATA_MAX, patience=120.0, shim_path=None):
        """A whole file in segments of ``segment``, written to a temporary name and renamed once the whole hash
        matches; the same content already there is not rewritten (``same``). The last answer's fields."""
        whole, offset, answer = hashlib.sha256(data).hexdigest(), 0, {}
        while True:
            part = data[offset:offset + segment]
            answer = self.put(path, part, offset=offset, total=len(data), total_sha256=whole, mode=mode,
                              patience=patience, shim_path=shim_path)
            offset += len(part)
            if answer.get("same") or offset >= len(data):
                return answer

    def hello(self, want=("claude", "codex"), *, patience=30.0, shim_path=None):
        """The device's ``hello`` answer, after checking that the shim speaks a protocol this hub does."""
        answer = self.call("hello", proto=PROTO, want=list(want), patience=patience, shim_path=shim_path)
        low = answer.get("min_proto", 1)
        if isinstance(low, int) and low > PROTO:
            raise BadProto("the shim on %s needs protocol %d, this hub speaks %d" % (self.alias, low, PROTO))
        return answer

    def shell(self, script, data=b"", *, patience=30.0):
        """A plain ``sh -c`` on the device (no shim: this is how the shim is pushed): ``(exit code, stdout, stderr)``."""
        self.before("shell")
        code, out, err = self._run("sh -c " + shlex.quote(script), data, patience)
        return code, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    # -- the long connection
    def attach(self, task, offset=0, *, patience=15.0, shim_path=None):
        """An ``Attachment`` replaying the task's journal from byte ``offset`` (``BadOffset`` past its end, ``NoTask``
        for a task that is not there)."""
        self.before("attach")
        proc = self._popen(self.remote("attach", shim_path), stdin=subprocess.PIPE)
        connection = self.attachment_class(proc, self.alias)
        try:
            connection.handshake(encode(self.request("attach", {"task": task, "from": offset})), patience)
        except BaseException:
            connection.close(detach=False)
            raise
        return connection

    def close(self):
        """Nothing to release here; ``SshTransport`` can end its master connection."""


class SshTransport(Transport):
    """ssh to the device. ``ssh_argv(remote=None, op=None)`` gives the argv (core/devices.py ``ssh_argv``): options,
    host and ``-- <remote command>``; with ``op`` (``check`` or ``exit``) a control-master request instead."""

    def __init__(self, alias, ssh_argv, *, hub, shim, python="python3", environ=None):
        super().__init__(alias, hub=hub, shim=shim, python=python)
        self.ssh_argv, self.environ = ssh_argv, environ

    def _popen(self, remote, *, stdin):
        return subprocess.Popen(self.ssh_argv(remote), stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=self.environ, start_new_session=True)

    def _control(self, op, patience=10.0):
        try:
            done = subprocess.run(self.ssh_argv(op=op), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, env=self.environ, timeout=patience, start_new_session=True)
        except (OSError, subprocess.SubprocessError):
            return False
        return done.returncode == 0

    def master_alive(self):
        """Whether a ControlMaster connection to the device is up (``ssh -O check``)."""
        return self._control("check")

    def close(self):
        """End the master connection (``ssh -O exit``); the next call opens a new one."""
        self._control("exit")


class LocalTransport(Transport):
    """The same remote command line run by ``sh -c`` here, under a temporary HOME: the real shim, no ssh.

    ``home`` stands in for the device's HOME (the shim keeps everything under ``<home>/.covey``); ``python`` is this
    interpreter. ``env`` adds variables to what the shim and its tasks inherit."""

    def __init__(self, home, *, alias="local", hub="hub1", shim=None, python=None, env=None):
        super().__init__(alias, hub=hub, shim=shim or shim_id(shim_bytes()), python=python or sys.executable)
        self.home = Path(home)
        self.env = {"HOME": str(self.home), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    "PYTHONDONTWRITEBYTECODE": "1", "LANG": "C", **(env or {})}

    def _popen(self, remote, *, stdin):
        return subprocess.Popen(["sh", "-c", remote], stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=self.env, start_new_session=True)

    def task_dir(self, task):
        return self.home / ".covey" / "tasks" / self.hub / task
