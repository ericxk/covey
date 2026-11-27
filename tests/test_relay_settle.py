"""终态统一补回带 (W1-F step 6, app/butler_relay.py settle_missing): the end of a turn the butler handed on whose task
runner never relayed it is relayed by the Bridge, once: one left interrupted with nothing to go on by itself
(app/task_controls.py left_stopped, relay_unfinished), a result a redelivery got to its topic after its first send
failed (delivered_relay, Redelivery.attempted), one the outbox gave up on (paused); and a 做完了 relay a runner left
PENDING when it exited is taken by the Bridge (app/relay_send.py take_left)."""
from __future__ import annotations

import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from test_butler_relay import REPLY, TOPIC, Case
from covey.app import relay_send
from covey.app.butler_relay import relay, settle_missing, take_left
from covey.app.outbox import PAUSED_REASON
from covey.app.task_controls import TaskControls, left_stopped
from covey.cards.relay_card import DONE, STOPPED, WAITING
from covey.core.jobs import find_job_record, job_key, upsert_job_record
from covey.core.state import read_object, write_json
from covey.feishu.transport import Failure
from support.butler import CHAT

KEY = job_key("main-t1", 9)


def words(send):
    """What the one relay ``send`` was asked to send says: its text and its card's JSON."""
    import json
    chat, text, key, reply_to, card, _ = send.call_args.args
    return text + json.dumps(card, ensure_ascii=False)


class SettleCase(Case):
    def setUp(self):
        super().setUp()
        self.opened("t1", plan={"start": True}, topic_id=TOPIC)

    def record(self, **fields):
        return upsert_job_record(self.state, KEY, **{"job_id": 9, "message_id": "main-t1", "chat_id": CHAT,
                                                     "topic_id": TOPIC, "kind": "chat", "reply_to_message_id": "om_x",
                                                     **fields})

    def saved(self):
        return read_object(relay_send.relay_path(self.state, KEY))


class LeftStoppedTest(SettleCase):
    def test_an_interrupted_turn_with_nothing_to_go_on(self):
        self.assertTrue(left_stopped(self.state, self.record(status="interrupted")))

    def test_one_that_goes_on_by_itself_or_waits_on_a_form_is_not(self):
        cases = {"auto retry pending": {"auto_retry_state": "pending"},
                 "auto retry submitting": {"auto_retry_state": "submitting"},
                 "a restart's retry to plan": {"interrupted_by": "restart"},
                 "检查并继续 queued": {"retried_by": "om_retry"},
                 "a saved reply the outbox still sends": {"outbox_version": 1, "outbox_state": "pending"}}
        for name, fields in cases.items():
            with self.subTest(name):
                upsert_job_record(self.state, KEY, auto_retry_state=None, interrupted_by=None, retried_by=None,
                                  outbox_version=None, outbox_state=None)
                self.assertFalse(left_stopped(self.state, self.record(status="interrupted", **fields)))
        clean = self.record(auto_retry_state=None, interrupted_by=None, retried_by=None, outbox_version=None,
                            outbox_state=None)
        self.assertTrue(left_stopped(self.state, clean))
        self.assertFalse(left_stopped(self.state, {**clean, "status": "failed"}))  # Its own runner relayed that one.

    def test_an_open_question_keeps_it(self):
        record = self.record(status="interrupted")
        write_json(self.state / "questions" / "q1.json", {"job_id": 9, "topic_id": TOPIC, "chat_id": CHAT,
                                                          "status": "pending"})
        self.assertFalse(left_stopped(self.state, record))
        write_json(self.state / "questions" / "q1.json", {"job_id": 9, "topic_id": TOPIC, "chat_id": CHAT,
                                                          "status": "answered"})
        self.assertTrue(left_stopped(self.state, record))


class SettleMissingTest(SettleCase):
    def test_an_interrupted_turn_relays_stopped_once(self):
        record, send = self.record(status="interrupted"), self.send()
        self.assertEqual("sent", settle_missing(self.state, record, STOPPED, send=send, profile=None, log=None))
        self.assertEqual(("sent", STOPPED), (self.saved()["state"], self.saved()["outcome"]))
        self.assertIn("停住了", words(send))
        self.assertEqual("sent", settle_missing(self.state, record, STOPPED, send=send, profile=None, log=None))
        self.assertEqual(1, send.call_count)

    def test_a_redelivered_result_relays_done_with_the_reply_sent(self):
        record, send = self.record(status="done"), self.send()
        self.assertEqual("sent", settle_missing(self.state, record, DONE, send=send, profile=None, log=None,
                                                text="## 重投的正文\n就是这一份。"))
        self.assertIn("就是这一份。", words(send))
        self.assertEqual(DONE, self.saved()["outcome"])

    def test_done_without_the_reply_sent_relays_nothing(self):
        send = self.send()
        self.assertIsNone(settle_missing(self.state, self.record(status="done"), DONE, send=send, profile=None, log=None))
        send.assert_not_called()
        self.assertEqual({}, self.saved())

    def test_the_runners_own_relay_first_means_none_from_the_bridge(self):
        send = self.send()
        self.assertEqual("sent", relay(self.state, self.job(), DONE, send=send, text=REPLY))
        self.assertEqual("sent", settle_missing(self.state, self.record(status="interrupted"), STOPPED, send=send,
                                                profile=None, log=None))
        self.assertEqual(1, send.call_count)
        self.assertEqual(DONE, self.saved()["outcome"])

    def test_the_runners_relay_and_the_bridges_at_once_send_once(self):
        record, barrier, sent = self.record(status="done"), threading.Barrier(2), []

        def slow(chat, text, key, reply_to, card, on_sent):
            sent.append(key)
            time.sleep(0.05)
            on_sent("om_relay")
            return True
        send = mock.Mock(side_effect=slow)
        results = []

        def runner():
            barrier.wait()
            results.append(relay(self.state, self.job(), DONE, send=send, text=REPLY, merge=False))

        def bridge():
            barrier.wait()
            results.append(settle_missing(self.state, record, DONE, send=send, profile=None, log=None, text=REPLY))
        threads = [threading.Thread(target=runner), threading.Thread(target=bridge)]
        for each in threads:
            each.start()
        for each in threads:
            each.join()
        self.assertEqual(1, send.call_count, sent)
        self.assertIn("sent", results)

    def test_never_merged(self):
        send = self.send()
        with mock.patch.object(relay_send, "others_at_work", return_value=True), \
                mock.patch.object(relay_send.background, "start") as start:
            self.assertEqual("sent", settle_missing(self.state, self.record(status="done"), DONE, send=send,
                                                    profile=None, log=None, text=REPLY))
        start.assert_not_called()
        self.assertEqual(1, send.call_count)

    def test_a_waiting_turn_whose_question_is_copied_up_already(self):
        record, send = self.record(status="waiting_user"), self.send()
        write_json(self.state / "questions" / "q1.json", {"job_id": 9, "topic_id": TOPIC, "chat_id": CHAT,
                                                          "status": "pending", "mirror_message_id": "om_copy"})
        self.assertEqual("mirrored", settle_missing(self.state, record, WAITING, send=send, profile=None, log=None))
        send.assert_not_called()

    def test_a_stop_reason_given_is_said(self):
        send = self.send()
        settle_missing(self.state, self.record(status="failed"), STOPPED, send=send, profile=None, log=None,
                       text=PAUSED_REASON)
        self.assertIn(PAUSED_REASON, words(send))

    def test_a_turn_the_butler_did_not_hand_on_relays_nothing(self):
        send = self.send()
        upsert_job_record(self.state, job_key("om_typed", 11), job_id=11,
                          message_id="om_typed", chat_id=CHAT, topic_id=TOPIC, status="interrupted")
        self.assertIsNone(settle_missing(self.state, find_job_record(self.state, job_key("om_typed", 11)), STOPPED,
                                         send=send, profile=None, log=None))
        send.assert_not_called()

    def test_a_failure_is_a_log_line(self):
        log = mock.Mock()
        with mock.patch("covey.app.butler_relay.relay", side_effect=OSError("NAS gone")):
            self.assertIsNone(settle_missing(self.state, self.record(status="interrupted"), STOPPED, send=self.send(),
                                             profile=None, log=log))
        self.assertIn("failed: OSError", log.call_args.args[0])


class ControlsCase(SettleCase):
    """TaskControls' part (app/task_controls.py): its messenger's send_message stands for the relay's send."""

    def setUp(self):
        super().setUp()
        self.sent = []

        def send_message(profile, chat, text, key, reply_to_message_id="", voice=None, card=None, on_sent=None):
            self.sent.append((chat, text, key, card))
            on_sent(f"om_sent{len(self.sent)}")
            return True
        self.controls = TaskControls.__new__(TaskControls)
        self.controls.messenger = SimpleNamespace(send_message=send_message)
        self.controls.voice = SimpleNamespace(key=lambda tag, key: f"{tag}:{key}")
        self.controls.runtime = SimpleNamespace(log=mock.Mock())
        self.ctx = SimpleNamespace(state_dir=self.state, profile=None)


class RelayUnfinishedTest(ControlsCase):
    def test_interrupted_with_nothing_to_go_on_says_stopped_at_the_top_level(self):
        self.record(status="interrupted")
        self.assertEqual("sent", self.controls.relay_unfinished(self.ctx, KEY))
        self.assertEqual([("main-relay:" + KEY)], [each[2] for each in self.sent])
        self.assertEqual(STOPPED, self.saved()["outcome"])

    def test_a_planned_automatic_retry_says_nothing(self):
        self.record(status="interrupted", auto_retry_state="pending")
        self.assertIsNone(self.controls.relay_unfinished(self.ctx, KEY))
        self.assertEqual([], self.sent)
        self.assertEqual({}, self.saved())


class RedeliveredTest(ControlsCase):
    def test_a_result_redelivered_relays_done_with_the_reply_sent(self):
        self.record(status="done", outbox_version=1, outbox_state="pending")
        self.assertEqual("delivered", self.controls.attempted(self.ctx, KEY, None, delivered=True, text="重投的那份"))
        self.assertEqual(1, len(self.sent))
        self.assertIn("重投的那份", self.sent[0][1] + str(self.sent[0][3]))
        self.assertEqual(DONE, self.saved()["outcome"])

    def test_a_question_redelivered_relays_waiting(self):
        self.record(status="waiting_user", execution_status="waiting_user", checkpoint_path="")
        self.assertEqual("sent", self.controls.delivered_relay(self.ctx, KEY))
        self.assertEqual(WAITING, self.saved()["outcome"])

    def test_the_outbox_giving_up_says_stopped_with_why(self):
        self.record(status="done", outbox_version=1, outbox_state="pending")
        self.assertEqual("paused", self.controls.attempted(self.ctx, KEY, Failure("api_rejected")))
        self.assertEqual(1, len(self.sent))
        self.assertIn(PAUSED_REASON, self.sent[0][1] + str(self.sent[0][3]))
        self.assertEqual(STOPPED, self.saved()["outcome"])

    def test_a_send_that_may_have_landed_says_nothing_yet(self):
        self.record(status="done", outbox_version=1, outbox_state="pending")
        for failure in (Failure("reconciliation_required"), Failure("network", ambiguous=True), None):
            with self.subTest(failure):
                self.assertEqual("paused", self.controls.attempted(self.ctx, KEY, failure))
        self.assertEqual([], self.sent)
        self.assertEqual({}, self.saved())

    def test_a_retry_still_to_come_says_nothing(self):
        self.record(status="done", outbox_version=1, outbox_state="pending")
        self.assertEqual("pending", self.controls.attempted(self.ctx, KEY, Failure("rate_limited", retryable=True)))
        self.assertEqual([], self.sent)


class TakeLeftTest(SettleCase):
    def pending(self):
        """A 做完了 relay of job 9 left PENDING by a runner whose window's thread never ran."""
        with mock.patch.object(relay_send, "others_at_work", return_value=True), \
                mock.patch.object(relay_send.background, "start") as start:
            self.assertEqual(relay_send.PENDING, relay(self.state, self.job(), DONE, send=self.send(), text=REPLY))
        start.assert_called_once()
        return start.call_args.args[1]  # The window's thread, which died with its runner.

    def test_the_bridge_sends_a_relay_its_exited_runner_left_pending(self):
        self.pending()
        send = self.send()
        self.assertEqual("sent", take_left(self.state, self.job(), send=send, profile=None, log=None))
        self.assertEqual(1, send.call_count)
        chat, text, key, reply_to, card, _ = send.call_args.args
        self.assertEqual((CHAT, KEY), (chat, key))
        self.assertIn("T410", text)
        self.assertEqual("sent", self.saved()["state"])
        self.assertNotIn(KEY, read_object(self.state / relay_send.WAITING).get(CHAT, []))
        # Once: a second take sends nothing, and the window's thread, had it lived, would send nothing either.
        self.assertEqual("sent", take_left(self.state, self.job(), send=send, profile=None, log=None))
        self.assertEqual(1, send.call_count)

    def test_the_window_that_did_run_leaves_nothing_to_take(self):
        window = self.pending()
        with mock.patch.object(relay_send, "pause"), mock.patch.object(relay_send, "_send", return_value="sent") as sent:
            window()
        sent.assert_called_once()
        upsert = read_object(relay_send.relay_path(self.state, KEY))
        self.assertEqual("submitting", upsert["state"])  # As _send (stood in for) found it: taken by the window.
        send = self.send()
        self.assertEqual("submitting", take_left(self.state, self.job(), send=send, profile=None, log=None))
        send.assert_not_called()

    def test_nothing_pending_nothing_sent(self):
        send = self.send()
        self.assertIsNone(take_left(self.state, self.job(), send=send, profile=None, log=None))
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
