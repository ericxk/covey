"""Words handed to a topic's running turn (the butler's topic_send, project_send, an edit passed on) that the turn never
takes: the fourth way of app/steering.py end_unread_steers (WAVE12 W2-B). Queued as the topic's next turn with the 轮次来源
they were handed with; held for a question the topic waits on instead; a line in its thread when nothing is queued; the
Bridge's own reminders (``internal``) neither."""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest import mock

from support.world import TopicFixture

from covey.app import detached
from covey.core import turn_source
from covey.core.deliveries import delivery_path, on_arrival, read_delivery, update_delivery
from covey.core.jobs import context_for_job


SOURCE = {"kind": "relay", "asserted_by": "owner", "origin": {"message_id": "om_owner"}, "via": "butler"}
# What app/butler_topics.py next_turn makes of a hand-on, written out: the steering tests below run on a release before it.
HANDED = {"text": "主人说：把文档也改了", "prompt": "NEXT：把文档也改了", "topic": True, "title": "改文档", "source": SOURCE}


class HandedRuleTest(unittest.TestCase):
    """core/deliveries.py: which records are words handed to a topic, queued in it."""

    def test_handed_words_are_queued_in_their_topic_and_the_butlers_are_not(self):
        from covey.core.deliveries import handed, in_topic, marked
        handed_words = {"requeue": {"text": "a", "prompt": "b", "topic": True}, "requeue_next": True}
        butler = {"requeue": {"text": "a", "prompt": "a"}, "requeue_auto": True}
        arrived = {"requeue": {"text": "a"}, "receipt": {"kind": "job", "auto": True}, "requeue_next": True}
        self.assertTrue(handed(handed_words) and in_topic(handed_words) and marked(handed_words))
        self.assertFalse(handed(butler) or in_topic(butler))
        self.assertTrue(marked(butler))
        self.assertTrue(in_topic(arrived) and on_arrival(arrived) and not handed(arrived))
        # A receipt card is a click's or an arrival's: never handed words.
        self.assertFalse(handed({"requeue": {"topic": True}, "receipt": {"kind": "browse"}}))
        # The butler's mark on handed words is not theirs: a release before W2-B reads every requeue_auto as the butler's.
        self.assertFalse(marked({**handed_words, "requeue_next": False, "requeue_auto": True}))

    def test_internal_is_the_bridges_own(self):
        from covey.core.deliveries import internal
        self.assertTrue(internal({"internal": True}))
        self.assertFalse(internal({"internal": "yes"}) or internal({}) or internal(None))

    def test_the_requeue_of_a_hand_on_names_its_source_title_and_address(self):
        from covey.app.butler_topics import next_turn
        self.assertEqual(next_turn(text=HANDED["text"], prompt=HANDED["prompt"], title="改文档", source=SOURCE), HANDED)
        again = next_turn(text="t", prompt="p", title="标题", source=SOURCE, address="project-lead-p1")
        self.assertEqual(again, {"text": "t", "prompt": "p", "topic": True, "title": "标题", "source": SOURCE,
                                 "address": "project-lead-p1"})
        self.assertNotIn("source", next_turn(text="t", prompt="p", title="", source=None))


class HandedSteerTest(TopicFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.backend.declare(steering=True, steer_reads=True)
        self.receive("om_run", "第一项任务")
        self.running = self.app.runtime.jobs.queue.get_nowait()
        self.running.execution_started = True
        self.control = SimpleNamespace(steer=mock.Mock(return_value=None), supports_steer=True)
        self.running.backend_control = self.control
        self.running.timings["backend_requested"] = 0
        self.app.runtime.jobs.register(self.running)
        self.ctx_topic = self.topic("om_run")
        self.app.harness.context = self.ctx
        self.app.cards.set_task_element = mock.Mock(return_value=True)
        self.app.messenger.reply = mock.Mock(return_value=True)

    def synchronous(self):
        return mock.patch("covey.app.steering.background.start", side_effect=lambda _, fn, *a: fn(*a))

    def hand(self, mid="main-abc", *, again=True, **extra):
        requeue = dict(HANDED) if again else None
        saved = self.app.steering.deliver_steer(context_for_job(self.ctx, self.running), self.running, "STEER：把文档也改了",
                                                mid, shown="主人说：把文档也改了", requeue=requeue)
        if extra:
            update_delivery(self.path(mid), **extra)
        return saved

    def path(self, mid="main-abc"):
        return delivery_path(self.ctx_topic.state_dir, self.running.topic_id, mid)

    def ended(self):
        with self.synchronous():
            self.app.steering.end_unread_steers(context_for_job(self.ctx, self.running), self.running)

    def others(self):
        return [job for job in self.app.runtime.jobs.topic_jobs(self.running.topic_id, "oc_chat") if job is not self.running]

    def lines(self):
        return [str(call.args[2]) for call in self.app.messenger.reply.call_args_list]

    def test_handed_words_its_turn_never_read_become_its_next_turn_with_their_source(self):
        self.assertEqual(self.hand()["status"], "sent")
        self.ended()
        again, = self.others()
        self.assertTrue(again.message_id.startswith("requeue-"))
        self.assertEqual((again.user_text, again.prompt_text, again.topic_id), ("主人说：把文档也改了", "NEXT：把文档也改了",
                                                                               self.running.topic_id))
        self.assertEqual(turn_source.of(again), SOURCE)  # Still the butler's hand-on of the owner's words, never his message.
        self.assertEqual(again.title, "改文档")
        record = read_delivery(self.path())
        self.assertEqual((record["requeue_state"], record["requeued_job_id"]), ("queued", again.job_id))
        self.assertNotIn("requeue_auto", record)  # Its own mark: never the butler's.
        # Once: the end again, the Bridge after a runner, a restart queue nothing more.
        self.ended()
        detached.queue_lost(self.app, self.running, self.ctx)
        self.assertEqual(self.others(), [again])
        self.assertFalse([line for line in self.lines() if "没赶上" in line])

    def test_words_waiting_on_a_question_are_held_for_its_answer_not_a_turn(self):
        self.hand()
        self.app.steering.hold = mock.Mock(return_value=True)  # app/app.py workspace_command: a held note.
        self.ended()
        self.assertEqual(self.others(), [])
        target, words, chat, mid, source = self.app.steering.hold.call_args.args
        self.assertEqual((target.topic_id, words, chat, source), (self.running.topic_id, "主人说：把文档也改了", "oc_chat", SOURCE))
        self.assertTrue(mid.startswith("requeue-"))
        record = read_delivery(self.path())
        self.assertEqual((record["requeue_state"], record.get("requeue_next")), ("held", False))
        detached.queue_lost(self.app, self.running, self.ctx)
        self.assertEqual(self.others(), [])  # Held once, never queued after.

    def test_the_apps_hold_keeps_them_as_a_note_while_a_question_waits(self):
        self.hand()
        held = []
        self.app.interaction.interactive_command = mock.Mock(side_effect=lambda *a, **k: held.append((a, k)) or True)
        self.ended()
        self.assertEqual(self.others(), [])
        (args, kwargs), = held
        self.assertEqual((args[1], kwargs["note_only"], kwargs["source"]), ("主人说：把文档也改了", True, SOURCE))

    def test_no_question_waiting_queues_them(self):
        self.hand()
        self.app.interaction.interactive_command = mock.Mock(return_value=False)
        self.ended()
        self.assertEqual(len(self.others()), 1)

    def test_the_owners_stop_queues_nothing_and_says_it_in_the_thread(self):
        self.hand()
        self.running.cancel_event.set()
        self.ended()
        self.assertEqual(self.others(), [])
        line, = [line for line in self.lines() if "没赶上" in line]
        self.assertIn(f"任务 #{self.running.job_id}", line)
        self.assertIn("没有自动接着做", line)

    def test_words_handed_before_w2b_get_a_line_in_the_thread(self):
        self.hand(again=False)
        self.ended()
        self.assertEqual(self.others(), [])
        self.assertEqual(len([line for line in self.lines() if "没赶上" in line]), 1)

    def test_the_bridges_own_reminder_is_neither_queued_nor_said(self):
        self.hand(internal=True)
        self.ended()
        self.assertEqual(self.others(), [])
        self.assertEqual(self.lines(), [])

    def test_a_late_refusal_queues_them_too(self):
        self.running.backend_control = None  # Started, its backend not up yet: held.
        self.assertEqual(self.hand()["status"], "held")
        with self.synchronous():
            self.app.steering.settle_held_at_end(context_for_job(self.ctx, self.running), self.running)
        again, = self.others()
        self.assertEqual(turn_source.of(again), SOURCE)

    def test_a_late_uncertain_one_is_said_never_sent_again(self):
        self.running.backend_control = None  # Held, then sent from the hold with no answer of the backend's (_send_held).
        self.assertEqual(self.hand()["status"], "held")
        with self.synchronous():
            self.app.steering.settle_steer(context_for_job(self.ctx, self.running), self.running, "main-abc",
                                           status="uncertain", error="后端没有确认收到")
        self.assertEqual(self.others(), [])
        line, = [line for line in self.lines() if "没赶上" in line]
        self.assertIn("可能没有送到", line)

    def test_a_task_runner_marks_them_and_its_bridge_queues_them_whatever_the_topic_steer_switch(self):
        self.hand()
        with mock.patch.object(type(self.app.runtime), "in_runner", new_callable=mock.PropertyMock, return_value=True):
            self.ended()
        self.assertEqual(self.others(), [])
        self.assertTrue(read_delivery(self.path())["requeue_next"])
        with mock.patch.dict(os.environ, {"COVEY_TOPIC_STEER": "0"}), self.synchronous():
            detached.queue_lost(self.app, self.running, self.ctx)
        self.assertEqual(len(self.others()), 1)


if __name__ == "__main__":
    unittest.main()
