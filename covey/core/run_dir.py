"""The run directory of a job handed to a task runner, ``<state>/runs/<job>/`` (app/detached.py).

Every release's Bridge and task runner read and write these names, so they are defined here once and
never change. The Bridge writes handoff.json (the job and its arguments), runner.json (the runner's
process identity; a runner writes it itself only when it does not name it) and inbox/ (clicks, supplements and answers it
forwards); the runner writes wake.json (where to signal an inbox write), acks/ (its answers to the
inbox), requests/ (continuations it asks for), done.json (its job has ended, uploads may remain) and
exit.json, and the Bridge answers its requests in replies/. Each message is one JSON file named by its
op id, ``<time_ns>-<hex8>``, read once, oldest first. A job waiting for its turn is kept in
``<state>/queued/<job>.json``.
"""
from __future__ import annotations

import errno
import fcntl
import os
import re
import secrets
import time
from pathlib import Path

from covey.core.file_lock import open_lock
from covey.core.jobs import job_path, running_job_records
from covey.core.statuses import RUNNING
from covey.core.process import process_identity
from covey.core.state import read_json, write_json

RUNS = "runs"
QUEUED = "queued"
HANDOFF = "handoff.json"
RUNNER = "runner.json"
WAKE = "wake.json"
DONE = "done.json"
EXIT = "exit.json"
LOG = "runner.log"
INBOX = "inbox"
ACKS = "acks"
REQUESTS = "requests"
REPLIES = "replies"
REPOS = "repos.json"  # A task's ``covey repo use`` requests (core/topic_repos.py); a Bridge that does not know it leaves it.
# The device channel (core/device_access.py): what the turn's start wrote for ``covey device`` (its limit and its chat's
# space), and one line per call of it. A Bridge or runner that does not know them leaves them.
DEVICE_TURN = "device-turn.json"
DEVICES = "devices.jsonl"
OP = re.compile(r"[0-9]+-[0-9a-f]+")
# A read of a run directory's file that fails (OSError, or JSON cut short: ValueError) while the directory is there is
# made again after each of these pauses (read_steady): on ByteFuse a read of runner.json that met another process
# replacing it failed for a moment, and the Bridge took the runner it had just handed a job for dead and started a
# second one for the same job (T740). A directory that is not there is not waited for.
READ_PAUSES = (0.01, 0.04)
# A task runner holds its run directory's lock (flock on runner.lock, through core/file_lock.py open_lock, so in
# COVEY_LOCK_ROOT when that is set) from taking its handoff to its exit, and the OS lets go of it when the runner ends
# however it ends. A second runner of the same job finds it held and runs nothing (hold_run_lock); the Bridge asks it
# last in runner_here, so a runner whose runner.json and wake.json cannot be read for a while is still alive to it
# (T814: a Bridge that took a live runner for gone started a second one, and both wrote the job's card). Runners of
# releases before it hold no lock: for them the two files alone answer, as before.
RUN_LOCK = "runner.lock"
# How long a runner tries for the lock before it leaves the job to the one that holds it. A Bridge's look at the lock
# (run_lock_held) holds it for a moment only; a runner that has the job holds it for the whole job.
RUN_LOCK_WAIT = 3.0
_BUSY = {errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES}
_held: dict = {}  # The run directories whose lock this process holds (absolute path -> descriptor).


def _stem(state_dir: Path, key: str) -> str:
    return job_path(Path(state_dir), key).stem


def run_dir(state_dir: Path, key: str) -> Path:
    """Absolute: a runner's argv names its run directory so (runner_alive)."""
    return (Path(state_dir) / RUNS / _stem(state_dir, key)).absolute()


def queued_path(state_dir: Path, key: str) -> Path:
    return Path(state_dir) / QUEUED / (_stem(state_dir, key) + ".json")


def read_steady(path: Path):
    """``read_json(path)``, made again after each of READ_PAUSES while it fails and ``path``'s directory is there; what the
    last one raised is raised."""
    path = Path(path)
    for pause in READ_PAUSES:
        try:
            return read_json(path)
        except (OSError, ValueError):
            if not path.parent.is_dir():
                raise
        time.sleep(pause)
    return read_json(path)


def runner_info(directory: Path) -> dict | None:
    """What runner.json says (read_steady); None when it cannot be read."""
    try:
        return read_steady(Path(directory) / RUNNER)
    except (OSError, ValueError):
        return None


def names_alive(directory: Path, info) -> bool:
    """``info`` (runner.json's, or wake.json's) names a live process of this user, by its pid, start time and boot,
    that has ``directory`` as one argument."""
    if not isinstance(info, dict) or not isinstance(info.get("pid"), int):
        return False
    identity = process_identity(info["pid"])
    if not identity or any(identity.get(k) != info.get(k) for k in ("start_ticks", "boot_id")):
        return False
    try:
        argv = Path(f"/proc/{info['pid']}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    return str(Path(directory).absolute()).encode() in argv


def runner_alive(directory: Path) -> bool:
    """The task runner recorded in this run directory's runner.json (read as runner_info does) is still the same live process."""
    return names_alive(directory, runner_info(directory))


def runner_here(directory: Path, *, patient: bool = True) -> bool:
    """A live task runner holds ``directory``: the one its runner.json names (runner_alive), else the one its wake.json does
    (a runner writes its identity there once it serves its inbox, app/runner_ipc.py accept_wake), read once, else one
    holds its run lock (run_lock_held). Asked where a wrong no starts a second runner of a job or settles a live one's
    (app/detached.py, app/runner_spawn.py start_runner, app/runner_ipc.py runner_for): two files and a lock, so a read of
    the files that fails still leaves the lock, which no file read decides. ``patient`` False: runner.json is read once
    too, for a directory where its absence is the rule (a prestarted runner's, before its handoff)."""
    info = runner_info(directory) if patient else _read_once(Path(directory) / RUNNER)
    return (names_alive(directory, info) or names_alive(directory, _read_once(Path(directory) / WAKE))
            or run_lock_held(directory))


def hold_run_lock(directory: Path, wait: float | None = None):
    """In a task runner that has its handoff, before it writes anything of the job: take ``directory``'s run lock and hold
    it until release_run_lock (or the process's end). True when held; False when another process held it all ``wait``
    seconds (RUN_LOCK_WAIT): a runner of this job runs it, and this one must run nothing and change nothing there; None
    when no lock can be taken there (its file cannot be made, or the filesystem refuses locks), and the runner goes on as
    runners did before the lock."""
    key = str(Path(directory).absolute())
    if key in _held:
        return True
    try:
        fd = open_lock(Path(key) / RUN_LOCK)
    except (OSError, ValueError):
        return None
    deadline = time.monotonic() + (RUN_LOCK_WAIT if wait is None else wait)
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in _BUSY and time.monotonic() < deadline:
                time.sleep(0.02)
                continue
            os.close(fd)
            return False if exc.errno in _BUSY else None
        _held[key] = fd
        return True


def release_run_lock(directory: Path) -> None:
    """The runner is done with ``directory``: its run lock goes (the process's end would let go of it as well)."""
    fd = _held.pop(str(Path(directory).absolute()), None)
    if fd is not None:
        os.close(fd)


def run_lock_held(directory: Path) -> bool:
    """A task runner holds ``directory``'s run lock (hold_run_lock): True when this process does, or another refuses it a
    shared lock now. False when no lock file is there (no runner of a release with the lock was started there) or it
    cannot be asked. This process never opens a lock it holds: on NFS a lock is the process's, and closing another
    descriptor of the file would let go of it."""
    key = str(Path(directory).absolute())
    if key in _held:
        return True
    try:
        fd = open_lock(Path(key) / RUN_LOCK, create=False)
    except (OSError, ValueError):
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except OSError as exc:
        return exc.errno in _BUSY
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _read_once(path: Path):
    try:
        return read_json(path)
    except (OSError, ValueError):
        return None


def argv_dirs(root: Path) -> set[str] | None:
    """The names ``n`` for which a process of this user has ``<root>/n`` as one whole argument: the only run
    directories runner_alive can say yes to, as it looks for the directory in the argv of the process its
    runner.json names (that process must be this user's). One read of /proc per process and none under ``root``:
    reading every runner.json took 3-8 s on NAS for 1700 run directories, several times at each start (T648).
    None when /proc cannot be listed (no procfs): then every directory is asked."""
    prefix = str(Path(root).absolute()).encode() + b"/"
    try:
        pids = [name for name in os.listdir("/proc") if name.isdigit()]
    except OSError:
        return None
    uid, names = os.getuid(), set()
    for pid in pids:
        try:
            if os.stat(f"/proc/{pid}").st_uid != uid:
                continue
            with open(f"/proc/{pid}/cmdline", "rb") as file:
                argv = file.read().split(b"\0")
        except OSError:
            continue
        for arg in argv:
            name = arg[len(prefix):] if arg.startswith(prefix) else b""
            if name and b"/" not in name:
                names.add(name.decode(errors="surrogateescape"))
    return names


def _asked(root: Path, alive) -> list[Path]:
    """The entries of ``root`` to ask ``alive`` about: with runner_alive or runner_here only those argv_dirs names (a
    runner that holds a run lock has its run directory in its argv too), by name; with another test (a caller's or a
    test's), every one, as before argv_dirs."""
    named = argv_dirs(root) if alive in (runner_alive, runner_here) else None
    if named is not None:
        return [root / name for name in sorted(named)]
    return sorted(root.iterdir()) if root.is_dir() else []


def live_dirs(state_dir: Path, alive=runner_alive) -> list[Path]:
    """The run directories whose task runner is alive (``alive``; the Bridge's start asks runner_here), absolute, by name:
    those a Bridge adopts at its start."""
    return [directory for directory in (entry.absolute() for entry in _asked(Path(state_dir) / RUNS, alive))
            if directory.is_dir() and alive(directory)]


def live_keys(state_dir: Path, alive=runner_alive) -> set:
    """Job keys whose task runner is alive, whichever bridge started it."""
    keys = set()
    for directory in _asked(Path(state_dir) / RUNS, alive):
        info = runner_info(directory)
        if info and info.get("key") and alive(directory):
            keys.add(info["key"])
    return keys



def restart_blockers(state_dir: Path, live: set) -> list:
    """Unfinished jobs a bridge restart would interrupt now: those without a live task
    runner (``live``, from live_keys), other than queued jobs saved for the next bridge.
    Empty means a deploy can go ahead."""
    blockers = []
    for record in running_job_records(Path(state_dir)):
        key = str(record.get("key") or "")
        if record.get("status") not in RUNNING or key in live:
            continue
        if record.get("status") == "queued" and key and queued_path(state_dir, key).exists():
            continue
        if record.get("status") == "cancel_requested":
            continue  # Stopped, and no runner has it: a restart interrupts nothing (one never started).
        blockers.append({"key": key, "job_id": record.get("job_id"), "status": record.get("status")})
    return blockers


def new_op() -> str:
    return f"{time.time_ns()}-{secrets.token_hex(4)}"


def op_time(op) -> int | None:
    """When the op id ``op`` was made (time_ns); None when it names no time."""
    written = str(op or "").split("-", 1)[0]
    return int(written) if written.isdigit() else None


def post(folder: Path, payload: dict) -> str:
    """Write ``payload`` to ``folder`` as a new message; its op id."""
    op = new_op()
    write_json(Path(folder) / (op + ".json"), {**payload, "id": op}, ensure_ascii=False)
    return op


def ack(directory: Path, op: str, ok: bool, error: str = "", folder: str = ACKS, **extra) -> None:
    """Answer message ``op``: in acks/ for the inbox, in replies/ for a request."""
    write_json(Path(directory) / folder / (op + ".json"), {"ok": ok, "error": error, **extra}, ensure_ascii=False)


def await_reply(path: Path, deadline: float) -> dict | None:
    """The answer at ``path``, removed once read; None when none came by ``deadline`` (time.monotonic)."""
    while time.monotonic() < deadline:
        try:
            result = read_json(path)
        except (OSError, ValueError):
            time.sleep(0.1)
            continue
        path.unlink(missing_ok=True)
        return result
    return None


def take(folder: Path):
    """Each message once, oldest first; a torn or vanished file is skipped."""
    for path in sorted(Path(folder).glob("*.json")):
        try:
            payload = read_json(path)
        except (OSError, ValueError):
            continue
        path.unlink(missing_ok=True)
        yield payload


def mark_done(directory: Path, status: str) -> None:
    """In a task runner: its job has ended and its card's last writes are done; only uploads are left
    (done.json). Older bridges ignore it and wait for exit.json, as before."""
    try:
        write_json(Path(directory) / DONE, {"status": status, "done_at": time.time()})
    except OSError:
        pass  # The bridge waits for the exit instead.


def mark_exit(directory: Path, status: str) -> None:
    """In a task runner, last: how it ended (exit.json)."""
    write_json(Path(directory) / EXIT, {"status": status, "finished_at": time.time()})


def read_exit(directory: Path) -> dict:
    """What exit.json says; empty when the runner left none."""
    try:
        return read_json(Path(directory) / EXIT)
    except (OSError, ValueError):
        return {}
