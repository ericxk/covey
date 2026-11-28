"""A Harness's own memory (Claude Code's auto memory, Codex's memories) and the turns that must stay out of it: a 代答 turn,
and on Claude Code one an extension's hook put behind a relay (backends/claude/argv.py turn_env, backends/codex/appserver.py
server_flags and keep_out_of_memories)."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support import make_context
from covey.backends.claude import argv as claude_argv
from covey.backends.claude import upstream
from covey.backends.codex import appserver
from covey.backends.codex.rpc import AppServer, RpcError
from covey.core.harness import CONSULT, LEAD, LOOK, PROXY, READ, TOOL_LIMIT


def flag_pairs(flags):
    return dict(flags[i + 1].split("=", 1) for i in range(0, len(flags), 2))


class ClaudeAutoMemoryTest(unittest.TestCase):
    def env(self, **options):
        return claude_argv.turn_env(make_context(Path("/x"), options=options), {"PATH": "/bin"})

    def test_a_proxy_turn_and_one_behind_a_relay_run_without_auto_memory(self):
        self.assertEqual(self.env(**{TOOL_LIMIT: PROXY})[claude_argv.NO_AUTO_MEMORY], "1")
        relayed = {upstream.SETTINGS_ENV: {"ANTHROPIC_BASE_URL": "http://127.0.0.1:9/"}}
        self.assertEqual(self.env(**relayed)[claude_argv.NO_AUTO_MEMORY], "1")

    def test_every_other_turn_keeps_it_as_the_environment_has_it(self):
        for options in ({}, {TOOL_LIMIT: READ}, {TOOL_LIMIT: LOOK}, {TOOL_LIMIT: LEAD}, {TOOL_LIMIT: CONSULT},
                        {"mcp_config": {"mcpServers": {"bridge": {}}}}):
            self.assertNotIn(claude_argv.NO_AUTO_MEMORY, self.env(**options), options)
        kept = claude_argv.turn_env(make_context(Path("/x")), {"PATH": "/bin", claude_argv.NO_AUTO_MEMORY: "1"})
        self.assertEqual(kept[claude_argv.NO_AUTO_MEMORY], "1")  # The deployer's own value stays.


class CodexMemoriesTest(unittest.TestCase):
    def ctx(self, **options):
        return make_context(Path("/x"), options=options)

    def test_a_proxy_turns_server_neither_reads_nor_makes_memories_and_keeps_its_question(self):
        pairs = flag_pairs(appserver.server_flags(self.ctx(**{TOOL_LIMIT: PROXY})))
        self.assertEqual(pairs, {"features.default_mode_request_user_input": "true", "features.memories": "false",
                                 "memories.generate_memories": "false", "memories.use_memories": "false"})

    def test_an_ordinary_turn_has_the_question_alone_as_before(self):
        self.assertEqual(appserver.server_flags(self.ctx()), ["-c", "features.default_mode_request_user_input=true"])

    def test_a_proxy_turns_thread_is_marked_out_of_memories_without_waiting_for_the_answer(self):
        posted = []
        rpc = SimpleNamespace(thread_id="thread-1", post=lambda method, params: posted.append((method, params)))
        appserver.keep_out_of_memories(rpc, self.ctx(**{TOOL_LIMIT: PROXY}))
        self.assertEqual(posted, [("thread/memoryMode/set", {"threadId": "thread-1", "mode": "disabled"})])
        for options in ({}, {TOOL_LIMIT: READ}, {TOOL_LIMIT: CONSULT}):
            appserver.keep_out_of_memories(rpc, self.ctx(**options))
        self.assertEqual(len(posted), 1)  # Only a 代答 turn's.

    def test_a_failed_post_leaves_the_turn_alone(self):
        def refuse(method, params):
            raise RpcError("App Server connection closed")
        appserver.keep_out_of_memories(SimpleNamespace(thread_id="t", post=refuse), self.ctx(**{TOOL_LIMIT: PROXY}))

    def test_open_thread_marks_a_proxy_turns_thread_after_its_identity_is_checked(self):
        sent = []
        rpc = SimpleNamespace(process=None, thread_id="", request=lambda method, params, timeout=0: (
            sent.append(method) or ({"thread": {"id": "0190a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b"}} if method.startswith("thread/") else {})),
            send=lambda row: None, post=lambda method, params: sent.append(method))
        request = SimpleNamespace(context=self.ctx(**{TOOL_LIMIT: PROXY}), session_id=None, instructions="",
                                  observers=SimpleNamespace(session=lambda tid: sent.append("session")))
        state = SimpleNamespace(invalidate=lambda: None)
        with mock.patch.object(appserver, "thread_params", return_value=({}, None)):
            appserver.open_thread(rpc, request, state, lambda process: None)
        self.assertEqual(sent, ["initialize", "thread/start", "session", "thread/memoryMode/set"])


class PostTest(unittest.TestCase):
    def test_post_writes_a_request_with_its_own_id_and_waits_for_nothing(self):
        server = AppServer.__new__(AppServer)
        written = []
        server.lock, server.counter, server.pending, server.closed = mock.MagicMock(), 4, {}, False
        server.process = SimpleNamespace(stdin=SimpleNamespace(write=written.append, flush=lambda: None))
        server.post("thread/memoryMode/set", {"threadId": "t", "mode": "disabled"})
        self.assertEqual(json.loads(written[0]), {"id": 5, "method": "thread/memoryMode/set",
                                                  "params": {"threadId": "t", "mode": "disabled"}})
        self.assertEqual(server.pending, {})


if __name__ == "__main__":
    unittest.main()
