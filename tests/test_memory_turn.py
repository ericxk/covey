"""每轮带法 (app/memory_turn.py, app/prompts.py memory_rules_block / memory_tail_block): the rules carry the 规矩 alone and stay the
same turn after turn (the prompt cache), the tail carries the decisions in progress, the notes the turn's words find and the
titles of the related ones; a decision of the topic's own is carried until it is answered anew, the lead's ends with the lead's
delivered commit, another topic's of the project is never carried whole (only its title, when the search finds it: T908 4.3);
a rule the lead wrote is in the rules however long no turn used it, 整理记忆 never puts it away; a light user gets nothing; the
job record names what was carried; the web page's preview."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support import UNSET_OFF
from support.records import write_job, write_topic

from covey.app import memory_picks, memory_tidy, memory_turn
from covey.app.prompts import PICKED_HEAD, POOL_HEAD, POOL_TITLES, RULES_HEAD, TAIL_HEAD, turn_tail
from covey.core import butler, memory, memory_review, memory_search, memory_usage, memstore, projects
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
        # T908 M5: the owner's own turn neither carries nor lists another project's or another topic's either (its search
        # command still reads past them: ``test_the_owners_own_turn_still_reads_past_the_project``).
        mine, mine_listed = memory_turn.notes_of(self.state, ["网关这次的网页改动怎么用无头浏览器截深浅两色验收？"], topic=T1, project=PID, owner=True)
        seen = [e.text for e, _ in mine] + [i["title"] + i["text"] for i in mine_listed]
        self.assertIn("网关的网页改动用无头浏览器截深浅两色验收", seen)
        self.assertFalse([t for t in seen if "推理服务" in t or "话题8" in t], seen)
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


class ReadingM5Test(Case):
    """T908 M5: the lead's topic reads 全局 alone (its project's in its pack), the line of titles is cut of the lead's only,
    the block says the narrower scope wins and keeps "先查一下他答过没有", and the owner's bottom lines are never lost."""

    def setUp(self):
        super().setUp()
        write_topic(self.state, topic_id=LEAD, topic_number=3, chat_id=CHAT, title="负责人",
                    project=projects.project_field(PID, projects.LEAD))

    def test_the_leads_topic_reads_global_alone_and_nothing_of_its_project_at_the_tail(self):
        self.rule("回复主人用中文")
        self.rule("部署前先跑预检", scope="project", owner_key=PID)
        memstore.add(self.state, kind="note", scope="project", owner_key=PID, text="网关的预检脚本在 scripts/check.sh 里")
        self.decide("网关这批要不要先发预发？", "先发预发", LEAD, time.time() - 60)
        self.assertEqual(memory_turn.lead_project(self.state, LEAD), PID)
        text = self.block(LEAD, "网关预检脚本在哪，要不要先发预发")
        self.assertIn("回复主人用中文", text)
        self.assertNotIn("部署前先跑预检", text)  # The pack's (app/project_lead.py store_memory), never twice.
        tail = self.tail(LEAD, "网关预检脚本在哪，要不要先发预发")
        for words in ("先发预发", "scripts/check.sh", "部署前先跑预检"):
            self.assertNotIn(words, tail)
        memory_turn._plans.clear()
        member = self.block(T1)  # A member topic reads its project's as before.
        self.assertIn("部署前先跑预检", member)

    def test_what_was_moved_to_another_scope_is_not_listed_by_its_old_copy(self):
        made = self.rule("线上流量增加可直接扩容推理服务", title="线上流量增加可直接扩容")
        memstore.move(self.state, made.ref, scope="project", owner_key=PID)  # 全局 -> this project: the old copy put away.
        memory_turn._plans.clear()
        text = self.block(LEAD, "线上流量增加了要不要扩容") + self.tail(LEAD, "线上流量增加了要不要扩容")
        self.assertNotIn(made.code, text)  # The lead's pack has it; its old 全局 copy is not listed at the tail either.

    def test_the_head_says_the_narrower_scope_wins_and_the_guide_still_says_to_look_first(self):
        from covey.app import prompts
        from covey.core.checkpoint import ASK_SHORT

        self.rule("回复主人用中文")
        text = self.block(T1)
        self.assertIn("听范围小的", text.splitlines()[0])
        self.assertIn(ASK_SHORT, prompts.RULES_GUIDE)
        self.assertIn("要问主人之前，先查一下他答过没有", text)
        self.assertLess(len(prompts.RULES_GUIDE), 160)  # Shorter than the 191 characters it had.

    def test_the_line_of_titles_is_cut_of_the_leads_alone(self):
        owners = [self.rule(f"主人定的第{i}条：上线前先在预发跑一遍完整的回归测试{i}" * 3, scope="project", owner_key=PID,
                            title=f"主人的标题第{i}条很长很长很长很长很长", now=1000.0 + i) for i in range(14)]
        leads = [self.rule(f"负责人记的第{i}条：合入前跑一遍静态检查{i}", scope="project", owner_key=PID, said="summary",
                           title=f"负责人的标题第{i}条也挺长挺长挺长挺长", now=500.0 + i) for i in range(30)]
        text = self.block(T1)
        for entry in owners:
            self.assertTrue(entry.text in text or f"{entry.code} {entry.title}" in text, entry.title)
        named = [entry for entry in leads if entry.code in text]
        self.assertLess(len(named), len(leads))
        self.assertIn(f"另有 {len(leads) - len(named)} 条负责人记的", text)
        made = memory_turn.plan(self.ctx(T1), self.job(T1), lambda: True)
        self.assertEqual(made.cut, len(leads) - len(named))
        listed = [row["ref"] for row in memory_turn.ids_of(made)[1]]
        self.assertTrue(all(entry.ref not in listed for entry in leads if entry.code not in text))  # Only the shown are listed.
        self.assertTrue(all(entry.ref in listed for entry in named))

    def test_the_owners_bottom_lines_are_in_every_block_of_the_project_however_full_it_is(self):
        """Every rule the owner said of production (deploy, stable, delete, a colleague's machine): its words or its title,
        in each member topic's block and in the lead's pack, with the project past its room and 31 days of no use."""
        from covey.app import project_lead

        bottom = [self.rule(text, scope="project", owner_key=PID, title=title, now=100.0 + n) for n, (title, text) in enumerate((
            ("不推 stable 通道", "任何时候都不推 stable 通道，stable 由主人自己发"),
            ("同事机器只读", "同事的机器全程只读，不改任何东西，主人授权的批次除外"),
            ("删除先问主人", "删除数据、强推、改线上数据之前一律先问主人"),
            ("部署只到本机现网", "改完检查过就部署到主人本机现网，别人的机器和回滚照常问"),
        ))]
        for i in range(12):
            self.rule(f"主人后来定的第{i}条：合入前先同步主干再快进合并，测试全过才合" + "并且写清楚" * 4, scope="project",
                      owner_key=PID, title=f"后来定的第{i}条", now=5000.0 + i)
        for i in range(25):
            self.rule(f"负责人记的第{i}条：派活前先看已有话题，同一件事追加给原话题" * 2, scope="project", owner_key=PID, said="summary",
                      title=f"负责人第{i}条", now=9000.0 + i)
        later = time.time() + 31 * 86400
        with mock.patch.object(time, "time", return_value=later):
            memory_tidy.MemoryTidy(SimpleNamespace(log=lambda line: None)).run(
                SimpleNamespace(state_dir=self.state), CHAT, memory_tidy.day(later), now=later)
            for topic in (T1, T2):
                memory_turn._plans.clear()
                text = self.block(topic, owner=False)
                for entry in bottom:
                    self.assertTrue(entry.text in text or f"{entry.code} {entry.title}" in text, (topic, entry.title))
            pack = project_lead.state_pack(self.state, projects.read_record(self.state, PID), "lead-1", full=True, receipts=[])
            for entry in bottom:
                self.assertTrue(entry.text in pack or f"{entry.code} {entry.title}" in pack, ("lead", entry.title))


if __name__ == "__main__":
    unittest.main()


class PoolTest(Case):
    """P2 (T923 DESIGN §1, §2, §10.4–10.5): the butler's and a lead's rules carry the 全局 资料 whole after the 全局 rules, with
    a room of their own (10 and 1,000 characters, past it titles); an ordinary topic and a project's topic carry none of it in
    their rules; a group member's turn neither half; the pool never takes the 全局 rules' room."""

    def setUp(self):
        super().setUp()
        write_topic(self.state, topic_id=LEAD, topic_number=3, chat_id=CHAT, title="负责人",
                    project=projects.project_field(PID, projects.LEAD))
        self.butler = butler.butler_topic_id(CHAT)
        self.plain = "c3" * 32
        write_topic(self.state, topic_id=self.plain, topic_number=9, chat_id=CHAT, title="普通话题")

    def note(self, text, **kw):
        return memstore.add(self.state, kind="note", scope="global", text=text, said=kw.pop("said", "owner"), by="owner", **kw)

    def fresh(self, topic, owner=True, text="帮我看看"):
        memory_turn._plans.clear()
        return self.block(topic, text, owner=owner)

    def test_the_butler_and_the_lead_carry_the_pool_whole_and_no_topic_does(self):
        self.rule("回复主人用中文")
        deploy = self.note("Covey 的专项话题改完、检查过就直接部署到主人本机现网")
        for topic in (self.butler, LEAD):
            with self.subTest(topic=topic[:6]):
                text = self.fresh(topic)
                self.assertIn(POOL_HEAD, text)
                self.assertIn(f"- {deploy.code} Covey 的专项话题改完", text)
                self.assertLess(text.index("回复主人用中文"), text.index(POOL_HEAD))
                self.assertLess(text.index(POOL_HEAD), text.index("怎么用："))
                ids, _ = memory_turn.ids_of(memory_turn.plan(self.ctx(topic), self.job(topic), lambda: True))
                self.assertIn(deploy.code, [row["n"] for row in ids])
                # Carried in the rules, never again at the tail.
                self.assertNotIn("Covey 的专项话题改完", self.tail(topic, "改完部署到主人本机现网"))
        for topic in (T1, self.plain):
            with self.subTest(topic=topic[:6]):
                text = self.fresh(topic)
                self.assertIn("回复主人用中文", text)
                self.assertNotIn(POOL_HEAD, text)
                self.assertNotIn("Covey 的专项话题改完", text)
        # A group member's turn (not the owner's): neither the 全局 rules nor the pool, the butler's as anyone's.
        theirs = self.fresh(self.butler, owner=False)
        self.assertNotIn("回复主人用中文", theirs)
        self.assertNotIn(POOL_HEAD, theirs)

    def test_the_pool_is_the_same_turn_after_turn_and_only_a_note_changes_it(self):
        self.note("同事的机器只读，不改任何东西")
        first = self.fresh(self.butler, text="部署一下")
        self.assertEqual(first, self.fresh(self.butler, text="去同事机器看日志"))
        self.note("以主人身份发消息走代答，先给主人看草稿")
        self.assertNotEqual(first, self.fresh(self.butler))

    def test_past_its_room_the_pool_lists_titles_and_takes_nothing_of_the_rules_room(self):
        for i in range(15):
            self.rule(f"全局规矩 {i:02d}：" + "规" * 70)  # 15 × 78 = 1,170 characters: the 全局 room is full, not past it.
        made = [self.note(f"池子资料 {i:02d}：" + "资" * 90, title=f"池子 {i:02d}") for i in range(12)]
        text = self.fresh(self.butler)
        self.assertEqual(sum(f"全局规矩 {i:02d}：" in text for i in range(15)), 15)
        self.assertNotIn("条规矩放不下", text)
        whole = [entry for entry in made if entry.text in text]
        self.assertEqual(len(whole), 10)  # 10 × 99 = 990 ≤ 1,000.
        self.assertIn(POOL_TITLES.format(count=2), text)
        left = [entry for entry in made if entry not in whole]
        self.assertTrue(all(f"{entry.code} {entry.title}" in text for entry in left))
        _, listed = memory_turn.ids_of(memory_turn.plan(self.ctx(self.butler), self.job(self.butler), lambda: True))
        self.assertTrue({entry.code for entry in left} <= {row["n"] for row in listed})

    def test_a_light_users_13_rules_of_1176_characters_and_4_pool_notes_are_all_there(self):
        """信宇's memory today (T923 DESIGN §10.5): 13 全局 rules, 1,176 characters, and 4 notes in the pool. An ordinary topic
        carries the 13 whole with no line of titles; the butler the 13 and the 4 whole: the pool takes none of the room."""
        sizes = [90] * 12 + [96]
        rules = [self.rule(f"第{i:02d}条" + "规" * (size - 4)) for i, size in enumerate(sizes)]
        self.assertEqual(sum(len(entry.text) for entry in rules), 1176)
        notes = [self.note(f"池子第{i}条：" + "资" * 60) for i in range(4)]
        for topic, pool in ((self.plain, False), (T1, False), (self.butler, True)):
            with self.subTest(topic=topic[:6]):
                text = self.fresh(topic)
                self.assertTrue(all(entry.text in text for entry in rules))
                self.assertNotIn("放不下", text)
                self.assertEqual(all(entry.text in text for entry in notes), pool)
                self.assertEqual(any(entry.text in text for entry in notes), pool)


class PickedTest(Case):
    """P2 (T923 DESIGN §3, §10.3–10.4): what the butler or the lead picked for a topic (its topic.json ``memory_carried``) is
    carried whole at the tail of each of its turns, under the Bridge's own head, before the decisions; recorded ``by``
    picked; a pick made mid-way changes the tail, never the rules (``rules_sent.version`` stays)."""

    def note(self, text, scope="global", owner_key="", said="owner"):
        return memstore.add(self.state, kind="note", scope=scope, owner_key=owner_key, text=text, said=said,
                            by="owner" if said == "owner" else "lead")

    def pick(self, topic, entries, by="lead"):
        path = self.state / "topics" / topic / "topic.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        memory_picks.remember(data, entries, by)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        memory_turn._plans.clear()

    def test_the_picked_are_carried_whole_before_the_decisions_and_recorded_as_picked(self):
        self.rule("回复主人用中文")
        deploy = self.note("Covey 的专项话题改完、检查过就直接部署到主人本机现网")
        summary = self.note("网关的预检脚本在 scripts/check.sh", scope="project", owner_key=PID, said="summary")
        self.decide("这批要不要现在合入？", "现在合入", T1, time.time() - 600)
        before = self.block(T1, job_id=5)
        self.pick(T1, [deploy, summary])
        tail = self.tail(T1, "看下额度条颜色")
        head = PICKED_HEAD.format(who="负责人")
        self.assertIn(head, tail)
        self.assertIn(f"- {deploy.code} Covey 的专项话题改完、检查过就直接部署到主人本机现网\n", tail + "\n")
        self.assertIn(f"- {summary.code} 网关的预检脚本在 scripts/check.sh（负责人记的）", tail)
        self.assertLess(tail.index(head), tail.index("决定（本话题的、本专项在办的）"))
        # The rules are what they were: a pick mid-way changes the tail alone (the cached prefix, rules_sent.version).
        self.assertEqual(before, self.block(T1, job_id=6))
        ids, _ = memory_turn.ids_of(memory_turn.plan(self.ctx(T1), self.job(T1, job_id=7), lambda: True))
        picked = {row["n"]: row["by"] for row in ids if row["by"] == "picked"}
        self.assertEqual(picked, {deploy.code: "picked", summary.code: "picked"})
        # Carried once: the search that finds the same note does not list it again.
        self.assertEqual(self.tail(T1, "部署到主人本机现网").count(deploy.text), 1)

    def test_the_butlers_pick_says_so_and_one_put_away_since_is_no_longer_carried(self):
        a = self.note("同事的机器只读，不改任何东西")
        b = self.note("以主人身份发消息走代答，先给主人看草稿")
        self.pick(T2, [a], by="butler")
        self.pick(T2, [b], by="lead")
        tail = self.tail(T2)
        self.assertIn(PICKED_HEAD.format(who="管家和负责人"), tail)
        memstore.void(self.state, a.ref, who="owner")
        memory_turn._plans.clear()
        tail = self.tail(T2)
        self.assertNotIn(a.text, tail)
        self.assertIn(PICKED_HEAD.format(who="负责人"), tail)


class ProbeTest(Case):
    """T923 DESIGN §4's seven probes, on fixture memory (not the live one): with the pool's notes made 全局 资料, the tail's
    search (``queries_of``, ``notes_of``) carries the deploy note to a turn that says it will deploy, the colleagues' note to a
    turn on a colleague's machine, the 代答 note to one sending as the owner, and none of them to a paper, a gateway's alert or
    a merge that does not deploy (the one the lead has to pick for)."""

    RULES = ("分析需求时先代入主人的视角，想清楚他为什么问、卡在哪里", "写文档和下结论只写实际有的数据，推算出来的数字不写",
             "回复主人用中文，会话再长也不换成英文", "开工前先查看当前状态，确认现况后再动手", "要主人拍板的决定用问题卡发出")
    NOTES = {
        "deploy": "Covey 的专项话题改完、检查过（测试和部署前预检通过）就直接部署到主人本机现网，网页有改一起发布",
        "peer": "同事（小林、小周）的机器全程只读、不改任何东西；同事原话与运行数据只在本地用、不外传",
        "send": "话题要以主人身份给同事发飞书消息时走 Covey 的代答发送（covey-internal），先给主人看草稿确认再发",
        "open": "管家开话题（排查、调研、改代码写实现）一律直接开工，不发启动卡等主人点开始",
    }
    CASES = (
        ("解读论文 2611.02440", ["群里发的这个 https://arxiv.org/pdf/2611.02440 帮忙解读一下",
                               "获取并通读论文，系统解读这篇论文的核心问题、方法、关键实验结果与启示"], set(), {"deploy", "peer", "send"}),
        ("解读 Code RL 涨点实验与算法", ["结合飞书文档、实验配置及训练代码库全面解读 3 次 Code RL 涨点实验的算法原理、超参与实现细节"],
         set(), {"deploy", "peer", "send"}),
        ("负责人与待审核语义收窄", ["按定稿方案实施记忆改造，合入主干并部署主人本机现网", "covey covey-internal"], {"deploy"}, set()),
        ("记忆一种读法与隔离", ["改 memory_turn.py 和 prompts.py，跑全量测试后合进 main", "covey"], set(), {"deploy"}),
        ("排查小林的 Covey 为什么卡住", ["去小林的机器上看看 Covey 日志，找出卡住的原因"], {"peer"}, {"deploy"}),
        ("通知小周升级", ["以主人身份给小周发条消息，告诉他今晚升级 covey-internal"], {"send"}, set()),
        ("开个话题把网关的报警阈值调一下", ["模型网关报警阈值调高"], set(), {"deploy", "peer", "send"}),
    )

    def test_the_seven_probes(self):
        for text in self.RULES:
            self.rule(text)
        codes = {name: memstore.add(self.state, kind="note", scope="global", text=text, said="owner", by="owner").code
                 for name, text in self.NOTES.items()}
        names = {code: name for name, code in codes.items()}
        for title, words, wanted, unwanted in self.CASES:
            with self.subTest(title):
                notes, _ = memory_turn.notes_of(self.state, [title, *words], owner=True)
                found = {names.get(entry.code) for entry, _ in notes}
                self.assertLessEqual(wanted, found)
                self.assertFalse(found & unwanted, found)
