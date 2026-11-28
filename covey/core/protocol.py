"""Versioned public Harness events; vendor payloads stay inside adapters."""

from __future__ import annotations

import datetime
import logging
import re
from dataclasses import dataclass, field


# Event data, a dict per kind:
#   tool: id, name, status ("running", "done", "failed"), started_at; optional detail, ended_at,
#     child_id, exit_code, public_progress (a trusted adapter's opt-in to the compact live summary).
#     A name in STEP_TITLES is shown by its title (Claude Code's tools, Codex's bash, edit and
#     websearch, background for a command left running, context_compaction); any other as it is (Codex's
#     调用工具, 更新计划, 读取文件 and 浏览目录, another tool's own name).
#   progress_note: id, text; optional received_at, child_id, urgent (omitted: the note shows at once),
#     title (default 进展说明), retry (attempt, reason, next, since, limit: the main task's model request
#     is being retried, shown on the status line until the main task's next event), queue (ahead, running, cap,
#     limit, name, since: the main task's command waits for a free seat at its gateway, ``queue_note``; shown on the
#     status line until the main task's next event).
#   agent_state: id, name, status, started_at; optional parent_id, ended_at, description, summary. A subtask
#     that ended and runs again (Codex resumeAgent, a sub-agent's next run) comes with a later started_at:
#     the trace takes a running state no later than the ended one's start for a stale event (cards/trace.py).
#   first_token: ttft_seconds, ttft_source (core/ttft.py SOURCES: one per backend).
#   token_usage: any of input_tokens, output_tokens, measured_output_tokens, generation_seconds,
#     context_tokens (the main task's latest request: its input and output), context_window, model (the id of
#     the model the main task ran, Claude's: its init row; the answer card's 输入 names it when none was chosen).
#   subtask_usage: agents (id -> name, level, input_tokens, cache_read_tokens, cache_write_tokens,
#     output_tokens), unreported, pending: this turn's subtasks at every level (core/usage.py).
#   model_reasoning: no fields; the main task's model began reasoning that streams no text.
#   model_output: optional id, received_at; the main task's model is streaming output, never its content.
#   model_request: optional id, received_at; the main task's first model call started (capability model_requests).
#   backend_failure: status, code, stage; optional exception, reason, errno, process_exit_code / exit_code, write_size,
#     write_offset, stderr (the CLI's last lines of stderr, credentials hidden, at most a few dozen): why a run that did
#     not complete failed, once, for the Bridge's log (app/runs.py log_backend_failure); never shown on a card.
#   policy_tolerated: event (backends/claude/policy.py policy_signal), where, text: a subagent's first refusal, let
#     through (docs/policy-stop.md); the Bridge shows ``text`` in 执行过程 and records it (app/policy_guard.py tolerate).

# A progress_note title: the Bridge's own record of waiting out a busy model gateway ("<display name> 繁忙记录",
# core/gateways.py busy_notes), a line of its own in 执行过程 rather than an operation (cards/trace_view.py).
# A tool step's title: compacting the session's context, before the turn (the Bridge asked) or within
# it (Claude Code's own); a line of its own in 执行过程 rather than an operation (cards/trace_view.py).
COMPACT_STEP = "压缩上下文"
# A tool step's title by its name (cards/trace.py); the ids and titles are kept in traces/, so they never change.
STEP_TITLES = {"bash": "执行命令", "read": "读取文件", "edit": "修改文件", "write": "写入文件", "glob": "查找文件",
               "grep": "搜索文件", "webfetch": "读取网页", "websearch": "搜索网页", "question": "等待回答",
               "task": "分配子任务", "agent": "分配子任务", "background": "后台命令", "context_compaction": COMPACT_STEP}
# A tool step's status once it has ended: it never runs again (core/steps.py, cards/trace.py).
STEP_ENDED = frozenset({"done", "failed"})
# An agent_state status of a subtask that has ended: its steps end with it (cards/trace.py).
AGENT_ENDED = frozenset({"completed", "errored", "interrupted", "cancelled", "shutdown"})
# An agent_state status of a subtask still running, or about to (cards/trace.py).
AGENT_LIVE = frozenset({"running", "pendingInit"})

# The reasons a progress_note's retry gives for a model request being retried (Codex's).
RATE_LIMITED_REASON = "上游限流（429）"
UNAVAILABLE_REASON = "模型服务暂时不可用"
# The same, when the failure's own words say it is the network or a proxy (a refused or reset connection, a name
# that does not resolve, a TLS error, Codex's refresh token answered 403 from a region it refuses): say so.
NETWORK_REASON = "模型服务连不上，可能是网络或代理问题（检查 HTTPS_PROXY）"
NETWORK_MESSAGE = re.compile(
    r"econn(?:refused|reset|aborted)|etimedout|enotfound|eai_again|getaddrinfo|could not resolve|"
    r"name or service not known|connection (?:refused|reset|timed out)|error sending request|"
    r"\bproxy\b|tls handshake|certificate verify|refresh token.{0,80}\b403\b|\b403\b.{0,80}refresh token", re.I | re.S)
# A 403 that is a spent quota, not a refused identity: a gateway says which by its ``quota_pattern``
# (core/gateways.py quota_text; Acme's "quota exceeded: Daily Limits exceeded. Resets at …Z").
RESETS_AT = re.compile(r"Resets at (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?Z)")


def quota_reason(message):
    """The retry line's reason for a spent quota, with when it comes back in local time where the message says."""
    found = RESETS_AT.search(message)
    try:
        when = datetime.datetime.fromisoformat(found[1].replace("Z", "+00:00")).astimezone() if found else None
    except ValueError:
        when = None
    return "模型服务额度已用完" + (f"，{when:%m-%d %H:%M} 恢复" if when else "") + "，重试不会成功，可点停止"


def retry_reason(code, message):
    """The retry line's reason for a request that failed with the bridge's failure ``code``."""
    if code == "quota_exhausted":
        return quota_reason(message)
    if code == "rate_limited":
        return RATE_LIMITED_REASON
    return NETWORK_REASON if NETWORK_MESSAGE.search(str(message or "")) else UNAVAILABLE_REASON


def retry_note(attempt, reason, received_at=None, **extra):
    """The progress_note saying the main task's model request is being retried; ``received_at=None``
    leaves the field out, ``extra`` goes into its retry (next, since, limit)."""
    note = {"id": "backend-retry", "text": "模型请求重试中"}
    if received_at is not None:
        note["received_at"] = received_at
    note["retry"] = {"attempt": attempt, "reason": reason, **extra}
    return note


QUEUE_NOTE = "backend-queue"


def queue_note(queue, since):
    """The progress_note saying the main task's command waits for a free seat at its gateway, from ``since`` (ms since
    the epoch): ``queue`` is what the command said (backends/claude/process.py queue_line); when it says its turn came
    (``admitted``), the note that ends the wait."""
    if queue.get("admitted"):
        return {"id": QUEUE_NOTE, "text": "排队结束，开始执行"}
    where = f"{queue['name']} " if queue.get("name") else ""
    ahead = f"，前面还有 {queue['ahead']} 个" if "ahead" in queue else ""
    return {"id": QUEUE_NOTE, "text": f"{where}排队等空位{ahead}", "queue": {**queue, "since": since}}

# Display-only events. A failure while showing one is logged and never decides the
# turn; question, answer and input events keep their failure semantics.
PROGRESS_EVENT_KINDS = frozenset({
    "tool", "progress_note", "agent_state", "answer_snapshot", "first_token", "token_usage", "subtask_usage",
    "trace_finished", "question_resolved", "model_reasoning", "model_output", "model_request", "backend_failure",
})


def deliver_event(callback, kind, data, control=None, log=None):
    try:
        return callback(kind, data, control)
    except Exception as exc:
        if kind not in PROGRESS_EVENT_KINDS:
            raise
        # The type only: the message may quote task text.
        (log or logging.getLogger(__name__)).warning("Progress event failed: %s (%s)", kind, type(exc).__name__)
        return None


@dataclass(frozen=True)
class HarnessEvent:
    kind: str
    data: dict = field(default_factory=dict)
    version: int = 1

    def deliver(self, callback, control=None):
        if self.version != 1:
            raise ValueError("Unsupported Harness event version")
        deliver_event(callback, self.kind, self.data, control)

