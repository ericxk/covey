"""A project's continuing runs and guards (docs/architecture.md 专项, app/project_loop.py): what wakes the lead and what does
not, the guards that pause a project (tokens, wake-ups, stalling), the receipts the Bridge writes when a thread's turn ends
and when the owner speaks in a thread, the tokens counted once, the wake-up turn whose reply is not sent, and that with
COVEY_PROJECTS off none of it changes anything."""
from __future__ import annotations

import json
import os
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from support import make_job
from support.records import write_job, write_topic
from support.world import UnifiedCase

from covey.app import project_lead, project_loop
from covey.app.cards import Cards
from covey.app.project_cards import facts
from covey.app.project_loop import ProjectLoop
from covey.core import butler, butler_settings, placement, projects
from covey.core.jobs import job_key
from covey.core.state import write_json

from support.butler import CHAT as BUTLER_CHAT
from test_butler_quiet import DeliverCase
from test_project_lead import CHAT, LEAD, NOW, ON, PID, Case

THREAD = "b" * 32
HOUR = 3600


def lead_app(case):
    """A Harness app that queues what it is given: ``case.queued`` lists (message id, prompt) of each turn."""
    case.queued = []

    def event_context(base, event):
        topic = butler_topic(case, event["message_id"])
        return SimpleNamespace(topic_id=topic, history_path=case.state / "topics" / topic / "history.jsonl")

    def enqueue(ctx, content, chat, message, **fields):
        case.queued.append((message, fields.get("prompt_content")))
        return SimpleNamespace(job_id=len(case.queued))
    return SimpleNamespace(harness=SimpleNamespace(context=object()), controls=object(),
                           submission=SimpleNamespace(event_context=event_context, enqueue_chat_job=enqueue))


def butler_topic(case, message):
    """The topic an alias of ``message`` names, as core/topics.py context_for_topic reads it."""
    from covey.core.topics import message_topic
    return message_topic(case.state, CHAT, message)


class Loop(Case):
    """A project with its lead topic and a loop over a fake app; ``self.loop.tick(now)``."""

    def setUp(self):
        super().setUp()
        self.runtime = SimpleNamespace(log=self.logs.append, shutdown=mock.Mock())
        self.app = lead_app(self)
        self.env = SimpleNamespace(state_dir=self.state, topics=SimpleNamespace(apps={"claude": self.app}))
        self.stop = mock.Mock(return_value=0)
        self.loop = ProjectLoop(self.runtime, self.env, self.stop)
        os.environ.pop(projects.AUTOWAKE_ENV, None)

    def autowake(self):
        mock.patch.dict(os.environ, {projects.AUTOWAKE_ENV: "1"}).start()
        self.addCleanup(mock.patch.stopall)

    def receipt(self, kind="thread_end", text="T41 做完了", at=None):
        return project_lead.add_receipt(self.state, PID, kind, text, thread=THREAD, at=NOW if at is None else at)

    def update(self, **fields):
        projects.update(self.state, PID, lambda record: record.update(fields))


class WakeTest(Loop):
    def test_a_start_receipt_wakes_the_lead_without_the_switch(self):
        self.receipt("start", "接手一个已在进行的事")
        self.loop.tick(NOW + 5)
        self.assertEqual([message for message, _ in self.queued], [projects.wake_id(PID, 1)])
        self.assertEqual(self.queued[0][0], f"lead-{PID}-1")
        self.assertEqual(self.queued[0][1], project_loop.WAKE_TEXT)
        self.assertEqual(self.reload()["wakes"]["count"], 1)
        self.assertEqual(self.reload()["wakes"]["streak"], 1)

    def test_other_receipts_wake_only_with_autowake(self):
        self.receipt("thread_end")
        self.receipt("owner_said")
        self.loop.tick(NOW + 5)
        self.assertEqual(self.queued, [])
        self.autowake()
        self.loop.tick(NOW + 5)
        self.assertEqual(len(self.queued), 1)

    def test_one_wake_at_a_time(self):
        self.autowake()
        self.receipt()
        self.loop.tick(NOW + 5)
        self.loop.tick(NOW + 6)  # Its turn is not on disk in this fake: the record says queued, as the Bridge's does.
        write_job(self.state, "k-w1", topic_id=LEAD, chat_id=CHAT, job_id=1, status="queued", message_id=f"lead-{PID}-1")
        self.loop.tick(NOW + 7)
        self.assertEqual(len(self.queued), 2)  # Without the record the second tick woke again; with it the third did not.
        for status in ("running", "waiting_user"):
            write_job(self.state, "k-w1", topic_id=LEAD, chat_id=CHAT, job_id=1, status=status, message_id=f"lead-{PID}-1")
            self.loop.tick(NOW + 8)
        self.assertEqual(len(self.queued), 2)

    def test_the_message_id_counts_up_and_the_alias_makes_it_the_leads(self):
        self.autowake()
        self.receipt()
        self.loop.tick(NOW + 5)
        self.assertEqual(butler_topic(self, f"lead-{PID}-1"), LEAD)
        self.receipt(text="T42 做完了")
        self.loop.tick(NOW + 6)
        self.assertEqual([message for message, _ in self.queued], [f"lead-{PID}-1", f"lead-{PID}-2"])

    def test_receipts_a_good_turn_read_do_not_wake_but_a_broken_turns_do(self):
        self.autowake()
        self.receipt()
        write_job(self.state, "k-good", topic_id=LEAD, chat_id=CHAT, job_id=1, status="done", message_id=f"lead-{PID}-1")
        project_lead.take_receipts(self.state, PID, "k-good")
        self.loop.tick(NOW + 5)
        self.assertEqual(self.queued, [])
        item = self.receipt(text="T42 做完了")
        write_job(self.state, "k-bad", topic_id=LEAD, chat_id=CHAT, job_id=2, status="interrupted", message_id=f"lead-{PID}-2")
        project_lead.take_receipts(self.state, PID, "k-bad")
        self.assertEqual([row["id"] for row in project_loop.unread(self.state, PID)], [item["id"]])
        self.loop.tick(NOW + 6)
        self.assertEqual(len(self.queued), 1)

    def test_a_restart_finds_the_receipts_on_disk(self):
        self.autowake()
        self.receipt()
        self.queued.clear()
        ProjectLoop(self.runtime, self.env).tick(NOW + 5)  # A new loop: nothing in memory.
        self.assertEqual(len(self.queued), 1)

    def test_paused_done_and_cancelled_projects_are_left_alone(self):
        self.autowake()
        self.receipt()
        for state in (projects.PAUSED, projects.DONE):
            self.update(state=state)
            self.loop.tick(NOW + 5)
        self.assertEqual(self.queued, [])

    def test_a_wake_that_could_not_be_queued_is_counted_and_looked_for_again(self):
        self.autowake()
        self.receipt()
        self.app.submission.enqueue_chat_job = lambda *a, **k: None
        self.loop.tick(NOW + 5)
        self.assertEqual(self.reload()["wakes"]["count"], 1)
        self.assertTrue(any("not queued" in line for line in self.logs))

    def test_the_wake_is_a_turn_of_the_lead_in_its_history(self):
        self.receipt("start")
        self.loop.tick(NOW + 5)
        history = self.state / "topics" / LEAD / "history.jsonl"
        self.assertIn(project_loop.WAKE_TEXT[:12], history.read_text())

    def test_without_the_switch_there_is_no_thread_and_no_wake(self):
        self.receipt("start")
        with mock.patch.dict(os.environ, {projects.ENV: "0"}):
            self.assertEqual(self.loop.threads(), ())
            self.loop.tick(NOW + 5)
        self.assertEqual(self.queued, [])
        self.assertEqual([name for name, _ in self.loop.threads()], ["project_loop"])


class GuardTest(Loop):
    def test_tokens_over_the_budget_pause_the_project_and_wake_no_more(self):
        self.receipt("start")
        self.update(tokens_used=projects.DEFAULT_TOKENS)
        self.loop.tick(NOW + 5)
        record = self.reload()
        self.assertEqual((record["state"], record["state_reason"]), (projects.PAUSED, "tokens"))
        self.assertEqual(self.queued, [])
        self.loop.tick(NOW + 6)
        self.assertEqual(self.queued, [])

    def test_the_budget_is_the_owners(self):
        self.update(tokens_used=500)
        with mock.patch.dict(os.environ, {projects.BUDGET_ENV: "400"}):
            self.assertEqual(projects.token_limit(), 400)
            self.loop.tick(NOW + 5)
        self.assertEqual(self.reload()["state_reason"], "tokens")
        for value in ("", "x", "0", "-5"):
            with mock.patch.dict(os.environ, {projects.BUDGET_ENV: value}):
                self.assertEqual(projects.token_limit(), projects.DEFAULT_TOKENS)

    def test_a_launch_keeps_the_budget_and_the_older_name_still_counts(self):
        # The acceptance of 2026-11-17: COVEY_PROJECT_TOKENS has TOKEN in its name, the service manifest took it for a
        # secret and dropped it, so the budget a launch set never reached the Bridge.
        from covey.service import manager
        self.assertTrue(manager.persistable(projects.BUDGET_ENV))
        self.assertTrue([k for k in manager.runtime_environment({projects.BUDGET_ENV: "400"}) if k.endswith("PROJECT_BUDGET")])
        with mock.patch.dict(os.environ, {projects.TOKENS_ENV: "300"}):
            self.assertEqual(projects.token_limit(), 300)
            with mock.patch.dict(os.environ, {projects.BUDGET_ENV: "400"}):
                self.assertEqual(projects.token_limit(), 400)

    def test_thirty_wakes_in_a_row_pause_the_project(self):
        self.autowake()
        self.update(wakes={"count": 30, "streak": 30, "last_at": NOW, "recent": []})
        self.receipt()
        self.loop.tick(NOW + 5)
        self.assertEqual((self.reload()["state"], self.reload()["state_reason"]), (projects.PAUSED, "wakes"))
        self.assertEqual(self.queued, [])

    def test_twelve_wakes_in_an_hour_pause_it_but_not_after_the_hour(self):
        self.autowake()
        self.update(wakes={"count": 12, "streak": 12, "last_at": NOW, "recent": [NOW + i for i in range(12)]})
        self.receipt()
        self.loop.tick(NOW + 100)
        self.assertEqual(self.reload()["state_reason"], "wakes")
        self.update(state=projects.ACTIVE, state_reason="")
        self.loop.tick(NOW + 2 * HOUR)
        self.assertEqual(len(self.queued), 1)

    def test_the_owners_turn_clears_the_counts_and_starts_a_paused_project(self):
        self.update(wakes={"count": 30, "streak": 30, "last_at": NOW, "recent": [NOW]}, state=projects.PAUSED,
                    state_reason="wakes", stall={"at": NOW, "activity": NOW})
        project_loop.owner_turn(self.state, LEAD, CHAT, "om_1")
        record = self.reload()
        self.assertEqual((record["state"], record["state_reason"]), (projects.ACTIVE, ""))
        self.assertEqual((record["wakes"]["streak"], record["wakes"]["recent"], record["wakes"]["count"]), (0, [], 30))
        self.assertNotIn("stall", record)

    def test_a_wake_up_turn_is_not_the_owners(self):
        self.update(wakes={"count": 3, "streak": 3, "last_at": NOW, "recent": []}, state=projects.PAUSED, state_reason="wakes")
        project_loop.owner_turn(self.state, LEAD, CHAT, f"lead-{PID}-4")
        self.assertEqual((self.reload()["state"], self.reload()["wakes"]["streak"]), (projects.PAUSED, 3))

    def test_the_owners_turn_starts_what_the_owner_stopped_but_not_a_finished_project(self):
        self.update(state=projects.PAUSED, state_reason=projects.OWNER_STOP)
        project_loop.owner_turn(self.state, LEAD, CHAT, "main-x")
        self.assertEqual(self.reload()["state"], projects.ACTIVE)
        self.update(state=projects.DONE, state_reason="完成")
        project_loop.owner_turn(self.state, LEAD, CHAT, "om_2")
        self.assertEqual(self.reload()["state"], projects.DONE)

    def test_after_a_tokens_pause_the_owners_turn_starts_a_new_budget(self):
        self.update(tokens_used=projects.DEFAULT_TOKENS, state=projects.PAUSED, state_reason="tokens")
        project_loop.owner_turn(self.state, LEAD, CHAT, "om_3")
        self.loop.tick(NOW + 5)
        self.assertEqual(self.reload()["state"], projects.ACTIVE)
        self.update(tokens_used=2 * projects.DEFAULT_TOKENS)
        self.loop.tick(NOW + 6)
        self.assertEqual(self.reload()["state_reason"], "tokens")

    def test_the_owners_turn_in_another_topic_changes_nothing(self):
        self.update(state=projects.PAUSED, state_reason="wakes")
        project_loop.owner_turn(self.state, THREAD, CHAT, "om_4")
        self.assertEqual(self.reload()["state"], projects.PAUSED)


class PauseAskTest(Loop):
    """COVEY_PROJECT_ASK=1 (the owner is asked what to do with a pause a guard made, app/project_cards.py sends the card); with
    it off the record is what it always was."""

    def ask_on(self):
        patcher = mock.patch.dict(os.environ, {projects.ASK_ENV: "1"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def paused(self, reason, **fields):
        self.update(state=projects.PAUSED, state_reason=reason, **fields)
        self.update(pause_ask={"reason": reason, "why": "为了测试", "at": NOW, "choice": "", "halted": 0,
                               "card": {"card_id": "c1", "message_id": "om_ask", "sequence": 0}})

    def test_off_a_guards_pause_leaves_no_question_and_stops_no_task(self):
        self.update(tokens_used=projects.DEFAULT_TOKENS, threads=[THREAD])
        self.loop.tick(NOW + 5)
        self.assertEqual((self.reload()["state"], self.reload()["state_reason"]), (projects.PAUSED, "tokens"))
        self.assertNotIn("pause_ask", self.reload())
        self.stop.assert_not_called()

    def test_a_tokens_pause_asks_in_the_owners_words_and_stops_the_member_topics_tasks(self):
        self.ask_on()
        self.update(tokens_used=projects.DEFAULT_TOKENS, threads=[THREAD])
        self.stop.return_value = 2
        self.loop.tick(NOW + 5)
        ask = self.reload()["pause_ask"]
        self.assertEqual((ask["reason"], ask["why"], ask["choice"], ask["halted"]), ("tokens", "token 用到上限 1000 万", "", 2))
        self.assertEqual(self.stop.call_args.args[2:4], ([THREAD], CHAT))  # The member topics, not the lead.

    def test_a_wakes_pause_leaves_the_running_topics_alone(self):
        self.ask_on()
        self.autowake()
        self.update(wakes={"count": 30, "streak": 30, "last_at": NOW, "recent": []}, threads=[THREAD])
        self.receipt()
        self.loop.tick(NOW + 5)
        self.assertEqual(self.reload()["pause_ask"]["why"], "负责人连续叫醒 30 次没有你的消息")
        self.stop.assert_not_called()
        self.update(state=projects.ACTIVE, state_reason="", wakes={"count": 12, "streak": 3, "last_at": NOW, "recent": [NOW] * 12})
        self.loop.tick(NOW + 100)
        self.assertEqual(self.reload()["pause_ask"]["why"], "负责人一小时内叫醒了 12 次")

    def test_a_stall_pause_says_how_long_nothing_moved(self):
        self.ask_on()
        self.autowake()
        self.update(created_at=NOW)
        self.thread(THREAD, "迁移", 41, status="queued")  # Something to do that does not move.
        self.loop.tick(NOW + 31 * 60)
        project_lead.take_receipts(self.state, PID, "k-w1")
        write_job(self.state, "k-w1", topic_id=LEAD, chat_id=CHAT, job_id=1, status="done", message_id=f"lead-{PID}-1")
        self.loop.tick(NOW + 62 * 60)
        self.assertEqual(self.reload()["pause_ask"]["why"], "62 分钟没有进展")
        self.stop.assert_not_called()  # Nothing was running.

    def test_the_stall_time_can_be_set_for_a_trial(self):
        self.ask_on()
        self.autowake()
        self.update(created_at=NOW)
        self.thread(THREAD, "迁移", 41, status="queued")
        self.assertEqual(projects.stall_seconds(), projects.STALL_SECONDS)
        with mock.patch.dict(os.environ, {projects.STALL_ENV: "60"}):
            self.assertEqual(projects.stall_seconds(), 60)
            self.loop.tick(NOW + 61)
            self.assertEqual([item["kind"] for item in project_loop.unread(self.state, PID)], ["stalled"])
        for value in ("", "x", "0", "-5"):
            with mock.patch.dict(os.environ, {projects.STALL_ENV: value}):
                self.assertEqual(projects.stall_seconds(), projects.STALL_SECONDS)

    def test_a_project_the_owner_stopped_meanwhile_is_not_asked_about(self):
        self.ask_on()
        self.update(tokens_used=projects.DEFAULT_TOKENS)
        record = self.reload()
        self.update(state=projects.PAUSED, state_reason=projects.OWNER_STOP)  # The loop's look is of the old record.
        self.loop.pause(record, "tokens", NOW + 5)
        self.assertNotIn("pause_ask", self.reload())

    def test_continue_starts_again_clears_the_counts_and_gives_a_tokens_pause_a_new_budget(self):
        self.paused("tokens", tokens_used=projects.DEFAULT_TOKENS + 7, wakes={"count": 30, "streak": 30, "last_at": NOW, "recent": [NOW]})
        self.assertEqual(project_loop.answer_pause(self.state, PID, "continue", "om_ask"), "ok")
        record = self.reload()
        self.assertEqual((record["state"], record["state_reason"], record["token_floor"]), (projects.ACTIVE, "", projects.DEFAULT_TOKENS + 7))
        self.assertEqual((record["wakes"]["streak"], record["wakes"]["recent"]), (0, []))
        self.assertEqual(record["pause_ask"]["choice"], "continue")
        project_loop.pause_receipt(self.state, PID, "continue", self.reload()["pause_ask"])
        self.loop.tick(NOW + 5)  # The receipt wakes the lead without COVEY_PROJECT_AUTOWAKE.
        self.assertEqual(len(self.queued), 1)
        self.assertEqual(self.reload()["state"], projects.ACTIVE)

    def test_finish_starts_it_again_and_asks_the_lead_for_the_summary(self):
        self.paused("stalled", stall={"at": NOW, "activity": NOW})
        self.assertEqual(project_loop.answer_pause(self.state, PID, "finish"), "ok")
        project_loop.pause_receipt(self.state, PID, "finish", self.reload()["pause_ask"])
        [item] = project_loop.unread(self.state, PID)
        self.assertEqual(item["kind"], "owner_choice")
        self.assertIn("project_finish", item["text"])
        self.assertNotIn("stall", self.reload())
        self.assertEqual(self.reload()["state"], projects.ACTIVE)

    def test_a_tokens_receipt_says_the_stopped_topics_do_not_start_by_themselves(self):
        self.paused("tokens")
        projects.update(self.state, PID, lambda record: record["pause_ask"].update(halted=2))
        project_loop.pause_receipt(self.state, PID, "continue", self.reload()["pause_ask"])
        [item] = project_loop.unread(self.state, PID)
        self.assertIn("2 个话题的任务被请停了", item["text"])
        self.assertIn("project_topic_send", item["text"])

    def test_stop_keeps_it_paused_as_the_owners_stop_and_leaves_no_receipt(self):
        self.paused("wakes")
        self.assertEqual(project_loop.answer_pause(self.state, PID, "stop", "om_ask"), "ok")
        record = self.reload()
        self.assertEqual((record["state"], record["state_reason"]), (projects.PAUSED, projects.OWNER_STOP))
        self.assertNotIn("token_floor", record)
        project_loop.pause_receipt(self.state, PID, "stop", self.reload()["pause_ask"])
        self.assertEqual(project_loop.unread(self.state, PID), [])

    def test_a_stop_after_a_tokens_pause_gives_the_budget_the_later_start_needs(self):
        self.paused("tokens", tokens_used=projects.DEFAULT_TOKENS + 5)
        self.assertEqual(project_loop.answer_pause(self.state, PID, "stop"), "ok")
        project_loop.owner_turn(self.state, LEAD, CHAT, "om_later")  # The owner speaks to the lead some time after.
        self.assertEqual((self.reload()["state"], self.reload()["token_floor"]), (projects.ACTIVE, projects.DEFAULT_TOKENS + 5))
        self.loop.tick(NOW + 5)
        self.assertEqual(self.reload()["state"], projects.ACTIVE)  # Not paused again at once.

    def test_a_second_click_another_pauses_card_and_a_project_that_moved_on_change_nothing(self):
        self.paused("tokens")
        self.assertEqual(project_loop.answer_pause(self.state, PID, "continue", "om_old"), "stale")
        self.assertEqual(self.reload()["state"], projects.PAUSED)
        self.assertEqual(project_loop.answer_pause(self.state, PID, "continue", "om_ask"), "ok")
        self.assertEqual(project_loop.answer_pause(self.state, PID, "stop", "om_ask"), "answered")
        self.assertEqual(self.reload()["state"], projects.ACTIVE)
        self.paused("tokens")
        self.update(state=projects.ACTIVE, state_reason="")  # The owner's own turn got there first.
        self.assertEqual(project_loop.answer_pause(self.state, PID, "stop"), "moved")
        self.assertEqual(self.reload()["state"], projects.ACTIVE)
        projects.update(self.state, PID, lambda record: record.pop("pause_ask"))
        self.assertEqual(project_loop.answer_pause(self.state, PID, "stop"), "none")

    def test_a_start_or_owner_choice_receipt_wakes_without_the_switch_and_a_thread_end_does_not(self):
        self.assertTrue(project_loop.autowake_due([{"kind": "owner_choice"}]))
        self.assertTrue(project_loop.autowake_due([{"kind": "start"}]))
        self.assertFalse(project_loop.autowake_due([{"kind": "thread_end"}]))

    def test_the_words_of_a_count(self):
        self.assertEqual([projects.count_words(n) for n in (8000, 10_000_000, 25_000_000, 150_000_000, 20_000)],
                         ["8000", "1000 万", "2500 万", "1.5 亿", "2 万"])


class IdleTest(Loop):
    """A project with nothing to do and nothing said for COVEY_PROJECT_IDLE_DAYS days is folded away (PAUSED + ``idle``), and its
    word, new work or the owner's turn open it again; only a project with something to do can stall."""
    DAY = 86400

    def setUp(self):
        super().setUp()
        self.update(created_at=NOW)

    def state_of(self):
        record = self.reload()
        return record["state"], record["state_reason"]

    def test_nothing_to_do_for_seven_days_folds_it_without_a_question_or_a_receipt(self):
        mock.patch.dict(os.environ, {projects.ASK_ENV: "1", projects.AUTOWAKE_ENV: "1"}).start()
        self.addCleanup(mock.patch.stopall)
        self.loop.tick(NOW + 7 * self.DAY - 60)
        self.assertEqual(self.state_of(), (projects.ACTIVE, ""))
        self.loop.tick(NOW + 7 * self.DAY + 60)
        self.assertEqual(self.state_of(), (projects.PAUSED, projects.IDLE))
        record = self.reload()
        self.assertEqual(record["idle"], {"at": NOW + 7 * self.DAY + 60, "days": 7})
        self.assertNotIn("pause_ask", record)  # No pause card, no question.
        self.assertEqual(project_loop.unread(self.state, PID), [])  # And no reminder on the way.
        self.assertEqual(self.queued, [])
        self.assertTrue(projects.folded(record))

    def test_the_days_can_be_set_down_to_a_fraction_for_a_trial_and_a_bad_value_is_seven(self):
        self.assertEqual(projects.idle_days(), 7)
        with mock.patch.dict(os.environ, {projects.IDLE_ENV: "2"}):
            self.loop.tick(NOW + 2 * self.DAY + 1)
        self.assertEqual((self.state_of(), self.reload()["idle"]["days"]), ((projects.PAUSED, projects.IDLE), 2))
        self.update(state=projects.ACTIVE, state_reason="", created_at=NOW)
        with mock.patch.dict(os.environ, {projects.IDLE_ENV: "0.001"}):  # About 86 seconds.
            self.loop.tick(NOW + 80)
            self.assertEqual(self.state_of(), (projects.ACTIVE, ""))
            self.loop.tick(NOW + 90)
            self.assertEqual((self.state_of(), self.reload()["idle"]["days"]), ((projects.PAUSED, projects.IDLE), 0.001))
        with mock.patch.dict(os.environ, {projects.IDLE_ENV: "1.5"}):
            self.assertEqual(projects.idle_seconds(), 1.5 * self.DAY)
        for value in ("", "x", "0", "-3", "nan", "inf", "1e9"):
            with mock.patch.dict(os.environ, {projects.IDLE_ENV: value}):
                self.assertEqual((projects.idle_days(), projects.idle_seconds()), (7, 7 * self.DAY))

    def test_anything_to_do_keeps_it_open(self):
        for name, setup in (("a queued task", lambda: self.thread(THREAD, "迁移", 41, status="queued")),
                            ("a task waiting for the owner", lambda: self.thread(THREAD, "迁移", 41, status="waiting_user")),
                            ("a receipt nobody read", lambda: self.receipt("thread_end", "T41 做完了", at=NOW)),
                            ("a question waiting", lambda: write_json(self.state / "questions" / "q1.json", {
                                "question_id": "q1", "topic_id": THREAD, "chat_id": CHAT, "status": "pending", "created_at": NOW}))):
            with self.subTest(name):
                self.update(state=projects.ACTIVE, state_reason="")
                setup()
                self.loop.tick(NOW + 30 * self.DAY)
                self.assertEqual(self.state_of(), (projects.ACTIVE, ""))

    def test_a_task_that_ended_or_a_word_from_the_owner_counts_from_then(self):
        self.thread(THREAD, "迁移", 41, status="done", timings={"execution_finished": NOW + 5 * self.DAY})
        self.loop.tick(NOW + 8 * self.DAY)
        self.assertEqual(self.state_of(), (projects.ACTIVE, ""))
        self.loop.tick(NOW + 12 * self.DAY + 60)
        self.assertEqual(self.state_of(), (projects.PAUSED, projects.IDLE))

    def test_the_owners_turn_the_butlers_send_and_his_word_in_a_topic_of_it_open_it_again(self):
        self.thread(THREAD, "迁移", 41, status="done", timings={"execution_finished": NOW})
        for opener in (lambda: project_loop.owner_turn(self.state, LEAD, CHAT, "om_1"),  # What project_send's turn goes through too.
                       lambda: project_loop.heard(self.state, THREAD, "迁移再看看")):
            self.update(state=projects.PAUSED, state_reason=projects.IDLE, idle={"at": NOW, "days": 7})
            opener()
            self.assertEqual(self.state_of(), (projects.ACTIVE, ""))
            self.assertNotIn("idle", self.reload())

    def test_a_project_just_opened_again_is_not_folded_before_something_has_happened(self):
        self.update(state=projects.PAUSED, state_reason=projects.IDLE, idle={"at": NOW, "days": 7})
        with mock.patch("covey.app.project_loop.time.time", return_value=NOW + 20 * self.DAY):
            project_loop.owner_turn(self.state, LEAD, CHAT, "om_1")
        self.loop.tick(NOW + 20 * self.DAY + 5)
        self.assertEqual(self.state_of(), (projects.ACTIVE, ""))
        self.loop.tick(NOW + 27 * self.DAY + 10)
        self.assertEqual(self.state_of(), (projects.PAUSED, projects.IDLE))

    def test_a_guards_pause_is_not_reopened_by_the_idle_logic_and_the_loop_skips_a_folded_project(self):
        self.update(state=projects.PAUSED, state_reason="stalled")
        self.loop.tick(NOW + 30 * self.DAY)
        self.assertEqual(self.state_of(), (projects.PAUSED, "stalled"))
        self.assertNotIn("idle", self.reload())

    def test_nothing_to_do_never_stalls_but_something_that_does_not_move_does(self):
        self.autowake()
        self.loop.tick(NOW + 31 * 60)
        self.loop.tick(NOW + 3 * HOUR)
        self.assertEqual(project_loop.unread(self.state, PID), [])
        self.assertEqual(self.state_of(), (projects.ACTIVE, ""))
        self.thread(THREAD, "迁移", 41, status="queued")
        self.loop.tick(NOW + 3 * HOUR + 10)
        self.assertEqual([item["kind"] for item in project_loop.unread(self.state, PID)], ["stalled"])

    def test_waiting_for_the_owner_neither_stalls_nor_folds_away(self):
        # A question the owner has not answered is theirs to move: no reminder for the lead, no pause card, and not idle either.
        self.autowake()
        self.thread(THREAD, "迁移", 41, status="waiting_user")
        for at in (NOW + 31 * 60, NOW + 3 * HOUR, NOW + 30 * self.DAY):
            self.loop.tick(at)
        self.assertEqual(project_loop.unread(self.state, PID), [])
        self.assertEqual(self.state_of(), (projects.ACTIVE, ""))


class StallTest(Loop):
    def setUp(self):
        super().setUp()
        self.autowake()
        self.update(created_at=NOW)
        self.thread(THREAD, "迁移", 41, status="queued")  # Something to do that does not move: only that stalls.

    def test_a_reminder_after_half_an_hour_then_a_pause_after_another(self):
        self.loop.tick(NOW + 29 * 60)
        self.assertEqual(project_loop.unread(self.state, PID), [])
        self.loop.tick(NOW + 31 * 60)
        [item] = project_loop.unread(self.state, PID)
        self.assertEqual(item["kind"], "stalled")
        self.loop.tick(NOW + 31 * 60 + 10)
        self.assertEqual(self.queued[0][0], f"lead-{PID}-1")  # The reminder wakes the lead (COVEY_PROJECT_AUTOWAKE=1).
        project_lead.take_receipts(self.state, PID, "k-w1")
        write_job(self.state, "k-w1", topic_id=LEAD, chat_id=CHAT, job_id=1, status="done", message_id=f"lead-{PID}-1")
        self.loop.tick(NOW + 50 * 60)
        self.assertEqual(self.reload()["state"], projects.ACTIVE)
        self.loop.tick(NOW + 62 * 60)
        record = self.reload()
        self.assertEqual((record["state"], record["state_reason"]), (projects.PAUSED, "stalled"))
        self.assertEqual(len(self.queued), 1)

    def test_nothing_without_the_switch(self):
        mock.patch.stopall()
        self.loop.tick(NOW + 31 * 60)
        self.assertEqual(project_loop.unread(self.state, PID), [])

    def test_a_running_thread_is_not_a_stall(self):
        self.thread(THREAD, "迁移", 41, status="running")
        self.loop.tick(NOW + 5 * HOUR)
        self.assertEqual(project_loop.unread(self.state, PID), [])
        self.assertEqual(self.reload()["state"], projects.ACTIVE)

    def test_a_task_ending_after_the_reminder_starts_the_count_again(self):
        self.loop.tick(NOW + 31 * 60)
        project_lead.take_receipts(self.state, PID, "k-w1")
        write_job(self.state, "k-w1", topic_id=LEAD, chat_id=CHAT, job_id=1, status="done", message_id=f"lead-{PID}-1")
        self.thread(THREAD, "迁移", 41, status="done", timings={"execution_finished": NOW + 45 * 60})
        self.loop.tick(NOW + 70 * 60)
        self.assertNotIn("stall", self.reload())
        self.assertEqual(self.reload()["state"], projects.ACTIVE)

    def test_the_wake_up_turn_itself_is_no_activity(self):
        self.loop.tick(NOW + 31 * 60)
        write_job(self.state, "k-w1", topic_id=LEAD, chat_id=CHAT, job_id=1, status="done", message_id=f"lead-{PID}-1",
                  timings={"execution_finished": NOW + 35 * 60})
        self.assertLess(project_loop.last_activity(self.state, self.reload(), []), NOW + 35 * 60)


class ReceiptTest(Case):
    def job(self, topic, key="k-t", status="done", **fields):
        job = make_job(job_id=41, chat_id=CHAT, message_id="om_t1", topic_id=topic)
        write_job(self.state, job_key(job.message_id, job.job_id), topic_id=topic, chat_id=CHAT, job_id=41, message_id="om_t1",
                  status=status, input_tokens=1000, output_tokens=200,
                  subtask_usage={"count": 1, "levels": [{"input_tokens": 300, "output_tokens": 50}]}, **fields)
        return job

    def after(self, job):
        project_loop.after_runner(self.logs.append, self.state, job)

    def test_a_threads_end_is_a_receipt_with_its_conclusion_and_its_tokens_are_counted(self):
        self.thread(THREAD, "迁移", 41)
        job = self.job(THREAD, final_text="已迁完\n全部 12 张表对账一致")
        self.after(job)
        [item] = project_loop.unread(self.state, PID)
        self.assertEqual((item["kind"], item["thread"]), ("thread_end", THREAD))
        self.assertIn("任务 #41 完成", item["text"])
        self.assertIn("全部 12 张表对账一致", item["text"])
        self.assertEqual(self.reload()["tokens_used"], 1550)

    def test_counted_and_told_once(self):
        self.thread(THREAD, "迁移", 41)
        job = self.job(THREAD, final_text="好了")
        self.after(job)
        self.after(job)
        self.assertEqual(len(project_loop.unread(self.state, PID)), 1)
        self.assertEqual(self.reload()["tokens_used"], 1550)

    def test_a_failed_or_stopped_turn_says_so(self):
        self.thread(THREAD, "迁移", 41)
        self.after(self.job(THREAD, status="cancelled"))
        [item] = project_loop.unread(self.state, PID)
        self.assertIn("已停止", item["text"])

    def test_a_turn_waiting_for_the_owner_counts_its_tokens_but_leaves_no_receipt(self):
        self.thread(THREAD, "迁移", 41)
        self.after(self.job(THREAD, status="waiting_user"))
        self.assertEqual(project_loop.unread(self.state, PID), [])
        self.assertEqual(self.reload()["tokens_used"], 1550)

    def test_an_interrupted_turn_leaves_no_receipt_and_a_running_one_nothing(self):
        self.thread(THREAD, "迁移", 41)
        self.after(self.job(THREAD, status="interrupted"))
        self.assertEqual(project_loop.unread(self.state, PID), [])
        write_job(self.state, "k-r", topic_id=THREAD, chat_id=CHAT, job_id=42, message_id="om_r", status="running", input_tokens=9)
        self.after(make_job(job_id=42, chat_id=CHAT, message_id="om_r", topic_id=THREAD))
        self.assertEqual(self.reload()["tokens_used"], 1550)

    def test_the_leads_own_turn_counts_its_tokens_and_is_told_to_no_one(self):
        job = self.job(LEAD)
        self.after(job)
        self.assertEqual(project_loop.unread(self.state, PID), [])
        self.assertEqual(self.reload()["tokens_used"], 1550)

    def test_another_topic_and_a_missing_project_change_nothing(self):
        write_topic(self.state, topic_id="c" * 32, chat_id=CHAT, harness_id="claude")
        self.after(self.job("c" * 32))
        self.assertNotIn("tokens_used", self.reload())
        self.logs.clear()
        write_topic(self.state, topic_id="d" * 32, chat_id=CHAT, harness_id="claude", project=projects.project_field("zzzzzz1", "thread"))
        self.after(self.job("d" * 32))
        self.assertEqual(self.logs, [])

    def test_with_the_switch_off_nothing_is_written(self):
        self.thread(THREAD, "迁移", 41)
        with mock.patch.dict(os.environ, {projects.ENV: "0"}):
            self.after(self.job(THREAD, final_text="好了"))
            project_loop.heard(self.state, THREAD, "加一张表")
            project_loop.owner_turn(self.state, LEAD, CHAT, "om_1")
        self.assertFalse((projects.folder(self.state) / PID / "inbox").exists())
        self.assertNotIn("tokens_used", self.reload())

    def test_the_owner_speaking_in_a_thread_is_a_receipt_and_steps_in(self):
        self.thread(THREAD, "迁移", 41)
        self.update_wakes(streak=7)
        project_loop.heard(self.state, THREAD, "先别动 orders 表")
        [item] = project_loop.unread(self.state, PID)
        self.assertEqual((item["kind"], item["thread"]), ("owner_said", THREAD))
        self.assertIn("先别动 orders 表", item["text"])
        self.assertEqual(self.reload()["wakes"]["streak"], 0)

    def test_the_owner_speaking_elsewhere_is_no_receipt(self):
        write_topic(self.state, topic_id="c" * 32, chat_id=CHAT, harness_id="claude")
        project_loop.heard(self.state, "c" * 32, "你好")
        project_loop.heard(self.state, LEAD, "你好")  # The lead's own topic: its turn reads the message itself.
        self.assertEqual(project_loop.unread(self.state, PID), [])

    def update_wakes(self, **fields):
        projects.update(self.state, PID, lambda record: record.__setitem__(
            "wakes", {**project_loop.wakes_of(record), **fields}))


class SilentTest(DeliverCase):
    """The wake-up turn's reply stays saved and counts as delivered, unless the lead reported in it."""

    def setUp(self):
        super().setUp()
        mock.patch.dict(os.environ, ON).start()
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(butler.configure_state, butler.configure_state(self.state))
        write_json(self.state / butler_settings.FILE, {"mode": "agent"})
        butler.forget_mode()
        self.lead_topic = projects.lead_topic_id(BUTLER_CHAT, PID)
        projects.create(self.state, projects.new_record(chat_id=BUTLER_CHAT, name="迁移账单", project_id=PID, now=NOW))
        write_topic(self.state, topic_id=self.lead_topic, chat_id=BUTLER_CHAT, harness_id="claude",
                    project=projects.project_field(PID, projects.LEAD))

    def lead(self, message=f"lead-{PID}-1"):
        return self.job(topic=self.lead_topic, message=message)

    def note(self, key, **fields):
        row = {"id": "n1", "at": NOW, "text": "第一批迁完", "report": True, "kind": "milestone", "job": key, **fields}
        projects.update(self.state, PID, lambda record: record.update(notes=[row]))

    def test_a_wake_up_turn_is_quiet_and_never_shows_a_card(self):
        job = self.lead()
        self.assertTrue(placement.quiet(self.state, job))
        self.assertFalse(placement.marked(self.state, job))
        self.assertFalse(placement.quiet(self.state, self.job(topic=self.lead_topic, message="main-tool-1")))
        cards = Cards.__new__(Cards)
        cards.runtime = mock.Mock()
        cards.create_streaming_reply = mock.Mock(return_value=SimpleNamespace(card_id="c9", message_id="om_card"))
        job = self.lead()
        job.card_deferred = True
        job.timings["backend_requested"] = time.time() - 600
        self.assertFalse(cards.reveal(self.ctx, job, "x"))
        cards.create_streaming_reply.assert_not_called()
        job = self.job(topic=self.lead_topic, message="om_1", card_deferred=True)
        job.timings["backend_requested"] = time.time() - 600
        self.assertTrue(cards.reveal(self.ctx, job, "x"))  # An owner's turn in the lead is Cards.reveal's as ever.

    def test_the_reply_is_not_sent_and_counts_as_delivered(self):
        delivery = self.deliver(self.lead(), reply="收到，等线程。")
        self.assertTrue(delivery.silent)
        self.send_text.assert_not_called()
        self.artifacts.send_result_file.assert_not_called()
        states = [call for call in delivery.record_job_state.call_args_list]
        self.assertTrue(all(call.kwargs.get("delivery_ok") is not False for call in states[1:]), states)
        self.assertTrue(states[-1].kwargs["delivery_ok"])

    def test_a_report_note_its_style_lets_through_sends_the_reply_to_the_top_level(self):
        job = self.lead()
        self.note(job_key(job.message_id, job.job_id))
        delivery = self.deliver(job, reply="第一批迁完了。")
        self.assertFalse(delivery.silent)
        self.send_text.assert_called_once()
        self.assertEqual(self.send_text.call_args.args[0].reply_to_message_id, "")

    def test_a_report_note_the_style_holds_back_is_not_sent_either(self):
        job = self.lead()
        self.note(job_key(job.message_id, job.job_id), kind="thread")
        delivery = self.deliver(job, reply="线程做完了。")
        self.assertTrue(delivery.silent)
        self.send_text.assert_not_called()

    def test_a_note_that_is_not_a_report_or_is_another_turns_changes_nothing(self):
        job = self.lead()
        self.note(job_key(job.message_id, job.job_id), report=False)
        self.assertTrue(self.deliver(job).silent)
        self.note("other-key")
        self.assertTrue(self.deliver(job).silent)

    def test_the_owners_turn_in_the_lead_is_sent_as_before(self):
        delivery = self.deliver(self.lead("om_9"), reply="好的，我来拆。")
        self.assertFalse(delivery.silent)
        self.send_text.assert_called_once()

    def test_a_failed_wake_up_turn_is_not_hidden(self):
        delivery = self.deliver(self.lead(), status="failed", reply="出错了")
        self.assertFalse(delivery.silent)
        self.send_text.assert_called()

    def test_without_the_switch_a_message_id_of_that_shape_is_nothing_special(self):
        with mock.patch.dict(os.environ, {projects.ENV: "0"}):
            self.assertFalse(project_loop.silent(SimpleNamespace(job=self.lead(), turn=SimpleNamespace(), ctx=self.ctx,
                                                                 runtime=mock.Mock())))


class CardWordsTest(Loop):
    def test_a_paused_project_says_why(self):
        for reason, words in (("tokens", "token 用完了"), ("wakes", "连续唤醒次数到上限"), ("stalled", "很久没有动静")):
            self.update(state=projects.PAUSED, state_reason=reason)
            self.assertEqual(facts(self.state, self.reload(), NOW).lead_state, f"停住 · {words}")
        self.update(state=projects.PAUSED, state_reason="主人停止")
        self.assertEqual(facts(self.state, self.reload(), NOW).lead_state, "待命")
        self.update(state=projects.ACTIVE, state_reason="tokens")
        self.assertEqual(projects.reason_words(self.reload()), "")


class WiringTest(UnifiedCase):
    """The loop is one of the Bridge's feature objects, and the Bridge's own places call its hooks."""

    def setUp(self):
        super().setUp()
        mock.patch.dict(os.environ, ON).start()
        self.addCleanup(mock.patch.stopall)
        self.state = self.ctx.state_dir

    def thread_topic(self):
        """A topic of chat 'chat' that is a thread of a project, as it is once the lead has opened it."""
        self.receive("om_t", "迁移第一张表")
        topic = self.route("om_t")[1].topic_id
        projects.create(self.state, projects.new_record(chat_id="chat", name="迁移账单", project_id=PID, now=NOW))
        data = json.loads((self.state / "topics" / topic / "topic.json").read_text())
        write_json(self.state / "topics" / topic / "topic.json", {**data, "project": projects.project_field(PID, "thread")})
        projects.add_member(self.state, PID, topic)
        return topic

    def test_the_thread_exists_only_with_the_switch(self):
        self.assertIn("project_loop", dict(self.app.worker_targets()))
        with mock.patch.dict(os.environ, {projects.ENV: "0"}):
            self.assertNotIn("project_loop", dict(self.app.worker_targets()))

    def test_the_owner_writing_in_a_thread_leaves_a_receipt(self):
        topic = self.thread_topic()
        self.receive("om_t2", "先别动 orders 表", root="om_t")
        [item] = project_loop.unread(self.state, PID)
        self.assertEqual((item["kind"], item["thread"]), ("owner_said", topic))
        self.assertIn("先别动 orders 表", item["text"])

    def test_a_message_in_an_ordinary_topic_leaves_none(self):
        self.receive("om_a", "你好")
        self.receive("om_a2", "再说一句", root="om_a")
        self.assertFalse((projects.folder(self.state) / PID / "inbox").exists())

    def test_every_turn_queued_tells_the_loop_who_started_it(self):
        with mock.patch.object(project_loop, "owner_turn") as owner:
            self.receive("om_b", "你好")
        topic = self.route("om_b")[1].topic_id
        owner.assert_called_once_with(self.state, topic, "chat", "om_b")

    def test_a_runner_ending_tells_the_loop(self):
        from covey.app import butler_relay
        job = make_job(job_id=5, chat_id="chat", message_id="om_c", topic_id="t")
        with mock.patch.object(project_loop, "after_runner") as after:
            butler_relay.after_runner(SimpleNamespace(runtime=SimpleNamespace(log=print)), job, self.ctx)
        after.assert_called_once()
        self.assertEqual(after.call_args.args[1:], (self.state, job))


if __name__ == "__main__":
    unittest.main()
