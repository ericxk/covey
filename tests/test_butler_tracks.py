"""多路并行的底座 (core/butler_tracks.py, docs/butler.md 同时做几件事, COVEY_BUTLER_TRACKS; B1): the slots' topic ids and the
switches, the family predicate and the root one with the call sites that follow it, the family's live tasks, a slot's
topic.json, and the scheduler's order for the slots. With the switch off every answer is the one before the tracks
existed."""
from __future__ import annotations

import hashlib
import json
import os
import queue
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from support.butler import switch_on
from support.records import state_dir, write_job
from support.world import UnifiedCase
from test_butler_tools import make_env, butler_job
from covey.app import butler_tools, butler_topics
from covey.app.butler_search import turns_of
from covey.app.butler_tools import REFUSED, ButlerTools
from covey.app.runtime import JobRegistry, job_stage
from covey.core import board_read, butler_requests, butler, butler_tracks
from covey.core.butler import is_butler_root, is_butler_topic, butler_topic_id
from covey.core.butler_tracks import SLOTS, track_of, track_topic_id
from covey.core.scheduler import WorkspaceScheduler, topic_key
from covey.core.topics import remember_topic_message

CHAT = "oc_dm"
OTHER_CHAT = "oc_other"
CLEAN = {butler_tracks.ENV: "", "LARK_BRIDGE_MAIN_TRACKS": "", butler_tracks.MAX_ENV: "", "LARK_BRIDGE_MAIN_TRACKS_MAX": ""}


class Env(unittest.TestCase):
    """The tracks' two variables cleared, then set by ``tracks`` (switch) and ``more`` until the test ends."""

    def setUp(self):
        patch = mock.patch.dict(os.environ, CLEAN)
        patch.start()
        self.addCleanup(patch.stop)

    def tracks(self, value="1", **more):
        patch = mock.patch.dict(os.environ, {butler_tracks.ENV: value, **more})
        patch.start()
        self.addCleanup(patch.stop)


class SlotIdsTest(Env):
    def test_every_slot_has_a_stable_32_hex_id_and_no_two_meet(self):
        ids = {(chat, slot): track_topic_id(chat, slot) for chat in (CHAT, OTHER_CHAT, "") for slot in range(SLOTS + 1)}
        self.assertEqual(len(set(ids.values())), len(ids))
        for topic_id in ids.values():
            self.assertRegex(topic_id, r"^[0-9a-f]{32}$")  # app/topics.py Topics.target takes [0-9a-f]{16,128}.
        self.assertEqual(track_topic_id(CHAT, 2), track_topic_id(CHAT, 2))
        self.assertEqual(track_topic_id(CHAT, 2), hashlib.sha256(f"main-thread-track\0{2}\0{CHAT}".encode()).hexdigest()[:32])

    def test_slot_0_is_the_butlers_own_topic_as_it_always_was(self):
        for chat in (CHAT, OTHER_CHAT, ""):
            self.assertEqual(track_topic_id(chat, 0), butler_topic_id(chat))
            self.assertEqual(butler_topic_id(chat), hashlib.sha256(f"main-thread\0{chat}".encode()).hexdigest()[:32])

    def test_a_slot_outside_0_to_4_is_refused(self):
        for slot in (-1, 5, "1", 1.0, None, True):
            with self.assertRaises(ValueError, msg=repr(slot)):
                track_topic_id(CHAT, slot)

    def test_a_slot_is_no_message_alias_and_matches_no_topic_of_a_message(self):
        # A topic made of a message (core/topics.py _key) hashes other input: the ids cannot be a message's.
        self.assertNotIn(track_topic_id(CHAT, 1), {hashlib.sha256(f"{CHAT}\0om_{n}".encode()).hexdigest()[:32] for n in range(50)})


class SwitchTest(Env):
    def test_off_unless_exactly_1(self):
        self.assertFalse(butler_tracks.enabled())
        for value in ("", "0", "true", "yes", " ", "2"):
            with mock.patch.dict(os.environ, {butler_tracks.ENV: value}):
                self.assertFalse(butler_tracks.enabled(), value)
        for value in ("1", " 1 "):
            with mock.patch.dict(os.environ, {butler_tracks.ENV: value}):
                self.assertTrue(butler_tracks.enabled(), value)

    def test_the_old_spelling_works_and_the_new_one_wins(self):
        with mock.patch.dict(os.environ, {"LARK_BRIDGE_MAIN_TRACKS": "1"}):
            del os.environ[butler_tracks.ENV]  # The cleared new spelling would win; patch.dict puts it back afterwards.
            self.assertTrue(butler_tracks.enabled())
        with mock.patch.dict(os.environ, {"LARK_BRIDGE_MAIN_TRACKS": "1", butler_tracks.ENV: "0"}):
            self.assertFalse(butler_tracks.enabled())

    def test_the_limit_is_3_unset_or_unreadable_and_held_to_2_to_4_otherwise(self):
        self.assertEqual(butler_tracks.limit(), 3)
        for value, wanted in (("", 3), ("x", 3), ("2.5", 3), ("3", 3), ("2", 2), ("4", 4), ("1", 2), ("0", 2), ("-3", 2),
                              ("9", 4), (" 4 ", 4)):
            with mock.patch.dict(os.environ, {butler_tracks.MAX_ENV: value}):
                self.assertEqual(butler_tracks.limit(), wanted, value)
        with mock.patch.dict(os.environ, {"LARK_BRIDGE_MAIN_TRACKS_MAX": "4"}):
            del os.environ[butler_tracks.MAX_ENV]
            self.assertEqual(butler_tracks.limit(), 4)


class PredicateTest(Env):
    def setUp(self):
        super().setUp()
        switch_on(self, "1")

    def test_off_only_slot_0_is_the_butler_and_nothing_else_changes(self):
        self.assertTrue(is_butler_topic(butler_topic_id(CHAT), CHAT))
        self.assertEqual(track_of(butler_topic_id(CHAT), CHAT), 0)
        for slot in range(1, SLOTS + 1):
            self.assertFalse(is_butler_topic(track_topic_id(CHAT, slot), CHAT))
            self.assertIsNone(track_of(track_topic_id(CHAT, slot), CHAT))
            self.assertIs(butler.traits(track_topic_id(CHAT, slot), CHAT), butler.TOPIC)
        self.assertIs(butler.traits(butler_topic_id(CHAT), CHAT), butler.BUTLER)
        self.assertEqual(butler_tracks.family_ids(CHAT), (butler_topic_id(CHAT),))

    def test_the_predicate_answers_bools_for_odd_input_as_before(self):
        for value in (None, "", 0, 5, b"x", "ab" * 16):
            self.assertIs(is_butler_topic(value, CHAT), False, value)
            self.assertIs(is_butler_root(value, CHAT), False, value)
        self.assertIs(is_butler_topic(butler_topic_id(""), None), True)  # A missing chat is the empty one, as before.
        self.assertIs(is_butler_topic(butler_topic_id(CHAT), OTHER_CHAT), False)  # Another chat's butler is no one's here.

    def test_on_slots_0_to_4_are_the_family_and_only_slot_0_is_the_root(self):
        self.tracks()
        for slot in range(SLOTS + 1):
            topic = track_topic_id(CHAT, slot)
            self.assertTrue(is_butler_topic(topic, CHAT), slot)
            self.assertEqual(track_of(topic, CHAT), slot)
            self.assertIs(is_butler_root(topic, CHAT), slot == 0)
            self.assertIs(butler.traits(topic, CHAT), butler.BUTLER)
            self.assertFalse(is_butler_topic(topic, OTHER_CHAT))  # Another chat's slot is a stranger.
        self.assertEqual(butler_tracks.family_ids(CHAT), tuple(track_topic_id(CHAT, slot) for slot in range(SLOTS + 1)))
        self.assertFalse(is_butler_topic("ab" * 16, CHAT))
        self.assertIsNone(track_of("ab" * 16, CHAT))

    def test_the_root_predicate_does_not_depend_on_the_switch(self):
        for value in ("", "1"):
            with mock.patch.dict(os.environ, {butler_tracks.ENV: value}):
                self.assertTrue(is_butler_root(butler_topic_id(CHAT), CHAT))
                self.assertFalse(is_butler_root(track_topic_id(CHAT, 1), CHAT))

    def test_a_slot_is_listed_nowhere_with_the_butler_off_it_reads_as_one_whose_session_is_gone(self):
        self.tracks()
        with mock.patch.dict(os.environ, {butler.ENV: "0"}):
            self.assertIs(butler.traits(track_topic_id(CHAT, 3), CHAT), butler.BUTLER_OFF)


class CallSitesTest(Env):
    """The call sites that follow the family (B-SPEC 1.2): off, the answer is slot 0's alone; on, a slot counts too."""

    def setUp(self):
        super().setUp()
        switch_on(self, "1")
        self.state = state_dir(self)

    def jobs(self):
        for key, slot in (("root", 0), ("t1", 1), ("t2", 2), ("plain", None)):
            topic = track_topic_id(CHAT, slot) if slot is not None else "ab" * 16
            write_job(self.state, key, chat_id=CHAT, topic_id=topic, job_id=len(key), status="done", user_text=key)

    def test_the_board_reads_every_topic_but_the_butlers(self):
        self.jobs()
        self.assertEqual(set(board_read.topic_jobs(self.state, CHAT)),
                         {"ab" * 16, track_topic_id(CHAT, 1), track_topic_id(CHAT, 2)})
        self.tracks()
        self.assertEqual(set(board_read.topic_jobs(self.state, CHAT)), {"ab" * 16})
        self.assertFalse(board_read.listed(track_topic_id(CHAT, 4), CHAT))

    def test_topics_search_leaves_the_slots_out_as_the_butler(self):
        self.jobs()
        env = SimpleNamespace(state_dir=self.state)
        self.assertEqual({row["topic_id"] for row in turns_of(env, CHAT)},
                         {"ab" * 16, track_topic_id(CHAT, 1), track_topic_id(CHAT, 2)})
        self.tracks()
        self.assertEqual({row["topic_id"] for row in turns_of(env, CHAT)}, {"ab" * 16})

    def test_the_tools_serve_a_running_job_of_any_slot_only_with_the_switch(self):
        handler = mock.Mock(return_value=butler_tools.reply(True, "好"))
        tools = ButlerTools(make_env(self.state), {"topics_list": handler})
        write_job(self.state, "slot-job", chat_id=CHAT, topic_id=track_topic_id(CHAT, 2), job_id=7, status="running")
        butler_job(self.state)

        def ask(key):
            request_id = f"r{key}"
            butler_requests._write(butler_requests.folder(self.state) / f"{request_id}.json",
                                 {"id": request_id, "tool": "topics_list", "args": {}, "job_key": key,
                                  "created_at": time.time()})
            tools.tick()
            return json.loads((butler_requests.folder(self.state) / f"{request_id}.answer.json").read_text())["text"]
        self.assertEqual(ask("slot-job"), REFUSED)
        self.assertEqual(ask("main-1"), "好")
        self.tracks()
        write_job(self.state, "slot-job-2", chat_id=CHAT, topic_id=track_topic_id(CHAT, 3), job_id=8, status="running")
        self.assertEqual(ask("slot-job-2"), "好")
        write_job(self.state, "stranger", chat_id=CHAT, topic_id="cd" * 16, job_id=9, status="running")
        self.assertEqual(ask("stranger"), REFUSED)

    def test_a_slot_is_no_topic_to_read_or_fork_with_the_switch_on(self):
        env = SimpleNamespace(state_dir=self.state)
        slot = track_topic_id(CHAT, 1)
        with mock.patch.object(butler_tools, "topic_data", return_value={"chat_id": CHAT}):
            self.assertEqual(butler_tools.resolve(env, CHAT, slot), (slot, []))  # By id, off: a topic nothing ran in.
            self.tracks()
            for ref in (slot, butler_topic_id(CHAT)):
                with self.assertRaises(ValueError):
                    butler_tools.resolve(env, CHAT, ref)

    def test_an_area_is_free_on_a_question_kept_by_the_butler_of_any_slot(self):
        env = SimpleNamespace(state_dir=str(self.state))
        write_job(self.state, "main-1", chat_id=CHAT, topic_id=butler_topic_id(CHAT), job_id=1, status="running",
                  message_id="om_ask")
        request = SimpleNamespace(job_key="main-1")
        with mock.patch.object(butler_topics.butler, "enabled", return_value=True):
            self.assertEqual(butler_topics.place(env, request, CHAT), ("om_ask", False))  # Unknown, or slot 0's.
            remember_topic_message(self.state, track_topic_id(CHAT, 2), CHAT, "om_ask")
            self.assertEqual(butler_topics.place(env, request, CHAT), ("", True))  # Slot 2's message, no family yet.
            self.tracks()
            self.assertEqual(butler_topics.place(env, request, CHAT), ("om_ask", False))


class FamilyLiveTest(Env):
    def job(self, job_id, slot, *, cancelled=False, finalized=False, control=None, started=False, chat=CHAT):
        topic = track_topic_id(chat, slot) if slot is not None else "ab" * 16
        return SimpleNamespace(job_id=job_id, message_id=f"om_{job_id}", chat_id=chat, topic_id=topic,
                               cancel_event=SimpleNamespace(is_set=lambda: cancelled), answer_finalized=finalized,
                               backend_control=control, execution_started=started)

    def registry(self, *jobs):
        registry = JobRegistry()
        for job in jobs:
            registry.register(job)
        return registry

    def test_stages_are_the_steering_ones(self):
        waiting = SimpleNamespace(pause_for_question="q")
        answering = SimpleNamespace(pause_for_question="", awaiting_answer=True)
        self.assertEqual(job_stage(self.job(1, 0)), "queued")
        self.assertEqual(job_stage(self.job(1, 0, started=True)), "starting")
        self.assertEqual(job_stage(self.job(1, 0, control=SimpleNamespace())), "running")
        self.assertEqual(job_stage(self.job(1, 0, control=waiting)), "paused")
        self.assertEqual(job_stage(self.job(1, 0, control=answering)), "paused")
        self.assertEqual(job_stage(self.job(1, 0, cancelled=True)), "ended")
        self.assertEqual(job_stage(self.job(1, 0, finalized=True)), "ended")

    def test_off_only_slot_0s_live_tasks_count(self):
        registry = self.registry(self.job(1, 0), self.job(2, 1, control=SimpleNamespace()))
        self.assertEqual([job.job_id for job in registry.family_live(CHAT)], [1])

    def test_on_the_family_but_neither_ended_nor_waiting_tasks_nor_strangers_count_oldest_first(self):
        self.tracks()
        running, waiting = SimpleNamespace(), SimpleNamespace(pause_for_question="q")
        registry = self.registry(
            self.job(7, 2, control=running), self.job(3, 0, started=True), self.job(5, 1),
            self.job(4, 3, control=waiting), self.job(6, 4, cancelled=True), self.job(8, 1, finalized=True),
            self.job(9, None, control=running), self.job(10, 1, chat=OTHER_CHAT, control=running))
        self.assertEqual([job.job_id for job in registry.family_live(CHAT)], [3, 5, 7])
        self.assertEqual([job.job_id for job in registry.family_live(OTHER_CHAT)], [10])
        self.assertEqual(registry.family_live("oc_none"), [])

    def test_it_reads_the_registry_only(self):
        self.tracks()
        registry = self.registry(self.job(1, 1, control=SimpleNamespace()))
        with mock.patch("builtins.open", side_effect=AssertionError("a file read")), \
                mock.patch("socket.socket", side_effect=AssertionError("a call")):
            self.assertEqual(len(registry.family_live(CHAT)), 1)


class TrackTopicFileTest(UnifiedCase):
    """Butler.keep_track_topic: a slot's topic.json is slot 0's, with its own id and ``track``."""

    def setUp(self):
        switch_on(self, "1")
        patch = mock.patch.dict(os.environ, CLEAN)
        patch.start()
        self.addCleanup(patch.stop)
        super().setUp()
        self.app.runtime.state_dir = self.ctx.state_dir
        self.state = self.ctx.state_dir
        self.claude = self.app.apps["claude"]
        self.mt = self.app.butler

    def read(self, topic_id):
        return json.loads((self.state / "topics" / topic_id / "topic.json").read_text())

    def keep(self, slot):
        return self.mt.keep_track_topic(self.ctx, self.claude, CHAT, slot)

    def settings(self, **changes):
        from covey.core import butler_settings
        butler_settings.update(self.state, **changes)

    def test_it_copies_slot_0s_settings_and_adds_its_track(self):
        self.settings(model="claude-opus-5-5", effort="high", redaction=True)
        topic = self.keep(2)
        self.assertEqual(topic, track_topic_id(CHAT, 2))
        data, root = self.read(topic), self.read(butler_topic_id(CHAT))
        self.assertEqual(data["track"], 2)
        self.assertEqual(data["topic_id"], topic)
        self.assertNotIn("track", root)  # Slot 0 stays as it was.
        self.assertEqual(root["topic_id"], butler_topic_id(CHAT))
        self.assertEqual({k: v for k, v in data.items() if k not in ("topic_id", "track")},
                         {k: v for k, v in root.items() if k != "topic_id"})
        for key in ("model", "effort", "workdir", "workspace_name", "harness_id", "chat_id", "binding_revision"):
            self.assertEqual(data[key], root[key], key)
        self.assertEqual((data["model"], data["effort"], data["redaction"], data["main_thread"]),
                         ("claude-opus-5-5", "high", True, True))
        self.assertEqual((data["root_id"], data["thread_id"]), ("", ""))
        self.assertNotIn("topic_number", data)

    def test_the_slot_is_a_butler_topic_only_with_the_tracks_on(self):
        topic = self.keep(1)
        self.assertFalse(is_butler_topic(topic, CHAT))
        with mock.patch.dict(os.environ, {butler_tracks.ENV: "1"}):
            self.assertTrue(is_butler_topic(topic, CHAT))
            self.assertEqual(butler.traits_of(self.read(topic)).name, "butler")  # topic.json says so, with chat_id.

    def test_each_slot_has_its_own_file_and_a_second_call_writes_nothing(self):
        first, second = self.keep(1), self.keep(3)
        self.assertNotEqual(first, second)
        path = self.state / "topics" / first / "topic.json"
        stamp = path.stat().st_mtime_ns
        self.keep(1)
        self.assertEqual(path.stat().st_mtime_ns, stamp)
        self.assertEqual([self.read(first)["track"], self.read(second)["track"]], [1, 3])

    def test_new_settings_reach_the_slot_on_the_next_call(self):
        topic = self.keep(1)
        self.assertEqual(self.read(topic)["model"], "")
        self.settings(model="claude-opus-5-5", effort="max")
        self.keep(1)
        self.assertEqual((self.read(topic)["model"], self.read(topic)["effort"]), ("claude-opus-5-5", "max"))
        self.settings(redaction=True)
        self.keep(1)
        self.assertIs(self.read(topic)["redaction"], True)
        self.settings(redaction=False)
        self.keep(1)
        self.assertNotIn("redaction", self.read(topic))

    def test_another_workspace_waits_until_nothing_runs_in_the_slot_and_starts_a_new_session(self):
        from covey.core.workspaces import add_workspace
        topic = self.keep(1)
        pointer = self.state / "topics" / topic / self.claude.spec.session_filename
        pointer.write_text("11111111-1111-4111-8111-111111111111\n")
        other = self.root / "other"
        other.mkdir()
        add_workspace(self.state, self.ctx.default_workdir, str(other))
        self.settings(workspace="other")
        old = self.read(topic)["workdir"]
        job = SimpleNamespace(job_id=1, message_id="om_x", topic_id=topic, chat_id=CHAT, execution_started=True)
        self.app.runtime.jobs.register(job)
        self.keep(1)
        self.assertEqual(self.read(topic)["workdir"], old)  # A job runs in the slot: not yet.
        self.assertTrue(pointer.exists())
        self.app.runtime.jobs.unregister(job)
        self.keep(1)
        data = self.read(topic)
        self.assertEqual((data["workspace_name"], data["workdir"]), ("other", str(other.resolve())))
        self.assertEqual(data["binding_revision"], 2)
        self.assertIsNone(self.claude.sessions.read_session_id(pointer))

    def test_a_slot_outside_1_to_4_is_refused_and_writes_nothing(self):
        for slot in (0, 5, -1, "1", None):
            with self.assertRaises(ValueError, msg=repr(slot)):
                self.keep(slot)
        self.assertFalse((self.state / "topics").exists())

    def test_keep_topic_is_unchanged_for_slot_0(self):
        self.mt.keep_topic(self.ctx, self.claude, CHAT)
        data = self.read(butler_topic_id(CHAT))
        self.assertEqual(data["topic_id"], butler_topic_id(CHAT))
        self.assertNotIn("track", data)
        self.assertEqual((data["main_thread"], data["binding_revision"], data["root_id"]), (True, 1, ""))


class SchedulerSlotsTest(unittest.TestCase):
    """The scheduler is not changed: a topic key is (chat, topic), so two slots run together and one slot in order."""

    def job(self, name, slot):
        return SimpleNamespace(message_id=name, chat_id=CHAT, topic_id=track_topic_id(CHAT, slot),
                               cancel_event=threading.Event(), start_held_until=0)

    def test_the_key_is_the_slots_topic(self):
        self.assertEqual(topic_key(self.job("a", 1)), (CHAT, track_topic_id(CHAT, 1)))
        self.assertNotEqual(topic_key(self.job("a", 1)), topic_key(self.job("a", 2)))

    def test_different_slots_run_together_and_one_slot_in_order(self):
        shutdown, incoming = threading.Event(), queue.Queue()
        release, started, order = threading.Event(), {}, []
        events = {name: threading.Event() for name in ("s1a", "s2a", "s1b", "s1c")}
        lock = threading.Lock()
        self.addCleanup(release.set)
        self.addCleanup(shutdown.set)

        def process(job):
            with lock:
                started[job.message_id] = time.time()
                order.append(job.message_id)
            events[job.message_id].set()
            release.wait(3)
        for name, slot in (("s1a", 1), ("s1b", 1), ("s2a", 2), ("s1c", 1)):
            incoming.put(self.job(name, slot))
        scheduler = WorkspaceScheduler(shutdown, incoming, process, lambda _: None)
        thread = threading.Thread(target=scheduler.run, daemon=True)
        thread.start()
        try:
            self.assertTrue(events["s1a"].wait(2))
            self.assertTrue(events["s2a"].wait(2))  # Another slot started although slot 1's first is still running.
            time.sleep(0.4)
            self.assertFalse(events["s1b"].is_set())  # The same slot waits.
            self.assertFalse(events["s1c"].is_set())
            release.set()
            self.assertTrue(events["s1b"].wait(2))
            self.assertTrue(events["s1c"].wait(2))
        finally:
            release.set()
            shutdown.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual([name for name in order if name.startswith("s1")], ["s1a", "s1b", "s1c"])

    def test_the_root_and_a_slot_run_together_too(self):
        shutdown, incoming = threading.Event(), queue.Queue()
        release, both = threading.Event(), [threading.Event(), threading.Event()]
        self.addCleanup(release.set)
        self.addCleanup(shutdown.set)

        def process(job):
            both[0 if job.message_id == "root" else 1].set()
            release.wait(3)
        for name, slot in (("root", 0), ("track", 4)):
            incoming.put(self.job(name, slot))
        scheduler = WorkspaceScheduler(shutdown, incoming, process, lambda _: None)
        thread = threading.Thread(target=scheduler.run, daemon=True)
        thread.start()
        try:
            self.assertTrue(both[0].wait(2) and both[1].wait(2))
        finally:
            release.set()
            shutdown.set()
            thread.join(3)


if __name__ == "__main__":
    unittest.main()
