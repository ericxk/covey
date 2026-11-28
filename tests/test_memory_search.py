"""查记忆 (core/memory_search.py, bin/covey_memory.py): the words (short symbols and hyphenated names kept, the words every
question has dropped), BM25F with an absolute similarity (one weak hit stays weak, a word no entry has counts for nothing),
enough words in common, the scopes' weights, a question's decisions once, who may read what, two texts' mutual similarity and
the command's search, show (the ``show`` signal) and list."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import REPO, UNSET_OFF
from support.records import write_topic

from covey.core import memory_search, memory_usage, memstore, projects

PID, OTHER, CHAT = "a1b2c3d4e5f6", "f6e5d4c3b2a1", "oc_owner_dm"
TOPIC = "ab" * 32
NOW = 1_800_000_000.0


class TermsTest(unittest.TestCase):
    def test_short_symbols_next_to_chinese_and_hyphenated_names_are_kept(self):
        words = memory_search.terms("选 F 版，P0-A 先做；covey-internal 的 kvoff-idx256")
        for word in ("f", "p0-a", "p0", "covey-internal", "covey", "internal", "kvoff-idx256", "idx256"):
            self.assertIn(word, words)
        self.assertNotIn("x", memory_search.terms("x y z only latin"))  # A lone letter among Latin words is no word.

    def test_the_words_every_question_has_are_dropped(self):
        words = memory_search.terms("要不要现在部署")
        self.assertNotIn("要不", words)
        self.assertNotIn("不要", words)
        self.assertIn("部署", words)

    def test_synonyms_widen_the_query_alone_at_half_weight(self):
        wanted = memory_search.query_weights("什么时候上线")
        self.assertEqual(wanted["上线"], 1.0)
        self.assertEqual(wanted["部署"], memory_search.SYNONYM_WEIGHT)
        self.assertNotIn("部署", memory_search.tokens("什么时候上线"))


class Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        patcher = mock.patch.dict(os.environ, {name: value for name, value in os.environ.items()
                                               if not name.startswith(("COVEY_", "LARK_BRIDGE_"))} | UNSET_OFF, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        for pid, name in ((PID, "网关"), (OTHER, "推理")):
            projects.create(self.state, projects.new_record(chat_id=CHAT, name=name, project_id=pid, now=NOW))
        write_topic(self.state, topic_id=TOPIC, topic_number=7, chat_id=CHAT, project=projects.project_field(PID, projects.MEMBER))
        projects.add_member(self.state, PID, TOPIC)

    def add(self, text, kind="note", scope="project", owner_key=PID, **kw):
        return memstore.add(self.state, kind=kind, scope=scope, owner_key=owner_key if scope != "global" else "", text=text, **kw)

    def found(self, query, **context):
        return memory_search.eval_search(self.state, query, {"project": PID, "topic": TOPIC, **context}, limit=8)


class ScoreTest(Case):
    def test_similarity_is_absolute_a_lone_weak_hit_stays_weak(self):
        weak = self.add("镜像仓库在内网，拉取要走代理")
        self.add("周报每周五发，网关的事抄送负责人")  # The query's other words are in other entries: they weigh in its own score.
        self.add("预发环境回归测试的脚本在仓库根目录")
        self.add("上线发布按批次走")
        query = "镜像拉取走代理吗，另外网关发布后要先在预发环境回归测试再上线并通知负责人"
        alone = {hit["ref"]: hit["sim"] for hit in self.found(query, tau=0.0)}[weak.ref]
        self.assertLess(alone, 0.5)  # The best there is, and still not 1 (no rel = final / max).
        strong = self.add("网关发布后先在预发环境回归测试，再上线并通知负责人；镜像拉取走代理")
        found = {hit["ref"]: hit["sim"] for hit in self.found(query, tau=0.0)}
        self.assertGreater(found[strong.ref], 0.8)
        self.assertLess(found[weak.ref], 0.5)

    def test_a_word_no_entry_has_counts_neither_for_nor_against(self):
        self.add("网关镜像先发预发再发生产")
        plain = self.found("网关镜像先发预发", tau=0.0)[0]["sim"]
        odd = self.found("网关镜像先发预发 zxqv", tau=0.0)[0]["sim"]
        self.assertAlmostEqual(plain, odd, places=3)

    def test_one_common_word_alone_is_not_a_hit(self):
        for i in range(30):
            self.add(f"第{i}批部署记录{i}")
        self.add("夜间备份数据库要加密")
        hits = self.found("部署", tau=0.0)
        self.assertTrue(all(not hit["covered"] for hit in memory_search.rank_index(memory_search.index(self.state), "部署",
                                                                                   {"tau": 0.0})) or hits == [])
        self.assertEqual(self.found("部署"), [])

    def test_the_turns_own_scopes_come_before_another_projects_and_done_after_both(self):
        mine = self.add("发布前先在预发环境验证一遍")
        theirs = self.add("发布前先在预发环境验证一遍。", owner_key=OTHER)
        old = self.add("发布前先在预发环境验证一遍！")
        memstore.archive(self.state, old.ref)
        refs = [hit["ref"] for hit in self.found("发布前要先在预发环境验证吗")]
        self.assertEqual(refs[:3], [mine.ref, theirs.ref, old.ref])

    def test_a_questions_decisions_are_listed_once(self):
        memstore.add_decision(self.state, scope="project", owner_key=PID, question="镜像要不要先发预发？", answer="先发",
                              question_ref="aaaa-1", now=NOW)
        memstore.add_decision(self.state, scope="project", owner_key=PID, question="镜像要不要先发预发？", answer="直接发",
                              question_ref="bbbb-1", now=NOW + 10)
        hits = [hit for hit in self.found("镜像先发预发吗") if hit["kind"] == "decision"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(sorted(hits[0]), sorted(["ref", "code", "kind", "scope", "owner_key", "project", "status", "said",
                                                  "title", "text", "aliases", "at", "sim", "score", "weight", "covered",
                                                  "shared"]))


class ReadersTest(Case):
    def test_who_may_read_what(self):
        self.add("回复主人用中文", kind="rule", scope="global")
        self.add("脱敏话题里记的接口名", redacted=True)
        self.add("待审核的一条接口名", status="pending")
        self.assertTrue(self.found("回复主人用中文"))
        self.assertEqual(self.found("回复主人用中文", owner=False), [])
        self.assertEqual(self.found("接口名", tau=0.0, kinds=("note",)), [])
        self.assertTrue(self.found("脱敏话题里记的接口名", redacted=True))
        self.assertEqual(self.found("回复主人用中文", proxy=True), [])

    def test_the_index_follows_a_write(self):
        self.assertEqual(self.found("镜像仓库在内网"), [])
        self.add("镜像仓库在内网，拉取要走代理")
        self.assertTrue(self.found("镜像仓库在内网"))


class MutualTest(unittest.TestCase):
    def test_the_smaller_of_both_directions(self):
        self.assertEqual(memory_search.mutual_similarity("回复主人用中文", "回复主人用中文"), (1.0, True))
        one, covered = memory_search.mutual_similarity("回复主人用中文", "回复主人一律用中文")
        self.assertTrue(0.5 < one < 1.0 and covered)
        self.assertEqual(memory_search.mutual_similarity("回复主人用中文", "回复主人一律用中文"),
                         memory_search.mutual_similarity("回复主人一律用中文", "回复主人用中文"))
        self.assertLess(memory_search.mutual_similarity("部署前先跑预检", "合并前先 fetch 再 rebase")[0], 0.2)


class CommandTest(Case):
    def run_cli(self, *args):
        found = subprocess.run([sys.executable, "-B", str(REPO / "bin" / "covey_memory.py"), *args, "--state", str(self.state)],
                               capture_output=True, text=True, timeout=60, env={**os.environ, "PYTHONPATH": ""})
        return found.returncode, found.stdout

    def test_search_show_and_list(self):
        made = self.add("镜像仓库在内网，拉取要走代理")
        code, out = self.run_cli("search", "镜像仓库拉取", "--topic", TOPIC)
        self.assertEqual(code, 0)
        self.assertIn(f"- {made.code} · 资料 · 专项 {PID} · 你定的：镜像仓库在内网", out)
        code, out = self.run_cli("search", "镜像仓库拉取", "--topic", TOPIC, "--json")
        self.assertEqual(json.loads(out)[0]["code"], made.code)
        code, out = self.run_cli("show", made.code.lower(), "--topic", TOPIC)
        self.assertEqual(code, 0)
        self.assertIn("正文：镜像仓库在内网，拉取要走代理", out)
        self.assertEqual(memory_usage.counts(self.state)[made.ref]["h"], 1.0)  # The show signal.
        self.assertEqual(self.run_cli("show", "RZZZZ")[0], 1)
        code, out = self.run_cli("list", "--topic", TOPIC)
        self.assertIn(made.code, out)

    def test_the_old_workspace_option_is_taken_and_ignored(self):
        self.add("镜像仓库在内网，拉取要走代理")
        code, out = self.run_cli("search", "镜像仓库拉取", "--topic", TOPIC, "--workspace", "ws")
        self.assertEqual(code, 0)
        self.assertIn("镜像仓库在内网", out)



class IsolationTest(Case):
    """T908 M5: ``show`` without ``--owner`` reads what the turn's ``search`` reads (``_allowed``), and the topic's own record,
    not the command line, says whether it is a 代答 topic and whether it reads the lines from topics with redaction on."""

    def redact(self, topic, on=True, **more):
        path = self.state / "topics" / topic / "topic.json"
        path.write_text(json.dumps({**json.loads(path.read_text()), "redaction": on, **more}, ensure_ascii=False))

    def test_show_without_owner_holds_to_the_turns_topic_and_project(self):
        other_topic = "cd" * 32
        write_topic(self.state, topic_id=other_topic, topic_number=8, chat_id=CHAT, project=projects.project_field(PID, projects.MEMBER))
        mine = self.add("网关镜像仓库在内网")
        theirs = self.add("推理服务的镜像在另一个仓库", owner_key=OTHER)
        sibling = self.add("话题8自己的约定", scope="topic", owner_key=other_topic)
        own = self.add("这个话题自己的约定", scope="topic", owner_key=TOPIC)
        everyone = self.add("回复主人用中文", kind="rule", scope="global")
        decision = memstore.add_decision(self.state, scope="project", owner_key=PID, question="先发预发吗？", answer="先发预发",
                                         topic_key=other_topic, question_ref="cdcd1-1")
        shown = lambda key, **kw: memory_search.show(self.state, key, topic_id=TOPIC, **kw)  # noqa: E731
        for entry in (mine, own, decision):
            self.assertIsNotNone(shown(entry.code), entry.text)
        for entry in (theirs, sibling, everyone):
            self.assertIsNone(shown(entry.code), entry.text)  # Before M5 another project's and topic's came back whole.
            self.assertIsNotNone(shown(entry.code, owner=True), entry.text)  # --owner: as before (the known limit).
        self.assertIsNone(memory_search.show(self.state, mine.code))  # No topic, no --owner: nothing is the turn's.

    def test_redaction_and_proxy_are_read_off_the_topic_not_the_command_line(self):
        hidden = self.add("脱敏话题里记的接口名", redacted=True)
        self.assertIsNone(memory_search.show(self.state, hidden.code, topic_id=TOPIC, redacted=True))  # The flag is read past.
        self.assertEqual(memory_search.search(self.state, "脱敏话题里记的接口名", TOPIC, redacted=True), [])
        self.assertNotIn(hidden.code, [row["code"] for row in memory_search.listing(self.state, TOPIC, redacted=True)])
        self.redact(TOPIC)
        self.assertIsNotNone(memory_search.show(self.state, hidden.code, topic_id=TOPIC))  # The topic has it on: read.
        self.assertTrue(memory_search.search(self.state, "脱敏话题里记的接口名", TOPIC))
        self.assertIn(hidden.code, [row["code"] for row in memory_search.listing(self.state, TOPIC)])
        self.redact(TOPIC, proxy=True)
        self.assertIsNone(memory_search.show(self.state, hidden.code, topic_id=TOPIC, owner=True))
        # No topic (a person at a terminal): the flag is what there is.
        self.assertTrue(memory_search.search(self.state, "脱敏话题里记的接口名", owner=True, redacted=True))
        self.assertEqual(memory_search.search(self.state, "脱敏话题里记的接口名", owner=True), [])

    def test_a_turns_use_does_not_build_the_index_anew(self):
        made = self.add("镜像仓库在内网，拉取要走代理")
        first = memory_search.index(self.state)
        memory_usage.signal(self.state, [made.ref], "show")
        again = memory_search.index(self.state)
        self.assertIs(again, first)  # The same postings: before M5 the usage file was part of the key.
        self.assertEqual(again.usage[made.ref]["h"], 1.0)  # with the use counts as they are now.
        self.add("另一条：发布先过预发")
        self.assertIsNot(memory_search.index(self.state), first)  # A write still builds it anew.


if __name__ == "__main__":
    unittest.main()
