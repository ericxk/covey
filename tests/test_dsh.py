"""dsh over its headless profile: route patch, events, verified completion and failure codes."""
import dataclasses
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from support import FIXTURES
from covey.app.availability import local_check
from covey.app.unified import UnifiedBridgeApp
from covey.backends.dsh.bridge import DshEventAdapter, route_patch
from covey.backends.gateway import connection
from support import acme as acme_gateway
from covey.core.harness import LEAD, LOOK, TOOL_LIMIT, RunCallbacks, RunInputs, RunRequest

KEY = "sk-unit-private-key"
RUN = "33333333-3333-4333-8333-333333333333"
SID = "session-11111111-1111-4111-8111-111111111111"
OTHER = "session-22222222-2222-4222-8222-222222222222"

FAKE = FIXTURES / "harness/dsh_headless.py"


def catalog_rows():
    return [{"id": "ali-deepseek-v4.1-flash", "protocol": "anthropic-messages",
             "supported_reasoning_levels": ["low", "high", "max"], "default_reasoning_level": "high"},
            {"id": "seed-plain", "protocol": "anthropic-messages"}]


class DshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        clean = {k: v for k, v in os.environ.items() if not k.startswith(("ACME_", "DSH_"))}
        patcher = mock.patch.dict(os.environ, {**clean, "HOME": str(self.root), "ACME_API_KEY": KEY,
                                               "FAKE_DSH_RECORD": str(self.root / "record.json")}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.app = UnifiedBridgeApp(script_path=Path(__file__).resolve().parents[1] / "bin/covey_bridge.py")
        self.app.build_context(self.app.build_parser().parse_args(
            ["--workdir", str(self.root), "--state-dir", str(self.root / "state"), "--profile", "unit"]))
        self.backend = self.app.apps["dsh"].backend
        self.ctx = dataclasses.replace(self.app.harnesses["dsh"].context, backend_cmd=[sys.executable, "-I", str(FAKE)],
                                       effort="max")
        path = connection.cache_path(acme_gateway(), self.ctx.state_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"updated_at": time.time(), "rows": catalog_rows()}))

    def run_dsh(self, scenario, session=None, cancel=None, options=None):
        os.environ["FAKE_DSH_SCENARIO"] = scenario
        if options is not None:
            self.ctx = dataclasses.replace(self.ctx, options={**self.ctx.options, **options})
        events, replies, sessions = [], [], []
        result = self.backend.run(RunRequest(self.ctx, "做个任务", session_id=session, callbacks=RunCallbacks(
            event=lambda kind, data, control: events.append((kind, data)), session=sessions.append,
            verified_reply=replies.append), inputs=RunInputs(cancel or threading.Event(), run_id=RUN)))
        record = json.loads((self.root / "record.json").read_text()) if (self.root / "record.json").exists() else {}
        return result, events, [r for r in replies if r is not None], sessions, record

    def test_completed_turn_is_verified_and_its_events_are_public_only(self):
        result, events, replies, sessions, record = self.run_dsh("ok")
        self.assertFalse(result.is_error, result.text)
        self.assertEqual((result.text, result.session_id, sessions), ("完成", SID, [SID]))
        self.assertTrue(result.outcome.resume_allowed)
        self.assertEqual(replies[-1].backend, "dsh")
        self.assertEqual(replies[-1].fingerprint, result.outcome.reply_fingerprint)
        published = json.dumps(events, ensure_ascii=False)
        for private in ("PRIVATE_REASONING", "PRIVATE_OUTPUT", KEY):
            self.assertNotIn(private, published)
        tools = [d for k, d in events if k == "tool"]
        self.assertEqual([t["status"] for t in tools], ["running", "done"])
        self.assertEqual(tools[0]["detail"], "List files · ls")
        self.assertIn(("progress_note", "先看一下目录"), [(k, d.get("text")) for k, d in events])
        usage = [d for k, d in events if k == "token_usage"][-1]
        seconds = usage.pop("generation_seconds")
        self.assertEqual(usage, {"output_tokens": 10, "input_tokens": 210, "cache_read_tokens": 90,
                                 "cache_write_tokens": 0, "measured_output_tokens": 10, "context_tokens": 113})
        self.assertGreaterEqual(seconds, 0)
        self.assertEqual(events[-1][0], "trace_finished")
        self.assertEqual(record["stdin"], "做个任务")
        self.assertNotIn("--session-id", record["argv"])
        self.assertEqual(record["argv"][:3], ["--profile", "headless", "--patch"])
        self.assertEqual(record["patch_mode"], "0o600")
        self.assertFalse(any(KEY in arg for arg in record["argv"]))
        self.assertEqual(record["env"], {"ACME_API_KEY": KEY, "DSH_HOME": str(self.root / ".dsh"),
                                         "DSH_PERMISSION_MODE": "danger-full-access", "DSH_TELEMETRY_MODE": "DISABLED"})
        self.assertEqual(list((self.ctx.state_dir / "tmp").glob("dsh-patch-*")), [])

    def test_patch_declares_the_acme_route_and_selects_the_task_model(self):
        _result, _events, _replies, _sessions, record = self.run_dsh("ok")
        route, default, title = record["patch"]
        provider = route["config"]["providers"]["acme"]
        self.assertEqual((provider["api"], provider["baseURL"], provider["apiKeyEnv"], provider["headers"]),
                         ("anthropic-messages", "https://gateway.example.com", "ACME_API_KEY",
                          {"x-source": "bridge-tests"}))
        flash, plain = provider["models"]
        self.assertEqual(flash["reasoningEfforts"], {"low": "low", "high": "high", "max": "max"})
        self.assertEqual(flash["compat"], {"forceAdaptiveThinking": True})
        self.assertIs(plain["reasoningEfforts"], False)
        self.assertNotIn("compat", plain)
        self.assertEqual(default, {"id": "agent-default-model", "config": {
            "provider": "acme", "model": "ali-deepseek-v4.1-flash", "reasoningEffort": "max"}})
        self.assertEqual(title, {"id": "session-title-llm", "disabled": True})
        self.assertEqual(provider["retryPolicy"], {"mode": "normal", "maxRetries": 64, "backoff": {
            "initialDelayMs": 2000, "maxDelayMs": 60000, "jitterRatio": 0.2}})
        self.assertNotIn("reasoningEffort", route_patch(acme_gateway(), {}, "m", "")[1]["config"])

    BRIDGE_TOOLS = {"mcpServers": {"bridge": {"command": "/abs/python3", "args": ["/abs/mcp.py", "--state", "/abs/state"],
                                              "env": {"COVEY_X": "1", "COVEY_Y": "2"}}}}

    def test_bridge_tools_become_an_insert_of_mcp_client_rows(self):
        self.assertTrue(self.backend.capabilities(self.ctx).bridge_tools)
        _r, _e, _rep, _s, plain = self.run_dsh("ok")
        self.assertEqual(len(plain["patch"]), 3)  # No tools, no MCP row.
        _r, _e, _rep, _s, record = self.run_dsh("ok", options={"mcp_config": self.BRIDGE_TOOLS})
        self.assertEqual(record["patch"][:3], plain["patch"])
        self.assertEqual(record["patch"][3:], [{"insert": [{
            "id": "mcp-bridge", "name": "@deepseek-ai/dsh-mcp-client",
            "config": {"serverName": "bridge", "transport": "stdio", "command": "/abs/python3",
                       "args": ["/abs/mcp.py", "--state", "/abs/state"], "env": {"COVEY_X": "1", "COVEY_Y": "2"},
                       "toolCallTimeoutMs": 150000}}]}])
        # The call outlives the 120 seconds the Bridge may take to answer a tool request.
        self.assertGreater(150000, 120 * 1000)
        self.assertEqual(route_patch(acme_gateway(), {}, "m", "", {"mcpServers": {"x": {"args": []}}}),
                         route_patch(acme_gateway(), {}, "m", ""))  # A server without a command is left out.

    def test_a_lead_turn_is_read_only_and_asks_nobody(self):
        _r, _e, _rep, _s, record = self.run_dsh("ok", options={TOOL_LIMIT: LEAD, "mcp_config": self.BRIDGE_TOOLS})
        self.assertEqual(record["env"]["DSH_PERMISSION_MODE"], "read-only")
        argv = record["argv"]
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "read-only")
        self.assertEqual(argv[argv.index("--approval-policy") + 1], "never")
        self.assertEqual(argv[-1], "-")
        self.assertIn("mcp-bridge", json.dumps(record["patch"]))
        self.assertNotIn("danger-full-access", self.backend.prompt_runtime_note(self.ctx))
        self.assertIn("read-only", self.backend.prompt_runtime_note(self.ctx))

    def test_other_turns_keep_full_access_and_their_argv(self):
        _r, _e, _rep, _s, base = self.run_dsh("ok")
        for options in ({}, {TOOL_LIMIT: LOOK}, {"mcp_config": self.BRIDGE_TOOLS}):
            self.ctx = dataclasses.replace(self.ctx, options=options)
            _r, _e, _rep, _s, record = self.run_dsh("ok")
            self.assertEqual(record["env"], base["env"])
            self.assertEqual(record["argv"][:2], base["argv"][:2])
            self.assertNotIn("--permission-mode", record["argv"])
            self.assertNotIn("--approval-policy", record["argv"])
            self.assertEqual(record["argv"][4:], base["argv"][4:])
            self.assertEqual(self.backend.prompt_runtime_note(self.ctx), "dsh 权限模式：danger-full-access")

    def test_resume_passes_the_session_and_rejects_another(self):
        result, *_rest, record = self.run_dsh("ok", session=SID)
        self.assertFalse(result.is_error, result.text)
        self.assertEqual(record["argv"][record["argv"].index("--session-id") + 1], SID)
        result, _events, replies, _sessions, _record = self.run_dsh("mismatch", session=SID)
        self.assertTrue(result.is_error)
        self.assertEqual(result.outcome.error_code, "protocol_error")
        self.assertEqual(replies, [])

    def test_a_stopped_turn_is_never_a_limit_or_a_fault(self):
        """The stop's own end (dsh cut off mid-stream) says cancelled, never what app/auto_retry.py would retry."""
        from types import SimpleNamespace
        from covey.backends.dsh.bridge import headless_outcome
        stopped, running = threading.Event(), threading.Event()
        stopped.set()
        for native, code in (("STREAM_CLOSED", "server_fault"), ("TRANSPORT", "server_fault"), ("RATE_LIMIT", "rate_limited")):
            with self.subTest(native=native):
                rows = SimpleNamespace(reason={"kind": "error", "error": {"code": native, "message": "x"}}, invalid=False,
                                       final="", sid=SID, started=True, driver_error=None)
                self.assertEqual(headless_outcome(rows, 1, stopped), ("cancelled", "cancelled"))
                self.assertEqual(headless_outcome(rows, 1, running), ("failed", code))
        for native, code in (("CONTENT_REFUSED", "root_failed"), ("EMPTY_RESPONSE", "root_failed"),
                             ("INVALID_REQUEST", "root_failed"), ("ACCOUNT_SIGN_IN_REQUIRED", "authentication_error"),
                             ("ACCOUNT_QUOTA", "quota_exhausted")):
            with self.subTest(native=native):
                rows = SimpleNamespace(reason={"kind": "error", "error": {"code": native, "message": "x"}}, invalid=False,
                                       final="", sid=SID, started=True, driver_error=None)
                self.assertEqual(headless_outcome(rows, 1, running), ("failed", code))

    def test_failures_map_to_bridge_codes_without_native_text(self):
        for scenario, code in (("missing-credential", "authentication_error"), ("usage-error", "process_exit"),
                               ("garbage", "protocol_error"), ("busy", "rate_limited"),
                               # dsh's QUOTA is a spent balance, never retried; a 5xx or a dropped stream is a fault.
                               ("quota", "quota_exhausted"), ("server", "server_fault"), ("transport", "server_fault"),
                               ("auth", "authentication_error"), ("auth-quota", "quota_exhausted"),
                               ("unknown-model", "root_failed")):
            with self.subTest(scenario=scenario):
                result, _events, replies, _sessions, _record = self.run_dsh(scenario)
                self.assertTrue(result.is_error)
                self.assertEqual(result.outcome.error_code, code)
                self.assertFalse(result.outcome.resume_allowed)
                self.assertTrue(result.text.startswith("Acme 当前繁忙" if scenario == "busy" else "dsh "), result.text)
                # The failure card takes the route's own words only for a busy Acme.
                self.assertEqual(result.failure_detail, connection.busy_text(acme_gateway()) if scenario == "busy" else "")
                self.assertNotIn("PRIVATE_DETAIL", result.text)
                self.assertEqual(replies, [])

    def test_stop_ends_the_process_and_is_not_resumable(self):
        cancel = threading.Event()
        threading.Timer(1.0, cancel.set).start()
        started = time.monotonic()
        result, *_rest = self.run_dsh("slow", cancel=cancel)
        self.assertLess(time.monotonic() - started, 20)
        self.assertEqual(result.outcome.status, "cancelled")
        self.assertFalse(result.outcome.resume_allowed)

    def test_unlisted_model_effort_or_missing_key_never_starts_dsh(self):
        for change in ({"model": "gone"}, {"effort": "xhigh"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.backend.run(RunRequest(dataclasses.replace(self.ctx, **change), "x"))
        del os.environ["ACME_API_KEY"]
        with self.assertRaisesRegex(ValueError, "个人密钥未配置"):
            self.backend.run(RunRequest(self.ctx, "x"))
        self.assertFalse((self.root / "record.json").exists())
        self.assertEqual(local_check("dsh", self.ctx)["reason"], "authentication")

    def test_each_step_says_the_context_the_next_request_starts_from_and_no_window(self):
        seen = []
        adapter = DshEventAdapter(lambda kind, data: seen.append((kind, data)))
        for usage in ({"inputTokens": 100, "cacheReadTokens": 5000, "cacheWriteTokens": 50, "outputTokens": 70},
                      {"inputTokens": 10, "cacheReadTokens": 6000, "outputTokens": 30}):
            adapter.accept({"type": "status", "phase": "step_start"}, 1.0)
            adapter.accept({"type": "status", "phase": "step_end", "usage": usage}, 2.0)
        counts = [d for k, d in seen if k == "token_usage"]
        # The latest request's input with the cache and its output, not the turn's sum.
        self.assertEqual([d["context_tokens"] for d in counts], [100 + 5000 + 50 + 70, 10 + 6000 + 30])
        self.assertEqual(counts[-1]["input_tokens"], 100 + 5000 + 50 + 10 + 6000)
        self.assertTrue(all("context_window" not in d for d in counts))  # The gateway's directory has no window field.

    def test_generation_time_stops_at_each_steps_first_commit(self):
        seen = []
        adapter = DshEventAdapter(lambda kind, data: seen.append((kind, data)))
        for row, at in (({"type": "status", "phase": "step_start"}, 10.0), ({"type": "thinking", "text": "x"}, 13.5),
                        ({"type": "tool_call", "callId": "c", "tool": "bash", "input": {}}, 13.5),
                        ({"type": "tool_result", "callId": "c", "status": "completed"}, 19.5),
                        ({"type": "status", "phase": "step_end", "usage": {"outputTokens": 70}}, 19.5),
                        ({"type": "status", "phase": "step_start"}, 20.0), ({"type": "text", "text": "ok"}, 22.0),
                        ({"type": "status", "phase": "step_end", "usage": {"outputTokens": 30}}, 22.0)):
            adapter.accept(row, at)
        usage = [d for k, d in seen if k == "token_usage"][-1]
        # The tool's six seconds are not generation time.
        self.assertEqual((usage["measured_output_tokens"], usage["generation_seconds"]), (100, 5.5))
        from covey.core.trace import ToolTrace
        trace = ToolTrace()
        trace.accept("token_usage", usage)
        self.assertEqual((trace.measured_output_tokens, trace.generation_seconds), (100, 5.5))

    def test_adapter_flushes_commentary_before_tools(self):
        seen = []
        adapter = DshEventAdapter(lambda kind, data: seen.append((kind, data.get("text"))))
        adapter.accept({"type": "text", "text": "计划"}, 1.0)
        adapter.accept({"type": "tool_call", "callId": "c", "tool": "Read", "input": {"path": "a.py"}}, 2.0)
        self.assertEqual(seen[:4], [("answer_snapshot", "计划"), ("progress_note", "计划"), ("answer_snapshot", ""),
                                    ("tool", None)])
        self.assertEqual(adapter.tools["c"]["detail"], "a.py")
        self.assertEqual(adapter.tools["c"]["name"], "read")

    def test_a_fetched_urls_query_stays_off_the_card(self):
        adapter = DshEventAdapter(lambda kind, data: None)
        adapter.accept({"type": "tool_call", "callId": "w", "tool": "WebFetch",
                        "input": {"url": "https://example.invalid/doc?token=PRIVATE#part"}}, 1.0)
        self.assertEqual(adapter.tools["w"]["detail"], "https://example.invalid/doc")


if __name__ == "__main__":
    unittest.main()
