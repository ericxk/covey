"""专项's three entrances (app/projects.py, app/project_cards.py downgrade; docs/architecture.md 专项): a 派发卡 upgraded to a
project (the switch COVEY_PROJECT_UPGRADE, the seven steps, each safe to run again after a crash), a project opened directly
(topic_start with ``project``), 不要专项, the butler's two tools, and that with COVEY_PROJECTS unset (or the upgrade ``off``)
none of it does anything."""
from __future__ import annotations

import dataclasses
import json
import os
import time
from types import SimpleNamespace
from unittest import mock

from support.records import write_topic
from test_dispatch_cards import ASK, ASK_URL, CHAT, DispatchCase

from covey.app import butler_topics, project_cards, project_lead
from covey.app import projects as upgrades
from covey.cards.elements import find_element
from covey.core import dispatch_records as records
from covey.core import butler_mcp, butler, butler_settings, butler_tool_specs, projects
from covey.core.jobs import BridgeReplyJob, find_job_record, upsert_job_record
from covey.core.butler_requests import Request
from covey.core.state import read_object, write_json
from covey.core.topics import load_topic, message_topic, routed_topic, topic_title

LONG = "这件事长期做，接着做"


def text(card) -> str:
    return json.dumps(card, ensure_ascii=False)


class UpCase(DispatchCase):
    """A Bridge with COVEY_PROJECTS on and the upgrade on ``butler``, the butler's topic.json written."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {projects.ENV: "1", upgrades.ENV: "butler"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.state = self.ctx.state_dir
        previous = butler.configure_state(self.state)
        self.addCleanup(butler.configure_state, previous)
        write_json(self.state / butler_settings.FILE, {"mode": "agent"})  # the butler: the lead needs the butler.
        butler.forget_mode()
        self.addCleanup(butler.forget_mode)
        write_topic(self.state, topic_id=butler.butler_topic_id(CHAT), chat_id=CHAT, harness_id="claude", model="claude-opus-x",
                    effort="medium", main_thread=True, workspace_name="main-space", workdir="/absolute/main", root_id="")
        self.up = upgrades.Projects(self.env)
        self.env.projects = self.up
        place = mock.patch("covey.app.topic_links.place_of", return_value={"position": 9})
        place.start()
        self.addCleanup(place.stop)
        self.writer = project_cards.ProjectCards(self.app.runtime, self.app.messenger, self.app.voice, {"claude": "Claude Code"})

    def long(self, words=LONG):
        """The butler's ``project`` flag on the 派发卡 the next topic_start leaves (``words`` are the owner's, which decide
        nothing: only the flag turns a topic into a project)."""
        upsert_job_record(self.state, "main-job", user_text=words)
        real = butler_topics.dispatch_card
        patcher = mock.patch.object(butler_topics, "dispatch_card",
                                    lambda env, request, topic_id, **how: real(env, request, topic_id, **{**how, "project": True}))
        patcher.start()
        self.addCleanup(patcher.stop)

    def plain(self, request_id="r1", **args):
        """A topic opened with the upgrade off: its 派发卡 stays live."""
        with mock.patch.dict(os.environ, {upgrades.ENV: ""}):
            answer = self.start(request_id, **args)
        self.assertTrue(answer["ok"], answer)
        return answer["data"]["topic_id"], self.record(request_id)

    def project(self, dispatch):
        return projects.read_record(self.state, upgrades.project_id("upgrade", dispatch["token"]))

    def receipts(self, pid):
        return [read_object(path) for path in sorted(project_lead.inbox_dir(self.state, pid).glob("*.json"))]

    def projects_on_disk(self):
        return projects.records(self.state)

    def second_dispatch(self, topic, token, **fields):
        """Another live 派发卡 of ``topic``, as the butler's tools leave one."""
        record = records.new_record(token=token, chat_id=CHAT, topic_id=topic, job_key="main-job", mid="m-" + token, kind="send",
                                    owner_message="om_owner_question", request="再看看", starts=True)
        record.update(card_id="card-" + token, card_message_id="om_" + token, sequence=3, **fields)
        records.folder(self.state).mkdir(parents=True, exist_ok=True)
        records.record_path(self.state, token).write_text(json.dumps(record))
        return record


class SwitchTest(UpCase):
    def test_the_upgrade_is_off_by_default_and_the_butlers_flag_changes_nothing_then(self):
        self.long()
        with mock.patch.dict(os.environ, {upgrades.ENV: ""}):
            answer = self.start()
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(self.projects_on_disk(), [])
        self.assertEqual((self.record()["state"], self.record()["project_id"]), ("live", None))
        self.assertEqual(upgrades.mode(), "butler")
        with mock.patch.dict(os.environ, {upgrades.ENV: "word"}):
            self.assertEqual(upgrades.mode(), "butler")  # The old value, the same meaning.
        for bad in ("", "off", "yes", "1"):
            with mock.patch.dict(os.environ, {upgrades.ENV: bad}):
                self.assertEqual(upgrades.mode(), "off", bad)

    def test_with_the_total_switch_off_nothing_is_done_whatever_the_upgrade_says(self):
        self.long()
        with mock.patch.dict(os.environ, {projects.ENV: ""}):
            self.assertEqual(self.up.threads(), ())
            answer = self.start()
            self.assertEqual(self.up.consider(self.record()["token"]), "")
        self.assertTrue(answer["ok"])
        self.assertEqual(self.projects_on_disk(), [])
        self.assertFalse((self.state / "projects").exists())
        self.assertEqual(self.record()["state"], "live")

    def test_butler_upgrades_on_the_butlers_flag_alone_not_on_a_word_or_on_rounds(self):
        topic, dispatch = self.plain()
        self.assertEqual(self.up.consider(dispatch["token"]), "")  # No word, no flag.
        self.second_dispatch(topic, "b" * 16, state="settled")
        self.second_dispatch(topic, "c" * 16, state="settled")
        self.assertEqual(upgrades.rounds_for_topic(self.state, topic), 3)
        self.assertEqual(self.up.consider(dispatch["token"]), "")  # The third round is ``auto``'s.
        self.assertEqual(self.projects_on_disk(), [])
        hinted = {**dispatch, "upgrade_hint": {"long_word": False, "project": True}}
        self.assertEqual(self.up.reason(hinted), ("butler", 0))

    def test_auto_also_upgrades_on_the_third_round_and_the_card_says_which(self):
        topic, dispatch = self.plain()
        self.second_dispatch(topic, "b" * 16, state="settled")
        with mock.patch.dict(os.environ, {upgrades.ENV: "auto"}):
            self.assertEqual(self.up.consider(dispatch["token"]), "")  # Two rounds.
            self.second_dispatch(topic, "c" * 16, state="settled")
            pid = self.up.consider(dispatch["token"])
        self.assertTrue(pid)
        project = projects.read_record(self.state, pid)
        self.assertEqual(project["origin"]["note"], "来回 3 轮后转成专项")
        self.assertEqual((project["origin"]["why"], project["origin"]["rounds"]), ("rounds", 3))

    def test_the_rounds_are_the_larger_of_the_cards_and_the_owners_own_turns_and_the_old_field_is_not_read(self):
        topic, dispatch = self.plain()
        self.assertEqual(upgrades.rounds_for_topic(self.state, topic), 1)
        path = records.record_path(self.state, dispatch["token"])
        path.write_text(json.dumps({**dispatch, "rounds": 9}))  # The record's own count is ignored.
        self.assertEqual(upgrades.rounds_for_topic(self.state, topic), 1)
        for number, message in enumerate(("om_a", "om_b", "om_c", "main-x", "lead-1-1"), 1):
            upsert_job_record(self.state, f"own{number}", topic_id=topic, chat_id=CHAT, job_id=100 + number, message_id=message,
                              status="done")
        self.assertEqual(upgrades.rounds_for_topic(self.state, topic), 3)  # Only the om_ ones are the owner's.
        self.second_dispatch(topic, "d" * 16)
        self.second_dispatch(topic, "e" * 16)
        self.second_dispatch(topic, "f" * 16)
        self.assertEqual(upgrades.rounds_for_topic(self.state, topic), 4)  # The cards, now more.

    def test_the_butler_flag_on_topic_send_is_kept_in_the_dispatch_record_only_with_the_switch_on(self):
        request = SimpleNamespace(args={"project": True})
        self.assertEqual(butler_topics.project_hint(request), {"project": True})
        self.assertEqual(butler_topics.project_hint(SimpleNamespace(args={"project": "yes"})), {})
        self.assertEqual(butler_topics.project_hint(SimpleNamespace(args={})), {})
        with mock.patch.dict(os.environ, {projects.ENV: ""}):
            self.assertEqual(butler_topics.project_hint(request), {})

    def test_a_thread_and_a_lead_are_never_upgraded_again(self):
        self.long()
        answer = self.start()
        topic = answer["data"]["topic_id"]
        again = self.second_dispatch(topic, "9" * 16)
        self.assertEqual(self.up.consider(again["token"]), "")  # The topic is a project's thread now.
        self.assertEqual(len(self.projects_on_disk()), 1)


class UpgradeTest(UpCase):
    def upgraded(self):
        self.long()
        answer = self.start()
        self.assertTrue(answer["ok"], answer)
        dispatch = self.record()
        return answer["data"]["topic_id"], dispatch, self.project(dispatch)

    def test_the_butlers_flag_turns_the_card_into_the_projects_with_all_seven_steps_done(self):
        topic, dispatch, project = self.upgraded()
        self.assertEqual(set(project["steps"]), set(upgrades.UPGRADE_STEPS))
        self.assertEqual(project["name"], topic_title(self.state, topic))
        self.assertEqual((project["state"], project["threads"], project["chat_id"]), ("active", [topic], CHAT))
        self.assertEqual(project["origin"]["note"], "由管家转成专项")
        self.assertEqual(project["origin"]["topic_id"], topic)
        # The same card and message, the dispatch record settled behind it.
        self.assertEqual((project["card"]["card_id"], project["card"]["message_id"]),
                         (dispatch["card_id"], dispatch["card_message_id"]))
        self.assertEqual((dispatch["state"], dispatch["settled_by"], dispatch["project_id"]), ("settled", "upgrade", project["id"]))
        self.assertEqual(len(self.dispatch_sends()), 1)  # No new card was sent.
        # The topic is a thread; the lead has its own topic.json.
        self.assertEqual(load_topic(self.state, topic)["project"], {"id": project["id"], "role": "thread"})
        self.assertTrue(projects.is_member_topic(self.state, topic))
        lead = load_topic(self.state, project["lead_topic_id"])
        self.assertEqual(lead["project"], {"id": project["id"], "role": "lead"})
        self.assertTrue(projects.is_lead_topic(self.state, project["lead_topic_id"], CHAT))
        self.assertEqual((lead["topic_id"], lead["chat_id"], lead["harness_id"], lead["model"], lead["effort"]),
                         (project["lead_topic_id"], CHAT, "claude", "claude-opus-x", "medium"))
        self.assertTrue(lead["workspace_locked"] and lead["harness_locked"])
        self.assertNotIn("main_thread", lead)
        self.assertEqual(lead["workspace_name"], "project")  # The topic's, not the butler's.
        self.assertEqual(lead["workdir"], load_topic(self.state, topic)["workdir"])
        self.assertEqual(routed_topic(self.state, CHAT, dispatch["card_message_id"]), project["lead_topic_id"])
        # Its clicks go to the lead as well: the buttons name the lead's topic (accept7: a click on an upgraded card
        # was refused as other_topic while the 派发卡's alias still named the thread).
        self.assertEqual(message_topic(self.state, CHAT, dispatch["card_message_id"]), project["lead_topic_id"])

    def test_the_start_receipt_says_what_the_lead_takes_over(self):
        topic, dispatch, project = self.upgraded()
        (receipt,) = self.receipts(project["id"])
        self.assertEqual((receipt["kind"], receipt["thread"]), ("start", topic))
        self.assertTrue(receipt["text"].startswith("接手一个已在进行的事：话题 T"), receipt["text"])
        self.assertIn("查日志", receipt["text"])
        self.assertIn("主人要的：", receipt["text"])
        self.assertEqual(project["ask"]["text"], LONG)

    def test_the_card_goes_on_from_the_dispatch_cards_sequence_and_says_where_it_came_from(self):
        topic, dispatch = self.plain()
        path = records.record_path(self.state, dispatch["token"])
        path.write_text(json.dumps({**dispatch, "sequence": 7}))
        pid = self.up.upgrade(read_object(path), "word")
        project = projects.read_record(self.state, pid)
        self.assertEqual(project["card"]["sequence"], 7)
        self.writer.tick(self.ctx, time.time() + 5)
        args, kwargs = self.put.call_args
        self.assertEqual((args[1], kwargs["sequence"]), (dispatch["card_id"], 8))
        self.assertIn("由管家转成专项", find_element(args[2], "pj_meta")["content"])
        self.assertIsNotNone(find_element(args[2], "pj_down"))  # 不要专项.
        self.assertIn("你问", text(find_element(args[2], "answer_ask")))

    def test_the_other_live_cards_of_the_topic_become_a_line_and_route_to_the_lead(self):
        topic, dispatch = self.plain()
        other = self.second_dispatch(topic, "b" * 16)
        pid = self.up.upgrade(dispatch, "word")
        project = projects.read_record(self.state, pid)
        saved = read_object(records.record_path(self.state, "b" * 16))
        self.assertEqual((saved["state"], saved["settled_by"], saved["project_id"]), ("settled", "merged", pid))
        card = next(call.args[2] for call in self.put.call_args_list if call.args[1] == other["card_id"])
        self.assertIn("已并入专项", text(card))
        self.assertEqual(routed_topic(self.state, CHAT, other["card_message_id"]), project["lead_topic_id"])
        self.assertEqual(self.put.call_args.kwargs["sequence"], 4)  # After the card's own 3.

    def test_a_merged_line_that_cardkit_did_not_confirm_is_tried_again_by_the_next_run(self):
        topic, dispatch = self.plain()
        self.second_dispatch(topic, "b" * 16)
        self.put.return_value = None
        pid = self.up.upgrade(dispatch, "word")
        project = projects.read_record(self.state, pid)
        self.assertNotIn("merged", project["steps"])
        self.assertNotIn("route", project["steps"])
        self.assertEqual(read_object(records.record_path(self.state, "b" * 16))["state"], "live")
        self.put.return_value = {"code": 0}
        self.up.upgrade(dispatch, "word")
        self.assertEqual(set(projects.read_record(self.state, pid)["steps"]), set(upgrades.UPGRADE_STEPS))
        self.assertEqual(read_object(records.record_path(self.state, "b" * 16))["state"], "settled")

    def test_three_active_projects_stop_the_next_upgrade_and_a_stopped_one_does_not_count(self):
        for number in range(projects.MAX_ACTIVE):
            projects.create(self.state, projects.new_record(chat_id=CHAT, name=f"n{number}", project_id=f"{number:012x}"))
        self.assertEqual(projects.active_count(self.state), 3)
        topic, dispatch = self.plain()
        self.assertEqual(self.up.upgrade(dispatch, "word"), "")
        self.assertEqual(self.record()["state"], "live")
        self.assertTrue(any("projects are on" in line for line in self.logs))
        projects.set_state(self.state, f"{0:012x}", projects.PAUSED)
        self.assertEqual(projects.active_count(self.state), 2)
        self.assertTrue(self.up.upgrade(dispatch, "word"))
        with mock.patch.dict(os.environ, {projects.ENV: ""}):
            self.assertEqual(projects.active_count(self.state), 0)

    def test_a_stale_write_of_the_dispatch_thread_is_dropped_after_the_upgrade(self):
        topic, dispatch = self.plain()
        stale = read_object(records.record_path(self.state, dispatch["token"]))
        self.up.upgrade(dispatch, "word")
        before = self.put.call_count
        seen = {"failures": 0, "wrote": 0, "next": 0.0}
        path = records.record_path(self.state, dispatch["token"])
        self.assertFalse(self.cards.write(self.ctx, path, stale, seen, {"schema": "2.0"}, 1000.0))
        self.assertEqual(self.put.call_count, before)  # Nothing went out over the project's card.
        self.assertEqual(read_object(path)["state"], "settled")  # And the record was not written live again.
        self.assertEqual(seen["failures"], 0)

    def test_the_dispatch_thread_does_not_write_a_settled_record_live_again_after_its_write(self):
        topic, dispatch = self.plain()
        self.set_job(status="running", phase="跑测试", workspace_name="project", harness_id="claude", model="Claude · Sonnet")
        original = self.put.side_effect

        def upgrade_meanwhile(*args, **kwargs):
            self.up.upgrade(read_object(records.record_path(self.state, dispatch["token"])), "word")
            return {"code": 0}
        self.put.side_effect = upgrade_meanwhile
        self.tick(self.record()["started_at"] + 20)  # Its own write is under way when the upgrade runs (another thread's, here the same).
        self.put.side_effect = original
        self.assertEqual(self.record()["state"], "settled")
        self.assertEqual(self.record()["settled_by"], "upgrade")

    def test_a_topic_the_owner_turned_back_is_not_upgraded_by_a_word_or_a_round_but_by_the_butlers_flag(self):
        topic, dispatch = self.plain()
        pid = self.up.upgrade({**dispatch, "upgrade_hint": {"long_word": True}}, "word")
        projects.set_state(self.state, pid, projects.CANCELLED, "downgraded")
        again = self.second_dispatch(topic, "8" * 16, upgrade_hint={"long_word": True})
        self.assertEqual(self.up.reason(again), None)
        with mock.patch.dict(os.environ, {upgrades.ENV: "auto"}):
            self.assertEqual(self.up.reason(self.second_dispatch(topic, "7" * 16)), None)
        self.assertEqual(self.up.reason({**again, "upgrade_hint": {"project": True}}), ("butler", 0))
        projects.create(self.state, {**projects.new_record(chat_id=CHAT, name="x", project_id="0f9e8d7c6b5a",
                                                           origin={"topic_id": "ab" * 16}), "state": "cancelled", "state_reason": "stopped"})
        self.assertFalse(self.up.turned_back("ab" * 16))

    def test_a_dispatch_record_that_ended_some_other_way_gives_the_project_a_card_of_its_own(self):
        topic, dispatch = self.plain()
        path = records.record_path(self.state, dispatch["token"])
        path.write_text(json.dumps({**dispatch, "state": "settled", "settled_by": "relay"}))
        before = self.sent.call_count
        pid = self.up.upgrade(read_object(path), "word")
        project = projects.read_record(self.state, pid)
        self.assertNotEqual(project["card"]["card_id"], dispatch["card_id"])
        self.assertEqual(self.sent.call_count, before + 1)
        self.assertEqual(set(project["steps"]), set(upgrades.UPGRADE_STEPS))

    def test_a_card_that_was_not_sent_is_not_upgraded(self):
        topic, dispatch = self.plain()
        self.assertEqual(self.up.upgrade({**dispatch, "card_message_id": ""}, "word"), "")
        self.assertEqual(self.projects_on_disk(), [])

    def test_the_lead_calls_make_no_card_and_so_no_upgrade(self):
        self.long()
        with mock.patch.object(butler_topics, "lead_of", return_value=("a1b2c3d4e5f6", "p")):
            answer = self.start()
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(self.projects_on_disk(), [])
        self.assertEqual(self.dispatch_sends(), [])


class CrashTest(UpCase):
    """Each step runs again harmlessly: stopped before it did anything, or after it did it and before it was ticked."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(projects, "MAX_ACTIVE", 99)
        patcher.start()
        self.addCleanup(patcher.stop)

    def outcome(self, pid, topic, dispatch):
        project = projects.read_record(self.state, pid)
        saved = read_object(records.record_path(self.state, dispatch["token"]))
        return {"steps": sorted(project["steps"]), "threads": project["threads"], "card": project["card"]["message_id"],
                "sequence": project["card"]["sequence"], "settled": saved["state"], "receipts": len(self.receipts(pid)),
                "topic": load_topic(self.state, topic)["project"], "lead": load_topic(self.state, project["lead_topic_id"])["project"],
                "route": routed_topic(self.state, CHAT, dispatch["card_message_id"]), "state": project["state"],
                "projects": len(self.projects_on_disk())}

    def test_the_end_state_is_the_same_wherever_it_stopped(self):
        topic, dispatch = self.plain("clean")
        self.second_dispatch(topic, "e" * 16)
        pid = self.up.upgrade(dispatch, "word")
        clean = self.outcome(pid, topic, dispatch)
        self.assertEqual(clean["steps"], sorted(upgrades.UPGRADE_STEPS))
        for number, step in enumerate(upgrades.UPGRADE_STEPS[1:]):
            for after in (False, True):
                with self.subTest(step=step, after_the_work=after):
                    topic, dispatch = self.plain(f"{step}-{after}")
                    self.second_dispatch(topic, f"{number}{int(after)}".ljust(16, "0"))
                    original = getattr(upgrades.Projects, "step_" + step)

                    def crash(this, record, original=original, after=after):
                        if after:
                            original(this, record)
                        raise RuntimeError("boom")
                    with mock.patch.object(upgrades.Projects, "step_" + step, crash):
                        pid = self.up.upgrade(dispatch, "word")
                    stopped = projects.read_record(self.state, pid)
                    self.assertNotIn(step, stopped["steps"])
                    index = upgrades.UPGRADE_STEPS.index(step)
                    self.assertEqual(set(stopped["steps"]), set(upgrades.UPGRADE_STEPS[:index]))
                    self.assertTrue(any(f"step={step}" in line for line in self.logs))
                    self.assertEqual(self.up.upgrade(dispatch, "word"), pid)
                    done = self.outcome(pid, topic, dispatch)
                    self.assertEqual(done["steps"], clean["steps"])
                    self.assertEqual((done["threads"], done["settled"], done["receipts"], done["state"]),
                                     ([topic], "settled", 1, "active"))
                    self.assertEqual(done["topic"]["role"], "thread")
                    self.assertEqual(done["lead"]["role"], "lead")
                    self.assertEqual(done["card"], dispatch["card_message_id"])
                    self.assertEqual(done["route"], projects.read_record(self.state, pid)["lead_topic_id"])
                    self.assertEqual(done["sequence"], 0)  # Never taken back to an older number, nor moved by a repeat.
        self.assertEqual(len(self.projects_on_disk()), 1 + 2 * len(upgrades.UPGRADE_STEPS[1:]))  # One a run, none twice.

    def test_a_card_adopted_twice_does_not_take_its_sequence_back(self):
        topic, dispatch = self.plain()
        pid = self.up.upgrade(dispatch, "word")
        projects.update_card(self.state, pid, sequence=40)
        projects.update(self.state, pid, lambda record: record["steps"].pop("card"))
        self.up.upgrade(dispatch, "word")
        self.assertEqual(projects.read_record(self.state, pid)["card"]["sequence"], 40)

    def test_two_threads_of_this_process_do_not_run_the_same_projects_steps_at_once(self):
        topic, dispatch = self.plain()
        pid = self.up.upgrade(dispatch, "word")
        projects.update(self.state, pid, lambda record: record["steps"].pop("receipt"))
        with upgrades._busy_lock:
            upgrades._busy.add(pid)
        try:
            self.assertFalse(self.up.advance(pid))
        finally:
            upgrades._busy.discard(pid)
        self.assertEqual(len(self.receipts(pid)), 1)
        self.assertTrue(self.up.advance(pid))
        self.assertEqual(len(self.receipts(pid)), 1)  # The receipt was there already: not written twice.

    def test_the_thread_finishes_what_a_crash_left_and_backs_off_from_what_keeps_failing(self):
        topic, dispatch = self.plain()
        with mock.patch.object(upgrades.Projects, "step_merged", side_effect=RuntimeError("boom")):
            pid = self.up.upgrade(dispatch, "word")
        updated = projects.read_record(self.state, pid)["updated_at"]
        self.up.resume(now=updated + 5)  # Just written to: still in someone's hands.
        self.assertNotIn("merged", projects.read_record(self.state, pid)["steps"])
        with mock.patch.object(upgrades.Projects, "step_merged", side_effect=RuntimeError("boom")) as failing:
            self.up.resume(now=updated + 100)
            self.up.resume(now=updated + 101)  # Backed off.
            self.assertEqual(failing.call_count, 1)
            self.up.resume(now=updated + 100 + 2 * upgrades.RESUME_TICK + 1)
            self.assertEqual(failing.call_count, 2)
        self.up.resume(now=updated + 100 + 10 * upgrades.RESUME_TICK)
        self.assertEqual(set(projects.read_record(self.state, pid)["steps"]), set(upgrades.UPGRADE_STEPS))
        self.assertEqual(self.up.tries, {})

    def test_the_thread_runs_only_with_the_switch_on_and_leaves_a_stopped_project_alone(self):
        self.assertEqual([name for name, _ in self.up.threads()], ["projects"])
        topic, dispatch = self.plain()
        with mock.patch.object(upgrades.Projects, "step_card", side_effect=RuntimeError("boom")):
            pid = self.up.upgrade(dispatch, "word")
        projects.set_state(self.state, pid, projects.CANCELLED, "downgraded")
        self.up.resume(now=time.time() + 1000)
        self.assertNotIn("card", projects.read_record(self.state, pid)["steps"])


class DirectTest(UpCase):
    def test_topic_start_with_project_makes_a_project_card_and_a_receipt_and_opens_no_topic(self):
        answer = self.start("p1", project=True, title="迁移订单库", prompt="把订单库迁到新集群，分表要验证")
        self.assertTrue(answer["ok"], answer)
        pid = answer["data"]["project_id"]
        self.assertEqual(pid, upgrades.project_id("direct", butler_topics.request_token(SimpleNamespace(id="p1"))))
        project = projects.read_record(self.state, pid)
        self.assertEqual((project["name"], project["goal"], project["state"]), ("迁移订单库", "把订单库迁到新集群，分表要验证", "active"))
        self.assertEqual(set(project["steps"]), set(upgrades.START_STEPS))
        self.assertEqual(project["origin"]["kind"], "direct")
        self.assertEqual(project["threads"], [])
        self.assertEqual(self.sent.call_count, 1)  # The project's card, not a topic card and not a 派发卡.
        (profile, chat, card, key), kwargs = self.sent.call_args
        self.assertEqual((chat, card["header"]["title"]["content"]), (CHAT, "迁移订单库"))
        self.assertFalse(kwargs.get("reply_to_message_id"))
        ask = find_element(card, "answer_ask")
        self.assertIn(ASK, text(ask))
        self.assertEqual(ask["behaviors"][0]["default_url"], ASK_URL)
        self.assertEqual(self.dispatch_sends(), [])
        self.assertEqual(records.live_paths(self.state), [])
        (receipt,) = self.receipts(pid)
        self.assertEqual((receipt["kind"], receipt["text"]), ("start", ASK))  # The owner's request as he wrote it.
        lead = load_topic(self.state, project["lead_topic_id"])
        self.assertEqual((lead["project"]["role"], lead["workspace_name"], lead["model"]), ("lead", "project", "claude-opus-x"))
        self.assertEqual(routed_topic(self.state, CHAT, project["card"]["message_id"]), project["lead_topic_id"])
        self.assertIn("已开专项「迁移订单库」", answer["text"])

    def test_a_request_asked_twice_makes_one_project(self):
        first = self.start("p1", project=True)
        second = self.start("p1", project=True)
        self.assertEqual(first, second)
        self.assertEqual(len(self.projects_on_disk()), 1)
        self.assertEqual(self.sent.call_count, 1)
        self.assertEqual(len(self.receipts(first["data"]["project_id"])), 1)

    def test_the_fourth_active_project_is_refused_and_nothing_is_made(self):
        for number in range(projects.MAX_ACTIVE):
            projects.create(self.state, projects.new_record(chat_id=CHAT, name=f"n{number}", project_id=f"{number:012x}"))
        answer = self.start("p4", project=True)
        self.assertFalse(answer["ok"])
        self.assertIn("已经有 3 个专项在做", answer["text"])
        self.assertEqual(len(self.projects_on_disk()), 3)
        self.assertEqual(self.sent.call_count, 0)

    def test_an_unknown_workspace_is_refused_as_topic_start_refuses_it(self):
        answer = self.start("p5", project=True, workspace="nowhere")
        self.assertFalse(answer["ok"])
        self.assertIn("没有登记工作区", answer["text"])
        self.assertEqual(self.projects_on_disk(), [])

    def test_a_card_that_was_not_sent_is_made_up_for_by_the_thread(self):
        self.sent.side_effect = lambda *args, **kwargs: ("", "")
        answer = self.start("p6", project=True)
        self.assertTrue(answer["ok"])
        self.assertFalse(answer["data"]["card"])
        self.assertIn("Bridge 会自己补", answer["text"])
        pid = answer["data"]["project_id"]
        project = projects.read_record(self.state, pid)
        self.assertEqual(set(project["steps"]), {"record", "lead"})
        self.sent.side_effect = self.send_card
        self.up.resume(now=project["updated_at"] + 100)
        project = projects.read_record(self.state, pid)
        self.assertEqual(set(project["steps"]), set(upgrades.START_STEPS))
        self.assertTrue(project["card"]["message_id"])
        self.assertEqual(len(self.receipts(pid)), 1)

    def test_without_the_total_switch_or_for_a_lead_project_is_ignored_and_a_topic_opens(self):
        with mock.patch.dict(os.environ, {projects.ENV: ""}):
            answer = self.start("q1", project=True)
        self.assertTrue(answer["ok"], answer)
        self.assertIn("topic_id", answer["data"])
        self.assertEqual(self.projects_on_disk(), [])
        with mock.patch.object(butler_topics, "lead_of", return_value=("a1b2c3d4e5f6", "p")):
            answer = self.start("q2", project=True)
        self.assertIn("topic_id", answer["data"])
        self.assertEqual(self.projects_on_disk(), [])

    def test_direct_does_not_need_the_upgrade_switch(self):
        with mock.patch.dict(os.environ, {upgrades.ENV: ""}):
            answer = self.start("q3", project=True)
        self.assertIn("project_id", answer["data"])


class DowngradeTest(UpCase):
    def setUp(self):
        super().setUp()
        self.claude = self.app.apps["claude"]
        self.claude.messenger.reply = mock.Mock(return_value=True)
        self.long()
        self.topic = self.start()["data"]["topic_id"]
        self.dispatch = self.record()
        self.project = self.project_for(self.dispatch)
        self.lead = self.project["lead_topic_id"]
        self.lead_ctx = dataclasses.replace(self.ctx, topic_id=self.lead)

    def project_for(self, dispatch):
        return projects.read_record(self.state, upgrades.project_id("upgrade", dispatch["token"]))

    def click(self, **value):
        event = {"operator_id": "ou_owner", "chat_id": CHAT, "message_id": self.dispatch["card_message_id"], "event_id": "ev1",
                 "action_value": {"kind": "bridge_project_downgrade", "project_id": self.project["id"], "topic_id": self.lead, **value}}
        return self.claude.controls.project_downgrade(self.lead_ctx, event)

    def said(self):
        return self.claude.messenger.reply.call_args.args[2]

    def lead_job(self, number=5):
        job = BridgeReplyJob(job_id=number, user_text="x", chat_id=CHAT, message_id=f"lead-p-{number}", timeout=0, topic_id=self.lead)
        job.execution_started = True
        self.app.runtime.jobs.register(job)
        return job

    def test_the_project_is_cancelled_the_lead_stopped_the_thread_plain_again_and_the_card_goes_back(self):
        self.lead_job(5)
        other = self.second_dispatch(self.topic, "7" * 16, state="settled", project_id=self.project["id"])
        with mock.patch.object(self.claude.controls, "cancel_reply_job_from_card", return_value=True) as cancel:
            self.assertTrue(self.click())
        project = self.project_for(self.dispatch)
        self.assertEqual((project["state"], project["state_reason"]), ("cancelled", "downgraded"))
        self.assertEqual([(call.args[0].topic_id, call.args[1]["job_id"]) for call in cancel.call_args_list], [(self.lead, "5")])
        self.assertNotIn("project", load_topic(self.state, self.topic))
        self.assertFalse(projects.is_member_topic(self.state, self.topic))
        self.assertEqual(routed_topic(self.state, CHAT, self.dispatch["card_message_id"]), self.topic)
        self.assertEqual(message_topic(self.state, CHAT, self.dispatch["card_message_id"]), self.topic)
        self.assertEqual(routed_topic(self.state, CHAT, other["card_message_id"]), self.topic)
        self.assertIn("降回普通话题：已请负责人的 1 个任务停下，1 个话题变回普通话题", self.said())
        self.assertEqual(projects.cells(self.state, CHAT), {})  # The board cell is gone with the project.

    def test_the_card_then_shrinks_to_the_line_and_a_second_click_does_the_rest_again_harmlessly(self):
        self.assertTrue(self.click())
        self.put.reset_mock()
        self.writer.tick(self.ctx, time.time() + 5)
        self.assertIn("⏹ 专项已降为普通话题", text(self.put.call_args.args[2]))
        self.assertTrue(self.project_for(self.dispatch)["card"]["closed"])
        with mock.patch.object(self.claude.controls, "cancel_reply_job_from_card") as cancel:
            self.assertTrue(self.click())
        self.assertFalse(cancel.called)
        self.assertEqual(self.project_for(self.dispatch)["state"], "cancelled")
        self.assertIn("降回普通话题", self.said())

    def test_a_downgrade_that_stopped_half_way_is_finished_by_the_next_click(self):
        with mock.patch.object(project_cards, "release", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.click()
        self.assertEqual(self.project_for(self.dispatch)["state"], "cancelled")
        self.assertTrue(projects.is_member_topic(self.state, self.topic))  # Not yet.
        self.assertTrue(self.click())
        self.assertFalse(projects.is_member_topic(self.state, self.topic))
        self.assertEqual(routed_topic(self.state, CHAT, self.dispatch["card_message_id"]), self.topic)

    def test_a_finished_project_and_one_cancelled_another_way_are_not_downgraded(self):
        projects.set_state(self.state, self.project["id"], projects.DONE, "ok")
        self.assertTrue(self.click())
        self.assertIn("已经做完", self.said())
        self.assertEqual(self.project_for(self.dispatch)["state"], "done")
        self.assertTrue(projects.is_member_topic(self.state, self.topic))
        other = projects.create(self.state, {**projects.new_record(chat_id=CHAT, name="x", project_id="0f9e8d7c6b5a"),
                                             "state": "cancelled", "state_reason": "other"})[0]
        self.assertIn("已经取消了", project_cards.downgrade(self.claude.controls, self.ctx, other))

    def test_a_thread_of_another_project_is_left_as_it_is(self):
        self.assertTrue(project_cards.release(self.state, self.topic, "0f9e8d7c6b5a") is False)
        self.assertTrue(projects.is_member_topic(self.state, self.topic))
        self.assertTrue(project_cards.release(self.state, "ab" * 16, self.project["id"]))  # No topic.json, no project field.

    def test_a_direct_project_goes_back_to_its_first_thread_or_to_none(self):
        answer = self.start("d1", project=True)
        pid = answer["data"]["project_id"]
        record = projects.read_record(self.state, pid)
        card = record["card"]["message_id"]
        event = {"chat_id": CHAT, "message_id": card, "event_id": "ev2",
                 "action_value": {"kind": "bridge_project_downgrade", "project_id": pid, "topic_id": record["lead_topic_id"]}}
        lead_ctx = dataclasses.replace(self.ctx, topic_id=record["lead_topic_id"])
        self.assertTrue(self.claude.controls.project_downgrade(lead_ctx, event))
        self.assertIn("它没有话题，引用这张卡回复也没有话题可回", self.said())
        self.assertEqual(projects.read_record(self.state, pid)["state"], "cancelled")


class ToolTest(UpCase):
    def call(self, tool, request_id="t1", **args):
        request = Request(request_id, tool, args, "main-job", 0.0)
        return upgrades.TOOLS[tool](self.env, request)

    def upgraded(self):
        self.long()
        topic = self.start()["data"]["topic_id"]
        project = self.project(self.record())
        return topic, project

    def test_the_list_has_the_tools_only_with_the_switch_on_and_with_it_off_not_a_word_differs(self):
        base = butler_tool_specs.listed({})
        self.assertEqual(base, butler_tool_specs.TOOLS)
        for environ in ({"COVEY_PROJECTS": "0"}, {"COVEY_PROJECTS": ""}, {"COVEY_PROJECTS": "yes"}):
            self.assertEqual(butler_tool_specs.listed(environ), base)
        for switches in ({}, {"COVEY_BUTLER_RULES": "1"}, {"COVEY_KNOWLEDGE": "1"}):
            off = butler_tool_specs.listed(switches)
            on = butler_tool_specs.listed({**switches, "COVEY_PROJECTS": "1"})
            self.assertEqual([tool["name"] for tool in on], [tool["name"] for tool in off] + ["projects_list", "project_send"])
            for before, after in zip(off, on):
                if before["name"] in ("topic_start", "topic_send"):
                    self.assertEqual(after["input_schema"]["properties"]["project"]["type"], "boolean")
                    self.assertNotIn("project", before["input_schema"]["properties"])
                    self.assertNotIn("project", before["input_schema"].get("required", ()))
                    self.assertEqual(after["description"], before["description"])
                else:
                    self.assertEqual(after, before)

    def test_the_lead_still_has_only_its_own_tools(self):
        names = [tool["name"] for tool in butler_mcp.tool_list({butler.LEAD_ENV: "a1b2c3d4e5f6", "COVEY_PROJECTS": "1"})]
        self.assertNotIn("project_send", names)
        self.assertNotIn("projects_list", names)
        self.assertIn("project_note", names)

    def test_a_server_without_the_switch_refuses_the_butlers_project_tools(self):
        server = butler_mcp.Server({butler.STATE_ENV: str(self.state), butler.JOB_ENV: "k"}, submit=mock.Mock())
        answer = server.call({"name": "project_send", "arguments": {"pid": "x", "text": "y"}})
        self.assertIn("没有开 project_send", json.dumps(answer, ensure_ascii=False))

    def test_the_bridge_runs_both_tools(self):
        self.assertIn("project_send", self.app.butler_tools.handlers)
        self.assertIn("projects_list", self.app.butler_tools.handlers)
        self.assertIs(self.app.butler_env.projects, self.app.projects)
        self.assertIn(self.app.projects, self.app.features)

    def test_a_projects_thread_takes_no_topic_send_and_is_pointed_to_project_send(self):
        topic, project = self.upgraded()
        answer = butler_topics.TOOLS["topic_send"](self.env, Request("s1", "topic_send", {"topic": topic, "text": "再加一条"}, "main-job", 0.0))
        self.assertFalse(answer["ok"])
        self.assertIn("project_send", answer["text"])
        with mock.patch.object(butler_topics, "lead_of", return_value=(project["id"], "p")):  # The lead's own call is not refused.
            answer = butler_topics.TOOLS["topic_send"](self.env, Request("s2", "topic_send", {"topic": topic, "text": "加"}, "main-job", 0.0))
        self.assertNotIn("project_send", answer["text"])
        with mock.patch.dict(os.environ, {projects.ENV: ""}):  # And with the switch off it is a topic like any.
            answer = butler_topics.TOOLS["topic_send"](self.env, Request("s3", "topic_send", {"topic": topic, "text": "加"}, "main-job", 0.0))
        self.assertNotIn("project_send", answer["text"])

    def test_the_list_names_the_projects_that_are_on_and_the_last_ones_that_ended(self):
        self.assertEqual(self.call("projects_list")["text"], "还没有专项。")
        topic, project = self.upgraded()
        projects.update(self.state, project["id"], lambda record: record.update(lead_note={"text": "分表方案定了", "at": 1}))
        for number in range(5):
            record = projects.new_record(chat_id=CHAT, name=f"旧{number}", project_id=f"{number + 1:012x}", now=100 + number)
            projects.create(self.state, {**record, "state": "done"})
        answer = self.call("projects_list", "t2")
        rows = answer["text"].split("\n")
        self.assertEqual(len(rows), 1 + 3)
        self.assertTrue(rows[0].startswith(project["id"]), rows[0])
        self.assertIn("在做", rows[0])
        self.assertIn("话题 T", rows[0])
        self.assertIn("分表方案定了", rows[0])
        self.assertEqual([row.split(" · ")[2] for row in rows[1:]], ["做完"] * 3)
        with mock.patch.object(butler_topics, "lead_of", return_value=(project["id"], "p")):
            self.assertFalse(self.call("projects_list", "t3")["ok"])
        with mock.patch.dict(os.environ, {projects.ENV: ""}):
            self.assertFalse(self.call("projects_list", "t4")["ok"])

    def test_project_send_gives_a_running_lead_turn_a_supplement_and_an_idle_lead_the_next_turn_as_the_owners(self):
        topic, project = self.upgraded()
        lead = project["lead_topic_id"]
        app = upgrades.lead_target(self.env, project)[0]
        job = BridgeReplyJob(job_id=21, user_text="x", chat_id=CHAT, message_id="lead-p-1", timeout=0, topic_id=lead)
        job.execution_started = True
        job.backend_control = SimpleNamespace(steer=mock.Mock(return_value=None), remote=False)
        app.runtime.jobs.register(job)
        with mock.patch.object(app.steering, "deliver_steer", return_value={"status": "sent"}) as steer:
            answer = self.call("project_send", "send1", pid=project["id"][:6], text="先别动订单表，只做用户表")
        self.assertTrue(answer["ok"], answer)
        self.assertTrue(answer["data"]["steered"])
        sent = steer.call_args.args[2]
        self.assertIn("主人触发的一轮，不是自动唤醒", sent)
        self.assertIn("先别动订单表，只做用户表", sent)
        self.assertIn("主人原话：\n> " + LONG, sent)
        app.runtime.unregister_reply_job(job)
        answer = self.call("project_send", "send2", pid="迁", text="那就继续")
        self.assertFalse(answer["ok"])  # No project named that.
        name = project["name"][:4]
        answer = self.call("project_send", "send3", pid=name, text="那就继续")
        self.assertTrue(answer["ok"], answer)
        self.assertFalse(answer["data"]["steered"])
        mid = "main-" + butler_topics.request_token(SimpleNamespace(id="send3"))
        queued = self.app.runtime.jobs.find(0, mid) or find_job_record(self.state, mid)
        self.assertIsNotNone(queued)
        self.assertEqual(getattr(queued, "topic_id", None) or queued.get("topic_id"), lead)
        self.assertFalse(mid.startswith("lead-"))  # The owner's turn, not a wake-up.
        reply_to = queued.get("reply_to_message_id") if isinstance(queued, dict) else queued.reply_to_message_id
        # Not a reply to the Bridge's own alias: accept7 saw Feishu refuse it twice before the card went to the top level.
        self.assertEqual(reply_to, "")
        again = self.call("project_send", "send3", pid=name, text="那就继续")
        self.assertEqual(again, answer)  # Once a request.

    def test_project_send_refuses_what_it_cannot_send(self):
        topic, project = self.upgraded()
        self.assertFalse(self.call("project_send", "x1", pid="", text="y")["ok"])
        self.assertFalse(self.call("project_send", "x2", pid=project["id"], text="  ")["ok"])
        self.assertIn("没有在做的专项", self.call("project_send", "x3", pid="0f9e8d7c6b5a", text="y")["text"])
        projects.set_state(self.state, project["id"], projects.DONE)
        self.assertIn("没有在做的专项", self.call("project_send", "x4", pid=project["id"], text="y")["text"])
        with mock.patch.object(butler_topics, "lead_of", return_value=(project["id"], "p")):
            self.assertIn("负责人不用这个工具", self.call("project_send", "x5", pid=project["id"], text="y")["text"])

    def test_two_projects_with_the_same_words_in_their_names_ask_for_the_id(self):
        for number, name in enumerate(("迁移订单库", "迁移用户库"), 1):
            projects.create(self.state, projects.new_record(chat_id=CHAT, name=name, project_id=f"{number:012x}"))
        answer = self.call("project_send", "y1", pid="迁移", text="x")
        self.assertFalse(answer["ok"])
        self.assertIn("对得上不止一个专项", answer["text"])


class McpTest(UpCase):
    def test_the_switch_is_handed_on_to_the_server_only_when_it_is_on(self):
        config = butler.mcp_config(self.ctx.state_dir, self.ctx.state_dir, "k", "python3")
        self.assertEqual(config["mcpServers"]["bridge"]["env"]["COVEY_PROJECTS"], "1")
        with mock.patch.dict(os.environ, {projects.ENV: ""}):
            config = butler.mcp_config(self.ctx.state_dir, self.ctx.state_dir, "k", "python3")
        self.assertNotIn("COVEY_PROJECTS", config["mcpServers"]["bridge"]["env"])
        self.assertNotIn("LARK_BRIDGE_PROJECTS", config["mcpServers"]["bridge"]["env"])


class AcceptanceFindingsTest(DispatchCase):
    """What the first real acceptance of the projects (accept7, 2026-11-16) found, as small checks."""

    def test_opening_a_project_is_the_butlers_flag_and_a_no_is_no_long_job(self):
        # 开个专项 is topic_start's project flag (the butler set it itself in accept8); 专项 as a long word upgraded
        # "开个普通话题（不要专项）" there, so it is not one, and a no right before a long word stops it too.
        self.assertIn("开个专项", butler_tool_specs.START_PROJECT["description"])
        self.assertFalse(hasattr(records, "LONG_WORDS"))  # No keyword of the owner's is matched any more.
        for said in ("这是个长活，接着做", "这不是长活", "开个普通话题（不要专项，project 别带）"):
            hint = records.new_record(token="a" * 16, chat_id=CHAT, topic_id="t", job_key="j", mid="m", kind="start",
                                      owner_message="om", request=said, starts=True)["upgrade_hint"]
            self.assertEqual(hint, {}, said)

    def test_a_lead_turn_the_owner_did_not_start_has_no_ask_block_and_quotes_no_message(self):
        from covey.app.answer_cards import asked

        for message in (projects.wake_id(PID_X, 2), projects.LEAD_ALIAS_PREFIX + PID_X):
            self.assertTrue(projects.own_message(message))
            # Answered before the state directory is read: no record of the turn is needed.
            self.assertIsNone(asked("", {"message_id": message, "user_text": "[Bridge 唤醒] 收件箱有新的回执"}))
        self.assertFalse(projects.own_message("om_owner"))

    def test_a_leads_words_under_the_topic_lines_say_they_are_the_leads(self):
        from covey.app.butler_reply import reply_fold

        self.assertIn("负责人的回答", json.dumps(reply_fold("好", lead=True), ensure_ascii=False))
        self.assertIn("管家的回答", json.dumps(reply_fold("好"), ensure_ascii=False))


PID_X = "f00dfeed0001"

