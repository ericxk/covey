"""Starting a job's task runner, the Bridge side of app/detached.py: the job as a handoff or queued file
saves it (dump_job, load_job), the runner's command line and environment (runner_argv, runner_env), a
runner started before its job's card and record (prestart_runner, await_handoff), the handoff itself
(start_runner, register_runner) and the main thread's compaction runner (compaction_launch)."""
from __future__ import annotations

import dataclasses
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from covey.core import env as cenv
from covey.core.state import ensure_private_dir, read_json, write_json
from covey.app import idle_compaction
from covey.app.task_card import StreamingReply
from covey.core.jobs import BridgeReplyJob
from covey.core.process import process_identity, signal_group
from covey.core.run_dir import (ACKS, DONE, EXIT, HANDOFF, INBOX, LOG, REPLIES, REQUESTS, RUNNER, WAKE,
                                         queued_path, run_dir, runner_alive as alive)

RUNNER_PYTHONPATH = "COVEY_RUNNER_PYTHONPATH"
# A runner started before its handoff (prestart_runner) waits this long for it, then exits having run
# nothing; the bridge hands it a job only while it is younger than PRESTART_USE.
PRESTART_WAIT = 30.0
PRESTART_USE = 20.0

_SKIP_FIELDS = {"runtime", "cancel_event", "backend_process", "execution_lock", "progress", "early_runner",
                "start_held_until"}
_PATH_FIELDS = {"workdir", "history_path", "session_path", "sessions_dir"}


def _jsonable(value) -> bool:
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return False
    return True


def _default(f):
    return f.default_factory() if f.default is dataclasses.MISSING else f.default


def dump_job(job) -> dict:
    declared = dataclasses.fields(job)
    names = {f.name for f in declared}
    fields, extras = {}, {}
    for f in declared:
        if f.name in _SKIP_FIELDS:
            continue
        value = getattr(job, f.name)
        if f.metadata.get("extra"):
            # State that used to be set ad hoc: "extras", once set, is where every release reads it.
            if _jsonable(value) and value != _default(f):
                extras[f.name] = value
            continue
        value = str(value) if isinstance(value, Path) else value
        if _jsonable(value):
            fields[f.name] = value
    fields["execution_started"] = False  # The runner's worker starts it, like a fresh dequeue.
    extras.update({k: v for k, v in vars(job).items() if k not in names and not k.startswith("_") and _jsonable(v)})
    progress = None
    if job.progress is not None:
        progress = {k: v for k, v in vars(job.progress).items() if _jsonable(v)}
    return {"fields": fields, "extras": extras, "progress": progress, "cancelled": job.cancel_event.is_set()}


def load_job(data: dict):
    fields = dict(data["fields"])
    for name in _PATH_FIELDS:
        if fields.get(name):
            fields[name] = Path(fields[name])
    init = {f.name for f in dataclasses.fields(BridgeReplyJob) if f.init}
    job = BridgeReplyJob(**{k: v for k, v in fields.items() if k in init})
    for key, value in (data.get("extras") or {}).items():
        setattr(job, key, value)
    if data.get("progress"):
        known = {f.name for f in dataclasses.fields(StreamingReply)}
        progress = StreamingReply(**{k: v for k, v in data["progress"].items() if k in known})
        for key, value in data["progress"].items():
            if key not in known:
                setattr(progress, key, value)
        job.progress = progress
    if data.get("cancelled"):
        job.cancel_event.set()
    return job


def register_runner(directory: Path, pid: int, release: Path, job_id, key: str, harness: str) -> None:
    identity = process_identity(pid) or {"pid": pid}
    write_json(Path(directory) / RUNNER, {
        **identity, "release": str(release), "started_at": time.time(), "job_id": job_id,
        "key": key, "harness": harness})


def runner_env() -> dict:
    """The bridge's environment for a task runner, without PYTHONPATH entries resolved against the cwd.

    An empty or relative entry makes the interpreter read its cwd before it runs any code, and a
    NAS cwd that cannot be read at that moment ends the runner at startup. The runner puts the
    original back, so the commands its task runs see the bridge's PYTHONPATH."""
    env = os.environ.copy()
    if "PYTHONPATH" in env:
        entries = env["PYTHONPATH"].split(os.pathsep)
        kept = [entry for entry in entries if os.path.isabs(entry)]
        if kept != entries:
            cenv.put(env, RUNNER_PYTHONPATH, env["PYTHONPATH"])
            env["PYTHONPATH"] = os.pathsep.join(kept)
    return env


# Bridge options holding a path, which the bridge read against its own cwd.
_PATH_OPTIONS = ("--harness-config", "--pairing-file")


def runner_argv(argv, ctx) -> list:
    """The bridge's arguments for a task runner, which starts in its run directory: its path
    options made absolute, then the state and default workspace directories the bridge
    resolved (the last occurrence of an option wins), so the runner never reads a cwd."""
    out, rest = [], list(argv)
    while rest:
        arg = rest.pop(0)
        name, eq, value = arg.partition("=")
        if eq and name in _PATH_OPTIONS:
            arg = name + "=" + str(Path(value).absolute())
        elif arg in _PATH_OPTIONS and rest:
            out.append(arg)
            arg = str(Path(rest.pop(0)).absolute())
        out.append(arg)
    return [*out, "--state-dir", str(ctx.state_dir), "--workdir", str(ctx.default_workdir)]


def _spawn(runtime, directory: Path, *args):
    """A task runner of the bridge's own release in ``directory``: (process, release)."""
    # The runner is the bridge's own release: the same code from start to delivery.
    release = Path(runtime.script_path).resolve().parents[1]
    command = [sys.executable, "-B", str(release / "bin" / "covey_runner.py"), "--run-dir", str(directory), *args]
    fd = os.open(directory / LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        # Its own session: stopping the bridge's process group never reaches it. Its cwd is its run
        # directory, which exists: the bridge's (a NAS checkout) once failed to read at interpreter start.
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=fd, stderr=fd, cwd=directory,
                                   start_new_session=True, env=runner_env())
    finally:
        os.close(fd)
    try:
        threading.Thread(target=process.wait, daemon=True, name="task-runner-reaper").start()
    except BaseException:
        _abandon(process)  # Nothing would reap it, and the bridge reports this start as failed.
        raise
    return process, release


def _abandon(process) -> None:
    """End a runner whose start the bridge could not record, before it can take its job: start_runner
    then raises and run_detached reports the job as not started."""
    signal_group(process, signal.SIGKILL)
    try:
        process.wait(timeout=5)
    except Exception:  # noqa: BLE001 - its reaper may hold it; it was killed either way
        pass


def prestart_runner(runtime, job, ctx) -> None:
    """Start the task runner of a job about to run before its task card and record are written: it
    imports its code and builds its Bridge meanwhile (0.8 s of each task's start, S34) and waits for
    the handoff that names it (``--await-handoff``). start_runner hands it the job as it would a new
    runner; one it cannot use (ended, too old) is left to exit by itself, having run nothing. Only
    the live bridge, and never into a directory whose runner is alive."""
    if not runtime.detached() or job.early_runner is not None:
        return
    directory = run_dir(ctx.state_dir, job.message_id)
    if alive(directory):
        return
    nonce = secrets.token_hex(8)
    try:
        ensure_private_dir(directory)
        process, _ = _spawn(runtime, directory, "--await-handoff", nonce)
    except Exception as exc:  # noqa: BLE001 - never the submission's failure: the dispatch starts one as before
        runtime.log(f"task runner prestart unavailable: {type(exc).__name__}")
        return
    job.early_runner = SimpleNamespace(process=process, nonce=nonce, started=time.monotonic())


def start_runner(runtime, name: str, job, ctx) -> Path:
    directory = run_dir(ctx.state_dir, job.message_id)
    early, job.early_runner = job.early_runner, None
    if early is not None and (early.process.poll() is not None or time.monotonic() - early.started > PRESTART_USE):
        early = None  # Ended, or close to giving up: a new runner instead.
    ensure_private_dir(directory)
    for stale in (RUNNER, EXIT, WAKE, DONE):
        (directory / stale).unlink(missing_ok=True)
    for folder in (INBOX, ACKS, REQUESTS, REPLIES):
        ensure_private_dir(directory / folder)
        for path in (directory / folder).glob("*.json"):
            path.unlink(missing_ok=True)
    handoff = {"schema_version": 1, "harness": name, "argv": runner_argv(runtime.cli_argv, ctx),
               "owner": runtime.owner_open_id or "", "script_path": str(Path(runtime.script_path).resolve()),
               "job": dump_job(job), "created_at": time.time()}
    if early is not None:
        handoff["prestart"] = early.nonce  # Only that runner takes it; any other waiting here ignores it.
    write_json(directory / HANDOFF, handoff, ensure_ascii=False)
    if early is not None:
        process, release = early.process, Path(runtime.script_path).resolve().parents[1]
    else:
        process, release = _spawn(runtime, directory)
    try:
        register_runner(directory, process.pid, release, job.job_id, job.message_id, name)
    except BaseException:
        # A runner the bridge has no record of would run the job unwatched: none runs it.
        _abandon(process)
        (directory / HANDOFF).unlink(missing_ok=True)
        raise
    try:
        queued_path(ctx.state_dir, job.message_id).unlink(missing_ok=True)  # As detached.forget_queued.
    except OSError as exc:  # The runner has the job: a copy left in queued/ is not queued again once it runs.
        runtime.log(f"queued copy of job #{job.job_id} not removed: {type(exc).__name__}")
    return directory


def await_handoff(directory: Path, nonce: str, wait: float | None = None) -> dict | None:
    """The handoff naming this prestarted runner, or None when none came in ``wait`` (PRESTART_WAIT)."""
    deadline = time.monotonic() + (PRESTART_WAIT if wait is None else wait)
    while True:
        try:
            handoff = read_json(Path(directory) / HANDOFF)
        except (OSError, ValueError):
            handoff = None
        if isinstance(handoff, dict) and handoff.get("prestart") == nonce:
            return handoff
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.02)


def compaction_launch(runtime, app, job, ctx) -> SimpleNamespace:
    """How app/idle_compaction.py starts the main thread's compaction runner: this turn's ``handoff()`` (as
    start_runner writes one) and ``spawn(directory)``, a runner of this release with ``--compact-only``."""
    def handoff():
        return {"harness": app.spec.name, "argv": runner_argv(runtime.cli_argv, ctx),
                "owner": runtime.owner_open_id or "", "script_path": str(Path(runtime.script_path).resolve()),
                "job": dump_job(job)}
    return SimpleNamespace(handoff=handoff, spawn=lambda directory: _spawn(runtime, directory, idle_compaction.ARG)[0])
