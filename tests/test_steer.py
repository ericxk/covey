"""Supplements: a message for a running task reaches its turn once.

The supplement form, delivery states and receipts, the Claude and remote controls, and
messages that queue behind a task and turn into a supplement of the right turn.
"""

from __future__ import annotations

import dataclasses
import hashlib
import itertools
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support import quiet
from support.cards import remember_browse_card, text_of, walk_dicts
from support.fake_backends import RecordingBackend
from support.fake_lark import FakeCardKit
from support.processes import leftover_check
from support.world import QuestionFixture, TopicFixture, acknowledging_lanes, harness_app, wait_for
from support.records import run_files

from covey.app import detached, runner_ipc, runner_spawn
from covey.app.submission import queue_hint
from covey.backends.claude.control import ClaudeControl
from support import require_extension

require_extension()  # Imports the company extension below.
from covey_internal.maso.control import MasoControl
from covey.cards.steer_receipts import (CHECK_KIND, REQUEUE_KIND, WITHDRAW_KIND, history_lines, receipt_card,
                                                 stage_line)
from covey.app.steer_forms import STEER_CLOSED
from covey.app.steer_receipts_flow import REQUEUE_IN_FLIGHT
from covey.cards.streaming import STEER_LABEL, build_stop_action_row
from covey.core import run_dir as rd
from covey.core.deliveries import (REQUEUE, delivery_path, end_unread, offers, read_delivery, reconcile,
                                            save_delivery, update_delivery)
from covey.core.harness import HarnessCapabilities, JobCancelled
from covey.core.jobs import find_job_record, upsert_job_record
from covey.core.steer import SteerNotReady, SteerRefused, SteerUncertain
from covey.app.runner_ipc import RunnerLink

setUpModule, tearDownModule = leftover_check()


class ClaudeSteerTest(unittest.TestCase):
    def control(self):
        sent, events = [], []
        process = SimpleNamespace(send=sent.append)
        state = SimpleNamespace(submit=lambda _: None, session_id="s")
        return ClaudeControl(process, state, lambda kind, data: events.append((kind, data))), sent, events

    def test_refusals_send_nothing_and_say_why(self):
        control, sent, _ = self.control()
        with self.assertRaisesRegex(SteerNotReady, "刚开始"):
            control.steer("more", "e1")  # Before Claude acknowledged the first input: held, not refused.
        control.accepting_steer = True
        control.user_requests.add("q")
        with self.assertRaisesRegex(SteerRefused, "回答"):
            control.steer("more", "e1")
        control.user_requests.clear()
        control.accepting_steer, control.closing = False, True
        with self.assertRaisesRegex(SteerRefused, "收尾") as closing:
            control.steer("more", "e1")
        self.assertNotIsInstance(closing.exception, SteerNotReady)
        self.assertEqual(sent, [])

    def test_a_pending_question_marks_the_turn_as_awaiting_an_answer(self):
        control, _, _ = self.control()
        self.assertFalse(control.awaiting_answer)
        control.user_requests.add("q")
        self.assertTrue(control.awaiting_answer)

    def test_the_replayed_supplement_reports_that_the_model_took_it(self):
        control, sent, events = self.control()
        control.accepting_steer = True
        control.steer("more", "e1")
        input_id = sent[0]["uuid"]
        control.accept({"type": "user", "uuid": "other"})
        control.accept({"type": "user", "uuid": input_id})
        control.accept({"type": "user", "uuid": input_id})
        self.assertEqual(events, [("steer_read", {"event_id": "e1"})])


class AwaitingAnswerTest(unittest.TestCase):
    def test_claude_and_maso_report_a_pending_question(self):
        for cls, field in ((ClaudeControl, "user_requests"), (MasoControl, "questions")):
            control = cls.__new__(cls)
            setattr(control, field, {})
            self.assertFalse(control.awaiting_answer)
            setattr(control, field, {"q": {}})
            self.assertTrue(control.awaiting_answer)


class RemoteSteerTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = run_files(Path(tmp.name), folders=(rd.INBOX, rd.ACKS))

    def answer(self, **ack):
        def respond():
            for _ in range(200):
                ops = list((self.directory / rd.INBOX).glob("*.json"))
                if ops:
                    op = json.loads(ops[0].read_text())
                    ops[0].unlink()
                    rd.ack(self.directory, op["id"], **ack)
                    return
                time.sleep(0.02)
        thread = threading.Thread(target=respond)
        thread.start()
        self.addCleanup(thread.join, 5)

    def test_a_runner_refusal_and_failure_stay_apart(self):
        control = runner_ipc.RemoteControl(self.directory, True)
        self.answer(ok=False, error="任务正在等待回答问题，请先回答", refused=True)
        with self.assertRaisesRegex(SteerRefused, "等待回答"):
            control.steer("more", "e1")
        self.answer(ok=False, error="RuntimeError")  # A runner from before "refused": a failure.
        with self.assertRaises(SteerUncertain):
            control.steer("more", "e2")
        self.answer(ok=False, error="当前不能补充")  # ... and its refusal message.
        with self.assertRaises(SteerRefused):
            control.steer("more", "e3")
        self.answer(ok=True, held=True)  # The runner keeps it until its task can take it.
        self.assertEqual(control.steer("more", "e4"), "held")

    def test_a_late_acknowledgement_settles_an_unconfirmed_delivery(self):
        path = self.directory / "delivery.json"
        record = {"status": "uncertain", "op": "123-abcd", "run_dir": str(self.directory), "text": "more", "job_id": 7}
        save_delivery(path, record)
        self.assertEqual(reconcile(path, dict(record))["status"], "uncertain")
        rd.ack(self.directory, "123-abcd", True)
        self.assertEqual(reconcile(path, dict(record))["status"], "sent")
        self.assertEqual(read_delivery(path)["status"], "sent")
        # A settled record never moves back, whatever a stale copy says.
        (self.directory / rd.ACKS / "123-abcd.json").unlink()
        (self.directory / rd.INBOX / "123-abcd.json").write_text("{}")  # By hand: a stale copy, no op id in it.
        run_files(self.directory, exit={})
        self.assertEqual(reconcile(path, dict(record))["status"], "sent")
        # A runner that ended with the request still in its inbox never took it.
        save_delivery(path, record)
        self.assertEqual(reconcile(path, dict(record))["status"], "refused")


class DeliveryTransitionTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "delivery.json"
        save_delivery(self.path, {"status": "submitting", "text": "x"})

    def test_a_record_never_moves_back_and_stamps_sent_once(self):
        self.assertEqual(update_delivery(self.path, status="read", read_at=2.0)["status"], "read")
        sent_at = read_delivery(self.path)["sent_at"]
        self.assertIsNone(update_delivery(self.path, status="sent"))  # Claude's replay came first.
        self.assertIsNone(update_delivery(self.path, status="held"))
        self.assertEqual(read_delivery(self.path)["status"], "read")
        self.assertEqual(read_delivery(self.path)["sent_at"], sent_at)
        self.assertEqual(update_delivery(self.path, receipt={"kind": "browse"})["receipt"], {"kind": "browse"})

    def test_held_only_replaces_submitting_and_final_states_stay(self):
        self.assertEqual(update_delivery(self.path, status="held")["status"], "held")
        self.assertEqual(update_delivery(self.path, status="withdrawn")["status"], "withdrawn")
        for status in ("sent", "read", "refused", "uncertain", "held"):
            self.assertIsNone(update_delivery(self.path, status=status))
        self.assertIsNone(update_delivery(self.path.with_name("missing.json"), status="sent"))

    def test_a_task_that_ended_marks_only_its_unread_supplements(self):
        # #1773 was stopped at 12:36 with a supplement still "sent": its receipt kept saying 已补充到.
        root = self.path.parent
        records = {"unread": {"status": "sent"}, "read": {"status": "read"}, "merged": {"status": "sent", "merged": True},
                   "other": {"status": "sent", "job_key": "om_other"}}
        for name, record in records.items():
            save_delivery(root / "interaction-deliveries" / (name + ".json"),
                          {"job_key": "om_run", "job_id": 1773, "text": "x", **record})
        ended = end_unread(root, "om_run")
        self.assertEqual([r["status"] for r in ended], ["sent"])
        unread = read_delivery(root / "interaction-deliveries" / "unread.json")
        self.assertTrue(unread["target_ended"])
        self.assertIn("模型读到", unread["error"])
        for name in ("read", "merged", "other"):
            self.assertNotIn("target_ended", read_delivery(root / "interaction-deliveries" / (name + ".json")))
        self.assertEqual(end_unread(root, "om_run"), [])  # Once.
        late = update_delivery(root / "interaction-deliveries" / "unread.json", status="read", read_at=3.0)
        self.assertNotIn("target_ended", late)  # A read after all wins.

    def test_the_end_of_a_task_skips_records_written_before_it_started(self):
        root = self.path.parent
        old, new = root / "interaction-deliveries" / "old.json", root / "interaction-deliveries" / "new.json"
        for path in (old, new):
            save_delivery(path, {"job_key": "om_run", "job_id": 1, "text": "x", "status": "sent"})
        started = time.time()
        os.utime(old, (started - 3600, started - 3600))  # Last written an hour before the turn began.
        self.assertEqual(len(end_unread(root, "om_run", started)), 1)
        self.assertNotIn("target_ended", read_delivery(old))
        self.assertTrue(read_delivery(new)["target_ended"])


class ReceiptCardTest(unittest.TestCase):
    def test_the_timeline_reads_in_the_order_things_happened(self):
        # #277 listed 等任务准备好 last though it came second.
        record = {"status": "read", "job_id": 277, "text": "x", "saved_at": 100.0, "held_at": 100.5, "sent_at": 107.0,
                  "read_at": 112.0, "topic_id": "t", "token": "a" * 64}
        line = history_lines(record)
        self.assertEqual([line.index(w) for w in ("已保存", "等任务准备好", "后端已接收", "模型已读取")],
                         sorted(line.index(w) for w in ("已保存", "等任务准备好", "后端已接收", "模型已读取")))

    def test_a_received_supplement_says_the_model_reads_it_once_its_step_ends(self):
        # Repo review U6: #1052 and #1078 were read 45 minutes after "✓ 后端已接收": both came while a foreground
        # subagent ran, and Claude Code takes queued input in only once the step it is on returns.
        record = {"status": "sent", "job_id": 1190, "text": "x", "saved_at": 1.0, "sent_at": 2.0,
                  "topic_id": "t", "token": "a" * 64}
        card = text_of(receipt_card(record))
        self.assertIn("**已补充到 #1190** · 后端已接收 · 当前一步结束后读到", card)
        self.assertIn("一个子任务）结束后才读到它", card)
        read = text_of(receipt_card({**record, "status": "read", "read_at": 3.0}))
        self.assertIn("· 模型已读取", read)
        self.assertNotIn("当前一步结束后读到", read)

    def test_one_line_with_details_and_a_check_button_only_while_unconfirmed(self):
        record = {"status": "sent", "job_id": 252, "text": "第一行\n第二行", "saved_at": 1.0, "sent_at": 2.0,
                  "topic_id": "t", "token": "a" * 64}
        card = receipt_card(record, source="来源：#253 排队的消息")
        self.assertIn("**已补充到 #252**，不再单独排队", text_of(card))
        self.assertIn("后端已接收", text_of(card))
        self.assertNotIn(CHECK_KIND, text_of(card))
        self.assertNotIn("排队中", text_of(card))
        self.assertIn(CHECK_KIND, text_of(receipt_card({**record, "status": "uncertain", "error": "超时"})))
        read = receipt_card({**record, "status": "read", "read_at": 3.0})
        self.assertIn("模型已读取", text_of(read))
        ended = receipt_card({**record, "status": "uncertain", "target_ended": True, "requeue": {"text": "x"}})
        self.assertIn(REQUEUE_KIND, text_of(ended))

    def test_a_supplement_its_task_ended_without_reading_offers_requeue(self):
        record = {"status": "sent", "job_id": 1773, "text": "x", "saved_at": 1.0, "sent_at": 2.0, "ended_at": 3.0,
                  "target_ended": True, "topic_id": "t", "token": "a" * 64, "requeue": {"text": "x"}}
        card = text_of(receipt_card(record))
        self.assertIn("任务 #1773 已结束，模型没有读到这条补充", card)
        self.assertNotIn("已补充到", card)
        self.assertIn("任务结束，模型未读取", card)
        self.assertIn(REQUEUE_KIND, card)
        self.assertNotIn(REQUEUE_KIND, text_of(receipt_card({**record, "target_ended": False})))
        self.assertEqual(stage_line(record), "任务已结束，模型没有读到")  # The text reply of a click.
        self.assertEqual(stage_line({**record, "target_ended": False}), "后端已接收 · 当前一步结束后读到")
        self.assertEqual(stage_line({**record, "merged": True}), "后端已接收 · 当前一步结束后读到")
        unread = {**record, "target_ended": False, "reads": True}
        self.assertIn(CHECK_KIND, text_of(receipt_card(unread)))  # Its task may end outside the worker.
        self.assertNotIn(CHECK_KIND, text_of(receipt_card({**unread, "reads": False})))
        self.assertNotIn(CHECK_KIND, text_of(receipt_card({**unread, "merged": True})))
        self.assertNotIn(CHECK_KIND, text_of(receipt_card(record)))

    def test_offers_follow_the_record(self):
        from covey.core.deliveries import CHECK, REQUEUE, WITHDRAW, offers
        again = {"text": "x"}
        self.assertEqual(offers({"status": "held", "requeue": again}), {WITHDRAW})
        self.assertEqual(offers({"requeue": again}), {CHECK})  # No status yet: submitting.
        self.assertEqual(offers({"status": "uncertain", "target_ended": True, "requeue": again}), {CHECK, REQUEUE})
        self.assertEqual(offers({"status": "sent", "reads": True}), {CHECK})
        self.assertEqual(offers({"status": "sent", "reads": True, "merged": True}), set())
        self.assertEqual(offers({"status": "sent", "reads": True, "target_ended": True, "requeue": again}), {REQUEUE})
        self.assertEqual(offers({"status": "withdrawn", "requeue": again}), {REQUEUE})
        self.assertEqual(offers({"status": "refused"}), set())  # Nothing to queue again.
        for done in ({"requeue_state": "uncertain"}, {"requeue_state": "queued", "requeued_job_id": 8}):
            self.assertEqual(offers({"status": "refused", "requeue": again, **done}), set())

    def test_the_receipt_shows_the_buttons_its_record_offers(self):
        from covey.core.deliveries import CHECK, REQUEUE, WITHDRAW, offers
        kinds = ((WITHDRAW, WITHDRAW_KIND), (CHECK, CHECK_KIND), (REQUEUE, REQUEUE_KIND))
        for status, merged, ended, reads, again, state, requeued in itertools.product(
                ("submitting", "held", "sent", "read", "refused", "uncertain", "withdrawn"), (False, True),
                (False, True), (False, True), (None, {"text": "x"}), ("", "submitting", "uncertain", "queued"),
                (None, 8)):
            record = {"status": status, "merged": merged, "target_ended": ended, "reads": reads, "requeue": again,
                      "requeue_state": state, "requeued_job_id": requeued, "job_id": 7, "text": "x", "token": "a" * 64}
            shown = [node["value"]["kind"] for node in walk_dicts(receipt_card(record)) if node.get("tag") == "button"]
            self.assertEqual(shown, [kind for offer, kind in kinds if offer in offers(record)], record)

    def test_held_and_requeued_notes_reach_the_details_panel(self):
        record = {"status": "held", "job_id": 31, "text": "x", "saved_at": 1.0, "topic_id": "t", "token": "a" * 64}
        held = text_of(receipt_card(record))
        self.assertIn("送入前可以撤回", held)
        self.assertIn(WITHDRAW_KIND, held)
        requeued = text_of(receipt_card({**record, "status": "refused", "requeue": {"text": "x"}, "requeued_job_id": 32}))
        self.assertIn("已重新排队为任务 #32", requeued)
        self.assertNotIn(REQUEUE_KIND, requeued)


class FormSteerTest(TopicFixture, unittest.TestCase):
    """The #252 case: a supplement refused at the start of a task can simply be submitted again."""

    def setUp(self):
        super().setUp()
        self.fast_polling()
        self.receive("om_run", "task")
        self.job = self.app.runtime.jobs.queue.get_nowait()
        self.job.execution_started = True
        self.control = SimpleNamespace(steer=mock.Mock(side_effect=[SteerRefused("任务正在等待回答问题，请先回答"),
                                                                    None]), supports_steer=True)
        self.job.backend_control = self.control
        self.job.progress = SimpleNamespace(card_id="task-card", message_id="task-msg", sequence=1, element_id="answer")
        self.app.runtime.jobs.register(self.job)
        self.ctx_topic = self.topic("om_run")
        self.app.cards.set_task_element = mock.Mock(return_value=True)
        remember_browse_card(self.app, self.ctx_topic, "form-card", "cf")
        self.app.messenger.publish_card = mock.Mock(return_value=True)
        self.value = {"kind": "bridge_steer", "topic_id": self.job.topic_id, "job_id": self.job.job_id}
        self.addCleanup(self.app.runtime.unregister_reply_job, self.job)  # Ends any sender still holding a supplement.
        self.path = delivery_path(self.ctx_topic.state_dir, self.ctx_topic.topic_id, "steer-form-form-card")

    def status(self):
        return (read_delivery(self.path) or {}).get("status")

    def press(self, kind, event_id):
        record = read_delivery(self.path)
        self.app.interaction.interaction_action(self.ctx_topic, {"chat_id": "oc_chat", "event_id": event_id, "message_id": "form-card",
            "action_value": {"kind": kind, "topic_id": self.ctx_topic.topic_id, "token": record["token"],
                             "job_id": self.job.job_id}})

    def submit(self, event_id):
        self.app.interaction.interaction_action(self.ctx_topic, {"chat_id": "oc_chat", "event_id": event_id, "message_id": "form-card",
                                                     "action_value": self.value, "form_value": {"supplement": "加一节对比"}})
        return self.app.messenger.publish_card.call_args

    def test_refused_keeps_the_text_on_the_form_and_the_next_submit_delivers_it(self):
        call = self.submit("e1")
        card = text_of(call.args[3])
        self.assertTrue(call.kwargs["update_only"])
        self.assertIn("没有发出", card)
        self.assertIn('"default_value": "加一节对比"', card)
        self.assertIn(f"提交补充到 #{self.job.job_id}", card)
        path = delivery_path(self.ctx_topic.state_dir, self.ctx_topic.topic_id, "steer-form-form-card")
        self.assertEqual(read_delivery(path)["status"], "refused")
        card = text_of(self.submit("e2").args[3])
        self.assertIn(f"已补充到 #{self.job.job_id}", card)
        self.assertNotIn('"tag": "form"', card)
        self.assertEqual(self.control.steer.call_count, 2)
        self.assertEqual(read_delivery(path)["status"], "sent")
        # The task card names the newest supplement, above the original request.
        element = self.app.cards.set_task_element.call_args
        self.assertEqual(element.kwargs["before"], "current_question")
        self.assertIn("加一节对比", element.args[2]["content"])
        self.submit("e3")  # A repeated callback shows the receipt and never resends.
        self.assertEqual(self.control.steer.call_count, 2)

    def test_a_runner_opens_the_form_before_its_backend_starts(self):
        # The task card exists before the runner's job has started: 补充 must not say the task ended.
        self.job.execution_started = False
        self.job.backend_control = None
        self.app.runtime.runner = RunnerLink(self.root / "run")  # This process is a task runner.
        self.app.interaction.interaction_action(self.ctx_topic, {"chat_id": "oc_chat", "event_id": "open", "action_value": self.value})
        self.assertIn(f"提交补充到 #{self.job.job_id}", text_of(self.app.messenger.publish_card.call_args.args[3]))
        # Its worker has not read the request yet: the supplement joins it.
        self.assertIn("任务开始前收到，已并入本轮请求", text_of(self.submit("e1").args[3]))
        self.assertEqual(self.status(), "sent")
        self.assertIn("用户在任务开始前补充：\n加一节对比", self.job.prompt_text)

    def test_the_card_of_a_task_about_to_start_takes_a_supplement_into_its_request(self):
        # #279: 补充当前任务 on a card that still said 已入队, with nothing ahead in the topic.
        self.job.execution_started = False
        self.job.backend_control = None
        self.app.interaction.interaction_action(self.ctx_topic, {"chat_id": "oc_chat", "event_id": "open", "action_value": self.value})
        self.assertIn(f"提交补充到 #{self.job.job_id}", text_of(self.app.messenger.publish_card.call_args.args[3]))
        card = text_of(self.submit("e1").args[3])
        self.assertIn(f"已补充到 #{self.job.job_id}", card)
        self.assertIn("并入本轮请求", card)
        self.assertIn("加一节对比", self.job.prompt_text)
        self.assertIn("加一节对比", find_job_record(self.ctx_topic.state_dir, self.job.message_id)["prompt_text"])
        self.control.steer.assert_not_called()
        # Once it has started, the same card sends the usual way.
        self.job.execution_started = True
        self.job.backend_control = self.control
        self.control.steer.side_effect = None
        self.app.messenger.publish_card.reset_mock()
        self.app.interaction.interaction_action(self.ctx_topic, {"chat_id": "oc_chat", "event_id": "open-2", "message_id": "x",
                                                     "action_value": self.value})
        self.app.messenger.publish_card.assert_called_once()

    def test_a_supplement_before_the_task_is_ready_is_saved_and_sent_by_itself(self):
        self.control.steer.side_effect = [SteerNotReady("任务刚开始，Claude 还没准备好接收补充"), None]
        sent = threading.Event()
        shown = []
        self.app.steering.show_steer_receipt = mock.Mock(side_effect=lambda ctx, record: (shown.append(record["status"]),
                                                                                  record["status"] == "sent" and sent.set()))
        card = text_of(self.submit("e1").args[3])
        self.assertIn("已保存，尚未送达", card)
        self.assertIn(WITHDRAW_KIND, card)
        self.assertNotIn('"tag": "form"', card)
        self.assertEqual(self.status(), "held")
        self.assertTrue(sent.wait(8))  # Sent once the task could take it, without a second submit.
        self.assertEqual(self.status(), "sent")
        self.assertEqual(self.control.steer.call_count, 2)
        for _ in range(50):  # The task card's latest-supplement line follows the receipt.
            if self.app.cards.set_task_element.call_args:
                break
            time.sleep(0.1)
        self.assertIn("加一节对比", self.app.cards.set_task_element.call_args.args[2]["content"])
        self.submit("e2")  # A repeated callback never sends it again.
        self.assertEqual(self.control.steer.call_count, 2)

    def test_a_held_supplement_can_be_withdrawn_and_is_never_sent(self):
        self.control.steer.side_effect = SteerNotReady("任务刚开始")
        self.app.steering.show_steer_receipt = mock.Mock(return_value=True)
        self.submit("e1")
        self.press(WITHDRAW_KIND, "w1")
        self.assertEqual(self.status(), "withdrawn")
        tries = self.control.steer.call_count
        self.job.held_thread.join(10)
        self.assertFalse(self.job.held_thread.is_alive())  # Nothing left to try: the sender ended ...
        self.assertEqual(self.control.steer.call_count, tries)  # ... without trying again.
        self.assertIn("已撤回", text_of(receipt_card(read_delivery(self.path))))

    def test_a_task_that_ends_first_marks_its_held_supplement_not_sent(self):
        self.control.steer.side_effect = SteerNotReady("任务刚开始")
        self.app.steering.show_steer_receipt = mock.Mock(return_value=True)
        self.submit("e1")
        self.app.steering.close_steer_forms(self.ctx_topic, self.job)  # What the end of the task does.
        record = read_delivery(self.path)
        self.assertEqual(record["status"], "refused")
        self.assertIn("没有发出", record["error"])
        self.assertIn("没有补充到", text_of(receipt_card(record)))
        # A topic's keeps its receipt and 重新排队执行; only the butler's queue by themselves (requeue_auto).
        self.assertNotIn("requeue_auto", record)
        self.assertEqual(self.app.runtime.jobs.topic_jobs(self.job.topic_id, self.job.chat_id), [self.job])

    def test_a_task_still_starting_sends_the_held_supplement_when_its_backend_comes_up(self):
        self.job.backend_control = None
        self.assertIn("已保存，尚未送达", text_of(self.submit("e1").args[3]))
        sent = threading.Event()
        self.app.steering.show_steer_receipt = mock.Mock(side_effect=lambda ctx, record: record["status"] == "sent" and sent.set())
        self.control.steer.side_effect = None
        self.job.backend_control = self.control
        self.assertTrue(sent.wait(8))
        self.control.steer.assert_called_once()

    def test_a_task_waiting_for_an_answer_is_sent_nothing_and_the_form_keeps_the_text(self):
        # Steering a paused Codex turn would fail the question it waits on.
        self.control.pause_for_question = "选哪个方案？"
        with self.assertRaisesRegex(ValueError, "正在等待回答"):
            self.app.interaction.interaction_action(self.ctx_topic, {"chat_id": "oc_chat", "event_id": "open",
                                                         "message_id": "task-msg", "action_value": self.value})
        card = text_of(self.submit("e1").args[3])
        self.assertIn("请先回答", card)
        self.assertIn('"default_value": "加一节对比"', card)
        self.control.steer.assert_not_called()

    def test_a_turn_held_open_for_an_answer_opens_no_second_form(self):
        # Claude and MASO keep the turn open on a question (CUA #335 got a second form here).
        self.control.awaiting_answer = True
        with self.assertRaisesRegex(ValueError, "正在等待回答"):
            self.app.interaction.interaction_action(self.ctx_topic, {"chat_id": "oc_chat", "event_id": "open",
                                                         "message_id": "task-msg", "action_value": self.value})
        self.app.messenger.publish_card.assert_not_called()
        card = text_of(self.submit("e1").args[3])
        self.assertIn("请先回答", card)
        self.control.steer.assert_not_called()

    def test_a_held_supplement_waits_while_the_task_waits_for_an_answer(self):
        self.job.backend_control = None
        self.assertIn("已保存，尚未送达", text_of(self.submit("e1").args[3]))
        self.control.steer.side_effect = None
        self.control.pause_for_question = "选哪个方案？"
        self.job.backend_control = self.control
        self.assertTrue(self.shutdown.wait_rounds("steer-hold", 3))  # Rounds that saw the paused turn ...
        self.control.steer.assert_not_called()  # ... sent nothing.
        self.assertEqual(self.status(), "held")
        sent = threading.Event()
        self.app.steering.show_steer_receipt = mock.Mock(side_effect=lambda ctx, record: record["status"] == "sent" and sent.set())
        self.control.pause_for_question = ""  # Answered: the turn runs again.
        self.assertTrue(sent.wait(8))
        self.control.steer.assert_called_once()

    def test_steer_read_marks_the_delivery_and_repaints_its_receipt(self):
        self.control.steer.side_effect = None
        self.submit("e1")
        repaint = threading.Event()
        self.app.steering.show_steer_receipt = mock.Mock(side_effect=lambda *a: repaint.set())
        self.app.interaction.structured_harness_event(self.ctx_topic, self.job, "steer_read", {"event_id": "steer-form-form-card"}, None)
        self.assertTrue(repaint.wait(5))
        path = delivery_path(self.ctx_topic.state_dir, self.ctx_topic.topic_id, "steer-form-form-card")
        self.assertEqual(read_delivery(path)["status"], "read")


class MergeFormClicks:
    """补充当前任务 on a queued message's card opens the 补充 form filled in with the message (repo review U3:
    the same form as on a running task's card); submitting it is the owner's second click. The form's
    repaints (its receipt, a refusal) are in ``self.repaints``."""

    def watch_repaints(self):
        self.repaints = []
        self.app.cards.repaint_browse_card = mock.Mock(
            side_effect=lambda ctx, chat, mid, source, card: (self.repaints.append((source, text_of(card))), True)[1])

    def open_merge_form(self, job, event_id="press"):
        """补充当前任务 clicked on ``job``'s card; the forms it opened (none for a queued message)."""
        forms = []

        def publish(ctx, chat, mid, card, *args, on_sent=None, **kwargs):
            forms.append(card)
            if on_sent:
                on_sent("form-" + event_id)
            return True
        with mock.patch.object(self.app.messenger, "publish_card", side_effect=publish):
            self.app.interaction.interaction_action(self.ctx_topic, {"chat_id": "oc_chat", "event_id": event_id,
                "action_value": {"kind": "bridge_steer", "topic_id": job.topic_id, "job_id": job.job_id}})
        return forms

    def submit_merge_form(self, form, event_id="press", text=None):
        button = next(e for e in walk_dicts(form) if e.get("tag") == "button")
        box = next(e for e in walk_dicts(form) if e.get("name") == "supplement")
        remember_browse_card(self.app, self.ctx_topic, "form-" + event_id, "card-form-" + event_id)
        self.app.interaction.interaction_action(self.ctx_topic, {
            "chat_id": "oc_chat", "event_id": event_id + "-submit", "message_id": "form-" + event_id,
            "action_value": button["behaviors"][0]["value"],
            "form_value": {"supplement": box.get("default_value", "") if text is None else text}})

    def merge(self, job, event_id="press"):
        """One click sends the queued message itself, attachments and all (the owner, 2026-10-31: a form filled
        in with its text lost the files); it opens no form."""
        forms = self.open_merge_form(job, event_id)
        self.assertEqual(forms, [])


class FerryAccelerationRetiredTest(TopicFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        from covey.backends.codex.bridge import CODEX_SPEC
        from covey.backends.codex.sessions import CodexSessionStore

        self.ctx = dataclasses.replace(
            self.ctx, model="ferry-gpt-6-astra-max", options={"harness_id": "codex"}
        )
        self.backend = RecordingBackend()
        self.backend.declare(steering=True)
        self.app = harness_app(CODEX_SPEC, self.backend, CodexSessionStore(self.root / "codex-sessions"))
        self.logs = quiet(self.app)
        self.app.cards.streaming = False
        self.app.messenger.send_message = mock.Mock(return_value=True)
        self.app.runtime.owner_open_id = "ou_user"
        card_ids, message_ids = itertools.count(1), itertools.count(1)

        def card_api(_profile, method, _path, _data, **_kwargs):
            if method == "POST":
                return {"data": {"card_id": f"card-{next(card_ids)}"}}
            return {"code": 0}

        def send_card(*_args, **_kwargs):
            return f"om_receipt_{next(message_ids)}", False

        self.cardkit = FakeCardKit(side_effect=card_api, sent_effect=send_card).start(self)

    def start(self, *, model="ferry-gpt-6-astra-max", steer=None):
        self.ctx = dataclasses.replace(self.ctx, model=model)
        self.receive("om_run", "task")
        self.running = self.app.runtime.jobs.queue.get_nowait()
        self.running.execution_started = True
        self.control = SimpleNamespace(
            steer=steer or mock.Mock(return_value="sent"), supports_steer=True
        )
        self.running.backend_control = self.control
        return self.running

    def test_a_ferry_acceleration_word_is_an_ordinary_message_even_with_the_retired_switch(self):
        """Ferry auto-acceleration is gone: "加速" while a ferry-* turn runs is a new message, as it
        was with the switch off, and nothing is steered."""
        self.start()
        with mock.patch.dict(os.environ, {"COVEY_FERRY_AUTO_ACCELERATE": "1"}):
            self.receive("om_boost", "加速", root="om_run")
        queued = self.app.runtime.jobs.queue.get_nowait()
        self.assertEqual(queued.user_text, "加速")
        self.control.steer.assert_not_called()
        self.assertIsNone(read_delivery(delivery_path(self.ctx.state_dir, self.running.topic_id, "om_boost")))


class QueueStatusTest(unittest.TestCase):
    def test_a_queued_card_says_what_it_waits_for_and_how_long_that_has_run(self):
        # Repo review U3: the owner stopped tasks that had run 85 minutes to 6 hours to get a message in; the
        # queued card said only "本话题前方 1 个任务".
        from covey.app.submission import queue_status
        began = time.mktime((2026, 10, 28, 10, 5, 0, 0, 0, -1))
        running = SimpleNamespace(job_id=1190, execution_started=True, message_id="om_run",
                                  timings={"backend_requested": began})
        waiting = SimpleNamespace(job_id=1191, execution_started=False, message_id="om_wait", timings={})
        with tempfile.TemporaryDirectory() as state:
            self.assertEqual(queue_status(Path(state), [running], now=began + 83 * 60 + 5),
                             "排队中 · 排在 #1190 后面 · #1190 已运行 1小时23分（10:05 开始）")
            self.assertEqual(queue_status(Path(state), [running, waiting], now=began + 30),
                             "排队中 · 本话题前方 2 个任务 · #1190 已运行 30秒（10:05 开始）")
            self.assertEqual(queue_status(Path(state), [waiting]), "排队中 · 排在 #1191 后面")  # Not started yet.


class QueuedReceiptTest(MergeFormClicks, TopicFixture, unittest.TestCase):
    """The #253 case: a queued message turned into a supplement shows one line, not a queued task card."""

    def setUp(self):
        super().setUp()
        self.receive("om_run", "第一项任务")
        self.running = self.app.runtime.jobs.queue.get_nowait()
        self.running.execution_started = True
        self.running.backend_control = SimpleNamespace(steer=mock.Mock(), supports_steer=True)
        self.app.cards.set_task_element = mock.Mock(return_value=True)
        self.app.messenger.send_message.reset_mock()
        self.app.cards.create_streaming_reply = mock.Mock(side_effect=lambda ctx, job, content: SimpleNamespace(
            card_id="card-" + job.message_id, message_id="msg-" + job.message_id, sequence=1, disabled=False))
        self.receive("om_next", "顺便把日志也看一下", root="om_run", thread="omt_run")
        self.queued = self.app.runtime.jobs.queue.get_nowait()
        self.ctx_topic = self.topic("om_run")
        self.app.cards.close_streaming_reply = mock.Mock()
        self.app.cards.replace_record_card = mock.Mock(return_value=True)
        self.watch_repaints()

    def test_no_second_queue_notice_when_the_card_says_it(self):
        sent = " ".join(str(c) for c in self.app.messenger.send_message.call_args_list)
        self.assertNotIn("尚未传给", sent)
        status = find_job_record(self.ctx_topic.state_dir, "om_next")["status_message"]
        self.assertIn("排队中", status)

    def test_a_queued_message_for_a_task_still_starting_is_held_then_sent(self):
        self.running.backend_control.steer = mock.Mock(side_effect=[SteerNotReady("任务刚开始"), None])
        self.app.runtime.jobs.register(self.running)
        self.addCleanup(self.app.runtime.unregister_reply_job, self.running)
        sent = threading.Event()
        original = self.app.steering.show_steer_receipt
        self.app.steering.show_steer_receipt = mock.Mock(side_effect=lambda ctx, record: (original(ctx, record),
                                                                                  record["status"] == "sent" and sent.set()))
        self.merge(self.queued)
        self.assertIn("已保存，尚未送达", text_of(self.app.cards.replace_record_card.call_args.args[2]))
        self.assertIn("准备好后自动送入", find_job_record(self.ctx_topic.state_dir, "om_next")["status_message"])
        self.assertTrue(self.queued.cancelled_before_start)
        self.assertTrue(sent.wait(8))
        wait_for(self, lambda: "已补充到" in text_of(self.app.cards.replace_record_card.call_args.args[2]))

    def test_a_late_held_repaint_never_overwrites_the_sent_receipt(self):
        """The task takes the held supplement while this click is still closing the queued card: the
        click's own repaint comes last and must show where the supplement is now, not where it was."""
        self.fast_polling()
        self.running.backend_control.steer = mock.Mock(side_effect=[SteerNotReady("任务刚开始"), None])
        self.app.runtime.jobs.register(self.running)
        self.addCleanup(self.app.runtime.unregister_reply_job, self.running)
        painted_sent, cards = threading.Event(), []

        def replace(ctx, record, card):
            cards.append(text_of(card))
            if "已补充到" in cards[-1]:
                painted_sent.set()
            return True
        self.app.cards.replace_record_card = mock.Mock(side_effect=replace)
        self.app.cards.close_streaming_reply = mock.Mock(side_effect=lambda *args: painted_sent.wait(5))
        self.merge(self.queued)
        self.assertTrue(painted_sent.is_set())
        self.assertIn("已补充到", cards[-1])
        self.assertNotIn("已保存，尚未送达", cards[-1])

    def test_one_click_sends_the_message_with_its_attachments_and_the_queued_card_becomes_the_receipt(self):
        # The owner, 2026-10-31: the click opened a form filled in with the text only, so the files never went.
        self.queued.prompt_text = self.queued.user_text + "\n\n收到的附件（已下载到本地）：\n\n- 文件 1（a.json）: `/x/a.json`"
        self.merge(self.queued)
        self.assertIn("/x/a.json", self.running.backend_control.steer.call_args.args[0])
        record, card = self.app.cards.replace_record_card.call_args.args[1:]
        self.assertEqual(record["card_id"], "card-om_next")
        self.assertIn(f"已补充到 #{self.running.job_id}**，不再单独排队", text_of(card))
        self.assertNotIn("排队中", text_of(card))
        self.assertNotIn("current_question", text_of(card))
        self.assertEqual(self.repaints, [])  # No form.
        self.app.cards.close_streaming_reply.assert_called_once()
        merged = find_job_record(self.ctx_topic.state_dir, "om_next")
        self.assertEqual((merged["merged_into_job_id"], merged["never_started"]), (self.running.job_id, True))
        self.assertNotIn("尚未传给", " ".join(str(c) for c in self.app.messenger.send_message.call_args_list))

    def test_a_form_an_older_release_opened_still_sends_what_it_says(self):
        # 8ec6c66 to 2a08179 opened a form filled in with the text: one already in the topic keeps working.
        from covey.cards.steer_form import steer_form
        form = steer_form(self.running.job_id, f"#{self.running.job_id}", {
            "kind": "bridge_steer", "topic_id": self.queued.topic_id, "job_id": self.queued.job_id,
            "merge": self.queued.job_id}, text=self.queued.user_text)
        self.submit_merge_form(form, text="改过的补充")
        self.running.backend_control.steer.assert_called_once_with("改过的补充", "queued-steer-om_next")
        self.assertTrue(self.queued.cancelled_before_start)
        self.assertEqual(self.repaints[-1][0], "form-press")
        self.assertIn(f"已补充到 #{self.running.job_id}", self.repaints[-1][1])

    def test_the_receipt_shows_what_the_user_wrote_not_the_topic_background(self):
        self.queued.prompt_text = self.queued.user_text + "\n\n话题起始消息（背景参考）：\n- parent_id: `om_run`"
        self.running.progress = SimpleNamespace(card_id="card-om_run")
        self.merge(self.queued)  # The message as it came, its background too.
        self.assertIn("话题起始消息", self.running.backend_control.steer.call_args.args[0])  # The model still gets it.
        card = text_of(self.app.cards.replace_record_card.call_args.args[2])
        self.assertIn("顺便把日志也看一下", card)
        self.assertNotIn("话题起始消息", card)
        self.assertNotIn("parent_id", card)
        # The running task's card names the supplement in the same words (CUA #348).
        note = next(c.args[2]["content"] for c in self.app.cards.set_task_element.call_args_list
                    if c.args[2]["element_id"] == "latest_request")
        self.assertIn("顺便把日志也看一下", note)
        self.assertNotIn("话题起始消息", note)

    def test_a_task_runner_names_the_supplement_in_the_users_words(self):
        # A runner gets the full text in its inbox and finds what the user wrote in the delivery record.
        prompt = self.queued.user_text + "\n\n话题起始消息（背景参考）：\n- parent_id: `om_run`"
        path = delivery_path(self.ctx_topic.state_dir, self.running.topic_id, "queued-steer-om_next")
        save_delivery(path, {"status": "sent", "text": prompt, "receipt": {"kind": "job", "text": self.queued.user_text}})
        self.running.progress = SimpleNamespace(card_id="card-om_run")
        self.app.steering.note_latest_request(self.ctx_topic, self.running, prompt, "queued-steer-om_next")
        note = self.app.cards.set_task_element.call_args.args[2]["content"]
        self.assertIn("顺便把日志也看一下", note)
        self.assertNotIn("parent_id", note)


class ReceiptActionTest(TopicFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.fast_polling()
        self.ctx_topic = self.topic("om_run")
        self.path = delivery_path(self.ctx_topic.state_dir, self.ctx_topic.topic_id, "queued-steer-om_next")
        self.token = self.path.stem
        upsert_job_record(self.ctx_topic.state_dir, "om_run", job_id=7, status="running", topic_id=self.ctx_topic.topic_id,
                          chat_id="oc_chat")
        save_delivery(self.path, {"status": "uncertain", "error": "超时", "topic_id": self.ctx_topic.topic_id,
                                  "chat_id": "oc_chat", "job_id": 7, "job_key": "om_run", "token": self.token,
                                  "text": "看日志", "requeue": {"text": "看日志", "prompt": "看日志"}})
        self.app.steering.show_steer_receipt = mock.Mock(return_value=True)

    def press(self, kind):
        self.app.interaction.interaction_action(self.ctx_topic, {"chat_id": "oc_chat", "event_id": "e-" + kind, "action_value": {
            "kind": kind, "topic_id": self.ctx_topic.topic_id, "token": self.token}})
        return self.app.steering.show_steer_receipt.call_args.args[1]

    def test_a_late_runner_acknowledgement_repaints_the_receipt_by_itself(self):
        run = run_files(self.root / "run", folders=[rd.ACKS])
        record = read_delivery(self.path)
        save_delivery(self.path, {**record, "op": "1-ab", "run_dir": str(run)})
        repainted = threading.Event()
        self.app.steering.show_steer_receipt = mock.Mock(side_effect=lambda *a: repainted.set())
        self.app.steering.watch_steer(self.ctx_topic, self.path, seconds=10)
        rd.ack(run, "1-ab", True)
        self.assertTrue(repainted.wait(8))
        self.assertEqual(self.app.steering.show_steer_receipt.call_args.args[1]["status"], "sent")

    def test_check_keeps_a_running_task_unconfirmed_and_a_finished_one_offers_requeue(self):
        self.assertEqual(self.press(CHECK_KIND)["status"], "uncertain")
        with self.assertRaisesRegex(ValueError, "不能重新排队"):
            self.press(REQUEUE_KIND)  # The task may still take it.
        upsert_job_record(self.ctx_topic.state_dir, "om_run", status="done")
        record = self.press(CHECK_KIND)
        self.assertTrue(record["target_ended"])
        self.assertIn("无法确认", record["error"])
        record = self.press(REQUEUE_KIND)
        job = self.app.runtime.jobs.queue.get_nowait()
        self.assertEqual((job.user_text, record["requeued_job_id"]), ("看日志", job.job_id))
        with self.assertRaisesRegex(ValueError, "已经排过队"):
            self.press(REQUEUE_KIND)

    def test_a_supplement_its_task_ended_without_reading_can_be_queued_again(self):
        record = read_delivery(self.path)
        save_delivery(self.path, {**record, "status": "sent", "error": "", "target_ended": True})
        upsert_job_record(self.ctx_topic.state_dir, "om_run", status="cancelled")
        record = self.press(REQUEUE_KIND)
        job = self.app.runtime.jobs.queue.get_nowait()
        self.assertEqual((job.user_text, record["requeued_job_id"]), ("看日志", job.job_id))

    def test_a_check_after_its_task_ended_elsewhere_marks_an_unread_supplement(self):
        # A task runner that died, a retired Harness or a restart ends the task outside the worker.
        record = read_delivery(self.path)
        save_delivery(self.path, {**record, "status": "sent", "error": "", "reads": True})
        self.assertNotIn("target_ended", self.press(CHECK_KIND))  # Still running: it may yet be read.
        upsert_job_record(self.ctx_topic.state_dir, "om_run", status="interrupted")
        record = self.press(CHECK_KIND)
        self.assertEqual(record["status"], "sent")
        self.assertTrue(record["target_ended"])
        self.assertIn("模型读到", record["error"])
        record = self.press(REQUEUE_KIND)
        self.assertEqual(record["requeued_job_id"], self.app.runtime.jobs.queue.get_nowait().job_id)

    def test_a_check_leaves_a_sent_supplement_of_a_backend_without_reads_as_it_was(self):
        record = read_delivery(self.path)
        save_delivery(self.path, {**record, "status": "sent", "error": ""})  # Old record, or Codex.
        upsert_job_record(self.ctx_topic.state_dir, "om_run", status="interrupted")
        self.assertNotIn("target_ended", self.press(CHECK_KIND))
        with self.assertRaisesRegex(ValueError, "不能重新排队"):
            self.press(REQUEUE_KIND)

    def refuse(self):
        record = read_delivery(self.path)
        save_delivery(self.path, {**record, "status": "refused", "error": "任务已结束"})

    def test_requeue_sends_with_the_record_lock_free(self):
        """Rule 1: the delivery record's cross-process lock is free while the new task's
        submission talks to Feishu (S19 repro requeue_lock_repro.py saw it held)."""
        self.refuse()
        lockfile = self.path.parent / ".locks" / (self.path.name + ".lock")
        probe = ("import fcntl,os,sys\nfd=os.open(sys.argv[1],os.O_RDWR)\n"
                 "try:\n fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB); print('free')\n"
                 "except BlockingIOError: print('held')\n")
        seen, states = [], []

        def send(*args, **kwargs):
            seen.append(subprocess.run([sys.executable, "-c", probe, str(lockfile)], capture_output=True,
                                       text=True, timeout=30).stdout.strip())
            states.append(read_delivery(self.path).get("requeue_state"))
            return True
        self.app.messenger.send_message = mock.Mock(side_effect=send)
        record = self.press(REQUEUE_KIND)
        job = self.app.runtime.jobs.queue.get_nowait()
        self.assertTrue(seen)
        self.assertEqual(set(seen), {"free"})
        self.assertEqual(set(states), {"submitting"})  # The intent was saved before the send.
        self.assertEqual((record["requeue_state"], record["requeued_job_id"]), ("queued", job.job_id))

    def test_requeue_is_acknowledged_before_its_task_card_and_a_repeat_meanwhile_is_refused(self):
        # REVIEW2 item 9: the new task's card and the receipt took 2.9 s in the click.
        self.refuse()
        queuing, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        enqueue = self.app.steering.enqueue_chat_job
        self.app.steering.enqueue_chat_job = mock.Mock(side_effect=lambda *a, **k: (
            queuing.set(), release.wait(30), enqueue(*a, **k))[-1])
        lanes, acknowledged = acknowledging_lanes(self, self.app.handle_card_action_event)
        for event_id in ("requeue", "again"):
            lanes.submit({"operator_id": "ou_user", "chat_id": "oc_chat", "event_id": event_id, "action_value": {
                "kind": REQUEUE_KIND, "topic_id": self.ctx_topic.topic_id, "token": self.token}}, self.ctx_topic)
        self.assertTrue(queuing.wait(5))
        wait_for(self, lambda: acknowledged == ["requeue", "again"])
        wait_for(self, lambda: any(REQUEUE_IN_FLIGHT in str(c.args[2]) for c in self.app.messenger.send_message.call_args_list))
        release.set()
        wait_for(self, lambda: read_delivery(self.path).get("requeue_state") == "queued" and not lanes.lanes)
        self.assertEqual(self.app.steering.enqueue_chat_job.call_count, 1)
        self.assertEqual(self.app.runtime.jobs.queue.qsize(), 1)

    def test_an_error_while_requeuing_is_unconfirmed_and_never_queued_again(self):
        self.refuse()
        self.app.steering.enqueue_chat_job = mock.Mock(side_effect=RuntimeError("feishu"))
        with self.assertRaises(RuntimeError):
            self.press(REQUEUE_KIND)
        record = read_delivery(self.path)
        self.assertEqual(record["requeue_state"], "uncertain")
        card = text_of(receipt_card(record))
        self.assertNotIn(REQUEUE_KIND, card)
        self.assertIn("结果未确认", card)
        with self.assertRaisesRegex(ValueError, "结果未确认"):
            self.press(REQUEUE_KIND)
        self.assertEqual(self.app.steering.enqueue_chat_job.call_count, 1)

    def test_a_requeue_refused_before_queuing_can_be_tried_again(self):
        self.refuse()
        self.app.steering.enqueue_chat_job = mock.Mock(return_value=None)
        for _ in range(2):
            with self.assertRaisesRegex(ValueError, "重新排队未成功"):
                self.press(REQUEUE_KIND)
        self.assertEqual(self.app.steering.enqueue_chat_job.call_count, 2)
        self.assertIn(REQUEUE_KIND, text_of(receipt_card(read_delivery(self.path))))


class RunningTaskFormTest(QuestionFixture, unittest.TestCase):

    def test_steer_form_submit_routes_to_current_turn(self):
        value = {"kind": "bridge_steer", "topic_id": self.job.topic_id, "job_id": self.job.job_id}
        self.app.messenger.publish_card = mock.Mock()
        self.app.interaction.interaction_action(self.ctx_topic, {"chat_id": "oc_chat", "action_value": value})
        card = self.app.messenger.publish_card.call_args.args[-1]
        self.assertIn(f"补充任务 #{self.job.job_id}", json.dumps(card, ensure_ascii=False))  # It names its task.
        box, submit = next(e for e in card["body"]["elements"] if e.get("tag") == "form")["elements"]
        # A box that shows a long supplement (the owner, 2026-10-31: the old one was a few lines), at Feishu's limit.
        self.assertEqual((box["rows"], box["max_rows"], box["max_length"], box["width"]), (6, 15, 1000, "fill"))
        self.assertIn("附件", json.dumps(card, ensure_ascii=False))  # Where a longer one or files go.
        self.assertEqual(submit["form_action_type"], "submit")
        callback = submit["behaviors"][0]
        self.assertEqual(callback["type"], "callback")
        self.app.steering.deliver_steer = mock.Mock(wraps=self.app.steering.deliver_steer)
        remember_browse_card(self.app, self.ctx_topic, 'steer-card', 'steer-card')
        self.app.interaction.interaction_action(self.ctx_topic, {
            "chat_id": "oc_chat", "event_id": "steer-answer", "message_id": "steer-card", "action_value": callback["value"],
            "form_value": json.dumps({"supplement": "第一行\n第二行"}),
        })
        self.assertEqual(self.app.steering.deliver_steer.call_args.args, (self.ctx_topic, self.job, "第一行\n第二行", "steer-form-steer-card"))

    def test_a_supplement_to_a_task_that_reports_reads_says_so_on_its_record(self):
        self.job.backend_control = SimpleNamespace(steer=lambda text, mid: "sent")
        self.backend.declare(steering=True, steer_reads=True)
        record = self.app.steering.deliver_steer(self.ctx_topic, self.job, "看日志", "steer-reads")
        self.assertTrue(record["reads"])
        self.backend.declare(steering=True)  # Codex and the rest: as before.
        self.assertNotIn("reads", self.app.steering.deliver_steer(self.ctx_topic, self.job, "看日志", "steer-plain"))

    def test_a_submitted_form_shows_its_receipt_before_the_task_card_notes_it(self):
        # S37: the task card's note (0.35-1.1 s) was written before the form showed its receipt.
        order = []
        self.job.progress = SimpleNamespace(card_id="card-run")
        self.job.backend_control = SimpleNamespace(steer=lambda text, mid: order.append("sent") or "sent")
        self.app.cards.repaint_browse_card = mock.Mock(side_effect=lambda *a: order.append("receipt") or True)
        self.app.cards.set_task_element = mock.Mock(side_effect=lambda *a, **k: order.append("note"))
        remember_browse_card(self.app, self.ctx_topic, 'steer-card', 'steer-card')
        value = {"kind": "bridge_steer", "topic_id": self.job.topic_id, "job_id": self.job.job_id}
        self.app.interaction.interaction_action(self.ctx_topic, {
            "chat_id": "oc_chat", "event_id": "steer-answer", "message_id": "steer-card", "action_value": value,
            "form_value": json.dumps({"supplement": "再看一下日志"})})
        self.assertEqual(order, ["sent", "receipt", "note"])
        self.assertIn("再看一下日志", self.app.cards.set_task_element.call_args.args[2]["content"])

    def test_open_steer_forms_become_read_only_when_the_task_ends(self):
        value = {"kind": "bridge_steer", "topic_id": self.job.topic_id, "job_id": self.job.job_id}
        self.app.messenger.publish_card = mock.Mock(side_effect=lambda *a, on_sent=None, **k: on_sent and on_sent("steer-open"))
        self.app.interaction.interaction_action(self.ctx_topic, {"chat_id": "oc_chat", "event_id": "open", "action_value": value})
        self.app.interaction.interaction_action(self.ctx_topic, {"chat_id": "oc_chat", "event_id": "open-2", "action_value": value})
        self.assertEqual(self.job.steer_forms, ["steer-open", "steer-open"])
        self.job.steer_forms = ["steer-open", "steer-sent"]
        # A form whose supplement was delivered keeps its receipt.
        token = hashlib.sha256(f"{self.ctx_topic.topic_id}-steer-form-steer-sent".encode()).hexdigest()
        sent = self.ctx_topic.state_dir / "interaction-deliveries" / (token + ".json")
        sent.parent.mkdir(parents=True, exist_ok=True)
        sent.write_text(json.dumps({"status": "sent"}))
        self.app.messenger.publish_card.reset_mock()
        self.app.steering.close_steer_forms(self.ctx_topic, self.job)
        self.app.steering.close_steer_forms(self.ctx_topic, self.job)
        self.assertEqual([c.kwargs["source_message"] for c in self.app.messenger.publish_card.call_args_list], ["steer-open"])
        call = self.app.messenger.publish_card.call_args
        self.assertTrue(call.kwargs["update_only"])
        self.assertIn(STEER_CLOSED, json.dumps(call.args[3], ensure_ascii=False))

    def add_second_question(self):
        self.record["questions"].append({"id": "order", "question": "顺序？", "options": []})
        self.path.write_text(json.dumps(self.record))


class UnreadAtTheEndTest(TopicFixture, unittest.TestCase):
    """A supplement the backend took but the model never read when its turn ended (#1773, stopped at 12:36)."""

    def run_turn(self, *, ending=None, read=False, **capabilities):
        self.backend.declare(steering=True, **capabilities)
        self.app.steering.show_steer_receipt = mock.Mock(return_value=True)
        self.receive("om_run", "task")
        job = self.app.runtime.jobs.queue.get_nowait()
        path = delivery_path(self.ctx.state_dir, job.topic_id, "steer-1")
        run = self.backend.run

        def run_with_a_supplement(request):  # Sent while the turn ran; no steer_read came.
            save_delivery(path, {"status": "sent", "job_id": job.job_id, "job_key": job.message_id, "text": "x",
                                 "sent_at": 1.0, "receipt": {"kind": "browse", "source": "form"},
                                 "requeue": {"text": "x"}})
            if read:  # Claude replayed it: the model took it in.
                self.app.steering.mark_steer_read(self.ctx, job, "steer-1")
            if ending is not None:
                raise ending
            return run(request)
        self.backend.run = run_with_a_supplement
        self.app.process_reply_job(job, self.ctx)
        return read_delivery(path)

    def test_claude_marks_it_ended_unread(self):
        record = self.run_turn(steer_reads=True)
        self.assertEqual(record["status"], "sent")
        self.assertTrue(record["target_ended"])
        wait_for(self, lambda: self.app.steering.show_steer_receipt.called)
        self.assertNotIn("requeue_auto", record)  # Its receipt offers 重新排队执行; nothing queues by itself.
        self.assertIn(REQUEUE, offers(record))

    def test_a_stopped_turn_marks_it_ended_unread(self):
        record = self.run_turn(ending=JobCancelled("任务已停止"), steer_reads=True)  # #1773 was stopped.
        self.assertEqual(record["status"], "sent")
        self.assertTrue(record["target_ended"])

    def test_a_supplement_read_during_the_turn_stays_read(self):
        record = self.run_turn(read=True, steer_reads=True)
        self.assertEqual(record["status"], "read")
        self.assertNotIn("target_ended", record)

    def test_it_is_marked_after_the_result_is_delivered(self):
        order = []
        end = self.app.steering.end_unread_steers
        self.app.steering.end_unread_steers = lambda ctx, job, **kw: order.append("marked") or end(ctx, job, **kw)
        with mock.patch("covey.app.worker.TurnDelivery.deliver",
                        autospec=True, side_effect=lambda *a, **k: order.append("delivered")):
            self.run_turn(steer_reads=True)
        self.assertEqual(order, ["delivered", "marked"])

    def test_a_backend_without_read_receipts_keeps_it_as_it_was(self):
        record = self.run_turn()
        self.assertEqual(record["status"], "sent")
        self.assertNotIn("target_ended", record)


class QueuedSteerTest(MergeFormClicks, TopicFixture, unittest.TestCase):
    """"补充当前任务" on a queued message opens the 补充 form filled in with it; submitted, the message goes into
    the running task."""

    def setUp(self):
        super().setUp()
        self.receive("om_run", "第一项任务")
        self.running = self.app.runtime.jobs.queue.get_nowait()
        self.running.execution_started = True
        self.running.backend_control = SimpleNamespace(steer=mock.Mock())
        self.receive("om_next", "顺便把日志也看一下", root="om_run", thread="omt_run")
        self.queued = self.app.runtime.jobs.queue.get_nowait()
        self.ctx_topic = self.topic("om_run")
        self.value = {"kind": "bridge_steer", "topic_id": self.queued.topic_id, "job_id": self.queued.job_id}
        self.app.record_job_state = mock.Mock(wraps=self.app.record_job_state)
        self.watch_repaints()

    def press(self, event_id="press"):
        return self.merge(self.queued, event_id)

    def test_queued_message_is_delivered_once_and_never_runs(self):
        self.press()
        self.running.backend_control.steer.assert_called_once_with("顺便把日志也看一下", "queued-steer-om_next")
        self.assertTrue(self.queued.cancelled_before_start)
        self.assertIsNone(self.app.runtime.jobs.find(self.queued.job_id))
        status = [c.args[2:4] for c in self.app.record_job_state.call_args_list if c.args[1] is self.queued]
        self.assertEqual(status[-1][0], "cancelled")
        self.assertIn(f"#{self.running.job_id}", status[-1][1])
        history = self.ctx_topic.history_path.read_text().splitlines()
        self.assertEqual(sum("顺便把日志也看一下" in line for line in history), 1)  # only the arrival record
        self.app.process_reply_job(self.queued, self.ctx)
        self.assertEqual(len(self.backend.calls), 0)

    def test_a_refused_supplement_keeps_the_message_queued(self):
        self.running.backend_control.steer.side_effect = SteerRefused("turn already finished")
        self.press()  # Said on the queued card (here, without cards, in one message), not as an error.
        self.assertRegex(str(self.app.messenger.send_message.call_args), "没有发出.*继续排队")
        self.assertFalse(self.queued.cancelled_before_start)
        self.assertIs(self.app.runtime.jobs.find(self.queued.job_id), self.queued)
        # Nothing was sent, so pressing again sends it.
        self.running.backend_control.steer.side_effect = None
        self.press("press-again")
        self.assertEqual(self.running.backend_control.steer.call_count, 2)
        self.assertTrue(self.queued.cancelled_before_start)

    def test_an_unconfirmed_supplement_never_runs_the_message_again(self):
        self.running.backend_control.steer.side_effect = RuntimeError("connection lost")
        self.press()  # It may be in the running task: the queued message must not repeat it.
        self.assertTrue(self.queued.cancelled_before_start)
        status = [c.args[2:4] for c in self.app.record_job_state.call_args_list if c.args[1] is self.queued]
        self.assertIn("送达未确认", status[-1][1])
        self.assertIn("送达未确认", str(self.app.messenger.send_message.call_args))

    def test_with_the_task_ahead_not_started_the_message_joins_its_request(self):
        self.running.backend_control = None
        self.running.execution_started = False
        self.press()
        self.assertTrue(self.queued.cancelled_before_start)
        self.assertIn("用户在任务开始前补充：\n顺便把日志也看一下", self.running.prompt_text)
        self.assertIn("开始后会一并处理", str(self.app.record_job_state.call_args_list[-1]))
        self.assertIn("开始后会一并处理", str(self.app.messenger.send_message.call_args))

    def third_message(self):
        self.receive("om_third", "补充第二项的验收要求", root="om_run", thread="omt_run")
        return self.app.runtime.jobs.queue.get_nowait()

    def press_job(self, job):
        return self.merge(job, "press-third")

    def test_third_message_joins_previous_queued_question_not_first_running_task(self):
        third = self.third_message()
        detached.remember_queued(self.ctx_topic.state_dir, "claude", self.queued)
        self.press_job(third)
        self.running.backend_control.steer.assert_not_called()
        self.assertIn("补充第二项的验收要求", self.queued.prompt_text)
        self.assertTrue(third.cancelled_before_start)
        record = find_job_record(self.ctx_topic.state_dir, third.message_id)
        self.assertEqual(record["merged_into_job_id"], self.queued.job_id)
        restored = runner_spawn.load_job(json.loads(
            detached.queued_path(self.ctx_topic.state_dir, self.queued.message_id).read_text())["job"])
        self.assertIn("补充第二项的验收要求", restored.prompt_text)
        with self.assertRaisesRegex(ValueError, "已结束"):
            self.open_merge_form(third, "press-third-again")
        self.assertEqual(self.queued.prompt_text.count("补充第二项的验收要求"), 1)

    def test_previous_question_starting_does_not_redirect_to_first_task(self):
        third = self.third_message()
        self.queued.execution_started = True
        self.queued.backend_control = SimpleNamespace(steer=mock.Mock())
        self.press_job(third)
        self.running.backend_control.steer.assert_not_called()
        self.queued.backend_control.steer.assert_called_once_with(
            "补充第二项的验收要求", "queued-steer-om_third")

    def test_reply_to_topic_root_after_first_task_finished_steers_previous_turn(self):
        self.app.runtime.unregister_reply_job(self.running)
        self.queued.execution_started = True
        self.queued.backend_control = SimpleNamespace(steer=mock.Mock())
        third = self.third_message()
        self.assertEqual(third.reply_to_message_id, self.running.message_id)
        self.press_job(third)
        self.queued.backend_control.steer.assert_called_once_with(
            "补充第二项的验收要求", "queued-steer-om_third")
        self.running.backend_control.steer.assert_not_called()

    def test_finished_previous_target_never_falls_back_to_first_running_task(self):
        third = self.third_message()
        self.app.runtime.unregister_reply_job(self.queued)
        self.press_job(third)
        self.running.backend_control.steer.assert_not_called()
        self.assertFalse(third.cancelled_before_start)
        self.assertRegex(str(self.app.messenger.send_message.call_args), f"#{self.queued.job_id}.*继续排队")

    def test_card_target_survives_restart_and_is_visible_before_click(self):
        self.backend.capabilities = lambda _: HarnessCapabilities(steering=True)
        self.running.timings["backend_requested"] = time.time() - 125
        with mock.patch.object(self.app.cards, "create_streaming_reply", return_value=None) as create:
            third = self.third_message()
        self.assertIn(f"送入 #{self.queued.job_id}", create.call_args.args[2])
        # U3: how long the task running ahead of it has run, and since when.
        self.assertIn(f"排队中 · 本话题前方 2 个任务 · #{self.running.job_id} 已运行 2分0", create.call_args.args[2])
        restored = runner_spawn.load_job(json.loads(json.dumps(runner_spawn.dump_job(third))))
        self.assertEqual(restored.steer_target_job_id, self.queued.job_id)
        self.assertEqual(find_job_record(self.ctx_topic.state_dir, third.message_id)["steer_target_job_id"],
                         self.queued.job_id)
        restored.harness_capabilities = SimpleNamespace(steering=True)
        card = str(build_stop_action_row(restored, include_steer=True))
        self.assertIn(STEER_LABEL, card)
        self.assertIn(f"#{self.queued.job_id}", queue_hint(restored))
        restored.execution_started = True
        self.assertIn(STEER_LABEL, str(build_stop_action_row(restored, include_steer=True)))

    def test_legacy_queued_card_also_targets_nearest_preceding_question(self):
        third = self.third_message()
        third.steer_target_job_id = None  # Queued before target binding was introduced.
        self.press_job(third)
        self.running.backend_control.steer.assert_not_called()
        self.assertIn("补充第二项的验收要求", self.queued.prompt_text)

    def test_a_task_ahead_that_has_started_keeps_the_message_until_it_can_take_it(self):
        # #279: the click came between the hand-off and the backend being up.
        self.running.backend_control = None
        self.addCleanup(self.app.runtime.unregister_reply_job, self.running)
        self.press()
        self.assertTrue(self.queued.cancelled_before_start)
        self.assertIn("准备好后自动送入", str(self.app.record_job_state.call_args_list[-1]))
        control = SimpleNamespace(steer=mock.Mock(), supports_steer=True)
        self.running.backend_control = control
        for _ in range(50):
            if control.steer.called:
                break
            time.sleep(0.1)
        control.steer.assert_called_once_with("顺便把日志也看一下", "queued-steer-om_next")

    def test_a_started_job_opens_the_ordinary_form(self):
        self.running.backend_control = SimpleNamespace(steer=mock.Mock())
        self.queued.execution_started = True
        self.app.messenger.publish_card = mock.Mock()
        self.app.interaction.interaction_action(self.ctx_topic, {"chat_id": "oc_chat", "event_id": "press-started",
                                                                 "action_value": self.value})
        self.running.backend_control.steer.assert_not_called()
        self.app.messenger.publish_card.assert_called_once()  # a started job opens the ordinary supplement form
