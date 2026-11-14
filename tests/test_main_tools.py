"""The main thread's tool channel (docs/main-thread.md 工具通道): the MCP server in its session (core/main_mcp.py through
bin/covey_main_mcp.py, run as a process), the Bridge's loop that runs the calls (app/main_tools.py MainTools) and
the handlers of app/main_tools.py TOOLS on records written into a temporary state directory."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support import REPO
from support.records import write_job, state_dir, write_topic
from support.world import QuestionFixture
from test_question_followup import AnswerFollowUpFixture
from covey.app import main_tools
from covey.app.answers import answer_fields
from covey.app.main_env import MainEnv, reply
from covey.app.main_tools import PROXY_NOTE, REFUSED, MainTools
from covey.core import main_mcp, main_requests, main_thread
from covey.core.main_tool_specs import NAMES
from covey.core.state import write_private_text
from covey.core.history_storage import append_history
from covey.core.topic_seen import SEEN_FILE

CHAT = "oc_dm"
MAIN = main_thread.main_topic_id(CHAT)
NOW = time.time()


def runtime(chat=CHAT, logs=None):
    logs = [] if logs is None else logs
    return SimpleNamespace(log=logs.append, access=SimpleNamespace(owner_chat=lambda: chat),
                           shutdown=threading.Event())


def make_env(state, *, chat=CHAT, logs=None, topics=None):
    topics = topics or SimpleNamespace(harnesses={}, default="claude", target=lambda *args: None)
    return MainEnv(runtime(chat, logs), topics, None, None, state_dir=Path(state), profile="unit")


def main_job(state, key="main-1", status="running", chat=CHAT):
    return write_job(state, key, chat_id=chat, topic_id=main_thread.main_topic_id(chat), job_id=1, status=status)


def iso(seconds):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))


class McpServerTest(unittest.TestCase):
    """bin/covey_main_mcp.py as Claude Code runs it: lines in, lines out."""

    def setUp(self):
        self.state = state_dir(self)

    def serve(self, messages, env):
        lines = "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in messages)
        result = subprocess.run([sys.executable, "-B", str(REPO / "bin/covey_main_mcp.py")], input=lines,
                                capture_output=True, text=True, timeout=60,
                                env={"PATH": os.environ.get("PATH", ""), **env})
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        return {reply["id"]: reply for reply in map(json.loads, result.stdout.splitlines())}

    def bridge(self, answers, seen, stop):
        """A stand-in for the Bridge: answers each request it claims with ``answers[tool]``."""
        while not stop.is_set():
            for request in main_requests.pending(self.state):
                if main_requests.claim(self.state, request.id):
                    seen.append(request)
                    main_requests.answer(self.state, request.id, answers[request.tool])
            stop.wait(0.01)

    def test_initialize_list_and_calls(self):
        seen, stop = [], threading.Event()
        worker = threading.Thread(target=self.bridge, args=({
            "topics_list": reply(True, "T1 · 标题", total=1),
            "topic_read": reply(False, "找不到话题“T9”")}, seen, stop))
        worker.start()
        self.addCleanup(worker.join)
        self.addCleanup(stop.set)
        replies = self.serve([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "claude-code"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "ping"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
             "params": {"name": "topics_list", "arguments": {"group": "all"}}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
             "params": {"name": "topic_read", "arguments": {"topic": "T9"}}},
            {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "rm_rf", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 7, "method": "resources/list"},
        ], {main_thread.STATE_ENV: str(self.state), main_thread.JOB_ENV: "main-1"})
        self.assertEqual(sorted(replies), [1, 2, 3, 4, 5, 6, 7])  # Nothing for the notification.
        init = replies[1]["result"]
        self.assertEqual(init["protocolVersion"], "2025-06-18")
        self.assertIn("tools", init["capabilities"])
        self.assertEqual(init["serverInfo"]["name"], main_mcp.SERVER["name"])
        self.assertEqual(replies[2]["result"], {})
        tools = replies[3]["result"]["tools"]
        self.assertEqual([tool["name"] for tool in tools], list(NAMES))
        for tool in tools:
            self.assertEqual(set(tool), {"name", "description", "inputSchema"})
            self.assertEqual(tool["inputSchema"]["type"], "object")
        listed = replies[4]["result"]
        self.assertFalse(listed["isError"])
        self.assertEqual(listed["content"][0]["type"], "text")
        self.assertTrue(listed["content"][0]["text"].startswith("T1 · 标题\n\n"))
        self.assertEqual(json.loads(listed["content"][0]["text"].split("\n\n", 1)[1]), {"total": 1})
        self.assertEqual(replies[5]["result"], {"content": [{"type": "text", "text": "找不到话题“T9”"}], "isError": True})
        self.assertTrue(replies[6]["result"]["isError"])
        self.assertEqual(replies[7]["error"]["code"], main_mcp.NO_METHOD)
        # Each tools/call runs on a thread of its own: the Bridge may see them in either order.
        self.assertCountEqual([(r.tool, r.args, r.job_key) for r in seen],
                              [("topics_list", {"group": "all"}, "main-1"), ("topic_read", {"topic": "T9"}, "main-1")])
        self.assertEqual(list(main_requests.folder(self.state).iterdir()), [])

    def test_an_older_protocol_is_answered_in_kind_and_an_unknown_one_with_the_newest(self):
        replies = self.serve([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
            {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}}], {})
        self.assertEqual(replies[1]["result"]["protocolVersion"], "2024-11-05")
        self.assertEqual(replies[2]["result"]["protocolVersion"], main_mcp.PROTOCOLS[0])

    def test_without_its_environment_every_call_says_what_is_missing(self):
        replies = self.serve([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "topics_list", "arguments": {}}}],
            {main_thread.STATE_ENV: str(self.state)})
        self.assertEqual(len(replies[1]["result"]["tools"]), len(NAMES))
        result = replies[2]["result"]
        self.assertTrue(result["isError"])
        self.assertIn(main_thread.JOB_ENV, result["content"][0]["text"])
        self.assertNotIn(main_thread.STATE_ENV, result["content"][0]["text"])
        self.assertFalse((self.state / main_requests.DIR).exists())  # Nothing was written for the Bridge.

    def test_bad_lines_get_errors_and_the_server_keeps_going(self):
        server = main_mcp.Server({})
        out = []
        ping = json.dumps({"jsonrpc": "2.0", "id": 9, "method": "ping"})
        main_mcp.serve(server, ["not json\n", "[1]\n", ping + "\n"], out.append)
        codes = [json.loads(line).get("error", {}).get("code") for line in out]
        self.assertEqual(codes, [main_mcp.PARSE_ERROR, main_mcp.INVALID_REQUEST, None])

    def test_arguments_must_be_an_object(self):
        server = main_mcp.Server({main_thread.STATE_ENV: "/s", main_thread.JOB_ENV: "k"},
                                 submit=mock.Mock(side_effect=AssertionError("not submitted")))
        answer = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                "params": {"name": "topics_list", "arguments": [1]}})
        self.assertTrue(answer["result"]["isError"])


class LoopTest(unittest.TestCase):
    def setUp(self):
        self.state = state_dir(self)
        self.logs = []
        self.handler = mock.Mock(return_value=reply(True, "好"))
        self.tools = MainTools(make_env(self.state, logs=self.logs), {"topics_list": self.handler})

    def request(self, tool="topics_list", job_key="main-1", args=None):
        request_id = f"r{len(list(main_requests.folder(self.state).glob('*.json')))}"
        main_requests._write(main_requests.folder(self.state) / f"{request_id}.json",
                             {"id": request_id, "tool": tool, "args": args or {}, "job_key": job_key,
                              "created_at": time.time()})
        return request_id

    def answer(self, request_id):
        return json.loads((main_requests.folder(self.state) / f"{request_id}.answer.json").read_text())

    def test_only_with_the_switch_a_thread(self):
        with mock.patch.dict(os.environ, {main_thread.ENV: "0"}):
            self.assertEqual(self.tools.threads(), ())
        with mock.patch.dict(os.environ, {main_thread.ENV: "1"}):
            self.assertEqual([name for name, _ in self.tools.threads()], ["main-tools"])

    def test_the_running_main_job_is_served_and_logged(self):
        main_job(self.state)
        request_id = self.request(args={"group": "all"})
        self.tools.tick()
        self.assertEqual(self.answer(request_id), reply(True, "好"))
        called = self.handler.call_args.args
        self.assertEqual((called[1].tool, called[1].args), ("topics_list", {"group": "all"}))
        self.assertRegex(self.logs[-1], r"^main tool name=topics_list ok=True ms=\d+ job=main-1$")
        self.tools.tick()
        self.handler.assert_called_once()  # Claimed once, answered once.

    def test_calls_from_anything_but_the_running_main_job_are_refused(self):
        main_job(self.state, "ended", status="done")
        write_job(self.state, "topic-job", chat_id=CHAT, topic_id="ab" * 16, job_id=2, status="running")
        main_job(self.state, "other-chat", chat="oc_other")
        ids = [self.request(job_key=key) for key in ("ended", "topic-job", "other-chat", "missing", "")]
        self.tools.tick()
        self.assertEqual([self.answer(i)["text"] for i in ids], [REFUSED] * 5)
        self.handler.assert_not_called()

    def test_no_private_chat_yet_refuses(self):
        main_job(self.state)
        tools = MainTools(make_env(self.state, chat=""), {"topics_list": self.handler})
        request_id = self.request()
        tools.tick()
        self.assertEqual(self.answer(request_id)["text"], REFUSED)

    def test_an_unknown_tool_is_refused(self):
        main_job(self.state)
        request_id = self.request(tool="topic_start")
        self.tools.tick()
        answer = self.answer(request_id)
        self.assertFalse(answer["ok"])
        self.assertIn("topic_start", answer["text"])

    def test_a_handler_that_raises_is_answered_and_the_loop_goes_on(self):
        main_job(self.state)
        self.handler.side_effect = [RuntimeError("boom"), ValueError("话题 T9 不存在"), reply(True, "好")]
        first, second, third = self.request(), self.request(), self.request()
        self.tools.tick()
        answers = [self.answer(i) for i in (first, second, third)]
        self.assertEqual([a["ok"] for a in answers], [False, False, True])
        self.assertIn("RuntimeError", answers[0]["text"])
        self.assertIn("话题 T9 不存在", answers[1]["text"])
        self.assertTrue(any("failed: RuntimeError: boom" in line for line in self.logs))
        self.assertIn("ok=False", self.logs[1])

    def test_orphans_are_answered_uncertain_and_not_run(self):
        main_job(self.state)
        request_id = self.request()
        self.assertTrue(main_requests.claim(self.state, request_id))  # A previous process took it and stopped.
        self.tools.recover()
        self.assertEqual(self.answer(request_id), main_requests.UNCERTAIN)
        self.tools.tick()
        self.handler.assert_not_called()

    def test_run_recovers_then_polls_until_shutdown(self):
        main_job(self.state)
        request_id = self.request()
        main_requests.claim(self.state, request_id)
        fresh = self.request()
        runtime = self.tools.env.runtime
        runtime.shutdown.wait = lambda seconds: runtime.shutdown.set()  # One round.
        self.tools.run(SimpleNamespace())
        self.assertEqual(self.answer(request_id), main_requests.UNCERTAIN)
        self.assertEqual(self.answer(fresh)["text"], "好")


class HandlerTest(unittest.TestCase):
    def setUp(self):
        self.state = state_dir(self)
        self.harnesses = {"claude": SimpleNamespace(spec=SimpleNamespace(title="Claude Code"))}
        self.env = make_env(self.state, topics=SimpleNamespace(harnesses=self.harnesses, default="claude",
                                                               target=lambda *args: None))

    def topic(self, topic_id, number, title, *, workspace="bridge", jobs=(), chat=CHAT):
        write_topic(self.state, topic_id=topic_id, chat_id=chat, topic_number=number, harness_id="claude",
                    workspace_name=workspace, root_id="om_" + topic_id[:6])
        append_history(self.state / "topics" / topic_id / "history.jsonl", "user", title + "\n第二行")
        for job_id, fields in jobs:
            # A task card, as every job that ran has: the board's results are those (core/board_read.py).
            write_job(self.state, f"{topic_id[:6]}-{job_id}", chat_id=chat, topic_id=topic_id, job_id=job_id,
                      workspace_name=workspace, **{"card_message_id": f"om_card_{topic_id[:6]}{job_id}", **fields})

    def question(self, token, topic_id, questions, **fields):
        write_private_text(self.state / "questions" / (token + ".json"), json.dumps({
            "token": token, "topic_id": topic_id, "chat_id": CHAT, "job_id": 1, "status": "pending",
            "created_at": NOW - 60, "question_message_id": "om_q_" + token[:6], "questions": questions, **fields},
            ensure_ascii=False))

    def call(self, name, **args):
        return main_tools.TOOLS[name](self.env, main_requests.Request("r1", name, args, "main-1", NOW))

    def seed(self):
        self.topic("a1" * 16, 1, "修首页", jobs=[(1, {"status": "running", "user_text": "修首页", "phase": "改代码"})])
        self.topic("b2" * 16, 2, "查日志", jobs=[(2, {"status": "done", "user_text": "查日志", "final_text": "查完了\n没有异常"})])
        self.topic("c3" * 16, 3, "要不要发版", jobs=[(3, {"status": "done", "final_text": "等你定"})])
        self.question("q" * 16, "c3" * 16, [{"id": "ship", "question": "现在发版吗？", "options": [{"label": "发"}]}])
        self.topic("d4" * 16, 4, "很久以前", jobs=[(4, {"status": "done", "final_text": "旧的"})])
        path = self.state / "jobs"
        for record in path.glob("d4d4d4-4*.json"):
            data = json.loads(record.read_text())
            data["updated_at"] = iso(NOW - 10 * 86400)
            record.write_text(json.dumps(data))
        main_job(self.state)

    def test_topics_list_groups(self):
        self.seed()
        listed = self.call("topics_list", group="all")
        lines = listed["text"].splitlines()
        self.assertTrue(listed["ok"])
        self.assertEqual([line.split(" · ")[0] for line in lines], ["T3", "T2", "T1", "T4"])
        self.assertEqual([line.split(" · ")[2] for line in lines], ["等你回答", "有结果未看", "进行中", "更早"])
        self.assertIn("问：现在发版吗？", lines[0])
        self.assertIn("最后：没有异常", lines[1])
        self.assertIn("在做：改代码", lines[2])
        self.assertIn("Claude Code", lines[2])
        self.assertTrue(lines[0].endswith("c3" * 16))
        self.assertNotIn(MAIN, listed["text"])
        self.assertEqual([l.split(" · ")[0] for l in self.call("topics_list")["text"].splitlines()], ["T3", "T2", "T1"])
        self.assertEqual(self.call("topics_list", group="running")["text"].split(" · ")[0], "T1")
        self.assertEqual(self.call("topics_list", group="all", query="日志")["text"].split(" · ")[0], "T2")
        self.assertEqual(len(self.call("topics_list", group="all", limit=1)["text"].splitlines()), 2)  # And the count.

    def test_a_result_the_owner_has_seen_is_not_unread(self):
        self.seed()
        # T3 also ended unseen, but it asks a question: a topic is in one group.
        self.assertEqual(self.call("topics_list", group="result")["text"].split(" · ")[0], "T2")
        self.assertEqual(self.call("topics_list", group="result")["data"]["total"], 1)
        # Read after the job record was written: it has no timings, so it ended when written (its updated_at),
        # which comes long after NOW when this module runs late in a process that imported it first.
        write_private_text(self.state / SEEN_FILE, json.dumps({"b2" * 16: time.time() + 60}))
        self.assertEqual(self.call("topics_list", group="result")["data"]["total"], 0)

    def test_a_record_written_after_its_end_is_not_a_new_result(self):
        ended = NOW - 3600
        self.topic("b2" * 16, 2, "查日志", jobs=[(2, {"status": "done", "final_text": "查完了",
                                                     "timings": {"execution_finished": ended - 5, "delivered": ended}})])
        # Seen after it ended; the record's updated_at, written just now, is later than that.
        write_private_text(self.state / SEEN_FILE, json.dumps({"b2" * 16: ended + 60}))
        self.assertEqual(self.call("topics_list", group="result")["data"]["total"], 0)
        write_private_text(self.state / SEEN_FILE, json.dumps({"b2" * 16: ended - 60}))
        self.assertEqual(self.call("topics_list", group="result")["data"]["total"], 1)

    def test_topic_read(self):
        checkpoint = self.state / "cp.json"
        checkpoint.write_text(json.dumps({"phase": "验证", "completed": ["改完"], "next_step": "跑测试",
                                          "requires_user": True, "question": "要不要发版？"}), encoding="utf-8")
        self.topic("c3" * 16, 3, "发版", jobs=[(1, {"status": "done", "user_text": "第一轮", "final_text": "旧"}),
                                               (2, {"status": "done", "user_text": "第二轮", "final_text": "结论" * 400}),
                                               (3, {"status": "done", "user_text": "第三轮", "final_text": "等你定",
                                                    "checkpoint_path": str(checkpoint)})])
        self.question("q" * 16, "c3" * 16, [
            {"id": "ship", "header": "发版", "question": "现在发版吗？", "options": [{"label": "发"}, {"label": "不发"}]},
            {"id": "why", "question": "为什么？"}])
        read = self.call("topic_read", topic="T3")
        text = read["text"]
        self.assertTrue(read["ok"])
        self.assertNotIn("第一轮", text)
        self.assertIn("主人：第二轮", text)
        self.assertIn("结论" * 250 + "…", text)
        self.assertIn("检查点阶段：验证", text)
        self.assertIn("检查点下一步：跑测试", text)
        self.assertIn("检查点：在等主人", text)
        self.assertIn("[ship] 发版：现在发版吗？（单选）", text)
        self.assertIn("    · 不发", text)
        self.assertIn("[why] 为什么？（自由回答，没有选项）", text)
        self.assertEqual(read["data"]["pending_questions"], 1)
        self.assertIn("第一轮", self.call("topic_read", topic="c3" * 16, turns=5)["text"])
        with self.assertRaisesRegex(ValueError, "T9"):
            self.call("topic_read", topic="T9")

    def test_another_chats_topic_is_not_found(self):
        self.topic("e5" * 16, 5, "别人的", chat="oc_other", jobs=[(1, {"status": "done"})])
        for ref in ("T5", "e5" * 16):
            with self.assertRaises(ValueError):
                self.call("topic_read", topic=ref)

    def test_workspaces_list(self):
        (self.state / "workspace-config.json").write_text(json.dumps(
            {"default": "bridge", "workspaces": {"bridge": "/w/bridge", "misc": "/w/misc"}}))
        (self.state / "main-thread.json").write_text(json.dumps({"workspace_notes": {"bridge": "Bridge 仓库"}},
                                                                ensure_ascii=False), encoding="utf-8")
        self.topic("a1" * 16, 1, "修首页", jobs=[(1, {"status": "done"})])
        lines = self.call("workspaces_list")["text"].splitlines()
        self.assertEqual(lines, ["bridge（默认） · /w/bridge · 用途：Bridge 仓库 · 最近：T1 修首页", "misc · /w/misc"])

    def test_topic_priority(self):
        self.topic("a1" * 16, 1, "修首页", jobs=[(1, {"status": "done"})])
        answer = self.call("topic_priority", topic="t1")
        self.assertTrue(answer["ok"])
        saved = json.loads((self.state / main_tools.PRIORITY).read_text())
        self.assertEqual(list(saved), ["a1" * 16])
        self.assertGreaterEqual(saved["a1" * 16], NOW)

    def test_topic_priority_can_take_a_topic_off_the_board(self):
        from covey.app import pins
        from covey.core.topic_seen import PINNED_TOPICS_ENV
        self.topic("a1" * 16, 1, "修首页", jobs=[(1, {"status": "done"})])
        with mock.patch.dict(os.environ, {PINNED_TOPICS_ENV: ""}):
            answer = self.call("topic_priority", topic="t1", remove=True)
        self.assertFalse(answer["ok"])  # No board: nothing to take off.
        self.assertFalse((self.state / pins.RECORD).exists())
        with mock.patch.dict(os.environ, {PINNED_TOPICS_ENV: "1"}):
            answer = self.call("topic_priority", topic="t1", remove=True)
        self.assertTrue(answer["ok"], answer)
        self.assertIn("从看板撤下", answer["text"])
        dismissed = json.loads((self.state / pins.RECORD).read_text())["dismissed"]
        self.assertEqual(list(dismissed), ["a1" * 16])
        self.assertGreaterEqual(dismissed["a1" * 16]["at"], NOW)
        self.assertFalse((self.state / main_tools.PRIORITY).exists())  # Not moved up.

    def test_topic_priority_describes_the_board_order(self):
        # The main thread explains the board from this note: it names the groups in pins.GROUPS order (D2).
        from covey.app import pins
        from covey.core.main_tool_specs import TOOLS
        note = next(tool["description"] for tool in TOOLS if tool["name"] == "topic_priority")
        names = {"waiting": "等你回答", "running": "进行中", "result": "有结果"}
        self.assertEqual(" → ".join(names[group] for group in pins.GROUPS), "等你回答 → 进行中 → 有结果")
        self.assertIn("等你回答 → 进行中 → 有结果", note)


class AnswerFieldsTest(unittest.TestCase):
    RECORD = {"questions": [
        {"id": "ship", "question": "现在发版吗？", "options": [{"label": "发"}, {"label": "不发"}]},
        {"id": "where", "question": "发到哪？", "multiple": True, "options": [{"label": "灰度"}, {"label": "全量"}]}]}

    def test_choices_become_the_card_form(self):
        fields = answer_fields(self.RECORD, {"ship": "不发", "发到哪？": ["全量", "灰度"]}, PROXY_NOTE)
        self.assertEqual(fields, {"option_0": "1", "option_1": ["1", "0"], "supplement": PROXY_NOTE})
        self.assertEqual(answer_fields(self.RECORD, {"ship": ["发"], "where": "灰度"}, PROXY_NOTE)["option_1"], ["0"])

    def test_refusals(self):
        for answers, said in (({"ship": "发"}, "每题都要答"),
                              ({"ship": ["发", "不发"], "where": "灰度"}, "单选"),
                              ({"ship": "也许", "where": "灰度"}, "不是"),
                              ({"ship": "发", "where": "灰度", "nope": "x"}, "没有题目"),
                              ({"ship": "发", "现在发版吗？": "发", "where": "灰度"}, "答了两次"),
                              ({"ship": 1, "where": "灰度"}, "选项原文"),
                              ([], "对象")):
            with self.subTest(said), self.assertRaisesRegex(ValueError, said):
                answer_fields(self.RECORD, answers, PROXY_NOTE)
        free = {"questions": [*self.RECORD["questions"], {"id": "why", "question": "为什么？"}]}
        with self.assertRaisesRegex(ValueError, "topic_send"):
            answer_fields(free, {"ship": "发", "where": "灰度", "why": "因为"}, PROXY_NOTE)


class FinishedTaskAnswerTest(AnswerFollowUpFixture, unittest.TestCase):
    """question_answer when the task that asked has ended: the answer is saved and a continuation task queued."""

    def test_the_continuation_carries_the_choice_and_the_mark(self):
        state = self.ctx_topic.state_dir
        write_topic(state, topic_id=self.ctx_topic.topic_id, chat_id="oc_chat", topic_number=8, harness_id="claude",
                    root_id="om_q")
        record = json.loads(self.path.read_text())
        record["questions"][0]["options"] = [{"label": "全部"}, {"label": "一半"}]
        self.path.write_text(json.dumps(record))
        self.app.questions.enqueue_continuation = mock.Mock(return_value=SimpleNamespace(job_id=93))
        env = MainEnv(runtime("oc_chat"),
                      SimpleNamespace(harnesses={}, target=lambda *args: (self.app, self.ctx_topic, {})),
                      None, None, state_dir=state, profile="unit")
        request = main_requests.Request("r2", "question_answer", {"topic": "T8", "answers": {"q": "一半"}}, "main-1", NOW)
        answer = main_tools.question_answer(env, request)
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["data"]["status"], "answered")
        self.assertEqual(self.record()["continuation_job_id"], 93)
        self.app.questions.enqueue_continuation.assert_called_once()
        sent = self.app.questions.enqueue_continuation.call_args.args[1]
        self.assertIn("范围？\n一半", sent)
        self.assertIn(PROXY_NOTE, sent)


class QuestionAnswerTest(QuestionFixture, unittest.TestCase):
    """question_answer through the path of 提交回答 on the card: a running task's blocking question (request 17)."""

    def setUp(self):
        super().setUp()
        state = self.ctx_topic.state_dir
        write_topic(state, topic_id=self.job.topic_id, chat_id="oc_chat", topic_number=7, harness_id="claude",
                    root_id="om_question")
        self.targets = []

        def target(state_dir, topic_id, chat):
            self.targets.append((topic_id, chat))
            return self.app, self.ctx_topic, {}

        self.env = MainEnv(runtime("oc_chat"), SimpleNamespace(harnesses={}, target=target), None, None,
                           state_dir=state, profile="unit")

    def call(self, **args):
        return main_tools.question_answer(self.env, main_requests.Request("r1", "question_answer", args, "main-1", NOW))

    def test_the_answer_reaches_the_task_marked_as_the_main_threads(self):
        answer = self.call(topic="T7", answers={"scope": "全部"})
        self.assertTrue(answer["ok"], answer)
        self.assertIn("范围？ → 全部", answer["text"])
        self.assertEqual(self.targets, [(self.job.topic_id, "oc_chat")])
        self.rpc.answer.assert_called_once_with(17, {"scope": {"answers": ["全部", "补充说明：" + PROXY_NOTE]}})
        saved = json.loads(self.path.read_text())
        self.assertEqual((saved["status"], saved["supplement"], saved["answer_event"]),
                         ("answered", PROXY_NOTE, "main-r1"))
        again = self.call(topic="T7", answers={"scope": "全部"})
        self.assertFalse(again["ok"])
        self.assertIn("没有等主人回答的问题", again["text"])
        self.rpc.answer.assert_called_once()

    def test_a_label_not_in_the_list_sends_nothing(self):
        with self.assertRaisesRegex(ValueError, "不是"):
            self.call(topic="T7", answers={"scope": "一半"})
        self.rpc.answer.assert_not_called()
        self.assertEqual(json.loads(self.path.read_text())["status"], "pending")


if __name__ == "__main__":
    unittest.main()
