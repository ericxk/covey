"""多路并行's reactions on the owner's message (app/butler_reactions.py, app/butler_tracks.py ``Tracks.noted``): 排队中
(OneSecond) on a message that queues behind a track, taken off and OnIt put on when its turn starts; 并进 (THUMBSUP) on a
message that joined a task, off again when the owner quotes it with 单独答; the stop's reply says how many tracks are
still going. Each is a Feishu call outside any lock, a failure only a log line, and with the switch off none is made."""
from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest import mock

from support.butler import switch_on
from support.world import UnifiedCase
from test_butler_tracks_runtime import CHAT, LONG, TracksCase
from covey.app import butler_reactions, track_judge
from covey.core import butler_tracks
from covey.core.locks import LOCK

SECOND = "明天北京天气怎么样，要不要带伞呢"
THIRD = "再来一个问题，帮我看一下日志里的报错"


class Fake:
    """A Messenger that keeps what was done to reactions, in order: (verb, message, emoji or reaction id)."""

    def __init__(self, listed=None, fail=()):
        self.done, self.listed, self.fail = [], dict(listed or {}), set(fail)
        self.log = []

    def react(self, profile, message, emoji):
        self.done.append(("react", message, emoji))
        if "react" in self.fail:
            raise RuntimeError("down")
        return f"rid-{emoji}"

    def unreact(self, profile, message, reaction):
        self.done.append(("unreact", message, reaction))
        return "unreact" not in self.fail

    def reaction_ids(self, profile, message, emoji):
        self.done.append(("list", message, emoji))
        if "list" in self.fail:
            return None
        return list(self.listed.get((message, emoji), []))


def job(message="om_1", topic="t", job_id=3):
    return SimpleNamespace(message_id=message, topic_id=topic, chat_id=CHAT, job_id=job_id, reaction_message="")


class HelpersTest(unittest.TestCase):
    def setUp(self):
        self.log = mock.Mock()
        patch = mock.patch.object(butler_reactions, "owner_quiet", return_value=True)
        patch.start()
        self.addCleanup(patch.stop)
        butler_reactions.HELD.clear()

    def test_begin_takes_queued_off_before_onit_goes_on(self):
        fake = Fake({("om_1", butler_reactions.QUEUED): ["r-q"]})
        with mock.patch.object(butler_tracks, "enabled", return_value=True), \
                mock.patch.object(butler_reactions.placement, "marked", return_value=True):
            self.assertTrue(butler_reactions.begin(fake, "/s", "p", job(), self.log))
        self.assertEqual(fake.done, [("list", "om_1", "OneSecond"), ("unreact", "om_1", "r-q"), ("react", "om_1", "OnIt")])

    def test_begin_leaves_a_message_that_never_queued_alone(self):
        fake = Fake()
        with mock.patch.object(butler_tracks, "enabled", return_value=True), \
                mock.patch.object(butler_reactions.placement, "marked", return_value=True):
            self.assertFalse(butler_reactions.begin(fake, "/s", "p", job(), self.log))
        self.assertEqual(fake.done, [("list", "om_1", "OneSecond")])

    def test_begin_with_the_switch_off_calls_nothing(self):
        fake = Fake({("om_1", butler_reactions.QUEUED): ["r-q"]})
        with mock.patch.object(butler_tracks, "enabled", return_value=False):
            self.assertFalse(butler_reactions.begin(fake, "/s", "p", job(), self.log))
            butler_reactions.unqueue(fake, "/s", "p", job(), self.log)
        self.assertEqual(fake.done, [])

    def test_begin_when_the_listing_fails_still_puts_onit_on_and_does_not_raise(self):
        fake = Fake(fail=("list",))
        with mock.patch.object(butler_tracks, "enabled", return_value=True), \
                mock.patch.object(butler_reactions.placement, "marked", return_value=True):
            self.assertTrue(butler_reactions.begin(fake, "/s", "p", job(), self.log))
        self.assertEqual([verb for verb, *_ in fake.done], ["list", "react"])
        self.log.assert_called()

    def test_every_failure_is_a_log_line(self):
        fake = Fake({("om_1", "OneSecond"): ["r"]}, fail=("react", "unreact", "list"))
        self.assertFalse(butler_reactions.queue(fake, "/s", "p", CHAT, "om_1", self.log))
        self.assertFalse(butler_reactions.merge(fake, "/s", "p", CHAT, "om_1", self.log))
        self.assertFalse(butler_reactions.split(fake, "p", "om_1", self.log))
        self.assertEqual(butler_reactions.HELD, {})
        self.assertGreaterEqual(self.log.call_count, 3)

    def test_a_removal_that_fails_is_logged_and_the_rest_go(self):
        fake = Fake({("om_1", "THUMBSUP"): ["a", "b"]}, fail=("unreact",))
        self.assertTrue(butler_reactions.split(fake, "p", "om_1", self.log))
        self.assertEqual([done for done in fake.done if done[0] == "unreact"], [("unreact", "om_1", "a"), ("unreact", "om_1", "b")])
        self.assertEqual(self.log.call_count, 2)

    def test_start_leaves_onit_to_begin_for_a_queued_message_once(self):
        fake = Fake()
        self.assertTrue(butler_reactions.queue(fake, "/s", "p", CHAT, "om_1", self.log))
        with mock.patch.object(butler_reactions.placement, "marked", return_value=True):
            self.assertTrue(butler_reactions.start(fake, "/s", "p", job(), self.log))
            self.assertEqual(fake.done, [("react", "om_1", "OneSecond")])
            self.assertTrue(butler_reactions.start(fake, "/s", "p", job(), self.log))  # A retry: the usual OnIt.
        self.assertEqual(fake.done[-1], ("react", "om_1", "OnIt"))

    def test_a_message_that_is_not_a_quiet_turns_gets_no_queued_mark(self):
        fake = Fake()
        with mock.patch.object(butler_reactions, "owner_quiet", return_value=False):
            self.assertFalse(butler_reactions.queue(fake, "/s", "p", CHAT, "om_1", self.log))
            self.assertFalse(butler_reactions.merge(fake, "/s", "p", CHAT, "om_1", self.log))
        self.assertEqual(fake.done, [])


class ReactionsCase(TracksCase):
    def setUp(self):
        super().setUp()
        butler_reactions.HELD.clear()
        self.fake = Fake()
        self.fake.log = self.app.runtime.log
        for name in ("react", "unreact", "reaction_ids"):
            setattr(self.app.messenger, name, getattr(self.fake, name))
        self.lock_free = []
        real = self.fake.react

        def react(profile, message, emoji):
            thread = __import__("threading").Thread(target=lambda: self.lock_free.append(LOCK.acquire(timeout=1)) or LOCK.release())
            thread.start()
            thread.join()
            return real(profile, message, emoji)
        self.app.messenger.react = react
        self.replies = []

    def marks(self, message=None):
        return [done for done in self.fake.done if message in (None, done[1])]


class QueuedTest(ReactionsCase):
    def test_a_message_that_queues_gets_queued_and_no_onit_until_its_turn_starts(self):
        with mock.patch.dict(os.environ, {butler_tracks.MAX_ENV: "2"}):
            self.send("om_1", LONG)
            self.run_job("om_1")
            self.send("om_2", SECOND)
            self.run_job("om_2")
            self.fake.done.clear()
            self.send("om_3", THIRD)  # Both slots are busy: it queues behind slot 0.
        self.assertEqual(self.slot_of("om_3"), 0)
        self.assertEqual(self.marks("om_3"), [("react", "om_3", "OneSecond")])
        self.assertEqual(self.lock_free, [True] * len(self.lock_free))
        # Its turn starts in the task process: 排队中 off first, then OnIt.
        self.fake.listed[("om_3", "OneSecond")] = ["r-q"]
        self.fake.done.clear()
        butler_reactions.begin(self.fake, self.state, "p", self.job_of("om_3"), self.app.runtime.log)
        self.assertEqual(self.marks("om_3"), [("list", "om_3", "OneSecond"), ("unreact", "om_3", "r-q"), ("react", "om_3", "OnIt")])

    def test_a_message_that_starts_at_once_is_onit_alone(self):
        self.send("om_1", LONG)
        self.assertEqual(self.marks("om_1"), [("react", "om_1", "OnIt")])

    def test_a_queued_message_cancelled_before_it_starts_loses_the_mark(self):
        with mock.patch.dict(os.environ, {butler_tracks.MAX_ENV: "2"}):
            self.send("om_1", LONG)
            self.run_job("om_1")
            self.send("om_2", SECOND)
            self.run_job("om_2")
            self.send("om_3", THIRD)
        self.fake.listed[("om_3", "OneSecond")] = ["r-q"]
        self.fake.done.clear()
        self.app.apps["claude"].controls.cancel_reply_job_from_card(
            self.ctx, {"job_id": str(self.job_of("om_3").job_id), "message_id": "om_3"}, CHAT, "om_9")
        self.assertIn(("unreact", "om_3", "r-q"), self.marks("om_3"))

    def test_a_reaction_that_fails_does_not_stop_the_message_from_queueing(self):
        self.fake.fail.add("react")
        with mock.patch.dict(os.environ, {butler_tracks.MAX_ENV: "2"}):
            self.send("om_1", LONG)
            self.run_job("om_1")
            self.send("om_2", SECOND)
            self.run_job("om_2")
            self.send("om_3", THIRD)
        self.assertIsNotNone(self.job_of("om_3"))


class MergedTest(ReactionsCase):
    def merged(self):
        self.send("om_1", LONG)
        self.run_job("om_1")
        self.send("om_2", SECOND)
        self.run_job("om_2")
        self.verdicts.append(track_judge.Verdict("merge", 1, 0.9))
        self.fake.done.clear()
        self.send("om_3", "顺便把明天后天的都查一下吧")

    def test_a_message_that_joins_a_task_gets_the_merged_mark_and_keeps_it(self):
        self.merged()
        self.assertEqual(self.marks("om_3"), [("react", "om_3", "THUMBSUP")])
        self.assertEqual(self.lock_free, [True] * len(self.lock_free))

    def test_a_supplement_that_is_refused_is_not_marked_merged(self):
        from test_butler_tracks_runtime import Control
        from covey.core.steer import SteerRefused
        self.send("om_1", LONG)
        self.run_job("om_1", Control(SteerRefused("任务正在收尾")))
        self.fake.done.clear()
        self.send("om_2", "补充一点", parent_id="om_1")
        self.assertNotIn(("react", "om_2", "THUMBSUP"), self.marks("om_2"))

    def test_quoting_it_with_alone_takes_the_mark_off_and_the_new_track_runs_as_usual(self):
        self.merged()
        self.fake.listed[("om_3", "THUMBSUP")] = ["r-m"]
        self.fake.done.clear()
        self.send("om_4", "单独答", parent_id="om_3")
        self.assertEqual(self.marks("om_3"), [("list", "om_3", "THUMBSUP"), ("unreact", "om_3", "r-m")])
        self.assertEqual(self.marks("om_4"), [("react", "om_4", "OnIt")])
        self.assertIsNotNone(self.job_of("om_4"))


class StopTest(ReactionsCase):
    def stopped(self, text="停止"):
        self.send("om_1", LONG)
        self.run_job("om_1")
        self.send("om_2", SECOND)
        self.run_job("om_2")
        with mock.patch.object(self.app.messenger, "reply") as reply:
            self.send("om_3", text)
        return " ".join(str(call.args[2]) for call in reply.call_args_list)

    def test_the_stop_reply_says_how_many_tracks_are_still_going(self):
        self.assertIn("还有 1 件事在做。", self.stopped())

    def test_a_stop_of_the_only_track_says_nothing_of_the_others(self):
        self.send("om_1", LONG)
        self.run_job("om_1")
        with mock.patch.object(self.app.messenger, "reply") as reply:
            self.send("om_2", "停止")
        self.assertNotIn("还有", " ".join(str(call.args[2]) for call in reply.call_args_list))


class SwitchOffTest(UnifiedCase):
    def test_with_the_switch_off_no_reaction_beyond_the_usual_onit(self):
        switch_on(self, "1")
        with mock.patch.dict(os.environ, {butler_tracks.ENV: "0"}):
            self.app.messenger.react.reset_mock()
            self.app.messenger.reaction_ids.reset_mock()
            self.app.butler.tracks.noted("queued", CHAT, "om_1", ctx=self.ctx, app=self.app.apps["claude"])
            self.app.butler.tracks.noted("merged", CHAT, "om_1", ctx=self.ctx, app=self.app.apps["claude"])
            self.app.butler.tracks.noted("alone", CHAT, "om_1", ctx=self.ctx, app=self.app.apps["claude"])
        self.app.messenger.react.assert_not_called()
        self.app.messenger.reaction_ids.assert_not_called()


if __name__ == "__main__":
    unittest.main()
