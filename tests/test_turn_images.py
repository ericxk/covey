"""Images a running task shows its owner at once (app/turn_images.py; T476, 2026-11-10: its QR codes, sent by the
model itself with lark-cli, landed in the thread of a relay card, not the topic's, and the owner never saw them)."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support import make_context, make_job
from covey.app import turn_images
from covey.app.turn_images import FIELD, MOST, TurnImages, wanted
from covey.core.jobs import find_job_record, job_key
from covey.core.longtask import TaskMonitor
from covey.core.main_thread import main_topic_id
from covey.core.state import write_json
from covey.feishu.messenger import Messenger, Voice

CHAT = "oc_owner"
PNG = b"\x89PNG\r\n\x1a\n fake"


class Case(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.ctx = make_context(self.root, reply_to_message_id="om_topic_root")
        self.ctx.state_dir.mkdir(parents=True)
        write_json(self.ctx.state_dir / "topics" / "t476" / "topic.json",
                   {"chat_id": CHAT, "root_id": "om_topic_root", "topic_number": 476})
        self.image = self.root / "qr.png"
        self.image.write_bytes(PNG)
        self.messenger = mock.Mock()
        self.messenger.send_image.side_effect = lambda profile, chat, path, key, **kwargs: f"om_img_{len(self.sent())}"
        self.links = mock.Mock(return_value="https://applink.feishu.cn/client/thread/open?open_thread_id=omt_t476")
        sync = mock.patch.object(turn_images.background, "start", side_effect=lambda kind, fn, *args: fn(*args))
        sync.start()
        self.addCleanup(sync.stop)
        self.logs = []

    def images(self, topic="t476", message="om_turn"):
        job = make_job(job_id=3690, message_id=message, chat_id=CHAT, topic_id=topic)
        return TurnImages(self.ctx, job, messenger=self.messenger, voice=mock.Mock(), log=self.logs.append,
                          link=self.links)

    def sent(self):
        """(where, what) of each send: where the reply goes ('' the top level), what ("note: …" or the image's name)."""
        calls = []
        for call in self.messenger.mock_calls:
            name, args, kwargs = call
            if name == "send_message":
                calls.append((kwargs.get("reply_to_message_id", ""), "note: " + args[2]))
            elif name == "send_image":
                calls.append((kwargs.get("reply_to_message_id", ""), args[2].name))
        return calls


class WantedTest(Case):
    def test_only_absolute_image_files_there_and_not_too_many(self):
        (self.root / "notes.txt").write_text("x")
        big = self.root / "big.png"
        with big.open("wb") as handle:
            handle.truncate(turn_images.MAX_BYTES + 1)
        listed = [str(self.image), {"path": str(self.image), "note": "  扫码  登录 ", "urgent": True},
                  "qr.png", str(self.root / "notes.txt"), str(self.root / "gone.png"), str(big), {"note": "x"}, 7]
        self.assertEqual(wanted({FIELD: listed}), [(self.image, "", False), (self.image, "扫码 登录", True)])
        self.assertEqual(len(wanted({FIELD: [str(self.image)] * (MOST + 3)})), MOST)
        self.assertEqual(wanted({FIELD: "x"}), [])
        self.assertEqual(wanted({}), [])


class SendTest(Case):
    def test_a_topic_turns_image_goes_to_its_thread_with_its_line(self):
        self.images()({FIELD: [{"path": str(self.image), "note": "SeedHarbor 登录二维码（5 分钟有效）"}]})
        self.assertEqual(self.sent(), [("om_topic_root", "note: SeedHarbor 登录二维码（5 分钟有效）"),
                                       ("om_topic_root", "qr.png")])

    def test_an_urgent_one_goes_to_the_chat_too_naming_and_linking_its_topic(self):
        self.images()({FIELD: [{"path": str(self.image), "note": "扫码登录", "urgent": True}]})
        sent = self.sent()
        self.assertEqual(sent[:2], [("om_topic_root", "note: 扫码登录"), ("om_topic_root", "qr.png")])
        (where, line), image = sent[2], sent[3]
        self.assertEqual(where, "")
        self.assertTrue(line.startswith("note: T476 · 扫码登录（[到话题里看 ›](https://applink.feishu.cn/"), line)
        self.assertEqual(image, ("", "qr.png"))

    def test_a_thread_send_that_failed_goes_to_the_chat(self):
        self.messenger.send_image.side_effect = lambda profile, chat, path, key, **kwargs: (
            "" if kwargs.get("reply_to_message_id") else "om_top")
        self.images()({FIELD: [str(self.image)]})
        self.assertEqual([where for where, _ in self.sent()], ["om_topic_root", "om_topic_root", "", ""])
        self.assertTrue(any("thread=- top=om_top" in line for line in self.logs), self.logs)

    def test_the_main_threads_own_go_to_the_chat_only(self):
        self.images(topic=main_topic_id(CHAT))({FIELD: [{"path": str(self.image), "note": "看这张", "urgent": True}]})
        self.assertEqual(self.sent(), [("", "note: 看这张"), ("", "qr.png")])  # No thread under his message (D1).

    def test_each_image_once_a_restart_too_and_a_file_written_again_is_new(self):
        checkpoint = {FIELD: [str(self.image)]}
        sender = self.images()
        sender(checkpoint)
        sender(checkpoint)
        self.assertEqual(len(self.sent()), 2)
        record = find_job_record(self.ctx.state_dir, job_key("om_turn", 3690))
        self.assertEqual(len(record["shown_images"]), 1)
        self.images()(checkpoint)  # A restarted runner: the record says it went.
        self.assertEqual(len(self.sent()), 2)
        stat = self.image.stat()
        os.utime(self.image, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9))  # A new QR code in the same file.
        sender(checkpoint)
        self.assertEqual(len(self.sent()), 4)

    def test_a_failure_never_reaches_the_turn(self):
        self.messenger.send_message.side_effect = RuntimeError("boom")
        self.images()({FIELD: [str(self.image)]})
        self.assertTrue(any("turn image failed" in line for line in self.logs))


class MonitorTest(Case):
    def test_the_monitor_tells_each_changed_checkpoint_and_once_more_as_it_stops(self):
        checkpoint = self.root / "checkpoint.json"
        seen = []
        job = make_job(backend_process=SimpleNamespace(poll=lambda: None))
        monitor = TaskMonitor(job, checkpoint, lambda *_: None, lambda **_: None, lambda *_: True, lambda _: None,
                              lambda: None, images=seen.append)
        monitor.tick()
        write_json(checkpoint, {"phase": "等扫码", FIELD: [str(self.image)]})
        monitor.tick()
        monitor.tick()  # Unchanged: not told again.
        self.assertEqual([cp.get("phase") for cp in seen], ["等扫码"])
        write_json(checkpoint, {"phase": "已发二维码", FIELD: [str(self.image)]})  # Written after the last tick.
        monitor.thread.start()
        monitor.stop()
        self.assertEqual(seen[-1]["phase"], "已发二维码")


class MessengerTest(unittest.TestCase):
    def test_an_image_goes_in_the_thread_of_its_reply_under_a_key_run_in_its_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "qr.png"
            path.write_bytes(PNG)
            done = subprocess.CompletedProcess([], 0, json.dumps({"data": {"message_id": "om_image"}}), "")
            with mock.patch("covey.feishu.messenger.subprocess.run", return_value=done) as run:
                got = Messenger(lambda _: None).send_image("unit", CHAT, path, "img-1", voice=Voice("lark", "LARK"),
                                                           reply_to_message_id="om_root")
            self.assertEqual(got, "om_image")
            argv, kwargs = run.call_args.args[0], run.call_args.kwargs
            self.assertEqual(kwargs["cwd"], directory)
            for part in ("+messages-reply", "--reply-in-thread", "--image", "qr.png", "--idempotency-key"):
                self.assertIn(part, argv)
            failed = subprocess.CompletedProcess([], 1, "", "boom")
            with mock.patch("covey.feishu.messenger.subprocess.run", return_value=failed):
                self.assertEqual(Messenger(lambda _: None).send_image("unit", CHAT, path, "img-2", voice=Voice("lark", "LARK")), "")


if __name__ == "__main__":
    unittest.main()
