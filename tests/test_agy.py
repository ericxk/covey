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
from covey.core.steer import SteerNotReady, SteerRefused

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
        for leftover in (path, path.with_name("record.json.all")):
            if leftover.exists():
                leftover.unlink()
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
        usage = [d for k, d in events if k == "token_usage"]
        self.assertEqual(len(usage), 2)  # One a model request, the result's sum not added again.
        self.assertEqual((usage[-1]["output_tokens"], usage[-1]["input_tokens"], usage[-1]["cache_read_tokens"]),
                         (132, 15436 + 11130, 11130))
        self.assertEqual((usage[-1]["measured_output_tokens"], usage[-1]["generation_seconds"]), (132, 5.0))
        self.assertEqual(events[-1][0], "trace_finished")
        self.assertEqual(json.loads(record["stdin"]), {"event": "user", "message": {"content": "做个任务"}})
        self.assertEqual(record["argv"], ["--output-format", "stream-json", "--input-format", "stream-json",
                                          "--disable-slash-commands", "--dangerously-skip-permissions",
                                          "--model", "gemini-3.8-pro-high", "--effort", "high"])
        self.assertEqual(Path(record["cwd"]).resolve(), Path(self.ctx.workdir).resolve())

    def test_the_answer_is_what_the_model_wrote_after_its_last_tool_subagent_or_system_message(self):
        # agy's result.response joins the progress lines and the waits before the answer (T588 #5298, #5309).
        result, events, replies, *_rest = self.run_agy("waits")
        self.assertFalse(result.is_error, result.text)
        self.assertEqual((result.text, replies[-1].text), ("完成", "完成"))
        notes = [d.get("text") for k, d in events if k == "progress_note"]
        self.assertIn("正在等待子 Agent。\n", notes)
        self.assertIn(("agent", "done"), [(d["name"], d["status"]) for k, d in events if k == "tool" and d["id"] == "step-3"])

    def test_an_answer_that_is_not_the_end_of_the_response_falls_back_to_the_response(self):
        from covey.backends.agy.bridge import StreamRows
        rows = StreamRows(None, AntigravityEventAdapter(lambda *a: None), None)
        rows.sid, rows.result = SID, {"response": "全文"}
        rows.kinds, rows.texts = {0: "agent_response"}, {0: "别的"}
        self.assertEqual(rows.reply(), "全文")
        rows.texts = {}
        self.assertEqual(rows.reply(), "全文")

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

    def test_a_supplement_stops_the_turn_and_the_conversation_goes_on_with_it(self):
        os.environ["FAKE_AGY_SCENARIO"] = "steer"
        events, replies, controls, tools = [], [], [], threading.Event()

        def event(kind, data, control):
            events.append((kind, data))
            if kind == "tool" and data.get("status") == "running":
                tools.set()
        done = {}
        request = RunRequest(self.ctx, "做个任务", callbacks=RunCallbacks(
            event=event, control=controls.append, verified_reply=replies.append),
            inputs=RunInputs(threading.Event(), run_id=RUN))
        worker = threading.Thread(target=lambda: done.update(result=self.backend.run(request)))
        worker.start()
        self.assertTrue(tools.wait(20))
        control = controls[0]
        control.steer("改成 B", "ev-1")
        worker.join(30)
        result = done["result"]
        self.assertFalse(result.is_error, result.text)
        self.assertEqual((result.text, result.session_id), ("完成", SID))
        runs = [json.loads(line) for line in (self.root / "record.json.all").read_text().splitlines()]
        self.assertEqual(len(runs), 2)
        self.assertNotIn("--conversation", runs[0]["argv"])
        self.assertEqual(runs[1]["argv"][runs[1]["argv"].index("--conversation") + 1], SID)
        message = json.loads(runs[1]["stdin"])["message"]["content"]
        self.assertTrue(message.startswith("用户补充（你刚才的执行被这条补充打断了") and message.endswith("改成 B"), message)
        self.assertIn(("steer_read", {"event_id": "ev-1"}), events)
        steps = {}
        for kind, data in events:
            if kind == "tool":
                steps[data["id"]] = data["status"]
        # The stopped command ends failed; the next process's steps have keys of their own.
        self.assertEqual(steps, {"step-2": "failed", "step-1-2": "done"})
        self.assertEqual([k for k, _d in events].count("trace_finished"), 1)
        child = int((self.root / "record.json.child").read_text())
        self.assertFalse(Path(f"/proc/{child}").exists() and "python" in Path(f"/proc/{child}/cmdline").read_text())
        self.assertIsNone(controls[-1])  # The controls are dropped when the turn ends.
        with self.assertRaises(SteerRefused):
            control.steer("太晚了", "ev-2")

    def test_a_supplement_before_agy_said_its_conversation_waits(self):
        from covey.backends.agy.bridge import AgyControl
        control = AgyControl(lambda *a: None)
        with self.assertRaises(SteerNotReady):
            control.steer("x", "ev")
        with self.assertRaises(SteerRefused):
            control.steer("  ", "ev")

    def test_an_unknown_budget_never_starts_agy(self):
        with self.assertRaises(ValueError):
            self.backend.run(RunRequest(dataclasses.replace(self.ctx, effort="ultra"), "x"))
        self.assertFalse((self.root / "record.json").exists())

    def test_catalog_lists_agy_models_and_refuses_without_a_login(self):
        os.environ["FAKE_AGY_SCENARIO"] = "ok"
        catalog = self.backend.refresh_model_catalog(self.ctx)
        self.assertEqual(catalog, {
            "gemini-4-neon": {"display_name": "Gemini 4 Neon", "default_reasoning_level": "high",
                              "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}]},
            "gemini-pro-orchestrator": {"display_name": "Orchestrator (Carbon)", "supported_reasoning_levels": []},
            "gemini-3.8-flash": {"display_name": "Gemini 3.8 Flash", "default_reasoning_level": "medium",
                                 "supported_reasoning_levels": [{"effort": "medium"}]}})
        os.environ["FAKE_AGY_SCENARIO"] = "logged-out"
        with self.assertRaisesRegex(ValueError, "还没有登录"):
            self.backend.refresh_model_catalog(self.ctx)
        self.assertEqual(models_from_listing("Fetching available models...\nModels\n"), {})

    def test_the_saved_catalog_keeps_budgets_agy_refuses_from_it(self):
        from covey.app.launch_models import refresh_catalog
        os.environ["FAKE_AGY_SCENARIO"] = "ok"
        refresh_catalog(self.app.apps["agy"], self.ctx)
        with self.assertRaisesRegex(ValueError, "不支持所选推理档位"):
            self.backend.run(RunRequest(dataclasses.replace(self.ctx, model="gemini-4-neon", effort="medium"), "x"))
        self.assertFalse((self.root / "record.json").exists())
        _result, *_rest, record = self.run_agy("ok", ctx=dataclasses.replace(
            self.ctx, model="gemini-pro-orchestrator", effort="high"))
        self.assertNotIn("--effort", record["argv"])  # agy refuses a budget for a model without any.
        _result, *_rest, record = self.run_agy("ok", ctx=dataclasses.replace(self.ctx, model="gemini-4-neon"))
        self.assertEqual(record["argv"][-4:], ["--model", "gemini-4-neon", "--effort", "high"])

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
