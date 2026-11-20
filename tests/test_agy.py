"""Antigravity (agy) over its headless stream-json: argv, events, verified completion and failure codes."""
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
from covey.app.unified import UnifiedBridgeApp
from covey.backends.agy.bridge import (AntigravityEventAdapter, NOT_LOGGED_IN, models_from_listing, tool_inputs)
from covey.core.harness import RunCallbacks, RunInputs, RunRequest

RUN = "33333333-3333-4333-8333-333333333333"
SID = "055a398f-db14-4c5f-abbb-1bf03f8120a7"
FAKE = FIXTURES / "harness/agy_stream.py"


class AntigravityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        clean = {k: v for k, v in os.environ.items() if not k.startswith("FAKE_AGY")}
        patcher = mock.patch.dict(os.environ, {**clean, "HOME": str(self.root),
                                               "FAKE_AGY_RECORD": str(self.root / "record.json")}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.app = UnifiedBridgeApp(script_path=Path(__file__).resolve().parents[1] / "bin/covey_bridge.py")
        self.app.build_context(self.app.build_parser().parse_args(
            ["--workdir", str(self.root), "--state-dir", str(self.root / "state"), "--profile", "unit"]))
        self.backend = self.app.apps["agy"].backend
        self.ctx = dataclasses.replace(self.app.harnesses["agy"].context, backend_cmd=[sys.executable, "-I", str(FAKE)],
                                       model="gemini-3.8-pro-high", effort="high")

    def run_agy(self, scenario, session=None, cancel=None, ctx=None):
        os.environ["FAKE_AGY_SCENARIO"] = scenario
        events, replies, sessions = [], [], []
        result = self.backend.run(RunRequest(ctx or self.ctx, "做个任务", session_id=session, callbacks=RunCallbacks(
            event=lambda kind, data, control: events.append((kind, data)), session=sessions.append,
            verified_reply=replies.append), inputs=RunInputs(cancel or threading.Event(), run_id=RUN)))
        path = self.root / "record.json"
        record = json.loads(path.read_text()) if path.exists() else {}
        if path.exists():
            path.unlink()
        return result, events, [r for r in replies if r is not None], sessions, record

    def test_completed_turn_is_verified_and_its_events_are_public_only(self):
        result, events, replies, sessions, record = self.run_agy("ok")
        self.assertFalse(result.is_error, result.text)
        self.assertEqual((result.text, result.session_id, sessions), ("完成", SID, [SID]))
        self.assertTrue(result.outcome.resume_allowed)
        self.assertEqual(replies[-1].backend, "agy")
        self.assertEqual(replies[-1].fingerprint, result.outcome.reply_fingerprint)
        published = json.dumps(events, ensure_ascii=False)
        for private in ("PRIVATE_OUTPUT", "PRIVATE_SUBAGENT"):
            self.assertNotIn(private, published)
        tools = [d for k, d in events if k == "tool"]
        self.assertEqual([(t["name"], t["status"]) for t in tools], [("bash", "running"), ("bash", "done")])
        self.assertEqual(tools[0]["detail"], "ls")
        self.assertIn(("progress_note", "先看一下目录"), [(k, d.get("text")) for k, d in events])
        self.assertEqual([k for k, _d in events].count("model_output"), 1)
        usage = [d for k, d in events if k == "token_usage"][-1]
        self.assertEqual((usage["output_tokens"], usage["input_tokens"], usage["cache_read_tokens"]),
                         (657, 10415, 8113))
        self.assertEqual(events[-1][0], "trace_finished")
        self.assertEqual(json.loads(record["stdin"]), {"event": "user", "message": {"content": "做个任务"}})
        self.assertEqual(record["argv"], ["--output-format", "stream-json", "--input-format", "stream-json",
                                          "--disable-slash-commands", "--dangerously-skip-permissions",
                                          "--model", "gemini-3.8-pro-high", "--effort", "high"])
        self.assertEqual(Path(record["cwd"]).resolve(), Path(self.ctx.workdir).resolve())

    def test_agys_own_default_model_and_budget_pass_no_flag(self):
        result, *_rest, record = self.run_agy("ok", ctx=dataclasses.replace(self.ctx, model="", effort=""))
        self.assertFalse(result.is_error, result.text)
        self.assertNotIn("--model", record["argv"])
        self.assertNotIn("--effort", record["argv"])

    def test_without_skip_permissions_agy_asks_for_none(self):
        _result, *_rest, record = self.run_agy("ok", ctx=dataclasses.replace(self.ctx, skip_permissions=False))
        self.assertNotIn("--dangerously-skip-permissions", record["argv"])

    def test_resume_passes_the_conversation_and_rejects_another(self):
        result, *_rest, record = self.run_agy("ok", session=SID)
        self.assertFalse(result.is_error, result.text)
        self.assertEqual(record["argv"][record["argv"].index("--conversation") + 1], SID)
        result, _events, replies, _sessions, _record = self.run_agy("mismatch", session=SID)
        self.assertTrue(result.is_error)
        self.assertEqual(result.outcome.error_code, "protocol_error")
        self.assertEqual(replies, [])

    def test_failures_map_to_bridge_codes_without_native_text(self):
        for scenario, code in (("logged-out", "authentication_error"), ("garbage", "protocol_error"),
                               ("busy", "rate_limited"), ("error", "root_failed"),
                               ("exit-after-result", "root_failed")):
            with self.subTest(scenario=scenario):
                result, events, replies, _sessions, _record = self.run_agy(scenario)
                self.assertTrue(result.is_error)
                self.assertEqual(result.outcome.error_code, code)
                self.assertFalse(result.outcome.resume_allowed)
                self.assertNotIn("PRIVATE_DETAIL", result.text)
                self.assertEqual(replies, [])
                if scenario == "logged-out":
                    self.assertEqual(result.text, NOT_LOGGED_IN)
                    failure = [d for k, d in events if k == "backend_failure"][-1]
                    self.assertFalse([line for line in failure["stderr"] if "oauth2" in line])

    def test_stop_ends_the_process_and_is_not_resumable(self):
        cancel = threading.Event()
        threading.Timer(1.0, cancel.set).start()
        started = time.monotonic()
        result, *_rest = self.run_agy("slow", cancel=cancel)
        self.assertLess(time.monotonic() - started, 20)
        self.assertEqual(result.outcome.status, "cancelled")
        self.assertFalse(result.outcome.resume_allowed)

    def test_an_unknown_budget_never_starts_agy(self):
        with self.assertRaises(ValueError):
            self.backend.run(RunRequest(dataclasses.replace(self.ctx, effort="ultra"), "x"))
        self.assertFalse((self.root / "record.json").exists())

    def test_catalog_lists_agy_models_and_refuses_without_a_login(self):
        os.environ["FAKE_AGY_SCENARIO"] = "ok"
        catalog = self.backend.refresh_model_catalog(self.ctx)
        self.assertEqual(list(catalog), ["gemini-3.8-pro-high", "gemini-3.8-flash-medium", "claude-opus-5-5"])
        os.environ["FAKE_AGY_SCENARIO"] = "logged-out"
        with self.assertRaisesRegex(ValueError, "还没有登录"):
            self.backend.refresh_model_catalog(self.ctx)
        self.assertEqual(models_from_listing("Models\n────\n* gemini-x-pro  Gemini X Pro\n"),
                         {"gemini-x-pro": {"display_name": "gemini-x-pro", "supported_reasoning_levels": []}})

    def test_tool_parameters_read_like_the_other_harnesses(self):
        self.assertEqual(tool_inputs({"CommandLine": "ls", "Cwd": "/x"}), {"command": "ls"})
        self.assertEqual(tool_inputs({"AbsolutePath": "/a.py", "StartLine": 3}), {"path": "/a.py"})
        adapter = AntigravityEventAdapter(lambda kind, data: None)
        adapter.accept({"event": "step_update", "step_update": {
            "step_index": 4, "state": "ACTIVE", "step_type": "tool", "tool_name": "read_url_content",
            "tool_info": {"parameters": {"Url": "https://example.invalid/doc?token=PRIVATE#part"}}}}, 1.0)
        self.assertEqual(adapter.tools["step-4"]["detail"], "https://example.invalid/doc")
        self.assertEqual(adapter.tools["step-4"]["name"], "webfetch")


if __name__ == "__main__":
    unittest.main()
