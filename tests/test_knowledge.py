"""知识条目 (core/knowledge.py, docs/butler.md 知识库) and the history search's Bridge side
(app/butler_knowledge.py history_search, COVEY_HISTORY_CMD): the store (replay, same text once, rewrite and drop,
a bad file, a torn line, two processes writing), the block every new session reads and what a resumed one is told
(app/knowledge_turn.py, app/prompts.py), a topic's 记一下 collected by the Bridge (app/knowledge_collect.py: each of
the five conditions, once per turn, a failure harmless), the butler's tools and its MCP server's list, the
digest's part, and that with both variables unset every prompt, tool list, view and MCP config is what it was."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from support import REPO, exec_tempdir, make_context, quiet
from support.fake_backends import FakeBackend
from support.records import write_job, write_topic
from support.world import UnifiedCase, harness_app
from covey.core import env as cenv
from covey.app import digest, knowledge_collect, butler_knowledge, butler_tools
from covey.app.butler_env import ButlerEnv
from covey.app.prompts import (AREA_OPENING, area_rule, KNOWLEDGE_HEAD, KNOWLEDGE_NEW_HEAD, REMEMBER_RULE,
                                        SHARED_RULES, USAGE_RULE, constraints, history_line, resume_reminders,
                                        knowledge_block, butler_rules, workspace_block)
from covey.backends.claude.bridge import CLAUDE_SPEC
from covey.backends.claude.sessions import ClaudeSessionStore
from covey.cards.digest import digest_card, digest_text
from covey.core import knowledge, butler_mcp, butler_requests, butler, butler_tool_specs
from covey.core.attachments import ListedAttachment, attachment_block
from covey.core.checkpoint import CHECKPOINT_RULES, checkpoint_rules
from covey.core.jobs import BridgeReplyJob as ReplyJob
from covey.core.jobs import find_job_record, upsert_job_record
from covey.feishu.media import enrich_incoming_event
from covey.core.state import write_json

ON = {knowledge.ENV: "1"}
OWNER, OTHER, DM, GROUP = "ou_owner", "ou_colleague", "oc_dm", "oc_group"
SID = "11111111-1111-4111-8111-111111111111"


class StateCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)

    def env(self, **values):
        patcher = mock.patch.dict(os.environ, values)
        patcher.start()
        self.addCleanup(patcher.stop)


class StoreTest(StateCase):
    def test_a_saved_entry_reads_back_with_its_source_and_the_files_are_made(self):
        entry, new = knowledge.save(self.state, "owner", "decision", "  发版前\n先跑全量 ", why="上次漏了",
                                    source={"topic": "T483", "job_key": "k", "job_id": 3})
        self.assertTrue(new)
        self.assertRegex(entry["id"], knowledge.ID)
        self.assertEqual((entry["text"], entry["why"], entry["by"], entry["state"]),
                         ("发版前 先跑全量", "上次漏了", "main", "current"))
        self.assertRegex(entry["at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$")
        event = json.loads((self.state / "knowledge" / "owner.jsonl").read_text())
        self.assertEqual((event["v"], event["op"], event["id"], event["entry"]["kind"], event["source"]["topic"]),
                         (1, "add", entry["id"], "decision", "T483"))
        space, _ = knowledge.save(self.state, "workspace", "pitfall", "rg 会扫 NAS", workspace="bridge")
        self.assertEqual(space["workspace"], "bridge")
        owner, here = knowledge.for_turn(self.state, "bridge")
        self.assertEqual(([e["id"] for e in owner], [e["id"] for e in here]), ([entry["id"]], [space["id"]]))
        self.assertEqual(knowledge.for_turn(self.state, "other"), (owner, []))
        self.assertEqual(knowledge.for_turn(self.state, ""), (owner, []))
        md = (self.state / "knowledge" / "owner.md").read_text()
        self.assertIn("## 决定", md)
        self.assertIn(f"{entry['id']} · ", md)
        self.assertIn("（原因：上次漏了） · 来源 T483", md)
        self.assertTrue((self.state / "knowledge" / "workspaces" / "bridge.md").is_file())
        brief = (self.state / "knowledge" / "owner-brief.md").read_text()
        self.assertIn("- [决定] 发版前 先跑全量（原因：上次漏了）", brief)
        self.assertIn("- [坑] rg 会扫 NAS ·bridge", brief)

    def test_the_same_text_is_kept_once_in_a_group_but_not_across_groups(self):
        first, _ = knowledge.save(self.state, "owner", "fact", "Reply in Chinese。")
        again, new = knowledge.save(self.state, "owner", "practice", " reply  in chinese ")
        self.assertEqual((again["id"], new), (first["id"], False))
        other, new = knowledge.save(self.state, "workspace", "fact", "reply in chinese", workspace="w")
        self.assertTrue(new)
        self.assertEqual(len((self.state / "knowledge" / "owner.jsonl").read_text().splitlines()), 1)
        self.assertNotEqual(other["id"], first["id"])

    def test_bad_input_says_why_and_writes_nothing(self):
        for args, said in ((("everyone", "fact", "x"), "scope"), (("owner", "idea", "x"), "kind"),
                           (("owner", "fact", "  "), "空的"), (("owner", "fact", "字" * 201), "200 字"),
                           (("workspace", "fact", "x"), "哪个工作区")):
            with self.subTest(said), self.assertRaisesRegex(ValueError, said):
                knowledge.save(self.state, *args)
        with self.assertRaisesRegex(ValueError, "原因"):
            knowledge.save(self.state, "owner", "fact", "x", why="字" * 201)
        self.assertFalse((self.state / "knowledge").exists())

    def test_an_update_replaces_the_entry_and_the_old_one_is_history(self):
        old, _ = knowledge.save(self.state, "owner", "fact", "部署用 A", why="旧原因", source={"topic": "T1"})
        new = knowledge.update(self.state, old["id"], "部署用 B", reason="主人改了")
        self.assertEqual((new["replaces"], new["kind"], new["why"], new["reason"]), (old["id"], "fact", "旧原因", "主人改了"))
        self.assertNotEqual(new["id"], old["id"])
        self.assertEqual([e["id"] for e in knowledge.entries(self.state)], [new["id"]])
        past = knowledge.find(self.state, old["id"])
        self.assertEqual((past["state"], past["superseded_by"]), ("superseded", new["id"]))
        with self.assertRaisesRegex(ValueError, "已经已取代.*" + new["id"]):
            knowledge.update(self.state, old["id"], "C", reason="x")
        with self.assertRaisesRegex(ValueError, "reason|原因"):
            knowledge.update(self.state, new["id"], "C", reason=" ")
        with self.assertRaisesRegex(ValueError, "没有 id"):
            knowledge.update(self.state, "k000000", "C", reason="x")
        kinded = knowledge.update(self.state, new["id"], "部署用 C", kind="decision", why="", reason="r")
        self.assertEqual((kinded["kind"], kinded["why"]), ("decision", ""))
        other, _ = knowledge.save(self.state, "owner", "fact", "已有的")
        with self.assertRaisesRegex(ValueError, "已有相同的条目 " + other["id"]):
            knowledge.update(self.state, kinded["id"], "已有的", reason="r")

    def test_a_drop_needs_a_reason_and_keeps_the_record(self):
        entry, _ = knowledge.save(self.state, "workspace", "pitfall", "别用 rm -rf", workspace="w")
        with self.assertRaisesRegex(ValueError, "原因"):
            knowledge.drop(self.state, entry["id"], "")
        dropped = knowledge.drop(self.state, entry["id"], "过时了")
        self.assertEqual(dropped["id"], entry["id"])
        self.assertEqual(knowledge.entries(self.state), [])
        past = knowledge.find(self.state, entry["id"])
        self.assertEqual((past["state"], past["reason"]), ("dropped", "过时了"))
        with self.assertRaisesRegex(ValueError, "已经已作废"):
            knowledge.drop(self.state, entry["id"], "again")
        self.assertEqual(len((self.state / "knowledge" / "workspaces" / "w.jsonl").read_text().splitlines()), 2)
        self.assertIn("现在没有当前条目", (self.state / "knowledge" / "workspaces" / "w.md").read_text())

    def test_a_workspace_name_that_is_not_a_plain_file_name_gets_a_safe_one_and_keeps_its_name(self):
        entry, _ = knowledge.save(self.state, "workspace", "fact", "x", workspace="../a b/中文")
        files = list((self.state / "knowledge" / "workspaces").glob("*.jsonl"))
        self.assertEqual(len(files), 1)
        self.assertNotIn("/", files[0].name)
        self.assertRegex(files[0].name, r"-[0-9a-f]{6}\.jsonl$")
        self.assertEqual(knowledge.find(self.state, entry["id"])["workspace"], "../a b/中文")
        self.assertEqual(knowledge.for_turn(self.state, "../a b/中文")[1][0]["id"], entry["id"])
        self.assertEqual(knowledge.slug("bridge"), "bridge")
        self.assertNotEqual(knowledge.slug("a b"), knowledge.slug("a_b"))

    def test_lines_that_are_not_events_are_skipped_and_a_torn_last_line_does_not_swallow_the_next_event(self):
        first, _ = knowledge.save(self.state, "owner", "fact", "一")
        file = self.state / "knowledge" / "owner.jsonl"
        with open(file, "a", encoding="utf-8") as stream:
            stream.write('not json\n[1]\n{"op":"add","id":"bad","entry":{}}\n{"op":"add","id":"k99')  # No newline.
        second, _ = knowledge.save(self.state, "owner", "fact", "二")
        self.assertEqual([e["text"] for e in knowledge.entries(self.state)][::-1], ["一", "二"])
        self.assertTrue(all(e["id"] in (first["id"], second["id"]) for e in knowledge.entries(self.state)))

    def test_a_file_that_cannot_be_read_reads_as_empty_and_is_kept_aside_by_the_next_write(self):
        knowledge.save(self.state, "owner", "fact", "一")
        file = self.state / "knowledge" / "owner.jsonl"
        file.write_bytes(b"\xff\xfe\x00 not utf-8 \x80")
        self.assertEqual(knowledge.entries(self.state), [])
        entry, new = knowledge.save(self.state, "owner", "fact", "二", now=1700000000.5)
        self.assertTrue(new)
        self.assertEqual(file.with_name("owner.jsonl.bad-1700000000").read_bytes(), b"\xff\xfe\x00 not utf-8 \x80")
        self.assertEqual([e["id"] for e in knowledge.entries(self.state)], [entry["id"]])

    def test_two_processes_writing_at_once_lose_nothing(self):
        code = ("import sys; from covey.core import knowledge\n"
                "for i in range(15): knowledge.save(sys.argv[2], 'owner', 'fact', f'{sys.argv[1]}-{i}')\n")
        env = {**os.environ, "PYTHONPATH": str(REPO)}
        procs = [subprocess.Popen([sys.executable, "-c", code, tag, str(self.state)], env=env, cwd=str(REPO))
                 for tag in ("a", "b", "c")]
        for proc in procs:
            # 60 s ran out once with the machine's load near 440 (2026-11-16); an idle machine needs a few seconds.
            self.assertEqual(proc.wait(timeout=180), 0)
        texts = sorted(e["text"] for e in knowledge.entries(self.state))
        self.assertEqual(texts, sorted(f"{t}-{i}" for t in "abc" for i in range(15)))
        self.assertEqual(len((self.state / "knowledge" / "owner.jsonl").read_text().splitlines()), 45)

    def test_search_ranks_by_words_hit_and_history_adds_the_past(self):
        a, _ = knowledge.save(self.state, "owner", "decision", "部署前先跑全量测试", source={"topic": "T7"})
        b, _ = knowledge.save(self.state, "workspace", "pitfall", "全量测试要 20 分钟", workspace="w")
        c, _ = knowledge.save(self.state, "workspace", "fact", "别的工作区的事实", workspace="x")
        found = knowledge.search(self.state, "全量测试 部署")
        self.assertEqual([e["id"] for e in found], [a["id"], b["id"]])
        self.assertEqual([e["id"] for e in knowledge.search(self.state, "全量", workspace="w")], [b["id"], a["id"]])
        self.assertEqual([e["id"] for e in knowledge.search(self.state, "T7")], [a["id"]])
        self.assertEqual([e["id"] for e in knowledge.search(self.state, "坑")], [b["id"]])
        self.assertEqual(len(knowledge.search(self.state, "")), 3)
        self.assertEqual(knowledge.search(self.state, "没有这个词"), [])
        new = knowledge.update(self.state, a["id"], "部署前先跑全量测试和 lint", reason="补充")
        self.assertEqual([e["id"] for e in knowledge.search(self.state, "部署")], [new["id"]])
        past = knowledge.search(self.state, "部署", history=True)
        self.assertEqual({e["id"] for e in past}, {a["id"], new["id"]})
        self.assertIn("已取代（改为 " + new["id"] + "）", knowledge.row(knowledge.find(self.state, a["id"]), state=True))
        self.assertIn("工作区 x", knowledge.row(c))

    def test_remember_cuts_slips_writes_at_most_five_and_once_per_turn(self):
        items = [{"kind": "decision", "text": "一", "why": "因为", "scope": "owner"},
                 {"kind": "nonsense", "text": "二", "scope": "workspace"}, {"text": ""}, "not an object",
                 {"kind": "fact", "text": "三" * 300}, {"text": "四"}, {"text": "五"}, {"text": "六"}, {"text": "七"}]
        source = {"job_key": "j1", "job_id": 4, "topic": "T9"}
        written = knowledge.remember(self.state, items, job_key="j1", source=source, workspace="w")
        # Only the first five items are looked at: two of them are nothing, the rest are left out.
        self.assertEqual([e["text"][:1] for e in written], ["一", "二", "三"])
        self.assertEqual([(e["scope"], e["kind"], e["by"]) for e in written],
                         [("owner", "decision", "topic"), ("workspace", "fact", "topic"), ("workspace", "fact", "topic")])
        self.assertEqual(len(written[2]["text"]), knowledge.TEXT_LIMIT)
        self.assertTrue(knowledge.collected(self.state, "j1"))
        self.assertEqual(knowledge.remember(self.state, items, job_key="j1", source=source, workspace="w"), [])
        # Without a workspace (a turn outside one) everything is 通用.
        other = knowledge.remember(self.state, [{"text": "八", "scope": "workspace"}], job_key="j2", source={"job_key": "j2"},
                                   workspace="")
        self.assertEqual(other[0]["scope"], "owner")
        self.assertEqual(knowledge.remember(self.state, "not a list", job_key="j3", source={}, workspace="w"), [])

    def test_changes_and_what_is_new_since_a_time(self):
        a, _ = knowledge.save(self.state, "owner", "fact", "旧的")
        time.sleep(2.1)
        mid = time.time() - 1  # After a's second, before b's.
        b, _ = knowledge.save(self.state, "workspace", "fact", "新的", workspace="w")
        new = knowledge.update(self.state, a["id"], "旧的改了", reason="r")
        knowledge.drop(self.state, b["id"], "不要了")
        rows = knowledge.changes(self.state, mid, time.time() + 5)
        self.assertEqual(sorted((r["op"], r["text"]) for r in rows),
                         [("add", "新的"), ("drop", "新的"), ("update", "旧的改了")])
        self.assertEqual(len(knowledge.changes(self.state, 0, time.time() + 5)), 4)
        self.assertEqual(knowledge.changes(self.state, time.time() + 5, time.time() + 9), [])
        owner, space = knowledge.added_since(self.state, "w", mid)
        self.assertEqual(([e["id"] for e in owner], space), ([new["id"]], []))  # b was dropped.

    def test_the_switches(self):
        self.assertTrue(knowledge.enabled({}))
        self.assertTrue(knowledge.enabled({knowledge.ENV: " 1 "}))
        self.assertTrue(knowledge.enabled({knowledge.ENV: ""}))
        self.assertFalse(knowledge.enabled({knowledge.ENV: "0"}))
        self.assertFalse(knowledge.enabled({knowledge.ENV: "off"}))
        self.assertEqual(knowledge.history_cmd({knowledge.HISTORY_ENV: " /a/kb-history "}), "/a/kb-history")
        self.assertEqual(knowledge.history_cmd({knowledge.HISTORY_ENV: "kb-history"}), "")
        self.assertEqual(knowledge.history_cmd({}), "")
        self.assertEqual((butler_tool_specs.KNOWLEDGE_ENV, butler_tool_specs.HISTORY_ENV), (knowledge.ENV, knowledge.HISTORY_ENV))


def entry(text, at, **fields):
    return {"id": "k" + format(abs(hash(text)) % 0xFFFFFF, "06x"), "scope": "owner", "workspace": "", "kind": "fact",
            "text": text, "why": "", "at": at, "by": "main", "source": {}, "state": "current", **fields}


class BlockTest(StateCase):
    def test_the_block_lists_the_newest_first_within_the_limit_and_says_how_many_are_left(self):
        owner = [entry(f"通用第{i}条" + "字" * 50, f"2026-11-0{i + 1}T10:00:00+08:00") for i in range(5)]
        space = [entry("工作区的条目", "2026-11-09T10:00:00+08:00", scope="workspace", workspace="ws")]
        block = knowledge_block(owner, space, "ws", state_dir=self.state, limit=200)
        lines = block.splitlines()
        self.assertEqual(lines[0], KNOWLEDGE_HEAD)
        self.assertIn("工作区 ws：\n- [事实] 工作区的条目", block)
        self.assertLess(block.index("通用第4条"), block.index("通用第3条"))
        self.assertNotIn("通用第0条", block)
        self.assertRegex(lines[-1], r"^另有 \d 条较早的未列出，见 .*owner\.md。$")
        self.assertIn(str(self.state / "knowledge" / "owner.md"), lines[-1])
        self.assertNotIn("workspaces/ws.md", lines[-1])  # Nothing of the workspace's group was cut.

    def test_the_default_limit_is_1500_characters_of_lines(self):
        rows = [entry(f"{i:03d}" + "字" * 60, f"2026-10-{(i % 28) + 1:02d}T10:00:00+08:00") for i in range(60)]
        block = knowledge_block(rows, (), "ws")
        body = [line for line in block.splitlines() if line.startswith("- ")]
        self.assertLessEqual(sum(len(line) + 1 for line in body), knowledge.KNOWLEDGE_CHARS)
        self.assertGreater(len(body), 15)
        self.assertRegex(block.splitlines()[-1], r"^另有 \d+ 条较早的未列出。$")  # No state directory: no path.

    def test_new_entries_have_their_own_head_and_nothing_gives_an_empty_block(self):
        one = [entry("新的", "2026-11-09T10:00:00+08:00", why="因为")]
        self.assertEqual(knowledge_block(one, (), "ws", new=True),
                         KNOWLEDGE_NEW_HEAD + "\n通用：\n- [事实] 新的（原因：因为）")
        self.assertEqual(knowledge_block([], [], "ws"), "")
        self.assertEqual(knowledge_block([], [], "ws", history=""), "")

    def test_the_history_line_stands_alone_and_after_the_entries(self):
        cmd = "/absolute/kb-history"
        self.assertEqual(knowledge_block([], [], "ws", history=cmd), history_line(cmd))
        self.assertIn("`/absolute/kb-history search <一句话>`", history_line(cmd))
        self.assertIn("不要自己 rg 状态目录或会话目录", history_line(cmd))
        both = knowledge_block([entry("x", "2026-11-09T10:00:00+08:00")], [], "ws", history=cmd)
        self.assertTrue(both.endswith(history_line(cmd)))


class TurnTest(StateCase):
    """What a turn's rules and a resumed turn's reminder carry (app/runs.py), and that without the variables they are
    exactly what they were."""

    def setUp(self):
        super().setUp()
        self.root = self.state
        self.ctx = make_context(self.root)

    def app(self, backend=None):
        app = harness_app(CLAUDE_SPEC, backend or FakeBackend(), ClaudeSessionStore())
        quiet(app)
        app.runtime.state_dir, app.runtime.owner_open_id = self.ctx.state_dir, OWNER
        app.runtime.access.remember_owner_chat(DM)
        return app

    def job(self, chat=GROUP, sender=OWNER, topic="", job_id=5, **fields):
        return ReplyJob(job_id=job_id, user_text="帮我检查代码", chat_id=chat, message_id=f"om{job_id}", timeout=0,
                        sender_id=sender, topic_id=topic, **fields)

    def old_rules(self, runs, job):
        """bridge_rules as it was before: the pieces joined, no knowledge in them."""
        principles = runs.principles(self.ctx, job)
        return "\n\n".join((constraints(runs.harness.spec.prompt_agent_name, runs.harness.backend.prompt_runtime_note(self.ctx),
                                        tuple(getattr(runs.harness.backend, "prompt_notes", lambda ctx: ())(self.ctx))),
                            workspace_block(self.ctx.workspace_name, self.ctx.workdir),
                            *((principles,) if principles else ()), checkpoint_rules()))

    def save(self):
        knowledge.save(self.ctx.state_dir, "owner", "decision", "发版前先跑全量")
        knowledge.save(self.ctx.state_dir, "workspace", "pitfall", "ws 里别用 rg 扫 NAS", workspace="ws")
        knowledge.save(self.ctx.state_dir, "workspace", "fact", "别处的事实", workspace="other")

    def test_with_both_variables_unset_the_rules_and_the_resumed_reminder_are_word_for_word_what_they_were(self):
        self.env(**{knowledge.ENV: "0", knowledge.HISTORY_ENV: ""})
        self.save()  # Entries on disk change nothing while the switch is off.
        runs = self.app().runs
        job = self.job()
        self.assertEqual(runs.bridge_rules(self.ctx, job), self.old_rules(runs, job))
        self.assertEqual(runs.bridge_rules(self.ctx), self.old_rules(runs, None))
        rules = runs.bridge_rules(self.ctx, job)
        resumed = runs._with_rules(self.ctx, job, "MSG", rules, system=False, fresh=False, rules_session=False)
        # The resumed contract is all of it but the image rule (WAVE12 C9: one text every turn, so its version holds).
        self.assertEqual(resumed, "MSG\n\n" + resume_reminders() + "\n\n" + workspace_block(self.ctx.workspace_name, self.ctx.workdir)
                         + "\n\n" + checkpoint_rules(images=False))
        fresh = runs._with_rules(self.ctx, job, "MSG", rules, system=False, fresh=True, rules_session=False)
        self.assertEqual(fresh, rules + "\n\nMSG")
        self.assertEqual(butler_rules(), "\n".join(("[管家规则]", *AREA_OPENING, *SHARED_RULES, USAGE_RULE, area_rule())))
        self.assertEqual(butler_tool_specs.listed({"COVEY_KNOWLEDGE": "0", "COVEY_PROJECTS": "0", "COVEY_BUTLER_RULES": "0"}), butler_tool_specs.TOOLS)
        self.assertFalse(Path(self.ctx.state_dir, "knowledge", "x").exists())

    def test_a_new_session_reads_the_entries_before_the_checkpoint_rules_with_the_remember_rule_after_them(self):
        self.env(**ON)
        self.save()
        rules = self.app().runs.bridge_rules(self.ctx, self.job())
        self.assertEqual(rules.count(KNOWLEDGE_HEAD), 1)
        self.assertIn("通用：\n- [决定] 发版前先跑全量\n工作区 ws：\n- [坑] ws 里别用 rg 扫 NAS", rules)
        self.assertNotIn("别处的事实", rules)
        self.assertLess(rules.index(KNOWLEDGE_HEAD), rules.index(CHECKPOINT_RULES))
        self.assertTrue(rules.endswith(checkpoint_rules() + "\n\n" + REMEMBER_RULE))
        self.assertNotIn("/absolute/kb-history", rules)

    def test_without_entries_there_is_no_block_but_the_remember_rule_still_is(self):
        self.env(**ON)
        rules = self.app().runs.bridge_rules(self.ctx, self.job())
        self.assertNotIn(KNOWLEDGE_HEAD, rules)
        self.assertTrue(rules.endswith(REMEMBER_RULE))

    def test_the_owners_entries_and_the_history_command_only_on_the_owners_turn(self):
        self.env(**ON, **{knowledge.HISTORY_ENV: "/absolute/kb-history"})
        self.save()
        runs = self.app().runs
        for name, job, owner in (("owner in a group", self.job(), True), ("colleague", self.job(sender=OTHER), False),
                                 ("no sender in a group", self.job(sender=""), False),
                                 ("owner's chat", self.job(chat=DM, sender=""), True)):
            with self.subTest(name):
                rules = runs.bridge_rules(self.ctx, job)
                self.assertIn("ws 里别用 rg 扫 NAS", rules)  # The workspace's: whoever asks.
                self.assertEqual("发版前先跑全量" in rules, owner)
                self.assertEqual("/absolute/kb-history search" in rules, owner)
                self.assertEqual(REMEMBER_RULE in rules, owner)

    def test_the_history_command_is_there_without_any_entry_and_without_the_other_switch(self):
        self.env(**{knowledge.ENV: "0", knowledge.HISTORY_ENV: "/absolute/kb-history"})
        rules = self.app().runs.bridge_rules(self.ctx, self.job())
        self.assertIn(history_line("/absolute/kb-history"), rules)
        self.assertNotIn(REMEMBER_RULE, rules)

    def test_a_proxy_topic_and_the_butler_get_no_remember_rule_and_a_proxy_topic_nothing(self):
        self.env(**ON, **{knowledge.HISTORY_ENV: "/absolute/kb-history"})
        self.save()
        runs = self.app().runs
        main = self.job(chat=DM, sender="", topic=butler.butler_topic_id(DM))
        self.assertNotIn(REMEMBER_RULE, runs.bridge_rules(self.ctx, main))
        self.assertIn(KNOWLEDGE_HEAD, runs.bridge_rules(self.ctx, main))
        write_topic(self.ctx, proxy={"contact": "ou_x"})
        rules = runs.bridge_rules(self.ctx, self.job(chat=DM, sender=OWNER))
        self.assertNotIn(KNOWLEDGE_HEAD, rules)
        self.assertNotIn("kb-history", rules)
        self.assertNotIn(REMEMBER_RULE, rules)

    def test_a_resumed_turn_is_told_only_what_is_new_since_the_topics_previous_turn(self):
        self.env(**ON)
        runs = self.app().runs
        old, _ = knowledge.save(self.ctx.state_dir, "owner", "fact", "早就有的")
        ended = time.time() - 100
        write_job(self.ctx.state_dir, "om4", job_id=4, topic_id="t1", chat_id=GROUP, status="done",
                  timings={"execution_finished": ended})
        write_job(self.ctx.state_dir, "om3", job_id=3, topic_id="t2", chat_id=GROUP, status="done",
                  timings={"execution_finished": ended - 9999})  # Another topic's.
        # Written long before the previous turn ended.
        knowledge_path = Path(self.ctx.state_dir) / "knowledge" / "owner.jsonl"
        event = json.loads(knowledge_path.read_text())
        event["at"] = "2020-01-01T00:00:00+08:00"
        knowledge_path.write_text(json.dumps(event, ensure_ascii=False) + "\n")
        job = self.job(topic="t1", job_id=5)
        rules = runs.bridge_rules(self.ctx, job)
        text = lambda: runs._with_rules(self.ctx, job, "MSG", rules, system=False, fresh=False, rules_session=False)  # noqa: E731
        self.assertIn("早就有的", rules)  # A new session has everything.
        self.assertNotIn("早就有的", text())
        self.assertNotIn(KNOWLEDGE_NEW_HEAD, text())
        fresh, _ = knowledge.save(self.ctx.state_dir, "owner", "decision", "刚记的")
        collected, _ = knowledge.save(self.ctx.state_dir, "owner", "fact", "上一轮自己收集的",
                                      source={"job_key": "om4"}, by="topic")
        resumed = text()
        self.assertIn(KNOWLEDGE_NEW_HEAD + "\n通用：\n- [决定] 刚记的", resumed)
        self.assertNotIn("上一轮自己收集的", resumed)
        self.assertNotIn("早就有的", resumed)
        self.assertLess(resumed.index(KNOWLEDGE_NEW_HEAD), resumed.index(CHECKPOINT_RULES))
        self.assertTrue(resumed.endswith(REMEMBER_RULE))
        self.assertTrue(old["id"] and fresh["id"] and collected["id"])

    def test_a_resumed_turn_without_a_previous_turn_in_the_records_is_told_no_entries(self):
        self.env(**ON, **{knowledge.HISTORY_ENV: "/absolute/kb-history"})
        runs = self.app().runs
        knowledge.save(self.ctx.state_dir, "owner", "fact", "有的")
        job = self.job(topic="t9")
        text = runs._with_rules(self.ctx, job, "MSG", "RULES", system=False, fresh=False, rules_session=False)
        self.assertNotIn("有的", text)
        self.assertIn(history_line("/absolute/kb-history"), text)  # The command line is every turn's.

    def test_a_turn_the_butler_or_a_lead_handed_on_gets_no_remember_rule_and_the_owners_own_message_does(self):
        """app/knowledge_collect.py drops a hand-on's remember (the butler took the owner's 记一下 with its own tool), so its
        model is never told to write one and to tell the owner 已记下 (2026-11-22, T651). Its message id says it, not its
        prompt_text: the owner's own message has one too."""
        self.env(**ON)
        runs = self.app().runs
        handed = ReplyJob(job_id=6, user_text="这个坑记一下", chat_id=GROUP, message_id="main-tok1", timeout=0,
                          sender_id=OWNER, prompt_text="[Bridge 说明] 管家把这件事交给你")
        own = ReplyJob(job_id=7, user_text="这个坑记一下", chat_id=GROUP, message_id="om7", timeout=0, sender_id=OWNER,
                       prompt_text="这个坑记一下")
        for job, carried in ((handed, False), (own, True)):
            with self.subTest(job.message_id):
                self.assertEqual(REMEMBER_RULE in runs.bridge_rules(self.ctx, job), carried)
                resumed = runs._with_rules(self.ctx, job, "MSG", "RULES", system=False, fresh=False, rules_session=False)
                self.assertEqual(REMEMBER_RULE in resumed, carried)

    def test_the_session_that_has_the_rules_at_system_level_gets_no_reminder(self):
        self.env(**ON)
        runs = self.app().runs
        knowledge.save(self.ctx.state_dir, "owner", "fact", "有的")
        self.assertEqual(runs._with_rules(self.ctx, self.job(), "MSG", "RULES", system=True, fresh=False, rules_session=True),
                         "MSG")


class SpecsTest(StateCase):
    def test_the_tools_are_listed_only_where_the_variables_say_so(self):
        quiet = {"COVEY_PROJECTS": "0"}
        names = lambda env: [t["name"] for t in butler_tool_specs.listed({**quiet, **env})]  # noqa: E731
        base = [t["name"] for t in butler_tool_specs.TOOLS]
        # The knowledge tools gave way to the memory tools, in TOOLS whatever the switch says (docs/butler.md 记忆).
        self.assertEqual(names({knowledge.ENV: "0"}), base)
        self.assertEqual(names({}), base)
        self.assertNotIn("knowledge_save", [t["name"] for t in butler_tool_specs.listed({})])
        self.assertIn("memory_save", base)
        self.assertEqual(names({knowledge.ENV: "1"}), base)
        self.assertEqual(names({knowledge.ENV: "0", knowledge.HISTORY_ENV: "/a/kb-history"}), base + ["history_search"])
        self.assertEqual(names({knowledge.ENV: "0", knowledge.HISTORY_ENV: "kb-history"}), base)
        self.assertEqual(len(names({knowledge.ENV: "1", knowledge.HISTORY_ENV: "/a"})), len(base) + 1)
        for tool in butler_tool_specs.MEMORY_TOOLS + butler_tool_specs.HISTORY_TOOLS:
            self.assertEqual(set(tool), {"name", "description", "input_schema"})
            for required in tool["input_schema"].get("required", ()):
                self.assertIn(required, tool["input_schema"]["properties"], tool["name"])
        self.assertEqual(set(butler_tool_specs.ALL_NAMES) - set(butler_tool_specs.NAMES), {"history_search"})
        self.assertLessEqual(set(butler_knowledge.TOOLS) - {"history_search"},  # An older session's names, run as the new.
                             set(butler_tool_specs.RENAMED_TOOLS))

    def test_the_mcp_server_lists_and_accepts_what_its_environment_says(self):
        seen = []
        submit = lambda state, name, args, job_key: seen.append(name) or {"ok": True, "text": "ok", "data": {}}  # noqa: E731
        quiet = {"COVEY_PROJECTS": "0"}
        plain = butler_mcp.Server({butler.STATE_ENV: str(self.state), butler.JOB_ENV: "j", knowledge.ENV: "0", **quiet},
                                  submit=submit)
        on = butler_mcp.Server({butler.STATE_ENV: str(self.state), butler.JOB_ENV: "j", **quiet, **ON,
                              knowledge.HISTORY_ENV: "/absolute/kb-history"}, submit=submit)
        self.assertEqual(len(butler_mcp.tool_list(plain.environ)), len(butler_tool_specs.TOOLS))
        self.assertEqual(len(butler_mcp.tool_list(on.environ)), len(butler_tool_specs.TOOLS) + 1)
        renamed = plain.call({"name": "knowledge_save", "arguments": {}})  # An older session's: run as memory_save.
        self.assertFalse(renamed["isError"])
        self.assertEqual(seen.pop(), "memory_save")
        self.assertEqual(plain.call({"name": "rm_rf", "arguments": {}})["content"][0]["text"], "没有这个工具：rm_rf")
        self.assertFalse(on.call({"name": "history_search", "arguments": {"query": "x"}})["isError"])
        self.assertEqual(seen, ["history_search"])

    def test_the_mcp_config_hands_the_variables_on_only_when_the_bridge_has_them(self):
        mine = {butler.STATE_ENV, butler.JOB_ENV, knowledge.ENV, knowledge.HISTORY_ENV}
        mine |= {name for key in mine for name in cenv.spellings(key)}
        handed = lambda: {k: v for k, v in butler.mcp_config(Path("/r"), Path("/s"), "job-1", "/usr/bin/python3")  # noqa: E731
                          ["mcpServers"]["bridge"]["env"].items() if k in mine}
        self.env(**{knowledge.ENV: "0", knowledge.HISTORY_ENV: ""})
        self.assertEqual(handed(), cenv.mirrored({butler.STATE_ENV: "/s", butler.JOB_ENV: "job-1", knowledge.ENV: "0"}))
        self.env(**ON, **{knowledge.HISTORY_ENV: " /absolute/kb-history "})
        self.assertEqual(handed(), cenv.mirrored({butler.STATE_ENV: "/s", butler.JOB_ENV: "job-1", knowledge.ENV: "1",
                                                  knowledge.HISTORY_ENV: "/absolute/kb-history"}))

    def test_the_butlers_rules_name_the_order_of_looking_things_up(self):
        self.env(**{knowledge.ENV: "0", knowledge.HISTORY_ENV: ""})
        self.assertNotIn("“记一下……”", butler_rules())
        self.assertNotIn("history_search", butler_rules())
        self.env(**ON)
        rules = butler_rules()
        # The memory tools the butler is listed (T870 Wave 2B): memory_*, never the older names it cannot call.
        for words in ("memory_save", "“记一下……”", "topic_read", "列出短码和正文", "memory_revise", "action retire",
                      "memory_search → topics_search，"):
            self.assertIn(words, rules)
        for words in ("knowledge_save", "knowledge_update", "knowledge_drop", "knowledge_search", "principle_save", "principles_list"):
            self.assertNotIn(words, rules)
        self.assertNotIn("history_search", rules)
        self.env(**{knowledge.HISTORY_ENV: "/absolute/kb-history"})
        self.assertIn("memory_search → topics_search → history_search", butler_rules())
        self.env(**{knowledge.ENV: "0"})
        self.assertIn("topics_search → history_search", butler_rules())
        self.assertNotIn("“记一下……”", butler_rules())


class ToolTest(StateCase):
    CHAT = "oc_dm"

    def setUp(self):
        super().setUp()
        (self.state / "workspace-config.json").write_text(json.dumps({"workspaces": {"bridge": "/x", "kb": "/y"}}))
        write_job(self.state, "main-1", chat_id=self.CHAT, topic_id=butler.butler_topic_id(self.CHAT), job_id=12,
                  status="running")
        runtime = type("R", (), {"log": staticmethod(lambda line: None),
                                 "access": type("A", (), {"owner_chat": staticmethod(lambda: self.CHAT)})()})()
        self.env_ = ButlerEnv(runtime, None, None, None, state_dir=self.state, profile="unit")

    def call(self, name, **args):
        return butler_knowledge.TOOLS[name](self.env_, butler_requests.Request("r1", name, args, "main-1", time.time()))

    def test_every_tool_says_so_when_the_switch_is_off(self):
        self.env(**{knowledge.ENV: "0"})
        for name, args in (("knowledge_save", {"scope": "owner", "kind": "fact", "text": "x"}),
                           ("knowledge_update", {"id": "k000000", "text": "x", "reason": "r"}),
                           ("knowledge_drop", {"id": "k000000", "reason": "r"}), ("knowledge_search", {"query": "x"})):
            answer = self.call(name, **args)
            self.assertFalse(answer["ok"], name)
            self.assertIn("COVEY_KNOWLEDGE", answer["text"])
        self.assertFalse((self.state / "knowledge").exists())

    def test_save_search_update_drop(self):
        self.env(**ON)
        saved = self.call("knowledge_save", scope="owner", kind="decision", text="发版前先跑全量", why="漏过一次",
                          source_topic="483")
        self.assertTrue(saved["ok"], saved)
        kid = saved["data"]["id"]
        self.assertTrue(saved["data"]["new"])
        self.assertIn(f"已记下（{kid} · 通用 · 决定 · 20", saved["text"])
        self.assertIn("发版前先跑全量（原因：漏过一次） · 来源 T483", saved["text"])
        event = json.loads((self.state / "knowledge" / "owner.jsonl").read_text())
        self.assertEqual(event["source"], {"job_key": "main-1", "job_id": 12, "topic": "T483"})
        self.assertEqual(event["by"], "main")
        again = self.call("knowledge_save", scope="owner", kind="fact", text="发版前先跑全量。")
        self.assertEqual((again["ok"], again["data"]), (True, {"id": kid, "new": False}))
        self.assertIn("没有重复记", again["text"])
        space = self.call("knowledge_save", scope="workspace", workspace="bridge", kind="pitfall", text="rg 扫 NAS 会爆内存")
        self.assertIn("工作区 bridge", space["text"])
        found = self.call("knowledge_search", query="全量 NAS")
        self.assertEqual(found["data"]["count"], 2)
        self.assertEqual(self.call("knowledge_search", query="NAS", workspace="kb")["data"]["count"], 0)
        self.assertEqual(self.call("knowledge_search", query="NAS", workspace="bridge")["data"]["count"], 1)
        updated = self.call("knowledge_update", id=kid, text="发版前先跑全量和 lint", reason="主人补充")
        self.assertTrue(updated["ok"], updated)
        self.assertEqual(updated["data"]["replaces"], kid)
        self.assertIn("发版前先跑全量和 lint", updated["text"])
        past = self.call("knowledge_search", query="全量", history=True)
        self.assertEqual(past["data"]["count"], 2)
        self.assertIn("已取代（改为 " + updated["data"]["id"] + "）", past["text"])
        nothing = self.call("knowledge_search", query="完全不相关")
        self.assertIn("history: true", nothing["text"])
        dropped = self.call("knowledge_drop", id=updated["data"]["id"], reason="不用了")
        self.assertTrue(dropped["ok"])
        self.assertIn("已作废：" + updated["data"]["id"], dropped["text"])
        self.assertEqual(self.call("knowledge_search", query="全量")["data"]["count"], 0)

    def test_bad_arguments_are_refused_in_words(self):
        self.env(**ON)
        for args, said in (({"scope": "workspace", "workspace": "nope", "kind": "fact", "text": "x"}, "可选：bridge、kb"),
                           ({"scope": "workspace", "kind": "fact", "text": "x"}, "哪个工作区"),
                           ({"scope": "owner", "kind": "idea", "text": "x"}, "kind"),
                           ({"scope": "owner", "kind": "fact", "text": "字" * 201}, "200 字"),
                           ({"scope": "owner", "kind": "fact", "text": "x", "source_topic": "ABC"}, "T 号")):
            with self.subTest(said), self.assertRaisesRegex(ValueError, said):
                self.call("knowledge_save", **args)
        with self.assertRaisesRegex(ValueError, "没有 id"):
            self.call("knowledge_drop", id="k123456", reason="r")
        with self.assertRaisesRegex(ValueError, "没有工作区"):
            self.call("knowledge_search", query="x", workspace="nope")
        self.assertFalse((self.state / "knowledge").exists())

    def test_the_loop_answers_a_refusal_in_words(self):
        self.env(**ON)
        answer = butler_tools.ButlerTools(self.env_, butler_knowledge.TOOLS).execute(
            butler_requests.Request("r2", "knowledge_drop", {"id": "k123456", "reason": "r"}, "main-1", time.time()))
        self.assertFalse(answer["ok"])
        self.assertEqual(answer["text"], "knowledge_drop 没有完成：没有 id 为 k123456 的条目：用 knowledge_search 找")


class HistoryTest(StateCase):
    """history_search: the command run in its own process group, bounded in time and size, every failure one sentence."""

    def setUp(self):
        super().setUp()
        tmp = exec_tempdir()
        self.addCleanup(tmp.cleanup)
        self.bin = Path(tmp.name)
        self.env_ = ButlerEnv(mock.Mock(), None, None, None, state_dir=self.state, profile="unit")

    def script(self, body, name="kb-history"):
        path = self.bin / name
        path.write_text("#!/bin/bash\n" + body)
        path.chmod(0o755)
        self.env(**{knowledge.HISTORY_ENV: str(path)})
        return path

    def call(self, **args):
        return butler_knowledge.history_search(self.env_, butler_requests.Request("r1", "history_search", args, "main-1", time.time()))

    def test_it_runs_the_command_with_the_query_after_double_dash_and_returns_its_output(self):
        self.script('printf "%s|" "$@"')
        answer = self.call(query="  上次  怎么部署的 ", workspace="bridge", since="30d")
        self.assertEqual((answer["ok"], answer["data"]["truncated"]), (True, False))
        self.assertEqual(answer["text"], "search|--workspace|bridge|--since|30d|--|上次 怎么部署的|")
        self.assertEqual(self.call(query="-x --json")["text"], "search|--|-x --json|")

    def test_an_empty_output_and_bad_arguments(self):
        self.script("exit 0")
        self.assertEqual(self.call(query="x")["text"], "没有找到相关的历史轮次。")
        with self.assertRaisesRegex(ValueError, "搜索词"):
            self.call(query=" ")
        with self.assertRaisesRegex(ValueError, "30d"):
            self.call(query="x", since="一个月")

    def test_without_the_variable_or_with_a_command_that_is_missing_or_not_executable(self):
        self.env(**{knowledge.HISTORY_ENV: ""})
        self.assertIn("没有配置", self.call(query="x")["text"])
        self.env(**{knowledge.HISTORY_ENV: str(self.bin / "nope")})
        answer = self.call(query="x")
        self.assertFalse(answer["ok"])
        self.assertIn("命令不存在", answer["text"])
        plain = self.bin / "plain"
        plain.write_text("#!/bin/sh\necho hi\n")
        plain.chmod(0o644)
        self.env(**{knowledge.HISTORY_ENV: str(plain)})
        self.assertIn("不能执行", self.call(query="x")["text"])

    def test_a_failing_command_says_its_exit_code_and_what_it_printed(self):
        self.script('echo "索引不存在"; exit 3')
        answer = self.call(query="x")
        self.assertFalse(answer["ok"])
        self.assertIn("退出码 3", answer["text"])
        self.assertIn("索引不存在", answer["text"])

    def test_a_command_over_the_time_is_stopped_with_its_whole_group(self):
        pidfile = self.bin / "child.pid"
        self.script(f'sleep 60 &\necho $! > {pidfile}\nwait\n')
        with mock.patch.object(butler_knowledge, "HISTORY_TIMEOUT", 1):
            started = time.monotonic()
            answer = self.call(query="x")
        self.assertLess(time.monotonic() - started, 20)
        self.assertFalse(answer["ok"])
        self.assertIn("超过 1 秒没有结果，已经停掉", answer["text"])
        child = int(pidfile.read_text())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            self.fail("the command's child outlived the group stop")

    def test_output_over_the_limit_is_cut_and_the_command_stopped(self):
        self.script('head -c 100000 /dev/zero | tr "\\0" "a"; sleep 30\n')
        started = time.monotonic()
        answer = self.call(query="x")
        self.assertLess(time.monotonic() - started, 20)
        self.assertEqual((answer["ok"], answer["data"]["truncated"]), (True, True))
        self.assertIn("已截断", answer["text"])
        body = answer["text"].split("\n\n（输出超过")[0]
        self.assertEqual(len(body), butler_knowledge.HISTORY_BYTES)


class CollectTest(StateCase):
    """app/knowledge_collect.py: a topic's remember written once, by the Bridge, under five conditions."""

    REMEMBER = [{"kind": "pitfall", "text": "rg 会扫 NAS", "why": "爆过内存", "scope": "workspace"},
                {"kind": "decision", "text": "发版前先跑全量", "scope": "owner"}]

    def setUp(self):
        super().setUp()
        self.env(**ON)
        self.logs = []
        owner_turn = lambda chat, sender: sender == OWNER or (not sender and chat == DM)  # noqa: E731
        self.runtime = mock.Mock(log=self.logs.append, access=mock.Mock(owner_turn=owner_turn))
        self.ctx = make_context(self.state)
        self.state = self.ctx.state_dir
        write_topic(self.state, topic_id="t1", topic_number=483)
        self.ctx.topic_id = "t1"

    def turn(self, *, remember=None, status="done", user_text="这个坑记一下", sender=OWNER, prompt_text="", chat=GROUP,
             topic="t1", key=None, job_id=None, checkpoint=True, **record):
        # A job record never goes back from a final status, so each turn is a job of its own.
        self.count = getattr(self, "count", 4) + 1
        key, job_id = key or f"om{self.count}", job_id or self.count
        path = self.state / f"cp-{key}.json"
        if checkpoint:
            write_json(path, {"phase": "完成", **({"remember": self.REMEMBER if remember is None else remember})})
        write_job(self.state, key, job_id=job_id, topic_id=topic, chat_id=chat, status=status, user_text=user_text,
                  workspace_name="bridge", checkpoint_path=str(path) if checkpoint else "", prompt_text=prompt_text,
                  **({"sender_id": sender} if sender else {}), **record)
        return ReplyJob(job_id=job_id, user_text=user_text, chat_id=chat, message_id=key, timeout=0, sender_id=sender,
                        topic_id=topic, prompt_text=prompt_text or None)

    def collect(self, job):
        knowledge_collect.collect(self.runtime, job, self.ctx)

    def written(self):
        return knowledge.entries(self.state)

    def test_a_finished_owner_turn_that_asked_for_it_writes_its_entries_with_the_turn_as_source(self):
        self.collect(self.turn())
        rows = {e["text"]: e for e in self.written()}
        self.assertEqual(set(rows), {"rg 会扫 NAS", "发版前先跑全量"})
        space, owner = rows["rg 会扫 NAS"], rows["发版前先跑全量"]
        self.assertEqual((space["scope"], space["workspace"], space["kind"], space["why"], space["by"]),
                         ("workspace", "bridge", "pitfall", "爆过内存", "topic"))
        self.assertEqual(owner["scope"], "owner")
        self.assertEqual(space["source"], {"job_key": "om5", "job_id": 5, "topic": "T483"})
        self.assertIn("knowledge remember written=2 job=#5", self.logs)

    def test_with_the_memory_on_a_remember_is_dropped_the_memorys_one_rule_takes_it(self):
        # T908 M3a: with COVEY_MEMORY=1 the remember rule is never told (app/knowledge_turn.py remembers); one a session still
        # writes from older rules carries no quote of his words to prove it by, and is dropped.
        from covey.core import memstore
        self.env(COVEY_MEMORY="1")
        job = self.turn(remember=[*self.REMEMBER, {"kind": "fact", "text": "目前是第 3 代", "scope": "owner"}])
        self.collect(job)
        self.assertEqual(memstore.entries(self.state), [])
        self.assertEqual(self.written(), [])  # Nor the old store.
        self.assertNotIn("knowledge_remembered", find_job_record(self.state, job.message_id))
        self.assertEqual(self.logs, [f"knowledge remember ignored reason=memory job=#{job.job_id}"])

    def test_a_turn_waiting_for_the_owners_answer_counts_as_ended_normally(self):
        self.collect(self.turn(status="waiting_user"))
        self.assertEqual(len(self.written()), 2)

    def test_each_condition_that_fails_writes_nothing_and_says_why(self):
        cases = (("not-finished", dict(status="failed")), ("not-finished", dict(status="interrupted")),
                 ("not-owner", dict(sender=OTHER)), ("not-owner", dict(sender="", chat=GROUP)),
                 ("no-trigger", dict(user_text="帮我看看这段代码")), ("handed-on", dict(key="main-tok9", prompt_text="[Bridge 说明] 管家把这件事交给你")),
                 ("main-thread", dict(chat=DM, sender="", topic=butler.butler_topic_id(DM))))
        for reason, fields in cases:
            with self.subTest(reason, **{k: str(v)[:12] for k, v in fields.items()}):
                self.logs.clear()
                job = self.turn(**fields)
                self.collect(job)
                self.assertEqual(self.written(), [])
                self.assertEqual(self.logs, [f"knowledge remember ignored reason={reason} job=#{job.job_id}"])
        self.assertFalse((self.state / "knowledge").exists())

    def test_the_owners_own_turn_is_collected_whatever_its_prompt_text_holds(self):
        """Every message the owner writes is queued with its expanded input as prompt_text (app/app.py handle_event), and so
        are an answer, a page's words and a supplement queued again: none of them is a hand-on. Read as one, every topic's
        记一下 was dropped while its model told the owner 已记下 (2026-11-22, T651)."""
        listed = "\n".join(attachment_block([ListedAttachment("image", "a.png", "/absolute/incoming_media/a.png")]))
        cases = {"as written": dict(prompt_text="这个坑记一下"),
                 "a group's sender line": dict(prompt_text="发送者：主人\n\n这个坑记一下"),
                 "attachments": dict(prompt_text="这个坑记一下\n\n" + listed),
                 "merged before it started": dict(prompt_text="这个坑记一下\n\n用户在任务开始前补充：\n还有这个"),
                 "the web page's words": dict(key="web-op1", prompt_text="这个坑记一下"),
                 "a supplement queued again": dict(key="requeue-" + "a" * 24, prompt_text="这个坑记一下")}
        for n, (name, fields) in enumerate(cases.items()):
            with self.subTest(name):
                self.logs.clear()
                job = self.turn(remember=[{"kind": "pitfall", "text": f"坑 {n}", "scope": "workspace"}], **fields)
                self.collect(job)
                self.assertEqual(self.logs, [f"knowledge remember written=1 job=#{job.job_id}"])
        self.assertEqual(sorted(e["text"] for e in self.written()), [f"坑 {n}" for n in range(len(cases))])

    def test_a_proxy_topic_writes_nothing(self):
        for topic_data in ({"proxy": {"contact": "ou_x"}},):
            write_topic(self.state, topic_id="t1", topic_number=483, **topic_data)
            self.ctx.history_path = self.state / "topics" / "t1" / "history.jsonl"
            with self.subTest(topic_data):
                self.collect(self.turn())
                self.assertEqual(self.written(), [])

    def test_the_same_turn_collected_twice_writes_once(self):
        job = self.turn()
        self.collect(job)
        self.collect(job)
        self.assertEqual(len(self.written()), 2)
        self.assertEqual(len((self.state / "knowledge" / "owner.jsonl").read_text().splitlines()), 1)

    def test_nothing_to_remember_is_silent(self):
        for fields in (dict(remember=[]), dict(checkpoint=False), dict(remember="not a list")):
            self.collect(self.turn(**fields))
        self.assertEqual((self.written(), self.logs), ([], []))
        job = self.turn(user_text="没有触发词")
        self.collect(job)
        self.assertEqual(self.logs, [f"knowledge remember ignored reason=no-trigger job=#{job.job_id}"])

    def test_off_it_does_not_even_read_the_checkpoint(self):
        self.env(**{knowledge.ENV: "0"})
        with mock.patch.object(knowledge_collect, "read_checkpoint", side_effect=AssertionError("read")):
            self.collect(self.turn())
        self.assertEqual((self.written(), self.logs), ([], []))

    def test_a_failure_to_write_is_a_log_line_not_an_error(self):
        job = self.turn()
        with mock.patch.object(knowledge.os, "open", side_effect=OSError("NAS gone")):
            self.collect(job)
        self.assertEqual(self.written(), [])
        self.assertEqual(self.logs, ["knowledge remember failed job=#5: OSError"])
        with mock.patch.object(knowledge_collect, "read_checkpoint", side_effect=ValueError("bad")):
            self.collect(job)
        self.assertEqual(self.logs[-1], "knowledge remember failed job=#5: ValueError")
        self.assertEqual(find_job_record(self.state, "om5")["status"], "done")  # The record is as the turn left it.
        self.collect(job)  # And a later try writes.
        self.assertEqual(len(self.written()), 2)


class OwnerMessageTest(UnifiedCase):
    """The regression of 2026-11-22 (T651) end to end: the owner's message through handle_event, its job record as the
    Bridge writes it (prompt_text set, as for every message), its 记一下 collected; a hand-on into the same topic is not."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, ON)
        patcher.start()
        self.addCleanup(patcher.stop)

    def finished(self, key, text) -> ReplyJob:
        """Job ``key`` ended with ``text`` in its checkpoint's remember; the job as its record says."""
        state = self.ctx.state_dir
        path = state / f"cp-{key}.json"
        write_json(path, {"phase": "完成", "remember": [{"kind": "pitfall", "text": text, "scope": "owner"}]})
        for status in ("queued", "running", "done"):
            record = upsert_job_record(state, key, status=status, checkpoint_path=str(path))
        return ReplyJob(job_id=record["job_id"], user_text=record["user_text"], chat_id="dm", message_id=key, timeout=0,
                        sender_id=record.get("sender_id") or "", topic_id=record["topic_id"],
                        prompt_text=record.get("prompt_text") or None)

    def test_the_owners_message_in_a_topic_is_collected_and_a_hand_on_is_not(self):
        real = enrich_incoming_event
        with mock.patch("covey.app.app.enrich_incoming_event",
                        side_effect=lambda *a, **k: real(*a, **{**k, "fetch_message_details": False})):
            self.app.handle_event({"chat_id": "dm", "chat_type": "p2p", "sender_type": "user", "sender_id": "owner",
                                   "message_id": "d1", "content": "这个坑记一下：rg 会扫 NAS"}, self.ctx)
        self.assertEqual(find_job_record(self.ctx.state_dir, "d1")["prompt_text"], "这个坑记一下：rg 会扫 NAS")
        app, topic = self.app.topics.route(self.ctx, {"chat_id": "dm", "message_id": "d1"})
        knowledge_collect.collect(self.app.runtime, self.finished("d1", "rg 会扫 NAS"), topic)
        self.assertEqual([e["text"] for e in knowledge.entries(self.ctx.state_dir)], ["rg 会扫 NAS"])
        # The butler hands the owner's words on into the same topic ("main-" + its request's token).
        app.submission.enqueue_chat_job(topic, "这个也记一下", "dm", "main-tok1", prompt_content="[Bridge 说明] 管家转来",
                                        sender_id="owner")
        job = self.finished("main-tok1", "管家已经记过的")
        knowledge_collect.collect(self.app.runtime, job, topic)
        self.assertIn(f"knowledge remember ignored reason=handed-on job=#{job.job_id}", self.logs)
        self.assertEqual([e["text"] for e in knowledge.entries(self.ctx.state_dir)], ["rg 会扫 NAS"])


class DigestTest(StateCase):
    def test_the_part_lists_new_rewritten_and_dropped_entries_and_is_left_out_when_off_or_empty(self):
        a, _ = knowledge.save(self.state, "owner", "fact", "旧的")
        now = time.time()
        self.env(**{knowledge.ENV: "0"})
        self.assertEqual(digest.knowledge_view(self.state, now - 60, now + 60), {})
        self.env(**ON)
        view = digest.knowledge_view(self.state, now - 60, now + 60)
        self.assertEqual(view["more"], 0)
        self.assertEqual(view["rows"], [{"op": "新增", "id": a["id"], "where": "通用", "text": "旧的"}])
        self.assertEqual(digest.knowledge_view(self.state, now + 60, now + 120), {})
        b = knowledge.update(self.state, a["id"], "新的" * 30, reason="r")
        knowledge.drop(self.state, b["id"], "r")
        rows = digest.knowledge_view(self.state, now - 60, now + 60)["rows"]
        self.assertEqual(sorted(r["op"] for r in rows), ["作废", "改写", "新增"])
        self.assertTrue(all(len(r["text"]) <= digest.KNOWLEDGE_TEXT + 1 for r in rows))

    def test_ten_rows_and_one_line_for_the_rest(self):
        self.env(**ON)
        for i in range(13):
            knowledge.save(self.state, "workspace", "fact", f"条目{i}", workspace="w")
        view = digest.knowledge_view(self.state, 0, time.time() + 60)
        self.assertEqual((len(view["rows"]), view["more"]), (10, 3))
        shown = {"slot": 1.0, "since": 0.0, "waiting": [], "stale": [], "running": [], "done": [], "older": 0,
                 "knowledge": view}
        card = json.dumps(digest_card(shown), ensure_ascii=False)
        self.assertIn("**新记的知识**  <font color='grey'>13</font>", card)  # A part's heading as the 看板 draws one (T657 B1).
        self.assertIn("新增 · ", card)
        self.assertIn("另有 3 条", card)
        text = digest_text(shown)
        self.assertIn("**新记的知识（13）**", text)
        self.assertIn("另有 3 条", text)

    def test_a_view_without_the_part_draws_as_it_did(self):
        view = {"slot": 1.0, "since": 0.0, "waiting": [], "stale": [], "running": [], "done": [], "older": 0}
        self.assertNotIn("新记的知识", json.dumps(digest_card(view), ensure_ascii=False))
        self.assertNotIn("新记的知识", digest_text(view))
        self.assertIn("08:00 时没有要你处理的", digest_text({**view, "knowledge": {"rows": [{"op": "新增", "id": "k1", "where": "通用",
                                                                                 "text": "x"}], "more": 0}}))

    def test_collect_adds_the_part_only_with_something_to_list(self):
        self.env(**ON)
        now = time.time()
        quiet_view = digest.collect(self.state, DM, now, now - 3600, now)
        self.assertNotIn("knowledge", quiet_view)
        knowledge.save(self.state, "owner", "fact", "新的")
        view = digest.collect(self.state, DM, now + 60, now - 3600, now + 60)
        self.assertEqual(view["knowledge"]["rows"][0]["text"], "新的")
        self.env(**{knowledge.ENV: "0"})
        self.assertNotIn("knowledge", digest.collect(self.state, DM, now + 60, now - 3600, now + 60))


if __name__ == "__main__":
    unittest.main()
