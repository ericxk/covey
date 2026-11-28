"""入口 × Harness × 界面 (AGENTS.md 概念清单): every way into Covey on every core Harness, and where its turns show.

The entries are the rows: the butler (管家, the private chat's top level), a project's lead (负责人), a topic (话题: the
launch card, 换配置, the butler's topic_start) and a topic opened on the web page (网页). The columns are core/roster.py
CORE_HARNESSES. The surfaces are the Feishu cards (core/placement.py by role, the relay of a web topic to the private chat)
and the web page (app/web_collect.py). One subTest a cell, each asked the same way of every Harness; what a backend does in
detail stays in its own tests (test_claude, test_codex, test_agy, test_dsh, test_butler_harness, test_lead_harness).

规则 (gate G8, W2-A) is a row of its own (``RulesRowTest``): on each Harness, how its turn asks the owner, whether a
resumed turn takes the anchor in place of the whole rules (C9),
whether a high context is told, whether the butler is reminded past its own tool calls and whether a search of the whole
home is refused, on the command line and inside a program.

话题 · 外部话题 (T923 §5, ``ExternalRowTest``): a topic opened with ``local`` false, which keeps off this machine's
repositories and files: on each Harness, whether its turn leaves the home's own instruction files out (Claude Code's
claudeMdExcludes, Codex's project_doc_max_bytes=0, Antigravity started in the topic's outputs directory), in a seed home and
a taken-over one, whether its rules carry nothing of the home's repositories, and whether an ordinary topic is as before.

A cell left out is what the owner kept finding by hand: the butler only on Claude Code (T614), a lead that could not choose
its Harness (T664), the butler on Antigravity without topic_start (T750, T754), a topic opened on the web page whose outcome
never came back to the private chat (T762). A Harness added to the roster fails ``test_every_core_harness_says_how_its_cells_are_seen`` until ``SEEN`` says how its backend takes the Bridge's
tools and holds a lead to reading. A cell that does not hold on a Harness on purpose is in ``GAPS`` with why; it fails here
when it starts to hold, so the list stays true.
"""
from __future__ import annotations

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
from support.git import init_git_repo
from support.records import write_job, write_topic
from support.world import UnifiedCase
from support.butler import switch_on
from test_web_sync import T0, WebCase

from covey.app import butler_harness, home_turn, lead_harness, prompts, relay_targets, repos_turn, rules_sent, tool_limits, web_launch
from covey.app.butler_model_choice import resolve
from covey.app.launch_models import available_models
from covey.backends.agy import home as agy_home
from covey.backends.agy import bridge as agy_bridge
from covey.backends.agy.bridge import READ_ONLY_SETTINGS, AgyControl, AntigravityBackend, AntigravityEventAdapter
from covey.backends.claude import argv as claude_argv
from covey.backends.claude import tool_guard
from covey.backends.claude.bridge import ClaudeBackend
from covey.backends.claude.events import ClaudeEventAdapter
from covey.backends.claude.hooks import TurnHooks
from covey.backends.codex import appserver
from covey.backends.codex.turn import context_of as codex_context_of
from covey.backends.dsh import bridge as dsh_bridge
from covey.core import butler, butler_mcp, butler_settings, home as covey_home, placement, projects, repomap, topic_repos
from covey.core.butler import butler_topic_id
from covey.core.butler_tool_specs import HANDING
from covey.core.harness import EXTERNAL, LEAD, READ, TOOL_LIMIT, backend_capabilities
from covey.core.history_storage import append_history
from covey.core.menu_choice import butler_harnesses, harness_of
from covey.core.roster import CORE_HARNESSES
from covey.core.state import write_json
from covey.core.steer import SteerRefused

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


# 话题 · 外部话题 (T923 §5, VERIFY-P1 第 5 节): whether a turn whose options carry EXTERNAL leaves ``home``'s own instruction
# files out on each backend, as the backend wires it.
ABOVE = ("AGENTS.md", "CLAUDE.md", "CLAUDE.local.md")


def claude_external(ctx, home) -> bool:
    """Claude Code: the turn's --settings claudeMdExcludes has the home's five and the top-level three of the directory above
    it, and no .claude/ outside the home (under HOME that is the user's own CLAUDE.md, V2)."""
    argv = claude_argv.stream_argv(ClaudeBackend(), ctx)
    excluded = json.loads(_value(argv, "--settings") or "{}").get("claudeMdExcludes") or []
    want = {str(home / name) for name in (*ABOVE, ".claude/CLAUDE.md", ".claude/rules/**")}
    want |= {str(home.parent / name) for name in ABOVE}
    return want <= set(excluded) and not any("/.claude/" in path and not path.startswith(str(home) + os.sep)
                                             for path in excluded)


def codex_external(ctx, home) -> bool:
    """Codex: its app server reads no project doc (-c project_doc_max_bytes=0, V3)."""
    flags = appserver.server_flags(ctx)
    return appserver.NO_PROJECT_DOC in flags and flags[flags.index(appserver.NO_PROJECT_DOC) - 1] == "-c"


def agy_external(ctx, home) -> bool:
    """Antigravity: no setting turns its directory rules off (V1), so its process starts in the topic's outputs directory
    (tests/test_agy.py: the process's own cwd), its file tools let out of that workspace."""
    settings = json.loads(_value(AntigravityBackend().argv(ctx, None), "--settings") or "{}")
    return (covey_home.external_cwd(ctx) == (ctx.options.get(EXTERNAL) or {}).get("dir") != str(ctx.workdir)
            and settings.get("allowNonWorkspaceAccess") is True and AntigravityBackend().capabilities(ctx).external_cwd)


def dsh_external(ctx, home) -> bool:
    """DeepSeek Harness: nothing of its own (GAPS)."""
    return False


def claude_external_free(ctx) -> bool:
    argv = claude_argv.stream_argv(ClaudeBackend(), ctx)
    return "claudeMdExcludes" not in json.loads(_value(argv, "--settings") or "{}")


def codex_external_free(ctx) -> bool:
    return appserver.NO_PROJECT_DOC not in appserver.server_flags(ctx)


def agy_external_free(ctx) -> bool:
    settings = _value(AntigravityBackend().argv(ctx, None), "--settings")
    return covey_home.external_cwd(ctx) == str(ctx.workdir) and "allowNonWorkspaceAccess" not in settings


EXTERNAL_SEEN = {
    "claude": (claude_external, claude_external_free),
    "codex": (codex_external, codex_external_free),
    "dsh": (dsh_external, lambda ctx: True),
    "agy": (agy_external, agy_external_free),
}


# 规则 (G8, W2-A): the native question tool each backend wires for an ordinary turn ('' none), and the token_usage it
# reports for a turn whose last request read a large context (the window too, when the backend knows it).
def claude_asks(ctx) -> str:
    """Claude Code's AskUserQuestion, answered by its control (backends/claude/control.py), unless the turn's argv takes it
    away."""
    argv = claude_argv.stream_argv(ClaudeBackend(), ctx)
    return "" if "AskUserQuestion" in _value(argv, "--disallowedTools").split(",") else "AskUserQuestion"


def codex_asks(ctx) -> str:
    """Codex's request_user_input, on for the turn by its app server's switch."""
    return appserver.QUESTION_TOOL if appserver.QUESTION_FLAG in appserver.server_flags(ctx) else ""


def agy_asks(ctx) -> str:
    """Antigravity has no native question: its control refuses an answer."""
    try:
        AgyControl(lambda *args: None).answer("r1", {})
    except SteerRefused:
        return ""
    return "?"


def dsh_asks(ctx) -> str:
    """DeepSeek Harness has none: its turn asks through the checkpoint."""
    return ""


def claude_context() -> dict:
    events = []
    adapter = ClaudeEventAdapter(lambda kind, data: events.append((kind, dict(data))))
    adapter.accept({"type": "system", "subtype": "init", "session_id": "s1", "model": "claude/opus"})
    adapter.accept({"type": "stream_event", "session_id": "s1", "event": {"type": "message_start", "message": {
        "id": "m1", "usage": {"input_tokens": 10, "cache_read_input_tokens": 700000, "cache_creation_input_tokens": 0,
                                     "output_tokens": 1}}}})
    adapter.accept({"type": "stream_event", "session_id": "s1", "event": {"type": "message_delta", "usage": {"output_tokens": 5}}})
    adapter.accept({"type": "result", "subtype": "success", "session_id": "s1", "is_error": False, "result": "ok",
                    "modelUsage": {"claude/opus": {"contextWindow": 1000000}}})
    return next((data for kind, data in reversed(events) if kind == "token_usage"), {})


def codex_context() -> dict:
    return codex_context_of({"last": {"totalTokens": 240000}, "modelContextWindow": 258400})


def agy_context() -> dict:
    events = []
    adapter = AntigravityEventAdapter(lambda kind, data: events.append((kind, dict(data))))
    adapter.accept({"event": "step_update", "step_update": {
        "step_index": 1, "step_type": "agent_response", "state": "DONE", "text_delta": "ok", "duration_seconds": 1.0,
        "usage": {"output_tokens": 50, "input_tokens": 700000, "cache_read_tokens": 100000}}}, 1.0)
    return next((data for kind, data in reversed(events) if kind == "token_usage"), {})


def dsh_context() -> dict:
    events = []
    adapter = dsh_bridge.DshEventAdapter(lambda kind, data: events.append((kind, dict(data))))
    adapter.accept({"type": "status", "phase": "step_start"}, 1.0)
    adapter.accept({"type": "status", "phase": "step_end", "usage": {"inputTokens": 700000, "outputTokens": 50}}, 2.0)
    return next((data for kind, data in reversed(events) if kind == "token_usage"), {})


RULES_SEEN = {
    "claude": {"asks": claude_asks, "context": claude_context},
    "codex": {"asks": codex_asks, "context": codex_context},
    "dsh": {"asks": dsh_asks, "context": dsh_context},
    "agy": {"asks": agy_asks, "context": agy_context},
}


# How each core Harness's backend is seen to take the Bridge's tools (the names a turn has at hand, given the server's
# list: all of them, or Antigravity's up-front ones) and to hold a lead's turn to reading with those tools kept.
SEEN = {
    "claude": {"tools": claude_tools, "lead": claude_lead},
    "codex": {"tools": codex_tools, "lead": codex_lead},
    "dsh": {"tools": dsh_tools, "lead": dsh_lead},
    "agy": {"tools": agy_tools, "lead": agy_lead},
}
RULE_ASK, RULE_ANCHOR, RULE_CONTEXT, RULE_NUDGE, RULE_SEARCH, RULE_WALK = (
    "规则 · 提问方式", "规则 · 系统级规则", "规则 · 上下文偏高提醒", "规则 · 管家工具提醒",
    "规则 · 主目录搜索 · 命令行", "规则 · 主目录搜索 · 程序内递归")
EXT_FILES, EXT_GIT, EXT_FREE = ("话题 · 外部话题 · 不读主目录说明文件", "话题 · 外部话题 · 主目录在 git 工作树里",
                                 "话题 · 外部话题 · 普通话题照旧")
# Cells that do not hold on a Harness, on purpose or not yet, with why. A cell here that starts to hold fails
# test_the_known_gaps_are_still_gaps: take it off.
GAPS = {
    (EXT_FILES, "dsh"): "dsh 会不会读工作目录的 AGENTS.md、有没有关掉的设置都没实测过（T923 VERIFY-P1 只测了 Claude Code、Codex、"
                        "agy）：外部话题只靠外部版主目录段和边界句",
    (EXT_GIT, "dsh"): "同上：没有实测过的隔离办法",
    (EXT_GIT, "agy"): "agy 1.5.19 没有关闭目录规则的设置（--settings 拒收 memoryRulesDisabled、rulesDiscoveryConfig，文档无此类键），"
                      "外部话题靠把 cwd 设成产物目录来隔离；主目录在 git 工作树里时 agy 会读到仓库根的 AGENTS.md，隔离不成立"
                      "（这时 cwd 不改，core/home.py external_start）",
    ("话题 · 群成员只读", "agy"): "agy 没声明 read_only：群成员只读的群里这一轮不启动（app/tool_limits.py refusal）。"
                                  "它给负责人用的 deny 规则（READ_ONLY_SETTINGS）也能守住只读，待接上",
    ("话题 · 群成员只读", "dsh"): "dsh 没声明 read_only：同上；它给负责人用的 --permission-mode read-only 待接上",
    (RULE_CONTEXT, "agy"): "agy 只报上下文大小（context_tokens），不报窗口：stream 的 init/step/result 行、agy models 和本机会话库"
                           "都没有窗口字段（agy 1.5.18，W2-A 查过），context_high 不知道窗口就不提醒",
    (RULE_CONTEXT, "dsh"): "dsh 只报上下文大小，不报窗口：网关模型目录只有 id、协议、显示名和推理档位（docs/harnesses.md）；"
                           "网关目录加了窗口字段再接",
    (RULE_NUDGE, "dsh"): "dsh 不能执行中补充（没声明 steering），Bridge 送不进提醒，它也没有自己的 hook",
    (RULE_WALK, "codex"): "搜索垫片（core/home.py SEARCH_SHIMS）只看命令行：Python 的 os.walk、rglob、glob('**')、git grep 这类"
                          "程序自己递归的看不到；只有 Claude Code 的钩子能看到命令行里内联的 Python",
    (RULE_WALK, "agy"): "同上：垫片看不到程序内递归",
    (RULE_WALK, "dsh"): "同上：垫片看不到程序内递归",
}
# An extension's Harness, outside CORE_HARNESSES (each extension's own tests ask its cells).
EXTENSION_CELLS = {
    (RULE_ASK, "扩展的 Harness"): "声明了 question_tool 的，管家规则写它的名字当场问；没声明的写检查点 questions（app/prompts.py "
                                 "ask_words），扩展自己的测试核对它声明的工具真接得住",
    (RULE_NUDGE, "扩展的 Harness"): "没声明 nudges 又能执行中补充（steering）的，由 Bridge 数工具步骤送提醒（app/interaction.py "
                                   "count_for_nudge），和核心的 Codex、agy 一样",
    (RULE_SEARCH, "扩展的 Harness"): "本机进程的 PATH 都由 core/home.py env 给，同样经过搜索垫片；程序内递归同样看不到",
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
        switch_on(self)
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

    def test_a_leads_harness_is_the_one_the_call_names_whatever_the_butlers_topic_had(self):
        """The lead's Harness is the one the butler's call names on every Harness (app/lead_harness.py binding); an older
        butler topic's ``redaction`` (敏感词脱敏 was taken out, T947) holds none back."""
        root = {"harness_id": "claude", "model": "claude-model", "redaction": True}
        for name in CORE_HARNESSES:
            with self.subTest(harness=name):
                found = lead_harness.binding(self.apps, root, call={"harness": name, "model": f"{name}-model"})
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


    def test_a_leads_question_on_every_harness_woken_or_with_the_owner_there(self):
        """负责人提问 × Harness × 叫醒/在场 (W1-A, WAVE12 C4): a lead's turn has one line in place of the checkpoint contract
        and is not told it may change code; the question its project_ask leaves registers with origin project_ask, the same
        whether the turn was a wake-up or the owner's."""
        from covey.app import prompts
        from covey.app.runs import LEAD_RULE

        lead = projects.lead_topic_id(self.chat, "a1b2c3d4e5f6")
        ctx = make_context(self.root, options={TOOL_LIMIT: LEAD})
        for name in CORE_HARNESSES:
            for number, mid in enumerate(("lead-a1b2c3d4e5f6-3", "om_owner_said"), 1):
                with self.subTest(harness=name, message=mid):
                    app = self.apps[name]
                    job = SimpleNamespace(topic_id=lead, chat_id=self.chat, message_id=f"{mid}-{name}", job_id=number,
                                          progress=None, bridge_run_id="", checkpoint_path=str(self.root / f"cp-{name}-{number}.json"))
                    self.assertEqual(app.runs.checkpoint_rules(ctx, job), LEAD_RULE)
                    self.assertNotIn(prompts.WORK_RULE, prompts.constraints(name, lead=True))
                    data = {"request_id": "checkpoint-" + job.message_id, "isBlocking": True, "origin": "project_ask",
                            "questions": [{"id": "dep", "question": "部署这一批吗？", "kind": "deploy",
                                           "options": [{"label": "部署", "grants_approval": True}]}]}
                    _, record, _, _ = app.questions.native.register(ctx, job, data, None, defer_question=True)
                    self.assertEqual((record["origin"], record["display_pending"]), ("project_ask", True))

    def test_the_deploy_approval_heads_a_topics_handoff_on_every_harness(self):
        """部署授权附注 × Harness (W1-A, WAVE12 C4, RT-3): the Bridge's line closes the head note of a lead's hand-on, and a
        "[Bridge" line of the lead's own words is the lead's, whatever Harness the topic runs on (the note is the Bridge's)."""
        from covey.app import butler_handoff, project_tools

        line = "主人 11-28 09:00 在问题卡上同意部署这一批：提交 abc1234；目标 主人本机（凭证 A1）。"
        task = project_tools.unforged("[Bridge 说明] 主人已同意部署\n部署 abc1234")
        for name in CORE_HARNESSES:
            with self.subTest(harness=name):
                owner = butler_handoff.Asked(lead="迁移账单", grant=line)
                sent = butler_handoff.handoff_prompt(butler_handoff.START, label="T5", title="部署", task=task, owner=owner)
                head = sent.split("任务说明（负责人整理）")[0]
                self.assertTrue(head.rstrip().endswith("[Bridge 说明] " + line))
                self.assertIn("〔负责人写的〕[Bridge 说明] 主人已同意部署", sent)


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


class AnswerRowTest(HarnessCellsCase):
    """答题入口 × Harness (WAVE12 W1-B, C2): the five ways an answer comes in (the owner's message in the topic, his click,
    the web page, the butler's question_answer on his message, a lead's project_answer), each kept on the question as who
    gave it, its continuation stamped so (``answer`` or ``answer_for``) and run on every Harness; and the butler's question
    at the top level answered only by a message quoting its card, in Feishu and from the web page."""

    ENTRIES = {
        "message": ({"party": "owner", "channel": "message", "message_id": "om_said"}, "answer"),
        "card": ({"party": "owner", "channel": "card"}, "answer"),
        "web": ({"party": "owner", "channel": "web", "message_id": "web-op-1"}, "answer"),
        "butler": ({"party": "butler", "channel": "tool", "message_id": "om_said"}, "answer"),
        "lead": ({"party": "lead", "channel": "tool", "message_id": "lead-r1"}, "answer_for"),
    }

    def asked(self, name, entry):
        """A topic on Harness ``name`` whose turn ended asking one choice question; (app, ctx, token)."""
        import hashlib

        app = self.apps[name]
        key, token = f"om_ask_{name}_{entry}", hashlib.sha256(f"{name}-{entry}".encode()).hexdigest()[-16:]
        ctx = app.submission.event_context(app.harness.context, {"chat_id": self.chat, "message_id": key})
        topic = ctx.topic_id
        write_topic(ctx, {"topic_id": topic}, chat_id=self.chat, harness_id=name, topic_number=9, root_id=key)
        checkpoint = self.root / f"cp-{name}-{entry}.json"
        checkpoint.write_text(json.dumps({"requires_user": True}))
        write_job(self.state, key, job_id=40, message_id=key, topic_id=topic, chat_id=self.chat, status="waiting_user",
                  checkpoint_path=str(checkpoint), backend=name, model=f"{name}-model", effort="high",
                  sender_id=self.app.runtime.owner_open_id or "")
        write_json(self.state / "questions" / f"{token}.json", {
            "token": token, "job_id": 40, "job_key": key, "topic_id": topic, "chat_id": self.chat, "status": "pending",
            "blocking": True, "request_id": "checkpoint-" + key, "created_at": 1, "question_message_id": f"om_q_{token}",
            "questions": [{"id": "fmt", "question": "用哪种格式？", "options": [{"label": "表格"}, {"label": "列表"}]}]})
        return app, ctx, token

    def answer(self, app, ctx, token, entry):
        from covey.app.answers import AnswerBy

        owner = self.app.runtime.owner_open_id or ""
        if entry == "message":
            app.questions.submit_answer(ctx, self.chat, token, {"answer_0": "表格"}, "om_said",
                                        by=app.interaction.answerer(owner, "message", "om_said"))
        elif entry == "card":
            app.interaction.interaction_action(ctx, {
                "chat_id": self.chat, "event_id": f"ev-{token}", "message_id": f"om_q_{token}", "operator_id": owner,
                "action_value": {"kind": "bridge_question", "topic_id": ctx.topic_id, "token": token},
                "form_value": {"option_0": "0"}})
        else:
            party, channel, message = {"web": ("owner", "web", "web-op-1"), "butler": ("butler", "tool", "om_said"),
                                       "lead": ("lead", "tool", "lead-r1")}[entry]
            app.questions.submit_answer(ctx, self.chat, token, {"option_0": "0"}, f"{entry}-{token}",
                                        by=AnswerBy(party, channel, open_id=owner if party != "lead" else "",
                                                    message_id=message))

    def test_every_way_to_answer_says_who_and_its_continuation_runs_on_every_harness(self):
        from covey.core import turn_source

        self.app.runtime.owner_open_id = self.app.runtime.owner_open_id or "ou_owner"
        for name in CORE_HARNESSES:
            for entry, (kept, kind) in self.ENTRIES.items():
                with self.subTest(harness=name, entry=entry), \
                        mock.patch("covey.core.model_settings.strict_recorded_model_settings",
                                   return_value=(f"{name}-model", "high")):
                    app, ctx, token = self.asked(name, entry)
                    app.questions.paint_question_state = mock.Mock()
                    app.cards.repaint_browse_card = mock.Mock(return_value=True)
                    self.answer(app, ctx, token, entry)
                    saved = json.loads((self.state / "questions" / f"{token}.json").read_text())
                    self.assertEqual(saved["answer_by"], kept)
                    job = next(job for job in app.runtime.jobs.queue.queue if job.message_id == "answer-" + token)
                    self.assertEqual((job.turn_source["kind"], job.turn_source["origin"]["question"]), (kind, token))
                    self.assertEqual(turn_source.refusal(job, capabilities=self.capabilities(name)), "")

    def test_the_butlers_question_at_the_top_level_is_answered_only_quoting_its_card(self):
        from covey.app.questions import Questions

        ctx = self.apps["claude"].submission.event_context(self.apps["claude"].harness.context,
                                                           {"chat_id": self.chat, "message_id": "om_butler"})
        topic = butler_topic_id(self.chat)
        write_json(self.state / "questions" / ("b" * 16 + ".json"), {
            "token": "b" * 16, "job_id": 41, "job_key": "om_b_ask", "topic_id": topic, "chat_id": self.chat,
            "status": "pending", "blocking": True, "request_id": "checkpoint-om_b_ask", "created_at": 1, "at_top": True,
            "question_message_id": "om_bq", "mirror_message_id": "om_bq_top",
            "questions": [{"id": "where", "question": "放到哪个话题区做？", "options": [{"label": "新开"}]}]})
        import dataclasses

        ctx = dataclasses.replace(ctx, topic_id=topic)
        app = self.apps["claude"]
        cases = {  # (surface, the message id, what it quotes): an answer?
            ("飞书", "不引用"): ("om_new", "", False),
            ("飞书", "引用题卡"): ("om_quote", "om_bq", True),
            ("飞书", "引用顶层副本"): ("om_quote_top", "om_bq_top", True),
            ("网页", "不引用"): ("web-op-9", "", False),  # The page's words to the butler quote nothing.
        }
        with mock.patch("covey.core.harness.backend_capabilities",
                        return_value=SimpleNamespace(questions=True, steering=True)), \
                mock.patch.object(Questions, "pending_questions",
                                  lambda self, ctx, chat: [json.loads((ctx.state_dir / "questions" / ("b" * 16 + ".json"))
                                                                      .read_text())]):
            for (surface, how), (message, quoted, answers) in cases.items():
                with self.subTest(surface=surface, how=how), mock.patch.object(app.questions, "submit_answer") as submit:
                    taken = app.interaction.interactive_command(ctx, "我看看还有没有别的方案", self.chat, message,
                                                                by=self.app.runtime.owner_open_id or "ou_owner",
                                                                **({"quoted": quoted} if quoted else {}))
                    self.assertEqual((bool(taken), submit.called), (answers, answers))


class HandedRowTest(HarnessCellsCase):
    """转进在跑任务的话 × Harness (WAVE12 W2-B, the fourth way of app/steering.py end_unread_steers): words the butler (``relay``)
    or a project's lead (``lead``) handed to a topic's running turn that the turn never took become that topic's next turn on
    its own Harness, with the source they came with, which that Harness runs; with a question waiting there they are held
    for its answer instead."""

    SOURCES = {"relay": {"kind": "relay", "asserted_by": "owner", "origin": {"message_id": "om_said"}, "via": "butler"},
               "lead": {"kind": "lead", "asserted_by": "lead", "origin": {"topic": "lead-topic"}, "via": "lead"}}

    def handed(self, name, kind, *, waiting=False):
        """A topic on Harness ``name`` whose turn took words of ``kind`` and ended without them; the jobs it then has."""
        import threading
        from covey.core.deliveries import delivery_path, delivery_token, save_delivery

        app = self.apps[name]
        key = f"om_run_{name}_{kind}"
        ctx = app.submission.event_context(app.harness.context, {"chat_id": self.chat, "message_id": key, "root_id": key,
                                                                 "thread_id": "omt_" + key})
        self.assertIs(butler.traits(ctx.topic_id, self.chat), butler.TOPIC)  # An ordinary topic's thread.
        topic = ctx.history_path.parent / "topic.json"
        write_json(topic, {**json.loads(topic.read_text()), "harness_id": name, "topic_number": 9})
        mid = f"main-{name}-{kind}"
        save_delivery(delivery_path(self.state, ctx.topic_id, mid), {
            "status": "refused", "text": "补充", "topic_id": ctx.topic_id, "chat_id": self.chat, "job_id": 41, "job_key": key,
            "token": delivery_token(ctx.topic_id, mid), "mid": mid, "saved_at": 1.0,
            # app/butler_topics.py next_turn's, written out.
            "requeue": {"text": "把文档也改了", "prompt": "NEXT：把文档也改了", "topic": True, "title": "改文档",
                        "source": self.SOURCES[kind]}})
        job = SimpleNamespace(cancel_event=threading.Event(), topic_id=ctx.topic_id, chat_id=self.chat, message_id=key,
                              job_id=41, timings={})
        record = json.loads(delivery_path(self.state, ctx.topic_id, mid).read_text())
        app.steering.hold = mock.Mock(return_value=waiting)
        queued, enqueue = [], app.steering.enqueue_chat_job
        app.steering.enqueue_chat_job = lambda *a, **k: queued.append(enqueue(*a, **k)) or queued[-1]
        with mock.patch("covey.app.steering.background.start", side_effect=lambda _, fn, *a: fn(*a)):
            app.steering.requeue_lost(app.harness.context, job, [record])
        app.steering.enqueue_chat_job = enqueue
        return app, [each for each in queued if each is not None and each.topic_id == ctx.topic_id]

    def test_handed_words_a_turn_never_took_are_its_next_turn_on_every_harness(self):
        from covey.core import turn_source

        for name in CORE_HARNESSES:
            for kind in self.SOURCES:
                with self.subTest(harness=name, kind=kind):
                    app, (again,) = self.handed(name, kind)
                    self.assertTrue(again.message_id.startswith("requeue-"))
                    self.assertEqual((turn_source.kind(again), again.prompt_text), (kind, "NEXT：把文档也改了"))
                    self.assertEqual(turn_source.refusal(again, capabilities=self.capabilities(name)), "")

    def test_with_a_question_waiting_they_are_held_on_every_harness(self):
        for name in CORE_HARNESSES:
            with self.subTest(harness=name):
                app, queued = self.handed(name, "relay", waiting=True)
                self.assertEqual(queued, [])
                app.steering.hold.assert_called_once()


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


EXTERNAL_GOLDEN = Path(__file__).resolve().parent / "golden" / "external_topic_prompt.txt"
FORMS = ("seed", "接管")  # A home Covey made (core/home.py seed_home) and a taken-over one with its own AGENTS.md.


class ExternalRowTest(HarnessCellsCase):
    """话题 · 外部话题 × Harness (T923 §5 and §10, VERIFY-P1 第 5 节): a topic the butler or a lead opened with ``local`` false
    (T922: reading a paper) in a seed home and a taken-over one. Its first turn's whole prompt (the rules, the message, the
    tail, the read-only note) has its outputs directory and the boundary line and nothing of the home's repositories; each
    Harness leaves the home's instruction files out its own way (``EXTERNAL_SEEN``), Antigravity only outside a git work
    tree; a topic without the field (T877: reading training code) is as before, and ``covey repo use`` still works in an
    external one."""

    TOPIC = "e" * 32
    NOT_SAID = ("仓库地图", "repos.md", "covey repo use <仓库> --write", "先遵守这里的 AGENTS.md", "alpha", "回一句就行", "按 X 做了")

    def setUp(self):
        super().setUp()
        self.user = self.root / "user"  # The user's HOME: the home is under it, its ~/.claude/CLAUDE.md the user's own.
        (self.user / ".claude").mkdir(parents=True)
        (self.user / ".claude" / "CLAUDE.md").write_text("USER_OWN\n")
        patch = mock.patch.dict(os.environ, {"HOME": str(self.user)})
        patch.start()
        self.addCleanup(patch.stop)
        topic_repos.cache.clear()

    def make_home(self, form) -> Path:
        """A home of ``form`` under the user's HOME, its home.json and repo map (``alpha``, a repository in it) written."""
        home = self.user / f"work-{form}"
        home.mkdir()
        if form == "seed":
            covey_home.seed_home(home)
            covey_home.write(self.state, covey_home.HomeConfig())
        else:
            (home / "AGENTS.md").write_text("先遵守这里的规矩：主人的工作区。\n")
            covey_home.write(self.state, covey_home.HomeConfig(
                runs_template="{home}/misc-tasks/runs/{date}-{slug}-{topic8}/{run}",
                sessions_template="{home}/misc-tasks/sessions/{date}-{slug}-{topic8}"))
        alpha = home / "alpha"
        (alpha / ".git").mkdir(parents=True)
        (alpha / "AGENTS.md").write_text("ALPHA_RULES\n")
        repomap.write(self.state, [repomap.Repo(name="alpha", path=str(alpha), write_mode="inplace")])
        repomap.render(self.state, home)
        return home

    def topic_ctx(self, home, name, *, local=False):
        origin = {"kind": "main", "request": "r1", "job_key": "main-job", "why": "解读论文",
                  "user_requested": True, "title": "解读论文", "at": T0}
        if local is not None:
            origin["local"] = local
        history = self.state / "topics" / self.TOPIC / "history.jsonl"
        write_topic(make_context(self.root, history_path=history), {
            "harness_id": name, "binding_revision": 1, "origin": origin, "home_start": True, "started_at": T0,
            "slug": "paper", "topic_number": 922, "workspace_name": "home", "workdir": str(home), "workspace_locked": True})
        return make_context(self.root, workdir=home, default_workdir=home, state_dir=self.state, history_path=history,
                            topic_id=self.TOPIC, workspace_name="home")

    def turn(self, name, ctx, number=1, session=None):
        """(the prompt and rules of a turn, its context after app/runs.py set it up, its job) on ``name``."""
        from support import make_job

        job = make_job(job_id=number, message_id=f"om_ext-{number}", topic_id=self.TOPIC, chat_id=self.chat,
                       user_text="解读论文 2611.02440", result_backend=name,
                       checkpoint_path=str(self.state / "checkpoints" / f"om_ext-{number}.json"))
        live = SimpleNamespace(ctx=ctx, rules_session=False)
        prompt, rules, _system, _ = self.apps[name].runs._prompt(live, job, session)
        return prompt, rules, live.ctx, job

    def test_an_external_topic_is_told_nothing_of_the_homes_repositories_and_each_harness_leaves_its_files_out(self):
        for form in FORMS:
            home = self.make_home(form)
            for name in CORE_HARNESSES:
                with self.subTest(form=form, harness=name):
                    prompt, rules, ctx, _ = self.turn(name, self.topic_ctx(home, name))
                    whole = rules + "\n" + prompt
                    found = ctx.options.get(EXTERNAL) or {}
                    self.assertEqual(found.get("home"), str(home))
                    self.assertTrue(Path(found.get("dir", "")).is_dir())
                    self.assertIn(home_turn.static_block(ctx), rules)
                    self.assertIn(home_turn.EXTERNAL, whole)
                    self.assertIn("本话题产物目录：" + found["dir"], whole)
                    for words in self.NOT_SAID:
                        self.assertNotIn(words, whole, words)
                    self.assertNotIn(str(repomap.rendered_path(self.state, home)), whole)
                    isolated, _ = EXTERNAL_SEEN[name]
                    self.assertEqual(isolated(ctx, home), held(EXT_FILES, name), gap_words(EXT_FILES, name))
            # Its outputs directory, made by the turn, under the home's runs root (never the home's root).
            (runs, _sessions) = covey_home.task_roots(covey_home.read(self.state), home)
            self.assertTrue(found["dir"].startswith(runs + os.sep), runs)

    def test_the_external_part_of_the_prompt_is_the_golden_one(self):
        """The home block of an external topic, its outputs directory put as a placeholder: a change of it shows here
        (tests/golden/external_topic_prompt.txt)."""
        for form in FORMS:
            home = self.make_home(form)
            with self.subTest(form=form):
                _prompt, rules, ctx, _job = self.turn("claude", self.topic_ctx(home, "claude"))
                found = ctx.options[EXTERNAL]["dir"]
                shown = (home_turn.static_block(ctx) + "\n").replace(found, "<产物目录>")
                self.assertIn(home_turn.static_block(ctx), rules)
                self.assertEqual(shown, EXTERNAL_GOLDEN.read_text(encoding="utf-8"))

    def test_a_home_in_a_git_work_tree_keeps_antigravity_where_it_was(self):
        for form in FORMS:
            home = self.make_home(form)
            init_git_repo(home)
            for name in CORE_HARNESSES:
                with self.subTest(form=form, harness=name):
                    _, _, ctx, _ = self.turn(name, self.topic_ctx(home, name))
                    isolated, _ = EXTERNAL_SEEN[name]
                    self.assertEqual(isolated(ctx, home), held(EXT_GIT, name), gap_words(EXT_GIT, name))
                    if name == "agy":
                        self.assertEqual(covey_home.external_cwd(ctx), str(home))

    def test_a_topic_without_the_field_is_as_before(self):
        """T877 (reading training code, ``local`` left out): the home block with the repo map and the write command, and
        no Harness leaves anything out; ``local`` true is the same."""
        for form in FORMS:
            home = self.make_home(form)
            for name in CORE_HARNESSES:
                for local in (None, True):
                    with self.subTest(form=form, harness=name, local=local):
                        _, rules, ctx, _ = self.turn(name, self.topic_ctx(home, name, local=local))
                        self.assertNotIn(EXTERNAL, ctx.options)
                        self.assertIn(f"主目录：{home}（本话题从这里启动，先遵守这里的 AGENTS.md）", rules)
                        self.assertIn("仓库地图：", rules)
                        self.assertIn("covey repo use <仓库> --write", rules)
                        self.assertNotIn(home_turn.EXTERNAL, rules)
                        _, free = EXTERNAL_SEEN[name]
                        self.assertTrue(free(ctx), gap_words(EXT_FREE, name))

    def test_the_rules_stay_the_same_and_covey_repo_use_still_works(self):
        """The system rules' version of an external topic is the same on its first turn and its second; after ``covey repo
        use alpha`` (read) the second turn's repository block lists alpha, with no write command, and its rules are the same."""
        home = self.make_home("接管")
        for name in ("claude", "codex", "agy"):
            with self.subTest(harness=name):
                ctx = self.topic_ctx(home, name)
                _, rules, first, _ = self.turn(name, ctx)
                task = self.state / "runs" / f"om_ext-{name}"
                topic_repos.add_request(task, {"at": T0, "name": "alpha", "path": str(home / "alpha"), "mode": "read",
                                               "rules_file": "AGENTS.md"})
                job = SimpleNamespace(topic_id=self.TOPIC, job_id=1, message_id="om_ext-1", tool_trace=None)
                repos_turn.settle(first, job, task)
                tail = repos_turn.tail(ctx, job)
                self.assertIn("- alpha（读，", tail)
                self.assertNotIn("--write", tail)
                _, again, second, _ = self.turn(name, ctx, number=2)
                self.assertEqual(rules_sent.version(again), rules_sent.version(rules))
                self.assertEqual(second.options.get(EXTERNAL), first.options.get(EXTERNAL))

    def test_claude_code_keeps_the_users_own_files_when_the_home_is_home(self):
        """A home that is the user's HOME itself: its .claude/ is the user's (CLAUDE.md, rules), never left out."""
        ctx = make_context(self.root, workdir=self.user, default_workdir=self.user,
                           options={EXTERNAL: {"home": str(self.user), "dir": str(self.root / "out")}})
        excluded = claude_argv.md_excludes(ctx)
        self.assertIn(str(self.user / "AGENTS.md"), excluded)
        self.assertFalse([path for path in excluded if "/.claude/" in path])


class RulesRowTest(HarnessCellsCase):
    """规则 × Harness (W2-A, gate G8): what each core Harness's turn is told and does, asked the same way of each. A cell
    that does not hold is in GAPS with why (and fails here once it holds); an extension's Harness is in EXTENSION_CELLS."""

    def test_every_harness_says_how_its_rules_are_seen(self):
        self.assertEqual(sorted(RULES_SEEN), sorted(CORE_HARNESSES))

    def test_how_its_turn_asks_the_owner(self):
        """The question tool the Harness declares is the one its backend wires, and the butler's rules on it name that
        tool, or, with none, the checkpoint's questions and no tool of another Harness."""
        ctx = make_context(self.root)
        for name in CORE_HARNESSES:
            with self.subTest(harness=name):
                wired, declared = RULES_SEEN[name]["asks"](ctx), self.capabilities(name).question_tool
                rules = prompts.butler_rules(prompts.ask_words(self.capabilities(name)))
                others = {"AskUserQuestion", appserver.QUESTION_TOOL} - {wired}
                holds = wired == declared and (f"用 {wired} 出一道单选题" in rules if wired else prompts.LATER_PICK in rules) \
                    and not any(other in rules for other in others)
                self.assertEqual(holds, held(RULE_ASK, name), gap_words(RULE_ASK, name))
        self.assertIn(appserver.QUESTION_TOOL, appserver.CODEX_INSTRUCTIONS)

    def system_plan(self, name, sid="11111111-1111-4111-8111-111111111111"):
        """(the backend's capabilities, the request's rules at the system level, the plan) of a topic's resumed turn on
        ``name`` after one that began its session with them and ended well."""
        from covey.app import rules_sent
        from support import make_job

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root, topic = Path(tmp.name), "r" * 32
        ctx = make_context(root, topic_id=topic, history_path=root / "state" / "topics" / topic / "history.jsonl")
        write_topic(ctx, {"binding_revision": 1})
        runs, caps = self.apps[name].runs, self.capabilities(name)
        first, second = (make_job(job_id=number, message_id=f"om-r{number}", topic_id=topic, chat_id=self.chat,
                                  result_backend=name, user_text="接着做") for number in (1, 2))
        rules = runs.bridge_rules(ctx, second, system=True)
        write_job(ctx.state_dir, "om-r1", job_id=1, topic_id=topic, chat_id=self.chat, backend=name, status="done")
        rules_sent.record(ctx, first, sid, rules_sent.Plan(rules_sent.SYSTEM, "new_session", rules_sent.version(rules),
                                                         caps.system_refresh))
        return caps, rules, runs._rules_plan(ctx, second, sid, rules, caps)

    def test_a_resumed_turn_takes_the_anchor(self):
        """C9 (W1-E, the owner's call of 11-28): the bridge rules at the system level of every core Harness's session
        (its own channel: system_instructions, system_refresh), so a resumed turn's message has none of them; with
        COVEY_SYSTEM_RULES=0 they are in the message everywhere, as before."""
        from covey.app import rules_sent

        for name in CORE_HARNESSES:
            with self.subTest(harness=name):
                caps, rules, found = self.system_plan(name)
                holds = (caps.system_instructions and rules_sent.enabled() and found is not None
                         and found.mode == rules_sent.SYSTEM and "检查点" in rules)
                self.assertEqual(holds, held(RULE_ANCHOR, name), gap_words(RULE_ANCHOR, name))
        with mock.patch.dict(os.environ, {rules_sent.ENV: "0"}):
            self.assertFalse(rules_sent.enabled())

    def test_a_high_context_is_told(self):
        """The backend reports the latest request's context and the window it compacts against, so the next resumed turn
        is told (app/prompts.py context_high) instead of stopping short of its work."""
        for name in CORE_HARNESSES:
            with self.subTest(harness=name):
                usage = RULES_SEEN[name]["context"]()
                window = usage.get("context_window")
                holds = type(usage.get("context_tokens")) is int and type(window) is int and bool(
                    prompts.context_high({"context_tokens": int(window * 0.95), "context_window": window}))
                self.assertEqual(holds, held(RULE_CONTEXT, name), gap_words(RULE_CONTEXT, name))
                self.assertIn("context_tokens", usage)  # Every core Harness says the context size at least.

    def test_the_butler_is_reminded_past_its_own_tool_calls_once(self):
        """Claude Code by its own PostToolUse hook (nudges); any other Harness that takes a supplement by the Bridge's
        count, as an internal supplement, never twice and never for the Bridge's own tools."""
        nudge = prompts.tool_nudge("查一下")
        ctx = make_context(self.root, options={"tool_nudge": nudge})
        inline = SimpleNamespace(CARD_UPDATES="card-updates", start=lambda _, fn, *args: fn(*args))
        for name in CORE_HARNESSES:
            with self.subTest(harness=name):
                app, caps = self.apps[name], self.capabilities(name)
                job = SimpleNamespace(tool_nudge={}, job_id=7, message_id=f"om_{name}")
                with mock.patch.object(app.interaction.steering, "deliver_steer") as deliver, \
                        mock.patch("covey.app.interaction.background", inline):
                    for index in range(nudge["after"] - 1):
                        app.interaction.count_for_nudge(ctx, job, {"id": f"s{index}", "name": "bash", "detail": "ls"})
                    app.interaction.count_for_nudge(ctx, job, {"id": "b", "name": "mcp_bridge_topics_list"})
                    before = deliver.call_count
                    for index in range(nudge["after"] - 1, nudge["after"] + 2):
                        app.interaction.count_for_nudge(ctx, job, {"id": f"s{index}", "name": "bash", "detail": "ls"})
                bridge_side = before == 0 and deliver.call_count == 1 and deliver.call_args.kwargs.get("internal") is True
                hooked = caps.nudges and "PostToolUse" in TurnHooks(ctx, {}).registration() and deliver.call_count == 0
                self.assertEqual(hooked or bridge_side, held(RULE_NUDGE, name), gap_words(RULE_NUDGE, name))

    def test_a_search_of_the_whole_home_on_the_command_line_is_refused(self):
        """Claude Code's home-search hook, and for every Harness's process the search shims first on its PATH (core/home.py
        env, SEARCH_SHIMS): rg or find over the home is refused, one in a repository runs."""
        home = self.root / "home"
        (home / "repo").mkdir(parents=True)
        ctx = make_context(self.root, workdir=home, default_workdir=home)
        for name in CORE_HARNESSES:
            with self.subTest(harness=name):
                env = covey_home.env(ctx)
                shimmed = (env.get("PATH", "").split(os.pathsep)[0] == str(covey_home.bin_dir())
                           and env.get("COVEY_HOME") == str(covey_home.of(ctx))
                           and {"rg", "find"} <= set(covey_home.SEARCH_SHIMS)
                           and bool(tool_guard.shim_refusal("find", ["."], str(home), str(home)))
                           and bool(tool_guard.shim_refusal("rg", ["x", str(home)], str(home), str(home)))
                           and not tool_guard.shim_refusal("rg", ["x", "repo"], str(home), str(home)))
                if name == "claude":
                    shimmed = shimmed and bool(tool_guard.bash_refusal("rg -n x", str(home), str(home)))
                self.assertEqual(shimmed, held(RULE_SEARCH, name), gap_words(RULE_SEARCH, name))

    def test_a_search_inside_a_program_is_refused_where_it_can_be_seen(self):
        """``python3 -c "os.walk('.')"`` from the home: Claude Code's hook reads the inline Python; a shim sees only the
        command it stands for."""
        home = self.root / "home"
        home.mkdir(parents=True, exist_ok=True)
        command = 'python3 -c "import os; print(sum(1 for _ in os.walk(\'.\')))"'
        for name in CORE_HARNESSES:
            with self.subTest(harness=name):
                if name == "claude":
                    holds = bool(tool_guard.bash_refusal(command, str(home), str(home)))
                else:
                    holds = bool(tool_guard.shim_refusal("python3", shlex.split(command)[1:], str(home), str(home)))
                self.assertEqual(holds, held(RULE_WALK, name), gap_words(RULE_WALK, name))

    def test_every_gap_and_extension_cell_says_why(self):
        self.assertTrue(all(why.strip() for why in (*GAPS.values(), *EXTENSION_CELLS.values())))


DEVICES_JSON = {"schema": 1, "devices": {"gpu1": {"ssh": {"host": "gpu1"}, "access": "write"},
                                         "peer": {"ssh": {"host": "peer"}, "access": "read"}}}


def device_reach(name, ctx) -> bool:
    """Whether a turn of Harness ``name`` with context ``ctx`` (after app/runs.py set it up) can run ``covey device`` from its
    shell: Codex's read-only sandbox cannot reach the ssh socket (U2), agy's deny rules for commands stop it, Claude Code's
    and dsh's shell are as the turn has them."""
    if name == "codex":
        return appserver.sandbox_mode(ctx) != "read-only"
    if name == "agy":
        return "command(*)" not in json.dumps(agy_bridge.settings_args(ctx))
    return True


class DeviceRowTest(HarnessCellsCase):
    """话题 · 设备通道 (core/device_access.py, F2a) × Harness: a home topic (local) and an external one (``local`` false),
    each against a writable device and a read-only one, over the real device program under a temporary HOME
    (LocalTransport). The rules say how to use a device only to a local topic; every turn's start writes its limit and its
    chat's space for ``covey device``; what the channel lets each do is the same on every Harness (the guards are the
    channel's), and each turn's shell reaches it (``device_reach``)."""

    TOPIC = "d" * 32

    def setUp(self):
        super().setUp()
        patch = mock.patch.dict(os.environ, {"COVEY_DEVICES": "1", "COVEY_DEVICES_FILE": str(self.root / "devices.json")})
        patch.start()
        self.addCleanup(patch.stop)
        (self.root / "devices.json").write_text(json.dumps(DEVICES_JSON))
        from covey.core import devices
        devices._CACHE.clear()
        self.config = devices.current()
        self.device_home = self.root / "device-home"
        self.device_home.mkdir()
        self.links = {}
        topic_repos.cache.clear()
        self.app.runtime.access.remember_owner_chat(self.chat)  # The owner's private chat: devices' owner_private.

    def connect(self, config, alias, state_dir):
        from covey.core import device_transport, devices
        if alias not in self.links:
            link = device_transport.LocalTransport(self.device_home, alias=alias, hub="hub1")
            devices.push_file(link, device_transport.shim_bytes())
            self.links[alias] = link
        return self.links[alias]

    def topic_ctx(self, home, name, *, local):
        origin = {"kind": "main", "request": "r1", "job_key": "main-job", "why": "看训练",
                  "user_requested": True, "title": "看训练", "at": T0, **({} if local else {"local": False})}
        history = self.state / "topics" / self.TOPIC / "history.jsonl"
        write_topic(make_context(self.root, history_path=history), {
            "harness_id": name, "binding_revision": 1, "origin": origin, "home_start": True, "started_at": T0,
            "slug": "train", "topic_number": 951, "workspace_name": "home", "workdir": str(home), "workspace_locked": True})
        return make_context(self.root, workdir=home, default_workdir=home, state_dir=self.state, history_path=history,
                            topic_id=self.TOPIC, workspace_name="home")

    def turn(self, name, ctx):
        """(rules, prompt, the turn's context after app/runs.py set it up, its run directory) of a first turn on ``name``."""
        from support import make_job
        from covey.core.jobs import job_key
        from covey.core.run_dir import run_dir

        message = f"om_dev-{name}"
        job = make_job(job_id=1, message_id=message, topic_id=self.TOPIC, chat_id=self.chat, user_text="看看训练",
                       result_backend=name, checkpoint_path=str(self.state / "checkpoints" / f"{message}.json"))
        task = run_dir(self.state, job_key(message, 1))
        task.mkdir(parents=True, exist_ok=True)
        live = SimpleNamespace(ctx=ctx, rules_session=False)
        prompt, rules, _system, _ = self.apps[name].runs._prompt(live, job, None)
        return rules, prompt, live.ctx, task

    def test_each_harness_x_local_external_x_writable_read_only_device(self):
        from covey.core import device_access
        from covey.core.run_dir import DEVICE_TURN

        home = self.root / "home"
        covey_home.seed_home(home)
        covey_home.write(self.state, covey_home.HomeConfig())
        for name in CORE_HARNESSES:
            for kind in ("本机", "外部"):
                with self.subTest(harness=name, kind=kind):
                    ctx = self.topic_ctx(home, name, local=kind != "外部")
                    rules, prompt, turn_ctx, task = self.turn(name, ctx)
                    told = home_turn.DEVICE_RULE in rules + "\n" + prompt
                    self.assertEqual(told, kind != "外部", "the rules name covey device to a local topic only")
                    written = json.loads((task / DEVICE_TURN).read_text())
                    self.assertEqual((written["limit"], written["space"]), ("", "owner_private"))
                    turn = device_access.turn_from_env({"COVEY_STATE": str(self.state), "COVEY_TASK_DIR": str(task)})
                    channel = device_access.Channel(turn, config=self.config, connect=self.connect)
                    for alias, writes in (("gpu1", True), ("peer", False)):
                        self.assertEqual(channel.run(alias, ["uname", "-s"])["code"], 0, f"{alias}: reading runs")
                        if writes:
                            target = self.device_home / f"{name}-{kind}.txt"
                            self.assertEqual(channel.run(alias, ["touch", str(target)])["code"], 0)
                            self.assertTrue(target.exists())
                        else:
                            with self.assertRaises(device_access.Refused):
                                channel.run(alias, ["touch", str(self.device_home / "no.txt")])
                            with self.assertRaises(device_access.Refused):
                                channel.job_start(alias, ["sleep", "1"])
                    self.assertFalse((self.device_home / "no.txt").exists())
                    rows = device_access.read_audit(task)
                    self.assertTrue(rows and all(row.get("device") for row in rows), "every call is in the audit")
                    self.assertTrue(device_reach(name, turn_ctx), f"{name} {kind}: its shell reaches covey device")


class KnownGapsTest(HarnessCellsCase):
    def test_the_known_gaps_are_still_gaps(self):
        """The cells GAPS lists are asked above (test_a_group_members_read_only_turn, RulesRowTest): one that holds now
        fails there; this one keeps GAPS to cells those tests ask."""
        self.assertLessEqual({cell for cell, _ in GAPS}, {"话题 · 群成员只读", RULE_ASK, RULE_ANCHOR, RULE_CONTEXT,
                                                          RULE_NUDGE, RULE_SEARCH, RULE_WALK, EXT_FILES, EXT_GIT, EXT_FREE})


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
