"""The verified reply and the outcome of one turn of Codex, Pi or dsh.

A callback saves evidence; only the final outcome can authorize delivery (rule 5). Each failure
code has one text (core/result.py FAILURES), the Harness that failed named by ``{label}``; a route
may word some codes its own way (``error_texts``).
"""
from __future__ import annotations

from covey.core.harness import RunResult
from covey.core.result import FAILURES, ExecutionOutcome, VerifiedRootReply


def failure_text(code, label="Codex"):
    """The shared text of a failure code; transport_error's for a code without one."""
    return FAILURES.get(code, FAILURES["transport_error"]).format(label=label)


class ReplyError(RuntimeError):
    """A failure that ends the turn with ``code``."""

    def __init__(self, message, code="transport_error"):
        super().__init__(message)
        self.code = code


class ReplyState:
    """A callback saves evidence; only the final outcome can authorize delivery."""
    error = ReplyError  # What a failed save raises.

    def __init__(self, options, run_id="", publish=None):
        self.run_id = run_id
        self.backend = options.get("harness_id", "codex")
        self.label = options.get("harness_label", "Codex")  # Backends sharing this state name themselves in errors.
        self.texts = options.get("error_texts", {})  # A route's own wording for some failure codes.
        self.callback = publish
        self.enabled = bool(self.run_id) and callable(self.callback)
        # The shared saved-result store starts at generation 1. Every None
        # notification advances both sides, including the initial revocation.
        self.generation = 1
        self.reply = None
        self.storage_failed = False

    def invalidate(self):
        self.reply = None
        self.generation += 1
        if self.enabled:
            try:
                self.callback(None)
            except Exception:
                self.storage_failed = True
                raise self.error(failure_text("storage_failed"), "storage_failed") from None

    def preserve(self, session_id, boundary, message_ids, text):
        if not self.enabled:
            return
        self.reply = VerifiedRootReply(self.backend, self.run_id, session_id,
            tuple(boundary), tuple(message_ids), text, self.generation)
        try:
            self.callback(self.reply)
        except Exception:
            self.storage_failed = True
            raise self.error(failure_text("storage_failed"), "storage_failed") from None

    def result(self, text, session_id, *, status="completed", code="", identity=False,
               cleanup=False, protection=False):
        if self.storage_failed:
            status, code, protection = "failed", "storage_failed", False
        if status == "completed" and not (identity and cleanup and protection):
            status, code = "failed", "cleanup_unconfirmed"
        is_error = status != "completed"
        visible = (self.texts.get(code) or failure_text(code, self.label)) if is_error else text
        # The failure card says the route's wording too, not only the backend's text.
        detail = self.texts.get(code, "") if is_error else ""
        if not self.enabled:
            return RunResult(visible, session_id, is_error, failure_detail=detail)
        outcome = ExecutionOutcome(status, code, identity, cleanup, protection,
            status == "completed" and identity and cleanup and protection,
            self.reply.fingerprint if self.reply is not None else "")
        return RunResult(visible, session_id, is_error, self.reply, outcome, detail)

    def finish(self, text, session_id, status, code, *, identity, cleanup, protection=None, cancel_event=None,
               emit=None):
        """The turn's result once its process is gone: a completed turn stopped meanwhile is cancelled, and
        ``emit`` (RunRequest.emit) closes the trace first, which never fails the turn. ``protection``
        defaults to ``cleanup``."""
        if status == "completed" and cancel_event is not None and cancel_event.is_set():
            status, code = "cancelled", "cancelled"
        if emit is not None:
            try:
                emit("trace_finished", {}, None)
            except Exception:
                pass
        return self.result(text, session_id, status=status, code=code, identity=identity, cleanup=cleanup,
                           protection=cleanup if protection is None else protection)
