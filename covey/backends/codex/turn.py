"""One Codex turn read to its end (read_turn): items and server requests as they come, the root's final reply
(end_turn, completed_turn_reply), a paused native question (paused_question_end), the failure codes, and the
controls and process closed afterwards (close_turn)."""

from __future__ import annotations

import queue
import subprocess
import time

from covey.backends.codex.rpc import RpcError, failed, request_identifier
from covey.core.harness import JobCancelled
from covey.core import gateways
from covey.core.protocol import retry_note, retry_reason
from covey.core.result import MAX_REPLY_BYTES, is_identifier
from covey.core.ttft import FirstToken


def context_of(usage):
    """The context the next turn resumes with, as the task card's 上下文 tile takes it (core/trace.py
    CONTEXT_FIELDS): the latest request's tokens (``last.totalTokens``: its input with the cached part, and its
    output) and the model's window when Codex knows it. {} when it gave neither. Codex cards read 上下文 — in
    older releases though the counts were there (87,300 of 258,400)."""
    if not isinstance(usage, dict):
        return {}
    last, window = usage.get("last") or {}, usage.get("modelContextWindow")
    tokens = last.get("totalTokens") if isinstance(last, dict) else None
    if type(tokens) is not int or tokens <= 0:
        return {}
    return {"context_tokens": tokens, **({"context_window": window} if type(window) is int and window >= tokens else {})}


def codex_failure_code(error):
    """Classify known failures without publishing backend text or request data."""
    if not isinstance(error, dict):
        return "root_failed"
    if error.get("message") == "workspace routing discovery timed out":
        return "workspace_routing_timeout"
    info = error.get("codexErrorInfo")
    variant = next(iter(info), "") if isinstance(info, dict) else info
    name = variant.replace("_", "").lower() if isinstance(variant, str) else ""
    details = info.get(variant) if isinstance(info, dict) else None
    status = details.get("httpStatusCode") if isinstance(details, dict) else None
    status = status if type(status) is int else None
    if status == 403 and gateways.quota_text(error.get("message") or ""):
        # A gateway's spent daily quota.
        return "quota_exhausted"
    if name == "unauthorized" or status in {401, 403}:
        return "authentication_error"
    if name == "usagelimitexceeded" or status == 429:
        return "rate_limited"
    if name in {"httpconnectionfailed", "responsestreamconnectionfailed", "responsestreamdisconnected",
                "responsetoomanyfailedattempts"}:
        return "transport_error"
    return "root_failed"


def _matches_question_pause(rpc, state, turn_id):
    pause = getattr(rpc, "_paused_question", None)
    return (isinstance(pause, dict) and pause.get("run_id") == state.run_id
            and pause.get("generation") == state.generation
            and pause.get("session_id") == rpc.thread_id and pause.get("turn_id") == turn_id
            and bool(pause.get("request_ids"))
            and all(request_identifier(rid) for rid in pause["request_ids"]))


def completed_turn_reply(rpc, turn):
    """Only exact, full current-turn items are authoritative; never use latest."""
    if turn.get("itemsView") not in {"full", "summary", "notLoaded"}:
        raise failed("protocol_error")
    if turn.get("itemsView") != "full":
        snapshot = rpc.request("thread/read", {"threadId": rpc.thread_id, "includeTurns": True}, timeout=30)
        thread = snapshot.get("thread") if isinstance(snapshot, dict) else None
        if not isinstance(thread, dict) or thread.get("id") != rpc.thread_id:
            raise failed("identity_mismatch")
        turns = thread.get("turns")
        if not isinstance(turns, list):
            raise failed("root_incomplete")
        matches = [t for t in turns if isinstance(t, dict) and t.get("id") == rpc.turn_id]
        if len(matches) != 1:
            raise failed("identity_mismatch")
        turn = matches[0]
    if (turn.get("id") != rpc.turn_id or turn.get("status") != "completed"
            or turn.get("error") is not None or turn.get("itemsView") != "full"
            or not isinstance(turn.get("items"), list)):
        raise failed("root_incomplete")
    finals, unphased, seen = [], [], set()
    for item in turn["items"]:
        if not isinstance(item, dict):
            raise failed("protocol_error")
        if item.get("type") != "agentMessage":
            continue  # Never access reasoning/tool-output fields.
        mid, text, phase = item.get("id"), item.get("text"), item.get("phase")
        if (not is_identifier(mid) or mid in seen or not isinstance(text, str)
                or phase not in {None, "commentary", "final_answer"}):
            raise failed("protocol_error")
        seen.add(mid)
        if phase == "final_answer":
            finals.append((mid, text))
        elif phase is None:
            unphased.append((mid, text))
    selected = finals or unphased
    text = "\n\n".join(t for _, t in selected).strip()
    if not text or len(text.encode("utf-8")) > MAX_REPLY_BYTES:
        raise failed("output_missing")
    return text, tuple(mid for mid, _ in selected)


def answer_server_request(rpc, row, p, method, generation_clock, event):
    """A request from the app server: the root turn's request_user_input becomes a question;
    any other request is refused."""
    if (method == "item/tool/requestUserInput" and p.get("threadId") == rpc.thread_id
            and p.get("turnId") == rpc.turn_id):
        if not request_identifier(row["id"]):
            raise failed("protocol_error")
        generation_clock.block("question-" + str(row["id"]), row.get("_received_at"))
        rpc.user_requests.add(row["id"])
        rpc._question_request_id = row["id"]
        try:
            event("question", {**p, "request_id": row["id"]}, rpc)
        finally:
            rpc._question_request_id = None
    else:
        rpc.send({"id": row["id"], "error": {"code": -32601,
                 "message": "Unsupported or mismatched server request"}})


def end_turn(rpc, state, turn, ctx, started, cancel, text):
    """The root turn's turn/completed: (status, code, text, native_question, paused_turn_id, finished).
    A turn that paused for a question or failed ends the loop without turn_finished (``finished``
    False). Raises when the turn cannot be trusted, was stopped or ran out of time."""
    with rpc.input_lock:
        if rpc.control_error:
            raise failed("identity_mismatch")
        if turn.get("status") == "interrupted" and rpc.pause_for_question:
            if turn.get("error") is not None or not _matches_question_pause(rpc, state, rpc.turn_id):
                raise failed("root_incomplete")
            rpc.accepting_input = False
            return "completed", "", rpc.pause_for_question, True, rpc.turn_id, False
        if turn.get("status") == "interrupted":
            raise JobCancelled("任务已停止")
        if turn.get("status") != "completed":
            return "failed", codex_failure_code(turn.get("error")), text, False, "", False
        if rpc.user_requests:
            raise failed("root_incomplete")
        text, mids = completed_turn_reply(rpc, turn)
        if cancel is not None and cancel.is_set():
            raise JobCancelled("任务已停止")
        if ctx.timeout > 0 and time.monotonic() - started > ctx.timeout:
            raise subprocess.TimeoutExpired("codex app-server", ctx.timeout)
        rpc.accepting_input = False
        boundary = ("app-server", rpc.thread_id, rpc.turn_id, *rpc.input_ids)
        state.preserve(rpc.thread_id, boundary, mids, text)
        rpc.turn_id = ""
        return "completed", "", text, False, "", True


def output_item_started(method, p):
    """The root turn's model began an output item: reasoning (also when it streams no text) or an
    answer. Its start is Codex's first sign of model output; tool items start only once the model
    has written the call. Only the type is read."""
    item = p.get("item")
    return method == "item/started" and isinstance(item, dict) and item.get("type") in {"reasoning", "agentMessage"}


def tool_call_started(method, p):
    """The root turn's model wrote a tool call. Before any reasoning or answer item it was the turn's
    first output, whose first token is not known: the item starts once the call is written. A later
    item would count the tool's run (S31 K4: 17.00 s for a command written about 5 s in)."""
    item = p.get("item")
    return method == "item/started" and isinstance(item, dict) and item.get("type") in {
        "commandExecution", "fileChange", "webSearch", "mcpToolCall", "dynamicToolCall", "collabAgentToolCall"}


def on_item(method, p, row, adapter, generation_clock, event, rpc):
    """An item of the root turn starts or completes: tool clocks, the trace, sub-agents, and the
    answer or commentary of an agent message."""
    item = p.get("item")
    if not isinstance(item, dict):
        return
    if method == "item/started" and item.get("type") == "reasoning":
        # Reasoning that streams no text gives no first token; its start alone says the model is at
        # work (not "等待模型开始输出" for 29 s). Only the type is read, never its fields.
        event("model_reasoning", {}, rpc)
        return
    if item.get("type") in {"commandExecution", "fileChange", "webSearch", "mcpToolCall",
            "dynamicToolCall", "collabAgentToolCall", "subAgentActivity"} and item.get("id"):
        control_clock = generation_clock.block if method == "item/started" else generation_clock.unblock
        control_clock("tool-" + item["id"], row.get("_received_at"))
    adapter.item(method, item, row.get("_received_at"))
    if item.get("type") in {"collabAgentToolCall", "subAgentActivity"}:
        adapter.agents(item, row.get("_received_at"))
    elif item.get("type") == "agentMessage" and method == "item/completed":
        body, phase = item.get("text"), item.get("phase")
        if phase in {None, "final_answer"} and isinstance(body, str) and body:
            event("answer_snapshot", {"text": body}, rpc)
        elif phase == "commentary" and isinstance(body, str) and body:
            event("progress_note", {"id": "note-" + str(item.get("id")),
                  "text": body, "received_at": row.get("_received_at")}, rpc)


def close_turn(rpc, control, event, status, code):
    """End the turn's controls and trace, close the app server; (status, code, cleanup, protection)."""
    with rpc.input_lock:
        rpc.accepting_input = False
    try:
        control(None)
        event("trace_finished", {}, rpc)
        if status == "cancelled":
            event("cancel_questions", {}, rpc)
    except Exception:
        if status == "completed":
            status, code = "failed", "transport_error"
    try:
        cleanup = rpc.close() is True and type(rpc.process.poll()) is int
        protection = cleanup and not rpc.accepting_input
        if status == "completed" and rpc.process.poll() != 0:
            status, code = "failed", "process_exit"
    except Exception:
        cleanup = protection = False
        if status == "completed":
            status, code = "failed", "cleanup_unconfirmed"
    return status, code, cleanup, protection


def paused_question_end(rpc, state, ctx, started, paused_turn_id, confirmed):
    """A turn paused for a native question: (status, code) when the pause cannot be trusted, else
    None once its pause proof is recorded. ``confirmed``: identity, cleanup and protection held."""
    if ctx.timeout > 0 and time.monotonic() - started > ctx.timeout:
        return "timed_out", "timeout"
    if rpc.control_error or state.storage_failed or not _matches_question_pause(rpc, state, paused_turn_id):
        return "failed", "root_incomplete"
    if not (confirmed and rpc.cleanup_confirmed is True):
        return "failed", "cleanup_unconfirmed"
    if state.enabled and is_identifier(state.run_id):
        rpc.paused_question_proof = {"version": 1, "backend": state.backend,
            **rpc._paused_question, "request_ids": list(rpc._paused_question["request_ids"]),
            "terminal_status": "interrupted", "pause_confirmed": True,
            "input_closed": True, "cleanup_confirmed": True}
    return None


def on_request_resolved(rpc, p, row, generation_clock, event):
    # A previously accepted root request ID is authoritative even
    # when this notification does not repeat thread/turn fields.
    rid = p.get("requestId")
    if (rid in rpc.user_requests and p.get("threadId") in {None, rpc.thread_id}
            and p.get("turnId") in {None, rpc.turn_id}):
        generation_clock.unblock("question-" + str(rid), row.get("_received_at"))
        rpc.user_requests.discard(rid)
        event("question_resolved", p, rpc)


def check_stop(rpc, cancel, ctx, started):
    """Raise when the task was stopped (interrupting the turn first) or is out of time."""
    if cancel.is_set():
        try:
            rpc.request("turn/interrupt", {"threadId": rpc.thread_id, "turnId": rpc.turn_id}, timeout=5)
        except RpcError:
            pass
        raise JobCancelled("任务已停止")
    if ctx.timeout > 0 and time.monotonic() - started > ctx.timeout:
        raise subprocess.TimeoutExpired("codex app-server", ctx.timeout)


class MeasuredOutput:
    """The output tokens that go with the measured generation time, for 平均 token/s. Codex reports
    the turn's cumulative usage after each model request, and the generation clock stops while a
    tool item is open, so a request made then (polling a running command) adds tokens but no time:
    S31 K4 read 6.4 → 42.4 token/s over the same 5.3 s while the model only polled `sleep 45`. Only
    an update that came with generation time adds its tokens."""

    def __init__(self):
        self.tokens = self.output = 0
        self.seconds = 0.0

    def update(self, output, seconds):
        if seconds > self.seconds:
            self.tokens += max(0, output - self.output)
            self.seconds = seconds
        self.output = max(self.output, output)
        return {"measured_output_tokens": self.tokens, "generation_seconds": self.seconds}


def read_turn(rpc, request, ctx, state, adapter, generation_clock, first_token_start, event, started, turn_usage, text):
    """Read the app server's events until the root turn completes: end_turn's result. Raises
    when stopped, out of time or the transport closes."""
    retries = 0
    first_token = FirstToken(lambda kind, data: event(kind, data, rpc), "codex_item")
    output_seen = False
    measured = MeasuredOutput()
    while True:
        cancel = request.inputs.cancel_event
        check_stop(rpc, cancel, ctx, started)
        try:
            row = rpc.events.get(timeout=0.25)
        except queue.Empty:
            continue
        method, p = row.get("method", ""), row.get("params", {})
        if method == "bridge/closed":
            raise failed("transport_error")
        if not isinstance(p, dict):
            continue
        if "id" in row:
            answer_server_request(rpc, row, p, method, generation_clock, event)
            continue
        if method == "serverRequest/resolved":
            on_request_resolved(rpc, p, row, generation_clock, event)
            continue
        if p.get("threadId") != rpc.thread_id:
            if p.get("threadId") and method == "thread/tokenUsage/updated":
                adapter.child_tokens(p["threadId"], p.get("tokenUsage"), row.get("_received_at"))
            elif p.get("threadId"):
                adapter.child(method, p, row.get("_received_at"))
            continue
        if method == "error" and p.get("willRetry") is True and p.get("turnId") in {None, rpc.turn_id}:
            retries += 1
            message = str((p.get("error") or {}).get("message") or "")
            # The status is in codexErrorInfo; the message may not say it.
            code = codex_failure_code(p.get("error"))
            if code != "quota_exhausted" and "429" in message:
                code = "rate_limited"
            event("progress_note", retry_note(retries, retry_reason(code, message)), rpc)
            continue
        if method == "thread/tokenUsage/updated":
            counts = turn_usage.update(p.get("tokenUsage") or {}, p.get("turnId") == rpc.turn_id)
            if counts is not None:
                event("token_usage", {**counts, **measured.update(
                    counts["output_tokens"], generation_clock.sample(row.get("_received_at"))),
                    **context_of(p.get("tokenUsage"))}, rpc)
            continue
        if method == "turn/completed":
            turn = p.get("turn")
            if not isinstance(turn, dict) or turn.get("id") != rpc.turn_id:
                continue
            return end_turn(rpc, state, turn, ctx, started, cancel, text)
        # Root message bodies and tool progress both require the current turn.
        if p.get("turnId") != rpc.turn_id:
            continue
        if method == "item/agentMessage/delta":
            continue  # Deltas lack phase/completeness proof; do not retain or publish.
        if method == "turn/plan/updated":
            event("progress_note", {"id": "plan-" + rpc.turn_id,
                  "text": "Codex 正在更新任务计划", "title": "任务计划"}, rpc)
        if method in {"item/started", "item/completed"}:
            on_item(method, p, row, adapter, generation_clock, event, rpc)
        if tool_call_started(method, p):
            first_token.skip()  # No TTFT for this turn (执行指标口径: 测不到时不写).
        # After on_item: the status line hears of a reasoning start before the TTFT it gives.
        if method == "bridge/modelToken" or output_item_started(method, p):
            first_token.output(first_token_start, row.get("_received_at"),
                               "codex_delta" if method == "bridge/modelToken" else "codex_item")
        if method == "bridge/modelToken" and not output_seen:
            # Text streams: the status line moves on from 模型思考中 (the TTFT may be the reasoning's start).
            output_seen = True
            event("model_output", {}, rpc)
