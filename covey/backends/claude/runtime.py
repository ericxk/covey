"""One turn of the Claude CLI over its JSONL transport, following the official Agent SDK protocol (run_stream):
start it, send the prompt, read its rows until the turn settles, close it; and the idle compaction (compact_only).

Control requests and user inputs are written once. This module never replays a
write after a timeout, resumes an interrupted session, or reads historical text.
The process and its transport are backends/claude/process.py, the control side
backends/claude/control.py, the argv and environment backends/claude/argv.py.
"""

from __future__ import annotations

import subprocess
import time
import uuid

from covey.backends.claude import redaction
from covey.backends.claude.argv import stream_argv, subagents_dir, turn_env
from covey.backends.claude.compaction import compact_before_turn
from covey.backends.claude.completion import ClaudeReplyState, result_failure
from covey.backends.claude.control import ClaudeControl
from covey.backends.claude.events import ClaudeEventAdapter
from covey.backends.claude.process import (ClaudeProcess, IMAGE_FRAME, failure_event, save_transport_details, transport_error,
                                                    transport_failure_details)
from covey.core.attachments import MAX_IMAGE_BYTES, read_images
from covey.core.completion import CompletionBudget
from covey.core.harness import JobCancelled

# Seconds the CLI has to exit once its output ends; still running then, the run failed (process_exit).
EXIT_AFTER_EOF = 3


def image_content(attachments, prompt):
    content = [{"type": "text", "text": prompt}]
    total = len(prompt.encode())
    for mime, encoded in read_images(attachments, limit=MAX_IMAGE_BYTES):
        total += len(encoded)
        if total > IMAGE_FRAME:
            raise ValueError("Claude attachments exceed request size limit")
        content.append({"type": "image", "source": {"type": "base64", "media_type": mime, "data": encoded}})
    return content


def accept_row(row, state, control, adapter, waiting, process, replied, closing_deadline):
    """One row of the root session under the control lock: state, controls and trace take it; a
    settled or failed root closes input. (replied, closing_deadline) as they are after it."""
    with control.control_lock:
        accepted = state.accept(row)
        if state.identity_error:
            raise ValueError("Claude session identity mismatch")
        if state.policy_event:
            # Do not let a later permission, tool, final answer or idle event undo this stop.
            control.accepting_steer = False
            control.closing = True
            process.end_input()
            return replied, closing_deadline
        if accepted and row.get("type") == "user" and state.acknowledged and closing_deadline is None:
            control.accepting_steer = True
        control.accept(row)
        if accepted:
            adapter.accept(row)
        elif row.get("parent_tool_use_id") is not None and row.get("session_id") == state.session_id:
            adapter.accept(row)
        if state.result is not None and state.result is not replied:
            replied = state.result
            waiting.renew()
        if control.control_error:
            raise transport_error("input_delivery_uncertain")
        failed = state.result is not None and result_failure(state.result)[0] != "completed"
        if closing_deadline is not None and not control.closing and not failed:
            closing_deadline = None  # The CLI took up the queued supplement (its replay dropped the result).
        if closing_deadline is None and (
            failed or (state.settled() and not control.user_requests and not control.answered)
        ):
            if failed and set(state.inputs) != state.acknowledged:
                # A supplement sent before the turn failed waits in the CLI, which starts a turn for it
                # right after; closing input now cut that turn off 10 s later as a settle_timeout (task
                # #1556). Input stays open for the settle wait; a replay in it lets the turn run on.
                return replied, time.monotonic() + 10
            if not failed:
                state.capture()
            control.accepting_steer = False
            control.closing = True
            process.end_input()
            closing_deadline = time.monotonic() + 10
    return replied, closing_deadline


def settle_failure(state, control, keep_last_reply):
    """Input is closed and the CLI has not exited within the settle wait. (failure, stopped) for
    ClaudeReplyState.finish: a failed root result keeps its own class (an API error such as a rate
    limit); a settled complete one is the turn's answer, and the CLI is only stopped; a complete
    reply with only background tasks left is kept, sent with the settle notice; with no result,
    settle_timeout."""
    with control.control_lock:
        if state.result is not None:
            status, code = result_failure(state.result)
            if status != "completed":
                return (status, code), False
            if state.settled() and not control.user_requests and not control.answered:
                state.capture()
                return None, True
    keep_last_reply()
    return ("timed_out", "settle_timeout"), False


def read_rows(process, state, control, adapter, waiting, flush_events, keep_last_reply):
    """Read the CLI's rows until its output ends, the settle wait after the root result runs out
    or the completion budget expires: (rc, failure, stopped). Stops, timeouts and stream or
    transport errors raise to run_stream."""
    rc, failure, stopped = None, None, False
    replied = closing_deadline = None
    while True:
        if control.control_error:
            raise transport_error("input_delivery_uncertain")
        row = process.receive()
        if row is not None:
            kind = row.get("type")
            if kind == "bridge_error":
                raise ValueError("Invalid Claude JSON stream")
            if kind == "bridge_eof":
                try:
                    rc = process.process.wait(timeout=EXIT_AFTER_EOF)
                except subprocess.TimeoutExpired:
                    # Output ended but the CLI did not exit: not a task timeout. close() reaps it.
                    failure = ("failed", "process_exit")
                break
            replied, closing_deadline = accept_row(row, state, control, adapter, waiting, process, replied,
                                                   closing_deadline)
            if state.policy_event:
                failure = ("cancelled", "policy_" + state.policy_event["kind"])
                process.policy_stop = True
                break  # close_stream stops this task's private process group before UI delivery.
            flush_events()
        if closing_deadline is not None and time.monotonic() >= closing_deadline:
            failure, stopped = settle_failure(state, control, keep_last_reply)
            break
        if waiting.expired():
            failure = ("timed_out", "background_timeout" if keep_last_reply() else "settle_timeout")
            break
    return rc, failure, stopped


def close_stream(request, process, control, emit, flush_events, failure, rc):
    """Stop steering and close the questions, then close the CLI: (failure, cleanup, rc)."""
    cleanup = True
    if control is not None:
        with control.control_lock:
            control.accepting_steer = False
            control.closing = True
        try:
            emit("cancel_questions", {})
            flush_events()
        except Exception:
            failure = failure or ("failed", "storage_failed")
    if process is not None:
        try:
            cleanup = process.close()
            rc = process.process.poll() if rc is None else rc
        except (OSError, subprocess.TimeoutExpired):
            cleanup = False
    request.observers.control(None)
    return failure, cleanup, rc


def settle_subtasks(adapter, ctx, session_id, env):
    """Subagent usage from the session's transcripts, read once the CLI has exited; display-only."""
    try:
        directory = None
        if session_id and adapter.usage.start is not None and (adapter.subtasks.messages or adapter.subtasks.calls):
            directory = subagents_dir(ctx, session_id, env)
        adapter.subtasks.settle(directory, adapter.usage.start)
    except Exception:
        return


class _Uncounted:
    """No turn's usage: an idle compaction's request is counted nowhere."""

    @staticmethod
    def update(*args, **kwargs):
        return None


def compact_only(backend, request, env):
    """``compact_before_turn`` with no turn after it (the butler's idle compaction, app/idle_compaction.py): the
    claude process the turn would start (its argv, environment and, in ``request.context``, the term relay of a
    redacted topic), its step and events dropped. Whether the session was compacted; ``request.inputs.cancel_event``
    stops it (JobCancelled), as the task's deadline does."""
    ctx, child = request.context, turn_env(request.context, env)

    def spawn():
        return ClaudeProcess(stream_argv(backend, ctx, request.session_id, instructions=request.instructions), ctx,
                             child, cancel=request.inputs.cancel_event, observe=request.observers.process)
    return compact_before_turn(request, env, spawn, lambda kind, data: None, lambda: None, _Uncounted())


def run_stream(backend, request, env):
    ctx, prompt, session_id = request.context, request.prompt, request.session_id
    state = ClaudeReplyState(ctx, session_id, run_id=request.inputs.run_id, publish=request.publish,
                             on_session=request.observers.session)
    process = control = None
    rc, failure, stopped = None, None, False
    transport_details, stage = None, "spawn"
    pending_events = []

    def emit(kind, data):
        pending_events.append((kind, data))

    def flush_events():
        # Shared question persistence takes its resource lock before the
        # control lock. Dispatch outside the latter to avoid lock inversion
        # when a card retry or answer races with an incoming CLI event.
        events, pending_events[:] = pending_events[:], []
        for kind, data in events:
            request.emit(kind, data, control)

    def keep_last_reply():
        """Stopped or out of time while only background tasks ran: the last complete reply
        is still this round's answer, so it is sent rather than dropped."""
        if control is None:
            return False
        with control.control_lock:
            if state.idle_after_reply() and not control.user_requests and not control.answered:
                state.capture()
                return True
        return False

    adapter = ClaudeEventAdapter(emit)
    # An ended subagent's transcript is read while the turn runs (repo review S1), from the session's store.
    adapter.subtasks.locate = lambda: (subagents_dir(ctx, state.session_id, env), adapter.usage.start)
    child_env = turn_env(ctx, env)

    def spawn():
        return ClaudeProcess(stream_argv(backend, ctx, session_id, instructions=request.instructions), ctx, child_env,
                             cancel=request.inputs.cancel_event, observe=request.observers.process)
    try:
        compact_before_turn(request, env, spawn, emit, flush_events, adapter.usage)
        process = spawn()
        stage = "initialize"
        process.initialize()
        control = ClaudeControl(process, state, emit)
        for row in process.prelude:
            state.accept(row)
        request.observers.control(control)
        stage = "build_input"
        content = image_content(request.inputs.attachments, prompt)
        adapter.usage.arm(time.time())
        stage = "send_input"
        control.prompt_id = str(uuid.uuid4())
        control.send_user(content, control.prompt_id)
        stage = "receive_events"
        # Background tasks may run for days: no default limit, only the task timeout or a
        # configured one. Each root reply restarts a configured wait.
        waiting = CompletionBudget(process.deadline, ctx.options.get("completion_wait_seconds"),
                                   total_seconds=ctx.options.get("completion_total_seconds"), default_wait=None)
        rc, failure, stopped = read_rows(process, state, control, adapter, waiting, flush_events, keep_last_reply)
    except JobCancelled:
        keep_last_reply()
        failure = ("cancelled", "cancelled")
    except subprocess.TimeoutExpired:
        failure = ("timed_out", "background_timeout" if keep_last_reply() else "timeout")
    except ValueError:
        failure = ("failed", "protocol_error")
    except (OSError, RuntimeError) as exc:
        failure = ("failed", "transport_error")
        transport_details = transport_failure_details(exc, stage, process)
    finally:
        failure, cleanup, rc = close_stream(request, process, control, emit, flush_events, failure, rc)
    result = state.finish(rc, cleanup=cleanup, failure=failure, stopped=stopped)
    save_transport_details(state, transport_details)
    diagnosis = failure_event(result.outcome, transport_details, stage, process, rc)
    if diagnosis is not None:
        emit("backend_failure", diagnosis)
    settle_subtasks(adapter, ctx, state.session_id, env)
    redacted = redaction.turn_event(ctx)
    if redacted is not None:
        emit("redaction", redacted)
    emit("trace_finished", {})
    flush_events()
    return result
