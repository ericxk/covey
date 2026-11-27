"""The butler's memory tools (app/butler_knowledge.py memory_save, memory_revise, memory_search): a save goes through the write
gate and into core/memstore.py as the owner's own by the butler, a revise updates, replaces or retires what its key names, a
search lists short code, kind, scope and text. The tools are not in ``TOOLS`` (app/butler_memory.py adapts them)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from covey.app import butler_knowledge
from covey.core import memory_gate, memstore
from covey.core.state import write_json

PID = "a1b2c3d4e5f6"


def gate(text, *, kind, title=""):
    """The write gate's stand-in: a time word is refused with the sentence rewritten, 这个 only flagged."""
    if "今天" in text:
        return memory_gate.Verdict(False, text.replace("今天", ""), (), "含有会过期的时间词")
    return memory_gate.Verdict(True, "", ("vague",) if "这个" in text else (), "")


class Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.env = SimpleNamespace(state_dir=self.state)
        patcher = mock.patch.object(memory_gate, "check", gate)
        patcher.start()
        self.addCleanup(patcher.stop)

    def save(self, **args):
        return butler_knowledge.memory_save(self.env, {"kind": "rule", "scope": "global", **args})


class ShapeTest(Case):
    def test_the_memory_tools_are_not_in_the_tool_table(self):
        self.assertFalse([name for name in butler_knowledge.TOOLS if name.startswith("memory_")])
        self.assertIn("knowledge_save", butler_knowledge.TOOLS)


class SaveTest(Case):
    def test_a_global_rule_is_written_as_the_owners_by_the_butler(self):
        answer = self.save(text="汇报先给结论", title="汇报顺序", why="他嫌长")
        self.assertTrue(answer["ok"])
        [entry] = memstore.entries(self.state, scope="global")
        self.assertEqual((entry.kind, entry.said, entry.by, entry.title, entry.why), ("rule", "owner", "butler", "汇报顺序", "他嫌长"))
        self.assertEqual(answer["text"], f"记下了：{entry.code} 汇报顺序 · 管全局")
        self.assertEqual((answer["data"]["code"], answer["data"]["ref"]), (entry.code, entry.ref))

    def test_a_note_keeps_its_repos_and_a_decision_goes_to_the_decisions(self):
        self.assertTrue(self.save(kind="note", text="rg 会扫 NAS", repos=["covey"])["ok"])
        [note] = memstore.entries(self.state, scope="global", kinds=("note",))
        self.assertEqual(note.repos, ("covey",))
        write_json(self.state / "projects" / f"{PID}.json", {"id": PID, "name": "迁移", "chat_id": "oc"})
        answer = self.save(kind="decision", scope="project", project="迁移", text="选 Postgres，不用 Redis")
        self.assertTrue(answer["ok"], answer)
        [decision] = memstore.decisions(self.state, "project", PID)
        self.assertEqual((decision.text, decision.said, decision.by), ("选 Postgres，不用 Redis", "owner", "butler"))
        self.assertTrue(answer["text"].endswith("管这个专项"))

    def test_a_topic_is_named_by_its_id_or_its_t_number(self):
        write_json(self.state / "topics" / "om_t1" / "topic.json", {"topic_number": 483})
        for name in ("om_t1", "T483"):
            with self.subTest(name):
                self.assertTrue(self.save(scope="topic", topic=name, text=f"这件事别再先问（{name}）")["ok"])
        self.assertEqual(len(memstore.entries(self.state, scope="topic", owner_key="om_t1")), 2)

    def test_a_flagged_text_is_written_and_a_refused_one_is_not(self):
        self.assertTrue(self.save(text="这个仓库不跑全量")["ok"])
        answer = self.save(text="今天起汇报先给结论")
        self.assertFalse(answer["ok"])
        self.assertIn("可以改成：起汇报先给结论", answer["text"])
        self.assertEqual([e.text for e in memstore.entries(self.state, scope="global")], ["这个仓库不跑全量"])

    def test_a_project_or_topic_needs_saying_which_and_it_must_exist(self):
        for args, words in ((dict(scope="project"), "project"), (dict(scope="project", project="没有"), "没有叫"),
                            (dict(scope="topic"), "topic"), (dict(scope="topic", topic="T9"), "没有 T9")):
            with self.subTest(args):
                answer = self.save(text="汇报先给结论", **args)
                self.assertFalse(answer["ok"])
                self.assertIn(words, answer["text"])
        self.assertFalse(self.save(text="")["ok"])
        self.assertEqual(memstore.entries(self.state), [])


class ReviseTest(Case):
    def setUp(self):
        super().setUp()
        self.entry = memstore.add(self.state, kind="rule", scope="global", text="汇报先给表", title="汇报")

    def revise(self, **args):
        return butler_knowledge.memory_revise(self.env, args)

    def test_update_edits_in_place_and_keeps_the_code(self):
        answer = self.revise(key=self.entry.code, action="update", text="汇报先给结论")
        self.assertTrue(answer["ok"], answer)
        [entry] = memstore.entries(self.state, scope="global")
        self.assertEqual((entry.code, entry.text, entry.title), (self.entry.code, "汇报先给结论", "汇报"))

    def test_supersede_writes_a_new_entry_and_puts_the_old_one_away(self):
        answer = self.revise(key=self.entry.ref, action="supersede", text="汇报先给结论", title="汇报顺序")
        self.assertTrue(answer["ok"], answer)
        [new] = memstore.entries(self.state, scope="global")
        self.assertEqual((new.text, new.kind), ("汇报先给结论", "rule"))
        old = memstore.find(self.state, self.entry.ref)
        self.assertEqual((old.status, old.superseded_by), ("archived", new.ref))

    def test_retire_archives_a_rule_and_settles_a_decision(self):
        self.assertTrue(self.revise(key=self.entry.code.lower(), action="retire")["ok"])
        self.assertEqual(memstore.find(self.state, self.entry.ref).status, "archived")
        write_json(self.state / "projects" / f"{PID}.json", {"id": PID, "name": "迁移", "chat_id": "oc"})
        decision = memstore.add_decision(self.state, scope="project", owner_key=PID, text="选 Postgres")
        self.assertTrue(self.revise(key=decision.code, action="retire")["ok"])
        self.assertEqual(memstore.find(self.state, decision.ref).status, "done")

    def test_an_unknown_key_a_bad_action_a_missing_text_and_a_refused_text_change_nothing(self):
        self.assertEqual(self.revise(key="ZZZZZ", action="retire")["text"], "没有 ZZZZZ：先用 memory_search 找短码。")
        self.assertFalse(self.revise(key=self.entry.code, action="erase")["ok"])
        self.assertFalse(self.revise(key=self.entry.code, action="supersede")["ok"])
        answer = self.revise(key=self.entry.code, action="update", text="今天起先给结论")
        self.assertFalse(answer["ok"])
        self.assertIn("可以改成", answer["text"])
        entry = memstore.find(self.state, self.entry.ref)
        self.assertEqual((entry.text, entry.status), ("汇报先给表", "active"))


class SearchTest(Case):
    def test_a_query_is_ranked_by_the_search_and_marks_what_is_done_or_put_away(self):
        live = memstore.add(self.state, kind="rule", scope="global", text="汇报先给结论")
        gone = memstore.add(self.state, kind="note", scope="global", text="旧的汇报写法")
        memstore.archive(self.state, gone.ref)
        rows = [{"ref": e.ref, "code": e.code, "kind": e.kind, "scope": e.scope, "title": e.title, "text": e.text, "sim": 0.8}
                for e in (live, gone)]
        with mock.patch("covey.core.memory_search.eval_search", return_value=rows) as search:
            answer = butler_knowledge.memory_search(self.env, {"query": "汇报", "limit": 50})
        self.assertEqual(search.call_args.args[:2], (self.state, "汇报"))
        self.assertEqual(search.call_args.args[3], 20)  # At most twenty.
        self.assertEqual(search.call_args.args[2]["statuses"], ("active", "done", "archived"))
        self.assertTrue(search.call_args.args[2]["owner"])
        lines = answer["text"].splitlines()
        self.assertEqual(lines[0], f"{live.code} · 规矩 · 全局 · 汇报先给结论")
        self.assertEqual(lines[1], f"{gone.code} · 资料 · 全局 · 旧的汇报写法（已收起）")
        self.assertEqual(answer["data"]["count"], 2)
        self.assertEqual(answer["data"]["items"][0], {"code": live.code, "ref": live.ref, "kind": "rule", "scope": "global",
                                                      "title": live.title, "sim": 0.8})

    def test_no_query_lists_the_newest_active_entries_cut_to_a_line(self):
        for n in range(3):
            memstore.add(self.state, kind="rule", scope="global", text=f"第 {n} 条" + "长" * 150, now=1_800_000_000 + n)
        answer = butler_knowledge.memory_search(self.env, {"limit": 2})
        lines = answer["text"].splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith(f"{memstore.entries(self.state)[2].code} · 规矩 · 全局 · 第 2 条"))
        self.assertEqual(len(lines[0].split(" · ")[3]), 120)
        self.assertEqual(answer["data"]["count"], 2)

    def test_nothing_found_says_so(self):
        answer = butler_knowledge.memory_search(self.env, {})
        self.assertEqual((answer["ok"], answer["data"]), (True, {"count": 0, "items": []}))


class LeadSearchTest(unittest.TestCase):
    """WAVE12 W2-C: a project's lead has memory_search (core/butler_tool_specs.py LEAD_SHARED, SECONDARY on agy), and its call
    reads only its own project's memory and its topics' (app/butler_memory.py memory_search, app/butler_knowledge.py
    lead_scope): never 全局, never another project's or an ordinary topic's, whatever it names; the butler's reads as before."""

    def setUp(self):
        import os

        from support.records import state_dir, write_job, write_topic
        from covey.core import butler, projects

        self.state = state_dir(self)
        patch = mock.patch.dict(os.environ, {projects.ENV: "1"})
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(butler.configure_state, butler.configure_state(self.state))
        self.chat, self.other = "oc_owner_dm", "f6e5d4c3b2a1"
        for pid, name in ((PID, "改造"), (self.other, "别的")):
            projects.create(self.state, projects.new_record(chat_id=self.chat, name=name, project_id=pid, now=1_800_000_000))
        self.lead, self.member, self.theirs, self.plain = (projects.lead_topic_id(self.chat, PID), "ab" * 16, "ce" * 16, "cd" * 16)
        write_topic(self.state, topic_id=self.lead, chat_id=self.chat, project=projects.project_field(PID, projects.LEAD))
        write_topic(self.state, topic_id=self.member, chat_id=self.chat, topic_number=31,
                    project=projects.project_field(PID, projects.MEMBER))
        write_topic(self.state, topic_id=self.theirs, chat_id=self.chat, topic_number=32,
                    project=projects.project_field(self.other, projects.MEMBER))
        write_topic(self.state, topic_id=self.plain, chat_id=self.chat, topic_number=33)
        write_job(self.state, "lead-job", topic_id=self.lead, chat_id=self.chat, job_id=9, status="running")
        write_job(self.state, "butler-job", topic_id="", chat_id=self.chat, job_id=10, status="running")
        self.env = SimpleNamespace(state_dir=self.state, runtime=SimpleNamespace(log=lambda line: None, access=None))
        self.mine = memstore.add(self.state, kind="rule", scope="project", owner_key=PID, text="部署窗口在晚上十点以后")
        self.topic_line = memstore.add(self.state, kind="note", scope="topic", owner_key=self.member, text="部署脚本在 ops 目录")
        memstore.add(self.state, kind="rule", scope="project", owner_key=self.other, text="部署窗口在早上八点以前")
        memstore.add(self.state, kind="rule", scope="global", text="部署前先跑预检")
        memstore.add(self.state, kind="note", scope="topic", owner_key=self.plain, text="部署记录写在普通话题里")

    def search(self, job="lead-job", **args):
        from covey.app import butler_memory

        return butler_memory.memory_search(self.env, SimpleNamespace(job_key=job, args=args, id="r1", tool="memory_search"))

    def test_the_lead_lists_memory_search_and_agy_keeps_it_out_of_the_up_front_room(self):
        from covey.core import butler_tool_specs

        names = [tool["name"] for tool in butler_tool_specs.listed({"COVEY_PROJECT_LEAD": PID, "COVEY_PROJECTS": "1"})]
        self.assertIn("memory_search", names)
        self.assertNotIn("memory_save", names)
        self.assertNotIn("memory_revise", names)
        self.assertIn("memory_search", butler_tool_specs.SECONDARY)

    def test_a_query_reads_only_its_own_project_as_a_turn_not_the_owners(self):
        seen = []
        real = __import__("covey.core.memory_search", fromlist=["eval_search"]).eval_search

        def spy(state, query, context=None, limit=5):
            seen.append(dict(context or {}))
            return real(state, query, context, limit)
        with mock.patch("covey.core.memory_search.eval_search", spy):
            answer = self.search(query="部署窗口")
        self.assertTrue(answer["ok"], answer)
        self.assertEqual((seen[0]["owner"], seen[0]["project"], seen[0]["topic"]), (False, PID, ""))
        self.assertIn(self.mine.code, answer["text"])
        for words in ("早上八点", "先跑预检", "普通话题"):
            self.assertNotIn(words, answer["text"])
        with mock.patch("covey.core.memory_search.eval_search", spy):
            self.search(query="部署脚本", topic="T31")  # One of its topics: that topic's lines too.
        self.assertEqual((seen[1]["owner"], seen[1]["project"], seen[1]["topic"]), (False, PID, self.member))

    def test_another_project_or_a_topic_not_its_own_is_refused(self):
        for args in ({"project": self.other}, {"project": "别的"}, {"topic": "T32"}, {"topic": "T33"}, {"topic": "T99"}):
            with mock.patch("covey.core.memory_search.eval_search") as search:
                answer = self.search(query="部署", **args)
            self.assertFalse(answer["ok"], args)
            self.assertIn("只能查本专项的记忆", answer["text"])
            search.assert_not_called()
        self.assertTrue(self.search(query="部署窗口", project=PID)["ok"])  # Its own, named: as without.

    def test_no_query_lists_its_projects_newest_and_never_another_scope(self):
        answer = self.search()
        self.assertEqual(answer["data"]["count"], 1)
        self.assertIn(self.mine.code, answer["text"])
        answer = self.search(topic=self.member)
        self.assertEqual([item["code"] for item in answer["data"]["items"]], [self.topic_line.code])

    def test_the_butler_still_reads_everything_as_the_owner(self):
        seen = []
        with mock.patch("covey.core.memory_search.eval_search", lambda state, query, context=None, limit=5:
                        seen.append(dict(context or {})) or []):
            self.search(job="butler-job", query="部署", project=self.other)
        self.assertEqual((seen[0]["owner"], seen[0]["project"]), (True, self.other))


if __name__ == "__main__":
    unittest.main()
