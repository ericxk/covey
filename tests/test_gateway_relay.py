"""The relay makes Codex retry Acme's capacity 429s and otherwise passes traffic through unchanged."""
import json
import threading
import time
import unittest
import urllib.error
import urllib.request

from covey.backends.gateway.relay import (BOTH, GAVE_UP, LIMITED, OPENING_BYTES, REASON, RECOVERED,
                                                  RETRYING, Busy, BusyRelay, busy_detail, limit_wait)
from covey.core.serving import LoopbackServer

SUB = {"thread-id": "child-thread-1", "x-openai-subagent": "collab_spawn"}  # A Codex sub-agent's request.

BUSY = json.dumps({"error": {"message": "业务申请资源不足，平台申请扩容", "code": "-2004"}}, ensure_ascii=False).encode()
SSE = {"Content-Type": "text/event-stream"}
CREATED = b'event: response.created\ndata: {"type":"response.created","response":{"id":"resp_1","error":null}}\n\n'
# What a Pi turn on gpt-6.1-sol got on 2026-11-04 00:49: a 200 stream whose first event past the opening is an error.
THROUGHPUT = CREATED + (b'data: {"error":{"code":"429","message":"Requests have exceeded the throughput limit on your '
                        b'Provisioned-Managed deployment. If you continue to exceed your limit, consider increasing the '
                        b'number of provisioned throughput units deployed."}}\n\n')
ANSWER = (CREATED + b'event: response.in_progress\ndata: {"type":"response.in_progress"}\n\n'
          b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"ok"}\n\n'
          b'event: response.completed\ndata: {"type":"response.completed"}\n\n')


def relay_for(*args, **kwargs):
    return BusyRelay(*args, name="acme", display="Acme", **kwargs)


class Upstream:
    """Answers each POST with the next scripted reply, then with `then`, after `latency` seconds; records
    what it received."""
    def __init__(self, replies, then=None, latency=0):
        self.replies, self.then, self.latency, self.seen = list(replies), then, latency, []
        # A POST's redirect, if followed, comes as a GET.
        self.server = LoopbackServer(self.handle, None).__enter__()

    def handle(self, handler, body):
        self.seen.append((handler.path, handler.headers, body or b""))
        time.sleep(self.latency)
        status, headers, payload = self.replies.pop(0) if self.replies else self.then
        handler.send_response(status)
        for key, value in headers.items():
            handler.send_header(key, value)
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)

    @property
    def url(self):
        return self.server.base_url

    def close(self):
        self.server.__exit__()


class Stay(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # The client sees a redirect the relay passed back.


def post(url, body=b'{"model":"m"}', timeout=10, headers=None):
    request = urllib.request.Request(url + "/v1/responses", data=body, method="POST", headers={
        "Authorization": "Bearer sk-unit", "x-source": "bridge-tests", "Content-Type": "application/json",
        "Connection": "keep-alive", "thread-id": "main-thread", **(headers or {})})
    try:
        with urllib.request.build_opener(Stay).open(request, timeout=timeout) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


class RelayTests(unittest.TestCase):
    def upstream(self, replies, then=None, latency=0):
        upstream = Upstream(replies, then, latency)
        self.addCleanup(upstream.close)
        return upstream

    def test_capacity_429s_are_held_and_sent_again_at_once_until_an_answer_comes(self):
        ok = (200, {"Content-Type": "text/event-stream"}, b"data: one\n\ndata: two\n\n")
        upstream = self.upstream([(429, {}, BUSY)] * 3 + [ok])
        notes, logs = [], []
        started = time.monotonic()
        with relay_for(upstream.url, notify=notes.append, log=logs.append) as relay:
            status, headers, body = post(relay.base_url)
        self.assertLess(time.monotonic() - started, 1.5)  # No backoff between the tries.
        self.assertEqual((status, body), (200, b"data: one\n\ndata: two\n\n"))  # Codex never saw the 429s.
        self.assertEqual(headers["Content-Type"], "text/event-stream")
        self.assertEqual(notes, [])  # A burst shorter than SHOW_AFTER is only counted; the card never hears of it.
        self.assertEqual(len(upstream.seen), 4)
        for path, sent, data in upstream.seen:  # The same request each time, the SK and source passed through.
            self.assertEqual((path, data), ("/v1/responses", b'{"model":"m"}'))
            self.assertEqual((sent["Authorization"], sent["x-source"]), ("Bearer sk-unit", "bridge-tests"))
            self.assertNotEqual(sent.get("Connection"), "keep-alive")  # Hop-by-hop stays on its own hop.
        self.assertTrue(logs[0].startswith("acme busy began code=-2004 message=业务申请资源不足"), logs)
        self.assertRegex(logs[1], r"^acme busy ended status=200 retries=3 limited=0 seconds=\d+$")
        self.assertNotIn("sk-unit", "".join(logs))

    def test_a_long_run_reaches_the_card_after_show_after_then_every_note_interval(self):
        upstream = self.upstream([], then=(429, {}, BUSY), latency=0.02)
        notes = []
        with relay_for(upstream.url, window=0.7, note_every=0.2, show_after=0.2, notify=notes.append) as relay:
            self.assertEqual(post(relay.base_url)[0], 429)
        self.assertGreater(len(upstream.seen), 10)  # Tried again as soon as each 429 came back.
        *retrying, last = notes
        self.assertIn(len(retrying), range(2, 5))  # From 0.2 s, about every 0.2 s of the 0.7 s window.
        self.assertEqual({(note.state, note.child, note.runs) for note in retrying}, {(RETRYING, "", 1)})
        self.assertGreaterEqual(retrying[0].seconds, 0.2)
        self.assertGreater(retrying[0].retries, 1)  # The tries before SHOW_AFTER count.
        self.assertEqual([note.retries for note in retrying], sorted({note.retries for note in retrying}))
        # Given up: every 429 but the last was sent again.
        self.assertEqual((last.state, last.retries, last.total_retries), (GAVE_UP, len(upstream.seen) - 1,
                                                                           len(upstream.seen) - 1))
        self.assertGreater(last.seconds, 0.7)

    def test_runs_are_kept_per_thread_and_the_turns_totals_add_up(self):
        ok = (200, {}, b"data: x\n\n")
        upstream = self.upstream([(429, {}, BUSY)] * 2 + [ok] + [(429, {}, BUSY)] * 3 + [ok], latency=0.02)
        notes, logs = [], []
        with relay_for(upstream.url, show_after=0, notify=notes.append, log=logs.append) as relay:
            post(relay.base_url)
            post(relay.base_url, headers=SUB)
        main, child = [n for n in notes if n.state == RECOVERED]
        self.assertEqual((main.child, main.retries, main.runs, main.total_retries), ("", 2, 1, 2))
        # The sub-agent's wait is its own, told by its thread-id and x-openai-subagent headers.
        self.assertEqual((child.child, child.retries, child.runs, child.total_retries), ("child-thread-1", 3, 1, 3))
        self.assertTrue(logs[-1].endswith(" agent=thread-1"), logs)  # A UUIDv7 thread's random end.
        relay = relay_for("http://127.0.0.1:1", show_after=0)
        self.addCleanup(relay.server.server_close)
        relay.busy_answer("", BUSY)
        relay.busy_answer("child", BUSY)
        self.assertEqual(relay.answered("child", 200).child, "child")
        self.assertEqual(set(relay.runs), {""})  # A sub-agent's answer leaves the main task's run going.
        ended = [relay.answered("", 200)]
        for retries in (2, 3):
            for _ in range(retries):
                relay.busy_answer("", BUSY)
            ended.append(relay.answered("", 200))
        self.assertEqual([(n.retries, n.runs, n.total_retries) for n in ended], [(1, 1, 1), (2, 2, 3), (3, 3, 6)])
        self.assertAlmostEqual(ended[-1].total_seconds, sum(n.seconds for n in ended), places=6)
        self.assertIsNone(relay.answered("", 200))  # No run, nothing to end.

    def test_a_request_is_held_for_the_whole_window_then_codex_ends_the_turn(self):
        upstream = self.upstream([], then=(429, {}, BUSY), latency=0.02)
        logs = []
        started = time.monotonic()
        with relay_for(upstream.url, window=0.5, log=logs.append) as relay:
            first = post(relay.base_url)
            held = time.monotonic() - started
            second = post(relay.base_url)
        # One request held through the window, never handed back to retry; then back without Retry-After, so
        # Codex ends the turn at once.
        self.assertGreater(held, 0.5)
        self.assertGreater(len(upstream.seen), 10)
        for answer in (first, second):
            self.assertEqual((answer[0], answer[2]), (429, BUSY))
            self.assertIsNone(answer[1].get("Retry-After"))
        # Giving up ends the run: a later request of the thread starts a run and a window of its own.
        self.assertEqual(sum(line.startswith("acme busy began") for line in logs), 2)
        self.assertRegex(logs[-1], r"^acme busy gave up retries=\d+ limited=0 seconds=\d+$")

    def test_closing_the_relay_stops_a_held_request_from_going_upstream_again(self):
        upstream = self.upstream([], then=(429, {}, BUSY), latency=0.2)
        relay = relay_for(upstream.url).__enter__()
        answers = []

        def call():
            try:
                answers.append(post(relay.base_url, timeout=5))
            except OSError as exc:  # The relay closes the held connection without an answer.
                answers.append(exc)
        caller = threading.Thread(target=call, daemon=True)
        caller.start()
        for _ in range(100):
            if upstream.seen:
                break
            threading.Event().wait(0.02)
        started = threading.Event()
        threading.Thread(target=lambda: (relay.__exit__(None, None, None), started.set()), daemon=True).start()
        self.assertTrue(started.wait(5))
        caller.join(5)
        self.assertIsInstance(answers[0], OSError)
        sent = len(upstream.seen)
        self.assertLessEqual(sent, 3)  # The try in flight at closing was the last.
        threading.Event().wait(0.5)
        self.assertEqual(len(upstream.seen), sent)

    def test_a_rate_limit_429_is_held_for_its_retry_after_and_other_errors_pass_through(self):
        # Acme over the key's request rate (#2304, 2026-11-02): Codex used to get these and gave up in 12 s.
        limit = {"Retry-After": "0.3", "x-ratelimit-limit": "120", "x-ratelimit-remaining": "0"}
        ok = (200, {}, b"data: x\n\n")
        upstream = self.upstream([(429, limit, b"{}"), (429, {}, BUSY), (429, limit, b"{}"), ok,
                                  (500, {}, b"boom"), (403, {}, b"quota exceeded")])
        notes, logs = [], []
        started = time.monotonic()
        with relay_for(upstream.url, show_after=0, notify=notes.append, log=logs.append) as relay:
            answer = post(relay.base_url)
            held = time.monotonic() - started
            failed, quota = post(relay.base_url), post(relay.base_url)
        self.assertEqual((answer[0], answer[2]), (200, b"data: x\n\n"))  # Codex never saw the three 429s.
        self.assertGreater(held, 0.6)  # Each rate-limit 429's Retry-After waited out, the capacity one not.
        self.assertLess(held, 3)
        self.assertEqual(notes[-1].state, RECOVERED)
        self.assertEqual((notes[-1].retries, notes[-1].limited, notes[-1].reason), (3, 2, BOTH.format("Acme")))
        self.assertEqual(logs[0], "acme busy began body={} retry_after=0.3 limit=120 remaining=0")
        self.assertRegex(logs[1], r"^acme busy ended status=200 retries=3 limited=2 seconds=\d+$")
        # Errors other than 429, a spent daily quota's 403 among them, go to Codex as they came.
        self.assertEqual([(failed[0], failed[2]), (quota[0], quota[2])], [(500, b"boom"), (403, b"quota exceeded")])
        self.assertEqual(len(upstream.seen), 6)

    def test_a_redirect_goes_back_as_it_came_and_the_sk_never_follows_it(self):
        elsewhere = self.upstream([], then=(200, {}, b"followed"))
        moved = elsewhere.url + "/v1/responses"
        upstream = self.upstream([(302, {"Location": moved}, b"")])
        with relay_for(upstream.url) as relay:
            status, headers, body = post(relay.base_url)
        self.assertEqual((status, headers["Location"], body), (302, moved, b""))
        self.assertEqual((len(upstream.seen), elsewhere.seen), (1, []))  # Nothing reached the other address.

    def test_retry_after_is_waited_up_to_a_minute_and_what_a_run_was_is_named(self):
        self.assertIsNone(limit_wait({}))  # A capacity 429: sent again at once.
        self.assertEqual([limit_wait({"Retry-After": v}) for v in ("1", "0.5", "-3", "7200", "soon")],
                         [1, 0.5, 0, 60, 1])
        self.assertIsNone(BusyRelay.__init__.__kwdefaults__["window"])  # No time limit unless one is given.
        self.assertEqual([Busy("retrying", 4, 9, 1, 4, 9, "", limited).reason for limited in (0, 4, 1)],
                         [x.format("模型网关") for x in (REASON, LIMITED, BOTH)])

    def test_busy_detail_masks_emails_and_survives_a_body_that_is_not_json(self):
        self.assertEqual(busy_detail(json.dumps({"error": {"code": 7, "message": "quota of a@example.com\nused"}}).encode()),
                         "code=7 message=quota of <email> used")
        self.assertEqual(busy_detail(b"<html>busy</html>"), "body=<html>busy</html>")

    def test_an_unreachable_upstream_is_a_502_and_the_relay_closes_with_its_task(self):
        upstream = self.upstream([])
        url = upstream.url
        upstream.close()
        with relay_for(url) as relay:
            self.assertEqual(post(relay.base_url)[0], 502)
            base = relay.base_url
        with self.assertRaises(OSError):
            urllib.request.urlopen(base + "/v1/models", timeout=2)

    def test_a_capacity_error_inside_a_200_stream_is_held_like_a_capacity_429(self):
        failed = CREATED + (b'event: response.failed\ndata: {"type":"response.failed","response":{"error":'
                            b'{"code":"rate_limit_exceeded","message":"Rate limit reached"}}}\n\n')
        upstream = self.upstream([(200, SSE, THROUGHPUT), (200, SSE, failed), (429, {}, BUSY), (200, SSE, ANSWER)])
        logs = []
        with relay_for(upstream.url, log=logs.append) as relay:
            status, headers, body = post(relay.base_url)
        self.assertEqual((status, body), (200, ANSWER))  # The client saw neither failed stream.
        self.assertEqual(len(upstream.seen), 4)
        self.assertTrue(logs[0].startswith("acme busy began in-stream code=429 message=Requests have exceeded the "
                                           "throughput limit"), logs)
        self.assertRegex(logs[1], r"^acme busy ended status=200 retries=3 limited=0 seconds=\d+$")

    def test_other_streams_pass_through_unchanged(self):
        crlf = ANSWER.replace(b"\n", b"\r\n")
        other = CREATED + b'data: {"type":"response.failed","response":{"error":{"code":"server_error","message":"boom"}}}\n\n'
        late = ANSWER[:-1] + THROUGHPUT[len(CREATED):]  # A capacity error once the output began goes to the client.
        big = CREATED + b": " + b"x" * OPENING_BYTES + b"\n\n" + ANSWER[len(CREATED):]
        json_reply = (200, {"Content-Type": "application/json"}, b'{"error":{"message":"throughput limit"}}')
        for reply in [(200, SSE, crlf), (200, SSE, other), (200, SSE, late), (200, SSE, big), json_reply,
                      (200, SSE, CREATED), (200, SSE, b"")]:
            upstream = self.upstream([reply])
            with relay_for(upstream.url) as relay:
                status, _, body = post(relay.base_url)
            self.assertEqual((status, body, len(upstream.seen)), (200, reply[2], 1), reply[2][-80:])

    def test_a_held_stream_failure_past_the_window_goes_to_the_client_as_it_came(self):
        upstream = self.upstream([], then=(200, SSE, THROUGHPUT))
        notes = []
        with relay_for(upstream.url, window=0.2, show_after=0, notify=notes.append) as relay:
            status, _, body = post(relay.base_url)
        self.assertEqual((status, body), (200, THROUGHPUT))
        self.assertGreater(len(upstream.seen), 1)
        self.assertEqual(notes[-1].state, GAVE_UP)

    def test_a_client_that_hangs_up_mid_stream_closes_the_upstream_quietly(self):
        from unittest import mock
        chunks = iter([b"data: one\n\n", b"data: two\n\n"])
        response = mock.MagicMock(status=200, headers={"Content-Type": "text/event-stream"})
        response.__enter__.return_value = response
        response.read1.side_effect = lambda size: next(chunks, b"")
        handler = mock.Mock(headers={}, path="/v1/responses", command="POST")
        handler.wfile.write.side_effect = [None, BrokenPipeError()]
        handler.close_connection = False
        relay = relay_for("http://127.0.0.1:1")
        self.addCleanup(relay.server.server_close)
        with mock.patch("covey.backends.gateway.relay.NO_REDIRECT.open", return_value=response):
            relay.forward(handler, b"{}")  # Raised BrokenPipeError before.
        response.__exit__.assert_called_once()
        self.assertTrue(handler.close_connection)


if __name__ == "__main__":
    unittest.main()
