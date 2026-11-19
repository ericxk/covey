"""整理专项 across projects (T574, the owner, 2026-11-20: 会不会把 A 专项的某些话题、B 专项的某些话题和不在专项里的话题组合到一个新
专项里去？): which suggestions of the small model get through (app/project_sorter.py parse), what a split does to the project
it takes topics from and to the new one (app/project_tidy.py apply, app/projects.py gather/leave_topic), and what two proposals
about the same topic do one after the other. A test marked expectedFailure is a gap found then, with the behaviour wanted."""
from __future__ import annotations

import json
import time
import unittest
from types import SimpleNamespace

from test_dispatch_cards import CHAT
from test_project_tidy import TidyCase

from covey.app import project_lead, project_tools
from covey.app import projects as upgrades
from covey.core import projects
from covey.core import project_proposals as proposals
from covey.app import project_sorter as sorter


def item(topic, project=""):
    return SimpleNamespace(topic=topic, project=project)


class ParseTest(unittest.TestCase):
    """What the small model may suggest: a new project of loose topics, a split within one project, a merge of whole projects."""

    def setUp(self):
        t = self.t = {n: f"{n:x}".rjust(32, "c") for n in range(1, 8)}  # Topic ids as core/projects.py TOPIC_RE takes them.
        self.a = {"id": "aaaaaa000001", "name": "A", "threads": [t[1], t[2], t[3]], "state": "active"}
        self.b = {"id": "bbbbbb000002", "name": "B", "threads": [t[4], t[5]], "state": "active"}
        self.items = [item(t[1], self.a["id"]), item(t[2], self.a["id"]), item(t[3], self.a["id"]),
                      item(t[4], self.b["id"]), item(t[5], self.b["id"]), item(t[6]), item(t[7])]

    def parse(self, *suggestions):
        text = json.dumps({"suggestions": [{"why": "", "confidence": 0.9, **row} for row in suggestions]})
        return sorter.parse(text, [self.a, self.b], self.items)

    def test_a_new_project_of_topics_from_two_projects_and_loose_ones_is_dropped(self):
        for topics in ([1, 4, 6], [1, 4], [1, 6], [4, 6, 7]):
            with self.subTest(topics=topics):
                self.assertEqual(self.parse({"kind": "create", "topics": topics, "name": "混合", "goal": "做完"}), [])
        [kept] = self.parse({"kind": "create", "topics": [6, 7], "name": "散的", "goal": "做完"})
        self.assertEqual(kept["topics"], [self.t[6], self.t[7]])

    def test_a_split_takes_topics_of_its_own_project_only_and_never_all_of_them(self):
        for topics in ([1, 4], [1, 6], [1], [1, 2, 3]):
            with self.subTest(topics=topics):
                self.assertEqual(self.parse({"kind": "split", "project": 1, "topics": topics, "name": "拆", "goal": "g"}), [])
        [kept] = self.parse({"kind": "split", "project": 1, "topics": [1, 2], "name": "拆", "goal": "g"})
        self.assertEqual((kept["project"], kept["topics"]), (self.a["id"], [self.t[1], self.t[2]]))

    def test_a_merge_takes_whole_projects(self):
        [kept] = self.parse({"kind": "merge", "projects": [1, 2], "name": "合"})
        self.assertEqual((kept["project"], kept["projects"], kept["topics"]),
                         (self.a["id"], [self.a["id"], self.b["id"]], [self.t[n] for n in range(1, 6)]))


class SplitTest(TidyCase):
    def setUp(self):
        super().setUp()
        self.t = [self.topic(f"话题 {n}") for n in range(5)]
        self.a = self.project("A 专项", self.t[:3])
        self.b = self.project("B 专项", self.t[3:])

    def split(self, pid, topics, name="拆出的专项"):
        made = proposals.new_proposal(proposals.SPLIT, topics=topics, project=pid, name=name, goal="做完", why="")
        saved, _ = proposals.put_proposal(self.state, made)
        return saved

    def apply(self, record):
        return self.tidy.apply(self.ctx, CHAT, record)

    def record(self, pid):
        return projects.read_record(self.state, pid)

    def made(self, proposal):
        return self.record(upgrades.project_id("tidy", proposal["id"]))

    def test_a_split_moves_its_topics_out_once_and_tells_the_lead_it_took_them_from(self):
        proposal = self.split(self.a, self.t[:2])
        self.assertEqual(self.apply(proposal), (True, "已拆出专项「拆出的专项」"))
        source = self.record(self.a)
        self.assertEqual((source["state"], source["threads"]), (projects.ACTIVE, [self.t[2]]))
        left = [row for row in project_lead.take_receipts(self.state, self.a, "k") if row["kind"] == upgrades.LEFT]
        self.assertEqual(sorted(row["thread"] for row in left), sorted(self.t[:2]))
        self.assertIn("话题（1 个", project_lead.state_pack(self.state, source, "k2", full=False, receipts=[]))
        new = self.made(proposal)
        self.assertEqual((new["state"], new["origin"]["kind"], new["threads"]), (projects.ACTIVE, "tidy", self.t[:2]))
        for topic in self.t[:2]:
            self.assertEqual(projects.topic_project(self.state, topic).id, new["id"])
        # The new project is not finished by its lead's first wake-up turn (T574's gate).
        self.assertIn("收进来的话题做完不等于专项完成", project_tools.finish_gate(new))
        self.assertIn("不要收尾", [row for row in project_lead.take_receipts(self.state, new["id"], "k3")
                                  if row["kind"] == "start"][0]["text"])

    def test_a_split_with_three_projects_on_is_not_made(self):
        self.assertTrue(self.apply(self.split(self.a, self.t[:2], "一"))[0])
        self.assertEqual(self.apply(self.split(self.a, [self.t[2]], "二")), (False, "已经有 3 个专项在做，没拆"))

    def test_a_split_of_a_topic_another_proposal_moved_first_leaves_it_where_it_went(self):
        projects.set_state(self.state, self.b, projects.PAUSED)  # Under MAX_ACTIVE for the second split.
        first, second = self.split(self.a, self.t[:2], "一"), self.split(self.a, self.t[1:3], "二")
        self.assertTrue(self.apply(first)[0])
        self.assertTrue(self.apply(second)[0])
        self.assertEqual(self.made(first)["threads"], self.t[:2])
        self.assertEqual(self.made(second)["threads"], [self.t[2]])  # A project of one topic now.
        self.assertEqual(self.record(self.a)["threads"], [])
        self.assertEqual(self.record(self.a)["state"], projects.ACTIVE)  # Left on and empty: it folds after the idle days.

    def test_a_split_of_a_project_merged_away_since_fails_and_says_so(self):
        merge = proposals.new_proposal(proposals.MERGE, topics=[*self.t], project=self.b, projects_=[self.b, self.a], name="")
        self.assertTrue(self.apply(merge)[0])
        self.assertEqual(self.record(self.a)["state"], projects.CANCELLED)
        self.assertEqual(self.record(self.b)["threads"], [*self.t[3:], *self.t[:3]])
        self.assertEqual(self.apply(self.split(self.a, self.t[:2])), (False, "这个专项已经结束了"))
        self.assertEqual(self.record(self.b)["threads"], [*self.t[3:], *self.t[:3]])  # Nothing moved twice.

    @unittest.expectedFailure
    def test_gap_a_split_keeps_out_a_topic_the_owner_took_out_of_projects_since(self):
        proposal = self.split(self.a, self.t[:2])
        self.tidy.leave(self.ctx, CHAT, self.t[0], self.a)  # 移出: never_auto, kept out of projects for good.
        self.assertTrue(self.apply(proposal)[0])
        self.assertNotIn(self.t[0], self.made(proposal)["threads"])

    @unittest.expectedFailure
    def test_gap_an_open_split_follows_its_topics_leaving_the_project(self):
        proposal = self.split(self.a, self.t[:2])
        upgrades.leave_topic(self.state, self.a, self.t[0], why="")
        upgrades.join_topic(self.state, self.b, self.t[0])
        self.tidy.follow(self.ctx, CHAT)
        self.assertNotEqual(proposals.read_proposal(self.state, proposal["id"])["state"], proposals.OPEN)

    @unittest.expectedFailure
    def test_gap_a_split_that_would_leave_one_topic_or_none_is_not_made(self):
        projects.set_state(self.state, self.b, projects.PAUSED)
        first, second = self.split(self.a, self.t[:2], "一"), self.split(self.a, self.t[1:3], "二")
        self.apply(first)
        ok, words = self.apply(second)
        self.assertFalse(ok, words)


class TypedNumberTest(TidyCase):
    @unittest.expectedFailure
    def test_gap_a_topic_of_another_project_typed_on_the_proposal_card_is_named_as_left_out(self):
        from unittest import mock
        from covey.app import project_tidy as tidy_module
        from covey.cards import project_proposal as cards
        from covey.core.state import read_object
        self.filler()
        pair = [self.topic("网关失联"), self.topic("网关扩容")]
        other = self.topic("别的专项的话题")
        self.project("别的专项", [other])
        self.tidy.recent_topics = lambda state, chat, now, **kw: list(pair)
        self.verdicts.append({"suggestions": [{"kind": "create", "topics": [1, 2], "name": "网关稳定", "goal": "不失联",
                                               "why": "", "confidence": 0.9}]})
        self.tidy.realtime(self.ctx, CHAT, pair[0], "new", time.time())
        record = proposals.proposals(self.state, None)[-1]
        number = read_object(self.state / "topics" / other / "topic.json")["topic_number"]
        with mock.patch.object(tidy_module.topic_links, "topic_numbers", return_value={number: other}):
            self.click({"kind": cards.NAMED_KIND, "proposal": record["id"]}, record["card"]["message_id"],
                       topics=["0", "1"], more=f"T{number}", name="网关稳定")
        made = projects.read_record(self.state, upgrades.project_id("tidy", record["id"]))
        self.assertEqual(made["threads"], pair)  # Today: left in its project, as it should be...
        view = proposals.read_proposal(self.state, record["id"])["view"]
        self.assertIn(f"T{number}", view["note"])  # ...but the card's note does not say so,
        self.assertEqual(len(view["labels"]), 2)  # and its list names it among the new project's topics.
