"""专项's foundation (docs/architecture.md 专项, core/projects.py): the lead topic's id, the record and its state machine, the
topic.json ``project`` field and its cached read, the predicates and Traits row of the lead (core/butler.py), the
two placement roles (core/placement.py), a project as one cell of the board (core/board_read.py) and the maintenance rule;
and that with COVEY_PROJECTS unset none of it changes anything."""
from __future__ import annotations

import json
import os
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from support import enter_context
from support.records import state_dir, write_job, write_topic

from covey.app import answer_cards, butler_tools, pins, policy_retry
from covey.app.butler_env import ButlerEnv
from covey.app.butler_tools import ButlerTools, reply
from covey.core import board_read, butler, butler_tracks, placement, projects, topics
from covey.core import butler_settings as settings
from covey.core.maintenance import maintain
from covey.core.state import DAY, write_json

CHAT = "oc_dm"
ON = {projects.ENV: "1"}
PID, OTHER = "a1b2c3d4e5f6", "0f9e8d7c6b5a"
LEAD = projects.lead_topic_id(CHAT, PID)
THREAD = "ab" * 16


class Case(unittest.TestCase):
    """A state directory the butler is configured on (mode agent) and COVEY_PROJECTS set as ``flag`` says."""

    flag = "1"

    def setUp(self):
        self.state = state_dir(self)
        previous = butler.configure_state(self.state)
        self.addCleanup(butler.configure_state, previous)
        env = {name: value for name, value in os.environ.items() if not name.startswith(("COVEY_", "LARK_BRIDGE_"))}
        enter_context(self, mock.patch.dict(os.environ, env, clear=True))
        if self.flag:
            os.environ[projects.ENV] = self.flag
        self.mode("agent")

    def mode(self, mode):
        write_json(self.state / settings.FILE, {"mode": mode})
        butler.forget_mode()

    def topic(self, topic_id, **fields):
        return write_topic(self.state, topic_id=topic_id, chat_id=CHAT, harness_id="claude", **fields)

    def lead(self, pid=PID, chat=CHAT):
        topic_id = projects.lead_topic_id(chat, pid)
        write_topic(self.state, topic_id=topic_id, chat_id=chat, harness_id="claude",
                    project=projects.project_field(pid, projects.LEAD))
        return topic_id

    def thread(self, topic_id=THREAD, pid=PID):
        write_topic(self.state, topic_id=topic_id, chat_id=CHAT, harness_id="claude",
                    project=projects.project_field(pid, projects.MEMBER))
        return topic_id


class IdTest(unittest.TestCase):
    def test_the_lead_topic_id_is_hex_stable_and_per_chat_and_project(self):
        one = projects.lead_topic_id("oc_a", PID)
        self.assertRegex(one, "^[0-9a-f]{32}$")
        self.assertEqual(one, projects.lead_topic_id("oc_a", PID))
        self.assertNotEqual(one, projects.lead_topic_id("oc_b", PID))
        self.assertNotEqual(one, projects.lead_topic_id("oc_a", OTHER))

    def test_it_meets_no_id_of_the_butlers_family(self):
        family = set(butler_tracks._slots("oc_a"))
        ids = {projects.lead_topic_id("oc_a", f"{n:012x}") for n in range(200)}
        self.assertEqual(len(ids), 200)
        self.assertTrue(ids.isdisjoint(family))

    def test_ids_and_paths(self):
        self.assertTrue(projects.valid_id(projects.new_id()))
        self.assertNotEqual(projects.new_id(), projects.new_id())
        for bad in ("", "short", "ABCDEF1234", "../x" * 3, "a/b" * 3, None, 5, "a" * 33):
            self.assertFalse(projects.valid_id(bad), bad)
            with self.assertRaises(ValueError):
                projects.record_path("/s", bad)
        self.assertEqual(projects.record_path("/s", PID), Path("/s/projects") / f"{PID}.json")

    def test_topic_json_is_where_core_topics_says(self):
        self.assertEqual(projects.topic_file("/s", "ab" * 16), topics.topic_file(Path("/s"), "ab" * 16))


class TransitionTest(Case):
    def record(self, **fields):
        record, _ = projects.create(self.state, {**projects.new_record(chat_id=CHAT, name="n", project_id=PID), **fields})
        return record

    def test_the_table(self):
        self.assertEqual(projects.PROJECT_TRANSITIONS, {
            "active": {"paused", "done", "cancelled"}, "paused": {"active", "done", "cancelled"},
            "done": set(), "cancelled": set()})
        self.assertEqual(projects.STATES, {"active", "paused", "done", "cancelled"})

    def test_a_final_state_never_moves_and_nothing_is_written_when_it_is_refused(self):
        self.record()
        projects.set_state(self.state, PID, projects.DONE, "finished")
        path = projects.record_path(self.state, PID)
        before = path.read_text()
        for to in ("active", "paused", "cancelled", "bogus"):
            with self.assertRaises(ValueError):
                projects.set_state(self.state, PID, to)
        self.assertEqual(path.read_text(), before)

    def test_pause_and_resume(self):
        self.record()
        self.assertEqual(projects.set_state(self.state, PID, "paused", "stalled")["state_reason"], "stalled")
        self.assertEqual(projects.set_state(self.state, PID, "active")["state"], "active")

    def test_a_change_that_moves_the_state_goes_through_the_table(self):
        self.record()
        projects.set_state(self.state, PID, "cancelled")
        with self.assertRaises(ValueError):
            projects.update(self.state, PID, lambda record: record.update(state="active", name="x"))
        self.assertEqual(projects.read_record(self.state, PID)["name"], "n")

    def test_transition_edits_a_copy_nobody_holds(self):
        record = {"state": "active"}
        self.assertIs(projects.transition(record, "done"), record)
        with self.assertRaises(ValueError):
            projects.transition(record, "active")


class RecordTest(Case):
    def test_a_new_record_is_active_and_carries_the_fields_of_phase_3_as_defaults(self):
        record = projects.new_record(chat_id=CHAT, name="  整理  专项\n", goal="g", project_id=PID, now=5.0,
                                     origin={"kind": "upgrade"})
        self.assertEqual((record["state"], record["name"], record["lead_topic_id"], record["threads"], record["origin"]),
                         ("active", "整理 专项", LEAD, [], {"kind": "upgrade"}))
        self.assertEqual((record["created_at"], record["updated_at"]), (5.0, 5.0))
        self.assertEqual((record["direction"], record["leader"], record["claims"], record["agreements"], record["place"]),
                         (None, {"kind": "temp", "profile": None}, [], [], {"chat_id": CHAT}))
        self.assertEqual(record["card"], {"card_id": "", "message_id": "", "sequence": 0})
        self.assertEqual(len(projects.new_record(chat_id=CHAT, name="x" * 99)["name"]), projects.NAME_CHARS)
        with self.assertRaises(ValueError):
            projects.new_record(chat_id=CHAT, name="n", project_id="NO")

    def test_create_writes_once_and_a_second_call_finds_the_first(self):
        first, wrote = projects.create(self.state, projects.new_record(chat_id=CHAT, name="one", project_id=PID))
        again, wrote_again = projects.create(self.state, projects.new_record(chat_id=CHAT, name="two", project_id=PID))
        self.assertEqual((wrote, wrote_again, again["name"]), (True, False, "one"))
        self.assertEqual(projects.read_record(self.state, PID), first)
        self.assertEqual(oct(projects.record_path(self.state, PID).stat().st_mode & 0o777), "0o600")

    def test_read_gives_an_empty_record_for_none_a_bad_id_or_a_file_that_is_no_object(self):
        self.assertEqual(projects.read_record(self.state, PID), {})
        self.assertEqual(projects.read_record(self.state, "../x/../y"), {})
        folder = projects.folder(self.state)
        folder.mkdir(parents=True)
        (folder / f"{PID}.json").write_text("[1]")
        self.assertEqual(projects.read_record(self.state, PID), {})
        with self.assertRaises(KeyError):
            projects.update(self.state, PID, lambda record: None)

    def test_update_stamps_updated_at_only_when_something_changed(self):
        projects.create(self.state, projects.new_record(chat_id=CHAT, name="n", project_id=PID, now=1.0))
        same = projects.update(self.state, PID, lambda record: None)
        self.assertEqual(same["updated_at"], 1.0)
        changed = projects.update(self.state, PID, lambda record: record.update(goal="new"))
        self.assertGreater(changed["updated_at"], 1.0)
        self.assertEqual(projects.read_record(self.state, PID)["goal"], "new")

    def test_add_thread_keeps_each_once_in_order_and_takes_topic_ids_only(self):
        projects.create(self.state, projects.new_record(chat_id=CHAT, name="n", project_id=PID))
        for topic in (THREAD, "cd" * 16, THREAD):
            record = projects.add_member(self.state, PID, topic)
        self.assertEqual(record["threads"], [THREAD, "cd" * 16])
        for bad in ("", "x", "../" + "a" * 20, None):
            with self.assertRaises(ValueError):
                projects.add_member(self.state, PID, bad)

    def test_records_of_a_chat_oldest_first_and_without_what_is_not_one(self):
        for n, (pid, chat) in enumerate(((PID, CHAT), (OTHER, "oc_b"), ("0123456789ab", CHAT))):
            projects.create(self.state, projects.new_record(chat_id=chat, name=pid, project_id=pid, now=100 - n))
        folder = projects.folder(self.state)
        (folder / "stray.json").write_text("{}")
        (folder / f"{'f' * 12}.json").write_text(json.dumps({"id": "somebodyelse1"}))
        (folder / ".locks").mkdir(exist_ok=True)
        self.assertEqual([r["id"] for r in projects.records(self.state, CHAT)], ["0123456789ab", PID])
        self.assertEqual(len(projects.records(self.state)), 3)
        self.assertEqual(projects.records(self.state / "none"), [])

    def test_concurrent_writers_lose_no_thread(self):
        projects.create(self.state, projects.new_record(chat_id=CHAT, name="n", project_id=PID))
        topics_ = [f"{n:032x}" for n in range(24)]
        errors = []

        def add(topic):
            try:
                projects.add_member(self.state, PID, topic)
                projects.update(self.state, PID, lambda record: record["steps"].__setitem__(topic[-4:], True))
            except Exception as exc:  # noqa: BLE001 - reported below
                errors.append(exc)

        workers = [threading.Thread(target=add, args=(topic,)) for topic in topics_]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(20)
        record = projects.read_record(self.state, PID)
        self.assertEqual((errors, sorted(record["threads"])), ([], sorted(topics_)))
        self.assertEqual(len(record["steps"]), 24)

    def test_the_lock_is_the_shared_record_lock_so_another_process_waits_too(self):
        from covey.core import locks

        with mock.patch.object(locks, "shared_file_lock", wraps=locks.shared_file_lock) as shared:
            projects.create(self.state, projects.new_record(chat_id=CHAT, name="n", project_id=PID))
        self.assertEqual(Path(shared.call_args[0][0]), projects.record_path(self.state, PID))


class TopicFieldTest(Case):
    def test_set_keeps_the_rest_of_topic_json_and_says_whether_it_changed(self):
        self.topic(THREAD, topic_number=7, workdir="/w", origin={"title": "t"})
        self.assertTrue(topics.set_topic_project(self.state, THREAD, PID, "thread"))
        self.assertFalse(topics.set_topic_project(self.state, THREAD, PID, "thread"))
        data = json.loads(topics.topic_file(self.state, THREAD).read_text())
        self.assertEqual((data["project"], data["topic_number"], data["workdir"], data["origin"]),
                         ({"id": PID, "role": "thread"}, 7, "/w", {"title": "t"}))
        self.assertEqual(topics.topic_project(self.state, THREAD), projects.TopicProject(PID, "thread", CHAT))
        self.assertTrue(topics.set_topic_project(self.state, THREAD, PID, "lead"))
        self.assertEqual(topics.topic_project(self.state, THREAD).role, "lead")

    def test_clear_takes_the_field_out(self):
        self.thread()
        self.assertTrue(topics.clear_topic_project(self.state, THREAD))
        self.assertFalse(topics.clear_topic_project(self.state, THREAD))
        self.assertIsNone(topics.topic_project(self.state, THREAD))
        self.assertNotIn("project", json.loads(topics.topic_file(self.state, THREAD).read_text()))

    def test_a_bad_field_or_a_missing_topic_is_refused(self):
        self.topic(THREAD)
        for pid, role in (("NO", "thread"), (PID, "boss"), (PID, "")):
            with self.assertRaises(ValueError):
                topics.set_topic_project(self.state, THREAD, pid, role)
        with self.assertRaises(OSError):
            topics.set_topic_project(self.state, "ee" * 16, PID, "thread")
        self.assertNotIn("project", json.loads(topics.topic_file(self.state, THREAD).read_text()))

    def test_a_field_that_is_not_id_and_role_reads_as_none(self):
        for field in ("lead", {"id": PID}, {"id": "NO", "role": "lead"}, {"id": PID, "role": "boss"}, [PID], None):
            self.topic(THREAD, project=field)
            self.assertIsNone(projects.topic_project(self.state, THREAD), field)
        self.assertIsNone(projects.topic_project(self.state, "ee" * 16))
        self.assertIsNone(projects.topic_project(self.state, "../x"))
        self.assertIsNone(projects.topic_project(None, THREAD))

    def test_other_topic_writers_keep_the_field(self):
        # bind_topic and topic_number rewrite topic.json whole from what they read.
        self.thread()
        data = json.loads(topics.topic_file(self.state, THREAD).read_text())
        data.pop("topic_number", None)
        topics.topic_file(self.state, THREAD).write_text(json.dumps({**data, "chat_id": CHAT}))
        topics.topic_number(self.state, THREAD)
        self.assertEqual(topics.topic_project(self.state, THREAD).role, "thread")


class PredicateTest(Case):
    def test_the_lead_is_known_by_its_field_and_its_id(self):
        lead = self.lead()
        self.assertTrue(projects.is_lead_topic(self.state, lead, CHAT))
        self.assertTrue(projects.is_lead_topic(self.state, lead))  # The chat from the file.
        self.assertFalse(projects.is_lead_topic(self.state, lead, "oc_other"))
        self.assertFalse(projects.is_member_topic(self.state, lead))
        self.thread()
        self.assertTrue(projects.is_member_topic(self.state, THREAD))
        self.assertFalse(projects.is_lead_topic(self.state, THREAD, CHAT))

    def test_a_topic_json_copied_to_another_topic_is_no_lead(self):
        write_topic(self.state, topic_id="cd" * 16, chat_id=CHAT, project=projects.project_field(PID, "lead"))
        self.assertFalse(projects.is_lead_topic(self.state, "cd" * 16, CHAT))
        elsewhere = projects.lead_topic_id("oc_b", PID)
        write_topic(self.state, topic_id=elsewhere, chat_id="oc_b", project=projects.project_field(PID, "lead"))
        self.assertTrue(projects.is_lead_topic(self.state, elsewhere, "oc_b"))
        self.assertFalse(projects.is_lead_topic(self.state, elsewhere, CHAT))

    def test_the_read_is_kept_until_the_file_changes_and_a_change_shows_at_once(self):
        lead = self.lead()
        with mock.patch.object(projects, "read_object", wraps=projects.read_object) as read:
            for _ in range(5):
                self.assertTrue(projects.is_lead_topic(self.state, lead, CHAT))
            self.assertEqual(read.call_count, 1)
            topics.set_topic_project(self.state, lead, PID, "thread")  # Role changed: the next ask sees it.
            self.assertFalse(projects.is_lead_topic(self.state, lead, CHAT))
            self.assertTrue(projects.is_member_topic(self.state, lead))
            self.assertEqual(read.call_count, 2)
            topics.clear_topic_project(self.state, lead)
            self.assertFalse(projects.is_member_topic(self.state, lead))
            topics.set_topic_project(self.state, lead, PID, "lead")
            self.assertTrue(projects.is_lead_topic(self.state, lead, CHAT))

    def test_a_removed_file_is_not_kept(self):
        lead = self.lead()
        self.assertTrue(projects.is_lead_topic(self.state, lead, CHAT))
        topics.topic_file(self.state, lead).unlink()
        self.assertFalse(projects.is_lead_topic(self.state, lead, CHAT))

    def test_a_rewrite_of_the_same_size_in_the_same_instant_is_seen(self):
        # Role lead -> thread is the same size: the inode (every write replaces the file) tells.
        lead = self.lead()
        path = topics.topic_file(self.state, lead)
        stamp = path.stat()
        self.assertTrue(projects.is_lead_topic(self.state, lead, CHAT))
        data = json.loads(path.read_text())
        data["project"]["role"] = "thread"
        write_json(path, data, ensure_ascii=False, indent=2)
        os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        self.assertFalse(projects.is_lead_topic(self.state, lead, CHAT))


class FlagOffTest(Case):
    flag = ""

    def test_every_predicate_is_false_and_cells_are_empty(self):
        lead, thread = self.lead(), self.thread()
        projects.create(self.state, {**projects.new_record(chat_id=CHAT, name="n", project_id=PID), "threads": [thread]})
        self.assertEqual([projects.is_lead_topic(self.state, lead, CHAT), projects.is_member_topic(self.state, thread),
                          butler.is_lead(lead, CHAT), butler.is_butler_topic(lead, CHAT)], [False] * 4)
        self.assertEqual(projects.cells(self.state, CHAT), {})
        for flag in ("0", "true", "yes", "2"):
            os.environ[projects.ENV] = flag
            self.assertFalse(projects.enabled(), flag)
            self.assertFalse(butler.is_butler_topic(lead, CHAT), flag)

    def test_traits_and_placement_are_the_old_ones(self):
        lead, thread = self.lead(), self.thread()
        self.assertEqual((butler.traits(lead, CHAT), butler.traits(thread, CHAT)),
                         (butler.TOPIC, butler.TOPIC))
        for topic in (lead, thread):
            plan = placement.plan(self.state, {"topic_id": topic, "chat_id": CHAT})
            self.assertEqual((plan.role, plan.row), (placement.TOPIC_ROLE, placement.TABLE[(placement.AREA, "topic")]))
        main = butler.butler_topic_id(CHAT)
        self.assertEqual((butler.traits(main, CHAT), placement.plan(self.state, {"topic_id": main, "chat_id": CHAT}).role),
                         (butler.BUTLER, placement.BUTLER_ROLE))

    def test_no_stat_is_made_for_a_topic_with_the_flag_off(self):
        with mock.patch.object(projects, "topic_project") as read:
            butler.is_butler_topic("ab" * 16, CHAT)
            butler.traits("ab" * 16, CHAT)
            placement.plan(self.state, {"topic_id": "ab" * 16, "chat_id": CHAT})
        read.assert_not_called()

    def test_the_board_groups_a_projects_threads_as_separate_topics(self):
        records = [job(7, THREAD, "done", 100), job(8, "cd" * 16, "done", 200)]
        found = board_read.groups([], records, {}, 1000.0, cells=None)
        self.assertEqual(set(found.results), {THREAD, "cd" * 16})
        self.assertEqual(board_read.read(self.state, CHAT, 1000.0).cells, {})


class ButlerTest(Case):
    def test_the_lead_is_of_the_family_but_not_its_root(self):
        lead = self.lead()
        self.assertTrue(butler.is_butler_topic(lead, CHAT))
        self.assertTrue(butler.is_lead(lead, CHAT))
        self.assertFalse(butler.is_butler_root(lead, CHAT))
        self.assertTrue(butler.is_butler_root(butler.butler_topic_id(CHAT), CHAT))
        self.assertFalse(butler.is_lead(butler.butler_topic_id(CHAT), CHAT))
        self.assertFalse(butler.is_butler_topic(lead, "oc_other"))
        self.assertFalse(butler.is_butler_topic("", CHAT))

    def test_a_thread_is_a_topic_and_a_track_is_still_a_track(self):
        thread = self.thread()
        self.assertEqual((butler.is_butler_topic(thread, CHAT), butler.traits(thread, CHAT)),
                         (False, butler.TOPIC))
        os.environ[butler_tracks.ENV] = "1"
        slot = butler_tracks.track_topic_id(CHAT, 2)
        self.assertEqual((butler.is_butler_topic(slot, CHAT), butler.is_lead(slot, CHAT), butler.traits(slot, CHAT)),
                         (True, False, butler.BUTLER))

    def test_the_lead_row_is_the_butlers_without_a_wait_for_words(self):
        lead = self.lead()
        row = butler.traits(lead, CHAT)
        self.assertIs(row, butler.LEAD)
        self.assertEqual((row.name, row.hold_seconds, row.own_turn, row.listed, row.numbered, row.relays, row.has_thread,
                          row.card_target, row.switch_config, row.rewind, row.top_level),
                         ("lead", 0, True, False, False, False, False, False, False, False, True))
        self.assertEqual({field.name: getattr(row, field.name) for field in butler.dataclasses.fields(row)
                          if field.name not in ("name", "hold_seconds")},
                         {field.name: getattr(butler.BUTLER, field.name) for field in butler.dataclasses.fields(row)
                          if field.name not in ("name", "hold_seconds")})
        self.assertIs(butler.traits_of({"topic_id": lead, "chat_id": CHAT}), butler.LEAD)

    def test_with_the_butler_off_it_is_the_off_row(self):
        lead = self.lead()
        self.mode("topics")
        row = butler.traits(lead, CHAT)
        self.assertIs(row, butler.LEAD_OFF)
        self.assertEqual((row.own_turn, row.answers_in_place, row.lists_supplements, row.wording, row.slim_card, row.linkify_body),
                         (False, False, False, (), False, False))

    def test_without_a_state_directory_nothing_is_a_lead(self):
        lead = self.lead()
        butler.configure_state(None)
        self.assertFalse(butler.is_lead(lead, CHAT))
        self.assertEqual(butler.traits(lead, CHAT), butler.TOPIC)

    def test_a_lead_is_found_from_a_context_by_its_topic_json(self):
        lead = self.lead()
        path = topics.topic_file(self.state, lead)  # The Bridge's topic.json names its topic and chat (context_for_topic).
        write_json(path, {**json.loads(path.read_text()), "topic_id": lead})
        ctx = mock.Mock(history_path=path.parent / "history.jsonl")
        self.assertIs(topics.topic_traits(ctx), butler.LEAD)

    def test_the_lead_is_no_numbered_topic(self):
        lead = self.lead()
        self.assertEqual(topics.topic_number(self.state, lead), 0)


class PlacementTest(Case):
    def plan(self, topic, **extra):
        return placement.plan(self.state, {"topic_id": topic, "chat_id": CHAT, **extra})

    def test_every_layout_and_role_has_a_cell_and_the_two_new_ones_are_the_spec(self):
        p = placement
        roles = (p.BUTLER_ROLE, p.TOPIC_ROLE, p.LEAD_ROLE, p.MEMBER_ROLE)
        self.assertEqual(set(p.TABLE), {(layout, role) for layout in (p.AREA, p.TOPICS) for role in roles})
        self.assertEqual(set(p.ROLES.values()), {p.BUTLER_ROLE, p.TOPIC_ROLE, p.LEAD_ROLE})  # A thread has no Traits row.
        lead, thread = p.TABLE[(p.AREA, p.LEAD_ROLE)], p.TABLE[(p.AREA, p.MEMBER_ROLE)]
        self.assertEqual((lead.answer, lead.conclusion, lead.stop, lead.question_copy, lead.quiet, lead.reply_top_level),
                         (p.TOP, p.NONE, p.NONE, p.NONE, True, False))
        self.assertEqual((thread.answer, thread.conclusion, thread.stop, thread.question_copy, thread.quiet),
                         (p.THREAD, p.NONE, p.NONE, p.TOP, False))
        for role in (p.LEAD_ROLE, p.MEMBER_ROLE):
            self.assertIs(p.TABLE[(p.TOPICS, role)], p.TABLE[(p.TOPICS, p.TOPIC_ROLE)])

    def test_a_topics_role_is_read_from_its_topic_json(self):
        lead, thread = self.lead(), self.thread()
        plain = "cd" * 16
        self.topic(plain)
        got = [(plan.role, plan.row.quiet, plan.row.conclusion) for plan in map(self.plan, (lead, thread, plain))]
        self.assertEqual(got, [("lead", True, ""), ("project_member", False, ""), ("topic", False, placement.TOP)])
        self.assertIs(self.plan(lead).traits, butler.LEAD)
        self.assertIs(self.plan(thread).traits, butler.TOPIC)

    def test_a_job_or_a_record_or_topic_json_all_name_the_topic(self):
        thread = self.thread()
        job_like = mock.Mock(topic_id=thread, chat_id=CHAT)
        for of in (job_like, {"topic_id": thread, "chat_id": CHAT}, write_job(self.state, "k", chat_id=CHAT, topic_id=thread)):
            self.assertEqual(placement.plan(self.state, of).role, "project_member")
        # A Traits row names no topic: the thread's role cannot be told from it.
        self.assertEqual(placement.plan(self.state, butler.traits(thread, CHAT)).role, "topic")

    def test_an_upgrade_takes_effect_on_the_next_plan(self):
        topic = "cd" * 16
        self.topic(topic)
        self.assertEqual(self.plan(topic).role, "topic")
        topics.set_topic_project(self.state, topic, PID, "thread")
        self.assertEqual(self.plan(topic).role, "project_member")
        topics.clear_topic_project(self.state, topic)
        self.assertEqual(self.plan(topic).role, "topic")

    def test_the_read_is_a_stat_a_call(self):
        thread = self.thread()
        with mock.patch.object(projects, "read_object", wraps=projects.read_object) as read:
            for _ in range(4):
                self.plan(thread)
        self.assertEqual(read.call_count, 1)

    def test_without_a_state_directory_or_a_topic_the_role_is_the_traits(self):
        thread = self.thread()
        self.assertEqual(placement.plan(None, {"topic_id": thread, "chat_id": CHAT}).role, "topic")
        self.assertEqual(placement.plan(self.state, {"chat_id": CHAT}).role, "topic")
        self.assertEqual(placement.plan(self.state, {"topic_id": 5, "chat_id": CHAT}).role, "topic")

    def test_the_butlers_are_unchanged(self):
        main = butler.butler_topic_id(CHAT)
        plan = self.plan(main)
        self.assertEqual((plan.role, plan.row.answer_on_card, plan.row.quiet), ("butler", placement.ROUTES, True))
        self.mode("topics")
        plan = self.plan(self.thread())
        self.assertEqual((plan.layout, plan.role, plan.row), (placement.TOPICS, "project_member",
                                                              placement.TABLE[(placement.TOPICS, "topic")]))

    def test_a_threads_conclusion_does_not_relay(self):
        from covey.app.relay_targets import relays

        thread, plain = self.thread(), "cd" * 16
        self.topic(plain)
        self.assertEqual((relays(self.state, thread, CHAT), relays(self.state, plain, CHAT)), (False, True))


def job(number, topic, status, ended, *, card=True, question=None):
    return {"job_id": number, "topic_id": topic, "chat_id": CHAT, "status": status, "updated_at": "2026-11-16T00:00:00Z",
            "card_message_id": f"om_card{number}" if card else "", "timings": {"backend_started": ended - 60,
                                                                                   "execution_finished": ended}}


class BoardTest(Case):
    """A project's threads are one cell of the board: the lead's topic id."""

    NOW = 10_000.0
    T1, T2, T3 = "a1" * 16, "a2" * 16, "a3" * 16
    CELLS = {"a1" * 16: LEAD, "a2" * 16: LEAD, "a3" * 16: LEAD}

    def groups(self, jobs, questions=(), seen=None, cells=CELLS, rules=board_read.Rules()):
        return board_read.groups(list(questions), jobs, seen or {}, self.NOW, rules, cells)

    def question(self, topic, at, number=1):
        return {"topic_id": topic, "created_at": at, "question_message_id": f"om_q{number}", "token": f"q{number}"}

    def test_the_threads_are_one_cell(self):
        found = self.groups([job(1, self.T1, "done", 9000), job(2, self.T2, "done", 9500), job(3, "cd" * 16, "done", 9400)])
        self.assertEqual(set(found.results), {LEAD, "cd" * 16})
        self.assertEqual(found.waiting, {})

    def test_a_question_in_any_thread_waits_the_newest_one_and_hides_the_rest(self):
        found = self.groups([job(1, self.T1, "done", 9000), job(2, self.T2, "running", 9500)],
                            [self.question(self.T1, 9100, 1), self.question(self.T3, 9200, 2)])
        self.assertEqual({topic: record["token"] for topic, record in found.waiting.items()}, {LEAD: "q2"})
        self.assertEqual((found.running, found.results), ({}, {}))

    def test_a_running_thread_hides_the_results_and_the_running_one_the_board_shows_is_one_job(self):
        found = self.groups([job(1, self.T1, "done", 9000), job(2, self.T2, "queued", 9500), job(3, self.T3, "running", 9600)])
        self.assertEqual({topic: record["job_id"] for topic, record in found.running.items()}, {LEAD: 3})
        self.assertEqual(found.results, {})
        again = self.groups([job(5, self.T1, "done", 9000), job(4, self.T2, "queued", 9500)])
        self.assertEqual(again.running[LEAD]["job_id"], 4)  # Nothing running: the queued one shows.

    def test_the_result_is_the_one_not_seen_that_ended_last_else_the_one_that_ended_last(self):
        # Job ids are the Bridge's one count: thread 1's job 5 ended after thread 2's job 6.
        jobs = [job(5, self.T1, "done", 9500), job(6, self.T2, "done", 9000)]
        both_unseen = self.groups(jobs)
        self.assertEqual(both_unseen.results[LEAD][0]["job_id"], 5)  # Ended last, not the highest job id.
        seen = {self.T1: self.NOW - 10}  # The owner has read thread 1: thread 2's older one is the unseen result.
        seen_one = self.groups(jobs, seen=seen)
        self.assertEqual((seen_one.results[LEAD][0]["job_id"], seen_one.results[LEAD][2]), (6, None))
        seen_both = self.groups(jobs, seen={self.T1: self.NOW - 10, self.T2: self.NOW - 10})
        self.assertEqual(seen_both.results[LEAD][0]["job_id"], 5)
        self.assertIsNotNone(seen_both.results[LEAD][2])

    def test_unseen_counts_the_cell_once(self):
        found = self.groups([job(5, self.T1, "done", 9500), job(6, self.T2, "done", 9000)])
        self.assertEqual(list(board_read.unseen(found)), [LEAD])
        self.assertEqual(board_read.unseen(found, off={LEAD}), {})

    def test_a_job_cancelled_before_it_ran_is_no_latest_of_its_thread(self):
        stopped = {**job(7, self.T2, "cancelled", 9900), "never_started": True}
        found = self.groups([job(5, self.T1, "done", 9500), stopped])
        self.assertEqual(found.results[LEAD][0]["job_id"], 5)

    def test_a_thread_outside_the_cells_is_its_own_cell_and_no_cells_is_the_old_grouping(self):
        jobs = [job(1, self.T1, "done", 9000), job(2, self.T2, "done", 9100)]
        self.assertEqual(set(self.groups(jobs, cells={self.T1: LEAD}).results), {LEAD, self.T2})
        self.assertEqual(set(self.groups(jobs, cells=None).results), {self.T1, self.T2})
        self.assertEqual(set(self.groups(jobs, cells={}).results), {self.T1, self.T2})

    def test_the_cells_of_a_chat_are_its_projects_threads_not_cancelled_ones(self):
        for pid, state, chat, threads in ((PID, "active", CHAT, [self.T1, self.T2]), (OTHER, "cancelled", CHAT, [self.T3]),
                                          ("0123456789ab", "done", "oc_b", ["b1" * 16])):
            record = {**projects.new_record(chat_id=chat, name="n", project_id=pid), "threads": threads}
            projects.create(self.state, record)
            projects.set_state(self.state, pid, state) if state != "active" else None
        self.assertEqual(projects.cells(self.state, CHAT), {self.T1: LEAD, self.T2: LEAD})
        self.assertEqual(projects.cells(self.state, "oc_b"), {"b1" * 16: projects.lead_topic_id("oc_b", "0123456789ab")})
        self.assertEqual(projects.cells(self.state, "oc_none"), {})
        self.assertEqual(projects.cells(None, CHAT), {})

    def test_the_lead_itself_is_no_cell_of_its_own_threads_and_not_listed(self):
        self.lead()
        self.assertFalse(board_read.listed(LEAD, CHAT))
        projects.create(self.state, {**projects.new_record(chat_id=CHAT, name="n", project_id=PID), "threads": [LEAD, self.T1]})
        self.assertEqual(projects.cells(self.state, CHAT), {self.T1: LEAD})

    def test_through_the_board_one_pin_per_project(self):
        now = time.time()
        for number, topic in ((1, self.T1), (2, self.T2)):
            write_topic(self.state, topic_id=topic, chat_id=CHAT, harness_id="claude", topic_number=number, root_id=f"om_r{number}",
                        project=projects.project_field(PID, "thread"))
            write_job(self.state, f"om_job{number}-{number}", chat_id=CHAT, topic_id=topic, job_id=number, status="done",
                      card_message_id=f"om_card{number}", harness_id="claude",
                      timings={"backend_started": now - 600 - number, "execution_finished": now - 300 - number})
        write_topic(self.state, topic_id="cd" * 16, chat_id=CHAT, topic_number=3, harness_id="claude", root_id="om_r3")
        write_job(self.state, "om_job3-3", chat_id=CHAT, topic_id="cd" * 16, job_id=3, status="done", card_message_id="om_card3",
                  harness_id="claude", timings={"backend_started": now - 700, "execution_finished": now - 400})
        projects.create(self.state, {**projects.new_record(chat_id=CHAT, name="n", project_id=PID), "threads": [self.T1, self.T2]})
        long_ago = now - 30 * DAY
        write_json(self.state / pins.RECORD, {"schema_version": 1, "chat_id": CHAT, "since": long_ago, "keep_since": long_ago,
                                              "stopped_since": long_ago, "pins": [], "dismissed": {}, "results": {}})
        rows, _ = pins.PinnedTopics(None, None).look(self.state, CHAT, now)
        self.assertEqual(sorted(row["topic"] for row in rows), sorted([LEAD, "cd" * 16]))
        self.assertEqual({row["topic"]: row["record"]["job_id"] for row in rows}[LEAD], 1)  # Ended last.
        os.environ[projects.ENV] = ""
        rows, _ = pins.PinnedTopics(None, None).look(self.state, CHAT, now)
        self.assertEqual(sorted(row["topic"] for row in rows), sorted([self.T1, self.T2, "cd" * 16]))

    def test_the_board_record_keeps_a_cells_older_result_and_read_reaches_it(self):
        old = time.time() - 5 * DAY
        write_topic(self.state, topic_id=self.T1, chat_id=CHAT, harness_id="claude", topic_number=1, root_id="om_r1")
        record = write_job(self.state, "om_old-1", chat_id=CHAT, topic_id=self.T1, job_id=1, status="done",
                           card_message_id="om_card1", harness_id="claude", timings={"execution_finished": old})
        path = Path(self.state) / "jobs" / "om_old-1.json"
        data = json.loads(path.read_text())
        data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(old))
        path.write_text(json.dumps(data))
        projects.create(self.state, {**projects.new_record(chat_id=CHAT, name="n", project_id=PID), "threads": [self.T1]})
        self.assertEqual(board_read.read(self.state, CHAT, time.time()).jobs, [])
        kept = board_read.read(self.state, CHAT, time.time(), keep={LEAD})
        self.assertEqual([r["job_id"] for r in kept.jobs], [record["job_id"]])
        self.assertEqual(kept.keep, frozenset({LEAD}))


class MaintenanceTest(Case):
    def test_an_ended_record_goes_with_its_folder_after_180_days_and_a_live_one_stays(self):
        pids = {"active": "aaaaaa111111", "paused": "bbbbbb111111", "done": "cccccc111111", "cancelled": "dddddd111111"}
        for state, pid in pids.items():
            projects.create(self.state, projects.new_record(chat_id=CHAT, name=state, project_id=pid))
            if state != "active":
                projects.set_state(self.state, pid, state)
            (projects.folder(self.state) / pid).mkdir()
            (projects.folder(self.state) / pid / "memory.json").write_text("{}")
        recent = projects.record_path(self.state, "dddddd111111")
        later = time.time() + 200 * DAY
        os.utime(recent, (later - 10 * DAY, later - 10 * DAY))  # Ended, but changed lately.
        plan = maintain(self.state, now=later)
        self.assertEqual(plan["remove_projects"], [str(projects.record_path(self.state, "cccccc111111"))])
        maintain(self.state, apply=True, now=later)
        left = sorted(p.name for p in projects.folder(self.state).iterdir() if not p.name.startswith("."))
        self.assertEqual(left, sorted(f"{pid}{suffix}" for pid in ("aaaaaa111111", "bbbbbb111111", "dddddd111111")
                                      for suffix in (".json", "")))

    def test_the_rule_is_in_the_plan_the_cleanup_and_the_dry_run_and_projects_locks_are_swept(self):
        from covey.core import maintenance_rules as rules

        self.assertIn(rules.PROJECT_RECORDS, rules.RULES)
        self.assertEqual((rules.CLEANUP.count(rules.PROJECT_RECORDS), rules.DRY_RUN.count(rules.PROJECT_RECORDS)), (1, 1))
        self.assertIn("projects", rules.LOCK_DIRS)
        self.assertIn("remove_projects", rules.REMOVALS)

    def test_with_no_projects_folder_it_selects_nothing(self):
        self.assertEqual(maintain(self.state)["remove_projects"], [])

    def test_a_record_whose_name_is_no_project_id_is_never_removed(self):
        folder = projects.folder(self.state)
        folder.mkdir(parents=True)
        stray = folder / "stray.json"
        stray.write_text(json.dumps({"state": "done"}))
        os.utime(stray, (1, 1))
        self.assertEqual(maintain(self.state, apply=True)["remove_projects"], [])
        self.assertTrue(stray.exists())


class AuditTest(Case):
    """The call sites of is_butler_topic and what they do with a lead (of the family) and a thread (a topic): the audit of
    docs/architecture.md 专项, one case each where a lead's answer differs from a thread's."""

    def setUp(self):
        super().setUp()
        self.lead_id, self.thread_id = self.lead(), self.thread()
        self.env = make_env(self.state)

    def test_butler_tools_run_for_the_lead_and_refuse_a_thread(self):
        tools = ButlerTools(self.env, {"topics_list": lambda env, request: reply(True, "ok")})
        request = mock.Mock(tool="topics_list")
        for topic, allowed in ((self.lead_id, True), (self.thread_id, False), (butler.butler_topic_id(CHAT), True)):
            write_job(self.state, "k", chat_id=CHAT, topic_id=topic, job_id=1, status="running")
            request.job_key = "k"
            self.assertEqual(tools.refusal(request) is None, allowed, topic)

    def test_a_tool_cannot_open_the_lead_as_a_topic_but_can_a_thread(self):
        write_job(self.state, "j1", chat_id=CHAT, topic_id=THREAD, job_id=1, status="done")
        self.assertEqual(butler_tools.resolve(self.env, CHAT, self.thread_id)[0], self.thread_id)
        with self.assertRaises(ValueError):
            butler_tools.resolve(self.env, CHAT, self.lead_id)

    def test_the_board_read_leaves_the_lead_out_and_lists_the_thread(self):
        write_job(self.state, "j1", chat_id=CHAT, topic_id=THREAD, job_id=1, status="done")
        write_job(self.state, "j2", chat_id=CHAT, topic_id=self.lead_id, job_id=2, status="done")
        self.assertEqual(set(board_read.topic_jobs(self.state, CHAT)), {THREAD})

    def test_the_policy_retry_leaves_the_lead_and_takes_a_thread(self):
        event = mock.Mock()
        with mock.patch.object(policy_retry, "fallback_model", return_value=("claude-old", "old")):
            for topic, found in ((self.lead_id, None), (self.thread_id, ("claude-old", "old"))):
                record = {"backend": "claude", "topic_id": topic, "chat_id": CHAT, "model": "claude-new"}
                self.assertEqual(policy_retry.eligible(record, event), found, topic)

    def test_a_message_quoting_a_leads_card_stays_with_the_butler_app_and_a_threads_does_not(self):
        write_json(topics_alias(self.state, "om_leadcard"), {"topic_id": self.lead_id})
        write_json(topics_alias(self.state, "om_threadcard"), {"topic_id": self.thread_id})
        known = [topics.message_topic(self.state, CHAT, mid) for mid in ("om_leadcard", "om_threadcard")]
        self.assertEqual([butler.is_butler_topic(topic, CHAT) for topic in known], [True, False])

    def test_the_answer_link_of_a_lead_turn_is_the_owners_message_not_a_thread(self):
        with mock.patch("covey.app.topic_links.topic_thread_link", return_value=("https://thread", "thread")) as thread_link, \
                mock.patch.object(answer_cards, "owner_message", return_value="om_ask"), \
                mock.patch.object(answer_cards, "ask_link", return_value="https://chat") as chat_link:
            self.assertEqual(answer_cards.ask_url(self.state, {"chat_id": CHAT, "topic_id": self.thread_id}), "https://thread")
            self.assertEqual(answer_cards.ask_url(self.state, {"chat_id": CHAT, "topic_id": self.lead_id}), "https://chat")
        self.assertEqual((thread_link.call_count, chat_link.call_count), (1, 1))

    def test_a_lead_turn_is_the_owners_whatever_its_sender(self):
        from covey.app.runs import Runs

        runs = Runs.__new__(Runs)
        runs.runtime = mock.Mock(access=mock.Mock(owner_turn=mock.Mock(return_value=False)))
        ctx = mock.Mock(topic_id="")
        owners = [runs.owner_turn(ctx, mock.Mock(topic_id=topic, chat_id=CHAT, sender_id="ou_x"))
                  for topic in (self.lead_id, self.thread_id)]
        self.assertEqual(owners, [True, False])

    def test_the_lead_cards_quote_stays_the_leads(self):
        from covey.app import butler_relay

        gateway = mock.Mock()
        job = {"topic_id": self.lead_id, "chat_id": CHAT, "message_id": "om_x", "job_id": 3}
        with mock.patch.object(butler_relay, "areas_of", return_value={THREAD}) as areas, \
                mock.patch.object(butler_relay, "find_job_record", return_value={"card_message_id": "om_card"}), \
                mock.patch.object(butler_relay, "route_message") as route:
            butler_relay.after_runner(gateway, job, mock.Mock(state_dir=self.state))
            areas.assert_not_called()
            route.assert_not_called()
            butler_relay.after_runner(gateway, {**job, "topic_id": butler.butler_topic_id(CHAT)}, mock.Mock(state_dir=self.state))
            route.assert_called_once()


def make_env(state):
    runtime = mock.Mock(log=lambda line: None, access=mock.Mock(owner_chat=lambda: CHAT))
    return ButlerEnv(runtime, mock.Mock(harnesses={}, default="claude", target=lambda *args: None), None, None,
                   state_dir=Path(state), profile="unit")


def topics_alias(state, message_id):
    return topics._alias_path(Path(state) / "topics", CHAT, message_id)


if __name__ == "__main__":
    unittest.main()
