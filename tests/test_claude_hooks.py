"""The SDK hook callbacks of a turn's Claude Code (backends/claude/hooks.py): the subagents' rules and the Bridge's tools
refused to them (#33), the butler's one reminder past its own tool calls (#34), registered in the initialize request and
answered by the control side; COVEY_CLAUDE_HOOKS=0 is the turn as before."""
from __future__ import annotations

import dataclasses
import functools
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support.clock import FakeClock
from support.fake_backends import RUN, ScriptedClaudeProcess, codex_context

from covey.backends.claude import hooks, runtime
from covey.backends.claude.bridge import ClaudeBackend
from covey.backends.claude.control import ClaudeControl
from covey.backends.claude.process import ClaudeProcess
from covey.core.completion import CompletionBudget
from covey.core.harness import RunInputs, RunRequest

BRIDGE = {"mcpServers": {"bridge": {"command": "python3"}}}


def context(**options):
    return SimpleNamespace(options=options)


class Clean(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {})
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in (hooks.SWITCH, "LARK_BRIDGE_CLAUDE_HOOKS"):
            os.environ.pop(name, None)


class RegistrationTest(Clean):
    def test_every_turn_tells_its_subagents_the_rules_and_one_with_bridge_tools_refuses_them_those(self):
        self.assertEqual(hooks.TurnHooks(context()).registration(),
                         {"SubagentStart": [{"hookCallbackIds": [hooks.START]}]})
        butler = hooks.TurnHooks(context(mcp_config=BRIDGE)).registration()
        self.assertEqual(butler["SubagentStart"], [{"hookCallbackIds": [hooks.START]}])
        self.assertEqual(butler["PreToolUse"], [{"matcher": "mcp__bridge__.*", "hookCallbackIds": [hooks.BRIDGE_TOOL]}])
        self.assertNotIn("PostToolUse", butler)

    def test_the_switch_off_registers_nothing(self):
        for value in ("0", "false", "off", "no"):
            with mock.patch.dict(os.environ, {hooks.SWITCH: value}):
                turn = hooks.TurnHooks(context(mcp_config=BRIDGE, tool_nudge={"after": 2, "text": "交给话题"}))
                self.assertEqual(turn.registration(), {})
                self.assertEqual(turn.answer(hooks.START, {"agent_id": "a", "agent_type": "general-purpose"}), {})
        with mock.patch.dict(os.environ, {hooks.SWITCH: "1"}):
            self.assertTrue(hooks.TurnHooks(context()).registration())

    def test_the_rules_are_the_four_sentences(self):
        # 5.9 row 10, the four sentences ClaudePartner gives each subagent, in the owner's words.
        lines = hooks.RULES.split("\n")
        self.assertEqual(lines, ["[子 Agent 守则]",
                                 "- 你在替主 Agent 干活，不直接对主人说话，也不能发消息和文件。",
                                 "- 最终回复就是你的全部交付：写清做了什么、结果、没做完的、每个产物的绝对路径。",
                                 "- 需要主人拍板时，不可逆的事不要猜，用一个能原样转给主人的问题收尾。",
                                 "- 你之后可能被带着答案叫回来接着做。"])


class AnswerTest(Clean):
    def test_a_subagent_but_explore_gets_the_rules_when_it_starts(self):
        turn = hooks.TurnHooks(context())
        told = {"hookSpecificOutput": {"hookEventName": "SubagentStart", "additionalContext": hooks.RULES}}
        for kind in ("general-purpose", "Plan", "code-reviewer"):
            self.assertEqual(turn.answer(hooks.START, {"hook_event_name": "SubagentStart", "agent_id": "a1",
                                                      "agent_type": kind}), told, kind)
        self.assertEqual(turn.answer(hooks.START, {"agent_id": "a2", "agent_type": "Explore"}), {})  # Its own prompt.
        self.assertEqual(turn.answer(hooks.START, None), told)

    def test_a_bridge_tool_is_refused_to_a_subagent_and_left_to_the_main_thread(self):
        turn = hooks.TurnHooks(context(mcp_config=BRIDGE))
        refused = turn.answer(hooks.BRIDGE_TOOL, {"hook_event_name": "PreToolUse", "tool_name": "mcp__bridge__topic_start",
                                                  "agent_id": "a1", "agent_type": "general-purpose"})
        self.assertEqual(refused, {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                          "permissionDecisionReason": hooks.REFUSED}})
        self.assertIn("写进你的最终回复，主 Agent 会转达或去做", hooks.REFUSED)  # Refused with a way on.
        self.assertEqual(turn.answer(hooks.BRIDGE_TOOL, {"tool_name": "mcp__bridge__topic_start"}), {})  # The butler's own.
        self.assertEqual(turn.answer(hooks.BRIDGE_TOOL, {"tool_name": "Bash", "agent_id": "a1"}), {})
        self.assertEqual(turn.answer("someone_elses", {"tool_name": "mcp__bridge__x", "agent_id": "a1"}), {})


class NudgeTest(Clean):
    NUDGE = {"after": 3, "text": "交给话题"}

    def call(self, turn, tool, **fields):
        return turn.answer(hooks.COUNT, {"hook_event_name": "PostToolUse", "tool_name": tool, **fields})

    def test_the_main_threads_own_calls_are_counted_and_it_is_reminded_once(self):
        turn = hooks.TurnHooks(context(mcp_config=BRIDGE, tool_nudge=self.NUDGE))
        self.assertEqual(turn.registration()["PostToolUse"], [{"hookCallbackIds": [hooks.COUNT]}])
        told = {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "交给话题"}}
        self.assertEqual(self.call(turn, "Read"), {})
        self.assertEqual(self.call(turn, "mcp__bridge__topic_read"), {})  # The Bridge's tools are its job.
        self.assertEqual(self.call(turn, "Bash", agent_id="a1"), {})  # A subagent's calls are the subagent's.
        self.assertEqual(self.call(turn, ""), {})
        self.assertEqual(self.call(turn, "Grep"), {})
        self.assertEqual(self.call(turn, "Bash"), told)
        self.assertEqual(turn.calls, 3)
        for tool in ("Bash", "Edit", "Read"):
            self.assertEqual(self.call(turn, tool), {})  # Once.

    def test_no_reminder_without_the_option_or_with_an_unusable_one(self):
        for nudge in (None, {}, {"after": 0, "text": "x"}, {"after": "3", "text": "x"}, {"after": 3, "text": ""}, "x"):
            turn = hooks.TurnHooks(context(tool_nudge=nudge) if nudge is not None else context())
            self.assertNotIn("PostToolUse", turn.registration(), nudge)
            for _ in range(5):
                self.assertEqual(self.call(turn, "Bash"), {}, nudge)


class ControlTest(Clean):
    def request(self, rid, callback, data):
        return {"type": "control_request", "request_id": rid,
                "request": {"subtype": "hook_callback", "callback_id": callback, "input": data, "tool_use_id": "tu1"}}

    def test_the_control_side_answers_each_callback_and_one_it_has_none_for_goes_on(self):
        sent = []
        state = SimpleNamespace(session_id="s")
        control = ClaudeControl(SimpleNamespace(send=sent.append), state, lambda *a: None,
                                hooks.TurnHooks(context(mcp_config=BRIDGE)))
        control.accept(self.request("r1", hooks.START, {"agent_id": "a", "agent_type": "general-purpose"}))
        control.accept(self.request("r2", hooks.BRIDGE_TOOL, {"tool_name": "mcp__bridge__topic_send", "agent_id": "a"}))
        self.assertEqual([row["type"] for row in sent], ["control_response", "control_response"])
        self.assertEqual([row["response"]["request_id"] for row in sent], ["r1", "r2"])
        self.assertEqual(sent[0]["response"]["response"]["hookSpecificOutput"]["additionalContext"], hooks.RULES)
        self.assertEqual(sent[1]["response"]["response"]["hookSpecificOutput"]["permissionDecision"], "deny")
        sent.clear()
        plain = ClaudeControl(SimpleNamespace(send=sent.append), state, lambda *a: None)
        plain.accept(self.request("r3", hooks.START, {"agent_id": "a"}))
        self.assertEqual(sent, [{"type": "control_response",
                                 "response": {"subtype": "success", "request_id": "r3", "response": {}}}])

    def test_the_initialize_request_carries_the_hooks_and_without_them_is_as_before(self):
        sent = []

        def answer():
            return {"type": "control_response", "response": {"subtype": "success", "request_id": sent[-1]["request_id"],
                                                             "response": {}}}
        fake = SimpleNamespace(send=sent.append, receive=answer, prelude=[])
        registration = hooks.TurnHooks(context(mcp_config=BRIDGE)).registration()
        ClaudeProcess.initialize(fake, registration)
        self.assertEqual(sent[0]["request"], {"subtype": "initialize", "forwardSubagentText": True, "hooks": registration})
        ClaudeProcess.initialize(fake, {})
        ClaudeProcess.initialize(fake)
        for row in sent[1:]:
            self.assertEqual(row["request"], {"subtype": "initialize", "forwardSubagentText": True})


class TurnTest(Clean):
    def test_a_turn_starts_its_claude_with_the_hooks(self):
        clock, started = FakeClock(), []
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = dataclasses.replace(codex_context(Path(tmp.name), "stream-json"), backend_cmd=["never-run-claude"])

        def launch(argv, ctx, _env, cancel=None, observe=None):
            process = ScriptedClaudeProcess(clock, ctx, "success", cancel=cancel)
            started.append(process)
            return process

        def run(ctx):
            with mock.patch.object(runtime, "ClaudeProcess", launch), mock.patch.object(
                    runtime, "CompletionBudget", functools.partial(CompletionBudget, clock=clock.monotonic)):
                return ClaudeBackend().run(RunRequest(ctx, "本轮消息", None,
                                                      inputs=RunInputs(threading.Event(), run_id=RUN)))
        for options, expected in (({}, {"SubagentStart"}), ({"mcp_config": BRIDGE}, {"SubagentStart", "PreToolUse"}),
                                  ({"mcp_config": BRIDGE, "tool_nudge": {"after": 6, "text": "x"}},
                                   {"SubagentStart", "PreToolUse", "PostToolUse"})):
            ctx = dataclasses.replace(base, options={**base.options, **options})
            self.assertEqual(run(ctx).outcome.status, "completed")
            self.assertEqual(set(started[-1].hooks), expected)
        with mock.patch.dict(os.environ, {hooks.SWITCH: "0"}):
            run(base)
        self.assertEqual(started[-1].hooks, {})


if __name__ == "__main__":
    unittest.main()
