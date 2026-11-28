"""每轮带法 (app/memory_turn.py, app/prompts.py memory_rules_block / memory_tail_block): the rules carry the 规矩 alone and stay the
same turn after turn (the prompt cache), the tail carries the decisions in progress, the notes the turn's words find and the
titles of the related ones; a decision of the topic's own is carried until it is answered anew, the lead's ends with the lead's
delivered commit, another topic's of the project is never carried whole (only its title, when the search finds it: T908 4.3);
a rule the lead wrote is in the rules however long no turn used it, 整理记忆 never puts it away; a light user gets nothing; the
job record names what was carried; the web page's preview."""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support import UNSET_OFF
from support.records import write_job, write_topic

from covey.app import memory_tidy, memory_turn
from covey.app.prompts import RULES_HEAD, TAIL_HEAD, turn_tail
from covey.core import memory, memory_review, memory_search, memory_usage, memstore, projects
from covey.core.jobs import find_job_record, job_key

PID, CHAT = "a1b2c3d4e5f6", "oc_owner_dm"
T1, T2 = "a1" * 32, "b2" * 32
LEAD = projects.lead_topic_id(CHAT, PID)


class Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        patcher = mock.patch.dict(os.environ, {name: value for name, value in os.environ.items()
                                               if not name.startswith(("COVEY_", "LARK_BRIDGE_"))} | UNSET_OFF
                                  | {memory.ENV: "1", projects.ENV: "1", projects.MEMORY_ENV: "1"}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        memory_turn._plans.clear()
        projects.create(self.state, projects.new_record(chat_id=CHAT, name="网关", project_id=PID, now=time.time()))
        for number, topic in ((7, T1), (8, T2)):
            write_topic(self.state, topic_id=topic, topic_number=number, chat_id=CHAT, title=f"话题{number}",
                        project=projects.project_field(PID, projects.MEMBER))
            projects.add_member(self.state, PID, topic)

    def ctx(self, topic):
        return SimpleNamespace(state_dir=self.state, topic_id=topic, workspace_name="", device="", options={})

    def job(self, topic, text="帮我看看", job_id=5):
        return SimpleNamespace(topic_id=topic, chat_id=CHAT, message_id=f"om{topic[:6]}{job_id}", job_id=job_id, user_text=text,
                               sender_id="ou_owner")

    def rule(self, text, scope="global", owner_key="", said="owner", **kw):
        return memstore.add(self.state, kind="rule", scope=scope, owner_key=owner_key, text=text, said=said,
                            by="owner" if said == "owner" else "lead", **kw)

    def decide(self, question, answer, topic, at, **kw):
        return memstore.add_decision(self.state, scope="project", owner_key=PID, question=question, answer=answer,
                                     topic_key=topic, question_ref=f"{topic[:4]}{int(at)}-1", now=at, **kw)

    def tail(self, topic, text="帮我看看", owner=True):
        return memory_turn.tail(self.ctx(topic), self.job(topic, text), lambda: owner)

    def block(self, topic, text="帮我看看", owner=True, job_id=5):
        return memory_turn.block(self.ctx(topic), self.job(topic, text, job_id), owner_turn=lambda: owner)


class RulesTest(Case):
    def test_a_light_user_gets_nothing_at_all(self):
        self.assertEqual(self.block(T1), "")
        self.assertEqual(self.tail(T1), "")
        self.assertEqual(turn_tail(self.tail(T1), ""), "")

    def test_the_rules_are_the_same_turn_after_turn_whatever_the_words(self):
        self.rule("回复主人用中文")
        self.rule("部署前先跑预检", scope="project", owner_key=PID)
        self.decide("这批要不要现在合入？", "现在合入", T1, time.time() - 600)
        first = self.block(T1, "这批合入以后部署吗", job_id=5)
        memory_turn._plans.clear()
        second = self.block(T1, "看下额度条颜色", job_id=6)
        self.assertEqual(first, second)
        self.assertIn(RULES_HEAD, first)
        self.assertIn("回复主人用中文", first)
        self.assertNotIn("现在合入", first)  # A decision is the tail's, never the rules'.
        self.assertIn("现在合入", self.tail(T1))

    def test_global_rules_only_on_the_owners_turn_and_a_proxy_topic_reads_nothing(self):
        self.rule("回复主人用中文")
        self.rule("部署前先跑预检", scope="project", owner_key=PID)
        self.assertIn("回复主人用中文", self.block(T1))
        memory_turn._plans.clear()
        theirs = self.block(T1, owner=False)
        self.assertNotIn("回复主人用中文", theirs)
        self.assertIn("部署前先跑预检", theirs)
        with mock.patch.object(memory_turn, "proxy_topic", return_value=True):
            self.assertEqual(self.block(T2), "")
            self.assertEqual(self.tail(T2), "")

    def test_a_rule_past_its_scopes_room_is_listed_by_title_never_dropped(self):
        for i in range(12):
            self.rule(f"专项规矩第{i}条：发布前先在预发环境验证{i}", scope="project", owner_key=PID, title=f"规矩{i}", now=1000.0 + i)
        text = self.block(T1)
        carried = [line for line in text.splitlines() if line.startswith("- R")]
        self.assertEqual(len(carried), 10)
        self.assertIn("还有 2 条规矩放不下", text)
        self.assertIn("规矩0", text)  # The room goes to the newest: the two oldest are listed by title.
        self.assertIn("规矩1", text)
        self.assertNotIn("第0条", text)
        self.assertIn("第11条", text)

    def test_past_the_room_the_oldest_not_the_owners_are_the_ones_listed_by_title(self):
        made = [self.rule(f"负责人记的第{i}条：上线前在 1 台上验证{i}", scope="project", owner_key=PID, said="summary",
                          title=f"旧约束{i}", now=1000.0 + i) for i in range(10)]
        mine = self.rule("同事机器全程只读，不改任何东西", scope="project", owner_key=PID, title="同事机器只读", now=900.0)
        newest = self.rule("连夜改造不动 stable 通道", scope="project", owner_key=PID, said="summary", title="不动 stable",
                           now=5000.0)
        carried, left = memory_turn.rules_of(self.state, project=PID, owner=True)
        self.assertEqual([e.ref for e in left], [made[0].ref, made[1].ref])  # The two oldest the owner did not say.
        self.assertIn(mine.ref, [e.ref for e in carried])
        self.assertIn(newest.ref, [e.ref for e in carried])
        # Carried in the written order (the owner's first, then the oldest): the rules' prefix stays put.
        self.assertEqual([e.ref for e in carried], [mine.ref, *(e.ref for e in made[2:]), newest.ref])
        text = self.block(T1)
        self.assertIn("同事机器全程只读", text)
        self.assertIn("连夜改造不动 stable 通道", text)
        self.assertIn("旧约束0", text)
        self.assertNotIn("上线前在 1 台上验证0", text)

    def test_the_leads_rule_says_so_and_a_pending_one_is_not_carried(self):
        self.rule("接口叫 bill_v2，别改名", scope="project", owner_key=PID, said="summary")
        self.rule("待定的说法", scope="project", owner_key=PID, status="pending")
        text = self.block(T1)
        self.assertIn("接口叫 bill_v2，别改名（负责人记的）", text)
        self.assertNotIn("待定的说法", text)

    def test_what_waits_in_review_is_never_carried(self):
        memory_review.add(self.state, memory_review.ReviewItem(key="", type="learn", title="回答先给结论", text="回答先给结论",
                                                               suggest={"kind": "rule", "scope": "global"},
                                                               created_at=time.time()))
        self.assertEqual(self.block(T1), "")
        self.assertNotIn("回答先给结论", self.tail(T1, "回答先给结论吗"))


class DecisionTest(Case):
    def test_a_topics_own_decision_stays_after_its_commit(self):
        self.decide("网关镜像要不要先发预发？", "先发预发", T1, time.time() - 3600)
        self.assertIn("先发预发", self.tail(T1))
        write_job(self.state, job_key("omt1", 9), job_id=9, topic_id=T1, chat_id=CHAT, status="done",
                  delivery={"commit": "0123abcd", "deployed": "", "verified": ""})
        memory_turn._plans.clear()
        self.assertIn("先发预发", self.tail(T1))  # The topic's own: until it ends or the question is answered anew.

    def test_the_leads_decision_is_carried_to_its_topics_until_the_lead_delivers(self):
        self.decide("网关镜像要不要先发预发？", "先发预发", LEAD, time.time() - 3600)
        self.assertIn("→先发预发", self.tail(T1))
        self.assertIn("→先发预发", self.tail(T2))
        write_job(self.state, job_key("omlead", 9), job_id=9, topic_id=LEAD, chat_id=CHAT, status="done",
                  delivery={"commit": "0123abcd", "deployed": "", "verified": ""})
        memory_turn._plans.clear()
        self.assertNotIn("→先发预发", self.tail(T2))

    def test_the_leads_decision_older_than_the_window_is_not_carried(self):
        self.decide("旧批次要不要重跑？", "不重跑", LEAD, time.time() - 3 * 86400)
        self.assertNotIn("不重跑", self.tail(T2))
        self.decide("这一批要不要重跑？", "要重跑", T1, time.time() - 3 * 86400)
        self.assertIn("要重跑", self.tail(T1))  # The topic's own: whatever its age.

    def test_another_topics_decision_is_never_carried_whole_only_its_title_when_the_search_finds_it(self):
        """T908 4.3: what a sibling topic asked the owner is carried in that topic alone; another topic of the project sees
        its title among 相关但没带的 when its words find it (``covey memory show`` has the whole), and nothing otherwise."""
        made = self.decide("网关镜像要不要先发预发？", "先发预发", T1, time.time() - 60)
        found = memory_turn.make_plan(self.state, topic=T2, project=PID, owner=True, words="网关镜像要不要先发预发")
        self.assertNotIn(made.ref, [entry.ref for entry in found.decisions])
        self.assertIn(made.ref, [item["ref"] for item in found.listed])
        text = self.tail(T2, "网关镜像要不要先发预发")
        self.assertIn(made.code, text)
        self.assertNotIn("→先发预发", text)
        quiet = memory_turn.make_plan(self.state, topic=T2, project=PID, owner=True, words="看下额度条颜色")
        self.assertNotIn(made.ref, [entry.ref for entry in quiet.decisions])
        self.assertNotIn(made.ref, [item["ref"] for item in quiet.listed])
        self.assertIn("→先发预发", self.tail(T1))  # Its own topic carries it whole.
        self.assertTrue(memory_turn.sibling(made, T2, LEAD))
        self.assertFalse(memory_turn.sibling(made, T1, LEAD))

    def test_the_same_question_answered_anew_carries_the_newer_answer(self):
        self.decide("额度条用什么颜色？", "黄色", T1, time.time() - 7200)
        self.decide("额度条用什么颜色？", "红色", T1, time.time() - 60)
        text = self.tail(T1)
        self.assertIn("红色", text)
        self.assertNotIn("黄色", text)

    def test_a_decision_says_where_it_was_asked(self):
        self.decide("网关镜像要不要先发预发？", "先发预发", T1, time.time() - 60)
        self.assertRegex(self.tail(T1), r"- D[0-9A-Z]{4,6} 网关镜像要不要先发预发？→先发预发（T7 · ")


class LeadRuleTest(Case):
    def test_a_leads_rule_unused_for_31_days_is_still_in_the_rules_block(self):
        """T908 4.2: 整理记忆 never puts a 规矩 away for not being used, the lead's (负责人记的, a limit on what to do) neither:
        31 days with no ``memory_used``, and the day's tidy run, it is still in the rules a topic of the project reads."""
        old = time.time() - 31 * 86400
        made = memstore.add(self.state, kind="rule", scope="project", owner_key=PID, text="改共享配置前先在预发验证一遍",
                            said="summary", by="lead", now=old)
        note = memstore.add(self.state, kind="note", scope="project", owner_key=PID, text="预发集群的地址在项目简报里",
                            said="summary", by="lead", now=old)
        usage = memory_usage.idle_view(self.state)
        self.assertTrue(memory_usage.idle(usage, made.ref, made.at))  # No turn used it.
        tidy = memory_tidy.MemoryTidy(SimpleNamespace(log=lambda line: None))
        run = tidy.run(SimpleNamespace(state_dir=self.state), CHAT, memory_tidy.day(time.time()), now=time.time())
        self.assertEqual(memstore.find(self.state, made.ref).status, "active")
        self.assertEqual(memstore.find(self.state, note.ref).status, "archived")  # Its 资料 is put away.
        self.assertEqual([row[0] for row in run["projects"][0]["settled"]], [memstore.parse_ref(note.ref)[2]])
        memory_turn._plans.clear()
        block = self.block(T1)
        self.assertIn("改共享配置前先在预发验证一遍", block)
        self.assertIn("负责人记的", block)


class NotesTest(Case):
    def test_the_turns_words_find_a_note_and_the_tail_says_so(self):
        memstore.add(self.state, kind="note", scope="project", owner_key=PID, text="网页改动由话题自己用无头浏览器截深浅两色验收",
                     said="summary", by="lead")
        memstore.add(self.state, kind="note", scope="project", owner_key=PID, text="镜像仓库在内网，拉取要走代理")
        text = self.tail(T1, "这次网页改动怎么验收？")
        self.assertIn(TAIL_HEAD, text)
        self.assertIn("无头浏览器截深浅两色验收（负责人记的）", text)
        self.assertNotIn("镜像仓库在内网", text)

    def test_a_global_note_is_not_read_on_a_turn_not_the_owners(self):
        memstore.add(self.state, kind="note", text="网页改动由话题自己用无头浏览器截深浅两色验收", scope="global")
        self.assertIn("无头浏览器", self.tail(T1, "这次网页改动怎么验收？"))
        memory_turn._plans.clear()
        self.assertEqual(self.tail(T1, "这次网页改动怎么验收？", owner=False), "")


class OthersTurnTest(Case):
    """T870 Wave 3.5 (Fable P0-2): a turn not the owner's reads its own topic's and its project's memory only, as it did before
    memory_v2: no 全局, no other project's notes or decisions (not even their titles), no other topic's."""

    def setUp(self):
        super().setUp()
        self.other = "f6e5d4c3b2a1"
        projects.create(self.state, projects.new_record(chat_id=CHAT, name="推理服务", project_id=self.other, now=time.time()))
        memstore.add(self.state, kind="note", scope="project", owner_key=self.other, text="推理服务的网页改动由值班同学用无头浏览器验收",
                     title="推理服务网页验收", said="summary", by="lead")
        memstore.add_decision(self.state, scope="project", owner_key=self.other, question="推理服务的网页改动怎么验收？",
                              answer="无头浏览器截深浅两色", topic_key="c3" * 32, question_ref="c3c3-1", now=time.time() - 60)
        memstore.add(self.state, kind="note", scope="topic", owner_key=T2, text="话题8的网页改动用无头浏览器截图验收", title="话题8验收")
        memstore.add(self.state, kind="note", scope="project", owner_key=PID, text="网关的网页改动用无头浏览器截深浅两色验收",
                     title="网关网页验收")

    def test_another_projects_notes_and_decisions_are_not_read_nor_listed(self):
        notes, listed = memory_turn.notes_of(self.state, ["网关这次的网页改动怎么用无头浏览器截深浅两色验收？"], topic=T1, project=PID, owner=False)
        self.assertIn("网关的网页改动用无头浏览器截深浅两色验收", [entry.text for entry, _ in notes])
        seen = [entry.text for entry, _ in notes] + [item["title"] + item["text"] for item in listed]
        self.assertFalse([t for t in seen if "推理服务" in t or "话题8" in t], seen)
        mine, mine_listed = memory_turn.notes_of(self.state, ["网关这次的网页改动怎么用无头浏览器截深浅两色验收？"], topic=T1, project=PID, owner=True)
        self.assertTrue([t for t in [e.text for e, _ in mine] + [i["title"] + i["text"] for i in mine_listed] if "推理服务" in t])
        hits = memory_search.eval_search(self.state, "网关的网页改动怎么用无头浏览器验收", {"topic": T1, "project": PID, "owner": False},
                                         limit=10)
        self.assertTrue(hits)
        self.assertTrue(all((h["scope"], h["owner_key"]) in (("project", PID), ("topic", T1)) for h in hits), hits)

    def test_the_owners_own_turn_still_reads_past_the_project(self):
        mine = memory_search.eval_search(self.state, "网关的网页改动怎么用无头浏览器验收", {"topic": T1, "project": PID, "owner": True},
                                         limit=10)
        self.assertIn(self.other, {h["owner_key"] for h in mine})


class RecordTest(Case):
    def test_the_record_names_what_was_carried_whole_and_what_only_by_title(self):
        made = self.rule("回复主人用中文")
        early = self.rule("专项规矩首条：合入后通知负责人", scope="project", owner_key=PID, now=900.0)
        for i in range(10):
            self.rule(f"专项规矩{i}：发布前先验证第{i}项", scope="project", owner_key=PID, now=1000.0 + i)
        job = self.job(T1)
        write_job(self.state, job_key(job.message_id, job.job_id), job_id=5, topic_id=T1, chat_id=CHAT, status="running",
                  message_id=job.message_id)
        memory_turn.record(self.ctx(T1), job, lambda: True)
        record = find_job_record(self.state, job.message_id)
        ids = {row["ref"]: row for row in record["memory_ids"]}
        self.assertEqual(ids[made.ref]["n"], made.code)
        self.assertTrue(memstore.CODE_RE.match(ids[made.ref]["n"]))
        self.assertEqual(ids[made.ref]["kind"], "rule")
        self.assertIn(early.ref, [row["ref"] for row in record["memory_listed"]])  # Past the room: the oldest by title.


class PreviewTest(Case):
    def test_the_preview_has_the_contracts_shape(self):
        made = self.rule("部署前先跑预检", scope="project", owner_key=PID)
        self.decide("网关镜像要不要先发预发？", "先发预发", T1, time.time() - 60)
        found = memory_turn.preview(self.state, T1, query="部署怎么验收")
        self.assertEqual(set(found), {"topic_key", "title", "chars", "rules", "decisions", "notes", "listed"})
        self.assertEqual(found["topic_key"], T1)
        self.assertEqual(found["title"], "话题7")
        self.assertIn(made.code, found["rules"])
        self.assertEqual(len(found["decisions"]), 1)
        self.assertGreater(found["chars"], 0)
        self.assertTrue(all(set(note) == {"code", "sim"} for note in found["notes"]))


if __name__ == "__main__":
    unittest.main()
