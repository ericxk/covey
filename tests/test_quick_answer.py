"""追问只发答案卡 (docs/cards.md, T604 #16): a topic's follow-up holds its task card back for a few seconds
(COVEY_QUICK_ANSWER_SECONDS, app/submission.py quick_hold); its first step, a question or those seconds send it
(app/cards.py Cards.reveal, reveal_held), and a turn that answered before any of them sends only its answer card, with
换配置 and 回退 under it. Any other end, and an answer that did not go out, sends the card first, as before."""
from __future__ import annotations

import os
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from support import make_context, make_job
from support.butler import CHAT, BUTLER, ButlerCase
from support.world import TopicFixture
from covey.app import submission, worker
from covey.app.cards import Cards
from covey.app.interaction import Interaction
from covey.app.turn_delivery import TurnDelivery
from covey.core import butler
from covey.core.jobs import find_job_record, job_key, quick_held
from covey.core.trace import task_trace

TOPIC = "t" * 32
REPLY = "可以，按第二种做。"
PANEL = {"tag": "column_set", "element_id": "fork_actions", "columns": []}


def follow_up(**fields):
    """A message the owner sent in a topic's thread after its first answer."""
    base = dict(job_id=7, chat_id=CHAT, message_id="om_2", topic_id=TOPIC,
                history_snapshot=[{"role": "user", "content": "第一句"}, {"role": "assistant", "content": "答"}])
    return make_job(**{**base, **fields})


def held(**fields):
    return follow_up(card_deferred=True, card_after=10, **fields)


class HoldTest(ButlerCase):
    """Which turns hold their card back, and for how long."""

    context_topic = TOPIC

    def test_a_topics_follow_up_holds_its_card_for_the_setting_s_seconds(self):
        self.assertEqual(submission.quick_hold(self.ctx, follow_up()), submission.QUICK_SECONDS)
        with mock.patch.dict(os.environ, {submission.QUICK_ENV: "4"}):
            self.assertEqual(submission.quick_hold(self.ctx, follow_up()), 4)
        for raw in ("x", "-1", str(submission.QUICK_MAX + 1)):
            with mock.patch.dict(os.environ, {submission.QUICK_ENV: raw}):
                self.assertEqual(submission.quick_hold(self.ctx, follow_up()), submission.QUICK_SECONDS)

    def test_off_and_every_other_turn_send_their_card_at_once(self):
        with mock.patch.dict(os.environ, {submission.QUICK_ENV: "0"}):
            self.assertEqual(submission.quick_hold(self.ctx, follow_up()), 0)  # Off: as before.
        self.assertEqual(submission.quick_hold(self.ctx, follow_up(history_snapshot=[])), 0)  # A topic's first message.
        self.assertEqual(submission.quick_hold(self.ctx, follow_up(message_id="main-tool-1")), 0)  # Not the owner's.
        butler_ctx = make_context(self.root, topic_id=BUTLER)
        self.assertEqual(submission.quick_hold(butler_ctx, follow_up(topic_id=BUTLER)), 0)  # Its own way (Row.quiet).
        with mock.patch.object(submission, "proxy_topic", return_value=True):
            self.assertEqual(submission.quick_hold(self.ctx, follow_up()), 0)  # A 代答 topic's reply goes to others.

    def test_only_a_held_card_not_sent_yet_is_a_quick_one(self):
        self.assertTrue(quick_held(held()))
        self.assertFalse(quick_held(follow_up(card_deferred=True)))  # A butler turn's hold.
        self.assertFalse(quick_held(held(progress=SimpleNamespace(card_id="c"))))
        self.assertFalse(quick_held(follow_up()))


class FollowUpSubmissionTest(TopicFixture, unittest.TestCase):
    """Through the Bridge's entry: the first message's card as before, the follow-up's held, no 已入队 notice."""

    def acks(self):
        return [c for c in self.app.messenger.reply.call_args_list if "任务已入队" in str(c.args[2:3])]

    def test_the_follow_up_is_held_and_says_nothing_until_its_card_or_answer(self):
        self.app.messenger.reply = mock.Mock(return_value=True)
        self.receive("om_a", "第一句")
        first = self.app.runtime.jobs.queue.get_nowait()
        self.assertEqual((first.card_deferred, first.card_after), (False, 0.0))
        self.assertEqual(len(self.acks()), 1)  # Without a card (cards off here): the notice, as before.
        self.app.process_reply_job(first, self.ctx)  # Its answer is in the topic's history now.
        self.receive("om_a2", "追问", root="om_a")
        job = self.app.runtime.jobs.queue.get_nowait()
        self.assertEqual((job.card_deferred, job.card_after), (True, submission.QUICK_SECONDS))
        self.assertEqual(len(self.acks()), 1)

    def test_switched_off_the_follow_up_is_as_before(self):
        self.app.messenger.reply = mock.Mock(return_value=True)
        with mock.patch.dict(os.environ, {submission.QUICK_ENV: "0"}):
            self.receive("om_a", "第一句")
            self.app.process_reply_job(self.app.runtime.jobs.queue.get_nowait(), self.ctx)
            self.receive("om_a2", "追问", root="om_a")
        job = self.app.runtime.jobs.queue.get_nowait()
        self.assertEqual((job.card_deferred, job.card_after), (False, 0.0))
        self.assertEqual(len(self.acks()), 2)


class RevealTest(ButlerCase):
    """The held card goes out from the status line's ticks: at the turn's first step, else after its seconds."""

    context_topic = TOPIC

    def cards(self):
        cards = Cards.__new__(Cards)
        cards.runtime = mock.Mock()
        cards.create_streaming_reply = mock.Mock(return_value=SimpleNamespace(card_id="c9", message_id="om_card"))
        cards.queue_trace = mock.Mock()
        return cards

    def test_an_answer_alone_keeps_it_back_until_its_seconds(self):
        cards, job = self.cards(), held()
        job.timings["backend_requested"] = time.time() - 3
        self.assertFalse(cards.update_card_activity(self.ctx, job, "执行中"))
        cards.create_streaming_reply.assert_not_called()
        job.timings["backend_requested"] = time.time() - 11
        self.assertTrue(cards.update_card_activity(self.ctx, job, "执行中"))
        self.assertEqual((job.progress.card_id, job.card_deferred, job.body_clear_pending), ("c9", False, True))
        record = find_job_record(self.state, job_key("om_2", 7))
        self.assertEqual((record["card_id"], record["card_message_id"]), ("c9", "om_card"))
        cards.queue_trace.assert_not_called()  # No step to draw.

    def test_its_first_step_sends_it_at_once_with_its_process(self):
        cards, job = self.cards(), held()
        job.timings["backend_requested"] = time.time() - 2
        job.tool_trace = task_trace()
        job.tool_trace.accept("tool", {"id": "ls", "name": "bash", "status": "running", "detail": "ls",
                                       "started_at": time.time()})
        self.assertTrue(cards.update_card_activity(self.ctx, job, "执行中"))
        cards.create_streaming_reply.assert_called_once()
        cards.queue_trace.assert_called_once_with(self.ctx, job, urgent=True)

    def test_a_butler_turns_hold_is_as_before(self):
        cards, job = self.cards(), follow_up(card_deferred=True, topic_id=BUTLER)
        job.timings["backend_requested"] = time.time() - 30
        job.tool_trace = task_trace()
        job.tool_trace.accept("tool", {"id": "ls", "name": "bash", "status": "done", "detail": "ls",
                                       "started_at": time.time() - 2, "ended_at": time.time() - 1})
        self.assertFalse(cards.update_card_activity(self.ctx, job, "执行中"))  # A step does not send it.
        self.assertFalse(cards.reveal_held(self.ctx, job, "ended"))  # Nor its end: its own way.
        job.timings["backend_requested"] = time.time() - butler.CARD_AFTER - 1
        self.assertTrue(cards.reveal(self.ctx, job, "执行中"))
        self.assertTrue(any("main thread card sent after" in str(c) for c in cards.runtime.log.call_args_list))

    def test_reveal_held_sends_it_whatever_the_turn_is_doing(self):
        cards, job = self.cards(), held(answer_finalized=True, cancel_requested_at="t")
        self.assertFalse(cards.reveal(self.ctx, job, "x"))  # Stopping or delivering: not from a tick.
        self.assertTrue(cards.reveal_held(self.ctx, job, "undelivered"))
        self.assertEqual(job.progress.card_id, "c9")
        self.assertFalse(job.body_clear_pending)  # Ended: what follows writes its body.
        self.assertFalse(cards.reveal_held(self.ctx, job, "undelivered"))  # Once.
        cards.create_streaming_reply.assert_called_once()


class EndTest(ButlerCase):
    """How a held follow-up ends: only its answer card, or its card first."""

    context_topic = TOPIC

    def turn(self, status="done", verified=True, outcome="completed"):
        return SimpleNamespace(status=status, status_message="", final_text=REPLY,
                               verified_delivery=SimpleNamespace(text=REPLY, fingerprint="f1") if verified else None,
                               verified_outcome=SimpleNamespace(status=outcome, notice=lambda: "", error_code=""),
                               outcome=None, allow_resume=True)

    def test_only_a_whole_saved_answer_is_a_quick_answer(self):
        self.assertTrue(worker.quick_answer(self.turn()))
        self.assertFalse(worker.quick_answer(self.turn(verified=False)))
        self.assertFalse(worker.quick_answer(self.turn(status="waiting_user")))
        self.assertFalse(worker.quick_answer(self.turn(status="failed")))
        self.assertFalse(worker.quick_answer(self.turn(outcome="incomplete")))

    def end(self, job, turn):
        cards = mock.Mock()
        with mock.patch.object(worker, "TurnDelivery"), \
                mock.patch.object(worker.auto_retry, "plan", return_value=None), \
                mock.patch.object(worker.policy_retry, "at_stop", return_value=None), \
                mock.patch.object(worker.butler_reply, "react_end"):
            worker._deliver_turn(job, self.ctx, turn, None, harness=mock.Mock(), runtime=mock.Mock(),
                                 messenger=mock.Mock(), cards=cards, artifacts=mock.Mock(), questions=mock.Mock(),
                                 steering=mock.Mock(), record_job_state=mock.Mock(), send_text=mock.Mock(),
                                 capabilities=SimpleNamespace(steer_reads=False))
        return [name for name, *_ in cards.method_calls]

    def test_a_quick_answer_sends_no_card_and_any_other_end_sends_it_first(self):
        self.assertEqual(self.end(held(), self.turn()), ["show_outcome"])  # A no-op without a card.
        for turn in (self.turn(status="failed", verified=False), self.turn(status="waiting_user"),
                     self.turn(status="cancelled")):
            self.assertEqual(self.end(held(), turn), ["reveal_held", "show_outcome"])
        self.assertEqual(self.end(follow_up(), self.turn(status="failed")), ["show_outcome"])  # Not held: as before.

    def deliver(self, job, sent=True):
        cards = mock.Mock()
        cards.render_stream_final = lambda job, text, **kwargs: "✅ " + text
        cards.exceeds_stream_card_limit = lambda text: False
        self.send_text = mock.Mock(return_value=sent)
        delivery = TurnDelivery(job, self.ctx, self.turn(), prefix="p", cards=cards, runtime=mock.Mock(),
                                artifacts=mock.Mock(), questions=mock.Mock(), steering=mock.Mock(),
                                record_job_state=mock.Mock(), send_text=self.send_text)
        with mock.patch.object(TurnDelivery, "texts", return_value=(REPLY, REPLY, False)), \
                mock.patch.object(TurnDelivery, "answer_tail", return_value=([], False, None)), \
                mock.patch("covey.app.answer_delivery.topic_controls", return_value=PANEL), \
                mock.patch("covey.app.turn_delivery.close_streaming_reply_async"):
            delivery.deliver(None)
        return cards

    def test_a_quick_answer_is_its_answer_card_alone_with_the_topic_s_buttons(self):
        cards = self.deliver(held())
        self.send_text.assert_called_once()
        elements = self.send_text.call_args.kwargs["card"]["body"]["elements"]
        self.assertEqual(elements[-1], PANEL)  # 换配置 and 回退: no task card carries them.
        self.assertIn(REPLY, str(elements))
        cards.reveal_held.assert_not_called()

    def test_an_answer_that_did_not_go_out_sends_the_card_to_say_so(self):
        cards = self.deliver(held(), sent=False)
        cards.reveal_held.assert_called_once()
        self.assertEqual(cards.reveal_held.call_args.args[2], "undelivered")

    def test_a_card_that_went_out_keeps_its_buttons_on_it(self):
        self.deliver(follow_up())
        elements = self.send_text.call_args.kwargs["card"]["body"]["elements"]
        self.assertNotIn(PANEL, elements)


class QuestionTest(ButlerCase):
    """A question of a held follow-up is asked as on any task card: the card goes out first."""

    context_topic = TOPIC

    def interaction(self):
        cards, questions = mock.Mock(), mock.Mock()
        order = mock.Mock()
        order.attach_mock(cards.reveal_held, "reveal_held")
        order.attach_mock(questions.dispatch_question_event, "dispatch")
        inter = Interaction(SimpleNamespace(voice=None), runtime=mock.Mock(), messenger=mock.Mock(), cards=cards,
                            questions=questions, steering=mock.Mock())
        return inter, order

    def test_the_card_goes_before_the_question(self):
        inter, order = self.interaction()
        inter.structured_harness_event(self.ctx, held(), "question", {"request_id": 1}, None, from_backend=False)
        self.assertEqual([name for name, *_ in order.method_calls], ["reveal_held", "dispatch"])
        inter, order = self.interaction()
        inter.structured_harness_event(self.ctx, follow_up(), "question", {"request_id": 1}, None, from_backend=False)
        self.assertEqual([name for name, *_ in order.method_calls], ["dispatch"])


if __name__ == "__main__":
    unittest.main()
