"""How a butler turn ends on its card (app/butler_reply.py, docs/butler.md 一次请求一张卡): the answer in place,
one line per topic it opened, the butler's wording, and the supplements a stopped or refused turn took."""
from __future__ import annotations

import json
import os
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from support import make_job
from support.butler import CHAT, BUTLER, ButlerCase
from covey.app import butler_reply
from covey.app.turn_delivery import TurnDelivery
from covey.app.worker import STOPPED_NOTE
from covey.core import butler
from covey.core.deliveries import delivery_path, save_delivery
from covey.core.jobs import job_key
from covey.core.state import write_json

REPLY = "## 结论\n两种做法都行，先用第一种。"


class Case(ButlerCase):
    def job(self, topic=BUTLER):
        return make_job(job_id=7, chat_id=CHAT, message_id="om_1", topic_id=topic, progress=SimpleNamespace(),
                        checkpoint_path=str(self.root / "checkpoint.json"))

    def opened(self, name, at, **record):
        return super().opened(name, **{"job_key": job_key("om_1", 7), "at": at, **record})

    def topic(self, topic_id, number, **origin):
        return super().topic(topic_id, topic_id=topic_id, topic_number=number, origin=origin)


class DeliverCase(Case):
    def cards(self, *, taken=True, element=True, limit=1000):
        cards = mock.Mock()
        cards.render_stream_final = lambda job, text, **kwargs: ("↓ " if kwargs.get("answer_elsewhere") else "✅ ") + text
        cards.exceeds_stream_card_limit = lambda text: len(text) > limit
        cards.body_cut.return_value = False
        cards.update_job_streaming_reply.return_value = taken
        cards.set_task_element.return_value = element
        return cards

    def deliver(self, job, cards, *, reply=REPLY, status="done", outcome="completed"):
        verified = SimpleNamespace(text=reply, fingerprint="f1")
        turn = SimpleNamespace(status=status, status_message="", final_text=reply, verified_delivery=verified,
                               verified_outcome=SimpleNamespace(status=outcome, notice=lambda: "", error_code=""),
                               outcome=None, allow_resume=True)
        send_text = mock.Mock(return_value=True)
        delivery = TurnDelivery(job, self.ctx, turn, prefix="p", cards=cards, runtime=mock.Mock(), artifacts=mock.Mock(),
                                questions=mock.Mock(), steering=mock.Mock(), record_job_state=mock.Mock(),
                                send_text=send_text)
        with mock.patch.object(TurnDelivery, "texts", return_value=(reply, reply, False)), \
                mock.patch("covey.app.turn_delivery.close_streaming_reply_async"):
            delivery.deliver(None)
        return delivery, send_text

    def bodies(self, cards):
        return [call.args[2] for call in cards.update_job_streaming_reply.call_args_list]


class InPlaceTest(DeliverCase):
    def test_threaded_the_answer_goes_to_the_top_level_with_a_you_asked_block(self):
        # 2026-11-08 (the owner): the card is in the owner's message's thread; the answer is a message of its own at
        # the top level, quoting nothing, a "你问" block on it (cards/answer_card.py ask_block).
        cards = self.cards()
        with mock.patch("covey.app.answer_cards.asked",
                        return_value={"tag": "interactive_container", "element_id": "answer_ask"}):
            delivery, send_text = self.deliver(self.job(), cards)
        self.assertFalse(delivery.in_place)
        send_text.assert_called_once()
        where = send_text.call_args.args[0]
        self.assertEqual(where.reply_to_message_id, "")
        self.assertEqual(send_text.call_args.kwargs["card"]["body"]["elements"][0]["element_id"], "answer_ask")

    def test_a_card_write_that_fails_sends_the_answer_card_as_before(self):
        cards = self.cards(taken=False)
        delivery, send_text = self.deliver(self.job(), cards)
        self.assertFalse(delivery.in_place)
        send_text.assert_called_once()
        self.assertIsNotNone(send_text.call_args.kwargs["card"])  # The saved answer card, its key the saved one.
        self.assertIn("verified-result", send_text.call_args.args[3])
        self.assertEqual(send_text.call_args.args[2], REPLY)

    def test_an_answer_too_long_for_the_card_goes_as_its_own_card(self):
        cards = self.cards(limit=10)
        delivery, send_text = self.deliver(self.job(), cards)
        self.assertFalse(delivery.in_place)
        send_text.assert_called_once()
        self.assertNotIn("✅ " + REPLY, self.bodies(cards))

    def test_a_card_cut_to_its_size_falls_back(self):
        cards = self.cards()
        cards.body_cut.return_value = True
        delivery, send_text = self.deliver(self.job(), cards)
        self.assertFalse(delivery.in_place)
        send_text.assert_called_once()

    def test_a_turn_that_did_not_complete_keeps_its_answer_card(self):
        cards = self.cards()
        delivery, send_text = self.deliver(self.job(), cards, status="cancelled", outcome="cancelled")
        self.assertFalse(delivery.in_place)
        send_text.assert_called_once()

    def test_a_topic_turn_is_unchanged(self):
        cards = self.cards()
        delivery, send_text = self.deliver(self.job(topic="a" * 32), cards)
        self.assertFalse(delivery.in_place)
        send_text.assert_called_once()


class LeadInPlaceTest(DeliverCase):
    def test_a_leads_conclusion_is_a_message_of_its_own_never_its_card(self):
        # T538: the lead's turn the owner started has a task card once it runs long; its answer went onto it, an edit
        # the owner is not told of. Its row says never (answer_on_card NONE): the answer card goes out instead.
        from covey.core import butler as roles, placement

        row = placement.TABLE[(placement.AREA, placement.LEAD_ROLE)]
        cards = self.cards()
        with mock.patch("covey.app.butler_reply.traits_of", return_value=roles.LEAD), \
                mock.patch("covey.app.butler_reply.placement.plan", return_value=SimpleNamespace(row=row)):
            delivery, send_text = self.deliver(self.job(), cards)
        self.assertFalse(delivery.in_place)
        send_text.assert_called_once()
        self.assertNotIn("✅ " + REPLY, self.bodies(cards))


class SwitchOffTest(DeliverCase):
    switch = "0"

    def test_the_answer_card_goes_out_as_before(self):
        cards = self.cards()
        delivery, send_text = self.deliver(self.job(), cards)
        self.assertFalse(delivery.in_place)
        send_text.assert_called_once()


class TopicLinesTest(DeliverCase):
    def setUp(self):
        super().setUp()
        self.topic("t405", 405, readonly=True)
        self.topic("t403", 403)
        self.topic("t406", 406, title="换个做法")
        self.opened("a", 2, title="迁移数据", topic_id="t405", plan={"workspace": "DataLab", "start": True},
                    answer={"ok": True, "data": {"label": "T405"}})
        self.opened("b", 3, topic_id="t403", answer={"ok": True, "data": {"label": "T403"}})
        self.opened("c", 4, source="t403", fork_topic_id="t406", answer={"ok": True, "data": {}})
        self.opened("d", 5, title="没开成", topic_id="t407", plan={}, answer={"ok": False, "text": "x"})
        self.opened("e", 1, title="别的轮", topic_id="t408", plan={}, answer={"ok": True}, job_key="other")

    def test_one_line_per_topic_it_opened_forked_or_handed_over(self):
        self.assertEqual(butler_reply.opened_lines(self.state, job_key("om_1", 7)), [
            "开了 **T405**「迁移数据」· DataLab · 只读", "转给 **T403**", "从 **T403** 分叉出 **T406**「换个做法」"])

    def test_a_topic_waiting_for_its_launch_card_says_so(self):
        self.opened("a", 2, title="迁移数据", topic_id="t405", plan={"workspace": "DataLab", "start": False},
                    answer={"ok": True})
        self.assertIn("等你点开始", butler_reply.opened_lines(self.state, job_key("om_1", 7))[0])

    def test_the_lines_are_the_body_and_the_answer_is_folded_below(self):
        cards = self.cards()
        delivery, send_text = self.deliver(self.job(), cards)
        self.assertTrue(delivery.in_place)
        send_text.assert_not_called()
        body = self.bodies(cards)[0]
        self.assertIn("开了 **T405**", body)
        self.assertNotIn("两种做法", body)
        fold = cards.set_task_element.call_args.args[2]
        self.assertEqual(fold["tag"], "collapsible_panel")
        self.assertFalse(fold["expanded"])
        self.assertEqual(fold["elements"][0]["content"], REPLY)

    def test_a_fold_that_fails_sends_the_answer_card(self):
        cards = self.cards(element=False)
        delivery, send_text = self.deliver(self.job(), cards)
        self.assertFalse(delivery.in_place)
        send_text.assert_called_once()


class WordingTest(DeliverCase):
    def test_the_butler_is_no_topic(self):
        worded, main = butler.worded, {"topic_id": BUTLER, "chat_id": CHAT}
        self.assertEqual(worded(STOPPED_NOTE, main), "已保存的结果和记录保留在私聊里。")
        self.assertEqual(worded("请先检查任务内容，再在本话题明确提出调整后的要求。", butler.BUTLER),
                         "请先检查任务内容，再在私聊里明确提出调整后的要求。")
        self.assertEqual(worded("排队中 · 本话题前方 1 个任务", main), "排队中 · 管家前方 1 个任务")
        self.assertEqual(worded("请查看本话题的结果卡片。", main), "请查看管家的结果卡片。")
        self.assertEqual(worded(STOPPED_NOTE, {"topic_id": "a" * 32, "chat_id": CHAT}), STOPPED_NOTE)
        self.assertEqual(worded(STOPPED_NOTE, butler.TOPIC), STOPPED_NOTE)

    def test_one_helper_decides_by_the_topic(self):
        self.assertIs(butler.traits(BUTLER, CHAT), butler.BUTLER)
        self.assertIs(butler.traits("a" * 32, CHAT), butler.TOPIC)
        self.assertIs(butler.traits("", CHAT), butler.TOPIC)
        self.assertIs(butler.traits(BUTLER, "oc_other"), butler.TOPIC)
        job = mock.Mock(topic_id=BUTLER, chat_id=CHAT)
        self.assertIs(butler.traits_of(job), butler.BUTLER)
        with mock.patch.dict(os.environ, {butler.ENV: "0"}):
            # Its topic stays no topic's; its session's ways and words go with the switch.
            self.assertIs(butler.traits_of(job), butler.BUTLER_OFF)
            self.assertTrue(butler.is_butler_topic(BUTLER, CHAT))
            self.assertEqual(butler.worded(STOPPED_NOTE, job), STOPPED_NOTE)
        off, on = butler.BUTLER_OFF, butler.BUTLER
        self.assertEqual((off.switch_config, off.rewind, off.card_target, off.numbered, off.top_level, off.listed),
                         (on.switch_config, on.rewind, on.card_target, on.numbered, on.top_level, on.listed))
        self.assertEqual((off.own_turn, off.answers_in_place, off.lists_supplements, off.hold_seconds, off.wording),
                         (False, False, False, 0, ()))

    def test_a_refusal_or_failure_the_bridge_writes_and_never_the_models_reply(self):
        from covey.app.turn_settle import TurnResult, _settle_status
        refusal = "本轮因官方拒绝已停止。请先检查任务内容，再在本话题明确提出调整后的要求。"
        outcome = SimpleNamespace(failure_reply=lambda detail: refusal, job_status="failed", error_code="policy",
                                  policy_event={"kind": "refusal"}, summary=lambda detail="": "官方拒绝")
        for topic, expected in ((BUTLER, "再在私聊里明确提出"), ("a" * 32, "再在本话题明确提出")):
            turn = TurnResult()
            _settle_status(turn, SimpleNamespace(public_reply=None, failure_detail=""), self.job(topic), outcome, "", False)
            self.assertIn(expected, turn.final_text)
        # The verified reply is shown as the model wrote it (rule 5).
        reply = SimpleNamespace(text="这件事在本话题里说过")
        turn = TurnResult(verified_delivery=reply)
        done = SimpleNamespace(job_status="done", error_code="", policy_event=None)
        _settle_status(turn, SimpleNamespace(public_reply=reply, failure_detail=""), self.job(), done, reply.text, False)
        self.assertEqual(turn.final_text, "这件事在本话题里说过")

    def test_a_recovery_card_and_a_steer_receipt(self):
        from covey.app.recovery_cards import recovery_card
        from covey.cards.steer_receipts import receipt_card
        record = {"job_id": 7, "topic_id": BUTLER, "chat_id": CHAT, "status": "failed", "execution_status": "failed",
                  "final_text": "x", "policy_stop": {"kind": "refusal"}}
        text = json.dumps(recovery_card(record), ensure_ascii=False)
        self.assertNotIn("本话题", text)
        receipt = {"job_id": 7, "topic_id": BUTLER, "chat_id": CHAT, "status": "sent", "target_ended": True,
                   "text": "补充", "mid": "om_2"}
        self.assertNotIn("本话题", json.dumps(receipt_card(receipt), ensure_ascii=False))
        receipt["topic_id"] = "a" * 32
        self.assertIn("本话题", json.dumps(receipt_card(receipt), ensure_ascii=False))


class DroppedTest(DeliverCase):
    def supplement(self, mid, text, at, **fields):
        path = delivery_path(self.state, BUTLER, mid)
        path.parent.mkdir(parents=True, exist_ok=True)
        save_delivery(path, {"status": "sent", "text": text, "topic_id": BUTLER, "chat_id": CHAT, "job_id": 7,
                             "job_key": "om_1", "mid": mid, "saved_at": at, **fields})

    def finish(self, status, *, policy=None, job=None):
        job = job or self.job()
        job.timings["received"] = time.time() - 60
        cards = self.cards()
        turn = SimpleNamespace(status=status, status_message="", final_text="出错了",
                               outcome=SimpleNamespace(policy_event=policy) if policy else None)
        delivery = TurnDelivery(job, self.ctx, turn, prefix="p", cards=cards, runtime=mock.Mock(), artifacts=None,
                                questions=None, steering=None, record_job_state=mock.Mock(), send_text=mock.Mock())
        with mock.patch("covey.app.turn_delivery.close_streaming_reply_async"):
            delivery.finish_card(True, status, "", False, False)
        self.sent = [call.args[2] for call in delivery.send_text.call_args_list]
        self.delivery_send = delivery.send_text
        notes = [call.args[2] for call in cards.set_task_element.call_args_list
                 if call.args[2].get("element_id") == butler_reply.DROPPED]
        return notes[0]["content"] if notes else ""

    def setUp(self):
        super().setUp()
        now = time.time()
        self.supplement("om_2", "就用第一种\n别动别的", now - 30, merged=True)
        self.supplement("om_3", "再加一个测试", now - 20, status="read")
        self.supplement("om_4", "这条排到了下一轮", now - 10, status="refused")
        self.supplement("om_5", "别的任务的", now - 10, job_id=8, job_key="om_9")

    def test_a_stopped_turn_lists_the_supplements_it_took(self):
        note = self.finish("cancelled")
        self.assertIn("这一轮停了，期间你发的 2 条补充也一起停了", note)
        self.assertIn("不会自动重发", note)
        self.assertIn("就用第一种 别动别的", note)
        self.assertIn("再加一个测试", note)
        self.assertNotIn("下一轮", note)
        self.assertNotIn("别的任务", note)

    def test_a_refused_turn_too(self):
        self.assertIn("这一轮被拒", self.finish("failed", policy={"kind": "refusal"}))

    def test_a_finished_turn_or_a_topics_lists_nothing(self):
        self.assertEqual(self.finish("done"), "")
        self.assertEqual(self.finish("cancelled", job=self.job(topic="a" * 32)), "")

    def test_in_the_area_layout_a_failure_is_said_at_the_top_level_with_its_you_asked_block(self):
        self.finish("failed")
        call = self.delivery_send.call_args
        self.assertEqual(call.args[0].reply_to_message_id, "")  # A new message: not in the owner's message's thread.
        self.assertEqual(call.args[2], "出错了")
        self.assertIn("出错了", json.dumps(call.kwargs["card"], ensure_ascii=False))

    def test_a_turn_the_policy_guard_stopped_lists_them_on_its_recovery_card(self):
        # app/policy_guard.py finish repaints the card with the recovery card: the runner is gone by then.
        from covey.app.recovery_cards import recovery_card
        from covey.core.model_policy import POLICY_RECOVERY
        record = {"job_id": 7, "message_id": "om_1", "topic_id": BUTLER, "chat_id": CHAT, "status": "cancelled",
                  "execution_status": "cancelled", "policy_stop": {"kind": "refusal"}, "final_text": "被拒。" + POLICY_RECOVERY}
        text = json.dumps(recovery_card(record, state_dir=self.state), ensure_ascii=False)
        self.assertIn("这一轮被拒，期间你发的 2 条补充也一起停了", text)
        self.assertIn("再加一个测试", text)
        self.assertNotIn("本话题", text)
        record["topic_id"] = "a" * 32
        text = json.dumps(recovery_card(record, state_dir=self.state), ensure_ascii=False)
        self.assertNotIn("这一轮被拒", text)
        self.assertIn("本话题", text)

    def test_without_supplements_nothing(self):
        for path in (self.state / "interaction-deliveries").glob("*.json"):
            path.unlink()
        self.assertEqual(self.finish("cancelled"), "")


if __name__ == "__main__":
    unittest.main()


class SlimLinesTest(DeliverCase):
    """A slim card's body when the turn sent work to topics (app/butler_reply.py opened_lines, routed_body)."""

    def setUp(self):
        super().setUp()
        self.topic("t405", 405, readonly=True)
        self.topic("t403", 403)
        history = self.state / "topics" / "t403" / "history.jsonl"
        history.write_text(json.dumps({"role": "user", "content": "迁移数据\n细节"}, ensure_ascii=False) + "\n")
        self.opened("a", 2, title="查日志", topic_id="t405", plan={"workspace": "DataLab", "start": True},
                    answer={"ok": True, "data": {"label": "T405"}})
        self.opened("b", 3, topic_id="t403", note="顺便把日志也看一下", answer={"ok": True, "data": {"label": "T403"}})

    def test_each_topic_line_leads_with_its_arrow_and_a_hand_over_names_title_and_note(self):
        self.assertEqual(butler_reply.opened_lines(self.state, job_key("om_1", 7), slim=True), [
            "↪ 开了 **T405**「查日志」· DataLab · 只读", "↪ 转给 **T403**「迁移数据」：顺便把日志也看一下"])

    def test_a_hand_over_recorded_before_the_note_was_kept_names_the_title_only(self):
        self.opened("b", 3, topic_id="t403", answer={"ok": True, "data": {"label": "T403"}})
        self.assertEqual(butler_reply.opened_lines(self.state, job_key("om_1", 7), slim=True)[1],
                         "↪ 转给 **T403**「迁移数据」")

    def test_the_body_is_the_lines_alone_in_the_area_layout_and_the_reply_goes_in_the_details(self):
        job = self.job()
        job.progress = SimpleNamespace(slim=True)
        cards = self.cards()
        delivery, send_text = self.deliver(job, cards)
        self.assertTrue(delivery.in_place)
        body = self.bodies(cards)[0]
        self.assertIn("↪ 转给 **T403**「迁移数据」：顺便把日志也看一下", body)
        self.assertNotIn("结论出来后会回到这里", body)  # S3: each outcome comes to the chat on its own.
        call = cards.set_task_element.call_args
        self.assertEqual(call.args[2]["element_id"], butler_reply.REPLY_FOLD)
        self.assertFalse(call.kwargs.get("under", False))  # Into 详情, after the body's anchor.

    def test_a_card_sent_before_keeps_its_lines(self):
        cards = self.cards()
        self.deliver(self.job(), cards)
        body = self.bodies(cards)[0]
        self.assertNotIn("↪", body)
        self.assertNotIn("结论出来后会回到这里", body)


class OutcomeWordsTest(DeliverCase):
    """A butler card's last status line and chat-list preview say what the turn did (V6/S13, 2026-11-08)."""

    def setUp(self):
        super().setUp()
        self.topic("t444", 444)
        self.topic("t442", 442)
        self.key = job_key("om_1", 7)

    def words(self, reply=REPLY):
        return butler_reply.outcome_words(self.state, self.key, reply)

    def test_an_answer_says_its_lead_after_its_heading(self):
        self.assertEqual(self.words(), "✅ 已答：两种做法都行，先用第一种")
        self.assertEqual(self.words("**好**"), "✅ 已答：好")
        self.assertEqual(self.words(""), "")

    def test_each_kind_of_hand_on_and_more_than_one(self):
        self.opened("a", 2, title="查日志", topic_id="t444", plan={"start": True}, answer={"ok": True, "data": {}})
        self.assertEqual(self.words(), "↪ 开了 T444「查日志」")
        self.opened("a", 2, topic_id="t442", answer={"ok": True, "data": {}})
        self.assertEqual(self.words(), "↪ 交给 T442 接着做")
        self.opened("a", 2, topic_id="t444", answer={"ok": True, "data": {"steered": True}})
        self.assertEqual(self.words(), "↪ 补充给 T444（它正在跑）")
        self.opened("b", 3, topic_id="t442", answer={"ok": True, "data": {}})
        self.assertEqual(self.words(), "↪ 拆成 2 件")
        self.opened("c", 4, topic_id="t445", answer={"ok": False})  # Not done: not counted.
        self.assertEqual(self.words(), "↪ 拆成 2 件")

    def test_the_card_says_it_once_delivered(self):
        job = self.job()
        cards = self.cards()
        self.deliver(job, cards)
        cards.say_outcome.assert_called_once_with(self.ctx, job, "✅ 已答：两种做法都行，先用第一种")

    def test_desk_topics_left_true_too(self):
        write_json(self.state / "main-thread.json", {"desk_topics": True})  # Retired 2026-11-09: as without it.
        cards = self.cards()
        self.deliver(self.job(), cards)
        cards.say_outcome.assert_called_once()

    def test_a_topics_card_a_failed_turn_and_the_butler_off_keep_their_line(self):
        cards = self.cards()
        self.deliver(self.job(topic="t444"), cards)
        cards.say_outcome.assert_not_called()
        cards = self.cards()
        self.deliver(self.job(), cards, status="failed")
        cards.say_outcome.assert_not_called()
        with mock.patch.dict(os.environ, {butler.ENV: "0"}):
            cards = self.cards()
            self.deliver(self.job(), cards)
        cards.say_outcome.assert_not_called()


class SupplementPreviewTest(Case):
    """The reply context a message is sent to the model with never shows in a supplement's preview (T3027)."""

    TEXT = "看看这两个样本\n\n回复上下文：\n- parent_id: `om_x100b61e3185454b8`\n- root_id: `om_root`"

    def test_the_preview_stops_before_the_reply_context(self):
        from covey.core.deliveries import shown_preview
        self.assertEqual(shown_preview({"text": self.TEXT}), "看看这两个样本\n")
        self.assertEqual(shown_preview("开头\n话题起始消息（背景参考）：\n- parent_id: `om_1`"), "开头")
        self.assertEqual(shown_preview({"text": "没有上下文"}), "没有上下文")

    def test_the_latest_supplement_line_and_the_dropped_list(self):
        from covey.app.steering import Steering
        fake = SimpleNamespace(cards=mock.Mock())
        job = self.job()
        Steering.note_latest_request(fake, self.ctx, job, self.TEXT)
        line = fake.cards.set_task_element.call_args.args[2]["content"]
        self.assertIn("看看这两个样本", line)
        self.assertNotIn("回复上下文", line)  # Its words are escaped on the card: parent\_id.
        self.assertNotIn("parent", line)
        path = delivery_path(self.state, BUTLER, "om_2")
        path.parent.mkdir(parents=True, exist_ok=True)
        save_delivery(path, {"status": "sent", "text": self.TEXT, "topic_id": BUTLER, "chat_id": CHAT, "job_id": 7,
                             "job_key": "om_1", "mid": "om_2", "saved_at": time.time()})
        listed = butler_reply.dropped_lines(self.state, {"job_key": "om_1", "job_id": 7, "topic_id": BUTLER}, False)
        self.assertIn("看看这两个样本", listed)
        self.assertNotIn("回复上下文", listed)
        self.assertNotIn("parent", listed)


class SlimCardTest(Case):
    """The butler's slim task card (cards/streaming.py build_stream_card, app/task_card.py StreamingReply.slim)."""

    def build(self, topic=BUTLER, content="**执行进度**\n📌 阶段：已入队"):
        from covey.cards.streaming import build_stream_card
        from support.cards import sample_job, stream_runtime
        job = sample_job(topic_id=topic, chat_id=CHAT, topic_label="")
        return build_stream_card(content, "任务", stream_runtime(), job=job, include_stop_action=True)

    def test_no_title_bar_the_body_one_line_and_the_rest_folded_in_details(self):
        from covey.cards.elements import find_element
        from covey.cards.streaming import SLIM_DETAILS_ID
        card = self.build()
        self.assertNotIn("header", card)
        answer, activity, details = card["body"]["elements"]
        self.assertEqual((answer["element_id"], answer["content"]), ("answer", ""))
        self.assertEqual(activity["element_id"], "activity")
        self.assertEqual(activity["content"], "⏳ 已入队")  # The queued stage, no ## heading.
        self.assertEqual(details["element_id"], SLIM_DETAILS_ID)
        self.assertEqual(details["tag"], "collapsible_panel")
        self.assertFalse(details["expanded"])
        # The 输入/输出 box first (the "E" sample, 2026-11-08; one line of tiles before), its 输出 by their old ID.
        self.assertEqual([e["element_id"] for e in details["elements"]],
                         ["task_io", "current_question", "action_row"])  # 任务详情 once it has words (2026-11-09).
        self.assertIsNotNone(find_element(card, "task_figures"))
        self.assertIsNotNone(find_element(card, "job_stop_action"))
        self.assertIsNotNone(find_element(card, "steer_action"))
        self.assertRegex(SLIM_DETAILS_ID, r"^[A-Za-z][A-Za-z0-9_]{0,19}$")

    def test_a_topics_card_and_the_butlers_with_the_switch_off_are_as_before(self):
        self.assertIn("header", self.build(topic="a" * 32))
        with mock.patch.dict(os.environ, {butler.ENV: "0"}):
            card = self.build()
        self.assertIn("header", card)
        self.assertEqual(card["body"]["elements"][0]["element_id"], "activity")

    def test_the_status_line(self):
        from covey.cards.task_status import slim_status_markdown, status_elements
        from covey.cards.ui import state_word
        self.assertEqual(slim_status_markdown("在做 · 已运行 6 秒 · 阶段：读日志\n现在：Read"),
                         state_word("running") + " · 已运行 6 秒 · <font color='grey'>阶段：读日志 · 现在：Read</font>")
        self.assertEqual(slim_status_markdown("有结果 · 用时 13 秒"), state_word("result") + " · 用时 13 秒")
        self.assertEqual(status_elements("已停止 · 用时 2 秒", False, True)[0]["content"],
                         state_word("cancelled") + " · 用时 2 秒")

    def test_the_progress_knows_it_and_anchors_in_the_details(self):
        from covey.app.task_card import StreamingCard, StreamingReply, body_anchor, bottom_anchor, under_body
        from support.cards import sample_job
        created = {}
        messenger = SimpleNamespace(create_card=lambda profile, card: {"data": {"card_id": "card_1"}})
        stream = StreamingCard("t", "t", "answer", lambda **kw: created.update(kw) or StreamingReply(**kw),
                               lambda _: None, lambda s: s, lambda *a: True, lambda s, n: s[:n], messenger,
                               lambda *a, **k: ("om_card", False))
        progress = stream.create(None, sample_job(topic_id=BUTLER, chat_id=CHAT, topic_label=""), "**执行进度**\n📌 阶段：已入队")
        self.assertTrue(progress.slim)
        self.assertTrue(progress.dashboard)
        self.assertTrue(progress.io_box)
        # What goes after the body goes after the 输入/输出 box: after its 输出 text it would land in the box.
        # The bottom is 本轮请求 while the card has no 任务详情 (2026-11-09), 任务详情 once it has.
        self.assertEqual((body_anchor(progress), under_body(progress), bottom_anchor(progress, True)),
                         ("task_io", "answer", "current_question"))
        self.assertLess(progress.others["slim_details"], progress.others["task_io"])  # The fold alone: its parts
        self.assertIn("current_question", progress.others)  # are counted by their own IDs, as they are written.
        progress.details_panel = True
        self.assertEqual(bottom_anchor(progress, True), "task_details")
        old = StreamingReply(card_id="c", message_id="m", dashboard=True)
        self.assertEqual((body_anchor(old), under_body(old), bottom_anchor(old, True)),
                         ("answer", "answer", "task_details"))
        sent_before = StreamingReply(card_id="c", message_id="m", dashboard=True, slim=True)  # Its tiles' line.
        self.assertEqual(body_anchor(sent_before), "task_figures")

    def test_an_answer_has_no_title_on_it(self):
        from covey.cards.review import render_stream_final
        job = SimpleNamespace(job_id=7)
        self.assertEqual(render_stream_final(job, "正文", bare=True), "正文")
        self.assertEqual(render_stream_final(job, "正文"), "**答案**\n正文")
        self.assertEqual(render_stream_final(job, "出错", status="failed", bare=True), "**停住了**\n出错")

    def test_what_stays_under_the_body_and_what_goes_in_the_details(self):
        from covey.app.cards import Cards
        from covey.app.task_card import StreamingReply
        from covey.cards.elements import md
        fake = SimpleNamespace(_write=mock.Mock(return_value={}), _write_failed=mock.Mock())
        job = SimpleNamespace(progress=StreamingReply(card_id="c", message_id="m", slim=True, dashboard=True),
                              interactive_card_id=None, interactive_elements=set(), trace_rendered_panel=None,
                              trace_rendered_card="")

        def target(element, **kwargs):
            Cards.set_task_element(fake, self.ctx, job, element, **kwargs)
            return fake._write.call_args.args[4]["target_element_id"]

        self.assertEqual(target(md("x", element_id=butler_reply.DROPPED), under=True), "answer")
        self.assertEqual(target(md("q", element_id="q_0123")), "answer")
        self.assertEqual(target(butler_reply.reply_fold("words")), "task_figures")
        self.assertEqual(target(md("b", element_id="turn_export"), at_bottom=True), "task_details")


    def test_a_queued_body_and_a_records_status_line(self):
        from covey.app.cards import Cards
        from covey.app.record_cards import RecordCards
        from covey.app.task_card import StreamingReply
        job = SimpleNamespace(progress=StreamingReply(card_id="c", message_id="m", slim=True))
        self.assertEqual(Cards.render_stream_status(SimpleNamespace(), job, "正在停止", ["已收到"]),
                         "<font color='grey'>正在停止 · 已收到</font>")
        fake = SimpleNamespace(_record_part=mock.Mock(return_value=True))
        RecordCards.set_record_activity(fake, self.ctx, {"topic_id": BUTLER, "chat_id": CHAT}, "↪ 已回答 → #8")
        self.assertEqual(json.loads(fake._record_part.call_args.args[5]["partial_element"])["content"], "↪ 已回答 → #8")
        RecordCards.set_record_activity(fake, self.ctx, {"topic_id": "a" * 32, "chat_id": CHAT}, "↪ 已回答 → #8")
        self.assertEqual(json.loads(fake._record_part.call_args.args[5]["partial_element"])["content"], "## ↪ 已回答 → #8")


class PolicyWordingTest(Case):
    def test_the_policy_guards_saved_text_is_in_the_butlers_words(self):
        from covey.app import policy_guard
        from covey.core.jobs import find_job_record, upsert_job_record
        upsert_job_record(self.state, "k1", job_id=7, backend="none", status="running", topic_id=BUTLER, chat_id=CHAT)
        path = policy_guard.intent_path(self.state, "k1")
        event = {"kind": "refusal", "category": "frontier_llm", "scope": "main", "child_id": "", "source_model": "",
                 "target_model": ""}
        write_json(path, {"key": "k1", "job_id": 7, "observation": {"event": event}, "phase": "stopped",
                          "notice": "pending", "attempts": 0, "next_attempt": 0, "card_updated": False})
        policy_guard.finish(SimpleNamespace(apps={}), self.ctx, path)
        text = find_job_record(self.state, "k1")["final_text"]
        self.assertIn("再在私聊里明确提出", text)
        self.assertNotIn("本话题", text)


class ElementIdTest(unittest.TestCase):
    """CardKit takes an element_id of letters, digits and _, a letter first, at most 20 (code 300301 otherwise:
    "main_dropped_supplements" was refused when 2026-11-06's CardKit check built it)."""

    def ids(self, element):
        if isinstance(element, dict):
            yield from ([element["element_id"]] if "element_id" in element else [])
            for value in element.values():
                yield from self.ids(value)
        elif isinstance(element, list):
            for value in element:
                yield from self.ids(value)

    def test_the_elements_a_butler_turn_adds_have_ids_cardkit_takes(self):
        from covey.cards.elements import md
        found = [*self.ids(butler_reply.reply_fold("管家的回答正文")),
                 *self.ids(md("这一轮停了", element_id=butler_reply.DROPPED))]
        self.assertIn(butler_reply.DROPPED, found)
        self.assertIn(butler_reply.REPLY_FOLD, found)
        for element_id in found:
            self.assertRegex(element_id, r"^[A-Za-z][A-Za-z0-9_]{0,19}$")
