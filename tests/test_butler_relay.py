"""A topic turn the butler started or handed on, back at the top level (app/butler_relay.py, docs/butler.md
结论回到顶层): which turns, the card, sent once, and the board that leaves a relayed result out."""
from __future__ import annotations

import json

import time
import unittest
from types import SimpleNamespace
from unittest import mock

from support import make_job
from support.butler import CHAT, BUTLER, ButlerCase
from covey.app import auto_retry, butler_relay, pins, relay_send, relay_targets
from covey.app.butler_relay import relay
from covey.app.relay_targets import relay_target
from covey.cards.relay_card import CARD_TEXT_BYTES, DONE, STOPPED, WAITING, relay_card
from covey.app.worker import TurnDelivery
from covey.core.jobs import job_key, upsert_job_record
from covey.core.state import read_object, write_json
from covey.core.topics import message_topic

TOPIC = "ab" * 16
OWNER = "om_owner"
REPLY = "## 结论\n两种做法都行，先用第一种。"


class Case(ButlerCase):
    context_topic = TOPIC

    def setUp(self):
        super().setUp()
        self.topic(TOPIC, topic_id=TOPIC, topic_number=410)
        # The butler's turn that took the owner's top-level message.
        upsert_job_record(self.state, job_key(OWNER, 5), job_id=5, message_id=OWNER, reply_to_message_id=OWNER,
                          chat_id=CHAT, topic_id=BUTLER, status="done")

    def opened(self, token, at=1.0, **record):
        return super().opened(token, **{"request": token, "job_key": job_key(OWNER, 5), "chat_id": CHAT, "at": at,
                                        **record})

    def job(self, message_id="main-t1", job_id=9, topic=TOPIC, **fields):
        return make_job(job_id=job_id, chat_id=CHAT, message_id=message_id, topic_id=topic, **fields)

    def send(self, result=True):
        def sent(chat, text, key, reply_to, card, on_sent):
            if result is True:
                on_sent("om_relay")
            return result
        return mock.Mock(side_effect=sent)


class TargetTest(Case):
    def test_topic_start_send_and_fork_turns_are_the_butlers(self):
        self.opened("t1", plan={"start": True}, topic_id=TOPIC)
        self.opened("t2", topic_id=TOPIC, answer={"ok": True, "data": {"topic_id": TOPIC, "steered": False}})
        self.opened("t3", fork_topic_id=TOPIC, source="cd" * 16)
        for token, kind in (("t1", "start"), ("t2", "send"), ("t3", "fork")):
            target = relay_target(self.state, self.job(message_id="main-" + token))
            self.assertEqual({"chat_id": CHAT, "reply_to": OWNER, "label": "T410", "token": token, "kind": kind,
                              "topic_id": TOPIC, "main_key": job_key(OWNER, 5)}, target)

    def test_a_supplement_topic_send_gave_a_running_turn(self):
        self.opened("old", at=1.0, answer={"ok": True, "data": {"topic_id": TOPIC, "job_id": 9, "steered": True}})
        upsert_job_record(self.state, job_key("om_later", 6), job_id=6, message_id="om_later", chat_id=CHAT,
                          topic_id=BUTLER)
        self.opened("new", at=2.0, job_key=job_key("om_later", 6),
                    answer={"ok": True, "data": {"topic_id": TOPIC, "job_id": 9, "steered": True}})
        target = relay_target(self.state, self.job(message_id="om_in_topic"))
        self.assertEqual(("steer", "new", "om_later"), (target["kind"], target["token"], target["reply_to"]))
        # Another job of the topic, or a send that was refused, is not.
        self.assertIsNone(relay_target(self.state, self.job(message_id="om_other", job_id=10)))

    def test_a_turn_the_owner_started_in_the_topic_is_not(self):
        self.opened("t1", plan={"start": True}, topic_id=TOPIC)
        self.assertIsNone(relay_target(self.state, self.job(message_id="om_typed_in_topic", job_id=11)))

    def test_the_butlers_own_turn_is_not(self):
        self.opened("t1", plan={"start": True})
        self.assertIsNone(relay_target(self.state, self.job(message_id="main-t1", topic=BUTLER)))

    def test_an_automatic_retry_relays_for_the_turn_it_continues(self):
        self.opened("t1", plan={"start": True}, topic_id=TOPIC)
        upsert_job_record(self.state, job_key("main-t1", 9), job_id=9, message_id="main-t1", auto_retry_state="queued")
        upsert_job_record(self.state, job_key("om_retry", 12), job_id=12, message_id="om_retry",
                          retry_of=job_key("main-t1", 9))
        self.assertEqual("start", relay_target(self.state, self.job(message_id="om_retry", job_id=12))["kind"])
        # A retry the owner clicked is his own.
        upsert_job_record(self.state, job_key("main-t1", 9), auto_retry_state="cancelled")
        self.assertIsNone(relay_target(self.state, self.job(message_id="om_retry", job_id=12)))

    def test_without_the_owners_message_there_is_nowhere_to_quote(self):
        self.opened("t1", plan={"start": True}, job_key="gone")
        self.assertIsNone(relay_target(self.state, self.job()))


class CardTest(unittest.TestCase):
    def test_a_conclusion_is_the_whole_reply(self):
        reply = "先说结论。\n\n" + "细节。" * 300 + "\n\n## 逐层结果\n\n最后一段。"
        card, text = relay_card("T410", DONE, text=reply, inputs=["Claude Code"], outputs=[])
        self.assertNotIn("header", card)
        self.assertEqual("T410 · 有结论：先说结论", card["config"]["summary"]["content"])
        elements = card["body"]["elements"]
        self.assertTrue(elements[0]["content"].startswith("先说结论。"))
        self.assertTrue(elements[0]["content"].endswith("最后一段。"))  # Not cut: no "…", no 全文在 T410.
        self.assertIn("T410 ›", str(elements[-1]))  # The answer card's 输入/输出 box, 输出 ending in its T number.
        self.assertNotIn("全文", text)
        self.assertTrue(text.startswith("先说结论。"))

    def test_a_reply_too_large_for_a_card_goes_as_markdown(self):
        reply = "很长的回答。" * (CARD_TEXT_BYTES // 6)
        card, text = relay_card("T410", DONE, text=reply, inputs=[], outputs=[])
        self.assertIsNone(card)
        self.assertEqual(text, reply)  # The Markdown alone: its 产物 and box follow on a small card (send_once).

    def test_a_question_and_a_stop(self):
        card, _ = relay_card("T408", WAITING, question="选 A 还是 B？")
        self.assertEqual(["**T408 需要你回答**", "选 A 还是 B？"], [e["content"] for e in card["body"]["elements"][:2]])
        self.assertIn("在 T408 的问题卡里答，或在这里回「T408 …」", card["body"]["elements"][2]["content"])
        card, _ = relay_card("T408", STOPPED, reason="用户停止了任务")
        self.assertEqual("**T408 停了**：用户停止了任务", card["body"]["elements"][0]["content"])
        self.assertEqual("T408 · 停了：用户停止了任务", card["config"]["summary"]["content"])


class SendTest(Case):
    def setUp(self):
        super().setUp()
        self.opened("t1", plan={"start": True}, topic_id=TOPIC)

    def test_sent_once_quoting_the_owner_and_kept_as_the_topics(self):
        send = self.send()
        self.assertEqual("sent", relay(self.state, self.job(), DONE, send=send, text=REPLY))
        chat, _, key, reply_to, card, _ = send.call_args.args
        # A message of its own at the top level, quoting no card (butler_settings.threaded, 2026-11-08).
        self.assertEqual((CHAT, job_key("main-t1", 9), ""), (chat, key, reply_to))
        saved = read_object(relay_send.relay_path(self.state, job_key("main-t1", 9)))
        self.assertEqual(("sent", "om_relay", DONE), (saved["state"], saved["message_id"], saved["outcome"]))
        # The topic's: quoting it goes there (the owner, 2026-11-07: 引用直达).
        self.assertEqual(TOPIC, message_topic(self.state, CHAT, "om_relay"))
        self.assertEqual("sent", relay(self.state, self.job(), DONE, send=send, text=REPLY))
        self.assertEqual(1, send.call_count)  # The second call sends nothing.

    def test_its_t_number_opens_the_turns_own_card(self):
        # The owner, 2026-11-08: 点 T439 只能到话题上，到不了具体那个答案卡片上。
        from covey.app import topic_links
        with mock.patch.object(topic_links, "linkify", side_effect=lambda words, *a, **k: words) as linkify:
            relay(self.state, self.job(card_message_id="om_card9"), DONE, send=self.send(), text=REPLY)
        self.assertEqual({k.kwargs["cards"][TOPIC] for k in linkify.call_args_list}, {"om_card9"})

    def test_a_waiting_turn_opens_its_question_card(self):
        job = self.job(card_message_id="om_card9")
        self.assertEqual("om_card9", relay_targets.turn_card(self.state, job, WAITING))
        write_json(self.state / "questions" / "q1.json", {"job_id": 9, "topic_id": TOPIC, "chat_id": CHAT,
                   "status": "pending", "created_at": 1.0, "question_message_id": "om_question"})
        self.assertEqual("om_question", relay_targets.turn_card(self.state, job, WAITING))
        self.assertEqual("om_card9", relay_targets.turn_card(self.state, job, DONE))

    def test_an_unconfirmed_send_is_never_sent_again(self):
        send, log = mock.Mock(side_effect=TimeoutError()), mock.Mock()
        self.assertEqual("uncertain", relay(self.state, self.job(), DONE, send=send, log=log, text=REPLY))
        again = self.send()
        self.assertEqual("uncertain", relay(self.state, self.job(), DONE, send=again, log=log, text=REPLY))
        again.assert_not_called()
        # One line for the one try; the second call tries nothing (its "你问" link lines aside).
        tries = [call.args[0] for call in log.call_args_list if call.args[0].startswith("main relay ")]
        self.assertEqual(len(tries), 1, tries)
        self.assertRegex(tries[0], r"^main relay state=uncertain outcome=done job=\S+ kind=\w+ "
                                   r"message= error=TimeoutError$")
        self.assertFalse(butler_relay.relayed(self.state, {"key": job_key("main-t1", 9)}))

    def test_a_sent_relay_is_a_log_line(self):
        log = mock.Mock()
        self.assertEqual("sent", relay(self.state, self.job(), DONE, send=self.send(), log=log, text=REPLY))
        self.assertRegex(log.call_args.args[0], r"^main relay state=sent outcome=done job=\S+ kind=\w+ "
                                                r"message=om_relay$")

    def test_the_record_says_submitting_before_the_send(self):
        def check(*args):
            path = relay_send.relay_path(self.state, job_key("main-t1", 9))
            self.assertEqual("submitting", read_object(path)["state"])
            return True
        relay(self.state, self.job(), STOPPED, send=mock.Mock(side_effect=check), reason="x")


    def test_a_card_that_cannot_be_made_is_unsent_and_never_submitting(self):
        # T441 #3270 (11-08): a part of the card raised before the send, and the relay was lost with no record.
        from covey.app import relay_send
        path = relay_send.relay_path(self.state, job_key("main-t1", 9))
        for name in ("relay_card", "linked"):
            path.unlink(missing_ok=True)
            send, log = self.send(), mock.Mock()
            with mock.patch.object(relay_send, name, side_effect=AttributeError("x")):
                self.assertEqual("unsent", relay(self.state, self.job(), DONE, send=send, log=log, text=REPLY))
            send.assert_not_called()
            saved = read_object(path)
            self.assertEqual(("unsent", "AttributeError", DONE), (saved["state"], saved["error"], saved["outcome"]))
            self.assertRegex(log.call_args.args[0], r"^main relay state=unsent outcome=done job=\S+ kind=start "
                                                    r"error=AttributeError$")
            # Kept as it is: nothing sends it later, the next call included.
            self.assertEqual("unsent", relay(self.state, self.job(), DONE, send=send, text=REPLY))
            send.assert_not_called()
            self.assertFalse(butler_relay.relayed(self.state, {"key": job_key("main-t1", 9)}))


    def test_a_stop_the_owner_pressed_is_recorded_not_sent(self):
        # D4 (the owner, 2026-11-08): he pressed 停止 on its card in the 话题区's thread, looking at it.
        send, log = self.send(), mock.Mock()
        job = self.job(cancel_requested_by="card")
        self.assertEqual("skipped", relay(self.state, job, STOPPED, send=send, log=log, reason="用户停止了任务"))
        send.assert_not_called()
        saved = read_object(relay_send.relay_path(self.state, job_key("main-t1", 9)))
        self.assertEqual(("skipped", "owner_stop", STOPPED), (saved["state"], saved["why"], saved["outcome"]))
        self.assertRegex(log.call_args.args[0], r"^main relay state=skipped outcome=stopped job=\S+ kind=start "
                                                r"why=owner_stop$")
        self.assertEqual("skipped", relay(self.state, job, STOPPED, send=send, reason="x"))
        send.assert_not_called()

    def test_a_failure_still_goes_and_desk_topics_left_true_changes_nothing(self):
        from covey.core import butler_settings
        send = self.send()
        self.assertEqual("sent", relay(self.state, self.job(), STOPPED, send=send, reason="超时"))
        # desk_topics, retired on 2026-11-09, sent the owner's own stops too; read as false now.
        (self.state / butler_settings.FILE).write_text('{"mode": "agent", "desk_topics": true}')
        self.opened("t2", plan={"start": True}, topic_id=TOPIC)
        again = self.send()
        self.assertEqual("skipped", relay(self.state, self.job("main-t2", cancel_requested_by="card"), STOPPED,
                                          send=again, reason="用户停止了任务"))
        again.assert_not_called()


class AskedTest(Case):
    """A question asked while the turn runs (relay_question, app/native_questions.py): at the top level once."""

    def setUp(self):
        super().setUp()
        self.opened("t1", plan={"start": True}, topic_id=TOPIC)
        self.question = {"token": "q1", "questions": [{"question": "选 A 还是 B？"}]}

    def test_each_question_goes_once_quoting_the_owner(self):
        send = self.send()
        self.assertEqual("sent", butler_relay.relay_question(self.state, self.job(), self.question, send=send))
        chat, text, key, reply_to, card, _ = send.call_args.args
        self.assertEqual((chat, key, reply_to), (CHAT, job_key("main-t1", 9) + "--q1", ""))
        self.assertIn("T410 需要你回答", text)
        self.assertIn("选 A 还是 B？", text)
        saved = read_object(butler_relay.question_relay_path(self.state, job_key("main-t1", 9), "q1"))
        self.assertEqual((saved["state"], saved["outcome"]), ("sent", WAITING))
        self.assertEqual("sent", butler_relay.relay_question(self.state, self.job(), self.question, send=send))
        other = {"token": "q2", "questions": [{"question": "再问一个"}]}
        butler_relay.relay_question(self.state, self.job(), other, send=send)
        self.assertEqual(2, send.call_count)  # The same question once; another one once more.
        # The turn then ending waiting does not ask again.
        self.assertEqual("asked", relay(self.state, self.job(), WAITING, send=send, question="选 A 还是 B？"))
        self.assertEqual(2, send.call_count)

    def test_unconfirmed_is_never_sent_again(self):
        send = mock.Mock(side_effect=TimeoutError())
        self.assertEqual("uncertain", butler_relay.relay_question(self.state, self.job(), self.question, send=send))
        again = self.send()
        butler_relay.relay_question(self.state, self.job(), self.question, send=again)
        again.assert_not_called()

    def test_not_for_a_turn_the_owner_started(self):
        send = self.send()
        self.assertIsNone(butler_relay.relay_question(self.state, self.job(message_id="om_own"), self.question, send=send))
        send.assert_not_called()


class QuestionTest(Case):
    def test_the_question_from_its_record_else_the_checkpoint(self):
        point = self.root / "checkpoint.json"
        write_json(point, {"requires_user": True, "question": "检查点里的问题？"}, ensure_ascii=False)
        job = self.job(checkpoint_path=str(point))
        self.assertEqual("检查点里的问题？", butler_relay._question(self.state, job))
        write_json(self.state / "questions" / "q1.json", {"job_id": 9, "topic_id": TOPIC, "chat_id": CHAT,
                   "status": "pending", "created_at": 1.0,
                   "questions": [{"id": "a", "header": "补充信息", "question": "选 A 还是 B？"}]}, ensure_ascii=False)
        self.assertEqual("选 A 还是 B？", butler_relay._question(self.state, job))


class SwitchOffTest(Case):
    switch = ""

    def test_nothing_is_sent(self):
        self.opened("t1", plan={"start": True}, topic_id=TOPIC)
        send = self.send()
        self.assertIsNone(relay(self.state, self.job(), DONE, send=send, text=REPLY))
        send.assert_not_called()


class DeliverTest(Case):
    """TurnDelivery.deliver calls the relay after the card's last words, with what came of the turn."""

    def setUp(self):
        super().setUp()
        self.opened("t1", plan={"start": True}, topic_id=TOPIC)

    def deliver(self, job, *, status="done", retry=None, sent=True):
        cards = mock.Mock()
        cards.render_stream_final = lambda job, text, **kwargs: text
        cards.exceeds_stream_card_limit = lambda text: False
        cards.body_cut.return_value = False
        cards.update_job_streaming_reply.return_value = True
        verified = SimpleNamespace(text=REPLY, fingerprint="f1") if status == "done" else None
        turn = SimpleNamespace(status=status, status_message="用户停止了任务" if status == "cancelled" else "任务完成",
                               final_text=REPLY, verified_delivery=verified,
                               verified_outcome=SimpleNamespace(status="completed", notice=lambda: "", error_code=""),
                               outcome=None, allow_resume=True)
        messenger = mock.Mock()
        messenger.send_message.side_effect = lambda *args, on_sent=None, **kwargs: (on_sent("om_relay"), True)[1]
        voice = SimpleNamespace(key=lambda tag, mid: f"bot-{tag}-{mid}")
        delivery = TurnDelivery(job, self.ctx, turn, prefix="p", cards=cards, runtime=mock.Mock(),
                                artifacts=mock.Mock(), questions=mock.Mock(), steering=mock.Mock(),
                                record_job_state=mock.Mock(), send_text=mock.Mock(return_value=True), retry=retry,
                                relay=lambda *args: butler_relay.after_turn(*args, messenger=messenger, profile="unit",
                                                                          voice=voice))
        with mock.patch.object(TurnDelivery, "texts", return_value=(REPLY, REPLY, False)), \
                mock.patch.object(TurnDelivery, "send", return_value=(sent, True)), \
                mock.patch("covey.app.worker.record_attempt"), \
                mock.patch("covey.app.worker.close_streaming_reply_async"):
            delivery.deliver(None)
        return messenger.send_message

    def test_a_done_turn_sends_its_conclusion(self):
        send = self.deliver(self.job(progress=SimpleNamespace()))
        send.assert_called_once()
        args, kwargs = send.call_args
        self.assertEqual(("unit", CHAT), args[:2])
        self.assertEqual(f"bot-main-relay-{job_key('main-t1', 9)}", args[3])
        self.assertEqual("", kwargs["reply_to_message_id"])  # Its own message at the top level, quoting no card.
        # Who speaks first (docs/ui.md 5.3): the topic, by its T number, alone on the line when no words are quoted.
        speaker = kwargs["card"]["body"]["elements"][0]
        self.assertEqual(speaker["element_id"], "answer_ask")
        self.assertIn("**T410", str(speaker))
        self.assertIn("两种做法都行", kwargs["card"]["body"]["elements"][1]["content"])
        # The answer card's 输入/输出 box (cards/answer_card.py), 输出 ending in the T number.
        facts = kwargs["card"]["body"]["elements"][-1]
        self.assertEqual(facts["element_id"], "answer_facts")
        self.assertIn("输出", str(facts))
        self.assertIn("T410 ›", str(facts))

    def test_a_turn_that_changed_code_has_its_change_folded_above_the_box(self):
        point = self.state / "delivery-checkpoint.json"
        point.write_text(json.dumps({"delivery": {"commit": "a6bc148d38ce", "deployed": "第 322 代"}}, ensure_ascii=False))
        send = self.deliver(self.job(progress=SimpleNamespace(), checkpoint_path=str(point)))
        elements = send.call_args.kwargs["card"]["body"]["elements"]
        self.assertEqual([element.get("element_id") for element in elements[-2:]], ["answer_change", "answer_facts"])
        self.assertIn("改动 · a6bc148 · 第 322 代 · 未验收", str(elements[-2]))
        self.assertIn("a6bc148d38ce", str(elements[-2]))  # Open, the commit in full.
        relay_send.relay_path(self.state, job_key("main-t1", 9)).unlink()
        # A research turn's delivery without a commit, and no files changed: none.
        point.write_text(json.dumps({"delivery": {"verified": "报告已发群"}}, ensure_ascii=False))
        send = self.deliver(self.job(progress=SimpleNamespace(), checkpoint_path=str(point)))
        self.assertNotIn("改动", str(send.call_args.kwargs["card"]["body"]["elements"]))

    def test_a_stopped_turn_with_a_retry_planned_sends_nothing(self):
        send = self.deliver(self.job(progress=SimpleNamespace()), status="failed", retry=auto_retry.Planned(time.time() + 60, 1, auto_retry.RATE_LIMITED))
        send.assert_not_called()

    def test_a_stopped_turn_without_one_says_it_stopped(self):
        send = self.deliver(self.job(progress=SimpleNamespace()), status="cancelled")
        self.assertEqual("**T410 停了**：用户停止了任务", send.call_args.kwargs["card"]["body"]["elements"][1]["content"])

    def test_an_undelivered_result_sends_nothing(self):
        send = self.deliver(self.job(progress=SimpleNamespace()), sent=False)
        send.assert_not_called()

    def test_a_failing_relay_never_fails_the_delivery(self):
        with mock.patch.object(butler_relay, "relay", side_effect=RuntimeError("boom")):
            self.deliver(self.job(progress=SimpleNamespace()))


    def test_an_answer_part_that_raises_is_left_out_and_the_relay_goes(self):
        from covey.app import answer_cards
        for name in ("inputs", "outputs"):
            relay_send.relay_path(self.state, job_key("main-t1", 9)).unlink(missing_ok=True)
            with mock.patch.object(answer_cards, name, side_effect=AttributeError("x")):
                send = self.deliver(self.job(progress=SimpleNamespace()))
            send.assert_called_once()
            self.assertIn("两种做法都行", send.call_args.kwargs["card"]["body"]["elements"][1]["content"])
            saved = read_object(relay_send.relay_path(self.state, job_key("main-t1", 9)))
            self.assertEqual("sent", saved["state"])

    def test_a_failure_before_the_record_leaves_it_unsent(self):
        with mock.patch.object(butler_relay, "relay_target", side_effect=AttributeError("x")):
            send = self.deliver(self.job(progress=SimpleNamespace()))
        send.assert_not_called()
        saved = read_object(relay_send.relay_path(self.state, job_key("main-t1", 9)))
        self.assertEqual(("unsent", "AttributeError", DONE), (saved["state"], saved["error"], saved["outcome"]))


class BoardTest(Case):
    def test_a_conclusion_sent_back_is_seen_by_its_own_read_only(self):
        """LA-10 (T419, 2026-11-08): a top-level read of any message 2 s before the relay went out was its seen. A
        relay sent as a message of its own is the topic's (butler_relay remember_sent): its read is the topic's."""
        t0 = 1793856000.0
        done = {"key": job_key("main-t1", 9), "topic_id": TOPIC, "job_id": 9, "status": "done",
                "card_message_id": "om_card", "timings": {"execution_finished": t0, "delivered": t0 + 1}}
        relayed = lambda record: butler_relay.relayed(self.state, record)  # noqa: E731
        inline = lambda record: butler_relay.inline_at(self.state, record)  # noqa: E731

        def seen(read_top, topic_seen=None, **kwargs):
            rows = pins.board([], [done], topic_seen or {}, {}, t0 + 60, relayed=relayed, inline=inline,
                              read_top=read_top, **kwargs)
            return rows[0]["seen"] if rows else "off"
        write_json(relay_send.relay_path(self.state, done["key"]),
                   {"state": "sent", "outcome": DONE, "message_id": "om_relay", "at": t0 + 5, "sent_at": t0 + 8})
        self.assertIsNone(seen(t0 + 3))  # Read at the top level before the relay went out: not seen.
        self.assertIsNone(seen(t0 + 30))  # Nor after: another message's read is not this one's.
        self.assertEqual(seen(t0 + 3, {TOPIC: t0 + 6}), t0 + 6)  # The relay (or the thread's answer card) read.
        self.assertIsNone(seen(t0 + 30, {TOPIC: t0 - 1}))  # Read before it ended.
        # Written on the butler's card by an older release (INLINE): no read receipt of its own, the top
        # level's once it was there.
        write_json(relay_send.relay_path(self.state, done["key"]),
                   {"state": butler_relay.INLINE, "outcome": DONE, "message_id": "", "at": t0 + 2, "sent_at": t0 + 5})
        self.assertEqual(seen(t0 + 6), t0 + 6)
        self.assertIsNone(seen(t0 + 4))
        # Ended before these rules: as before, a relayed one done is not on the board.
        self.assertEqual(seen(t0 + 30, keep_since=t0 + 1), "off")
        # A question still comes first, relayed or not.
        asked = {"topic_id": TOPIC, "created_at": t0 + 30, "question_message_id": "om_q"}
        self.assertEqual("waiting", pins.board([asked], [done], {}, {}, t0 + 60, relayed=relayed)[0]["group"])
        self.assertIsNone(butler_relay.inline_at(self.state, {"key": "none"}))


if __name__ == "__main__":
    unittest.main()
