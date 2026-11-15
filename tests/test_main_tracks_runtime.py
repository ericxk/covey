"""多路并行 at run time (app/main_tracks.py, docs/main-thread.md 多路并行): a message of the owner's while the main thread's
family has live tasks is placed by the router (stop, merge, a track of its own), a track's turn forks the main thread's
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

from support.main_thread import switch_on
from support.world import UnifiedCase
from covey.app import main_thread as session
from covey.app import main_tracks as runtime_tracks
from covey.app import track_judge
from covey.core import main_thread, main_tracks
from covey.core.jobs import upsert_job_record
from covey.core.locks import LOCK
from covey.core.state import write_json
from covey.core.steer import SteerRefused
from covey.core.topics import message_topic
from covey.feishu import message_request

CHAT = "oc_main"
MAIN = main_thread.main_topic_id(CHAT)
SLOT1 = main_tracks.track_topic_id(CHAT, 1)
SLOT2 = main_tracks.track_topic_id(CHAT, 2)
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
        patch = mock.patch.dict(os.environ, {main_tracks.ENV: self.tracks, "COVEY_TRACK_JUDGE": "claude"})
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
        self.main = self.app.main_thread

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
        return main_tracks.track_of(self.topic_of(mid), CHAT)


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

    def test_the_answer_to_a_track_that_asks_joins_it(self):
        self.send("om_1", LONG)
        control = Control(paused=True)
        self.run_job("om_1", control)
        self.send("om_2", "好的")
        self.judge.assert_not_called()
        self.assertEqual(control.sent, [])  # A paused task takes no supplement: the usual continuation, queued behind.
        self.assertEqual([job.message_id for job in self.app.runtime.jobs.topic_jobs(MAIN, CHAT)], ["om_1", "om_2"])

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
        self.assertEqual([job.message_id for job in self.app.runtime.jobs.topic_jobs(MAIN, CHAT)], ["om_1"])

    def test_a_task_that_ended_while_the_model_thought_makes_a_new_track(self):
        self.send("om_1", LONG)
        job = self.run_job("om_1")

        def ends(message, **kwargs):
            self.app.runtime.jobs.unregister(job)
            return track_judge.Verdict("merge", 0, 0.9)
        self.judge.side_effect = ends
        self.send("om_2", "顺便把明天后天的都查一下吧")
        self.assertEqual(self.slot_of("om_2"), 0)  # Nothing is live any more: the main thread's own slot, no fork.

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


class CapacityTest(TracksCase):
    def test_at_the_limit_a_new_request_queues_behind_the_least_busy_track(self):
        with mock.patch.dict(os.environ, {main_tracks.MAX_ENV: "2"}):
            self.send("om_1", LONG)
            self.run_job("om_1")
            self.send("om_2", "明天北京天气怎么样，要不要带伞呢")
            self.run_job("om_2")
            with mock.patch.object(self.main.tracks, "noted") as noted:
                self.send("om_3", "再来一个问题，帮我看一下日志里的报错")
            noted.assert_any_call("queued", CHAT, "om_3")
            self.assertEqual(self.slot_of("om_3"), 0)  # One each: the lowest slot.
            self.send("om_4", "又一个新的问题，现在磁盘还剩多少空间呢")
            self.assertEqual(self.slot_of("om_4"), 1)  # Slot 0 has two now.
            self.assertEqual([job.message_id for job in self.app.runtime.jobs.topic_jobs(MAIN, CHAT)], ["om_1", "om_3"])

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
        self.assertEqual([job.message_id for job in self.app.runtime.jobs.topic_jobs(MAIN, CHAT)], ["om_1", "om_2"])

    def test_the_mode_pure_topics_turns_it_off_too(self):
        with mock.patch.dict(os.environ, {main_tracks.ENV: "1"}), mock.patch.object(main_thread, "enabled", return_value=False):
            self.assertFalse(runtime_tracks.Tracks.enabled())
        with mock.patch.dict(os.environ, {main_tracks.ENV: "1"}):
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
        self.assertFalse(main_thread.traits(SLOT1, CHAT).own_turn)


class TurnTest(TracksCase):
    """A track's turn (main_thread.main_turn, app/main_tracks.py fork_options)."""

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
        folder = self.state / "topics" / MAIN
        folder.mkdir(parents=True, exist_ok=True)
        write_json(folder / "topic.json", {"topic_id": MAIN, "chat_id": CHAT, "workdir": str(self.claude.harness.context.workdir)})
        (folder / self.claude.harness.spec.session_filename).write_text(session)
        self.sessions.mkdir(parents=True, exist_ok=True)
        (self.sessions / f"{session}.jsonl").write_text("{}\n")

    def track_job(self, mid="om_9", slot=1):
        job = SimpleNamespace(topic_id=main_tracks.track_topic_id(CHAT, slot), chat_id=CHAT, message_id=mid, job_id=9,
                              kind="chat", prompt_text=None, user_text="新问题", cancel_event=threading.Event())
        return job

    def turn(self, job, harness, session_id=None, compaction=None):
        ctx = self.claude.submission.event_context(self.claude.harness.context, {"chat_id": CHAT, "message_id": job.message_id})
        ctx = dataclasses.replace(ctx, sessions_dir=self.sessions)
        compaction = compaction or SimpleNamespace(request=mock.Mock(return_value=None))
        with mock.patch.object(main_thread, "traits_of", return_value=main_thread.MAIN):
            return session.main_turn(ctx, job, "RAW", session_id, compaction, self.logs.append, harness), compaction

    def opened(self, mid="om_9", slot=1):
        topic = main_tracks.track_topic_id(CHAT, slot)
        write_json(self.state / "topics" / topic / "topic.json", {"topic_id": topic, "chat_id": CHAT})

    def test_a_new_track_forks_the_main_threads_session_before_the_question_under_way(self):
        self.root_session()
        upsert_job_record(self.state, "om_r", status="running", topic_id=MAIN, chat_id=CHAT, job_id=3,
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
        self.assertIn("并行的一路", prompt)
        self.assertIn("[快照", prompt)
        compaction.request.assert_not_called()

    def test_with_no_turn_under_way_the_whole_session_is_forked(self):
        self.root_session()
        (ctx, prompt), _ = self.turn(self.track_job(), self.harness())
        self.assertEqual(ctx.options["fork_session"], self.SESSION)
        self.assertNotIn("fork_at", ctx.options)

    def test_the_other_tracks_are_in_the_note(self):
        self.root_session()
        upsert_job_record(self.state, "om_a", status="running", topic_id=MAIN, chat_id=CHAT, job_id=3,
                          user_text="帮我查失败任务的原因")
        (_, prompt), _ = self.turn(self.track_job(), self.harness())
        self.assertIn("此刻另几路在做", prompt)
        self.assertIn("#3「帮我查失败任务的原因」· 在做", prompt)

    def test_without_a_fork_the_track_starts_a_new_session_with_the_recent_conversation(self):
        (self.state / "topics" / MAIN).mkdir(parents=True, exist_ok=True)
        (self.state / "topics" / MAIN / "history.jsonl").write_text(
            json.dumps({"role": "user", "content": "之前聊过的事", "message_id": "om_old"}) + "\n")
        (ctx, prompt), _ = self.turn(self.track_job(), self.harness())  # The main thread has no session yet.
        self.assertNotIn("fork_session", ctx.options)
        self.assertIn("Recent conversation:", prompt)
        self.assertIn("之前聊过的事", prompt)
        self.assertNotIn("track_fork", ctx.options)

    def test_a_question_whose_place_is_empty_falls_back(self):
        self.root_session()
        upsert_job_record(self.state, "om_r", status="running", topic_id=MAIN, chat_id=CHAT, job_id=3,
                          prompt_uuid="U1", prompt_session=self.SESSION)
        (ctx, prompt), _ = self.turn(self.track_job(), self.harness(keep=""))
        self.assertNotIn("fork_session", ctx.options)
        self.assertIn("Recent conversation:", prompt)

    def test_a_backend_that_cannot_fork_falls_back(self):
        self.root_session()
        (ctx, _), _ = self.turn(self.track_job(), self.harness(native_fork=False))
        self.assertNotIn("fork_session", ctx.options)

    def test_the_snapshot_is_whole_and_the_main_threads_record_is_left_alone(self):
        self.root_session()
        path = self.state / "topics" / MAIN / "main-snapshot.json"
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
            self.assertIn("并行的一路" if options else "Recent conversation:", prompt)

    def test_a_track_on_its_own_session_resumes_it_and_forks_nothing(self):
        self.root_session()
        (ctx, prompt), _ = self.turn(self.track_job(), self.harness(), session_id="22222222-2222-4222-8222-222222222222")
        self.assertNotIn("fork_session", ctx.options)
        self.assertTrue(prompt.endswith("RAW"))
        self.assertIn("并行的一路", prompt)

    def test_the_main_threads_own_turn_is_what_it_was(self):
        job = self.track_job(slot=0)
        job.topic_id = MAIN
        (ctx, prompt), compaction = self.turn(job, self.harness())
        self.assertNotIn("并行", prompt)
        compaction.request.assert_called_once()

    def test_a_track_never_asks_for_a_compaction(self):
        compaction = mock.Mock()
        job = self.track_job()
        with mock.patch.object(main_thread, "traits_of", return_value=main_thread.MAIN):
            session.compact_main_thread(compaction, self.ctx, job)
        compaction.set_requested.assert_not_called()
        compaction.supported.assert_not_called()


class ReleaseTest(TracksCase):
    def pointer(self, topic):
        path = self.state / "topics" / topic / self.claude.harness.spec.session_filename
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def test_the_session_pointer_goes_when_the_turn_ends_but_not_the_main_threads(self):
        track, root = self.pointer(SLOT1), self.pointer(MAIN)
        track.write_text("00000000-0000-4000-8000-000000000003")
        root.write_text("00000000-0000-4000-8000-000000000004")
        ctx = SimpleNamespace(state_dir=self.state)
        log = mock.Mock()
        for topic in (MAIN, SLOT1):
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
                mock.patch.object(worker.main_tracks, "release_pointer", side_effect=lambda *a: calls.append(a)):
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


if __name__ == "__main__":
    unittest.main()
