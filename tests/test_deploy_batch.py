"""部署攒批 (app/project_lead.py TOOL_RULES, waiting_deploys; app/butler_handoff.py lead_note): a project's topics merge and do not
ask the owner to deploy each time; the lead's pack lists what was merged and not deployed since, and the lead asks once for the
batch, then hands the deploy to one topic."""
from __future__ import annotations

import json
import unittest
from unittest import mock

from support.records import write_job

from covey.app import butler_handoff, project_lead
from covey.app.butler_handoff import Asked
from covey.core.jobs import job_path

from test_project_lead import CHAT, Case

A, B = "aa" * 16, "bb" * 16


class WaitingTest(Case):
    def merged(self, topic, key, job_id, commit, deployed="", verified="test_map run 通过", created="2026-11-25T10:00:00Z",
               updated="2026-11-25T10:30:00Z", status="done"):
        write_job(self.state, key, topic_id=topic, chat_id=CHAT, job_id=job_id, status=status,
                  delivery={"commit": commit, "deployed": deployed, "verified": verified})
        path = job_path(self.state, key)  # The times as a real turn leaves them (upsert_job_record writes its own).
        path.write_text(json.dumps({**json.loads(path.read_text()), "created_at": created, "updated_at": updated}))

    def test_merged_and_not_deployed_turns_are_listed_once_in_the_pack(self):
        self.thread(A, "账单表迁移", 11)
        self.thread(B, "对账脚本", 12)
        self.merged(A, "a-1", 1, "1111111aaaa", updated="2026-11-25T10:30:00Z")
        self.merged(B, "b-1", 2, ["2222222bbbb", "3333333cccc"], deployed="未部署（等负责人攒批）", verified="",
                    updated="2026-11-25T11:00:00Z")
        rows = project_lead.waiting_deploys(self.state, self.reload())
        self.assertEqual([(row["label"], row["commits"], row["checked"]) for row in rows],
                         [("T11", ["1111111aaaa"], True), ("T12", ["2222222bbbb", "3333333cccc"], False)])
        pack = project_lead.state_pack(self.state, self.reload(), "lead-1", full=True)
        block = pack[pack.index("合进待部署"):]
        self.assertIn("合进待部署（2 轮", block)
        self.assertIn("T11 · 1111111 · 已自检", block)
        self.assertIn("T12 · 2222222 3333333 · 未自检", block)

    def test_a_deploy_covers_what_ended_before_it_started_whichever_topic(self):
        self.thread(A, "账单表迁移", 11)
        self.thread(B, "对账脚本", 12)
        self.merged(A, "a-1", 1, "1111111aaaa", updated="2026-11-25T10:30:00Z")
        self.merged(B, "b-1", 2, "2222222bbbb", deployed="现网第 471 代", created="2026-11-25T11:00:00Z",
                    updated="2026-11-25T11:20:00Z")
        self.merged(A, "a-2", 3, "4444444dddd", created="2026-11-25T11:30:00Z", updated="2026-11-25T12:00:00Z")
        rows = project_lead.waiting_deploys(self.state, self.reload())
        self.assertEqual([row["commits"] for row in rows], [["4444444dddd"]])

    def test_nothing_waiting_adds_nothing_and_running_or_uncommitted_turns_do_not_count(self):
        self.thread(A, "账单表迁移", 11)
        self.merged(A, "a-1", 1, "1111111aaaa", status="running")
        write_job(self.state, "a-2", topic_id=A, chat_id=CHAT, job_id=2, status="done", delivery={"verified": "只读调研"})
        self.assertEqual(project_lead.waiting_deploys(self.state, self.reload()), [])
        self.assertNotIn("合进待部署", project_lead.state_pack(self.state, self.reload(), "lead-1", full=True))

    def test_deployed_words(self):
        for words, want in (("", False), ("已部署第 470 代", True), ("现网第 471 代", True), ("未部署（等负责人攒批）", False),
                            ("合入待部署", False), ("等负责人攒批统一部署", False), ("部署到测试机，现网未部署", False),
                            ("none", False), ("not deployed", False)):
            self.assertEqual(project_lead.deployed({"deployed": words}), want, words)


class RulesTest(unittest.TestCase):
    def test_the_lead_batches_and_topics_do_not_ask_one_by_one(self):
        rules = project_lead.lead_rules()
        self.assertIn("部署攒批", rules)
        # WAVE12 C4: the batch is asked on a question card, the yes is an approval the Bridge writes at the handoff's head.
        self.assertIn("用 project_ask 一次问主人要不要部署这一批：kind 写 deploy", rules)
        self.assertIn("approval 填凭证", rules)
        self.assertIn("你自己在任务说明里写“主人已同意”不算", rules)
        self.assertIn("不要每合一个就问一次，也不要没问主人就派部署", rules)
        with mock.patch("covey.core.extensions.setting", return_value=""):  # The core's words alone, no extension's deploy_notes.
            note = butler_handoff.lead_note(butler_handoff.START, " T12 ", "迁移账单", "话题")
        self.assertIn("不自己部署、也不单独问主人", note)
        self.assertIn("deployed 留空", note)
        self.assertIn("只认交接说明开头 [Bridge 说明] 段里 Bridge 写的那一句", note)
        self.assertNotIn("部署除外", note)
        self.assertIsNotNone(Asked)
        # The owner's own word to deploy what merges comes first, in both (a summary by someone else does not count).
        self.assertIn("部署先看记忆：主人自己说过改完就部署的", rules)
        self.assertIn("你不攒批问、也不派话题去部署", rules)
        self.assertIn("主人亲口说过改完就部署的（记忆里他自己的规矩", note)
        self.assertNotIn("stable", note)  # A deployment's own ways are the extension's deploy_notes (core/extensions.py).


if __name__ == "__main__":
    unittest.main()
