"""The device-side program the hub pushes over ssh: it supervises Harness tasks and serves files, stdlib only.

A single file with no import from covey: the hub reads its bytes and pushes them to ``~/.covey/shim/<id>/shim.py``
on a device, where ``<id>`` is the first 16 hex digits of the SHA-256 of those bytes, and runs
``python3 shim.py <cmd>`` through ssh; the hub never imports it. It must run on Python 3.9 (the interpreter a
Mac's command line tools carry) and use the standard library only; ``scripts/check_shim_compat.py`` checks both.

Protocol 1 (docs/architecture.md, "设备端小程序"). stdout's first line is always
``{"k":"hi","proto":1,"shim":"<id>"}``; what a login shell printed before it is the hub's to drop. A command is
one JSON line on stdin, ``{"cmd","id",...}``, answered by one line, ``{"ok":true,"id",...}`` or
``{"ok":false,"id","err":"<code>","msg":"..."}``. ``get`` and ``put`` carry ``len`` in the header line, then
that many bytes and a newline. ``attach`` is a long connection in both directions (NDJSON, 16 MiB a line at most):
the device sends record frames ``{"o":start,"e":end,"k":"out|cp|ack|exit",...}`` read from the task's journal by
byte offset, and ``{"k":"hb"}`` every 5 seconds; the hub sends ``{"op":"send|sig|detach",...}``. Unknown keys
and unknown ``k`` are ignored on both sides; fields are only ever added.

One supervisor per task (double fork, setsid) holds the Harness's stdin and stdout under
``~/.covey/tasks/<hub>/<task>/``: ``spec.json`` (the spawn request's hash, which makes spawn idempotent),
``state.json`` (pid, pgid, boot id, proto, shim path, spec_sha; the pgid is the supervisor's own getpgrp, never
its pid), ``journal`` (append only), ``inbox/<seq>.json``, ``seq`` (the last input delivered), ``ctl/``,
``checkpoint.json``, ``incoming/``, ``err.log`` and, once the Agent has ended, ``exit.json``.

An ``exec`` may carry a ``mark`` (the hub's turn, core/device_access.py): while its command runs, its process group is
kept in ``~/.covey/marks/<hub>/<mark>/<pid>.json`` (pid, start, boot) and the command's environment has
``COVEY_DEVICE_MARK=<hub>/<mark>``, so ``reap`` with that ``mark`` stops what is still running of it after the hub's ssh
went away (the command outlives its connection: an ``exec`` ends only when its command does, or at its timeout). The
record goes when its command ends, and the mark's folder with its last one; ``gc`` drops the folders and the records of
gone groups a killed ``exec`` left behind (``gc_marks``).
"""
from __future__ import annotations

import base64
import fnmatch
import hashlib
import json
import os
import platform
import re
import select
import shlex
import shutil
import signal
import stat as statmod
import subprocess
import sys
import time
import traceback

PROTO = 1
MIN_PROTO = 1
LINE_MAX = 16 << 20  # A frame line, either direction.
DATA_MAX = 64 << 20  # One get or put segment.
CHUNK = 2 << 20  # A longer Agent line is split into pieces of this many bytes (JSON escaping can grow them 6x).
CP_MAX = 1 << 20  # A checkpoint larger than this is only marked in its record.
ERRLOG_MAX = 1 << 20
SEEDS_MAX = 8 << 20  # The seed files of one spawn, in bytes before base64.
HEARTBEAT = 5.0
TICK = 0.1  # The supervisor's loop.
ATTACH_TICK = 0.05
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
TOOL_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
LOGGED_OUT = re.compile(r'"loggedIn"\s*:\s*false')
SIGNALS = {"TERM": signal.SIGTERM, "KILL": signal.SIGKILL}
# What hello reports of the device's environment: proxies and certificate paths, never a token.
ENV_REPORT = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy",
              "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")
EXTRA_BIN = (".local/bin", ".claude/local", ".npm-global/bin", ".volta/bin", ".cargo/bin", ".bun/bin",
             "/usr/local/bin", "/opt/homebrew/bin", "/usr/bin")
# A Harness whose sign-in is a file under the home rather than a command's answer: Antigravity's Google sign-in (without it
# agy waits a minute for a browser sign-in, and its words about it go to a stderr the hub never sees). Only looked at.
SIGN_IN_FILES = {"agy": (".gemini", "antigravity-cli", "antigravity-oauth-token")}
NETWORK_FS = {"nfs", "nfs4", "cifs", "smbfs", "smb3", "afs", "ceph", "lustre", "gpfs", "9p", "beegfs", "glusterfs",
              "fuse.sshfs", "fuse.glusterfs", "fuse.cephfs", "fuse.nfs", "fuse.s3fs", "virtiofs"}


class Fail(Exception):
    """An error answer: ``code`` is the ``err`` field, ``msg`` the human part."""

    def __init__(self, code, msg=""):
        super().__init__(code, msg)
        self.code, self.msg = code, msg


class Gone(Exception):
    """The hub's end of the pipe is closed: stop quietly."""


def now():
    return round(time.time(), 3)


def encode(obj):
    """One line, ASCII only whatever the device's locale, so non-UTF-8 file names and lone surrogates survive."""
    return json.dumps(obj, separators=(",", ":")).encode("ascii") + b"\n"


def home():
    return os.path.expanduser("~")


def covey_root():
    return os.path.join(home(), ".covey")


def expand(path):
    if not isinstance(path, str) or not path or "\0" in path:
        raise Fail("bad_request", "path must be a non-empty string")
    return os.path.expanduser(path)


def own_path():
    return os.path.abspath(__file__)


def own_id():
    with open(own_path(), "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()[:16]


def name_of(value, what):
    if not isinstance(value, str) or not NAME.match(value):
        raise Fail("bad_request", "%s must match %s" % (what, NAME.pattern))
    return value


def number(value, default, low, high):
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise Fail("bad_request", "a number was expected, got %r" % (value,))
    return max(low, min(high, value))


def write_file(path, data, mode=0o600):
    """Write ``data`` through a temporary file in the same directory and rename it over ``path``."""
    tmp = "%s.tmp%d" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_json(path, value):
    write_file(path, json.dumps(value, separators=(",", ":")).encode("ascii"))


def create_exclusive(path, data):
    """Create ``path`` with ``data`` whole, or return False when it exists (a hard link is atomic)."""
    tmp = "%s.new%d" % (path, os.getpid())
    write_file(tmp, data)
    try:
        os.link(tmp, path)
        return True
    except FileExistsError:
        return False
    finally:
        os.unlink(tmp)


def read_json(path):
    try:
        with open(path, "rb") as handle:
            value = json.loads(handle.read().decode("utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def read_int(path, default=0):
    try:
        with open(path, "r") as handle:
            return int(handle.read().strip() or default)
    except (OSError, ValueError):
        return default


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def text_or_b64(data):
    """{"d": text} for UTF-8 data, else {"b64": ...}."""
    try:
        return {"d": data.decode("utf-8")}
    except UnicodeDecodeError:
        return {"b64": base64.b64encode(data).decode("ascii")}


# ---------------------------------------------------------------- processes and the machine

def boot_id():
    try:
        with open("/proc/sys/kernel/random/boot_id") as handle:
            return handle.read().strip()
    except OSError:
        pass
    try:
        out = subprocess.run(["sysctl", "-n", "kern.boottime"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             timeout=5).stdout.decode("utf-8", "replace").strip()
        return out or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def proc_info(pid):
    """(state letter, process group, start marker) of a process, or None when there is none."""
    try:
        with open("/proc/%d/stat" % pid) as handle:
            fields = handle.read().rsplit(")", 1)[1].split()
        return fields[0], int(fields[2]), fields[19]
    except (OSError, IndexError, ValueError):
        pass
    if os.path.isdir("/proc/self"):
        return None  # Linux: the process is gone.
    try:
        out = subprocess.run(["ps", "-o", "state=,pgid=,lstart=", "-p", str(pid)], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=5).stdout.decode("utf-8", "replace").split(None, 2)
        return out[0], int(out[1]), out[2].strip()
    except (OSError, subprocess.SubprocessError, IndexError, ValueError):
        return None


def process_alive(pid, start=None):
    """Whether ``pid`` runs (a zombie does not count) and, given ``start``, is still the process that began then."""
    if not isinstance(pid, int) or pid <= 1:
        return False
    info = proc_info(pid)
    return bool(info) and info[0][:1] not in "ZX" and (start is None or info[2] == start)


def group_members(pgid):
    """The pids of the live (non-zombie) processes in a process group."""
    found = []
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return []
    except OSError:
        pass
    if os.path.isdir("/proc/self"):
        for entry in os.listdir("/proc"):
            if entry.isdigit():
                info = proc_info(int(entry))
                if info and info[1] == pgid and info[0][:1] not in "ZX":
                    found.append(int(entry))
        return sorted(found)
    try:
        out = subprocess.run(["ps", "-A", "-o", "pid=,pgid=,state="], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             timeout=10).stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[1] == str(pgid) and parts[2][:1] not in "ZX":
            found.append(int(parts[0]))
    return sorted(found)


def filesystem_of(path):
    """(filesystem type, whether it is a network one) of the mount holding ``path``; ("", False) when unknown."""
    try:
        real = os.path.realpath(path)
        best, kind = "", ""
        with open("/proc/mounts") as handle:
            for line in handle:
                parts = line.split()
                if len(parts) >= 3:
                    mount = parts[1].replace("\\040", " ")
                    if (real == mount or real.startswith(mount.rstrip("/") + "/")) and len(mount) >= len(best):
                        best, kind = mount, parts[2]
        return kind, kind in NETWORK_FS or kind.startswith("nfs")
    except OSError:
        return "", False


# ---------------------------------------------------------------- the wire

class Wire:
    """stdin and stdout of one invocation: bounded line reads, exact reads, framed writes."""

    def __init__(self):
        self.buf = bytearray()
        self.scanned = 0
        self.eof = False
        self.out = sys.stdout.buffer

    def fill(self, timeout):
        """Read what is waiting on stdin (up to ``timeout`` seconds); True when something or EOF arrived."""
        if self.eof:
            return True
        ready = select.select([0], [], [], timeout)[0]
        if not ready:
            return False
        chunk = os.read(0, 1 << 20)
        if chunk:
            self.buf += chunk
        else:
            self.eof = True
        return True

    def line(self, timeout=None):
        """The next complete line without its newline, or None (nothing yet, or EOF: ``eof`` tells). A line
        past LINE_MAX raises Fail("too_large")."""
        deadline = None if timeout is None else time.time() + timeout
        while True:
            index = self.buf.find(b"\n", self.scanned)
            if index >= 0:
                line = bytes(self.buf[:index])
                del self.buf[:index + 1]
                self.scanned = 0
                return line
            self.scanned = len(self.buf)
            if self.scanned > LINE_MAX:
                raise Fail("too_large", "a line is longer than %d bytes" % LINE_MAX)
            if self.eof:
                return None
            remaining = None if deadline is None else deadline - time.time()
            if remaining is not None and remaining <= 0:
                return None
            self.fill(1.0 if remaining is None else min(remaining, 1.0))

    def exact(self, count, timeout=120.0):
        deadline = time.time() + timeout
        while len(self.buf) < count:
            if self.eof:
                raise Fail("io", "stdin closed before %d bytes arrived" % count)
            if time.time() > deadline:
                raise Fail("timeout", "no data for %d seconds" % timeout)
            self.fill(1.0)
        data = bytes(self.buf[:count])
        del self.buf[:count]
        self.scanned = 0
        return data

    def write(self, data):
        try:
            self.out.write(data)
            self.out.flush()
        except (BrokenPipeError, ValueError, OSError):
            raise Gone()

    def send(self, obj):
        self.write(encode(obj))


# ---------------------------------------------------------------- tasks: paths and records

def task_dir(hub, key, must_exist=True):
    path = os.path.join(covey_root(), "tasks", name_of(hub, "hub"), name_of(key, "task"))
    if must_exist and not os.path.isfile(os.path.join(path, "spec.json")):
        raise Fail("no_task", "no task %s/%s on this device" % (hub, key))
    return path


def request_task(req):
    return task_dir(req.get("hub"), req.get("task"))


def read_exit(tdir):
    return read_json(os.path.join(tdir, "exit.json"))


def supervisor_alive(state):
    return bool(state) and process_alive(state.get("pid"), state.get("start") or None) and state.get("boot") == boot_id()


def journal_size(tdir):
    try:
        return os.path.getsize(os.path.join(tdir, "journal"))
    except OSError:
        return 0


def append_record(tdir, record):
    """Append one record to a task's journal. Only the supervisor does, or ``reap`` once it is dead."""
    path = os.path.join(tdir, "journal")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        if os.fstat(fd).st_size:
            with open(path, "rb") as handle:
                handle.seek(-1, os.SEEK_END)
                if handle.read(1) != b"\n":
                    os.write(fd, b"\n")  # A line the dead supervisor did not finish.
        os.write(fd, encode(record))
    finally:
        os.close(fd)


def do_send(tdir, seq, data):
    """Queue input ``seq``: "applied", or "dup" when it was queued or delivered before."""
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        raise Fail("bad_request", "seq must be an integer from 1")
    if not isinstance(data, str) or "\n" in data:
        raise Fail("bad_request", "d must be one line of text")
    body = json.dumps({"seq": seq, "d": data}, separators=(",", ":")).encode("ascii")
    if len(body) > LINE_MAX:
        raise Fail("too_large", "the input is larger than %d bytes" % LINE_MAX)
    inbox = os.path.join(tdir, "inbox", "%d.json" % seq)
    stored = read_json(inbox)
    if stored is not None:
        if stored.get("d") != data:
            raise Fail("conflict", "seq %d was queued with other content" % seq)
        return "dup"
    if seq <= read_int(os.path.join(tdir, "seq")):
        return "dup"  # Delivered, and its inbox file already cleaned up.
    if read_exit(tdir) is not None:
        raise Fail("exited", "the task has ended")
    if create_exclusive(inbox, body):
        return "applied"
    stored = read_json(inbox)
    if stored is not None and stored.get("d") != data:
        raise Fail("conflict", "seq %d was queued with other content" % seq)
    return "dup"


def request_control(tdir, op, **fields):
    """Ask the supervisor for ``op`` (stop, term, kill, eof) through a file in ``ctl/``."""
    ctl = os.path.join(tdir, "ctl")
    os.makedirs(ctl, exist_ok=True)
    fields["op"] = op
    write_file(os.path.join(ctl, "%020d-%d.json" % (time.time_ns(), os.getpid())), json.dumps(fields).encode("ascii"))


# ---------------------------------------------------------------- the supervisor

def close_inherited_fds():
    for directory in ("/proc/self/fd", "/dev/fd"):
        try:
            fds = [int(name) for name in os.listdir(directory) if name.isdigit()]
            break
        except OSError:
            fds = None
    if fds is None:
        os.closerange(3, 1024)
        return
    for fd in fds:
        if fd > 2:
            try:
                os.close(fd)
            except OSError:
                pass


class Supervisor:
    """Owns one Harness: its pipes, the journal, the inbox, signals and the checkpoint poll. One thread, one
    select loop, one writer of the journal."""

    def __init__(self, tdir, spec, sha, hub, key):
        self.tdir, self.spec, self.sha, self.hub, self.key = tdir, spec, sha, hub, key
        self.pgid = os.getpgrp()
        self.child = None
        self.obuf = bytearray()
        self.pending = bytearray()
        self.inflight = 0  # The seq whose bytes are in ``pending``.
        self.last_seq = read_int(os.path.join(tdir, "seq"))
        self.stdin_open = True
        self.eof_requested = False
        self.stop = None  # {"phase": "term"|"kill", "at": time, "grace": seconds, "reason": str}
        self.signalled = 0
        self.sent_term = 0.0
        self.last_kill = 0.0
        self.err_size = 0
        self.err_cut = False
        self.cp_sig = None
        self.cp_next = 0.0
        self.cp_tries = 0
        self.log_fd = None

    # --- small writers
    def journal(self, record):
        record["t"] = now()
        data = encode(record)
        while data:
            data = data[os.write(self.jfd, data):]

    def log(self, text):
        try:
            os.write(self.log_fd, ("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), text)).encode("utf-8", "replace"))
        except OSError:
            pass

    def update_state(self, **fields):
        self.state.update(fields)
        write_json(os.path.join(self.tdir, "state.json"), self.state)

    # --- start
    def run(self):
        os.chdir("/")
        self.jfd = os.open(os.path.join(self.tdir, "journal"), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        self.log_fd = os.open(os.path.join(self.tdir, "err.log"), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        self.err_size = os.fstat(self.log_fd).st_size
        for number_ in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(number_, self.on_signal)
        pid = os.getpid()
        self.state = {"task": self.key, "hub": self.hub, "pid": pid, "pgid": self.pgid, "child": None,
                      "start": (proc_info(pid) or ("", 0, ""))[2], "boot": boot_id(), "proto": PROTO,
                      "shim": own_path(), "spec_sha": self.sha, "started": now()}
        write_json(os.path.join(self.tdir, "state.json"), self.state)
        try:
            self.launch()
        except (OSError, ValueError) as exc:
            self.finish(127, "spawn_failed", error="%s: %s" % (type(exc).__name__, exc))
            return
        try:
            self.loop()
        except BaseException:  # A supervisor bug must not leave an Agent nobody supervises.
            self.log("supervisor error:\n" + traceback.format_exc())
            self.stop = self.stop or {"phase": "kill", "at": 0, "grace": 0, "reason": "supervisor_error"}
            self.kill_group()
        self.close_stdin()
        self.finish(self.child.wait(), self.stop["reason"] if self.stop else "exit")

    def launch(self):
        spec = self.spec
        argv = list(spec["argv"])
        if spec.get("shell") == "login":
            argv = ["bash" if shutil.which("bash") else "sh", "-ic" if shutil.which("bash") else "-c", shlex.join(argv)]
        cwd = os.path.expanduser(spec["cwd"]) if spec.get("cwd") else None
        merged = dict(os.environ)
        for name, value in (spec.get("env") or {}).items():
            if value is None:
                merged.pop(name, None)
            else:
                merged[name] = value
        merged["COVEY_TASK_DIR"] = self.tdir
        if cwd:
            merged["PWD"] = cwd
        self.child = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      cwd=cwd, env=merged, bufsize=0)
        os.set_blocking(self.child.stdin.fileno(), False)
        info = proc_info(self.child.pid)
        self.update_state(child=self.child.pid, child_start=info[2] if info else None)
        checkpoint = spec.get("checkpoint") or {}
        self.cp_path = os.path.expanduser(checkpoint["path"]) if checkpoint.get("path") else None
        self.cp_poll = number(checkpoint.get("poll"), 1.0, 0.1, 60.0) if checkpoint else 1.0
        self.cp_sig = self.signature()
        self.deadline = spec.get("deadline") or 0

    def on_signal(self, signum, frame):
        self.signalled = signum

    # --- the loop
    def loop(self):
        out, err = self.child.stdout.fileno(), self.child.stderr.fileno()
        open_fds = {out, err}
        ended_at = None
        while True:
            writers = [self.child.stdin.fileno()] if self.pending and self.stdin_open else []
            ready_r, ready_w, _ = select.select(sorted(open_fds), writers, [], TICK)
            for fd in ready_r:
                data = os.read(fd, 1 << 16)
                if not data:
                    open_fds.discard(fd)
                elif fd == out:
                    self.feed(data)
                else:
                    self.write_err(data)
            if ready_w:
                self.push()
            self.tick()
            if self.child.poll() is not None:
                ended_at = ended_at or time.time()
                # A descendant that kept the pipes open must not hold the end up for long.
                if not open_fds or (time.time() - ended_at > 1.0 and not ready_r):
                    break
        self.flush_partial()
        self.compare_checkpoint(final=True)

    def tick(self):
        t = time.time()
        if self.signalled and not self.stop and t - self.sent_term > 1.0:
            self.begin_stop("signal", 5.0)
        self.signalled = 0
        if self.deadline and now() > self.deadline and not self.stop:
            self.begin_stop("deadline", 5.0)
        self.take_controls()
        if self.stop and self.child.poll() is None and t - self.stop["at"] >= self.stop["grace"]:
            self.kill_group()
            self.stop["phase"] = "kill"
        if self.stdin_open and not self.pending:
            self.next_input()
        if self.eof_requested and self.stdin_open and not self.pending and not self.inbox_waiting():
            self.close_stdin()
        if self.cp_path and t >= self.cp_next:
            self.cp_next = t + self.cp_poll
            self.compare_checkpoint()

    def begin_stop(self, reason, grace):
        self.stop = {"phase": "term", "at": time.time(), "grace": grace, "reason": reason}
        self.send_term()

    def send_term(self):
        self.sent_term = time.time()
        try:
            os.killpg(self.pgid, signal.SIGTERM)  # The supervisor is in the group: on_signal swallows its own.
        except OSError:
            pass

    def kill_group(self):
        """SIGKILL everything in the group but the supervisor, which has an exit to write."""
        me = os.getpid()
        if time.time() - self.last_kill < 0.5:
            return
        self.last_kill = time.time()
        for pid in group_members(self.pgid):
            if pid != me:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        try:
            self.child.kill()
        except OSError:
            pass

    def take_controls(self):
        ctl = os.path.join(self.tdir, "ctl")
        try:
            names = sorted(os.listdir(ctl))
        except OSError:
            return
        for name in names:
            path = os.path.join(ctl, name)
            request = read_json(path)
            try:
                os.unlink(path)
            except OSError:
                pass
            if not request:
                continue
            op = request.get("op")
            if op == "stop" and not self.stop:
                self.begin_stop("stopped", number(request.get("grace"), 5.0, 0.0, 600.0))
            elif op == "term":
                self.send_term()
            elif op == "kill":
                self.stop = self.stop or {"phase": "kill", "at": 0, "grace": 0, "reason": "killed"}
                self.kill_group()
            elif op == "eof":
                self.eof_requested = True

    # --- Agent output
    def feed(self, data):
        self.obuf += data
        while self.obuf:
            index = self.obuf.find(b"\n")
            if 0 <= index < CHUNK:
                self.emit(bytes(self.obuf[:index]), False)
                del self.obuf[:index + 1]
            elif len(self.obuf) >= CHUNK:
                self.emit(bytes(self.obuf[:CHUNK]), True)
                del self.obuf[:CHUNK]
            else:
                break

    def flush_partial(self):
        if self.obuf:
            self.emit(bytes(self.obuf), False)
            self.obuf.clear()

    def emit(self, line, more):
        record = {"k": "out"}
        record.update(text_or_b64(line))
        if more:
            record["more"] = True
        self.journal(record)

    def write_err(self, data):
        if self.err_size < ERRLOG_MAX:
            data = data[:ERRLOG_MAX - self.err_size]
            os.write(self.log_fd, data)
            self.err_size += len(data)
        elif not self.err_cut:
            self.err_cut = True
            os.write(self.log_fd, b"\n[err.log is full: the rest is dropped]\n")

    # --- Agent input
    def inbox_waiting(self):
        return os.path.exists(os.path.join(self.tdir, "inbox", "%d.json" % (self.last_seq + 1)))

    def next_input(self):
        seq = self.last_seq + 1
        frame = read_json(os.path.join(self.tdir, "inbox", "%d.json" % seq))
        if frame is None or not isinstance(frame.get("d"), str):
            return
        self.pending += frame["d"].encode("utf-8") + b"\n"
        self.inflight = seq
        self.push()

    def push(self):
        try:
            while self.pending:
                written = os.write(self.child.stdin.fileno(), bytes(self.pending[:1 << 16]))
                del self.pending[:written]
        except BlockingIOError:
            return
        except (BrokenPipeError, OSError):
            self.pending.clear()
            self.stdin_open = False
            self.log("the Agent's stdin is closed: input %d was not delivered" % self.inflight)
            return
        if self.inflight:
            self.last_seq = self.inflight
            write_file(os.path.join(self.tdir, "seq"), str(self.last_seq).encode("ascii"))
            self.journal({"k": "ack", "seq": self.last_seq})
            self.inflight = 0

    def close_stdin(self):
        if self.stdin_open:
            self.stdin_open = False
            try:
                self.child.stdin.close()
            except OSError:
                pass

    # --- checkpoint
    def signature(self):
        try:
            st = os.stat(self.cp_path) if self.cp_path else None
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size, st.st_ino) if st else None

    def compare_checkpoint(self, final=False):
        if not self.cp_path:
            return
        before = self.signature()
        if before == self.cp_sig:
            self.cp_tries = 0
            return
        record = {"k": "cp"}
        if before is None:
            record["gone"] = True
        elif before[1] > CP_MAX:
            record.update(size=before[1], over=True)
        else:
            try:
                with open(self.cp_path, "rb") as handle:
                    data = handle.read(CP_MAX + 1)
            except OSError:
                return
            if self.signature() != before and not final and self.cp_tries < 3:
                self.cp_tries += 1  # Written while read: look again next tick.
                return
            record.update(text_or_b64(data), size=len(data))
        self.cp_sig, self.cp_tries = before, 0
        self.journal(record)

    # --- the end
    def finish(self, code, reason, error=None):
        sig = -code if isinstance(code, int) and code < 0 else None
        left = [pid for pid in group_members(self.pgid) if pid != os.getpid()]
        result = {"code": code, "sig": sig, "reason": reason, "left": len(left)}
        if error:
            result["error"] = error
        self.journal(dict(result, k="exit"))
        write_json(os.path.join(self.tdir, "exit.json"), dict(result, ended=now()))


def start_supervisor(tdir, spec, sha, hub, key):
    """Fork twice (setsid between), so the supervisor is no one's child and ssh closing cannot reach it."""
    first = os.fork()
    if first:
        os.waitpid(first, 0)
        return
    try:
        os.setsid()
        if os.fork():
            os._exit(0)
        close_inherited_fds()
        devnull = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(devnull, fd)
        try:
            Supervisor(tdir, spec, sha, hub, key).run()
        except BaseException:
            try:
                with open(os.path.join(tdir, "err.log"), "ab") as handle:
                    handle.write(traceback.format_exc().encode("utf-8", "replace"))
            except OSError:
                pass
    finally:
        os._exit(0)


# ---------------------------------------------------------------- commands

def cmd_hello(req, wire):
    hub_proto = req.get("proto", PROTO)
    if isinstance(hub_proto, int) and hub_proto < MIN_PROTO:
        raise Fail("bad_proto", "this shim speaks protocol %d to %d" % (MIN_PROTO, PROTO))
    found = {}
    for name in req.get("want") if isinstance(req.get("want"), list) else []:
        if isinstance(name, str) and TOOL_NAME.match(name):
            found[name] = probe_tool(name)
    host = dict(os.environ)
    settings = read_json(os.path.join(home(), ".claude", "settings.json")) or {}
    declared = settings.get("env") if isinstance(settings.get("env"), dict) else {}
    fs_kind, network = filesystem_of(home())
    try:
        usage = shutil.disk_usage(home())
        disk = {"free": usage.free, "total": usage.total}
    except OSError:
        disk = {}
    return {
        "shim": own_id(), "proto": PROTO, "min_proto": MIN_PROTO, "hub": req.get("hub"),
        "os": platform.system(), "release": platform.release(), "machine": platform.machine(),
        "python": list(sys.version_info[:3]), "python_exe": sys.executable, "boot": boot_id(),
        "user": os.path.basename(home()), "home": home(), "home_fs": fs_kind, "home_network": network,
        "tools": found,
        "proxy": {name: host[name] for name in ENV_REPORT if name in host},
        "settings_env": {name: str(declared[name]) for name in ENV_REPORT if name in declared},
        "settings_env_dropped": len([name for name in declared if name not in ENV_REPORT]),
        "time": now(), "disk": disk, "tasks_root": os.path.join(covey_root(), "tasks"),
    }


def run_quiet(argv, timeout, errors=subprocess.STDOUT):
    """(exit code or None, stdout text); stderr goes to ``errors`` (merged into stdout unless told otherwise)."""
    try:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=errors, timeout=timeout)
        return done.returncode, done.stdout.decode("utf-8", "replace").strip()
    except (OSError, subprocess.SubprocessError):
        return None, ""


def probe_tool(name):
    path = shutil.which(name)
    if not path:
        for entry in EXTRA_BIN:
            candidate = os.path.join(entry if os.path.isabs(entry) else os.path.join(home(), entry), name)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                path = candidate
                break
    if not path:
        return {"found": False}
    code, text = run_quiet([path, "--version"], 20, subprocess.DEVNULL)
    info = {"found": True, "path": path, "version": text.splitlines()[0][:200] if text else ""}
    if code not in (None, 0):
        info["version_error"] = code
    login = {"claude": [path, "auth", "status"], "codex": [path, "login", "status"]}.get(name)
    if login:
        code, text = run_quiet(login, 20)
        info["login"] = "unknown" if code is None else "ok" if code == 0 and not LOGGED_OUT.search(text) else "no"
        info["login_detail"] = text[:300]
    elif name in SIGN_IN_FILES:
        found = os.path.isfile(os.path.join(home(), *SIGN_IN_FILES[name]))
        info["login"] = "ok" if found else "no"
        info["login_detail"] = "" if found else "~/%s not found" % "/".join(SIGN_IN_FILES[name])
    return info


def spawn_spec(req):
    argv = req.get("argv")
    if (not isinstance(argv, list) or not argv or len(argv) > 4096
            or not all(isinstance(item, str) and item and "\0" not in item for item in argv)):
        raise Fail("bad_request", "argv must be a non-empty list of strings")
    env = req.get("env") or {}
    if (not isinstance(env, dict) or not all(isinstance(k, str) and k and "=" not in k and "\0" not in k and
                                             (v is None or isinstance(v, str) and "\0" not in v) for k, v in env.items())):
        raise Fail("bad_request", "env must map names to strings (null removes one)")
    cwd = req.get("cwd")
    if cwd is not None and not isinstance(cwd, str):
        raise Fail("bad_request", "cwd must be a string")
    shell = req.get("shell") or ""
    if shell not in ("", "login"):
        raise Fail("bad_request", "shell is '' or 'login'")
    checkpoint = req.get("checkpoint")
    if checkpoint is not None and not (isinstance(checkpoint, dict) and isinstance(checkpoint.get("path"), str)):
        raise Fail("bad_request", "checkpoint is {path, poll}")
    files = req.get("files") or []
    if not isinstance(files, list):
        raise Fail("bad_request", "files must be a list")
    total = 0
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not (
                isinstance(item.get("text"), str) or isinstance(item.get("b64"), str)):
            raise Fail("bad_request", "a file is {path, text or b64, mode}")
        total += len(item.get("text") or item.get("b64"))
    if total > SEEDS_MAX * 4 // 3 + 16:
        raise Fail("too_large", "the seed files are larger than %d bytes" % SEEDS_MAX)
    deadline = number(req.get("deadline"), 0, 0, 4e9)
    return {"argv": argv, "cwd": cwd, "env": env, "shell": shell, "deadline": deadline, "files": files,
            "checkpoint": checkpoint}


def seed_files(tdir, files):
    for item in files:
        path = os.path.expanduser(item["path"])
        if not os.path.isabs(path):
            path = os.path.join(tdir, path)
        data = item["text"].encode("utf-8") if isinstance(item.get("text"), str) else base64.b64decode(item["b64"])
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        write_file(path, data, item.get("mode") if isinstance(item.get("mode"), int) else 0o600)


def spawn_answer(tdir, existed):
    """Wait (up to 10 s) for the supervisor to have started the Agent, then describe the task."""
    deadline = time.time() + 10
    while time.time() < deadline:
        state = read_json(os.path.join(tdir, "state.json")) or {}
        if state.get("child") or read_exit(tdir) is not None:
            break
        time.sleep(0.05)
    state = read_json(os.path.join(tdir, "state.json")) or {}
    ended = read_exit(tdir)
    if ended and ended.get("reason") == "spawn_failed":
        raise Fail("spawn_failed", ended.get("error", "the Agent did not start"))
    if not state.get("pgid"):
        raise Fail("timeout", "the supervisor did not start within 10 seconds")
    return {"pgid": state.get("pgid"), "pid": state.get("pid"), "child": state.get("child"), "existed": existed,
            "task_dir": tdir, "spec_sha": state.get("spec_sha")}


def cmd_spawn(req, wire):
    hub, key = name_of(req.get("hub"), "hub"), name_of(req.get("task"), "task")
    spec = spawn_spec(req)
    sha = hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(",", ":")).encode("ascii")).hexdigest()
    tdir = task_dir(hub, key, must_exist=False)
    cwd = os.path.expanduser(spec["cwd"]) if spec["cwd"] else None
    existing = read_json(os.path.join(tdir, "spec.json"))
    if existing is None:
        if cwd is not None and not os.path.isdir(cwd):
            raise Fail("bad_cwd", "no directory %s on this device" % cwd)
        for sub in ("inbox", "ctl", "incoming"):
            os.makedirs(os.path.join(tdir, sub), mode=0o700, exist_ok=True)
        stored = dict(spec, files=[{"path": item["path"]} for item in spec["files"]])
        record = {"spec_sha": sha, "spec": stored, "hub": hub, "task": key, "created": now()}
        if create_exclusive(os.path.join(tdir, "spec.json"), json.dumps(record).encode("ascii")):
            try:
                seed_files(tdir, spec["files"])
            except (OSError, ValueError) as exc:
                write_json(os.path.join(tdir, "exit.json"), {"code": 127, "reason": "spawn_failed", "ended": now(),
                                                             "error": "seed file: %s" % exc})
                raise Fail("io", "seed file: %s" % exc)
            start_supervisor(tdir, spec, sha, hub, key)
            return spawn_answer(tdir, False)
        existing = read_json(os.path.join(tdir, "spec.json"))
    if existing is None or existing.get("spec_sha") != sha:
        raise Fail("conflict", "task %s exists with another spec" % key)
    return spawn_answer(tdir, True)


def cmd_send(req, wire):
    tdir = request_task(req)
    return {"r": do_send(tdir, req.get("seq"), req.get("d"))}


def task_status(tdir):
    state = read_json(os.path.join(tdir, "state.json"))
    spec = read_json(os.path.join(tdir, "spec.json")) or {}
    ended = read_exit(tdir)
    alive = supervisor_alive(state)
    if ended is not None:
        label = "exited"
    elif alive:
        label = "running"
    elif state is None and time.time() - (spec.get("created") or 0) < 10:
        label = "starting"
    else:
        label = "lost"
    result = {"state": label, "seq": read_int(os.path.join(tdir, "seq")), "journal": journal_size(tdir),
              "supervisor": {"alive": alive}, "exit": ended, "task_dir": tdir}
    try:
        names = [int(n[:-5]) for n in os.listdir(os.path.join(tdir, "inbox")) if n.endswith(".json") and n[:-5].isdigit()]
    except OSError:
        names = []
    result["inbox_max"] = max(names) if names else 0
    if state:
        result["supervisor"].update(pid=state.get("pid"), start=state.get("start"), boot=state.get("boot"))
        result.update(pgid=state.get("pgid"), child=state.get("child"), proto=state.get("proto"),
                      shim=state.get("shim"), spec_sha=state.get("spec_sha"))
        result["rebooted"] = state.get("boot") != boot_id()
        result["group_alive"] = bool(group_members(state["pgid"])) if isinstance(state.get("pgid"), int) else False
    return result


def cmd_status(req, wire):
    return task_status(request_task(req))


def wait_for(predicate, timeout, step=0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return bool(predicate())


def owns_group(state, tdir, members):
    """Whether the live members of the recorded group are this task's, so a recycled group id is never killed:
    the supervisor or the Agent by pid and start time, or (Linux) a member whose environment names the task."""
    if state.get("boot") != boot_id():
        return False
    if state.get("pid") in members and process_alive(state["pid"], state.get("start")):
        return True
    if state.get("child") in members and state.get("child_start") and process_alive(state["child"], state["child_start"]):
        return True
    marker = b"COVEY_TASK_DIR=" + tdir.encode("utf-8", "surrogateescape")
    for pid in members:
        try:
            with open("/proc/%d/environ" % pid, "rb") as handle:
                if marker in handle.read().split(b"\0"):
                    return True
        except OSError:
            continue
    return False


def reap_task(tdir, grace):
    state = read_json(os.path.join(tdir, "state.json"))
    pgid = (state or {}).get("pgid")
    if not isinstance(pgid, int) or pgid <= 1:
        raise Fail("no_pgid", "the task has no process group on record")
    before = group_members(pgid)
    info = {"pgid": pgid, "before": before}
    if before and not owns_group(state, tdir, before):
        info.update(after=before, refused="identity")  # Not killed: it may be another job's group.
        return info
    if before and supervisor_alive(state) and read_exit(tdir) is None:
        request_control(tdir, "stop", grace=grace)
        wait_for(lambda: read_exit(tdir) is not None, grace + 6)
        wait_for(lambda: not [p for p in group_members(pgid) if p != state.get("pid")], 2, 0.2)
        info["via"] = "supervisor"
    members = group_members(pgid)
    if members:
        try:
            os.killpg(pgid, signal.SIGTERM)
            info["term"] = True
        except OSError:
            pass
        if not wait_for(lambda: not group_members(pgid), grace, 0.2):
            try:
                os.killpg(pgid, signal.SIGKILL)
                info["kill"] = True
            except OSError:
                pass
            wait_for(lambda: not group_members(pgid), 3, 0.2)
    info["after"] = group_members(pgid)
    if read_exit(tdir) is None and not supervisor_alive(state):
        result = {"code": None, "sig": None, "reason": "reaped", "reaped": True}
        append_record(tdir, dict(result, k="exit", t=now()))
        write_json(os.path.join(tdir, "exit.json"), dict(result, ended=now()))
        info["wrote_exit"] = True
    info["exit"] = read_exit(tdir)
    return info


def cmd_reap(req, wire):
    grace = number(req.get("grace"), 5.0, 0.0, 600.0)
    if req.get("task") is not None:
        return reap_task(request_task(req), grace)
    if req.get("mark") is not None:
        return reap_mark(name_of(req.get("hub"), "hub"), name_of(req.get("mark"), "mark"), grace)
    pgid = req.get("pgid")
    if isinstance(pgid, bool) or not isinstance(pgid, int) or pgid <= 1:
        raise Fail("bad_request", "reap needs task (with hub), mark (with hub) or pgid")
    tasks = os.path.join(covey_root(), "tasks")
    hubs = [name_of(req["hub"], "hub")] if req.get("hub") else sorted(os.listdir(tasks)) if os.path.isdir(tasks) else []
    for hub in hubs:
        for key in sorted(os.listdir(os.path.join(tasks, hub))) if os.path.isdir(os.path.join(tasks, hub)) else []:
            state = read_json(os.path.join(tasks, hub, key, "state.json"))
            if state and state.get("pgid") == pgid:
                return reap_task(os.path.join(tasks, hub, key), grace)
    raise Fail("not_found", "no task of this device has process group %d" % pgid)


def cmd_stop(req, wire):
    tdir = request_task(req)
    grace = number(req.get("grace"), 5.0, 0.0, 600.0)
    state = read_json(os.path.join(tdir, "state.json"))
    if read_exit(tdir) is None and state and supervisor_alive(state):
        request_control(tdir, "stop", grace=grace)
        if req.get("wait", True):
            wait_for(lambda: read_exit(tdir) is not None, grace + 8)
    elif read_exit(tdir) is None and state:
        reaped = reap_task(tdir, grace)  # No supervisor to ask: stop the group directly.
        return dict(reaped, via="reap", exited=reaped.get("exit") is not None)
    return {"exited": read_exit(tdir) is not None, "exit": read_exit(tdir)}


def read_record(handle, offset):
    """(line bytes, end offset) of the complete journal line at ``offset``, or None when it is not complete yet."""
    handle.seek(offset)
    data = b""
    while True:
        block = handle.read(1 << 20)
        if not block:
            return None
        index = block.find(b"\n")
        if index >= 0:
            data += block[:index]
            return data, offset + len(data) + 1
        data += block


def cmd_attach(req, wire):
    tdir = request_task(req)
    offset = req.get("from", 0)
    size = journal_size(tdir)
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0 or offset > size:
        raise Fail("bad_offset", "from must be 0 to %d (the journal's size)" % size)
    wire.send({"ok": True, "id": req.get("id"), "k": "att", "from": offset, "size": size})
    journal = os.path.join(tdir, "journal")
    last_sent, last_check, saw_exit = time.time(), 0.0, False
    open(journal, "ab").close()
    with open(journal, "rb") as handle:
        while True:
            sent = 0
            while sent < 2000:
                found = read_record(handle, offset)
                if found is None:
                    break
                line, end = found
                try:
                    record = json.loads(line.decode("utf-8"))
                except ValueError:
                    record = None
                if isinstance(record, dict):
                    record["o"], record["e"] = offset, end
                    wire.send(record)
                    saw_exit = saw_exit or record.get("k") == "exit"
                    sent += 1
                offset = end
                if saw_exit:
                    return None
            if sent:
                last_sent = time.time()
            else:
                if time.time() - last_check >= 1.0:
                    last_check = time.time()
                    tail = journal_size(tdir)
                    ended = read_exit(tdir)
                    if tail == offset and ended is not None:  # An exit.json without a journal record (a crash).
                        wire.send(dict(ended, k="exit", o=offset, e=offset, synth=True))
                        return None
                    state = read_json(os.path.join(tdir, "state.json"))
                    if tail == offset and ended is None and state is not None and not supervisor_alive(state):
                        wire.send({"k": "gone", "o": offset, "e": offset, "why": "rebooted" if state.get("boot") != boot_id()
                                   else "supervisor_lost", "group_alive": bool(group_members(state.get("pgid") or 0))})
                        return None
                if time.time() - last_sent >= HEARTBEAT:
                    wire.send({"k": "hb"})
                    last_sent = time.time()
            wire.fill(0 if sent else ATTACH_TICK)
            while True:
                line = wire.line(0)
                if line is None:
                    break
                if attach_op(tdir, wire, line):
                    return None
            if wire.eof:
                return None  # The hub's end is gone (ssh closed).


def attach_op(tdir, wire, line):
    """Handle one hub line of an attach; True ends the attach (detach)."""
    try:
        frame = json.loads(line.decode("utf-8"))
    except ValueError:
        return False
    if not isinstance(frame, dict):
        return False
    op = frame.get("op")
    if op == "detach":
        return True
    reply = {"k": "r", "op": op}
    try:
        if op == "send":
            reply.update(seq=frame.get("seq"), r=do_send(tdir, frame.get("seq"), frame.get("d")))
        elif op == "sig":
            name = frame.get("sig")
            if name not in ("TERM", "KILL", "EOF"):
                raise Fail("bad_request", "sig is TERM, KILL or EOF")
            if not supervisor_alive(read_json(os.path.join(tdir, "state.json"))):
                raise Fail("gone", "no supervisor to signal: reap the task")
            request_control(tdir, {"TERM": "term", "KILL": "kill", "EOF": "eof"}[name])
            reply["sig"] = name
        else:
            return False
        reply["ok"] = True
    except Fail as exc:
        reply.update(ok=False, err=exc.code, msg=exc.msg)
    wire.send(reply)
    return False


def cmd_ls(req, wire):
    path = os.path.abspath(expand(req.get("path", "~")))
    limit = int(number(req.get("limit"), 5000, 1, 50000))
    entries = []
    try:
        with os.scandir(path) as scan:
            for entry in scan:
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                kind = "link" if statmod.S_ISLNK(st.st_mode) else "dir" if statmod.S_ISDIR(st.st_mode) else \
                    "file" if statmod.S_ISREG(st.st_mode) else "other"
                item = {"name": entry.name, "kind": kind, "size": st.st_size, "mtime": st.st_mtime}
                if kind == "link":
                    item["target_dir"] = entry.is_dir()
                entries.append(item)
    except FileNotFoundError:
        raise Fail("not_found", "no such directory: %s" % path)
    except NotADirectoryError:
        raise Fail("not_dir", "not a directory: %s" % path)
    except PermissionError:
        raise Fail("permission", "cannot read %s" % path)
    entries.sort(key=lambda item: item["name"])
    return {"path": path, "home": home(), "entries": entries[:limit], "truncated": len(entries) > limit,
            "count": len(entries)}


def within(path, root):
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def cmd_stat(req, wire):
    paths = req.get("paths")
    if not isinstance(paths, list) or len(paths) > 5000:
        raise Fail("bad_request", "paths must be a list of at most 5000")
    lex_root = os.path.abspath(expand(req["root"])) if req.get("root") else None
    root = os.path.realpath(lex_root) if lex_root else None
    base = os.path.abspath(expand(req["base"])) if req.get("base") else lex_root or home()
    names = [n for n in req.get("deny_names") or [] if isinstance(n, str)]
    dirs = [os.path.realpath(expand(d)) for d in req.get("deny_dirs") or [] if isinstance(d, str)]

    def judged(part):
        return any(fnmatch.fnmatchcase(part, pattern) for pattern in names)

    results = []
    for raw in paths:
        full = os.path.normpath(os.path.join(base, expand(raw)))
        real = os.path.realpath(full)
        item = {"path": full, "realpath": real, "exists": False, "escapes": False, "denied": False}
        try:
            st = os.stat(full)
            item.update(exists=True, size=st.st_size, mtime=st.st_mtime, is_dir=statmod.S_ISDIR(st.st_mode),
                        is_file=statmod.S_ISREG(st.st_mode))
        except FileNotFoundError:
            pass
        except OSError as exc:
            item["error"] = exc.__class__.__name__
        item["is_link"] = os.path.islink(full)
        if root is not None:
            item["escapes"] = not within(real, root)
        # Judged below the root only (the root's own path is the deployer's); a path out of the root: all of it.
        lexical = (os.path.relpath(full, lex_root).split(os.sep) if root is not None and within(full, lex_root)
                   else os.path.relpath(full, root).split(os.sep) if root is not None and within(full, root)
                   else full.split(os.sep))
        actual = os.path.relpath(real, root).split(os.sep) if root is not None and not item["escapes"] else real.split(os.sep)
        item["denied"] = any(judged(part) for part in lexical + actual if part not in ("", ".")) or any(
            within(real, d) or within(full, d) for d in dirs)
        results.append(item)
    return {"results": results, "root": root}


class Raw:
    """An answer with a body: the header line, then ``len`` bytes and a newline."""

    def __init__(self, header, body):
        self.header, self.body = header, body


def cmd_get(req, wire):
    path = expand(req.get("path"))
    offset = req.get("offset", 0)
    limit = int(number(req.get("limit"), DATA_MAX, 0, DATA_MAX))
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise Fail("bad_request", "offset must be a non-negative integer")
    try:
        with open(path, "rb") as handle:
            st = os.fstat(handle.fileno())
            if not statmod.S_ISREG(st.st_mode):
                raise Fail("not_file", "not a regular file: %s" % path)
            handle.seek(offset)
            data = handle.read(limit)
    except FileNotFoundError:
        raise Fail("not_found", "no such file: %s" % path)
    except IsADirectoryError:
        raise Fail("not_file", "not a regular file: %s" % path)
    except PermissionError:
        raise Fail("permission", "cannot read %s" % path)
    header = {"ok": True, "id": req.get("id"), "len": len(data), "size": st.st_size, "offset": offset,
              "eof": offset + len(data) >= st.st_size, "mtime": st.st_mtime, "sha256": hashlib.sha256(data).hexdigest()}
    return Raw(header, data)


def cmd_put(req, wire):
    path = os.path.abspath(expand(req.get("path")))
    count, offset = req.get("len"), req.get("offset", 0)
    if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in (count, offset)):
        raise Fail("bad_request", "len and offset must be non-negative integers")
    if count > DATA_MAX:
        raise Fail("too_large", "a put segment is at most %d bytes" % DATA_MAX)
    total = req.get("total", offset + count if offset == 0 else None)
    if not isinstance(total, int) or isinstance(total, bool) or total < offset + count:
        raise Fail("bad_request", "total must cover offset+len (it is required past the first segment)")
    whole = req.get("total_sha256", req.get("sha256") if offset == 0 and total == count else None)
    mode = req.get("mode") if isinstance(req.get("mode"), int) else 0o600
    data = wire.exact(count)
    if wire.exact(1) != b"\n":
        raise Fail("bad_request", "the body must end with a newline")
    if hashlib.sha256(data).hexdigest() != req.get("sha256"):
        raise Fail("sha_mismatch", "the segment's sha256 differs from the header's")
    if offset == 0 and whole and os.path.isfile(path) and os.path.getsize(path) == total and sha256_of(path) == whole:
        return {"same": True, "written": 0}
    part = path + ".covey-part"
    if offset == 0:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    else:
        try:
            have = os.path.getsize(part)
        except OSError:
            have = -1
        if have != offset:
            raise Fail("bad_offset", "the partial file holds %d bytes, not %d" % (have, offset))
        fd = os.open(part, os.O_WRONLY | os.O_APPEND)
    with os.fdopen(fd, "ab") as handle:
        handle.write(data)
    done = offset + count >= total
    if done:
        if whole and sha256_of(part) != whole:
            os.unlink(part)
            raise Fail("sha_mismatch", "the assembled file's sha256 differs from total_sha256")
        os.chmod(part, mode)
        os.replace(part, path)
    return {"same": False, "written": count, "done": done}


def cmd_exec(req, wire):
    argv = req.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(item, str) and "\0" not in item for item in argv):
        raise Fail("bad_request", "argv must be a non-empty list of strings")
    timeout = number(req.get("timeout"), 30.0, 0.1, 3600.0)
    cap = int(number(req.get("max_out"), 1 << 20, 0, LINE_MAX))
    merged = dict(os.environ)
    for name, value in (req.get("env") or {}).items():
        if value is None:
            merged.pop(name, None)
        elif isinstance(name, str) and isinstance(value, str):
            merged[name] = value
    cwd = os.path.expanduser(req["cwd"]) if isinstance(req.get("cwd"), str) else None
    given = req.get("input")
    if given is not None and len(str(given).encode("utf-8")) > 60000:
        raise Fail("too_large", "input is at most 60000 bytes")
    marks = None
    if req.get("mark") is not None:
        marks = mark_dir(name_of(req.get("hub"), "hub"), name_of(req.get("mark"), "mark"))
        merged[MARK_ENV] = "%s/%s" % (req["hub"], req["mark"])
    try:
        child = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd,
                                 env=merged, bufsize=0, start_new_session=True)
    except OSError as exc:
        raise Fail("spawn_failed", "%s: %s" % (type(exc).__name__, exc))
    record = note_mark(marks, child.pid) if marks else None
    try:
        return run_exec(child, given, timeout, cap)
    finally:
        if record:
            try:
                os.unlink(record)
            except OSError:
                pass
            drop_empty(marks)  # The mark's folder too, once its last command ended (another of the turn may still run).


MARK_ENV = "COVEY_DEVICE_MARK"


def mark_dir(hub, mark):
    return os.path.join(covey_root(), "marks", hub, mark)


def note_mark(folder, pid):
    """Keep ``pid``'s group (it leads its own session) under ``folder``: the path written, None when it could not be. The
    folder made again when another command of the mark ended and took it away in between (``drop_empty``)."""
    info = proc_info(pid)
    path = os.path.join(folder, "%d.json" % pid)
    for _ in range(3):
        try:
            os.makedirs(folder, mode=0o700, exist_ok=True)
            write_json(path, {"pid": pid, "pgid": pid, "start": info[2] if info else None, "boot": boot_id(), "at": now()})
            return path
        except FileNotFoundError:
            continue
        except OSError:
            return None
    return None


def drop_empty(folder):
    """Remove ``folder`` (a mark's) and its hub's folder when nothing is left in them."""
    for each in (folder, os.path.dirname(folder)):
        try:
            os.rmdir(each)
        except OSError:
            return


def owns_marked(record, members, marker):
    """Whether the live ``members`` of a marked group are still the command's: same boot, and its leader by pid and start
    time, or (Linux) a member whose environment carries the mark, so a recycled group id is never killed."""
    if record.get("boot") != boot_id():
        return False
    pid = record.get("pid")
    if pid in members and record.get("start") and process_alive(pid, record["start"]):
        return True
    needle = (MARK_ENV + "=" + marker).encode("utf-8", "surrogateescape")
    for member in members:
        try:
            with open("/proc/%d/environ" % member, "rb") as handle:
                if needle in handle.read().split(b"\0"):
                    return True
        except OSError:
            continue
    return False


def reap_mark(hub, mark, grace):
    """Stop every group kept under ``mark`` that is still the command's (TERM, then KILL after ``grace``); a record whose
    group is gone is dropped. ``{"mark", "groups", "after", "refused"}``: ``after`` the pids still there."""
    folder, marker = mark_dir(hub, mark), "%s/%s" % (hub, mark)
    try:
        names = sorted(name for name in os.listdir(folder) if name.endswith(".json"))
    except OSError:
        names = []
    groups, after, refused = [], [], []
    for name in names:
        path = os.path.join(folder, name)
        record = read_json(path) or {}
        pgid = record.get("pgid")
        if isinstance(pgid, bool) or not isinstance(pgid, int) or pgid <= 1:
            continue
        members = group_members(pgid)
        if members and not owns_marked(record, members, marker):
            refused.append(pgid)  # Not killed: the id may be another program's by now.
            continue
        if members:
            groups.append(pgid)
            for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 3.0)):
                try:
                    os.killpg(pgid, sig)
                except OSError:
                    pass
                if wait_for(lambda: not group_members(pgid), wait, 0.2):
                    break
        left = group_members(pgid)
        after += left
        if not left:
            try:
                os.unlink(path)
            except OSError:
                pass
    try:
        os.rmdir(folder)
    except OSError:
        pass
    return {"mark": mark, "groups": groups, "after": after, "refused": refused}


def run_exec(child, given, timeout, cap):
    chunks = {child.stdout.fileno(): bytearray(), child.stderr.fileno(): bytearray()}
    names = {child.stdout.fileno(): "out", child.stderr.fileno(): "stderr"}
    truncated, timed_out = False, False
    if given is not None:
        try:
            child.stdin.write(str(given).encode("utf-8"))
        except OSError:
            pass
    try:
        child.stdin.close()
    except OSError:
        pass
    deadline = time.time() + timeout
    open_fds = set(chunks)
    while open_fds:
        left = deadline - time.time()
        if left <= 0:
            timed_out = True
            break
        for fd in select.select(sorted(open_fds), [], [], min(left, 0.5))[0]:
            data = os.read(fd, 1 << 16)
            if not data:
                open_fds.discard(fd)
                continue
            room = cap - sum(len(c) for c in chunks.values())
            truncated = truncated or len(data) > room
            chunks[fd] += data[:max(room, 0)]
    if timed_out:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(child.pid, sig)
            except OSError:
                pass
            try:
                child.wait(1.0 if sig == signal.SIGTERM else 5.0)
                break
            except subprocess.TimeoutExpired:
                continue
    else:
        try:
            child.wait(max(0.1, deadline - time.time()))
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(child.pid, signal.SIGKILL)
            child.wait(5.0)
    result = {"code": child.returncode, "timed_out": timed_out, "truncated": truncated}
    for fd, buf in chunks.items():
        data = bytes(buf)
        key = names[fd]
        try:
            result[key] = data.decode("utf-8")
        except UnicodeDecodeError:
            result[key + "_b64"] = base64.b64encode(data).decode("ascii")
    return result


def referenced_shims(tasks):
    """The directories of the shim files the tasks on disk record: they must stay while the tasks do."""
    found = set()
    for hub in os.listdir(tasks) if os.path.isdir(tasks) else []:
        for key in os.listdir(os.path.join(tasks, hub)):
            state = read_json(os.path.join(tasks, hub, key, "state.json"))
            if state and isinstance(state.get("shim"), str):
                found.add(os.path.realpath(os.path.dirname(state["shim"])))
    return found


def live_tasks(tasks):
    """``(key, task dir)`` of the tasks not over (a supervisor or a group member is still there): what they write or read
    under ``~/.covey/checkpoints`` and ``~/.covey/incoming`` must stay however old it is."""
    found = []
    for hub in sorted(os.listdir(tasks)) if os.path.isdir(tasks) else []:
        for key in sorted(os.listdir(os.path.join(tasks, hub))):
            tdir = os.path.join(tasks, hub, key)
            state = read_json(os.path.join(tdir, "state.json"))
            if state and (supervisor_alive(state) or group_members(state.get("pgid") or 0)):
                found.append((key, tdir))
    return found


def live_checkpoints(live):
    """The realpaths of the checkpoint files the ``live`` tasks (``live_tasks``) mirror."""
    found = set()
    for _, tdir in live:
        path = (((read_json(os.path.join(tdir, "spec.json")) or {}).get("spec") or {}).get("checkpoint") or {}).get("path")
        if isinstance(path, str) and path:
            found.add(os.path.realpath(os.path.expanduser(path)))
    return found


def newest_mtime(path):
    """The latest modification time of ``path`` and, for a directory, of everything under it."""
    newest = os.path.getmtime(path)
    for folder, names, files in os.walk(path):
        for name in names + files:
            try:
                newest = max(newest, os.path.getmtime(os.path.join(folder, name)))
            except OSError:
                continue
    return newest


def gc_mirrors(tasks, keep):
    """Remove what the hub puts on the device outside a task's own directory, because its path has to be known before the
    task exists: the checkpoint mirrors (``~/.covey/checkpoints/<hub>/<job>.json``, the supervisor writes them) and the
    attachments of a turn (``~/.covey/incoming/<hub>/<job>/``). One not changed for ``keep`` seconds and not belonging to a
    task still running (a checkpoint a live task names; an attachment directory named like a live task's key without its
    ``-<suffix>``) goes, and so do the hub directories left empty. ``(checkpoints removed, incoming removed)``, as
    ``<hub>/<name>``."""
    base = covey_root()
    removed = {"checkpoints": [], "incoming": []}
    live = None
    for kind in ("checkpoints", "incoming"):
        root = os.path.join(base, kind)
        for hub in sorted(os.listdir(root)) if os.path.isdir(root) else []:
            folder = os.path.join(root, hub)
            if os.path.islink(folder) or not os.path.isdir(folder):
                continue
            for name in sorted(os.listdir(folder)):
                path = os.path.join(folder, name)
                try:
                    if os.path.islink(path) or time.time() - newest_mtime(path) <= keep:
                        continue
                    live = live_tasks(tasks) if live is None else live
                    if kind == "checkpoints":
                        if not os.path.isfile(path) or os.path.realpath(path) in live_checkpoints(live):
                            continue
                        os.unlink(path)
                    else:
                        if not os.path.isdir(path) or any(key.startswith(name + "-") or key == name for key, _ in live):
                            continue
                        shutil.rmtree(path, ignore_errors=True)
                    removed[kind].append("%s/%s" % (hub, name))
                except OSError:
                    continue
            try:
                os.rmdir(folder)  # Only when it is empty.
            except OSError:
                pass
    return removed["checkpoints"], removed["incoming"]


MARK_STALE = 3600.0  # Seconds before gc drops a mark's record whose group is gone (one just written is left alone).


def gc_marks(min_age=MARK_STALE):
    """Tidy ``~/.covey/marks/<hub>/<mark>/``: a record whose group is gone (or of an earlier boot) and older than
    ``min_age`` is dropped, as ``reap`` would drop it, and a mark's folder left empty goes, and so does a hub's. A record
    whose group still runs stays for ``reap``. The marks removed, as ``<hub>/<mark>``."""
    root = os.path.join(covey_root(), "marks")
    removed = []
    for hub in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        folder = os.path.join(root, hub)
        if os.path.islink(folder) or not os.path.isdir(folder):
            continue
        for mark in sorted(os.listdir(folder)):
            path = os.path.join(folder, mark)
            if os.path.islink(path) or not os.path.isdir(path):
                continue
            try:
                names = os.listdir(path)
            except OSError:
                continue
            for name in names:
                entry = os.path.join(path, name)
                try:
                    if os.path.islink(entry) or not name.endswith(".json") or time.time() - os.path.getmtime(entry) <= min_age:
                        continue
                except OSError:
                    continue
                record = read_json(entry) or {}
                pgid = record.get("pgid")
                running = (record.get("boot") == boot_id() and isinstance(pgid, int) and not isinstance(pgid, bool)
                           and pgid > 1 and group_members(pgid))
                if not running:
                    try:
                        os.unlink(entry)
                    except OSError:
                        pass
            try:
                os.rmdir(path)  # Only when it is empty.
                removed.append("%s/%s" % (hub, mark))
            except OSError:
                pass
        try:
            os.rmdir(folder)
        except OSError:
            pass
    return removed


def cmd_gc(req, wire):
    keep = number(req.get("keep_days"), 7.0, 0.0, 3650.0) * 86400
    min_age = number(req.get("shim_min_age"), 3600.0, 0.0, 86400.0 * 365)
    tasks = os.path.join(covey_root(), "tasks")
    removed, shims_removed, shims_kept = [], [], []
    for hub in sorted(os.listdir(tasks)) if os.path.isdir(tasks) else []:
        for key in sorted(os.listdir(os.path.join(tasks, hub))):
            tdir = os.path.join(tasks, hub, key)
            state = read_json(os.path.join(tdir, "state.json"))
            ended = os.path.join(tdir, "exit.json")
            marker = ended if os.path.exists(ended) else os.path.join(tdir, "spec.json")
            try:
                age = time.time() - os.path.getmtime(marker)
            except OSError:
                continue
            idle = not state or (not supervisor_alive(state) and not group_members(state.get("pgid") or 0))
            if age > keep and idle:
                shutil.rmtree(tdir, ignore_errors=True)
                removed.append("%s/%s" % (hub, key))
    shims = os.path.join(covey_root(), "shim")
    used = referenced_shims(tasks) | {os.path.realpath(os.path.dirname(own_path()))}
    for entry in sorted(os.listdir(shims)) if os.path.isdir(shims) else []:
        path = os.path.realpath(os.path.join(shims, entry))
        if path in used or time.time() - os.path.getmtime(path) < min_age:
            shims_kept.append(entry)
        else:
            shutil.rmtree(path, ignore_errors=True)
            shims_removed.append(entry)
    checkpoints, incoming = gc_mirrors(tasks, keep)
    return {"removed_tasks": removed, "removed_shims": shims_removed, "kept_shims": shims_kept,
            "removed_checkpoints": checkpoints, "removed_incoming": incoming, "removed_marks": gc_marks()}


COMMANDS = {"hello": cmd_hello, "spawn": cmd_spawn, "attach": cmd_attach, "send": cmd_send, "status": cmd_status,
            "stop": cmd_stop, "reap": cmd_reap, "ls": cmd_ls, "stat": cmd_stat, "get": cmd_get, "put": cmd_put,
            "exec": cmd_exec, "gc": cmd_gc}


def main(argv):
    wire = Wire()
    request_id = None
    try:
        wire.send({"k": "hi", "proto": PROTO, "shim": own_id()})
        command = argv[1] if len(argv) > 1 else ""
        line = wire.line(timeout=30)
        if line is None and wire.buf:
            line = bytes(wire.buf)
        try:
            req = json.loads(line.decode("utf-8")) if line else {}
        except ValueError:
            raise Fail("bad_request", "the request is not JSON")
        if not isinstance(req, dict):
            raise Fail("bad_request", "the request must be a JSON object")
        request_id = req.get("id")
        if req.get("cmd") not in (None, command):
            raise Fail("bad_request", "cmd %r does not match the command line %r" % (req.get("cmd"), command))
        if command not in COMMANDS:
            raise Fail("bad_request", "unknown command %r" % command)
        answer = COMMANDS[command](req, wire)
        if isinstance(answer, Raw):
            wire.write(encode(answer.header).rstrip(b"\n") + b"\n" + answer.body + b"\n")
        elif answer is not None:
            wire.send(dict(answer, ok=True, id=request_id))
    except Fail as exc:
        try:
            wire.send({"ok": False, "id": request_id, "err": exc.code, "msg": exc.msg})
        except Gone:
            pass
    except Gone:
        pass
    except Exception as exc:
        sys.stderr.write(traceback.format_exc())
        try:
            wire.send({"ok": False, "id": request_id, "err": "internal", "msg": "%s: %s" % (type(exc).__name__, exc)})
        except Gone:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
