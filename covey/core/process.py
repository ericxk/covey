"""Which processes belong to whom: identities that survive PID reuse, owned process groups,
and the per-user instance lock; a command's stdout read line by line (stream_subprocess_lines).
Standard library only; the Manager, the Bridge, task runners and the event-bus watchdog all use it."""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable

from covey.core import procfs


def log_to(path):
    """Append this process's stdout and stderr to ``path`` (0600), unless they already go to that file
    (``ensure_manager`` and the boot watchdog pass it as the Manager's output), so a Manager started by an
    older watchdog, whose code PID 1 loaded at container start, logs as well; the Bridge takes its own log
    the same way. A file that cannot be opened leaves the output where it was."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        opened = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    except OSError:
        return
    fd = fcntl.fcntl(opened, fcntl.F_DUPFD_CLOEXEC, 3)  # Above 2: an fd 1 or 2 that was closed got it.
    if opened != fd and opened > 2:
        os.close(opened)
    try:
        log = os.fstat(fd)
        for stream, number in ((sys.stdout, 1), (sys.stderr, 2)):
            with contextlib.suppress(OSError):
                if os.path.samestat(os.fstat(number), log):
                    continue
            if stream:
                stream.flush()
            os.dup2(fd, number)
    finally:
        os.close(fd)


def service_key(state: Path) -> str:
    return hashlib.sha256(str(state.expanduser().resolve()).encode()).hexdigest()[:24]


def socket_address(name: str) -> str:
    """The abstract Unix socket named ``name``: ``instance_lock`` binds it, ``query`` asks it."""
    return "\0lark-bridge-" + str(os.getuid()) + "-" + name


def instance_lock(name: str):
    sock = socket.socket(socket.AF_UNIX)
    try:
        sock.bind(socket_address(name))
    except OSError:
        sock.close()
        return None
    return sock


def query(name, *, timeout=2):
    """The JSON object the health server at ``name`` answers with, or None. Here, not in app/, so that the
    Manager's checks never load the app stack."""
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(timeout)
        try:
            sock.connect(socket_address(name))
            chunks, size = [], 0
            while size < 65536:
                part = sock.recv(4096)
                if not part:
                    value = json.loads(b"".join(chunks))
                    return value if isinstance(value, dict) else None
                chunks.append(part)
                size += len(part)
        except (OSError, ValueError):
            pass
    return None


def process_identity(pid: int):
    """What tells this live process of this user apart from a later one given the same PID: its start time,
    as the string /proc shows (runner.json and wake.json compare it so), and the boot. None when the process
    has gone, is a zombie or is another user's."""
    stat = procfs.stat(pid)
    if stat is None or stat.state == "Z":
        return None
    try:
        if (Path("/proc") / str(pid)).stat().st_uid != os.getuid():
            return None
        return {"pid": pid, "start_ticks": str(stat.start_ticks), "boot_id": procfs.boot_id()}
    except (OSError, ValueError):
        return None


def is_owned(identity):
    return bool(identity and process_identity(int(identity["pid"])) == identity)


def paths_in_use(root: Path) -> set[Path]:
    """The entries directly under ``root`` that some process of this user has as its working
    directory or holds open, read from /proc."""
    root = Path(root).absolute()
    used = set()
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            if proc.stat().st_uid != os.getuid():
                continue
            links = [proc / "cwd", *(proc / "fd").iterdir()]
        except OSError:
            continue  # Exited, or not ours to read.
        for link in links:
            try:
                target = Path(os.readlink(link))
            except OSError:
                continue
            if target.is_relative_to(root) and target != root:
                used.add(root / target.relative_to(root).parts[0])
    return used


def device_of(ctx):
    """The alias of the device ``ctx`` works on, or ``""``: a context without the field (devices are off) or with
    something that is not a string there is local."""
    value = getattr(ctx, "device", "")
    return value if isinstance(value, str) else ""


def spawn_process(argv, *, ctx=None, cwd=None, env=None, stdin=None, stdout=None, stderr=None, text=False,
                  bufsize=-1, **device):
    """The process a backend starts for a turn: ``subprocess.Popen`` in a session of its own, or, when ``ctx`` names
    a device (``ctx.device``), a ``DeviceProcess`` (core/device_link.py: the same Popen face, the process runs on
    the device). ``device`` (``run_dir``, ``checkpoint``, ``files``) is for the device only."""
    if not device_of(ctx):
        return subprocess.Popen(argv, cwd=cwd, env=env, start_new_session=True, stdin=stdin, stdout=stdout,
                                stderr=stderr, text=text, bufsize=bufsize)
    from covey.core.device_link import DeviceProcess
    return DeviceProcess(argv, ctx=ctx, cwd=cwd, env=env, stdin=stdin, stdout=stdout, stderr=stderr, text=text,
                         bufsize=bufsize, **device)


def signal_group(process, signum):
    """Signal the private process group ``process`` leads (it was started with
    start_new_session). Nothing once it has exited or its group is gone; when the group
    cannot be signalled, the process itself. A process of a class that has its own ``signal_group`` (a device's,
    core/device_link.py) signals itself."""
    own = getattr(type(process), "signal_group", None)
    if own is not None:
        own(process, signum)
        return
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        return
    except OSError:
        with contextlib.suppress(OSError):
            process.send_signal(signum)


def stop_group(process, grace):
    """Stop the private process group ``process`` leads: SIGTERM, wait ``grace`` seconds,
    then SIGKILL and wait ``grace`` again. Whether the process has exited; one that outlives
    both waits is reported, never raised."""
    for signum in (signal.SIGTERM, signal.SIGKILL):
        if process.poll() is not None:
            return True
        signal_group(process, signum)
        try:
            process.wait(timeout=grace)
            return True
        except subprocess.TimeoutExpired:
            continue
    return False


def terminate_owned(identity, timeout=15):
    if not is_owned(identity):
        return
    pid = int(identity["pid"])
    # Every service is launched in its own session. Never signal an unrelated PG.
    if os.getpgid(pid) != pid:
        raise RuntimeError("service no longer owns its process group")
    os.killpg(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while is_owned(identity) and time.monotonic() < deadline:
        time.sleep(0.1)
    if is_owned(identity):
        os.killpg(pid, signal.SIGKILL)


def cleanup_group(identity):
    """Reap consumers in a departed service's original session, never by name."""
    if not identity or identity.get("boot_id") != procfs.boot_id():
        return
    pid = identity["pid"]
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit() or (stat := procfs.stat(proc.name)) is None:
            continue
        try:
            if (proc.stat().st_uid == os.getuid() and stat.pgrp == pid and stat.session == pid
                    and stat.start_ticks >= int(identity["start_ticks"])):
                os.killpg(pid, signal.SIGKILL)
                return
        except (OSError, ValueError):
            continue


def descendant(pid, ancestor):
    for _ in range(16):
        if pid == ancestor:
            return True
        stat = procfs.stat(pid)
        if stat is None:
            return False
        pid = stat.ppid
        if pid <= 1:
            return False
    return False


# Stopping the Bridge's event consumer processes.
def stop_process(proc: subprocess.Popen[str] | None, label: str, log: Callable[[str], None]) -> None:
    if not proc or proc.poll() is not None:
        return
    log(f"stopping {label}")
    try:
        if proc.stdin:
            proc.stdin.close()
    except OSError:
        pass
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def stop_processes(processes: Iterable[tuple[str, subprocess.Popen[str] | None]], log: Callable[[str], None]) -> None:
    for label, proc in processes:
        stop_process(proc, label, log)


class JobCancelled(Exception):
    """Raised when a user cancels a running bridge job."""


def stream_subprocess_lines(
    argv: list[str],
    *,
    stdin_text: str | None,
    timeout: int,
    on_line: Callable[[str], None],
    env: dict[str, str] | None = None,
    cwd: str | None = None,
    cancel_event: threading.Event | None = None,
    process_observer: Callable | None = None,
) -> tuple[int, str]:
    """Run ``argv`` streaming stdout lines to ``on_line``; return (rc, stderr).

    Raises ``subprocess.TimeoutExpired`` when ``timeout`` (>0) elapses.
    """
    if cancel_event is not None and cancel_event.is_set():
        raise JobCancelled("任务已手动停止")
    stderr_parts = deque(maxlen=2000)  # Bound memory for multi-hour subprocesses.
    timed_out = threading.Event()
    cancelled = threading.Event()
    finished = threading.Event()
    proc = subprocess.Popen(
        argv,
        text=True,
        stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=cwd,
        start_new_session=True,
    )
    stderr_thread, timer, cancel_thread = _watch_process(proc, stderr_parts, timeout, timed_out, cancel_event,
                                                         cancelled, finished)
    try:
        if process_observer is not None:
            process_observer(proc)
        if stdin_text is not None:
            assert proc.stdin is not None
            proc.stdin.write(stdin_text)
            proc.stdin.close()
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if line:
                on_line(line)
        rc = proc.wait()
        stderr_thread.join(timeout=1)
        if cancelled.is_set():
            raise JobCancelled("任务已手动停止")
        if timed_out.is_set():
            raise subprocess.TimeoutExpired(argv, timeout)
        return rc, "".join(stderr_parts)
    finally:
        _reap_process(proc, finished, timer, cancel_thread, stderr_thread)


def _watch_process(proc, stderr_parts, timeout, timed_out, cancel_event, cancelled, finished):
    """Start the stderr reader, the timeout killer (``timeout`` > 0) and the cancel watcher
    (with ``cancel_event``); return (stderr_thread, timer, cancel_thread)."""
    def collect_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_parts.append(line)

    stderr_thread = threading.Thread(target=collect_stderr, daemon=True)
    stderr_thread.start()
    timer: threading.Timer | None = None
    if timeout > 0:
        def kill_for_timeout() -> None:
            if proc.poll() is None:
                timed_out.set()
                signal_group(proc, signal.SIGKILL)

        timer = threading.Timer(timeout, kill_for_timeout)
        timer.daemon = True
        timer.start()
    cancel_thread: threading.Thread | None = None
    if cancel_event is not None:
        def terminate_for_cancel() -> None:
            while not finished.wait(0.2):
                if not cancel_event.is_set():
                    continue
                cancelled.set()
                stop_group(proc, 5)
                return

        cancel_thread = threading.Thread(target=terminate_for_cancel, daemon=True)
        cancel_thread.start()
    return stderr_thread, timer, cancel_thread


def _reap_process(proc, finished, timer, cancel_thread, stderr_thread):
    """Stop the watchers, kill and reap the command group, and close our pipe ends."""
    finished.set()
    if timer is not None:
        timer.cancel()
    # A raising handler or observer must not leave the command group running.
    signal_group(proc, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=3)
    if cancel_thread is not None:
        cancel_thread.join(timeout=1)
    # Close our pipe ends; stderr only once its reader is done, never under it.
    stderr_thread.join(timeout=1)
    for stream in (proc.stdin, proc.stdout, None if stderr_thread.is_alive() else proc.stderr):
        if stream is not None:
            with contextlib.suppress(OSError):
                stream.close()
