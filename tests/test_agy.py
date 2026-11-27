"""Antigravity (agy) over its headless stream-json: argv, events, verified completion and failure codes."""
import dataclasses
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from support import FIXTURES
from covey.app.unified import UnifiedBridgeApp
from covey.backends.agy.bridge import (AntigravityEventAdapter, CONVERSATIONS, NOT_LOGGED_IN, models_from_listing, tool_inputs,
                                      tool_summary)
from covey.app import idle_compaction
from covey.core.harness import RunCallbacks, RunInputs, RunRequest, backend_capabilities
from covey.core.steer import SteerNotReady, SteerRefused

RUN = "33333333-3333-4333-8333-333333333333"
SID = "055a398f-db14-4c5f-abbb-1bf03f8120a7"
FAKE = FIXTURES / "harness/agy_stream.py"


class AntigravityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        clean = {k: v for k, v in os.environ.items() if not k.startswith("FAKE_AGY")}
        patcher = mock.patch.dict(os.environ, {**clean, "HOME": str(self.root),
                                               "FAKE_AGY_RECORD": str(self.root / "record.json")}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.app = UnifiedBridgeApp(script_path=Path(__file__).resolve().parents[1] / "bin/covey_bridge.py")
        self.app.build_context(self.app.build_parser().parse_args(
            ["--workdir", str(self.root), "--state-dir", str(self.root / "state"), "--profile", "unit"]))
        self.backend = self.app.apps["agy"].backend
        self.ctx = dataclasses.replace(self.app.harnesses["agy"].context, backend_cmd=[sys.executable, "-I", str(FAKE)],
                                       model="gemini-3.8-pro-high", effort="high")

    def run_agy(self, scenario, session=None, cancel=None, ctx=None):
        os.environ["FAKE_AGY_SCENARIO"] = scenario
        events, replies, sessions = [], [], []
        result = self.backend.run(RunRequest(ctx or self.ctx, "做个任务", session_id=session, callbacks=RunCallbacks(
            event=lambda kind, data, control: events.append((kind, data)), session=sessions.append,
            verified_reply=replies.append), inputs=RunInputs(cancel or threading.Event(), run_id=RUN)))
        path = self.root / "record.json"
        record = json.loads(path.read_text()) if path.exists() else {}
        for leftover in (path, path.with_name("record.json.all")):
            if leftover.exists():
                leftover.unlink()
        return result, events, [r for r in replies if r is not None], sessions, record

    def test_completed_turn_is_verified_and_its_events_are_public_only(self):
        result, events, replies, sessions, record = self.run_agy("ok")
        self.assertFalse(result.is_error, result.text)
        self.assertEqual((result.text, result.session_id, sessions), ("完成", SID, [SID]))
        self.assertTrue(result.outcome.resume_allowed)
        self.assertEqual(replies[-1].backend, "agy")
        self.assertEqual(replies[-1].fingerprint, result.outcome.reply_fingerprint)
        published = json.dumps(events, ensure_ascii=False)
        for private in ("PRIVATE_OUTPUT", "PRIVATE_SUBAGENT"):
            self.assertNotIn(private, published)
        tools = [d for k, d in events if k == "tool"]
        self.assertEqual([(t["name"], t["status"]) for t in tools], [("bash", "running"), ("bash", "done")])
        self.assertEqual(tools[0]["detail"], "ls")
        self.assertIn(("progress_note", "先看一下目录"), [(k, d.get("text")) for k, d in events])
        self.assertEqual([k for k, _d in events].count("model_output"), 1)
        usage = [d for k, d in events if k == "token_usage"]
        self.assertEqual(len(usage), 2)  # One a model request, the result's sum not added again.
        self.assertEqual((usage[-1]["output_tokens"], usage[-1]["input_tokens"], usage[-1]["cache_read_tokens"]),
                         (132, 15436 + 11130, 11130))
        self.assertEqual((usage[-1]["measured_output_tokens"], usage[-1]["generation_seconds"]), (132, 5.0))
        self.assertEqual(events[-1][0], "trace_finished")
        self.assertEqual(json.loads(record["stdin"]), {"event": "user", "message": {"content": "做个任务"}})
        self.assertEqual(record["argv"], ["--output-format", "stream-json", "--input-format", "stream-json",
                                          "--disable-slash-commands", "--dangerously-skip-permissions",
                                          "--model", "gemini-3.8-pro-high", "--effort", "high"])
        self.assertEqual(Path(record["cwd"]).resolve(), Path(self.ctx.workdir).resolve())

    def test_the_answer_is_what_the_model_wrote_after_its_last_tool_subagent_or_system_message(self):
        # agy's result.response joins the progress lines and the waits before the answer (T588 #5298, #5309).
        result, events, replies, *_rest = self.run_agy("waits")
        self.assertFalse(result.is_error, result.text)
        self.assertEqual((result.text, replies[-1].text), ("完成", "完成"))
        notes = [d.get("text") for k, d in events if k == "progress_note"]
        self.assertIn("正在等待子 Agent。\n", notes)
        self.assertIn(("agent", "done"), [(d["name"], d["status"]) for k, d in events if k == "tool" and d["id"] == "step-3"])

    def test_an_answer_that_is_not_the_end_of_the_response_falls_back_to_the_response(self):
        from covey.backends.agy.bridge import StreamRows
        rows = StreamRows(None, AntigravityEventAdapter(lambda *a: None), None)
        rows.sid, rows.result = SID, {"response": "全文"}
        rows.kinds, rows.texts = {0: "agent_response"}, {0: "别的"}
        self.assertEqual(rows.reply(), "全文")
        rows.texts = {}
        self.assertEqual(rows.reply(), "全文")

    def test_the_butlers_turn_gets_the_bridge_tools_through_a_home_of_its_own(self):
        """T614: agy reads MCP servers from ~/.gemini/config/mcp_config.json alone. The butler's turn runs with a home
        of its own: every entry a link to the real one but that file, which adds the Bridge's server (eager tools, a
        wait above the server's own 120 seconds) to the owner's; the real file is left alone and the home goes."""
        from covey.core.butler import mcp_config
        real = self.root / ".gemini" / "config"
        real.mkdir(parents=True)
        owner = {"mcpServers": {"mine": {"command": "my-server", "args": ["--x"]}}}
        (real / "mcp_config.json").write_text(json.dumps(owner))
        (real / "config.json").write_text("{}")
        (self.root / ".gemini" / "antigravity-cli").mkdir()
        (self.root / ".gitconfig").write_text("[user]\n")
        config = mcp_config(Path(__file__).resolve().parents[1], self.root / "state", "om_1-1", sys.executable)
        ctx = dataclasses.replace(self.ctx, options={**self.ctx.options, "mcp_config": config})
        result, _events, _replies, _sessions, record = self.run_agy("ok", ctx=ctx)
        self.assertFalse(result.is_error, result.text)
        home = Path(record["home"])
        self.assertNotEqual(home, self.root)
        self.assertEqual(home.parent, self.root / "state" / "tmp")
        self.assertFalse(home.exists())  # Removed with the turn, its links never followed.
        self.assertEqual(json.loads((real / "mcp_config.json").read_text()), owner)
        self.assertTrue((real / "config.json").exists() and (self.root / ".gitconfig").exists())
        servers = record["mcp"]["mcpServers"]
        self.assertEqual(servers["mine"], owner["mcpServers"]["mine"])
        bridge = servers["bridge"]
        self.assertEqual((bridge["command"], bridge["args"], bridge["env"]),
                         (config["mcpServers"]["bridge"]["command"], config["mcpServers"]["bridge"]["args"],
                          config["mcpServers"]["bridge"]["env"]))
        self.assertEqual(bridge["timeoutSeconds"], 150)
        self.assertIn("topic_start", bridge["tools"])
        self.assertTrue(all(tool == {"eager": True} for tool in bridge["tools"].values()))

    def test_a_private_home_links_everything_but_the_mcp_file_and_says_when_the_owners_file_is_not_json(self):
        from covey.backends.agy.home import PrivateHome
        real = self.root / ".gemini" / "config"
        real.mkdir(parents=True)
        (real / "mcp_config.json").write_text('{"mcpServers": {"mine": {"command": "x"},},}  // agy takes this')
        (self.root / ".gemini" / "antigravity-cli").mkdir()
        server = {"command": sys.executable, "args": ["-c", "pass"]}  # Answers nothing: its tools are not eager.
        home = PrivateHome(self.root / "state", self.root, {"mcpServers": {"bridge": server}}, dict(os.environ))
        with home as path:
            self.assertTrue((path / ".gemini" / "antigravity-cli").is_symlink())
            self.assertTrue((path / ".gemini" / "config").is_dir() and not (path / ".gemini" / "config").is_symlink())
            self.assertTrue((path / ".gitconfig").is_symlink() or not (self.root / ".gitconfig").exists())
            data = json.loads((path / ".gemini" / "config" / "mcp_config.json").read_text())
            self.assertEqual(list(data["mcpServers"]), ["bridge"])
            self.assertNotIn("tools", data["mcpServers"]["bridge"])
            self.assertIn("not plain JSON", home.note)
        self.assertFalse(path.exists())
        self.assertTrue((self.root / ".gemini" / "antigravity-cli").is_dir())
        (real / "mcp_config.json").write_text("")  # As agy leaves it with no server: nothing to say.
        home = PrivateHome(self.root / "state", self.root, {"mcpServers": {"bridge": server}}, dict(os.environ))
        with home:
            self.assertEqual(home.note, "")

    def test_a_topics_turns_run_in_a_home_of_the_same_name(self):
        """T776: agy writes its HOME into the start of its prompt (App Data Directory, every skill's path). A name of
        its own each turn made the butler's and the leads' first request of a turn read nothing from the cache; each
        turn of a topic now runs in the same one."""
        from covey.backends.agy.home import home_name
        from covey.core.butler import mcp_config
        (self.root / ".gemini" / "antigravity-cli").mkdir(parents=True)
        config = mcp_config(Path(__file__).resolve().parents[1], self.root / "state", "om_1-1", sys.executable)
        ctx = dataclasses.replace(self.ctx, topic_id="topic-1", options={**self.ctx.options, "mcp_config": config})
        homes = [Path(self.run_agy("ok", ctx=ctx)[-1]["home"]) for _ in range(2)]
        self.assertEqual(homes, [self.root / "state" / "tmp" / home_name("topic-1", self.root)] * 2)
        self.assertFalse(homes[0].exists())  # Removed with each turn all the same.
        other = Path(self.run_agy("ok", ctx=dataclasses.replace(ctx, topic_id="topic-2"))[-1]["home"])
        self.assertNotEqual(other, homes[0])
        self.assertEqual(other.parent, homes[0].parent)

    def test_a_turn_takes_a_home_of_its_own_while_another_of_the_topic_holds_its_name(self):
        from covey.backends.agy.home import PrivateHome, home_name
        (self.root / ".gemini" / "antigravity-cli").mkdir(parents=True)
        server = {"command": sys.executable, "args": ["-c", "pass"]}

        def home(key="topic-1"):
            return PrivateHome(self.root / "state", self.root, {"mcpServers": {"bridge": server}}, dict(os.environ), key)
        with home() as first:
            self.assertEqual(first.name, home_name("topic-1", self.root))
            with home() as second:  # An idle compaction not stopped yet: the turn still runs, in a name of its own.
                self.assertNotEqual(second, first)
                self.assertTrue((second / ".gemini" / "config" / "mcp_config.json").exists())
            self.assertTrue(first.is_dir())  # The other's end leaves this one's alone.
        self.assertFalse(first.exists())
        (first / ".gemini").mkdir(parents=True)  # What a killed turn left: made again, empty.
        (first / "left").write_text("x")
        with home() as again:
            self.assertEqual(again, first)
            self.assertFalse((again / "left").exists())
        with home("") as own:  # No topic: a name of its own, as before.
            self.assertNotEqual(own, first)
            self.assertTrue(own.name.startswith("agy-home-"))

    def test_the_results_usage_counts_only_in_a_conversation_the_turn_started(self):
        """agy's result.usage is the whole conversation's: in one the turn goes on with it is every turn's before (T776:
        a butler turn stopped by a supplement before its first request ended took 164M as its own)."""
        _result, events, *_rest = self.run_agy("result-usage")
        usage = [d for k, d in events if k == "token_usage"]
        self.assertEqual((usage[-1]["output_tokens"], usage[-1]["input_tokens"], usage[-1]["cache_read_tokens"]),
                         (132, 15436 + 11130, 11130))
        # The result's total is no request's context: never a context_tokens from it (context=False).
        self.assertTrue(all("context_tokens" not in d and "context_window" not in d for d in usage))
        result, events, *_rest = self.run_agy("result-usage", session=SID)
        self.assertFalse(result.is_error, result.text)
        self.assertEqual([d for k, d in events if k == "token_usage"], [])

    BUTLER_ENV = {"COVEY_BUTLER_RULES": "1", "COVEY_PROJECTS": "1", "COVEY_KNOWLEDGE": "1",
                  "COVEY_HISTORY_CMD": "/absolute/history", "COVEY_PROJECT_TIDY": "suggest"}

    def butler_home(self, **env):
        """The butler's server in a private home with BUTLER_ENV's switches on: its eager tools, in order, and the
        names it listed."""
        from covey.backends.agy.home import PrivateHome
        from covey.core.butler import mcp_config
        (self.root / ".gemini" / "antigravity-cli").mkdir(parents=True, exist_ok=True)
        with mock.patch.dict(os.environ, {**self.BUTLER_ENV, **env}):
            config = mcp_config(Path(__file__).resolve().parents[1], self.root / "state", "om_1-1", sys.executable)
            home = PrivateHome(self.root / "state", self.root, config, dict(os.environ))
            with home as path:
                bridge = json.loads((path / ".gemini" / "config" / "mcp_config.json").read_text())["mcpServers"]["bridge"]
        self.assertTrue(all(tool == {"eager": True} for tool in (bridge.get("tools") or {}).values()))
        return list(bridge.get("tools") or {}), home.listed["bridge"]

    @staticmethod
    def sizes(environ):
        from covey.backends.agy.home import tool_size
        from covey.core.butler_tool_specs import listed
        return {tool["name"]: tool_size({"description": tool["description"], "inputSchema": tool["input_schema"]})
                for tool in listed(environ)}

    def test_the_butlers_tools_that_work_with_topics_are_loaded_up_front_and_the_others_named(self):
        """T750: agy fits a turn's skills and MCP tools in one budget and drops the largest tool whole. With all 21 of
        the butler's eager beside 62 skills topic_start was gone, and the butler forked a finished topic for each new
        matter. Eager are those that hand work to topics, then the others but SECONDARY, within EAGER_BYTES."""
        from covey.backends.agy.home import EAGER_BYTES
        from covey.core.butler_tool_specs import SECONDARY
        eager, names = self.butler_home()
        sizes = self.sizes(self.BUTLER_ENV)
        self.assertEqual(names, list(sizes))
        self.assertEqual(eager[:4], ["topic_start", "topic_send", "topic_fork", "project_send"])
        working = [name for name in names if name not in SECONDARY]
        self.assertLessEqual(sum(sizes[name] for name in working), EAGER_BYTES,
                             "the butler's tools that work with topics outgrew EAGER_BYTES: agy would not load the last "
                             "of them up front; make their descriptions shorter, or measure agy's room again")
        self.assertEqual(eager[:len(working)], sorted(working, key=lambda name: name not in {
            "topic_start", "topic_send", "topic_fork", "project_send"}))
        self.assertLessEqual(sum(sizes[name] for name in eager), EAGER_BYTES)
        self.assertFalse({"knowledge_save", "knowledge_search", "history_search", "projects_tidy"} & set(eager))

    def test_a_lower_room_keeps_the_tools_that_hand_work_on_and_none_names_them_all(self):
        sizes = self.sizes(self.BUTLER_ENV)
        room = sizes["topic_start"] + sizes["topic_send"]
        eager, names = self.butler_home(COVEY_AGY_EAGER_BYTES=str(room))
        self.assertEqual(eager, ["topic_start", "topic_send"])
        eager, names = self.butler_home(COVEY_AGY_EAGER_BYTES="0")
        self.assertEqual((eager, len(names)), ([], len(sizes)))
        eager, _names = self.butler_home(COVEY_AGY_EAGER_BYTES="lots")  # Not a number: the default.
        self.assertIn("project_send", eager)

    def test_a_leads_tools_are_all_loaded_up_front(self):
        """All but SECONDARY (topics_switch, T786: a lead proposes a batch switch rarely; agy names it when the room is out)."""
        from covey.backends.agy.home import EAGER_BYTES
        from covey.core.butler_tool_specs import SECONDARY
        lead = {"COVEY_PROJECT_LEAD": "p1", "COVEY_PROJECT_TRIAGE": "1", "COVEY_COLLAB": "1", "COVEY_PROJECT_AUTOWAKE": "1"}
        sizes = self.sizes(lead)
        self.assertLessEqual(sum(size for name, size in sizes.items() if name not in SECONDARY), EAGER_BYTES,
                             "a lead's tools outgrew EAGER_BYTES: agy would name the last of them only")
        self.assertIn("topics_switch", sizes)

    BLOCK = ("<mcp_servers>\nEach MCP server has a directory `<appDataDir>/mcp/<serverName>` containing tool schemas.\n"
             "```\n# <serverName>\nEager:\n<toolName>\nLazy:\n<toolName>\n```\n"
             "# bridge\nEager:\ntopics_list\n{eager}Lazy:\nknowledge_search\n# mine\nEager:\nmy_tool\n</mcp_servers>")

    def conversation(self, *blobs):
        """A conversation's database as agy keeps it: its model requests' rows (table gen_metadata), oldest first."""
        import contextlib
        import sqlite3
        path = self.root / CONVERSATIONS / f"{SID}.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(sqlite3.connect(path)) as db:
            db.execute("CREATE TABLE gen_metadata (idx integer, data blob, size integer NOT NULL DEFAULT 0, PRIMARY KEY (idx))")
            db.executemany("INSERT INTO gen_metadata (idx, data) VALUES (?, ?)", enumerate(blobs))
            db.commit()
        return path

    def test_left_out_names_the_bridge_tools_agys_latest_request_did_not_have(self):
        from covey.backends.agy.bridge import left_out
        listed = {"bridge": ["topics_list", "topic_start", "topic_send", "knowledge_search"]}
        self.assertEqual(left_out(self.root, SID, listed), {})  # No conversation: nothing to say.
        older = b"\x12\x08proto" + self.BLOCK.format(eager="topic_start\n").encode() + b"\x1a\x00"
        latest = b"\x12\x08proto" + self.BLOCK.format(eager="topic_send\n").encode() + b"\x1a\x00"
        self.conversation(older, latest, b"\x0a\x02ok")  # A small row after the request's.
        self.assertEqual(left_out(self.root, SID, listed), {"bridge": ["topic_start"]})
        self.assertEqual(left_out(self.root, SID, {"bridge": ["topics_list", "knowledge_search"]}), {})
        self.assertEqual(left_out(self.root, SID, {"other": ["x", "y"]}), {"other": ["x", "y"]})  # Never started.
        self.assertEqual(left_out(self.root, "not-a-uuid", listed), {})

    def test_left_out_says_nothing_of_a_conversation_whose_requests_have_no_server_list(self):
        from covey.backends.agy.bridge import left_out
        self.conversation(b"\x12\x08no servers here")
        self.assertEqual(left_out(self.root, SID, {"bridge": ["topic_start"]}), {})

    def test_a_butler_turn_whose_tools_agy_left_out_says_which_in_the_log(self):
        from covey.core.butler import mcp_config
        (self.root / ".gemini" / "antigravity-cli").mkdir(parents=True, exist_ok=True)
        self.conversation(self.BLOCK.format(eager="topic_send\n").encode())
        config = mcp_config(Path(__file__).resolve().parents[1], self.root / "state", "om_1-1", sys.executable)
        ctx = dataclasses.replace(self.ctx, options={**self.ctx.options, "mcp_config": config})
        with self.assertLogs("covey.backends.agy.bridge", level="WARNING") as logs:
            result, *_rest = self.run_agy("ok", ctx=ctx)
        self.assertFalse(result.is_error, result.text)
        said = "\n".join(logs.output)
        self.assertIn(f"out of conversation {SID}: ", said)
        self.assertIn("topic_start", said)
        self.assertNotIn("topics_list,", said)
        self.assertIn("COVEY_AGY_EAGER_BYTES", said)

    def test_any_other_turn_keeps_the_real_home(self):
        _result, _events, _replies, _sessions, record = self.run_agy("ok")
        self.assertEqual(Path(record["home"]), self.root)

    def test_agys_own_default_model_and_budget_pass_no_flag(self):
        result, *_rest, record = self.run_agy("ok", ctx=dataclasses.replace(self.ctx, model="", effort=""))
        self.assertFalse(result.is_error, result.text)
        self.assertNotIn("--model", record["argv"])
        self.assertNotIn("--effort", record["argv"])

    def test_without_skip_permissions_agy_asks_for_none(self):
        _result, *_rest, record = self.run_agy("ok", ctx=dataclasses.replace(self.ctx, skip_permissions=False))
        self.assertNotIn("--dangerously-skip-permissions", record["argv"])

    def test_resume_passes_the_conversation_and_rejects_another(self):
        result, *_rest, record = self.run_agy("ok", session=SID)
        self.assertFalse(result.is_error, result.text)
        self.assertEqual(record["argv"][record["argv"].index("--conversation") + 1], SID)
        result, _events, replies, _sessions, _record = self.run_agy("mismatch", session=SID)
        self.assertTrue(result.is_error)
        self.assertEqual(result.outcome.error_code, "protocol_error")
        self.assertEqual(replies, [])

    def test_failures_map_to_bridge_codes_without_native_text(self):
        for scenario, code in (("logged-out", "authentication_error"), ("garbage", "protocol_error"),
                               ("busy", "rate_limited"), ("error", "root_failed"),
                               ("exit-after-result", "root_failed"), ("unavailable", "server_fault"),
                               ("quota-window", "rate_limited"), ("quota", "quota_exhausted")):
            with self.subTest(scenario=scenario):
                result, events, replies, _sessions, _record = self.run_agy(scenario)
                self.assertTrue(result.is_error)
                self.assertEqual(result.outcome.error_code, code)
                self.assertFalse(result.outcome.resume_allowed)
                self.assertNotIn("PRIVATE_DETAIL", result.text)
                self.assertEqual(replies, [])
                if scenario == "logged-out":
                    self.assertEqual(result.text, NOT_LOGGED_IN)
                    failure = [d for k, d in events if k == "backend_failure"][-1]
                    self.assertFalse([line for line in failure["stderr"] if "oauth2" in line])

    def test_a_limit_and_a_passing_fault_are_told_from_what_no_retry_gets_past(self):
        """agy's ``result.error`` read for app/auto_retry.py: a limit or a quota that resets by itself (retried), a
        passing server fault (retried), and a login, a spent quota, a safety block, a model the account cannot use or
        anything else (never)."""
        from covey.backends.agy.bridge import error_code
        cases = {
            "rate_limited": ("429 Too Many Requests", "RESOURCE_EXHAUSTED (code 429): rate limit exceeded",
                             "Quota exceeded for quota metric 'Generate Content API requests per minute'",
                             "Your quota will reset after 4h12m. You have exhausted your capacity on this model."),
            "server_fault": ("UNAVAILABLE (code 503): The model is overloaded.", "INTERNAL (code 500): oops",
                             "DEADLINE_EXCEEDED (code 504)", "stream error: stream ID 7; INTERNAL_ERROR",
                             'Post "https://example.invalid/v1": read: connection reset by peer',
                             "The model API is currently overloaded and may experience intermittent errors.",
                             "unexpected EOF", "context deadline exceeded",
                             "There was a network issue connecting to the server, please try again."),
            "quota_exhausted": ("you are out of credits", "Quota exhausted for this account", "usage limit reached"),
            "authentication_error": ("You are not logged into Antigravity.", "401 Unauthorized",
                                     "authentication failed or timed out"),
            # A safety block is never a limit or a fault, whatever else it says (T872: said as what it is).
            "policy_refusal": ("INTERNAL (code 500): response blocked by SAFETY", "429: content violates the usage policy",
                               "UNAVAILABLE: PROHIBITED_CONTENT"),
            "model_unavailable": ("model not found",
                                  'invalid model selection (--model "gemini-4-neon" --effort "xhigh"): --effort is not '
                                  'supported for model "gemini-4-neon"',
                                  "failed to construct executor: unknown model key MODEL_PLACEHOLDER_M535: model not found"),
            "": ("PRIVATE_DETAIL internal", "PERMISSION_DENIED (code 403): Your location is not eligible",
                 "INVALID_ARGUMENT (code 400): bad request", "tool run_command failed", ""),
        }
        cases["rate_limited"] += ("model gemini-3.1-pro is out of quota until 2026-11-23T08:00:00Z",)
        for code, errors in cases.items():
            for error in errors:
                with self.subTest(error=error):
                    self.assertEqual(error_code(error), code)

    def test_a_failure_says_what_it_was_and_agys_own_words_go_to_the_card(self):
        """T872: a failure agy named is said as what it was (a model the account lost: the title's failure_detail), and
        whatever the error, agy's own words go to the card's 技术详情 (backend_said) and the run's log."""
        for scenario, code, title, said in (
                ("model-gone", "model_unavailable", "Antigravity 不认 gemini-3.8-pro-high", "invalid model selection"),
                ("refused", "policy_refusal", "", "response blocked by SAFETY"),
                ("bad-request", "root_failed", "", "INVALID_ARGUMENT (code 400): * GenerateContentRequest.tools"),
                ("error", "root_failed", "", "PRIVATE_DETAIL internal"),
                # Ended with no result and no error line: its last line of stderr (T872).
                ("crash", "process_exit", "", "panic: runtime error: index out of range")):
            with self.subTest(scenario=scenario):
                result, events, _replies, _sessions, _record = self.run_agy(scenario)
                self.assertEqual((result.is_error, result.outcome.error_code), (True, code))
                self.assertEqual(result.failure_detail.startswith(title) and bool(result.failure_detail) == bool(title),
                                 True, result.failure_detail)
                self.assertIn(said, result.backend_said)
                self.assertNotIn("PRIVATE_DETAIL", result.text)
                failure = [d for k, d in events if k == "backend_failure"][-1]
                self.assertEqual(failure["code"], code)

    def test_a_quota_back_in_days_is_spent_held_and_refused_before_the_next_turn_starts(self):
        """T872, 2026-11-27: "Individual quota reached … Resets in 157h33m41s" was a passing limit, retried every few
        minutes, each failing. It is a spent quota with its reset (app/auto_retry.py retries none that far off), and the
        model's group is held: the next turn on it is refused before it starts, until HOLD_RECHECK passes."""
        from covey.backends.agy import accounts
        before = time.time()
        result, *_rest = self.run_agy("quota-days")
        self.assertEqual(result.outcome.error_code, "quota_exhausted")
        self.assertAlmostEqual(result.limit["reset_at"] - before, 157 * 3600 + 33 * 60 + 41, delta=60)
        self.assertIn("gemini-3.8-pro-high 额度用完了", result.failure_detail)
        self.assertIn("恢复", result.failure_detail)
        self.assertNotIn("PRIVATE_DETAIL", result.failure_detail)
        code, words, reset = self.backend.start_refusal(self.ctx)
        self.assertEqual((code, reset), ("quota_exhausted", result.limit["reset_at"]))
        self.assertIn("额度用完了", words)
        # The same group's other models too; another group's not; the hold over after the recheck wait.
        self.assertIsNotNone(self.backend.start_refusal(dataclasses.replace(self.ctx, model="gemini-3.8-flash")))
        self.assertIsNone(self.backend.start_refusal(dataclasses.replace(self.ctx, model="claude-opus-4-6-thinking")))
        later = time.time() + accounts.HOLD_RECHECK + 1
        self.assertIsNone(accounts.holding(self.ctx.state_dir, self.ctx.model, now=later))
        # A window that comes back within hours stays a passing limit, retried at its reset.
        result, *_rest = self.run_agy("quota-window")
        self.assertEqual(result.outcome.error_code, "rate_limited")
        self.assertAlmostEqual(result.limit["reset_at"] - time.time(), 3723, delta=60)

    def test_a_model_gone_from_the_saved_list_is_refused_before_the_turn_starts(self):
        """F2 (T872): the account's list (launch-models/agy.json, ``agy models``) no longer has the topic's model: the
        turn is refused before it starts, in words that say to switch; agy's own default and an empty list never are."""
        folder = Path(self.ctx.state_dir) / "launch-models"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "agy.json").write_text(json.dumps({"models": {"gemini-4-hydrogen": {"levels": ["high"]}}}))
        code, words, reset = self.backend.start_refusal(dataclasses.replace(self.ctx, model="gemini-4-neon"))
        self.assertEqual((code, reset), ("model_unavailable", 0.0))
        self.assertIn("gemini-4-neon", words)
        self.assertIsNone(self.backend.start_refusal(dataclasses.replace(self.ctx, model="gemini-4-hydrogen")))
        self.assertIsNone(self.backend.start_refusal(dataclasses.replace(self.ctx, model="")))
        (folder / "agy.json").write_text(json.dumps({"models": {}}))
        self.assertIsNone(self.backend.start_refusal(dataclasses.replace(self.ctx, model="gemini-4-neon")))

    def test_a_stopped_turn_is_never_a_limit_or_a_fault(self):
        """The stop's own end (agy cut off mid-stream) says cancelled, never what app/auto_retry.py would retry."""
        from types import SimpleNamespace
        from covey.backends.agy.bridge import stream_outcome
        stopped, running = threading.Event(), threading.Event()
        stopped.set()
        for error in ("UNAVAILABLE (code 503): stream closed", "429 Too Many Requests"):
            with self.subTest(error=error):
                rows = SimpleNamespace(result={"status": "ERROR", "error": error}, invalid=False, sid=SID)
                self.assertEqual(stream_outcome(rows, 1, stopped), ("cancelled", "cancelled"))
                self.assertIn(stream_outcome(rows, 1, running)[1], ("server_fault", "rate_limited"))
        # A login that failed is still said as one.
        rows = SimpleNamespace(result={"status": "ERROR", "error": "401 Unauthorized"}, invalid=False, sid=SID)
        self.assertEqual(stream_outcome(rows, 1, stopped), ("failed", "authentication_error"))

    def test_stop_ends_the_process_and_is_not_resumable(self):
        cancel = threading.Event()
        threading.Timer(1.0, cancel.set).start()
        started = time.monotonic()
        result, *_rest = self.run_agy("slow", cancel=cancel)
        self.assertLess(time.monotonic() - started, 20)
        self.assertEqual(result.outcome.status, "cancelled")
        self.assertFalse(result.outcome.resume_allowed)

    def test_a_supplement_stops_the_turn_and_the_conversation_goes_on_with_it(self):
        os.environ["FAKE_AGY_SCENARIO"] = "steer"
        events, replies, controls, tools = [], [], [], threading.Event()

        def event(kind, data, control):
            events.append((kind, data))
            if kind == "tool" and data.get("status") == "running":
                tools.set()
        done = {}
        request = RunRequest(self.ctx, "做个任务", callbacks=RunCallbacks(
            event=event, control=controls.append, verified_reply=replies.append),
            inputs=RunInputs(threading.Event(), run_id=RUN))
        worker = threading.Thread(target=lambda: done.update(result=self.backend.run(request)))
        worker.start()
        self.assertTrue(tools.wait(20))
        control = controls[0]
        control.steer("改成 B", "ev-1")
        # The command runs on: a supplement waits for its step. The owner's 打断它，马上读 (or an urgent one) stops it.
        time.sleep(0.5)
        self.assertTrue(worker.is_alive() and not (self.root / "record.json.all").read_text().count("--conversation"))
        self.assertEqual(control.stop_command("Bridge 停了命令"), {"stopped": ["ls"]})
        worker.join(30)
        result = done["result"]
        self.assertFalse(result.is_error, result.text)
        self.assertEqual((result.text, result.session_id), ("完成", SID))
        runs = [json.loads(line) for line in (self.root / "record.json.all").read_text().splitlines()]
        self.assertEqual(len(runs), 2)
        self.assertNotIn("--conversation", runs[0]["argv"])
        self.assertEqual(runs[1]["argv"][runs[1]["argv"].index("--conversation") + 1], SID)
        message = json.loads(runs[1]["stdin"])["message"]["content"]
        self.assertTrue(message.startswith("用户补充（你刚才的执行被这条补充打断了") and message.endswith("改成 B"), message)
        self.assertIn("停下来的命令没有跑完", message)
        self.assertLess(message.index("Bridge 停了命令"), message.index("改成 B"))
        self.assertIn("最后的回复要写完整的结论", message)
        self.assertIn(("steer_read", {"event_id": "ev-1"}), events)
        steps = {}
        for kind, data in events:
            if kind == "tool":
                steps[data["id"]] = data["status"]
        # The stopped command ends failed; the next process's steps have keys of their own.
        self.assertEqual(steps, {"step-2": "failed", "step-1-2": "done"})
        self.assertEqual([k for k, _d in events].count("trace_finished"), 1)
        child = int((self.root / "record.json.child").read_text())
        self.assertFalse(Path(f"/proc/{child}").exists() and "python" in Path(f"/proc/{child}/cmdline").read_text())
        self.assertIsNone(controls[-1])  # The controls are dropped when the turn ends.
        with self.assertRaises(SteerRefused):
            control.steer("太晚了", "ev-2")

    def test_a_supplement_waits_for_the_running_step_and_the_next_process_reads_it_after_it(self):
        os.environ["FAKE_AGY_SCENARIO"] = "steer-between"
        events, controls, tools = [], [], threading.Event()

        def event(kind, data, control):
            events.append((kind, data))
            if kind == "tool" and data.get("status") == "running":
                tools.set()
        done = {}
        request = RunRequest(self.ctx, "做个任务", callbacks=RunCallbacks(
            event=event, control=controls.append, verified_reply=lambda reply: None),
            inputs=RunInputs(threading.Event(), run_id=RUN))
        worker = threading.Thread(target=lambda: done.update(result=self.backend.run(request)))
        worker.start()
        self.assertTrue(tools.wait(20))
        controls[0].steer("改成 B", "ev-1")
        time.sleep(0.7)
        child = int((self.root / "record.json.child").read_text())
        self.assertTrue(Path(f"/proc/{child}").exists())  # The step's command was not stopped.
        self.assertEqual(len((self.root / "record.json.all").read_text().splitlines()), 1)
        (self.root / "record.json.go").write_text("")  # The command ends: the step is DONE, then the process stops.
        worker.join(40)
        result = done["result"]
        self.assertFalse(result.is_error, result.text)
        runs = [json.loads(line) for line in (self.root / "record.json.all").read_text().splitlines()]
        self.assertEqual(len(runs), 2)
        message = json.loads(runs[1]["stdin"])["message"]["content"]
        self.assertTrue(message.startswith("用户补充（上一步做完后停下来读这条补充") and message.endswith("改成 B"), message)
        self.assertNotIn("停下来的命令没有跑完", message)
        self.assertIn("最后的回复要写完整的结论", message)
        self.assertIn(("steer_read", {"event_id": "ev-1"}), events)
        steps = {}
        for kind, data in events:
            if kind == "tool":
                steps[data["id"]] = data["status"]
        self.assertEqual(steps["step-2"], "done")  # Its command finished: the stop did not fail it.

    def test_agy_stops_commands_on_this_machine_only(self):
        self.assertTrue(backend_capabilities(self.backend, self.ctx).command_stop)
        self.assertFalse(backend_capabilities(self.backend, dataclasses.replace(self.ctx, device="box")).command_stop)

    def test_the_switch_words_ask_for_the_whole_reply_too(self):
        from covey.backends.agy.bridge import SWITCH_RESUMED
        self.assertIn("最后的回复要写完整的结论", SWITCH_RESUMED)

    def test_a_supplement_before_agy_said_its_conversation_waits(self):
        from covey.backends.agy.bridge import AgyControl
        control = AgyControl(lambda *a: None)
        with self.assertRaises(SteerNotReady):
            control.steer("x", "ev")
        with self.assertRaises(SteerRefused):
            control.steer("  ", "ev")

    def test_an_unknown_budget_never_starts_agy(self):
        with self.assertRaises(ValueError):
            self.backend.run(RunRequest(dataclasses.replace(self.ctx, effort="ultra"), "x"))
        self.assertFalse((self.root / "record.json").exists())

    def test_catalog_lists_agy_models_and_refuses_without_a_login(self):
        os.environ["FAKE_AGY_SCENARIO"] = "ok"
        catalog = self.backend.refresh_model_catalog(self.ctx)
        self.assertEqual(catalog, {
            "gemini-4-neon": {"display_name": "Gemini 4 Neon", "default_reasoning_level": "high",
                              "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}]},
            "gemini-pro-orchestrator": {"display_name": "Orchestrator (Carbon)", "supported_reasoning_levels": []},
            "gemini-3.8-flash": {"display_name": "Gemini 3.8 Flash", "default_reasoning_level": "medium",
                                 "supported_reasoning_levels": [{"effort": "medium"}]}})
        os.environ["FAKE_AGY_SCENARIO"] = "logged-out"
        with self.assertRaisesRegex(ValueError, "还没有登录"):
            self.backend.refresh_model_catalog(self.ctx)
        self.assertEqual(models_from_listing("Fetching available models...\nModels\n"), {})

    def test_the_saved_catalog_keeps_budgets_agy_refuses_from_it(self):
        from covey.app.launch_models import refresh_catalog
        os.environ["FAKE_AGY_SCENARIO"] = "ok"
        refresh_catalog(self.app.apps["agy"], self.ctx)
        with self.assertRaisesRegex(ValueError, "不支持所选推理档位"):
            self.backend.run(RunRequest(dataclasses.replace(self.ctx, model="gemini-4-neon", effort="medium"), "x"))
        self.assertFalse((self.root / "record.json").exists())
        _result, *_rest, record = self.run_agy("ok", ctx=dataclasses.replace(
            self.ctx, model="gemini-pro-orchestrator", effort="high"))
        self.assertNotIn("--effort", record["argv"])  # agy refuses a budget for a model without any.
        _result, *_rest, record = self.run_agy("ok", ctx=dataclasses.replace(self.ctx, model="gemini-4-neon"))
        self.assertEqual(record["argv"][-4:], ["--model", "gemini-4-neon", "--effort", "high"])

    def test_tool_parameters_read_like_the_other_harnesses(self):
        self.assertEqual(tool_inputs({"CommandLine": "ls", "Cwd": "/x"}), {"command": "ls"})
        self.assertEqual(tool_inputs({"AbsolutePath": "/a.py", "StartLine": 3}), {"path": "/a.py"})
        adapter = AntigravityEventAdapter(lambda kind, data: None)
        adapter.accept({"event": "step_update", "step_update": {
            "step_index": 4, "state": "ACTIVE", "step_type": "tool", "tool_name": "read_url_content",
            "tool_info": {"parameters": {"Url": "https://example.invalid/doc?token=PRIVATE#part"}}}}, 1.0)
        self.assertEqual(adapter.tools["step-4"]["detail"], "https://example.invalid/doc")
        self.assertEqual(adapter.tools["step-4"]["name"], "webfetch")


    def test_each_root_request_says_the_context_the_next_one_starts_from(self):
        # What the butler's compaction threshold reads (app/butler.py compact_butler): input, cache reads and output.
        _result, events, *_ = self.run_agy("ok")
        usage = [d for k, d in events if k == "token_usage"]
        self.assertEqual([d.get("context_tokens") for d in usage], [13187 + 0 + 91, 2249 + 11130 + 41])

    def test_no_context_window_is_reported_as_agy_names_none(self):
        # agy's stream (init, step and result rows), `agy models` and its conversation files carry no window (T910 W2-A).
        _result, events, *_ = self.run_agy("ok")
        usage = [d for k, d in events if k == "token_usage"]
        self.assertTrue(usage and all("context_window" not in d for d in usage))

    def compact(self, how="", ctx=None):
        os.environ["FAKE_AGY_SCENARIO"] = "ok"
        os.environ["FAKE_AGY_COMPACT"] = how
        processes = []
        done = self.backend.compact_session(RunRequest(ctx or self.ctx, "", session_id=SID, callbacks=RunCallbacks(
            process=processes.append), inputs=RunInputs(threading.Event(), run_id=RUN)))
        path = self.root / "record.json"
        record = json.loads(path.read_text()) if path.exists() else {}
        return done, record, processes

    def test_the_idle_compaction_is_agys_own_compact_with_slash_commands_on(self):
        done, record, processes = self.compact()
        self.assertTrue(done)
        self.assertEqual(json.loads(record["stdin"]), {"event": "user", "message": {"content": "/compact"}})
        self.assertEqual(record["argv"], ["--output-format", "stream-json", "--input-format", "stream-json",
                                          "--dangerously-skip-permissions", "--conversation", SID,
                                          "--model", "gemini-3.8-pro-high", "--effort", "high"])
        self.assertTrue(processes)  # The runner records the process, so 停止 can stop it.

    def test_a_conversation_agy_did_not_compact_is_no_compaction(self):
        for how in ("short", "fail"):
            with self.subTest(how=how):
                self.assertFalse(self.compact(how)[0])

    def test_the_idle_compaction_runs_in_the_butlers_home_with_its_tools(self):
        config = {"mcpServers": {"bridge": {"command": sys.executable, "args": ["-c", "pass"]}}}
        ctx = dataclasses.replace(self.ctx, options={**self.ctx.options, "mcp_config": config})
        done, record, _ = self.compact(ctx=ctx)
        self.assertTrue(done)
        self.assertIn("bridge", record["mcp"]["mcpServers"])
        self.assertNotEqual(record["home"], str(self.root))

    def test_a_turn_asked_to_compact_first_compacts_then_runs(self):
        os.environ["FAKE_AGY_COMPACT"] = ""
        ctx = dataclasses.replace(self.ctx, options={**self.ctx.options, "compact_first": True})
        result, events, replies, _sessions, _record = self.run_agy("ok", session=SID, ctx=ctx)
        self.assertFalse(result.is_error, result.text)
        steps = [d for k, d in events if k == "tool" and d.get("id") == "compact-before-turn"]
        self.assertEqual([d["status"] for d in steps], ["running", "done"])
        self.assertIn("压缩前上下文 7.30 万 token", steps[-1]["detail"])
        usage = [d for k, d in events if k == "token_usage"]
        self.assertEqual(usage[-1]["input_tokens"], 3125 + 69866 + 13187 + 0 + 2249 + 11130)
        self.assertEqual(usage[-1]["context_tokens"], 2249 + 11130 + 41)  # The compaction's is not the context after it.

    def test_a_failed_compaction_first_leaves_the_turn_as_it_was(self):
        os.environ["FAKE_AGY_COMPACT"] = "fail"
        ctx = dataclasses.replace(self.ctx, options={**self.ctx.options, "compact_first": True})
        result, events, *_ = self.run_agy("ok", session=SID, ctx=ctx)
        self.assertFalse(result.is_error, result.text)
        step = [d for k, d in events if k == "tool" and d.get("id") == "compact-before-turn"][-1]
        self.assertEqual(step["status"], "failed")
        self.assertIn("本轮按原上下文继续", step["detail"])

    def test_it_compacts_here_and_not_on_a_device(self):
        self.assertTrue(backend_capabilities(self.backend, self.ctx).compaction)
        device = dataclasses.replace(self.ctx, device="box")
        self.assertFalse(backend_capabilities(self.backend, device).compaction)
        self.assertFalse(self.backend.compact_session(RunRequest(device, "", session_id=SID)))
        self.assertEqual(idle_compaction.idle_seconds(self.backend), 180.0)


class AgyStepWordsTests(unittest.TestCase):
    """T738 (2026-11-24): 执行过程 of an Antigravity turn read "set -euo pipefail for v in http_proxy…" step under step and a bare
    "schedule". agy's stream-json leaves a call's toolSummary out of its parameters (1.5.14: a run_command streams its CommandLine
    alone); the call as agy keeps it in this machine's conversation database has it."""

    CONVERSATION = "0d6c1f9e-1b2a-4c3d-8e9f-0a1b2c3d4e5f"
    COMMAND = "set -euo pipefail\nfor v in http_proxy https_proxy; do unset \"$v\"; done\n.venv/bin/python tools/check.py --pool a"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        (self.home / CONVERSATIONS).mkdir(parents=True)

    def database(self, rows):
        """A conversation database in agy's shape: ``steps`` rows whose payload is protobuf bytes around the call's JSON."""
        import sqlite3
        db = sqlite3.connect(self.home / CONVERSATIONS / f"{self.CONVERSATION}.db")
        db.execute("CREATE TABLE steps (idx integer PRIMARY KEY, step_type integer, step_payload blob)")
        for index, call in rows:
            payload = b"\n\x18call_6767\x12\x0brun_command\x1a\xac\x04" + json.dumps(call, ensure_ascii=False).encode() + b":\xbf\x02\x12"
            db.execute("INSERT INTO steps VALUES (?, 132, ?)", (index, payload))
        db.commit()
        db.close()

    def run_rows(self, rows, home=True):
        events = []
        adapter = AntigravityEventAdapter(lambda kind, data: events.append((kind, data)))
        adapter.home, adapter.conversation = (str(self.home) if home else None), self.CONVERSATION
        for row in rows:
            adapter.accept({"event": "step_update", "step_update": row}, 1.0)
        return adapter.tools

    def tool(self, index, name, parameters, state="ACTIVE"):
        return {"step_index": index, "state": state, "step_type": "tool", "tool_name": name,
                "tool_info": {"name": name, "parameters": parameters}}

    def test_a_command_leads_with_its_calls_summary(self):
        self.database([(5, {"CommandLine": "ls", "toolSummary": "别的调用"}),
                       (6, {"CommandLine": self.COMMAND, "WaitMsBeforeAsync": 5000, "toolSummary": "看 a 池的在线状态"})])
        steps = self.run_rows([self.tool(6, "run_command", {"CommandLine": self.COMMAND}),
                               self.tool(6, "run_command", {"CommandLine": self.COMMAND}, "DONE")])
        self.assertEqual(steps["step-6"]["detail"], "看 a 池的在线状态 · " + self.COMMAND)
        self.assertEqual((steps["step-6"]["name"], steps["step-6"]["status"]), ("bash", "done"))

    def test_without_its_summary_it_reads_as_what_it_runs(self):
        from covey.core.text import safe_text, shown_command
        self.database([(6, {"CommandLine": "other", "toolSummary": "不是这一条"})])
        for home in (True, False):  # Not found, or a device's turn (no HOME of this machine).
            with self.subTest(home=home):
                detail = self.run_rows([self.tool(6, "run_command", {"CommandLine": self.COMMAND})], home)["step-6"]["detail"]
                self.assertEqual(detail, self.COMMAND)
                self.assertEqual(shown_command(safe_text(detail, None)), ".venv/bin/python tools/check.py --pool a")

    def test_a_summary_written_down_after_the_start_is_found_at_the_end(self):
        steps = self.run_rows([self.tool(6, "run_command", {"CommandLine": "make test"})])
        self.assertEqual(steps["step-6"]["detail"], "make test")
        self.database([(6, {"CommandLine": "make test", "toolSummary": "跑单测"})])
        adapter = AntigravityEventAdapter(lambda kind, data: None)
        adapter.home, adapter.conversation = str(self.home), self.CONVERSATION
        adapter.accept({"event": "step_update", "step_update": self.tool(6, "run_command", {"CommandLine": "make test"})}, 1.0)
        adapter.accept({"event": "step_update", "step_update": self.tool(6, "run_command", {"CommandLine": "make test"}, "DONE")}, 2.0)
        self.assertEqual(adapter.tools["step-6"]["detail"], "跑单测 · make test")

    def test_a_call_with_nothing_streamed_reads_its_own_row_only(self):
        self.database([(8, {"toolSummary": "上一步的话"}), (9, {"toolSummary": "列出工作区"})])
        steps = self.run_rows([self.tool(9, "mcp_bridge_workspaces_list", {}), self.tool(10, "mcp_bridge_workspaces_list", {})])
        self.assertEqual((steps["step-9"]["detail"], steps["step-10"]["detail"]), ("列出工作区", ""))

    def test_a_database_it_cannot_read_gives_nothing(self):
        (self.home / CONVERSATIONS / f"{self.CONVERSATION}.db").write_bytes(b"not sqlite")
        self.assertIsNone(tool_summary(str(self.home), self.CONVERSATION, 6, {"CommandLine": "ls"}))
        self.assertIsNone(tool_summary(str(self.home), "not-a-uuid", 6, {"CommandLine": "ls"}))
        self.assertIsNone(tool_summary(None, self.CONVERSATION, 6, {"CommandLine": "ls"}))

    def test_a_schedule_says_what_it_comes_back_to_do_and_when(self):
        steps = self.run_rows([self.tool(2, "schedule", {"DurationSeconds": 180, "Prompt": "查看第 2 批的严检结果"}),
                               self.tool(3, "schedule", {"DurationSeconds": 5, "Prompt": "检查探针"}),
                               self.tool(4, "schedule", {"DurationSeconds": 7200})])
        self.assertEqual([(steps[k]["name"], steps[k]["detail"]) for k in ("step-2", "step-3", "step-4")],
                         [("定时等待", "查看第 2 批的严检结果（3 分钟后）"), ("定时等待", "检查探针（5 秒后）"), ("定时等待", "2 小时后回来接着做")])

    def test_newer_tool_names_read_as_the_others(self):
        steps = self.run_rows([self.tool(1, "edit_file", {"Path": "/x/a.py"}), self.tool(2, "write_file", {"Path": "/x/b.md"}),
                               self.tool(3, "update_steps", {"steps": []})])
        self.assertEqual([steps[k]["name"] for k in ("step-1", "step-2", "step-3")], ["edit", "write", "更新计划"])
        self.assertEqual(steps["step-1"]["detail"], "/x/a.py")


class AgyTurnHookTests(unittest.TestCase):
    """T674: an extension's ``agy_turn`` hook gives a turn its environment and, after a process that failed on a spent
    quota, a limit or a sign-in, another one in which the same conversation goes on (several Google accounts, each a
    HOME of its own, are the extension's). Without the hook, or when it has none, a turn runs and fails as before."""

    def setUp(self):
        AntigravityTests.setUp(self)
        self.failures = []
        # Each process's record, kept past run_agy (which removes record.json's): ``runs`` reads and removes them.
        self.record = self.root / "hook-record.json"
        os.environ["FAKE_AGY_RECORD"] = str(self.record)

    run_agy = AntigravityTests.run_agy

    def runs(self):
        path = self.record.with_name(self.record.name + ".all")
        found = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        if path.exists():
            path.unlink()
        return found

    def hook(self, *spent, note="主号的 Gemini 额度用完了，换副号接着做", raises=False, cancel=None):
        """A hook whose turn starts with ``FAKE_AGY_SPENT=spent[0]`` (the fake agy's failure, '' none) and HOME
        <root>/acct-0, each switch going on with the next one; None once they are used up."""
        import contextlib
        from types import SimpleNamespace

        def env_of(env, index):
            home = self.root / f"acct-{index}"
            home.mkdir(exist_ok=True)
            return {**env, "HOME": str(home), "FAKE_AGY_SPENT": spent[index]}

        @contextlib.contextmanager
        def turn(ctx, env, *, run_id=""):
            self.assertEqual(run_id, RUN)
            left = list(range(1, len(spent)))

            def switch(failure):
                self.failures.append(failure)
                if raises:
                    raise RuntimeError("the hook broke")
                if cancel is not None:
                    cancel.set()
                return (env_of(env, left.pop(0)), note) if left else None
            yield env_of(env, 0), switch
        return SimpleNamespace(turn=turn)

    def hooked(self, hook, scenario="ok", **kwargs):
        from covey.core import extensions
        with mock.patch.object(extensions, "hook", return_value=hook) as asked:
            found = self.run_agy(scenario, **kwargs)
        asked.assert_called_with("agy_turn")
        return found

    def test_a_spent_quota_goes_on_in_the_hooks_next_environment_in_the_same_conversation(self):
        from covey.backends.agy.bridge import SWITCH_RESUMED
        result, events, replies, sessions, _record = self.hooked(self.hook("after-steps", ""))
        self.assertFalse(result.is_error, result.text)
        self.assertEqual((result.text, result.session_id, sessions), ("完成", SID, [SID]))
        self.assertEqual(replies[-1].fingerprint, result.outcome.reply_fingerprint)
        first, second = self.runs()
        self.assertEqual((first["spent"], first["home"]), ("after-steps", str(self.root / "acct-0")))
        self.assertEqual((second["spent"], second["home"]), ("", str(self.root / "acct-1")))
        self.assertNotIn("--conversation", first["argv"])
        self.assertEqual(second["argv"][second["argv"].index("--conversation") + 1], SID)
        self.assertEqual(json.loads(second["stdin"])["message"]["content"], SWITCH_RESUMED)
        failure, = self.failures
        self.assertEqual((failure["code"], failure["conversation"], failure["model"]),
                         ("quota_exhausted", SID, "gemini-3.8-pro-high"))
        self.assertIn("exhausted your quota", failure["error"])
        notes = [data for kind, data in events if kind == "progress_note" and data["id"].startswith("agy-switch-")]
        self.assertEqual([note["text"] for note in notes], ["主号的 Gemini 额度用完了，换副号接着做"])
        steps = {}
        for kind, data in events:
            if kind == "tool":
                steps[data["id"]] = data["status"]
        self.assertEqual(steps, {"step-2": "done", "step-1-2": "done"})  # The next process's steps have keys of their own.
        self.assertEqual([kind for kind, _data in events].count("backend_failure"), 0)
        self.assertEqual([kind for kind, _data in events].count("trace_finished"), 1)

    def test_words_agy_never_took_in_go_again_and_taken_ones_are_gone_on_from(self):
        from covey.backends.agy.bridge import SWITCH_RESUMED
        cases = (("before-init", None, False, "做个任务"), ("before-init", SID, True, "做个任务"),
                 ("after-input", None, True, SWITCH_RESUMED), ("logged-out", None, False, "做个任务"))
        for spent, session, resumes, words in cases:
            with self.subTest(spent=spent, session=session):
                self.failures.clear()
                result, _events, _replies, sessions, _record = self.hooked(self.hook(spent, ""), session=session)
                self.assertFalse(result.is_error, result.text)
                self.assertEqual(sessions, [SID])  # Said once, by the process that began it.
                first, second = self.runs()
                self.assertEqual("--conversation" in second["argv"], resumes)
                self.assertEqual(json.loads(second["stdin"])["message"]["content"], words)
                if spent == "before-init":
                    self.assertEqual((second["argv"], second["stdin"]), (first["argv"], first["stdin"]))
                self.assertEqual(self.failures[0]["code"],
                                 {"before-init": "rate_limited", "after-input": "quota_exhausted",
                                  "logged-out": "authentication_error"}[spent])

    def test_without_the_hook_or_another_environment_the_turn_fails_as_before(self):
        from covey.core import extensions
        with mock.patch.object(extensions, "hook", return_value=None):
            result, *_rest = self.run_agy("ok")
        self.assertFalse(result.is_error)
        self.assertEqual(Path(self.runs()[0]["home"]), self.root)  # The environment as it is.
        for raises in (False, True):
            with self.subTest(raises=raises), self.assertLogs("covey.backends.agy.bridge", "WARNING") as logs:
                result, events, replies, _sessions, _record = self.hooked(self.hook("after-steps", raises=raises))
                if not raises:
                    logging.getLogger("covey.backends.agy.bridge").warning("none")  # assertLogs wants one.
                self.assertEqual(any("agy_turn switch failed" in line for line in logs.output), raises)
                self.assertTrue(result.is_error)
                self.assertEqual(result.outcome.error_code, "quota_exhausted")
                self.assertEqual((len(self.runs()), replies), (1, []))
                self.assertFalse([data for kind, data in events if kind == "progress_note"
                                  and data["id"].startswith("agy-switch-")])
                self.assertEqual([kind for kind, _data in events].count("backend_failure"), 1)

    def test_a_turn_goes_on_elsewhere_at_most_max_switches_times(self):
        from covey.backends.agy.bridge import MAX_SWITCHES
        result, *_rest = self.hooked(self.hook(*["after-steps"] * (MAX_SWITCHES + 2)))
        self.assertEqual(result.outcome.error_code, "quota_exhausted")
        self.assertEqual((len(self.runs()), len(self.failures)), (MAX_SWITCHES + 1, MAX_SWITCHES))

    def test_other_failures_a_stop_and_a_device_never_ask_the_hook(self):
        from covey.backends.agy import bridge
        result, *_rest = self.hooked(self.hook("", ""), scenario="error")
        self.assertEqual((result.outcome.error_code, len(self.runs()), self.failures), ("root_failed", 1, []))
        cancel = threading.Event()
        result, *_rest = self.hooked(self.hook("after-steps", "", cancel=cancel), cancel=cancel)
        self.assertTrue(result.is_error)
        self.assertEqual((len(self.runs()), len(self.failures)), (1, 1))  # Stopped while the hook looked: no next one.
        hook = mock.Mock()
        with mock.patch.object(bridge, "device_of", return_value="box"), \
                mock.patch.object(bridge.extensions, "hook", return_value=hook):
            with bridge.extension_turn(self.ctx, {"HOME": "/x"}) as found:
                self.assertEqual(found, ({"HOME": "/x"}, None))
        hook.turn.assert_not_called()

    def test_the_butlers_home_is_made_again_over_the_next_environments_home(self):
        from covey.core.butler import mcp_config
        for index in (0, 1):
            cli = self.root / f"acct-{index}" / ".gemini" / "antigravity-cli"
            cli.mkdir(parents=True)
            (cli.parent / "config").mkdir()
        config = mcp_config(Path(__file__).resolve().parents[1], self.root / "state", "om_1-1", sys.executable)
        ctx = dataclasses.replace(self.ctx, options={**self.ctx.options, "mcp_config": config})
        result, *_rest = self.hooked(self.hook("after-steps", ""), ctx=ctx)
        self.assertFalse(result.is_error, result.text)
        first, second = self.runs()
        for index, run in enumerate((first, second)):
            self.assertEqual(Path(run["home"]).parent, self.root / "state" / "tmp")
            self.assertFalse(Path(run["home"]).exists())  # Each removed: the first when the turn moved on.
            self.assertEqual(run["cli"], str((self.root / f"acct-{index}" / ".gemini" / "antigravity-cli").resolve()))
            self.assertIn("bridge", run["mcp"]["mcpServers"])
        self.assertNotEqual(first["home"], second["home"])


class FakeProcess:
    pid = 4242

    def __init__(self):
        self.code = None

    def poll(self):
        return self.code


class AgyControlStagingTests(unittest.TestCase):
    """AgyControl: a supplement waits for the running step, the stop is once, stop_command (T910 W2-A)."""

    def make(self):
        from covey.backends.agy import bridge
        control = bridge.AgyControl(lambda *a: None)
        control.started(FakeProcess())
        control.ready = True
        kills = []
        for patcher in (mock.patch.object(bridge.os, "kill", lambda pid, sig: kills.append((pid, sig))),
                        mock.patch.object(bridge, "descendants", lambda pid: []),
                        mock.patch.object(bridge.AgyControl, "reap", staticmethod(lambda process, left: None))):
            patcher.start()
            self.addCleanup(patcher.stop)
        return control, kills

    def test_no_step_running_stops_at_once(self):
        control, kills = self.make()
        control.steer("改成 B", "ev-1")
        self.assertEqual(len(kills), 1)
        message, ids = control.take()
        self.assertEqual(ids, ["ev-1"])
        self.assertIn("停下来的命令没有跑完", message)

    def test_a_running_step_holds_the_stop_until_the_last_one_is_done(self):
        control, kills = self.make()
        control.step_started("a", "pytest -q")
        control.step_started("b", "")
        control.steer("改成 B", "ev-1")
        control.steer("再加 C", "ev-2")
        control.step_done("a")
        self.assertEqual(kills, [])
        control.step_done("b")
        self.assertEqual(len(kills), 1)
        control.step_done("c")  # Nothing runs and it is stopping: never a second one.
        control.steer("D", "ev-3")
        self.assertEqual(len(kills), 1)
        message, ids = control.take()
        self.assertEqual(ids, ["ev-1", "ev-2", "ev-3"])
        self.assertTrue(message.startswith("用户补充（上一步做完后停下来读这条补充"), message)
        self.assertNotIn("停下来的命令没有跑完", message)
        self.assertIn("最后的回复要写完整的结论", message)

    def test_a_step_done_without_a_supplement_stops_nothing(self):
        control, kills = self.make()
        control.step_started("a", "ls")
        control.step_done("a")
        self.assertEqual(kills, [])

    def test_a_supplement_and_the_end_of_the_step_together_stop_once_and_lose_none(self):
        for _ in range(60):
            control, kills = self.make()
            control.step_started("a", "pytest -q")
            gate = threading.Barrier(2)

            def steer():
                gate.wait()
                control.steer("改成 B", "ev-1")

            def finish():
                gate.wait()
                control.step_done("a")
            threads = [threading.Thread(target=steer), threading.Thread(target=finish)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(len(kills), 1, kills)
            message, ids = control.take()
            self.assertEqual((ids, message.count("改成 B")), (["ev-1"], 1))

    def test_stop_command_stops_the_step_with_the_note_ahead_of_the_supplement(self):
        control, kills = self.make()
        control.step_started("a", "pytest -q")
        control.steer("改成 B", "ev-1")
        self.assertEqual(control.stop_command("（Bridge 停了命令）"), {"stopped": ["pytest -q"]})
        self.assertEqual(len(kills), 1)
        message, _ids = control.take()
        self.assertIn("停下来的命令没有跑完", message)
        self.assertLess(message.index("Bridge 停了命令"), message.index("改成 B"))
        self.assertEqual(control.stop_command("x")["reason"], "模型现在没有在等命令")

    def test_stop_command_keeps_a_push_and_says_why_it_stopped_nothing(self):
        control, kills = self.make()
        self.assertEqual(control.stop_command("n"), {"reason": "模型现在没有在等命令"})
        control.step_started("a", "git push origin main")
        control.steer("改成 B", "ev-1")
        self.assertEqual(control.stop_command("n"), {"kept": "git push origin main"})
        control.step_started("b", "")  # A subagent: no command to stop.
        control.steps.pop("a")
        self.assertIn("子任务", control.stop_command("n")["reason"])
        self.assertEqual(kills, [])
        control.step_done("b")  # The held supplement goes on at the end of the step.
        self.assertEqual(len(kills), 1)

    def test_stop_command_refuses_while_stopping_closing_and_on_a_device(self):
        from covey.backends.agy.bridge import AgyControl
        control, kills = self.make()
        control.step_started("a", "pytest -q")
        control.steer("x", "ev")
        control.stop_command("n")
        self.assertIn("正在", control.stop_command("n")["reason"])
        closing, _ = self.make()
        closing.closing = True
        self.assertEqual(closing.stop_command("n"), {"reason": "任务正在收尾"})
        self.assertIn("设备", AgyControl(lambda *a: None, device=True).stop_command("n")["reason"])

    def test_the_adapter_tells_its_control_when_a_step_starts_and_ends(self):
        calls = []
        control = mock.Mock(step_started=lambda *a: calls.append(("start", *a)), step_done=lambda *a: calls.append(("done", *a)))
        adapter = AntigravityEventAdapter(lambda *a: None)
        adapter.control = control
        row = {"conversation_id": SID, "step_index": 2, "step_type": "tool", "tool_name": "run_command",
               "tool_info": {"name": "run_command", "parameters": {"CommandLine": "pytest -q"}}}
        adapter.accept({"event": "step_update", "step_update": {**row, "state": "ACTIVE"}}, 1.0)
        adapter.accept({"event": "step_update", "step_update": {**row, "state": "DONE"}}, 2.0)
        sub = {"conversation_id": SID, "step_index": 3, "step_type": "subagent", "tool_name": "invoke_subagent",
               "state": "ACTIVE"}
        adapter.accept({"event": "step_update", "step_update": sub}, 3.0)
        self.assertEqual(calls, [("start", "step-2", "pytest -q"), ("done", "step-2"), ("start", "step-3", "")])


class EagerNamesTests(unittest.TestCase):
    def test_a_tool_too_big_for_the_room_is_left_out_and_the_small_ones_after_it_still_load(self):
        from covey.backends.agy import home
        small = lambda name: {"name": name, "description": "x" * 10}
        big = {"name": "big_one", "description": "y" * 500}
        tools = [small("a"), big, small("b"), small("c")]
        room = home.tool_size(small("a")) * 3 + 5
        self.assertEqual(home.eager_names(tools, room), ["a", "b", "c"])
        # Skipped, it takes none of the room: the three small ones fill it exactly.
        self.assertEqual(home.eager_names(tools, home.tool_size(small("a")) * 3), ["a", "b", "c"])
        self.assertEqual(home.eager_names(tools, 0), [])


if __name__ == "__main__":
    unittest.main()
