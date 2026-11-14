"""Acme through the personal SK: directory, key handling and Codex's scoped provider."""
import dataclasses
import datetime
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from support.fake_backends import codex_server
from covey.app.launch_models import available_models, refresh_catalog
from covey.app.unified import UnifiedBridgeApp
from support import acme as acme_gateway
from covey.backends.codex import gateway as codex_gateway
from covey.backends.gateway import connection
from covey.backends.gateway.catalog import launch_catalog
from covey.core.menu_choice import effort_options
from covey.core.defaults import RECOMMENDED
from covey.core.harness import RunCallbacks, RunInputs, RunRequest
from covey.core.serving import serve_thread

KEY = "sk-unit-private-key"


def rows():
    return [
        {"id": "auto", "protocol": "anthropic-messages"},
        {"id": "ali-deepseek-v4.1-flash", "protocol": "anthropic-messages",
         "supported_reasoning_levels": ["low", "high", "max"], "default_reasoning_level": "high"},
        {"id": "gpt-6-astra", "protocol": "openai-responses", "display_name": "gpt-6-astra",
         "supported_reasoning_levels": ["low", "medium", "high", "xhigh", "max"], "default_reasoning_level": "medium"},
        {"id": "gpt-6.1-sol", "protocol": "openai-responses",
         "supported_reasoning_levels": ["low", "medium", "high", "xhigh", "max"], "default_reasoning_level": "low"},
        {"id": "seed-evolving", "protocol": "anthropic"},
        {"id": "gpt-hidden", "protocol": "openai-responses", "hidden": True},
    ]


def official():
    return {"gpt-6-astra": {"slug": "gpt-6-astra", "display_name": "GPT-6-Astra", "visibility": "list",
                            "supported_reasoning_levels": [{"effort": e} for e in ("low", "high", "ultra")]}}


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class AcmeTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        clean = {k: v for k, v in os.environ.items() if not k.startswith(("ACME_", "XDG_CONFIG_HOME"))}
        patcher = mock.patch.dict(os.environ, {**clean, "HOME": str(self.root), "ACME_API_KEY": KEY}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.state = self.root / "state"

    def fetch(self, data=None):
        body = json.dumps({"data": rows() if data is None else data}).encode()
        return mock.patch("covey.backends.gateway.connection.NO_REDIRECT.open",
                          return_value=Response(body))


class GatewayTests(AcmeTestCase):
    def test_directory_splits_by_protocol_and_drops_routers_and_hidden_models(self):
        self.assertEqual(set(connection.parse_catalog(rows(), connection.RESPONSES)), {"gpt-6-astra", "gpt-6.1-sol"})
        messages = connection.parse_catalog(rows(), connection.MESSAGES)
        # "anthropic" is Acme's newer name for anthropic-messages (2026-11).
        self.assertEqual(set(messages), {"ali-deepseek-v4.1-flash", "seed-evolving"})
        self.assertEqual([r["effort"] for r in messages["ali-deepseek-v4.1-flash"]["supported_reasoning_levels"]],
                         ["low", "high", "max"])
        self.assertEqual(messages["ali-deepseek-v4.1-flash"]["default_reasoning_level"], "high")

    def test_request_carries_key_and_source_and_the_cache_expires(self):
        with self.fetch() as urlopen:
            connection.refresh_catalog(acme_gateway(), self.state, connection.RESPONSES)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://gateway.example.com/v1/models")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + KEY)
        self.assertEqual(request.get_header("X-source"), "bridge-tests")
        self.assertNotIn(KEY, connection.cache_path(acme_gateway(), self.state).read_text())
        self.assertIn("gpt-6.1-sol", connection.cached_catalog(acme_gateway(), self.state, connection.RESPONSES))
        saved = json.loads(connection.cache_path(acme_gateway(), self.state).read_text())
        saved["updated_at"] = time.time() - connection.CACHE_SECONDS - 1
        connection.cache_path(acme_gateway(), self.state).write_text(json.dumps(saved))
        self.assertEqual(connection.cached_catalog(acme_gateway(), self.state, connection.RESPONSES), {})

    def test_failures_never_expose_response_bodies(self):
        error = urllib.error.HTTPError("https://acme", 403, "PRIVATE_BODY " + KEY, {}, io.BytesIO(b"PRIVATE_BODY"))
        with mock.patch("covey.backends.gateway.connection.NO_REDIRECT.open", side_effect=error):
            with self.assertRaises(ValueError) as caught:
                connection.refresh_catalog(acme_gateway(), self.state, connection.RESPONSES)
        self.assertNotIn("PRIVATE_BODY", str(caught.exception))
        self.assertNotIn(KEY, str(caught.exception))

    def test_a_redirect_is_refused_and_the_key_never_follows_it(self):
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                seen.append((self.server.server_address[1], self.path, self.headers.get("Authorization")))
                moved = self.path == "/v1/models"
                body = b"" if moved else json.dumps({"data": rows()}).encode()
                self.send_response(302 if moved else 200)
                if moved:
                    self.send_header("Location", f"http://127.0.0.1:{elsewhere.server_address[1]}/v1/models/")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        upstream_server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        elsewhere = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        for server in (upstream_server, elsewhere):
            serve_thread(server).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        os.environ["ACME_BASE_URL"] = f"http://127.0.0.1:{upstream_server.server_address[1]}"
        with self.assertRaisesRegex(ValueError, "模型目录不可用"):
            connection.refresh_catalog(acme_gateway(), self.state, connection.RESPONSES)
        self.assertEqual(seen, [(upstream_server.server_address[1], "/v1/models", "Bearer " + KEY)])  # Never the other address.

    def test_key_comes_from_the_environment_or_an_owner_only_file(self):
        self.assertEqual(connection.api_key(acme_gateway()), KEY)
        del os.environ["ACME_API_KEY"]
        self.assertEqual(connection.api_key(acme_gateway()), "")
        with self.assertRaisesRegex(ValueError, "个人密钥未配置"):
            connection.child_env(acme_gateway(), {})
        path = self.root / ".config/harness-bridge/acme-api-key"
        path.parent.mkdir(parents=True)
        path.write_text("sk-from-file\n")
        path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "权限 600"):
            connection.api_key(acme_gateway())
        path.chmod(0o600)
        self.assertEqual(connection.child_env(acme_gateway(), {"A": "1"}), {"A": "1", "ACME_API_KEY": "sk-from-file"})

    def test_task_model_and_effort_are_checked_against_the_directory(self):
        with self.fetch():
            connection.check_model(acme_gateway(), self.state, connection.RESPONSES, "gpt-6-astra", "xhigh")
        for model, effort in (("missing", ""), ("gpt-6-astra", "ultra"), ("auto", "")):
            with self.subTest(model=model, effort=effort), self.assertRaises(ValueError):
                connection.check_model(acme_gateway(), self.state, connection.RESPONSES, model, effort)


class CodexAcmeTests(AcmeTestCase):
    def setUp(self):
        super().setUp()
        self.app = UnifiedBridgeApp(script_path=Path(__file__).resolve().parents[1] / "bin/covey_bridge.py")
        self.ctx = self.app.build_context(self.app.build_parser().parse_args(
            ["--workdir", str(self.root), "--state-dir", str(self.state), "--profile", "unit"]))
        self.codex = self.app.apps["codex"]
        official_patch = mock.patch("covey.core.model_settings.model_catalog", return_value=official())
        official_patch.start()
        self.addCleanup(official_patch.stop)

    def refresh(self, name):
        with self.fetch():
            return refresh_catalog(self.app.apps[name], self.app.harnesses[name].context)

    def test_launch_card_keeps_account_and_acme_models_apart(self):
        self.refresh("codex")
        self.refresh("dsh")
        choices = available_models(self.app.topics, self.ctx)
        self.assertEqual(choices["codex:gpt-6-astra"]["levels"], ["low", "high", "ultra"])
        # Codex runs ultra itself and sends xhigh, so an Acme model has it when the account's
        # model of the same name does and Acme serves xhigh.
        self.assertEqual(choices["codex:acme/gpt-6-astra"]["levels"], ["low", "medium", "high", "xhigh", "max", "ultra"])
        self.assertEqual(effort_options(choices["codex:acme/gpt-6-astra"])[0][0], (RECOMMENDED, "推荐 · xhigh"))
        self.assertNotIn("ultra", choices["codex:acme/gpt-6.1-sol"]["levels"])
        without_xhigh = launch_catalog(acme_gateway(), {"gpt-6-astra": {"display_name": "gpt-6-astra",
                                                               "supported_reasoning_levels": [{"effort": "max"}]}}, official())
        self.assertEqual(without_xhigh["acme/gpt-6-astra"]["supported_reasoning_levels"], [{"effort": "max"}])
        self.assertEqual(choices["codex:acme/gpt-6-astra"]["model"], "acme/gpt-6-astra")
        # Harness · provider · model, the provider before the name: a topic's side panel showed Codex · GPT-6 Astra · LLM…
        # (UI review P15). The values stay the route keys, in the same order.
        self.assertEqual(choices["codex:acme/gpt-6-astra"]["label"], "Codex · Acme · GPT-6 Astra")
        self.assertEqual(choices["codex:acme/gpt-6.1-sol"]["label"], "Codex · Acme · GPT-6.1 Sol")
        self.assertEqual(choices["codex:acme/gpt-6-astra"]["display_name"], "GPT-6 Astra")
        # Codex's own name read like the others (2026-11-03: it was GPT-6-Astra next to MASO's GPT-6 Astra).
        self.assertEqual(choices["codex:gpt-6-astra"]["label"], "Codex · GPT-6 Astra")
        self.assertEqual(choices["dsh:ali-deepseek-v4.1-flash"]["label"], "DeepSeek Harness · DeepSeek V4.1 Flash")
        self.assertFalse({"codex:acme/ali-deepseek-v4.1-flash", "dsh:gpt-6-astra", "dsh:auto",
                          "codex:acme/auto", "codex:acme/seed-evolving"} & set(choices))
        self.assertIn("dsh:seed-evolving", choices)

    def test_unreachable_acme_keeps_the_account_models(self):
        with mock.patch("covey.backends.gateway.connection.NO_REDIRECT.open", side_effect=OSError):
            catalog = refresh_catalog(self.codex, self.app.harnesses["codex"].context)
        self.assertEqual(set(catalog), {"gpt-6-astra"})
        del os.environ["ACME_API_KEY"]
        self.assertEqual(set(self.codex.backend.refresh_model_catalog(self.app.harnesses["codex"].context)), {"gpt-6-astra"})

    def run_codex(self, model, session=None, effort="high"):
        from support.fake_backends import FakeRpc, RUN, finished
        ctx = dataclasses.replace(self.app.harnesses["codex"].context, model=model, effort=effort)
        rpc = FakeRpc([finished()])
        with codex_server(rpc) as server:
            result = self.codex.backend.run(RunRequest(ctx, "probe", session_id=session,
                                                       inputs=RunInputs(threading.Event(), run_id=RUN)))
        return result, dict(rpc.requests), server.call_args.args

    def test_acme_threads_bind_the_scoped_provider_on_start_and_resume(self):
        from support.fake_backends import SID
        self.refresh("codex")
        for session in (None, SID):
            result, requests, (argv, _cwd, env) = self.run_codex("acme/gpt-6-astra", session)
            self.assertFalse(result.is_error, result.text)
            params = requests["thread/resume" if session else "thread/start"]
            self.assertEqual((params["model"], params["modelProvider"]), ("gpt-6-astra", "acme"))
            config = params["config"]
            self.assertRegex(config["model_providers.acme.base_url"], r"^http://127\.0\.0\.1:\d+/v1$")
            # For 5xx and dropped streams only: the relay holds a busy request for the whole window.
            self.assertEqual((config["model_providers.acme.request_max_retries"],
                              config["model_providers.acme.stream_max_retries"]), (10, 10))
            self.assertIn("127.0.0.1", env["NO_PROXY"].split(","))
            self.assertEqual(config["model_providers.acme.env_key"], "ACME_API_KEY")
            self.assertEqual(config["model_providers.acme.wire_api"], "responses")
            self.assertEqual(config["model_providers.acme.http_headers.x-source"], "bridge-tests")
            self.assertIs(config["model_providers.acme.requires_openai_auth"], False)
            self.assertEqual(requests["turn/start"]["model"], "gpt-6-astra")
            self.assertIn('model_provider="acme"', argv)
            self.assertEqual(env["ACME_API_KEY"], KEY)
            self.assertNotIn(KEY, json.dumps([argv, params]))
            self.assertNotIn("CODEX_HOME", set(env) - set(os.environ))

    def test_ultra_reaches_codex_and_acme_is_checked_for_xhigh(self):
        self.refresh("codex")
        result, requests, _args = self.run_codex("acme/gpt-6-astra", effort="ultra")
        self.assertFalse(result.is_error, result.text)
        self.assertEqual(requests["thread/start"]["config"]["model_reasoning_effort"], "ultra")
        self.assertEqual(requests["turn/start"]["effort"], "ultra")
        with mock.patch.object(connection, "check_model") as check:
            codex_gateway.check(dataclasses.replace(self.app.harnesses["codex"].context, model="acme/gpt-6-astra",
                                                    effort="ultra"), acme_gateway(), official())
        self.assertEqual(check.call_args.args[3:], ("gpt-6-astra", "xhigh"))

    def test_account_models_never_receive_the_acme_provider_or_key(self):
        result, requests, (argv, _cwd, env) = self.run_codex("gpt-6-astra")
        self.assertFalse(result.is_error, result.text)
        self.assertNotIn("modelProvider", requests["thread/start"])
        self.assertFalse(any(k.startswith("model_provider") for k in requests["thread/start"]["config"]))
        self.assertNotIn("model_provider", " ".join(argv))
        self.assertEqual(env.get("ACME_API_KEY"), KEY)  # Inherited from the service, not added.
        del os.environ["ACME_API_KEY"]
        _result, _requests, (_argv, _cwd, env) = self.run_codex("gpt-6-astra")
        self.assertNotIn("ACME_API_KEY", env)

    def test_acme_failures_name_the_route_and_unlisted_models_never_start(self):
        from support.fake_backends import FakeRpc
        self.refresh("codex")
        ctx = dataclasses.replace(self.app.harnesses["codex"].context, model="acme/gpt-6-astra", effort="low")
        rpc = FakeRpc([{"method": "turn/completed", "params": {"threadId": "11111111-1111-4111-8111-111111111111",
                        "turn": {"id": "turn-current", "status": "failed", "error": {
                            "codexErrorInfo": {"responseStreamDisconnected": {"httpStatusCode": 429}}}}}}])
        with codex_server(rpc):
            result = self.codex.backend.run(RunRequest(ctx, "probe"))
        self.assertTrue(result.is_error)
        self.assertEqual(result.text, codex_gateway.failed_text(acme_gateway()))
        self.assertNotIn("分钟", result.text)  # The relay has no time limit; never claim it waited one out.
        self.assertEqual(result.failure_detail, codex_gateway.failed_text(acme_gateway()))  # The failure card says it too.
        for model, effort in (("acme/gone", "low"), ("acme/gpt-6.1-sol", "ultra")):
            with codex_server() as server:
                with self.assertRaises(ValueError):
                    self.codex.backend.run(RunRequest(dataclasses.replace(ctx, model=model, effort=effort), "probe"))
            server.assert_not_called()

    def test_codex_retries_show_on_the_card_and_the_relay_closes_with_the_turn(self):
        from support.fake_backends import FakeRpc, RUN, SID, finished
        self.refresh("codex")
        ctx = dataclasses.replace(self.app.harnesses["codex"].context, model="acme/gpt-6.1-sol", effort="low")

        def retry(message):
            return {"method": "error", "params": {"threadId": SID, "turnId": "turn-current", "willRetry": True,
                    "error": {"message": message}}}
        # The relay never hands a capacity 429 back, so Codex's own retries (an upstream 429 with Retry-After,
        # a 5xx) all show.
        limited = retry("stream disconnected - retrying sampling request (2/10)")
        limited["params"]["error"]["codexErrorInfo"] = {"responseStreamDisconnected": {"httpStatusCode": 429}}
        quota = retry("unexpected status 403 Forbidden: quota exceeded: Daily Limits exceeded. Resets at 2026-11-03T16:00:00Z.")
        quota["params"]["error"]["codexErrorInfo"] = {"httpConnectionFailed": {"httpStatusCode": 403}}
        rpc = FakeRpc([retry("Reconnecting... 1/10 (exceeded retry limit, last status: 429 Too Many Requests)"),
                       limited,  # The status only in codexErrorInfo (#2304 read 模型服务暂时不可用).
                       retry("Reconnecting... 3/10 (unexpected status 503 Service Unavailable)"),
                       quota, finished()])  # A spent quota says so, and when it comes back (T272 #2611).
        events = []
        with codex_server(rpc) as server:
            result = self.codex.backend.run(RunRequest(ctx, "probe", callbacks=RunCallbacks(
                event=lambda kind, data, control: events.append((kind, data))),
                inputs=RunInputs(threading.Event(), run_id=RUN)))
        self.assertFalse(result.is_error, result.text)
        notes = [data for kind, data in events if kind == "progress_note" and data["id"] == "backend-retry"]
        back = datetime.datetime.fromisoformat("2026-11-03T16:00:00+00:00").astimezone().strftime("%m-%d %H:%M")
        self.assertEqual([note["retry"] for note in notes], [{"attempt": 1, "reason": "上游限流（429）"},
                                                             {"attempt": 2, "reason": "上游限流（429）"},
                                                             {"attempt": 3, "reason": "模型服务暂时不可用"},
                                                             {"attempt": 4, "reason": "模型服务额度已用完，" + back + " 恢复，重试不会成功，可点停止"}])
        config = dict(rpc.requests)["thread/start"]["config"]
        self.assertEqual(config["model_providers.acme.stream_max_retries"], 10)
        base = config["model_providers.acme.base_url"]
        import urllib.request
        with self.assertRaises(OSError):  # The relay ended with the turn.
            urllib.request.urlopen(base[:-3] + "/v1/models", timeout=2)
        self.assertEqual(server.call_count, 1)

    def test_the_relays_waits_reach_the_card_and_its_log(self):
        from covey.backends.gateway.catalog import retry_notice
        from covey.backends.gateway.relay import BusyRelay, Busy
        events = []
        request = RunRequest(self.app.harnesses["codex"].context, "probe", callbacks=RunCallbacks(
            event=lambda kind, data, control: events.append((kind, data))))
        notify = retry_notice(request, acme_gateway())
        notify(Busy("retrying", 3, 65.4, 1, 3, 65.4))
        (kind, data), = events
        since = data["retry"].pop("since")
        self.assertAlmostEqual(since / 1000, time.time() - 65.4, delta=2)
        self.assertEqual((kind, data), ("progress_note", {
            "id": "acme-busy-1", "title": "Acme 繁忙记录", "text": "Acme 繁忙（429），正在第 3 次重试，已持续 1 分 05 秒",
            "retry": {"attempt": 3, "reason": "Acme 繁忙（429）"}}))  # No "next", and no limit to show.
        events.clear()
        notify(Busy("retrying", 6, 20, 1, 6, 20, "", 6))  # Rate-limit 429s are named as such.
        self.assertEqual((events[0][1]["text"], events[0][1]["retry"]["reason"]),
                         ("Acme 限速（429，请求太频繁），正在第 6 次重试，已持续 20 秒", "Acme 限速（429，请求太频繁）"))
        events.clear()
        notify(Busy("recovered", 40, 125, 1, 40, 125))
        notify(Busy("gave_up", 900, 3601, 3, 960, 3700))
        notify(Busy("retrying", 5, 20, 2, 9, 31, "child-thread"))
        # Without "retry" the status line's retry ends; each run's 执行过程 line keeps its count and, after the
        # first run, the turn's totals. A sub-agent's goes to its sub-task and never takes the status line.
        self.assertEqual(events, [
            ("progress_note", {"id": "acme-busy-1", "title": "Acme 繁忙记录", "urgent": False,
                               "text": "Acme 繁忙（429），重试 40 次后恢复，持续 2 分 05 秒"}),
            ("progress_note", {"id": "acme-busy-3", "title": "Acme 繁忙记录", "urgent": False,
                               "text": "Acme 繁忙（429），重试 900 次、持续 1 小时 0 分 仍未恢复，本轮结束；"
                                       "本轮共繁忙 3 次，重试 960 次，累计 1 小时 1 分"}),
            ("progress_note", {"id": "acme-busy-2", "title": "Acme 繁忙记录", "child_id": "child-thread",
                               "text": "Acme 繁忙（429），正在第 5 次重试，已持续 20 秒；本轮共繁忙 2 次，重试 9 次，累计 31 秒"})])
        state = self.root / "state"
        log = connection.relay_log(acme_gateway(), state)
        log("acme busy began code=-2004 message=业务申请资源不足")
        path = state / "acme" / "relay.log"
        self.assertRegex(path.read_text(encoding="utf-8"), r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d acme busy began code=-2004")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertIsNone(BusyRelay("http://127.0.0.1:1").window)  # Codex's relay retries until the task ends.
        self.assertIn(f"约 {connection.BUSY_WINDOW // 60} 分钟", connection.busy_text(acme_gateway()))  # dsh's own retries still stop.


if __name__ == "__main__":
    unittest.main()
