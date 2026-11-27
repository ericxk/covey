"""A project's continuing runs and guards (docs/architecture.md 专项, app/project_loop.py): what wakes the lead and what does
not, the guards that pause a project (tokens, wake-ups, stalling), the receipts the Bridge writes when a thread's turn ends
and when the owner speaks in a thread, the tokens counted once, the wake-up turn whose reply is not sent, and that with
COVEY_PROJECTS off none of it changes anything."""
from __future__ import annotations

import dataclasses
import json
import os
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from support import make_job
from support.records import write_job, write_topic
from support.world import UnifiedCase

from covey.app import auto_retry, project_lead, project_loop
from covey.app.cards import Cards
from covey.app.project_facts import facts
from covey.app.project_loop import ProjectLoop
from covey.core import butler, butler_settings, failure, placement, projects, quota_hold
from covey.core.jobs import find_job_record, job_key, topic_job_records
from covey.core.state import write_json
from covey.core.topics import load_topic

from support.butler import CHAT as BUTLER_CHAT
from test_butler_quiet import DeliverCase
from test_project_lead import CHAT, LEAD, NOW, ON, PID, Case
from test_quota_hold import configure, spent_text

THREAD = "b" * 32
HOUR = 3600
BUDGET = 10_000_000  # The budget the guard tests set (COVEY_PROJECT_BUDGET): without one no project is paused for its tokens.


def lead_app(case):
    """A Harness app that queues what it is given: ``case.queued`` lists (message id, prompt) of each turn."""
    case.queued, case.sources = [], []

    def event_context(base, event):
        topic = butler_topic(case, event["message_id"])
        return SimpleNamespace(topic_id=topic, history_path=case.state / "topics" / topic / "history.jsonl")

    def enqueue(ctx, content, chat, message, **fields):
        case.queued.append((message, fields.get("prompt_content")))
        case.sources.append(fields.get("source"))
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
        budget = mock.patch.dict(os.environ, {projects.AUTOWAKE_ENV: "0", projects.BUDGET_ENV: str(BUDGET)})
        budget.start()
        self.addCleanup(budget.stop)

    def no_budget(self):
        """The default: neither COVEY_PROJECT_BUDGET nor the older COVEY_PROJECT_TOKENS."""
        os.environ.pop(projects.BUDGET_ENV, None)
        os.environ.pop(projects.TOKENS_ENV, None)

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


class WakeSourceTest(Loop):
    """W1-A C4 ① and ③: the wake-up is stamped ``wake`` (it answers no question the lead waits on), and a lead that only
    waits for the owner's answer to its project_ask is woken as ever."""

    def lead_turn(self, status="waiting_user", key="lead-k1", message=f"lead-{PID}-1"):
        write_job(self.state, key, topic_id=LEAD, chat_id=CHAT, job_id=1, message_id=message, status=status)
        return key

    def asked(self, key, origin=project_loop.ASK_ORIGIN, status="pending", token="q" * 16):
        write_json(self.state / "questions" / f"{token}.json",
                   {"token": token, "topic_id": LEAD, "chat_id": CHAT, "job_key": key, "status": status, "origin": origin})

    def test_the_wake_up_is_stamped_wake_and_answers_no_question(self):
        from covey.core import turn_source

        self.receipt("start", "开始")
        self.loop.tick(NOW)
        [source] = self.sources
        self.assertEqual((turn_source.kind({"turn_source": source}), source["asserted_by"]), (turn_source.WAKE, turn_source.PARTY_BRIDGE))
        acts = turn_source.behavior({"turn_source": source})
        self.assertEqual((acts.answers_questions, acts.owner_words, acts.owner_send), (False, False, False))
        with mock.patch.object(turn_source, "STAMPING", False):  # The one switch back: unstamped, as before.
            self.receipt("start", "再开始")
            self.update(wakes={})
            self.loop.tick(NOW + 60)
        self.assertIsNone(self.sources[-1])

    def test_a_lead_waiting_on_its_own_question_is_woken_by_a_receipt(self):
        key = self.lead_turn()
        self.asked(key)
        self.assertFalse(project_loop.lead_busy(self.state, LEAD))
        self.receipt("start", "T41 做完了")
        self.loop.tick(NOW)
        self.assertEqual(len(self.queued), 1)

    def test_a_lead_waiting_on_another_question_or_running_is_not(self):
        for name, status, origin in (("checkpoint question", "waiting_user", ""), ("answered", "waiting_user", "answered"),
                                     ("running", "running", project_loop.ASK_ORIGIN)):
            with self.subTest(name):
                key = self.lead_turn(status=status)
                self.asked(key, origin=project_loop.ASK_ORIGIN if origin == "answered" else origin,
                           status="answered" if origin == "answered" else "pending")
                self.assertTrue(project_loop.lead_busy(self.state, LEAD))

    def test_the_wake_up_does_not_close_the_question_the_lead_waits_on(self):
        # C4 ①: neither the start of the wake-up's turn (app/runs.py _continue_previous) nor its queueing
        # (app/task_controls.py acknowledge_waiting_answer) marks the waiting turn answered: both ask answers_questions.
        from covey.app.runs import Runs
        from covey.app.task_controls import TaskControls
        from covey.core import turn_source

        key = self.lead_turn()
        self.asked(key)
        waiting = find_job_record(self.state, key)
        wake = SimpleNamespace(message_id=f"lead-{PID}-2", job_id=2, topic_id=LEAD, chat_id=CHAT, recovery_context=None,
                               turn_source=turn_source.stamp(turn_source.WAKE))
        harness = mock.Mock()
        harness.topic_records.return_value = [{**waiting, "checkpoint_path": str(self.state / "cp.json")}]
        ctx = SimpleNamespace(state_dir=self.state, topic_id=LEAD)
        self.assertIsNone(Runs._continue_previous(SimpleNamespace(harness=harness), ctx, wake))
        controls = TaskControls.__new__(TaskControls)
        controls.questions, controls.harness = mock.Mock(), harness
        self.assertIsNone(controls.acknowledge_waiting_answer(ctx, wake))
        harness.topic_records.assert_not_called()
        self.assertEqual(find_job_record(self.state, key)["status"], "waiting_user")
        self.assertEqual(json.loads((self.state / "questions" / ("q" * 16 + ".json")).read_text())["status"], "pending")

    def test_a_wake_up_carrying_the_owners_message_is_his(self):
        # C5 item 5 (stamped by W2-C): the owner's own start of a project; read now.
        from covey.core import turn_source

        his = SimpleNamespace(message_id=f"lead-{PID}-3", job_id=3, turn_source=turn_source.stamp(
            turn_source.WAKE, origin={"message_id": "om_start"}))
        bridge = SimpleNamespace(message_id=f"lead-{PID}-4", job_id=4, turn_source=turn_source.stamp(turn_source.WAKE))
        old = SimpleNamespace(message_id=f"lead-{PID}-5", job_id=5)  # Queued before the stamp: its message id says it.
        self.assertEqual([project_loop.woken(self.state, job) for job in (his, bridge, old)], [False, True, True])
        relay = SimpleNamespace(message_id=f"lead-{PID}-6", job_id=6, turn_source=turn_source.stamp(turn_source.RELAY))
        self.assertFalse(project_loop.woken(self.state, relay))  # Another source: no wake-up, whatever its id says.


class GuardTest(Loop):
    def test_tokens_over_the_budget_pause_the_project_and_wake_no_more(self):
        self.receipt("start")
        self.update(tokens_used=BUDGET)
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
                self.assertEqual(projects.token_limit(), 0)  # No budget, not a default one.

    def test_a_launch_keeps_the_budget_and_the_older_name_still_counts(self):
        # The acceptance of 2026-11-17: COVEY_PROJECT_TOKENS has TOKEN in its name, the service manifest took it for a
        # secret and dropped it, so the budget a launch set never reached the Bridge.
        from covey.service import manager
        self.no_budget()
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

    def test_the_owners_turn_starts_what_the_owner_stopped_and_opens_a_finished_project_again(self):
        self.update(state=projects.PAUSED, state_reason=projects.OWNER_STOP)
        project_loop.owner_turn(self.state, LEAD, CHAT, "main-x")
        self.assertEqual(self.reload()["state"], projects.ACTIVE)
        self.update(state=projects.DONE, state_reason="完成", state_job="lead-x-1", conclusion_message_id="om_c",
                    card={"card_id": "c", "message_id": "om_card", "sequence": 8, "closed": True, "settled_at": NOW, "digest": "d"})
        project_loop.owner_turn(self.state, LEAD, CHAT, f"lead-{PID}-9")  # A wake-up is not the owner.
        self.assertEqual(self.reload()["state"], projects.DONE)
        with mock.patch("covey.app.project_loop.time.time", return_value=NOW + 60):
            project_loop.owner_turn(self.state, LEAD, CHAT, "om_2")
        record = self.reload()
        self.assertEqual((record["state"], record["state_reason"], record["owner_at"]), (projects.ACTIVE, "", NOW + 60))
        self.assertEqual(record["reopened"], [{"at": NOW + 60, "job": "lead-x-1", "summary": "完成"}])
        self.assertNotIn("state_job", record)
        self.assertNotIn("conclusion_message_id", record)
        self.assertEqual((record["card"]["closed"], record["card"]["digest"], record["card"]["sequence"]), (False, "", 8))
        self.assertNotIn("settled_at", record["card"])
        self.assertIn("重开：这个专项收过尾（完成）", project_lead.state_pack(self.state, record, "k", full=False, receipts=[]))

    def test_a_cancelled_project_is_not_opened_by_the_owners_word(self):
        self.update(state=projects.CANCELLED, state_reason="downgraded")
        project_loop.owner_turn(self.state, LEAD, CHAT, "om_2")
        project_loop.heard(self.state, THREAD, "还在吗")
        self.assertEqual(self.reload()["state"], projects.CANCELLED)

    def test_after_a_tokens_pause_the_owners_turn_starts_a_new_budget(self):
        self.update(tokens_used=BUDGET, state=projects.PAUSED, state_reason="tokens")
        project_loop.owner_turn(self.state, LEAD, CHAT, "om_3")
        self.loop.tick(NOW + 5)
        self.assertEqual(self.reload()["state"], projects.ACTIVE)
        self.update(tokens_used=2 * BUDGET)
        self.loop.tick(NOW + 6)
        self.assertEqual(self.reload()["state_reason"], "tokens")

    def test_the_owners_turn_in_another_topic_changes_nothing(self):
        self.update(state=projects.PAUSED, state_reason="wakes")
        project_loop.owner_turn(self.state, THREAD, CHAT, "om_4")
        self.assertEqual(self.reload()["state"], projects.PAUSED)


class NoBudgetTest(Loop):
    """The owner, 2026-11-19: no project is paused for its tokens unless COVEY_PROJECT_BUDGET is set; the other guards stay."""

    def test_without_a_budget_tokens_pause_nothing_stop_no_task_and_ask_nothing(self):
        self.no_budget()
        mock.patch.dict(os.environ, {projects.ASK_ENV: "1"}).start()
        self.addCleanup(mock.patch.stopall)
        self.receipt("start")
        self.update(tokens_used=50 * BUDGET, threads=[THREAD])
        self.loop.tick(NOW + 5)
        record = self.reload()
        self.assertEqual((record["state"], record.get("state_reason") or ""), (projects.ACTIVE, ""))
        self.assertNotIn("pause_ask", record)
        self.stop.assert_not_called()
        self.assertEqual(len(self.queued), 1)  # The start receipt wakes the lead as it always did.
        self.assertEqual(project_loop.hold(record, NOW + 5), "")

    def test_without_a_budget_the_wakes_guard_still_pauses(self):
        self.no_budget()
        self.autowake()
        self.update(tokens_used=50 * BUDGET, wakes={"count": 30, "streak": 30, "last_at": NOW, "recent": []})
        self.receipt()
        self.loop.tick(NOW + 5)
        self.assertEqual((self.reload()["state"], self.reload()["state_reason"]), (projects.PAUSED, "wakes"))

    def test_an_older_tokens_pause_card_still_answers_and_reads_once_the_budget_is_gone(self):
        self.no_budget()
        self.update(state=projects.PAUSED, state_reason="tokens", tokens_used=BUDGET)
        self.update(pause_ask={"reason": "tokens", "why": "token 用到上限 1000 万", "at": NOW, "choice": "", "halted": 1,
                               "card": {"card_id": "c1", "message_id": "om_ask", "sequence": 0}})
        self.assertEqual(project_loop.pause_words(self.reload(), "tokens", NOW), "token 用到上限")
        self.assertEqual(project_loop.answer_pause(self.state, PID, "continue", "om_ask", now=NOW), "ok")
        self.assertEqual(self.reload()["state"], projects.ACTIVE)
        self.loop.tick(NOW + 5)
        self.assertEqual(self.reload()["state"], projects.ACTIVE)  # And it is not paused again for its tokens.


class LimitTest(Loop):
    """T99 (2026-11-25 21:59–22:11): three accounts spent; each lead's wake-up failed on the rate limit, gave its receipts
    back, and the next look woke the lead again, about every 20 s (65 failed turns in two hours); each wake, a new message
    in the topic, cancelled the retry the failed turn had at the account's reset. Now the lead is not woken while a wake
    would only fail on a limit, and the receipts wait for the one turn that reads them all (``limit_wait``)."""

    def failed(self, number, ended=NOW + 15, key="", **fields):
        """The lead's turn ``number`` (a wake-up unless ``key`` says otherwise) failed on the rate limit at ``ended``, its
        automatic retry at the reset an hour on, as app/auto_retry.py schedule_fields writes it (``fields`` change it, a
        None drops one); its key."""
        key = key or projects.wake_id(PID, number)
        record = {"status": "failed", "failure_code": "rate_limited", "auto_retry_state": "pending", "auto_retry_kind": "rate_limited",
                  "auto_retry_at": NOW + HOUR, "auto_retry_attempt": 1, "backend": "claude",
                  "timings": {"execution_finished": ended}, **fields}
        write_job(self.state, key, topic_id=LEAD, chat_id=CHAT, job_id=number, message_id=key,
                  **{name: value for name, value in record.items() if value is not None})
        self.update(wakes={"count": number, "streak": number, "last_at": NOW, "recent": [NOW]})
        return key

    def woken(self):
        return [message for message, _ in self.queued]

    def down(self, number, ended, key="", why="ValueError: 模型后端尚未登录"):
        """The lead's turn ``number`` (a wake-up unless ``key`` says otherwise) did not start, its Harness's login check
        failing, as app/job_dispatch.py records it: its record's last write, at ``ended``, is its end. Its key."""
        key = key or projects.wake_id(PID, number)
        with mock.patch("covey.core.jobs.utc_now_iso", return_value=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ended))):
            write_job(self.state, key, topic_id=LEAD, chat_id=CHAT, job_id=number, message_id=key, backend="claude",
                      status="failed", status_message=failure.STARTUP_FAILED + why)
        return key

    def wake_job(self, number, message=""):
        return SimpleNamespace(message_id=message or projects.wake_id(PID, number), job_id=number, topic_id=LEAD)

    def test_a_backend_that_does_not_start_wakes_the_lead_on_a_ladder_and_the_first_turn_that_starts_reads_every_receipt(self):
        # 2026-11-26 10:57: the lead's Claude Code on the remote machine failed its login check, a wake every 10 s, each
        # failing 5 s on and telling the owner 后端暂不可用 with a recovery card, until the wakes guard paused the project.
        # Here: 40 minutes of a look every 10 s, a topic's receipt every 3 minutes, every wake failing its login check.
        self.autowake()
        came = [self.receipt(text="T41 的任务 #7 停住了")]
        woken_at = []
        for n in range(241):
            at = NOW + 5 + 10 * n
            if n and n % 18 == 0:
                came.append(self.receipt(text=f"T{41 + n} 的任务 #{7 + n} 停住了", at=at - 1))
            self.loop.tick(at)
            if len(self.queued) > len(woken_at):  # The Bridge runs the wake: its login check fails 5 s on.
                woken_at.append(at)
                self.down(len(woken_at), at + 5)
        # 1, 3, then 10 minutes after the latest (before: 240 wakes, or 12 and a pause card each hour with the guard).
        self.assertEqual([later - earlier for earlier, later in zip(woken_at, woken_at[1:])], [70, 190, 610, 610, 610])
        record = self.reload()
        self.assertEqual((record["wakes"]["count"], record["state"]), (6, projects.ACTIVE))  # Not paused: no pause card.
        self.stop.assert_not_called()
        self.assertEqual({line.split("why=")[1].split()[0] for line in self.logs if "wake held" in line}, {"down"})
        # The owner was told of the first that did not start; of the tries after it, nothing (app/job_dispatch.py).
        self.assertEqual([project_loop.quiet_start(self.state, self.wake_job(n), now=woken_at[n - 1] + 5)
                          for n in range(1, 7)], [False] + [True] * 5)
        # Every receipt kept, unread.
        self.assertEqual([item["id"] for item in project_loop.unread(self.state, PID)], [item["id"] for item in came])
        # The backend is back: the next wake starts, takes them all in one turn, and leaves nothing to wake for.
        self.loop.tick(NOW + 2700)
        self.assertEqual(len(self.queued), 7)
        key = projects.wake_id(PID, 7)
        write_job(self.state, key, topic_id=LEAD, chat_id=CHAT, job_id=7, message_id=key, status="running")
        self.assertEqual([item["id"] for item in project_lead.take_receipts(self.state, PID, key)],
                         [item["id"] for item in came])
        write_job(self.state, key, status="done")
        for at in range(2710, 3400, 10):
            self.loop.tick(NOW + at)
        self.assertEqual(len(self.queued), 7)
        self.assertEqual(project_loop.unread(self.state, PID), [])

    def test_the_ladder_counts_the_lead_turns_in_a_row_that_did_not_start(self):
        self.autowake()
        self.receipt()
        self.down(1, NOW)
        self.update(wakes={"count": 1, "streak": 1, "last_at": NOW, "recent": [NOW]})
        write_job(self.state, "k-done", topic_id=LEAD, chat_id=CHAT, job_id=2, message_id="k-done", status="done",
                  timings={"execution_finished": NOW + 100})
        self.down(3, NOW + 200, key="om_owner", why="ValueError: 模型后端程序不可用")  # The owner's own turn counts too.
        self.loop.tick(NOW + 255)
        self.assertEqual(self.woken(), [])  # One in a row: a minute.
        self.loop.tick(NOW + 265)
        self.assertEqual(len(self.queued), 1)

    def test_a_task_runner_that_did_not_start_holds_as_a_backend_down(self):
        self.autowake()
        self.receipt()
        key = projects.wake_id(PID, 1)
        write_job(self.state, key, topic_id=LEAD, chat_id=CHAT, job_id=1, message_id=key, status="failed",
                  runner_start_failed=True, timings={"execution_finished": NOW})
        self.update(wakes={"count": 1, "streak": 1, "last_at": NOW, "recent": [NOW]})
        self.loop.tick(NOW + 30)
        self.assertEqual(self.woken(), [])
        self.loop.tick(NOW + 65)
        self.assertEqual(len(self.queued), 1)

    def test_a_lead_turn_that_failed_as_the_next_would_holds_as_a_backend_down(self):
        """T872, 2026-11-27: a lead on a model its account had lost was woken every 10 s, 12 failed turns in two
        minutes. A refusal, a model the account cannot use, or a failure that did nothing at all waits the ladder too."""
        for n, fields in enumerate(({"failure_code": "model_unavailable"}, {"failure_code": "policy_refusal"},
                                    {"failure_code": "root_failed", "tool_step_count": 0, "input_tokens": 0}), 1):
            with self.subTest(fields=fields):
                self.setUp()
                self.autowake()
                self.receipt()
                key = projects.wake_id(PID, n)
                write_job(self.state, key, topic_id=LEAD, chat_id=CHAT, job_id=n, message_id=key, status="failed",
                          timings={"execution_finished": NOW}, **fields)
                self.update(wakes={"count": 1, "streak": 1, "last_at": NOW, "recent": [NOW]})
                self.loop.tick(NOW + 30)
                self.assertEqual(self.woken(), [])
                self.loop.tick(NOW + 65)
                self.assertEqual(len(self.queued), 1)
        # A failure that did work goes on as before: woken at once.
        self.setUp()
        self.autowake()
        self.receipt()
        key = projects.wake_id(PID, 9)
        write_job(self.state, key, topic_id=LEAD, chat_id=CHAT, job_id=9, message_id=key, status="failed",
                  failure_code="root_failed", tool_step_count=12, input_tokens=90000, timings={"execution_finished": NOW})
        self.update(wakes={"count": 1, "streak": 1, "last_at": NOW, "recent": [NOW]})
        self.loop.tick(NOW + 30)
        self.assertEqual(len(self.queued), 1)

    def test_the_owners_choice_after_a_backend_down_wakes_the_lead_at_once(self):
        self.receipt("start", at=NOW - 60)
        self.down(1, NOW)
        self.update(wakes={"count": 1, "streak": 1, "last_at": NOW, "recent": [NOW]})
        self.loop.tick(NOW + 10)
        self.assertEqual(self.woken(), [])
        self.receipt("owner_choice", "主人看了停住的原因，选了继续", at=NOW + 20)
        self.loop.tick(NOW + 25)
        self.assertEqual(self.woken(), [f"lead-{PID}-2"])

    def test_only_a_wake_up_after_a_lead_turn_that_did_not_start_tells_the_owner_nothing(self):
        quiet = project_loop.quiet_start
        self.assertFalse(quiet(self.state, self.wake_job(1), now=NOW))  # The first: he is told.
        self.down(1, NOW)
        self.assertTrue(quiet(self.state, self.wake_job(2), now=NOW + 70))
        self.assertFalse(quiet(self.state, self.wake_job(2, "om_owner"), now=NOW + 70))  # His own turn: told.
        self.assertFalse(quiet(self.state, self.wake_job(2), now=NOW + HOUR + 1))  # An hour on: a new outage, told.
        write_job(self.state, "k-done", topic_id=LEAD, chat_id=CHAT, job_id=2, message_id="k-done", status="done")
        self.assertFalse(quiet(self.state, self.wake_job(3), now=NOW + 70))  # The turn before it started: told.

    def test_topics_stopping_one_after_another_wake_the_lead_once_and_its_retry_reads_every_receipt(self):
        self.autowake()
        came = [self.receipt(text="T41 的任务 #7 停住了：模型服务限流")]
        self.loop.tick(NOW + 5)
        self.assertEqual(self.woken(), [f"lead-{PID}-1"])
        key = self.failed(1)
        self.assertEqual(len(project_lead.take_receipts(self.state, PID, key)), 1)  # Its turn took the receipt, then failed.
        for n in range(1, 61):  # Ten minutes of a look every 10 s, a topic stopping on the limit every minute.
            if n % 6 == 0:
                came.append(self.receipt(text=f"T{41 + n} 的任务 #{7 + n} 停住了：模型服务限流", at=NOW + 10 * n))
            self.loop.tick(NOW + 15 + 10 * n)
        self.assertEqual(self.woken(), [f"lead-{PID}-1"])  # Not woken again (before: 30 more turns, each failing).
        record = self.reload()
        self.assertEqual((record["wakes"]["count"], record["state"]), (1, projects.ACTIVE))  # No wakes-guard pause either.
        self.assertEqual([line for line in self.logs if "wake held" in line],
                         [f"project wake held project={PID[:8]} why=retry until={time.strftime('%H:%M:%S', time.localtime(NOW + HOUR))}"])
        # The retry stays: no newer task in the topic cancels it as a new message (app/auto_retry.py blocker).
        jobs = SimpleNamespace(runtime=SimpleNamespace(jobs=SimpleNamespace(topic_jobs=lambda *a: [])))
        records = topic_job_records(self.state, LEAD)
        self.assertEqual(auto_retry.blocker(jobs, find_job_record(self.state, key), records, load_topic(self.state, LEAD)), "")
        # Every receipt kept, unread.
        self.assertEqual([item["id"] for item in project_loop.unread(self.state, PID)], [item["id"] for item in came])
        # The reset: the retry runs and takes them all in one turn; a turn that ended well leaves nothing to wake for.
        write_job(self.state, key, auto_retry_state="queued")
        retry = "auto-retry-1-1"
        write_job(self.state, retry, topic_id=LEAD, chat_id=CHAT, job_id=2, message_id=retry, status="running")
        self.loop.tick(NOW + HOUR + 40)
        taken = project_lead.take_receipts(self.state, PID, retry)
        self.assertEqual([item["id"] for item in taken], [item["id"] for item in came])
        write_job(self.state, retry, status="done")
        self.loop.tick(NOW + HOUR + 120)
        self.assertEqual(self.woken(), [f"lead-{PID}-1"])
        self.assertEqual(project_loop.unread(self.state, PID), [])

    def test_a_retry_that_fails_again_keeps_the_wait_until_its_next_one(self):
        self.autowake()
        self.receipt()
        self.failed(1)
        self.loop.tick(NOW + 30)
        write_job(self.state, f"lead-{PID}-1", auto_retry_state="queued")
        retry = self.failed(2, ended=NOW + HOUR + 60, key="auto-retry-1-1", auto_retry_at=NOW + HOUR + 240,
                            auto_retry_attempt=2)  # The retry, failed on the limit too: its own next one is at +4 min.
        project_lead.take_receipts(self.state, PID, retry)
        self.loop.tick(NOW + HOUR + 70)
        self.assertEqual(self.woken(), [])
        self.assertTrue(any("until=" + time.strftime("%H:%M:%S", time.localtime(NOW + HOUR + 240)) in line for line in self.logs))

    def test_a_lead_waiting_out_a_limit_is_not_paused_by_the_wakes_guard(self):
        # No wake is queued, so there is nothing for the guard to stop: the retry is not a wake and is not counted.
        self.autowake()
        self.receipt()
        self.failed(1)
        self.update(wakes={"count": 12, "streak": 12, "last_at": NOW, "recent": [NOW + i for i in range(12)]})
        self.loop.tick(NOW + 30)
        self.assertEqual((self.reload()["state"], self.woken()), (projects.ACTIVE, []))

    def test_a_server_fault_holds_as_a_rate_limit_does_and_a_spent_quota_rests(self):
        self.autowake()
        self.receipt()
        self.failed(1, failure_code="server_fault", auto_retry_kind="server_fault", auto_retry_at=NOW + 135)
        self.loop.tick(NOW + 30)
        # A spent gateway quota is retried by nobody (app/auto_retry.py KINDS): the next wake rests after it.
        self.failed(2, key="k-quota", failure_code="quota_exhausted", auto_retry_state=None, auto_retry_at=None,
                    auto_retry_kind=None, auto_retry_attempt=None)
        self.loop.tick(NOW + 60)
        self.assertEqual(self.woken(), [])
        self.assertEqual([line.split("why=")[1].split()[0] for line in self.logs if "wake held" in line], ["retry", "rest"])

    def test_with_no_retry_to_come_the_next_wake_waits_after_the_failure(self):
        self.autowake()
        self.receipt()
        self.failed(1, auto_retry_state=None, auto_retry_at=None, auto_retry_kind=None, auto_retry_attempt=None)
        self.loop.tick(NOW + 15 + project_loop.LIMIT_REST - 5)
        self.assertEqual(self.woken(), [])
        self.loop.tick(NOW + 15 + project_loop.LIMIT_REST + 5)
        self.assertEqual(self.woken(), [f"lead-{PID}-2"])

    def test_a_retry_long_overdue_holds_no_more(self):
        self.autowake()
        self.receipt()
        self.failed(1, auto_retry_at=NOW + 60)
        self.loop.tick(NOW + 60 + project_loop.LIMIT_REST - 5)
        self.assertEqual(self.woken(), [])
        self.loop.tick(NOW + 60 + project_loop.LIMIT_REST + 5)
        self.assertEqual(len(self.queued), 1)

    def test_a_retry_cancelled_by_the_owner_or_a_model_switch_holds_nothing(self):
        self.autowake()
        self.receipt()
        for note in ("model_switch", "stopped"):
            self.queued.clear()
            self.failed(1, auto_retry_state="cancelled", auto_retry_note=note)
            self.loop.tick(NOW + 30)
            self.assertEqual(len(self.queued), 1, note)

    def test_other_failures_wake_as_before(self):
        self.autowake()
        self.receipt()
        self.failed(1, failure_code="failed", auto_retry_state=None, auto_retry_at=None, auto_retry_kind=None)
        self.loop.tick(NOW + 30)
        self.assertEqual(len(self.queued), 1)

    def test_the_owners_choice_after_the_failure_wakes_the_lead_once(self):
        self.receipt("start", at=NOW - 60)  # Read and failed on: the start is older than the failed turn.
        self.failed(1)
        self.loop.tick(NOW + 30)
        self.assertEqual(self.woken(), [])
        self.receipt("owner_choice", "主人看了停住的原因，选了继续", at=NOW + 40)
        self.loop.tick(NOW + 45)
        self.assertEqual(self.woken(), [f"lead-{PID}-2"])
        self.failed(2, ended=NOW + 60, auto_retry_at=NOW + HOUR)  # That turn failed on the limit too.
        for at in range(70, 400, 10):
            self.loop.tick(NOW + at)
        self.assertEqual(self.woken(), [f"lead-{PID}-2"])

    def test_a_lead_on_a_held_gateway_quota_waits_for_its_recheck(self):
        gateway = configure(self)
        self.env.topics.apps["codex"] = self.app
        write_topic(self.state, topic_id=LEAD, chat_id=CHAT, harness_id="codex", model="mygw/gpt-x",
                    project=projects.project_field(PID, projects.LEAD))
        quota_hold.note(self.state, gateway, spent_text(NOW + HOUR), now=NOW)
        self.autowake()
        self.receipt()
        self.loop.tick(NOW + 5)
        self.assertEqual(self.woken(), [])
        self.assertTrue(any("why=quota" in line for line in self.logs))
        self.loop.tick(NOW + quota_hold.RECHECK + 5)  # A turn then starts and asks the gateway again (core/quota_hold.py blocks).
        self.assertEqual(len(self.queued), 1)

    def test_the_owners_own_turn_in_the_lead_topic_is_not_held(self):
        # The owner speaking to the lead is his message's own turn (app/project_loop.py owner_turn), not a wake: nothing
        # here holds it, and it is the topic's newest task, so the pending retry gives way to it as before.
        self.autowake()
        self.receipt()
        key = self.failed(1)
        project_loop.owner_turn(self.state, LEAD, CHAT, "om_owner")
        write_job(self.state, "om_owner", topic_id=LEAD, chat_id=CHAT, job_id=2, message_id="om_owner", status="running")
        jobs = SimpleNamespace(runtime=SimpleNamespace(jobs=SimpleNamespace(topic_jobs=lambda *a: [])))
        records = topic_job_records(self.state, LEAD)
        self.assertEqual(auto_retry.blocker(jobs, find_job_record(self.state, key), records, load_topic(self.state, LEAD)),
                         "new_message")
        self.loop.tick(NOW + 30)
        self.assertEqual(self.woken(), [])  # Its turn runs: no wake on top of it.


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
        self.update(tokens_used=BUDGET, threads=[THREAD])
        self.loop.tick(NOW + 5)
        self.assertEqual((self.reload()["state"], self.reload()["state_reason"]), (projects.PAUSED, "tokens"))
        self.assertNotIn("pause_ask", self.reload())
        self.stop.assert_not_called()

    def test_a_tokens_pause_asks_in_the_owners_words_and_stops_the_member_topics_tasks(self):
        self.ask_on()
        self.update(tokens_used=BUDGET, threads=[THREAD])
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
        self.update(tokens_used=BUDGET)
        record = self.reload()
        self.update(state=projects.PAUSED, state_reason=projects.OWNER_STOP)  # The loop's look is of the old record.
        self.loop.pause(record, "tokens", NOW + 5)
        self.assertNotIn("pause_ask", self.reload())

    def test_continue_starts_again_clears_the_counts_and_gives_a_tokens_pause_a_new_budget(self):
        self.paused("tokens", tokens_used=BUDGET + 7, wakes={"count": 30, "streak": 30, "last_at": NOW, "recent": [NOW]})
        self.assertEqual(project_loop.answer_pause(self.state, PID, "continue", "om_ask"), "ok")
        record = self.reload()
        self.assertEqual((record["state"], record["state_reason"], record["token_floor"]), (projects.ACTIVE, "", BUDGET + 7))
        self.assertEqual((record["wakes"]["streak"], record["wakes"]["recent"]), (0, []))
        self.assertEqual(record["pause_ask"]["choice"], "continue")
        project_loop.pause_receipt(self.state, PID, "continue", self.reload()["pause_ask"])
        self.loop.tick(NOW + 5)  # The receipt wakes the lead without COVEY_PROJECT_AUTOWAKE.
        self.assertEqual(len(self.queued), 1)
        self.assertEqual(self.reload()["state"], projects.ACTIVE)

    def test_the_boards_continue_is_the_pause_cards_continue(self):
        """P0-3 (T647): 继续 on the 看板's block of a project a guard paused (cards/board.py PROJECT_GO_KIND) answers the pause
        put to the owner as the pause card's 继续 does: the choice kept (the card becomes "你选了继续"), the project on
        again with a new budget after a tokens pause, the lead woken by the owner_choice receipt."""
        self.paused("tokens", tokens_used=BUDGET + 3)
        self.assertEqual(project_loop.resume(self.state, PID, CHAT), "ok")
        record = self.reload()
        self.assertEqual((record["state"], record["pause_ask"]["choice"], record["token_floor"]),
                         (projects.ACTIVE, "continue", BUDGET + 3))
        [item] = project_loop.unread(self.state, PID)
        self.assertEqual(item["kind"], "owner_choice")
        self.loop.tick(NOW + 5)
        self.assertEqual(len(self.queued), 1)
        self.assertEqual(project_loop.resume(self.state, PID, CHAT), "moved")  # Once: it is on already.

    def test_the_boards_continue_without_a_pause_card_starts_it_as_the_owners_word(self):
        """With COVEY_PROJECT_ASK off there is no pause card: 继续 is the owner's word (owner_spoke), and the lead hears of it
        (owner_choice, which wakes it). A project in another chat, one not paused by a guard, none at all: nothing changes."""
        self.update(state=projects.PAUSED, state_reason="wakes", wakes={"count": 30, "streak": 30, "last_at": NOW, "recent": [NOW]})
        self.assertEqual(project_loop.resume(self.state, PID, "oc_elsewhere"), "none")
        self.assertEqual(project_loop.resume(self.state, "0" * 12, CHAT), "none")
        self.assertEqual(self.reload()["state"], projects.PAUSED)
        self.assertEqual(project_loop.resume(self.state, PID, CHAT), "ok")
        record = self.reload()
        self.assertEqual((record["state"], record["state_reason"], record["wakes"]["streak"]), (projects.ACTIVE, "", 0))
        self.assertIn("owner_at", record)
        [item] = project_loop.unread(self.state, PID)
        self.assertEqual(item["kind"], "owner_choice")
        self.assertIn("连续唤醒次数到上限", item["text"])
        self.update(state=projects.PAUSED, state_reason=projects.OWNER_STOP)  # The owner's own stop: not the 看板's 继续.
        self.assertEqual(project_loop.resume(self.state, PID, CHAT), "moved")
        self.assertEqual(self.reload()["state"], projects.PAUSED)
        self.update(state=projects.ACTIVE, state_reason="")
        self.update(state=projects.PAUSED, state_reason="budget_hours")  # A newer release's guard: his click starts it too.
        self.assertTrue(projects.halted(self.reload()))
        self.assertEqual(project_loop.resume(self.state, PID, CHAT), "ok")
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
        self.paused("tokens", tokens_used=BUDGET + 5)
        self.assertEqual(project_loop.answer_pause(self.state, PID, "stop"), "ok")
        project_loop.owner_turn(self.state, LEAD, CHAT, "om_later")  # The owner speaks to the lead some time after.
        self.assertEqual((self.reload()["state"], self.reload()["token_floor"]), (projects.ACTIVE, BUDGET + 5))
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

    def test_anything_the_lead_can_move_keeps_it_open(self):
        # What only waits for the owner (a task waiting_user, a question card) folds away after the idle days all the same:
        # test_waiting_for_the_owner_never_stalls_and_folds_away_after_the_idle_days. A receipt moves it only where it wakes
        # the lead (COVEY_PROJECT_AUTOWAKE=1): test_without_autowake_a_receipt_wakes_no_one_and_it_folds_after_the_idle_days.
        for name, setup in (("a queued task", lambda: self.thread(THREAD, "迁移", 41, status="queued")),
                            ("a receipt nobody read", lambda: (self.autowake(), self.receipt("thread_end", "T41 做完了", at=NOW)))):
            with self.subTest(name):
                self.update(state=projects.ACTIVE, state_reason="")
                setup()
                self.loop.tick(NOW + 30 * self.DAY)
                self.assertEqual(self.state_of(), (projects.ACTIVE, ""))

    def test_without_autowake_a_receipt_wakes_no_one_and_it_folds_after_the_idle_days(self):
        # T647 P0-6: with COVEY_PROJECT_AUTOWAKE off a member's end woke no one, and the project said 在做 and never folded.
        self.thread(THREAD, "迁移", 41, status="done", timings={"execution_finished": NOW})
        self.receipt("thread_end", "T41 做完了", at=NOW + self.DAY)
        self.loop.tick(NOW + 7 * self.DAY + 60)
        self.assertEqual(self.state_of(), (projects.ACTIVE, ""))  # The idle days run from the receipt.
        self.loop.tick(NOW + 8 * self.DAY + 60)
        self.assertEqual(self.state_of(), (projects.PAUSED, projects.IDLE))
        self.assertEqual(self.queued, [])  # Nobody woken,
        self.assertEqual([item["kind"] for item in project_loop.unread(self.state, PID)], ["thread_end"])  # read next time.

    def test_without_autowake_a_member_at_work_keeps_it_open(self):
        self.receipt("thread_end", "T41 做完了", at=NOW)
        self.thread(THREAD, "迁移", 41, status="running")
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

    def test_his_word_in_a_topic_of_a_finished_project_leaves_it_finished_and_his_word_to_it_opens_it(self):
        """W2-C: his word in a member topic is that topic's (its receipt waits); his turn in the lead's topic opens the project."""
        self.thread(THREAD, "迁移", 41, status="done", timings={"execution_finished": NOW})
        self.update(state=projects.DONE, state_reason="迁完了", state_job="lead-x-1")
        project_loop.heard(self.state, THREAD, "迁移再看看")
        record = self.reload()
        self.assertEqual((self.state_of(), record.get("reopened"), record["state_job"]), ((projects.DONE, "迁完了"), None, "lead-x-1"))
        self.assertTrue(record["owner_at"])
        self.assertNotIn("owner_lead_at", record)
        self.assertIn("owner_said", [item["kind"] for item in project_loop.unread(self.state, PID)])
        project_loop.owner_turn(self.state, LEAD, CHAT, "om_1")
        record = self.reload()
        self.assertEqual((self.state_of(), record["reopened"][-1]["summary"]), ((projects.ACTIVE, ""), "迁完了"))
        self.assertTrue(record["owner_lead_at"])

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

    def test_waiting_for_the_owner_never_stalls_and_folds_away_after_the_idle_days(self):
        # A question the owner has not answered is theirs to move: no reminder for the lead, no pause card. Left for the idle days
        # it folds away like a project with nothing to do (2026-11-17 acceptance: a waiting topic kept a project open for good).
        self.autowake()
        self.thread(THREAD, "迁移", 41, status="waiting_user")
        for at in (NOW + 31 * 60, NOW + 3 * HOUR, NOW + 6 * self.DAY):
            self.loop.tick(at)
        self.assertEqual(project_loop.unread(self.state, PID), [])
        self.assertEqual(self.state_of(), (projects.ACTIVE, ""))
        self.loop.tick(NOW + 7 * self.DAY + 60)
        self.assertEqual(self.state_of(), (projects.PAUSED, projects.IDLE))

    def test_a_question_card_waiting_folds_away_after_the_idle_days_too(self):
        write_json(self.state / "questions" / ("c" * 16 + ".json"),
                   {"token": "c" * 16, "topic_id": THREAD, "chat_id": CHAT, "status": "pending", "created_at": NOW})
        self.assertTrue(project_loop.has_question(self.state, [THREAD]))  # The card counts as work: it is waiting.
        self.loop.tick(NOW + 6 * self.DAY)
        self.assertEqual(self.state_of(), (projects.ACTIVE, ""))
        self.loop.tick(NOW + 7 * self.DAY + 60)
        self.assertEqual(self.state_of(), (projects.PAUSED, projects.IDLE))


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
        self.assertIn("任务 #41 做完", item["text"])
        self.assertIn("全部 12 张表对账一致", item["text"])
        self.assertEqual(self.reload()["tokens_used"], 1550)

    def test_the_receipt_has_the_summary_and_where_the_whole_reply_is(self):
        self.thread(THREAD, "迁移", 41)
        self.after(self.job(THREAD, final_text="## 结论\n\n导出好了，12 张表一致\n\n明细见下"))
        path = self.state / "result-files" / f"{job_key('om_t1', 41)}.md"
        [item] = project_loop.unread(self.state, PID)
        self.assertEqual(item["text"], f"T41 的任务 #41 做完：导出好了，12 张表一致（全文 {path}）")
        self.assertEqual(path.read_text(), "## 结论\n\n导出好了，12 张表一致\n\n明细见下")

    def test_a_long_replys_summary_is_cut_to_300_and_the_path_stays_whole(self):
        self.thread(THREAD, "迁移", 41)
        self.after(self.job(THREAD, final_text="结论" * 1000))
        path = self.state / "result-files" / f"{job_key('om_t1', 41)}.md"
        [item] = project_loop.unread(self.state, PID)
        said = item["text"].split("做完：", 1)[1].rsplit("（全文 ", 1)[0]
        self.assertEqual((len(said), said[-1]), (300, "…"))
        self.assertTrue(item["text"].endswith(f"（全文 {path}）"))

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

    def test_a_turn_whose_result_the_owner_was_sent_says_so_and_a_thread_report_of_it_is_not_sent_again(self):
        # T601, 2026-11-20: a member turn the owner waits for had its result sent to him (app/relay_targets.py
        # awaited_target); its receipt says so, and the lead's wake-up turn that only retells it stays quiet.
        from covey.app.relay_send import relay_path

        self.thread(THREAD, "对比报告", 41)
        job = self.job(THREAD, final_text="报告写好了")
        write_json(relay_path(self.state, job_key(job.message_id, job.job_id)), {"state": "sent", "outcome": "done"})
        self.after(job)
        [item] = project_loop.unread(self.state, PID)
        self.assertTrue(item["relayed"])
        self.assertIn(project_lead.RELAYED_NOTE, project_lead.receipt_line(item))
        wake = f"lead-{PID}-1"
        project_lead.take_receipts(self.state, PID, wake)
        style = {"report": "each_thread", "detail": "简"}
        note = {"id": "n1", "at": NOW, "text": "报告出了", "report": True, "kind": "thread", "job": wake}
        projects.update(self.state, PID, lambda record: record.update(style=style, notes=[note]))
        self.assertFalse(project_loop.reported(self.state, LEAD, wake))
        # A milestone of it is the lead's own judgment: it goes.
        projects.update(self.state, PID, lambda record: record.update(notes=[{**note, "kind": "milestone"}]))
        self.assertTrue(project_loop.reported(self.state, LEAD, wake))

    def test_a_result_not_sent_to_the_owner_is_the_leads_to_tell_as_before(self):
        self.thread(THREAD, "迁移", 41)
        self.after(self.job(THREAD, final_text="好了"))
        [item] = project_loop.unread(self.state, PID)
        self.assertNotIn("relayed", item)
        wake = f"lead-{PID}-1"
        project_lead.take_receipts(self.state, PID, wake)
        note = {"id": "n1", "at": NOW, "text": "迁完了", "report": True, "kind": "thread", "job": wake}
        projects.update(self.state, PID, lambda record: record.update(style={"report": "each_thread"}, notes=[note]))
        self.assertTrue(project_loop.reported(self.state, LEAD, wake))

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

    def cards(self):
        cards = Cards.__new__(Cards)
        cards.runtime = mock.Mock()
        cards.create_streaming_reply = mock.Mock(return_value=SimpleNamespace(card_id="c9", message_id="om_card"))
        return cards

    def test_every_turn_of_the_lead_is_quiet_and_never_shows_a_card(self):
        # The owner, 2026-11-22 (负责人一律不往私聊顶层吐过程卡): no task card of the lead's at the private chat's top level, whatever started
        # the turn: a wake-up, his message quoting the 专项卡 or his words on the web page, the butler's project_send, an
        # answer's continuation, a supplement queued again as a turn of its own (core/placement.py Row.task_card).
        cards = self.cards()
        for message in (f"lead-{PID}-1", "om_1", "web-op-1", "main-send1", "answer-tok", "requeue-tok"):
            job = self.job(topic=self.lead_topic, message=message, card_deferred=True)
            self.assertTrue(placement.quiet(self.state, job), message)
            self.assertTrue(placement.cardless(self.state, job), message)
            self.assertEqual(placement.marked(self.state, job), message == "om_1", message)  # Reactions on his own only.
            job.timings["backend_requested"] = time.time() - 600
            self.assertFalse(cards.reveal(self.ctx, job, "x"), message)
        cards.create_streaming_reply.assert_not_called()

    def test_the_butlers_turns_and_the_topics_layout_are_as_before(self):
        cards = self.cards()
        butler_turn = self.job(message="om_1", card_deferred=True)  # The butler's own: its card after CARD_AFTER.
        self.assertFalse(placement.cardless(self.state, butler_turn))
        butler_turn.timings["backend_requested"] = time.time() - 600
        self.assertTrue(cards.reveal(self.ctx, butler_turn, "x"))
        self.assertFalse(placement.quiet(self.state, self.job(message="main-tool-1")))  # A turn its tools started.
        with mock.patch.dict(os.environ, {butler.ENV: "0"}):  # The butler off: what is left of a lead, in its thread.
            for message in ("om_1", "main-send1"):
                job = self.job(topic=self.lead_topic, message=message)
                self.assertFalse(placement.quiet(self.state, job), message)
                self.assertFalse(placement.cardless(self.state, job), message)

    def test_a_turn_the_butler_handed_on_says_its_answer_and_its_failure_at_the_top_level_with_no_card(self):
        # project_send's turn ("main-…") is the owner's, not a wake-up: its answer goes out, a new top-level message.
        delivery = self.deliver(self.job(topic=self.lead_topic, message="main-send1"), reply="收到，开了 T12 去迁。")
        self.assertFalse(delivery.silent)
        self.send_text.assert_called_once()
        self.assertEqual(self.send_text.call_args.args[0].reply_to_message_id, "")
        self.assertEqual(self.emojis(), [])  # No message of the owner's to react on.
        self.deliver(self.job(topic=self.lead_topic, message="main-send2"), status="failed", verified=False,
                     reply="任务 #7 处理出错（X），未满足结果发布条件。")
        call = self.send_text.call_args
        self.assertEqual(call.args[0].reply_to_message_id, "")  # The few words a quiet turn's failure gets (bare).
        self.assertIn("card", call.kwargs)

    def test_the_you_asked_of_a_turn_the_butler_handed_on_opens_the_owners_message(self):
        # project_send's turn ("main-<token>") has no message of the owner's and the lead no thread: its answer's 你问 (and
        # the row's click) opens the message the butler took, from that request's record (core/butler_opened.py).
        from covey.app import answer_cards
        from covey.core.jobs import upsert_job_record
        butler_key = job_key("om_owner", 3)
        upsert_job_record(self.state, butler_key, message_id="om_owner", job_id=3, chat_id=BUTLER_CHAT,
                          topic_id=butler.butler_topic_id(BUTLER_CHAT))
        write_json(self.state / "main-thread" / "opened" / "tok1.json",
                   {"job_key": butler_key, "chat_id": BUTLER_CHAT, "lead_topic_id": self.lead_topic, "project_id": PID})
        job = self.job(topic=self.lead_topic, message="main-tok1", user_text="迁移接着做")
        self.assertEqual(answer_cards.owner_message(self.state, job), "om_owner")
        with mock.patch.object(answer_cards, "ask_link", return_value="https://applink.feishu.cn/x") as link:
            self.assertEqual(answer_cards.ask_url(self.state, job), "https://applink.feishu.cn/x")
        self.assertEqual(link.call_args.args[3], "om_owner")
        # A butler's turn that was itself an answer: the message its question was asked from.
        write_json(self.state / "questions" / "q1.json", {"token": "q1", "job_key": butler_key})
        answer_key = job_key("answer-q1", 4)
        upsert_job_record(self.state, answer_key, message_id="answer-q1", job_id=4, chat_id=BUTLER_CHAT,
                          topic_id=butler.butler_topic_id(BUTLER_CHAT))
        write_json(self.state / "main-thread" / "opened" / "tok2.json", {"job_key": answer_key})
        self.assertEqual(answer_cards.owner_message(self.state, self.job(topic=self.lead_topic, message="main-tok2")),
                         "om_owner")
        # No record of the request, or no token: no message (the block keeps its words, without a link), as before.
        for message in ("main-gone", "main-", "main-../opened/tok1"):
            self.assertEqual(answer_cards.owner_message(self.state, self.job(topic=self.lead_topic, message=message)), "",
                             message)

    def test_a_retry_of_a_leads_turn_an_older_release_showed_a_card_for_gets_none(self):
        from covey.app.task_controls import TaskControls
        controls = TaskControls.__new__(TaskControls)
        controls.harness = SimpleNamespace(name="claude", spec=SimpleNamespace(name="claude", native_default_model=True))
        controls.voice = SimpleNamespace(key=lambda *parts: "-".join(map(str, parts)))
        controls.runtime = mock.Mock()
        controls.runtime.jobs.next_id.return_value = 8
        controls.messenger = mock.Mock()
        controls.cards = mock.Mock()
        controls.cards.replace_record_card.return_value = True
        controls.models = mock.Mock()
        controls.models.effort_preset.side_effect = lambda ctx, model, effort: (model, effort)
        controls.record_job_state = mock.Mock()
        record = {"key": job_key("main-send1", 7), "job_id": 7, "message_id": "main-send1", "chat_id": BUTLER_CHAT,
                  "topic_id": self.lead_topic, "reply_to_message_id": "", "user_text": "接着迁", "status": "failed",
                  "card_message_id": "om_old_card"}
        with mock.patch("covey.app.task_controls.strict_recorded_model_settings", return_value=("m", "")), \
                mock.patch("covey.app.task_controls.recovery_card", return_value={"body": {"elements": []}}):
            controls.requeue(dataclasses.replace(self.ctx, topic_id=self.lead_topic, reply_to_message_id=""), BUTLER_CHAT,
                             "auto-retry-7-1", record, automatic=1)
        job = controls.runtime.enqueue_reply_job.call_args.args[0]
        controls.cards.create_streaming_reply.assert_not_called()
        self.assertIsNone(job.progress)
        self.assertTrue(job.card_deferred)
        self.assertEqual(placement.owner_message(job), "main-send1")
        self.assertTrue(placement.quiet(self.state, job))
        controls.messenger.react.assert_not_called()  # No message of the owner's to react on.

    def test_the_reply_is_not_sent_and_counts_as_delivered(self):
        delivery = self.deliver(self.lead(), reply="收到，等线程。")
        self.assertTrue(delivery.silent)
        self.send_text.assert_not_called()
        self.artifacts.send_result_file.assert_not_called()
        states = [call for call in delivery.record_job_state.call_args_list]
        self.assertTrue(all(call.kwargs.get("delivery_ok") is not False for call in states[1:]), states)
        self.assertTrue(states[-1].kwargs["delivery_ok"])

    def test_a_report_note_its_style_lets_through_goes_on_its_own_and_the_reply_stays(self):
        # W1-A C4 (the report channel): the note as the lead wrote it, under who says it, at the top level; never the reply.
        job = self.lead()
        key = job_key(job.message_id, job.job_id)
        self.note(key)
        delivery = self.deliver(job, reply="第一批迁完了，我还顺手看了日志……")
        self.assertTrue(delivery.silent)
        self.send_text.assert_called_once()
        where, chat, text, idem = self.send_text.call_args.args
        self.assertEqual((where.reply_to_message_id, chat, idem), ("", BUTLER_CHAT, f"lead-report-{key}"))
        self.assertEqual(text, "专项「迁移账单」负责人：第一批迁完")
        self.assertNotIn("日志", text)

    def test_several_report_notes_go_as_one_message_and_a_detailed_style_says_what_the_turn_did(self):
        from covey.app import project_tools

        job = self.lead()
        key = job_key(job.message_id, job.job_id)
        rows = [{"id": "n1", "at": NOW, "text": "第一批迁完", "report": True, "kind": "milestone", "job": key},
                {"id": "n2", "at": NOW, "text": "T12 对账一致", "report": True, "kind": "thread", "job": key}]
        projects.update(self.state, PID, lambda record: record.update(notes=rows, style={"report": "each_thread", "detail": "简"}))
        project_tools.acted(SimpleNamespace(state_dir=self.state), PID, SimpleNamespace(job_key=key), "start")
        self.deliver(job)
        text = self.send_text.call_args.args[2]
        self.assertEqual(text.splitlines()[:3], ["专项「迁移账单」负责人：", "- 第一批迁完", "- T12 对账一致"])
        self.assertEqual(text.splitlines()[-1], "这一轮：开了话题。")
        # The newer rows say what a turn did as ``whats``.
        projects.update(self.state, PID, lambda record: record.update(acted=[{"job": key, "whats": ["send", "stop"], "at": NOW}]))
        self.deliver(job)
        self.assertEqual(self.send_text.call_args.args[2].splitlines()[-1], "这一轮：给话题加了话、停了话题。")
        # Under milestones: no line of what it did, and the thread note is held back by the style.
        projects.update(self.state, PID, lambda record: record.update(style={"report": "milestones", "detail": "简"}))
        self.deliver(job)
        self.assertEqual(self.send_text.call_args.args[2], "专项「迁移账单」负责人：第一批迁完")

    def test_a_report_note_the_style_holds_back_is_not_sent_either(self):
        job = self.lead()
        self.note(job_key(job.message_id, job.job_id), kind="thread")
        delivery = self.deliver(job, reply="线程做完了。")
        self.assertTrue(delivery.silent)
        self.send_text.assert_not_called()
        self.assertFalse(project_loop.report_out(delivery))

    def test_a_note_that_is_not_a_report_or_is_another_turns_changes_nothing(self):
        job = self.lead()
        self.note(job_key(job.message_id, job.job_id), report=False)
        self.assertTrue(self.deliver(job).silent)
        self.note("other-key")
        self.assertTrue(self.deliver(job).silent)

    def test_a_wake_up_turn_that_only_did_things_tells_nobody(self):
        # W1-A C4: what a wake-up turn changed (app/project_tools.py acted) no longer sends its whole reply (2026-11-19's
        # 有变化才发); the owner hears of it through its report notes and its questions.
        from covey.app import project_tools

        job = self.lead()
        project_tools.acted(SimpleNamespace(state_dir=self.state), PID,
                            SimpleNamespace(job_key=job_key(job.message_id, job.job_id)), "start")
        delivery = self.deliver(job, reply="开了 T12 去迁第二批。")
        self.assertTrue(delivery.silent)
        self.send_text.assert_not_called()

    def deliver_waiting(self, job, shown=True, reply="要不要部署这一批？"):
        """``deliver`` of a wake-up turn that asked the owner (project_ask): it waits for his answer, its question card to go
        (``shown``: whether Questions.deliver_checkpoint_questions sent it)."""
        from covey.app import butler_reply
        from covey.app.turn_delivery import TurnDelivery
        from test_butler_quiet import ASK

        job.checkpoint_question_deferred = True
        turn = SimpleNamespace(status="waiting_user", status_message="", final_text=reply,
                               verified_delivery=SimpleNamespace(text=reply, fingerprint="f1"),
                               verified_outcome=SimpleNamespace(status="completed", notice=lambda: "", error_code=""),
                               outcome=None, allow_resume=True)
        self.send_text = mock.Mock(return_value=True)
        questions = mock.Mock()
        questions.deliver_checkpoint_questions.return_value = shown
        delivery = TurnDelivery(job, self.ctx, turn, prefix="p", cards=DeliverCase.cards(self), runtime=mock.Mock(),
                                artifacts=mock.Mock(), questions=questions, steering=mock.Mock(),
                                record_job_state=mock.Mock(), send_text=self.send_text, react=mock.Mock(return_value=True))
        with mock.patch.object(TurnDelivery, "texts", return_value=(reply, reply, False)), \
                mock.patch.object(TurnDelivery, "ask_block", return_value=ASK), \
                mock.patch.object(TurnDelivery, "answer_tail", return_value=([], False, None)), \
                mock.patch("covey.app.turn_delivery.close_streaming_reply_async"):
            delivery.deliver(None)
            butler_reply.react_end(delivery)
        return delivery

    def test_a_wake_up_waiting_on_its_question_sends_the_card_and_not_the_reply(self):
        # W1-A C4 ④: the turn that asked with project_ask waits for the owner; its question card goes, its reply does not.
        delivery = self.deliver_waiting(self.lead())
        self.assertTrue(delivery.silent)
        self.send_text.assert_not_called()
        delivery.questions.deliver_checkpoint_questions.assert_called_once()
        last = delivery.record_job_state.call_args_list[-1]
        self.assertEqual((last.kwargs["delivery_ok"], last.kwargs["question_delivery_ok"]), (True, True))

    def test_a_question_card_that_does_not_go_says_so_and_is_never_swallowed(self):
        # W1-A C4 ④: the reply is held back, but a question card that could not be sent is said, and the turn can be
        # redelivered (its record says the question did not go), as any turn's.
        job = self.lead()
        delivery = self.deliver_waiting(job, shown=False)
        self.assertTrue(delivery.silent)
        [call] = self.send_text.call_args_list
        self.assertIn("问题控件未能发送", call.args[2])
        self.assertEqual(call.args[3], f"p-question-delivery-failed-{job.message_id}")
        last = delivery.record_job_state.call_args_list[-1]
        self.assertEqual((last.args[2], last.kwargs["question_delivery_ok"]), ("failed", False))
        self.assertIn("问题控件发送失败", last.args[3])

    def test_a_wake_up_paused_on_a_native_question_is_not_hidden(self):
        job = self.lead()
        job.paused_question_delivery = {"kind": "native"}
        delivery = SimpleNamespace(job=job, ctx=self.ctx, runtime=mock.Mock(), turn=SimpleNamespace(
            status="waiting_user", verified_delivery=object(), verified_outcome=SimpleNamespace(status="completed")))
        self.assertFalse(project_loop.silent(delivery))
        job.paused_question_delivery = None
        self.assertTrue(project_loop.silent(delivery))

    def test_the_owners_words_steered_into_a_wake_up_turn_make_it_his(self):
        # W0-2 (T892 P0-5): the butler's project_send went into the running wake-up turn as a supplement; the turn stayed a
        # wake-up (its message id), its reply was held back and the wake-ups went on counting.
        from covey.core.jobs import job_path, read_job_record, upsert_job_record

        job, other = self.lead(), self.lead(f"lead-{PID}-2")
        for turn in (job, other):
            upsert_job_record(self.state, job_key(turn.message_id, turn.job_id), message_id=turn.message_id,
                              job_id=turn.job_id, topic_id=self.lead_topic, chat_id=BUTLER_CHAT, status="running")
        projects.update(self.state, PID, lambda record: record.update(wakes={"count": 9, "streak": 9, "recent": [NOW]}))
        self.assertTrue(project_loop.woken(self.state, job))
        project_loop.owner_steered(self.state, job)
        self.assertEqual(projects.read_record(self.state, PID)["wakes"]["streak"], 0)
        record = read_job_record(job_path(self.state, job_key(job.message_id, job.job_id)))
        self.assertIs(record[projects.OWNER_PRESENT], True)
        self.assertFalse(project_loop.woken(self.state, job))
        self.assertFalse(projects.woken(record))
        delivery = self.deliver(job, reply="收到，改成先迁 orders 表。")
        self.assertFalse(delivery.silent)
        self.send_text.assert_called_once()
        # A task runner's job of the same turn has no such attribute: its record says it.
        runner_job = self.lead()
        self.assertFalse(project_loop.woken(self.state, runner_job))
        self.assertTrue(self.deliver(other, reply="还在等 T12。").silent)  # Another wake-up: as before.

    def steering(self):
        from covey.app.steering import Steering

        steering = Steering.__new__(Steering)
        steering.runtime = SimpleNamespace(log=mock.Mock())
        return steering

    def running(self, job):
        from covey.core.jobs import upsert_job_record

        upsert_job_record(self.state, job_key(job.message_id, job.job_id), message_id=job.message_id, job_id=job.job_id,
                          topic_id=self.lead_topic, chat_id=BUTLER_CHAT, status="running")
        return job

    def test_a_supplement_into_a_wake_up_turn_marks_the_owner_there_before_it_goes(self):
        # P1-8 (Wave 0 F5): the mark is on the record before the supplement is sent; one that surely did not go takes it back.
        from covey.app.steering import Steering
        from covey.core.steer import SteerNotReady, SteerRefused, SteerUncertain

        steering, job = self.steering(), self.running(self.lead())
        seen = []

        def send(*args, **kwargs):
            seen.append(project_loop.woken(self.state, self.lead()))  # A task runner's job of the same turn, from disk.
            return {"status": "sent"}
        with mock.patch.object(Steering, "_deliver_steer", side_effect=send):
            steering.deliver_steer(self.ctx, job, "先迁 orders", "main-tok")
        self.assertEqual(seen, [False])  # Already his while it went.
        self.assertFalse(project_loop.woken(self.state, job))
        for number, refusal in enumerate((SteerRefused("任务已结束或已取消"), SteerNotReady("还没准备好")), 5):
            with self.subTest(refusal=type(refusal).__name__):
                job = self.running(self.lead(f"lead-{PID}-{number}"))
                with mock.patch.object(Steering, "_deliver_steer", side_effect=refusal), self.assertRaises(type(refusal)):
                    steering.deliver_steer(self.ctx, job, "先迁 orders", "main-tok")
                self.assertTrue(project_loop.woken(self.state, job))  # Nothing went in: taken back.
                self.assertTrue(project_loop.woken(self.state, self.lead(job.message_id)))
        job = self.running(self.lead(f"lead-{PID}-8"))
        with mock.patch.object(Steering, "_deliver_steer", return_value={"status": "refused"}):
            steering.deliver_steer(self.ctx, job, "先迁 orders", "main-tok")
        self.assertTrue(project_loop.woken(self.state, job))
        job = self.running(self.lead(f"lead-{PID}-9"))
        with mock.patch.object(Steering, "_deliver_steer", side_effect=SteerUncertain("没确认")), self.assertRaises(SteerUncertain):
            steering.deliver_steer(self.ctx, job, "先迁 orders", "main-tok")
        self.assertFalse(project_loop.woken(self.state, job))  # It may have gone: his.

    def test_a_turn_that_takes_the_supplement_and_ends_at_once_sends_its_reply(self):
        # P1-8 (fable RT|steer_then_judge_then_marker): the supplement went in, the turn ended and was judged before the
        # Bridge wrote the mark; its reply was held back and no new turn came. Now the judgment sees the mark.
        from covey.app.steering import Steering

        steering, job = self.steering(), self.running(self.lead())
        judged = []

        def send(*args, **kwargs):
            judged.append(self.deliver(self.lead(), reply="收到，改成先迁 orders 表。").silent)  # The runner, at once.
            return {"status": "sent"}
        with mock.patch.object(Steering, "_deliver_steer", side_effect=send):
            steering.deliver_steer(self.ctx, job, "先迁 orders", "main-tok")
        self.assertEqual(judged, [False])
        self.send_text.assert_called_once()

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


class RelayRaceTest(DeliverCase):
    """§5.4 R3: a member's result the owner gets by its relay is not told again by the lead's report, read when the lead
    reports (a merged relay waits ``pending`` a moment; the Bridge's own relay may come after the receipt), and a relay
    that failed leaves the report to tell it."""

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
        write_topic(self.state, topic_id=THREAD, chat_id=BUTLER_CHAT, harness_id="claude", topic_number=41,
                    project=projects.project_field(PID, projects.MEMBER))
        projects.add_member(self.state, PID, THREAD)
        projects.update(self.state, PID, lambda record: record.update(style={"report": "each_thread", "detail": "简"}))
        self.logs = []
        self.member = job_key("om_t1", 41)
        write_job(self.state, self.member, topic_id=THREAD, chat_id=BUTLER_CHAT, job_id=41, message_id="om_t1", status="done",
                  final_text="报告写好了")

    def relay(self, state):
        from covey.app.relay_send import relay_path

        write_json(relay_path(self.state, self.member), {"state": state, "outcome": "done", "job_key": self.member})

    def ended(self):
        """The member's turn ends (its receipt) and the lead's wake-up takes it and reports it as a thread note."""
        project_loop.after_runner(self.logs.append, self.state, make_job(job_id=41, chat_id=BUTLER_CHAT, message_id="om_t1",
                                                                          topic_id=THREAD))
        job = self.job(topic=self.lead_topic, message=f"lead-{PID}-1")
        key = job_key(job.message_id, job.job_id)
        project_lead.take_receipts(self.state, PID, key)
        note = {"id": "n1", "at": NOW, "text": "T41 报告出了", "report": True, "kind": "thread", "job": key}
        projects.update(self.state, PID, lambda record: record.update(notes=[note]))
        return job

    def test_a_relay_waiting_in_its_merge_window_is_not_told_again(self):
        self.relay("pending")  # The runner left its relay to be merged with the chat's others a moment later.
        job = self.ended()
        delivery = self.deliver(job, reply="T41 报告出了，结论……")
        self.assertTrue(delivery.silent)
        self.send_text.assert_not_called()  # The owner gets it once: from the merged relay.
        self.relay("sent")
        self.deliver(job)
        self.send_text.assert_not_called()

    def test_a_relay_the_bridge_wrote_after_the_receipt_counts_when_the_lead_reports(self):
        job = self.ended()  # No relay yet: the receipt says nothing of one.
        [item] = project_lead.taken_receipts(self.state, PID, job_key(job.message_id, job.job_id))
        self.assertNotIn("relayed", item)
        for state in ("pending", "submitting", "uncertain"):
            with self.subTest(state=state):
                self.relay(state)
                self.deliver(job)
                self.send_text.assert_not_called()

    def test_a_relay_that_failed_leaves_the_report_to_tell_it(self):
        self.relay("pending")
        job = self.ended()
        self.relay("failed")  # Feishu refused the merged relay: the lead's report is the owner's way to hear it.
        self.deliver(job)
        self.send_text.assert_called_once()
        self.assertEqual(self.send_text.call_args.args[2], "专项「迁移账单」负责人：T41 报告出了")
        self.relay("unsent")
        self.deliver(job)
        self.send_text.assert_called_once()

    def test_a_receipt_that_names_its_task_is_read_by_it(self):
        self.relay("failed")
        item = {"kind": "thread_end", "thread": THREAD, "text": "T41 的任务 #41 做完：x", "relayed": True}
        self.assertEqual(project_loop.receipt_job(self.state, item), self.member)
        self.assertFalse(project_loop.receipt_told(self.state, item))  # Its relay now, not what the receipt said.
        self.assertTrue(project_loop.receipt_told(self.state, {**item, "text": "别的话题做完"}))  # No task: as it said.
        self.assertEqual(project_loop.receipt_job(self.state, {**item, "job": "k-given"}), "k-given")


class CardWordsTest(Loop):
    def test_a_paused_project_says_why(self):
        for reason, words in (("tokens", "token 用完了"), ("wakes", "连续唤醒次数到上限"), ("stalled", "很久没有动静")):
            self.update(state=projects.PAUSED, state_reason=reason)
            self.assertEqual(facts(self.state, self.reload(), NOW).lead_state, f"停住了 · {words}")
            self.assertEqual(facts(self.state, self.reload(), NOW).status, "stopped")  # 停住了 · why, red (P0-3).
            self.assertEqual(facts(self.state, self.reload(), NOW).reason, words)
        self.update(state=projects.PAUSED, state_reason="主人停止")
        self.assertEqual(facts(self.state, self.reload(), NOW).lead_state, "待命")
        self.assertEqual((facts(self.state, self.reload(), NOW).status, facts(self.state, self.reload(), NOW).reason),
                         ("cancelled", ""))  # 已停止: the owner's own stop.
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
