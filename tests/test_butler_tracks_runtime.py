"""多路并行 at run time (app/butler_tracks.py, docs/butler.md 同时做几件事): a message of the owner's while the butler's
family has live tasks is placed by the router (stop, merge, a track of its own), a track's turn forks the butler's
session and forgets it at its end, and with the switch off none of it happens. A fake backend: the tasks are registry
jobs with a fake control, the small model a stub."""
from __future__ import annotations

import dataclasses
import json
import os
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support.butler import switch_on
from support.records import write_topic
from support.world import UnifiedCase
from covey.app import butler as session
from covey.app import butler_tracks as runtime_tracks
from covey.app import track_judge
from covey.core import butler, butler_tracks
from covey.core.jobs import upsert_job_record
from covey.core.locks import LOCK
from covey.core.state import write_json
from covey.core.steer import SteerRefused
from covey.core.topics import message_topic, remember_topic_message
from covey.feishu import message_request

CHAT = "oc_main"
BUTLER = butler.butler_topic_id(CHAT)
SLOT1 = butler_tracks.track_topic_id(CHAT, 1)
SLOT2 = butler_tracks.track_topic_id(CHAT, 2)
LONG = "帮我查一下上周所有失败任务的原因，并且整理成一份报告"


class Control:
    remote = False
    supports_steer = True

    def __init__(self, error=None, paused=False):
        self.error, self.sent = error, []
        if paused:
            self.pause_for_question = "q"

    def steer(self, text, mid):
        if self.error:
            raise self.error
        self.sent.append((text, mid))


class TracksCase(UnifiedCase):
    tracks = "1"

    def setUp(self):
        switch_on(self, "1")
        patch = mock.patch.dict(os.environ, {butler_tracks.ENV: self.tracks, "COVEY_TRACK_JUDGE": "claude"})
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(message_request.configure_top_level, None)
        super().setUp()
        self.app.runtime.state_dir = self.ctx.state_dir
        self.state = self.ctx.state_dir
        self.cards = iter(range(100))
        for app in self.app.apps.values():
            app.cards.create_streaming_reply = mock.Mock(
                side_effect=lambda *a, **k: SimpleNamespace(message_id=f"om_card{next(self.cards)}", card_id="c"))
            app.cards.set_task_element = mock.Mock(return_value=True)
        self.claude = self.app.apps["claude"]
        notes = mock.patch.object(self.claude.steering, "note_latest_request")
        notes.start()
        self.addCleanup(notes.stop)
        self.verdicts = []
        judge = mock.patch.object(track_judge, "judge", side_effect=self.judged)
        self.judge = judge.start()
        self.addCleanup(judge.stop)
        warm = mock.patch.object(track_judge, "warm", return_value=False)  # No Claude Code is started by a test.
        self.warm = warm.start()
        self.addCleanup(warm.stop)
        self.main = self.app.butler

    def judged(self, message, **kwargs):
        free = []
        thread = threading.Thread(target=lambda: free.append(LOCK.acquire(timeout=1)) or LOCK.release())
        thread.start()
        thread.join()
        self.assertEqual(free, [True], "the small model is asked with LOCK held")
        return self.verdicts.pop(0) if self.verdicts else track_judge.Verdict("new", why="stub")

    def send(self, mid, content="task", **fields):
        event = {"chat_id": CHAT, "chat_type": "p2p", "sender_id": "owner", "message_id": mid, "root_id": "",
                 "content": content, **fields}
        with mock.patch("covey.app.app.enrich_incoming_event") as enrich:
            enrich.return_value = SimpleNamespace(text=content, display_text=content, event=event)
            self.app.handle_event(event, self.ctx)

    def job_of(self, mid):
        return self.app.runtime.jobs.find(0, mid)

    def run_job(self, mid, control=None):
        """The job of message ``mid`` is running, its backend taking supplements through ``control``."""
        job = self.job_of(mid)
        job.execution_started = True
        job.backend_control = control or Control()
        return job

    def topic_of(self, mid):
        return self.job_of(mid).topic_id

    def slot_of(self, mid):
        return butler_tracks.track_of(self.topic_of(mid), CHAT)


class RouteTest(TracksCase):
    def test_two_independent_questions_run_on_two_tracks(self):
        self.send("om_1", LONG)
        self.run_job("om_1")
        self.send("om_2", "明天北京天气怎么样，要不要带伞呢")
        self.assertEqual((self.slot_of("om_1"), self.slot_of("om_2")), (0, 1))
        self.judge.assert_called_once()
        self.assertEqual(json.loads((self.state / "topics" / SLOT1 / "topic.json").read_text())["track"], 1)
        self.assertEqual(message_topic(self.state, CHAT, "om_2"), SLOT1)
        self.assertTrue(self.job_of("om_2").topic_id != self.job_of("om_1").topic_id)
        # A third one takes the next free slot, a fourth waits behind the one with the fewest.
        self.run_job("om_2")
        self.send("om_3", "还有一个问题，帮我看一下日志里报错的数量是多少？")
        self.assertEqual(self.slot_of("om_3"), 2)

    def test_nothing_live_is_the_usual_path(self):
        self.send("om_1", LONG)
        self.assertEqual(self.slot_of("om_1"), 0)
        self.judge.assert_not_called()

    def test_a_message_keeps_a_standby_ready_and_the_judge_starts_the_next_one(self):
        order = mock.Mock()
        order.attach_mock(self.warm, "warm")
        order.attach_mock(self.judge, "judge")
        self.send("om_1", LONG)  # Nothing live yet, but this task is what the next message is told from.
        self.assertEqual([call[0] for call in order.mock_calls], ["warm"])
        self.run_job("om_1")
        self.send("om_2", "明天北京天气怎么样，要不要带伞呢")
        self.assertEqual([call[0] for call in order.mock_calls], ["warm", "warm", "judge", "warm"])
        self.assertIs(self.warm.call_args.args[0], self.claude.harness.backend)

    def test_a_reply_to_the_leads_message_goes_to_the_lead_while_a_track_runs(self):
        # 2026-11-19 (T531): the lead is of the family (core/butler.py is_lead), so a reply to its answer stayed the
        # butler's: a new track while the butler ran, a turn of the butler's root otherwise; never the lead's.
        lead = "1ead" * 8
        write_topic(self.state, topic_id=lead, chat_id=CHAT, harness_id="claude", root_id="", project={"role": "lead"},
                    workspace_name=self.ctx.workspace_name, workdir=str(self.ctx.workdir))
        remember_topic_message(self.state, lead, CHAT, "om_lead_answer")
        self.send("om_1", LONG)
        self.run_job("om_1")
        with mock.patch.object(butler, "is_lead", side_effect=lambda topic, chat: topic == lead):
            self.send("om_2", "这条按你说的办", parent_id="om_lead_answer")
        self.judge.assert_not_called()
        self.assertEqual(self.app.runtime.jobs.topic_jobs(SLOT1, CHAT), [])
        self.assertEqual(message_topic(self.state, CHAT, "om_2"), lead)  # The lead's, not the butler's root.
        self.assertEqual([job.message_id for job in self.app.runtime.jobs.topic_jobs(butler.butler_topic_id(CHAT), CHAT)],
                         ["om_1"])

    def test_stop_word_stops_the_newest_track_and_starts_nothing(self):
        self.send("om_1", LONG)
        self.run_job("om_1")
        self.send("om_2", "明天北京天气怎么样，要不要带伞呢")
        self.run_job("om_2")
        self.send("om_3", "停止")
        self.assertTrue(self.job_of("om_2").cancel_event.is_set())
        self.assertFalse(self.job_of("om_1").cancel_event.is_set())
        self.assertIsNone(self.job_of("om_3"))
        self.assertEqual(self.app.runtime.jobs.topic_jobs(SLOT2, CHAT), [])

    def test_stop_word_quoting_a_message_stops_its_track(self):
        self.send("om_1", LONG)
        self.run_job("om_1")
        self.send("om_2", "明天北京天气怎么样，要不要带伞呢")
        self.run_job("om_2")
        self.send("om_3", "算了", parent_id="om_1")
        self.assertTrue(self.job_of("om_1").cancel_event.is_set())
        self.assertFalse(self.job_of("om_2").cancel_event.is_set())

    def test_a_quote_the_consumer_names_reply_to_is_still_a_quote(self):
        # lark-cli 1.0.69+ gives the quoted message as ``reply_to`` and no ``parent_id``; the quote is read before
        # ``enrich_incoming_event`` (which copies it over), so "停止" quoting the first track stopped the newest one.
        self.send("om_1", LONG)
        self.run_job("om_1")
        self.send("om_2", "明天北京天气怎么样，要不要带伞呢")
        self.run_job("om_2")
        self.send("om_3", "停止", reply_to="om_1", root_id="om_1")
        self.assertTrue(self.job_of("om_1").cancel_event.is_set())
        self.assertFalse(self.job_of("om_2").cancel_event.is_set())

    def test_a_stop_word_reaches_the_runner_of_a_task_that_runs_in_one(self):
        # The task runs in a process of its own (app/detached.py): a stop only set in the Bridge's copy of the job reached
        # nobody, and both tasks ran on and sent their answers (2026-11-15). The stop word is the card's 停止 click,
        # forwarded to the runner like the button's, with how many other tracks go on.
        self.send("om_1", LONG)
        self.run_job("om_1")
        self.send("om_2", "明天北京天气怎么样，要不要带伞呢")
        self.run_job("om_2")
        with mock.patch.object(runtime_tracks.runner_ipc, "forward_card_action", return_value=True) as forward, \
                mock.patch.object(self.claude.controls, "cancel_reply_job_from_card") as local:
            self.send("om_3", "停止", reply_to="om_1")
        local.assert_not_called()
        runtime, target, click, action = forward.call_args.args
        self.assertEqual((target.topic_id, click["message_id"], click["chat_id"]), (BUTLER, "om_3", CHAT))
        self.assertEqual(action, {"kind": "bridge_job_stop", "job_id": str(self.job_of("om_1").job_id),
                                  "message_id": "om_1", "by": "word", "others": "1"})
        self.assertEqual(click["action_value"], action)

    def test_the_forwarded_stop_stops_the_job_and_says_how_many_tracks_go_on(self):
        self.send("om_1", LONG)
        job = self.run_job("om_1")
        self.send("om_2", "明天北京天气怎么样，要不要带伞呢")
        self.run_job("om_2")
        action = {"kind": "bridge_job_stop", "job_id": str(job.job_id), "message_id": "om_1", "others": "1"}
        click = {"chat_id": CHAT, "message_id": "om_3", "event_id": "track-stop-om_3", "operator_id": self.app.runtime.owner_open_id,
                 "action_value": action}
        runtime_tracks.move_topic_message(self.state, BUTLER, CHAT, "om_3")  # What Tracks.stop does before it forwards.
        with mock.patch.object(self.app.messenger, "reply") as reply:
            self.app.handle_card_action_event(click, self.ctx)
        self.assertTrue(job.cancel_event.is_set())
        self.assertFalse(self.job_of("om_2").cancel_event.is_set())
        self.assertIn("还有 1 件事在做。", " ".join(str(call.args[2]) for call in reply.call_args_list))

    def test_the_answer_to_a_track_that_asks_joins_it(self):
        self.send("om_1", LONG)
        control = Control(paused=True)
        self.run_job("om_1", control)
        self.send("om_2", "好的")
        self.judge.assert_not_called()
        self.assertEqual(control.sent, [])  # A paused task takes no supplement: the usual continuation, queued behind.
        self.assertEqual([job.message_id for job in self.app.runtime.jobs.topic_jobs(BUTLER, CHAT)], ["om_1", "om_2"])

    def test_the_judge_runs_claude_code_with_its_own_context_not_the_default_harness(self):
        # Deploy check 2026-11-16: with Codex the default Harness the event's context made the judge run `codex -p`.
        self.ctx = dataclasses.replace(self.ctx, backend_cmd=["codex"])
        own = list(self.claude.harness.context.backend_cmd)
        self.assertNotEqual(own, ["codex"])
        self.send("om_1", LONG)
        self.run_job("om_1")
        self.send("om_2", "明天北京天气怎么样，要不要带伞呢")
        self.assertTrue(self.judge.called)
        self.assertEqual(list(self.judge.call_args.kwargs["ctx"].backend_cmd), own)
        self.assertTrue(all(list(call.args[1].backend_cmd) == own for call in self.warm.call_args_list))

    def test_a_judge_merge_is_a_supplement_to_that_track(self):
        self.send("om_1", LONG)
        self.run_job("om_1")
        self.send("om_2", "明天北京天气怎么样，要不要带伞呢")
        control = self.run_job("om_2").backend_control
        self.verdicts.append(track_judge.Verdict("merge", 1, 0.9))
        self.send("om_3", "顺便把明天后天的都查一下吧")
        self.assertEqual(control.sent, [("顺便把明天后天的都查一下吧", "om_3")])
        self.assertIsNone(self.job_of("om_3"))

    def test_a_supplement_the_task_refuses_becomes_a_track_of_its_own(self):
        self.send("om_1", LONG)
        self.run_job("om_1", Control(SteerRefused("任务正在收尾")))
        self.send("om_2", "补充一点", parent_id="om_1")  # Quoting the task's message: rule 4, a supplement.
        self.assertEqual(self.slot_of("om_2"), 1)
        self.assertEqual(self.job_of("om_2").topic_id, SLOT1)
        self.assertEqual(message_topic(self.state, CHAT, "om_2"), SLOT1)
        self.assertEqual([job.message_id for job in self.app.runtime.jobs.topic_jobs(BUTLER, CHAT)], ["om_1"])

    def test_a_task_that_ended_while_the_model_thought_makes_a_new_track(self):
        self.send("om_1", LONG)
        job = self.run_job("om_1")

        def ends(message, **kwargs):
            self.app.runtime.jobs.unregister(job)
            return track_judge.Verdict("merge", 0, 0.9)
        self.judge.side_effect = ends
        self.send("om_2", "顺便把明天后天的都查一下吧")
        self.assertEqual(self.slot_of("om_2"), 0)  # Nothing is live any more: the butler's own slot, no fork.

    def test_a_forward_and_the_words_after_it_go_together(self):
        self.send("om_1", LONG)
        control = self.run_job("om_1").backend_control
        self.send("om_2", "{}", message_type="interactive")  # Rule 7: into the newest track.
        self.send("om_3", "你看看这个怎么样呢，写得清楚吗", message_type="text")
        self.judge.assert_not_called()
        self.assertIsNone(self.job_of("om_3"))
        self.assertEqual([mid for _, mid in control.sent], ["om_2", "om_3"])

    def test_the_held_forward_takes_the_words_without_the_router(self):
        self.send("om_1", "{}", message_type="interactive")
        job = self.job_of("om_1")
        self.assertGreater(job.start_held_until, 0)
        self.send("om_2", "帮我看看这张卡里写了什么内容吧", message_type="text")
        self.judge.assert_not_called()
        self.assertIn("帮我看看这张卡里写了什么", job.prompt_text)
        self.assertEqual(job.start_held_until, 0)

    def test_the_router_runs_the_judge_only_when_it_cannot_tell(self):
        self.send("om_1", LONG)
        self.run_job("om_1")
        with mock.patch.object(track_judge, "mode", return_value="off"):
            self.send("om_2", "明天北京天气怎么样，要不要带伞呢")
        self.judge.assert_not_called()
        self.assertEqual(self.slot_of("om_2"), 1)


class StopButtonTest(TracksCase):
    """The buttons stop a task as the stop word does, and say how many other tracks go on (the same count,
    app/butler_tracks.py ``others_going``): the task card's 停止 and the 派发卡's ⏹ 停止."""
    OTHER_TOPIC = "a" * 32  # A topic of its own, as the topic a 派发卡 hands a task to.

    def two_tracks(self):
        self.send("om_1", LONG)
        first = self.run_job("om_1")
        self.send("om_2", "明天北京天气怎么样，要不要带伞呢")
        self.run_job("om_2")
        return first

    def task_click(self, job, message="om_click"):
        runtime_tracks.move_topic_message(self.state, job.topic_id, CHAT, message)
        action = {"kind": "bridge_job_stop", "job_id": str(job.job_id), "message_id": job.message_id}
        return {"chat_id": CHAT, "message_id": message, "event_id": f"ev-{message}",
                "operator_id": self.app.runtime.owner_open_id, "action_value": action}

    def dispatch_click(self, job_id):
        runtime_tracks.move_topic_message(self.state, self.OTHER_TOPIC, CHAT, "om_dispatch")
        action = {"kind": "bridge_dispatch_stop", "topic_id": self.OTHER_TOPIC, "job_id": str(job_id)}
        return {"chat_id": CHAT, "message_id": "om_dispatch", "event_id": "ev-dispatch",
                "operator_id": self.app.runtime.owner_open_id, "action_value": action}

    def dispatched_job(self, number=90):
        from covey.core.jobs import BridgeReplyJob
        job = BridgeReplyJob(job_id=number, user_text="x", chat_id=CHAT, message_id="om_job90", timeout=0,
                             topic_id=self.OTHER_TOPIC)
        job.execution_started, job.backend_control = True, Control()
        self.app.runtime.jobs.register(job)
        return job

    def said(self, reply):
        return " ".join(str(call.args[2]) for call in reply.call_args_list)

    def click(self, event):
        with mock.patch.object(self.app.messenger, "reply") as reply:
            self.app.handle_card_action_event(event, self.ctx)
        return reply

    def test_the_task_cards_stop_says_how_many_other_tracks_go_on(self):
        job = self.two_tracks()
        reply = self.click(self.task_click(job))
        self.assertTrue(job.cancel_event.is_set())
        self.assertFalse(self.job_of("om_2").cancel_event.is_set())
        self.assertIn("还有 1 件事在做。", self.said(reply))

    def test_the_task_cards_stop_says_nothing_when_no_other_track_goes_on(self):
        self.send("om_1", LONG)
        job = self.run_job("om_1")
        reply = self.click(self.task_click(job))
        self.assertTrue(job.cancel_event.is_set())
        self.assertNotIn("还有", self.said(reply))

    def stopped_text(self, job):
        from covey.app import worker
        from covey.core.harness import JobCancelled
        return worker._turn_from_exception(JobCancelled("stopped"), job, True)["final_text"]

    def test_the_stopped_text_on_the_card_says_how_many_other_tracks_go_on(self):
        # A task that has its card gets no text reply, only its stopped text (the 2026-11-17 acceptance: #17 stopped,
        # #16 going on, and the card said nothing of it). The click here is the one its task runner gets.
        job = self.two_tracks()
        click = self.task_click(job)
        click["action_value"]["others"] = "1"
        self.click(click)
        self.assertEqual(job.stop_others, 1)
        self.assertIn("已保存的结果和记录保留在私聊里。还有 1 件事在做。", self.stopped_text(job))

    def test_the_stopped_text_says_nothing_of_other_tracks_when_none_goes_on(self):
        self.send("om_1", LONG)
        job = self.run_job("om_1")
        self.click(self.task_click(job))
        self.assertEqual(job.stop_others, 0)
        self.assertNotIn("还有", self.stopped_text(job))

    def test_a_track_waiting_for_the_owner_is_not_one_that_goes_on(self):
        self.send("om_1", LONG)
        job = self.run_job("om_1")
        self.send("om_2", "明天北京天气怎么样，要不要带伞呢")
        self.run_job("om_2", Control(paused=True))
        self.assertNotIn("还有", self.said(self.click(self.task_click(job))))

    def test_the_forwarded_click_carries_the_count(self):
        job = self.two_tracks()
        with mock.patch("covey.app.card_routing.runner_ipc.forward_card_action", return_value=True) as forward:
            self.app.handle_card_action_event(self.task_click(job), self.ctx)
        action = forward.call_args.args[2]["action_value"]
        self.assertEqual((action["others"], action["job_id"]), ("1", str(job.job_id)))

    def test_the_forwarded_click_of_a_lone_track_carries_no_count(self):
        self.send("om_1", LONG)
        job = self.run_job("om_1")
        with mock.patch("covey.app.card_routing.runner_ipc.forward_card_action", return_value=True) as forward:
            self.app.handle_card_action_event(self.task_click(job), self.ctx)
        self.assertNotIn("others", forward.call_args.args[2]["action_value"])

    def test_the_task_runner_says_it_from_the_click(self):
        job = self.two_tracks()
        click = self.task_click(job)
        click["action_value"]["others"] = "1"
        self.assertTrue(self.claude.controls.stop_action(self.ctx, click))
        self.assertTrue(job.cancel_event.is_set())

    def test_the_dispatch_cards_stop_says_how_many_tracks_go_on(self):
        self.two_tracks()
        job = self.dispatched_job()
        reply = self.click(self.dispatch_click(job.job_id))
        self.assertTrue(job.cancel_event.is_set())
        self.assertFalse(self.job_of("om_1").cancel_event.is_set())
        self.assertIn("还有 2 件事在做。", self.said(reply))

    def test_the_dispatch_cards_stop_says_nothing_when_no_track_goes_on(self):
        job = self.dispatched_job()
        reply = self.click(self.dispatch_click(job.job_id))
        self.assertTrue(job.cancel_event.is_set())
        self.assertNotIn("还有", self.said(reply))

    def test_the_forwarded_dispatch_click_carries_the_count(self):
        self.two_tracks()
        job = self.dispatched_job()
        with mock.patch("covey.app.card_routing.runner_ipc.forward_card_action", return_value=True) as forward:
            self.app.handle_card_action_event(self.dispatch_click(job.job_id), self.ctx)
        self.assertEqual(forward.call_args.args[2]["action_value"]["others"], "2")
        self.assertEqual(forward.call_args.args[2]["action_value"]["job_id"], str(job.job_id))

    def test_a_task_that_is_none_of_the_butlers_says_nothing_of_it(self):
        self.two_tracks()
        job = self.dispatched_job()
        click = self.task_click(job)  # The task card of an ordinary topic: not a track of the butler's.
        reply = self.click(click)
        self.assertTrue(job.cancel_event.is_set())
        self.assertNotIn("还有", self.said(reply))

    def test_with_the_switch_off_a_button_says_nothing_of_it(self):
        job = self.two_tracks()
        with mock.patch.dict(os.environ, {butler_tracks.ENV: "0"}):
            self.assertNotIn("还有", self.said(self.click(self.task_click(job))))


class CapacityTest(TracksCase):
    def test_at_the_limit_a_new_request_queues_behind_the_least_busy_track(self):
        with mock.patch.dict(os.environ, {butler_tracks.MAX_ENV: "2"}):
            self.send("om_1", LONG)
            self.run_job("om_1")
            self.send("om_2", "明天北京天气怎么样，要不要带伞呢")
            self.run_job("om_2")
            with mock.patch.object(self.main.tracks, "noted") as noted:
                self.send("om_3", "再来一个问题，帮我看一下日志里的报错")
            noted.assert_any_call("queued", CHAT, "om_3", ctx=mock.ANY, app=mock.ANY)
            self.assertEqual(self.slot_of("om_3"), 0)  # One each: the lowest slot.
            self.send("om_4", "又一个新的问题，现在磁盘还剩多少空间呢")
            self.assertEqual(self.slot_of("om_4"), 1)  # Slot 0 has two now.
            self.assertEqual([job.message_id for job in self.app.runtime.jobs.topic_jobs(BUTLER, CHAT)], ["om_1", "om_3"])

    def test_a_track_that_waits_for_an_answer_is_not_given_to_another(self):
        self.send("om_1", LONG)
        self.run_job("om_1")
        self.send("om_2", "明天北京天气怎么样，要不要带伞呢")
        self.run_job("om_2", Control(paused=True))
        self.send("om_3", "还有一个问题，帮我看一下日志里报错的数量是多少？")
        self.assertEqual(self.slot_of("om_3"), 2)

    def test_a_waiting_record_holds_its_slot_and_a_free_one_forgets_its_session(self):
        # Slot 1's task left the registry waiting for the owner; slot 2 has a session pointer an old turn left.
        upsert_job_record(self.state, "om_w", status="waiting_user", topic_id=SLOT1, chat_id=CHAT, job_id=7,
                          user_text="old question")
        pointer = self.state / "topics" / SLOT2 / self.claude.harness.spec.session_filename
        pointer.parent.mkdir(parents=True, exist_ok=True)
        pointer.write_text("00000000-0000-4000-8000-000000000001")
        self.send("om_1", LONG)
        self.run_job("om_1")
        self.send("om_2", "明天北京天气怎么样？要不要带伞呢")
        self.assertEqual(self.slot_of("om_2"), 2)
        self.assertFalse(pointer.exists() and pointer.read_text().strip())


class OffTest(TracksCase):
    tracks = ""

    def test_with_the_switch_off_the_router_is_never_asked(self):
        self.send("om_1", LONG)
        self.run_job("om_1", Control(SteerRefused("x")))
        with mock.patch.object(self.main.tracks, "handle") as handle:
            self.send("om_2", "补充一点", parent_id="om_1")
        handle.assert_not_called()
        self.warm.assert_not_called()  # No standby Claude Code without the switch.
        self.assertEqual([job.message_id for job in self.app.runtime.jobs.topic_jobs(BUTLER, CHAT)], ["om_1", "om_2"])

    def test_the_mode_pure_topics_turns_it_off_too(self):
        with mock.patch.dict(os.environ, {butler_tracks.ENV: "1"}), mock.patch.object(butler, "enabled", return_value=False):
            self.assertFalse(runtime_tracks.Tracks.enabled())
        with mock.patch.dict(os.environ, {butler_tracks.ENV: "1"}):
            self.assertTrue(runtime_tracks.Tracks.enabled())

    def test_a_track_that_waits_still_finds_its_session_by_its_topic(self):
        # The switch was on and is off again: the slot's topic is a plain topic with its own session pointer.
        pointer = self.state / "topics" / SLOT1 / self.claude.harness.spec.session_filename
        write_json(self.state / "topics" / SLOT1 / "topic.json", {
            "topic_id": SLOT1, "chat_id": CHAT, "root_id": "", "thread_id": "", "track": 1, "main_thread": True,
            "workspace_name": self.ctx.workspace_name, "workdir": str(self.ctx.workdir), "harness_id": "claude",
            "binding_revision": 1})
        pointer.write_text("00000000-0000-4000-8000-000000000002")
        from covey.core.topics import remember_topic_message
        remember_topic_message(self.state, SLOT1, CHAT, "om_q")
        from covey.core.topics import context_for_topic
        target = context_for_topic(self.claude.harness.context, {"chat_id": CHAT, "message_id": "om_a", "parent_id": "om_q",
                                                                "root_id": ""}, self.claude.harness.store.sessions_dir)
        self.assertEqual(target.topic_id, SLOT1)
        self.assertEqual(target.session_path, pointer)
        self.assertFalse(butler.traits(SLOT1, CHAT).own_turn)


class TurnTest(TracksCase):
    """A track's turn (butler.butler_turn, app/butler_tracks.py fork_options)."""

    SESSION = "11111111-1111-4111-8111-111111111111"

    def setUp(self):
        super().setUp()
        self.sessions = self.state / "claude-sessions"

    def harness(self, *, keep="K1", native_fork=True, rewind=True):
        backend = SimpleNamespace(rewind_point=mock.Mock(return_value={"keep": keep}))
        harness = SimpleNamespace(backend=backend, spec=self.claude.harness.spec)
        caps = SimpleNamespace(native_fork=native_fork, rewind=rewind)
        patch = mock.patch.object(runtime_tracks, "backend_capabilities", return_value=caps)
        patch.start()
        self.addCleanup(patch.stop)
        return harness

    def root_session(self, session=SESSION):
        folder = self.state / "topics" / BUTLER
        folder.mkdir(parents=True, exist_ok=True)
        write_json(folder / "topic.json", {"topic_id": BUTLER, "chat_id": CHAT, "workdir": str(self.claude.harness.context.workdir)})
        (folder / self.claude.harness.spec.session_filename).write_text(session)
        self.sessions.mkdir(parents=True, exist_ok=True)
        (self.sessions / f"{session}.jsonl").write_text("{}\n")

    def track_job(self, mid="om_9", slot=1):
        job = SimpleNamespace(topic_id=butler_tracks.track_topic_id(CHAT, slot), chat_id=CHAT, message_id=mid, job_id=9,
                              kind="chat", prompt_text=None, user_text="新问题", cancel_event=threading.Event())
        return job

    def turn(self, job, harness, session_id=None, compaction=None):
        ctx = self.claude.submission.event_context(self.claude.harness.context, {"chat_id": CHAT, "message_id": job.message_id})
        ctx = dataclasses.replace(ctx, sessions_dir=self.sessions)
        compaction = compaction or SimpleNamespace(request=mock.Mock(return_value=None))
        with mock.patch.object(butler, "traits_of", return_value=butler.BUTLER):
            return session.butler_turn(ctx, job, "RAW", session_id, compaction, self.logs.append, harness), compaction

    def opened(self, mid="om_9", slot=1):
        topic = butler_tracks.track_topic_id(CHAT, slot)
        write_json(self.state / "topics" / topic / "topic.json", {"topic_id": topic, "chat_id": CHAT})

    def test_a_new_track_forks_the_butlers_session_before_the_question_under_way(self):
        self.root_session()
        upsert_job_record(self.state, "om_r", status="running", topic_id=BUTLER, chat_id=CHAT, job_id=3,
                          prompt_uuid="U1", prompt_session=self.SESSION)
        harness = self.harness()
        job = self.track_job()
        (ctx, prompt), compaction = self.turn(job, harness)
        self.assertEqual((ctx.options["fork_session"], ctx.options["fork_at"]), (self.SESSION, "K1"))
        self.assertTrue(ctx.options["track_fork"])
        harness.backend.rewind_point.assert_called_once()
        self.assertEqual(harness.backend.rewind_point.call_args.args[1:], (self.SESSION, "U1"))
        self.assertIn("Latest Feishu user message:\n新问题", prompt)
        self.assertNotIn("Recent conversation", prompt)
        self.assertIn("同时在做的几件事之一", prompt)
        self.assertIn("[快照", prompt)
        compaction.request.assert_not_called()

    def test_with_no_turn_under_way_the_whole_session_is_forked(self):
        self.root_session()
        (ctx, prompt), _ = self.turn(self.track_job(), self.harness())
        self.assertEqual(ctx.options["fork_session"], self.SESSION)
        self.assertNotIn("fork_at", ctx.options)

    def test_the_other_tracks_are_in_the_note(self):
        self.root_session()
        upsert_job_record(self.state, "om_a", status="running", topic_id=BUTLER, chat_id=CHAT, job_id=3,
                          user_text="帮我查失败任务的原因")
        (_, prompt), _ = self.turn(self.track_job(), self.harness())
        self.assertIn("此刻另外几件事在做", prompt)
        self.assertIn("#3「帮我查失败任务的原因」· 在做", prompt)

    def test_without_a_fork_the_track_starts_a_new_session_with_the_recent_conversation(self):
        (self.state / "topics" / BUTLER).mkdir(parents=True, exist_ok=True)
        (self.state / "topics" / BUTLER / "history.jsonl").write_text(
            json.dumps({"role": "user", "content": "之前聊过的事", "message_id": "om_old"}) + "\n")
        (ctx, prompt), _ = self.turn(self.track_job(), self.harness())  # The butler has no session yet.
        self.assertNotIn("fork_session", ctx.options)
        self.assertIn("Recent conversation:", prompt)
        self.assertIn("之前聊过的事", prompt)
        self.assertNotIn("track_fork", ctx.options)

    def test_a_question_whose_place_is_empty_falls_back(self):
        self.root_session()
        upsert_job_record(self.state, "om_r", status="running", topic_id=BUTLER, chat_id=CHAT, job_id=3,
                          prompt_uuid="U1", prompt_session=self.SESSION)
        (ctx, prompt), _ = self.turn(self.track_job(), self.harness(keep=""))
        self.assertNotIn("fork_session", ctx.options)
        self.assertIn("Recent conversation:", prompt)

    def test_a_backend_that_cannot_fork_falls_back(self):
        self.root_session()
        (ctx, _), _ = self.turn(self.track_job(), self.harness(native_fork=False))
        self.assertNotIn("fork_session", ctx.options)

    def test_the_snapshot_is_whole_and_the_butlers_record_is_left_alone(self):
        self.root_session()
        path = self.state / "topics" / BUTLER / "main-snapshot.json"
        write_json(path, {"schema_version": 1, "job": "mine", "at": 1.0, "groups": {}})
        before = path.read_text()
        (_, prompt), _ = self.turn(self.track_job(), self.harness())
        self.assertIn("：全部]", prompt)
        self.assertEqual(path.read_text(), before)

    def test_a_forked_track_gets_the_reminders_not_the_first_turns_rules(self):
        """app/runs.py _prompt: the fork has its source's conversation (and the rules in it), so the message is a resumed
        session's, not a new one's with the whole rules in front."""
        from covey.app.runs import _Live
        from covey.core.jobs import context_for_job
        self.send("om_1", LONG)
        self.run_job("om_1")
        self.send("om_2", "明天北京天气怎么样，要不要带伞呢")
        job = self.job_of("om_2")
        ctx = context_for_job(self.claude.submission.event_context(self.claude.harness.context,
                                                                    {"chat_id": CHAT, "message_id": "om_2"}), job)
        forked = {"fork_session": self.SESSION, "fork_at": "K1"}
        for options, fresh in ((forked, False), (None, True)):
            with mock.patch.object(runtime_tracks, "fork_options", return_value=options):
                prompt, rules, system, _ = self.claude.runs._prompt(_Live(ctx), job, None)
            self.assertEqual(rules in prompt, fresh, options)
            self.assertIn("同时在做的几件事之一" if options else "Recent conversation:", prompt)

    def test_a_track_on_its_own_session_resumes_it_and_forks_nothing(self):
        self.root_session()
        (ctx, prompt), _ = self.turn(self.track_job(), self.harness(), session_id="22222222-2222-4222-8222-222222222222")
        self.assertNotIn("fork_session", ctx.options)
        self.assertTrue(prompt.endswith("RAW"))
        self.assertIn("同时在做的几件事之一", prompt)

    def test_the_butlers_own_turn_is_what_it_was(self):
        job = self.track_job(slot=0)
        job.topic_id = BUTLER
        (ctx, prompt), compaction = self.turn(job, self.harness())
        self.assertNotIn("并行", prompt)
        compaction.request.assert_called_once()

    def test_a_track_never_asks_for_a_compaction(self):
        compaction = mock.Mock()
        job = self.track_job()
        with mock.patch.object(butler, "traits_of", return_value=butler.BUTLER):
            session.compact_butler(compaction, self.ctx, job)
        compaction.set_requested.assert_not_called()
        compaction.supported.assert_not_called()


class ReleaseTest(TracksCase):
    def pointer(self, topic):
        path = self.state / "topics" / topic / self.claude.harness.spec.session_filename
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def test_the_session_pointer_goes_when_the_turn_ends_but_not_the_butlers(self):
        track, root = self.pointer(SLOT1), self.pointer(BUTLER)
        track.write_text("00000000-0000-4000-8000-000000000003")
        root.write_text("00000000-0000-4000-8000-000000000004")
        ctx = SimpleNamespace(state_dir=self.state)
        log = mock.Mock()
        for topic in (BUTLER, SLOT1):
            runtime_tracks.release_pointer(ctx, SimpleNamespace(topic_id=topic, chat_id=CHAT, job_id=1),
                                           self.claude.harness, SimpleNamespace(session_lock=threading.RLock(), log=log))
        self.assertFalse(track.exists() and track.read_text().strip())
        self.assertTrue(root.read_text().strip())

    def test_the_worker_keeps_the_session_of_a_turn_that_asks(self):
        from covey.app import worker
        self.assertIn("release_pointer", Path(worker.__file__).read_text())
        track = self.pointer(SLOT1)
        track.write_text("00000000-0000-4000-8000-000000000005")
        job = SimpleNamespace(topic_id=SLOT1, chat_id=CHAT, job_id=2, message_id="om_x", kind="chat", cancelled_before_start=False,
                              execution_lock=threading.Lock(), execution_started=False)
        turn = SimpleNamespace(status="waiting_user")
        calls = []
        with mock.patch.object(worker, "context_for_job", side_effect=lambda ctx, job: ctx), \
                mock.patch.object(worker, "_start_turn", return_value=(0, None)), \
                mock.patch.object(worker, "_run_turn", return_value=(turn, None)), \
                mock.patch.object(worker, "_deliver_turn"), \
                mock.patch.object(worker.butler_tracks, "release_pointer", side_effect=lambda *a: calls.append(a)):
            harness = SimpleNamespace(backend=None, spec=self.claude.harness.spec)
            worker.process_bridge_reply_job(job, self.ctx, harness=harness, runtime=self.app.runtime, messenger=None,
                                            cards=None, sessions=None, runs=None, artifacts=None, questions=None,
                                            steering=None, record_job_state=mock.Mock())
            self.assertEqual(calls, [])  # waiting_user: kept.
            turn.status = "done"
            worker.process_bridge_reply_job(job, self.ctx, harness=harness, runtime=self.app.runtime, messenger=None,
                                            cards=None, sessions=None, runs=None, artifacts=None, questions=None,
                                            steering=None, record_job_state=mock.Mock())
            self.assertEqual(len(calls), 1)


class StoppedReplyTest(TracksCase):
    """The owner's stop of a task of the family drops the reply a stopped Claude turn kept while only background tasks ran
    (backends/claude/runtime.py ``keep_last_reply``); a task of another topic, or with the switch off, still sends it."""

    def settle(self, topic, *, by="card", tracks="1"):
        from covey.app import turn_settle
        from covey.core.result import ExecutionOutcome
        outcome = ExecutionOutcome("cancelled", "cancelled", True, True, True, False, "a" * 64)
        result = SimpleNamespace(text="Reply 1", session_id="sid", is_error=False, outcome=outcome,
                                 public_reply=SimpleNamespace(fingerprint="a" * 64, session_id="sid"))
        job = SimpleNamespace(cancel_event=threading.Event(), interrupted=False, topic_id=topic, chat_id=CHAT,
                              cancel_requested_by=by, session_id="", result_backend="claude", model="m",
                              native_session_claims=set())
        job.cancel_event.set()
        with mock.patch.dict(os.environ, {butler_tracks.ENV: tracks}), mock.patch.object(turn_settle, "observe"):
            return turn_settle._turn_facts(SimpleNamespace(allow_resume=False), result, self.ctx, job, session_id="sid",
                                           runtime=self.app.runtime, prefix="claude")

    def test_the_owners_stop_of_a_track_sends_no_reply(self):
        from covey.core.harness import JobCancelled
        for topic in (BUTLER, SLOT1):
            with self.subTest(topic=topic), self.assertRaises(JobCancelled):
                self.settle(topic)

    def test_everything_else_still_sends_the_kept_reply(self):
        for label, topic, by, tracks in (("another topic", "other-topic", "card", "1"), ("switch off", BUTLER, "card", "0"),
                                         ("no stop request", BUTLER, "", "1")):
            with self.subTest(label):
                reply, _, outcome, _ = self.settle(topic, by=by, tracks=tracks)
                self.assertEqual((reply, outcome.status), ("Reply 1", "cancelled"))


if __name__ == "__main__":
    unittest.main()
