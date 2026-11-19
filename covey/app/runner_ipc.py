"""Messages between the Bridge and a job's task runner through its run directory (file names: core/run_dir.py).

Bridge side: write_inbox and wake_runner (the runner reads its inbox now), RemoteControl (steering a job
in a runner), forward_card_action and forward_answer (clicks and typed answers go to the runner whole),
serve_requests (continuations a runner asks the Bridge to queue). Runner side: accept_wake, await_wake and
release_wake, RunnerLink (Runtime.runner inside a runner) and take_inbox."""
from __future__ import annotations

import dataclasses
import datetime as _dt
import os
import re
import select
import signal
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from covey.core import timing
from covey.core.state import read_json, write_json
from covey.app.card_actions import BOT_ONLY, click_kind
from covey.core.jobs import context_for_job
from covey.core.process import process_identity
from covey.core.run_dir import (ACKS, INBOX, REPLIES, REQUESTS, WAKE, ack, await_reply, op_time, post, run_dir,
                                         runner_alive as alive, take)
from covey.core.steer import SteerNotReady, SteerRefused, SteerUncertain
from covey.app.launch import SUBMIT_PREFIX
from covey.app.policy_guard import requested
from covey.app.topics import topic_target
from covey.core.store import directory_store
from covey.core.topics import load_topic

ACK_TIMEOUT = 8.0
ANSWER_TIMEOUT = 45.0  # Covers a continuation the runner asks the bridge to enqueue.
ENQUEUE_TIMEOUT = 30.0
# The bridge's signal that a runner's inbox has something (wake_runner): read now, not at the next look.
WAKE_SIGNAL = signal.SIGUSR1


def write_inbox(directory: Path, payload: dict) -> str:
    op = post(Path(directory) / INBOX, payload)
    timing.mark("inbox")
    wake_runner(directory)
    timing.mark("wake")
    return op


def accept_wake(directory: Path):
    """In a task runner's main thread: the bridge's WAKE_SIGNAL makes the returned fd readable at once,
    on whichever thread the signal lands (the interpreter's wake-up fd, written by its C handler), and
    wake.json names this process, so the bridge sends the signal only here: it ends a runner of an
    older release, which has no handler. None when this process cannot take it (it polls its inbox)."""
    if threading.current_thread() is not threading.main_thread():
        return None
    read, write = os.pipe()
    installed = False
    try:
        os.set_blocking(read, False)
        os.set_blocking(write, False)
        signal.signal(WAKE_SIGNAL, lambda *_: None)
        signal.set_wakeup_fd(write, warn_on_full_buffer=False)
        installed = True
        identity = process_identity(os.getpid())
        if identity is None:
            raise OSError("process identity unavailable")
        write_json(Path(directory) / WAKE, identity)
    except (OSError, ValueError):
        if installed:
            signal.set_wakeup_fd(-1)
        os.close(read)
        os.close(write)
        return None
    return read


def await_wake(fd, timeout: float) -> None:
    """Until ``fd`` (accept_wake) is readable or ``timeout`` passes; what woke it is read away."""
    try:
        if select.select([fd], [], [], timeout)[0]:
            os.read(fd, 512)
    except (OSError, ValueError):
        time.sleep(timeout)


def release_wake(fd) -> None:
    """Undo accept_wake once nothing waits on ``fd`` any more."""
    if fd is None:
        return
    try:
        previous = signal.set_wakeup_fd(-1)
        if previous != -1:
            os.close(previous)
    except (OSError, ValueError):
        pass
    try:
        os.close(fd)
    except OSError:
        pass


def wake_runner(directory: Path) -> bool:
    """Have the runner read its inbox now, not at its next look (every 0.3 s: 0.25 s a forwarded
    click, S37). Only a runner whose wake.json names it alive, and through a pidfd opened before that
    check, so a pid reused meanwhile is never signalled. Whether the signal went."""
    try:
        named = read_json(Path(directory) / WAKE)
        pid = int(named["pid"])
        fd = os.pidfd_open(pid)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False
    try:
        if process_identity(pid) != named:
            return False
        signal.pidfd_send_signal(fd, WAKE_SIGNAL)
        return True
    except (OSError, AttributeError):
        return False
    finally:
        os.close(fd)


class RemoteControl:
    """The bridge's handle on a job in a task runner. Only steering is relayed;
    card clicks and typed answers on that job are forwarded to the runner whole."""
    pause_for_question = ""
    user_requests = frozenset()
    remote = True

    def __init__(self, directory: Path, supports_steer: bool):
        self.directory, self.supports_steer = Path(directory), supports_steer

    def steer(self, text, event_id):
        op = write_inbox(self.directory, {"kind": "steer", "text": text, "event_id": event_id})
        timing.mark("forward")
        result = await_reply(self.directory / ACKS / (op + ".json"), time.monotonic() + ACK_TIMEOUT)
        timing.mark("acked")
        if result is None:
            # The runner may still take it: its acknowledgement stays in acks/ for a later check.
            raise SteerUncertain("补充已转交任务进程，暂未确认送达；不会重复发送", op=op, run_dir=self.directory)
        if result.get("ok") and result.get("held"):
            return "held"  # The runner sends it once its task can take it.
        if not result.get("ok"):
            error = result.get("error") or "补充未送达当前任务"
            # compat(b20cfed): runners from before "refused" existed reported a refusal as its message, a failure as
            # its type; retire when no task runner before b20cfed runs (compat_report: steer-refused)
            refused = result.get("refused", not re.fullmatch(r"[A-Z]\w*(Error|Exception)", error))
            raise SteerRefused(error) if refused else SteerUncertain(error)

    def _card_only(self, *args, **kwargs):
        raise ValueError("该任务在独立进程中运行，请在任务卡上操作")

    answer = reject_question = wait_for_answer = _card_only

    def close(self):
        return None


def remote_control(app, ctx, directory: Path, caps=None) -> RemoteControl:
    """The bridge's handle on the job of the runner in ``directory``; it steers when the job's Harness
    does (``caps``, else its backend's capabilities in ``ctx``)."""
    from covey.core.harness import backend_capabilities
    caps = caps or backend_capabilities(app.backend, ctx)
    return RemoteControl(directory, bool(getattr(caps, "steering", False)))


def _question_job_id(state_dir: Path, token) -> int | None:
    if not isinstance(token, str) or len(token) != 16 or any(c not in "0123456789abcdef" for c in token):
        return None
    try:
        return read_json(Path(state_dir) / "questions" / (token + ".json")).get("job_id")
    except (OSError, ValueError):
        return None


def runner_for(runtime, state_dir: Path, *, job_id=None, key="") -> Path | None:
    """The live runner directory of a job, waiting out a handoff in progress."""
    job = None
    if job_id is not None:
        try:
            job = runtime.jobs.find(int(job_id), key)
        except (TypeError, ValueError):
            job = None
    elif key:
        with runtime.jobs.lock:
            job = runtime.jobs.by_message_id.get(key)
    if job is not None:
        # The scheduler hands a job off under this lock: once it is free, the runner exists or never will.
        with job.execution_lock:
            directory = job.detached_run_dir
        key = job.message_id
        if directory is not None:
            return directory if alive(directory) else None
    if not key:
        return None
    directory = run_dir(state_dir, key)
    return directory if alive(directory) else None  # A runner this bridge did not adopt.


def _forward_to_live(runtime, target, event, job_id) -> bool:
    """Send the click to the runner of job ``job_id`` when this Bridge runs it for the clicked topic: no read of the
    jobs records, whose shared scan a click once waited 149 s behind on a stalled NFS read (wait=jobs:149419). True
    when sent; False leaves the click to the records, as before."""
    jobs = getattr(runtime, "jobs", None)
    if job_id is None or jobs is None:
        return False
    try:
        job = jobs.find(int(job_id))
    except (TypeError, ValueError):
        return False
    if job is None or job.topic_id != target.topic_id or job.chat_id != event.get("chat_id", "") or not job.message_id:
        return False
    directory = runner_for(runtime, target.state_dir, job_id=job_id, key=job.message_id)
    timing.mark("fwd_live")
    if directory is None:
        return False
    write_inbox(directory, {"kind": "card_event", "event": event})
    return True


def forward_card_action(runtime, target, event: dict, raw: dict) -> bool:
    """Send a click on a job whose task runner is alive to that runner; True when sent."""
    if runtime.in_runner:
        return False
    if raw.get("kind") in BOT_ONLY or str(event.get("action_name") or "").startswith(SUBMIT_PREFIX):
        return False
    if raw.get("kind") == "bridge_question" and raw.get("mode") in {"feedback", "feedback_submit"}:
        return False  # Interpret feedback here, including for runners from an older release.
    job_id = raw.get("job_id")
    if str(raw.get("kind") or "").startswith("bridge_question"):
        job_id = _question_job_id(target.state_dir, raw.get("token")) or job_id
    if _forward_to_live(runtime, target, event, job_id):
        return True
    records = [r for _, r in directory_store(target.state_dir / "jobs").query(
        topic_id=target.topic_id, chat_id=event.get("chat_id", ""))]
    timing.mark("fwd_records")
    if job_id is not None:
        matches = [r for r in records if str(r.get("job_id")) == str(job_id)]
    else:
        matches = [r for r in records if r.get("card_message_id") == event.get("message_id")]
    for record in matches:
        if not record.get("key"):
            continue
        directory = runner_for(runtime, target.state_dir, job_id=record.get("job_id"), key=str(record["key"]))
        timing.mark("fwd_runner")
        if directory is not None:
            write_inbox(directory, {"kind": "card_event", "event": event})
            return True
    return False


def forward_answer(runtime, ctx, chat, token, fields, event_id) -> bool:
    """Hand a typed answer to the runner that asked the question; False when the bridge answers it."""
    if runtime.in_runner:
        return False
    job_id = _question_job_id(ctx.state_dir, token)
    if job_id is None:
        return False
    directory = runner_for(runtime, ctx.state_dir, job_id=job_id)
    timing.mark("fwd_runner")
    if directory is None:
        return False
    op = write_inbox(directory, {"kind": "answer", "chat_id": chat, "token": token, "fields": fields,
                                  "event_id": event_id})
    timing.mark("forward")
    result = await_reply(directory / ACKS / (op + ".json"), time.monotonic() + ANSWER_TIMEOUT)
    timing.mark("acked")
    if result is None:
        raise ValueError("回答已转交当前任务，暂未确认送达；请先查看回答状态，不会重复发送")
    if result.get("local"):
        return False  # The runner had no part in it yet: answer here.
    if not result.get("ok"):
        raise ValueError(result.get("error") or "回答未送达当前任务")
    return True


def serve_requests(gateway, app, job, ctx, directory: Path) -> None:
    """Continuations a runner needs: the bridge's queue runs every job."""
    if requested(ctx.state_dir, job.message_id):
        return  # An external policy stop owns this task; never enqueue its continuations.
    for payload in take(Path(directory) / REQUESTS):
        if payload.get("kind") != "enqueue_answer":
            continue
        if time.time() > payload.get("deadline", 0) - 2:
            continue  # The runner gave up waiting; it reports the answer as unconfirmed.
        try:
            topic = load_topic(Path(ctx.state_dir), payload["topic_id"])
            owner = gateway.apps[payload["harness"]]
            target = topic_target(owner, payload["chat_id"], topic)
            # A runner sends none when its job runs the default mode (RunnerLink.enqueue), as one from before work
            # modes (1bb6a08) always did.
            target = dataclasses.replace(target, model=payload["model"], effort=payload["effort"],
                                         work_mode=str(payload.get("work_mode") or ""))
            # A retry's continuation keeps its mark (app/policy_retry.py); a runner from before it sends none.
            retried = payload.get("policy_retry") if isinstance(payload.get("policy_retry"), dict) else None
            continuation = owner.submission.enqueue_chat_job(target, payload.get("display") or payload["text"],
                                                             payload["chat_id"], payload["message_id"],
                                                             prompt_content=payload["text"],
                                                             recovery_context=payload.get("recovery_context"),
                                                             title=str(payload.get("title") or ""),
                                                             policy_retry=retried)
            if continuation is None:
                raise ValueError("后续任务未能入队")
            ack(directory, payload["id"], True, folder=REPLIES, job_id=continuation.job_id)
        except Exception as exc:  # noqa: BLE001 - the runner reports the failure on the question
            gateway.runtime.log(f"continuation for task runner failed: {type(exc).__name__}")
            ack(directory, payload["id"], False, str(exc) if isinstance(exc, ValueError) else type(exc).__name__,
                folder=REPLIES)


# Runner side ---------------------------------------------------------------

class RunnerLink:
    """A task runner's link to its Bridge through the run directory; Runtime.runner inside a runner.

    A runner never queues a job itself: a continuation (an answer's follow-up) is queued by
    the Bridge, which replies with its number."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)

    def enqueue(self, harness, ctx, text, chat, message_id, recovery_context, title="", display="", policy_retry=None):
        deadline = time.time() + ENQUEUE_TIMEOUT
        op = post(self.directory / REQUESTS, {
            "kind": "enqueue_answer", "harness": harness, "chat_id": chat, "topic_id": ctx.topic_id,
            "message_id": message_id, "text": text, "model": ctx.model, "effort": ctx.effort,
            **({"work_mode": ctx.work_mode} if ctx.work_mode else {}),
            # Only on a retry's continuation: a Bridge from before it ignores it (serve_requests reads what it knows).
            **({"policy_retry": dict(policy_retry)} if policy_retry else {}),
            "recovery_context": recovery_context, "deadline": deadline, "title": title, "display": display})
        result = await_reply(self.directory / REPLIES / (op + ".json"), time.monotonic() + ENQUEUE_TIMEOUT + 3)
        if result is None:
            raise RuntimeError("后续任务入队未确认")
        if not result.get("ok"):
            raise ValueError(result.get("error") or "后续任务未能入队")
        return SimpleNamespace(job_id=result["job_id"])


def _inbox_flow(payload: dict, job):
    """The timing flow (core/timing.py) of a click, supplement or answer the bridge forwarded, from
    this runner taking it; ``inbox_ms`` is how long it waited since the bridge wrote it."""
    kind = payload.get("kind")
    if kind not in {"card_event", "steer", "answer"}:
        return None
    event = payload.get("event") if kind == "card_event" and isinstance(payload.get("event"), dict) else {}
    flow = timing.begin("click" if kind == "card_event" else kind, kind=click_kind(event) if event else None,
                        event=str(event.get("event_id") or payload.get("event_id") or "")[-10:],
                        message=str(event.get("message_id") or "")[-10:], runner=f"#{job.job_id}")
    if flow is not None:
        written = op_time(payload.get("id"))
        flow.note(inbox_ms=round(flow.wall * 1000 - written / 1e6) if written is not None else None)
    return flow


def take_inbox(gateway, app, job, ctx, directory: Path) -> None:
    job_ctx = context_for_job(app.harness.context, job)
    for payload in take(Path(directory) / INBOX):
        kind = payload.get("kind")
        flow = _inbox_flow(payload, job)
        try:
            if kind == "card_event":
                gateway.handle_card_action_event(payload["event"], ctx)
                timing.mark("ack")
            elif kind == "steer":
                if app.runtime.jobs.find(job.job_id) is not job:
                    raise SteerRefused("当前任务已结束，补充没有发出")
                control = job.backend_control
                if app.steering.steer_stage(job) == "paused":
                    raise SteerRefused("当前任务正在等待回答问题，请先回答")
                try:
                    if control is None:
                        raise SteerNotReady("任务刚开始，后端还没准备好接收补充")
                    control.steer(payload["text"], payload["event_id"])
                except SteerNotReady as exc:
                    # Kept here and sent once the task can take it; the bridge shows it as saved.
                    app.steering.hold_steer(job_ctx, job, payload["text"], payload["event_id"])
                    ack(directory, payload["id"], True, held=True, error=str(exc))
                    continue
                ack(directory, payload["id"], True)
                try:
                    app.steering.note_latest_request(job_ctx, job, payload["text"], payload["event_id"])
                except Exception as exc:  # noqa: BLE001 - the supplement is delivered; only the card note failed
                    gateway.runtime.log(f"latest supplement not shown: {type(exc).__name__}")
                # Its record says "sent" once the Bridge reads this ack: the watch starts from the record it finds.
                # It never raises (app/steer_interrupt.py): the acknowledgement above stands.
                watch = getattr(app.steering, "watch_unread_safely", None)
                if callable(watch):
                    watch(job_ctx, job, payload["event_id"])
            elif kind == "answer":
                app.questions.submit_answer(job_ctx, payload["chat_id"], payload["token"], payload["fields"], payload["event_id"])
                ack(directory, payload["id"], True)
            elif kind == "bridge_restarted" and job.tool_trace is not None:
                if app.runtime.jobs.find(job.job_id) is job:
                    at = _dt.datetime.fromtimestamp(payload["at"]).astimezone().strftime("%H:%M")
                    app.interaction.structured_harness_event(job_ctx, job, "progress_note", {
                        "id": f"bridge-restart-{int(payload['at'])}", "title": "机器人",
                        "text": f"机器人 {at} 重启过，任务未中断。"}, None, from_backend=False)
        except Exception as exc:  # noqa: BLE001 - one bad request never stops the task
            gateway.runtime.log(f"runner inbox {kind} failed: {type(exc).__name__}")
            if kind in {"steer", "answer"}:
                ack(directory, payload["id"], False, str(exc) if isinstance(exc, ValueError) else type(exc).__name__,
                    refused=isinstance(exc, SteerRefused))
        finally:
            timing.end(flow)
