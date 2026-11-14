"""Pi over ``pi --mode rpc``: its home and secrets, the turn's end, steering, stopping, sessions and catalog."""
import base64
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
from covey.core.roster import NAMES
from covey.app.launch_models import CATALOG_FILTERS, model_label
from covey.app.unified import UnifiedBridgeApp
from covey.backends.gateway import connection
from support import acme as acme_gateway
from covey.backends.pi import home
from covey.backends.pi.bridge import AUTH_TEXT, session_file, sessions_root
from covey.backends.pi.events import PiEventAdapter, failure_code
from covey.core.harness import RunCallbacks, RunInputs, RunRequest
from covey.core.providers import menu_name, provider_of
from covey.core.steer import SteerRefused

KEY = "sk-unit-private-key"
RUN = "33333333-3333-4333-8333-333333333333"
SID = "01a1a217-4147-747b-a08f-83323257ddf3"
FAKE = FIXTURES / "harness/pi_rpc.py"


def jwt(exp):
    part = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return "eyJhbGciOiJSUzI1NiJ9." + part + ".c2ln"


def codex_rows():
    levels = [{"effort": e} for e in ("low", "medium", "high", "xhigh", "max", "ultra")]
    return [{"slug": "gpt-6-astra", "display_name": "GPT-6-Astra", "visibility": "list", "context_window": 272000,
             "input_modalities": ["text", "image"], "supported_reasoning_levels": levels},
            {"slug": "gpt-6.1-sol", "display_name": "GPT-6.1-Sol", "visibility": "list", "context_window": 500000,
             "input_modalities": ["text", "image"], "supported_reasoning_levels": levels},
            {"slug": "codex-auto-review", "display_name": "Codex Auto Review", "visibility": "list",
             "supported_reasoning_levels": levels}]


class PiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.token = jwt(int(time.time()) + 86400)
        self.write_codex(self.token)
        clean = {k: v for k, v in os.environ.items() if not k.startswith(("ACME_", "PI_", "CODEX_", "FAKE_PI"))}
        patcher = mock.patch.dict(os.environ, {**clean, "HOME": str(self.root), "ACME_API_KEY": KEY,
                                               "FAKE_PI_RECORD": str(self.root / "record.json")}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.app = UnifiedBridgeApp(script_path=Path(__file__).resolve().parents[1] / "bin/covey_bridge.py")
        self.app.build_context(self.app.build_parser().parse_args(
            ["--workdir", str(self.root), "--state-dir", str(self.root / "state"), "--profile", "unit"]))
        self.backend = self.app.apps["pi"].backend
        self.ctx = dataclasses.replace(self.app.harnesses["pi"].context, backend_cmd=[sys.executable, "-I", str(FAKE)])
        path = connection.cache_path(acme_gateway(), self.ctx.state_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"updated_at": time.time(), "rows": [
            {"id": "gpt-6.1-sol", "protocol": connection.RESPONSES, "display_name": "gpt 6.1 sol",
             "supported_reasoning_levels": ["low", "high", "xhigh", "max"]},
            {"id": "seed-plain", "protocol": connection.RESPONSES}]}))

    def write_codex(self, token):
        codex = self.root / ".codex"
        codex.mkdir(exist_ok=True)
        (codex / "auth.json").write_text(json.dumps({"tokens": {"access_token": token, "refresh_token": "PRIVATE_REFRESH"}}))
        (codex / "models_cache.json").write_text(json.dumps({"models": codex_rows()}))

    def run_pi(self, scenario="ok", ctx=None, session=None, cancel=None, during=None, attachments=()):
        os.environ["FAKE_PI_SCENARIO"] = scenario
        events, replies, sessions, controls = [], [], [], []
        cancel = cancel or threading.Event()
        request = RunRequest(ctx or self.ctx, "做个任务", session_id=session, callbacks=RunCallbacks(
            event=lambda kind, data, control: events.append((kind, data)), session=sessions.append,
            control=controls.append, verified_reply=replies.append),
            inputs=RunInputs(cancel, run_id=RUN, attachments=tuple(attachments)))
        if during is None:
            result = self.backend.run(request)
        else:
            box = {}
            thread = threading.Thread(target=lambda: box.update(result=self.backend.run(request)))
            thread.start()
            during(events, controls, cancel)
            thread.join(30)
            self.assertFalse(thread.is_alive())
            result = box["result"]
        record = self.record()
        return result, events, replies, sessions, record

    def record(self):
        path = self.root / "record.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def wait_for(self, events, predicate):
        deadline = time.time() + 20
        while time.time() < deadline:
            if any(predicate(kind, data) for kind, data in list(events)):
                return
            time.sleep(0.02)
        self.fail("event never came")

    def test_codex_turn_is_verified_with_the_token_only_in_its_environment(self):
        result, events, replies, sessions, record = self.run_pi("tool")
        self.assertFalse(result.is_error, result.text)
        self.assertEqual((result.text, result.session_id, sessions), ("完成", SID, [SID]))
        self.assertTrue(result.outcome.resume_allowed)
        self.assertEqual(replies[-1].text, "完成")
        argv = record["argv"]
        self.assertEqual(argv[:2], ["--mode", "rpc"])
        self.assertEqual(argv[argv.index("--session-dir") + 1], str(self.ctx.state_dir / "pi-sessions"))
        self.assertEqual([argv[argv.index(f) + 1] for f in ("--provider", "--model", "--thinking")],
                         ["openai-codex", "gpt-6-astra", "high"])
        self.assertEqual(record["env"]["PI_CODEX_ACCESS_TOKEN"], self.token)
        self.assertIsNone(record["env"]["ACME_API_KEY"])  # Only the route's own secret.
        self.assertEqual((record["env"]["PI_TELEMETRY"], record["env"]["PI_OFFLINE"]), ("0", "1"))
        files = json.dumps(record["home"]) + json.dumps(argv)
        for secret in (self.token, "PRIVATE_REFRESH", KEY):
            self.assertNotIn(secret, files)
        self.assertEqual(record["home"]["auth.json"], {})
        # pi 1.x reads only $NAME or !command there; 0.73.1 runs the command too.
        self.assertEqual(record["home"]["models.json"]["providers"]["openai-codex"]["apiKey"],
                         "!printenv PI_CODEX_ACCESS_TOKEN")
        row = record["home"]["models.json"]["providers"]["openai-codex"]["models"][0]
        self.assertEqual((row["id"], row["api"], row["contextWindow"], row["input"]),
                         ("gpt-6-astra", "openai-codex-responses", 272000, ["text", "image"]))
        settings = record["home"]["settings.json"]
        self.assertEqual(settings["retry"]["provider"]["timeoutMs"], 2**31 - 1)
        self.assertTrue(settings["compaction"]["enabled"])
        # The model's commands run without the secrets the turn's pi reads.
        self.assertEqual(settings["shellCommandPrefix"], "unset PI_CODEX_ACCESS_TOKEN ACME_API_KEY")
        self.assertEqual(record["home_mode"], "0o700")
        self.assertEqual(list((self.ctx.state_dir / "tmp").glob("pi-home-*")), [])  # Removed with the turn.
        self.assertEqual([c["type"] for c in record["commands"]][:2], ["get_state", "prompt"])
        kinds = [kind for kind, _ in events]
        for kind in ("model_request", "model_reasoning", "first_token", "model_output", "token_usage", "trace_finished"):
            self.assertIn(kind, kinds)
        tools = [data for kind, data in events if kind == "tool"]
        self.assertEqual([(t["name"], t["detail"], t["status"]) for t in tools], [("bash", "sleep 6", "running"), ("bash", "sleep 6", "done")])
        notes = [data["text"] for kind, data in events if kind == "progress_note"]
        self.assertEqual(notes, ["先看一下目录"])
        usage = [data for kind, data in events if kind == "token_usage"][-1]
        self.assertEqual((usage["output_tokens"], usage["input_tokens"], usage["cache_read_tokens"], usage["context_window"]),
                         (14, 300, 100, 272000))
        self.assertEqual([data["ttft_source"] for kind, data in events if kind == "first_token"], ["pi_delta"])

    def test_budgets_map_to_pi_thinking_and_ultra_is_never_offered(self):
        _, _, _, _, record = self.run_pi(ctx=dataclasses.replace(self.ctx, effort="max"))
        self.assertEqual(record["argv"][record["argv"].index("--thinking") + 1], "xhigh")
        levels = record["home"]["models.json"]["providers"]["openai-codex"]["models"][0]["thinkingLevelMap"]
        self.assertEqual(levels, {"off": None, "minimal": None, "xhigh": "max"})
        catalog = self.backend.model_catalog(self.ctx)
        self.assertEqual(set(catalog), {"gpt-6-astra", "gpt-6.1-sol", "acme/gpt-6.1-sol"})  # GPT models only.
        for row in catalog.values():
            self.assertNotIn("ultra", [level["effort"] for level in row["supported_reasoning_levels"]])
        os.unlink(self.root / "record.json")
        with self.assertRaises(ValueError):
            self.run_pi(ctx=dataclasses.replace(self.ctx, effort="ultra"))
        self.assertEqual(self.record(), {})  # pi never started.

    def test_acme_turn_runs_behind_the_relay_with_the_sk_in_its_environment(self):
        ctx = dataclasses.replace(self.ctx, model="acme/gpt-6.1-sol", effort="xhigh")
        result, _, _, _, record = self.run_pi(ctx=ctx)
        self.assertFalse(result.is_error, result.text)
        argv = record["argv"]
        self.assertEqual([argv[argv.index(f) + 1] for f in ("--provider", "--model", "--thinking")],
                         ["acme", "gpt-6.1-sol", "xhigh"])
        provider = record["home"]["models.json"]["providers"]["acme"]
        self.assertRegex(provider["baseUrl"], r"^http://127\.0\.0\.1:\d+/v1$")
        self.assertEqual((provider["api"], provider["apiKey"], provider["headers"]),
                         ("openai-responses", "!printenv ACME_API_KEY", {"x-source": acme_gateway().header_dict()['x-source']}))
        self.assertEqual(provider["models"][0]["contextWindow"], 500000)  # The account's model of that name.
        self.assertEqual(record["env"]["ACME_API_KEY"], KEY)
        self.assertIsNone(record["env"]["PI_CODEX_ACCESS_TOKEN"])
        self.assertIn("127.0.0.1", record["env"]["NO_PROXY"].split(","))
        self.assertNotIn(KEY, json.dumps(record["home"]) + json.dumps(argv))

    def test_resume_and_fork_pass_the_session_file_never_a_bare_id(self):
        result, _, _, _, record = self.run_pi()
        path = session_file(sessions_root(self.ctx), SID)
        self.assertEqual(path.parent, sessions_root(self.ctx))
        self.assertEqual(self.app.harnesses["pi"].store.transcript(self.ctx.workdir, SID), path)
        self.assertTrue(path.is_file())
        result, _, _, _, record = self.run_pi(session=SID)
        self.assertFalse(result.is_error, result.text)
        self.assertEqual(record["argv"][record["argv"].index("--session") + 1], str(path))
        fork = dataclasses.replace(self.ctx, options={**self.ctx.options, "fork_session": SID})
        result, _, _, _, record = self.run_pi(ctx=fork)
        self.assertEqual(record["argv"][record["argv"].index("--fork") + 1], str(path))
        self.assertNotIn("--session", record["argv"])
        self.assertNotEqual(result.session_id, SID)
        result, *_ = self.run_pi("mismatch", session=SID)
        self.assertEqual(result.outcome.error_code, "protocol_error")
        with self.assertRaises(ValueError):
            self.run_pi(session="01a1a21e-0000-7000-8000-000000000000")

    def test_an_agent_end_followed_by_a_retry_is_not_the_end(self):
        result, events, *_ = self.run_pi("retry")
        self.assertFalse(result.is_error, result.text)
        self.assertEqual(result.text, "完成")
        retries = [data["retry"] for kind, data in events if kind == "progress_note" and "retry" in data]
        self.assertEqual([r["attempt"] for r in retries], [1, 2])
        self.assertEqual(retries[0]["reason"], "上游限流（429）")
        result, *_ = self.run_pi("retry-spent")
        self.assertEqual(result.outcome.error_code, "rate_limited")

    def test_failures_map_to_bridge_codes(self):
        result, *_ = self.run_pi("auth")
        self.assertEqual(result.outcome.error_code, "authentication_error")
        self.assertEqual(result.failure_detail, AUTH_TEXT)
        self.assertNotIn("Unauthorized", result.text)
        for scenario, code in (("refused", "authentication_error"), ("length", "root_incomplete"),
                               ("exit-after-end", "process_exit")):
            result, *_ = self.run_pi(scenario)
            self.assertEqual(result.outcome.error_code, code, scenario)
            self.assertFalse(result.outcome.resume_allowed)
        self.assertEqual(failure_code("403 quota exceeded: Daily Limits exceeded"), "quota_exhausted")
        self.assertEqual(failure_code("You have hit your ChatGPT usage limit (pro plan)"), "rate_limited")
        self.assertEqual(failure_code("Requests have exceeded the throughput limit on your Provisioned-Managed deployment."),
                         "rate_limited")
        self.assertEqual(failure_code("500 internal error"), "root_failed")

    def test_an_expiring_codex_token_stops_the_turn_before_pi_starts(self):
        self.write_codex(jwt(int(time.time()) + 600))
        result, *_ = self.run_pi()
        self.assertEqual(result.outcome.error_code, "authentication_error")
        self.assertIn("在 Codex Harness 跑一轮", result.failure_detail)
        self.assertEqual(self.record(), {})
        self.assertEqual(local_check("pi", dataclasses.replace(self.ctx, backend_cmd=[sys.executable]))["status"],
                         "configured")  # Only a missing login is unavailable; the turn says why it stopped.
        (self.root / ".codex/auth.json").unlink()
        self.assertEqual(local_check("pi", dataclasses.replace(self.ctx, backend_cmd=[sys.executable]))["reason"],
                         "authentication")

    def test_steer_is_acknowledged_and_read(self):
        def during(events, controls, cancel):
            self.wait_for(events, lambda kind, data: kind == "tool" and data["status"] == "running")
            controls[0].steer("改成 banana", "ev-1")
        result, events, *_ = self.run_pi("steer", during=during)
        self.assertEqual(result.text, "banana")
        self.assertIn(("steer_read", {"event_id": "ev-1"}), events)

    def test_a_steer_read_before_its_ack_is_confirmed_and_read(self):
        """pi may start the steered message before it prints the ack. The ack was waited for under the lock the
        turn's loop takes to see that start, so the wait ran out (8 s) and the read was lost."""
        took = {}
        def during(events, controls, cancel):
            self.wait_for(events, lambda kind, data: kind == "tool" and data["status"] == "running")
            started = time.time()
            controls[0].steer("改成 banana", "ev-3")
            took["seconds"] = time.time() - started
        result, events, *_ = self.run_pi("steer-early", during=during)
        self.assertEqual(result.text, "banana")
        self.assertIn(("steer_read", {"event_id": "ev-3"}), events)
        self.assertLess(took["seconds"], 5)

    def test_an_unconfirmed_steer_that_pi_reads_is_still_marked_read(self):
        def during(events, controls, cancel):
            self.wait_for(events, lambda kind, data: kind == "tool" and data["status"] == "running")
            with mock.patch("covey.backends.pi.rpc.ACK_SECONDS", 0.5), self.assertRaises(RuntimeError):
                controls[0].steer("改成 banana", "ev-4")  # Possibly delivered, never sent again (rule 6).
        result, events, *_ = self.run_pi("steer-unacked", during=during)
        self.assertEqual(result.text, "banana")
        self.assertIn(("steer_read", {"event_id": "ev-4"}), events)

    def test_a_refused_steer_raises_steer_refused(self):
        def during(events, controls, cancel):
            self.wait_for(events, lambda kind, data: kind == "tool")
            with self.assertRaises(SteerRefused):
                controls[0].steer("再看看", "ev-2")
        result, *_ = self.run_pi("steer-refused", during=during)
        self.assertFalse(result.is_error, result.text)

    def test_stop_aborts_the_turn_and_ends_the_process(self):
        def during(events, controls, cancel):
            self.wait_for(events, lambda kind, data: kind == "tool")
            cancel.set()
        started = time.time()
        result, events, _, _, record = self.run_pi("abort", during=during)
        self.assertLess(time.time() - started, 15)
        self.assertEqual(result.outcome.status, "cancelled")
        self.assertIn("abort", [c["type"] for c in record["commands"]])
        self.assertEqual([data["status"] for kind, data in events if kind == "tool"][-1], "failed")

    def test_auto_compaction_after_the_end_is_waited_for(self):
        result, events, _, _, record = self.run_pi("compact")
        self.assertFalse(result.is_error, result.text)
        self.assertEqual([c["type"] for c in record["commands"]].count("get_state"), 3)
        steps = [data for kind, data in events if kind == "tool" and data["name"] == "context_compaction"]
        self.assertEqual([s["status"] for s in steps], ["running", "done"])

    def test_adapter_shows_one_compaction_as_one_step(self):
        events = []
        adapter = PiEventAdapter(lambda kind, data: events.append((kind, data)))
        done = {"type": "compaction_end", "reason": "threshold", "result": {"summary": "s"}, "aborted": False}
        adapter.accept({"type": "compaction_start", "reason": "threshold"}, 10.0)
        adapter.accept({"type": "compaction_start", "reason": "threshold"}, 20.0)  # A repeat is that step.
        adapter.accept(done, 30.0)
        adapter.accept(done, 40.0)  # None is open: it changes no step.
        adapter.accept({"type": "compaction_start", "reason": "overflow"}, 50.0)
        steps = [data for kind, data in events if kind == "tool"]
        self.assertEqual([(s["id"], s["status"], s["started_at"], s.get("ended_at")) for s in steps],
                         [("compaction-1", "running", 10.0, None), ("compaction-1", "done", 10.0, 30.0),
                          ("compaction-2", "running", 50.0, None)])

    def test_images_go_with_the_prompt(self):
        image = self.root / "a.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        _, _, _, _, record = self.run_pi(attachments=[{"path": str(image), "mime": "image/png"}])
        self.assertEqual(record["commands"][1]["images"], 1)

    def test_an_image_pi_cannot_take_ends_the_turn_with_an_outcome(self):
        """The image was read before the turn's saved reply was cleared, and its error left run_rpc: the task
        said 处理出错（ValueError） with no outcome."""
        image = self.root / "a.bmp"
        image.write_bytes(b"BMfake")
        for attachment, code in (({"path": str(image), "mime": "image/bmp"}, "protocol_error"),
                                 ({"path": str(self.root / "gone.png"), "mime": "image/png"}, "transport_error")):
            with self.subTest(code=code):
                result, events, replies, _, record = self.run_pi(attachments=[attachment])
                self.assertTrue(result.is_error)
                self.assertEqual((result.outcome.status, result.outcome.error_code), ("failed", code))
                self.assertEqual(replies, [None])  # The saved reply was cleared first.
                self.assertEqual((events, record), ([], {}))  # pi never started.

    def test_adapter_publishes_no_reasoning_and_counts_only_generation(self):
        events = []
        adapter = PiEventAdapter(lambda kind, data: events.append((kind, data)))
        adapter.accept({"type": "turn_start"}, 10.0)
        adapter.accept({"type": "message_start", "message": {"role": "assistant"}}, 10.2)
        adapter.accept({"type": "message_update", "assistantMessageEvent": {"type": "thinking_delta", "delta": "PRIVATE"}}, 10.5)
        adapter.accept({"type": "message_end", "message": {"role": "assistant", "stopReason": "stop", "usage": {
            "input": 102, "output": 5, "cacheRead": 11392, "cacheWrite": 0}}}, 12.0)
        adapter.accept({"type": "tool_execution_start", "toolCallId": "t", "toolName": "bash", "args": {"command": "ls"}}, 13.0)
        self.assertNotIn("PRIVATE", json.dumps(events))
        usage = [data for kind, data in events if kind == "token_usage"][0]
        self.assertEqual((usage["input_tokens"], usage["generation_seconds"], usage["context_tokens"]), (11494, 2.0, 11499))

    def test_adapter_details_keep_a_urls_query_off_the_card(self):
        adapter = PiEventAdapter(lambda kind, data: None)
        adapter.accept({"type": "tool_execution_start", "toolCallId": "w", "toolName": "fetch",
                        "args": {"url": "https://example.invalid/doc?token=PRIVATE#part"}}, 1.0)
        adapter.accept({"type": "tool_execution_start", "toolCallId": "g", "toolName": "grep",
                        "args": {"pattern": "def main", "path": "src"}}, 2.0)
        adapter.accept({"type": "tool_execution_start", "toolCallId": "b", "toolName": "bash",
                        "args": {"command": "ls", "description": "List files"}}, 3.0)
        self.assertEqual([adapter.tools[k]["detail"] for k in "wgb"],
                         ["https://example.invalid/doc", "src", "List files · ls"])

    def test_an_answer_held_by_acme_is_timed_from_the_prompt(self):
        """Acme hands a whole response over at once: #2693 read TTFT 0.004 s and 15,800 token/s when both
        clocks started at pi's message_start, which comes with the response."""
        os.environ["FAKE_PI_HOLD"] = "1.0"
        result, events, *_ = self.run_pi()
        self.assertFalse(result.is_error, result.text)
        ttft = [data["ttft_seconds"] for kind, data in events if kind == "first_token"]
        usage = [data for kind, data in events if kind == "token_usage"][-1]
        self.assertEqual(len(ttft), 1)
        self.assertGreaterEqual(ttft[0], 0.9)
        self.assertGreaterEqual(usage["generation_seconds"], ttft[0])
        self.assertEqual(usage["measured_output_tokens"], 7)
        self.assertLess(usage["measured_output_tokens"] / usage["generation_seconds"], 10)
        self.assertEqual(len([kind for kind, _ in events if kind == "model_request"]), 1)

    def test_adapter_times_each_request_from_its_send_as_the_relay_delivers_it(self):
        events = []
        adapter = PiEventAdapter(lambda kind, data: events.append((kind, data)))
        user = {"role": "user", "content": [{"type": "text", "text": "做个任务"}]}
        def answer(at, reason, output):  # The whole response within 5 ms, as the relay hands it over.
            adapter.accept({"type": "message_start", "message": {"role": "assistant"}}, at)
            adapter.accept({"type": "message_update", "assistantMessageEvent": {"type": "thinking_start"}}, at + 0.001)
            adapter.accept({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "好"}}, at + 0.002)
            adapter.accept({"type": "message_end", "message": {"role": "assistant", "stopReason": reason, "content": [],
                            "usage": {"input": 10, "output": output, "cacheRead": 0, "cacheWrite": 0}}}, at + 0.005)
        adapter.request_sent(100.0)
        for row in ({"type": "agent_start"}, {"type": "turn_start"}, {"type": "message_start", "message": user},
                    {"type": "message_end", "message": user}):
            adapter.accept(row, 100.0)
        answer(160.0, "toolUse", 900)  # Held 60 s.
        adapter.accept({"type": "tool_execution_start", "toolCallId": "t", "toolName": "bash", "args": {"command": "sleep 30"}}, 160.01)
        adapter.accept({"type": "queue_update", "steering": ["x"]}, 170.0)  # Not a request's send.
        adapter.accept({"type": "tool_execution_end", "toolCallId": "t", "toolName": "bash"}, 190.0)
        for row in ({"type": "message_end", "message": {"role": "toolResult", "content": []}}, {"type": "turn_end"},
                    {"type": "turn_start"}):
            adapter.accept(row, 190.0)
        answer(220.0, "error", 0)  # A failed request and the retry's wait add nothing.
        adapter.accept({"type": "agent_end", "messages": []}, 220.01)
        adapter.accept({"type": "auto_retry_start", "attempt": 1, "errorMessage": "502"}, 220.01)
        adapter.accept({"type": "agent_start"}, 222.0)
        adapter.accept({"type": "turn_start"}, 222.0)
        answer(252.0, "stop", 600)
        ttft = [data["ttft_seconds"] for kind, data in events if kind == "first_token"]
        self.assertEqual(len(ttft), 1)
        self.assertAlmostEqual(ttft[0], 60.001, places=3)
        usage = [data for kind, data in events if kind == "token_usage"][-1]
        self.assertEqual((usage["output_tokens"], usage["measured_output_tokens"]), (1500, 1500))
        self.assertAlmostEqual(usage["generation_seconds"], 90.01, places=3)  # 60.005 + 30.005.
        self.assertEqual([data["received_at"] for kind, data in events if kind == "model_request"], [100.0])

    def test_adapter_gives_no_figure_for_a_request_it_cannot_time(self):
        events = []
        adapter = PiEventAdapter(lambda kind, data: events.append((kind, data)))
        def answer(at, output):
            adapter.accept({"type": "message_start", "message": {"role": "assistant"}}, at)
            adapter.accept({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "好"}}, at)
            adapter.accept({"type": "message_end", "message": {"role": "assistant", "stopReason": "stop", "content": [],
                            "usage": {"input": 10, "output": output, "cacheRead": 0, "cacheWrite": 0}}}, at + 0.003)
        answer(10.0, 14)  # No send seen: neither TTFT nor generation time.
        adapter.accept({"type": "turn_start"}, 20.0)
        answer(20.6, 9660)  # About 16,000 token/s, faster than any GPT route: timed wrong, left out.
        self.assertNotIn("first_token", [kind for kind, _ in events])
        usage = [data for kind, data in events if kind == "token_usage"][-1]
        self.assertEqual((usage["output_tokens"], usage["measured_output_tokens"], usage["generation_seconds"]),
                         (9674, 0, 0.0))

    def test_providers_labels_and_menus(self):
        self.assertIn("pi", NAMES)
        own, box = provider_of("pi", "gpt-6-astra"), provider_of("pi", "acme/gpt-6-astra")
        self.assertEqual((own.key, box.key), ("openai", "acme"))
        self.assertEqual((menu_name(own, "Pi"), menu_name(box, "Pi")), ("Codex 自带", "Acme"))
        view = CATALOG_FILTERS["pi"].view({"acme/gpt-6-astra": {"display_name": "gpt-6-astra (Acme)"},
                                    "gpt-6-astra": {"display_name": "GPT-6-Astra"}})
        self.assertEqual(model_label("Pi", {**view["gpt-6-astra"], "source": own.name}), "Pi · Codex · GPT-6 Astra")
        self.assertEqual(model_label("Pi", {**view["acme/gpt-6-astra"], "source": box.name}),
                         "Pi · Acme · GPT-6 Astra")
        self.assertEqual((self.ctx.model, self.ctx.effort), ("gpt-6-astra", "high"))

    def test_token_expiry_reads_the_jwt(self):
        self.assertEqual(home.token_expiry(jwt(1793721213)), 1793721213)
        self.assertIsNone(home.token_expiry("not-a-jwt"))
        with self.assertRaises(ValueError):
            home.access_token(now=time.time() + 86400)


if __name__ == "__main__":
    unittest.main()
