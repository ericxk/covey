"""Ordinary topic-job execution and local records: ``process_bridge_reply_job`` runs a turn, settles how it ended
(app/turn_settle.py) and hands its result to ``TurnDelivery`` (app/turn_delivery.py: the task card, the topic, question
forms, the card's closing state)."""
from __future__ import annotations

from covey.core.trace import task_trace
from covey.core.turn_words import device_restarted, retry_hint

import datetime as _dt
import functools
import json
import subprocess
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

from covey.core.diff import capture_job_diff_snapshot
from covey.core.harness import JobCancelled, backend_capabilities, normalize_run_result
from covey.core.saved_results import reruns_after_withdrawal, observe_verified_reply
from covey.app import devices as device_turns
from covey.core.jobs import DEVICE_LOST_STATUS, RECALLED, SIGTERM_STATUS, context_for_job, job_key, topic_job_records
from covey.core.progress import turn_seconds
from covey.core import timing
from covey.core.metrics import mark, mark_first_output
from covey.core.longtask import SEED_PHASE, TaskMonitor
from covey.core.status_line import STARTING
from covey.cards.review import stopped_sentence
from covey.core.state import write_json
from covey.core.logs import append_private_text
from covey.core.butler import worded
from covey.app import auto_retry, butler_reactions, butler_relay, butler_reply, butler_tracks, policy_retry, project_lead, track_digest
from covey.app.turn_delivery import TurnDelivery, outgoing_markdown, show_outcome
from covey.app.turn_images import TurnImages
from covey.app.turn_settle import TurnResult, settle_turn

PROGRESS_START = "正在处理本轮消息"
STOPPED_NOTE = "已保存的结果和记录保留在本话题。"  # After "任务 #N 已停止。" (stopped_sentence).
RECALLED_NOTE = "你撤回了这条消息，任务随之停下。" + STOPPED_NOTE  # A stop by the recall (app/source_watch.py).


def format_received_progress_event(event: str, received_at: float) -> str:
    timestamp = _dt.datetime.fromtimestamp(received_at).astimezone().strftime("%m-%d %H:%M:%S")
    return f"{timestamp}：{event}"


class TurnProgress:
    """The progress feed of one turn: each event goes to the job record and job-progress.jsonl; the task
    card shows the turn's trace, not these lines."""

    def __init__(self, job, ctx, *, record_job_state):
        self.job, self.ctx, self.record_job_state = job, ctx, record_job_state

    def __call__(self, event: str, force: bool = False) -> None:
        if not str(event or "").strip():
            return
        event = outgoing_markdown(event, self.ctx)
        rendered_event = format_received_progress_event(event, time.time())
        append_private_text(self.ctx.history_path.parent / "job-progress.jsonl", json.dumps(
            {"job_id": self.job.job_id, "message_id": self.job.message_id, "event": rendered_event}, ensure_ascii=False) + "\n")
        self.record_job_state(self.ctx, self.job, "running", event)
        if self.job.progress and not self.job.progress_body_cleared:
            # The queued text leaves the body with the first status line write (Cards.update_card_activity),
            # in one call: the backend starts without waiting for it (S33: 3 s), and 执行中 does not
            # come a call later (S34: 2 s). A streamed answer replaces it first.
            self.job.progress_body_cleared = self.job.body_clear_pending = True


def _start_turn(ctx, job, *, sessions, runtime, record_job_state):
    """Mark the job started: the topic's native session pointer, a run id, an empty checkpoint and a
    trace (every backend reports structured events). Returns (revision, session_id) of the session."""
    runtime.log(f"starting reply job #{job.job_id} message_id={job.message_id}")
    timing.mark("started")
    record_job_state(ctx, job, "running", "后台 worker 已开始处理")
    revision, session_id = sessions.capture_session(ctx)
    # A project's lead keeps its session only as a cache (app/project_lead.py): rebuilt when it is old, big or on another model.
    revision, session_id = project_lead.settle_session(ctx, job, sessions, runtime, revision, session_id)
    job.session_id = session_id
    job.bridge_run_id = str(uuid.uuid4())
    job.verified_reply = None
    job.checkpoint_path = str(ctx.state_dir / "checkpoints" / (job_key(job.message_id, job.job_id) + ".json"))
    write_json(Path(job.checkpoint_path), {"phase":SEED_PHASE, "completed":[],
        "artifacts":[], "external_tasks":[], "next_step":"检查任务要求与已有状态", "requires_user":False}, ensure_ascii=False)
    # Without a trace until its first event, a status line could say only "<后端> 已开始处理", with no
    # 已运行 (S28 #849), and its progress text showed as a 执行进度 block.
    job.tool_trace = task_trace()
    job.tool_trace.checkpoint = job.checkpoint_path
    return revision, session_id


def _task_monitor(ctx, job, *, cards, runtime, record_job_state, send_text, prefix, starting=""):
    """The job's activity monitor (not started): the card's activity line, the job record, and the
    long-task questions it sends to the topic. ``starting``: the backend's name, when it says when
    its first model call starts (``model_requests``)."""
    # The images the task lists to show at once (app/turn_images.py), in the voice of its task card's writer.
    messenger = getattr(cards, "messenger", None)
    images = (TurnImages(ctx, job, messenger=messenger, voice=cards.voice, log=runtime.log)
              if messenger is not None and getattr(cards, "voice", None) is not None else None)
    return TaskMonitor(job, Path(job.checkpoint_path),
        lambda content: cards.update_card_activity(ctx, job, content),
        lambda **fields: record_job_state(ctx, job, "running", "任务执行中", **fields),
        lambda text, key: send_text(ctx, job.chat_id, text, f"{prefix}-question-{job.message_id}-{key}"),
        runtime.log, lambda: job.progress and cards.close_streaming_reply(ctx, job.progress),
        starting=STARTING.format(starting) if starting else "",
        state=lambda phase, asking: cards.show_running_summary(ctx, job, phase, asking),
        images=images, history=_earlier_turns(ctx, job, runtime.log),
        stuck=(lambda on: cards.show_stuck(ctx, job, on)) if callable(getattr(cards, "show_stuck", None)) else None)


def _earlier_turns(ctx, job, log):
    """The 用时 of the topic's earlier turns (core/progress.py turn_seconds), read once as the turn starts, for its
    estimate; none when they cannot be read."""
    if not getattr(job, "topic_id", ""):
        return []
    try:
        return turn_seconds(topic_job_records(ctx.state_dir, job.topic_id, chat_id=job.chat_id), exclude=job.job_id)
    except (OSError, ValueError) as exc:
        log(f"earlier turns of job #{job.job_id} not read: {type(exc).__name__}")
        return []


def _generate_reply(ctx, job, session_id, progress, *, runs, record_job_state):
    """Run the turn on the backend; its first output is recorded once, every event goes to ``progress``."""
    mark(job, 'backend_requested')
    timing.mark('backend')
    job.tool_trace.started_at = job.timings['backend_requested']
    record_job_state(ctx, job, 'running', '正在启动后端')
    def backend_progress(event, force=False):
        if mark_first_output(job):
            record_job_state(ctx, job, 'running', '后端已输出首条进展')
        progress(event, force)
    return normalize_run_result(runs.generate_reply_for_worker(ctx, job, session_id, backend_progress))


def finished_before(result) -> bool:
    """The backend completed the turn before a SIGTERM reached it: the result stands and is delivered."""
    return result.outcome is not None and result.outcome.status == "completed"


def leave_interrupted(ctx, job, *, harness, runtime, record_job_state) -> None:
    """A turn a SIGTERM ended before its backend finished (``job.interrupted``, app/runner.py): no outcome,
    result or notice. Its candidate reply is withdrawn, as when the model goes on after one, so 检查并继续
    may run the task again. The record stays running and the card as it is: once this process exits the
    Bridge marks the job interrupted and shows its recovery card, as for a runner that died
    (app/detached.py ``finish_detached``, or ``recover_stale_jobs`` when the next Bridge starts)."""
    if job.verified_reply is not None and reruns_after_withdrawal(harness.spec.name, job.native_question_deferred):
        try:
            observe_verified_reply(ctx, job, harness.spec.name, None)
        except (OSError, ValueError) as exc:
            runtime.log(f"candidate reply of interrupted job #{job.job_id} kept: {type(exc).__name__}")
    if device_turns.end_of(job) == "lost":
        # The device stayed away past the patience: this runner ends too, and finish_detached registers the reap.
        record_job_state(ctx, job, "running", DEVICE_LOST_STATUS, device_state=device_turns.state_of(job))
        runtime.log(f"reply job #{job.job_id} lost its device {ctx.device}; the Bridge marks it interrupted")
        return
    record_job_state(ctx, job, "running", SIGTERM_STATUS)
    runtime.log(f"reply job #{job.job_id} interrupted by SIGTERM; the Bridge marks it interrupted")


def process_bridge_reply_job(job: Any, ctx: Any, *, harness, runtime, messenger, cards, sessions, runs, artifacts,
                             questions, steering, record_job_state) -> None:
    """Run one queued job of ``harness`` and deliver its result.

    - ``runtime``: the log; ``messenger``: text replies in the Harness's voice.
    - ``sessions``: the topic's native session pointer before and after the turn.
    - ``runs``: the turn on the backend.
    - ``cards``: the job's task card; ``artifacts``: a result too long for it, sent as a file.
    - ``questions``: a checkpoint question delivered with the result.
    - ``steering``: the job's 补充 forms and action row, closed at its end.
    - ``record_job_state``: the Harness's job-state recorder.
    """

    def send_text(ctx, chat_id, text, key, card=None, on_card=None):
        return messenger.reply(ctx, chat_id, text, key, voice=harness.voice, card=card,
                               **({"on_card": on_card} if on_card is not None else {}))

    ctx = context_for_job(ctx, job)
    if job.kind != "chat":
        record_job_state(ctx, job, "cancelled", "旧的专用任务类型已移除，不再执行")
        return
    with job.execution_lock:
        if job.cancelled_before_start:
            return
        job.execution_started = True
    butler_reactions.begin(messenger, ctx.state_dir, ctx.profile, job, runtime.log)  # 排队中 off, OnIt on (多路并行).
    capabilities = backend_capabilities(getattr(harness, "backend", None), ctx)
    revision, session_id = _start_turn(ctx, job, sessions=sessions, runtime=runtime, record_job_state=record_job_state)
    timing.mark("turn")
    turn, snapshot = _run_turn(job, ctx, harness=harness, runtime=runtime, cards=cards, sessions=sessions, runs=runs,
                               record_job_state=record_job_state, send_text=send_text, capabilities=capabilities,
                               revision=revision, session_id=session_id)
    if turn.status != "waiting_user":  # A track of the butler forgets its session (app/butler_tracks.py).
        butler_tracks.release_pointer(ctx, job, harness, runtime)
    if turn.status == "interrupted":
        leave_interrupted(ctx, job, harness=harness, runtime=runtime, record_job_state=record_job_state)
        return
    _deliver_turn(job, ctx, turn, snapshot, harness=harness, runtime=runtime, messenger=messenger, cards=cards,
                  artifacts=artifacts, questions=questions, steering=steering, record_job_state=record_job_state,
                  send_text=send_text, capabilities=capabilities)
    runtime.log(f"finished reply job #{job.job_id}")


TRACE_CHARS = 4000  # Of a failed turn's traceback in the log: its last frames, where it was raised.


def failure_trace(exc, job) -> str:
    """One log entry of the exception ``exc`` that failed ``job``'s turn: its type, words and traceback, the end
    kept when long. The words of an exception never hold the turn's content here (a backend's errors name files
    and states), and the log stays on this machine."""
    text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).rstrip()
    if len(text) > TRACE_CHARS:
        text = "…" + text[-TRACE_CHARS:]
    return f"job #{job.job_id} failed with {type(exc).__name__}: {text}"


def _stop_note(job) -> str:
    """A paragraph for a stopped turn's text when the device has not confirmed the stop (its reap is pending), else ''."""
    note = device_turns.stop_note(job)
    return f"\n\n{note}" if note else ""


def _others_note(job) -> str:
    """``还有 N 件事在做。`` after a stopped turn's text when its stop counted other tracks of the butler's still going
    (app/task_controls.py ``cancel_reply_job_from_card``: the 停止 button, the 派发卡's, the stop word), else ''."""
    others = getattr(job, "stop_others", 0)
    return f"还有 {others} 件事在做。" if isinstance(others, int) and others > 0 else ""


def _turn_from_exception(exc, job, settling) -> dict:
    """The TurnResult fields of a turn the exception ``exc`` ended (its native session is never resumed then):
    a timeout fails it; a SIGTERM before the backend finished (``settling`` False) leaves it interrupted, whatever
    the backend raised as it stopped; a stop cancels it, before the backend finished whatever it raised; anything else
    fails it, its type named."""
    if isinstance(exc, subprocess.TimeoutExpired):
        return {"status": "failed", "status_message": "任务超时",
                "final_text": worded(f"任务 #{job.job_id} 超时未完成，可在本话题{retry_hint()} 重试。", job)}
    if job.interrupted and not settling:
        return {"status": "interrupted"}
    if isinstance(exc, device_turns.DeviceLost):
        return {"status": "interrupted"}
    if isinstance(exc, device_turns.DeviceGone):
        text = device_restarted(str(exc))
        return {"status": "failed", "status_message": text, "final_text": worded(f"任务 #{job.job_id}：{text}", job)}
    stopped = getattr(job, "cancel_event", None) is not None and job.cancel_event.is_set()
    if isinstance(exc, JobCancelled) or (stopped and not settling):
        # A stop before the backend finished cancels the turn whatever the backend raised as it stopped (its process
        # may have been stopped from outside, app/task_controls.py hard_stop_later). Like a finished card since P1: no "直接回复本话题即可继续" (repo review U10).
        if getattr(job, "cancel_requested_by", "") == RECALLED:
            return {"status": "cancelled", "status_message": "原消息已撤回，任务已停止",
                    "final_text": stopped_sentence(job.job_id) + worded(RECALLED_NOTE, job) + _stop_note(job)}
        return {"status": "cancelled", "status_message": "用户停止了任务",
                "final_text": stopped_sentence(job.job_id) + worded(STOPPED_NOTE, job) + _others_note(job) + _stop_note(job)}
    name = type(exc).__name__
    return {"status": "failed", "status_message": "任务异常（" + name + "）",
            "final_text": f"任务 #{job.job_id} 处理出错（{name}），未满足结果发布条件。"}


def _run_turn(job, ctx, *, harness, runtime, cards, sessions, runs, record_job_state, send_text, capabilities,
              revision, session_id):
    """The turn on the backend, settled (``settle_turn``) or ended by an exception (``_turn_from_exception``), its
    native session protected unless settling did it and its activity monitor stopped. (turn, the workspace's diff
    snapshot from before it, None when it did not get that far)."""
    prefix = harness.spec.idempotency_prefix
    progress = TurnProgress(job, ctx, record_job_state=record_job_state)
    turn = TurnResult()
    snapshot = None
    activity = None
    settling = False  # The backend finished: a SIGTERM no longer interrupts the turn (``finished_before``).
    try:
        if job.cancel_event.is_set():
            raise JobCancelled("任务已在执行前停止")
        progress(PROGRESS_START, True)
        timing.mark("progress")
        activity = _task_monitor(ctx, job, cards=cards, runtime=runtime, record_job_state=record_job_state,
                                 send_text=send_text, prefix=prefix,
                                 starting=harness.spec.prompt_agent_name if capabilities.model_requests else "")
        job.activity_monitor = activity
        activity.start()
        timing.mark("monitor")
        # After the monitor starts: 执行中 does not wait for git (0.2-0.35 s, S34); the backend does.
        # A device's directory is not on this machine: git here would answer "no changes" (also skips finalize_job_diff).
        snapshot = None if getattr(ctx, "device", "") else capture_job_diff_snapshot(ctx.workdir)
        timing.mark("snapshot")
        result = _generate_reply(ctx, job, session_id, progress, runs=runs, record_job_state=record_job_state)
        if job.interrupted and not finished_before(result):
            raise JobCancelled("任务进程收到 SIGTERM")
        device_turns.verdict(job)  # A device that stayed away or lost the task: interrupted, or failed (DeviceLost, DeviceGone).
        settling = True
        settle_turn(turn, result, ctx, job, sessions=sessions, session_id=session_id, revision=revision,
                    runtime=runtime, prefix=prefix)
        if job.cancel_event.is_set() and turn.final_text:
            turn.final_text += _stop_note(job)  # A stop the device has not confirmed yet (its reap is pending).
    except Exception as exc:  # noqa: BLE001 - isolate backend and delivery failures per job.
        turn.allow_resume = False
        for name, value in _turn_from_exception(exc, job, settling).items():
            setattr(turn, name, value)
        if not isinstance(exc, (subprocess.TimeoutExpired, JobCancelled)):
            # A backend stopped under a SIGTERM may fail in its own way.
            runtime.log(f"interrupted job #{job.job_id} ended in {type(exc).__name__}" if turn.status == "interrupted"
                        else turn.status_message)
            # Where it came from: T480/T481 (2026-11-11) failed at their start with only "任务异常（RuntimeError）".
            runtime.log(failure_trace(exc, job))
    finally:
        if not turn.session_finalized:
            try:
                sessions.finalize_session_from_worker(ctx, job, session_id, job.session_id, revision, allow_resume=False)
            except Exception as exc:
                runtime.log("session protection failed: " + type(exc).__name__)
        if activity is not None:
            activity.stop()
    return turn, snapshot


def _deliver_turn(job, ctx, turn, snapshot, *, harness, runtime, messenger, cards, artifacts, questions, steering,
                  record_job_state, send_text, capabilities):
    """The ended turn's outcome atop its card, its automatic retry planned, its result delivered (TurnDelivery)
    and, once it is out, the supplements the backend never read marked so."""
    # The outcome replaces 执行中 at the top at once; preparing the result (a diff takes seconds) left
    # the top blank until the answer appeared lower down. The execution ended here: the status line's 用时 is the
    # answer card's and the relay's (core/metrics.py run_seconds; TurnDelivery.deliver's mark keeps this one).
    mark(job, 'execution_finished')
    show_outcome(cards, ctx, job, turn.status, runtime)

    retry = auto_retry.plan(harness, ctx, job, turn)
    promised = None
    try:
        # A refusal of cyber or bio: one retry on an older model, said in the stopped turn's text (app/policy_retry.py).
        promised = policy_retry.at_stop(ctx, job, turn)
    except (OSError, ValueError) as exc:
        runtime.log(f"policy retry of job #{job.job_id} not scheduled: {type(exc).__name__}")
    delivery = TurnDelivery(job, ctx, turn, prefix=harness.spec.idempotency_prefix, cards=cards, runtime=runtime,
                            artifacts=artifacts, questions=questions, steering=steering,
                            record_job_state=record_job_state, send_text=send_text, retry=retry,
                            relay=functools.partial(butler_relay.after_turn, messenger=messenger, profile=ctx.profile,
                                                    voice=harness.voice),
                            react=functools.partial(butler_reactions.finish, messenger, ctx.profile, log=runtime.log),
                            ledger=track_digest.record, retry_promised=promised is not None)
    try:
        delivery.deliver(snapshot)
        butler_reply.react_end(delivery)  # A quiet butler turn: OnIt on the owner's message becomes its end (D1).
    finally:
        job.supplements_listed = delivery.listed  # Those still held at its end too (settle_held_at_end).
        if capabilities.steer_reads:  # After the result: the scan never holds it back.
            try:
                # The turn is over: Claude reads nothing more. Supplements its card listed get no line of their own.
                steering.end_unread_steers(ctx, job, listed=delivery.listed)
            except Exception as exc:  # noqa: BLE001 - only the receipts stay as they were
                runtime.log(f"unread supplements not marked: {type(exc).__name__}")
