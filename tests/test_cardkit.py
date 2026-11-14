"""CardKit requests (feishu/cardkit.py): stable request ids for retries, and support.fake_lark.FakeCardKit seeing
every card write the Messenger makes."""
from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from covey.feishu import cardkit
from covey.feishu.cardkit import cardkit_api
from covey.feishu.messenger import Messenger, Voice
from support.fake_lark import FakeCardKit


class TransientRetryTest(unittest.TestCase):
    def test_long_event_ids_use_bounded_stable_uuid(self):
        request = {"uuid": "result-actions-" + "a" * 100}
        with mock.patch("covey.feishu.cardkit._cardkit_api_once", return_value={}) as call:
            cardkit_api("test", "POST", "/api", request, log=lambda _: None)
            first = call.call_args.args[3]["uuid"]
            cardkit_api("test", "POST", "/api", request, log=lambda _: None)
            self.assertEqual(first, call.call_args.args[3]["uuid"])
            self.assertLessEqual(len(first), 50)
            self.assertEqual(request["uuid"], "result-actions-" + "a" * 100)

    def test_idempotent_timeout_retries_same_uuid(self):
        with (
            mock.patch(
                "covey.feishu.transport.subprocess.run",
                side_effect=[
                    subprocess.TimeoutExpired("lark", 1),
                    subprocess.CompletedProcess([], 0, '{"data": {}}', ""),
                ],
            ) as run,
            mock.patch("covey.feishu.transport.time.sleep"),
        ):
            result = cardkit_api("test", "POST", "/open-apis/im/v1/messages", {"uuid": "stable"}, log=lambda _: None)
        self.assertIsNotNone(result)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0].args, run.call_args_list[1].args)

    def test_validation_failure_does_not_retry(self):
        with mock.patch(
            "covey.feishu.transport.subprocess.run",
            return_value=subprocess.CompletedProcess([], 2, "", '{"ok":false,"error":{"type":"validation","subtype":"invalid_argument"}}'),
        ) as run:
            self.assertIsNone(cardkit_api("test", "POST", "/api", {"uuid": "stable"}, log=lambda _: None))
        self.assertEqual(run.call_count, 1)

    def test_creation_without_idempotency_is_not_repeated(self):
        with mock.patch("covey.feishu.transport.subprocess.run", side_effect=subprocess.TimeoutExpired("lark", 1)) as run:
            cardkit_api("test", "POST", "/api", {}, log=lambda _: None)
        self.assertEqual(run.call_count, 1)


class FakeCardKitTest(unittest.TestCase):
    """Tests stand in for Feishu with support.fake_lark.FakeCardKit. It has to see every CardKit
    request and card message the Messenger makes, or those tests stop checking anything."""

    def test_every_messenger_card_write_reaches_the_fake(self):
        messenger, voice = Messenger(lambda _: None), Voice("unit", "UNIT")
        with FakeCardKit({"data": {"card_id": "c1"}}, sent=("om_1", False)) as lark:
            messenger.create_card("p", {"body": {"elements": []}})
            self.assertEqual(messenger.create_and_send_card("p", "chat", {"body": {}}, "k1", voice=voice), ("c1", "om_1"))
            messenger.put_card("p", "c1", {"body": {"elements": [1]}}, sequence=2, key="k2")
            messenger.write_card("p", "PATCH", "c1", "settings", {"settings": "{}"}, sequence=3, key="k3")
            messenger.send_card("p", "chat", "c1", "k4", voice=voice, reply_to_message_id="om_0")
        cards = "/open-apis/cardkit/v1/cards"
        self.assertEqual([(call.method, call.path) for call in lark.calls],
                         [("POST", cards), ("POST", cards), ("PUT", cards + "/c1"), ("PATCH", cards + "/c1/settings")])
        self.assertEqual(lark.calls[2].card, {"body": {"elements": [1]}})
        self.assertEqual((lark.calls[3].data["sequence"], lark.calls[3].data["uuid"]), (3, "k3"))
        self.assertEqual([call.args[2:4] for call in lark.send.call_args_list], [("c1", "k1"), ("c1", "k4")])
        self.assertEqual(lark.escaped, [])

    def test_a_request_around_the_fake_fails_the_test(self):
        with self.assertRaisesRegex(AssertionError, "went around FakeCardKit"):
            with FakeCardKit():
                cardkit._cardkit_api_once("p", "GET", "/open-apis/cardkit/v1/cards/c", {}, log=lambda _: None)
