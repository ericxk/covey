"""The small model's questions through a model gateway where there is no Claude Code (app/judge_gateway.py, COVEY_JUDGE_GATEWAY)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from covey.app import judge_gateway, patrol_judge, project_sorter, track_judge
from covey.app.track_standby import Failed
from covey.core import gateways, http_upstream
from covey.core.track_route import LiveJob, Message, Quote

KEY = "sk-test-not-a-real-key"


class Upstream:
    """A gateway on loopback: what each request was, and what it answers (``reply(path, body) -> (status, payload)``)."""

    def __init__(self):
        self.requests, self.delay = [], 0.0
        self.reply = lambda path, body: (200, {})
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                upstream.requests.append((self.path, {k.lower(): v for k, v in self.headers.items()}, body))
                time.sleep(upstream.delay)
                status, payload = upstream.reply(self.path, body)
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # The asker gave up (its limit): nothing to answer.

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def responses(text):
    return {"id": "resp_1", "output": [{"type": "reasoning", "summary": []},
                                       {"type": "message", "content": [{"type": "output_text", "text": text}]}]}


def messages(text):
    return {"id": "msg_1", "content": [{"type": "text", "text": text}], "stop_reason": "end_turn"}


class GatewayCase(unittest.TestCase):
    def setUp(self):
        self.upstream = Upstream()
        self.addCleanup(self.upstream.close)
        gateways.configure({"judgegw": {"base_url": self.upstream.url, "key_env": "JUDGE_TEST_KEY", "prefix": "jg/",
                                        "harnesses": ["codex"], "headers": {"x-source": "covey"}},
                            "dshgw": {"base_url": self.upstream.url, "key_env": "JUDGE_TEST_KEY", "harnesses": ["dsh"]}})
        self.addCleanup(gateways.configure, None)
        self.addCleanup(gateways.forget)
        self.enterContext(mock.patch.dict(os.environ, {"JUDGE_TEST_KEY": KEY, "COVEY_JUDGE_GATEWAY": "judgegw:small-1"}))
        self.enterContext(mock.patch.object(judge_gateway, "OPENER", http_upstream.opener(direct=True)))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)


class AskTests(GatewayCase):
    def test_unset_is_none_and_a_bad_value_says_so(self):
        with mock.patch.dict(os.environ, {"COVEY_JUDGE_GATEWAY": ""}):
            self.assertIsNone(judge_gateway.configured())
        self.assertEqual(judge_gateway.configured()[1], "small-1")
        for value in ("nogw:small-1", "judgegw:", "judgegw"):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"COVEY_JUDGE_GATEWAY": value}):
                with self.assertRaises(Failed) as caught:
                    judge_gateway.ask("system", "{}", 5)
                self.assertEqual(caught.exception.reason, "no_gateway")

    def test_a_responses_question_and_its_envelope(self):
        self.upstream.reply = lambda path, body: (200, responses('{"route":"new","target":null,"confidence":0.9}'))
        raw = judge_gateway.ask("the system", '{"message":"hi"}', 5)
        self.assertEqual(track_judge.answer_text(raw, "claude"), '{"route":"new","target":null,"confidence":0.9}')
        [(path, headers, body)] = self.upstream.requests
        self.assertEqual(path, "/v1/responses")
        self.assertEqual(headers["authorization"], "Bearer " + KEY)
        self.assertEqual(headers["x-source"], "covey")
        self.assertEqual((body["model"], body["instructions"], body["input"], body["stream"]),
                         ("small-1", "the system", '{"message":"hi"}', False))

    def test_a_messages_question_by_the_gateway_or_its_catalog(self):
        self.upstream.reply = lambda path, body: (200, messages("答案"))
        with mock.patch.dict(os.environ, {"COVEY_JUDGE_GATEWAY": "dshgw:deepseek-x"}):
            raw = judge_gateway.ask("系统", "{}", 5)
        self.assertEqual(json.loads(raw)["result"], "答案")
        path, headers, body = self.upstream.requests[-1]
        self.assertEqual((path, headers["x-api-key"], headers["anthropic-version"]), ("/v1/messages", KEY, "2023-06-01"))
        self.assertEqual((body["system"], body["messages"]), ("系统", [{"role": "user", "content": "{}"}]))
        catalog = self.state / "native-models" / "judgegw.json"
        catalog.parent.mkdir()
        catalog.write_text(json.dumps({"updated_at": time.time(), "rows": [{"id": "small-1", "protocol": "anthropic"}]}))
        judge_gateway.ask("系统", "{}", 5, state_dir=self.state)
        self.assertEqual(self.upstream.requests[-1][0], "/v1/messages")

    def test_failures_are_reasons_never_the_body(self):
        self.upstream.reply = lambda path, body: (429, {"error": {"message": "echo of " + json.dumps(body)}})
        for setup, reason in ((lambda: None, "http_429"),
                              (lambda: setattr(self.upstream, "reply", lambda p, b: (200, b"not json")), "bad_output"),
                              (lambda: setattr(self.upstream, "reply", lambda p, b: (200, responses(""))), "bad_output")):
            setup()
            with self.subTest(reason=reason), self.assertRaises(Failed) as caught:
                judge_gateway.ask("system", '{"secret":"x"}', 5)
            self.assertEqual(caught.exception.reason, reason)
            self.assertNotIn("secret", caught.exception.detail)
        self.upstream.delay = 1.5
        self.upstream.reply = lambda path, body: (200, responses("late"))
        with self.assertRaises(Failed) as caught:
            judge_gateway.ask("system", "{}", 0.5)
        self.assertEqual(caught.exception.reason, "timeout")
        with mock.patch.dict(os.environ, {"JUDGE_TEST_KEY": ""}), \
                mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.state)}), self.assertRaises(Failed) as caught:
            judge_gateway.ask("system", "{}", 5)
        self.assertEqual(caught.exception.reason, "no_key")


class JudgesTests(GatewayCase):
    """The three judges with no Claude Code: through the gateway when it is set, no_backend as before when it is not."""

    def test_tidy_judges_through_the_gateway(self):
        answer = {"suggestions": []}
        self.upstream.reply = lambda path, body: (200, responses(json.dumps(answer)))
        item = project_sorter.Topic(topic="t1", label="T1", title="一件事", gist={}, owner=["做点事"], conclusions=[],
                                    workspace="w", project="", days=1, turns=1, last_job="", updated=0)
        verdict = project_sorter.judge(self.state, [], [item], backend=None, ctx=None, log=lambda line: None)
        self.assertEqual(verdict.why, "ok")
        self.assertEqual(self.upstream.requests[-1][2]["instructions"], project_sorter.JUDGE_SYSTEM)
        with mock.patch.dict(os.environ, {"COVEY_JUDGE_GATEWAY": ""}):
            verdict = project_sorter.judge(self.state, [], [item], backend=None, ctx=None, log=lambda line: None)
        self.assertEqual(verdict.why, "no_backend")  # As before.

    def test_the_patrol_decides_through_the_gateway(self):
        batch = [{"id": "c1", "kind": "ask", "title": "标题", "what": "事", "topics": [], "wait": 3600}]
        self.upstream.reply = lambda path, body: (200, responses(
            '{"decisions":[{"index":1,"verdict":"note","why":"等了一小时","action":"none"}]}'))
        decisions, reason = patrol_judge.decide(self.state, batch, backend=None, ctx=None, log=lambda line: None)
        self.assertEqual((reason, [d["verdict"] for d in decisions]), ("ok", ["note"]))
        with mock.patch.dict(os.environ, {"COVEY_JUDGE_GATEWAY": ""}):
            self.assertEqual(patrol_judge.decide(self.state, batch, backend=None, ctx=None, log=lambda line: None),
                             ([], "no_backend"))

    def test_the_track_judge_asks_the_gateway_and_says_so(self):
        self.upstream.reply = lambda path, body: (200, responses('{"route":"merge","target":1,"confidence":0.95}'))
        live = (LiveJob(track=2, job_id="7", phase="running", request="修测试", started_at=1.0),)
        message = Message(text="顺便把文档也改了", live=live, quote=Quote(), now=2.0)
        lines = []
        with mock.patch.dict(os.environ, {"COVEY_TRACK_JUDGE_MS": "5000"}):
            verdict = track_judge.judge(message, backend=None, ctx=None, log=lines.append)
        self.assertEqual((verdict.route, verdict.target), ("merge", 2))
        self.assertTrue(lines[-1].startswith("track judge route=merge target=2") and lines[-1].endswith("via=gateway"))
        with mock.patch.dict(os.environ, {"COVEY_JUDGE_GATEWAY": ""}):
            verdict = track_judge.judge(message, backend=None, ctx=None, log=lines.append)
        self.assertEqual(verdict.why, "no_backend")


if __name__ == "__main__":
    unittest.main()
