"""The run directory of a job handed to a task runner, ``<state>/runs/<job>/`` (app/detached.py).

Every release's Bridge and task runner read and write these names, so they are defined here once and
never change. The Bridge writes handoff.json (the job and its arguments), runner.json (the runner's
process identity, written again by the runner) and inbox/ (clicks, supplements and answers it
forwards); the runner writes wake.json (where to signal an inbox write), acks/ (its answers to the
inbox), requests/ (continuations it asks for), done.json (its job has ended, uploads may remain) and
exit.json, and the Bridge answers its requests in replies/. Each message is one JSON file named by its
op id, ``<time_ns>-<hex8>``, read once, oldest first. A job waiting for its turn is kept in
``<state>/queued/<job>.json``.
"""
from __future__ import annotations

import json
import re
import secrets
import time
from pathlib import Path

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
OP = re.compile(r"[0-9]+-[0-9a-f]+")


def _stem(state_dir: Path, key: str) -> str:
    return job_path(Path(state_dir), key).stem


def run_dir(state_dir: Path, key: str) -> Path:
    """Absolute: a runner's argv names its run directory so (runner_alive)."""
    return (Path(state_dir) / RUNS / _stem(state_dir, key)).absolute()


def queued_path(state_dir: Path, key: str) -> Path:
    return Path(state_dir) / QUEUED / (_stem(state_dir, key) + ".json")


def runner_info(directory: Path) -> dict | None:
    try:
        return read_json(Path(directory) / RUNNER)
    except (OSError, ValueError):
        return None


def runner_alive(directory: Path) -> bool:
    """The task runner recorded in this run directory's runner.json is still the same live process."""
    try:
        info = json.loads((Path(directory) / RUNNER).read_text())
    except (OSError, ValueError):
        return False
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


def live_keys(state_dir: Path, alive=runner_alive) -> set:
    """Job keys whose task runner is alive, whichever bridge started it."""
    root = Path(state_dir) / RUNS
    keys = set()
    for directory in root.iterdir() if root.is_dir() else []:
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
            continue  # Stopped, and no runner has it: a restart interrupts nothing (one never started, 2026-10-28).
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
