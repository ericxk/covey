"""Claude result boundaries. Partial events and transcripts never prove completion."""

from __future__ import annotations

import contextlib
import json
import uuid

from covey.core.harness import RunResult, UUID_RE
from covey.core.result import ExecutionOutcome, MAX_REPLY_BYTES, VerifiedRootReply
from covey.core.state import write_private_text
from covey.backends.claude.policy import policy_signal

TERMINAL_TASK_STATES = frozenset({"completed", "failed", "stopped"})
# Failures that say only that the turn did not end well: a rate limit Claude Code gave up on
# in this turn names them better. 529 overloaded is not a rate limit here, as in the other backends.
RATE_LIMIT_MASKED = frozenset({"root_failed", "root_incomplete", "output_missing", "process_exit", "settle_timeout"})


def result_failure(row):
    """Return a public category, keeping raw model/API errors in private diagnostics."""
    if not isinstance(row, dict) or row.get("type") != "result":
        return "failed", "output_missing"
    reason = row.get("terminal_reason")
    if reason in ("aborted_streaming", "aborted_tools"):
        return "cancelled", "cancelled"
    subtype = row.get("subtype")
    if subtype == "error_max_turns" or reason == "max_turns":
        return "failed", "max_turns"
    if subtype == "error_max_budget_usd":
        return "failed", "budget_exceeded"
    if row.get("api_error_status") in (401, 403):
        return "failed", "authentication_error"
    if row.get("api_error_status") == 402:  # A used-up upstream quota: no retry helps until it resets.
        return "failed", "quota_exhausted"
    if row.get("api_error_status") == 451:  # An upstream's block of the account after repeated refusals.
        return "failed", "model_blocked"
    if row.get("api_error_status") == 429 or (
            row.get("is_error") is True and "rate_limit_error" in str(row.get("errors") or "") + str(row.get("result") or "")):
        return "failed", "rate_limited"
    if subtype != "success" or row.get("is_error") is not False or row.get("errors"):
        return "failed", "root_failed"
    if reason not in (None, "completed") or row.get("stop_reason") not in (None, "end_turn", "stop_sequence"):
        return "failed", "root_incomplete"
    if not isinstance(row.get("result"), str) or not row["result"].strip():
        return "failed", "output_missing"
    return "completed", ""


def save_diagnostic(ctx, run_id, row, status, code, *, cleanup=True, policy_event=None):
    # Never persist the prompt, credentials, tool results or the entire stream.
    fields = {
        key: row.get(key)
        for key in ("subtype", "is_error", "terminal_reason", "stop_reason", "api_error_status", "errors")
        if isinstance(row, dict) and key in row
    }
    for key in ("errors",):
        if key in fields:
            fields[key] = str(fields[key])[:8192]
    write_private_text(
        ctx.state_dir / "claude-diagnostics" / (run_id + ".json"),
        json.dumps({"version": 1, "status": status, "error_code": code, **({} if cleanup else {"cleanup_confirmed": False}),
                    **fields, **({"policy_event": policy_event} if policy_event else {})}, ensure_ascii=False),
    )


class ClaudeReplyState:
    def __init__(self, ctx, expected_session=None, *, run_id="", publish=None, on_session=None):
        self.ctx = ctx
        self.run_id = run_id or str(uuid.uuid4())
        if str(uuid.UUID(self.run_id)) != self.run_id:
            raise ValueError("Invalid Claude run identity")
        self.expected_session = expected_session
        self.session_id = None
        self.generation = 1
        self.publish = publish or (lambda _: None)
        self.on_session = on_session or (lambda _: None)
        self.messages = {}
        self.progress_ids = set()
        self.text_bytes = 0
        self.result = None
        self.candidate = None
        self.identity_error = False
        self.inputs = []
        self.acknowledged = set()
        self.tasks = {}
        # Child end states that arrived before the current root result. Claude Code
        # stays running until the root model has processed queued task
        # notifications, so these were handled by the turn that produced it.
        self.reported_tasks = set()
        self.session_state = None
        # Claude Code gave up on a rate limit in the current turn (an assistant row's error).
        self.rate_limited = False
        self.policy_event = None

    def invalidate(self):
        self.publish(None)
        self.generation += 1
        self.candidate = self.result = None
        self.session_state = None

    def submit(self, input_id):
        if self.inputs:
            self.invalidate()
        self.inputs.append(input_id)
        self.session_state = None
        self.rate_limited = False

    def accept(self, row):
        if self.session_id and row.get("session_id") == self.session_id and not self.policy_event:
            signal = policy_signal(row)
            if signal:
                self.policy_event = signal
                self.invalidate()
        if self.policy_event:
            return False
        if row.get("parent_tool_use_id") is not None:
            return False
        kind, subtype = row.get("type"), row.get("subtype")
        sid = row.get("session_id")
        if kind == "system" and subtype == "init":
            if (
                not isinstance(sid, str)
                or not UUID_RE.fullmatch(sid)
                or self.session_id not in (None, sid)
                or self.expected_session not in (None, sid)
            ):
                self.identity_error = True
                return False
            self.session_id = sid
            self.on_session(sid)
            return True
        if not self.session_id or sid != self.session_id:
            if kind == "result":
                self.identity_error = True
            return False
        if kind == "user" and row.get("uuid") in self.inputs:
            if self.result is not None and row["uuid"] not in self.acknowledged:
                # Claude Code queued this input until the turn ended and now starts a turn for it:
                # that result, success or failure, is no longer the answer (task #1556's 502).
                self.invalidate()
            self.acknowledged.add(row["uuid"])
        elif kind == "system" and subtype == "session_state_changed":
            self.session_state = row.get("state")
        elif kind == "system" and subtype in {"task_started", "task_progress", "task_notification"}:
            tid = row.get("task_id")
            state = row.get("status", "running") if subtype == "task_notification" else "running"
            if isinstance(tid, str) and tid and not (state == "running" and self.tasks.get(tid) in TERMINAL_TASK_STATES):
                self.tasks[tid] = state
        elif kind in {"assistant", "stream_event"}:
            if self.result is not None:
                self.invalidate()
            if kind == "assistant" and row.get("error") == "rate_limit":
                self.rate_limited = True
            if kind == "assistant":
                message = row.get("message")
                message = message if isinstance(message, dict) else {}
                content = message.get("content")
                key = row.get("uuid") or message.get("id")
                if isinstance(key, str) and key and isinstance(content, list) and not row.get("error"):
                    body = "\n".join(
                        b["text"]
                        for b in content
                        if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
                    ).strip()
                    if body:
                        self.text_bytes += len(body.encode("utf-8")) - len(self.messages.get(key, "").encode("utf-8"))
                        self.messages[key] = body
                        if len(self.messages) > 20000 or self.text_bytes > MAX_REPLY_BYTES:
                            raise ValueError("Claude public reply exceeds size limit")
                    if any(isinstance(b, dict) and b.get("type") == "tool_use"
                           and b.get("name") != "AskUserQuestion" for b in content):
                        self.progress_ids.update(self.messages)
        elif kind == "result":
            # Background notifications belong to this run, but cannot replace the
            # human result with a synthetic empty per-notification result.
            if row.get("origin") not in (None, {"kind": "human"}):
                origin = row.get("origin")
                if not isinstance(origin, dict) or origin.get("kind") != "task-notification":
                    self.identity_error = True
                    return False
                if not row.get("result") and row.get("num_turns") == 0:
                    # Claude Code handled the notification without a model turn.
                    self.reported_tasks = {tid for tid, state in self.tasks.items() if state in TERMINAL_TASK_STATES}
                    return True
            if self.result is not None and self.result != row:
                self.invalidate()
            self.result = row
            self.reported_tasks = {tid for tid, state in self.tasks.items() if state in TERMINAL_TASK_STATES}
        return True

    def ready(self):
        return (
            self.result is not None
            and self.session_state == "idle"
            and set(self.inputs) == self.acknowledged
            and all(s in TERMINAL_TASK_STATES for s in self.tasks.values())
        )

    def settled(self):
        """Ready, and every task had ended before the current result. A background
        task that ends after it makes Claude Code start a notification turn a moment
        later; closing input before that turn's own result would cut the turn off."""
        return self.ready() and set(self.tasks) <= self.reported_tasks

    def idle_after_reply(self):
        """Idle after a complete root reply while background tasks are still to report: what the
        end-wait limit cuts off. That reply is the round's latest answer, so it is still sent."""
        return (self.result is not None and self.session_state == "idle"
                and set(self.inputs) == self.acknowledged and result_failure(self.result)[0] == "completed"
                and not self.settled())

    def capture(self):
        if self.policy_event or self.identity_error or not self.session_id or result_failure(self.result)[0] != "completed":
            return None
        body = self.result["result"].strip()
        # The authoritative result must agree with the final complete root text.
        # Stream deltas, child text and an old transcript are never fallback data.
        if not self.messages or body != next(reversed(self.messages.values())):
            return None
        final_messages = {key: value for key, value in self.messages.items() if key not in self.progress_ids}
        text = "\n\n".join(final_messages.values())
        if not text:
            return None
        if len(text.encode("utf-8")) > MAX_REPLY_BYTES:
            return None
        rid = self.result.get("uuid")
        if not isinstance(rid, str) or not rid:
            return None
        boundary = (
            "stream-json",  # Part of saved reply fingerprints; kept from when a second transport existed.
            "invocation:" + self.run_id,
            "result:" + rid,
            *("input:" + value for value in self.inputs),
        )
        candidate = VerifiedRootReply(
            self.ctx.options.get("harness_id", "claude"), self.run_id, self.session_id,
            boundary, tuple(final_messages), text, self.generation
        )
        if self.candidate != candidate:
            self.publish(candidate)
            self.candidate = candidate
        return candidate

    def unsettled_tasks(self):
        """A failed or stopped child the root already handled (for example a check
        that failed and was rerun) does not demote the root result."""
        return {
            state for tid, state in self.tasks.items() if state != "completed" and tid not in self.reported_tasks
        }

    def finish(self, rc, *, cleanup, failure=None, stopped=False):
        """The run's outcome. ``stopped``: the Bridge stopped a CLI that had not exited after a
        settled result, so its exit code says nothing about the turn."""
        status, code = failure or result_failure(self.result)
        if self.policy_event:
            status, code = "cancelled", "policy_" + self.policy_event["kind"]
        if self.identity_error:
            status, code = "failed", "identity_mismatch"
        elif status == "completed" and rc != 0 and not stopped:
            status, code = "failed", "process_exit"
        if status == "completed":
            candidate = self.capture()
            if candidate is None:
                status, code = "failed", "output_missing"
            elif not self.ready():
                status, code = "failed", "root_incomplete"
        candidate = self.candidate
        if status in ("failed", "timed_out") and code in RATE_LIMIT_MASKED and self.rate_limited and candidate is None:
            status, code = "failed", "rate_limited"
        if not cleanup:
            # Still a rate limit when that is why the turn ended: the outcome says cleanup is unconfirmed.
            status, code = "unknown", code if code == "rate_limited" else "cleanup_unconfirmed"
        elif status == "completed" and (unsettled := self.unsettled_tasks()):
            status = "failed"
            code = (
                "child_error"
                if "failed" in unsettled
                else "child_aborted"
                if "stopped" in unsettled
                else "child_unknown"
            )
        if status != "completed":
            # Diagnostics are an aid; failing to write them must not lose the outcome or a saved reply.
            with contextlib.suppress(OSError):
                save_diagnostic(self.ctx, self.run_id, self.result, status, code, cleanup=cleanup, policy_event=self.policy_event)
        outcome = ExecutionOutcome(
            status,
            code,
            bool(self.session_id) and not self.identity_error,
            cleanup,
            True,
            status == "completed" and candidate is not None,
            candidate.fingerprint if candidate else "",
            self.policy_event,
        )
        return RunResult(
            candidate.text if candidate else outcome.summary(),
            self.session_id,
            status != "completed",
            candidate,
            outcome,
        )
