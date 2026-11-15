"""A redacted topic's Claude Code turn talks to the model only through the term relay, and fails
before it starts when it cannot (backends/claude/redaction.py)."""
from __future__ import annotations

import dataclasses
import gzip
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from support import exec_tempdir, make_context
from support.fake_backends import CLAUDE_JSONL, RUN, FakeBackend
from support.world import build_claude_app
from support.records import write_topic

from covey.backends.claude import redaction
from covey.backends.claude.bridge import REDACTION_NOTE, ClaudeBackend
from covey.backends.claude.argv import stream_argv
from covey.core.harness import RunCallbacks, RunInputs, RunRequest
from covey.core.jobs import BridgeReplyJob as ReplyJob
from covey.core.redaction import auto_stand_in, recent_turns, save_config
from covey.core.redactor import Redactor
from covey.core.serving import LoopbackServer

TERM = "Contoso"
STAND_IN = auto_stand_in(TERM)
TOKEN = "sk-ant-unit-secret"


class Upstream:
    """A stand-in model service: records each request; answers /v1/models, JSON messages that echo the
    stand-in, and SSE whose stand-in is split across writes."""

    def __init__(self):
        self.seen = []
        self.server = LoopbackServer(self.handle, None).__enter__()

    def handle(self, handler, body):
        self.seen.append((handler.command, handler.path, handler.headers, body or b""))
        if handler.command == "GET":
            return reply(handler, 200, "application/json", [json.dumps({"data": [{"id": "m", "note": STAND_IN}]}).encode()])
        if json.loads(body).get("stream"):
            events = [
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "see RX"}},
                {"type": "content_block_delta", "index": 0,
                 "delta": {"type": "text_delta", "text": STAND_IN[2:] + " now"}},
                {"type": "content_block_stop", "index": 0}]
            raw = b"".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode() for e in events)
            reply(handler, 200, "text/event-stream", [raw[i:i + 9] for i in range(0, len(raw), 9)])
        else:
            reply(handler, 200, "application/json", [json.dumps(
                {"type": "message", "content": [{"type": "text", "text": f"about {STAND_IN}"}]}).encode()])

    @property
    def url(self):
        return self.server.base_url

    def close(self):
        self.server.__exit__()


def reply(handler, status, kind, chunks):
    handler.send_response(status)
    handler.send_header("Content-Type", kind)
    handler.send_header("Content-Length", str(sum(map(len, chunks))))
    handler.end_headers()
    for chunk in chunks:
        handler.wfile.write(chunk)
        handler.wfile.flush()
        time.sleep(0.01)


def call(url, body=None, method="POST", headers=None):
    request = urllib.request.Request(url, data=body, method=method, headers={
        "Authorization": "Bearer " + TOKEN, "x-api-key": TOKEN, "Content-Type": "application/json", **(headers or {})})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def relay_log(ctx, needle):
    """relay.log once it has ``needle``: the line is written after the answer went out."""
    path = Path(ctx.state_dir) / "redaction" / "relay.log"
    for _ in range(100):
        text = path.read_text() if path.exists() else ""
        if needle in text:
            return text
        time.sleep(0.02)
    return text


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ctx = make_context(self.root, topic_id="t1", backend_cmd=["never-run-claude"],
                                options={"harness_id": "claude"})
        save_config(self.ctx.state_dir, {"terms": [{"term": TERM}]})
        self.upstream = Upstream()
        self.addCleanup(self.upstream.close)

    def topic(self, value=True, raw=None):
        if raw is None:
            write_topic(self.ctx.state_dir, {"redaction": value}, topic_id="t1")
            return
        # Text no topic writer makes (an unreadable topic.json), so written by hand.
        path = Path(self.ctx.state_dir) / "topics" / "t1" / "topic.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(raw)


class RelayTests(Base):
    def relay(self):
        relay = redaction.Relay(self.upstream.url, Redactor({"terms": [{"term": TERM, "stand_in": "", "anywhere": False}]}),
                                log=redaction.relay_log(self.ctx.state_dir))
        relay.__enter__()
        self.addCleanup(relay.__exit__)
        return relay

    def test_json_goes_out_redacted_with_its_headers_and_comes_back_restored(self):
        relay = self.relay()
        status, payload = call(relay.base_url + "/v1/messages?beta=true", json.dumps(
            {"model": "m", "messages": [{"role": "user", "content": f"fix {TERM}/a.py"}]}).encode())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)["content"][0]["text"], f"about {TERM}")
        method, path, headers, body = self.upstream.seen[-1]
        self.assertEqual(path, "/v1/messages?beta=true")
        self.assertNotIn(TERM.encode(), body)
        self.assertIn(STAND_IN.encode(), body)
        self.assertEqual((headers["Authorization"], headers["x-api-key"]), ("Bearer " + TOKEN, TOKEN))
        self.assertEqual(headers["Accept-Encoding"], "identity")
        self.assertEqual(relay.report(), {"terms": 1, "hits": 1, "requests": 1})
        log = relay_log(self.ctx, "POST /v1/messages status=200 replaced=1")
        self.assertIn("POST /v1/messages status=200 replaced=1", log)
        for secret in (TOKEN, TERM, STAND_IN, "beta=true", "Authorization"):
            self.assertNotIn(secret, log)

    def test_a_stream_comes_back_restored_across_split_chunks(self):
        relay = self.relay()
        status, payload = call(relay.base_url + "/v1/messages", json.dumps(
            {"stream": True, "messages": [{"role": "user", "content": TERM}]}).encode())
        self.assertEqual(status, 200)
        text = "".join(json.loads(line[6:])["delta"]["text"] for line in payload.decode().split("\n")
                       if line.startswith("data: ") and "text_delta" in line)
        self.assertEqual(text, f"see {TERM} now")

    def test_a_body_that_is_not_json_is_refused_and_get_passes(self):
        relay = self.relay()
        status, payload = call(relay.base_url + "/v1/messages", b"Contoso as plain text")
        self.assertEqual(status, 400)
        self.assertIn("不是 JSON", json.loads(payload)["error"]["message"])
        status, _ = call(relay.base_url + "/v1/messages", b'{"a": 1}', headers={"Content-Encoding": "gzip"})
        self.assertEqual(status, 400)
        status, _ = call(relay.base_url + "/v1/messages", b'{"a": 1}', headers={"Content-Encoding": "br"})
        self.assertEqual(status, 400)
        self.assertEqual(self.upstream.seen, [])  # None went out.
        status, payload = call(relay.base_url + "/v1/models", method="GET")
        # The model list's "data" is a structural key: it comes back as it came.
        self.assertEqual((status, json.loads(payload)["data"][0]["note"]), (200, STAND_IN))
        self.assertEqual(self.upstream.seen[-1][:2], ("GET", "/v1/models"))
        self.assertIn("refused=not_json", relay_log(self.ctx, "refused=not_json"))

    def test_a_compressed_body_goes_out_redacted_and_plain(self):
        relay = self.relay()
        body = gzip.compress(json.dumps({"messages": [{"role": "user", "content": TERM}]}).encode())
        status, _ = call(relay.base_url + "/v1/messages", body, headers={"Content-Encoding": "gzip"})
        self.assertEqual(status, 200)
        _, _, headers, sent = self.upstream.seen[-1]
        self.assertIsNone(headers.get("Content-Encoding"))
        self.assertEqual(json.loads(sent)["messages"][0]["content"], STAND_IN)

    def test_an_unreachable_upstream_is_a_502(self):
        relay = redaction.Relay("http://127.0.0.1:9", Redactor({"terms": []}))
        with relay:
            self.assertEqual(call(relay.base_url + "/v1/messages", b"{}")[0], 502)


class UpstreamTests(Base):
    def resolve(self, env=None, config=None):
        return redaction.resolve_upstream(self.ctx, {"HOME": str(self.root), **(env or {})},
                                          {"upstream": "", **(config or {})})

    def test_the_turns_own_gateway_then_config_then_wrapper_then_settings_then_anthropic(self):
        self.assertEqual(self.resolve({"ANTHROPIC_BASE_URL": "http://127.0.0.1:5/"}, {"upstream": "https://cfg"}),
                         "http://127.0.0.1:5")
        with self.assertRaisesRegex(redaction.Unavailable, "ANTHROPIC_BASE_URL"):
            self.resolve({"ANTHROPIC_BASE_URL": "not a url"})
        self.assertEqual(self.resolve(config={"upstream": "https://cfg"}), "https://cfg")
        self.assertEqual(self.resolve(), redaction.DEFAULT_UPSTREAM)  # never-run-claude reports nothing.
        settings = self.root / "cfg" / "settings.json"
        settings.parent.mkdir()
        settings.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://from-settings/"}}))
        (Path(self.ctx.state_dir) / "redaction" / "upstream.json").unlink()
        self.assertEqual(self.resolve({"CLAUDE_CONFIG_DIR": str(settings.parent)}), "https://from-settings")
        with exec_tempdir() as bin_dir:
            calls = Path(bin_dir) / "calls"
            fake = Path(bin_dir) / "claude"
            fake.write_text(f'#!/bin/sh\necho "$*" >> {calls}\n'
                            'echo \'{"ready": true, "endpoint": "http://127.0.0.1:13425"}\'\n')
            fake.chmod(0o755)
            self.ctx = dataclasses.replace(self.ctx, backend_cmd=[str(fake)])
            for _ in range(2):  # Asked once, then cached.
                self.assertEqual(self.resolve({"CLAUDE_CONFIG_DIR": str(settings.parent)}), "http://127.0.0.1:13425")
            self.assertEqual(calls.read_text(), "--gateway-status\n")
            cache = Path(self.ctx.state_dir) / "redaction" / "upstream.json"
            cache.write_text(json.dumps({**json.loads(cache.read_text()), "at": time.time() - redaction.STATUS_TTL - 1}))
            self.resolve()
            self.assertEqual(calls.read_text(), "--gateway-status\n" * 2)
            # The wrapper is there but its gateway is down: fail, and ask again next turn.
            cache.unlink()
            fake.write_text("#!/bin/sh\necho 'claude gateway: SSH tunnel failed' >&2\nexit 1\n")
            with self.assertRaisesRegex(redaction.Unavailable, "个人网关没有就绪"):
                self.resolve({"CLAUDE_CONFIG_DIR": str(settings.parent)})
            self.assertFalse(cache.exists())

    def test_the_hop_to_a_loopback_upstream_skips_the_proxy(self):
        self.assertTrue(redaction.is_loopback("http://127.0.0.1:13425"))
        self.assertTrue(redaction.is_loopback("http://[::1]:1"))
        self.assertFalse(redaction.is_loopback("https://api.anthropic.com"))
        with mock.patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9"}):
            relay = redaction.Relay(self.upstream.url, Redactor({"terms": []}))
            with relay:
                self.assertEqual(call(relay.base_url + "/v1/messages", b"{}")[0], 200)


class TurnTests(Base):
    def request(self, ctx=None, events=None):
        return RunRequest(ctx or self.ctx, "prompt", callbacks=RunCallbacks(event=lambda k, d, c: events.append((k, d)))
                          if events is not None else None, inputs=RunInputs(threading.Event(), run_id=RUN))

    def test_the_turn_points_claude_code_at_the_relay_and_records_its_counts(self):
        backend = ClaudeBackend()
        self.assertNotIn("--settings", stream_argv(backend, self.ctx))
        env = {"ANTHROPIC_BASE_URL": self.upstream.url, "NO_PROXY": "corp"}
        with redaction.turn(self.ctx, env, run_id=RUN) as (ctx, env2):
            self.assertIs(env2, env)
            argv = stream_argv(backend, ctx, "11111111-1111-4111-8111-111111111111")
            settings = json.loads(argv[argv.index("--settings") + 1])["env"]
            # It replaces Claude Code's NO_PROXY whole, so the loopback names stay in it.
            self.assertEqual(settings["NO_PROXY"], "corp,127.0.0.1,localhost,::1")
            self.assertEqual(settings["no_proxy"], settings["NO_PROXY"])
            self.assertTrue(settings["ANTHROPIC_BASE_URL"].startswith("http://127.0.0.1:"))
            # Fetching the gateway's models through the relay would file the list under the relay's port.
            self.assertEqual(settings["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"], "0")
            call(settings["ANTHROPIC_BASE_URL"] + "/v1/messages", json.dumps({"q": f"{TERM} {TERM}"}).encode())
            self.assertEqual(redaction.turn_event(ctx), {"terms": 1, "hits": 2, "requests": 1})
            # The next request carries the same history again: the turn counts what one request masked.
            call(settings["ANTHROPIC_BASE_URL"] + "/v1/messages", json.dumps({"q": f"{TERM} {TERM} ok"}).encode())
            self.assertEqual(redaction.turn_event(ctx), {"terms": 1, "hits": 2, "requests": 2})
        with self.assertRaises(OSError):  # Closed with the turn.
            urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                settings["ANTHROPIC_BASE_URL"] + "/v1/models", timeout=2)
        row, = recent_turns(self.ctx.state_dir)
        self.assertEqual({k: row[k] for k in ("topic_id", "run_id", "harness", "model", "terms", "requests", "hits")},
                         {"topic_id": "t1", "run_id": RUN, "harness": "claude", "model": self.ctx.model, "terms": 1,
                          "requests": 2, "hits": {TERM: 2}})
        self.assertIsNone(redaction.turn_event(self.ctx))

    def test_a_redacted_turn_runs_claude_through_the_relay_and_reports_before_the_trace_ends(self):
        self.topic()
        events = []
        ctx = dataclasses.replace(self.ctx, timeout=4, backend_cmd=[sys.executable, str(CLAUDE_JSONL), "success"])
        with mock.patch.dict(os.environ, {"ANTHROPIC_BASE_URL": self.upstream.url}):
            result = ClaudeBackend().run(self.request(ctx, events))
        self.assertFalse(result.is_error)
        kinds = [kind for kind, _ in events]
        self.assertEqual(kinds.index("redaction") + 1, kinds.index("trace_finished"))
        self.assertEqual(dict(events)["redaction"], {"terms": 1, "hits": 0, "requests": 0})
        self.assertEqual(len(recent_turns(self.ctx.state_dir)), 1)

    def test_a_turn_that_cannot_be_redacted_never_starts_claude_code(self):
        for name, prepare in (
                ("broken config", lambda: (Path(self.ctx.state_dir) / "redaction" / "config.json").write_text("{")),
                ("unreadable topic", lambda: self.topic(raw="{")),
                ("relay cannot start", lambda: mock.patch("covey.core.serving.LoopbackServer",
                                                                  side_effect=OSError("busy")).start()),
                ("upstream unknown", lambda: os.environ.__setitem__("ANTHROPIC_BASE_URL", "nonsense"))):
            with self.subTest(name), mock.patch.dict(os.environ), \
                    mock.patch("covey.backends.claude.runtime.run_stream") as run_stream:
                os.environ["CLAUDE_CONFIG_DIR"] = str(self.root / "no-claude-config")
                self.topic()
                save_config(self.ctx.state_dir, {"terms": [{"term": TERM}]})
                prepare()
                result = ClaudeBackend().run(self.request())
                self.assertTrue(result.is_error)
                self.assertEqual(result.outcome.error_code, "redaction_failed")
                self.assertIn("本轮未发送", result.failure_detail)
                self.assertNotIn(TERM, result.text)
                run_stream.assert_not_called()
                mock.patch.stopall()

    def test_an_unredacted_topic_runs_as_before_and_only_a_redacted_one_gets_the_note(self):
        backend = ClaudeBackend()
        with mock.patch("covey.backends.claude.runtime.run_stream", return_value="ran") as run_stream:
            self.assertEqual(backend.run(self.request()), "ran")
            self.topic(False)
            self.assertEqual(backend.run(self.request()), "ran")
        self.assertNotIn(redaction.SETTINGS_ENV, run_stream.call_args.args[1].context.options)
        self.assertNotIn(REDACTION_NOTE, backend.prompt_notes(self.ctx))
        self.topic()
        self.assertEqual(backend.prompt_notes(self.ctx)[-1], REDACTION_NOTE)
        self.assertTrue(backend.capabilities(self.ctx).redaction)


class GuardTests(Base):
    def run_turn(self, backend):
        app = build_claude_app(backend)
        ctx = dataclasses.replace(make_context(self.root), topic_id="t1")
        job = ReplyJob(job_id=1, user_text="x", chat_id="oc", message_id="om1", timeout=0, history_snapshot=[])
        return app.runs.generate_reply_for_worker(ctx, job, None, None)

    def test_a_harness_without_the_relay_never_runs_a_redacted_topic(self):
        backend = FakeBackend()
        self.topic()
        result = self.run_turn(backend)
        self.assertEqual(backend.prompts, [])
        self.assertEqual(result.outcome.error_code, "redaction_failed")
        self.assertIn("不支持脱敏", result.failure_detail)
        self.topic(raw="{")
        self.assertIn("无法确认敏感词脱敏", self.run_turn(backend).failure_detail)
        self.assertEqual(backend.prompts, [])
        self.topic(False)
        self.assertFalse(self.run_turn(backend).is_error)
        self.assertEqual(len(backend.prompts), 1)


if __name__ == "__main__":
    unittest.main()
