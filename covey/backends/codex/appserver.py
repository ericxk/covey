"""One Codex turn per task over its App Server (run_appserver): open or resume the thread, start the turn, read
it to its end (backends/codex/turn.py) and close the server (backends/codex/rpc.py), with bidirectional input and
typed events.

Only the owning task controls this process. A failed transport is never replayed
automatically: the caller's checkpoint-aware retry owns that decision.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import time

from covey.backends.codex.events import CodexEventAdapter
from covey.backends.codex.rpc import AppServer, RpcError, failed
from covey.backends.codex.turn import close_turn, paused_question_end, read_turn
from covey.backends.codex.usage import CodexTurnUsage
from covey.core.harness import JobCancelled, RunResult
from covey.core.paths import entry_script
from covey.core.process import device_of
from covey.core.protocol import deliver_event
from covey.core.reply_state import ReplyState
from covey.core.result import is_identifier
from covey.core.usage import ActiveGenerationClock


def dispatch_structured_event(callback, kind, data, rpc):
    # Input/approval handling must retain its failure semantics. Observability
    # failures, however, must never interrupt a backend turn or skip cleanup.
    return deliver_event(callback, kind, data, rpc, logging.getLogger(__name__))


class CodexReplyState(ReplyState):
    error = RpcError  # A failed save is an RpcError, as the app server's callers catch it.


# After the bridge rules: questions and sub-agents on the Codex protocol.
CODEX_INSTRUCTIONS = (
    "你通过飞书话题与用户交互。必须得到回答才能继续的问题，按检查点约定写 requires_user=true、question 和可选的 questions；"
    "桥接器会先交付完整回复，再创建问题卡片并在回答后续接。可以边做边问的非阻塞问题调用 request_user_input"
    "（不要使用 request_user_input_async），以便飞书展示。不要把未回答视为同意。提问正文支持长文本；避免在普通最终答案中假装等待工具回答。"
    "子 agent 仅执行明确的子任务；最终由主任务汇总结果。结束主任务前必须等待所有子 agent 达到最终状态并收集结果，不能把仍在运行的子 agent 留到下一轮。"
)


def thread_params(ctx, session_id, instructions="", process=None):
    """The thread/start, thread/resume or thread/fork parameters of this turn, and the session it
    forks (None unless a new topic forks one). Codex keeps the developer instructions a thread
    started with; they are sent on every turn all the same. On a device (``ctx.device``) the paths are the
    device's: its working directory, its task directory as the writable root and its copy of the workspace
    reader (``process``, the app server's ``DeviceProcess``, pushes and names it)."""
    if device_of(ctx):
        cwd = ctx.remote_workdir
        writable = str(process.task_dir)
        command, args = process.workspace_reader(cwd, extra_roots=[str(process.task_dir) + "/incoming"])
    else:
        cwd = str(ctx.workdir)
        writable = str(ctx.state_dir / "checkpoints")
        command = sys.executable
        # Attachments of this topic are downloaded outside the workspace, and so is
        # the transcript a model switch hands over (app/switch_handoff.py).
        args = ["-B", str(entry_script("covey_workspace_reader.py")), "--root", str(ctx.workdir),
                "--extra-root", str(ctx.history_path.parent / "incoming_media"),
                "--extra-root", str(ctx.history_path.parent / "handoff")]
    params = {
        "model": ctx.model,
        "cwd": cwd,
        "approvalPolicy": "never",
        "sandbox": ctx.options.get("sandbox", "danger-full-access"),
        "config": {
            **ctx.options.get("thread_config", {}),
            **({"model_reasoning_effort": ctx.effort} if ctx.effort else {}),
            "sandbox_workspace_write.writable_roots": [writable],
            "mcp_servers.bridge_workspace.command": command,
            "mcp_servers.bridge_workspace.args": args,
            "mcp_servers.bridge_workspace.enabled": True,
        },
        "developerInstructions": "\n\n".join(filter(None, (instructions, CODEX_INSTRUCTIONS))),
    }
    if ctx.options.get("model_provider"):
        params["modelProvider"] = ctx.options["model_provider"]
    fork = None if session_id else ctx.options.get("fork_session")
    if session_id or fork:
        params["threadId"] = session_id or fork
    return params, fork


def start_turn(rpc, request, ctx, prompt, session_id):
    """Start the turn on the thread and check that both identities are the ones asked for."""
    reply = rpc.request(
        "turn/start",
        {
            "threadId": rpc.thread_id,
            "input": [{"type": "text", "text": prompt}] + [{"type": "localImage", "path": item["path"]} for item in request.inputs.attachments],
            "model": ctx.model,
            **({"effort": ctx.effort} if ctx.effort else {}),
        },
        timeout=60,
    )
    rpc.turn_id = reply["turn"]["id"]
    if (not is_identifier(rpc.thread_id) or not is_identifier(rpc.turn_id)
            or (session_id is not None and rpc.thread_id != session_id)):
        raise failed("identity_mismatch")


def open_thread(rpc, request, state, observe):
    """Initialize the app server and start, resume or fork the thread; check its identity."""
    ctx, session_id = request.context, request.session_id
    rpc.reply_state = state
    rpc.paused_question_proof = None
    rpc._question_request_id = None
    rpc._paused_question = None
    observe(rpc.process)
    rpc.request("initialize", {"clientInfo": {"name": "feishu_bridge", "version": "1.0"},
                "capabilities": {"experimentalApi": True}})
    rpc.send({"method": "initialized"})
    params, fork = thread_params(ctx, session_id, request.instructions, rpc.process)
    reply = rpc.request("thread/resume" if session_id else "thread/fork" if fork else "thread/start", params, timeout=60)
    rpc.thread_id = reply["thread"]["id"]
    if (not is_identifier(rpc.thread_id) or (session_id is not None and rpc.thread_id != session_id)
            or (fork and rpc.thread_id == fork)):
        raise failed("identity_mismatch")
    request.observers.session(rpc.thread_id)
    state.invalidate()


def run_appserver(request, env):
    ctx, prompt, session_id = request.context, request.prompt, request.session_id
    state = CodexReplyState(ctx.options, request.inputs.run_id, request.publish)
    rpc = None
    observe, control, callback = request.observers.process, request.observers.control, request.observers.event
    def event(kind, data, owner):
        return dispatch_structured_event(callback, kind, data, owner)
    adapter = None
    started = time.monotonic()
    turn_usage = CodexTurnUsage()
    generation_clock = None
    status, code, text = "failed", "transport_error", ""
    identity = cleanup = protection = False
    native_question = False
    paused_turn_id = ""
    legacy_exception = None
    try:
        # Bound before anything can fail, so that finally closes the server.
        rpc = AppServer([*ctx.backend_cmd, "app-server", "--stdio", "-c", "agents.max_threads=4",
            "-c", "features.default_mode_request_user_input=true"], ctx.workdir, env,
            **({"ctx": ctx} if device_of(ctx) else {}))
        open_thread(rpc, request, state, observe)
        # Its own thread is never one of its subtasks (CodexEventAdapter.agents).
        adapter = CodexEventAdapter(lambda kind, data: event(kind, data, rpc), rpc.thread_id)
        generation_clock = ActiveGenerationClock()
        first_token_start = generation_clock.last
        start_turn(rpc, request, ctx, prompt, session_id)
        identity = True
        control(rpc)
        status, code, text, native_question, paused_turn_id, finished = read_turn(
            rpc, request, ctx, state, adapter, generation_clock, first_token_start, event, started, turn_usage, text)
        if finished:
            event("turn_finished", {}, rpc)
    except JobCancelled as exc:
        status, code, legacy_exception = "cancelled", "cancelled", exc
    except subprocess.TimeoutExpired:
        status, code = "timed_out", "timeout"
        legacy_exception = subprocess.TimeoutExpired("codex app-server", ctx.timeout)
    except RpcError as exc:
        status, code = "failed", "storage_failed" if state.storage_failed else exc.code
        if code == "identity_mismatch":
            identity = False
    except Exception:
        status, code = "failed", "storage_failed" if state.storage_failed else "protocol_error"
    finally:
        if rpc is not None:
            if adapter is not None:  # None before the thread opened: no subtasks to report.
                try:
                    adapter.publish_subtasks(time.time(), force=True)  # Display only, before trace_finished.
                except Exception:
                    pass
            status, code, cleanup, protection = close_turn(rpc, control, event, status, code)
    sid = rpc.thread_id if rpc is not None and rpc.thread_id else session_id
    cancel = request.inputs.cancel_event
    if native_question and status == "completed" and not cancel.is_set():
        refused = paused_question_end(rpc, state, ctx, started, paused_turn_id, identity and cleanup and protection)
        if refused is None:
            return RunResult(text, sid, False)  # Paused question only; never a verified root final.
        status, code = refused
    if legacy_exception is not None and not state.enabled:
        raise legacy_exception
    # trace_finished went out with the controls (close_turn).
    return state.finish(text, sid, status, code, identity=identity, cleanup=cleanup, protection=protection,
                        cancel_event=cancel)
