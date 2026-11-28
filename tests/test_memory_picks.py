"""派活时挑带 (app/memory_picks.py, T923 DESIGN §3, §10.4): what the butler or a project's lead names in ``memory`` is checked
code by code (in force, a 资料, of 全局 or the lead's own project, at
most 5 and 800 characters with what the topic carries already), each refused one said with why; the ones taken are kept in
the topic's ``memory_carried`` once each and read back as they are now."""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from covey.app import memory_picks
from covey.core import memstore, projects

PID, OTHER, CHAT = "a1b2c3d4e5f6", "f6e5d4c3b2a1", "oc_owner_dm"
T1 = "a1" * 32


class Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        for pid, name in ((PID, "网关"), (OTHER, "账单")):
            projects.create(self.state, projects.new_record(chat_id=CHAT, name=name, project_id=pid, now=time.time()))

    def note(self, text, scope="global", owner_key="", **kw):
        return memstore.add(self.state, kind="note", scope=scope, owner_key=owner_key, text=text, said=kw.pop("said", "owner"),
                            by="owner", **kw)

    def check(self, codes, **kw):
        return memory_picks.check(self.state, codes, **kw)


class CheckTest(Case):
    def test_a_global_note_is_taken_and_every_other_kind_of_code_is_refused_with_why(self):
        deploy = self.note("Covey 的专项话题改完、检查过就部署到主人本机现网")
        mine = self.note("网关的预检脚本在 scripts/check.sh", scope="project", owner_key=PID)
        theirs = self.note("账单库只读", scope="project", owner_key=OTHER)
        rule = memstore.add(self.state, kind="rule", scope="global", text="回复主人用中文", said="owner", by="owner")
        gone = self.note("旧的做法")
        memstore.void(self.state, gone.ref, who="owner")
        topic = self.note("只对这件事", scope="topic", owner_key=T1)
        taken, refused = self.check([deploy.code, mine.code, theirs.code, rule.code, gone.code, topic.code, "R9ZZZZ"],
                                    project=PID)
        self.assertEqual([entry.code for entry in taken], [deploy.code, mine.code])
        self.assertEqual(len(refused), 5)
        words = "\n".join(refused)
        # T969: a refused entry is named by its title (the code only when there is no entry).
        for named, why in ((f"「{theirs.title}」", "只能带全局的或本专项的"), (f"「{rule.title}」", "是规矩"),
                           (f"「{gone.title}」", "不在用了"), (f"「{topic.title}」", "只能带全局的或本专项的"), ("R9ZZZZ", "找不到")):
            self.assertIn(f"{named}：", words)
            self.assertIn(why, next(line for line in refused if line.startswith(named)))
        for entry in (theirs, rule, gone, topic):
            self.assertNotIn(entry.code, words)

    def test_the_butler_takes_global_only(self):
        mine = self.note("网关的预检脚本在 scripts/check.sh", scope="project", owner_key=PID)
        taken, refused = self.check([mine.code])
        self.assertEqual((taken, len(refused)), ([], 1))

    def test_at_most_five_and_800_characters_with_what_the_topic_carries_already(self):
        made = [self.note(f"第 {i} 条：" + "字" * 100) for i in range(7)]
        taken, refused = self.check([entry.code for entry in made])
        self.assertEqual(len(taken), 5)
        self.assertEqual(len(refused), 2)
        self.assertTrue(all("最多带 5 条" in line for line in refused))
        long = [self.note(f"长的 {i}：" + "字" * 190) for i in range(5)]
        taken, refused = self.check([entry.code for entry in long])
        self.assertEqual(len(taken), 4)  # 4 × ~195 within 800, the fifth past it.
        self.assertIn("800 字", refused[0])
        # What it carries already counts, and is not taken twice (nothing said of it).
        have = [(made[0], "butler")]
        taken, refused = self.check([made[0].code, *(entry.code for entry in made[1:5])], carried=have)
        self.assertEqual(([e.code for e in taken], refused), ([e.code for e in made[1:5]], []))

    def test_one_string_of_codes_and_the_same_code_twice(self):
        a, b = self.note("甲"), self.note("乙")
        self.assertEqual(memory_picks.codes_of(f"{a.code}，{b.code}、{a.code.lower()}"), [a.code, b.code])
        self.assertEqual(memory_picks.codes_of(None), [])
        self.assertEqual(memory_picks.codes_of({"x": 1}), [])


class RememberTest(Case):
    def test_added_once_each_and_read_back_as_they_are_now(self):
        a = self.note("Covey 的专项话题改完、检查过就部署到主人本机现网")
        b = self.note("同事的机器只读", said="summary")
        data = {}
        self.assertEqual(len(memory_picks.remember(data, [a], "lead")), 1)
        self.assertEqual(memory_picks.remember(data, [a, b], "butler"), [{"code": b.code, "ref": b.ref, "by": "butler"}])
        self.assertEqual(data[memory_picks.KEY], [{"code": a.code, "ref": a.ref, "by": "lead"},
                                                   {"code": b.code, "ref": b.ref, "by": "butler"}])
        self.assertEqual([(e.code, by) for e, by in memory_picks.entries_of(self.state, data)], [(a.code, "lead"), (b.code, "butler")])
        memstore.void(self.state, b.ref, who="owner")  # Put away since: no longer carried, never an error.
        self.assertEqual([e.code for e, _ in memory_picks.entries_of(self.state, data)], [a.code])
        memstore.set_kind(self.state, a.ref, "rule", who="owner")  # A rule now: every turn has it in its rules already.
        self.assertEqual(memory_picks.entries_of(self.state, data), [])
        self.assertEqual(memory_picks.entries_of(self.state, {memory_picks.KEY: ["R1", {"code": ""}]}), [])

    def test_a_project_note_is_read_back_only_in_that_project(self):
        mine = self.note("网关的预检脚本在 scripts/check.sh", scope="project", owner_key=PID)
        data = {}
        memory_picks.remember(data, [mine], "lead")
        self.assertEqual(len(memory_picks.entries_of(self.state, data, project=PID)), 1)
        self.assertEqual(memory_picks.entries_of(self.state, data, project=OTHER), [])

    def test_the_answer_says_what_was_taken_and_what_was_not(self):
        a = self.note("甲")
        self.assertEqual(memory_picks.said([], []), "")
        words = memory_picks.said([a], ["R9ZZZZ：找不到这条记忆"])
        self.assertIn(f"带上了记忆「{a.title}」（按原文放在每一轮消息的末尾）", words)
        self.assertNotIn(a.code, words)
        self.assertIn("没带上的记忆：R9ZZZZ：找不到这条记忆；其余照常", words)
        self.assertIn("从它的下一轮起", memory_picks.said([a], [], steered=True))


if __name__ == "__main__":
    unittest.main()
