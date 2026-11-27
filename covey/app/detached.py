"""Run each task in its own process so a bridge restart or deploy does not stop it.

The bridge hands a dequeued job to a task runner (bin/covey_runner.py) in
its own session. The runner builds the same app from the bridge's arguments and
runs the whole job there: backend, task card, delivery. While it is alive, the
bridge forwards every click on that job's cards and every typed answer to its
questions to the runner, so one process writes the job's card and record. The
runner never starts a job itself: a continuation it needs is enqueued by the
bridge on request. A runner that ends before its job begins ran nothing, so the
bridge starts it once more before reporting the job, after asking again whether it
really ended and its job never began (relaunch_unstarted). A restarted bridge adopts
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
for the handoff that names it. A job handed off is kept in ``<state>/turn-ends/<job>.json`` until
a Bridge has run its end (``after_job``): the runner's own Bridge once it exits, else the next
Bridge's start (``end_stranded``), so a runner that exits while no Bridge watches it (a deploy's
or a restart's gap) still has its end run, once.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from covey.core import timing
from covey.core.state import read_json, write_json
from covey.app import butler_asks, idle_compaction, knowledge_collect, memory_collect, memory_learn, butler_relay, owner_send
from covey.core import turn_words
from covey.core.turn_words import interrupted_notice, retry_hint
from covey.app.task_card import bind_record
from covey.core.card_sequence import adopt
from covey.app import devices as device_turns
from covey.core.jobs import (DEVICE_LOST_STATUS, SIGTERM_STATUS, context_for_job, find_job_record,
                                      interrupted_fields, job_key, upsert_job_record)
from covey.core.deliveries import lost
from covey.core.statuses import RUNNING
from covey.core.run_dir import (DONE, EXIT, HANDOFF, INBOX, QUEUED, READ_PAUSES, ack, live_dirs,
                                         live_keys as _live_keys, queued_path, read_exit, read_steady,
                                         restart_blockers as _restart_blockers, run_dir, runner_alive as alive,
                                         runner_here as here, runner_info, take)
from covey.core.saved_results import run_began
from covey.app.policy_guard import requested
from covey.app.runner_ipc import remote_control, serve_requests, write_inbox
from covey.app.runner_spawn import RunnerAlive, compaction_launch, dump_job, load_job, start_runner

RELAUNCH_DELAY = 2.0
# What relaunch_unstarted found once a runner looked gone: alive after all (a read of its files failed for a moment,
# T740), so it is watched again; it ended before its job began and another runner has the job now; or neither, and
# finish_detached settles the job.
ALIVE, RELAUNCHED, ENDED = "alive", "relaunched", "ended"
WATCH_AGAIN = 0.5  # The pause before a runner found ALIVE is watched again.
GONE_AGAIN = 0.5  # A runner that looked gone is looked at once more this much later before its job is settled (_gone).
ENDS = "turn-ends"  # <state>/turn-ends/<job>.json: a job handed to a task runner whose end no Bridge has run yet.
ENDS_KEPT = 24 * 3600  # A runner that ended longer ago than this at a start has its end dropped, not run.


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


def end_path(state_dir: Path, key: str) -> Path:
    return Path(state_dir) / ENDS / queued_path(state_dir, key).name


def end_due(state_dir: Path, job, log) -> None:
    """``job`` is handed to a task runner: a Bridge runs its end once the runner has exited (``after_job``), this one
    in finish_detached or, should the runner exit while no Bridge watches it, the next one at its start
    (``end_stranded``). A write that fails leaves it to this Bridge alone, as before."""
    try:
        write_json(end_path(state_dir, job.message_id),
                   {"schema_version": 1, "key": job.message_id, "job_id": job.job_id, "at": time.time()},
                   ensure_ascii=False)
    except OSError as exc:
        log(f"turn end not kept job=#{job.job_id}: {type(exc).__name__}")


def end_done(state_dir: Path, key: str) -> None:
    """A Bridge has run the end of job ``key`` (or the policy guard owns it): no start runs it again."""
    try:
        end_path(state_dir, key).unlink(missing_ok=True)
    except OSError:
        pass  # The next start runs the end again; its parts keep records of what they did of a turn.


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
            started = True
            try:
                directory = job.detached_run_dir = start_runner(gateway.runtime, app.spec.name, job, ctx)
            except RunnerAlive as exc:
                # A live runner holds the job's run directory (its key) already: watched as an adopted one is, never
                # joined by a second, and its files and record left as they are.
                directory = job.detached_run_dir = exc.directory
                started = False
            except Exception as exc:  # noqa: BLE001 - no runner has the job: it ends here
                not_started(gateway, app, job, ctx, exc)
                return
        timing.mark("spawned")
        timing.end_current("job_start")  # Its line: from the scheduler taking the job to this spawn.
        end_due(ctx.state_dir, job, app.runtime.log)  # Outside the job's lock and its start's timing: one small write.
        pid = (runner_info(directory) or {}).get('pid')
        app.runtime.log(f"task runner started job=#{job.job_id} pid={pid} dir={directory}" if started else
                        f"task runner of job #{job.job_id} already alive pid={pid}: watched, not started again")
    progress, job.progress = job.progress, None  # The runner owns the card now; nothing here may write to it.
    if job.backend_control is None:
        job.backend_control = remote_control(app, ctx, directory, job.harness_capabilities)
    while watch_until_exit(gateway, app, job, ctx, directory, release=True):
        outcome = relaunch_unstarted(gateway, app, job, ctx, directory, progress)
        if outcome == ENDED:
            finish_detached(gateway, app, job, ctx, directory)
            return
        if outcome == ALIVE and gateway.runtime.shutdown.wait(WATCH_AGAIN):
            return  # The runner keeps going; the next bridge adopts it.


def watch_until_exit(gateway, app, job, ctx, directory: Path, *, release=False) -> bool:
    """Serve the requests of the runner in ``directory`` while it lives (runner_here: its runner.json, else its wake.json)
    and once more after: True once it has exited. False when the bridge stops (the runner keeps going; the next bridge
    adopts it) or, with ``release``, when its job is done and release_done has freed its topic while it uploads."""
    while here(directory):
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
            while watch_until_exit(gateway, app, job, ctx, directory):  # Else the next bridge adopts and releases it.
                if _gone(gateway, app, job, directory) == ENDED:
                    finish_detached(gateway, app, job, ctx, directory)
                    break
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


def unstarted(record, directory: Path, state_dir=None) -> bool:
    """The runner ended before its job began: the record is still queued and nothing says a runner began the job, in
    the run directory (exit.json, done.json) or, given ``state_dir``, in the saved results (the job's index, which a run
    of it writes: core/saved_results.py run_began). So no model call, worker card write or external task happened."""
    record = record or {}
    if record.get("status") != "queued" or any((Path(directory) / name).exists() for name in (EXIT, DONE)):
        return False
    return state_dir is None or not _began(state_dir, record.get("key"))


def _began(state_dir, key) -> bool:
    """run_began, asked again after each of READ_PAUSES while it says no: a look at the index as another process replaced
    it could miss it on ByteFuse, as a read of runner.json did (T740)."""
    for pause in READ_PAUSES:
        if run_began(state_dir, key):
            return True
        time.sleep(pause)
    return run_began(state_dir, key)


def relaunch_unstarted(gateway, app, job, ctx, directory: Path, progress) -> str:
    """The runner in ``directory`` looked gone (watch_until_exit): ALIVE when it is alive after all, RELAUNCHED when it
    ended before its job began and a new runner has the job, else ENDED (finish_detached settles the job). Asked once
    more after RELAUNCH_DELAY, under the job's lock, from what is there then: whether a runner is alive, the record and
    the run directory (unstarted), the inbox; a runner not started again is looked at once more first (_gone). A new
    runner's card continues past the sequence the record has then."""
    if here(directory):
        return _still_alive(app, job)
    if job.runner_relaunched or not unstarted(find_job_record(ctx.state_dir, job.message_id), directory, ctx.state_dir):
        return _gone(gateway, app, job, directory)
    def inbox_used():
        return any((Path(directory) / INBOX).glob("*.json"))
    if inbox_used():
        return _gone(gateway, app, job, directory)  # A click or answer went to the dead runner: finish_detached hands it back.
    stopping = gateway.runtime.shutdown.wait(RELAUNCH_DELAY)
    with job.execution_lock:
        # Asked again after the wait: the runner may have looked gone for a moment only, or begun its job meanwhile
        # (T740: a second runner then failed at the job's saved results, and its failure took the first one's reply
        # key); a new runner starts with an empty inbox.
        if here(directory):
            return _still_alive(app, job)
        record = find_job_record(ctx.state_dir, job.message_id)
        if stopping or job.cancel_event.is_set() or inbox_used() or not unstarted(record, directory, ctx.state_dir):
            return ENDED
        job.runner_relaunched = True  # Saved in the handoff: a later bridge does not start it a third time.
        if progress is not None:
            job.progress = adopt(progress, record.get("card_sequence"))
        try:
            start_runner(gateway.runtime, app.spec.name, job, ctx)
        except RunnerAlive:
            job.runner_relaunched = False  # None started: start_runner found one alive there.
            return _still_alive(app, job)
        except Exception as exc:  # noqa: BLE001 - finish_detached reports the job as not started
            app.runtime.log(f"task runner relaunch failed job=#{job.job_id}: {start_error(exc)}")
            return ENDED
        finally:
            job.progress = None
    app.runtime.log(f"task runner relaunched job=#{job.job_id} pid={(runner_info(directory) or {}).get('pid')}: "
            "the last one ended before the job started")
    return RELAUNCHED


def _gone(gateway, app, job, directory: Path) -> str:
    """ENDED when the runner in ``directory``, gone at the last look, is still gone GONE_AGAIN later, else ALIVE: one look
    whose reads all failed for a moment must not settle a live runner's job (finish_detached marks it interrupted, hands
    its inbox back and runs its end, T740)."""
    gateway.runtime.shutdown.wait(GONE_AGAIN)
    return _still_alive(app, job) if here(directory) else ENDED


def _still_alive(app, job) -> str:
    app.runtime.log(f"task runner of job #{job.job_id} still alive: watched again")
    return ALIVE


def finish_detached(gateway, app, job, ctx, directory: Path) -> None:
    if requested(ctx.state_dir, job.message_id):
        app.runtime.unregister_reply_job(job)
        queue_lost(app, job, ctx)
        collab_after(gateway, job, ctx)  # A 协作 turn the guard stopped: its record settles it.
        butler_relay.after_runner(gateway, job, ctx)
        app.controls.take_left_relay(ctx, job)  # A 做完了 its runner left waiting for others of its chat.
        end_done(ctx.state_dir, job.message_id)
        return  # The guard owns the stopped runner's final card and notice.
    try:
        record = find_job_record(ctx.state_dir, job.message_id)
        outcome = read_exit(directory)
        app.runtime.log(f"task runner ended job=#{job.job_id} status={(record or {}).get('status')} exit={outcome.get('status', 'none')}")
        if record and record.get("status") in RUNNING:
            start_failed = unstarted(record, directory, ctx.state_dir)
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
            # Nothing goes on by itself (no automatic retry, no live question): a turn the butler handed on says
            # 停住了 at the top level, which its runner never sent (app/butler_relay.py settle_missing).
            app.controls.relay_unfinished(context_for_job(ctx, job), job.message_id)
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
    after_job(gateway, app, job, ctx)
    app.controls.take_left_relay(ctx, job)  # A 做完了 its runner left waiting for others of its chat: sent now.
    end_done(ctx.state_dir, job.message_id)
    _compact_when_idle(gateway, app, job, ctx)


def after_job(gateway, app, job, ctx) -> None:
    """What the Bridge does once ``job``'s turn has ended and its record is settled, in this order: the records and
    messages of other places a turn may lead to, which rule 8 keeps from a task runner (a task process changes only its
    own task's card and record). ``finish_detached`` calls it once the runner has exited; app/job_dispatch.py after a
    turn run in the Bridge's own process (no task runners: a test app). Nothing in a task runner, which runs the turn
    through app/job_dispatch.py too: there it would hand the butler a turn it cannot queue (app/butler_asks.py), and its
    Bridge does all of it again once the runner has exited."""
    if getattr(gateway.runtime, "in_runner", False):
        return
    knowledge_collect.collect(gateway.runtime, job, ctx)  # The owner's 记一下 in a topic's turn (off without COVEY_KNOWLEDGE).
    memory_collect.collect(gateway.runtime, getattr(app, "messenger", None), job, ctx)  # The owner's 以后 / 别再 … (off without COVEY_MEMORY).
    memory_learn.after_turn(gateway, app, job, ctx)  # What the memory learns of the turn (off without COVEY_MEMORY_LEARN).
    owner_send.collect(gateway, job, ctx)  # The turn's send_as_owner drafts: 代发草稿 cards (off without a 代答 sender).
    butler_asks.collect(gateway, job, ctx)  # The turn's ask_butler: handed to the butler (off without COVEY_PROJECTS=1).
    collab_after(gateway, job, ctx)  # 协作: an ask settled, a continuation's end, a turn's collab_ask (COVEY_COLLAB=1).
    butler_relay.after_runner(gateway, job, ctx)  # A butler card's quote, a project's receipt, 到点回来看.


def end_stranded(gateway, ctx) -> None:
    """At start, after ``adopt_runners`` and the apps' stale jobs (a dead runner's unfinished record is interrupted by
    then, as finish_detached would first): the jobs whose task runner exited while no Bridge watched it have their end
    run now, once (``after_job``, from the job in its handoff). A live runner's waits for its exit (finish_detached);
    one of a Harness no longer in the roster, or of a runner that ended over ENDS_KEPT ago, is dropped unrun. Each is
    tried once: what fails is logged and dropped. Never raises."""
    folder = Path(ctx.state_dir) / ENDS
    try:
        paths = sorted(folder.glob("*.json")) if folder.is_dir() else []
        adopted = set(getattr(gateway.runtime, "adopted_keys", None) or ())
        running = adopted | live_keys(ctx.state_dir) if paths else set()
    except Exception as exc:  # noqa: BLE001 - the ends wait for the next start; this one goes on
        gateway.runtime.log(f"turn ends not run at start: {type(exc).__name__}")
        return
    now = time.time()
    for path in paths:
        saved = {}
        try:
            saved = read_json(path)
            saved = saved if isinstance(saved, dict) else {}
            key = str(saved["key"])
            if key in running:
                continue  # Its runner lives: its own end (finish_detached) comes once it exits.
            directory = run_dir(ctx.state_dir, key)
            handoff = read_steady(directory / HANDOFF)
            app = gateway.apps.get(handoff.get("harness"))
            ended = read_exit(directory).get("finished_at") or saved.get("at") or 0
            if app is None or now - float(ended) > ENDS_KEPT:
                why = "its Harness is not in the roster" if app is None else "its runner ended too long ago"
                gateway.runtime.log(f"turn end dropped job=#{saved.get('job_id')}: {why}")
            else:
                job = load_job(handoff["job"])
                after_job(gateway, app, job, app.harness.context)
                app.controls.take_left_relay(app.harness.context, job)  # A 做完了 left waiting when its runner exited.
                gateway.runtime.log(f"turn end run at start job=#{saved.get('job_id')}: "
                                    "its runner exited while no Bridge watched it")
        except Exception as exc:  # noqa: BLE001 - one turn's end never holds up the start or the others
            gateway.runtime.log(f"turn end not run at start job=#{saved.get('job_id', '?')}: {type(exc).__name__}")
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass  # Tried again at the next start; its parts keep records of what they did of a turn.


def collab_after(gateway, job, ctx) -> None:
    """协作's part of a runner's end (app/collab_flow.py Collaboration.after_runner, the Bridge's feature object
    ``collab``; none in a gateway without it). Never raises."""
    feature = getattr(gateway, "collab", None)
    if feature is not None:
        feature.after_runner(job, ctx)


def device_notice(record, job_id, directory) -> str:
    """The text a runner's end sends when no card can be written: the turn's own for a device's, else the usual one."""
    return device_turns.lost_notice(record, directory) or turn_words.device_interrupted(record) or interrupted_notice(job_id)


def queue_lost(app, job, ctx) -> None:
    """A butler turn's runner is done or gone: the owner's messages its turn never took, which the runner marked
    (app/steering.py requeue_lost), become turns of their own here; a runner never queues a job. A topic's turn
    the same, for the messages that went into it as they arrived (COVEY_TOPIC_STEER, core/deliveries.py topic_steer) and
    the words handed to it (core/deliveries.py handed, WAVE12 W2-B: whatever the switch says; Steering.queue_lost reads
    the ones that went in as they arrived only with it on)."""
    try:
        app.steering.queue_lost(context_for_job(ctx, job), job)
    except Exception as exc:  # noqa: BLE001 - those not tried stay marked: the Bridge's next start queues them
        app.runtime.log(f"lost supplements not queued job=#{job.job_id}: {type(exc).__name__}")


def queue_stranded(gateway, ctx, app) -> None:
    """At start, after ``adopt_runners``: the owner's messages a butler turn never took, marked by its runner or
    this Bridge (app/steering.py requeue_lost), whose turn ended with no Bridge to queue them (the runner exited
    while the Bridge was down, or the Bridge stopped before its ``queue_lost`` ran), become turns of their own now,
    once. An adopted or still live runner's wait for its end here (``release_done``, ``finish_detached``). ``app``:
    the butler's Harness app, None when this Bridge has none. With COVEY_TOPIC_STEER on, a topic's messages that
    went into its turn as they arrived too, each by its topic's Harness app, and, whatever the switch says, the words
    handed to a topic (core/deliveries.py handed); the records are read once for all."""
    others = [each for each in gateway.apps.values() if each is not app]
    if app is None and not others:
        return
    try:
        running = set(getattr(gateway.runtime, "adopted_keys", None) or ()) | live_keys(ctx.state_dir)
        found = lost(ctx.state_dir)
        for each in ([app] if app is not None else []) + others:
            each.steering.queue_stranded(each.harness.context, running, found=found, butler=each is app)
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
    """Watch every live runner again and re-queue waiting jobs; returns the keys kept running. Live as runner_here says:
    a runner whose runner.json names another process (one its Bridge started in the same moment, which ran nothing) is
    still found by its wake.json or its run lock."""
    kept = set()
    for directory in live_dirs(ctx.state_dir, here):
        try:
            handoff = read_steady(directory / HANDOFF)  # A read that fails leaves a live runner's job to be interrupted.
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
