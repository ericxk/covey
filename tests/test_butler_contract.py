"""The butler's shared pieces (docs/butler.md): the switch and topic id (core/butler.py), the
settings file (core/butler_settings.py), the tool channel (core/butler_requests.py) and the tool list
(core/butler_tool_specs.py)."""
from __future__ import annotations

import dataclasses
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from support import enter_context

from covey.core import env as cenv
from covey.core import butler_requests, butler, butler_settings as settings
from covey.core.butler_tool_specs import NAMES, TOOLS


class ButlerNamesTest(unittest.TestCase):
    def test_the_switch_allows_and_turns_it_on_unless_0(self):
        for value, on in (("1", True), (" 1 ", True), ("0", False), (" 0 ", False), ("true", True), ("", True)):
            with mock.patch.dict(os.environ, {butler.ENV: value}):
                self.assertIs(butler.available(), on, value)
                self.assertIs(butler.enabled(), on, value)

    def test_topic_id_is_hex_stable_and_per_chat(self):
        one = butler.butler_topic_id("oc_a")
        self.assertRegex(one, "^[0-9a-f]{32}$")
        self.assertEqual(one, butler.butler_topic_id("oc_a"))
        self.assertNotEqual(one, butler.butler_topic_id("oc_b"))

    def test_mcp_config_names_the_entry_and_the_job(self):
        unset = {name: value for name, value in os.environ.items() if not name.startswith(("COVEY_", "LARK_BRIDGE_"))}
        with mock.patch.dict(os.environ, unset, clear=True):  # None of the switches it hands on set.
            config = butler.mcp_config(Path("/r"), Path("/s"), "job-1", "/usr/bin/python3")
        server = config["mcpServers"][butler.MCP_SERVER]
        self.assertEqual(server["args"], ["-B", "/r/" + butler.MCP_ENTRY])
        self.assertEqual(server["env"], cenv.mirrored({butler.STATE_ENV: "/s", butler.JOB_ENV: "job-1"}))


class SettingsTest(unittest.TestCase):
    def test_missing_file_reads_the_defaults(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {butler.ENV: "1"}):
            loaded = settings.load(tmp)
        self.assertEqual(loaded, settings.Settings())
        self.assertEqual(loaded.compact_tokens, settings.COMPACT_TOKENS)

    def test_bad_fields_read_as_defaults(self):
        with mock.patch.dict(os.environ, {butler.ENV: "1"}):
            loaded = settings.from_json({"enabled": False, "mode": "topics", "compact_tokens": -1,  # Retired: read past.
                                         "start": ["readonly"], "trusted": [1, "x|read"],  # Retired: ignored.
                                         "topic_defaults": {"run_mode": "elsewhere", "redaction": "on"},
                                         "workspace_notes": {"a": "用途", "b": 3}})
        self.assertEqual(loaded.compact_tokens, settings.COMPACT_TOKENS)
        self.assertFalse(hasattr(loaded, "start") or hasattr(loaded, "trusted") or hasattr(loaded, "enabled"))
        self.assertEqual(loaded.topic_defaults.run_mode, "direct")
        self.assertEqual(loaded.workspace_notes, (("a", "用途"),))
        self.assertEqual(loaded.note("a"), "用途")

    def test_round_trip_and_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = settings.Settings(compact_tokens=0,
                                      topic_defaults=settings.TopicDefaults(workspace="ws"),
                                      workspace_notes=(("ws", "Bridge 仓库"),))
            settings.save(tmp, first)
            self.assertEqual(settings.load(tmp), first)
            self.assertEqual(json.loads((Path(tmp) / settings.FILE).read_text())["schema_version"], settings.SCHEMA)
            second = settings.update(tmp, compact_tokens=100_000)
            self.assertEqual(second.compact_tokens, 100_000)
            self.assertEqual(settings.load(tmp), second)

    def test_the_retired_start_and_trusted_fields_are_read_past_and_not_written_back(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {butler.ENV: "1"}):
            (Path(tmp) / settings.FILE).write_text(json.dumps({"start": ["readonly"], "trusted": ["ws|edit"], "model": "m"}))
            self.assertEqual(settings.load(tmp), settings.Settings(model="m"))
            settings.update(tmp, effort="high")
            written = json.loads((Path(tmp) / settings.FILE).read_text())
            self.assertNotIn("start", written)
            self.assertNotIn("trusted", written)

    def test_the_retired_review_model_is_read_past_and_kept_for_an_older_release(self):
        # 独立复核 was taken out (T937): main-thread.json's ``review`` loads, no field reads it, a save keeps it as it was.
        review = {"harness": "agy", "model": "gemini-4-neon", "effort": "xhigh"}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {butler.ENV: "1"}):
            (Path(tmp) / settings.FILE).write_text(json.dumps({"review": review, "model": "m"}))
            loaded = settings.load(tmp)
            self.assertEqual(loaded, settings.Settings(model="m"))
            self.assertFalse(hasattr(loaded, "review"))
            settings.update(tmp, effort="high")
            settings.save(tmp, settings.load(tmp))
            written = json.loads((Path(tmp) / settings.FILE).read_text())
            self.assertEqual((written["review"], written["effort"]), (review, "high"))

    def test_the_retired_redaction_fields_are_read_past_and_kept_for_an_older_release(self):
        # 敏感词脱敏 was taken out (T947): main-thread.json's ``redaction`` and ``topic_defaults.redaction`` load, no field reads
        # them, and every kind of save keeps them as the file has them (topic_defaults is written whole, its other keys from the settings).
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {butler.ENV: "1"}):
            (Path(tmp) / settings.FILE).write_text(json.dumps(
                {"redaction": True, "model": "m", "topic_defaults": {"redaction": True, "workspace": "ws"}}))
            loaded = settings.load(tmp)
            self.assertEqual(loaded, settings.Settings(model="m", topic_defaults=settings.TopicDefaults(workspace="ws")))
            self.assertFalse(hasattr(loaded, "redaction") or hasattr(loaded.topic_defaults, "redaction"))
            settings.update(tmp, effort="high")
            settings.save(tmp, dataclasses.replace(settings.load(tmp), topic_defaults=settings.TopicDefaults(model="x")))
            written = json.loads((Path(tmp) / settings.FILE).read_text())
            self.assertEqual((written["redaction"], written["effort"]), (True, "high"))
            self.assertEqual(written["topic_defaults"], {"redaction": True, "harness": "", "model": "x", "effort": "",
                                                         "run_mode": "direct", "workspace": ""})
            (Path(tmp) / settings.FILE).write_text(json.dumps({"topic_defaults": {"redaction": True}}))
            settings.update(tmp, compact_tokens=0)  # topic_defaults is rewritten whole.
            self.assertEqual(json.loads((Path(tmp) / settings.FILE).read_text())["topic_defaults"]["redaction"], True)

    def test_a_retired_harness_chosen_before_reads_as_none_chosen(self):
        # Pi was retired (T766): the butler, its topics and a lead chosen on it follow what is enabled again.
        choice = {"harness": "pi", "model": "gpt-6-astra", "effort": "high"}
        loaded = settings.from_json({**choice, "topic_defaults": {**choice, "run_mode": "worktree"}, "lead": choice})
        self.assertEqual((loaded.harness, loaded.model, loaded.effort), ("", "", ""))
        self.assertEqual((loaded.topic_defaults.harness, loaded.topic_defaults.model, loaded.topic_defaults.run_mode),
                         ("", "", "worktree"))
        self.assertEqual(loaded.lead, settings.LeadModel())
        kept = settings.from_json({"harness": "codex", "model": "gpt-x", "lead": {"harness": "agy", "model": "n"}})
        self.assertEqual((kept.harness, kept.model, kept.lead.harness, kept.lead.model), ("codex", "gpt-x", "agy", "n"))


class RequestsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def bridge(self, result, *, seen):
        """A stand-in for the Bridge's loop: answers the first request it claims."""
        deadline = time.time() + 5
        while time.time() < deadline:
            for request in butler_requests.pending(self.state):
                if butler_requests.claim(self.state, request.id):
                    seen.append(request)
                    butler_requests.answer(self.state, request.id, result)
                    return
            time.sleep(0.01)

    def test_a_call_and_its_answer_leave_no_files(self):
        seen = []
        worker = threading.Thread(target=self.bridge, args=({"ok": True, "text": "好", "data": {}},), kwargs={"seen": seen})
        worker.start()
        answer = butler_requests.submit(self.state, "topics_list", {"group": "waiting"}, job_key="k1", timeout=5, poll=0.01)
        worker.join()
        self.assertEqual(answer["text"], "好")
        self.assertEqual((seen[0].tool, seen[0].args, seen[0].job_key), ("topics_list", {"group": "waiting"}, "k1"))
        self.assertEqual(list(butler_requests.folder(self.state).iterdir()), [])

    def test_claimed_once(self):
        butler_requests._write(butler_requests.folder(self.state) / "r1.json",
                             {"id": "r1", "tool": "t", "args": {}, "job_key": "", "created_at": time.time()})
        self.assertTrue(butler_requests.claim(self.state, "r1"))
        self.assertFalse(butler_requests.claim(self.state, "r1"))
        self.assertEqual(butler_requests.pending(self.state), [])
        self.assertEqual([r.id for r in butler_requests.orphans(self.state)], ["r1"])
        butler_requests.answer(self.state, "r1", butler_requests.UNCERTAIN)
        self.assertEqual(butler_requests.orphans(self.state), [])

    def test_unclaimed_timeout_withdraws_the_request(self):
        clock = iter(range(0, 100, 10))
        answer = butler_requests.submit(self.state, "t", {}, job_key="", timeout=15, clock=lambda: next(clock),
                                      sleep=lambda seconds: None)
        self.assertFalse(answer["ok"])
        self.assertNotIn("uncertain", answer["data"])
        self.assertEqual(list(butler_requests.folder(self.state).iterdir()), [])

    def test_stale_requests_and_unread_answers_are_removed(self):
        base = butler_requests.folder(self.state)
        butler_requests._write(base / "old.json", {"id": "old", "tool": "t", "args": {}, "job_key": "", "created_at": 1.0})
        butler_requests._write(base / "gone.answer.json", {"ok": True})
        os.utime(base / "gone.answer.json", (1.0, 1.0))
        self.assertEqual(butler_requests.pending(self.state, now=10_000.0), [])
        self.assertEqual(sorted(p.name for p in base.iterdir()), [])


class ToolSpecsTest(unittest.TestCase):
    def test_each_tool_is_named_once_and_has_an_object_schema(self):
        self.assertEqual(len(NAMES), len(set(NAMES)))
        for tool in TOOLS:
            self.assertEqual(set(tool), {"name", "description", "input_schema"}, tool["name"])
            self.assertEqual(tool["input_schema"]["type"], "object", tool["name"])
            for required in tool["input_schema"].get("required", ()):
                self.assertIn(required, tool["input_schema"]["properties"], tool["name"])

    def test_the_hand_on_tools_ask_for_a_clean_task_and_say_what_holds_in_both_layouts(self):
        """T444 S1/S2: the butler writes the task, the Bridge adds the owner's words and attachments; no tool
        says an answer goes on the butler's card, which holds in no layout with a 话题区."""
        tools = {tool["name"]: tool for tool in TOOLS}
        for name in ("topic_start", "topic_send", "topic_fork"):
            schema = tools[name]["input_schema"]["properties"]
            self.assertIn("attachments", schema, name)
            words = schema["text" if name == "topic_send" else "prompt"]["description"]
            self.assertIn("不含路由用语", words, name)
            self.assertNotIn("原话照转", tools[name]["description"] + words, name)
        for name in ("topic_start", "topic_send"):
            self.assertIn("结论由 Bridge 发给主人，你不用等、也不用转述。", tools[name]["description"])
            self.assertIn("Bridge 会", tools[name]["description"])
        for tool in TOOLS:
            self.assertNotIn("一分钟内", tool["description"], tool["name"])
            self.assertNotIn("顶层", tool["description"], tool["name"])
        self.assertIn("title", tools["topic_send"]["input_schema"]["properties"])


class RetiredModeTest(unittest.TestCase):
    """The private chat mode (私聊顶层的消息) is retired (docs/legacy.md): COVEY_BUTLER alone turns the butler on or
    off, main-thread.json's ``mode`` and ``enabled`` are read past and a save keeps them for an older release."""

    def setUp(self):
        self.state = Path(enter_context(self, tempfile.TemporaryDirectory()))
        previous = butler.configure_state(self.state)
        self.addCleanup(butler.configure_state, previous)

    def test_the_switch_alone_decides_whatever_the_file_says(self):
        for data in ({"mode": "topics", "enabled": False}, {"mode": "agent"}, {"enabled": False}, {}):
            (self.state / settings.FILE).write_text(json.dumps(data))
            for switch, on in (("", True), ("1", True), ("0", False)):
                with self.subTest(data=data, switch=switch), mock.patch.dict(os.environ, {butler.ENV: switch}):
                    self.assertIs(butler.enabled(), on)

    def test_a_save_keeps_the_mode_as_the_file_has_it(self):
        (self.state / settings.FILE).write_text(json.dumps({"mode": "topics", "enabled": False, "model": "m"}))
        settings.update(self.state, effort="high")
        settings.save(self.state, settings.load(self.state))
        data = json.loads((self.state / settings.FILE).read_text())
        self.assertEqual((data["mode"], data["enabled"], data["model"], data["effort"]), ("topics", False, "m", "high"))
        (self.state / settings.FILE).unlink()
        settings.update(self.state, effort="high")  # A new file: no mode is written.
        self.assertFalse({"mode", "enabled"} & set(json.loads((self.state / settings.FILE).read_text())))
        self.assertFalse(hasattr(settings, "settle_mode") or hasattr(butler, "agent_mode") or hasattr(butler, "MODES"))


class TraitsFieldsTest(unittest.TestCase):
    """The rows' placement fields (docs/butler.md 和话题的不同): each is one row's way, BUTLER_OFF's spelt out."""

    def test_each_row_says_its_own(self):
        expected = {  # relays, has_thread, linkify_body
            butler.TOPIC: (True, True, False),
            butler.BUTLER: (False, False, True),
            butler.BUTLER_OFF: (False, False, False),
        }
        for row, values in expected.items():
            got = (row.relays, row.has_thread, row.linkify_body)
            self.assertEqual(got, values, row.name)
        self.assertFalse(hasattr(butler, "DESK"))  # Retired and frozen (core/migrate.py desk-retire).


class PlacementTest(unittest.TestCase):
    """core/placement.py: the layout from the switch, a cell for each layout and role."""

    def setUp(self):
        from covey.core import placement

        self.placement = placement
        self.state = Path(enter_context(self, tempfile.TemporaryDirectory()))
        previous = butler.configure_state(self.state)
        self.addCleanup(butler.configure_state, previous)
        enter_context(self, mock.patch.dict(os.environ, {butler.ENV: ""}))  # Unset: the butler on.

    def test_the_layout_follows_the_switch_alone(self):
        # desk_topics was retired on 2026-11-09 and the mode on 2026-11-28: a file that still says them is read past.
        p = self.placement
        (self.state / settings.FILE).write_text(json.dumps({"mode": "topics", "desk_topics": True}))
        for switch, layout in (("", p.AREA), ("1", p.AREA), ("0", p.TOPICS)):
            with mock.patch.dict(os.environ, {butler.ENV: switch}):
                self.assertEqual(p.layout(), layout, switch)
        self.assertFalse(hasattr(p, "DESK"))

    def test_every_layout_and_role_has_a_cell(self):
        p = self.placement
        self.assertEqual(set(p.TABLE), {(layout, role) for layout in (p.AREA, p.TOPICS)
                                        for role in (p.BUTLER_ROLE, p.TOPIC_ROLE, p.LEAD_ROLE, p.MEMBER_ROLE)})
        # A project's thread is no Traits row's (core/placement.py plan reads its topic.json); the lead is (LEAD).
        self.assertEqual(set(p.ROLES.values()), {p.BUTLER_ROLE, p.TOPIC_ROLE, p.LEAD_ROLE})

    def test_the_cells_the_call_sites_read(self):
        p = self.placement
        cells = {  # reply_top_level, answer, answer_on_card, conclusion, stop, question_copy, ask_block
            (p.AREA, p.BUTLER_ROLE): (False, p.TOP, p.ROUTES, p.NONE, p.NONE, p.TOP, True),
            (p.AREA, p.TOPIC_ROLE): (None, p.THREAD, p.NONE, p.TOP, p.TOP, p.TOP, True),
        }
        for role in (p.BUTLER_ROLE, p.TOPIC_ROLE):
            cells[(p.TOPICS, role)] = (False, p.THREAD, p.NONE, p.NONE, p.NONE, p.NONE, False)
        for key, values in cells.items():
            row = p.TABLE[key]
            got = (row.reply_top_level, row.answer, row.answer_on_card, row.conclusion, row.stop,
                   row.question_copy, row.ask_block)
            self.assertEqual(got, values, key)

    def test_a_plan_reads_the_row_and_the_layout(self):
        p = self.placement
        chat = "oc_chat"
        main = {"topic_id": butler.butler_topic_id(chat), "chat_id": chat}
        topic = {"topic_id": "ab" * 32, "chat_id": chat}
        self.assertEqual([(x.layout, x.role) for x in (p.plan(self.state, main), p.plan(self.state, topic))],
                         [(p.AREA, p.BUTLER_ROLE), (p.AREA, p.TOPIC_ROLE)])
        self.assertIs(p.plan(self.state, main).top_level(), False)  # In the owner's message's thread.
        (self.state / settings.FILE).write_text(json.dumps({"desk_topics": True}))  # Retired: as without it.
        self.assertIs(p.plan(self.state, main).top_level(), False)
        self.assertIs(p.plan(self.state, topic).top_level(), False)
        with mock.patch.dict(os.environ, {butler.ENV: "0"}):
            plan = p.plan(self.state, main)
        self.assertEqual((plan.traits, plan.role, plan.top_level()), (butler.BUTLER_OFF, p.BUTLER_ROLE, False))


class NoNameComparisonTest(unittest.TestCase):
    """The call sites read Traits fields and placement cells; only core/butler.py and core/placement.py compare
    a row's name with a role (docs/butler.md 和话题的不同)."""

    ROLES = {"butler", "butler_off", "topic", "lead", "lead_off", "project_member"}
    ALLOWED = {"core/butler.py", "core/placement.py"}

    def roles_in(self, node):
        import ast

        if isinstance(node, ast.Constant):
            return {node.value} & self.ROLES
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            return {e.value for e in node.elts if isinstance(e, ast.Constant)} & self.ROLES
        return set()

    def test_no_call_site_compares_a_name_with_a_role(self):
        import ast

        root = Path(__file__).resolve().parents[1] / "covey"
        found = []
        for path in sorted(root.rglob("*.py")):
            relative = path.relative_to(root).as_posix()
            if relative in self.ALLOWED:
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not isinstance(node, ast.Compare):
                    continue
                sides = [node.left, *node.comparators]
                named = any((isinstance(side, ast.Attribute) and side.attr == "name")
                            or (isinstance(side, ast.Name) and side.id == "name") for side in sides)
                if named and any(self.roles_in(side) for side in sides):
                    found.append(f"{relative}:{node.lineno}")
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
