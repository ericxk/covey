"""The butler's tool channel (docs/butler.md 工具通道): the MCP server in its session (core/butler_mcp.py through
bin/covey_butler_mcp.py, run as a process), the Bridge's loop that runs the calls (app/butler_tools.py ButlerTools) and
the handlers of app/butler_tools.py TOOLS on records written into a temporary state directory."""
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
from covey.app import butler_tools
from covey.app.answers import answer_fields
from covey.app.butler_env import ButlerEnv, reply
from covey.app.butler_tools import PROXY_NOTE, REFUSED, ButlerTools
from covey.core import butler_mcp, butler_requests, butler
from covey.core.butler_tool_specs import NAMES
from covey.core.state import write_private_text
from covey.core.history_storage import append_history
from covey.core.topic_seen import SEEN_FILE

# The tool list the server is started with: the optional tools are off at 0 (unset means on).
SWITCHES_OFF = {"COVEY_KNOWLEDGE": "0", "COVEY_BUTLER_RULES": "0", "COVEY_PROJECTS": "0", "COVEY_PROJECT_TIDY": "off"}

CHAT = "oc_dm"
BUTLER = butler.butler_topic_id(CHAT)
NOW = time.time()


def runtime(chat=CHAT, logs=None):
    logs = [] if logs is None else logs
    return SimpleNamespace(log=logs.append, access=SimpleNamespace(owner_chat=lambda: chat),
                           shutdown=threading.Event())


def make_env(state, *, chat=CHAT, logs=None, topics=None):
    topics = topics or SimpleNamespace(harnesses={}, default="claude", target=lambda *args: None)
    return ButlerEnv(runtime(chat, logs), topics, None, None, state_dir=Path(state), profile="unit")


def butler_job(state, key="main-1", status="running", chat=CHAT):
    return write_job(state, key, chat_id=chat, topic_id=butler.butler_topic_id(chat), job_id=1, status=status)


def iso(seconds):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))


class McpServerTest(unittest.TestCase):
    """bin/covey_butler_mcp.py as Claude Code runs it: lines in, lines out."""

    def setUp(self):
        self.state = state_dir(self)

    def serve(self, messages, env):
        lines = "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in messages)
        result = subprocess.run([sys.executable, "-B", str(REPO / "bin/covey_butler_mcp.py")], input=lines,
                                capture_output=True, text=True, timeout=60,
                                env={"PATH": os.environ.get("PATH", ""), **env})
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        return {reply["id"]: reply for reply in map(json.loads, result.stdout.splitlines())}

    def bridge(self, answers, seen, stop):
        """A stand-in for the Bridge: answers each request it claims with ``answers[tool]``."""
        while not stop.is_set():
            for request in butler_requests.pending(self.state):
                if butler_requests.claim(self.state, request.id):
                    seen.append(request)
                    butler_requests.answer(self.state, request.id, answers[request.tool])
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
        ], {butler.STATE_ENV: str(self.state), butler.JOB_ENV: "main-1", **SWITCHES_OFF})
        self.assertEqual(sorted(replies), [1, 2, 3, 4, 5, 6, 7])  # Nothing for the notification.
        init = replies[1]["result"]
        self.assertEqual(init["protocolVersion"], "2025-06-18")
        self.assertIn("tools", init["capabilities"])
        self.assertEqual(init["serverInfo"]["name"], butler_mcp.SERVER["name"])
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
        self.assertEqual(replies[7]["error"]["code"], butler_mcp.NO_METHOD)
        # Each tools/call runs on a thread of its own: the Bridge may see them in either order.
        self.assertCountEqual([(r.tool, r.args, r.job_key) for r in seen],
                              [("topics_list", {"group": "all"}, "main-1"), ("topic_read", {"topic": "T9"}, "main-1")])
        self.assertEqual(list(butler_requests.folder(self.state).iterdir()), [])

    def test_an_older_protocol_is_answered_in_kind_and_an_unknown_one_with_the_newest(self):
        replies = self.serve([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
            {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}}], {})
        self.assertEqual(replies[1]["result"]["protocolVersion"], "2024-11-05")
        self.assertEqual(replies[2]["result"]["protocolVersion"], butler_mcp.PROTOCOLS[0])

    def test_without_its_environment_every_call_says_what_is_missing(self):
        replies = self.serve([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "topics_list", "arguments": {}}}],
            {butler.STATE_ENV: str(self.state), **SWITCHES_OFF})
        self.assertEqual(len(replies[1]["result"]["tools"]), len(NAMES))
        result = replies[2]["result"]
        self.assertTrue(result["isError"])
        self.assertIn(butler.JOB_ENV, result["content"][0]["text"])
        self.assertNotIn(butler.STATE_ENV, result["content"][0]["text"])
        self.assertFalse((self.state / butler_requests.DIR).exists())  # Nothing was written for the Bridge.

    def test_bad_lines_get_errors_and_the_server_keeps_going(self):
        server = butler_mcp.Server({})
        out = []
        ping = json.dumps({"jsonrpc": "2.0", "id": 9, "method": "ping"})
        butler_mcp.serve(server, ["not json\n", "[1]\n", ping + "\n"], out.append)
        codes = [json.loads(line).get("error", {}).get("code") for line in out]
        self.assertEqual(codes, [butler_mcp.PARSE_ERROR, butler_mcp.INVALID_REQUEST, None])

    def test_arguments_must_be_an_object(self):
        server = butler_mcp.Server({butler.STATE_ENV: "/s", butler.JOB_ENV: "k"},
                                 submit=mock.Mock(side_effect=AssertionError("not submitted")))
        answer = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                "params": {"name": "topics_list", "arguments": [1]}})
        self.assertTrue(answer["result"]["isError"])


class LoopTest(unittest.TestCase):
    def setUp(self):
        self.state = state_dir(self)
        self.logs = []
        self.handler = mock.Mock(return_value=reply(True, "好"))
        self.tools = ButlerTools(make_env(self.state, logs=self.logs), {"topics_list": self.handler})

    def request(self, tool="topics_list", job_key="main-1", args=None):
        request_id = f"r{len(list(butler_requests.folder(self.state).glob('*.json')))}"
        butler_requests._write(butler_requests.folder(self.state) / f"{request_id}.json",
                             {"id": request_id, "tool": tool, "args": args or {}, "job_key": job_key,
                              "created_at": time.time()})
        return request_id

    def answer(self, request_id):
        return json.loads((butler_requests.folder(self.state) / f"{request_id}.answer.json").read_text())

    def test_only_with_the_switch_a_thread(self):
        with mock.patch.dict(os.environ, {butler.ENV: "0"}):
            self.assertEqual(self.tools.threads(), ())
        with mock.patch.dict(os.environ, {butler.ENV: "1"}):
            self.assertEqual([name for name, _ in self.tools.threads()], ["main-tools"])

    def test_the_running_butler_job_is_served_and_logged(self):
        butler_job(self.state)
        request_id = self.request(args={"group": "all"})
        self.tools.tick()
        self.assertEqual(self.answer(request_id), reply(True, "好"))
        called = self.handler.call_args.args
        self.assertEqual((called[1].tool, called[1].args), ("topics_list", {"group": "all"}))
        self.assertRegex(self.logs[-1], r"^main tool name=topics_list ok=True ms=\d+ job=main-1$")
        self.tools.tick()
        self.handler.assert_called_once()  # Claimed once, answered once.

    def test_calls_from_anything_but_the_running_butler_job_are_refused(self):
        butler_job(self.state, "ended", status="done")
        write_job(self.state, "topic-job", chat_id=CHAT, topic_id="ab" * 16, job_id=2, status="running")
        butler_job(self.state, "other-chat", chat="oc_other")
        ids = [self.request(job_key=key) for key in ("ended", "topic-job", "other-chat", "missing", "")]
        self.tools.tick()
        self.assertEqual([self.answer(i)["text"] for i in ids], [REFUSED] * 5)
        self.handler.assert_not_called()

    def test_no_private_chat_yet_refuses(self):
        butler_job(self.state)
        tools = ButlerTools(make_env(self.state, chat=""), {"topics_list": self.handler})
        request_id = self.request()
        tools.tick()
        self.assertEqual(self.answer(request_id)["text"], REFUSED)

    def test_an_unknown_tool_is_refused(self):
        butler_job(self.state)
        request_id = self.request(tool="topic_start")
        self.tools.tick()
        answer = self.answer(request_id)
        self.assertFalse(answer["ok"])
        self.assertIn("topic_start", answer["text"])

    def test_a_handler_that_raises_is_answered_and_the_loop_goes_on(self):
        butler_job(self.state)
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
        butler_job(self.state)
        request_id = self.request()
        self.assertTrue(butler_requests.claim(self.state, request_id))  # A previous process took it and stopped.
        self.tools.recover()
        self.assertEqual(self.answer(request_id), butler_requests.UNCERTAIN)
        self.tools.tick()
        self.handler.assert_not_called()

    def test_run_recovers_then_polls_until_shutdown(self):
        butler_job(self.state)
        request_id = self.request()
        butler_requests.claim(self.state, request_id)
        fresh = self.request()
        runtime = self.tools.env.runtime
        runtime.shutdown.wait = lambda seconds: runtime.shutdown.set()  # One round.
        self.tools.run(SimpleNamespace())
        self.assertEqual(self.answer(request_id), butler_requests.UNCERTAIN)
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
        return butler_tools.TOOLS[name](self.env, butler_requests.Request("r1", name, args, "main-1", NOW))

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
        butler_job(self.state)

    def test_topics_list_groups(self):
        self.seed()
        listed = self.call("topics_list", group="all")
        lines = listed["text"].splitlines()
        self.assertTrue(listed["ok"])
        self.assertEqual([line.split(" · ")[0] for line in lines], ["T3", "T2", "T1", "T4"])
        self.assertEqual([line.split(" · ")[2] for line in lines], ["等你", "有结果没看", "在做", "更早"])
        self.assertIn("问：现在发版吗？", lines[0])
        self.assertIn("最后：查完了 没有异常", lines[1])  # Its reply's opening paragraph (core/checkpoint.py turn_summary).
        self.assertIn("在做：改代码", lines[2])
        self.assertIn("Claude Code", lines[2])
        self.assertTrue(lines[0].endswith("c3" * 16))
        self.assertNotIn(BUTLER, listed["text"])
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

    def test_topic_read_says_where_each_turns_whole_reply_is(self):
        kept = self.state / "result-files" / "k2.md"
        self.topic("c3" * 16, 3, "发版", jobs=[(1, {"status": "done", "user_text": "第一轮", "final_text": "旧"}),
                                               (2, {"status": "done", "user_text": "第二轮", "final_text": "做完了",
                                                    "result_file_path": str(kept), "result_file_kept": True})])
        text = self.call("topic_read", topic="T3", turns=5)["text"]
        self.assertIn(f"  结果开头：做完了\n  结果全文：{kept}", text)
        self.assertEqual(text.count("结果全文："), 1)  # The first turn kept none.

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

    def test_workspaces_list_says_which_device_a_workspace_is_on_and_what_runs_there(self):
        # COVEY_DEVICES=1 (docs/configuration.md 多设备): what topic_start checks (app/butler_topics.py device_binding), said up front.
        (self.state / "devices.json").write_text(json.dumps({"schema": 1, "control_dir": "/dev/shm/covey-ssh-tools", "devices": {
            "gpu1": {"title": "GPU 1", "ssh": {"host": "gpu1.example"}, "harnesses": ["claude", "agy"]},
            "team1": {"ssh": {"host": "team1.example"}, "spaces": ["team"]}}}))
        (self.state / "workspace-config.json").write_text(json.dumps({"default": "bridge", "workspaces": {
            "bridge": "/w/bridge", "gpu1-proj": "gpu1:/home/me/proj", "team-x": "team1:/srv/x", "gone": "gpu9:/x"}}))
        self.env.topics.apps = {"claude": SimpleNamespace(spec=SimpleNamespace(title="Claude Code"))}
        self.env.runtime.access.allows_device = lambda chat, device: chat == CHAT and device.alias == "gpu1"
        with mock.patch.dict(os.environ, {"COVEY_DEVICES": "1", "COVEY_DEVICES_FILE": str(self.state / "devices.json")}):
            lines = self.call("workspaces_list")["text"].splitlines()
        self.assertEqual(lines, ["bridge（默认） · /w/bridge",
                                 "gpu1-proj · gpu1:/home/me/proj · 设备 GPU 1：只能用 Claude Code（harness 填 claude）、"
                                 "agy（本机器人没启用），原目录运行",
                                 "team-x · team1:/srv/x · 设备 team1：这个聊天不能用（devices.json 的 spaces）",
                                 "gone · gpu9:/x · 设备 gpu9：devices.json 里没有这台设备，开不了话题"])
        # Off, each is the line it was.
        self.assertEqual(self.call("workspaces_list")["text"].splitlines()[1:], [
            "gpu1-proj · gpu1:/home/me/proj", "team-x · team1:/srv/x", "gone · gpu9:/x"])

    def test_topic_priority(self):
        self.topic("a1" * 16, 1, "修首页", jobs=[(1, {"status": "done"})])
        answer = self.call("topic_priority", topic="t1")
        self.assertTrue(answer["ok"])
        saved = json.loads((self.state / butler_tools.PRIORITY).read_text())
        self.assertEqual(list(saved), ["a1" * 16])
        self.assertGreaterEqual(saved["a1" * 16], NOW)

    def test_topic_priority_can_take_a_topic_off_the_board(self):
        from covey.app import pins
        from covey.core.topic_seen import PINNED_TOPICS_ENV
        self.topic("a1" * 16, 1, "修首页", jobs=[(1, {"status": "done"})])
        with mock.patch.dict(os.environ, {PINNED_TOPICS_ENV: "0"}):
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
        self.assertFalse((self.state / butler_tools.PRIORITY).exists())  # Not moved up.

    def test_topic_priority_describes_the_board_order(self):
        # The butler explains the board from this note: it names the groups in pins.GROUPS order (D2).
        from covey.app import pins
        from covey.core.butler_tool_specs import TOOLS
        note = next(tool["description"] for tool in TOOLS if tool["name"] == "topic_priority")
        names = {"waiting": "等你", "running": "在做", "result": "有结果"}
        self.assertEqual(" → ".join(names[group] for group in pins.GROUPS), "等你 → 在做 → 有结果")
        self.assertIn("等你 → 在做 → 有结果", note)


class ReposAndMemoryToolsTest(unittest.TestCase):
    """repos_list and the memory tools (docs/butler.md 记忆), each run as the older tools were until their own handlers
    are there (app/repos_turn.py repos_list, app/butler_knowledge.py memory_*), and the older names an earlier session
    still calls (core/butler_tool_specs.py RENAMED_TOOLS) taken with their arguments as they were."""

    setUp = HandlerTest.setUp

    def call(self, name, **args):
        from covey.app import butler_memory
        handlers = {**butler_tools.TOOLS, **butler_memory.TOOLS}  # As app/unified.py puts them together.
        return handlers[name](self.env, butler_requests.Request("r1", name, args, "main-1", NOW))

    def test_repos_list_reads_the_repo_map_and_without_one_the_saved_workspaces(self):
        (self.state / "workspace-config.json").write_text(json.dumps({"default": "bridge", "workspaces": {"bridge": "/w/bridge"}}))
        answer = self.call("repos_list")
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["text"].splitlines(), ["仓库地图还没有建，下面是登记的工作区（名字可以写进 repos）：", "bridge（默认） · /w/bridge"])
        self.assertEqual((answer["data"]["repos"], answer["data"]["count"], answer["data"]["workspaces"]["default"]), ([], 0, "bridge"))
        (self.state / "repos.json").write_text(json.dumps({"version": 1, "repos": [
            {"name": "covey", "path": "/w/covey", "note": "核心", "status": "active"},
            {"name": "old", "path": "/w/old", "status": "frozen"}]}, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(self.call("repos_list")["text"].splitlines(), ["covey · 可写 · 用途：核心 · /w/covey", "old · 已冻结 · /w/old"])

    def test_repos_list_says_app_repos_turns_answer_as_a_list_for_the_calling_jobs_topic(self):
        """Seam W2 ↔ W1 (T870): the Bridge tool hands app/repos_turn.py repos_list a context of its own (the home of the
        default Harness, the topic of the job that called), never the ButlerEnv, and says its dict as lines; the dict stays
        the answer's data."""
        home = self.state.parent / "home"
        home.mkdir()
        self.harnesses["claude"].context = SimpleNamespace(default_workdir=str(home))
        (self.state / "repos.json").write_text(json.dumps({"version": 1, "repos": [
            {"name": "covey", "path": "/w/covey", "note": "核心", "write_mode": "inplace"},
            {"name": "web", "path": "/w/web", "status": "readonly"}]}, ensure_ascii=False), encoding="utf-8")
        lead = "1e" * 16
        write_topic(self.state, topic_id=lead, chat_id=CHAT, harness_id="claude", repos=[
            {"name": "covey", "path": "/w/covey", "mode": "write", "worktree": "/wt/T1-x/covey", "source": "declared"},
            {"name": "notes", "path": "/w/notes", "mode": "read", "source": "touched"}])
        write_job(self.state, "lead-1", chat_id=CHAT, topic_id=lead, job_id=1, status="running")
        answer = butler_tools.TOOLS["repos_list"](self.env, butler_requests.Request("r1", "repos_list", {}, "lead-1", NOW))
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["text"].splitlines(), [
            f"主目录：{home} · 仓库地图：{self.state / 'repos.md'}",
            "covey · 可写 · 原地改 · 用途：核心 · /w/covey · 本话题在改", "web · 只读 · /w/web",
            "本话题还用到（地图里没有）：", "notes · /w/notes · 本话题在读"])
        data = answer["data"]
        self.assertEqual((data["home"], data["count"], data["map"]), (str(home), 2, str(self.state / "repos.md")))
        self.assertEqual([(row["name"], row.get("used"), row.get("worktree")) for row in data["repos"]],
                         [("covey", "write", "/wt/T1-x/covey"), ("web", None, None)])
        self.assertEqual(data["unmapped"], [{"name": "notes", "path": "/w/notes", "used": "read", "worktree": ""}])
        # A job whose record is gone: the map alone, no topic's repositories.
        alone = butler_tools.TOOLS["repos_list"](self.env, butler_requests.Request("r2", "repos_list", {}, "gone-1", NOW))
        self.assertEqual(alone["text"].splitlines()[1:], ["covey · 可写 · 原地改 · 用途：核心 · /w/covey", "web · 只读 · /w/web"])
        self.assertEqual(alone["data"]["unmapped"], [])
        # An empty map and no saved workspace: what the topic uses is still said.
        (self.state / "repos.json").write_text(json.dumps({"version": 1, "repos": []}), encoding="utf-8")
        empty = butler_tools.TOOLS["repos_list"](self.env, butler_requests.Request("r3", "repos_list", {}, "lead-1", NOW))
        self.assertTrue(empty["ok"], empty)
        self.assertIn("covey · /w/covey · 本话题在改", empty["text"])

    def test_the_older_names_run_as_the_new_tools(self):
        from covey.core.butler_tool_specs import current_name
        self.assertEqual([current_name(name) for name in ("workspaces_list", "principle_save", "knowledge_save", "principle_delete",
                                                          "knowledge_update", "knowledge_drop", "principles_list", "knowledge_search")],
                         ["repos_list", "memory_save", "memory_save", "memory_revise", "memory_revise", "memory_revise",
                          "memory_search", "memory_search"])

    def test_memory_save_writes_the_store_and_an_older_calls_arguments_are_said_in_the_new_words(self):
        """With package M2 in (T870 Wave 2B) the memory tools are app/butler_knowledge.py's on core/memstore.py: the code back."""
        from covey.core import memstore
        saved = self.call("memory_save", text="回复先给结论", kind="rule", scope="global")
        self.assertTrue(saved["ok"], saved)
        self.assertTrue(saved["text"].startswith("记下了："), saved)
        self.assertRegex(saved["data"]["code"], memstore.CODE_RE)
        entry = memstore.find(self.state, saved["data"]["ref"])
        self.assertEqual((entry.kind, entry.scope, entry.text), ("rule", "global", "回复先给结论"))
        old = self.call("memory_save", scope="owner", text="不在根目录递归搜索")  # principle_save's arguments.
        self.assertTrue(old["ok"], old)
        self.assertEqual(memstore.find(self.state, old["data"]["ref"]).kind, "rule")
        listed = self.call("memory_search")["text"]  # principles_list's: no query, the latest.
        self.assertIn(saved["data"]["code"], listed)
        self.assertIn(old["data"]["code"], listed)
        self.assertIn("加 project", self.call("memory_save", text="x", kind="rule", scope="project")["text"])
        self.assertFalse(self.call("memory_save", text="x", kind="practice", scope="global")["ok"])

    def test_memory_revise_changes_or_retires_an_entry_by_its_code_or_its_old_arguments(self):
        from covey.core import memstore
        first = self.call("memory_save", text="用 Codex 跑长任务", kind="rule", scope="global")["data"]
        changed = self.call("memory_revise", key=first["code"], action="update", text="长任务用 Codex")
        self.assertTrue(changed["ok"], changed)
        self.assertEqual(memstore.find(self.state, first["ref"]).text, "长任务用 Codex")
        gone = self.call("memory_revise", id=first["ref"])  # principle_delete's arguments: retired.
        self.assertTrue(gone["ok"], gone)
        self.assertNotEqual(memstore.find(self.state, first["ref"]).status, "active")
        self.assertNotIn(first["code"], self.call("memory_search")["text"])
        self.assertFalse(self.call("memory_revise", key="R7K2", action="retire")["ok"])

    def test_the_memory_tools_hand_on_to_their_own_handlers_once_they_are_there(self):
        calls = []

        def made(env, args):
            calls.append(args)
            return {"ok": True, "text": "记下了 R7K2", "data": {"code": "R7K2"}}
        with mock.patch("covey.app.butler_knowledge.memory_save", create=True, side_effect=made):
            self.assertEqual(self.call("memory_save", text="t", kind="note", scope="global")["text"], "记下了 R7K2")
            self.call("memory_save", scope="workspace", workspace="covey", kind="pitfall", text="坑")  # knowledge_save's.
        self.assertEqual(calls, [{"text": "t", "kind": "note", "scope": "global"},
                                 {"text": "坑", "kind": "note", "scope": "global", "repos": ["covey"]}])
        with mock.patch("covey.app.butler_knowledge.memory_revise", create=True, side_effect=made):
            self.call("memory_revise", id="k1a2b3c", reason="作废")  # knowledge_drop's.
        self.assertEqual(calls[-1], {"key": "k1a2b3c", "action": "retire", "why": "作废"})
        # memory_search's arguments go on as called (T870, for M2): what narrows a search is not cut to query and limit.
        with mock.patch("covey.app.butler_knowledge.memory_search", create=True, side_effect=made):
            self.call("memory_search", query="部署", limit=3, scope="project", project="P1", kinds=["rule"])
        self.assertEqual(calls[-1], {"query": "部署", "limit": 3, "scope": "project", "project": "P1", "kinds": ["rule"]})

    def test_the_leads_project_remember_hands_rule_and_note_on_as_they_are(self):
        seen = []
        from covey.app import butler_memory
        with mock.patch.dict(butler_tools.project_tools.TOOLS, {"project_remember": lambda env, request: seen.append(request.args) or {
                "ok": True, "text": "ok", "data": {}}}):
            butler_memory.TOOLS["project_remember"](self.env, butler_requests.Request("r1", "project_remember", {
                "kind": "rule", "title": "部署", "text": "改完就部署"}, "lead-1", NOW))
            butler_memory.TOOLS["project_remember"](self.env, butler_requests.Request("r2", "project_remember", {
                "kind": "fact", "text": "旧的"}, "lead-1", NOW))
        # M3's project_tools takes rule and note itself (MEMORY_KINDS): handed on as they are, never said in older kinds.
        self.assertEqual(seen, [{"kind": "rule", "title": "部署", "text": "改完就部署"}, {"kind": "fact", "text": "旧的"}])
        self.assertTrue({"rule", "note"} <= set(butler_tools.project_tools.MEMORY_KINDS))


class OpeningArgumentsTest(unittest.TestCase):
    """What the model reads of topic_start and project_topic_start: a Harness, model or budget the owner names must be
    passed on (2026-11-16: "用 Claude Code" opened a topic on the default Harness)."""

    def properties(self, tools, name):
        return next(tool for tool in tools if tool["name"] == name)["input_schema"]["properties"]

    def test_the_harness_model_and_budget_say_they_are_required_when_the_owner_names_them(self):
        from covey.core.butler_tool_specs import PROJECT_TOOLS, TOOLS
        for tools, name in ((TOOLS, "topic_start"), (PROJECT_TOOLS, "project_topic_start")):
            properties = self.properties(tools, name)
            for field in ("harness", "model", "effort"):
                text = properties[field]["description"]
                self.assertIn("点名", text, (name, field))
                self.assertIn("必须填", text, (name, field))
                self.assertIn("没点名就不填", text, (name, field))
            self.assertIn("claude", properties["harness"]["description"])
            self.assertIn("Claude Code", properties["harness"]["description"])

    def test_the_butlers_rules_say_to_pass_on_what_the_owner_names(self):
        from covey.app.prompts import BUTLER_RULES
        rule = next(rule for rule in BUTLER_RULES if "点名" in rule)
        for words in ("Harness", "模型", "思考预算", "topic_start", "没点名就不填"):
            self.assertIn(words, rule)


class GeminiSchemaTest(unittest.TestCase):
    """Every tool any Bridge MCP server lists, under every switch, as Gemini takes a function declaration: agy hands the
    butler's and a lead's tools to it, and it refuses the whole request over one of them (T839: ``waiting_on``'s enum
    ``["owner", ""]`` failed each turn of every lead on agy with 400 ``enum[1]: cannot be empty``)."""

    TYPES = {"string", "number", "integer", "boolean", "array", "object"}
    # Each switch of core/butler_tool_specs.py ``listed`` and ``lead_tools``, off and on.
    SWITCHES = {"COVEY_PROJECT_LEAD": ("", "p1"), "COVEY_KNOWLEDGE": ("0", "1"), "COVEY_HISTORY_CMD": ("", "/bin/true"),
                "COVEY_BUTLER_RULES": ("0", "1"), "COVEY_PROJECTS": ("0", "1"), "COVEY_PROJECT_TIDY": ("off", "suggest"),
                "COVEY_PROJECT_TRIAGE": ("0", "1"), "COVEY_COLLAB": ("0", "1"), "COVEY_PROJECT_AUTOWAKE": ("0", "1")}

    def tools(self):
        import itertools
        from covey.backends.codex.workspace_reader import TOOLS as READER
        found = {}
        for values in itertools.product(*self.SWITCHES.values()):
            for tool in butler_mcp.tool_list(dict(zip(self.SWITCHES, values))):
                found.setdefault(json.dumps(tool, sort_keys=True), tool)
        return [*found.values(), *READER]

    def faults(self, node, path):
        """What Gemini refuses in the schema ``node`` and those under it."""
        if not isinstance(node, dict):
            return [f"{path}: not an object"]
        faults, kind = [], node.get("type")
        if "enum" in node:
            values = node["enum"]
            if kind != "string" or not isinstance(values, list) or not values:
                faults.append(f"{path}.enum: a non-empty list on a string")
            faults += [f"{path}.enum[{index}]: {value!r} is not a non-empty string" for index, value in enumerate(values or ())
                       if not isinstance(value, str) or not value.strip()]
        if "type" in node and kind not in self.TYPES:
            faults.append(f"{path}.type: {kind!r}")
        if kind == "array" and "items" not in node:
            faults.append(f"{path}: an array with no items")
        properties = node.get("properties") or {}
        faults += [f"{path}.required: {name!r} is not a property" for name in node.get("required") or () if name not in properties]
        for name, each in properties.items():
            faults += self.faults(each, f"{path}.properties[{name}]")
        for key in ("items", "additionalProperties"):
            if isinstance(node.get(key), dict):
                faults += self.faults(node[key], f"{path}.{key}")
        for index, each in enumerate(node.get("anyOf") or ()):
            faults += self.faults(each, f"{path}.anyOf[{index}]")
        return faults

    def test_no_tool_has_what_gemini_refuses(self):
        tools = self.tools()
        self.assertIn("project_checklist", {tool["name"] for tool in tools})  # The switches reach the lead's 协作 tools.
        self.assertIn("project_memory_revise", {tool["name"] for tool in tools})
        for tool in tools:
            with self.subTest(tool=tool["name"]):
                self.assertEqual(tool["inputSchema"].get("type"), "object")
                self.assertEqual(self.faults(tool["inputSchema"], "parameters"), [])

    def test_the_check_finds_an_empty_enum_value(self):
        schema = {"type": "object", "required": ["a"], "properties": {"items": {"type": "array", "items": {
            "type": "object", "properties": {"waiting_on": {"type": "string", "enum": ["owner", ""]}}}}}}
        self.assertEqual(self.faults(schema, "parameters"),
                         ["parameters.required: 'a' is not a property",
                          "parameters.properties[items].items.properties[waiting_on].enum[1]: '' is not a non-empty string"])


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
        env = ButlerEnv(runtime("oc_chat"),
                      SimpleNamespace(harnesses={}, target=lambda *args: (self.app, self.ctx_topic, {})),
                      None, None, state_dir=state, profile="unit")
        request = butler_requests.Request("r2", "question_answer", {"topic": "T8", "answers": {"q": "一半"}}, "main-1", NOW)
        answer = butler_tools.question_answer(env, request)
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

        self.env = ButlerEnv(runtime("oc_chat"), SimpleNamespace(harnesses={}, target=target), None, None,
                           state_dir=state, profile="unit")

    def call(self, **args):
        return butler_tools.question_answer(self.env, butler_requests.Request("r1", "question_answer", args, "main-1", NOW))

    def test_the_answer_reaches_the_task_marked_as_the_butlers(self):
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
