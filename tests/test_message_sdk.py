"""Message sends, replies and reads through the in-process SDK (feishu/message_request.py, feishu/sdk.py).

With ``COVEY_CARD_TRANSPORT=sdk`` the request lark-cli would make is posted by the SDK
client instead: the same body (bodies below are lark-cli v1.0.98's own ``--dry-run`` output), the
same idempotency uuid on every retry, the same receipt and the same error classes; what only
lark-cli can do still goes to lark-cli. The SDK client is a fake; nothing reaches Feishu."""

import importlib
import importlib.util
import itertools
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import warnings
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import covey.feishu.transport as transport
from covey.core import timing
from covey.feishu.cardkit import cardkit_api, send_card_message
from covey.feishu.delivery import send_message_chunk
from covey.feishu.message_read import message_item, fetch_message_detail
from covey.feishu.message_request import from_argv

SDK = {"COVEY_CARD_TRANSPORT": "sdk"}
SENT = {"code": 0, "msg": "success", "data": {"message_id": "om_new", "chat_id": "oc_1"}}


def lark_oapi_missing():
    return importlib.util.find_spec("lark_oapi") is None


def load_lark_oapi(test):
    with warnings.catch_warnings():  # lark_oapi's own import warns (pkg_resources, utcfromtimestamp).
        warnings.simplefilter("ignore")
        importlib.import_module("lark_oapi")
    # lark_oapi.ws.client opens an event loop at import and never closes it.
    loop = getattr(sys.modules.get("lark_oapi.ws.client"), "loop", None)
    if loop is not None and not loop.is_running():
        test.addClassCleanup(loop.close)


class FakeClient:
    """``lark_oapi.Client.request``: each response is ``(status, body)`` or an exception to raise."""

    def __init__(self, *responses):
        self.responses, self.requests = list(responses), []

    def request(self, request):
        self.requests.append(request)
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        status, body, *headers = item
        content = body if isinstance(body, bytes) else json.dumps(body).encode()
        code = body.get("code", 0) if isinstance(body, dict) else 0
        return SimpleNamespace(code=code, msg="", raw=SimpleNamespace(status_code=status, content=content,
                                                                      headers=headers[0] if headers else {}))

    def bodies(self):
        return [request.body for request in self.requests]


@unittest.skipIf(lark_oapi_missing(), "optional Feishu SDK not installed")
class MessageSdkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load_lark_oapi(cls)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.addCleanup(transport.configure_receipts, transport.configure_receipts(self.state))
        self.addCleanup(transport.set_failure, None)
        self.logs = []
        for patcher in (mock.patch.dict(os.environ, SDK), mock.patch.object(transport.time, "sleep")):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.cli = mock.Mock(side_effect=AssertionError("lark-cli was spawned"))
        for target in ("covey.feishu.transport.subprocess.run", "covey.feishu.delivery.subprocess.run",
                       "covey.feishu.message_read.subprocess.run"):
            patcher = mock.patch(target, self.cli)
            patcher.start()
            self.addCleanup(patcher.stop)

    def client(self, *responses):
        fake = FakeClient(*responses)
        patcher = mock.patch("covey.feishu.sdk.client", return_value=fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    def send(self, text, key="key-1", *, fmt="markdown", reply_to=""):
        return send_message_chunk(profile="named", chat_id="oc_1", text=text, idempotency_key=key,
                                  message_format=fmt, log=self.logs.append, fallback_key_prefix="lb",
                                  reply_to_message_id=reply_to)

    def receipt(self):
        return json.loads(next((self.state / "delivery-receipts").glob("*.json")).read_text())

    def test_each_message_kind_posts_the_body_lark_cli_posts(self):
        fake = self.client((200, SENT))
        self.assertTrue(self.send("# 标题\n## 小节\n说明\n| a | b |\n|---|---|\n| 1 | 2 |\n```\n# 代码\n```\n\n\n\n"
                                  "![p](./p.png) ![k](img_k) <b>&"))
        self.assertTrue(self.send('a<b>&c "q" \u2028 中\n', "key-2", fmt="text", reply_to="om_1"))
        self.assertEqual(send_card_message("named", "oc_1", "7abc", "key-3", reply_to_message_id="om_1",
                                           log=self.logs.append), ("om_new", False))
        self.assertEqual(send_card_message("named", "oc_1", "7abc", "key-4", log=self.logs.append), ("om_new", False))
        self.assertEqual([(r.http_method.name, r.uri, r.queries) for r in fake.requests], [
            ("POST", "/open-apis/im/v1/messages", [("receive_id_type", "chat_id")]),
            ("POST", "/open-apis/im/v1/messages/om_1/reply", []),
            ("POST", "/open-apis/im/v1/messages/om_1/reply", []),
            ("POST", "/open-apis/im/v1/messages", [("receive_id_type", "chat_id")])])
        card = '{"type": "card", "data": {"card_id": "7abc"}}'
        self.assertEqual(fake.bodies(), [
            {"content": '{"zh_cn":{"content":[[{"tag":"md","text":"#### 标题\\n\\n##### 小节\\n说明\\n\\n| a | b |\\n'
                        '|---|---|\\n| 1 | 2 |\\n\\n```\\n# 代码\\n```\\n\\n ![k](img_k) \\u003cb\\u003e\\u0026"}]]}}',
             "msg_type": "post", "receive_id": "oc_1", "uuid": "key-1"},
            {"content": '{"text":"a\\u003cb\\u003e\\u0026c \\"q\\" \\u2028 中\\n"}', "msg_type": "text",
             "reply_in_thread": True, "uuid": "key-2"},
            {"content": card, "msg_type": "interactive", "reply_in_thread": True, "uuid": "key-3"},
            {"content": card, "msg_type": "interactive", "receive_id": "oc_1", "uuid": "key-4"}])
        self.cli.assert_not_called()

    def test_retries_reuse_the_uuid_and_the_receipt_answers_a_resend(self):
        fake = self.client(TimeoutError("read timed out"), (502, b"<html>bad gateway</html>"), (200, SENT))
        self.assertTrue(self.send("hello"))
        self.assertEqual([body["uuid"] for body in fake.bodies()], ["key-1"] * 3)
        saved = self.receipt()
        self.assertTrue(saved["delivered"])
        self.assertEqual(saved["response"], {"ok": True, "data": {"message_id": "om_new"}})
        self.assertNotIn("hello", json.dumps(saved))
        self.assertTrue(self.send("hello"))  # Delivered: the receipt answers, nothing is posted again.
        self.assertEqual(len(fake.requests), 3)

    def test_failures_keep_lark_clis_error_classes(self):
        cases = [  # (response, category, retryable, ambiguous)
            ((400, {"code": 230002, "msg": "Bot/User can NOT be out of the chat."}), "api_rejected", False, False),
            ((500, {"code": 1000, "msg": "internal"}), "api_rejected", False, False),  # The code, not the status.
            ((404, b"404 page not found"), "api_rejected", False, False),
            ((502, b"<html>bad gateway</html>"), "network", True, True),
            ((429, {"code": 99991400, "msg": "request trigger frequency limit"}), "rate_limited", True, False),
            ((400, {"code": 99991663, "msg": "Invalid access token"}), "authentication", False, False),
            ((403, {"code": 230027, "msg": "Lack of necessary permissions"}), "authentication", False, False),
            ((200, b"not json"), "unconfirmed", True, True),
            (ConnectionError("reset"), "network", True, True),
        ]
        for index, (response, category, retryable, ambiguous) in enumerate(cases):
            with self.subTest(category=category, response=str(response)[:40]):
                fake = self.client(response)
                self.assertFalse(self.send("hi", f"key-{index}", fmt="text"))
                failure = transport.last_failure()
                self.assertEqual((failure.category, failure.retryable, failure.ambiguous),
                                 (category, retryable, ambiguous))
                self.assertEqual(len(fake.requests), 3 if retryable else 1)
                self.assertEqual({body["uuid"] for body in fake.bodies()}, {f"key-{index}"})
        self.cli.assert_not_called()

    def test_an_invalid_card_is_reported_to_the_caller(self):
        self.client((400, {"code": 230001, "msg": "invalid param", "error": {"message": "card: cardid is invalid"}}))
        self.assertEqual(send_card_message("named", "oc_1", "gone", "key-1", log=self.logs.append), ("", True))

    def test_an_uncertain_send_stays_uncertain(self):
        fake = self.client(ConnectionError("reset"))
        self.assertFalse(self.send("hi"))
        path = next((self.state / "delivery-receipts").glob("*.json"))
        saved = json.loads(path.read_text())
        self.assertIn("uncertain_since", saved)
        saved["uncertain_since"] -= 3600  # Past Feishu's one-hour uuid window: never posted again.
        path.write_text(json.dumps(saved))
        self.assertFalse(self.send("hi"))
        self.assertEqual(transport.last_failure().category, "reconciliation_required")
        self.assertEqual(len(fake.requests), 3)

    def test_lark_cli_still_sends_what_only_it_can_and_when_the_sdk_is_off(self):
        self.cli.side_effect = None
        self.cli.return_value = subprocess.CompletedProcess([], 0, '{"ok":true,"data":{"message_id":"om_cli"}}', "")
        fake = self.client((200, SENT))
        self.assertTrue(self.send("![图](https://example.com/a.png)", "key-image"))  # Its image upload.
        with mock.patch.dict(os.environ, {"COVEY_CARD_TRANSPORT": "cli"}):
            self.assertTrue(self.send("plain", "key-off"))
            self.assertTrue(self.send("@notes.txt", "key-at-cli"))
        self.assertEqual(fake.requests, [])
        self.assertEqual([call.args[0][4] for call in self.cli.call_args_list], ["+messages-send"] * 3)
        # lark-cli would read "@notes.txt" as a file and send its contents: it gets the text, kept literal.
        self.assertEqual(self.cli.call_args_list[2].args[0][-6:-4], ["--markdown", "\u200b@notes.txt"])  # privacy: allow 测试里的文件名 notes.txt 不是邮箱

    def test_recall_and_reactions_go_through_the_sdk_as_lark_cli_sends_them(self):
        from covey.feishu.messenger import Messenger
        fake = self.client((200, {"code": 0, "msg": "success", "data": {"reaction_id": "r_1"}}))
        messenger = Messenger(self.logs.append)
        self.assertTrue(messenger.recall("named", "om_old"))
        self.assertEqual(messenger.react("named", "om_1", "OnIt"), "r_1")
        self.assertTrue(messenger.unreact("named", "om_1", "r_1"))
        self.assertEqual([(r.http_method.name, r.uri) for r in fake.requests], [
            ("DELETE", "/open-apis/im/v1/messages/om_old"), ("POST", "/open-apis/im/v1/messages/om_1/reactions"),
            ("DELETE", "/open-apis/im/v1/messages/om_1/reactions/r_1")])
        self.assertEqual(fake.bodies()[1], {"reaction_type": {"emoji_type": "OnIt"}})
        self.cli.assert_not_called()
        # Feishu's refusal (past the recall window) is a failure, as lark-cli's.
        self.client((400, {"code": 230020, "msg": "recall window"}))
        self.assertFalse(messenger.recall("named", "om_old"))
        # An id that does not look like one, or the SDK off: lark-cli, as before.
        self.cli.side_effect = None
        self.cli.return_value = subprocess.CompletedProcess([], 0, '{"ok":true,"data":{}}', "")
        self.assertTrue(messenger.recall("named", "not-a-message"))
        with mock.patch.dict(os.environ, {"COVEY_CARD_TRANSPORT": "cli"}):
            self.assertTrue(messenger.unreact("named", "om_1", "r_1"))
        argvs = [call.args[0] for call in self.cli.call_args_list]
        self.assertEqual([argv[argv.index("im"):argv.index("im") + 3] for argv in argvs],
                         [["im", "messages", "delete"], ["im", "reactions", "delete"]])

    def test_text_starting_with_at_or_a_lone_dash_is_sent_as_text(self):
        fake = self.client((200, SENT), (200, SENT))
        for text in ("@张三 看下这个", "-"):
            self.assertTrue(self.send(text, "key-" + str(len(text))))
        self.cli.assert_not_called()  # Both through the SDK, as text.
        sent = [json.loads(body["content"]) for body in fake.bodies()]
        texts = [part["text"] for content in sent for line in content["zh_cn"]["content"] for part in line]
        self.assertEqual(texts, ["\u200b@张三 看下这个", "\u200b-"])

    def test_a_client_that_cannot_be_built_falls_back_before_any_request(self):
        self.cli.side_effect = None
        self.cli.return_value = subprocess.CompletedProcess([], 0, '{"ok":true,"data":{"message_id":"om_cli"}}', "")
        with mock.patch("covey.feishu.sdk.client", side_effect=ValueError("SDK credential unreadable")):
            self.assertTrue(self.send("hello"))
        self.cli.assert_called_once()
        self.assertIn("Message SDK unavailable locally; using CLI", self.logs)
        self.assertNotIn("uncertain_since", self.receipt())

    def test_timing_names_the_sdk(self):
        lines = []
        self.addCleanup(timing.configure, timing.configure(lines.append))
        self.addCleanup(timing.flush)
        served = {"Server-Timing": "inner; dur=62, cdn-cache;desc=MISS,edge;dur=0,origin;dur=77"}
        self.client((200, SENT, served), (200, SENT), (400, {"code": 230110, "msg": "message not found"}, served))
        with timing.flow("t"):
            self.send("hello", reply_to="om_1")
            send_card_message("named", "oc_1", "7abc", "key-2", log=self.logs.append)
            fetch_message_detail("om_1", "named")
        calls = [dict(part.split("=", 1) for part in line.split(" ")[2:]) for line in lines
                 if line.startswith("feishu call ")]
        self.assertEqual([(call["op"], call["via"]) for call in calls],
                         [("msg_reply", "sdk"), ("msg_send", "sdk"), ("msg_get", "sdk")])
        # Feishu's own milliseconds, from its Server-Timing header: a refused request has them too.
        self.assertEqual([(call.get("inner_ms"), call.get("origin_ms")) for call in calls],
                         [("62", "77"), (None, None), ("62", "77")])
        self.assertEqual(calls[2]["result"], "rc:1")

    def test_a_message_read_returns_what_the_code_parses(self):
        item = {"message_id": "om_1", "root_id": "om_root", "thread_id": "omt_1", "msg_type": "text",
                "body": {"content": '{"text":"x"}'}}
        fake = self.client((200, {"code": 0, "msg": "success", "data": {"items": [item]}}))
        detail = fetch_message_detail("om_1", "named")
        self.assertEqual(message_item(detail, "om_1"), item)
        request = fake.requests[0]
        self.assertEqual((request.http_method.name, request.uri, request.body, request.queries),
                         ("GET", "/open-apis/im/v1/messages/om_1", None,
                          [("user_id_type", "open_id"), ("card_msg_content_type", "user_card_content")]))
        self.client((400, {"code": 230110, "msg": "message not found"}))
        failed = fetch_message_detail("om_2", "named")
        self.assertIs(failed["ok"], False)
        self.assertIn("230110", failed["error"])

    def test_the_real_sdk_client_puts_that_request_on_the_wire(self):
        from covey.feishu.sdk import _Client
        wire = []

        def http(method, url, headers=None, params=None, data=None, timeout=None, **kwargs):
            wire.append((method, url, dict(headers or {}), list(params or []), data, kwargs.get("proxies")))
            body = ({"code": 0, "msg": "ok", "tenant_access_token": "t-test", "expire": 7200}
                    if url.endswith("/tenant_access_token/internal") else SENT)
            return SimpleNamespace(status_code=200, headers={"Content-Type": "application/json"},
                                   content=json.dumps(body).encode())
        with mock.patch.dict(os.environ, {"COVEY_FEISHU_DIRECT": "1"}):
            sdk = _Client("cli_test", "secret", "https://open.feishu.cn")
        # Every request, the token's (requests.request) and the pooled session's alike, ends in Session.request.
        with mock.patch("covey.feishu.sdk.client", return_value=sdk), \
             mock.patch("requests.Session.request", side_effect=http):
            self.assertTrue(self.send("a <b>", fmt="text"))
            fetch_message_detail("om_1", "named")
        (method, url, headers, params, data, proxies), (read, read_url, _, read_params, read_data, _) = wire[-2:]
        self.assertEqual(proxies, {"http": None, "https": None, "all": None})  # Direct to Feishu (S48).
        self.assertEqual((method, url, params), ("POST", "https://open.feishu.cn/open-apis/im/v1/messages",
                                                 [("receive_id_type", "chat_id")]))
        self.assertEqual(json.loads(data), {"content": '{"text":"a \\u003cb\\u003e"}', "msg_type": "text",
                                            "receive_id": "oc_1", "uuid": "key-1"})
        self.assertEqual(headers.get("Authorization"), "Bearer t-test")
        self.assertEqual((read, read_url, read_params, read_data),
                         ("GET", "https://open.feishu.cn/open-apis/im/v1/messages/om_1",
                          [("user_id_type", "open_id"), ("card_msg_content_type", "user_card_content")], None))
        self.cli.assert_not_called()

    def test_a_file_message_reply_goes_through_the_sdk_with_its_receipt(self):
        fake = self.client((200, SENT))
        body = {"msg_type": "file", "content": '{"file_key": "file_1"}', "reply_in_thread": True, "uuid": "artifact-1"}
        self.assertIsNotNone(cardkit_api("named", "POST", "/open-apis/im/v1/messages/om_1/reply", body,
                                         log=self.logs.append))
        self.assertEqual((fake.requests[0].uri, fake.bodies()), ("/open-apis/im/v1/messages/om_1/reply", [body]))
        self.assertTrue(self.receipt()["delivered"])


@unittest.skipIf(lark_oapi_missing(), "optional Feishu SDK not installed")
class PooledSessionTests(unittest.TestCase):
    """S48: the SDK's requests share one pooled session, direct to Feishu, and ``warm`` readies the first one.
    requests' ``Session.request`` is a fake, the token's request too: nothing reaches Feishu."""
    APPS = itertools.count()

    @classmethod
    def setUpClass(cls):
        load_lark_oapi(cls)

    def client(self, **env):
        from covey.feishu.sdk import _Client
        with mock.patch.dict(os.environ, {"COVEY_FEISHU_DIRECT": "1", **env}):
            # An app id of its own: lark_oapi caches each app's token for the process.
            return _Client(f"cli_pool{next(self.APPS)}", "secret", "https://open.feishu.cn")

    def wire(self, answer=lambda method, url, kwargs: None):
        """Each request's (session, method, path, proxies, Authorization); ``answer`` may raise for one."""
        import requests
        calls = []

        def http(session, method, url, **kwargs):
            calls.append((session, method, url.removeprefix("https://open.feishu.cn"), kwargs.get("proxies"),
                          (kwargs.get("headers") or {}).get("Authorization", "")))
            failure = answer(method, url, kwargs)
            if failure is not None:
                raise failure
            body = ({"code": 0, "msg": "ok", "tenant_access_token": "t-pool", "expire": 7200}
                    if url.endswith("/tenant_access_token/internal") else {"code": 0, "data": {}})
            return SimpleNamespace(status_code=200, headers={"Content-Type": "application/json"},
                                   content=json.dumps(body).encode())
        patcher = mock.patch.object(requests.Session, "request", autospec=True, side_effect=http)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def put(self, client):
        from covey.feishu import sdk
        with mock.patch.object(sdk, "client", return_value=client):
            return sdk.runner("named", "PUT", "/open-apis/cardkit/v1/cards/c1", {"sequence": 1})(["lark-cli"])

    @staticmethod
    def api(calls):
        return [call for call in calls if call[2].startswith("/open-apis/cardkit/")]

    def test_writes_share_one_direct_session_until_a_pause_replaces_it(self):
        import requests
        from covey.feishu import sdk
        calls, client = self.wire(), self.client()
        self.assertEqual([self.put(client).returncode for _ in range(2)], [0, 0])
        first, second = self.api(calls)
        self.assertIs(first[0], second[0])  # One session: its connection is reused.
        self.assertEqual([first[3], second[3]], [sdk.DIRECT, sdk.DIRECT])
        self.assertEqual([c[2] for c in calls if c[2].endswith("/internal")],
                         ["/open-apis/auth/v3/tenant_access_token/internal"])  # One token for both.
        later = time.monotonic() + sdk.IDLE_SECONDS + 1
        with mock.patch.object(sdk.time, "monotonic", return_value=later), \
                mock.patch.object(requests.Session, "close", autospec=True) as close:
            self.put(client)
        self.assertIsNot(self.api(calls)[-1][0], first[0])  # Idle too long: none of its connections is used.
        close.assert_called_once_with(first[0])
        self.assertEqual(self.client(COVEY_FEISHU_DIRECT="0").direct, False)

    def test_a_direct_connection_that_cannot_open_goes_through_the_proxy_from_then_on(self):
        import requests
        from urllib3.exceptions import MaxRetryError, NewConnectionError
        from covey.feishu import sdk
        refused = requests.exceptions.ConnectionError(MaxRetryError(None, "/", NewConnectionError(None, "refused")))
        calls = self.wire(lambda method, url, kwargs: refused if kwargs.get("proxies") == sdk.DIRECT else None)
        client = self.client()
        self.assertEqual([self.put(client).returncode for _ in range(2)], [0, 0])
        # The token's request was the first: tried direct, then the environment's proxy, as is all after it.
        self.assertEqual([(c[2].rsplit("/", 1)[-1], c[3]) for c in calls],
                         [("internal", sdk.DIRECT), ("internal", None), ("c1", None), ("c1", None)])
        self.assertFalse(client.direct)

    def test_a_failure_once_connected_is_neither_sent_again_nor_rerouted(self):
        import requests
        from urllib3.exceptions import ProtocolError
        aborted = requests.exceptions.ConnectionError(ProtocolError("Connection aborted.", ConnectionResetError()))
        calls = self.wire(lambda method, url, kwargs: aborted if "/cardkit/" in url else None)
        client = self.client()
        done = self.put(client)
        self.assertEqual((done.returncode, json.loads(done.stderr)["error"]["type"]), (1, "network"))  # May have landed.
        self.assertEqual(len(self.api(calls)), 1)
        self.assertTrue(client.direct)

    def test_warm_fetches_the_token_on_the_connection_a_write_meanwhile_waits_for(self):
        from covey.feishu import sdk
        fetching, release = threading.Event(), threading.Event()

        def answer(method, url, kwargs):
            if url.endswith("/tenant_access_token/internal"):
                fetching.set()
                release.wait(5)
        calls, client = self.wire(answer), self.client()
        warmed, written = [], []
        warm = threading.Thread(target=lambda: warmed.append(client.warm()))
        warm.start()
        self.assertTrue(fetching.wait(5))
        write = threading.Thread(target=lambda: written.append(self.put(client).returncode))
        write.start()
        time.sleep(0.1)  # The write waits for the warmup's token instead of fetching its own.
        release.set()
        for thread in (warm, write):
            thread.join(5)
        self.assertEqual((warmed, written), ([True], [0]))
        [token] = [c for c in calls if c[2].endswith("/tenant_access_token/internal")]
        self.assertEqual((token[1], token[3], token[4]), ("POST", sdk.DIRECT, ""))
        [put] = self.api(calls)
        self.assertEqual(put[4], "Bearer t-pool")
        self.assertIs(put[0], token[0])  # On the session, and connection, the token's request opened.

    def test_the_token_is_fetched_again_ten_minutes_before_it_expires(self):
        from covey.feishu import sdk
        calls, client = self.wire(), self.client()
        self.put(client)
        self.assertAlmostEqual(client.token_until - time.time(), 7200 - 600, delta=5)  # Its expire, minus 10 minutes.
        self.put(client)
        with mock.patch.object(sdk.time, "time", return_value=client.token_until + 1):
            self.put(client)
        self.assertEqual(len([c for c in calls if c[2].endswith("/internal")]), 2)

    def test_a_refused_token_is_an_authentication_failure_that_names_nothing(self):
        import requests

        def refusing(session, method, url, **kwargs):
            body = {"code": 10014, "msg": "app secret invalid"} if url.endswith("/internal") else {"code": 0}
            return SimpleNamespace(status_code=200, headers={"Content-Type": "application/json"},
                                   content=json.dumps(body).encode())
        with mock.patch.object(requests.Session, "request", autospec=True, side_effect=refusing) as http:
            done = self.put(self.client())
        self.assertEqual(json.loads(done.stderr)["error"]["type"], "authentication")
        self.assertNotIn("secret", done.stderr)
        self.assertEqual(http.call_count, 1)  # No write without a token.


class LarkCliDirectTests(unittest.TestCase):
    """S48: lark-cli reaches Feishu's open platform without the proxy, through the no_proxy its Bridge leaves
    to every child. The connection is a fake; nothing reaches Feishu."""

    def run_it(self, environ, *, switch="1", refuse=False, brand="feishu"):
        from covey.feishu import sdk
        logs, tried = [], []

        def connect(address, timeout):
            tried.append(address)
            if refuse:
                raise ConnectionRefusedError("refused")
            return mock.Mock()
        config = json.dumps({"apps": [{"name": "named", "appId": "cli_x", "brand": brand}]}).encode()
        with mock.patch.dict(os.environ, {"COVEY_FEISHU_DIRECT": switch}), \
                mock.patch.object(sdk, "_private_read", return_value=config):
            added = sdk.lark_cli_direct("named", logs.append, environ=environ, connect=connect)
        return added, tried, logs

    def test_the_host_joins_both_spellings_once(self):
        environ = {"no_proxy": "localhost,.example.org", "NO_PROXY": "localhost,.example.org", "https_proxy": "http://p:1"}
        added, tried, logs = self.run_it(environ)
        self.assertEqual((added, tried), (True, [("open.feishu.cn", 443)]))
        self.assertEqual((environ["no_proxy"], environ["NO_PROXY"]),
                         ("localhost,.example.org,open.feishu.cn", "localhost,.example.org,open.feishu.cn"))
        self.assertEqual(environ["https_proxy"], "http://p:1")  # The rest still goes through it.
        self.run_it(environ)
        self.assertEqual(environ["no_proxy"], "localhost,.example.org,open.feishu.cn")  # Not twice.
        self.assertEqual(logs, ["Feishu direct: lark-cli reaches open.feishu.cn without the proxy"])
        only_upper = {"NO_PROXY": ""}
        self.run_it(only_upper, brand="lark")
        self.assertEqual(only_upper, {"NO_PROXY": "open.larksuite.com"})
        none = {}
        self.run_it(none)
        self.assertEqual(none, {"no_proxy": "open.feishu.cn", "NO_PROXY": "open.feishu.cn"})

    def test_no_direct_connection_or_the_switch_off_leaves_the_environment(self):
        environ = {"no_proxy": "localhost"}
        added, tried, logs = self.run_it(environ, refuse=True)
        self.assertEqual((added, environ), (False, {"no_proxy": "localhost"}))
        self.assertEqual(logs, ["Feishu direct unavailable (ConnectionRefusedError); lark-cli keeps the proxy"])
        added, tried, logs = self.run_it(environ, switch="off")
        self.assertEqual((added, tried, logs, environ), (False, [], [], {"no_proxy": "localhost"}))


@unittest.skipIf(lark_oapi_missing(), "optional Feishu SDK not installed")
class CoreOnlyImportTests(unittest.TestCase):
    def test_the_client_loads_no_api_module_and_the_package_still_completes(self):
        # lark_oapi/__init__.py imports every API module: 2.4 s of a task runner's first card write (S33).
        # A fresh interpreter: this one may have imported the whole package already.
        script = ("import sys\n"
                  "from covey.feishu.sdk import _Client\n"
                  "_Client('cli_test', 'secret', 'https://open.feishu.cn')\n"
                  "print(any(name.startswith('lark_oapi.api') for name in sys.modules))\n"
                  "import lark_oapi\n"
                  "print(lark_oapi.Client.__name__, lark_oapi.core is sys.modules['lark_oapi.core'])\n")
        env = {**os.environ, "PYTHONPATH": str(Path(transport.__file__).resolve().parents[2])}
        out = subprocess.run([sys.executable, "-B", "-W", "ignore", "-c", script], env=env, capture_output=True,
                             text=True, timeout=60, check=True).stdout.split("\n")
        self.assertEqual(out[:2], ["False", "Client True"])

    def test_a_request_loads_neither_lark_oapis_http_module_nor_httpx(self):
        # lark_oapi.core.http and .token load httpx and requests_toolbelt: 65 ms of a runner's first card write (S48 B2).
        script = ("import sys, json, types\n"
                  "import requests\n"
                  "def http(session, method, url, **kwargs):\n"
                  "    body = {'code': 0, 'tenant_access_token': 't', 'expire': 7200} if url.endswith('/internal') else {'code': 0}\n"
                  "    return types.SimpleNamespace(status_code=200, headers={'Content-Type': 'application/json'},\n"
                  "                                 content=json.dumps(body).encode())\n"
                  "requests.Session.request = http\n"
                  "from covey.feishu import sdk\n"
                  "client = sdk._Client('cli_test', 'secret', 'https://open.feishu.cn')\n"
                  "sdk.client = lambda profile: client\n"
                  "print(client.warm(), sdk.runner('named', 'PUT', '/open-apis/cardkit/v1/cards/c', {})(['x']).returncode)\n"
                  "print(sorted(n for n in ('lark_oapi.core.http', 'lark_oapi.core.token', 'httpx', 'requests_toolbelt')\n"
                  "             if n in sys.modules))\n")
        env = {**os.environ, "PYTHONPATH": str(Path(transport.__file__).resolve().parents[2])}
        out = subprocess.run([sys.executable, "-B", "-W", "ignore", "-c", script], env=env, capture_output=True,
                             text=True, timeout=60, check=True).stdout.split("\n")
        self.assertEqual(out[:2], ["True 0", "[]"])


class FromArgvTests(unittest.TestCase):
    def argv(self, *flags, shortcut="+messages-send", target=("--chat-id", "oc_1")):
        return ["lark-cli", "--profile", "named", "im", shortcut, "--as", "bot", *target, *flags,
                "--idempotency-key", "k", "--format", "json"]

    def test_only_what_lark_cli_would_post_unchanged_is_rebuilt(self):
        self.assertIsNotNone(from_argv(self.argv("--text", "- a list")))
        for argv in (self.argv("--text", ""), self.argv("--text", "-"), self.argv("--markdown", "@file"),
                     self.argv("--markdown", "![a](http://x/y.png)"), self.argv("--text", "x", target=("--chat-id", "c")),
                     self.argv("--text", "x", shortcut="+messages-reply", target=("--message-id", "x")),
                     self.argv("--msg-type", "text", "--text", "x"), self.argv("--text", "x", "--user-id", "ou_1"),
                     self.argv("--msg-type", "interactive", "--content", "{bad"),
                     ["lark-cli", "im", "+messages-send", "--as", "bot", "--chat-id", "oc_1", "--text", "x"]):
            with self.subTest(argv=argv[4:]):
                self.assertIsNone(from_argv(argv))
        long_key = self.argv("--text", "x")
        long_key[-3] = "k" * 51
        self.assertIsNone(from_argv(long_key))


class CliReadTests(unittest.TestCase):
    """Repo review R7: the lark-cli reads (代答's user_cli, the Bridge's run_json, the page's listed_messages,
    message details) go through transport.cli_json, each with its own words and timeout as before."""

    def runner(self, returncode=0, stdout="", stderr=""):
        calls = []

        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)
        return run, calls

    ERROR = json.dumps({"ok": False, "error": {"type": "api", "message": "permission\n  denied: im:message"}}, indent=2)

    def test_a_read_returns_its_json_and_says_why_it_failed_in_one_line(self):
        run, calls = self.runner(stdout='{"data": {"x": 1}}')
        self.assertEqual(transport.cli_json(["lark-cli", "im"], timeout=9, runner=run), {"data": {"x": 1}})
        self.assertEqual(calls, [(["lark-cli", "im"], {"capture_output": True, "text": True, "timeout": 9, "check": False})])
        run, calls = self.runner(stdout='{}')
        transport.cli_json(["lark-cli", "drive"], timeout=9, runner=run, cwd="/w")
        self.assertEqual(calls[0][1]["cwd"], "/w")
        for (returncode, stdout, stderr), reason in (((1, self.ERROR, "ignored"), "permission denied: im:message"),
                                                     ((2, "", "  boom\n  again "), "boom again"),
                                                     ((3, "plain\ntext", ""), "plain text"), ((4, "", ""), "")):
            with self.assertRaises(transport.CliFailed) as caught:
                transport.cli_json(["lark-cli"], timeout=1, runner=self.runner(returncode, stdout, stderr)[0])
            self.assertEqual((caught.exception.reason(), caught.exception.proc.returncode), (reason, returncode))
            self.assertEqual(str(caught.exception), reason or "lark-cli rc=4")
        self.assertEqual(transport.CliFailed(SimpleNamespace(returncode=1, stdout="", stderr="x" * 400)).reason(), "x" * 300)
        with self.assertRaises(ValueError):
            transport.cli_json(["lark-cli"], timeout=1, runner=self.runner(stdout="not json")[0])
        self.assertEqual(transport.cli_json(["lark-cli"], timeout=1, runner=self.runner()[0], empty={}), {})
        with self.assertRaises(transport.CliFailed):
            transport.cli_json(["lark-cli"], timeout=1, runner=self.runner(stdout="[1]")[0], expect=dict)

    def test_the_owners_reads_for_代答_say_what_they_said(self):
        from covey.app.proxy_send import user_cli
        cases = (((1, self.ERROR, "e"), "permission denied: im:message"), ((1, "", " too\n many "), "too many"),
                 ((5, "", ""), "lark-cli rc=5"), ((1, "", "y" * 400), "y" * 300))
        for (returncode, stdout, stderr), said in cases:
            run, calls = self.runner(returncode, stdout, stderr)
            with mock.patch("covey.app.proxy_send.subprocess.run", run), self.assertRaises(RuntimeError) as caught:
                user_cli("me", "im", "+chat-search", "--query", "q")
            self.assertEqual((type(caught.exception), str(caught.exception)), (RuntimeError, said))
        self.assertEqual(calls[0], (["lark-cli", "--profile", "me", "im", "+chat-search", "--query", "q", "--as", "user",
                                     "--format", "json"], {"capture_output": True, "text": True, "timeout": 60, "check": False}))
        run, calls = self.runner(stdout='{"data": {"chats": []}}')
        with mock.patch("covey.app.proxy_send.subprocess.run", run):
            self.assertEqual(user_cli("me", "x", timeout=7), {"data": {"chats": []}})
        self.assertEqual(calls[0][1]["timeout"], 7)
        with mock.patch("covey.app.proxy_send.subprocess.run", self.runner(stdout="")[0]), \
                self.assertRaises(ValueError):
            user_cli("me", "x")

    def test_the_bridges_reads_at_start_say_what_they_said(self):
        from covey.app.unified import run_json
        run, calls = self.runner(returncode=1, stdout="out", stderr="")
        with mock.patch("covey.app.unified.subprocess.run", run), self.assertRaises(RuntimeError) as caught:
            run_json(["lark-cli", "auth", "status"])
        self.assertEqual((type(caught.exception), str(caught.exception)), (RuntimeError, "lark-cli failed: out"))
        self.assertEqual(calls[0][1], {"capture_output": True, "text": True, "timeout": 30, "check": False})
        with mock.patch("covey.app.unified.subprocess.run", self.runner(1, "out", "err\n")[0]), \
                self.assertRaises(RuntimeError) as caught:
            run_json(["lark-cli"], timeout=20)
        self.assertEqual(str(caught.exception), "lark-cli failed: err\n")
        with mock.patch("covey.app.unified.subprocess.run", self.runner(stdout='{"appId": "a"}')[0]):
            self.assertEqual(run_json(["lark-cli"]), {"appId": "a"})

    def test_a_message_detail_read_says_what_it_said(self):
        for (returncode, stdout, stderr), said in (((1, "o", "e" * 1200), {"ok": False, "error": "e" * 1000}),
                                                   ((2, "out", ""), {"ok": False, "error": "out"}),
                                                   ((3, "", ""), {"ok": False, "error": "rc=3"}),
                                                   ((0, "x" * 1100, ""), {"ok": False, "error": "non-json response: " + "x" * 1000}),
                                                   ((0, "[1]", ""), {"ok": False, "error": "unexpected response shape"}),
                                                   ((0, '{"data": {}}', ""), {"data": {}})):
            run, calls = self.runner(returncode, stdout, stderr)
            with mock.patch("covey.feishu.message_read.message_request.read_runner", return_value=None), \
                    mock.patch("covey.feishu.message_read.subprocess.run", run):
                self.assertEqual(fetch_message_detail("om_1", "bot"), said)
        self.assertEqual(calls[0][1], {"capture_output": True, "text": True, "timeout": 30, "check": False})
        self.assertEqual(calls[0][0][:6], ["lark-cli", "--profile", "bot", "api", "GET", "/open-apis/im/v1/messages/om_1"])


if __name__ == "__main__":
    unittest.main()
