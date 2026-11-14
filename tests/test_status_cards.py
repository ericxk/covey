"""Topic status cards (app/status_cards.py, cards/status_card.py): what each line says per group, the card sent into
the topic's thread once and rewritten only when what it says changed, and the board pinning it in place of the task
or question card."""
from __future__ import annotations

import json
import os
from unittest import mock

from support.world import UnifiedCase
import test_pins
from test_pins import NOW, ON, FakePins

from covey.app import pins, status_cards
from covey.app.status_cards import StatusCards, lines, record_path
from covey.cards.status_card import status_card
from covey.core.state import read_object, write_json
from covey.core.topics import message_topic
from support.main_thread import switch_on
from support.records import write_topic


class Base(UnifiedCase):
    """RoundTests' world (a fake Feishu, the owner's chat, topics t1-t14) without its tests."""
    topic, job, question, record = (test_pins.RoundTests.topic, test_pins.RoundTests.job, test_pins.RoundTests.question,
                                    test_pins.RoundTests.record)

    def setUp(self):
        super().setUp()
        self.state = self.ctx.state_dir
        self.feishu = FakePins()
        messenger = self.app.messenger
        messenger.create_and_send_card = mock.Mock(return_value=("card1", "om_index1"))
        messenger.put_card = mock.Mock(return_value={"code": 0})
        messenger.pin, messenger.unpin, messenger.pin_info = self.feishu.pin, self.feishu.unpin, self.feishu.pin_info
        self.overview = self.app.overview
        self.app.runtime.access.remember_owner_chat("oc_dm")
        write_json(self.state / pins.RECORD, {"schema_version": 1, "since": NOW - 7 * 86400})
        patch = mock.patch.dict(os.environ, ON)
        patch.start()
        self.addCleanup(patch.stop)


class CardTest(Base):
    def test_the_card_is_three_lines_shown_as_written(self):
        card = status_card("T432 · 调研 <x>", "做完", "13:01 · 2 轮", "结论：*没找到*")
        contents = [e["content"] for e in card["body"]["elements"]]
        self.assertEqual(contents[0], "**T432 · 调研 &#60;x&#62;**")
        self.assertIn("<text_tag color='green'>做完</text_tag>", contents[1])
        self.assertEqual(card["body"]["elements"][2]["text_size"], "notation")
        self.assertNotIn("header", card)
        self.assertEqual(len(status_card("T1", "在做", "第 1 轮", "")["body"]["elements"]), 2)


class LinesTest(Base):
    def setUp(self):
        super().setUp()
        write_topic(self.state, {"topic_number": 432, "chat_id": "oc_dm", "root_id": "om_q432",
                                 "origin": {"title": "调研 Mac mini 雷电5+RDMA 能做什么"}}, topic_id="tq")

    def row(self, group, record):
        from covey.core.board_read import ended_at, started_at
        at = ended_at(record) if group == "result" else record.get("created_at") if group == "waiting" else started_at(record)
        return {"topic": "tq", "group": group, "record": record, "at": at, "started": started_at(record)}

    def test_a_running_turn_with_steps_says_which_step_in_place_of_its_phase(self):
        point = self.state / "steps.json"
        running = {"topic_id": "tq", "job_id": 3, "status": "running", "user_text": "改卡片",
                   "checkpoint_path": str(point), "timings": {"backend_started": NOW - 120}}
        write_json(point, {"phase": "改卡片中", "steps": [{"title": "读代码", "state": "done"},
                                                     {"title": "改卡片", "state": "doing"}, {"title": "写测试"}]})
        facts = lines(self.state, "oc_dm", self.row("running", running), NOW)[2]
        self.assertTrue(facts.endswith(" · 第 2/3 步：改卡片"), facts)
        self.assertNotIn("阶段", facts)
        write_json(point, {"phase": "收尾", "steps": [{"title": "读代码", "state": "done"}, {"title": "改卡片", "state": "todo"}]})
        self.assertTrue(lines(self.state, "oc_dm", self.row("running", running), NOW)[2].endswith(" · 已完成 1/2 步"))
        write_json(point, {"phase": "收尾", "steps": "not a list"})  # A malformed field: the phase as before.
        self.assertTrue(lines(self.state, "oc_dm", self.row("running", running), NOW)[2].endswith(" · 阶段：收尾"))

    def test_each_group_says_what_and_where(self):
        self.job("a1", 1, status="done", topic="tq")
        self.job("a2", 2, status="done", topic="tq")
        done = {"topic_id": "tq", "job_id": 2, "status": "done", "final_text": "**没找到完全对口的实测**。其余略。",
                "timings": {"backend_started": NOW - 349, "execution_finished": NOW - 10}}
        title, state, facts, line = lines(self.state, "oc_dm", self.row("result", done), NOW)
        self.assertEqual((title, state), ("T432 · 调研 Mac mini 雷电5+RDMA 能做什么", "做完"))
        self.assertIn("2 轮 · 用时 5分39秒", facts)
        self.assertEqual(line, "结论：没找到完全对口的实测")
        failed = {**done, "status": "failed", "status_message": "后端调用失败"}
        self.assertEqual(lines(self.state, "oc_dm", self.row("result", failed), NOW)[1:4:2], ("失败", "原因：后端调用失败"))
        stopped = {**done, "status": "cancelled", "status_message": "用户停止了任务"}
        self.assertEqual(lines(self.state, "oc_dm", self.row("result", stopped), NOW)[1:4:2],
                         ("已停止", "原因：用户停止了任务"))
        asked = {"topic_id": "tq", "job_id": 1, "created_at": NOW - 60,
                 "questions": [{"question": "要不要做？"}, {"question": "哪个仓库？"}]}
        _, state, facts, line = lines(self.state, "oc_dm", self.row("waiting", asked), NOW)
        self.assertEqual((state, line), ("等你回答", "问题：要不要做？（共 2 题）"))
        self.assertIn("第 1 轮", facts)
        point = self.state / "point.json"
        write_json(point, {"phase": "看图并用浏览器复现"})
        running = {"topic_id": "tq", "job_id": 3, "status": "running", "user_text": "再看看 Pin\n第二行",
                   "checkpoint_path": str(point), "timings": {"backend_started": NOW - 120}}
        _, state, facts, line = lines(self.state, "oc_dm", self.row("running", running), NOW)
        self.assertEqual((state, line), ("在做", "这一轮：再看看 Pin"))
        self.assertIn("阶段：看图并用浏览器复现", facts)
        self.assertEqual(lines(self.state, "oc_dm", self.row("running", {**running, "status": "queued"}), NOW)[1], "排队")
        # An answer form's turn: its words, not the heading before them.
        answered = {**running, "user_text": "用户补充回答：\n要不要现在开工？\n开工"}
        self.assertEqual(lines(self.state, "oc_dm", self.row("running", answered), NOW)[3], "这一轮：要不要现在开工？")

    def test_a_topic_without_a_title_uses_its_first_message(self):
        write_topic(self.state, {"topic_number": 7, "chat_id": "oc_dm", "root_id": "om_q7"}, topic_id="t7")
        (self.state / "topics" / "t7" / "history.jsonl").write_text(
            json.dumps({"role": "user", "display_content": "看看昨晚的失败日志\n细节"}) + "\n")
        row = {"topic": "t7", "group": "running", "record": {"job_id": 1, "status": "queued"}, "at": NOW}
        self.assertEqual(lines(self.state, "oc_dm", row, NOW)[0], "T7 · 看看昨晚的失败日志")


class EnsureTest(Base):
    def setUp(self):
        super().setUp()
        write_topic(self.state, {"topic_number": 432, "chat_id": "oc_dm", "root_id": "om_q432",
                                 "origin": {"title": "调研"}}, topic_id="tq")
        self.messenger = self.app.messenger
        self.messenger.create_and_send_card = mock.Mock(return_value=("card_s", "om_status"))
        self.messenger.put_card = mock.Mock(return_value={"code": 0})
        self.cards = StatusCards(self.app.runtime, self.messenger, self.app.voice)
        self.running = {"topic": "tq", "group": "running", "at": NOW,
                        "record": {"topic_id": "tq", "job_id": 1, "status": "running", "user_text": "查一下"}}

    def test_sent_once_into_the_thread_then_rewritten_when_it_changes(self):
        self.assertEqual(self.cards.ensure(self.ctx, "oc_dm", [self.running], NOW), {"tq": "om_status"})
        kwargs = self.messenger.create_and_send_card.call_args.kwargs
        self.assertEqual(kwargs["reply_to_message_id"], "om_q432")  # In the topic's thread.
        self.assertEqual(message_topic(self.state, "oc_dm", "om_status"), "tq")
        saved = read_object(record_path(self.state, "tq"))
        self.assertEqual((saved["state"], saved["card_id"], saved["message_id"]), ("sent", "card_s", "om_status"))
        # The same words: nothing written; changed, after WRITE_GAP: one whole-card write with a fresh sequence.
        self.cards.ensure(self.ctx, "oc_dm", [self.running], NOW + 1)
        self.messenger.put_card.assert_not_called()
        done = {**self.running, "group": "result", "record": {**self.running["record"], "status": "done",
                                                               "final_text": "好了"}}
        self.cards.ensure(self.ctx, "oc_dm", [done], NOW + 5)
        self.messenger.put_card.assert_not_called()  # Within WRITE_GAP.
        self.cards.ensure(self.ctx, "oc_dm", [done], NOW + status_cards.WRITE_GAP)
        self.assertEqual(self.messenger.put_card.call_count, 1)
        self.assertGreater(self.messenger.put_card.call_args.kwargs["sequence"], 0)
        self.cards.ensure(self.ctx, "oc_dm", [done], NOW + 3 * status_cards.WRITE_GAP)
        self.assertEqual(self.messenger.put_card.call_count, 1)
        self.messenger.create_and_send_card.assert_called_once()

    def test_an_uncertain_send_is_never_sent_again_and_a_topic_without_a_thread_has_none(self):
        self.messenger.create_and_send_card.return_value = ("card_s", "")
        self.assertEqual(self.cards.ensure(self.ctx, "oc_dm", [self.running], NOW), {})
        self.assertEqual(read_object(record_path(self.state, "tq"))["state"], "uncertain")
        self.assertEqual(self.cards.ensure(self.ctx, "oc_dm", [self.running], NOW + 60), {})
        self.messenger.create_and_send_card.assert_called_once()
        write_topic(self.state, {"topic_number": 9, "chat_id": "oc_dm"}, topic_id="bare")
        self.assertEqual(self.cards.ensure(self.ctx, "oc_dm", [{**self.running, "topic": "bare"}], NOW), {})


class BoardPinsTheCardTest(Base):
    def test_a_topic_with_a_thread_is_pinned_by_its_status_card(self):
        write_topic(self.state, {"topic_number": 401, "chat_id": "oc_dm", "root_id": "om_root_t1"}, topic_id="t1")
        sent = {}

        def create(profile, chat, card, key, **kwargs):
            if kwargs.get("reply_to_message_id"):
                sent["status"] = card
                return "card_status", "om_status_t1"
            return "card1", "om_index1"
        self.app.messenger.create_and_send_card = mock.Mock(side_effect=create)
        self.job("run", 1, timings={"backend_started": NOW - 60}, topic="t1")
        self.overview.tick(self.ctx, NOW)
        self.assertEqual(self.feishu.order(), ["om_status_t1", "om_index1"])
        self.assertEqual(self.record()["pins"][0]["topic_id"], "t1")
        self.assertIn("在做", json.dumps(sent["status"], ensure_ascii=False))

    def test_a_stopped_topic_s_status_card_says_so(self):
        write_topic(self.state, {"topic_number": 401, "chat_id": "oc_dm", "root_id": "om_root_t1"}, topic_id="t1")
        self.app.messenger.create_and_send_card = mock.Mock(side_effect=lambda profile, chat, card, key, **kwargs: (
            ("card_status", "om_status_t1") if kwargs.get("reply_to_message_id") else ("card1", "om_index1")))
        self.overview.tick(self.ctx, NOW)  # The rules start now (stopped_since).
        self.job("run", 1, status="cancelled", topic="t1", answer_message_id="om_answer_t1",
                 timings={"backend_started": NOW + 5, "execution_finished": NOW + 10})
        self.overview.tick(self.ctx, NOW + pins.GAP)
        self.assertEqual(self.feishu.order(), ["om_status_t1", "om_index1"])
        sent = [call.args[2] for call in self.app.messenger.create_and_send_card.call_args_list
                if call.kwargs.get("reply_to_message_id")]
        self.assertIn("<text_tag color='neutral'>已停止</text_tag>", json.dumps(sent[-1], ensure_ascii=False))


class AreaBoardTest(Base):
    """The 话题区 layout (Personal Agent; the owner's D3, 2026-11-08): a topic with a thread is pinned
    by its own cards in its thread, never a status card; one pinned by its status card before moves to them."""

    def setUp(self):
        super().setUp()
        from covey.core import main_thread
        switch_on(self)
        self.addCleanup(main_thread.configure_state, main_thread.configure_state(self.state))
        write_topic(self.state, {"topic_number": 401, "chat_id": "oc_dm", "root_id": "om_root_t1"}, topic_id="t1")
        self.sent = []

        def create(profile, chat, card, key, **kwargs):
            if kwargs.get("reply_to_message_id"):
                self.sent.append(card)
                return "card_status", "om_status_t1"
            return "card1", "om_index1"
        self.app.messenger.create_and_send_card = mock.Mock(side_effect=create)

    def test_its_question_task_and_answer_cards_are_pinned_no_status_card(self):
        self.job("run", 1, timings={"backend_started": NOW - 60}, topic="t1")
        self.overview.tick(self.ctx, NOW)
        self.assertEqual(self.feishu.order(), ["om_run", "om_index1"])  # 在做: its task card.
        self.question("ask", "t1", NOW + 10)
        self.overview.tick(self.ctx, NOW + pins.GAP)
        self.assertEqual(self.feishu.order(), ["om_ask", "om_index1"])  # 等我: the question card in its thread.
        self.question("ask", "t1", NOW + 10, status="answered")
        self.job("run", 1, status="done", timings={"execution_finished": NOW + 3 * pins.GAP},
                 answer_message_id="om_answer_t1")
        self.overview.tick(self.ctx, NOW + 4 * pins.GAP)
        self.assertEqual(self.feishu.order(), ["om_answer_t1", "om_index1"])  # 做完: its answer card.
        self.job("other", 2, status="failed", topic="t2", timings={"execution_finished": NOW + 3 * pins.GAP})
        self.overview.tick(self.ctx, NOW + 6 * pins.GAP)
        self.assertIn("om_other", self.feishu.order())  # No answer card sent apart: its task card.
        self.assertEqual(self.sent, [])  # No status card was sent.
        self.assertFalse((self.state / status_cards.FOLDER).exists())

    def test_a_stopped_turn_is_pinned_by_its_answer_card_else_its_task_card(self):
        self.overview.tick(self.ctx, NOW)  # The rules start now (stopped_since).
        self.job("run", 1, status="cancelled", topic="t1", answer_message_id="om_answer_t1",
                 timings={"backend_started": NOW + 5, "execution_finished": NOW + 10})
        self.overview.tick(self.ctx, NOW + pins.GAP)
        self.assertEqual(self.feishu.order(), ["om_answer_t1", "om_index1"])  # Its ⏹ 已停止 answer card.
        self.job("bare", 2, status="cancelled", topic="t2",
                 timings={"backend_started": NOW + 5, "execution_finished": NOW + 20})
        self.overview.tick(self.ctx, NOW + 3 * pins.GAP)
        self.assertEqual(self.feishu.order(), ["om_bare", "om_answer_t1", "om_index1"])  # No full reply: its task card.
        self.assertEqual(self.sent, [])

    def test_a_status_card_pinned_before_gives_way_to_the_turns_card(self):
        write_json(record_path(self.state, "t1"), {"schema_version": 1, "topic_id": "t1", "chat_id": "oc_dm",
                                                    "state": "sent", "card_id": "card_status",
                                                    "message_id": "om_status_t1", "shown": []})
        self.feishu.pin_info("", "om_status_t1")
        self.feishu.pin_info("", "om_index1")
        self.feishu.created["om_index1"] = int(NOW * 1000) - 5000
        write_json(self.state / pins.RECORD, {
            "schema_version": 1, "chat_id": "oc_dm", "since": NOW - 7 * 86400, "keep_since": NOW - 86400,
            "index_message_id": "om_index1", "results": {}, "dismissed": {},
            "pins": [{"message_id": "om_status_t1", "topic_id": "t1", "group": "waiting", "at": NOW - 100,
                      "create_time": str(self.feishu.created["om_status_t1"])}]})
        # It ran past its question a moment ago: a pin of its own cards would stay put (DEMOTE), not a status card.
        self.job("run", 2, timings={"backend_started": NOW - 30}, topic="t1")
        self.overview.tick(self.ctx, NOW)
        self.assertEqual(self.feishu.order(), ["om_run", "om_index1"])
        self.assertIn(("unpin", "om_status_t1"), self.feishu.calls)  # Off the pins, never recalled.
        self.assertEqual([pin["message_id"] for pin in self.record()["pins"]], ["om_run"])
        self.assertEqual(read_object(record_path(self.state, "t1"))["message_id"], "om_status_t1")  # Kept, readable.
        self.app.messenger.put_card.assert_not_called()
