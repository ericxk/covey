"""pi's RPC event rows to public Harness events.

Reasoning is never published (GPT's is encrypted and its thinking events carry no text). A
model request is one assistant message, but pi prints its ``message_start`` only once the response's
headers came (pi-ai pushes ``start`` after them), and Acme may hand a whole response over at once:
#2693's start, deltas and end came within milliseconds of each other (9,660 tokens in 0.61 s, TTFT
0.004 s). So a request is timed from when it was sent: the prompt's write for the first, and the last
row pi prints before it sends a later one (SEND_MARKS, or a user or tool-result message; pi-agent-core's
agent-loop.js). TTFT runs from there to the turn's first thinking or text, and the generation time adds
up the requests from send to ``message_end``: tools, retry waits and compaction are left out, as Codex's
clock leaves out tools. A request whose send or output count is unknown adds nothing, and one faster
than MAX_RATE was timed wrong. ``usage.input`` leaves out cache reads and writes, which are reported
beside it.
"""
from __future__ import annotations

from covey.core import gateways
from covey.core.protocol import retry_note, retry_reason
from covey.core.steps import AnswerNotes, Steps
from covey.core.text import tool_detail
from covey.core.ttft import FirstToken, elapsed
from covey.core.usage import TurnTokens

TTFT_SOURCE = "pi_delta"
DETAIL_KEYS = ("command", "path", "file_path", "pattern", "query", "url")  # A step's detail, the first it has.
# Rows pi prints just before it sends a model request, never while one is out (with any message_end
# but an assistant's): agent_start and turn_start begin one, the last tool's end closes a batch.
SEND_MARKS = {"agent_start", "turn_start", "tool_execution_end"}
COMPACT_SLOT = "compaction"  # compaction-1, compaction-2, ...
# Requests whose usage counts what they generated; an errored or aborted one may report no output.
COUNTED = {"stop", "length", "toolUse"}
# token/s no GPT route here comes near (Codex's Acme turns read tens): a faster request was timed wrong.
MAX_RATE = 1000


def message_texts(message):
    """The text of an assistant message: its final-answer parts where GPT marks them, else all its text."""
    parts = [c for c in (message.get("content") or []) if isinstance(c, dict) and c.get("type") == "text"
             and isinstance(c.get("text"), str)]
    final = [c["text"] for c in parts if '"phase":"final_answer"' in str(c.get("textSignature") or "")]
    return "".join(final or [c["text"] for c in parts])


def failure_code(message):
    """The bridge code for an assistant message that ended with ``stopReason: "error"``."""
    text = str(message or "")
    head = text[:200].lower()
    if gateways.quota_text(head):
        return "quota_exhausted"
    if head.startswith(("401", "403")) or "unauthorized" in head or "invalid api key" in head or "no api key" in head:
        return "authentication_error"
    # Acme's upstream also says it inside a 200 stream: "Requests have exceeded the throughput limit on your
    # Provisioned-Managed deployment" (2026-11-04), which neither the relay nor pi retries.
    if (head.startswith("429") or "rate limit" in head or "usage limit" in head or "too many requests" in head
            or "throughput limit" in head):
        return "rate_limited"
    return "root_failed"


class PiEventAdapter:
    def __init__(self, emit, context_window=0):
        self.emit = emit
        self.context_window = context_window
        self.steps = Steps(emit)
        self.notes = AnswerNotes(emit)
        self.texts = 0  # Notes so far: text-1, text-2, ...
        self.requested = False
        self.ttft = FirstToken(emit, TTFT_SOURCE)
        self.sent = None  # When pi sent (or is about to send) its next request.
        self.started = None  # When the request now streaming was sent; None when unknown.
        self.text = ""
        self.tokens = TurnTokens(MAX_RATE)

    @property
    def tools(self):
        return self.steps.steps

    def request_sent(self, now):
        """The prompt was written to pi: its first request goes out from here."""
        self.sent = now

    def accept(self, row, now):
        kind = row.get("type")
        message = row.get("message") if isinstance(row.get("message"), dict) else {}
        if kind in SEND_MARKS or (kind == "message_end" and message and message.get("role") != "assistant"):
            self.sent = now
        if kind == "agent_start" or (kind == "message_start" and message.get("role") == "assistant"):
            if not self.requested:  # pi's loop has begun: the status line moves on from 正在启动.
                self.requested = True
                self.emit("model_request", {"received_at": now})
        if kind == "message_start" and message.get("role") == "assistant":
            self.started, self.sent, self.text = self.sent, None, ""
        elif kind == "message_update":
            self.update(row.get("assistantMessageEvent") or {}, now)
        elif kind == "message_end" and message.get("role") == "assistant":
            self.finish(message, now)
        elif kind == "tool_execution_start" and isinstance(row.get("toolCallId"), str):
            self.steps.start(row["toolCallId"], str(row.get("toolName") or "tool").lower(),
                             tool_detail(row.get("args"), DETAIL_KEYS)[:500], now)
        elif kind == "tool_execution_end" and row.get("toolCallId") in self.steps:
            self.steps.finish(row["toolCallId"], not row.get("isError"), now)
        elif kind == "auto_retry_start":
            message = row.get("errorMessage")
            self.emit("progress_note", retry_note(row.get("attempt"), retry_reason(failure_code(message), str(message)), now))
        elif kind == "compaction_start":  # A repeat while one runs is that step, as for Claude Code's.
            self.steps.open_slot(COMPACT_SLOT, "context_compaction", now)
        elif kind == "compaction_end":  # One with none open changes no step.
            failed = row.get("aborted") or row.get("errorMessage") or not row.get("result")
            self.steps.close_slot(COMPACT_SLOT, not failed, now)

    def update(self, event, now):
        kind = event.get("type")
        if kind == "thinking_start":
            self.first_output(now)
            self.emit("model_reasoning", {})
        elif kind == "text_delta" and isinstance(event.get("delta"), str) and event["delta"]:
            self.first_output(now)
            self.text += event["delta"]
            self.emit("model_output", {"received_at": now})
            self.emit("answer_snapshot", {"text": self.text})
        elif kind == "toolcall_start":
            self.ttft.skip()  # A tool call came first: no TTFT that counts the tool's run.

    def first_output(self, now):
        """The turn's first output, timed from its request's send; no TTFT when that is unknown, nor from a
        later output."""
        self.ttft.output(self.started, now)
        self.ttft.skip()

    def finish(self, message, now):
        if message.get("stopReason") == "toolUse":
            text = message_texts(message).strip()
            if text:  # Words before a tool call are a progress note, not the answer.
                self.texts += 1
                self.notes.text(f"text-{self.texts}", text, show=False)
            self.notes.to_notes(now)
        usage = message.get("usage") if isinstance(message.get("usage"), dict) else {}
        counts = {name: usage.get(field) for name, field in (("output", "output"), ("input", "input"),
                  ("read", "cacheRead"), ("write", "cacheWrite"))}
        started, self.started = self.started, None
        if not self.tokens.add(*counts.values(), seconds=elapsed(started, now),
                               counted=message.get("stopReason") in COUNTED):
            return
        latest = sum(value for value in counts.values() if type(value) is int and value >= 0)
        extra = {"context_tokens": latest} if latest else {}
        if self.context_window:
            extra["context_window"] = self.context_window
        self.emit("token_usage", self.tokens.payload(**extra))
