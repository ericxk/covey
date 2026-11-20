"""Run each task in its own process so a bridge restart or deploy does not stop it.

The bridge hands a dequeued job to a task runner (bin/covey_runner.py) in
its own session. The runner builds the same app from the bridge's arguments and
runs the whole job there: backend, task card, delivery. While it is alive, the
bridge forwards every click on that job's cards and every typed answer to its
questions to the runner, so one process writes the job's card and record. The
runner never starts a job itself: a continuation it needs is enqueued by the
bridge on request. A runner that ends before its job begins ran nothing, so the
bridge starts it once more before reporting the job. A restarted bridge adopts
runners that are still alive (same pid, start time and run directory), marks a
job interrupted only when its runner is gone, and re-queues messages that were
waiting in its queue.

Starting a runner is in app/runner_spawn.py, the messages between the bridge and a runner in
app/runner_ipc.py; this module keeps the job's life: run_detached, watch_until_exit, release_done,
finish_detached, adopt_runners and the queued files. Run directory ``<state>/runs/<job>/`` (its names
and messages: core/run_dir.py): handoff.json (the
job and the bridge arguments), runner.json (process identity), runner.log, inbox/ and acks/
(bridge to runner), requests/ and replies/ (runner to bridge) and exit.json.
A queued job is kept in ``<state>/queued/<job>.json`` until it is handed off. A job about
to run gets its runner before its card and record are written (``prestart_runner``); it waits
for the handoff that names it.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from covey.core import timing
from covey.core.state import read_json, write_json
from covey.app import idle_compaction, knowledge_collect, memory_collect, butler_relay, owner_send
from covey.core import turn_words
from covey.core.turn_words import interrupted_notice, retry_hint
from covey.app.task_card import bind_record
from covey.core.card_sequence import adopt
from covey.app import devices as device_turns
from covey.core.jobs import (DEVICE_LOST_STATUS, SIGTERM_STATUS, context_for_job, find_job_record,
                                      interrupted_fields, job_key, upsert_job_record)
from covey.core.butler import traits_of
from covey.core.statuses import RUNNING
from covey.core.run_dir import (DONE, EXIT, HANDOFF, INBOX, QUEUED, RUNS, ack, live_keys as _live_keys,
                                         queued_path, read_exit, restart_blockers as _restart_blockers,
                                         runner_alive as alive, runner_info, take)
from covey.app.policy_guard import requested
from covey.app.runner_ipc import remote_control, serve_requests, write_inbox
from covey.app.runner_spawn import compaction_launch, dump_job, load_job, start_runner

RELAUNCH_DELAY = 2.0


def live_keys(state_dir: Path) -> set:
    """Job keys whose task runner is alive, whichever bridge started it."""
    return _live_keys(state_dir, alive)


def remember_queued(state_dir: Path, harness: str, job) -> None:
    """Keep a waiting job so the next bridge can queue it again after a restart."""
    write_json(queued_path(state_dir, job.message_id), {"schema_version": 1, "harness": harness, "job": dump_job(job)},
               ensure_ascii=False)


def update_queued(state_dir: Path, job) -> None:
    """A waiting job changed (a supplement joined its request): keep its saved copy in step."""
    path = queued_path(state_dir, job.message_id)
    try:
        saved = read_json(path)
    except (OSError, ValueError):
        return
    write_json(path, {**saved, "job": dump_job(job)}, ensure_ascii=False)


def forget_queued(state_dir: Path, key: str) -> None:
    queued_path(state_dir, key).unlink(missing_ok=True)


def restart_blockers(state_dir: Path) -> list:
    """What a restart would interrupt now (core.run_dir.restart_blockers), with the live runners
    from this module's live_keys and alive."""
    return _restart_blockers(state_dir, live_keys(state_dir))


def run_detached(gateway, app, job, ctx) -> None:
    """Hand the job to a task runner, or keep watching an adopted one, until that runner exits."""
    directory = job.detached_run_dir
    if directory is None:
        with job.execution_lock:
            if job.cancelled_before_start:
                app.runtime.unregister_reply_job(job)
                return
            if gateway.runtime.shutdown.is_set():
                return  # Still queued on disk: the next bridge queues it again.
            job.execution_started = True  # No longer queued: a queued message may steer into it.
            # Under the lock, so a click that missed the queued job waits for the runner to exist.
            try:
                directory = job.detached_run_dir = start_runner(gateway.runtime, app.spec.name, job, ctx)
            except Exception as exc:  # noqa: BLE001 - no runner has the job: it ends here
                not_started(gateway, app, job, ctx, exc)
                return
        timing.mark("spawned")
        timing.end_current("job_start")  # Its line: from the scheduler taking the job to this spawn.
        app.runtime.log(f"task runner started job=#{job.job_id} pid={(runner_info(directory) or {}).get('pid')} dir={directory}")
    progress, job.progress = job.progress, None  # The runner owns the card now; nothing here may write to it.
    if job.backend_control is None:
        job.backend_control = remote_control(app, ctx, directory, job.harness_capabilities)
    while watch_until_exit(gateway, app, job, ctx, directory, release=True):
        if not relaunch_unstarted(gateway, app, job, ctx, directory, progress):
            finish_detached(gateway, app, job, ctx, directory)
            return


def watch_until_exit(gateway, app, job, ctx, directory: Path, *, release=False) -> bool:
    """Serve the requests of the runner in ``directory`` while it lives and once more after: True once it
    has exited. False when the bridge stops (the runner keeps going; the next bridge adopts it) or, with
    ``release``, when its job is done and release_done has freed its topic while it uploads."""
    while alive(directory):
        serve_requests(gateway, app, job, ctx, directory)
        if release and job_done(ctx, job, directory):
            release_done(gateway, app, job, ctx, directory)
            return False  # The topic's next task starts while this runner uploads.
        if gateway.runtime.shutdown.wait(0.5):
            return False
    serve_requests(gateway, app, job, ctx, directory)
    return True


def job_done(ctx, job, directory: Path) -> bool:
    """The runner wrote done.json and the job's record has ended (or waits for an answer)."""
    if not (Path(directory) / DONE).exists():
        return False
    record = find_job_record(ctx.state_dir, job.message_id)
    return bool(record) and record.get("status") not in RUNNING


def release_done(gateway, app, job, ctx, directory: Path) -> None:
    """Free the topic of a job whose runner only uploads now (median 3 s, up to 29 s, 2026-10): the
    job leaves the registry and its scheduler slot, so the topic's next message, 检查并继续 and 换配置 go
    ahead. Clicks on its cards still go to the runner while it lives (runner_for finds its directory);
    the outbox, automatic retries and maintenance wait for its exit (JobRegistry.finishing); a thread
    serves its requests and, once it exits, ends it as finish_detached does."""
    runtime = gateway.runtime
    with runtime.jobs.lock:
        runtime.jobs.finishing.add(job.message_id)
    app.runtime.unregister_reply_job(job)
    app.runtime.log(f"task runner done job=#{job.job_id}; its topic goes on while it finishes")

    def watch():
        try:
            queue_lost(app, job, ctx)  # Those its runner has marked by now go before the topic's next message.
            if watch_until_exit(gateway, app, job, ctx, directory):  # Else the next bridge adopts and releases it.
                finish_detached(gateway, app, job, ctx, directory)
        except Exception as exc:  # noqa: BLE001 - its job has ended; only the exit's bookkeeping is lost
            app.runtime.log(f"task runner exit of job #{job.job_id} not settled: {type(exc).__name__}")
        finally:
            with runtime.jobs.lock:
                runtime.jobs.finishing.discard(job.message_id)
    threading.Thread(target=watch, daemon=True, name="runner-exit").start()


def start_error(exc) -> str:
    """What the card says of a runner that could not start: the error's kind, no path."""
    reason = getattr(exc, "strerror", None) if isinstance(exc, OSError) else None
    return f"{type(exc).__name__}: {reason}" if reason else type(exc).__name__


def not_started(gateway, app, job, ctx, exc) -> None:
    """start_runner raised, so no runner has the job (it kills one it could not record). The job ends
    as failed with runner_start_failed: its card says why and offers 检查并继续, its queued copy goes
    and its topic's slot is freed. Called under the job's execution lock: a stop click after this finds
    the record ended."""
    reason = start_error(exc)
    app.runtime.log(f"task runner not started job=#{job.job_id}: {reason}")
    job.progress = None  # The card is rewritten whole as the recovery card below.
    try:
        record = find_job_record(ctx.state_dir, job.message_id)
        if record and record.get("status") in RUNNING:
            record = upsert_job_record(ctx.state_dir, job.message_id, status="failed", execution_status="failed",
                                       runner_start_failed=True, runner_start_error=reason,
                                       status_message="task runner could not be started; the job did not run")
            app.controls.report_unfinished(context_for_job(ctx, job), record,
                                           f"任务 #{job.job_id} 未能启动（{reason}），没有执行。在原话题{retry_hint()} 检查后继续。",
                                           app.key("runner-start-failed", job.message_id),
                                           keep_question_forms=False, restart=False)
    finally:
        app.runtime.unregister_reply_job(job)


def unstarted(record, directory: Path) -> bool:
    """The runner ended before its job began: the record is still queued, so no model call,
    worker card write or external task happened."""
    return (record or {}).get("status") == "queued" and not (Path(directory) / EXIT).exists()


def relaunch_unstarted(gateway, app, job, ctx, directory: Path, progress) -> bool:
    """Start a runner that ended before its job began once more; True when one runs again."""
    record = find_job_record(ctx.state_dir, job.message_id)
    if job.runner_relaunched or not unstarted(record, directory):
        return False
    def inbox_used():
        return any((Path(directory) / INBOX).glob("*.json"))
    if inbox_used():
        return False  # A click or answer went to the dead runner: finish_detached hands it back.
    if gateway.runtime.shutdown.wait(RELAUNCH_DELAY):
        return False
    with job.execution_lock:
        # Checked again after the wait: a new runner starts with an empty inbox.
        if job.cancel_event.is_set() or inbox_used():
            return False
        job.runner_relaunched = True  # Saved in the handoff: a later bridge does not start it a third time.
        if progress is not None:
            job.progress = adopt(progress, record.get("card_sequence"))
        try:
            start_runner(gateway.runtime, app.spec.name, job, ctx)
        except Exception as exc:  # noqa: BLE001 - finish_detached reports the job as not started
            app.runtime.log(f"task runner relaunch failed job=#{job.job_id}: {start_error(exc)}")
            return False
        finally:
            job.progress = None
    app.runtime.log(f"task runner relaunched job=#{job.job_id} pid={(runner_info(directory) or {}).get('pid')}: "
            "the last one ended before the job started")
    return True


def finish_detached(gateway, app, job, ctx, directory: Path) -> None:
    if requested(ctx.state_dir, job.message_id):
        app.runtime.unregister_reply_job(job)
        queue_lost(app, job, ctx)
        butler_relay.after_runner(gateway, job, ctx)
        return  # The guard owns the stopped runner's final card and notice.
    try:
        record = find_job_record(ctx.state_dir, job.message_id)
        outcome = read_exit(directory)
        app.runtime.log(f"task runner ended job=#{job.job_id} status={(record or {}).get('status')} exit={outcome.get('status', 'none')}")
        if record and record.get("status") in RUNNING:
            start_failed = unstarted(record, directory)
            # A runner a SIGTERM ended (a container shutting down) left its turn this way.
            cause = "sigterm" if not start_failed and record.get("status_message") == SIGTERM_STATUS else ""
            if not start_failed and record.get("status_message") == DEVICE_LOST_STATUS:
                cause = "device_lost"  # Its device stayed away past the patience: not continued by itself (the device may be gone).
            updated = upsert_job_record(ctx.state_dir, job.message_id, runner_start_failed=start_failed,
                                        **interrupted_fields(record, "task runner exited before this job started"
                                                             if start_failed else
                                                             "task runner exited before this job reached a terminal state",
                                                             cause))
            app.controls.report_unfinished(context_for_job(ctx, job), updated,
                                           device_notice(updated, job.job_id, directory),
                                           app.key("runner-lost", job.message_id),
                                           keep_question_forms=False, restart=cause in ("sigterm", "restart"))
        # A turn on a device the runner left without seeing its Agent end (the runner was killed, the link was lost, a stop
        # could not be told): the device may still run it. Registered, never reaped here (core/device_reap.py).
        if record:
            device_turns.register_left(app.runtime.log, ctx.state_dir, record, directory)
    finally:
        app.runtime.unregister_reply_job(job)
    close_forms_sent_after_handoff(app, job, ctx, directory)
    for payload in take(Path(directory) / INBOX):
        # Requests that arrived as the runner exited: the card and record are the bridge's again.
        kind = payload.get("kind")
        if kind == "card_event":
            gateway.handle_card_action_event(payload["event"], gateway.harnesses[gateway.topics.default].context or ctx)
        elif kind == "steer":
            ack(directory, payload["id"], False, "任务已结束，补充没有发出", refused=True)
        elif kind == "answer":
            ack(directory, payload["id"], False, local=True)
    queue_lost(app, job, ctx)
    knowledge_collect.collect(gateway.runtime, job, ctx)  # The owner's 记一下 in a topic's turn (off without COVEY_KNOWLEDGE).
    memory_collect.collect(gateway.runtime, getattr(app, "messenger", None), job, ctx)  # The owner's 以后 / 别再 … (off without COVEY_MEMORY).
    owner_send.collect(gateway, job, ctx)  # The turn's send_as_owner drafts: 代发草稿 cards (off without a 代答 sender).
    butler_relay.after_runner(gateway, job, ctx)
    _compact_when_idle(gateway, app, job, ctx)


def device_notice(record, job_id, directory) -> str:
    """The text a runner's end sends when no card can be written: the turn's own for a device's, else the usual one."""
    return device_turns.lost_notice(record, directory) or turn_words.device_interrupted(record) or interrupted_notice(job_id)


def queue_lost(app, job, ctx) -> None:
    """A butler turn's runner is done or gone: the owner's messages its turn never took, which the runner marked
    (app/steering.py requeue_lost), become turns of their own here; a runner never queues a job."""
    if not traits_of(job).lists_supplements:
        return
    try:
        app.steering.queue_lost(context_for_job(ctx, job), job)
    except Exception as exc:  # noqa: BLE001 - those not tried stay marked: the Bridge's next start queues them
        app.runtime.log(f"lost supplements not queued job=#{job.job_id}: {type(exc).__name__}")


def queue_stranded(gateway, ctx, app) -> None:
    """At start, after ``adopt_runners``: the owner's messages a butler turn never took, marked by its runner or
    this Bridge (app/steering.py requeue_lost), whose turn ended with no Bridge to queue them (the runner exited
    while the Bridge was down, or the Bridge stopped before its ``queue_lost`` ran), become turns of their own now,
    once. An adopted or still live runner's wait for its end here (``release_done``, ``finish_detached``). ``app``:
    the butler's Harness app, None when this Bridge has none."""
    if app is None:
        return
    try:
        running = set(getattr(gateway.runtime, "adopted_keys", None) or ()) | live_keys(ctx.state_dir)
        app.steering.queue_stranded(app.harness.context, running)
    except Exception as exc:  # noqa: BLE001 - those not tried stay marked for the next start; startup goes on
        gateway.runtime.log(f"lost supplements not queued at start: {type(exc).__name__}")


def _compact_when_idle(gateway, app, job, ctx) -> None:
    """The butler's session compacted once its topic stays idle (app/idle_compaction.py after_runner)."""
    try:
        idle_compaction.after_runner(gateway, app, job, ctx, compaction_launch(gateway.runtime, app, job, ctx))
    except Exception as exc:  # noqa: BLE001 - the request stays for the next turn
        app.runtime.log(f"idle compaction not scheduled job=#{job.job_id}: {type(exc).__name__}")


def close_forms_sent_after_handoff(app, job, ctx, directory: Path) -> None:
    """补充 forms this bridge sent for a 补充 click that came while the job was handed over: the form
    went out after the handoff was written, so the runner never knew it and left it open.
    Closed here once the runner has ended; the ones in the handoff were the runner's to close."""
    try:
        extras = read_json(Path(directory) / HANDOFF)["job"].get("extras") or {}
        handed = set(extras.get("steer_forms") or [])
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        handed = set()  # Closing one twice writes the same note again.
    job.steer_forms = [mid for mid in job.steer_forms if mid not in handed]
    if not job.steer_forms:
        return
    try:
        app.steering.close_steer_forms(context_for_job(ctx, job), job, held=False)
    except Exception as exc:  # noqa: BLE001 - a stale form still refuses late submissions
        app.runtime.log(f"steer form not closed: {type(exc).__name__}")


def adopt_runners(gateway, ctx) -> set:
    """Watch every live runner again and re-queue waiting jobs; returns the keys kept running."""
    kept = set()
    root = Path(ctx.state_dir) / RUNS
    for directory in sorted(root.iterdir()) if root.is_dir() else []:
        directory = directory.absolute()
        if not directory.is_dir() or not alive(directory):
            continue
        try:
            handoff = read_json(directory / HANDOFF)
            if handoff["harness"] not in gateway.apps:
                # compat(ff57531): an older release's runner of a removed Harness finishes its job on its own; retire
                # when no live task runner's Harness is out of the roster (compat_report: removed-harness-runners)
                gateway.runtime.log(f"task runner not adopted: {directory.name} harness={handoff['harness']} not in roster")
                continue
            app = gateway.apps[handoff["harness"]]
            job = load_job(handoff["job"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            gateway.runtime.log(f"task runner not adopted: {directory.name} {type(exc).__name__}")
            continue
        job.detached_run_dir = directory
        job.execution_started = True
        job.backend_control = remote_control(app, app.harness.context, directory)
        app.runtime.enqueue_reply_job(job, app.harness.name)
        kept.add(job.message_id)
        write_inbox(directory, {"kind": "bridge_restarted", "at": time.time()})
        gateway.runtime.log(f"adopted task runner job=#{job.job_id} pid={(runner_info(directory) or {}).get('pid')}")
    waiting = []
    folder = Path(ctx.state_dir) / QUEUED
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            saved = read_json(path)
            if saved["harness"] not in gateway.apps:
                # Never runnable again; notify_stale_jobs ends its record with the topic's notice.
                gateway.runtime.log(f"queued job not restored: {path.name} harness={saved['harness']} not in roster")
                path.unlink(missing_ok=True)
                continue
            app = gateway.apps[saved["harness"]]
            job = load_job(saved["job"])
            record = find_job_record(ctx.state_dir, job.message_id) or {}
        except (OSError, ValueError, KeyError, TypeError) as exc:
            gateway.runtime.log(f"queued job not restored: {path.name} {type(exc).__name__}")
            continue
        if record.get("status") != "queued" or job.message_id in kept or job.cancel_event.is_set():
            path.unlink(missing_ok=True)
            continue
        if job.progress is not None:
            # The old process may have advanced the card past the saved sequence.
            adopt(job.progress, record.get("card_sequence"))
            bind_record(job.progress, ctx.state_dir, job_key(job.message_id, job.job_id))
        waiting.append((int(job.job_id), app, job))
    for _, app, job in sorted(waiting, key=lambda item: item[0]):
        app.runtime.enqueue_reply_job(job, app.harness.name)  # After the adopted runners: each topic still runs in order.
        kept.add(job.message_id)
        gateway.runtime.log(f"re-queued job=#{job.job_id} after restart")
    gateway.runtime.adopted_keys = kept
    return kept
