"""The main thread's shared pieces (docs/main-thread.md): the switch and topic id (core/main_thread.py), the
settings file (core/main_thread_settings.py), the tool channel (core/main_requests.py) and the tool list
(core/main_tool_specs.py)."""
from __future__ import annotations

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
from covey.core import main_requests, main_thread, main_thread_settings as settings
from covey.core.main_tool_specs import NAMES, TOOLS


class MainThreadNamesTest(unittest.TestCase):
    def test_the_switch_allows_unless_0_and_without_a_mode_only_1_turns_it_on(self):
        for value, allowed, on in (("1", True, True), (" 1 ", True, True), ("0", False, False),
                                   (" 0 ", False, False), ("true", True, False), ("", True, False)):
            with mock.patch.dict(os.environ, {main_thread.ENV: value}):
                self.assertIs(main_thread.available(), allowed, value)
                self.assertIs(main_thread.enabled(), on, value)  # No state configured: no mode to read.

    def test_topic_id_is_hex_stable_and_per_chat(self):
        one = main_thread.main_topic_id("oc_a")
        self.assertRegex(one, "^[0-9a-f]{32}$")
        self.assertEqual(one, main_thread.main_topic_id("oc_a"))
        self.assertNotEqual(one, main_thread.main_topic_id("oc_b"))

    def test_mcp_config_names_the_entry_and_the_job(self):
        config = main_thread.mcp_config(Path("/r"), Path("/s"), "job-1", "/usr/bin/python3")
        server = config["mcpServers"][main_thread.MCP_SERVER]
        self.assertEqual(server["args"], ["-B", "/r/" + main_thread.MCP_ENTRY])
        self.assertEqual(server["env"], cenv.mirrored({main_thread.STATE_ENV: "/s", main_thread.JOB_ENV: "job-1"}))


class SettingsTest(unittest.TestCase):
    def test_missing_file_reads_the_defaults(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {main_thread.ENV: "1"}):
            loaded = settings.load(tmp)
        self.assertEqual(loaded, settings.Settings())
        self.assertEqual(loaded.compact_tokens, settings.COMPACT_TOKENS)
        self.assertEqual(loaded.start, settings.TIERS)

    def test_bad_fields_read_as_defaults(self):
        with mock.patch.dict(os.environ, {main_thread.ENV: "1"}):
            loaded = settings.from_json({"enabled": "yes", "mode": "both", "compact_tokens": -1, "start": "all",
                                         "trusted": [1, "x|read"],
                                         "topic_defaults": {"run_mode": "elsewhere", "redaction": "on"},
                                         "workspace_notes": {"a": "用途", "b": 3}})
        self.assertTrue(loaded.enabled)
        self.assertEqual(loaded.compact_tokens, settings.COMPACT_TOKENS)
        self.assertEqual(loaded.start, settings.TIERS)
        self.assertEqual(loaded.trusted, ("x|read",))
        self.assertEqual(loaded.topic_defaults.run_mode, "direct")
        self.assertFalse(loaded.topic_defaults.redaction)
        self.assertEqual(loaded.workspace_notes, (("a", "用途"),))
        self.assertEqual(loaded.note("a"), "用途")

    def test_round_trip_and_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = settings.Settings(enabled=False, compact_tokens=0, start=("requested",), trusted=("ws|edit",),
                                      topic_defaults=settings.TopicDefaults(workspace="ws", redaction=True),
                                      workspace_notes=(("ws", "Bridge 仓库"),))
            settings.save(tmp, first)
            self.assertEqual(settings.load(tmp), first)
            self.assertEqual(json.loads((Path(tmp) / settings.FILE).read_text())["schema_version"], settings.SCHEMA)
            second = settings.update(tmp, enabled=True)
            self.assertTrue(second.enabled)
            self.assertEqual(settings.load(tmp), second)

    def test_start_tiers(self):
        everything = settings.Settings(trusted=(settings.trust_key("ws", False),))
        starts = settings.starts_at_once
        self.assertTrue(starts(everything, workspace="x", readonly=True, user_requested=False))
        self.assertTrue(starts(everything, workspace="x", readonly=False, user_requested=True))
        self.assertTrue(starts(everything, workspace="ws", readonly=False, user_requested=False))
        self.assertFalse(starts(everything, workspace="x", readonly=False, user_requested=False))
        only_asked = settings.Settings(start=("requested",), trusted=everything.trusted)
        self.assertFalse(starts(only_asked, workspace="ws", readonly=True, user_requested=False))
        self.assertTrue(starts(only_asked, workspace="ws", readonly=True, user_requested=True))


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
            for request in main_requests.pending(self.state):
                if main_requests.claim(self.state, request.id):
                    seen.append(request)
                    main_requests.answer(self.state, request.id, result)
                    return
            time.sleep(0.01)

    def test_a_call_and_its_answer_leave_no_files(self):
        seen = []
        worker = threading.Thread(target=self.bridge, args=({"ok": True, "text": "好", "data": {}},), kwargs={"seen": seen})
        worker.start()
        answer = main_requests.submit(self.state, "topics_list", {"group": "waiting"}, job_key="k1", timeout=5, poll=0.01)
        worker.join()
        self.assertEqual(answer["text"], "好")
        self.assertEqual((seen[0].tool, seen[0].args, seen[0].job_key), ("topics_list", {"group": "waiting"}, "k1"))
        self.assertEqual(list(main_requests.folder(self.state).iterdir()), [])

    def test_claimed_once(self):
        main_requests._write(main_requests.folder(self.state) / "r1.json",
                             {"id": "r1", "tool": "t", "args": {}, "job_key": "", "created_at": time.time()})
        self.assertTrue(main_requests.claim(self.state, "r1"))
        self.assertFalse(main_requests.claim(self.state, "r1"))
        self.assertEqual(main_requests.pending(self.state), [])
        self.assertEqual([r.id for r in main_requests.orphans(self.state)], ["r1"])
        main_requests.answer(self.state, "r1", main_requests.UNCERTAIN)
        self.assertEqual(main_requests.orphans(self.state), [])

    def test_unclaimed_timeout_withdraws_the_request(self):
        clock = iter(range(0, 100, 10))
        answer = main_requests.submit(self.state, "t", {}, job_key="", timeout=15, clock=lambda: next(clock),
                                      sleep=lambda seconds: None)
        self.assertFalse(answer["ok"])
        self.assertNotIn("uncertain", answer["data"])
        self.assertEqual(list(main_requests.folder(self.state).iterdir()), [])

    def test_stale_requests_and_unread_answers_are_removed(self):
        base = main_requests.folder(self.state)
        main_requests._write(base / "old.json", {"id": "old", "tool": "t", "args": {}, "job_key": "", "created_at": 1.0})
        main_requests._write(base / "gone.answer.json", {"ok": True})
        os.utime(base / "gone.answer.json", (1.0, 1.0))
        self.assertEqual(main_requests.pending(self.state, now=10_000.0), [])
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
        """T444 S1/S2: the main thread writes the task, the Bridge adds the owner's words and attachments; no tool
        says an answer goes on the main thread's card, which holds in no layout with a 话题区."""
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


class PrivateModeTest(unittest.TestCase):
    """The private chat mode (docs/main-thread.md 私聊模式): the deployment allows the main thread unless the switch
    is 0, the owner's mode in main-thread.json turns it on, and the Bridge's first start writes the mode once."""

    def setUp(self):
        self.state = Path(enter_context(self, tempfile.TemporaryDirectory()))
        previous = main_thread.configure_state(self.state)
        self.addCleanup(main_thread.configure_state, previous)
        enter_context(self, mock.patch.dict(os.environ, {main_thread.ENV: ""}))

    def write(self, data):
        (self.state / settings.FILE).write_text(json.dumps(data))
        main_thread.forget_mode()  # Two writes in one clock tick with the same size share a stat stamp.

    def test_the_mode_decides_whatever_the_switch_but_0(self):
        for mode, switch, on in (("agent", "", True), ("agent", "1", True), ("agent", "0", False),
                                 ("topics", "1", False), ("topics", "", False)):
            self.write({"mode": mode, "enabled": not on})  # ``enabled`` is only for an older release.
            with mock.patch.dict(os.environ, {main_thread.ENV: switch}):
                self.assertIs(main_thread.enabled(), on, (mode, switch))

    def test_without_a_mode_it_reads_as_before(self):
        for data, switch, on in (({}, "1", True), ({"enabled": True}, "1", True), ({"enabled": False}, "1", False),
                                 ({"enabled": True}, "", False), ({}, "", False)):
            self.write(data)
            with mock.patch.dict(os.environ, {main_thread.ENV: switch}):
                self.assertIs(main_thread.enabled(), on, (data, switch))
                self.assertIs(settings.load(self.state).enabled, on, (data, switch))

    def test_a_file_another_process_writes_is_read_again_and_an_update_at_once(self):
        self.write({"mode": "agent"})
        self.assertTrue(main_thread.enabled())
        self.write({"mode": "topics", "x": 1})  # Another process: its stat differs.
        self.assertFalse(main_thread.enabled())
        settings.update(self.state, enabled=True)
        self.assertTrue(main_thread.enabled())
        data = json.loads((self.state / settings.FILE).read_text())
        self.assertEqual((data["mode"], data["enabled"]), ("agent", True))
        settings.update(self.state, enabled=False)
        self.assertFalse(main_thread.enabled())
        (self.state / settings.FILE).unlink()
        self.assertFalse(main_thread.enabled())  # No file, no mode: as before, off without the switch's 1.

    def test_a_new_deployment_settles_on_personal_agent(self):
        (self.state / "topics" / "aliases").mkdir(parents=True)  # Aliases alone are no topic.
        self.assertEqual(settings.settle_mode(self.state), "agent")
        self.assertTrue(main_thread.enabled())
        self.assertIsNone(settings.settle_mode(self.state))  # Once: the file has a mode.

    def test_a_deployment_with_records_keeps_its_way(self):
        for switch, saved, mode in (("", {}, "topics"), ("1", {}, "agent"), ("1", {"enabled": False}, "topics"),
                                    ("", {"enabled": True, "compact_tokens": 0}, "topics")):
            with self.subTest(switch=switch, saved=saved), tempfile.TemporaryDirectory() as tmp, \
                    mock.patch.dict(os.environ, {main_thread.ENV: switch}):
                state = Path(tmp)
                (state / "jobs").mkdir()
                (state / "jobs" / "om_1.json").write_text("{}")
                if saved:
                    (state / settings.FILE).write_text(json.dumps({**saved, "custom": "kept"}))
                self.assertEqual(settings.settle_mode(state), mode)
                data = json.loads((state / settings.FILE).read_text())
                self.assertEqual(data["mode"], mode)
                if saved:
                    self.assertEqual((data["custom"], data["compact_tokens"]),
                                     ("kept", saved.get("compact_tokens", settings.COMPACT_TOKENS)))
        with tempfile.TemporaryDirectory() as tmp:  # A topic without any job yet counts too.
            (Path(tmp) / "topics" / "abc").mkdir(parents=True)
            self.assertEqual(settings.settle_mode(tmp), "topics")



class TraitsFieldsTest(unittest.TestCase):
    """The rows' placement fields (docs/main-thread.md 和话题的不同): each is one row's way, MAIN_OFF's spelt out."""

    def test_each_row_says_its_own(self):
        expected = {  # relays, has_thread, linkify_body
            main_thread.TOPIC: (True, True, False),
            main_thread.MAIN: (False, False, True),
            main_thread.MAIN_OFF: (False, False, False),
        }
        for row, values in expected.items():
            got = (row.relays, row.has_thread, row.linkify_body)
            self.assertEqual(got, values, row.name)
        self.assertFalse(hasattr(main_thread, "DESK"))  # Retired and frozen (core/migrate.py desk-retire).


class PlacementTest(unittest.TestCase):
    """core/placement.py: the layout from the mode, a cell for each layout and role."""

    def setUp(self):
        from covey.core import placement

        self.placement = placement
        self.state = Path(enter_context(self, tempfile.TemporaryDirectory()))
        previous = main_thread.configure_state(self.state)
        self.addCleanup(main_thread.configure_state, previous)
        enter_context(self, mock.patch.dict(os.environ, {main_thread.ENV: ""}))

    def write(self, data):
        (self.state / settings.FILE).write_text(json.dumps(data))
        main_thread.forget_mode()

    def test_the_layout_follows_the_mode_alone(self):
        # desk_topics was retired on 2026-11-09: a file that still says true is read as false.
        p = self.placement
        for data, layout in (({"mode": "topics"}, p.TOPICS), ({"mode": "agent"}, p.AREA),
                             ({"mode": "agent", "desk_topics": True}, p.AREA),
                             ({"mode": "topics", "desk_topics": True}, p.TOPICS)):
            self.write(data)
            self.assertEqual(p.layout(), layout, data)
        self.assertFalse(hasattr(p, "DESK"))

    def test_every_layout_and_role_has_a_cell(self):
        p = self.placement
        self.assertEqual(set(p.TABLE), {(layout, role) for layout in (p.AREA, p.TOPICS)
                                        for role in (p.MAIN_ROLE, p.TOPIC_ROLE)})
        self.assertEqual(set(p.ROLES.values()), {p.MAIN_ROLE, p.TOPIC_ROLE})

    def test_the_cells_the_call_sites_read(self):
        p = self.placement
        cells = {  # reply_top_level, answer, answer_on_card, conclusion, stop, question_copy, ask_block
            (p.AREA, p.MAIN_ROLE): (False, p.TOP, p.ROUTES, p.NONE, p.NONE, p.TOP, True),
            (p.AREA, p.TOPIC_ROLE): (None, p.THREAD, p.NONE, p.TOP, p.TOP, p.TOP, True),
        }
        for role in (p.MAIN_ROLE, p.TOPIC_ROLE):
            cells[(p.TOPICS, role)] = (False, p.THREAD, p.NONE, p.NONE, p.NONE, p.NONE, False)
        for key, values in cells.items():
            row = p.TABLE[key]
            got = (row.reply_top_level, row.answer, row.answer_on_card, row.conclusion, row.stop,
                   row.question_copy, row.ask_block)
            self.assertEqual(got, values, key)

    def test_a_plan_reads_the_row_and_the_layout(self):
        p = self.placement
        chat = "oc_chat"
        main = {"topic_id": main_thread.main_topic_id(chat), "chat_id": chat}
        topic = {"topic_id": "ab" * 32, "chat_id": chat}
        self.write({"mode": "agent"})
        self.assertEqual([(x.layout, x.role) for x in (p.plan(self.state, main), p.plan(self.state, topic))],
                         [(p.AREA, p.MAIN_ROLE), (p.AREA, p.TOPIC_ROLE)])
        self.assertIs(p.plan(self.state, main).top_level(), False)  # In the owner's message's thread.
        self.write({"mode": "agent", "desk_topics": True})  # Retired: as without it.
        self.assertIs(p.plan(self.state, main).top_level(), False)
        self.assertIs(p.plan(self.state, topic).top_level(), False)
        self.write({"mode": "topics"})
        plan = p.plan(self.state, main)
        self.assertEqual((plan.traits, plan.role, plan.top_level()), (main_thread.MAIN_OFF, p.MAIN_ROLE, False))


class NoNameComparisonTest(unittest.TestCase):
    """The call sites read Traits fields and placement cells; only core/main_thread.py and core/placement.py compare
    a row's name with a role (docs/main-thread.md 和话题的不同)."""

    ROLES = {"main", "main_off", "topic"}
    ALLOWED = {"core/main_thread.py", "core/placement.py"}

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
