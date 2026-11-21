"""到点回来看 (COVEY_REVISIT=1, app/revisits.py, core/revisits.py): a topic ends its turn with ``revisit`` in its checkpoint
and the Bridge queues one turn of it at that time; a look that found nothing new is not told; the owner sees and takes
back what waits on the 看板. Before T604 a topic that waited on CI or a training could only poll inside one turn, holding
a slot the whole time. Off, nothing of it runs."""
from __future__ import annotations

import json
import os
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from support import make_context, make_job
from support.records import state_dir, write_topic
from support.world import UnifiedCase
from covey.app import revisits
from covey.app.runs import Runs
from covey.app.turn_delivery import TurnDelivery
from covey.cards.board import REVISIT_KIND, board_card
from covey.core import revisits as records
from covey.core.board_read import ended_at
from covey.core.jobs import find_job_record, job_key, upsert_job_record
from covey.core.state import write_json
from covey.core.topic_seen import read_seen, record_reads

ON = {records.SWITCH: "1"}
MINUTE = 60


class RecordTest(unittest.TestCase):
    def test_the_field_is_read_within_bounds_and_anything_else_is_none(self):
        self.assertEqual(records.wanted({"revisit": {"minutes": 30, "look": " 看 CI\n#12 "}}),
                         {"minutes": 30.0, "look": "看 CI #12"})
        self.assertEqual(records.wanted({"revisit": {"minutes": "45"}}), {"minutes": 45.0, "look": ""})
        self.assertEqual(records.wanted({"revisit": {"minutes": 0.5}})["minutes"], records.MIN_MINUTES)
        self.assertEqual(records.wanted({"revisit": {"minutes": 10 ** 6}})["minutes"], records.MAX_MINUTES)
        for bad in ({}, {"revisit": 30}, {"revisit": {"minutes": True}}, {"revisit": {"minutes": "soon"}},
                    {"revisit": {"look": "x"}}, {"revisit": {"minutes": float("nan")}}, None):
            self.assertIsNone(records.wanted(bad), bad)
        self.assertTrue(records.unchanged({"unchanged": True}))
        self.assertFalse(records.unchanged({"unchanged": "true"}))

    def test_one_record_a_topic_the_owner_takes_back_only_the_one_he_saw(self):
        state = state_dir(self)
        first = records.change(state, "t1", lambda _: records.armed("t1", "chat", {"minutes": 5, "look": ""}, by="k1",
                                                                     round_=1, now=100.0))
        self.assertEqual((first["due_at"], first["state"]), (400.0, records.PENDING))
        records.change(state, "t1", lambda _: records.armed("t1", "chat", {"minutes": 9, "look": ""}, by="k2", round_=1,
                                                            now=200.0))
        self.assertFalse(records.cancel(state, "t1", 100.0))  # The one he saw was replaced: the newer one stays.
        self.assertEqual(records.read(state, "t1")["set_by"], "k2")
        self.assertTrue(records.cancel(state, "t1", 200.0))
        self.assertEqual((records.read(state, "t1"), records.records(state)), ({}, []))
        self.assertTrue(records.is_revisit(records.message_id({"topic_id": "t1" * 10, "set_at": 1.5, "tries": 2})))
        self.assertFalse(records.is_revisit("om_x"))


class RevisitCase(UnifiedCase):
    """The Bridge with one started ordinary topic in chat ``chat`` and the switch on; its first turn queued as ``first``."""

    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ, ON)
        env.start()
        self.addCleanup(env.stop)
        self.state = self.ctx.state_dir
        self.receive("src", "盯一下训练")
        app, ctx = self.route("src")
        app.submission.start_workspace(ctx, "chat", ctx.workspace_name)
        self.first = self.app.runtime.jobs.queue.get_nowait()
        self.topic = ctx.topic_id

    def end(self, job, status="done", **checkpoint):
        """``job`` ended with ``status`` and ``checkpoint``, out of the registry, as its runner leaves it."""
        path = self.root / f"checkpoint-{job.job_id}.json"
        write_json(path, checkpoint, ensure_ascii=False)
        now = time.time()
        upsert_job_record(self.state, job_key(job.message_id, job.job_id), status=status, checkpoint_path=str(path),
                          timings={"backend_requested": now - 5, "execution_finished": now})
        self.app.runtime.unregister_reply_job(job)
        revisits.after_runner(self.app.runtime.log, self.state, job)

    def record(self):
        return records.read(self.state, self.topic)

    def saved(self, job):
        return find_job_record(self.state, job_key(job.message_id, job.job_id))

    def restarted(self):
        """The Bridge again over this state directory, its sends and cards mocked as UnifiedCase mocks the first."""
        app, ctx = self.bridge()
        for name in ("send_message", "send_text", "recall", "react", "unreact"):
            setattr(app.messenger, name, mock.Mock(return_value=""))
        for harness_app in app.apps.values():
            harness_app.submission.selection_card = mock.Mock()
            harness_app.cards.create_streaming_reply = mock.Mock(return_value=None)
            harness_app.harness.started = True
            harness_app.models.prepare_submission_context = lambda ctx: ctx
        return app, ctx

    def tick(self, app=None, ctx=None, after=0):
        (app or self.app).revisits.tick(ctx or self.ctx, now=time.time() + after)


class FlowTest(RevisitCase):
    def test_a_turn_asks_the_bridge_queues_one_turn_at_its_time_and_the_watch_goes_on_until_a_look_reports(self):
        self.end(self.first, revisit={"minutes": 30, "look": "看训练 loss"})
        saved = self.record()
        self.assertEqual((saved["state"], saved["round"], saved["minutes"], saved["look"]), (records.PENDING, 1, 30, "看训练 loss"))
        self.assertAlmostEqual(saved["due_at"], time.time() + 30 * MINUTE, delta=60)
        self.tick(after=29 * MINUTE)
        self.assertTrue(self.app.runtime.jobs.queue.empty())  # Not yet.
        self.tick(after=31 * MINUTE)
        turn = self.app.runtime.jobs.queue.get_nowait()
        fired = self.record()
        self.assertEqual((fired["state"], fired["message"], turn.message_id, turn.topic_id),
                         (records.FIRED, turn.message_id, fired["message"], self.topic))
        self.assertTrue(records.is_revisit(turn.message_id))
        self.assertIn("看训练 loss", turn.user_text)
        self.assertIn('"unchanged": true', turn.user_text)
        self.tick(after=40 * MINUTE)
        self.assertTrue(self.app.runtime.jobs.queue.empty())  # One at a time: its turn is out.
        # Nothing new, look again in 10 minutes: the next round, the topic's result seen as the one before was.
        record_reads(self.state, {self.topic: ended_at(self.saved(self.first))})
        self.end(turn, revisit={"minutes": 10, "look": "看训练 loss"}, unchanged=True)
        again = self.record()
        self.assertEqual((again["state"], again["round"], again["minutes"]), (records.PENDING, 2, 10))
        self.assertEqual(read_seen(self.state)[self.topic], ended_at(self.saved(turn)))
        self.tick(after=11 * MINUTE)
        last = self.app.runtime.jobs.queue.get_nowait()
        self.end(last)  # It finished and asked for no more: the watch ends.
        self.assertEqual(self.record(), {})
        logs = "\n".join(self.logs)
        self.assertIn("revisit set topic=", logs)
        self.assertIn("revisit queued topic=", logs)
        self.assertIn("revisit ended topic=", logs)

    def test_a_busy_topic_waits_and_a_stopped_look_ends_the_watch(self):
        self.end(self.first, revisit={"minutes": 5})
        self.receive("next", "顺便问一句", root="src")  # The owner's own turn in the topic, still open.
        self.tick(after=6 * MINUTE)
        self.assertEqual(self.record()["state"], records.PENDING)
        owner_turn = self.app.runtime.jobs.queue.get_nowait()
        self.end(owner_turn)  # Asked for nothing: the revisit set before stays.
        self.assertEqual(self.record()["round"], 1)
        self.tick(after=6 * MINUTE)
        turn = self.app.runtime.jobs.queue.get_nowait()
        self.end(turn, status="cancelled")
        self.assertEqual(self.record(), {})

    def test_a_turn_marked_but_never_queued_is_queued_again_after_a_restart(self):
        self.end(self.first, revisit={"minutes": 5})
        records.change(self.state, self.topic, lambda data: {**data, "state": records.FIRED, "tries": 1,
                                                             "fired_at": time.time() - 3600,
                                                             "message": records.message_id({**data, "tries": 1})})
        app, ctx = self.restarted()  # The Bridge stopped between the mark and the queue.
        with mock.patch.dict(os.environ, ON):
            self.tick(app, ctx)
            self.assertEqual(self.record()["state"], records.PENDING)
            self.tick(app, ctx)
        turn = app.runtime.jobs.queue.get_nowait()
        self.assertEqual((self.record()["tries"], turn.message_id), (2, self.record()["message"]))

    def test_a_quiet_look_leaves_a_result_not_seen_unseen(self):
        self.end(self.first, revisit={"minutes": 5})
        self.tick(after=6 * MINUTE)
        turn = self.app.runtime.jobs.queue.get_nowait()
        self.end(turn, revisit={"minutes": 5}, unchanged=True)  # The owner never read the first turn's result.
        self.assertNotIn(self.topic, read_seen(self.state))

    def test_the_rounds_in_a_row_are_bounded(self):
        self.end(self.first, revisit={"minutes": 5})
        records.change(self.state, self.topic, lambda data: {**data, "round": records.MAX_ROUND})
        self.tick(after=6 * MINUTE)
        turn = self.app.runtime.jobs.queue.get_nowait()
        self.end(turn, revisit={"minutes": 5}, unchanged=True)
        self.assertEqual(self.record(), {})
        self.assertIn("(the last one in a row)", "\n".join(self.logs))

    def test_the_board_lists_it_and_the_owners_cancel_takes_it_back(self):
        self.end(self.first, revisit={"minutes": 30, "look": "看 CI"})
        self.app.digest.link = lambda ctx, chat, text, cards=None: text
        view = self.app.digest.board_view(self.ctx, "chat")
        [row] = view["revisits"]
        self.assertEqual((row["topic"], row["look"], row["fired"]), (self.topic, "看 CI", False))
        card = json.dumps(board_card(view), ensure_ascii=False)
        self.assertIn("到点回来看", card)
        self.assertIn(REVISIT_KIND, card)
        raw = {"kind": REVISIT_KIND, "topic": self.topic, "set_at": row["set_at"]}
        self.assertEqual(self.app.digest.claim(self.ctx, {"chat_id": "chat"}, raw, False), "owner_only")
        self.assertEqual(self.record()["state"], records.PENDING)
        self.assertEqual(self.app.digest.claim(self.ctx, {"chat_id": "chat"}, raw, True), "board_revisit_cancel")
        self.assertEqual(self.record(), {})
        self.assertNotIn("revisits", self.app.digest.board_view(self.ctx, "chat"))

    def test_only_an_ordinary_topics_turns_have_the_rule(self):
        job = make_job(topic_id=self.topic, chat_id="chat")
        self.assertIn("revisit", Runs._revisit_note(self.ctx, job))
        write_topic(self.state, {"proxy": {"contact": "user:x"}}, topic_id="p" * 64)
        self.assertEqual(Runs._revisit_note(self.ctx, make_job(topic_id="p" * 64, chat_id="chat")), "")
        self.assertFalse(revisits.ordinary(self.state, "p" * 64, "chat"))


class OffTest(RevisitCase):
    def test_off_nothing_is_read_written_or_said(self):
        with mock.patch.dict(os.environ, {records.SWITCH: ""}):
            self.assertEqual(self.app.revisits.threads(), ())
            self.end(self.first, revisit={"minutes": 5})
            self.assertFalse((self.state / records.FOLDER).exists())
            self.assertEqual(Runs._revisit_note(self.ctx, make_job(topic_id=self.topic, chat_id="chat")), "")
            self.app.digest.link = lambda ctx, chat, text, cards=None: text
            self.assertNotIn("revisits", self.app.digest.board_view(self.ctx, "chat"))
        self.assertEqual([name for name, _ in self.app.revisits.threads()], ["revisits"])


class SilentTest(unittest.TestCase):
    """A look that found nothing new and goes on is kept back: no answer, no relay to the top level."""

    def setUp(self):
        env = mock.patch.dict(os.environ, ON)
        env.start()
        self.addCleanup(env.stop)
        self.root = state_dir(self)
        self.ctx = make_context(self.root, topic_id="t" * 64)
        self.ctx.state_dir.mkdir(parents=True)
        self.message = records.message_id({"topic_id": "t" * 64, "set_at": 100.0, "tries": 1})
        records.change(self.ctx.state_dir, "t" * 64, lambda _: {
            **records.armed("t" * 64, "chat", {"minutes": 5, "look": ""}, by="k", round_=1, now=100.0),
            "state": records.FIRED, "message": self.message, "tries": 1})

    def job(self, message=None, **checkpoint):
        path = self.root / "checkpoint.json"
        write_json(path, checkpoint)
        return make_job(job_id=7, chat_id="chat", topic_id="t" * 64, message_id=message or self.message,
                        checkpoint_path=str(path))

    @staticmethod
    def turn(status="done"):
        return SimpleNamespace(status=status, status_message="", final_text="没有变化。",
                               verified_delivery=SimpleNamespace(text="没有变化。", fingerprint="f1"),
                               verified_outcome=SimpleNamespace(status="completed", notice=lambda: "", error_code=""),
                               outcome=None, allow_resume=True)

    def silent(self, job, turn=None):
        return revisits.silent(SimpleNamespace(job=job, turn=turn or self.turn(), ctx=self.ctx, runtime=mock.Mock()))

    def test_only_a_quiet_look_that_goes_on(self):
        self.assertTrue(self.silent(self.job(unchanged=True, revisit={"minutes": 5})))
        self.assertFalse(self.silent(self.job(unchanged=True)))  # The last look reports.
        self.assertFalse(self.silent(self.job(revisit={"minutes": 5})))  # Something changed.
        self.assertFalse(self.silent(self.job(message="om_owner", unchanged=True, revisit={"minutes": 5})))
        self.assertFalse(self.silent(self.job(unchanged=True, revisit={"minutes": 5}), self.turn("failed")))
        with mock.patch.dict(os.environ, {records.SWITCH: "0"}):
            self.assertFalse(self.silent(self.job(unchanged=True, revisit={"minutes": 5})))

    def test_its_reply_is_kept_and_not_relayed(self):
        cards = mock.Mock()
        cards.render_stream_final = lambda job, text, **kwargs: "✅ " + text
        cards.exceeds_stream_card_limit = lambda text: False
        cards.body_cut.return_value = False
        send_text, relay = mock.Mock(return_value=True), mock.Mock()
        delivery = TurnDelivery(self.job(unchanged=True, revisit={"minutes": 5}), self.ctx, self.turn(), prefix="p",
                                cards=cards, runtime=mock.Mock(), artifacts=mock.Mock(), questions=mock.Mock(),
                                steering=mock.Mock(), record_job_state=mock.Mock(), send_text=send_text, relay=relay)
        with mock.patch.object(TurnDelivery, "texts", return_value=("没有变化。", "没有变化。", False)), \
                mock.patch("covey.app.turn_delivery.close_streaming_reply_async"):
            delivery.deliver(None)
        self.assertTrue(delivery.silent and delivery.quiet_revisit)
        send_text.assert_not_called()
        relay.assert_not_called()


if __name__ == "__main__":
    unittest.main()
