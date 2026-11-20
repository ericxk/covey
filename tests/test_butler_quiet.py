"""A butler turn of the 话题区 layout opens no thread of its own (D1, 2026-11-08: 提问不开串; core/placement.py
Row.quiet): no card until it has run CARD_AFTER seconds, reactions on the owner's message, its failures, questions and
"你问" links at the top level. The butler off is as before; main-thread.json's desk_topics, retired on
2026-11-09, changes nothing."""
from __future__ import annotations

import json
import os
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from support import make_job
from support.butler import CHAT, BUTLER, ButlerCase
from covey.app import answer_cards, auto_retry, butler_reactions, butler_reply
from covey.app.cards import Cards
from covey.app.question_display import QuestionDisplay, standalone_card
from covey.app.turn_delivery import TurnDelivery
from covey.core import butler, placement
from covey.core.jobs import find_job_record, job_key, upsert_job_record
from covey.core.deliveries import delivery_path, save_delivery
from covey.core.state import write_json
from covey.feishu.messenger import Messenger

REPLY = "放到 T444「查日志」里做"
ASK = {"tag": "interactive_container", "element_id": "answer_ask"}
REFUSAL = "本轮已停止：主任务触发官方内容安全拒绝（上游未提供类别）。\n\n不会自动重试或自动检查并继续。"
REFUSED = SimpleNamespace(policy_event={"kind": "refusal"}, job_status="cancelled")


class Case(ButlerCase):
    def desk(self):
        """main-thread.json as a deployment that had desk_topics on left it: retired (2026-11-09), read as off."""
        write_json(self.state / "main-thread.json", {"desk_topics": True})

    def job(self, topic=BUTLER, message="om_1", progress=None, **fields):
        return make_job(job_id=7, chat_id=CHAT, message_id=message, topic_id=topic, progress=progress,
                        checkpoint_path=str(self.root / "checkpoint.json"), **fields)

    def opened(self, **record):
        return super().opened("a", **{"job_key": job_key("om_1", 7), "at": 2, "topic_id": "t444", "title": "查日志",
                                      "plan": {"start": True}, "answer": {"ok": True, "data": {"label": "T444"}},
                                      **record})


class PlacementTest(Case):
    def test_only_the_butlers_own_messages_in_the_area_layout(self):
        self.assertTrue(placement.quiet(self.state, self.job()))
        self.assertTrue(placement.quiet(self.state, {"message_id": "om_1", "topic_id": BUTLER, "chat_id": CHAT}))
        self.assertTrue(placement.marked(self.state, self.job()))
        self.assertFalse(placement.quiet(self.state, self.job(message="main-tool-1")))  # A butler tool's turn.
        self.assertFalse(placement.quiet(self.state, self.job(topic="a" * 32)))  # A topic's.
        self.desk()
        self.assertTrue(placement.quiet(self.state, self.job()))

    def test_an_answers_continuation_is_quiet_with_no_message_to_react_on(self):
        continuation = self.job(message="answer-tok")
        self.assertTrue(placement.quiet(self.state, continuation))
        self.assertFalse(placement.marked(self.state, continuation))
        self.desk()
        self.assertTrue(placement.quiet(self.state, continuation))

    def test_not_with_the_butler_off(self):
        with mock.patch.dict(os.environ, {butler.ENV: "0"}):
            self.assertFalse(placement.quiet(self.state, self.job()))


class RevealTest(Case):
    """The held-back card goes out from the task runner's status ticks once the turn has run CARD_AFTER seconds."""

    def cards(self):
        cards = Cards.__new__(Cards)
        cards.runtime = mock.Mock()
        cards.create_streaming_reply = mock.Mock(return_value=SimpleNamespace(card_id="c9", message_id="om_card"))
        return cards

    def test_not_before_and_once_after(self):
        cards = self.cards()
        job = self.job(card_deferred=True)
        job.timings["backend_requested"] = time.time() - 30
        self.assertFalse(cards.update_card_activity(self.ctx, job, "⏳ 执行中"))
        cards.create_streaming_reply.assert_not_called()
        job.timings["backend_requested"] = time.time() - butler.CARD_AFTER - 1
        self.assertTrue(cards.reveal(self.ctx, job, "⏳ 执行中"))
        self.assertEqual(job.progress.card_id, "c9")
        self.assertFalse(job.card_deferred)
        record = find_job_record(self.state, job_key("om_1", 7))
        self.assertEqual((record["card_id"], record["card_message_id"]), ("c9", "om_card"))
        self.assertIn("执行中", cards.create_streaming_reply.call_args.args[2])

    def test_a_card_not_held_a_turn_stopping_or_delivering_gets_none(self):
        cards = self.cards()
        for fields in ({}, {"card_deferred": True, "cancel_requested_at": 1.0},
                       {"card_deferred": True, "answer_finalized": True}):
            job = self.job(**fields)
            job.timings["backend_requested"] = time.time() - 600
            self.assertFalse(cards.reveal(self.ctx, job, "x"))
        cards.create_streaming_reply.assert_not_called()


class DeliverCase(Case):
    card_limit = 1000

    def cards(self):
        cards = mock.Mock()
        cards.render_stream_final = lambda job, text, **kwargs: "✅ " + text
        cards.exceeds_stream_card_limit = lambda text: len(text) > self.card_limit
        cards.body_cut.return_value = False
        cards.update_job_streaming_reply.return_value = True
        return cards

    def deliver(self, job, *, status="done", reply=REPLY, verified=True, react=True, outcome=None, retry=None):
        delivered = SimpleNamespace(text=reply, fingerprint="f1") if verified else None
        turn = SimpleNamespace(status=status, status_message="", final_text=reply, verified_delivery=delivered,
                               verified_outcome=SimpleNamespace(status="completed", notice=lambda: "", error_code=""),
                               outcome=outcome, allow_resume=True)
        self.send_text = mock.Mock(return_value=True)
        self.react = mock.Mock(return_value=react)
        self.artifacts = mock.Mock()
        delivery = TurnDelivery(job, self.ctx, turn, prefix="p", cards=self.cards(), runtime=mock.Mock(),
                                artifacts=self.artifacts, questions=mock.Mock(), steering=mock.Mock(),
                                record_job_state=mock.Mock(), send_text=self.send_text, react=self.react,
                                retry=retry)
        with mock.patch.object(TurnDelivery, "texts", return_value=(reply, reply, False)), \
                mock.patch.object(TurnDelivery, "ask_block", return_value=ASK), \
                mock.patch.object(TurnDelivery, "answer_tail", return_value=([], False, None)), \
                mock.patch("covey.app.turn_delivery.close_streaming_reply_async"):
            delivery.deliver(None)
            butler_reply.react_end(delivery)
        return delivery

    def emojis(self):
        return [call.args[1] for call in self.react.call_args_list]


class DeliverTest(DeliverCase):
    def test_an_answer_goes_to_the_top_level_and_the_message_says_done(self):
        delivery = self.deliver(self.job(), reply="两种做法都行。")
        self.send_text.assert_called_once()
        self.assertEqual(self.send_text.call_args.args[0].reply_to_message_id, "")
        self.assertEqual(self.emojis(), [butler_reactions.DONE])
        self.assertFalse(delivery.in_place)

    def test_a_turn_that_only_handed_work_on_sends_no_answer_and_says_get(self):
        self.opened()
        delivery = self.deliver(self.job())
        self.send_text.assert_not_called()
        self.assertTrue(delivery.in_place)
        self.assertEqual(self.emojis(), [butler_reactions.GOT])  # Once: react_end leaves it.

    def test_a_turn_that_handed_work_to_a_topic_there_hooks_it_under_the_owners_message(self):
        # The owner, 2026-11-10 17:59: his message showed Get alone, nothing led to the topic it went to.
        from covey.core.topics import message_topic
        self.topic("t453", chat_id=CHAT, root_id="om_t453", origin={"title": "把这块代码提交到 GitHub 上去的那件事"},
                   topic_number=453)
        self.topic("t454", chat_id=CHAT, root_id="om_t454", title="看板", topic_number=454)
        self.opened(plan=None, topic_id="t453", thread_message_id="om_line",
                    answer={"ok": True, "data": {"label": "T453"}})
        super(Case, self).opened("b", job_key=job_key("om_1", 7), at=3, topic_id="t454", thread_message_id="",
                                 answer={"ok": True, "data": {"label": "T454"}})
        super(Case, self).opened("c", job_key=job_key("om_1", 7), at=4, topic_id="t444", title="新开的",
                                 plan={"start": True}, answer={"ok": True, "data": {"label": "T444"}})
        cards = DeliverCase.cards(self)
        cards.messenger.create_and_send_card.return_value = ("card-h", "om_hook")
        links = {"t453": ("https://link/t453", "thread_at"), "t454": ("", "")}
        with mock.patch.object(DeliverCase, "cards", return_value=cards), \
                mock.patch("covey.app.topic_links.topic_thread_link",
                           side_effect=lambda state, topic, **kwargs: links[topic]) as thread_link:
            delivery = self.deliver(self.job(reaction_message="om_1"))
        self.assertEqual(self.emojis(), [butler_reactions.GOT])
        self.send_text.assert_not_called()
        self.assertEqual(thread_link.call_args_list[0].kwargs["at"], ["om_line"])
        profile, chat, built, key = cards.messenger.create_and_send_card.call_args.args
        self.assertEqual(cards.messenger.create_and_send_card.call_args.kwargs["reply_to_message_id"], "om_1")
        self.assertFalse(cards.messenger.create_and_send_card.call_args.kwargs["in_thread"])  # No thread (D1).
        self.assertEqual(built["config"]["width_mode"], "compact")  # Small (ACCEPT-351).
        first, second = built["body"]["elements"]  # The topics that were there; the one it opened hangs below.
        self.assertEqual(first["behaviors"], [{"type": "open_url", "default_url": "https://link/t453"}])
        self.assertIn("→ T453「把这块代码提交到 GitHub 上去的…」　›", json.dumps(first, ensure_ascii=False))
        self.assertNotIn("behaviors", second)  # No link: not clickable, no ›, and said.
        self.assertNotIn("›", json.dumps(second, ensure_ascii=False))
        self.assertTrue(any("hook link missing" in str(c.args[0]) for c in delivery.runtime.log.call_args_list))
        self.assertEqual(message_topic(self.state, CHAT, "om_hook"), "")  # Two topics: a quote of it stays here.

    def test_one_topic_hooked_takes_a_quote_of_the_hook(self):
        from covey.core.topics import message_topic
        self.topic("t453", chat_id=CHAT, root_id="om_t453", title="同步", topic_number=453)
        self.opened(plan=None, topic_id="t453", answer={"ok": True, "data": {"label": "T453"}})
        cards = DeliverCase.cards(self)
        cards.messenger.create_and_send_card.return_value = ("card-h", "om_hook")
        with mock.patch.object(DeliverCase, "cards", return_value=cards), \
                mock.patch("covey.app.topic_links.topic_thread_link", return_value=("https://link", "thread")):
            self.deliver(self.job())
        self.assertEqual(message_topic(self.state, CHAT, "om_hook"), "t453")

    def test_a_turn_that_only_opened_a_topic_gets_no_hook(self):
        self.opened()
        cards = DeliverCase.cards(self)
        with mock.patch.object(DeliverCase, "cards", return_value=cards):
            self.deliver(self.job())
        cards.messenger.create_and_send_card.assert_not_called()

    def test_a_turn_that_answered_and_handed_work_on_sends_its_answer(self):
        self.opened()
        delivery = self.deliver(self.job(), reply="服务状态：三台都正常。看板顺序放到 T444「查日志」里做。")
        self.send_text.assert_called_once()
        self.assertEqual(self.send_text.call_args.args[0].reply_to_message_id, "")
        self.assertFalse(delivery.in_place)

    def test_a_topic_waiting_for_its_start_gets_the_answer(self):
        self.opened(plan={"start": False})
        self.deliver(self.job())
        self.send_text.assert_called_once()

    def test_hand_off_sentences_alone(self):
        for text in ("好的，放到 **T444**「查日志」里做。", "接着在 T432 里做", "已交给 T12 处理了",
                     "放到 T1「甲」里做，接着在 T2 里做。"):
            self.assertTrue(butler_reply.only_handoff(text), text)
        for text in ("服务状态：三台都正常。放到 T1「看板」里做", "两种做法都行。", "接着在 T2 里做，另外日志里没有报错"):
            self.assertFalse(butler_reply.only_handoff(text), text)

    def test_an_answer_that_could_not_be_sent_says_so_at_the_top_level_with_its_button(self):
        upsert_job_record(self.state, job_key("om_1", 7), job_id=7, message_id="om_1", chat_id=CHAT, topic_id=BUTLER)
        send = mock.Mock(side_effect=[False, True])
        with mock.patch("covey.app.quiet_delivery.retry_label", return_value="重新投递结果"):
            self.deliver_failing(send)
        self.assertEqual(len(send.call_args_list), 2)
        notice = send.call_args_list[1]
        self.assertEqual(notice.args[0].reply_to_message_id, "")
        self.assertIn("没有发出去", notice.args[2])
        card = json.dumps(notice.kwargs["card"], ensure_ascii=False)
        self.assertIn("重新投递结果", card)
        self.assertIn("answer_ask", card)
        self.assertEqual(self.emojis(), [butler_reactions.FAILED])

    def deliver_failing(self, send):
        original = TurnDelivery.__init__

        def init(delivery, *args, **kwargs):
            original(delivery, *args, **kwargs)
            delivery.send_text = send
        with mock.patch.object(TurnDelivery, "__init__", init):
            self.deliver(self.job(), reply="两种做法都行。")

    def test_when_the_reaction_fails_the_answer_goes_out(self):
        self.opened()
        self.deliver(self.job(), react=False)
        self.send_text.assert_called_once()
        self.assertEqual(self.send_text.call_args.args[0].reply_to_message_id, "")

    def test_a_failure_is_a_short_top_level_message_with_its_you_asked_block(self):
        self.deliver(self.job(), status="failed", reply="任务 #7 处理出错（X），未满足结果发布条件。", verified=False)
        call = self.send_text.call_args
        self.assertEqual(call.args[0].reply_to_message_id, "")
        self.assertEqual(call.kwargs["card"]["body"]["elements"][0], ASK)
        self.assertEqual(self.emojis(), [butler_reactions.FAILED])

    def test_a_long_failure_says_its_first_line_and_a_retried_one_says_the_retry(self):
        self.deliver(self.job(), status="failed", reply="本轮任务未全部完成。上游 502。\n" + "细节" * 400, verified=False)
        card = self.send_text.call_args.kwargs["card"]
        self.assertEqual(self.send_text.call_args.args[2], "本轮任务未全部完成。上游 502。")
        self.assertNotIn("细节", json.dumps(card, ensure_ascii=False))
        self.artifacts.send_result_file.assert_not_called()
        self.deliver(self.job(), status="failed", reply="本轮任务未全部完成。上游 429。", verified=False,
                     retry=auto_retry.Planned(time.time() + 60, 1))
        self.assertIn("第 1/", self.send_text.call_args.args[2])
        self.assertEqual(self.send_text.call_args.args[0].reply_to_message_id, "")

    def test_a_failure_with_a_saved_reply_says_it_failed_and_where_the_answer_went(self):
        # B4 review: the notice said the answer's first line again, not that the turn failed.
        reply = "结论：A 比 B 快一倍。\n细节在下面。"
        self.deliver(self.job(progress=SimpleNamespace(slim=True)), status="failed", reply=reply)
        top = [call for call in self.send_text.call_args_list if call.args[0].reply_to_message_id == ""]
        self.assertEqual(len(top), 2)  # The answer card, then the notice.
        self.assertEqual(top[0].args[2], reply)
        notice = top[1].args[2]
        self.assertEqual(notice, "这一轮没做完。回答已经单独发在聊天区，这里不重复。")
        self.assertEqual(top[1].kwargs["card"]["config"]["summary"]["content"], "⚠️ 没做完：回答已经单独发在聊天区，这里不重复")
        self.assertNotIn("A 比 B", json.dumps(top[1].kwargs["card"], ensure_ascii=False))
        self.assertEqual(top[1].kwargs["card"]["body"]["elements"][0], ASK)

    def test_a_failure_with_a_saved_reply_sent_as_a_file_says_the_file(self):
        self.card_limit = 30000  # Over 20000 bytes it goes as a file (saved_delivery_kind), within a card's limit.
        reply = "长" * 7000
        self.deliver(self.job(progress=SimpleNamespace(slim=True)), status="failed", reply=reply)
        self.artifacts.send_result_file.assert_called_once()
        self.assertEqual(self.artifacts.send_result_file.call_args.args[0].reply_to_message_id, "")
        notice = self.send_text.call_args.args[2]
        self.assertEqual(notice, "这一轮没做完。回答太长，作为文件发在聊天区。")
        self.assertNotIn("长长", notice)

    def test_a_refusal_with_no_card_is_a_short_top_level_message_and_says_crossmark(self):
        self.deliver(self.job(), status="cancelled", reply=REFUSAL, verified=False, outcome=REFUSED)
        self.send_text.assert_called_once()
        call = self.send_text.call_args
        self.assertEqual(call.args[0].reply_to_message_id, "")
        self.assertEqual(call.args[2], REFUSAL.splitlines()[0])
        self.assertEqual(call.kwargs["card"]["body"]["elements"][0], ASK)
        self.assertTrue(call.kwargs["card"]["config"]["summary"]["content"].startswith("❌ 被拒："))  # Not ⏹.
        self.assertNotIn("#7", call.kwargs["card"]["config"]["summary"]["content"])
        self.assertEqual(self.emojis(), [butler_reactions.FAILED])

    def test_a_failure_with_no_reply_says_no_task_number(self):
        self.deliver(self.job(), status="failed", reply="", verified=False)
        call = self.send_text.call_args
        self.assertEqual(call.args[2], "这一轮没做完。")
        self.assertEqual(call.kwargs["card"]["config"]["summary"]["content"], "⚠️ 没做完")

    def test_its_own_question_card_at_the_top_level_has_no_task_number(self):
        from covey.cards.question_cards import question_card
        record = {"status": "pending", "job_id": 12, "token": "tok", "blocking": True, "topic_id": BUTLER,
                  "questions": [{"id": "q", "header": "范围", "question": "查哪台？", "options": []}]}
        self.assertIn("#12", json.dumps(question_card(record), ensure_ascii=False))
        top = json.dumps(question_card({**record, "mirror_state": "at_top"}), ensure_ascii=False)
        self.assertNotIn("#12", top)
        self.assertIn("等你", top)

    def test_a_refusal_with_its_late_card_is_said_at_the_top_level_too(self):
        self.deliver(self.job(progress=SimpleNamespace(slim=True)), status="cancelled", reply=REFUSAL, verified=False,
                     outcome=REFUSED)
        top = [call for call in self.send_text.call_args_list if call.args[0].reply_to_message_id == ""]
        self.assertEqual(len(top), 1)
        self.assertEqual(top[0].args[2], REFUSAL.splitlines()[0])
        self.assertEqual(self.emojis(), [butler_reactions.FAILED])

    def test_a_stop_with_no_card_says_nothing_and_takes_on_it_off(self):
        self.deliver(self.job(), status="cancelled", reply="已停止", verified=False)
        self.send_text.assert_not_called()
        self.assertEqual(self.emojis(), [""])

    def supplement(self, mid="om_2", text="再看一下昨天的"):
        path = delivery_path(self.state, BUTLER, mid)
        path.parent.mkdir(parents=True, exist_ok=True)
        save_delivery(path, {"status": "sent", "text": text, "topic_id": BUTLER, "chat_id": CHAT, "job_id": 7,
                             "job_key": "om_1", "mid": mid, "saved_at": time.time()})

    def test_a_refusal_with_no_card_lists_its_supplements_once_at_the_top_level(self):
        self.supplement()
        delivery = self.deliver(self.job(), status="cancelled", reply=REFUSAL, verified=False, outcome=REFUSED)
        self.send_text.assert_called_once()
        self.assertEqual(self.send_text.call_args.args[0].reply_to_message_id, "")
        self.assertIn("再看一下昨天的", self.send_text.call_args.args[2])
        self.assertTrue(delivery.listed)  # No line under the supplement: it would open its thread.

    def test_a_stop_with_no_card_lists_its_supplements_at_the_top_level(self):
        self.supplement()
        delivery = self.deliver(self.job(), status="cancelled", reply="已停止", verified=False)
        self.send_text.assert_called_once()
        call = self.send_text.call_args
        self.assertEqual(call.args[0].reply_to_message_id, "")
        self.assertTrue(call.args[2].startswith("**这一轮停了"))
        self.assertIn("再看一下昨天的", call.args[2])
        self.assertEqual(call.kwargs["card"]["body"]["elements"][0], ASK)
        self.assertTrue(delivery.listed)
        self.assertEqual(self.emojis(), [""])

    def test_a_list_that_did_not_go_leaves_each_supplement_its_line(self):
        self.supplement()
        with mock.patch.object(TurnDelivery, "top_notice", return_value=False):
            delivery = self.deliver(self.job(), status="cancelled", reply="已停止", verified=False)
        self.assertFalse(delivery.listed)

    def test_a_stop_or_a_refusal_with_its_late_card_lists_them_on_the_card(self):
        self.supplement()
        delivery = self.deliver(self.job(progress=SimpleNamespace(slim=True)), status="cancelled", reply="已停止",
                                verified=False)
        self.assertTrue(delivery.listed)
        self.assertFalse([call for call in self.send_text.call_args_list
                          if "再看一下昨天的" in str(call.args[2])])

    def test_a_turn_with_its_late_card_keeps_it(self):
        progress = SimpleNamespace(slim=True)
        self.opened()
        self.deliver(self.job(progress=progress))
        self.assertEqual(self.emojis(), [butler_reactions.GOT])  # The card has the lines; the message says Get.

    def test_an_answer_with_no_card_is_previewed_without_its_task_number(self):
        # ACCEPT-322 problem 3: the chat list read "✅ #3308 · 5", a task number the owner never saw.
        self.deliver(self.job(), reply="5")
        self.assertEqual(self.send_text.call_args.kwargs["card"]["config"]["summary"]["content"], "已答：5")
        # With its late card, or in a topic, as before.
        self.deliver(self.job(progress=SimpleNamespace(slim=True)), reply="5")
        summaries = [call.kwargs["card"]["config"]["summary"]["content"] for call in self.send_text.call_args_list
                     if call.kwargs.get("card")]
        self.assertEqual(summaries, ["#7 有结果 · 5"])

    def test_the_bare_preview_words(self):
        from covey.cards.review import bare_answer_summary
        self.assertEqual(bare_answer_summary("done", "两种做法都行"), "已答：两种做法都行")
        self.assertEqual(bare_answer_summary("done", ""), "有结果")
        self.assertEqual(bare_answer_summary("waiting_user", "查哪台"), "等你：查哪台")

    def test_a_topic_and_an_answers_continuation_get_no_reaction(self):
        self.deliver(self.job(topic="a" * 32, progress=SimpleNamespace()))
        self.assertEqual(self.emojis(), [])
        self.deliver(self.job(message="answer-tok"))
        self.assertEqual(self.emojis(), [])
        self.assertEqual(self.send_text.call_args.args[0].reply_to_message_id, "")  # Not under the question card.


class ReactionsTest(unittest.TestCase):
    def test_finish_learns_on_its_id_takes_it_off_and_adds_the_end(self):
        messenger = mock.Mock()
        messenger.react.return_value = "r-onit"
        job = make_job(job_id=7, message_id="om_1")
        self.assertTrue(butler_reactions.finish(messenger, "p", job, butler_reactions.DONE, log=mock.Mock()))
        self.assertEqual([c.args[2] for c in messenger.react.call_args_list], ["OnIt", "DONE"])
        messenger.unreact.assert_called_once_with("p", "om_1", "r-onit")

    def test_a_failure_is_a_log_line(self):
        messenger = mock.Mock()
        messenger.react.side_effect = RuntimeError("down")
        log = mock.Mock()
        self.assertFalse(butler_reactions.finish(messenger, "p", make_job(message_id="om_1"), "DONE", log=log))
        log.assert_called_once()

    def test_the_messenger_calls_lark_cli_as_the_probe_did(self):
        messenger = Messenger.__new__(Messenger)
        messenger.log = mock.Mock()
        with mock.patch("covey.feishu.messenger.call",
                        return_value={"ok": True, "data": {"reaction_id": "rid"}}) as call:
            self.assertEqual(messenger.react("bot", "om_1", "OnIt"), "rid")
            argv = call.call_args.args[0]
            self.assertIn("reactions", argv)
            self.assertIn(json.dumps({"reaction_type": {"emoji_type": "OnIt"}}), argv)
            self.assertTrue(messenger.unreact("bot", "om_1", "rid"))
            self.assertIn(json.dumps({"message_id": "om_1", "reaction_id": "rid"}), call.call_args.args[0])


class SubmissionTest(Case):
    def submission(self):
        from covey.app.submission import Submission
        sub = Submission.__new__(Submission)
        sub.cards = mock.Mock()
        sub.cards.streaming = True
        sub.messenger = mock.Mock()
        sub.messenger.react.return_value = "r1"
        sub.runtime = mock.Mock()
        sub.harness = SimpleNamespace(name="claude")
        sub.controls = mock.Mock()
        sub.controls.acknowledge_waiting_answer.return_value = None
        sub.record_job_state = mock.Mock()
        sub.voice = mock.Mock()
        return sub

    def publish(self, sub, job):
        with mock.patch("covey.app.submission.prestart_runner"):
            sub.publish_submission(self.ctx, job, {"ahead": 0, "capacity_full": False})

    def test_no_card_at_queue_time_and_on_it_on_the_message(self):
        sub = self.submission()
        job = self.job()
        self.publish(sub, job)
        sub.cards.create_streaming_reply.assert_not_called()
        self.assertTrue(job.card_deferred)
        self.assertIsNone(job.progress)
        sub.messenger.react.assert_called_once_with(self.ctx.profile, "om_1", "OnIt")
        sub.messenger.reply.assert_not_called()  # No "任务已入队" line instead.

    def test_an_answers_continuation_gets_no_card_at_queue_time_and_no_reaction(self):
        sub = self.submission()
        job = self.job(message="answer-tok")
        self.publish(sub, job)
        sub.cards.create_streaming_reply.assert_not_called()  # Not a thread under the question card at once.
        self.assertTrue(job.card_deferred)
        sub.messenger.react.assert_not_called()
        sub.messenger.reply.assert_not_called()

    def test_with_desk_topics_left_true_the_card_still_waits(self):
        self.desk()  # Retired on 2026-11-09: no card at once, the reaction instead, as without it.
        sub = self.submission()
        job = self.job()
        self.publish(sub, job)
        sub.cards.create_streaming_reply.assert_not_called()
        self.assertTrue(job.card_deferred)
        sub.messenger.react.assert_called_once_with(self.ctx.profile, "om_1", "OnIt")

    def test_a_held_cards_facts_reach_its_answer_cards_input_in_the_task_runner(self):
        # ACCEPT-322 problem 1: the answer card of a turn answered with no card read 工作区 "—".
        from covey.app.runner_spawn import dump_job, load_job
        sub = self.submission()
        cards = Cards.__new__(Cards)
        cards.streaming, cards.topics, cards.runtime = True, None, mock.Mock()
        cards.harness = SimpleNamespace(backend=object(), spec=SimpleNamespace(name="claude", title="Claude Code"))
        cards.create_streaming_reply = mock.Mock()
        sub.cards = cards
        job = self.job()
        self.publish(sub, job)
        cards.create_streaming_reply.assert_not_called()
        self.assertEqual(job.workspace_label, "ws")
        handed = load_job(json.loads(json.dumps(dump_job(job), ensure_ascii=False)))
        self.assertEqual(handed.workspace_label, "ws")
        self.assertEqual(handed.input_parts, job.input_parts)
        self.assertEqual(answer_cards.inputs(handed, cards.harness)[-1], "ws")
        # A job without them (handed by an older release): the workspace its context ran in.
        bare = self.job()
        self.assertEqual(answer_cards.inputs(bare, cards.harness)[-1], "—")
        self.assertEqual(answer_cards.inputs(bare, cards.harness, ctx=self.ctx)[-1], "ws")


class AskLinkTest(Case):
    def fetch(self, message_id, profile):
        return {"data": {"items": [{"message_id": message_id, "thread_id": "omt_1", "message_position": "42"}]}}

    def test_a_quiet_turn_links_the_messages_place_in_the_chat(self):
        thread = answer_cards.ask_link(self.state, CHAT, BUTLER, "om_1", fetch=self.fetch)
        place = answer_cards.ask_link(self.state, CHAT, BUTLER, "om_1", fetch=self.fetch, chat_place=True)
        self.assertIn("thread", thread)
        self.assertIn("position=42", place)
        self.assertNotIn("thread", place)

    def test_a_topic_turn_of_any_kind_links_its_topic(self):
        # The owner, 2026-11-10 16:55: 都能跳转到话题上 (a hand-on to a topic there linked the chat place before).
        from covey.app.relay_targets import ask_for
        with mock.patch.object(answer_cards, "asked", return_value=ASK) as asked:
            for kind in ("send", "steer", "start", "follow"):
                ask_for(self.state, self.job(), {"kind": kind, "main_key": "", "topic_id": "t444"}, at="om_card")
                self.assertEqual(asked.call_args.kwargs["topic_id"], "t444")
                self.assertEqual(asked.call_args.kwargs["at"][0], "om_card")


class QuestionTest(Case):
    def record(self):
        return {"token": "tok1", "job_key": job_key("om_1", 7), "job_id": 7, "topic_id": BUTLER, "chat_id": CHAT,
                "status": "pending", "blocking": True, "questions": [{"question": "用哪种？", "options": []}]}

    def display(self):
        display = QuestionDisplay.__new__(QuestionDisplay)
        display.messenger = mock.Mock()
        display.messenger.publish_card.return_value = True
        display.voice = mock.Mock()
        display.runtime = mock.Mock()
        return display

    def test_with_no_card_the_question_card_is_a_top_level_message_with_its_you_asked_block(self):
        upsert_job_record(self.state, job_key("om_1", 7), message_id="om_1", job_id=7, topic_id=BUTLER, chat_id=CHAT)
        display, record = self.display(), self.record()
        with mock.patch.object(answer_cards, "asked", return_value=ASK):
            display.publish(self.ctx, record)
        where, chat, _, built = display.messenger.publish_card.call_args.args[:4]
        self.assertEqual(where.reply_to_message_id, "")
        self.assertEqual(built["body"]["elements"][0], ASK)
        self.assertTrue(record["at_top"])
        self.assertEqual(record["mirror_state"], "at_top")  # No copy of it at the top level besides.
        self.assertEqual(standalone_card(self.ctx, record)["body"]["elements"][0], ASK)  # Repainted as sent.

    def test_with_its_late_card_or_desk_topics_as_before(self):
        upsert_job_record(self.state, job_key("om_1", 7), message_id="om_1", job_id=7, topic_id=BUTLER, chat_id=CHAT,
                          card_message_id="om_card")
        display, record = self.display(), self.record()
        display.publish(self.ctx, record)
        self.assertNotIn("at_top", record)
        self.assertEqual(display.messenger.publish_card.call_args.args[0].reply_to_message_id,
                         self.ctx.reply_to_message_id)


if __name__ == "__main__":
    unittest.main()


class RedeliveryTest(Case):
    def test_the_butlers_saved_answer_goes_again_to_the_chat_a_topics_to_its_thread(self):
        from covey.app.redelivery import delivery_context
        saved = SimpleNamespace(reply_to_message_id="om_1")
        record = {"message_id": "om_1", "topic_id": BUTLER, "chat_id": CHAT}
        self.assertEqual(delivery_context(self.ctx, record, saved).reply_to_message_id, "")
        self.assertEqual(delivery_context(self.ctx, {**record, "topic_id": "a" * 32}, saved).reply_to_message_id,
                         "om_1")
        self.desk()  # Retired: as without it.
        self.assertEqual(delivery_context(self.ctx, record, saved).reply_to_message_id, "")

    def test_a_saved_answer_of_a_turn_with_no_card_goes_again_without_its_task_number(self):
        from covey.app.task_controls import TaskControls
        record = {"key": job_key("om_1", 7), "job_id": 7, "message_id": "om_1", "topic_id": BUTLER, "chat_id": CHAT,
                  "execution_status": "done"}
        controls = TaskControls.__new__(TaskControls)
        controls.harness = SimpleNamespace(spec=SimpleNamespace(name="claude", idempotency_prefix="p", native_default_model=True))
        controls.messenger, controls.voice, controls.artifacts = mock.Mock(), mock.Mock(), mock.Mock()
        controls.messenger.reply.return_value = True
        controls.runtime = mock.Mock()
        reply = SimpleNamespace(text="5", fingerprint="f1")
        outcome = SimpleNamespace(job_status="completed")

        def redeliver(record):
            authorized = mock.MagicMock()
            authorized.return_value.__enter__.return_value = (reply, outcome, record)
            with mock.patch("covey.app.redelivery.authorized_delivery", authorized), \
                    mock.patch("covey.app.redelivery.display_saved_reply", return_value="5"), \
                    mock.patch("covey.app.redelivery.saved_delivery_kind", return_value="text"), \
                    mock.patch("covey.app.redelivery.upsert_job_record"), \
                    mock.patch("covey.app.redelivery.needs_question", return_value=False):
                controls.redeliver_verified_result(self.ctx, CHAT, record)
            return controls.messenger.reply.call_args.kwargs["card"]["config"]["summary"]["content"]

        self.assertEqual(redeliver(record), "已答：5")
        self.assertEqual(redeliver({**record, "card_message_id": "om_card"}), "#7 有结果 · 5")

    def test_a_paused_question_goes_again_where_its_first_send_put_it(self):
        # B1 (2026-11-10): as app/turn_delivery.py TurnDelivery._question_apart puts it: at the top level only when the quiet
        # turn sent no card, else where it replies (the owner's message's thread), no longer the top level always.
        from covey.app.task_controls import TaskControls
        controls = TaskControls.__new__(TaskControls)
        controls.harness = SimpleNamespace(spec=SimpleNamespace(name="claude", idempotency_prefix="p", native_default_model=True))
        controls.messenger, controls.voice, controls.artifacts = mock.Mock(), mock.Mock(), mock.Mock()
        controls.messenger.reply.return_value = True
        controls.questions = mock.Mock()
        controls.questions.deliver_checkpoint_questions.return_value = True
        controls.runtime = mock.Mock()
        record = {"key": job_key("om_1", 7), "job_id": 7, "message_id": "om_1", "topic_id": BUTLER, "chat_id": CHAT,
                  "reply_to_message_id": "om_1", "final_text": "选哪个？", "paused_question_delivery": {"run_id": "r1", "generation": 1}}

        def where(record):
            current = mock.MagicMock()
            current.return_value.__enter__.return_value = record
            with mock.patch("covey.app.redelivery.paused_question_delivery", current), \
                    mock.patch("covey.app.redelivery.upsert_job_record"), \
                    mock.patch("covey.app.redelivery.record_attempt"):
                self.assertIn("已重新投递", controls.redeliver_paused_question(self.ctx, CHAT, record))
            return controls.messenger.reply.call_args.args[0].reply_to_message_id

        self.assertEqual(where(record), "")
        self.assertEqual(where({**record, "card_message_id": "om_card"}), "om_1")


class RetryAndRecoveryTest(Case):
    """A quiet turn retried or resumed (app/task_controls.py requeue) or interrupted with no card
    (show_recovery_card) opens no thread either, and its reactions stay on the owner's message."""

    def controls(self):
        from covey.app.task_controls import TaskControls
        controls = TaskControls.__new__(TaskControls)
        controls.harness = SimpleNamespace(name="claude", spec=SimpleNamespace(name="claude", native_default_model=True))
        controls.voice = SimpleNamespace(key=lambda *parts: "-".join(map(str, parts)))
        controls.runtime = mock.Mock()
        controls.runtime.jobs.next_id.return_value = 8
        controls.messenger = mock.Mock()
        controls.messenger.react.return_value = "r1"
        controls.messenger.unreact.return_value = True
        controls.messenger.create_and_send_card.return_value = ("c1", "om_card")
        controls.cards = mock.Mock()
        controls.cards.replace_record_card.return_value = False
        controls.models = mock.Mock()
        controls.models.effort_preset.side_effect = lambda ctx, model, effort: (model, effort)
        controls.record_job_state = mock.Mock()
        return controls

    def record(self, **fields):
        return {"key": job_key("om_1", 7), "job_id": 7, "message_id": "om_1", "chat_id": CHAT, "topic_id": BUTLER,
                "reply_to_message_id": "om_1", "user_text": "查一下", "status": "failed", **fields}

    def requeue(self, record):
        controls = self.controls()
        self.ctx.reply_to_message_id = "om_1"
        with mock.patch("covey.app.task_controls.strict_recorded_model_settings", return_value=("m", "")), \
                mock.patch("covey.app.task_controls.recovery_card", return_value={"body": {"elements": []}}):
            controls.requeue(self.ctx, CHAT, "auto-retry-7-1", record, automatic=1)
        job = controls.runtime.enqueue_reply_job.call_args.args[0]
        return controls, job

    def test_a_retry_holds_its_card_and_reacts_on_the_owners_message(self):
        controls, job = self.requeue(self.record())
        controls.cards.create_streaming_reply.assert_not_called()
        self.assertIsNone(job.progress)
        self.assertTrue(job.card_deferred)
        self.assertEqual(job.reaction_message, "om_1")
        self.assertTrue(placement.marked(self.state, job))
        self.assertEqual(controls.record_job_state.call_args.kwargs["reaction_message"], "om_1")
        reacted = [call.args[1:] for call in controls.messenger.react.call_args_list]
        self.assertEqual(reacted, [("om_1", butler_reactions.FAILED), ("om_1", butler_reactions.ON_IT)])
        controls.messenger.unreact.assert_called_once_with(self.ctx.profile, "om_1", "r1")
        # Its end reaction goes there too.
        butler_reactions.finish(controls.messenger, self.ctx.profile, job, butler_reactions.DONE, mock.Mock())
        self.assertEqual(controls.messenger.react.call_args.args[1:], ("om_1", butler_reactions.DONE))

    def test_a_retry_of_a_turn_with_a_card_gets_one_at_once(self):
        controls, job = self.requeue(self.record(card_message_id="om_card"))
        controls.cards.create_streaming_reply.assert_called_once()
        self.assertFalse(job.card_deferred)

    def show(self, record):
        controls = self.controls()
        with mock.patch("covey.app.task_controls.recovery_card",
                        side_effect=lambda record, **kw: {"body": {"elements": [{"tag": "markdown"}]}}), \
                mock.patch("covey.app.answer_cards.asked", return_value=ASK):
            self.assertTrue(controls.show_recovery_card(self.ctx, record))
        return controls

    def test_an_interrupted_quiet_turn_gets_its_recovery_card_at_the_top_level(self):
        upsert_job_record(self.state, job_key("om_1", 7),
                          **{name: value for name, value in self.record().items() if name != "key"})
        controls = self.show(self.record())
        call = controls.messenger.create_and_send_card.call_args
        self.assertEqual((call.kwargs["reply_to_message_id"], call.kwargs["in_thread"]), ("", False))
        self.assertEqual(call.args[2]["body"]["elements"][0], ASK)
        self.assertTrue(find_job_record(self.state, job_key("om_1", 7))["card_top_level"])
        controls.messenger.unreact.assert_called_once_with(self.ctx.profile, "om_1", "r1")  # OnIt off.

    def test_a_topics_recovery_card_still_replies_in_its_thread(self):
        controls = self.show(self.record(topic_id="a" * 32))
        call = controls.messenger.create_and_send_card.call_args
        self.assertEqual(call.kwargs["reply_to_message_id"], "om_1")
        self.assertNotIn("in_thread", call.kwargs)
        self.assertNotEqual(call.args[2]["body"]["elements"][0], ASK)
        controls.messenger.react.assert_not_called()


class ResultFileTest(Case):
    """A result file of a quiet turn's answer at the top level is a new message in the chat (D1); others reply."""

    def send(self, reply_to):
        from covey.app.artifacts import Artifacts
        artifacts = Artifacts(SimpleNamespace(voice=SimpleNamespace(safe_key=lambda key: key.replace(":", "-"))),
                              runtime=mock.Mock(), messenger=mock.Mock(), cards=mock.Mock())
        artifacts.voice = SimpleNamespace(safe_key=lambda key: key.replace(":", "-"))
        artifacts.messenger.cardkit.return_value = {"data": {"message_id": "om_file"}}
        self.ctx.workdir = self.root
        ctx = self.ctx.__class__(**{**vars(self.ctx), "reply_to_message_id": reply_to})
        job = self.job(reply_to_message_id="om_1")
        with mock.patch("covey.feishu.drive.cli_result", return_value=({"data": {"file_key": "fk"}}, None)):
            self.assertTrue(artifacts.send_result_file(ctx, job, "长回答"))
        return artifacts.messenger.cardkit.call_args

    def test_to_the_chat_when_the_context_replies_to_nothing(self):
        call = self.send("")
        self.assertEqual(call.args[2], "/open-apis/im/v1/messages")
        self.assertEqual(call.args[3]["receive_id"], CHAT)
        self.assertEqual(call.kwargs["params"], {"receive_id_type": "chat_id"})

    def test_in_reply_to_the_jobs_message_otherwise(self):
        call = self.send("om_1")
        self.assertEqual(call.args[2], "/open-apis/im/v1/messages/om_1/reply")
        self.assertNotIn("receive_id", call.args[3])
