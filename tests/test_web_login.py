"""网页登录确认 (app/web_login.py, cards/web_login.py, app/web_ops.py ``login``): the page's ``login`` operation sends one card, once, to
the owner's private chat; 允许 / 不是我 is the owner's alone, once per event, answered before anything goes over the network, told to the
page as a ``login`` item (no lock held); the card becomes the result, says the login is void when the page took nothing, or says why
the page did not take it and can be clicked again; a request past five minutes is expired and sends nothing; once decided the page
is not told twice."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from covey.app import card_lanes
from covey.app.card_actions import ACTIONS
from covey.app.unified import UnifiedBridgeApp
from covey.app.web_login import WebLogin
from covey.app.web_ops import Ops
from covey.cards import web_login as cards
from covey.core import web_sync
from covey.core.state import read_object

T0 = 1_800_000_000.0
PAYLOAD = {"request_id": "req-0123456789", "code": "4827", "browser": "Chrome 130", "os": "macOS 15", "at": T0}


class Messenger:
    def __init__(self):
        self.sent, self.patched, self.ok = [], [], True

    def send_message(self, profile, chat, text, key, *, voice, card=None, on_card=None, **kwargs):
        self.sent.append((chat, text, key, card))
        if self.ok and on_card:
            on_card(f"om_{len(self.sent)}")
        return self.ok

    def patch_card(self, profile, message_id, card):
        self.patched.append((message_id, card))
        return True

    def patch_or_tell(self, profile, message_id, card, **kwargs):  # feishu/messenger.py: patch_card, a line when it fails.
        return self.patch_card(profile, message_id, card)


class Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.now, self.requests, self.failure, self.logs, self.answer = T0 + 10, [], None, [], {"ok": True, "upserted": 1}
        self.runtime = SimpleNamespace(log=self.logs.append)
        self.messenger = Messenger()
        self.login = WebLogin(self.runtime, self.messenger, SimpleNamespace(safe_key=lambda key: key), self.request,
                              clock=lambda: self.now)
        self.ctx = SimpleNamespace(state_dir=self.state, profile="p")

    def request(self, method, path, body=None):
        if self.failure:
            raise self.failure
        self.requests.append((method, path, body))
        return self.answer

    def send(self, **payload):
        return self.login.send(self.state, "oc_dm", "p", {**PAYLOAD, **payload})

    def click(self, kind=cards.ALLOW, *, event="ev1", message="om_1", owner=True, request_id=PAYLOAD["request_id"]):
        """Off a card lane, so the follow-ups (app/card_lanes.py after_ack) run at once."""
        return self.login.claim(self.ctx, {"event_id": event, "message_id": message}, {"kind": kind, "request_id": request_id}, owner)

    def last(self):
        return json.dumps(self.messenger.patched[-1][1], ensure_ascii=False)


class SendTests(Case):
    def test_the_card_says_what_to_compare_and_has_two_buttons(self):
        self.assertIn("已在飞书私聊发出", self.send())
        chat, text, key, card = self.messenger.sent[0]
        body = json.dumps(card, ensure_ascii=False)
        self.assertEqual(chat, "oc_dm")
        for part in ("Chrome 130", "macOS 15", "4827", "允许", "不是我", cards.ALLOW, cards.DENY):
            self.assertIn(part, body)
        self.assertEqual(read_object(self.state / "web" / "logins.json")["req-0123456789"]["message_id"], "om_1")

    def test_the_card_says_where_the_request_came_from(self):
        """This machine (a browser here or an ssh forward) is 本机; a proxy's client is its address; an older page says nothing."""
        for ip, said in (("127.0.0.1", "本机"), ("::1", "本机"), ("::ffff:127.0.0.1", "本机"), ("198.51.100.23", "198.51.100.23"),
                         ("2001:db8::7", "2001:db8::7")):
            with self.subTest(ip=ip):
                self.assertIn(f"**来源**　{said}", cards.facts({**PAYLOAD, "ip": ip}))
        self.assertNotIn("来源", cards.facts(PAYLOAD))
        self.send(ip="203.0.113.9<b>")
        body = json.dumps(self.messenger.sent[0][3], ensure_ascii=False)
        self.assertIn("**来源**　203.0.113.9b", body)
        self.assertEqual(read_object(self.state / "web" / "logins.json")["req-0123456789"]["view"]["ip"], "203.0.113.9b")

    def test_the_same_request_sends_one_card(self):
        self.send()
        self.assertIn("已经发过", self.send())
        self.assertEqual(len(self.messenger.sent), 1)

    def test_a_clock_ahead_does_not_make_a_request_live_longer(self):
        self.send(at=T0 + 3600)
        self.now = T0 + 10 + 301
        self.assertEqual(self.login.claim(self.ctx, {"event_id": "e", "message_id": "om_1"},
                                          {"kind": cards.ALLOW, "request_id": PAYLOAD["request_id"]}, True), "web_login_expired")
        self.assertEqual(self.requests, [])

    def test_an_old_or_malformed_request_gets_no_card(self):
        with self.assertRaises(ValueError):
            self.send(at=T0 - 400)
        for bad in ({"request_id": "x"}, {"at": "now"}, {"at": True}):
            with self.assertRaises(ValueError):
                self.send(**bad)
        self.assertEqual(self.messenger.sent, [])

    def test_a_card_feishu_refuses_is_a_failure_that_can_be_sent_again(self):
        self.messenger.ok = False
        with self.assertRaises(ValueError):
            self.send()
        self.assertEqual(read_object(self.state / "web" / "logins.json")["req-0123456789"]["state"], "failed")
        self.messenger.ok = True
        self.assertIn("已在飞书私聊发出", self.send())
        self.assertEqual(read_object(self.state / "web" / "logins.json")["req-0123456789"]["state"], "pending")

    def test_what_a_browser_says_cannot_add_markup(self):
        self.send(browser="<at id=all></at>**x**")
        self.assertNotIn("<at", json.dumps(self.messenger.sent[0][3], ensure_ascii=False))


class ClickTests(Case):
    def setUp(self):
        super().setUp()
        self.send()

    def test_allow_tells_the_page_once_and_the_card_becomes_the_result(self):
        self.assertEqual(self.click(), "web_login_allow")
        (method, path, body), = self.requests
        self.assertEqual((method, path), ("POST", "/sync/upsert"))
        item, = body["items"]
        self.assertEqual((item["type"], item["key"], item["data"]["decision"]), ("login", PAYLOAD["request_id"], "approved"))
        self.assertEqual(item["data"]["decided_at"], int(self.now))
        self.assertIn("已允许", self.last())
        self.assertNotIn(cards.ALLOW, self.last())
        self.assertEqual(self.click(event="ev2"), "web_login_done")  # Another click: the result again, the page told no more.
        self.assertEqual(self.click(cards.DENY, event="ev3"), "web_login_done")
        self.assertEqual(len(self.requests), 1)
        self.assertIn("已允许", self.last())

    def test_deny(self):
        self.click(cards.DENY)
        self.assertEqual(self.requests[0][2]["items"][0]["data"]["decision"], "denied")
        self.assertIn("已拒绝", self.last())

    def test_only_the_owner_once_per_event_on_the_card_it_sent(self):
        self.assertEqual(self.click(event="e1", owner=False), "owner_only")
        self.assertEqual(self.click(event="e2", message="om_other"), "web_login_stale")
        self.assertEqual(self.click(event="e3", request_id="req-unknown-000"), "web_login_stale")
        self.assertEqual(self.requests, [])
        self.click(event="same")
        self.assertEqual(self.click(event="same"), "duplicate")
        self.assertEqual(len(self.requests), 1)
        self.assertIsNone(self.login.claim(self.ctx, {}, {"kind": "bridge_other"}, True))

    def test_a_refused_push_says_why_and_the_buttons_stay(self):
        self.failure = OSError("http 503")
        self.click()
        self.assertIn("http 503", self.last())
        self.assertIn(cards.DENY, self.last())
        self.assertEqual(read_object(self.state / "web" / "logins.json")[PAYLOAD["request_id"]]["state"], "pending")
        self.failure = None
        self.click(event="ev2")
        self.assertIn("已允许", self.last())

    def test_past_five_minutes_it_is_expired_and_nothing_is_sent(self):
        self.now = T0 + 301
        self.assertEqual(self.click(), "web_login_expired")
        self.assertIn("已过期", self.last())
        self.assertEqual(self.requests, [])
        self.assertEqual(self.click(event="ev2"), "web_login_expired")

    def test_the_request_is_not_sent_from_under_the_record_lock(self):
        seen = []
        self.login.request = lambda *args: seen.append(self.login.lock.locked()) or {"upserted": 1}
        self.click()
        self.assertEqual(seen, [False])

    def test_the_click_is_answered_before_anything_goes_over_the_network(self):
        card_lanes._click.pending = []
        try:
            outcome = self.click()
            follow_ups = card_lanes._click.pending
        finally:
            card_lanes._click.pending = None
        self.assertEqual(outcome, "web_login_allow")
        self.assertEqual((self.requests, self.messenger.patched), ([], []))  # Feishu wants the answer within 3 s.
        self.assertEqual(self.click(event="ev2"), "web_login_busy")  # Its follow-up has not run: a second click waits for it.
        for work, _, _ in follow_ups:
            work()
        self.assertEqual(len(self.requests), 1)
        self.assertIn("已允许", self.last())

    def test_a_page_that_took_nothing_makes_the_login_void(self):
        self.answer = {"ok": True, "upserted": 0, "skipped": 1}  # Expired or decided on the page, or not a self-hosted page.
        self.assertEqual(self.click(), "web_login_allow")
        self.assertIn("已失效", self.last())
        self.assertNotIn("已允许", self.last())
        self.assertNotIn(cards.ALLOW, self.last())
        self.assertEqual(read_object(self.state / "web" / "logins.json")[PAYLOAD["request_id"]]["state"], "void")
        self.assertEqual(self.click(event="ev2"), "web_login_done")
        self.assertEqual(len(self.requests), 1)
        self.assertIn("已失效", self.last())


class WiringTests(Case):
    def test_the_kinds_are_in_the_table_and_the_panels(self):
        for kind in cards.KINDS:
            self.assertEqual(ACTIONS[kind].handler, "")
        self.assertIn("web_login", UnifiedBridgeApp.PANELS)
        self.assertIn("web_login", UnifiedBridgeApp.PARTS)
        self.assertIn("login", web_sync.KINDS)

    def test_the_operation_sends_the_card_and_says_so(self):
        env = SimpleNamespace(runtime=SimpleNamespace(access=SimpleNamespace(owner_chat=lambda: "oc_dm"), log=self.logs.append),
                              state_dir=self.state, profile="p")
        ops = Ops(env, login=self.login)
        state, message = ops.run({"kind": "login", "op_id": "login-" + PAYLOAD["request_id"], "payload": PAYLOAD})
        self.assertEqual(state, "done")
        self.assertIn("确认卡", message)
        self.assertEqual(len(self.messenger.sent), 1)
        self.assertEqual(Ops(env).run({"kind": "login", "payload": PAYLOAD})[0], "failed")  # No WebLogin: refused, not a crash.


if __name__ == "__main__":
    unittest.main()
