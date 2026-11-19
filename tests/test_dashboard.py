"""The sensitive-term dashboard (app/dashboard.py): its access rules, API, worker and the owner's menu item and 换口令 button; its
fixed entry and the link it sends when its address moves (app/dashboard_entry.py)."""
# privacy: allow-file 测扩展钩子 dashboard_entry 的固定入口与 bytedcli 提示
from __future__ import annotations

import http.client
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support import quiet, requires_extension
from support.world import UnifiedCase
from support.records import write_topic
from covey.app import dashboard, dashboard_entry, dashboard_http
from covey.app.dashboard import NOT_CONFIGURED, ROTATE_KIND, Dashboard, detect_upstream
from covey.app.dashboard_entry import (DASHBOARD_ENV, URL_ENV, dashboard_address, global_ipv6,
                                                link_host, parse_bind, read_record, record_path, update_record)
from covey.app.dashboard_http import COOKIE, dashboard_token, token_path
from covey.core import redaction_views
from covey.app.runtime import Runtime
from covey.backends.claude.redaction import Unavailable
from covey.core.redaction import auto_stand_in, config_path, stats_path
from covey.core.state import write_private_text


ENTRY_ENV = "COVEY_DASHBOARD_ENTRY"  # The company extension's fixed entry (covey_internal/dashboard_entry.py).


class Server(unittest.TestCase):
    """A dashboard listening on 127.0.0.1 over a temporary state directory."""

    detect = None

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name) / "state"
        self.runtime = Runtime(script_path="unit")
        self.logs = quiet(SimpleNamespace(runtime=self.runtime))
        self.dashboard = Dashboard(self.runtime, detect=self.detect)
        server = self.dashboard.start(self.state, ("127.0.0.1", 0))
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.port = server.server_address[1]
        self.token = dashboard_token(self.state)

    def call(self, method, path, body=None, *, token=None, cookie=None, write=True, headers=None, raw=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        self.addCleanup(connection.close)
        sent = dict(headers or {})
        if token is not None:
            sent["Authorization"] = "Bearer " + token
        if cookie is not None:
            sent["Cookie"] = f"{COOKIE}={cookie}"
        if write and method not in ("GET", "HEAD"):
            sent["X-Dashboard"] = "1"
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        if data is not None:
            sent["Content-Type"] = "application/json"
        connection.request(method, path, body=data, headers=sent)
        response = connection.getresponse()
        payload = response.read()
        try:
            parsed = json.loads(payload) if payload else None
        except ValueError:
            parsed = payload
        return response, parsed

    def api(self, method, path, body=None, **kwargs):
        kwargs.setdefault("cookie", self.token)
        return self.call(method, path, body, **kwargs)


class AccessTests(Server):
    def test_the_page_is_open_but_the_api_needs_the_token(self):
        response, page = self.call("GET", "/")
        self.assertEqual(response.status, 200)
        self.assertIn(b"/app.js", page)
        self.assertEqual(response.getheader("Cache-Control"), "no-store")
        self.assertEqual(response.getheader("X-Frame-Options"), "DENY")
        self.assertIn("script-src 'self'", response.getheader("Content-Security-Policy"))
        self.assertIsNone(response.getheader("Access-Control-Allow-Origin"))
        self.assertEqual(self.call("GET", "/app.js")[0].getheader("Content-Type"), "text/javascript; charset=utf-8")
        self.assertEqual(self.call("GET", "/api/config")[0].status, 401)
        self.assertEqual(self.call("GET", "/api/config", token=self.token)[0].status, 200)
        self.assertEqual(oct(token_path(self.state).stat().st_mode & 0o777), "0o600")

    def test_the_link_sets_the_cookie_and_leaves_the_address_bar(self):
        response, _ = self.call("GET", "/?t=" + self.token)
        self.assertEqual((response.status, response.getheader("Location")), (303, "/"))
        cookie = response.getheader("Set-Cookie")
        self.assertTrue(cookie.startswith(f"{COOKIE}={self.token};"))
        for part in ("HttpOnly", "SameSite=Strict", "Path=/", "Max-Age=2592000"):
            self.assertIn(part, cookie)
        self.assertEqual(self.api("GET", "/api/config")[0].status, 200)
        wrong, _ = self.call("GET", "/?t=nope")
        self.assertEqual((wrong.status, wrong.getheader("Set-Cookie")), (303, None))
        # Neither the token nor the query reaches the log.
        self.assertIn("dashboard GET / 303", self.logs)
        self.assertFalse([line for line in self.logs if self.token in line or "t=" in line])

    def test_the_login_form_takes_the_token(self):
        self.assertEqual(self.call("POST", "/api/login", {"token": "wrong"})[0].status, 401)
        self.assertEqual(self.call("POST", "/api/login", {"token": self.token}, write=False)[0].status, 403)
        response, body = self.call("POST", "/api/login", {"token": self.token})
        self.assertEqual((response.status, body), (200, {"ok": True}))
        self.assertIn("HttpOnly", response.getheader("Set-Cookie"))

    def test_wrong_tokens_shut_the_address_out_for_a_minute(self):
        for _ in range(4):
            self.assertEqual(self.api("GET", "/api/config", cookie="wrong-token")[0].status, 401)
        self.assertEqual(self.call("GET", "/?t=wrong")[0].status, 303)  # The fifth.
        self.assertEqual(self.api("GET", "/api/config")[0].status, 429)
        self.assertEqual(self.call("GET", "/?t=" + self.token)[0].status, 429)
        self.assertEqual(self.call("GET", "/")[0].status, 200)  # The page itself still loads.
        # A minute later.
        self.dashboard.http.blocked = {a: time.monotonic() - 1 for a in self.dashboard.http.blocked}
        self.assertEqual(self.api("GET", "/api/config")[0].status, 200)

    def test_a_request_that_is_not_http_is_logged_by_its_status_alone(self):
        # The start of a TLS ClientHello (a browser speaking https to this port), and a request line with the token.
        for raw in (b"\x16\x03\x01\x02\x00\x01\x00\x01\xfc\x03\x03\n", b"PUT/?t=" + self.token.encode() + b"\r\n"):
            with socket.create_connection(("127.0.0.1", self.port), timeout=30) as client:
                client.sendall(raw)
                while client.recv(4096):
                    pass
        self.assertEqual([line for line in self.logs if line.startswith("dashboard ")],
                         ["dashboard bad request 400: not HTTP (https to this http port?)"] * 2)
        self.assertFalse([line for line in self.logs if self.token in line or "t=" in line])

    def test_a_missing_cookie_is_not_a_wrong_token(self):
        for _ in range(8):
            self.assertEqual(self.call("GET", "/api/config")[0].status, 401)
        self.assertEqual(self.api("GET", "/api/config")[0].status, 200)

    def test_writes_need_the_header(self):
        response, body = self.api("PUT", "/api/config", {"terms": []}, write=False)
        self.assertEqual((response.status, body), (403, {"error": "缺少请求头 X-Dashboard"}))
        self.assertFalse(config_path(self.state).exists())
        self.assertEqual(self.api("PUT", "/api/config", {"terms": []})[0].status, 200)

    def test_a_rotated_token_ends_the_old_cookie(self):
        new = dashboard_token(self.state, rotate=True)
        self.assertNotEqual(new, self.token)
        self.assertEqual(self.api("GET", "/api/config")[0].status, 401)
        self.assertEqual(self.api("GET", "/api/config", cookie=new)[0].status, 200)

    def test_bodies_are_bounded_and_json(self):
        response, _ = self.api("POST", "/api/preview", raw=b"x",
                               headers={"Content-Length": str(dashboard_http.MAX_BODY + 1)})
        self.assertEqual(response.status, 413)
        self.assertEqual(self.api("POST", "/api/preview", raw=b"not json")[1], {"error": "请求不是 JSON"})
        self.assertEqual(self.api("DELETE", "/api/config")[0].status, 405)
        self.assertEqual(self.api("GET", "/api/nothing")[0].status, 404)


class ApiTests(Server):
    def test_the_config_round_trip(self):
        config = {"default_on": True, "ignore_case": True, "restore": True, "upstream": "",
                  "terms": [{"term": "Project Atlas", "stand_in": "", "anywhere": False},
                            {"term": "阿特拉斯", "stand_in": "甲项目", "anywhere": True},
                            {"term": "Orion", "stand_in": "Star", "anywhere": False},
                            {"term": "Vega", "stand_in": "Star", "anywhere": False}]}
        response, body = self.api("PUT", "/api/config", config)
        self.assertEqual(response.status, 200)
        # A stand-in an older page sends is dropped (T566): every term goes out as its automatic one.
        kept = {**config, "terms": [{"term": t["term"], "anywhere": t["anywhere"]} for t in config["terms"]]}
        self.assertEqual(body["config"], kept)
        _, body = self.api("GET", "/api/config")
        self.assertEqual(body["config"], kept)
        terms = {t["term"]: t for t in body["terms"]}
        self.assertEqual(terms["Project Atlas"]["effective"], auto_stand_in("Project Atlas"))
        self.assertEqual(terms["阿特拉斯"]["effective"], auto_stand_in("阿特拉斯"))
        self.assertEqual([terms[t]["restore"] for t in ("Project Atlas", "阿特拉斯", "Orion")], ["还原"] * 3)
        self.assertTrue(body["updated_at"])
        _, body = self.api("PUT", "/api/config", {**config, "restore": False})
        self.assertEqual({t["restore"] for t in body["terms"]}, {"不还原"})

    def test_a_wrong_config_is_refused_in_words(self):
        response, body = self.api("PUT", "/api/config", {"terms": [{"term": "x", "stand_in": ""}]})
        self.assertEqual((response.status, body), (400, {"error": "原词至少要 2 个字"}))
        self.assertFalse(config_path(self.state).exists())

    def test_a_broken_file_is_shown_not_failed(self):
        write_private_text(config_path(self.state), "{broken")
        response, body = self.api("GET", "/api/config")
        self.assertEqual(response.status, 200)
        self.assertIn("敏感词配置读取失败", body["error"])

    def test_preview_redacts_and_restores(self):
        self.api("PUT", "/api/config", {"terms": [{"term": "Atlas", "stand_in": "Delta"}]})
        _, body = self.api("POST", "/api/preview", {"text": "Atlas and atlas, not Atlases"})
        self.assertEqual(body["redacted"], f"{auto_stand_in('Atlas')} and {auto_stand_in('atlas')}, not Atlases")
        self.assertEqual(body["restored"], "Atlas and atlas, not Atlases")
        self.assertEqual(body["hits"], {"Atlas": 2})
        # The page's unsaved config, when it sends one.
        _, body = self.api("POST", "/api/preview", {"text": "Atlas", "config": {"terms": [{"term": "Atlas",
                                                                                          "stand_in": "Mu"}]}})
        self.assertEqual(body["redacted"], auto_stand_in("Atlas"))
        response, body = self.api("POST", "/api/preview", {"text": "x" * (redaction_views.MAX_PREVIEW + 1)})
        self.assertEqual(response.status, 400)
        response, body = self.api("POST", "/api/preview", {"text": "a", "config": {"terms": "no"}})
        self.assertEqual((response.status, body), (400, {"error": "词表必须是列表"}))

    def test_stats_count_terms_turns_and_redacted_topics(self):
        now = time.time()
        rows = [{"ts": now - 10 * 86400, "topic_id": "a" * 64, "harness": "claude", "model": "opus", "requests": 2,
                 "hits": {"Atlas": 5}},
                {"ts": now - 60, "topic_id": "a" * 64, "harness": "claude", "model": "opus", "requests": 3,
                 "hits": {"Atlas": 1, "Orion": 2}},
                {"ts": now - 30, "topic_id": "b" * 64, "harness": "doubao", "model": "", "requests": 1, "hits": {}},
                {"ts": now - 20, "topic_id": "../../etc", "requests": 1, "hits": {"Orion": 1}}]
        write_private_text(stats_path(self.state), "".join(json.dumps(r) + "\n" for r in rows) + "not json\n")
        write_topic(self.state, {"topic_number": 12, "redaction": True, "workspace_name": "ws", "root_id": "m1"},
                    topic_id="a" * 64)
        write_topic(self.state, {"topic_number": 13}, topic_id="b" * 64)
        _, body = self.api("GET", "/api/stats")
        self.assertEqual(body["terms"], [{"term": "Atlas", "hits": 6, "week": 1}, {"term": "Orion", "hits": 3, "week": 3}])
        self.assertEqual((body["week_hits"], body["turn_count"]), (4, 4))
        self.assertEqual([t["number"] for t in body["turns"]], [None, 13, 12, 12])  # Newest first.
        self.assertEqual(body["turns"][2]["replaced"], 3)
        self.assertEqual([(t["number"], t["turns"], t["hits"], t["workspace"]) for t in body["topics"]],
                         [(12, 2, 8, "ws")])
        _, config = self.api("GET", "/api/config")
        self.assertEqual(config["week_hits"], 4)


class DetectTests(Server):
    def detect(self):
        return {"upstream": "http://127.0.0.1:9", "source": "test"}

    def test_detect_answers_off_the_request_thread(self):
        response, body = self.api("POST", "/api/upstream/detect", {})
        self.assertEqual((response.status, body), (200, {"upstream": "http://127.0.0.1:9", "source": "test"}))
        self.assertFalse(self.dashboard.http.detecting.locked())

    def test_a_failed_detection_says_why(self):
        def fail():
            raise Unavailable("个人网关没有就绪")
        self.dashboard.http.detect = fail
        self.assertEqual(self.api("POST", "/api/upstream/detect", {})[1], {"error": "个人网关没有就绪"})

        def crash():
            raise RuntimeError("secret token sk-123")
        self.dashboard.http.detect = crash
        self.assertEqual(self.api("POST", "/api/upstream/detect", {})[1], {"error": "探测失败（RuntimeError）"})
        self.dashboard.http.detect = None
        self.assertEqual(self.api("POST", "/api/upstream/detect", {})[0].status, 503)

    def test_detect_upstream_names_its_source(self):
        ctx = SimpleNamespace(state_dir=self.state, backend_cmd=["claude"], workdir=self.state)
        harness = SimpleNamespace(context=ctx, backend=SimpleNamespace(child_env=lambda c: dict(self.env)))
        self.env = {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8080/"}
        self.assertEqual(detect_upstream(harness), {"upstream": "http://127.0.0.1:8080",
                                                    "source": "Bridge 环境里的 ANTHROPIC_BASE_URL"})
        self.env = {"HOME": str(self.state)}
        with mock.patch.object(dashboard, "gateway_status", return_value="http://[::1]:7000") as status:
            self.assertEqual(detect_upstream(harness)["upstream"], "http://[::1]:7000")
        self.assertIs(status.call_args.args[0], ctx)
        with mock.patch.object(dashboard, "gateway_status", return_value=""):
            self.assertEqual(detect_upstream(harness)["source"], "默认（Anthropic 官方地址）")
        self.assertRaises(ValueError, detect_upstream, SimpleNamespace(context=None))


class BindAndLinkTests(unittest.TestCase):
    def test_bind_specs(self):
        self.assertIsNone(parse_bind(""))
        self.assertEqual(parse_bind("10601"), ("::", 10601))
        self.assertEqual(parse_bind("[::]:10601"), ("::", 10601))
        self.assertEqual(parse_bind("0.0.0.0:10601"), ("0.0.0.0", 10601))
        self.assertEqual(parse_bind("[fdbd::1]:80"), ("fdbd::1", 80))
        for wrong in ("::1:80", "[::]", "host:", "host:0", "host:70000", ":80", "abc"):
            self.assertRaises(ValueError, parse_bind, wrong)

    def test_a_bind_spec_reads_the_reserved_port_from_the_environment(self):
        environ = {"ARNOLD_RESERVED_4_CURRENT_PORT": "10601", "HOST": "0.0.0.0"}
        self.assertEqual(parse_bind("[::]:$ARNOLD_RESERVED_4_CURRENT_PORT", environ), ("::", 10601))
        self.assertEqual(parse_bind("${HOST}:${ARNOLD_RESERVED_4_CURRENT_PORT}", environ), ("0.0.0.0", 10601))
        with self.assertRaisesRegex(ValueError, "ARNOLD_RESERVED_3_CURRENT_PORT 没有设置"):
            parse_bind("[::]:$ARNOLD_RESERVED_3_CURRENT_PORT", environ)
        with mock.patch.dict(os.environ, {"ARNOLD_RESERVED_4_CURRENT_PORT": "10777"}):
            self.assertEqual(parse_bind("$ARNOLD_RESERVED_4_CURRENT_PORT"), ("::", 10777))

    def test_link_hosts(self):
        # The IPv6 address before the name, which a browser may look up for IPv4 alone.
        with mock.patch.object(socket, "getfqdn", return_value="box.example.org"), \
                mock.patch.object(dashboard_entry, "resolves", return_value=True), \
                mock.patch.object(dashboard_entry, "global_ipv6", return_value="fdbd:dc01:28:37::14"):
            self.assertEqual(dashboard_address(("::", 10601), {}), "http://[fdbd:dc01:28:37::14]:10601")
            self.assertEqual(link_host("0.0.0.0"), "box.example.org")  # It listens on IPv4 only.
        with mock.patch.object(socket, "getfqdn", return_value="box.example.org"), \
                mock.patch.object(dashboard_entry, "global_ipv6", return_value=""):
            with mock.patch.object(dashboard_entry, "resolves", return_value=True):
                self.assertEqual(dashboard_address(("::", 10601), {}), "http://box.example.org:10601")
            with mock.patch.object(dashboard_entry, "resolves", return_value=False):
                self.assertEqual((link_host("::"), link_host("0.0.0.0")), ("localhost", "localhost"))
        self.assertEqual(link_host("192.0.2.5"), "192.0.2.5")
        self.assertEqual(link_host("fdbd::2"), "[fdbd::2]")
        self.assertEqual(dashboard_address(("::", 10601), {URL_ENV: "https://dash.example.org/"}),
                         "https://dash.example.org")

    def test_the_ipv6_address_is_the_default_routes(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = Path(tmp) / "if_inet6"
            # The interfaces list eth2's address before eth0's; the default route leaves from eth0's.
            table.write_text("00000000000000000000000000000001 01 80 10 80 lo\n"
                             "fe800000000000000e42a1fffe57cf10 02 40 20 80 eth0\n"
                             "fdbddc01002800380000000000000068 03 40 00 80 eth2\n"
                             "fdbddc01002800370000000000000014 02 40 00 80 eth0\n")
            with mock.patch.object(dashboard_entry, "IF_INET6", table):
                with mock.patch.object(socket, "socket", return_value=Probe("fdbd:dc01:28:37::14")):
                    self.assertEqual(global_ipv6(), "fdbd:dc01:28:37::14")
                # No IPv6 route, or one from a link-local address: the first global address of an interface.
                for probe in (Probe(OSError(101, "Network is unreachable")), Probe("fe80::1%eth0")):
                    with mock.patch.object(socket, "socket", return_value=probe):
                        self.assertEqual(global_ipv6(), "fdbd:dc01:28:38::68")
                table.write_text("00000000000000000000000000000001 01 80 10 80 lo\n")
                with mock.patch.object(socket, "socket", return_value=Probe(OSError(101, "Network is unreachable"))):
                    self.assertEqual(global_ipv6(), "")


class Probe:
    """The UDP socket global_ipv6 connects: it leaves from ``source``, or raises it (an OSError)."""

    def __init__(self, source):
        self.source = source

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def connect(self, address):
        if isinstance(self.source, OSError):
            raise self.source

    def getsockname(self):
        return self.source, 0, 0, 0


class WorkerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.runtime = Runtime(script_path="unit")
        self.logs = quiet(SimpleNamespace(runtime=self.runtime))
        self.ctx = SimpleNamespace(state_dir=Path(tmp.name))

    def run_worker(self, spec):
        worker = threading.Thread(target=Dashboard(self.runtime).run, args=(self.ctx,), daemon=True)
        with mock.patch.dict(os.environ, {DASHBOARD_ENV: spec, ENTRY_ENV: ""}):
            worker.start()
            for _ in range(200):
                if self.logs:
                    break
                time.sleep(0.05)
        self.assertTrue(worker.is_alive())  # Waits for the stop rather than return to be restarted.
        self.runtime.shutdown.set()
        worker.join(10)
        self.assertFalse(worker.is_alive())

    def test_it_serves_until_the_bridge_stops(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        self.run_worker(f"127.0.0.1:{port}")
        self.assertTrue(self.logs[0].startswith("dashboard listening on 127.0.0.1 port "))

    def test_a_busy_port_is_logged_once(self):
        taken = socket.socket()
        self.addCleanup(taken.close)
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        self.run_worker(f"127.0.0.1:{taken.getsockname()[1]}")
        self.assertEqual(len(self.logs), 1)
        self.assertIn("dashboard not started: cannot listen", self.logs[0])


@requires_extension()
class SettleTests(unittest.TestCase):
    """What the worker does once listening: the fixed entry, then the link to the owner's private chat."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.runtime = Runtime(script_path="unit")
        self.logs = quiet(SimpleNamespace(runtime=self.runtime))
        self.ctx = SimpleNamespace(state_dir=Path(tmp.name), profile="bot")
        self.messenger = SimpleNamespace(send_message=mock.Mock(return_value=True))
        voice = SimpleNamespace(key=lambda tag, message_id: f"v-{tag}-{message_id}")
        self.dashboard = Dashboard(self.runtime, self.messenger, voice)
        self.published, self.problem = [], ""
        for patcher in (mock.patch("covey_internal.dashboard_entry.publish", side_effect=self.publish),
                        mock.patch.dict(os.environ, {ENTRY_ENV: "Harness-Bridge-Dash", URL_ENV: ""})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def publish(self, state_dir, alias, target, *, owned):
        self.published.append((alias, target, owned))
        return self.problem

    def settle(self, host):
        with mock.patch.object(dashboard_entry, "link_host", return_value=host):
            self.dashboard.settle(self.ctx, ("::", 10601))

    def sent(self):
        return [c.args for c in self.messenger.send_message.call_args_list]

    def test_the_entry_follows_the_address(self):
        self.settle("a.example.org")
        self.settle("a.example.org")
        self.settle("b.example.org")
        self.assertEqual(self.published, [("harness-bridge-dash", "http://a.example.org:10601/", False),
                                          ("harness-bridge-dash", "http://b.example.org:10601/", True)])
        self.assertEqual(read_record(self.ctx.state_dir)["entry"]["target"], "http://b.example.org:10601/")
        self.assertIn("dashboard entry published at https://harness-bridge-dash.gf-preview.bytedance.net", self.logs)

    def test_an_old_page_is_published_again(self):
        update_record(self.ctx.state_dir, entry={"alias": "harness-bridge-dash", "at": time.time() - 200 * 86400,
                                                 "target": "http://a.example.org:10601/"})
        self.settle("a.example.org")
        self.assertEqual(self.published, [("harness-bridge-dash", "http://a.example.org:10601/", True)])

    def test_a_failed_publish_is_kept_for_the_reply(self):
        self.problem = "bytedcli 取不到登录凭据"
        self.settle("a.example.org")
        record = read_record(self.ctx.state_dir)
        self.assertNotIn("entry", record)
        self.assertEqual(record["entry_error"], self.problem)
        self.assertIn("dashboard entry not published: bytedcli 取不到登录凭据", self.logs)
        text = Dashboard.link_text(self.ctx.state_dir, "http://a.example.org:10601", "tok", lead="敏感词看板：")
        self.assertIn("还没指向这个地址：bytedcli 取不到登录凭据", text)
        self.problem = ""
        self.settle("a.example.org")
        self.assertNotIn("entry_error", read_record(self.ctx.state_dir))

    def test_no_link_is_sent_before_a_private_chat_asked(self):
        self.settle("a.example.org")
        self.assertEqual(self.sent(), [])

    def test_a_moved_address_goes_to_the_private_chat_once(self):
        update_record(self.ctx.state_dir, chat_id="oc_dm", address="http://a.example.org:10601")
        self.settle("a.example.org")
        self.assertEqual(self.sent(), [])
        self.settle("b.example.org")
        self.settle("b.example.org")
        [(profile, chat_id, text, key)] = self.sent()
        token = dashboard_token(self.ctx.state_dir)
        self.assertEqual((profile, chat_id), ("bot", "oc_dm"))
        self.assertTrue(text.startswith("脱敏看板换了地址（开发机重启后换了机器或端口），新链接："
                                        f"http://b.example.org:10601/?t={token}\n"))
        self.assertIn("固定入口：https://harness-bridge-dash.gf-preview.bytedance.net（", text)
        self.assertTrue(key.startswith("v-dashboard-address-"))
        record = read_record(self.ctx.state_dir)
        self.assertEqual(record["address"], "http://b.example.org:10601")
        self.assertNotIn("announcing", record)
        self.assertFalse([line for line in self.logs if token in line])

    def test_the_first_link_once_a_private_chat_is_known(self):
        update_record(self.ctx.state_dir, chat_id="oc_dm")
        self.settle("a.example.org")
        [(_, _, text, _)] = self.sent()
        self.assertTrue(text.startswith("脱敏看板：http://a.example.org:10601/?t="))

    def test_an_unsent_link_goes_again_under_its_key(self):
        update_record(self.ctx.state_dir, chat_id="oc_dm", address="http://a.example.org:10601")
        self.messenger.send_message.return_value = False
        self.settle("b.example.org")
        self.assertIn("dashboard address not announced: the message was not sent", self.logs)
        self.messenger.send_message.return_value = True
        self.settle("b.example.org")
        first, second = (args[3] for args in self.sent())
        self.assertEqual(first, second)
        self.assertEqual(read_record(self.ctx.state_dir)["address"], "http://b.example.org:10601")
        # Past Feishu's one-hour window the key no longer guards anything: a new one.
        update_record(self.ctx.state_dir, address="http://a.example.org:10601",
                      announcing={"address": "http://b.example.org:10601", "key": first, "at": time.time() - 4000})
        self.settle("b.example.org")
        self.assertNotEqual(self.sent()[-1][3], first)

    def test_no_entry_without_a_good_setting(self):
        update_record(self.ctx.state_dir, chat_id="oc_dm", address="http://a.example.org:10601")
        with mock.patch.dict(os.environ, {ENTRY_ENV: ""}):
            self.settle("b.example.org")
        self.assertNotIn("固定入口", self.sent()[0][2])
        with mock.patch.dict(os.environ, {ENTRY_ENV: "bad alias"}):
            self.settle("c.example.org")
        self.assertIn("固定入口没有开", self.sent()[1][2])
        self.assertEqual(self.published, [])
        self.assertTrue([line for line in self.logs if line.startswith("dashboard entry not published: ")])


class MenuTests(UnifiedCase):
    def setUp(self):
        super().setUp()
        self.app.runtime.access.remember_owner_chat('dm')
        self.cards = []
        self.app.messenger.create_and_send_card = mock.Mock(side_effect=lambda profile, chat, card, key, **kw: (
            self.cards.append((chat, card)), ('card', f'om_{len(self.cards)}'))[1])

    def menu(self, event_id):
        """A link card of its own, as the bot menu's 脱敏 sent before 2026-11-09 (its key opens the 设置 card on its
        脱敏 tab now: test_settings_hub)."""
        self.app.dashboard.send_link(self.ctx, 'dm', event_id,
                                     shown=self.app.menu.shown(self.ctx, 'redaction_dashboard', 'dm', event_id))

    def rotate(self, event_id, operator='owner'):
        return self.app.handle_card_action_event({'event_id': event_id, 'operator_id': operator, 'chat_id': 'dm',
                                                  'message_id': 'om_link', 'action_value': {'kind': ROTATE_KIND}},
                                                 self.ctx)

    def test_the_menu_sends_the_link_and_its_button_a_new_token(self):
        with mock.patch.dict(os.environ, {DASHBOARD_ENV: "127.0.0.1:10601"}):
            self.menu('ev1')
            token = dashboard_token(self.ctx.state_dir)
            self.rotate('click1')
            self.rotate('click1')  # Delivered twice: rotated once.
            self.rotate('click2', operator='someone')  # Not the owner's private chat: refused.
        new = dashboard_token(self.ctx.state_dir)
        self.assertEqual(self.said(), [])
        (chat1, first), (chat2, second) = self.cards
        self.assertEqual((chat1, chat2), ('dm', 'dm'))
        self.assertEqual((first['header']['title']['content'], first['header']['icon']['token']), ('脱敏看板', 'safe_outlined'))
        buttons = first['body']['elements'][0]['columns']
        (open_page,), (rotate,) = (column['elements'] for column in buttons)
        self.assertEqual((open_page['text']['content'], open_page['behaviors'][0]),
                         ('打开看板', {'type': 'open_url', 'default_url': f"http://127.0.0.1:10601/?t={token}"}))
        self.assertEqual((rotate['text']['content'], rotate['behaviors'][0]['value']), ('换口令', {'kind': ROTATE_KIND}))
        self.assertIn("要换口令点“换口令”", first['body']['elements'][1]['content'])
        self.assertNotEqual(new, token)
        self.assertEqual(second['body']['elements'][0]['content'],
                         "口令已换：之前的链接和已登录的浏览器都失效了，点“打开看板”用新口令登录。")
        self.assertIn(f"/?t={new}", json.dumps(second))
        # The new link's card replaces the menu's: the first is recalled.
        self.app.messenger.recall.assert_called_once_with(self.ctx.profile, 'om_1')
        self.assertEqual(list((self.ctx.state_dir / 'topics').glob('*/topic.json')), [])  # No task.
        self.assertFalse([line for line in self.logs if token in line or new in line])
        self.assertTrue([line for line in self.logs if 'outcome=duplicate' in line])

    def test_an_unset_dashboard_gets_words_only_and_the_old_text_is_a_task(self):
        with mock.patch.dict(os.environ, {DASHBOARD_ENV: ""}):
            self.menu('ev2')
            self.app.handle_event({'chat_id': 'dm', 'chat_type': 'p2p', 'sender_type': 'user', 'sender_id': 'owner',
                                   'message_id': 'd3', 'content': '敏感词看板 换口令'}, self.ctx)
        self.assertEqual(self.said(), [NOT_CONFIGURED])
        self.assertNotIn('card', self.app.messenger.send_message.call_args.kwargs)
        self.assertTrue(any(app.submission.selection_card.called for app in self.app.apps.values()))
        self.assertFalse(token_path(self.ctx.state_dir).exists())
        self.assertFalse(record_path(self.ctx.state_dir).exists())

    @requires_extension()
    def test_the_private_chat_is_where_a_moved_address_goes(self):
        with mock.patch.dict(os.environ, {DASHBOARD_ENV: "127.0.0.1:10601", ENTRY_ENV: "harness-bridge-dash"}):
            self.menu('ev3')
        record = read_record(self.ctx.state_dir)
        self.assertEqual((record["chat_id"], record["address"]), ("dm", "http://127.0.0.1:10601"))
        self.assertIn("固定入口 https://harness-bridge-dash.gf-preview.bytedance.net 正在更新到这个地址",
                      json.dumps(self.cards[0][1], ensure_ascii=False))

    def test_the_worker_runs_only_when_configured(self):
        with mock.patch.dict(os.environ, {DASHBOARD_ENV: ""}):
            self.assertNotIn('dashboard', dict(self.app.worker_targets()))
        with mock.patch.dict(os.environ, {DASHBOARD_ENV: "10601"}):
            self.assertIn('dashboard', dict(self.app.worker_targets()))


class NoEntryTests(unittest.TestCase):
    """Without the extension hook the dashboard has no fixed entry: nothing is published and the link says nothing of it."""

    def test_no_hook_means_no_entry(self):
        with mock.patch.dict(os.environ, {ENTRY_ENV: "harness-bridge-dash"}), \
                mock.patch("covey.app.dashboard.extensions.hook", return_value=None):
            tmp = tempfile.TemporaryDirectory()
            self.addCleanup(tmp.cleanup)
            board = Dashboard(Runtime(script_path="unit"), None, None)
            board.point_entry(Path(tmp.name), "http://a.example.org:10601")
            self.assertEqual(read_record(Path(tmp.name)), {})
            lines = Dashboard.link_lines(Path(tmp.name), "http://a.example.org:10601", "T", lead="L")[2]
            self.assertEqual(len(lines), 1)


if __name__ == "__main__":
    unittest.main()
