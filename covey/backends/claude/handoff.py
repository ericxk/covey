"""A handoff summary from a Claude Code session the topic is leaving (🔁 换配置, app/switch_handoff.py).

The first run after a switch asks the old model, on a fork of its session (``--resume <session> --fork-session``,
its model and budget), for a short summary the new model starts from instead of the raw history. The fork keeps
the session's prompt cache warm and leaves its file as it was; the fork's own file is removed afterwards. The call
runs with the turn's argv otherwise (the same tools and settings, so the cached prefix matches; removing tools
would change it), without ``--dangerously-skip-permissions``: the permission requests that reach the Bridge are
denied. Claude Code's default mode still runs read-only tools, and whatever the user's ``permissions.allow``
rules allow, without asking: the prompt's "no tools" is the guard for those. It never raises: ``summarize``
gives ``(text, outcome)``, ``text`` empty unless it succeeded.

A gateway that is down does not hold the new turn for SUMMARY_SECONDS: Claude Code's answer to initialize comes within
backends/claude/process.py INITIALIZE_SECONDS (30 s), and the old model's first sign after the question (a streamed
event, a message, a tool request or the result) within FIRST_SECONDS; past either the summary is given up at once and
the new model gets the last rounds instead.
"""
from __future__ import annotations

import dataclasses
import subprocess
import time
import uuid

from covey.backends.claude.argv import stream_argv, turn_env
from covey.backends.claude.process import INITIALIZE_SECONDS, TRANSPORT_FAILURES, ClaudeProcess
from covey.backends.claude.sessions import ClaudeSessionStore, claude_projects_root
from covey.core.harness import UUID_RE, JobCancelled

SUMMARY_SECONDS = 180  # The new turn waits at most this long for the summary, then starts without it.
FIRST_SECONDS = 30  # From the question sent to the old model's first sign (HEARD): none by then, its gateway is down.
HEARD = frozenset({"stream_event", "assistant", "result", "control_request"})  # Rows only the model's answer brings.
# Options of a turn that have no place in the summary call: a rewind point (a switch with a ↩️ 回退 pending is not
# summarized, app/switch_handoff.py), a compaction, the butler's tools.
DROPPED = ("resume_at", "compact_first", "compact_instructions", "mcp_config", "fork_session")
DENIED = "Bridge 交接摘要只要文字回答，不能调用工具。"


def summarize(backend, ctx, session_id, prompt, env, *, cancel=None, seconds=SUMMARY_SECONDS, first_seconds=None):
    """(summary text, outcome words) from a fork of ``session_id`` run in ``ctx`` (the old binding's
    workdir, model and budget, and the relay an extension's hook gave it). ``first_seconds``: FIRST_SECONDS unless given."""
    first_seconds = FIRST_SECONDS if first_seconds is None else first_seconds
    options = {k: v for k, v in ctx.options.items() if k not in DROPPED}
    ctx = dataclasses.replace(ctx, timeout=seconds, skip_permissions=False,
                              options={**options, "fork_session": session_id})
    fork, process = "", None
    try:
        process = ClaudeProcess(stream_argv(backend, ctx, None), ctx, turn_env(ctx, env), cancel=cancel)
        process.initialize()
        process.send({"type": "user", "uuid": str(uuid.uuid4()), "session_id": "default", "parent_tool_use_id": None,
                      "message": {"role": "user", "content": [{"type": "text", "text": prompt}]}})
        sent, heard = time.monotonic(), False
        while True:
            row = process.receive()
            if row is None:
                if not heard and time.monotonic() - sent >= first_seconds:
                    return "", f"原模型 {first_seconds} 秒没有回应（网关可能断开）"
                continue
            kind = row.get("type")
            heard = heard or kind in HEARD
            if kind in {"bridge_error", "bridge_eof"}:
                return "", "Claude Code 进程提前结束"
            if isinstance(row.get("session_id"), str) and row["session_id"] != session_id:
                fork = fork or row["session_id"]
            if kind == "control_request" and isinstance(row.get("request_id"), str):
                process.send({"type": "control_response", "response": {
                    "subtype": "success", "request_id": row["request_id"],
                    "response": {"behavior": "deny", "message": DENIED}}})
                continue
            if kind != "result" or row.get("parent_tool_use_id") is not None:
                continue
            text = row.get("result") if isinstance(row.get("result"), str) else ""
            if row.get("is_error") or row.get("subtype") != "success":
                return "", "Claude Code 报告失败"
            return (text.strip(), "写好了") if text.strip() else ("", "回答为空")
    except JobCancelled:
        return "", "已停止"
    except subprocess.TimeoutExpired:
        return "", f"超过 {seconds} 秒"
    except RuntimeError as exc:
        if str(exc) == TRANSPORT_FAILURES["initialize_timeout"]:
            return "", f"Claude Code {INITIALIZE_SECONDS} 秒没有就绪"
        if str(exc) == TRANSPORT_FAILURES["queue_timeout"]:
            return "", "Claude Code 排队超时"
        return "", f"出错（{type(exc).__name__}）"
    except (OSError, ValueError) as exc:
        return "", f"出错（{type(exc).__name__}）"
    finally:
        if process is not None:
            process.close()
        drop_fork(ctx, env, fork, session_id)


def drop_fork(ctx, env, fork, session_id):
    """Remove the fork's session file: nothing resumes it. Never the session it came from."""
    if not fork or fork == session_id or not UUID_RE.fullmatch(fork):
        return
    try:
        path = ClaudeSessionStore(claude_projects_root(env)).locate(ctx.workdir, fork)
        if path is not None:
            path.unlink()
    except OSError:
        pass
