"""One notice for several topics stopped on the same cause with an automatic retry planned (app/limit_notice.py)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import support  # Puts the repository on the path.
from covey.app import limit_notice
from covey.core.jobs import upsert_job_record

del support
NOW = 1_800_000_000.0
CHAT = "oc_fake_owner_chat"


class NoticeTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.state = Path(self.dir.name)
        self.sent, self.logs = [], []
        send = mock.Mock(side_effect=lambda profile, chat, text, key, **kw: self.sent.append((chat, text, key)) or True)
        self.gateway = SimpleNamespace(
            runtime=SimpleNamespace(access=SimpleNamespace(owner_chat=lambda: CHAT), log=self.logs.append),
            messenger=SimpleNamespace(send_message=send), voice=SimpleNamespace(safe_key=lambda key: key),
            apps={"claude": SimpleNamespace(spec=SimpleNamespace(title="Claude Code"))})
        self.ctx = SimpleNamespace(state_dir=self.state, profile="p")
        limit_notice._last.clear()

    def stop(self, number, *, at, chat=CHAT, kind="rate_limited", backend="claude", retried=0, due=None, **extra):
        upsert_job_record(self.state, f"job-{number}", job_id=number, topic_id=f"topic-{number}", chat_id=chat,
                          backend=backend, status="failed", auto_retry_state="pending", auto_retry_kind=kind,
                          auto_retry_at=due or at + 600, auto_retry_attempt=1, auto_retry=retried,
                          timings={"execution_finished": at}, **extra)

    def tick(self, now):
        limit_notice.tick(self.gateway, self.ctx, now=now)

    def test_one_stop_says_nothing_and_two_make_one_notice_once_the_burst_settles(self):
        self.stop(1, at=NOW)
        self.tick(NOW + 100)
        self.assertEqual(self.sent, [])
        self.stop(2, at=NOW + 20, auto_retry_reset_at=NOW + 3000)
        self.tick(NOW + 40)  # Too soon after the last one: a third may come.
        self.assertEqual(self.sent, [])
        self.stop(3, at=NOW + 50)
        self.tick(NOW + 100)
        self.assertEqual(self.sent, [])
        self.tick(NOW + 130)
        [(chat, text, key)] = self.sent
        self.assertEqual(chat, CHAT)
        self.assertIn("3 个话题同样停在模型服务限流**（Claude Code）", text)
        self.assertEqual(text.count("\n- "), 3)
        self.assertIn("限流预计", text)
        self.assertIn("现在重试", text)
        saved = json.loads((self.state / "limit-notices" / next(iter((self.state / "limit-notices").iterdir())).name).read_text())
        self.assertEqual((saved["state"], len(saved["topics"])), ("sent", 3))
        # Never again for that window, a later stop of it included.
        self.stop(4, at=NOW + 200)
        self.tick(NOW + 300)
        self.tick(NOW + 700)
        self.assertEqual(len(self.sent), 1)

    def test_a_stop_after_the_window_makes_a_new_one(self):
        self.stop(1, at=NOW)
        self.stop(2, at=NOW + 10)
        self.tick(NOW + 100)
        self.assertEqual(len(self.sent), 1)
        self.stop(3, at=NOW + limit_notice.WINDOW + 100)
        self.tick(NOW + limit_notice.WINDOW + 300)
        self.assertEqual(len(self.sent), 1)  # One stop alone.
        self.stop(4, at=NOW + limit_notice.WINDOW + 120)
        self.tick(NOW + limit_notice.WINDOW + 400)
        self.assertEqual(len(self.sent), 2)
        self.assertIn("2 个话题", self.sent[1][1])

    def test_only_what_the_notice_is_about_counts(self):
        self.stop(1, at=NOW, chat="oc_fake_other_chat")
        self.stop(2, at=NOW, retried=2)  # A retry that failed again: its topic was told.
        self.stop(3, at=NOW, kind="incomplete")
        self.stop(4, at=NOW)
        self.tick(NOW + 100)
        self.assertEqual(self.sent, [])
        self.stop(5, at=NOW, kind="server_fault")
        self.stop(6, at=NOW, kind="server_fault")
        self.tick(NOW + 200)
        [(_, text, _)] = self.sent
        self.assertIn("2 个话题同样停在模型服务临时故障", text)

    def test_a_send_that_was_not_confirmed_is_never_sent_again(self):
        self.gateway.messenger.send_message = mock.Mock(return_value=False)
        self.stop(1, at=NOW)
        self.stop(2, at=NOW)
        self.tick(NOW + 100)
        self.tick(NOW + 200)
        self.assertEqual(self.gateway.messenger.send_message.call_count, 1)
        [name] = (self.state / "limit-notices").iterdir()
        self.assertEqual(json.loads(name.read_text())["state"], "uncertain")

    def refusing(self, how):
        """The messenger's send_message failing, its last_outcome ``how`` (feishu/messenger.py)."""
        send = mock.Mock(side_effect=lambda profile, chat, text, key, **kw: self.sent.append((chat, text, key)) and False)
        self.gateway.messenger = SimpleNamespace(send_message=send, last_outcome=lambda: how)
        return send

    def record(self):
        [name] = (self.state / "limit-notices").iterdir()
        return json.loads(name.read_text())

    def test_a_notice_feishu_refused_goes_again_under_its_key_with_its_text_at_most_three_more_times(self):
        """WAVE12 C10 (T904): one that surely did not go is ``failed`` and tried again RETRY_GAP later, the same key and words."""
        send = self.refusing("not_sent")
        self.stop(1, at=NOW)
        self.stop(2, at=NOW)
        self.tick(NOW + 100)
        self.assertEqual((self.record()["state"], self.record()["tries"]), ("failed", 1))
        self.tick(NOW + 100 + limit_notice.RETRY_GAP - 1)
        self.assertEqual(send.call_count, 1)
        for n in range(1, limit_notice.RETRIES + 3):
            self.tick(NOW + 100 + n * limit_notice.RETRY_GAP)
        self.assertEqual(send.call_count, 1 + limit_notice.RETRIES)
        self.assertEqual(len({(text, key) for _, text, key in self.sent}), 1)
        self.assertEqual(self.record()["state"], "failed")
        # Then one that goes: sent.
        self.gateway.messenger.send_message = mock.Mock(return_value=True)
        self.tick(NOW + 100 + 9 * limit_notice.RETRY_GAP)
        self.gateway.messenger.send_message.assert_not_called()  # Its tries are spent: never again.

    def test_a_refused_notice_that_goes_the_next_time_is_sent(self):
        self.refusing("not_sent")
        self.stop(1, at=NOW)
        self.stop(2, at=NOW)
        self.tick(NOW + 100)
        self.gateway.messenger.send_message = mock.Mock(return_value=True)
        self.tick(NOW + 100 + limit_notice.RETRY_GAP)
        self.assertEqual(self.record()["state"], "sent")
        self.assertEqual(self.gateway.messenger.send_message.call_args.args[2], self.sent[0][1])

    def test_one_that_may_have_gone_is_never_sent_again(self):
        send = self.refusing("uncertain")
        self.stop(1, at=NOW)
        self.stop(2, at=NOW)
        for n in range(6):
            self.tick(NOW + 100 + n * limit_notice.RETRY_GAP)
        self.assertEqual((send.call_count, self.record()["state"]), (1, "uncertain"))

    def test_a_notice_left_sending_by_a_bridge_that_stopped_goes_again_under_its_key(self):
        """The Bridge stopped mid-send: ``sending`` stays; SENDING_STALE seconds on it goes again under its key, which
        Feishu takes once. One an older release left (no text kept) is never sent again."""
        self.stop(1, at=NOW)
        self.stop(2, at=NOW)
        group = limit_notice.group_key(CHAT, "claude", "rate_limited")
        path = limit_notice.path(self.state, group)
        path.parent.mkdir(parents=True)
        left = {"chat_id": CHAT, "backend": "claude", "kind": "rate_limited", "anchor": NOW, "state": "sending",
                "topics": ["topic-1", "topic-2"], "jobs": [1, 2], "text": "两个话题同样停在模型服务限流", "sending_at": NOW + 60,
                "tries": 1}
        path.write_text(json.dumps(left))
        self.tick(NOW + 60 + limit_notice.SENDING_STALE)
        self.assertEqual(self.sent, [])  # Not yet: it may still be on its way.
        self.tick(NOW + 61 + limit_notice.SENDING_STALE + limit_notice.TICK)
        self.assertEqual(self.sent, [(CHAT, left["text"], f"limit-notice-{group}-{int(NOW)}")])
        self.assertEqual((self.record()["state"], self.record()["tries"]), ("sent", 2))
        path.write_text(json.dumps({key: value for key, value in left.items() if key != "text"}))
        self.tick(NOW + 500)
        self.assertEqual(len(self.sent), 1)

    def test_off_nothing_is_read_or_sent(self):
        self.stop(1, at=NOW)
        self.stop(2, at=NOW)
        with mock.patch.dict("os.environ", {"COVEY_LIMIT_NOTICE": "0"}):
            self.tick(NOW + 100)
        self.assertEqual(self.sent, [])
        self.assertFalse((self.state / "limit-notices").exists())

    def test_a_long_list_is_cut_and_a_failing_step_never_raises(self):
        for number in range(1, 12):
            self.stop(number, at=NOW)
        self.tick(NOW + 100)
        self.assertIn("…另有 3 个", self.sent[0][1])
        self.gateway.runtime.access = None
        limit_notice._last.clear()
        self.tick(NOW + 200)
        self.assertTrue(any("limit notice failed" in line for line in self.logs))


if __name__ == "__main__":
    unittest.main()
