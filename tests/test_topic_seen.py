"""When the owner last saw each topic (core/topic_seen.py), from the read receipts of the bot's private chat messages
(app/topic_seen.py), and the topic aliases of the answers sent on their own, which those receipts are looked up in."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support.world import UnifiedCase
from covey.app.topic_seen import read_seconds
from covey.core import topic_seen
from covey.core.topic_seen import KEEP, PINNED_TOPICS_ENV, SEEN_FILE, read_seen, record_reads
from covey.core.topics import message_topic, remember_topic_message
from covey.feishu import delivery, events
from covey.feishu.messenger import Messenger, Voice

NOW = 1_762_300_000.0


class StoreTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)

    def test_nothing_seen_without_the_file_or_with_a_broken_one(self):
        self.assertEqual(read_seen(self.state), {})
        (self.state / SEEN_FILE).write_text("{")
        self.assertEqual(read_seen(self.state), {})
        (self.state / SEEN_FILE).write_text("[1]")
        self.assertEqual(read_seen(self.state), {})

    def test_each_topic_keeps_its_latest_read(self):
        self.assertEqual(record_reads(self.state, {"t1": NOW - 60, "t2": NOW - 30}, now=NOW),
                         {"t1": NOW - 60, "t2": NOW - 30})
        self.assertEqual(record_reads(self.state, {"t1": NOW - 120, "t2": NOW - 10}, now=NOW), {"t2": NOW - 10})
        self.assertEqual(read_seen(self.state), {"t1": NOW - 60, "t2": NOW - 10})
        self.assertEqual(json.loads((self.state / SEEN_FILE).read_text()), {"t2": NOW - 10, "t1": NOW - 60})

    def test_a_read_that_changes_nothing_writes_nothing(self):
        record_reads(self.state, {"t1": NOW}, now=NOW)
        with mock.patch.object(topic_seen, "write_json") as write:
            self.assertEqual(record_reads(self.state, {"t1": NOW - 1}, now=NOW), {})
        write.assert_not_called()

    def test_what_is_not_a_topic_and_a_time_is_left_out(self):
        (self.state / SEEN_FILE).write_text(json.dumps({"t1": "1", "t2": True, "t3": -1, "": 5, "t4": NOW}))
        self.assertEqual(read_seen(self.state), {"t4": NOW})
        record_reads(self.state, {"t5": float("nan"), "t6": None, 7: NOW, "t7": True, "t8": NOW}, now=NOW)
        self.assertEqual(read_seen(self.state), {"t4": NOW, "t8": NOW})

    def test_topics_seen_long_ago_and_past_the_limit_are_dropped(self):
        record_reads(self.state, {"old": NOW - KEEP - 1, "kept": NOW - KEEP + 1}, now=NOW)
        self.assertEqual(read_seen(self.state), {"kept": NOW - KEEP + 1})
        with mock.patch.object(topic_seen, "LIMIT", 3):
            self.assertEqual(record_reads(self.state, {f"t{i}": NOW - i for i in range(5)}, now=NOW),
                             {"t0": NOW, "t1": NOW - 1, "t2": NOW - 2})
        self.assertEqual(read_seen(self.state), {"t0": NOW, "t1": NOW - 1, "t2": NOW - 2})

    def test_writers_at_once_lose_no_read(self):
        def write(n):
            for i in range(20):
                record_reads(self.state, {f"own{n}-{i}": NOW - i, "shared": NOW - 100 + n * 20 + i}, now=NOW)
        threads = [threading.Thread(target=write, args=(n,)) for n in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        seen = read_seen(self.state)
        self.assertEqual(len(seen), 101)
        self.assertEqual(seen["shared"], NOW - 100 + 4 * 20 + 19)

    def test_the_switch_is_on_only_at_1(self):
        for value, on in (("1", True), (" 1 ", True), ("yes", False), ("0", False), ("", False)):
            with mock.patch.dict(os.environ, {PINNED_TOPICS_ENV: value}):
                self.assertIs(topic_seen.enabled(), on, value)
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(topic_seen.enabled())


class ReadEventTests(UnifiedCase):
    def setUp(self):
        super().setUp()
        self.ms = int(time.time() * 1000) - 60_000  # A minute ago: within KEEP.
        self.state = self.ctx.state_dir
        self.app.runtime.access.remember_owner_chat("oc_dm")
        remember_topic_message(self.state, "t1", "oc_dm", "om_answer")
        remember_topic_message(self.state, "t1", "oc_dm", "om_task")
        remember_topic_message(self.state, "t2", "oc_dm", "om_question")
        remember_topic_message(self.state, "t3", "oc_group", "om_group")

    def read(self, *messages, reader="owner", read_time=None):
        read_time = str(self.ms) if read_time is None else read_time
        self.app.topic_seen.handle({"event_id": "ev1", "reader_id": reader, "read_time": read_time,
                                    "message_ids": list(messages)}, self.ctx)

    def test_off_unless_the_switch_is_set(self):
        with mock.patch.dict(os.environ):
            os.environ.pop(PINNED_TOPICS_ENV, None)
            self.assertNotIn("topic-seen", dict(self.app.worker_targets()))
            self.assertEqual(self.app.topic_seen.threads(), ())
        with mock.patch.dict(os.environ, {PINNED_TOPICS_ENV: "yes"}):
            self.assertNotIn("topic-seen", dict(self.app.worker_targets()))

    def test_on_its_worker_consumes_the_read_event_and_stops_with_the_others(self):
        with mock.patch.dict(os.environ, {PINNED_TOPICS_ENV: "1"}):
            target = dict(self.app.worker_targets())["topic-seen"]
        with mock.patch("covey.app.unified.consume") as loop:
            target(self.ctx)
        self.assertEqual(loop.call_args.args[1], events.READ_EVENT_KEY)
        self.assertEqual(loop.call_args.kwargs["handle"], self.app.topic_seen.handle)
        with mock.patch("covey.app.unified.stop_processes") as stop:
            self.app.stop_children()
        self.assertIn("message read event consumer", [name for name, _ in stop.call_args.args[0]])

    def test_the_owner_reading_messages_of_topics_records_each_topic(self):
        self.read("om_task", "om_answer", "om_question")
        self.assertEqual(read_seen(self.state), {"t1": self.ms / 1000, "t2": self.ms / 1000})

    def test_a_later_read_moves_the_time_on_and_an_earlier_one_does_not(self):
        self.read("om_answer")
        self.read("om_answer", read_time=str(self.ms + 30_000))
        self.read("om_answer", read_time=str(self.ms - 30_000))
        self.assertEqual(read_seen(self.state), {"t1": (self.ms + 30_000) / 1000})

    def test_someone_else_reading_records_nothing(self):
        self.read("om_answer", reader="ou_other")
        self.read("om_answer", reader="")
        self.assertFalse((self.state / SEEN_FILE).exists())

    def test_messages_of_no_topic_in_the_owners_chat_record_nothing(self):
        # The chat's top level, a menu panel, and a group topic's message (receipts name no chat).
        with mock.patch("covey.feishu.transport.call") as call:
            self.read("om_unknown", "om_group")
        call.assert_not_called()
        self.assertFalse((self.state / SEEN_FILE).exists())

    def test_nothing_before_the_owners_private_chat_is_known(self):
        (self.state / "owner-chat.json").unlink()
        self.read("om_answer")
        self.assertFalse((self.state / SEEN_FILE).exists())

    def test_a_receipt_without_a_time_or_its_messages_records_nothing(self):
        self.read("om_answer", read_time="")
        self.read("om_answer", read_time="soon")
        self.app.topic_seen.handle({"reader_id": "owner", "read_time": str(self.ms), "message_ids": "om_answer"},
                                   self.ctx)
        self.read(None, 5)
        self.assertFalse((self.state / SEEN_FILE).exists())

    def test_read_time_is_feishus_milliseconds(self):
        self.assertEqual(read_seconds("1762300000123"), 1762300000.123)
        self.assertEqual(read_seconds(1762300000000), 1762300000.0)
        for value in (None, "", "0", "-5", "1.5", "x"):
            self.assertIsNone(read_seconds(value), value)


class SubscriptionTests(unittest.TestCase):
    def test_the_consumer_asks_for_the_read_event_with_its_fields(self):
        with mock.patch.object(events, "consume_event_stream") as stream:
            events.consume(mock.Mock(profile=None), events.READ_EVENT_KEY, shutdown=mock.Mock(), set_process=print,
                           handle=print, log=print, retry_seconds=300)
        argv = stream.call_args.kwargs["argv"]
        self.assertEqual(argv[3:6], ["im.message.message_read_v1", "--as", "bot"])
        self.assertEqual(argv[argv.index("--jq") + 1], events.READ_EVENT_JQ)
        self.assertEqual(stream.call_args.kwargs["restart_delay"](1), 300)  # Without im:message it exits failed.

    @unittest.skipUnless(shutil.which("jq"), "jq not installed")
    def test_the_fields_come_out_of_the_v2_envelope(self):
        envelope = {"schema": "2.0", "header": {"event_id": "ev1", "event_type": "im.message.message_read_v1",
                                                "create_time": "1762300000200"},
                    "event": {"message_id_list": ["om_a", "om_b"],
                              "reader": {"read_time": "1762300000123", "tenant_key": "tk",
                                         "reader_id": {"open_id": "ou_reader", "union_id": "on_x", "user_id": "u"}}}}
        out = subprocess.run(["jq", "-c", events.READ_EVENT_JQ], input=json.dumps(envelope), text=True,
                             capture_output=True, check=True).stdout
        self.assertEqual(json.loads(out), {"event_id": "ev1", "reader_id": "ou_reader", "read_time": "1762300000123",
                                           "message_ids": ["om_a", "om_b"]})


class AnswerAliasTests(unittest.TestCase):
    """An answer sent on its own (app/turn_delivery.py standalone_verified, app/task_controls.py 重新投递) goes through
    Messenger.reply: each message it sends is learned as its topic's, so the owner's read receipt finds the topic."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.logs = []
        self.messenger = Messenger(self.logs.append)
        self.voice = Voice("bridge", "TEST")
        self.ctx = SimpleNamespace(profile="bot", state_dir=self.state, topic_id="t1", reply_to_message_id="om_root")

    def send(self, answers, card=None, ctx=None):
        with mock.patch.object(delivery, "_send", side_effect=answers) as send:
            sent = self.messenger.reply(ctx or self.ctx, "oc_dm", "回答正文", "answer-k", voice=self.voice, card=card)
        return sent, send

    def test_the_answer_card_is_learned_as_its_topics(self):
        sent, send = self.send([{"code": 0, "data": {"message_id": "om_card"}}], card={"schema": "2.0"})
        self.assertTrue(sent)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(message_topic(self.state, "oc_dm", "om_card"), "t1")

    def test_a_markdown_answer_in_chunks_learns_each_message(self):
        with mock.patch.object(Voice, "chunk_limit", return_value=3):
            sent, _ = self.send([{"data": {"message_id": "om_1"}}, {"data": {"messageId": "om_2"}}])
        self.assertTrue(sent)
        self.assertEqual([message_topic(self.state, "oc_dm", m) for m in ("om_1", "om_2")], ["t1", "t1"])

    def test_an_answer_whose_id_is_unknown_or_unsent_learns_nothing(self):
        self.assertTrue(self.send([{"code": 0}])[0])
        self.assertFalse(self.send([None])[0])
        self.assertFalse((self.state / "topics" / "aliases").exists())

    def test_a_message_outside_any_topic_learns_nothing(self):
        ctx = SimpleNamespace(profile="bot", state_dir=self.state, topic_id="", reply_to_message_id="")
        self.assertTrue(self.send([{"data": {"message_id": "om_top"}}], ctx=ctx)[0])
        self.assertEqual(message_topic(self.state, "oc_dm", "om_top"), "")

    def test_a_failure_to_learn_it_leaves_the_send_sent(self):
        with mock.patch("covey.feishu.messenger.remember_topic_message", side_effect=OSError):
            sent, send = self.send([{"data": {"message_id": "om_card"}}], card={"schema": "2.0"})
        self.assertTrue(sent)
        self.assertEqual(send.call_count, 1)
        self.assertIn("sent message not remembered for its topic (OSError)", self.logs)


if __name__ == "__main__":
    unittest.main()
