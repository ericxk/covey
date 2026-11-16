"""topics_search (app/butler_search.py, docs/butler.md 按线索找话题): which topic and which turn a link, token, path
or keyword came from, on records written into a temporary state directory; topic_read given a turn (``job``)."""
from __future__ import annotations

import json
import os
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from support.records import write_job, state_dir, write_topic
from covey.app import butler_search, butler_tools
from covey.app.butler_env import ButlerEnv
from covey.core import butler_requests, butler
from covey.core.history_storage import append_history

CHAT = "oc_dm"
TOKEN = "HEJEwHDlgiFNKoklXtjcr3SBnPf"
LINK = f"https://example.feishu.cn/wiki/{TOKEN}?from=im"
ARTIFACT = "/absolute/data/run-1/closeread/REPORT.md"


class Fixture:
    def setUp(self):
        self.state = state_dir(self)
        self.logs = []
        self.runtime = SimpleNamespace(log=self.logs.append, shutdown=threading.Event(),
                                       access=SimpleNamespace(owner_chat=lambda: CHAT))
        self.env = ButlerEnv(self.runtime, SimpleNamespace(harnesses={}, default="claude", target=lambda *a: None),
                           None, None, state_dir=self.state, profile="unit")
        self.addCleanup(butler_search._checkpoints.clear)
        self.turns = 0

    def topic(self, topic_id, number, title, *turns, chat=CHAT, workspace="EnvReplay"):
        """A topic and its turns, each (owner's words, result, checkpoint or None, status); job ids go up across
        topics in the order written."""
        write_topic(self.state, topic_id=topic_id, chat_id=chat, topic_number=number, harness_id="claude",
                    workspace_name=workspace)
        append_history(self.state / "topics" / topic_id / "history.jsonl", "user", title)
        for user_text, final_text, checkpoint, status in turns:
            self.turns += 1
            fields = {"chat_id": chat, "topic_id": topic_id, "job_id": self.turns, "status": status,
                      "user_text": user_text, "final_text": final_text, "workspace_name": workspace}
            if checkpoint is not None:
                path = self.state / "checkpoints" / f"k{self.turns}.json"
                path.parent.mkdir(exist_ok=True)
                path.write_text(json.dumps(checkpoint, ensure_ascii=False), encoding="utf-8")
                fields["checkpoint_path"] = str(path)
            write_job(self.state, f"k{self.turns}", **fields)

    def call(self, name, **args):
        handlers = {**butler_tools.TOOLS, **butler_search.TOOLS}
        return handlers[name](self.env, butler_requests.Request("r1", name, args, "main-1", 0.0))


class SearchTest(Fixture, unittest.TestCase):
    def seed(self):
        # T1: the owner gives the doc and the turn draws in it; T2's later turn writes it and says so.
        self.topic("a1" * 16, 1, "画流程图", (f"在这个文档里画一个图 {LINK}", f"画好了：{LINK}", None, "done"))
        self.topic("b2" * 16, 2, "我们是一家小公司（前言）",
                   ("先看看代码", "看完了", None, "done"),
                   ("整理成清单", "清单在本地", {"phase": "写好清单", "artifacts": [ARTIFACT]}, "done"),
                   ("把流程写进文档", f"文档写好了：{LINK}", {"completed": [f"写入 wiki {TOKEN}"]}, "done"),
                   ("下一步？", f"流程文档（wiki {TOKEN[:8]}…）要不要链过来？", None, "done"))
        # The topic opened later for the same doc: the owner's words only.
        self.topic("c3" * 16, 3, "更新文档", (f"这是之前话题给的 {LINK}", "在读", None, "running"))
        # The butler's own turn and another chat's topic are not searched.
        write_job(self.state, "main-1", chat_id=CHAT, topic_id=butler.butler_topic_id(CHAT), job_id=99,
                  status="running", user_text=f"之前话题给的 {LINK}", final_text=f"没找到 {TOKEN}")
        self.topic("d4" * 16, 4, "别人的", ("x", f"也有 {TOKEN}", None, "done"), chat="oc_other")

    def test_a_link_is_found_by_its_token_in_every_turn_that_has_it(self):
        self.seed()
        found = self.call("topics_search", clues=[LINK])
        text = found["text"]
        self.assertTrue(found["ok"])
        self.assertEqual(text.splitlines()[0], f"线索 token {TOKEN}：4 轮")
        # T1's turn: the owner gave it and the result has it; T2's first with it: an earlier turn had it.
        self.assertRegex(text, r"#1 · [^\n]* · 结果里也有 · 结果：画好了：")
        self.assertRegex(text, r"#4 · [^\n]* · 结果里也有 · 结果：文档写好了：")
        # Written shortened: a partial match, in a later turn of a topic that had it.
        self.assertRegex(text, r"#5 · [^\n]* · 后续沿用（部分匹配） · 结果：流程文档（wiki HEJEwHDl…）")
        self.assertRegex(text, r"#6 · [^\n]* · 只是提到 · 主人的话：这是之前话题给的")
        self.assertNotIn("没找到", text)  # The butler's turn.
        self.assertNotIn("d4" * 16, text)
        # Ranked: #1 and #4 score alike, the older first; then T3's mention, then #5 (listed with T2).
        topics = [line.split(" · ")[0] for line in text.splitlines() if line.startswith("T")]
        self.assertEqual(topics, ["T1", "T2", "T3"])
        self.assertEqual(found["data"]["best"], {"topic": "T1", "job": 1, "topic_id": "a1" * 16})
        self.assertEqual([hit.record["job_id"] for hit in butler_search.search(self.env, CHAT, [butler_search.clue(LINK)])[0]],
                         [1, 4, 6, 5])
        self.assertEqual(found["data"]["total"], 4)
        self.assertIn("T2 · 我们是一家小公司（前言） · EnvReplay · " + "b2" * 16, text)

    def test_made_in_a_turn_is_its_first_appearance(self):
        self.seed()
        text = self.call("topics_search", clues=[ARTIFACT])["text"]
        self.assertEqual(text.splitlines()[0], f"线索 路径 {ARTIFACT}：1 轮")
        self.assertRegex(text, rf"#3 · [^\n]* · 第一次出现 · 检查点产物：{ARTIFACT}")
        # Not found whole: by its last two parts.
        partial = self.call("topics_search", clues=["/elsewhere/closeread/REPORT.md"])["text"]
        self.assertRegex(partial, r"#3 · [^\n]* · 第一次出现（部分匹配） · 检查点产物：")

    def test_keywords_and_several_clues(self):
        self.seed()
        found = self.call("topics_search", clues=["清单", "REPORT.md", "清单"])
        lines = found["text"].splitlines()
        self.assertEqual(lines[:2], ["[1] 线索 关键词 清单：1 轮", "[2] 线索 关键词 REPORT.md：1 轮"])
        # Both clues in #3, each marked; the owner asked for the 清单, so it is not its first appearance.
        self.assertEqual(lines[2].split(" · ")[0], "T2")
        self.assertRegex(found["text"], r"#3 · [^\n]* · \[1\] 结果里也有 · 结果：清单在本地；\[2\] 第一次出现 · 检查点产物：")
        self.assertIn("清单", self.call("topics_search", clues="清单")["text"])  # One clue as a string.

    def test_a_limit_says_how_many_were_left_out(self):
        self.seed()
        found = self.call("topics_search", clues=[TOKEN], limit=2)
        self.assertEqual(found["data"]["shown"], 2)
        self.assertIn("（共 4 轮命中，只列了最可能的 2 轮；加线索或换更具体的词缩小）", found["text"])

    def test_nothing_found_says_what_to_try(self):
        self.seed()
        found = self.call("topics_search", clues=["环境重建流程梳理"])
        self.assertTrue(found["ok"])
        self.assertEqual(found["data"]["total"], 0)
        self.assertIn("wiki 链接可先查出它对应的 docx token 再搜", found["text"])

    def test_bad_clues(self):
        for clues in ([], [""], None, "  "):
            with self.assertRaisesRegex(ValueError, "clues 要给 1 到 5 条线索"):
                self.call("topics_search", clues=clues)
        with self.assertRaisesRegex(ValueError, "太短"):
            self.call("topics_search", clues=["x"])
        with self.assertRaisesRegex(ValueError, "最多 5 条"):
            self.call("topics_search", clues=[f"词{i}" for i in range(6)])
        self.env.runtime.access.owner_chat = lambda: None
        self.assertFalse(self.call("topics_search", clues=["清单"])["ok"])

    def test_clues(self):
        self.assertEqual(butler_search.clue(f"<{LINK}>"), butler_search.Clue(f"{LINK}", "token", TOKEN))
        self.assertEqual(butler_search.clue("https://x.feishu.cn/docx/BpcvdCq1ZoPlftxMbw4cYfrhnGc").needle,
                         "BpcvdCq1ZoPlftxMbw4cYfrhnGc")
        self.assertEqual(butler_search.clue(TOKEN).kind, "token")
        self.assertEqual(butler_search.clue("abcdefghijklmnopqrst").kind, "keyword")  # No digit: a word.
        path = butler_search.clue("/a/b/c/REPORT.md/")
        self.assertEqual((path.kind, path.needle, path.tail), ("path", "/a/b/c/REPORT.md", "c/REPORT.md"))
        self.assertEqual(butler_search.clue("feat/golden").tail, "")
        self.assertEqual(butler_search.clue("Golden Localize"), butler_search.Clue("Golden Localize", "keyword",
                                                                               "golden localize"))

    def test_an_ended_turns_checkpoint_is_read_once(self):
        self.topic("a1" * 16, 1, "t", ("x", "y", {"phase": "旧的"}, "done"), ("x", "y", {"phase": "旧的"}, "running"))
        self.assertEqual(self.call("topics_search", clues=["旧的"])["data"]["total"], 2)
        for name in ("k1.json", "k2.json"):
            (self.state / "checkpoints" / name).write_text(json.dumps({"phase": "新的"}), encoding="utf-8")
        # The ended turn's is kept; the running one's is read again.
        self.assertRegex(self.call("topics_search", clues=["旧的"])["text"], r"#1 · ")
        self.assertRegex(self.call("topics_search", clues=["新的"])["text"], r"#2 · ")
        self.assertEqual(self.call("topics_search", clues=["新的"])["data"]["total"], 1)

    def test_the_bridges_own_checkpoint_fields_are_not_searched(self):
        self.topic("a1" * 16, 1, "t", ("x", "y", {"bridge_preflight": {"artifacts": [ARTIFACT]}}, "done"))
        self.assertEqual(self.call("topics_search", clues=[ARTIFACT])["data"]["total"], 0)

    def test_the_answers_a_checkpoint_keeps_are_the_owners_words(self):
        self.topic("a1" * 16, 1, "t", ("x", "y", {"saved_user_answers": {"answers": {"a": [LINK]}}}, "done"))
        self.assertRegex(self.call("topics_search", clues=[LINK])["text"], r"#1 · [^\n]* · 只是提到 · 检查点主人的回答：")


class WarmTest(Fixture, unittest.TestCase):
    def test_a_thread_unless_the_switch_is_0(self):
        search = butler_search.ButlerSearch(self.env)
        with mock.patch.dict(os.environ, {butler.ENV: "0"}):
            self.assertEqual(search.threads(), ())
        for value in ("1", ""):
            with mock.patch.dict(os.environ, {butler.ENV: value}):
                self.assertEqual([name for name, _ in search.threads()], ["main-search"])

    def test_in_the_mode_topics_it_waits_for_personal_agent_before_it_reads(self):
        self.topic("a1" * 16, 1, "t", ("x", "y", {"phase": "一"}, "done"))
        modes = iter((False, False, True))
        waited = []
        self.runtime.shutdown.wait = lambda *a: waited.append(a)
        with mock.patch.object(butler, "enabled", side_effect=lambda: next(modes)):
            butler_search.ButlerSearch(self.env).run(SimpleNamespace())
        self.assertEqual(waited, [(butler_search.MODE_POLL,), (butler_search.MODE_POLL,), ()])
        self.assertEqual(list(butler_search._checkpoints), [str(self.state / "checkpoints" / "k1.json")])

    def test_a_shutdown_while_it_waits_reads_nothing(self):
        self.runtime.shutdown.set()
        with mock.patch.object(butler, "enabled", return_value=False):
            butler_search.ButlerSearch(self.env).run(SimpleNamespace())
        self.assertEqual((list(butler_search._checkpoints), self.logs), ([], []))

    def test_it_reads_the_ended_turns_checkpoints_once_then_waits(self):
        self.topic("a1" * 16, 1, "t", ("x", "y", {"phase": "一"}, "done"), ("x", "y", {"phase": "二"}, "running"))
        waited = []
        self.runtime.shutdown.wait = lambda *a: waited.append(a)
        with mock.patch.object(butler, "enabled", return_value=True):
            butler_search.ButlerSearch(self.env).run(SimpleNamespace())
        self.assertEqual(list(butler_search._checkpoints), [str(self.state / "checkpoints" / "k1.json")])
        self.assertRegex(self.logs[-1], r"^main search warm checkpoints=1 ms=\d+$")
        self.assertEqual(waited, [()])

    def test_a_failure_is_logged_and_it_still_waits(self):
        waited = []
        self.runtime.shutdown.wait = lambda *a: waited.append(a)
        with mock.patch.object(butler_search, "turns_of", side_effect=OSError("disk")), \
                mock.patch.object(butler, "enabled", return_value=True):
            butler_search.ButlerSearch(self.env).run(SimpleNamespace())
        self.assertEqual(self.logs[-1], "main search warm failed: OSError: disk")
        self.assertEqual(waited, [()])


class TopicReadJobTest(Fixture, unittest.TestCase):
    def test_one_turn_by_its_number(self):
        self.topic("b2" * 16, 2, "t",
                   ("第一轮", "旧", None, "done"),
                   ("第二轮", "长" * 2000, {"artifacts": [f"/absolute/{i}.md" for i in range(7)], "phase": "写完"}, "done"),
                   ("第三轮", "新", None, "done"))
        text = self.call("topic_read", topic="T2", job="#2")["text"]
        self.assertIn("主人：第二轮", text)
        self.assertNotIn("第一轮", text)
        self.assertNotIn("第三轮", text)
        self.assertIn("长" * 2000, text)  # More of the result than the last turns get.
        self.assertIn("检查点产物（共 7 个）：/absolute/0.md；/absolute/1.md；/absolute/2.md；/absolute/3.md；/absolute/4.md\n",
                      text + "\n")
        both = self.call("topic_read", topic="T2", job="2", turns=2)["text"]
        self.assertIn("主人：第一轮", both)
        self.assertNotIn("第三轮", both)
        self.assertIn("长" * 500 + "…", self.call("topic_read", topic="T2", turns=2)["text"])
        with self.assertRaisesRegex(ValueError, "没有 #9 这一轮；它的轮次是 #1 到 #3"):
            self.call("topic_read", topic="T2", job="9")
        with self.assertRaisesRegex(ValueError, "job 要写轮次的编号"):
            self.call("topic_read", topic="T2", job="最新")


if __name__ == "__main__":
    unittest.main()
