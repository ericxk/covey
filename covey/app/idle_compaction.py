"""The butler's compaction while it is idle (docs/butler.md 会话): the owner no longer waits for it.

Before, a butler turn that ended past ``compact_tokens`` left a request (app/compaction.py) that the
next turn took: the owner's next message waited for the summary first (85 s of 109 s). Now its task runner
marks the request ``idle`` and the Bridge compacts between the turns:

- ``after_runner`` (app/detached.py finish_detached, once the turn's runner has exited): with a request waiting, a
  thread waits IDLE_SECONDS (the owner's quick follow-up goes first; still within the prompt cache's 5 minutes); a
  later turn's exit supersedes it. Then ``start``: the butler still on, the turn ``done`` (a turn waiting for an
  answer keeps its session as it is), no job of the topic here (queued, held or running), the request not ``failed``.
- Under the request's record lock it checks the jobs once more, takes the request, starts a runner of the Bridge's
  release that only compacts (``bin/covey_runner.py --compact-only``) and writes
  ``<state>/main-thread/compacting/<topic>/compacting.json``: the runner's identity (pid, start ticks; its pid leads
  its process group), when it started, the request. A turn that registers later finds it. No job, no card, no
  sequence: the runner's lines go to runner.log beside it, the Bridge's to its log.
- ``compact_main`` (app/compact_runner.py): the Bridge rebuilt from the finished turn's handoff (the same arguments, gateway, credentials),
  the backend's ``compact_session`` on the topic's session (the turn's environment and term relay; the session keeps
  its ID). Done: ``compacted.json`` in the topic's directory, from which the next turn's snapshot is whole
  (app/butler.py take_snapshot). Not done: the request back, ``failed``, which the next turn takes and compacts
  first, as before. SIGTERM: the request back as it was.
- ``stop_for_turn`` (app/butler.py butler_turn, in the next turn's runner): a compaction under way stops (its
  process group, at most STOP_SECONDS), the request back, and the turn runs on the session as it is; its end asks
  again. A context FALLBACK times the threshold compacts before the turn whatever happens here.
- ``clear_left`` (the Bridge's start): a compaction runner left behind is stopped, its request put back.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from covey.app.compaction import Compaction
from covey.core import butler as contract
from covey.core.jobs import context_for_job, find_job_record, job_key
from covey.core.locks import resource_lock
from covey.core.process import is_owned, process_identity, terminate_owned
from covey.core.state import ensure_private_dir, read_object, write_json

IDLE_SECONDS = 90.0
STOP_SECONDS = 15  # The runner's own stop closes Claude Code in about 3 s; then its group is killed.
CHILD_SECONDS = 3
DIR = Path("main-thread") / "compacting"
MARK = "compacting.json"
COMPACTED = "compacted.json"
ARG = "--compact-only"
# The compaction runner's exit status, and what the Bridge's line says of it.
DONE, NOT_DONE, CANCELLED, NOTHING = 0, 3, 4, 5
OUTCOMES = {DONE: "finished", NOT_DONE: "failed", CANCELLED: "cancelled", NOTHING: "skipped"}

_waiting: dict = {}  # Topic -> the token of the turn whose idle wait counts (the latest).
_waiting_lock = threading.Lock()


def run_dir(state_dir, topic_id) -> Path:
    return Path(state_dir) / DIR / topic_id


def compacted_at(topic_dir) -> float:
    """When the topic's session was last compacted between its turns (0: never)."""
    value = read_object(Path(topic_dir) / COMPACTED).get("at")
    return float(value) if isinstance(value, (int, float)) else 0.0


def put_back(request_path: Path, request, *, failed) -> None:
    """The request a compaction took, back unless a newer one waits; ``failed`` marks either so. Under its lock."""
    current = read_object(request_path)
    if current.get("at"):
        if failed and not current.get("failed"):
            write_json(request_path, {**current, "failed": True}, ensure_ascii=False)
    elif isinstance(request, dict) and request.get("at"):
        write_json(request_path, {**request, **({"failed": True} if failed else {})}, ensure_ascii=False)


# The Bridge's side ----------------------------------------------------------

def after_runner(gateway, app, job, ctx, launch) -> None:
    """A butler turn's runner has exited: with a request waiting, compact once the topic was idle IDLE_SECONDS.
    Only the live Bridge, only the butler's own turns (core/butler.py own_turn: the butler on).
    ``launch`` (app/runner_spawn.py compaction_launch): the turn's ``handoff()`` and ``spawn(directory)`` of a runner."""
    runtime = gateway.runtime
    if not runtime.detached() or not contract.traits_of(job).own_turn or not hasattr(app.backend, "compact_session"):
        return
    target = context_for_job(ctx, job)
    if target.device or app.compaction.request(target) is None:
        return  # A device's session is not ours to compact (backends/claude/compaction.py compact_before_turn).
    token = object()
    with _waiting_lock:
        _waiting[job.topic_id] = token

    def wait():
        if runtime.shutdown.wait(IDLE_SECONDS):
            return
        with _waiting_lock:
            if _waiting.get(job.topic_id) is not token:
                return  # A later turn's exit waits instead.
            _waiting.pop(job.topic_id, None)
        try:
            start(gateway, app, job, target, launch)
        except Exception as exc:  # noqa: BLE001 - the request stays; the next turn's end tries again
            runtime.log(f"idle compaction not started topic={job.topic_id[:12]}: {type(exc).__name__}")
    threading.Thread(target=wait, daemon=True, name="idle-compaction").start()


def start(gateway, app, job, ctx, launch):
    """Start the compaction runner and wait for it (on the caller's thread); its exit status, or None when it did not
    start: the butler off, the turn not ``done``, a job of the topic here, no request, or one that failed."""
    runtime, state = gateway.runtime, ctx.state_dir
    if not contract.traits_of(job).own_turn or ctx.device:  # The runner below reads process.pid: a device's has none.
        return None
    if (find_job_record(state, job.message_id) or {}).get("status") != "done":
        return None
    request_path = Compaction.path(ctx).absolute()
    directory = run_dir(state, job.topic_id)
    mark = directory / MARK
    with resource_lock("record", request_path):
        request = app.compaction.request(ctx)
        if request is None or request.get("failed") or read_object(mark) or runtime.jobs.in_topic(job.topic_id):
            return None
        ensure_private_dir(directory)
        marker = {"schema_version": 1, "topic_id": job.topic_id, "started_at": time.time(),
                  "job": job_key(job.message_id, job.job_id), "request_path": str(request_path), "request": request,
                  "handoff": launch.handoff()}
        write_json(mark, marker, ensure_ascii=False)
        try:
            process = launch.spawn(directory)
        except BaseException:
            mark.unlink(missing_ok=True)
            raise
        request_path.unlink(missing_ok=True)
        write_json(mark, {**marker, "process": process_identity(process.pid) or {"pid": process.pid}},
                   ensure_ascii=False)
    runtime.log(f"idle compaction started topic={job.topic_id[:12]} after job=#{job.job_id} pid={process.pid}")
    status = process.wait()
    runtime.log(f"idle compaction {OUTCOMES.get(status, 'failed')} topic={job.topic_id[:12]} status={status}")
    return status


def stop(mark: Path, marker: dict, log, why: str) -> None:
    """Stop the compaction ``marker`` names (its runner's group, then a Claude Code it left), and put its request back
    unless the runner settled it. Read again under the lock first: a Bridge starting it writes the runner's identity
    there right after the spawn."""
    request_path = Path(marker.get("request_path") or "")
    with resource_lock("record", request_path):
        marker = read_object(mark)
    if not marker.get("request_path"):
        return  # It ended meanwhile.
    for field, seconds in (("process", STOP_SECONDS), ("claude", CHILD_SECONDS)):
        identity = (read_object(mark) if field == "claude" else marker).get(field)
        if isinstance(identity, dict) and identity.get("start_ticks") and is_owned(identity):
            try:
                terminate_owned(identity, timeout=seconds)
            except (OSError, RuntimeError) as exc:
                log(f"idle compaction not stopped pid={identity.get('pid')}: {type(exc).__name__}")
    with resource_lock("record", request_path):
        if read_object(mark).get("started_at") != marker.get("started_at"):
            return  # It ended by itself: its request is settled.
        mark.unlink(missing_ok=True)
        put_back(request_path, marker.get("request"), failed=False)
    log(f"idle compaction cancelled {why} topic={str(marker.get('topic_id') or '')[:12]}")


def stop_for_turn(ctx, job, log) -> None:
    """A turn of the butler starts: a compaction of its session under way stops first."""
    mark = run_dir(ctx.state_dir, job.topic_id) / MARK
    marker = read_object(mark)
    if marker.get("request_path"):
        stop(mark, marker, log, f"by job=#{job.job_id}")


def clear_left(state_dir, log) -> None:
    """The Bridge's start: no compaction runner outlives the Bridge that started it."""
    root = Path(state_dir) / DIR
    for mark in sorted(root.glob(f"*/{MARK}")) if root.is_dir() else ():
        marker = read_object(mark)
        if marker.get("request_path"):
            stop(mark, marker, log, "at the Bridge's start")
        else:
            mark.unlink(missing_ok=True)
