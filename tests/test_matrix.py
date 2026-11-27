"""入口 × Harness × 界面 (AGENTS.md 概念清单): every way into Covey on every core Harness, and where its turns show.

The entries are the rows: the butler (管家, the private chat's top level), a project's lead (负责人), a topic (话题: the
launch card, 换配置, the butler's topic_start) and a topic opened on the web page (网页). The columns are core/roster.py
CORE_HARNESSES. The surfaces are the Feishu cards (core/placement.py by role, the relay of a web topic to the private chat)
and the web page (app/web_collect.py). One subTest a cell, each asked the same way of every Harness; what a backend does in
detail stays in its own tests (test_claude, test_codex, test_agy, test_dsh, test_butler_harness, test_lead_harness).

话题 · 只读首轮 (LOOK, W1-C contract C6, gate G8) is a row of its own (``LookRowTest``): on each Harness, whether a turn the
butler started without the owner's click writes only its checkpoint and the topic's outputs directory (the path gate, its
``.git`` refusal, its shell) and whether the topic's next turn is free again; what the gate leaves to the words is in
``BOUNDS``, and an extension's Harness, outside the roster, in ``EXTENSION_CELLS``.

A cell left out is what the owner kept finding by hand: the butler only on Claude Code (T614), a lead that could not choose
its Harness (T664), the butler on Antigravity without topic_start (T750, T754), a topic opened on the web page whose outcome
never came back to the private chat (T762). A Harness added to the roster fails ``test_every_core_harness_says_how_its_cells_are_seen`` until ``SEEN`` says how its backend takes the Bridge's
tools and holds a lead to reading. A cell that does not hold on a Harness on purpose is in ``GAPS`` with why; it fails here
when it starts to hold, so the list stays true.
"""
from __future__ import annotations

import io
import json
import os
import shlex
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support import UNSET_OFF
from support import make_context
from support.records import write_job, write_topic
from support.world import UnifiedCase
from test_web_sync import T0, WebCase

from covey.app import butler_harness, lead_harness, relay_targets, tool_limits, web_launch
from covey.app.butler_model_choice import resolve
from covey.app.launch_models import available_models
from covey.backends.agy import home as agy_home
from covey.backends.agy import bridge as agy_bridge
from covey.backends.agy.bridge import LOOK_SETTINGS, READ_ONLY_SETTINGS, AntigravityBackend
from covey.backends.claude import argv as claude_argv
from covey.backends.claude import tool_guard
from covey.backends.claude.bridge import ClaudeBackend
from covey.backends.codex import appserver
from covey.backends.codex.workspace_reader import WorkspaceReader
from covey.backends.dsh import bridge as dsh_bridge
from covey.core import butler, butler_mcp, butler_settings, look_write, placement, projects
from covey.core.butler import butler_topic_id
from covey.core.butler_tool_specs import HANDING
from covey.core.harness import LEAD, LOOK, LOOK_ROOTS, READ, TOOL_LIMIT, backend_capabilities
from covey.core.history_storage import append_history
from covey.core.menu_choice import butler_harnesses, harness_of
from covey.core.redaction import TOPIC_KEY as REDACTION_KEY
from covey.core.roster import CORE_HARNESSES
from covey.core.state import write_json

BRIDGE = {"mcpServers": {"bridge": {"command": "python3", "args": ["mcp.py"], "env": {"K": "v"}}}}
# The Bridge's tools with every switch that adds one on: the most a Harness with a budget (Antigravity) has to fit.
BUTLER_ENV = {"COVEY_PROJECTS": "1", "COVEY_KNOWLEDGE": "1", "COVEY_HISTORY_CMD": "/absolute/history",
              "COVEY_PROJECT_TIDY": "suggest", "COVEY_BUTLER_RULES": "1"}
LEAD_ENV = {"COVEY_PROJECT_LEAD": "p1", "COVEY_PROJECT_TRIAGE": "1", "COVEY_COLLAB": "1", "COVEY_PROJECT_AUTOWAKE": "1"}


def names(tools) -> set:
    return {tool["name"] for tool in tools}


def claude_tools(ctx, tools) -> set:
    argv = claude_argv.stream_argv(ClaudeBackend(), ctx)
    config = json.loads(argv[argv.index("--mcp-config") + 1]) if "--mcp-config" in argv else {}
    return names(tools) if "bridge" in config.get("mcpServers", {}) else set()


def claude_lead(ctx) -> bool:
    argv = claude_argv.stream_argv(ClaudeBackend(), ctx)
    return argv[argv.index("--tools") + 1] == ",".join(claude_argv.READ_TOOLS) and "--mcp-config" in argv


def codex_tools(ctx, tools) -> set:
    params, _ = appserver.thread_params(ctx, None)
    return names(tools) if params["config"].get("mcp_servers.bridge.enabled") is True else set()


def codex_lead(ctx) -> bool:
    params, _ = appserver.thread_params(ctx, None)
    return params["sandbox"] == "read-only" and params["config"].get("mcp_servers.bridge.enabled") is True


def dsh_tools(ctx, tools) -> set:
    rows = dsh_bridge.mcp_rows(ctx.options.get("mcp_config"))
    return names(tools) if any(row["id"] == "mcp-bridge" for row in rows) else set()


def dsh_lead(ctx) -> bool:
    return (dsh_bridge.lead_turn(ctx) and "read-only" in dsh_bridge.LEAD_ARGS
            and "read-only" in dsh_bridge.DshBackend().prompt_runtime_note(ctx))


def agy_tools(ctx, tools) -> set:
    """What agy loads up front of the Bridge's server in the butler's private home: its tool budget is the only one."""
    with mock.patch.object(agy_home, "listed_tools", lambda server, env, timeout=agy_home.LIST_SECONDS: list(tools)):
        servers, _ = agy_home.agy_servers(ctx.options.get("mcp_config"), {})
    return set((servers.get("bridge") or {}).get("tools") or ())


def agy_lead(ctx) -> bool:
    argv = AntigravityBackend().argv(ctx, None)
    return "--settings" in argv and json.loads(argv[argv.index("--settings") + 1]) == READ_ONLY_SETTINGS


def _value(args, flag):
    return args[args.index(flag) + 1] if flag in args else ""


def claude_look(ctx) -> dict:
    """A LOOK turn on Claude Code: the look hook its argv carries, run as Claude Code runs it, on a Write call (``write``)
    and a Bash call (``shell``); ``free``: a turn without the limit has neither the hook nor --disallowedTools."""
    argv = claude_argv.stream_argv(ClaudeBackend(), ctx)
    settings = json.loads(_value(argv, "--settings") or "{}")
    hooks = [hook["command"] for group in (settings.get("hooks") or {}).get("PreToolUse") or ()
             for hook in group.get("hooks") or () if "--mode look" in hook.get("command", "")]

    def run(call) -> bool:  # Refused?
        args = shlex.split(hooks[0])[3:]
        with mock.patch("sys.stderr", io.StringIO()):
            return tool_guard.main(args, stdin=io.StringIO(json.dumps(call))) == 2
    if not hooks or "--disallowedTools" in argv:
        return {"write": None, "shell": None}
    return {"write": lambda path: run({"tool_name": "Write", "tool_input": {"file_path": path}}),
            "shell": lambda command, cwd: run({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd})}


def claude_free(ctx) -> bool:
    argv = claude_argv.stream_argv(ClaudeBackend(), ctx)
    return "--disallowedTools" not in argv and "--mode look" not in " ".join(argv)


def codex_look(ctx) -> dict:
    """A LOOK turn on Codex: the workspace server its thread starts, with its write tool (``write``); its shell is in the
    read-only sandbox for the thread and the turn (``shell``: every write refused by the sandbox, tests/test_collab_c8.py)."""
    params, _ = appserver.thread_params(ctx, None)
    args = params["config"]["mcp_servers.bridge_workspace.args"]
    approved = params["config"].get("mcp_servers.bridge_workspace.default_tools_approval_mode") == "approve"
    if "--allow-file" not in args or not approved:
        return {"write": None, "shell": None}
    reader = WorkspaceReader(_value(args, "--root"), allow_file=_value(args, "--allow-file"), allow_dir=_value(args, "--allow-dir"))

    def write(path) -> bool:
        try:
            reader.call("workspace_write_file", {"path": path, "content": "x"})
        except ValueError:
            return True
        return False
    sandboxed = params["sandbox"] == "read-only" and appserver.sandbox_mode(ctx) == "read-only"
    return {"write": write, "shell": (lambda command, cwd: True) if sandboxed else None}


def codex_free(ctx) -> bool:
    params, _ = appserver.thread_params(ctx, None)
    return params["sandbox"] != "read-only" and "--allow-file" not in params["config"]["mcp_servers.bridge_workspace.args"]


def agy_look(ctx) -> dict:
    """A LOOK turn on Antigravity: LOOK_SETTINGS deny its own file tools (agy 1.5.18: tests/test_collab_c8.py AgyLookC6),
    and its private home's server (bin/covey_look_write.py) is the write tool; its shell is not denied (``shell`` None)."""
    argv = AntigravityBackend().argv(ctx, None)
    server = ((agy_bridge.home_config(ctx) or {}).get("mcpServers") or {}).get("bridge_workspace") or {}
    args = list(server.get("args") or ())
    denied = json.loads(_value(argv, "--settings") or "{}") == LOOK_SETTINGS
    if not denied or "covey_look_write.py" not in " ".join(args):
        return {"write": None, "shell": None}

    def write(path) -> bool:
        try:
            look_write.call("workspace_write_file", {"path": path, "content": "x"},
                            allow_file=_value(args, "--allow-file"), allow_dir=_value(args, "--allow-dir"))
        except ValueError:
            return True
        return False
    shell = (lambda command, cwd: True) if "command(*)" in LOOK_SETTINGS["permissions"]["deny"] else None
    return {"write": write, "shell": shell}


def agy_free(ctx) -> bool:
    return "--settings" not in AntigravityBackend().argv(ctx, None) and agy_bridge.home_config(ctx) is None


def dsh_look(ctx) -> dict:
    """DeepSeek Harness has no gate for a LOOK turn: the words alone (app/tool_limits.py LOOK_WORDS)."""
    return {"write": None, "shell": None}


def dsh_free(ctx) -> bool:
    return not dsh_bridge.lead_turn(ctx)


# How each core Harness's backend is seen to take the Bridge's tools (the names a turn has at hand, given the server's
# list: all of them, or Antigravity's up-front ones), to hold a lead's turn to reading with those tools kept, and to hold a
# LOOK turn's writes (``look``: its write and shell gates as the backend wires them; ``free``: a turn without the limit).
SEEN = {
    "claude": {"tools": claude_tools, "lead": claude_lead, "look": claude_look, "free": claude_free},
    "codex": {"tools": codex_tools, "lead": codex_lead, "look": codex_look, "free": codex_free},
    "dsh": {"tools": dsh_tools, "lead": dsh_lead, "look": dsh_look, "free": dsh_free},
    "agy": {"tools": agy_tools, "lead": agy_lead, "look": agy_look, "free": agy_free},
}
LOOK_GATE, LOOK_GIT, LOOK_SHELL, LOOK_NEXT = ("话题 · 只读首轮 · 路径闸", "话题 · 只读首轮 · .git 拒写", "话题 · 只读首轮 · 命令行写",
                                              "话题 · 只读首轮 · 第二轮解限")
# Cells that do not hold on a Harness, on purpose or not yet, with why. A cell here that starts to hold fails
# test_the_known_gaps_are_still_gaps: take it off.
GAPS = {
    ("话题 · 群成员只读", "agy"): "agy 没声明 read_only：群成员只读的群里这一轮不启动（app/tool_limits.py refusal）。"
                                  "它给负责人用的 deny 规则（READ_ONLY_SETTINGS）也能守住只读，待接上",
    ("话题 · 群成员只读", "dsh"): "dsh 没声明 read_only：同上；它给负责人用的 --permission-mode read-only 待接上",
    ("话题 · 敏感词脱敏", "codex"): "设计如此：脱敏经 Claude 的词表中转，只有 Claude Code 能脱敏（docs/architecture.md 敏感词脱敏）",
    ("话题 · 敏感词脱敏", "dsh"): "设计如此：同上",
    ("话题 · 敏感词脱敏", "agy"): "设计如此：同上",
    (LOOK_GATE, "dsh"): "dsh 没声明 look_gate：只读首轮只有文字约束（app/tool_limits.py LOOK_WORDS：只写检查点和产物目录，"
                         "写不了就略过），没有路径闸",
    (LOOK_GIT, "dsh"): "同上：没有路径闸，.git 也只靠文字",
    (LOOK_SHELL, "agy"): "设计如此（规范 C6）：LOOK_SETTINGS 只拒 agy 自己的写文件工具、不拒 command(*)，留着跑只读调研脚本；"
                         "命令行能写仓库（RT-4 第 5 类：agy 1.5.18 实测 echo >> 写进了仓库文件，AgyLookC6）",
    (LOOK_SHELL, "dsh"): "同 dsh 路径闸：文字约束",
}
# What a LOOK turn's gate leaves to the words on a Harness whose cells above hold (RT-4 第 5 类 and W0-5's read worktree).
BOUNDS = {
    "claude": "命令行只拦明显的写（重定向、tee、sed -i、cp/mv/rm/touch/mkdir、dd of=、curl -o、find -delete、xargs 写命令、"
              "写类 git、covey repo use --write）；脚本自己写文件（python -c、make、npm install）拦不住，靠文字",
    "codex": "只读沙箱里命令行什么都写不了；covey repo use 要 fetch、建最新主干的只读 worktree、写 .used，在只读沙箱里做不到："
             "退回读主检出并标“可能落后”（core/repomap.py use 的 note），下一轮（非只读）再用就是最新主干",
    "agy": "命令行不拦（见 GAPS）；agy 1.5.18 的 edit_file 按 write_file 的权限名检查，FILE_WRITES 补齐的四个名字是防旧名和新版本的",
    "dsh": "只有文字约束（见 GAPS）",
}
# An extension's Harness, outside CORE_HARNESSES (each extension's own tests ask its cells).
EXTENSION_CELLS = {
    (LOOK_GATE, "扩展的 Harness"): "没声明 look_gate 的：只读首轮只有文字约束（app/tool_limits.py LOOK_WORDS：只写检查点和产物目录），"
                                  "扩展自己的提示对齐同一口径，由扩展自己的测试核对",
}


def held(cell, harness) -> bool:
    return (cell, harness) not in GAPS


def gap_words(cell, harness) -> str:
    """What a failure of the cell says to do."""
    if held(cell, harness):
        return f"{cell} × {harness} 不成立了：修好，或写进 GAPS 并写明原因"
    return f"{cell} × {harness} 已经成立：从 GAPS 去掉"


class HarnessCellsCase(UnifiedCase):
    """The UnifiedBridgeApp with every core Harness, the butler on (agent mode) in the owner's private chat."""

    def setUp(self):
        super().setUp()
        env = {name: value for name, value in os.environ.items() if not name.startswith(("COVEY_", "LARK_BRIDGE_"))}
        patch = mock.patch.dict(os.environ, {**env, **UNSET_OFF}, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.state = self.ctx.state_dir
        self.addCleanup(butler.configure_state, butler.configure_state(self.state))
        write_json(self.state / butler_settings.FILE, {"mode": "agent"})
        butler.forget_mode()
        self.addCleanup(butler.forget_mode)
        self.apps = self.app.apps
        self.choices = available_models(self.app.topics, self.ctx)
        self.chat = "oc_owner_dm"

    def capabilities(self, name):
        app = self.apps[name]
        return backend_capabilities(app.backend, app.harness.context)

    def menu_harnesses(self, choices) -> set:
        return {harness_of(key, entry) for key, entry in choices.items()}


class RowsTest(HarnessCellsCase):
    def test_every_core_harness_says_how_its_cells_are_seen(self):
        self.assertEqual(sorted(SEEN), sorted(CORE_HARNESSES), "a Harness of the roster without its row in SEEN: say how its "
                         "backend takes the Bridge's tools and holds a lead to reading")
        self.assertLessEqual({harness for _, harness in GAPS}, set(CORE_HARNESSES))
        self.assertTrue(all(why.strip() for why in GAPS.values()))

    def test_every_core_harness_is_enabled_in_the_bridge_the_tests_build(self):
        self.assertLessEqual(set(CORE_HARNESSES), set(self.apps))
        self.assertLessEqual(set(CORE_HARNESSES), self.menu_harnesses(self.choices))


class ButlerRowTest(HarnessCellsCase):
    """管家 × Harness."""

    def test_the_butler_runs_on_every_harness_and_alone_falls_back_to_it(self):
        for name in CORE_HARNESSES:
            with self.subTest(harness=name):
                app = self.apps[name]
                self.assertTrue(butler_harness.takes_tools(app))
                self.assertEqual(butler_harness.default_name({name: app}), name)

    def test_the_settings_card_offers_every_harness_for_the_butler(self):
        offered = set(butler_harnesses(self.choices))
        for name in CORE_HARNESSES:
            with self.subTest(harness=name):
                self.assertIn(name, offered)

    def test_the_butlers_tools_that_hand_work_on_are_at_hand_on_every_harness(self):
        tools = butler_mcp.tool_list(BUTLER_ENV)
        wanted = HANDING & names(tools)
        self.assertTrue({"topic_start", "topic_send", "topic_fork", "project_send"} <= wanted)
        ctx = make_context(self.root, options={"mcp_config": BRIDGE})
        for name in CORE_HARNESSES:
            with self.subTest(harness=name):
                at_hand = SEEN[name]["tools"](ctx, tools)
                self.assertTrue(at_hand, "the Bridge's server does not reach the model")
                self.assertEqual(sorted(wanted - at_hand), [])


class LeadRowTest(HarnessCellsCase):
    """负责人 × Harness."""

    def test_a_lead_runs_on_every_harness(self):
        ctx = make_context(self.root, options={TOOL_LIMIT: LEAD})
        for name in CORE_HARNESSES:
            with self.subTest(harness=name):
                app = self.apps[name]
                self.assertTrue(lead_harness.runs_lead(app))
                self.assertEqual(lead_harness.refusal(self.apps, name), "")
                self.assertEqual(tool_limits.refusal(ctx, self.capabilities(name), name), "")

    def test_every_harness_is_a_leads_choice_on_the_cards_and_the_page(self):
        offered = self.menu_harnesses(lead_harness.lead_choices(self.choices))
        for name in CORE_HARNESSES:
            with self.subTest(harness=name):
                self.assertIn(name, offered)

    def test_where_the_butler_redacts_a_new_lead_takes_a_harness_only_if_it_redacts(self):
        """The lead's 脱敏 is the butler's when it is made (app/lead_harness.py binding): a Harness the butler's call names
        that cannot redact gives way to the butler's own, so no lead turn goes out unredacted. (换配置 on the 专项卡 can turn
        the lead's 脱敏 off afterwards, as a topic's: docs/projects.md 负责人用哪个 Harness.)"""
        root = {"harness_id": "claude", "model": "claude-model", REDACTION_KEY: True}
        for name in CORE_HARNESSES:
            with self.subTest(harness=name):
                found = lead_harness.binding(self.apps, root, call={"harness": name, "model": f"{name}-model"})
                self.assertEqual(found["source"] == "call", self.capabilities(name).redaction)
                found = lead_harness.binding(self.apps, {**root, REDACTION_KEY: False}, call={"harness": name})
                self.assertEqual((found["source"], found["harness_id"]), ("call", name))

    def test_a_leads_turn_is_held_to_reading_with_its_tools_on_every_harness(self):
        tools = butler_mcp.tool_list(LEAD_ENV)
        wanted = HANDING & names(tools)
        self.assertTrue({"project_topic_start", "project_topic_send"} <= wanted)
        ctx = make_context(self.root, options={TOOL_LIMIT: LEAD, "mcp_config": BRIDGE})
        for name in CORE_HARNESSES:
            with self.subTest(harness=name):
                self.assertTrue(SEEN[name]["lead"](ctx))
                self.assertEqual(sorted(wanted - SEEN[name]["tools"](ctx, tools)), [])


class TopicRowTest(HarnessCellsCase):
    """话题 × Harness: the launch card and 换配置 list the menu (app/launch_models.py available_models); the butler's
    topic_start names a Harness (app/butler_model_choice.py resolve)."""

    def test_the_launch_card_and_the_switch_card_list_every_harness(self):
        listed = self.menu_harnesses(self.choices)
        for name in CORE_HARNESSES:
            with self.subTest(harness=name):
                self.assertIn(name, listed)

    def test_the_butlers_topic_start_opens_a_topic_on_every_harness(self):
        for name in CORE_HARNESSES:
            with self.subTest(harness=name):
                found = resolve(self.app.topics, self.state, self.chat, butler_settings.TopicDefaults(), {"harness": name},
                                self.choices)
                self.assertIsInstance(found, dict, found)
                self.assertEqual(found["harness"], name)

    def test_a_group_members_read_only_turn(self):
        ctx = make_context(self.root, options={TOOL_LIMIT: READ})
        for name in CORE_HARNESSES:
            with self.subTest(harness=name):
                self.assertEqual(tool_limits.refusal(ctx, self.capabilities(name), name) == "", held("话题 · 群成员只读", name),
                                 gap_words("话题 · 群成员只读", name))

    def test_redaction(self):
        for name in CORE_HARNESSES:
            with self.subTest(harness=name):
                self.assertEqual(self.capabilities(name).redaction, held("话题 · 敏感词脱敏", name),
                                 gap_words("话题 · 敏感词脱敏", name))


class WebRowTest(HarnessCellsCase):
    """网页 × Harness: the page's 新话题 (app/web_launch.py catalog) and a topic it opened relaying to the private chat."""

    def test_the_web_page_opens_a_topic_on_every_harness(self):
        env = SimpleNamespace(topics=self.app.topics, runtime=self.app.runtime)
        models = web_launch.catalog(env, self.ctx, self.chat)["models"]
        offered = {model["harness"] for model in models}
        for name in CORE_HARNESSES:
            with self.subTest(harness=name):
                self.assertIn(name, offered)
                self.assertTrue(all(model["bridge_tools"] for model in models if model["harness"] == name))

    def test_the_web_page_opens_a_topic_in_the_home_on_every_harness_without_a_workspace(self):
        # Data contract v43: ``workspace`` and ``run_mode`` are optional; none, and the topic starts in the home.
        env = SimpleNamespace(topics=self.app.topics, runtime=self.app.runtime, state_dir=self.ctx.state_dir, profile=self.ctx.profile)
        models = web_launch.catalog(env, self.ctx, self.chat)["models"]
        for name in CORE_HARNESSES:
            with self.subTest(harness=name):
                model = next(model for model in models if model["harness"] == name)
                plan = web_launch.plan_of(env, self.ctx, self.chat, {"model": model["key"], "effort": model["start_budget"]})
                self.assertEqual((plan["harness"], plan["run_mode"], plan["start"], plan["home_start"]), (name, "direct", True, True))
                self.assertEqual(plan["workdir"], str(self.ctx.default_workdir.resolve()))

    def test_a_topic_opened_on_the_web_page_relays_to_the_private_chat_on_every_harness(self):
        for name in CORE_HARNESSES:
            with self.subTest(harness=name):
                topic = f"web-{name}"
                write_topic(self.state, topic_id=topic, chat_id=self.chat, harness_id=name, topic_number=7,
                            root_id=f"om_root_{name}", origin={"kind": "web", "request": f"web-op-{name}", "title": "页面开的"})
                target = relay_targets.area_target(self.state, topic, self.chat)
                self.assertIsNotNone(target)
                self.assertEqual((target["chat_id"], target["reply_to"], target["kind"]), (self.chat, f"om_root_{name}", "follow"))


class FeishuSurfaceTest(unittest.TestCase):
    """界面 · 飞书: each entry's turn has a place in the private chat (core/placement.py), whatever its Harness."""

    def test_every_entry_says_its_answer_somewhere(self):
        rows = {role: placement.TABLE[(placement.AREA, role)] for role in
                (placement.BUTLER_ROLE, placement.LEAD_ROLE, placement.TOPIC_ROLE, placement.MEMBER_ROLE)}
        for role, row in rows.items():
            with self.subTest(role=role):
                self.assertNotEqual(row.answer, placement.NONE)
        # A topic's outcome and its questions come back to the top level; a project member's go to its lead.
        self.assertEqual((rows["topic"].conclusion, rows["topic"].stop, rows["topic"].question_copy),
                         (placement.TOP, placement.TOP, placement.TOP))
        self.assertEqual(placement.TABLE[(placement.TOPICS, placement.TOPIC_ROLE)].answer, placement.THREAD)


class WebSurfaceTest(WebCase):
    """界面 · 网页: the butler's, the lead's, a topic's and a web-opened topic's turns are pushed to the page on every
    Harness, each a topic item of its kind naming its Harness, with the agent's answer as a turn."""

    def entries(self, name):
        """{topic id: kind} of the four entries written on ``name``, each with one finished turn."""
        butler_id = butler_topic_id(self.chat)
        lead_id = projects.lead_topic_id(self.chat, self.pid)
        write_topic(self.state, topic_id=butler_id, chat_id=self.chat, harness_id=name, main_thread=True,
                    workdir=str(self.state))
        write_topic(self.state, topic_id=lead_id, chat_id=self.chat, harness_id=name,
                    project=projects.project_field(self.pid, projects.LEAD))
        write_topic(self.state, topic_id="t-topic", chat_id=self.chat, harness_id=name, topic_number=7,
                    origin={"title": "普通话题"})
        write_topic(self.state, topic_id="t-web", chat_id=self.chat, harness_id=name, topic_number=8,
                    root_id="om_web_root", origin={"kind": "web", "request": "web-op-1", "title": "网页开的"})
        found = {butler_id: "butler", lead_id: "lead", "t-topic": "topic", "t-web": "topic"}
        for number, topic in enumerate(found, 1):
            write_job(self.state, f"k-{number}", topic_id=topic, chat_id=self.chat, job_id=number, status="done",
                      model=f"{name}-model", harness_id=name, message_id=f"om_m{number}",
                      timings={"backend_started": time.time() - 60})
            history = self.state / "topics" / topic / "history.jsonl"
            append_history(history, "user", "做件事", {"message_id": f"om_m{number}"})
            append_history(history, "assistant", f"{name} 做完了", {})
        return found

    def check(self, name):
        found = self.entries(name)
        self.sync.push_once(T0)
        by = {(item["type"], item["key"]): item["data"] for item in self.web.items()}
        for topic, kind in found.items():
            with self.subTest(harness=name, topic=topic):
                item = by.get(("topic", topic))
                self.assertIsNotNone(item, "not on the web page")
                self.assertEqual((item["kind"], item["harness"]), (kind, name))
                turns = [data for (kind_, _), data in by.items() if kind_ == "turn" and data.get("topic_key") == topic]
                self.assertIn(f"{name} 做完了", [turn.get("body") for turn in turns])

    def setUp(self):
        super().setUp()
        self.chat, self.pid = "oc_owner_dm", "a1b2c3d4e5f6"


for _name in CORE_HARNESSES:
    setattr(WebSurfaceTest, f"test_the_four_entries_show_on_the_web_page_on_{_name}", lambda self, n=_name: self.check(n))


class LookRowTest(HarnessCellsCase):
    """话题 · 只读首轮 (LOOK) × Harness (W1-C contract C6, G8): a turn the butler started without the owner's click, as
    each backend wires it with the turn's ``look_roots`` (``held`` its Harness's look_gate). The path gate writes the
    checkpoint and the outputs directory and refuses a repository file, another job's checkpoint and a shared read
    worktree; ``.git`` is refused through a link too; the shell's obvious writes are refused where the Harness can; the
    topic's next turn is as before."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        self.file = root / "state" / "checkpoints" / "om_look-7.json"
        self.dir = root / "state" / "topics" / "t1" / "outputs"
        self.repo = root / "home" / "covey"
        self.read = root / "data" / "worktrees" / "_read" / "covey" / "b4d687c49c3a"
        for folder in (self.file.parent, self.dir, self.repo / ".git", self.read):
            folder.mkdir(parents=True)
        (self.repo / "app.py").write_text("old\n")
        (self.dir / "gitlink").symlink_to(self.repo / ".git")
        self.home = root / "home"

    def look_ctx(self, name, limit=True):
        held = self.capabilities(name).look_gate
        options = {TOOL_LIMIT: LOOK, LOOK_ROOTS: {"file": str(self.file), "dir": str(self.dir), "held": held}} if limit else {}
        return make_context(self.home.parent, workdir=self.home, default_workdir=self.home, options=options)

    def test_the_path_gate(self):
        for name in CORE_HARNESSES:
            with self.subTest(harness=name):
                write = SEEN[name]["look"](self.look_ctx(name))["write"]
                holds = write is not None and not write(str(self.file)) and not write(str(self.dir / "r.md")) and all(
                    write(str(path)) for path in (self.repo / "app.py", self.file.parent / "om_other-8.json",
                                                  self.read / "app.py", self.dir / ".." / "x.md"))
                self.assertEqual(holds, held(LOOK_GATE, name), gap_words(LOOK_GATE, name))
                if holds:
                    self.assertEqual((self.repo / "app.py").read_text(), "old\n")

    def test_git_is_refused_through_a_link_too(self):
        for name in CORE_HARNESSES:
            with self.subTest(harness=name):
                write = SEEN[name]["look"](self.look_ctx(name))["write"]
                holds = write is not None and write(str(self.dir / "gitlink" / "config")) and write(str(self.repo / ".git" / "config"))
                self.assertEqual(holds, held(LOOK_GIT, name), gap_words(LOOK_GIT, name))

    def test_the_shells_obvious_writes(self):
        for name in CORE_HARNESSES:
            with self.subTest(harness=name):
                shell = SEEN[name]["look"](self.look_ctx(name))["shell"]
                holds = shell is not None and shell(f"echo x >> {self.repo}/app.py", str(self.repo)) and shell(
                    "git commit -am x", str(self.repo))
                if holds and name == "claude":  # Reading runs.
                    self.assertFalse(shell("rg -n old . && git log --oneline -3", str(self.repo)))
                    self.assertFalse(shell("covey repo use covey", str(self.repo)))
                self.assertEqual(holds, held(LOOK_SHELL, name), gap_words(LOOK_SHELL, name))

    def test_the_next_turn_is_free_again(self):
        for name in CORE_HARNESSES:
            with self.subTest(harness=name):
                self.assertTrue(SEEN[name]["free"](self.look_ctx(name, limit=False)), gap_words(LOOK_NEXT, name))

    def test_every_harness_says_what_the_gate_leaves_to_the_words(self):
        self.assertEqual(sorted(BOUNDS), sorted(CORE_HARNESSES))
        self.assertTrue(all(why.strip() for why in (*BOUNDS.values(), *EXTENSION_CELLS.values())))


class KnownGapsTest(HarnessCellsCase):
    def test_the_known_gaps_are_still_gaps(self):
        """The cells GAPS lists are asked above (test_a_group_members_read_only_turn, test_redaction, LookRowTest): one
        that holds now fails there; this one keeps GAPS to cells those tests ask."""
        self.assertLessEqual({cell for cell, _ in GAPS}, {"话题 · 群成员只读", "话题 · 敏感词脱敏", LOOK_GATE, LOOK_GIT,
                                                          LOOK_SHELL})


# 重启后再点一次 (WAVE12 G4, T904): each module that writes a mid-state (tests/golden/midstates.json, which test_structure's
# MidStateTest keeps true), on 飞书 and on 网页: the test that leaves the mid-state as a restart would and clicks (or ticks) again,
# "不适用：" with why the surface has none, or "缺口：" with what does not hold yet.
NOT_A_CLICK = "不适用：一轮里的步骤进度，跟着任务进程走，Bridge 重启时由启动恢复收尾（core/recovery.py），没有可点的"
RESTART_CELLS = {
    "covey/app/batch_switch.py": {
        "飞书": "test_batch_switch.MidStateTest.test_a_switching_whose_process_is_gone_is_settled_by_the_next_click_and_retried",
        "网页": "不适用：批量换配置只在飞书私聊的卡上确认，网页没有这张卡"},
    "covey/app/butler_asks.py": {
        "飞书": "test_butler_asks.HandOnTest.test_a_step_that_raised_before_or_in_the_send_is_said_as_what_it_left",
        "网页": "不适用：转给管家的话由任务轮发出，网页上没有这一步"},
    "covey/app/limit_notice.py": {
        "飞书": "test_limit_notice.NoticeTest.test_a_notice_left_sending_by_a_bridge_that_stopped_goes_again_under_its_key",
        "网页": "不适用：合并提示只发到飞书私聊"},
    "covey/app/owner_send.py": {
        "飞书": "test_owner_send.ClickTests.test_maybe_sent_is_never_sent_again",
        "网页": "不适用：代发草稿只在飞书卡上确认"},
    "covey/app/questions.py": {
        "飞书": "test_questions.QuestionTest.test_restart_with_unacknowledged_saved_answer_never_replays",
        "网页": "test_web_sync.InboxTest.test_a_restart_does_not_do_an_operation_again_and_an_unfinished_one_is_not_replayed"},
    "covey/app/relay_send.py": {
        "飞书": "test_butler_relay.SendTest.test_an_unconfirmed_send_is_never_sent_again",
        "网页": "不适用：回带只发到飞书私聊顶层，网页按任务记录现算（WebSurfaceTest）"},
    "covey/app/status_cards.py": {
        "飞书": "test_status_cards.SendOutcomeTest.test_a_card_left_creating_by_a_bridge_that_stopped_goes_again_under_its_key_with_its_card",
        "网页": "不适用：话题状态卡只在飞书话题里"},
    "covey/app/steer_receipts_flow.py": {
        "飞书": "test_steer.ReceiptActionTest.test_an_error_while_requeuing_is_unconfirmed_and_never_queued_again",
        "网页": "不适用：补充回执卡只在飞书话题里"},
    "covey/app/steering.py": {
        "飞书": "test_steer.QueuedSteerTest.test_card_target_survives_restart_and_is_visible_before_click",
        "网页": "test_web_sync.OpLaneTest.test_one_taken_by_an_earlier_process_is_still_failed_as_uncertain"},
    "covey/app/tidy_run.py": {
        "飞书": "缺口：Bridge 在整理途中停下，这次整理的记录留在 running，整理卡停在“正在整理”；重启后再点整理开的是新一次，旧卡不收尾",
        "网页": "缺口：网页的整理面板读到这条没有 finished_at 的 running 记录仍显示在整理（app/tidy_web.py last_run）"},
    "covey/app/web_collect.py": {
        "飞书": "不适用：只是推给网页的进度字段", "网页": "不适用：每次推送按任务记录现算，不落盘，重启后重算"},
    "covey/app/web_login.py": {
        "飞书": "test_web_login.ClickTests.test_only_the_owner_once_per_event_on_the_card_it_sent",
        "网页": "test_web_login.SendTests.test_a_card_feishu_refuses_is_a_failure_that_can_be_sent_again"},
    "covey/backends/claude/compaction.py": {"飞书": NOT_A_CLICK, "网页": NOT_A_CLICK},
    "covey/backends/claude/events.py": {"飞书": NOT_A_CLICK, "网页": NOT_A_CLICK},
    "covey/backends/codex/events.py": {"飞书": NOT_A_CLICK, "网页": NOT_A_CLICK},
    "covey/core/deliveries.py": {
        "飞书": "test_steer.QueuedSteerTest.test_an_unconfirmed_supplement_never_runs_the_message_again",
        "网页": "test_web_sync.InboxTest.test_a_restart_does_not_do_an_operation_again_and_an_unfinished_one_is_not_replayed"},
    "covey/core/longtask.py": {"飞书": "不适用：只在内存里算进度，不落盘", "网页": "不适用：只在内存里算进度，不落盘"},
    "covey/core/steps.py": {"飞书": NOT_A_CLICK, "网页": NOT_A_CLICK},
    "covey/core/trace.py": {"飞书": NOT_A_CLICK, "网页": NOT_A_CLICK},
    "covey/service/manager.py": {
        "飞书": "不适用：服务管理器自己的进程状态，重启时按 desired_state 重新拉起服务",
        "网页": "不适用：服务管理器自己的进程状态，重启时按 desired_state 重新拉起服务"},
}
RESTART_WORDS = ("不适用：", "缺口：")


class RestartColumnTest(unittest.TestCase):
    """重启后再点一次 × 飞书 / 网页 (WAVE12 G4): every cell names a test that is there, or says why the surface has none or
    what does not hold yet; a cell of a module that writes no mid-state any more fails test_structure's MidStateTest."""

    def test_every_cell_names_a_test_that_is_there_or_says_why_not(self):
        """Read from the test files' source: importing one may skip it here (a test module that needs the extension)."""
        import ast
        from pathlib import Path
        defined = {}
        for module, surfaces in RESTART_CELLS.items():
            for surface, cell in surfaces.items():
                with self.subTest(module=module, surface=surface):
                    if cell.startswith(RESTART_WORDS):
                        self.assertTrue(cell.split("：", 1)[1].strip(), "写明原因")
                        continue
                    name, cls, method = cell.split(".")
                    if name not in defined:
                        tree = ast.parse((Path(__file__).parent / f"{name}.py").read_text(encoding="utf-8"))
                        defined[name] = {(node.name, item.name) for node in tree.body if isinstance(node, ast.ClassDef)
                                         for item in node.body if isinstance(item, ast.FunctionDef)}
                    self.assertIn((cls, method), defined[name], f"{cell} 不存在：改成现有的测试")


class AgyBudgetTest(unittest.TestCase):
    """管家、负责人 × Antigravity: agy loads a turn's tools up front within EAGER_BYTES and drops the largest whole past it
    (T750). Every tool but SECONDARY, of the butler and of a lead under every switch, fits strictly within the room, so
    each of them is loaded up front; the memory tools and repos_list are SECONDARY, and the old names are not listed."""

    SWITCHES = {"COVEY_PROJECT_LEAD": ("", "p1"), "COVEY_KNOWLEDGE": ("0", "1"), "COVEY_HISTORY_CMD": ("", "/bin/true"),
                "COVEY_BUTLER_RULES": ("0", "1"), "COVEY_PROJECTS": ("0", "1"), "COVEY_PROJECT_TIDY": ("off", "suggest"),
                "COVEY_PROJECT_TRIAGE": ("0", "1"), "COVEY_COLLAB": ("0", "1"), "COVEY_PROJECT_AUTOWAKE": ("0", "1")}
    OLD = {"workspaces_list", "principle_save", "principle_delete", "principles_list", "knowledge_save", "knowledge_update",
           "knowledge_drop", "knowledge_search"}

    def test_every_tool_but_the_secondary_ones_is_loaded_up_front_on_agy(self):
        import itertools
        from covey.core.butler_tool_specs import MEMORY_TOOLS, SECONDARY
        self.assertLessEqual({tool["name"] for tool in MEMORY_TOOLS} | {"repos_list"}, SECONDARY)
        for values in itertools.product(*self.SWITCHES.values()):
            environ = dict(zip(self.SWITCHES, values))
            tools = butler_mcp.tool_list(environ)
            listed = names(tools)
            first = [tool for tool in tools if tool["name"] not in SECONDARY]
            used = sum(map(agy_home.tool_size, first))
            with self.subTest(**{key: value for key, value in environ.items() if value}):
                self.assertFalse(listed & self.OLD)
                self.assertLess(used, agy_home.EAGER_BYTES, "the tools agy loads first outgrew its room: shorten them")
                self.assertLessEqual(names(first), set(agy_home.eager_names(tools, agy_home.EAGER_BYTES)))


if __name__ == "__main__":
    unittest.main()
