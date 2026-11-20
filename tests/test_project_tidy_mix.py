"""整理专项 across projects (T574, the owner, 2026-11-20: 会不会把 A 专项的某些话题、B 专项的某些话题和不在专项里的话题组合到一个新
专项里去？): which suggestions of the small model get through (app/project_sorter.py parse), what a split does to the project
it takes topics from and to the new one (app/tidy_apply.py apply, app/projects.py gather, app/project_members.py leave_topic), and what two proposals
about the same topic do one after the other; the gaps found then are closed (a split takes only what is still the project's,
two at least, an open one follows its topics, a project left empty folds, the leads hear where the topics went)."""
from __future__ import annotations

import json
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from test_dispatch_cards import CHAT
from test_project_tidy import TidyCase

from covey.app import project_lead, project_tools, topic_links
from covey.cards import project_proposal as cards
from covey.core.state import read_object, write_json
from covey.app import project_members as members, projects as upgrades
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
        left = [row for row in project_lead.take_receipts(self.state, self.a, "k") if row["kind"] == members.LEFT]
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
        self.assertEqual(self.apply(self.split(self.b, self.t[3:], "二")), (False, "已经有 3 个专项在做，没拆"))
        self.assertEqual(self.record(self.b)["threads"], self.t[3:])

    def test_a_split_of_a_topic_another_proposal_moved_first_leaves_it_where_it_went_and_one_left_is_no_project(self):
        projects.set_state(self.state, self.b, projects.PAUSED)  # Under MAX_ACTIVE for the second split.
        first, second = self.split(self.a, self.t[:2], "一"), self.split(self.a, self.t[1:3], "二")
        self.assertTrue(self.apply(first)[0])
        ok, words = self.apply(second)
        self.assertFalse(ok)
        self.assertIn("已归进专项「一」", words)
        self.assertIn("剩下的不够另建一个专项，没拆", words)
        self.assertEqual(self.made(first)["threads"], self.t[:2])
        self.assertEqual(self.record(self.a)["threads"], [self.t[2]])  # Nothing moved twice, nothing left without a project.
        self.assertEqual(self.made(second), {})

    def test_a_split_of_a_project_merged_away_since_fails_and_says_so(self):
        merge = proposals.new_proposal(proposals.MERGE, topics=[*self.t], project=self.b, projects_=[self.b, self.a], name="")
        self.assertTrue(self.apply(merge)[0])
        self.assertEqual(self.record(self.a)["state"], projects.CANCELLED)
        self.assertEqual(self.record(self.b)["threads"], [*self.t[3:], *self.t[:3]])
        left = [row for row in project_lead.take_receipts(self.state, self.a, "k") if row["kind"] == members.LEFT]
        self.assertTrue(all("随合并转进专项「B 专项」" in row["text"] and "主人" not in row["text"] for row in left), left)
        self.assertEqual(self.apply(self.split(self.a, self.t[:2])), (False, "这个专项已经结束了"))
        self.assertEqual(self.record(self.b)["threads"], [*self.t[3:], *self.t[:3]])  # Nothing moved twice.

    def test_the_lead_a_split_took_topics_from_hears_where_they_went_not_that_the_owner_took_them_out(self):
        self.apply(self.split(self.a, self.t[:2]))
        left = [row for row in project_lead.take_receipts(self.state, self.a, "k") if row["kind"] == members.LEFT]
        self.assertEqual(len(left), 2)
        for row in left:
            self.assertIn("拆去了新专项「拆出的专项」，不再归这个专项", row["text"])
            self.assertNotIn("以后它不再归进来", row["text"])

    def test_a_split_keeps_out_a_topic_the_owner_took_out_of_projects_since(self):
        extra = self.topic("话题 extra")
        members.join_topic(self.state, self.a, extra)
        proposal = self.split(self.a, [*self.t[:2], extra])
        self.tidy.leave(self.ctx, CHAT, self.t[0], self.a)  # 移出: never_auto, kept out of projects for good.
        ok, words = self.apply(proposal)
        self.assertTrue(ok, words)
        self.assertEqual(self.made(proposal)["threads"], [self.t[1], extra])
        self.assertIsNone(projects.topic_project(self.state, self.t[0]))
        self.assertIn("已不在专项「A 专项」里，没带上", words)

    def test_a_split_that_takes_the_last_topics_folds_the_project_it_took_them_from(self):
        members.leave_topic(self.state, self.a, self.t[2], went="测试挪走")
        ok, words = self.apply(self.split(self.a, self.t[:2]))
        self.assertTrue(ok, words)
        self.assertIn("「A 专项」的话题都拆走了，已收起", words)
        self.assertTrue(projects.folded(self.record(self.a)))

    def test_moving_out_the_last_topic_folds_the_project(self):
        for topic in self.t[3:]:
            words = self.tidy.leave(self.ctx, CHAT, topic, self.b)
        self.assertIn("没有话题了，已收起", words)
        self.assertTrue(projects.folded(self.record(self.b)))

    def test_an_open_split_follows_its_topics_leaving_the_project(self):
        extra = self.topic("话题 extra")
        members.join_topic(self.state, self.a, extra)
        kept = self.split(self.a, [*self.t[:2], extra], "留两个")
        gone = self.split(self.a, self.t[1:3], "剩一个")
        rename = proposals.put_proposal(self.state, proposals.new_proposal(proposals.RENAME, topics=self.t[3:], project=self.b,
                                                                           name="新名字"))[0]
        run = {"message_id": "om_run", "view": {"run": "run-9", "state": "done", "rows": [
            {"proposal": record["id"], "line": "x", "md": True, "state": "open"} for record in (kept, gone, rename)]}}
        for record in (kept, gone, rename):
            proposals.update_proposal(self.state, record["id"], lambda data: data.update(run="run-9"))
        write_json(self.tidy.run_path(self.state, "run-9"), run, ensure_ascii=False)
        members.leave_topic(self.state, self.a, self.t[1], went="测试挪走")
        members.join_topic(self.state, self.b, self.t[1])
        projects.set_state(self.state, self.b, projects.CANCELLED, "downgraded")
        self.tidy.follow(self.ctx, CHAT)
        now = proposals.read_proposal(self.state, kept["id"])
        self.assertEqual((now["state"], now["topics"]), (proposals.OPEN, [self.t[0], extra]))
        self.assertEqual(proposals.read_proposal(self.state, gone["id"])["state"], proposals.EXPIRED)
        self.assertEqual(proposals.read_proposal(self.state, rename["id"])["state"], proposals.EXPIRED)
        rows = {row["proposal"]: row for row in read_object(self.tidy.run_path(self.state, "run-9"))["view"]["rows"]}
        self.assertEqual(rows[gone["id"]]["state"], proposals.EXPIRED)
        self.assertIn("剩下的不够另建一个专项", rows[gone["id"]]["note"])
        self.assertIn("只拆剩下的 2 个", rows[kept["id"]]["note"])
        self.assertEqual(len(rows[kept["id"]]["item"]["topics"]), 2)  # The block lists the 2 left (T572).
        self.assertIn("已经结束或并走了", rows[rename["id"]]["note"])
        self.assertEqual(self.app.messenger.patch_card.call_args.args[1], "om_run")  # The 整理专项 card, redrawn.
        self.assertEqual(self.tidy.follow(self.ctx, CHAT), 0)  # Once.


class TypedNumberTest(TidyCase):
    def test_a_topic_of_another_project_typed_on_the_proposal_card_is_named_as_left_out(self):
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
        with mock.patch.object(topic_links, "topic_numbers", return_value={number: other}):
            self.click({"kind": cards.NAMED_KIND, "proposal": record["id"]}, record["card"]["message_id"],
                       topics=["0", "1"], more=f"T{number}", name="网关稳定")
        made = projects.read_record(self.state, upgrades.project_id("tidy", record["id"]))
        self.assertEqual(made["threads"], pair)  # Left in its project...
        view = proposals.read_proposal(self.state, record["id"])["view"]
        self.assertIn(f"T{number} 已在专项「别的专项」里，没加", view["note"])  # ...and the card says so,
        self.assertEqual(len(view["labels"]), 2)  # not listing it among the new project's topics.

    def test_only_topics_of_other_projects_build_nothing(self):
        self.filler()
        pair = [self.topic("网关失联"), self.topic("网关扩容")]
        self.tidy.recent_topics = lambda state, chat, now, **kw: list(pair)
        self.verdicts.append({"suggestions": [{"kind": "create", "topics": [1, 2], "name": "网关稳定", "goal": "不失联",
                                               "why": "", "confidence": 0.9}]})
        self.tidy.realtime(self.ctx, CHAT, pair[0], "new", time.time())
        record = proposals.proposals(self.state, None)[-1]
        self.project("抢先的专项", pair)
        self.assertEqual(self.click({"kind": cards.CREATE_KIND, "proposal": record["id"]}, record["card"]["message_id"]),
                         "tidy_no_topics")
        self.assertIn("一个话题只能在一个专项里", self.app.messenger.send_message.call_args.args[2])
        self.assertEqual(projects.read_record(self.state, upgrades.project_id("tidy", record["id"])), {})
