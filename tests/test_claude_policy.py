"""Official refusal/fallback propagation and per-task stopping, without model calls."""
import dataclasses
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from support.fake_backends import CLAUDE_JSONL, RUN, SID, OTHER, assistant, codex_context, result
from covey.backends.claude.bridge import ClaudeBackend
from covey.backends.claude.completion import ClaudeReplyState
from covey.backends.claude.policy import policy_signal
from covey.core.harness import RunCallbacks, RunInputs, RunRequest
from covey.core.result import ExecutionOutcome
from covey.core.process import process_identity
from covey.app.auto_retry import plan
from covey.app.outbox import retry_label


def refusal(category="cyber", **extra):
    return {"type": "stream_event", "session_id": SID, "event": {"type": "message_delta", "delta": {
        "stop_reason": "refusal", "stop_details": {"type": "refusal", "category": category, "explanation": "PRIVATE"}}}, **extra}


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ctx = codex_context(self.root, "stream-json")

    def state(self):
        state = ClaudeReplyState(self.ctx, run_id=RUN)
        state.accept({"type": "system", "subtype": "init", "session_id": SID})
        return state

    def test_only_explicit_live_envelopes_trigger(self):
        for row in (assistant("cyber frontier_llm fallback"),
                    {"type": "user", "message": {"stop_reason": "refusal"}},
                    {"type": "assistant", "message": {"content": [{"type": "tool_use", "input": {"type": "fallback"}}]}},
                    refusal(isReplay=True),
                    {"type": "assistant", "message": {"model": "claude-opus-5"}}):
            self.assertIsNone(policy_signal(row))
        state = self.state()
        state.accept(refusal(session_id=OTHER))
        self.assertIsNone(state.policy_event)

    def test_root_child_and_whole_refusal_latch_before_later_success(self):
        for row in (refusal(), refusal("frontier_llm", parent_tool_use_id="child-1"),
                    {"type": "assistant", "session_id": SID, "message": {"id": "msg-1", "stop_reason": "refusal"}},
                    result(stop_reason="refusal")):
            with self.subTest(row=row):
                state = self.state()
                state.accept(row)
                state.accept(assistant())
                state.accept(result())
                ended = state.finish(0, cleanup=True)
                self.assertEqual(ended.outcome.error_code, "policy_refusal")
                self.assertFalse(ended.outcome.resume_allowed)
                self.assertIsNone(ended.public_reply)
                self.assertNotIn("PRIVATE", ended.outcome.failure_reply())
                self.assertIsNone(plan(None, None, None, SimpleNamespace(outcome=ended.outcome)))

    def test_fallback_blocks_and_iterations_are_not_name_comparisons(self):
        block = {"type": "fallback", "from": {"model": "claude-opus-5-5"}, "to": {"model": "claude-opus-5"},
                 "trigger": {"type": "refusal", "category": "frontier_llm"}}
        for row in ({"type": "assistant", "message": {"content": [block]}},
                    {"type": "stream_event", "event": {"type": "content_block_start", "content_block": block}},
                    {"type": "assistant", "message": {"usage": {"iterations": [{"type": "fallback_message"}]}}}):
            event = policy_signal(row)
            self.assertEqual(event["kind"], "fallback")
            self.assertTrue(ExecutionOutcome("cancelled", "policy_fallback", policy_event=event).is_policy_stop)

    def test_cleanup_failure_keeps_reason_and_blocks_every_automatic_retry(self):
        state = self.state()
        state.accept(refusal("frontier_llm"))
        ended = state.finish(1, cleanup=False, failure=("timed_out", "settle_timeout"))
        self.assertIn("清理尚未确认", ended.outcome.failure_reply())
        self.assertIn("frontier_llm", ended.outcome.failure_reply())
        self.assertIsNone(plan(None, None, None, SimpleNamespace(outcome=ended.outcome)))
        self.assertEqual(retry_label({"policy_stop": state.policy_event, "delivery_ok": True}), "")

    def test_diagnostics_store_metadata_without_original_explanation(self):
        state = self.state()
        state.accept(refusal())
        state.finish(1, cleanup=True)
        path = self.ctx.state_dir / "claude-diagnostics" / (RUN + ".json")
        text = path.read_text()
        self.assertEqual(json.loads(text)["policy_event"]["category"], "cyber")
        self.assertNotIn("PRIVATE", text)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_wire_event_stops_cli_and_never_delivers_later_success(self):
        for scenario in ("policy-refusal", "policy-child-refusal", "policy-fallback", "policy-child-fallback"):
            with self.subTest(scenario=scenario):
                processes, saved = [], []
                ctx = dataclasses.replace(self.ctx, timeout=5, backend_cmd=[sys.executable, str(CLAUDE_JSONL), scenario])
                answer = ClaudeBackend().run(RunRequest(ctx, "synthetic", callbacks=RunCallbacks(
                    process=processes.append, verified_reply=saved.append), inputs=RunInputs(threading.Event(), run_id=RUN)))
                self.assertEqual(answer.outcome.status, "cancelled")
                self.assertEqual(answer.outcome.error_code, "policy_" + ("fallback" if "fallback" in scenario else "refusal"))
                self.assertIsNone(answer.public_reply)
                self.assertFalse(any(saved))
                self.assertTrue(all(p.poll() is not None for p in processes))
                self.assertNotIn("LATER SUCCESS", answer.outcome.failure_reply())

    def test_policy_metadata_cannot_authorize_completed_or_resumable_reply(self):
        event = policy_signal(refusal())
        with self.assertRaises(ValueError):
            ExecutionOutcome("completed", policy_event=event)
        with self.assertRaises(ValueError):
            ExecutionOutcome("cancelled", policy_event={**event, "category": "<script>"})
        stopped = ExecutionOutcome("cancelled", "policy_refusal", True, True, True, policy_event=event)
        self.assertFalse(stopped.delivery_authorized)
        self.assertEqual(stopped.to_dict()["policy_event"], event)

    def test_ordinary_saved_outcomes_keep_the_old_release_wire_shape(self):
        ordinary = ExecutionOutcome("completed", "", True, True, True, True)
        legacy_fields = {"status", "error_code", "identity_confirmed", "cleanup_confirmed",
                         "protection_confirmed", "resume_allowed", "reply_fingerprint"}
        self.assertEqual(set(ordinary.to_dict()), legacy_fields)
        self.assertEqual(ExecutionOutcome(**ordinary.to_dict()), ordinary)

    def test_policy_stop_kills_a_child_that_ignores_term_not_another_task(self):
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
        try:
            ctx = dataclasses.replace(self.ctx, timeout=5,
                backend_cmd=[sys.executable, str(CLAUDE_JSONL), "policy-child-survivor"])
            answer = ClaudeBackend().run(RunRequest(ctx, "synthetic", inputs=RunInputs(threading.Event(), run_id=RUN)))
            self.assertEqual(answer.outcome.error_code, "policy_refusal")
            child = int((ctx.workdir / 'policy-child.pid').read_text())
            for _ in range(100):
                if process_identity(child) is None:
                    break
                threading.Event().wait(0.01)
            self.assertIsNone(process_identity(child))
            self.assertIsNone(other.poll())
        finally:
            other.terminate()
            other.wait(timeout=5)
