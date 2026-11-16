"""The butler's idle compaction runner (app/idle_compaction.py): ``bin/covey_runner.py --compact-only``.

No job, no card: the Bridge rebuilt from the finished turn's handoff in ``compacting.json`` (the same arguments,
gateway and credentials), the backend's ``compact_session`` on the topic's session as its pointer names it now (the
session keeps its ID), then the request settled under its record lock: ``compacted.json`` in the topic's directory
when it compacted, else the request back (``failed`` unless it was stopped). SIGTERM stops it, as 停止 stops a turn.
Only app/runner.py imports this module.
"""
from __future__ import annotations

import dataclasses
import signal
import threading
import time
from pathlib import Path

from covey.app import runner_ipc, runner_spawn
from covey.app import idle_compaction as idle
from covey.app.butler import COMPACT_INSTRUCTIONS
from covey.app.unified import UnifiedBridgeApp
from covey.core.harness import RunCallbacks, RunInputs, RunRequest
from covey.core.jobs import context_for_job
from covey.core.locks import resource_lock
from covey.core.process import JobCancelled, process_identity
from covey.core.state import read_object, write_json
from covey.core.topics import topic_file

def compact_main(directory) -> int:
    """The compaction runner (bin/covey_runner.py --compact-only): compact the session of the turn in
    ``compacting.json`` and settle its request. Its exit status is one of idle.OUTCOMES."""
    cancel = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: cancel.set())
    mark = Path(directory).absolute() / idle.MARK
    marker = read_object(mark)
    handoff = marker.get("handoff") or {}
    request_path = Path(marker.get("request_path") or "")
    if not marker.get("request_path"):
        return idle.NOTHING
    outcome, session, log = idle.NOTHING, "", print
    try:
        gateway = UnifiedBridgeApp(script_path=Path(handoff["script_path"]), task_runner=True)
        gateway.runtime.runner = runner_ipc.RunnerLink(mark.parent)  # In a runner: no job starts here.
        log = gateway.runtime.log
        gateway.build_context(gateway.build_parser().parse_args(handoff["argv"]))
        gateway.runtime.owner_open_id = handoff.get("owner") or None
        app = gateway.apps[handoff["harness"]]
        job = runner_spawn.load_job(handoff["job"])
        target = context_for_job(app.harness.context, job)
        session = app.sessions.read_session_id(target.session_path) or ""
        if target.device:
            session = ""  # On a device: its session file is not here, and its process has no pid (``observe``).
        if cancel.is_set():
            outcome = idle.CANCELLED
        elif session:
            target = dataclasses.replace(target, options={**target.options, "compact_first": True,
                                                          "compact_instructions": COMPACT_INSTRUCTIONS})

            def observe(process):
                if process is not None:
                    with resource_lock("record", request_path):
                        current = read_object(mark)
                        if current.get("started_at") == marker.get("started_at"):
                            write_json(mark, {**current, "claude": process_identity(process.pid)}, ensure_ascii=False)
            log(f"idle compaction topic={job.topic_id[:12]} session={session}")
            done = app.backend.compact_session(RunRequest(target, "", session, callbacks=RunCallbacks(process=observe),
                                                          inputs=RunInputs(cancel)))
            outcome = idle.DONE if done else idle.CANCELLED if cancel.is_set() else idle.NOT_DONE
    except JobCancelled:
        outcome = idle.CANCELLED
    except Exception as exc:  # noqa: BLE001 - the request goes back, failed: the next turn compacts first
        log(f"idle compaction failed: {type(exc).__name__}")
        outcome = idle.NOT_DONE
    with resource_lock("record", request_path):
        if outcome == idle.DONE:
            topic = str(marker.get("topic_id") or "")
            write_json(topic_file(state_of(mark), topic).parent / idle.COMPACTED,
                       {"at": time.time(), "session_id": session, "job": marker.get("job")}, ensure_ascii=False)
        if read_object(mark).get("started_at") == marker.get("started_at"):
            mark.unlink(missing_ok=True)
            if outcome != idle.DONE:
                idle.put_back(request_path, marker.get("request"), failed=outcome == idle.NOT_DONE)
    log(f"idle compaction {idle.OUTCOMES[outcome]} session={session}")
    return outcome


def state_of(mark: Path) -> Path:
    """<state>/main-thread/compacting/<topic>/compacting.json -> <state>."""
    return mark.parents[len(idle.DIR.parts) + 1]
