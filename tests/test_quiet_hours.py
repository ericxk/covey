"""夜里攒到早上 (core/quiet_hours.py, T604 #49): the quiet hours from COVEY_QUIET_HOURS or the 早晚汇总's times; a 整理专项
proposal the real-time look found at night waits for the morning (app/tidy_proposals.py), so does a node alert that is
not severe (app/node_alerts.py). Off, or without the hours, every card goes at once as before."""
from __future__ import annotations

import datetime
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from support import make_context
from test_nodes import Access, FakeMessenger, Runtime, Voice
from test_project_tidy import A, B, CHAT, TidyCase
from covey.app import node_alerts
from covey.cards import node_cards
from covey.core import nodes, quiet_hours
from covey.core import project_proposals as proposals
from covey.core.state import read_object


def at(hour, minute=0, day=20):
    """Epoch seconds of 2026-11-<day> hour:minute in Asia/Shanghai."""
    return datetime.datetime(2026, 11, day, hour, minute, tzinfo=quiet_hours.ZONE).timestamp()


def around_now(minutes=60):
    """A COVEY_QUIET_HOURS value whose hours hold the present moment, ``minutes`` either side."""
    now = datetime.datetime.now(quiet_hours.ZONE)
    start, end = now - datetime.timedelta(minutes=minutes), now + datetime.timedelta(minutes=minutes)
    return f"{start:%H:%M}-{end:%H:%M}"


class WindowTest(unittest.TestCase):
    def test_the_setting_else_the_digest_times_from_the_last_to_the_first(self):
        self.assertEqual(quiet_hours.window({"COVEY_QUIET_HOURS": "23:00-08:30"}), (23 * 60, 8 * 60 + 30))
        self.assertEqual(quiet_hours.window({"COVEY_DIGEST_TIMES": "09:30,21:30"}), (21 * 60 + 30, 9 * 60 + 30))
        self.assertEqual(quiet_hours.window({"COVEY_DIGEST_TIMES": "21:30, 13:00,09:30"}), (21 * 60 + 30, 9 * 60 + 30))
        self.assertEqual(quiet_hours.window({"COVEY_QUIET_HOURS": "0:30-6:00", "COVEY_DIGEST_TIMES": "09:30,21:30"}),
                         (30, 360))

    def test_none_off_with_one_digest_time_or_a_value_that_is_neither(self):
        for environ in ({}, {"COVEY_DIGEST_TIMES": "09:30"}, {"COVEY_QUIET_HOURS": "off", "COVEY_DIGEST_TIMES": "09:30,21:30"},
                        {"COVEY_QUIET_HOURS": "0"}, {"COVEY_QUIET_HOURS": "夜里"}, {"COVEY_QUIET_HOURS": "23:00"},
                        {"COVEY_QUIET_HOURS": "08:00-08:00"}, {"COVEY_QUIET_HOURS": "25:00-08:00"}):
            self.assertIsNone(quiet_hours.window(environ), environ)
            self.assertFalse(quiet_hours.quiet(at(2), environ))

    def test_the_start_is_in_them_the_end_is_not_across_midnight_or_not(self):
        night = {"COVEY_QUIET_HOURS": "23:00-08:30"}
        self.assertEqual([quiet_hours.quiet(at(h, m), night) for h, m in ((22, 59), (23, 0), (2, 0), (8, 29), (8, 30))],
                         [False, True, True, True, False])
        early = {"COVEY_QUIET_HOURS": "01:00-05:00"}
        self.assertEqual([quiet_hours.quiet(at(h), early) for h in (0, 1, 4, 5, 23)], [False, True, True, False, False])

    def test_when_they_end(self):
        night = {"COVEY_QUIET_HOURS": "23:00-08:30"}
        self.assertEqual(quiet_hours.ends(at(23, 30), night), at(8, 30, day=21))
        self.assertEqual(quiet_hours.ends(at(2), night), at(8, 30))
        self.assertIsNone(quiet_hours.ends(at(12), night))
        self.assertEqual(quiet_hours.words(at(2), night), "到 08:30")
        self.assertEqual(quiet_hours.words(at(12), night), "")


class ProposalNightTest(TidyCase):
    """A proposal the real-time look found at night: no card until the morning, listed on the 看板 meanwhile."""

    def setUp(self):
        super().setUp()
        self.filler()
        self.pair = [self.topic(A, gist={"essence": B}), self.topic("优化 Bootstrap 下一环节", gist={"essence": B})]
        self.see(lambda: list(self.pair))

    def propose(self):
        self.verdicts.append({"suggestions": [{"kind": "create", "topics": [1, 2], "name": "DataLab Prompt 优化",
                                               "goal": "新旧 Prompt 对率可比", "why": "都在改 Prompt", "confidence": 0.9}]})
        return self.tidy.realtime.handle(self.ctx, CHAT, self.pair[0], "new", time.time())

    def test_held_at_night_listed_meanwhile_and_sent_in_the_morning(self):
        with mock.patch.dict(os.environ, {quiet_hours.ENV: around_now()}):
            self.assertEqual(self.propose(), "held")
            self.assertEqual(self.sent_cards(), [])
            (record,) = proposals.proposals(self.state, (proposals.OPEN,))
            self.assertTrue(record["held"])
            self.assertEqual([found["id"] for found in proposals.waiting(self.state)], [record["id"]])  # The 看板's.
            self.assertEqual(self.tidy.proposal_cards.release(self.ctx, CHAT, time.time()), 0)  # Still night.
            self.assertEqual(self.tidy.proposal_cards.release(self.ctx, CHAT, time.time() + 3 * 3600), 1)  # Morning.
        self.assertIn("DataLab Prompt 优化", json.dumps(self.sent_cards()[-1], ensure_ascii=False))
        record = proposals.read_proposal(self.state, record["id"])
        self.assertNotIn("held", record)
        self.assertTrue(record["card"]["message_id"])
        self.assertEqual(self.tidy.proposal_cards.release(self.ctx, CHAT, time.time() + 3 * 3600), 0)  # Once.

    def test_one_decided_on_the_board_at_night_is_not_sent_in_the_morning(self):
        with mock.patch.dict(os.environ, {quiet_hours.ENV: around_now()}):
            self.propose()
        (record,) = proposals.proposals(self.state, (proposals.OPEN,))
        proposals.update_proposal(self.state, record["id"], lambda data: data.update(state=proposals.DECLINED))
        self.assertEqual(self.tidy.proposal_cards.release(self.ctx, CHAT, time.time()), 0)
        self.assertEqual(self.sent_cards(), [])

    def test_off_or_by_day_it_goes_at_once(self):
        with mock.patch.dict(os.environ, {quiet_hours.ENV: "off", "COVEY_DIGEST_TIMES": "09:30,21:30"}):
            self.assertEqual(self.propose(), "proposed")
        self.assertEqual(len(self.sent_cards()), 1)

    def test_one_a_whole_tidy_found_goes_at_once(self):
        made = {**proposals.new_proposal(proposals.CREATE, topics=self.pair, name="DataLab Prompt 优化", goal="对率可比"),
                "run": "r1"}  # A 整理 the owner set the time of or asked for.
        with mock.patch.dict(os.environ, {quiet_hours.ENV: around_now()}):
            self.assertEqual(self.tidy.proposal_cards.propose(self.ctx, CHAT, made), "proposed")


class AlertNightTest(unittest.TestCase):
    """A node alert that is not severe waits out the night, still pending; a severe one goes at once."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.ctx = make_context(self.root / "state")
        self.runtime = Runtime()
        self.runtime.access = Access()
        self.messenger = FakeMessenger()
        self.environ = {"COVEY_NODE_LISTEN": "1", quiet_hours.ENV: around_now()}
        self.alerts = node_alerts.NodeAlerts(self.runtime, self.messenger, Voice(), environ=self.environ)
        nodes.claim(self.ctx.state_dir, nodes.open_join(self.ctx.state_dir, "box", "盒子"), {})

    def event(self, kind, rule="mem", **more):
        folder = nodes.nodes_dir(self.ctx.state_dir) / "events"
        folder.mkdir(parents=True, exist_ok=True)
        event = {"alias": "box", "rule": rule, "kind": kind, "at": time.time(), "id": os.urandom(3).hex(),
                 "pct": 95, "used": 950, "limit": 1000, "top": [{"comm": "rg", "rss": 9 << 30}], **more}
        (folder / f"{time.time_ns()}.json").write_text(json.dumps(event))

    def record(self, rule):
        return read_object(node_alerts.alert_path(self.ctx.state_dir, "box", rule))

    def test_memory_tight_waits_for_the_morning_and_a_kill_goes_at_once(self):
        self.event("raise")
        self.event("once", rule="oom", count=1)
        self.alerts.round(self.ctx)
        self.alerts.round(self.ctx)
        self.assertEqual([text.splitlines()[0] for _, _, text, _ in self.messenger.sent], ["⚠️ **盒子 有进程因内存不足被杀**"])
        self.assertEqual(self.record("mem")["state"], "pending")
        self.assertTrue(self.record("mem")["held"])
        self.assertEqual(sum("node alert held" in line for line in self.runtime.lines), 1)  # Said once.
        self.environ[quiet_hours.ENV] = "off"  # The morning.
        self.alerts.round(self.ctx)
        self.assertEqual(len(self.messenger.sent), 2)
        self.assertIn("盒子 内存紧张", self.messenger.sent[-1][2])

    def test_one_cleared_at_night_is_never_sent(self):
        self.event("raise", rule="disk:/data", pct=93)
        self.alerts.round(self.ctx)
        self.event("clear", rule="disk:/data", pct=80)
        self.alerts.round(self.ctx)
        self.environ[quiet_hours.ENV] = "off"
        self.alerts.round(self.ctx)
        self.assertEqual(self.messenger.sent, [])
        self.assertEqual(self.record("disk:/data")["state"], "dropped")

    def test_which_are_severe(self):
        self.assertEqual([node_alerts.severe(rule) for rule in ("oom", "write:/data", "mem", "disk:/data", "offline")],
                         [True, True, False, False, False])

    def test_a_card_that_waited_since_yesterday_says_the_date(self):
        yesterday = time.time() - 86400
        self.assertRegex(node_cards.since(yesterday), r"^\d\d-\d\d \d\d:\d\d$")
        self.assertRegex(node_cards.since(time.time()), r"^\d\d:\d\d$")


if __name__ == "__main__":
    unittest.main()
