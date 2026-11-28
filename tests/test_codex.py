"""Codex over the App Server: verified replies, the RPC transport, sandbox permissions,
and the read-only workspace tools.

The shared delivery invariants (saved redelivery, sealed sessions) are in test_backend_contract.
"""

from __future__ import annotations

import dataclasses
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support import REPO as ROOT, exec_tempdir
from support.fake_backends import (
    codex_server,
    AppServerProcess,
    CodexRunCase,
    FakeRpc,
    OTHER,
    RUN,
    SECRET,
    SID,
    TURN,
    agent,
    codex_context,
    finished,
    turn,
)

from covey.app.unified import UnifiedBridgeApp
from covey.backends.codex.appserver import CodexReplyState
from covey.backends.codex.rpc import AppServer, RpcError
from covey.backends.codex.bridge import CodexBackend
from covey.backends.codex.workspace_reader import WorkspaceReader
from covey.core.harness import RunRequest, RunResult


class CodexVerifiedReplyTests(CodexRunCase):
    def test_failed_turn_preserves_safe_error_class_without_private_details(self):
        cases = [
            ({"message": "workspace routing discovery timed out", "codexErrorInfo": "other"},
             "workspace_routing_timeout"),
            ({"message": SECRET, "codexErrorInfo": "unauthorized"}, "authentication_error"),
            ({"message": SECRET, "codexErrorInfo": {"httpConnectionFailed": {"httpStatusCode": 401}}},
             "authentication_error"),
            ({"message": SECRET, "codexErrorInfo": {"httpConnectionFailed": {"httpStatusCode": 403}}},
             "authentication_error"),
            # Acme's spent daily quota is a 403 too (T272 #2611 read 拒绝了当前身份或权限).
            ({"message": "unexpected status 403 Forbidden: quota exceeded: Daily Limits exceeded. Resets at "
                         "2026-11-03T16:00:00Z.", "codexErrorInfo": {"httpConnectionFailed": {"httpStatusCode": 403}}},
             "quota_exhausted"),
            ({"message": SECRET, "codexErrorInfo": "usageLimitExceeded"}, "rate_limited"),
            ({"message": SECRET, "codexErrorInfo": "rateLimitExceeded"}, "rate_limited"),
            ({"message": SECRET, "codexErrorInfo": {"httpConnectionFailed": {"httpStatusCode": 429}}},
             "rate_limited"),
            # Codex's QuotaExceeded and UsageNotIncluded come as usageLimitExceeded: no retry helps those.
            ({"message": "Quota exceeded. Check your plan and billing details.", "codexErrorInfo": "usageLimitExceeded"},
             "quota_exhausted"),
            ({"message": "To use Codex with your ChatGPT plan, upgrade to Plus: https://chatgpt.com/explore/plus.",
              "codexErrorInfo": "usageLimitExceeded"}, "quota_exhausted"),
            # A passing server fault: by name, by a 5xx, or a connection that failed or dropped with no status.
            ({"message": SECRET, "codexErrorInfo": "serverOverloaded"}, "server_fault"),
            ({"message": SECRET, "codexErrorInfo": "internalServerError"}, "server_fault"),
            ({"message": SECRET, "codexErrorInfo": "flexUnavailable"}, "server_fault"),
            ({"message": SECRET, "codexErrorInfo": "responseStreamConnectionFailed"}, "server_fault"),
            ({"message": SECRET, "codexErrorInfo": {"responseStreamDisconnected": {"httpStatusCode": None}}},
             "server_fault"),
            ({"message": SECRET, "codexErrorInfo": {"httpConnectionFailed": {"httpStatusCode": 502}}}, "server_fault"),
            ({"message": SECRET, "codexErrorInfo": {"responseTooManyFailedAttempts": {"httpStatusCode": 529}}},
             "server_fault"),
            # A 5xx whose words are the account's is not a passing fault (T784).
            ({"message": "502: could not authenticate this account; sign in again",
              "codexErrorInfo": {"httpConnectionFailed": {"httpStatusCode": 502}}}, "authentication_error"),
            ({"message": "insufficient_quota", "codexErrorInfo": "internalServerError"}, "quota_exhausted"),
            # Any other status is the request refused: never retried.
            ({"message": SECRET, "codexErrorInfo": {"httpConnectionFailed": {"httpStatusCode": 400}}}, "transport_error"),
            ({"message": SECRET, "codexErrorInfo": {"responseStreamConnectionFailed": {"httpStatusCode": 404}}},
             "transport_error"),
            ({"message": SECRET, "codexErrorInfo": "other"}, "root_failed"),
            ({"message": "workspace routing discovery timed out " + SECRET}, "root_failed"),
            # The safety review's refusal, by name or by OpenAI's fixed sentence ("other" on codex-cli 0.167, T872).
            ({"message": "Invalid prompt: your prompt was flagged as potentially violating our usage policy. Please try "
                         "again with a different prompt: https://platform.openai.com/docs/guides/reasoning",
              "codexErrorInfo": "other"}, "policy_refusal"),
            ({"message": SECRET, "codexErrorInfo": "modelNotFound"}, "model_unavailable"),
        ] + [({"message": SECRET, "codexErrorInfo": name}, "policy_refusal") for name in (
            "cyberPolicy", "bioPolicy", "misalignmentPolicyViolation")] + [
            ({"message": SECRET, "codexErrorInfo": name}, "root_failed") for name in (
                "badRequest", "contextWindowExceeded", "sessionBudgetExceeded", "policyCapacityUnavailable",
                "tooManyDenials", "inferenceHookDenied", "sandboxError", "threadRollbackFailed")]
        for error, code in cases:
            with self.subTest(code=code, error=error.get("codexErrorInfo")):
                result = self.run_rpc(FakeRpc([finished(turn(status="failed", error=error, items=[]))]))
                self.assertTrue(result.is_error)
                self.assertEqual(result.outcome.error_code, code)
                self.assertFalse(result.outcome.resume_allowed)
                self.assertIsNone(result.public_reply)
                self.assertNotIn(SECRET, result.text + result.outcome.summary() + result.failure_detail)
                # What Codex said goes to the card's 技术详情 as it said it, kind first (T872: whatever the error).
                kind = error.get("codexErrorInfo")
                if isinstance(kind, str):
                    self.assertTrue(result.backend_said.startswith(f"{kind}: "), result.backend_said)
                self.assertIn(error["message"].split(" ")[0], result.backend_said)
        # A secret in Codex's words is hidden on the card, the rest of them kept.
        result = self.run_rpc(FakeRpc([finished(turn(status="failed", items=[], error={
            "codexErrorInfo": "badRequest", "message": "bad request: token=abc123SECRET at /absolute/someone/.codex/auth.json"}))]))
        self.assertIn("badRequest: bad request: token=", result.backend_said)
        self.assertNotIn("abc123SECRET", result.backend_said)
        self.assertNotIn("/absolute/someone", result.backend_said)
        # The app server's stderr: only its error lines are kept, for a failure no turn error explains (T872).
        import collections
        from types import SimpleNamespace
        from covey.backends.codex.rpc import AppServer
        server = SimpleNamespace(process=SimpleNamespace(stderr=iter([
            "user prompt: PRIVATE words\n", "2026-11-27T05:00:00Z ERROR codex_core::client: stream disconnected\n",
            "reasoning PRIVATE\n", "thread 'main' panicked at src/x.rs\n"])), error_lines=collections.deque(maxlen=5))
        AppServer._stderr(server)
        self.assertEqual([line.split(" ")[-1] for line in server.error_lines], ["disconnected", "src/x.rs"])
        self.assertNotIn("PRIVATE", " ".join(server.error_lines))
        # The fault's own words, not transport_error's 通信失败 they fell back to.
        result = self.run_rpc(FakeRpc([finished(turn(status="failed", error={"codexErrorInfo": "serverOverloaded"},
                                                     items=[]))]))
        self.assertIn("临时故障", result.text)

    def test_appserver_full_runtime_flow_multiple_finals(self):
        rpc = FakeRpc([finished(turn([agent("c", "comment", "commentary"),
            agent("f1", "First"), agent("f2", "Second")]))])
        result = self.run_rpc(rpc)
        self.assertIsInstance(result, RunResult)
        self.assertFalse(result.is_error)
        self.assertEqual(result.text, "First\n\nSecond")
        self.assertEqual(result.public_reply.message_ids, ("f1", "f2"))
        self.assertEqual(result.public_reply.boundary, ("app-server", SID, TURN))
        self.assertEqual(result.public_reply.generation, 2)
        self.assertEqual(self.saved, [None, result.public_reply])
        self.assertTrue(result.outcome.resume_allowed)
        self.assertTrue(result.outcome.delivery_authorized)

    def test_foreign_missing_and_other_turn_bodies_never_enter_reply(self):
        rows = []
        for tid, turn_id in [(OTHER, TURN), (SID, "old-turn"), (None, TURN), (SID, None)]:
            rows.append({"method": "item/agentMessage/delta", "params": {
                "threadId": tid, "turnId": turn_id, "itemId": "evil", "delta": SECRET}})
            rows.append({"method": "item/completed", "params": {
                "threadId": tid, "turnId": turn_id, "item": agent("evil", SECRET)}})
        rows.append(finished())
        result = self.run_rpc(FakeRpc(rows))
        self.assertEqual(result.text, "Verified answer")
        self.assertNotIn(SECRET, result.public_reply.text)

    def test_a_reasoning_start_is_reported_without_its_content(self):
        # Reasoning that streams no text gives no first token; its start says the model is at work.
        reasoning = {"type": "reasoning", "id": "r1", "summary": [SECRET], "content": [SECRET]}
        rows = [{"method": "item/started", "params": {"threadId": tid, "turnId": turn_id, "item": reasoning}}
                for tid, turn_id in [(OTHER, TURN), (SID, "old-turn"), (SID, TURN)]]
        rows.append({"method": "item/completed", "params": {"threadId": SID, "turnId": TURN, "item": reasoning}})
        self.run_rpc(FakeRpc(rows + [finished()]))
        self.assertEqual([event for event in self.events if event[0] == "model_reasoning"], [("model_reasoning", {})])
        self.assertNotIn(SECRET, json.dumps(self.events, default=str))

    def test_the_first_output_item_s_start_is_the_ttft_when_no_text_streams(self):
        # S29 #867 (GPT-6.1-Sol, max): reasoning streamed no text and the first output was a command, so
        # Codex had no TTFT at all. The reasoning item's start is its first sign of model output.
        now = time.time()
        def row(method, at, **params):
            return {"method": method, "_received_at": now + at, "params": {"threadId": SID, "turnId": TURN, **params}}
        reasoning = {"type": "reasoning", "id": "r1", "summary": [], "content": []}
        rows = [row("item/started", 1, item={"type": "userMessage", "id": "u1", "content": []}),
                row("item/started", 9, threadId=OTHER, item=reasoning),  # A sub-agent's reasoning.
                row("item/started", 25, item=reasoning),
                row("bridge/modelToken", 40), row("bridge/modelToken", 41),
                row("item/started", 42, item={"type": "agentMessage", "id": "a1", "text": ""})]
        self.run_rpc(FakeRpc(rows + [finished()]))
        kinds = [kind for kind, _ in self.events if kind in {"model_reasoning", "first_token", "model_output"}]
        self.assertEqual(kinds, ["model_reasoning", "first_token", "model_output"])  # Reasoning reaches the line first.
        first = next(data for kind, data in self.events if kind == "first_token")
        self.assertEqual(first["ttft_source"], "codex_item")
        self.assertAlmostEqual(first["ttft_seconds"], 25, delta=1)

    def test_a_command_before_any_reasoning_or_answer_gives_no_ttft(self):
        # S31 K4 (GPT-6.1-Sol, low): the first output was `sleep 45`, written about 5 s in; TTFT read
        # 17.00 s from a reasoning item during the command's polls. A tool item starts once the call is
        # written, so the first token is not known (执行指标口径: 测不到时不写).
        now = time.time()
        def row(method, at, **params):
            return {"method": method, "_received_at": now + at, "params": {"threadId": SID, "turnId": TURN, **params}}
        rows = [row("item/started", 1, item={"type": "userMessage", "id": "u1", "content": []}),
                row("item/started", 5, item={"type": "commandExecution", "id": "c1", "command": "sleep 45"}),
                row("item/started", 17, item={"type": "reasoning", "id": "r1", "summary": [], "content": []}),
                row("bridge/modelToken", 50),
                row("item/completed", 51, item={"type": "commandExecution", "id": "c1", "command": "sleep 45", "exitCode": 0})]
        self.run_rpc(FakeRpc(rows + [finished()]))
        self.assertEqual([data for kind, data in self.events if kind == "first_token"], [])
        self.assertEqual([kind for kind, _ in self.events if kind in {"model_reasoning", "model_output"}],
                         ["model_reasoning", "model_output"])  # The status line still hears of both.

    def test_token_rate_counts_only_output_that_came_with_generation_time(self):
        # S31 K4: while `sleep 45` ran, Codex polled it in model requests that the clock (stopped for
        # the open command) did not time; 平均 token/s climbed 6.4 -> 42.4 over the same 5.3 s.
        now = time.time()
        def row(method, at, **params):
            return {"method": method, "_received_at": now + at, "params": {"threadId": SID, "turnId": TURN, **params}}
        def usage(at, output):
            return row("thread/tokenUsage/updated", at, tokenUsage={
                "total": {"inputTokens": 30 * output, "outputTokens": output},
                "last": {"inputTokens": 30 * output, "outputTokens": output}})
        command = {"type": "commandExecution", "id": "c1", "command": "sleep 45"}
        rows = [row("item/started", 5, item=command), usage(6, 34), usage(20, 72), usage(30, 148), usage(40, 224),
                row("item/completed", 50, item={**command, "exitCode": 0}), usage(55, 260)]
        self.run_rpc(FakeRpc(rows + [finished()]))
        seen = [(d["output_tokens"], d["measured_output_tokens"], round(d["generation_seconds"]))
                for kind, d in self.events if kind == "token_usage"]
        self.assertEqual(seen, [(34, 34, 5), (72, 34, 5), (148, 34, 5), (224, 34, 5), (260, 70, 10)])
        # The card says which output its rate counts: 输出 ÷ 模型生成 is not it.
        from covey.core.trace import ToolTrace
        from covey.cards.streaming import usage_lines
        from support.cards import figures_line
        trace = ToolTrace()
        trace.accept("token_usage", [d for kind, d in self.events if kind == "token_usage"][-1])
        self.assertEqual(usage_lines(trace.snapshot()), [])  # The 输入/输出 box's since 2026-11-09.
        self.assertIn(" token/s（只算模型生成计时内的 70 token）", figures_line(trace.snapshot()))

    def test_the_context_tile_shows_the_latest_request_of_the_window(self):
        # The owner, 2026-11-04 17:54 (T386 #2790): 这里的上下文为啥显示 -. Codex gave it all along: its rollout's
        # last request was 87,300 tokens of a 258,400 window (codex 0.166 ThreadTokenUsage last / modelContextWindow).
        now = time.time()
        def usage(at, output, last, window):
            return {"method": "thread/tokenUsage/updated", "_received_at": now + at, "params": {
                "threadId": SID, "turnId": TURN, "tokenUsage": {
                    "total": {"inputTokens": 100000 + output, "cachedInputTokens": 0, "outputTokens": output,
                              "reasoningOutputTokens": 0, "totalTokens": 100000 + 2 * output},
                    "last": {"inputTokens": last - output, "cachedInputTokens": 0, "outputTokens": output,
                             "reasoningOutputTokens": 0, "totalTokens": last},
                    "modelContextWindow": window}}}
        self.run_rpc(FakeRpc([usage(2, 10, 40000, 258400), usage(4, 57, 87300, 258400), finished()]))
        usages = [d for kind, d in self.events if kind == "token_usage"]
        self.assertEqual([(d["context_tokens"], d["context_window"]) for d in usages], [(40000, 258400), (87300, 258400)])
        from covey.cards.task_figures import figure_cells
        from covey.core.trace import ToolTrace
        trace = ToolTrace()
        for data in usages:
            trace.accept("token_usage", data)
        self.assertEqual(figure_cells({**trace.snapshot(), "ended_at": trace.started_at + 1})[3], ("上下文", "34%", ""))
        # A window Codex does not know: the size alone; no last request: nothing, as before.
        from covey.backends.codex.turn import context_of
        self.assertEqual(context_of({"last": {"totalTokens": 87300}, "modelContextWindow": None}), {"context_tokens": 87300})
        self.assertEqual(context_of({"total": {}}), {})

    def test_streamed_text_before_any_item_start_is_still_the_ttft(self):
        now = time.time()
        rows = [{"method": "bridge/modelToken", "_received_at": now + 3, "params": {"threadId": SID, "turnId": TURN}}]
        self.run_rpc(FakeRpc(rows + [finished()]))
        first = [data for kind, data in self.events if kind == "first_token"]
        self.assertEqual([d["ttft_source"] for d in first], ["codex_delta"])
        self.assertEqual([kind for kind, _ in self.events if kind == "model_output"], ["model_output"])

    def test_summary_and_not_loaded_require_exact_full_read(self):
        for view in ("summary", "notLoaded"):
            with self.subTest(view=view):
                rpc = FakeRpc([finished(turn([agent(text="summary")], itemsView=view))],
                    snapshot={"thread": {"id": SID, "turns": [turn(), turn(id="newer-turn")]}})
                result = self.run_rpc(rpc)
                self.assertEqual(result.text, "Verified answer")
                self.assertIn(("thread/read", {"threadId": SID, "includeTurns": True}), rpc.requests)

    def test_readback_rejects_wrong_thread_missing_turn_or_incomplete_view(self):
        for snapshot in ({"thread": {"id": OTHER, "turns": [turn()]}},
                         {"thread": {"id": SID, "turns": [turn(id="other-turn")]}},
                         {"thread": {"id": SID, "turns": [turn(itemsView="summary")]}}):
            with self.subTest(snapshot=snapshot):
                result = self.run_rpc(FakeRpc([finished(turn(itemsView="summary"))], snapshot=snapshot))
                self.assertTrue(result.is_error)
                self.assertIsNone(result.public_reply)

    def test_null_and_missing_phase_compatible_but_commentary_excluded(self):
        unphased = agent("legacy", "Legacy", None)
        del unphased["phase"]
        result = self.run_rpc(FakeRpc([finished(turn([agent("comment", SECRET, "commentary"),
            agent("null", "Null", None), unphased]))]))
        self.assertEqual(result.text, "Null\n\nLegacy")

    def test_invalid_phase_view_empty_and_delta_only_rejected(self):
        for value in [turn([agent(phase="FINAL")]), turn(itemsView="Full"), turn([]),
                      turn([agent(phase="commentary")]), turn([agent(text="")])]:
            with self.subTest(value=value):
                rpc = FakeRpc([{"method": "item/agentMessage/delta", "params": {
                    "threadId": SID, "turnId": TURN, "itemId": "x", "delta": SECRET}}, finished(value)])
                result = self.run_rpc(rpc)
                self.assertTrue(result.is_error)
                self.assertIsNone(result.public_reply)
                self.assertNotIn(SECRET, result.text)

    def test_native_question_interrupt_keeps_dedicated_contract(self):
        rpc = FakeRpc([{"id": 7, "method": "item/tool/requestUserInput", "params": {
            "threadId": SID, "turnId": TURN, "questions": []}}, finished(turn(status="interrupted"))])
        def event(kind, data, control):
            if kind == "question":
                control.wait_for_answer("需要用户补充")
        result = self.run_rpc(rpc, event_hook=event)
        self.assertFalse(result.is_error)
        self.assertEqual(result.text, "需要用户补充")
        self.assertIsNone(result.public_reply)
        self.assertIsNone(result.outcome)

    def test_steering_revokes_and_advances_generation_before_input(self):
        rpc = FakeRpc([finished()])
        def control(owner):
            if owner is not None:
                owner.steer("more input", "steer-1")
        result = self.run_rpc(rpc, control_hook=control)
        self.assertEqual(self.saved[:2], [None, None])
        self.assertEqual(result.public_reply.generation, 3)
        self.assertIn("steer:steer-1", result.public_reply.boundary)
        with self.assertRaises(Exception):
            rpc.steer("too late", "late")

    def test_resume_identity_mismatch_does_not_submit_turn(self):
        rpc = FakeRpc([], start_sid=OTHER)
        result = self.run_rpc(rpc, session=SID)
        self.assertTrue(result.is_error)
        self.assertFalse(any(name == "turn/start" for name, _ in rpc.requests))
        self.assertIsNone(result.public_reply)

    def test_post_completion_callback_failure_preserves_partial(self):
        def event(kind, data, control):
            if kind == "turn_finished":
                raise RuntimeError(SECRET)
        result = self.run_rpc(FakeRpc([finished()]), event_hook=event)
        self.assertTrue(result.is_error)
        self.assertIsNotNone(result.public_reply)
        self.assertTrue(result.outcome.delivery_authorized)
        self.assertFalse(result.outcome.resume_allowed)
        self.assertNotIn(SECRET, result.text)

    def test_cleanup_failure_only_preserves_without_authorization(self):
        result = self.run_rpc(FakeRpc([finished()], close_error=True))
        self.assertTrue(result.is_error)
        self.assertIsNotNone(result.public_reply)
        self.assertFalse(result.outcome.cleanup_confirmed)
        self.assertFalse(result.outcome.delivery_authorized)

    def test_process_failure_after_normal_turn_retains_failure(self):
        result = self.run_rpc(FakeRpc([finished()], exit_code=1))
        self.assertEqual(result.outcome.status, "failed")
        self.assertEqual(result.outcome.error_code, "process_exit")
        self.assertIsNotNone(result.public_reply)
        self.assertFalse(result.outcome.resume_allowed)

    def test_cancel_after_verified_reply_keeps_stopped_fact(self):
        def save(reply):
            self.saved.append(reply)
            if reply is not None:
                self.cancel.set()
        result = self.run_rpc(FakeRpc([finished()]), save_hook=save)
        self.assertEqual(result.outcome.status, "cancelled")
        self.assertIsNotNone(result.public_reply)
        self.assertFalse(result.outcome.resume_allowed)

    def test_long_public_reply_not_progress_truncated(self):
        body = "完整正文" * 6000
        result = self.run_rpc(FakeRpc([finished(turn([agent(text=body)]))]))
        self.assertEqual(result.public_reply.text, body)
        self.assertEqual(result.text, body)

    def test_saved_callback_failure_does_not_authorize_resume(self):
        def save(reply):
            if reply is not None:
                raise OSError(SECRET)
        result = self.run_rpc(FakeRpc([finished()]), save_hook=save)
        self.assertIsNotNone(result.public_reply)
        self.assertEqual(result.outcome.error_code, "storage_failed")
        self.assertFalse(result.outcome.delivery_authorized)
        self.assertNotIn(SECRET, result.text)

    def test_native_answer_resolution_without_repeated_identity(self):
        question = {"id": 7, "method": "item/tool/requestUserInput", "params": {
            "threadId": SID, "turnId": TURN, "questions": []}}
        resolved = {"method": "serverRequest/resolved", "params": {"requestId": 7}}
        rpc = FakeRpc([question, resolved, finished()])
        def event(kind, data, control):
            if kind == "question":
                control.answer(7, {"answer": ["yes"]})
        result = self.run_rpc(rpc, event_hook=event)
        self.assertFalse(result.is_error)
        self.assertEqual(result.public_reply.generation, 3)
        self.assertIn("answer:7", result.public_reply.boundary)
        self.assertEqual(rpc.user_requests, set())

    def test_cancel_during_full_read_cannot_create_new_candidate(self):
        rpc = FakeRpc([finished(turn(itemsView="summary"))],
            snapshot={"thread": {"id": SID, "turns": [turn()]}})
        request = rpc.request
        def cancelling(method, params, timeout=30):
            if method == "thread/read":
                self.cancel.set()
            return request(method, params, timeout)
        rpc.request = cancelling
        result = self.run_rpc(rpc)
        self.assertEqual(result.outcome.status, "cancelled")
        self.assertIsNone(result.public_reply)
        self.assertEqual(self.saved, [None])

    def test_completed_notification_missing_thread_cannot_finish(self):
        row = finished()
        del row["params"]["threadId"]
        result = self.run_rpc(FakeRpc([row]))
        self.assertTrue(result.is_error)
        self.assertIsNone(result.public_reply)

    def test_completed_turn_with_error_field_cannot_create_candidate(self):
        result = self.run_rpc(FakeRpc([finished(turn(error={"message": SECRET}))]))
        self.assertTrue(result.is_error)
        self.assertIsNone(result.public_reply)
        self.assertNotIn(SECRET, result.text)

    def test_verified_callback_can_save_private_fixture_before_rpc_cleanup(self):
        rpc = FakeRpc([finished()])
        target = self.root / "private-result.json"
        def save(reply):
            if reply is None:
                return
            self.assertFalse(rpc.closed)
            fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w") as output:
                json.dump(reply.to_dict(), output)
        result = self.run_rpc(rpc, save_hook=save)
        self.assertEqual(json.loads(target.read_text())["text"], result.public_reply.text)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        # This is a fixture callback, not the shared worker/persistence module.

    def test_appserver_close_requires_exit_and_drained_rpc_readers(self):
        rpc = object.__new__(AppServer)
        rpc.input_lock = threading.RLock()
        rpc.accepting_input = True
        rpc.turn_id = TURN
        rpc.process = AppServerProcess(0)
        reader = mock.Mock()
        reader.is_alive.return_value = False
        rpc.readers = [reader]
        self.assertTrue(rpc.close())
        self.assertFalse(rpc.accepting_input)
        reader.is_alive.return_value = True
        self.assertFalse(rpc.close())

    def test_unconfirmed_steer_invalidates_without_replaying(self):
        rpc = FakeRpc([finished()])
        original = rpc.request
        def request(method, params, timeout=30):
            if method == "turn/steer":
                return {"turnId": "different-turn"}
            return original(method, params, timeout)
        rpc.request = request
        def control(owner):
            if owner is not None:
                with self.assertRaises(Exception):
                    owner.steer("private additional input", "steer-1")
        result = self.run_rpc(rpc, control_hook=control)
        self.assertTrue(result.is_error)
        self.assertIsNone(result.public_reply)
        self.assertEqual(self.saved, [None, None])

    def real_saved_observer(self):
        from covey.core.saved_results import observe_verified_reply
        ctx = codex_context(self.root, "app-server")
        job = SimpleNamespace(job_id=17, message_id="codex-real-save-fixture", chat_id="chat-fixture",
                              topic_id="topic-fixture", bridge_run_id=RUN)
        def observe(reply):
            observe_verified_reply(ctx, job, "codex", reply)
        return ctx, job, observe

    def read_real_candidate(self, ctx, job):
        path = ctx.state_dir / "verified-results" / (job.verified_result_key + ".json")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        return json.loads(path.read_text())

    def test_real_observer_accepts_first_appserver_candidate(self):
        ctx, job, observe = self.real_saved_observer()
        result = self.run_rpc(FakeRpc([finished()]), save_hook=observe)
        self.assertFalse(result.is_error)
        self.assertFalse(result.outcome.error_code)
        self.assertEqual(result.public_reply.generation, 2)
        saved = self.read_real_candidate(ctx, job)
        self.assertEqual(saved["state"], "candidate")
        self.assertEqual(saved["generation"], result.public_reply.generation)
        self.assertEqual(saved["reply"]["generation"], 2)
        self.assertEqual(saved["fingerprint"], result.public_reply.fingerprint)
        self.assertEqual(job.verified_reply, result.public_reply)
        self.assertNotIn("authorization", saved)
        self.assertFalse(ctx.session_path.exists())

    def test_real_observer_revokes_previous_generation_before_new_reply(self):
        from covey.core.saved_results import observe_verified_reply
        ctx, job, observe = self.real_saved_observer()
        state = CodexReplyState({}, RUN, observe)
        state.invalidate()
        state.preserve(SID, ("app-server", SID, TURN), ("first",), "First complete reply")
        first = state.reply
        self.assertEqual(first.generation, 2)
        self.assertEqual(self.read_real_candidate(ctx, job)["state"], "candidate")
        state.invalidate()
        invalidated = self.read_real_candidate(ctx, job)
        self.assertEqual(invalidated["state"], "invalidated")
        self.assertEqual(invalidated["generation"], 3)
        self.assertIsNone(job.verified_reply)
        with self.assertRaises(ValueError):
            observe_verified_reply(ctx, job, "codex", first)
        state.preserve(SID, ("app-server", SID, TURN, "steer:next"), ("next",), "New complete reply")
        saved = self.read_real_candidate(ctx, job)
        self.assertEqual(saved["state"], "candidate")
        self.assertEqual(saved["generation"], 3)
        self.assertEqual(saved["reply"]["generation"], 3)
        self.assertEqual(saved["fingerprint"], state.reply.fingerprint)
        self.assertNotEqual(saved["fingerprint"], first.fingerprint)
        self.assertNotIn("authorization", saved)
        self.assertFalse(ctx.session_path.exists())

    def test_real_observer_consecutive_revocations_before_any_candidate(self):
        ctx, job, observe = self.real_saved_observer()
        state = CodexReplyState({}, RUN, observe)
        for generation in (2, 3, 4):
            state.invalidate()
            saved = self.read_real_candidate(ctx, job)
            self.assertEqual(saved["generation"], generation)
            self.assertEqual(saved["state"], "invalidated")
            self.assertEqual(state.generation, generation)
            self.assertIsNone(job.verified_reply)
        state.preserve(SID, ("app-server", SID, TURN), ("last",), "Current reply")
        self.assertEqual(self.read_real_candidate(ctx, job)["reply"]["generation"], 4)

    def test_real_observer_steer_advances_before_current_reply(self):
        ctx, job, observe = self.real_saved_observer()
        rpc = FakeRpc([finished()])
        def control(owner):
            if owner is not None:
                self.assertEqual(self.read_real_candidate(ctx, job)["generation"], 2)
                owner.steer("new input", "steer-current")
                self.assertEqual(self.read_real_candidate(ctx, job)["generation"], 3)
                self.assertIsNone(job.verified_reply)
        result = self.run_rpc(rpc, control_hook=control, save_hook=observe)
        self.assertFalse(result.is_error)
        self.assertEqual(result.public_reply.generation, 3)
        self.assertIn("steer:steer-current", result.public_reply.boundary)
        saved = self.read_real_candidate(ctx, job)
        self.assertEqual(saved["reply"]["generation"], 3)
        self.assertEqual(saved["fingerprint"], result.public_reply.fingerprint)

    def test_real_observer_write_failure_exits_without_authorization(self):
        import covey.core.saved_results as saved_results
        ctx, job, observe = self.real_saved_observer()
        original = saved_results.write_private_text
        def write(path, text):
            value = json.loads(text)
            if value.get("state") == "candidate":
                raise OSError(SECRET)
            return original(path, text)
        rpc = FakeRpc([finished()])
        with mock.patch.object(saved_results, "write_private_text", side_effect=write):
            result = self.run_rpc(rpc, save_hook=observe)
        self.assertTrue(rpc.closed)
        self.assertTrue(result.is_error)
        self.assertEqual(result.outcome.error_code, "storage_failed")
        self.assertFalse(result.outcome.delivery_authorized)
        self.assertFalse(result.outcome.resume_allowed)
        self.assertEqual(result.public_reply.generation, 2)
        self.assertIsNone(job.verified_reply)
        saved = self.read_real_candidate(ctx, job)
        self.assertEqual(saved["state"], "invalidated")
        self.assertEqual(saved["generation"], 2)
        self.assertNotIn("authorization", saved)
        self.assertNotIn(SECRET, result.text)


class AppServerTransportTests(unittest.TestCase):
    def test_reader_delivers_requests_while_rpc_is_waiting_and_preserves_ids(self):
        code = """import sys,json
for line in sys.stdin:
 r=json.loads(line)
 if r.get('method')=='probe':
  print(json.dumps({'id':'question-1','method':'item/tool/requestUserInput','params':{}}),flush=True)
  print(json.dumps({'id':r['id'],'result':{'ok':True}}),flush=True)
"""
        rpc = AppServer([sys.executable, "-u", "-c", code], Path.cwd(), os.environ.copy())
        try:
            self.assertEqual(rpc.request("probe", {}), {"ok": True})
            self.assertEqual(rpc.events.get(timeout=2)["id"], "question-1")
        finally:
            rpc.close()
        self.assertIsNotNone(rpc.process.poll())

    def test_forced_close_stops_the_commands_the_app_server_started(self):
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "child.pid"
            code = ("import subprocess,sys,time,signal\n"
                    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                    "c=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\n"
                    f"open({str(pid_file)!r},'w').write(str(c.pid))\n"
                    "time.sleep(60)\n")
            rpc = AppServer([sys.executable, "-c", code], Path.cwd(), os.environ.copy())
            rpc.CLOSE_GRACE = 0.3
            deadline = time.monotonic() + 10
            while not (pid_file.exists() and pid_file.read_text()) and time.monotonic() < deadline:
                time.sleep(0.05)
            child = int(pid_file.read_text())
            self.assertTrue(rpc.close())
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    state = Path(f"/proc/{child}/stat").read_text().rsplit(")", 1)[1].split()[0]
                except OSError:
                    break
                if state == "Z":
                    break
                time.sleep(0.05)
            else:
                self.fail("the app server's child outlived close()")

    def test_transport_exit_unblocks_pending_request_without_replay(self):
        rpc = AppServer([sys.executable, "-c", "import sys;sys.stdin.readline()"], Path.cwd(), os.environ.copy())
        try:
            with self.assertRaises(RpcError):
                rpc.request("probe", {}, timeout=2)
        finally:
            rpc.close()


class CodexSandboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = exec_tempdir()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_direct_unified_entrypoint_default_and_explicit_restrictions(self):
        # A fresh interpreter: LARK_BRIDGE_CODEX_SANDBOX no longer sets the default (core/ignored_env.py).
        for env_mode, explicit, expected in (
            (None, [], "danger-full-access"),
            ("workspace-write", [], "danger-full-access"),
            ("workspace-write", ["--sandbox", "read-only"], "read-only"),
        ):
            with self.subTest(env_mode=env_mode, explicit=explicit):
                env = dict(os.environ, PYTHONPATH=str(ROOT))
                env.pop("LARK_BRIDGE_CODEX_SANDBOX", None)
                if env_mode:
                    env["LARK_BRIDGE_CODEX_SANDBOX"] = env_mode
                code = (
                    "from pathlib import Path; import sys; "
                    "from covey.app.unified import UnifiedBridgeApp; "
                    "app=UnifiedBridgeApp(script_path=Path('bin/covey_bridge.py')); "
                    "args=app.build_parser().parse_args(sys.argv[1:]); "
                    "print(app.harnesses['codex'].backend.build_config(args).options['sandbox'])"
                )
                probe = subprocess.run([sys.executable, "-B", "-c", code,
                    "--workdir", str(self.root), *explicit], env=env, capture_output=True, text=True, check=True)
                self.assertEqual(probe.stdout.strip(), expected)

    def test_bootstrap_persists_permissions_and_refuses_a_restricting_variable(self):
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        capture = self.root / "launch.json"
        wrapper = bin_dir / "python3"
        # Records `covey_service.py launch`; the permission check (`-c`) runs for real, nothing else does.
        wrapper.write_text(f"#!{sys.executable}\n" +
            "import json, os, sys\n"
            f"if '-c' in sys.argv: os.execv({sys.executable!r}, [{sys.executable!r}, *sys.argv[1:]])\n"
            "if 'launch' in sys.argv:\n"
            " with open(os.environ['SANDBOX_TEST_CAPTURE'], 'w') as f: json.dump(sys.argv[1:], f)\n")
        wrapper.chmod(0o700)
        app = UnifiedBridgeApp(script_path=ROOT / "bin/covey_bridge.py")
        for env_mode, explicit, expected in (
            (None, [], "danger-full-access"),
            (None, ["--sandbox", "read-only"], "read-only"),
            ("danger-full-access", [], "danger-full-access"),
            ("workspace-write", [], None),  # Registering it would run looser than asked: refused.
            ("workspace-write", ["--sandbox", "workspace-write"], None),  # Still in the manifest's environment.
        ):
            with self.subTest(env_mode=env_mode, explicit=explicit):
                capture.unlink(missing_ok=True)
                env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ["PATH"],
                           SANDBOX_TEST_CAPTURE=str(capture))
                env.pop("LARK_BRIDGE_CODEX_SANDBOX", None)
                env.pop("LARK_CLAUDE_BRIDGE_SKIP_PERMISSIONS", None)
                if env_mode:
                    env["LARK_BRIDGE_CODEX_SANDBOX"] = env_mode
                child = subprocess.run(["bash", str(ROOT / "scripts/bootstrap_harness_bridge.sh"),
                    "--workdir", str(self.root), "--state-dir", str(self.root / "state"),
                    "--profile", "unit", "--app-id", "cli_unit", *explicit],
                    env=env, capture_output=True, text=True)
                if expected is None:
                    self.assertEqual(child.returncode, 2, child.stderr)
                    self.assertIn("refused: LARK_BRIDGE_CODEX_SANDBOX=workspace-write is no longer read", child.stderr)
                    self.assertFalse(capture.exists())
                    continue
                self.assertEqual(child.returncode, 0, child.stderr)
                argv = json.loads(capture.read_text())
                launch_args = argv[argv.index("--harness") + 2:]
                self.assertIn("--sandbox", launch_args)
                args = app.build_parser().parse_args(launch_args)
                self.assertEqual(app.harnesses["codex"].backend.build_config(args).options["sandbox"], expected)

    def test_appserver_start_and_resume_keep_selected_permissions(self):
        for session in (None, SID):
            for mode in (None, "danger-full-access", "workspace-write", "read-only"):
                with self.subTest(session=session, mode=mode):
                    ctx = codex_context(self.root, "app-server")
                    if mode:
                        ctx.options["sandbox"] = mode
                    rpc = FakeRpc([finished()])
                    with codex_server(rpc):
                        CodexBackend().run(RunRequest(ctx, "unit probe", session))
                    method = "thread/resume" if session else "thread/start"
                    params = next(params for name, params in rpc.requests if name == method)
                    self.assertEqual(params["sandbox"], mode or "danger-full-access")
                    self.assertEqual(params["approvalPolicy"], "never")
                    if session:
                        self.assertEqual(params["threadId"], SID)


class WorkspaceReaderTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory()
  self.addCleanup(self.temp.cleanup)
  self.root=Path(self.temp.name)/'workspace'; self.root.mkdir()
  self.reader=WorkspaceReader(self.root)
 def test_tool_progress_names_the_file_without_exposing_result(self):
  from covey.backends.codex.events import CodexEventAdapter
  events=[]
  CodexEventAdapter(lambda *e: events.append(e)).item('item/completed',
   {'type':'mcpToolCall','id':'read','server':'bridge_workspace','tool':'workspace_read_file',
    'arguments':{'path':'review.md'},'result':{'text':'PRIVATE_BODY'}})
  self.assertEqual(events[0][1]['name'],'读取文件')
  self.assertEqual(events[0][1]['detail'],'review.md')
  self.assertNotIn('PRIVATE_BODY',str(events))
 def test_utf8_pagination_and_absolute_workspace_paths(self):
  f=self.root/'review.md';f.write_text('第一行 😀\nsecond\nthird\n')
  first=self.reader.call('workspace_read_file',{'path':str(f),'limit':2})
  self.assertEqual(first['content'],'1: 第一行 😀\n2: second')
  self.assertEqual(first['next_line'],3)
  last=self.reader.call('workspace_read_file',{'path':'review.md','start_line':3})
  self.assertEqual(last['content'],'3: third');self.assertIsNone(last['next_line'])
 def test_outside_symlink_fifo_and_binary_are_rejected(self):
  outside=self.root.parent/'secret';outside.write_text('DO_NOT_READ')
  (self.root/'link').symlink_to(outside)
  (self.root/'parent').symlink_to(self.root.parent,target_is_directory=True)
  os.mkfifo(self.root/'pipe')
  (self.root/'binary').write_bytes(b'\0data')
  for path in ['../secret',str(outside),'link','parent/secret','pipe','binary']:
   with self.subTest(path=path),self.assertRaises((OSError,ValueError)):
    self.reader.call('workspace_read_file',{'path':path})
 def test_directory_paging_and_validation(self):
  for name in ['b','a','c']:(self.root/name).touch()
  d=self.reader.call('workspace_list_directory',{'path':'.','limit':2})
  self.assertEqual([x['name'] for x in d['entries']],['a','b'])
  self.assertEqual(d['next_offset'],2)
  self.assertEqual(self.reader.call('workspace_list_directory',{'path':'.','offset':2})['entries'][0]['name'],'c')
  for args in [{'path':'a','limit':True},{'path':'a','start_line':0},{'path':'a','command':'id'},{'path':'a','offset':1}]:
   with self.assertRaises(ValueError):self.reader.call('workspace_read_file',args)
 def test_large_file_and_long_line_do_not_silently_truncate(self):
  (self.root/'large').write_bytes(b'x'*(4*1024*1024+1))
  (self.root/'line').write_text('😀'*20000)
  for name in ['large','line']:
   with self.assertRaises(ValueError):self.reader.call('workspace_read_file',{'path':name})
 def test_topic_attachments_are_readable_but_nothing_else_outside(self):
  media=Path(self.temp.name)/'state'/'topics'/'t1'/'incoming_media'
  reader=WorkspaceReader(self.root,[media])  # Created later, when the first attachment arrives.
  media.mkdir(parents=True)
  (media/'om_x-1-file.csv').write_text('marker,count\nFILE-1,17\n')
  out=reader.call('workspace_read_file',{'path':str(media/'om_x-1-file.csv')})
  self.assertIn('FILE-1,17',out['content'])
  self.assertEqual(out['path'],str(media.resolve()/'om_x-1-file.csv'))
  other=Path(self.temp.name)/'state'/'topics'/'t2'/'incoming_media'; other.mkdir(parents=True)
  (other/'secret.txt').write_text('other topic')
  for path in (str(other/'secret.txt'),str(media/'..'/'..'/'t2'/'incoming_media'/'secret.txt')):
   with self.assertRaises(ValueError):reader.call('workspace_read_file',{'path':path})
  os.symlink(other/'secret.txt',media/'link.txt')
  with self.assertRaises(OSError):reader.call('workspace_read_file',{'path':str(media/'link.txt')})
 def test_stdio_handshake_read_and_error(self):
  (self.root/'actual.txt').write_text('reader-proof')
  requests=[{'id':1,'method':'initialize','params':{'protocolVersion':'2025-03-26'}},
   {'method':'notifications/initialized'}, {'id':2,'method':'tools/list'},
   {'id':3,'method':'tools/call','params':{'name':'workspace_read_file','arguments':{'path':'actual.txt'}}},
   {'id':4,'method':'tools/call','params':{'name':'workspace_read_file','arguments':{'path':'../secret'}}}]
  run=subprocess.run([sys.executable,'-B',str(ROOT/'bin/covey_workspace_reader.py'),'--root',str(self.root)],
   input='\n'.join(json.dumps(x) for x in requests)+'\n',text=True,capture_output=True,timeout=5,check=True)
  rows=[json.loads(line) for line in run.stdout.splitlines()]
  self.assertEqual(len(rows),4);self.assertEqual(rows[0]['result']['protocolVersion'],'2025-03-26')
  self.assertEqual(len(rows[1]['result']['tools']),2)
  self.assertIn('reader-proof',rows[2]['result']['content'][0]['text'])
  self.assertFalse(rows[2]['result']['isError']);self.assertTrue(rows[3]['result']['isError'])
  self.assertEqual(run.stderr,'')
 def test_reader_is_configured_for_new_and_resumed_native_threads(self):
  for sid in [None,SID]:
   ctx=codex_context(self.root,'app-server');rpc=FakeRpc([finished()])
   with codex_server(rpc):
    CodexBackend().run(RunRequest(ctx, 'read local file', sid))
   params=next(p for name,p in rpc.requests if name in ['thread/start','thread/resume'])
   cfg=params['config']
   self.assertEqual(cfg['mcp_servers.bridge_workspace.args'][-6:],
    ['--root',str(self.root),'--extra-root',str(ctx.history_path.parent/'incoming_media'),
     '--extra-root',str(ctx.history_path.parent/'handoff')])
   self.assertTrue(cfg['mcp_servers.bridge_workspace.enabled'])
   # The reader describes itself; the instructions say nothing about how files are read or written (#748).
   self.assertNotIn('bridge_workspace',params['developerInstructions'])


 def test_the_butlers_bridge_tools_are_a_server_of_new_and_resumed_threads(self):
  # T614: the butler's turn (options["mcp_config"]) on Codex: its Bridge server beside the workspace reader, with a
  # tool wait above the server's own 120 seconds; every other turn has none of it.
  from covey.backends.codex.appserver import BRIDGE_TOOL_SECONDS, thread_params
  server={'command':'/usr/bin/python3','args':['-B','/release/bin/covey_butler_mcp.py'],'env':{'COVEY_BUTLER_JOB':'om_1-1'}}
  for sid in [None,SID]:
   ctx=codex_context(self.root,'app-server')
   ctx=dataclasses.replace(ctx,options={**ctx.options,'mcp_config':{'mcpServers':{'bridge':server}}})
   rpc=FakeRpc([finished()])
   with codex_server(rpc):
    CodexBackend().run(RunRequest(ctx,'开个话题',sid))
   cfg=next(p for name,p in rpc.requests if name in ['thread/start','thread/resume'])['config']
   self.assertEqual((cfg['mcp_servers.bridge.command'],cfg['mcp_servers.bridge.args'],cfg['mcp_servers.bridge.env']),
                    (server['command'],server['args'],server['env']))
   self.assertIs(cfg['mcp_servers.bridge.enabled'],True)
   self.assertEqual(cfg['mcp_servers.bridge.tool_timeout_sec'],BRIDGE_TOOL_SECONDS)
   self.assertGreater(BRIDGE_TOOL_SECONDS,120)
   self.assertTrue(cfg['mcp_servers.bridge_workspace.enabled'])
  plain=thread_params(codex_context(self.root,'app-server'),None)[0]['config']
  self.assertFalse([key for key in plain if key.startswith('mcp_servers.bridge.')])

 def test_bridge_rules_lead_the_developer_instructions_of_new_and_resumed_threads(self):
  from covey.backends.codex.appserver import CODEX_INSTRUCTIONS, thread_params
  rules='You are Codex, replying to the user through Feishu.\n\nConstraints for this bridge:\n- 规则'
  for sid in [None,SID]:
   ctx=codex_context(self.root,'app-server');rpc=FakeRpc([finished()])
   with codex_server(rpc):
    CodexBackend().run(RunRequest(ctx,'本轮消息',sid,instructions=rules))
   params=next(p for name,p in rpc.requests if name in ['thread/start','thread/resume'])
   self.assertEqual(params['developerInstructions'],rules+'\n\n'+CODEX_INSTRUCTIONS)
   turn=json.dumps(next(p for name,p in rpc.requests if name=='turn/start'),ensure_ascii=False)
   self.assertIn('本轮消息',turn);self.assertNotIn('Constraints for this bridge',turn)
  self.assertEqual(thread_params(codex_context(self.root,'app-server'),None)[0]['developerInstructions'],CODEX_INSTRUCTIONS)
  # Blocking questions go through the checkpoint, the rest through request_user_input; the plan-first
  # rule and the question schema are the checkpoint contract's.
  self.assertIn('requires_user=true',CODEX_INSTRUCTIONS);self.assertIn('不要使用 request_user_input_async',CODEX_INSTRUCTIONS)
  self.assertIn('不要把未回答视为同意',CODEX_INSTRUCTIONS);self.assertNotIn('最终回复',CODEX_INSTRUCTIONS)
  self.assertNotIn('优先调用',CODEX_INSTRUCTIONS);self.assertNotIn('id/header',CODEX_INSTRUCTIONS)


class CodexSubtaskStateTests(unittest.TestCase):
    def test_a_resumed_subtask_starts_after_it_ended(self):
        # resumeAgent ran the ended subtask again under its first start, which the trace takes for a late,
        # stale event: its row kept 已完成. Rows read in one batch share their time.
        from covey.backends.codex.events import CodexEventAdapter
        events = []
        adapter = CodexEventAdapter(lambda *e: events.append(e))
        collab = {"type": "collabAgentToolCall", "receiverThreadIds": ["a"]}
        adapter.agents({**collab, "tool": "spawnAgent", "agentPath": "/root/a",
                        "agentsStates": {"a": {"status": "running"}}}, 100)
        adapter.child("turn/completed", {"threadId": "a", "turn": {"status": "completed"}}, 100)
        adapter.agents({**collab, "tool": "resumeAgent", "agentsStates": {"a": {"status": "running"}}}, 100)
        adapter.child("turn/completed", {"threadId": "a", "turn": {"status": "completed"}}, 104)
        states = [data for kind, data in events if kind == "agent_state"]
        self.assertEqual([(s["status"], s.get("ended_at")) for s in states],
                         [("running", None), ("completed", 100), ("running", None), ("completed", 104)])
        self.assertEqual(states[1]["started_at"], 100)
        self.assertGreater(states[2]["started_at"], 100)
        self.assertEqual(states[3]["started_at"], states[2]["started_at"])


FORK = "44444444-4444-4444-8444-444444444444"


class ForkRpc(FakeRpc):
    """An app server that forks ``SID`` into ``FORK`` and plays ``rows`` (the fork's turn) once asked."""

    def request(self, method, params, timeout=30):
        self.requests.append((method, params))
        if method == "thread/fork":
            return {"thread": {"id": self.start_sid}}
        if method == "turn/start":
            return {"turn": {"id": TURN}}
        return {}


def fork_item(mid, text, phase="final_answer"):
    return {"method": "item/completed", "params": {"threadId": FORK, "turnId": TURN, "item": agent(mid, text, phase)}}


def fork_done(status="completed", **extra):
    return {"method": "turn/completed", "params": {"threadId": FORK, "turn": {
        "id": TURN, "status": status, "error": None, "itemsView": "notLoaded", "items": [], **extra}}}


class CodexHandoffSummaryTests(unittest.TestCase):
    """CodexBackend.handoff_summary (backends/codex/handoff.py): an ephemeral fork of the old thread in a home of its own,
    read-only and with no tool, never longer than its limit, never raising."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        home = self.root / "codex-home"
        home.mkdir()
        (home / "config.toml").write_text('model = "gpt-6-astra"\n[mcp_servers.owner]\ncommand = "never"\n')
        (home / "auth.json").write_text("{}")
        patcher = mock.patch.dict(os.environ, {"CODEX_HOME": str(home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ctx = dataclasses.replace(codex_context(self.root, "app-server"), effort="high")
        self.started = []

    def summarize(self, rows, *, start_sid=FORK, **kwargs):
        from covey.backends.codex import handoff
        rpc = ForkRpc(rows, start_sid=start_sid)

        def factory(argv, cwd, env):
            home = Path(env["CODEX_HOME"])
            self.started.append({"argv": argv, "cwd": cwd, "home": home, "linked": sorted(p.name for p in home.iterdir()),
                                 "config": (home / "config.toml").read_text()})
            return rpc
        with mock.patch.object(handoff, "AppServer", side_effect=factory):
            if kwargs.pop("direct", False):
                with handoff.HandoffHome(self.ctx.state_dir, dict(os.environ)) as env:
                    result = handoff.summarize(self.ctx, SID, "写交接摘要", env, **kwargs)
            else:
                result = CodexBackend().handoff_summary(self.ctx, SID, "写交接摘要", **kwargs)
        return result, rpc

    def test_the_summary_is_an_ephemeral_read_only_fork_with_no_tool(self):
        from covey.backends.codex import handoff
        approval = {"id": 7, "method": "item/commandExecution/requestApproval",
                    "params": {"threadId": FORK, "turnId": TURN, "itemId": "x"}}
        (text, outcome), rpc = self.summarize([fork_item("c", "我先看看", "commentary"), approval,
                                               fork_item("f", "### 目标\n交接"), fork_done()])
        self.assertEqual((text, outcome), ("### 目标\n交接", "写好了"))
        methods = [method for method, _ in rpc.requests]
        self.assertEqual(methods, ["initialize", "thread/fork", "turn/start"])
        fork = rpc.requests[1][1]
        self.assertEqual({key: fork[key] for key in ("threadId", "ephemeral", "sandbox", "approvalPolicy", "model")},
                         {"threadId": SID, "ephemeral": True, "sandbox": "read-only", "approvalPolicy": "never",
                          "model": "fixture-model"})
        self.assertEqual(fork["config"], {"model_reasoning_effort": "high"})  # No MCP server, not even the reader.
        self.assertNotIn("developerInstructions", fork)
        start = rpc.requests[2][1]
        self.assertEqual((start["threadId"], start["sandboxPolicy"], start["effort"]), (FORK, {"type": "readOnly"}, "high"))
        self.assertEqual(rpc.sent, [{"method": "initialized"},
                                    {"id": 7, "error": {"code": -32601, "message": handoff.DENIED}}])
        self.assertTrue(rpc.closed)
        [started] = self.started
        flags = started["argv"][started["argv"].index("--stdio") + 1:]
        self.assertEqual(flags, [part for flag in handoff.FLAGS for part in ("-c", flag)])
        for flag in ("features.shell_tool=false", "features.unified_exec=false", "features.multi_agent=false",
                     'web_search="disabled"', "features.apps=false", "features.hooks=false"):
            self.assertIn(flag, flags)
        # A home of its own: the login linked, the owner's config (and its MCP server) not, removed afterwards.
        self.assertEqual((started["home"].parent, started["home"].name[:len(handoff.HandoffHome.PREFIX)]),
                         (self.ctx.state_dir / "tmp", handoff.HandoffHome.PREFIX))
        self.assertEqual(started["linked"], ["auth.json", "config.toml"])
        self.assertEqual(started["config"], 'model = "gpt-6-astra"\n')
        self.assertFalse(started["home"].exists())
        # The turn's own items, when it gives them in full, are the answer.
        (text, _), _ = self.summarize([fork_item("f", "旧的"), fork_done(itemsView="full", items=[agent("g", "全的")])])
        self.assertEqual(text, "全的")

    def test_no_summary_without_a_clean_end_and_never_past_the_limit(self):
        failed = fork_done("failed", error={"codexErrorInfo": "serverOverloaded", "message": SECRET})
        cases = [([failed], "Codex 报告失败"), ([fork_item("c", "想想", "commentary"), fork_done()], "回答为空"),
                 ([], "Codex 进程提前结束")]
        for rows, said in cases:
            with self.subTest(said=said):
                (text, outcome), rpc = self.summarize(rows)
                self.assertEqual((text, outcome), ("", said))
                self.assertTrue(rpc.closed)
        (text, outcome), _ = self.summarize([fork_done()], start_sid=SID)
        self.assertEqual(outcome, "Codex 没有给出分叉")
        # Past its limit with nothing come, or stopped: the app server closed either way.
        rpc = ForkRpc([], start_sid=FORK)
        rpc.events = queue.Queue()  # Nothing comes, not even its end.
        from covey.backends.codex import handoff
        with mock.patch.object(handoff, "AppServer", return_value=rpc):
            started = time.monotonic()
            self.assertEqual(handoff.summarize(self.ctx, SID, "写", dict(os.environ), seconds=0.5), ("", "超过 0.5 秒"))
            self.assertLess(time.monotonic() - started, 5)
            stop = threading.Event()
            stop.set()
            self.assertEqual(handoff.summarize(self.ctx, SID, "写", dict(os.environ), cancel=stop), ("", "已停止"))
        self.assertIn(("turn/interrupt", {"threadId": FORK, "turnId": TURN}), rpc.requests)
        self.assertTrue(rpc.closed)
        self.assertEqual(list((self.ctx.state_dir / "tmp").glob("codex-handoff-*")), [])

    def test_a_gateway_that_is_down_gives_up_at_the_handshake_or_the_first_sign_not_the_whole_limit(self):
        """T884 A12: a model that shows no output within FIRST_SECONDS of turn/start (Codex retrying a lost connection says
        so with willRetry errors), or an app server whose own answer takes past HANDSHAKE_SECONDS, gives the summary up at
        once; the new turn does not wait SUMMARY_SECONDS."""
        from covey.backends.codex import handoff
        from covey.backends.codex.rpc import RpcError
        self.assertEqual((handoff.FIRST_SECONDS, handoff.HANDSHAKE_SECONDS), (30, 30))
        retrying = {"method": "error", "params": {"threadId": FORK, "turnId": TURN, "willRetry": True,
                                                  "error": {"message": "stream disconnected", "codexErrorInfo": {
                                                      "responseStreamConnectionFailed": {"httpStatusCode": None}}}}}
        rpc = ForkRpc([retrying], start_sid=FORK)
        rpc.events = queue.Queue()
        rpc.events.put(retrying)  # Then nothing: no bridge/closed either.
        with mock.patch.object(handoff, "AppServer", return_value=rpc):
            started = time.monotonic()
            self.assertEqual(handoff.summarize(self.ctx, SID, "写", dict(os.environ), seconds=20, first_seconds=0.3),
                             ("", "原模型 0.3 秒没有回应（网关可能断开）"))
            self.assertLess(time.monotonic() - started, 5)
        self.assertTrue(rpc.closed)
        # A reasoning item started in time: the model answers, the summary is read to its end.
        reasoning = {"method": "item/started", "params": {"threadId": FORK, "turnId": TURN,
                                                          "item": {"id": "r", "type": "reasoning"}}}
        (text, outcome), _ = self.summarize([reasoning, fork_item("f", "摘要"), fork_done()], direct=True,
                                            first_seconds=5)
        self.assertEqual((text, outcome), ("摘要", "写好了"))

        class SlowServer(ForkRpc):
            timeouts = []

            def request(self, method, params, timeout=30):
                SlowServer.timeouts.append((method, timeout))
                if method == "initialize":
                    raise RpcError(f"{method} response timed out; delivery is unknown")
                return super().request(method, params, timeout)
        slow = SlowServer([], start_sid=FORK)
        with mock.patch.object(handoff, "AppServer", return_value=slow):
            self.assertEqual(handoff.summarize(self.ctx, SID, "写", dict(os.environ)), ("", "Codex 30 秒没有回应"))
        self.assertEqual(SlowServer.timeouts[0][0], "initialize")
        self.assertLessEqual(SlowServer.timeouts[0][1], handoff.HANDSHAKE_SECONDS)
        self.assertTrue(slow.closed)

    def test_a_session_that_is_no_thread_sends_nothing(self):
        from covey.backends.codex import handoff
        with mock.patch.object(handoff, "AppServer") as server:
            self.assertEqual(CodexBackend().handoff_summary(self.ctx, "not-a-thread", "写"), ("", "原会话标识不对"))
        server.assert_not_called()
