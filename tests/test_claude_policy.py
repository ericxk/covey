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
        child = refusal("frontier_llm", parent_tool_use_id="child-1")
        for rows in ((refusal(),), (child, refusal("frontier_llm", parent_tool_use_id="child-2")), (child, refusal()),
                     ({"type": "assistant", "session_id": SID, "message": {"id": "msg-1", "stop_reason": "refusal"}},),
                     (result(stop_reason="refusal"),)):
            with self.subTest(rows=rows):
                state = self.state()
                for row in rows:
                    state.accept(row)
                state.accept(assistant())
                state.accept(result())
                ended = state.finish(0, cleanup=True)
                self.assertEqual(ended.outcome.error_code, "policy_refusal")
                self.assertFalse(ended.outcome.resume_allowed)
                self.assertIsNone(ended.public_reply)
                self.assertNotIn("PRIVATE", ended.outcome.failure_reply())
                self.assertIsNone(plan(None, None, None, SimpleNamespace(outcome=ended.outcome)))

    def test_a_subagents_first_refusal_is_let_through_and_the_next_one_stops(self):
        """C1 (2026-11-18): one of four subagents refused stopped the turn and the other three with it. The first
        subagent refusal of a task is let through, seen once however many rows say it; the next stops the turn."""
        seen = []
        state = ClaudeReplyState(self.ctx, run_id=RUN, on_policy=lambda signal, tolerated: seen.append(
            (signal["child_id"], tolerated)))
        state.accept({"type": "system", "subtype": "init", "session_id": SID})
        state.accept(refusal("frontier_llm", parent_tool_use_id="child-1"))
        state.accept({"type": "assistant", "session_id": SID, "parent_tool_use_id": "child-1",
                      "message": {"id": "msg-child", "stop_reason": "refusal", "content": []}})
        self.assertIsNone(state.policy_event)
        self.assertEqual(seen, [("child-1", True)])
        state.accept(assistant())
        state.accept(result())
        state.accept({"type": "system", "subtype": "session_state_changed", "session_id": SID, "state": "idle"})
        ended = state.finish(0, cleanup=True)
        self.assertEqual(ended.outcome.status, "completed")
        self.assertIsNotNone(ended.public_reply)
        for second in (refusal("cyber", parent_tool_use_id="child-2"), refusal("cyber")):
            with self.subTest(second=second):
                state = ClaudeReplyState(self.ctx, run_id=RUN)
                state.accept({"type": "system", "subtype": "init", "session_id": SID})
                state.accept(refusal("frontier_llm", parent_tool_use_id="child-1"))
                state.accept(second)
                self.assertEqual(state.policy_event["category"], "cyber")
                state.accept(assistant())
                state.accept(result())
                self.assertEqual(state.finish(0, cleanup=True).outcome.error_code, "policy_refusal")

    def test_the_wire_lets_one_subagent_refusal_through_and_says_who_and_doing_what(self):
        def run(scenario):
            events = []
            ctx = dataclasses.replace(self.ctx, timeout=5, backend_cmd=[sys.executable, str(CLAUDE_JSONL), scenario])
            answer = ClaudeBackend().run(RunRequest(ctx, "synthetic", callbacks=RunCallbacks(
                event=lambda kind, data, control: events.append((kind, data))),
                inputs=RunInputs(threading.Event(), run_id=RUN)))
            return answer, [data for kind, data in events if kind == "policy_tolerated"]
        answer, tolerated = run("policy-child-once")
        self.assertEqual(answer.outcome.status, "completed")
        self.assertEqual(answer.text, "主任务绕开了被拦的那块")
        self.assertEqual([row["text"] for row in tolerated],
                         ["子代理「标注第 1 块」在读 chunk01.txt 第 1201–1600 行时被拦（cyber），主任务继续"])
        self.assertNotIn("PRIVATE", json.dumps(tolerated, ensure_ascii=False))
        answer, tolerated = run("policy-child-refusal")
        self.assertEqual(answer.outcome.error_code, "policy_refusal")
        self.assertEqual(len(tolerated), 1)
        self.assertIn("本轮已停止：子代理「标注第 2 块」在读 chunk02.txt 第 1201–1600 行时被拦（cyber）。"
                      "这是本任务第 2 次被拦（之前子代理「标注第 1 块」在读 chunk01.txt 第 1201–1600 行时被拦过一次）",
                      answer.outcome.failure_reply(answer.failure_detail))
        self.assertTrue(answer.text.startswith("本轮已停止：子代理「标注第 2 块」"))
        answer, _ = run("policy-child-main")
        self.assertEqual(answer.outcome.policy_event["scope"], "main")
        self.assertIn("本轮已停止：主任务", answer.text)

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
            for _ in range(1000):  # Up to 10 s: a loaded machine takes longer than 1 s to reap it.
                if process_identity(child) is None:
                    break
                threading.Event().wait(0.01)
            self.assertIsNone(process_identity(child))
            self.assertIsNone(other.poll())
        finally:
            other.terminate()
            other.wait(timeout=5)
