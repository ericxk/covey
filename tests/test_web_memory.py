"""The web page's 记忆 tab of data contract v37 (app/web_memory.py; T748): the ``memory`` item the page reads (全局 and each
workspace's principles and knowledge entries, the room they use, which the next turn carries, the next turn's block) and its
push (app/web_sync.py push_memory): alone in its request, again only when a file of the memory changed. Read only since T908
M3b: v37's ``memory_edit`` without ``v`` is refused (the page writes through v43's, tests/test_web_memory_v43.py)."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, mock

from test_web_sync import WebCase
from covey.app import web_memory
from covey.app.web_ops import Ops
from covey.core import knowledge, memory, principles
from covey.core.state import write_json
from covey.core.workspaces import config_path


class StateCase(TestCase):
    def setUp(self):
        self.enterContext(mock.patch.dict(os.environ, {"COVEY_MEMORY": "1", "COVEY_KNOWLEDGE": "1", "COVEY_MEMORY_LEARN": "0"}))
        self.state = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for name in ("project", "docs"):
            (self.state / name).mkdir()
        write_json(config_path(self.state), {"default": "project", "workspaces": {
            "project": str(self.state / "project"), "docs": str(self.state / "docs")}})

    def group(self, data, workspace=""):
        return next(g for g in data["groups"] if g["workspace"] == workspace)


class ViewTests(StateCase):
    def test_groups_by_scope_with_room_carried_and_the_next_turns_block(self):
        a, _ = principles.save(self.state, principles.OWNER, "回复先给结论再给细节")
        b, _ = principles.save(self.state, principles.OWNER, "数字都要有出处")
        principles.pin(self.state, b["id"], True)
        principles.save(self.state, principles.WORKSPACE, "这个仓库的提交说明写英文", workspace="docs")
        k, _ = knowledge.save(self.state, knowledge.OWNER, "decision", "周报每周一早上发", why="主人定的")
        data = web_memory.view(self.state)
        self.assertEqual((data["on"], data["knowledge"], data["learn"], data["default"]), (True, True, False, "project"))
        self.assertEqual(data["workspaces"], ["project", "docs"])
        self.assertEqual(data["limits"][memory.GLOBAL], memory.LIMITS[memory.GLOBAL])
        self.assertEqual([g["title"] for g in data["groups"]], ["全局", "工作区 docs"])  # project has no entries: not a group.
        rows = self.group(data)["entries"]
        self.assertEqual(rows[0]["key"], f"principle:{b['id']}")  # 常驻 first.
        self.assertTrue(rows[0]["pinned"] and rows[0]["pinnable"] and rows[0]["editable"] and rows[0]["carried"])
        know = next(r for r in rows if r["kind"] == "knowledge")
        self.assertEqual((know["key"], know["knowledge_kind"], know["why"], know["pinnable"]), (f"knowledge:{k['id']}", "decision", "主人定的", False))
        self.assertEqual({r["by"] for r in rows}, {memory.OWNER})
        self.assertGreater(self.group(data)["used"], 0)
        self.assertEqual(self.group(data)["left"], 0)
        self.assertIn(a["text"], data["previews"]["project"]["text"])
        self.assertIn("这个仓库的提交说明写英文", data["previews"]["docs"]["text"])
        self.assertNotIn("这个仓库的提交说明写英文", data["previews"]["project"]["text"])
        self.assertEqual(data["candidates"], [])

    def test_knowledge_off_lists_principles_only_and_a_full_scope_says_what_is_left_out(self):
        knowledge.save(self.state, knowledge.OWNER, "fact", "一条知识")
        for n in range(30):
            principles.save(self.state, principles.OWNER, f"第 {n} 条很长的原则：" + "要" * 60)
        with mock.patch.dict(os.environ, {"COVEY_KNOWLEDGE": "0"}):
            data = web_memory.view(self.state)
        group = self.group(data)
        self.assertFalse(data["knowledge"])
        self.assertEqual({r["kind"] for r in group["entries"]}, {"principle"})
        self.assertGreater(group["left"], 0)
        self.assertEqual(sum(1 for r in group["entries"] if not r["carried"]), group["left"])



class T852Tests(StateCase):
    def test_a_knowledge_entry_is_pushed_as_its_own_words(self):
        k, _ = knowledge.save(self.state, knowledge.OWNER, "decision", "周报每周一早上发", why="主人定的")
        row = self.group(web_memory.view(self.state))["entries"][0]
        self.assertEqual((row["text"], row["knowledge_kind"], row["why"]), ("周报每周一早上发", "decision", "主人定的"))
        self.assertNotIn("原因", row["text"])
        self.assertEqual(row["size"], len("周报每周一早上发"))
        self.assertGreater(row["chars"], row["size"])  # The room a line uses: the number and the speaker around it count too.

    def test_origin_says_what_the_source_says(self):
        cases = [({"via": "web"}, "", "网页上记的", "", ""),
                 ({"via": "settings_card", "from": "p1"}, "", "设置卡里升为全局的", "", ""),
                 ({"by": "memory_learn", "job_key": "none", "quote": "以后回复先给结论"}, "", "自动学习记的", "以后回复先给结论", ""),
                 ({"by": "memory_candidates", "topics": []}, "", "晚间候选里记下的", "", ""),
                 ({"topic": "T83", "job_key": "j", "job_id": 4}, "topic", "话题 T83 里记的", "", "T83"),
                 ({"job_key": "j", "job_id": 4}, "main", "管家记的", "", ""),
                 ({}, "", "", "", "")]
        for source, by, words, quote, topic in cases:
            found = web_memory.origin_of(source, by, self.state)
            self.assertEqual((found["origin"], found["quote"], found["topic"]), (words, quote, topic), source)

    def test_a_note_in_the_source_follows_where_it_came_from(self):
        found = web_memory.origin_of({"by": "memory_candidates", "topics": [], "note": "原是 Bridge 模板里的话"}, "", self.state)
        self.assertEqual(found["origin"], "晚间候选里记下的；原是 Bridge 模板里的话")
        self.assertEqual(web_memory.origin_of({"note": "挪到全局"}, "", self.state)["origin"], "挪到全局")

    def test_origin_reads_the_topic_number_of_the_job_and_of_candidate_topics(self):
        write_json(self.state / "topics" / "t1" / "topic.json", {"topic_number": 586})
        write_json(self.state / "topics" / "t2" / "topic.json", {"topic_number": 488})
        found = web_memory.origin_of({"by": "memory_candidates", "topics": ["t1", "t2", "gone"]}, "", self.state)
        self.assertEqual((found["origin"], found["topic"]), ("晚间候选里记下的（话题 T586、T488）", "T586、T488"))

    def test_rows_carry_their_origin_quote_in_the_view(self):
        principles.save(self.state, principles.OWNER, "回复先给结论", source={"by": "memory_learn", "quote": "我说过很多次了，先给结论"})
        principles.save(self.state, principles.OWNER, "网页上记的一条", source={"via": "web"})
        rows = {r["text"]: r for r in self.group(web_memory.view(self.state))["entries"]}
        self.assertEqual((rows["回复先给结论"]["origin"], rows["回复先给结论"]["quote"]), ("自动学习记的", "我说过很多次了，先给结论"))
        self.assertEqual((rows["网页上记的一条"]["origin"], rows["网页上记的一条"]["quote"]), ("网页上记的", ""))



class OpTests(StateCase):
    def ops(self, owner=True):
        runtime = SimpleNamespace(owner_open_id="ou_owner", access=SimpleNamespace(is_owner=lambda open_id: owner))
        return SimpleNamespace(runtime=runtime)

    def test_the_operation_is_the_owners_only_needs_the_memory_on_and_an_older_pages_is_refused(self):
        payload = {"action": "add", "what": "principle", "scope": "global", "text": "先给结论"}  # v37's: no ``v``.
        with self.assertRaisesRegex(ValueError, "页面版本太旧"):
            Ops.memory_edit(self.ops(), self.state, "oc_chat", {}, payload)
        self.assertEqual(principles.entries(self.state), [])
        with self.assertRaisesRegex(ValueError, "只有主人"):
            Ops.memory_edit(self.ops(owner=False), self.state, "oc_chat", {}, payload)
        with mock.patch.dict(os.environ, {"COVEY_MEMORY": "0"}), self.assertRaisesRegex(ValueError, "记忆没开"):
            Ops.memory_edit(self.ops(), self.state, "oc_chat", {}, payload)
        self.assertFalse(hasattr(web_memory, "edit"))


class MemoryPushTest(WebCase):
    """The ``settings`` item ``memory`` in a request of its own: pushed when a file of the memory changed, ``{on: false}`` when off."""

    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.dict(os.environ, {"COVEY_MEMORY": "1", "COVEY_KNOWLEDGE": "0"}))

    def pushed(self):
        return [each for batch in self.web.upserts for each in batch["items"] if each["key"] == "memory"]

    def test_pushed_alone_when_the_memory_changed_and_off_says_so(self):
        principles.save(self.state, principles.OWNER, "先给结论")
        self.assertEqual(self.sync.push_memory(), 1)
        self.assertEqual([(each["type"], each["key"]) for batch in self.web.upserts for each in batch["items"]], [("settings", "memory")])
        self.assertEqual(self.sync.push_memory(), 0)  # Nothing changed.
        self.assertEqual(self.sync.push_memory(full=True), 0)  # The same rev is not sent again.
        principles.save(self.state, principles.OWNER, "数字要有出处")
        self.assertEqual(self.sync.push_memory(), 1)
        self.assertEqual({e["text"] for e in self.pushed()[-1]["data"]["groups"][0]["entries"]}, {"数字要有出处", "先给结论"})
        with mock.patch.dict(os.environ, {"COVEY_MEMORY": "0"}):
            self.assertEqual(self.sync.push_memory(), 1)  # The page had the tab: it is told the memory is off.
            self.assertEqual(self.sync.push_memory(full=True), 0)
        self.assertEqual(self.pushed()[-1]["data"], {"on": False})

    def test_never_on_never_pushed(self):
        with mock.patch.dict(os.environ, {"COVEY_MEMORY": "0"}):
            self.assertEqual(self.sync.push_memory(full=True), 0)
        self.assertEqual(self.pushed(), [])

    def test_an_older_pages_change_is_refused_and_nothing_is_written(self):
        self.sync.push_memory()
        self.web.op("memory_edit", 1, "op-m1", action="add", what="principle", scope="global", text="先给结论")
        with mock.patch.object(self.runtime, "access", SimpleNamespace(owner_chat=lambda: "oc_chat", is_owner=lambda open_id: True)):
            self.assertEqual(self.sync.inbox_once(), 1)
        self.assertEqual(principles.entries(self.state), [])
        self.assertEqual(self.web.acks[-1]["state"], "failed")
