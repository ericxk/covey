"""The task runner process: one job of the Bridge, to completion (bin/covey_runner.py).

It rebuilds the Bridge from the arguments in the run directory's handoff.json, runs
the job there and serves the Bridge's forwarded clicks and answers from the inbox
until the job and the uploads and card updates it started have finished. The run
directory protocol is in core/run_dir.py, app/runner_spawn.py and app/runner_ipc.py.
"""
from __future__ import annotations

import argparse
import dataclasses
import os
import signal
import threading
import time
from pathlib import Path
from typing import Any

from covey.core import env as cenv
from covey.app import runner_ipc, runner_spawn
from covey.feishu import sdk
from covey.app.unified import UnifiedBridgeApp
from covey.core import timing
from covey.core.background import CARD_CLOSE, CARD_UPDATES, RECORDS, UPLOAD, WARMUP
from covey.core.harness import backend_capabilities
from covey.core.jobs import context_for_job, jobs_dir
from covey.core.locks import configure_card_locks
from covey.core.run_dir import HANDOFF, mark_done, mark_exit
from covey.core.state import read_json
from covey.core.store import directory_store
from covey.core.task_processes import TASK_DIR_ENV
from covey.feishu.transport import configure_receipts

UPLOAD_GRACE = 30 * 60
UPDATE_GRACE = 60
BACKGROUND = {UPLOAD, CARD_UPDATES, CARD_CLOSE, WARMUP}


def warm_records(state_dir):
    """Bring this process's job and question stores (core/store.py) up to date on a background thread,
    from its first moment: from the Bridge's .index and the journal since, so later queries read only
    the records they select. Without .index it reads every record, 2.3 s from NAS for 927 job records
    (S34), which then slowed the runner's start by 0.3-0.8 s (S41). Building the prompt queried them
    before the backend started, and so did a 补充 click during the start; a query meanwhile waits for
    this instead of starting its own; a failed one changes nothing (the next query catches up)."""
    def warm():
        try:
            for folder in (jobs_dir(state_dir), state_dir / "questions"):
                directory_store(folder).catch_up()
        except Exception:  # noqa: BLE001 - only a head start
            pass
    thread = threading.Thread(target=warm, daemon=True, name=RECORDS)
    try:
        thread.start()
    except RuntimeError:
        return None
    return thread


def _background(before: set) -> list:
    return [t for t in threading.enumerate()
            if t not in before and t.is_alive() and t.name in BACKGROUND and t is not threading.current_thread()]


def _wait_background(before: set, *, uploads=True) -> None:
    """Uploads and card updates the job started finish before the process ends; ``uploads`` False:
    all but the uploads."""
    started = time.monotonic()
    for thread in _background(before):
        if not uploads and thread.name == UPLOAD:
            continue
        elapsed = time.monotonic() - started
        limit = UPLOAD_GRACE if thread.name == UPLOAD else min(UPDATE_GRACE, UPLOAD_GRACE)
        thread.join(max(0.0, limit - elapsed))


def interrupt(job) -> None:
    """SIGTERM: the backend stops as for 停止, but the task is interrupted, not stopped. A container's
    shutdown sends every process one: read as the stop button, it ended a task "已停止" and its card
    offered only 重新投递结果 (T331 #2407, 2026-11-02). The worker leaves the turn unsettled and the
    record running; the Bridge marks it interrupted with 检查并继续, as after a runner that died. A stop
    already asked for stays a stop. Flags only: the handler runs in the middle of the main thread's work."""
    if not job.cancel_event.is_set():
        job.interrupted = True
    job.cancel_event.set()


@dataclasses.dataclass
class _Setup:
    """The handed-off job in this process: its Bridge (``gateway``), Harness app, job and context, and what the
    runner's end puts back (``previous_timing``, ``previous_card_locks``) or ends (``started``)."""
    gateway: Any
    app: Any
    job: Any
    ctx: Any
    started: Any
    previous_timing: Any
    previous_card_locks: Any


@dataclasses.dataclass
class _Inbox:
    """The thread that serves the run directory's inbox while the job runs (``_serve``); ``before``: the threads
    there were before the job, so its exit waits only for those it started (``_wait_background``)."""
    before: set
    stop: threading.Event
    thread: threading.Thread
    woken: Any


def runner_main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run one bridge job to completion in this process.")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--await-handoff", default="", help=argparse.SUPPRESS)  # runner_spawn.prestart_runner
    parser.add_argument("--compact-only", action="store_true", help=argparse.SUPPRESS)  # app/idle_compaction.py
    args = parser.parse_args(argv)
    if args.compact_only:  # No job: the main thread's session compacted while it is idle, in its own directory.
        from covey.app.compact_runner import compact_main
        return compact_main(args.run_dir)
    original = cenv.get(runner_spawn.RUNNER_PYTHONPATH)
    for spelling in cenv.spellings(runner_spawn.RUNNER_PYTHONPATH):
        os.environ.pop(spelling, None)
    if original is not None:
        os.environ["PYTHONPATH"] = original  # See runner_env: the task's commands keep the bridge's.
    directory = args.run_dir.absolute()
    # Inherited by the Harness and every command it starts, wherever they end up (core/task_processes.py).
    cenv.put(os.environ, TASK_DIR_ENV, str(directory))  # Both spellings: tools of an older release read the old one.
    handoff, early = _await(args, directory)
    if handoff is None:
        return 0  # No job came: nothing ran and nothing is left behind.
    setup = _build(handoff, early, directory)
    inbox = _serve(setup, directory)
    status = "finished"
    try:
        setup.gateway.process_reply_job(setup.job, setup.ctx)
    except BaseException as exc:  # noqa: BLE001 - always leave an exit record
        status = "error:" + type(exc).__name__
        setup.gateway.runtime.log(f"task runner failed: {type(exc).__name__}")
    finally:
        _finish(setup, directory, inbox, status)
    return 0


def _await(args, directory):
    """(the handoff, the Bridge built while waiting for it or None). A prestarted runner (``--await-handoff``)
    builds its Bridge while the Bridge writes the job's card and record, then waits; the handoff is None when no
    job came. Otherwise the handoff is already in the run directory."""
    if not args.await_handoff:
        return read_json(directory / HANDOFF), None
    # Started while the bridge writes the job's card and record: this runner's slow start (its
    # imports, the records, its Bridge) is done by the time the job is handed to it.
    warm_records(directory.parents[1])  # run_dir: <state>/runs/<job>/
    script = Path(__file__).resolve().parents[2] / "bin" / "covey_bridge.py"
    early = (str(script), UnifiedBridgeApp(script_path=script))
    return runner_spawn.await_handoff(directory, args.await_handoff), early


def _build(handoff, early, directory) -> _Setup:
    """The handed-off job ready to run here: this runner registered first, its Bridge (the early one when it runs
    the same script), timing, the context, the job loaded and registered, SIGTERM interrupting it."""
    fields = handoff["job"]["fields"]
    # First, before anything slow: a bridge stopped mid-handoff still finds this runner alive.
    runner_spawn.register_runner(directory, os.getpid(), Path(__file__).resolve().parents[2], fields.get("job_id"),
                    fields.get("message_id", ""), handoff["harness"])
    if early is None:
        warm_records(directory.parents[1])
    if early is not None and early[0] == handoff["script_path"]:
        gateway = early[1]
    else:
        gateway = UnifiedBridgeApp(script_path=Path(handoff["script_path"]))
    previous_timing = timing.configure(gateway.runtime.log)  # Millisecond timing lines (core/timing.py).
    # This runner's start, from the bridge writing its handoff to its first Feishu call, on any thread.
    started = timing.begin("runner", started=timing.at(handoff["created_at"]) if handoff.get("created_at") else None,
                           process=True, until_call=True, job=f"#{fields.get('job_id')}", harness=handoff["harness"],
                           **({"prestarted": "yes"} if early is not None else {}))
    timing.mark("app")
    gateway.runtime.runner = runner_ipc.RunnerLink(directory)
    ctx = gateway.build_context(gateway.build_parser().parse_args(handoff["argv"]))
    timing.mark("built")
    gateway.runtime.owner_open_id = handoff.get("owner") or None
    configure_receipts(ctx.state_dir)
    previous_card_locks = configure_card_locks(ctx.state_dir)  # The bot locks this job's cards too (repo review E4).
    app = gateway.apps[handoff["harness"]]
    app.harness.started = True  # The bridge already ran the backend's startup check.
    job = runner_spawn.load_job(handoff["job"])
    job.harness_capabilities = backend_capabilities(app.backend, context_for_job(app.harness.context, job))
    gateway.runtime.jobs.register(job)
    signal.signal(signal.SIGTERM, lambda *_: interrupt(job))
    return _Setup(gateway, app, job, ctx, started, previous_timing, previous_card_locks)


def _serve(setup, directory) -> _Inbox:
    """Start serving the inbox (clicks, 补充, answers for this job) on ``runner-inbox``, woken by each write
    its Bridge signals, and the SDK warm-up after taking ``before``, so the exit waits for it too."""
    gateway, app, job, ctx = setup.gateway, setup.app, setup.job, setup.ctx
    stop = threading.Event()
    woken = runner_ipc.accept_wake(directory)  # Its bridge signals each inbox write.

    def serve():
        while not stop.is_set():
            runner_ipc.take_inbox(gateway, app, job, ctx, directory)
            if woken is None:
                stop.wait(0.3)
            else:
                runner_ipc.await_wake(woken, 0.3)
    before = set(threading.enumerate())
    sdk.warm_sdk(ctx.profile, gateway.runtime.log)  # After ``before``: the exit waits for it too.
    thread = threading.Thread(target=serve, daemon=True, name="runner-inbox")
    thread.start()
    gateway.runtime.log(f"task runner job=#{job.job_id} harness={app.spec.name} pid={os.getpid()}")
    return _Inbox(before, stop, thread, woken)


def _finish(setup, directory, inbox, status) -> None:
    """How the runner ends, in this order; a step that raises ends it there, leaving the ones after it undone.
    Clicks are still served meanwhile: this process owns the card. Its last card writes first; then the bridge
    may free the topic (done.json) while the uploads finish; then the inbox stops and what came last is served."""
    gateway, app, job, ctx, before = setup.gateway, setup.app, setup.job, setup.ctx, inbox.before
    if job.interrupted and status == "finished":
        status = "interrupted"  # The Bridge's "task runner ended" line says so.
    for step in (
            lambda: _wait_background(before, uploads=False),
            lambda: mark_done(directory, status),
            lambda: _wait_background(before),
            inbox.stop.set,
            lambda: inbox.thread.join(timeout=5),
            lambda: runner_ipc.release_wake(inbox.woken),
            lambda: runner_ipc.take_inbox(gateway, app, job, ctx, directory),
            # One held as the job left the registry (its process_reply_job settled those before): marked before
            # exit.json, so the Bridge's finish_detached queues the main thread's.
            lambda: app.steering.settle_held_at_end(context_for_job(app.harness.context, job), job),
            lambda: _wait_background(before),
            lambda: mark_exit(directory, status),
            lambda: timing.end(setup.started),
            timing.flush,
            lambda: timing.configure(setup.previous_timing),
            lambda: configure_card_locks(setup.previous_card_locks)):
        step()
