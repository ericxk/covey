"""What a task's commands leave running once its Harness has closed, found by the marker its runner puts
in its environment. Standard library only.

The task runner sets ``COVEY_TASK_DIR`` to its run directory (app/runner.py). The Harness and every
command it starts inherit it: also the commands in sessions of their own (Claude Code's) or process groups
of their own (Codex's), which stopping the Harness's group does not reach, and those adopted by PID 1 once
their parent is gone. When a turn's backend has closed, ``stop_leftovers`` ends the search tools still
running with the marker: a walk of a large tree can grow without bound (an ``rg --files`` left behind
once reached 166.9 GiB and the container restarted). Any other process is reported and never
signalled: it may be work the user asked to keep running, a server or a training run.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import os
from pathlib import Path
import re
import select
import shlex
import signal
import threading
import time

from covey.core import procfs

TASK_DIR_ENV = "COVEY_TASK_DIR"
OLD_TASK_DIR_ENV = "LARK_BRIDGE_TASK_DIR"  # What a task runner of an older release sets (the new one sets both).
# Process names (comm) of read-only search and listing tools.
SEARCH_TOOLS = frozenset({"rg", "grep", "egrep", "fgrep", "zgrep", "find", "fd", "fdfind", "ag", "ack", "ugrep",
                          "tree", "du", "locate"})
# A find with one of these acts on what it finds: it is reported, not stopped.
FIND_ACTIONS = frozenset({"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls"})
# Reading a process's cmdline or environ takes its mmap lock, which a process short of memory may hold for long:
# each read waits this long, and after SLOW_READS such reads the rest are not tried.
READ_DEADLINE = 1.0
SLOW_READS = 3
KILL_WAIT = 1.0
PAGE = os.sysconf("SC_PAGE_SIZE")
SECRET_VALUE = re.compile(r"(?i)([\w.-]*(?:token|secret|passw(?:or)?d|api[_-]?key|access[_-]?key|auth|cookie|"
                          r"credential)[\w.-]*\s*[=:])(.+)")
SECRET_FLAG = re.compile(r"(?i)^--?[\w-]*(?:token|secret|password|passwd|api-?key|access-?key|cookie|credential)")
LONG_RUN = re.compile(r"[A-Za-z0-9_\-]{40,}")


@dataclass(frozen=True)
class Leftover:
    pid: int
    start_ticks: int
    name: str
    rss: int
    command: str
    search: bool
    outcome: str = ""  # A search tool signalled: "stopped", or "pending" while it has not exited (stuck in the kernel).


def _stat(pid: int):
    """(name, state, ppid, start ticks, rss bytes) of ``pid``, or None."""
    stat = procfs.stat(pid)
    return None if stat is None else (stat.name, stat.state, stat.ppid, stat.start_ticks, stat.rss_pages * PAGE)


def _read_bounded(path: str, slow: list) -> bytes | None:
    """The bytes of ``path``; None when it cannot be read, or not within READ_DEADLINE (the reader is left behind
    and counted in ``slow``), or SLOW_READS reads have been slow already."""
    if len(slow) >= SLOW_READS:
        return None
    box = []

    def read():
        try:
            box.append(Path(path).read_bytes())
        except OSError:
            pass
    thread = threading.Thread(target=read, daemon=True, name="task-processes-read")
    thread.start()
    thread.join(READ_DEADLINE)
    if thread.is_alive():
        slow.append(path)
    return box[0] if box else None


def is_search(name: str, argv: list[str] | None, search=SEARCH_TOOLS) -> bool:
    """A read-only search: a find only when its arguments are known and none of them acts on what it finds."""
    return name in search and (name != "find" or (argv is not None and not FIND_ACTIONS.intersection(argv)))


def short_command(argv: list[str], limit: int = 160) -> str:
    """``argv`` for a person to read: credentials masked, long runs of letters and digits shortened."""
    shown, previous = [], ""
    for arg in argv:
        if SECRET_FLAG.search(previous) and "=" not in previous:
            shown.append("<redacted>")
        else:
            shown.append(LONG_RUN.sub("<long>", SECRET_VALUE.sub(lambda m: m.group(1) + "<redacted>", arg)))
        previous = arg
    text = " ".join(shlex.quote(arg) for arg in shown)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def find_leftovers(run_dir: Path, *, root: int | None = None, search=SEARCH_TOOLS) -> list[Leftover]:
    """This user's live processes whose environment names ``run_dir``, started after ``root`` (this process by default)
    and not under it: those under it are its own work (card writes, uploads) or a Harness that has not closed."""
    root = os.getpid() if root is None else root
    own = _stat(root)
    if own is None:
        return []
    markers = {f"{name}={Path(run_dir).absolute()}".encode() for name in (TASK_DIR_ENV, OLD_TASK_DIR_ENV)}
    table = {}
    for entry in os.scandir("/proc"):
        if entry.name.isdigit() and (row := _stat(int(entry.name))) is not None:
            table[int(entry.name)] = row
    uid, found, slow = os.getuid(), [], []
    for pid, (name, state, _, start, rss) in sorted(table.items()):
        if state == "Z" or start <= own[3] or _under(pid, root, table):
            continue
        try:
            if os.stat(f"/proc/{pid}").st_uid != uid:
                continue
        except OSError:
            continue
        environ = _read_bounded(f"/proc/{pid}/environ", slow)
        if environ is None or not markers.intersection(environ.split(b"\0")):
            continue
        raw = _read_bounded(f"/proc/{pid}/cmdline", slow)
        argv = None if raw is None else [arg.decode("utf-8", "replace") for arg in raw.split(b"\0") if arg]
        found.append(Leftover(pid, start, name, rss, short_command(argv or [name]), is_search(name, argv, search)))
    return found


def _under(pid: int, root: int, table: dict) -> bool:
    for _ in range(64):
        row = table.get(pid)
        if row is None or row[2] <= 1:
            return False
        pid = row[2]
        if pid == root:
            return True
    return False


def _pidfd(item: Leftover) -> int | None:
    """A pidfd for ``item`` while its PID still names the same process; None once it is gone or recycled."""
    try:
        fd = os.pidfd_open(item.pid)
    except OSError:
        return None
    row = _stat(item.pid)
    if row is None or row[3] != item.start_ticks or row[1] == "Z":
        os.close(fd)
        return None
    return fd


def _wait_exit(fds: dict[int, Leftover], seconds: float) -> None:
    """Drop from ``fds`` each process that exits within ``seconds`` (a pidfd turns readable)."""
    deadline = time.monotonic() + seconds
    while fds:
        left = deadline - time.monotonic()
        if left <= 0:
            return
        ready, _, _ = select.select(list(fds), [], [], left)
        for fd in ready:
            fds.pop(fd)
            os.close(fd)


def stop_leftovers(run_dir: Path, *, root: int | None = None, search=SEARCH_TOOLS,
                   grace: float = 2.0) -> tuple[list[Leftover], list[Leftover]]:
    """End the leftover search tools of ``run_dir``: SIGTERM, then SIGKILL after ``grace`` seconds, through pidfds
    so a recycled PID is never signalled. (search tools signalled, with their outcome; the other leftovers still
    running, not signalled)."""
    leftovers = find_leftovers(run_dir, root=root, search=search)
    pending: dict[int, Leftover] = {}
    for item in leftovers:
        if item.search and (fd := _pidfd(item)) is not None:
            pending[fd] = item
    signalled = list(pending.values())
    try:
        for signum, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, KILL_WAIT)):
            for fd in list(pending):
                try:
                    signal.pidfd_send_signal(fd, signum)
                except ProcessLookupError:
                    pass
            _wait_exit(pending, wait)
    finally:
        for fd in pending:
            os.close(fd)
    if signalled:
        time.sleep(0.2)  # A shell whose search ended exits next; it is not reported as still running.
    stuck = set(pending.values())
    stopped = [replace(item, outcome="pending" if item in stuck else "stopped") for item in signalled]
    running = [item for item in leftovers if not item.search and (row := _stat(item.pid)) is not None
               and row[3] == item.start_ticks and row[1] != "Z"]
    return stopped, running
