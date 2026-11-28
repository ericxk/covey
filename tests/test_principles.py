"""The owner's principles (core/principles.py): the store, the block every turn reads (app/prompts.py
principles_block, app/runs.py), and the butler's three tools (app/butler_tools.py)."""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from support import make_context, quiet
from support.fake_backends import FakeBackend
from support.records import write_job, write_topic
from support.world import harness_app
from covey.app import butler_tools
from covey.app.harnesses import components
from covey.app.butler_env import ButlerEnv
from covey.app.prompts import (BUTLER_RULES_TEXT, PRINCIPLES_HEAD, bridge_rules, constraints,
                                        butler_rules, principles_block, workspace_block)
from covey.backends.claude.bridge import CLAUDE_SPEC
from covey.backends.claude.sessions import ClaudeSessionStore
from covey.core import butler_requests, butler, principles
from covey.core.checkpoint import CHECKPOINT_RULES, checkpoint_rules
from covey.core.harness import backend_capabilities
from covey.core.jobs import BridgeReplyJob as ReplyJob
from covey.core.butler_tool_specs import TOOLS
from covey.core.roster import NAMES
from covey.core.session_guard import finish_native_session

SID = "11111111-1111-4111-8111-111111111111"


class StoreTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)

    def test_saved_principles_read_back_by_group_with_their_source(self):
        first, new = principles.save(self.state, "owner", "  回复用中文，\n 结论先行 ", source={"job_key": "k", "job_id": 3})
        self.assertTrue(new)
        self.assertRegex(first["id"], principles.ID)
        self.assertEqual(first["text"], "回复用中文， 结论先行")
        self.assertEqual(first["source"], {"job_key": "k", "job_id": 3})
        self.assertRegex(first["at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        second, _ = principles.save(self.state, "workspace", "卡片改动要真实客户端验收", workspace="bridge")
        data = json.loads(principles.path(self.state).read_text())
        self.assertEqual(data["version"], 1)
        self.assertEqual([item["id"] for item in data["owner"]], [first["id"]])
        self.assertEqual([item["id"] for item in data["workspaces"]["bridge"]], [second["id"]])
        self.assertEqual(principles.for_turn(self.state, "bridge"), (data["owner"], data["workspaces"]["bridge"]))
        self.assertEqual(principles.for_turn(self.state, "other"), (data["owner"], []))
        self.assertEqual(principles.for_turn(self.state, ""), (data["owner"], []))
        self.assertEqual([(row["scope"], row["workspace"]) for row in principles.entries(self.state)],
                         [("owner", ""), ("workspace", "bridge")])

    def test_the_same_principle_is_not_kept_twice_in_a_group(self):
        item, _ = principles.save(self.state, "owner", "Reply in Chinese。")
        again, new = principles.save(self.state, "owner", " reply  in chinese ")
        self.assertFalse(new)
        self.assertEqual(again["id"], item["id"])
        _, other = principles.save(self.state, "workspace", "Reply in Chinese", workspace="w")  # Another group.
        self.assertTrue(other)
        self.assertEqual(len(principles.entries(self.state)), 2)

    def test_refusals_write_nothing(self):
        for args, kwargs, said in ((("owner", " "), {}, "空"),
                                   (("owner", "字" * 201), {}, "200"),
                                   (("team", "x"), {}, "scope"),
                                   (("workspace", "x"), {}, "哪个工作区")):
            with self.subTest(said), self.assertRaisesRegex(ValueError, said):
                principles.save(self.state, *args, **kwargs)
        self.assertFalse(principles.path(self.state).exists())
        principles.save(self.state, "owner", "字" * 200)  # The limit itself is fine.

    def test_a_full_group_takes_no_more(self):
        for i in range(principles.GROUP_LIMIT):
            principles.save(self.state, "owner", f"原则 {i}")
        with self.assertRaisesRegex(ValueError, "已有 30 条"):
            principles.save(self.state, "owner", "再一条")
        self.assertEqual(len(principles.load(self.state)["owner"]), principles.GROUP_LIMIT)
        principles.save(self.state, "workspace", "再一条", workspace="w")  # Another group has room.

    def test_delete(self):
        kept, _ = principles.save(self.state, "owner", "留着")
        gone, _ = principles.save(self.state, "workspace", "删掉", workspace="w")
        removed = principles.delete(self.state, gone["id"])
        self.assertEqual((removed["scope"], removed["workspace"], removed["text"]), ("workspace", "w", "删掉"))
        self.assertIsNone(principles.delete(self.state, gone["id"]))
        self.assertIsNone(principles.delete(self.state, ""))
        data = json.loads(principles.path(self.state).read_text())
        self.assertEqual(data["workspaces"], {})  # An emptied workspace group goes.
        self.assertEqual([item["id"] for item in data["owner"]], [kept["id"]])

    def test_a_bad_file_reads_empty_and_is_kept_aside_on_the_next_write(self):
        file = principles.path(self.state)
        self.assertEqual(principles.load(self.state), {"version": 1, "owner": [], "workspaces": {}})  # Missing.
        for text in ("{not json", "[1, 2]"):
            with self.subTest(text):
                file.write_text(text)
                self.assertEqual(principles.load(self.state)["owner"], [])
                self.assertEqual(principles.entries(self.state), [])
        file.write_text("{not json")
        principles.save(self.state, "owner", "新的一条", now=1234)
        self.assertEqual((self.state / "principles.json.bad-1234").read_text(), "{not json")
        self.assertEqual([item["text"] for item in principles.load(self.state)["owner"]], ["新的一条"])

    def test_a_file_that_cannot_be_read_now_is_left_as_it_is(self):
        """W1-F (C10): a network disk's passing error was read as a bad file, moved aside and the principles started over."""
        from unittest import mock
        from covey.core.state import Unreadable
        first, _ = principles.save(self.state, "owner", "回复用中文", now=1)
        file = principles.path(self.state)
        before = file.read_bytes()
        for write in (lambda: principles.save(self.state, "owner", "新的一条", now=2),
                      lambda: principles.delete(self.state, first["id"], now=2)):
            with mock.patch.object(principles, "read_json", side_effect=OSError(5, "Input/output error")):
                with self.assertRaises(Unreadable):
                    write()
            self.assertEqual(file.read_bytes(), before)
            self.assertEqual(sorted(p.name for p in self.state.iterdir() if p.name.startswith("principles.json")), ["principles.json"])

    def test_bad_entries_are_skipped(self):
        principles.path(self.state).write_text(json.dumps({"owner": [
            {"id": "p000001", "text": "好的"}, {"id": "bad", "text": "id 不对"}, {"id": "p000002", "text": ""}, "x"],
            "workspaces": {"w": "not a list", "v": [{"id": "p000003", "text": "工作区的"}]}}))
        self.assertEqual([(row["id"], row["workspace"]) for row in principles.entries(self.state)],
                         [("p000001", ""), ("p000003", "v")])


def item(text, at):
    return {"text": text, "at": at}


class BlockTest(unittest.TestCase):
    def test_no_principles_leave_the_rules_as_they_were(self):
        before = "\n\n".join((constraints("unit"), workspace_block("ws", Path(".")), checkpoint_rules()))
        self.assertEqual(bridge_rules(backend_name="unit", workspace_name="ws", workdir=Path(".")), before)
        self.assertEqual(bridge_rules(backend_name="unit", workspace_name="ws", workdir=Path("."), principles=""), before)
        self.assertEqual(principles_block([], [], "ws"), "")

    def test_the_block_sits_between_the_workspace_and_the_checkpoint_contract(self):
        block = principles_block([item("回复用中文", "2026-11-08T01:00:00Z")],
                                 [item("和启动卡对齐", "2026-11-08T02:00:00Z")], "bridge")
        self.assertEqual(block.splitlines(), [PRINCIPLES_HEAD, "通用：", "- 回复用中文", "工作区 bridge：", "- 和启动卡对齐"])
        rules = bridge_rules(backend_name="unit", workspace_name="bridge", workdir=Path("."), principles=block)
        self.assertEqual(rules, "\n\n".join((constraints("unit"), workspace_block("bridge", Path(".")), block,
                                             checkpoint_rules())))

    def test_past_the_limit_the_oldest_of_either_group_are_left_out(self):
        owner = [item(f"通用 {i} " + "字" * 150, f"2026-11-0{1 + i % 2}T00:00:{i:02d}Z") for i in range(30)]
        space = [item(f"工作区 {i} " + "字" * 150, f"2026-11-01T12:00:{i:02d}Z") for i in range(30)]
        block = principles_block(owner, space, "w")
        lines = block.splitlines()
        listed = [line for line in lines if line.startswith("- ")]
        self.assertLessEqual(sum(len(line) + 1 for line in listed), 2000)
        self.assertEqual(lines[-1], f"另有 {60 - len(listed)} 条较早的原则未列出。")
        # The newest are those of 11-02, the owner's odd ones: the newest of them kept, listed oldest first.
        odd = ["- " + owner[i]["text"] for i in range(1, 30, 2)]
        self.assertEqual(listed, odd[len(odd) - len(listed):])
        self.assertFalse(any(line.startswith("- 工作区") for line in listed))
        self.assertNotIn("工作区 w：", lines)

    def test_the_butler_is_told_when_to_save_and_delete(self):
        for rules in (BUTLER_RULES_TEXT, butler_rules()):
            for words in ("memory_save", "“以后……”", "已记下：……", "不记", "memory_revise", "memory_search"):
                self.assertIn(words, rules)


OWNER, OTHER, DM, GROUP = "ou_owner", "ou_colleague", "oc_dm", "oc_group"
BOTH = "通用：\n- 回复用中文\n工作区 ws：\n- ws 的原则"
WORKSPACE_ONLY = PRINCIPLES_HEAD + "\n工作区 ws：\n- ws 的原则"


class TurnTest(unittest.TestCase):
    """Every Harness's turn carries the block in its message: the fresh session's rules, each resumed turn's
    reminder, the butler's too (its workspace's group). 通用 only on the owner's own turn (app/access.py
    owner_turn); a 代答 topic none."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.ctx = make_context(self.root)

    def save(self):
        principles.save(self.ctx.state_dir, "owner", "回复用中文")
        principles.save(self.ctx.state_dir, "workspace", "ws 的原则", workspace="ws")
        principles.save(self.ctx.state_dir, "workspace", "别处的原则", workspace="other")

    def app(self, backend=None, spec=CLAUDE_SPEC, store=None):
        app = harness_app(spec, backend or FakeBackend(), store or ClaudeSessionStore())
        quiet(app)
        app.runtime.state_dir, app.runtime.owner_open_id = self.ctx.state_dir, OWNER
        app.runtime.access.remember_owner_chat(DM)
        return app

    def job(self, kind="chat", chat=GROUP, sender=OWNER, topic=""):
        return ReplyJob(job_id=1, user_text="帮我检查代码", chat_id=chat, message_id="om", timeout=0, kind=kind,
                        prompt_text="REVIEW" if kind != "chat" else "", sender_id=sender, topic_id=topic)

    def turn(self, sid=None, **job):
        backend = FakeBackend()
        app, job = self.app(backend), self.job(**job)
        app.runs.generate_reply_for_worker(self.ctx, job, sid, None)
        for claimed in job.native_session_claims:  # Settled as the worker does, so the next turn may resume it.
            finish_native_session(self.ctx, CLAUDE_SPEC.name, claimed, job.bridge_run_id, True)
        return backend.prompts[-1]

    def test_fresh_and_resumed_turns_and_other_kinds_carry_it(self):
        self.save()
        for sid, kind in ((None, "chat"), (SID, "chat"), (SID, "review")):
            with self.subTest(resumed=bool(sid), kind=kind):
                prompt = self.turn(sid, kind=kind)
                self.assertEqual(prompt.count(PRINCIPLES_HEAD), 1)
                self.assertIn(BOTH, prompt)
                self.assertNotIn("别处的原则", prompt)
                self.assertLess(prompt.index(PRINCIPLES_HEAD), prompt.index(CHECKPOINT_RULES))

    def test_the_owners_own_principles_only_on_the_owners_turn(self):
        self.save()
        cases = (("the owner in a group", dict(sender=OWNER), True),
                 ("a colleague in a group", dict(sender=OTHER), False),
                 ("a group job that kept no sender", dict(sender=""), False),
                 ("the owner's private chat, no sender", dict(chat=DM, sender=""), True),
                 ("the main thread, no sender", dict(chat=DM, sender="", topic=butler.butler_topic_id(DM)), True))
        for name, job, owner in cases:
            for sid in (None, SID):
                with self.subTest(name, resumed=bool(sid)):
                    prompt = self.turn(sid, **job)
                    if owner:
                        self.assertIn(BOTH, prompt)
                    else:
                        self.assertIn(WORKSPACE_ONLY, prompt)
                        self.assertNotIn("回复用中文", prompt)

    def test_without_an_owner_or_its_chat_known_only_the_workspaces(self):
        self.save()
        app = harness_app(CLAUDE_SPEC, FakeBackend(), ClaudeSessionStore())  # No owner, no state directory.
        self.assertIn(WORKSPACE_ONLY, app.runs.bridge_rules(self.ctx, self.job(chat=DM, sender="")))
        self.assertIn(WORKSPACE_ONLY, app.runs.bridge_rules(self.ctx, self.job(sender=OWNER)))
        self.assertIn(WORKSPACE_ONLY, self.app().runs.bridge_rules(self.ctx))  # No job.

    def test_without_principles_no_block(self):
        for sid in (None, SID):
            self.assertNotIn(PRINCIPLES_HEAD, self.turn(sid))

    def test_a_proxy_topic_gets_none(self):
        self.save()
        write_topic(self.ctx, proxy={"contact": "ou_x"})
        self.assertNotIn(PRINCIPLES_HEAD, self.turn(chat=DM, sender=OWNER))

    def test_a_proxy_topics_turn_has_its_own_rules_and_no_checkpoint(self):
        # T571: a 代答 turn's replies go to other people in plain text; an ordinary topic's Markdown layout, checkpoint
        # contract and checkpoint note drew two of its three tool calls into bookkeeping.
        write_topic(self.ctx, proxy={"contact": "ou_x"})
        for sid in (None, SID):
            with self.subTest(resumed=bool(sid)):
                backend = FakeBackend()
                app, job = self.app(backend), self.job(chat=DM, sender=OWNER)
                job.checkpoint_path = str(self.ctx.state_dir / "checkpoints" / "cp.json")
                app.runs.generate_reply_for_worker(self.ctx, job, sid, None)
                for claimed in job.native_session_claims:
                    finish_native_session(self.ctx, CLAUDE_SPEC.name, claimed, job.bridge_run_id, True)
                prompt = backend.prompts[-1]
                self.assertNotIn(CHECKPOINT_RULES, prompt)
                self.assertNotIn("适合飞书的 Markdown", prompt)
                self.assertNotIn("本任务检查点文件", prompt)
                if sid is None:
                    self.assertIn("通过飞书桥接器替主人回复别人（代答）", prompt)
        # Any other topic keeps them.
        write_topic(self.ctx, proxy=None)
        self.assertIn(CHECKPOINT_RULES, self.app().runs.bridge_rules(self.ctx, self.job()))

    def test_every_harness_has_it_in_its_rules_and_its_message(self):
        """Every core backend takes the rules at the system level (the owner's call of 11-28, app/rules_sent.py), the
        principles with them, the system level's text and the message's alike."""
        self.save()
        for name in NAMES:
            with self.subTest(harness=name):
                spec, backend, store = components(name)
                self.assertTrue(backend_capabilities(backend, self.ctx).system_instructions)
                runs = self.app(backend, spec, store).runs
                self.assertIn(PRINCIPLES_HEAD + "\n" + BOTH, runs.bridge_rules(self.ctx, self.job(), system=True))
                self.assertIn(PRINCIPLES_HEAD + "\n" + BOTH, runs.bridge_rules(self.ctx, self.job()))
                self.assertIn(WORKSPACE_ONLY, runs.bridge_rules(self.ctx, self.job(sender=OTHER)))

    def test_a_turn_reads_its_own_workspaces_group(self):
        self.save()
        ctx = make_context(self.root, workspace_name="other")
        rules = self.app().runs.bridge_rules(ctx, self.job())
        self.assertIn("通用：\n- 回复用中文\n工作区 other：\n- 别处的原则", rules)
        self.assertNotIn("ws 的原则", rules)


class ToolTest(unittest.TestCase):
    CHAT = "oc_dm"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        (self.state / "workspace-config.json").write_text(json.dumps({"workspaces": {"bridge": "/x", "kb": "/y"}}))
        write_job(self.state, "main-1", chat_id=self.CHAT, topic_id=butler.butler_topic_id(self.CHAT), job_id=12,
                  status="running")
        runtime = type("R", (), {"log": staticmethod(lambda line: None),
                                 "access": type("A", (), {"owner_chat": staticmethod(lambda: self.CHAT)})()})()
        self.env = ButlerEnv(runtime, None, None, None, state_dir=self.state, profile="unit")

    def call(self, name, **args):
        return butler_tools.TOOLS[name](self.env, butler_requests.Request("r1", name, args, "main-1", time.time()))

    def test_save_list_delete(self):
        saved = self.call("principle_save", scope="owner", text="回复用中文")
        self.assertTrue(saved["ok"], saved)
        self.assertTrue(saved["data"]["new"])
        pid = saved["data"]["id"]
        self.assertIn(f"已记下通用的原则（{pid}）：回复用中文", saved["text"])
        self.assertEqual(principles.load(self.state)["owner"][0]["source"], {"job_key": "main-1", "job_id": 12})
        again = self.call("principle_save", scope="owner", text="回复用中文。")
        self.assertEqual((again["ok"], again["data"]), (True, {"id": pid, "new": False}))
        self.assertIn("没有重复记", again["text"])
        space = self.call("principle_save", scope="workspace", workspace="bridge", text="卡片要验收")
        self.assertIn("工作区 bridge 的原则", space["text"])
        listed = self.call("principles_list")
        self.assertEqual(listed["data"]["count"], 2)
        lines = listed["text"].splitlines()
        self.assertTrue(lines[0].startswith(f"{pid} · 通用 · 20") and lines[0].endswith(" · 回复用中文"), lines)
        self.assertIn(" · 工作区 bridge · ", lines[1])
        deleted = self.call("principle_delete", id=pid)
        self.assertTrue(deleted["ok"])
        self.assertIn("已删掉：" + pid, deleted["text"])
        self.assertEqual(self.call("principles_list")["data"]["count"], 1)

    def test_failures(self):
        self.assertEqual(self.call("principles_list")["text"], "还没有记下的原则。")
        missing = self.call("principle_delete", id="p123456")
        self.assertFalse(missing["ok"])
        self.assertIn("principles_list", missing["text"])
        for args, said in (({"scope": "workspace", "workspace": "nope", "text": "x"}, "可选：bridge、kb"),
                           ({"scope": "workspace", "text": "x"}, "哪个工作区"),
                           ({"scope": "everyone", "text": "x"}, "scope"),
                           ({"scope": "owner", "text": "字" * 201}, "200 字")):
            with self.subTest(said), self.assertRaisesRegex(ValueError, said):
                self.call("principle_save", **args)
        with self.assertRaisesRegex(ValueError, "id"):
            self.call("principle_delete")
        self.assertFalse(principles.path(self.state).exists())

    def test_the_loop_answers_a_refusal_in_words(self):
        answer = butler_tools.ButlerTools(self.env, butler_tools.TOOLS).execute(
            butler_requests.Request("r2", "principle_save", {"scope": "owner", "text": ""}, "main-1", time.time()))
        self.assertFalse(answer["ok"])
        self.assertEqual(answer["text"], "principle_save 没有完成：原则的正文是空的")

    def test_the_specs_name_every_handler(self):
        """The principle tools are no longer listed (docs/butler.md 记忆): an older session's call is run as the memory tool
        that took its place (core/butler_tool_specs.py RENAMED_TOOLS), and a request an older server wrote still has its handler."""
        from covey.app import butler_memory
        from covey.core.butler_tool_specs import current_name
        specs = {tool["name"]: tool for tool in TOOLS}
        for name in ("principle_save", "principle_delete", "principles_list"):
            self.assertIn(name, butler_tools.TOOLS)
            self.assertNotIn(name, specs)
            self.assertIn(current_name(name), specs)
            self.assertIn(current_name(name), butler_memory.TOOLS)
        self.assertEqual(specs["memory_save"]["input_schema"]["required"], ["text", "kind", "scope"])
        self.assertIn("一次性的要求", specs["memory_save"]["description"])


if __name__ == "__main__":
    unittest.main()
