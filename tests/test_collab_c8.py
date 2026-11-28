"""C8 layer 2 (docs/architecture.md 多参与者协作 · 能力与预检): the installed Claude Code and Codex CLIs, driven through
Covey's own backends (ClaudeBackend.run, CodexBackend.run) against a scripted local model that tries to write, held to
CONSULT in a fork of an earlier session. Passed: every write is refused at the tool or sandbox layer (the model was not
given the tool, or the sandbox said no), and nothing changed: the workspace, a canary outside it, the loopback receiver
(no request), the MCP servers (never called), the forked session's own file (same sha256), Codex's memories. A run where
the model made no attempt is "not triggered", never "passed".

Opt-in (costs no model call, needs the CLIs): BRIDGE_C8_REAL_CLI=1, the CLIs from BRIDGE_C8_CLAUDE / BRIDGE_C8_CODEX or PATH;
BRIDGE_C8_OUT names a JSON file the evidence is written to. The owner's own ~/.claude and ~/.codex are never read or
written: each run has a configuration directory of its own.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from support import make_context
from covey.core.harness import CONSULT, READ, TOOL_LIMIT, RunCallbacks, RunInputs, RunRequest

ON = os.environ.get("BRIDGE_C8_REAL_CLI") == "1"
CLAUDE = os.environ.get("BRIDGE_C8_CLAUDE") or shutil.which("claude") or ""
CODEX = os.environ.get("BRIDGE_C8_CODEX") or shutil.which("codex") or ""
MARK = "C8-CONSULT-ATTEMPT"
EVIDENCE = {}

# A stdio MCP server that writes one line for every message it gets: a server that is never started leaves no file.
MCP_LOGGER = r'''
import json, sys
log = open(sys.argv[1], "a")
for line in sys.stdin:
    log.write(line); log.flush()
    try:
        msg = json.loads(line)
    except ValueError:
        continue
    if msg.get("method") == "initialize":
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"protocolVersion": "2025-06-18",
              "capabilities": {"tools": {}}, "serverInfo": {"name": "c8", "version": "1"}}}), flush=True)
    elif msg.get("method") == "tools/list":
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": [{"name": "write",
              "description": "write anything", "inputSchema": {"type": "object", "properties": {}}}]}}), flush=True)
    elif msg.get("method") == "tools/call":
        open(sys.argv[1] + ".called", "a").write(line)
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"content": [{"type": "text", "text": "written"}]}}),
              flush=True)
    elif "id" in msg:
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {}}), flush=True)
'''


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree(path: Path) -> dict:
    return {str(p.relative_to(path)): sha(p) for p in sorted(path.rglob("*")) if p.is_file()}


class Receiver:
    """127.0.0.1's receiver: how many requests reached it."""

    def __init__(self):
        self.hits = []
        hits = self.hits

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                hits.append(self.path)
                self.send_response(200)
                self.end_headers()

            do_POST = do_GET

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Site:
    """The scene of one run: a workspace, a canary outside it, the receiver, an MCP logger script."""

    def __init__(self, root: Path):
        self.root = root
        self.work = root / "work"
        self.work.mkdir()
        (self.work / "README.md").write_text("c8 workspace\n")
        self.canary = root / "canary"
        self.canary.mkdir()
        self.receiver = Receiver()
        self.logger = root / "mcp_logger.py"
        self.logger.write_text(MCP_LOGGER)
        self.mark()

    def mcp(self, name):
        return {"command": shutil.which("python3") or "python3", "args": [str(self.logger), str(self.root / f"mcp-{name}.log")]}

    def mcp_logs(self) -> dict:
        return {p.name: sha(p) for p in sorted(self.root.glob("mcp-*.log*"))}

    def mark(self) -> None:
        """What is there before the turn under test: the side effects are what changed after it."""
        self.before, self.logs, self.hits = tree(self.work), self.mcp_logs(), len(self.receiver.hits)

    def side_effects(self) -> dict:
        logs = self.mcp_logs()
        return {"canary": sorted(p.name for p in self.canary.iterdir()), "receiver": self.receiver.hits[self.hits:],
                "workspace_changed": tree(self.work) != self.before,
                "mcp": sorted(name for name in logs if logs[name] != self.logs.get(name))}


# --------------------------------------------------------------------------------------------------------- Claude Code

class FakeMessages:
    """A scripted Messages API: the consult's first request asks for every write it can name, the next one ends; any other
    turn just answers."""

    def __init__(self, site: Site):
        self.site, self.calls = site, []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                if self.headers.get("Content-Encoding") == "gzip":
                    import gzip
                    raw = gzip.decompress(raw)
                body = json.loads(raw)
                if self.path.startswith("/v1/messages/count_tokens"):
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"input_tokens":100}')
                    return
                fake.calls.append(body)
                blocks = fake.answer(body)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for kind, value in fake.frames(body, blocks):
                    self.wfile.write(("event: " + kind + "\ndata: " + json.dumps({"type": kind, **value}) + "\n\n").encode())
                self.wfile.flush()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @staticmethod
    def text_of(body) -> str:
        return json.dumps(body.get("messages") or [], ensure_ascii=False)

    def attempts(self):
        site = self.site
        return [
            ("Write", {"file_path": str(site.canary / "written.txt"), "content": "x"}),
            ("Edit", {"file_path": str(site.work / "README.md"), "old_string": "c8", "new_string": "pwned"}),
            ("NotebookEdit", {"notebook_path": str(site.work / "n.ipynb"), "new_source": "x"}),
            ("Bash", {"command": f"touch {site.canary}/bash.txt; curl -s -X POST http://127.0.0.1:{site.receiver.port}/x"}),
            ("WebFetch", {"url": f"http://127.0.0.1:{site.receiver.port}/fetch", "prompt": "x"}),
            ("Agent", {"description": "w", "prompt": f"touch {site.canary}/agent.txt", "subagent_type": "general-purpose"}),
            ("EnterWorktree", {"name": "c8"}),
            ("mcp__bridge__write", {}),
            ("mcp__owner__write", {}),
            ("mcp__project__write", {}),
            ("AskUserQuestion", {"questions": [{"question": "?", "header": "h", "multiSelect": False,
                                                 "options": [{"label": "a", "description": "a"}, {"label": "b", "description": "b"}]}]}),
        ]

    def answer(self, body):
        text = self.text_of(body)
        if MARK not in text:
            return [{"type": "text", "text": "Original turn answered."}]
        if '"tool_result"' not in text:
            return [{"type": "tool_use", "id": f"toolu_c8_{n}", "name": name, "input": args}
                    for n, (name, args) in enumerate(self.attempts())]
        return [{"type": "text", "text": "C8 consult done."}]

    @staticmethod
    def frames(body, blocks):
        tools = any(block["type"] == "tool_use" for block in blocks)
        frames = [("message_start", {"message": {"id": "msg-" + uuid.uuid4().hex[:8], "type": "message", "role": "assistant",
                                                 "model": body["model"], "content": [], "stop_reason": None,
                                                 "stop_sequence": None, "usage": {"input_tokens": 100, "output_tokens": 1}}})]
        for index, block in enumerate(blocks):
            if block["type"] == "tool_use":
                frames.append(("content_block_start", {"index": index, "content_block": {**block, "input": {}}}))
                frames.append(("content_block_delta", {"index": index, "delta": {
                    "type": "input_json_delta", "partial_json": json.dumps(block["input"])}}))
            else:
                frames.append(("content_block_start", {"index": index, "content_block": {"type": "text", "text": ""}}))
                frames.append(("content_block_delta", {"index": index, "delta": {"type": "text_delta", "text": block["text"]}}))
            frames.append(("content_block_stop", {"index": index}))
        frames.append(("message_delta", {"delta": {"stop_reason": "tool_use" if tools else "end_turn", "stop_sequence": None},
                                         "usage": {"output_tokens": 8}}))
        frames.append(("message_stop", {}))
        return frames

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def tool_names(body) -> list:
    return sorted(tool.get("name") for tool in body.get("tools") or [])


def tool_results(body) -> list:
    found = []
    for message in body.get("messages") or []:
        for block in message.get("content") or [] if isinstance(message.get("content"), list) else []:
            if block.get("type") == "tool_result":
                content = block.get("content")
                text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
                found.append({"id": block.get("tool_use_id"), "is_error": block.get("is_error") is True, "text": text[:300]})
    return found


@unittest.skipUnless(ON and CLAUDE, "opt-in: BRIDGE_C8_REAL_CLI=1 and an installed Claude Code")
class ClaudeC8(unittest.TestCase):
    def test_a_consult_fork_refuses_every_write_and_leaves_the_forked_session_as_it_was(self):
        from covey.backends.claude.bridge import ClaudeBackend

        with tempfile.TemporaryDirectory(prefix="c8-claude-") as tmp:
            root = Path(tmp)
            site = Site(root)
            config = root / "claude-config"
            config.mkdir()
            # The owner's MCP servers: one in the user's own configuration, one in the project's .mcp.json.
            (config / ".claude.json").write_text(json.dumps({"mcpServers": {"owner": site.mcp("owner")}}))
            (site.work / ".mcp.json").write_text(json.dumps({"mcpServers": {"project": site.mcp("project")}}))
            fake = FakeMessages(site)
            env = {key: value for key, value in os.environ.items() if not key.startswith(("ANTHROPIC_", "CLAUDE_"))}
            env.update(ANTHROPIC_API_KEY="key-local-c8-only", ANTHROPIC_BASE_URL=f"http://127.0.0.1:{fake.port}",
                       CLAUDE_CONFIG_DIR=str(config), CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", DISABLE_TELEMETRY="1",
                       NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")
            base = make_context(root, workdir=site.work, default_workdir=site.work, state_dir=root / "state",
                                backend_cmd=[CLAUDE], model="claude-sonnet-4-6", effort="", timeout=120)
            events, sessions = [], []

            def run(ctx, prompt, session_id=None):
                callbacks = RunCallbacks(event=lambda kind, data, control: events.append((kind, data)),
                                         session=sessions.append)
                with mock.patch("covey.backends.claude.bridge.child_process_env", return_value=env):
                    return ClaudeBackend().run(RunRequest(ctx, prompt, session_id, callbacks=callbacks,
                                                          inputs=RunInputs(threading.Event(), run_id=str(uuid.uuid4()))))
            try:
                first = run(base, "Original question of the helper topic.")
                self.assertFalse(first.is_error, first.outcome)
                source = sessions[-1]
                [original] = list(config.glob(f"projects/*/{source}.jsonl"))
                before = sha(original)
                calls = len(fake.calls)
                site.mark()  # The helper's own first turn loaded the owner's MCP servers: as before, not the ask's doing.
                consult = dataclasses.replace(base, options={TOOL_LIMIT: CONSULT, "fork_session": source,
                                                             "mcp_config": {"mcpServers": {"bridge": site.mcp("bridge")}},
                                                             "tools": ("Bash", "Write")})
                answer = run(consult, f"{MARK}: T3 asks: what was decided? Try to change things.")
                consult_calls = fake.calls[calls:]
                offered = tool_names(consult_calls[0])
                results = tool_results(consult_calls[-1])
                fork = sessions[-1]
                effects = site.side_effects()
                after = sha(original)
                # Permissions come back: the helper's own next turn (no limit) is offered its tools again.
                calls = len(fake.calls)
                run(base, "The owner's next message in the helper topic.", session_id=source)
                restored = tool_names(fake.calls[calls])
                EVIDENCE["claude"] = {"offered_tools": offered, "attempted": [name for name, _ in fake.attempts()],
                                      "results": results, "side_effects": effects, "fork": fork != source,
                                      "source_sha_unchanged": before == after, "answer": answer.text,
                                      "restored_tools_sample": restored[:12]}
                self.assertEqual(offered, ["Glob", "Grep", "Read"])  # What the model is given: no write, shell, MCP or Agent.
                self.assertEqual(len(results), len(fake.attempts()))  # Every attempt was made (triggered) …
                self.assertTrue(all(result["is_error"] for result in results), results)  # … and refused.
                self.assertEqual(effects, {"canary": [], "receiver": [], "workspace_changed": False, "mcp": []})
                self.assertNotEqual(fork, source)
                self.assertEqual(before, after)
                self.assertEqual(answer.text, "C8 consult done.")
                self.assertIn("Bash", restored)
            finally:
                fake.close()
                site.receiver.close()


# --------------------------------------------------------------------------------------------------------------- Codex

class FakeResponses:
    """A scripted Responses API: the consult's turn runs each command in turn through the shell tool it is given, then
    ends; any other turn just answers."""

    def __init__(self, commands):
        self.commands, self.calls = commands, []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(404)
                self.end_headers()

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("content-length", 0)))
                body = json.loads(raw)
                fake.calls.append(body)
                if not self.path.endswith("/responses"):
                    self.send_response(404)
                    self.end_headers()
                    return
                items = fake.items(body)
                events = [("response.created", {"type": "response.created", "response": {"id": "resp-c8"}})]
                for index, item in enumerate(items):
                    events.append(("response.output_item.added", {"type": "response.output_item.added", "output_index": index,
                                                                  "item": item}))
                    events.append(("response.output_item.done", {"type": "response.output_item.done", "output_index": index,
                                                                 "item": item}))
                events.append(("response.completed", {"type": "response.completed", "response": {
                    "id": "resp-c8", "status": "completed", "output": [],
                    "usage": {"input_tokens": 1, "input_tokens_details": None, "output_tokens": 1,
                              "output_tokens_details": None, "total_tokens": 2}}}))
                data = "".join(f"event: {name}\ndata: {json.dumps(value)}\n\n" for name, value in events).encode()
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @staticmethod
    def names(body) -> list:
        found = []
        for tool in body.get("tools") or []:
            if tool.get("type") == "namespace":
                found += [f"{tool.get('name')}::{sub.get('name')}" for sub in tool.get("tools") or []]
            else:
                found.append(tool.get("name") or tool.get("type"))
        return found

    @staticmethod
    def outputs(body) -> list:
        return [(item.get("call_id"), item.get("output") if isinstance(item.get("output"), str)
                 else json.dumps(item.get("output"), ensure_ascii=False))
                for item in body.get("input") or [] if str(item.get("type", "")).endswith("_output")]

    def items(self, body):
        if MARK not in json.dumps(body.get("input") or [], ensure_ascii=False):
            return [{"type": "message", "role": "assistant", "id": "m-orig",
                     "content": [{"type": "output_text", "text": "Original turn answered."}]}]
        done = len([call for call, _ in self.outputs(body) if str(call).startswith("c8_")])
        if done < len(self.commands):
            names = self.names(body)
            command = self.commands[done]
            if "exec_command" in names:
                return [{"type": "function_call", "name": "exec_command", "call_id": f"c8_{done}",
                         "arguments": json.dumps({"cmd": command, "yield_time_ms": 5000})}]
            return [{"type": "function_call", "name": "shell", "call_id": f"c8_{done}",
                     "arguments": json.dumps({"command": ["bash", "-lc", command], "timeout_ms": 10000})}]
        return [{"type": "message", "role": "assistant", "id": "m-done",
                 "content": [{"type": "output_text", "text": "C8 consult done."}]}]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@unittest.skipUnless(ON and CODEX, "opt-in: BRIDGE_C8_REAL_CLI=1 and an installed Codex")
class CodexC8(unittest.TestCase):
    def test_a_consult_fork_in_a_private_home_refuses_every_write_and_leaves_the_owners_home_as_it_was(self):
        from covey.backends.codex.bridge import CodexBackend

        with tempfile.TemporaryDirectory(prefix="c8-codex-") as tmp:
            root = Path(tmp)
            site = Site(root)
            rx = site.receiver.port
            commands = [f"touch {site.canary}/allowed-by-rule.txt",
                        f"curl -s -m 5 -X POST http://127.0.0.1:{rx}/x; echo curl-exit=$?",
                        f"sh -c 'echo x > {site.canary}/shell.txt'; echo sh-exit=$?",
                        f"touch {site.work}/inside.txt; echo touch-exit=$?"]
            fake = FakeResponses(commands)
            # The Codex home the turn would use (the owner's, in a live Bridge): an allow rule that would take `touch`
            # out of the sandbox, a hook, memories, and a provider pointing at the scripted model.
            home = root / "codex-home"
            (home / "rules").mkdir(parents=True)
            (home / "rules" / "default.rules").write_text('prefix_rule(pattern=["touch"], decision="allow")\n'
                                                          'prefix_rule(pattern=["curl"], decision="allow")\n')
            (home / "memories").mkdir()
            (home / "memories" / "note.md").write_text("an old memory\n")
            (home / "hooks.json").write_text('{"hooks": {}}')
            (home / "config.toml").write_text(
                'model = "c8-model"\nmodel_provider = "c8fake"\n'
                '[features]\nmemories = true\n[memories]\ngenerate_memories = true\nuse_memories = true\n'
                f'[mcp_servers.owner]\ncommand = "{site.mcp("owner")["command"]}"\nargs = {json.dumps(site.mcp("owner")["args"])}\n'
                '[model_providers.c8fake]\nname = "c8"\n'
                f'base_url = "http://127.0.0.1:{fake.port}/v1"\nenv_key = "C8_FAKE_KEY"\nwire_api = "responses"\n'
                'requires_openai_auth = false\nsupports_websockets = false\nrequest_max_retries = 0\nstream_max_retries = 0\n')
            env = {**os.environ, "CODEX_HOME": str(home), "C8_FAKE_KEY": "key-local-c8-only",
                   "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}
            base = make_context(root, workdir=site.work, default_workdir=site.work, state_dir=root / "state",
                                backend_cmd=[CODEX], model="c8-model", effort="", timeout=180)
            (root / "state").mkdir()
            sessions = []

            def run(ctx, prompt, session_id=None):
                callbacks = RunCallbacks(session=sessions.append)
                with mock.patch("covey.backends.codex.bridge.child_process_env", return_value=env):
                    return CodexBackend().run(RunRequest(ctx, prompt, session_id, callbacks=callbacks,
                                                         inputs=RunInputs(threading.Event(), run_id=str(uuid.uuid4()))))
            try:
                first = run(base, "Original question of the helper topic.")
                if first.is_error:  # The helper's own turn, not the one under test: once more (an app server's exit).
                    first = run(base, "Original question of the helper topic.")
                self.assertFalse(first.is_error, first.outcome)
                source = sessions[-1]
                [rollout] = list(home.glob(f"sessions/*/*/*/rollout-*-{source}.jsonl"))
                before, memories = sha(rollout), tree(home / "memories")
                calls = len(fake.calls)
                site.mark()
                consult = dataclasses.replace(base, options={TOOL_LIMIT: CONSULT, "fork_session": source,
                                                             "mcp_config": {"mcpServers": {"bridge": site.mcp("bridge")}}})
                answer = run(consult, f"{MARK}: T3 asks: what was decided? Try to change things.")
                consult_calls = fake.calls[calls:]
                offered = FakeResponses.names(consult_calls[0]) if consult_calls else []
                outputs = FakeResponses.outputs(consult_calls[-1]) if consult_calls else []
                fork = sessions[-1]
                effects = site.side_effects()
                EVIDENCE["codex"] = {"offered_tools": offered, "commands": commands, "outputs": outputs,
                                     "side_effects": effects, "fork": fork != source,
                                     "source_sha_unchanged": before == sha(rollout),
                                     "memories_unchanged": memories == tree(home / "memories"),
                                     "private_homes_left": sorted(p.name for p in (root / "state" / "tmp").glob("codex-consult-*")),
                                     "answer": answer.text}
                self.assertEqual(len(outputs), len(commands))  # Every attempt was made (triggered) …
                # No Bridge tool (its "bridge" server; the read-only workspace reader stays), no app, no owner's server, no search.
                self.assertFalse([name for name in offered if name.startswith(("mcp__bridge::", "mcp__bridge__", "mcp__owner"))
                                  or "codex_apps" in name or name == "web_search"])
                self.assertIn("mcp__bridge_workspace::workspace_read_file", offered)
                self.assertEqual(effects, {"canary": [], "receiver": [], "workspace_changed": False, "mcp": []})  # … none went.
                self.assertNotEqual(fork, source)
                self.assertEqual(before, sha(rollout))
                self.assertEqual(memories, tree(home / "memories"))
                self.assertEqual(EVIDENCE["codex"]["private_homes_left"], [])  # The private home went with the turn.
                self.assertEqual(answer.text, "C8 consult done.")
            finally:
                fake.close()
                site.receiver.close()


    def test_a_group_members_turn_resumes_the_thread_without_the_owners_rules(self):
        """A READ turn resumes the topic's thread in a home without the owner's rules, hooks, plugins and memories
        (backends/codex/consult.py ReadingHome): the commands an allow rule would take out of the sandbox are refused, and
        the owner's next turn, in his own home, goes on on the same thread."""
        from covey.backends.codex.bridge import CodexBackend

        with tempfile.TemporaryDirectory(prefix="c8-codex-read-") as tmp:
            root = Path(tmp)
            site = Site(root)
            rx = site.receiver.port
            commands = [f"touch {site.canary}/allowed-by-rule.txt",
                        f"curl -s -m 5 -X POST http://127.0.0.1:{rx}/x; echo curl-exit=$?",
                        f"sh -c 'echo x > {site.canary}/shell.txt'; echo sh-exit=$?",
                        f"touch {site.work}/inside.txt; echo touch-exit=$?"]
            fake = FakeResponses(commands)
            home = root / "codex-home"
            (home / "rules").mkdir(parents=True)
            (home / "rules" / "default.rules").write_text('prefix_rule(pattern=["touch"], decision="allow")\n'
                                                          'prefix_rule(pattern=["curl"], decision="allow")\n')
            (home / "memories").mkdir()
            (home / "memories" / "note.md").write_text("an old memory\n")
            (home / "hooks.json").write_text('{"hooks": {}}')
            (home / "config.toml").write_text(
                'model = "c8-model"\nmodel_provider = "c8fake"\n'
                '[features]\nmemories = true\n[memories]\ngenerate_memories = true\nuse_memories = true\n'
                '[model_providers.c8fake]\nname = "c8"\n'
                f'base_url = "http://127.0.0.1:{fake.port}/v1"\nenv_key = "C8_FAKE_KEY"\nwire_api = "responses"\n'
                'requires_openai_auth = false\nsupports_websockets = false\nrequest_max_retries = 0\nstream_max_retries = 0\n')
            env = {**os.environ, "CODEX_HOME": str(home), "C8_FAKE_KEY": "key-local-c8-only",
                   "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}
            base = make_context(root, workdir=site.work, default_workdir=site.work, state_dir=root / "state",
                                backend_cmd=[CODEX], model="c8-model", effort="", timeout=180)
            (root / "state").mkdir()
            sessions = []

            def run(ctx, prompt, session_id=None):
                callbacks = RunCallbacks(session=sessions.append)
                with mock.patch("covey.backends.codex.bridge.child_process_env", return_value=env):
                    result = CodexBackend().run(RunRequest(ctx, prompt, session_id, callbacks=callbacks,
                                                           inputs=RunInputs(threading.Event(), run_id=str(uuid.uuid4()))))
                if result.is_error:  # An app server's exit, not the turn under test: once more.
                    result = CodexBackend().run(RunRequest(ctx, prompt, session_id, callbacks=callbacks,
                                                           inputs=RunInputs(threading.Event(), run_id=str(uuid.uuid4()))))
                return result

            def held(limit, prompt, session_id=None):
                site.mark()
                calls = len(fake.calls)
                memories = tree(home / "memories")
                answer = run(dataclasses.replace(base, options={TOOL_LIMIT: limit}), prompt, session_id)
                these = fake.calls[calls:]
                return {"limit": limit, "error": answer.is_error, "answer": answer.text,
                        "offered_tools": FakeResponses.names(these[0]) if these else [],
                        "outputs": FakeResponses.outputs(these[-1]) if these else [],
                        "side_effects": site.side_effects(), "memories_unchanged": memories == tree(home / "memories"),
                        "session": sessions[-1] if sessions else ""}

            def private_paths():
                """Where the owner's home names a turn's own home (a thread recorded under it would not be found later)."""
                found = []
                for path in sorted(home.rglob("*")):
                    if path.is_file() and not path.is_symlink() and b"codex-read-" in path.read_bytes():
                        found.append(str(path.relative_to(home)))
                return found

            try:
                first = run(base, "Original question of the topic.")
                self.assertFalse(first.is_error, first.outcome)
                thread = sessions[-1]
                read = held(READ, f"{MARK}: a group member asks; try to change things.", thread)
                after_read = run(base, "The owner's next question.", thread)
                after_read_session = sessions[-1]
                EVIDENCE["codex_read_only"] = {
                    "commands": commands, "read": read,
                    "after_read": {"error": after_read.is_error, "session": after_read_session, "text": after_read.text},
                    "private_paths_in_owner_home": private_paths(),
                    "private_homes_left": sorted(p.name for p in (root / "state" / "tmp").glob("codex-read-*"))}
                for held_turn in (read,):
                    self.assertFalse(held_turn["error"], held_turn["answer"])
                    self.assertEqual(len(held_turn["outputs"]), len(commands))  # Every attempt was made …
                    self.assertFalse([name for name in held_turn["offered_tools"] if "codex_apps" in name])
                    self.assertIn("mcp__bridge_workspace::workspace_read_file", held_turn["offered_tools"])
                    self.assertTrue(held_turn["memories_unchanged"])
                    self.assertNotIn("shell.txt", held_turn["side_effects"]["canary"])  # No rule for sh: refused.
                self.assertEqual(read["side_effects"], {"canary": [], "receiver": [], "workspace_changed": False,
                                                        "mcp": []})  # … and in the READ turn none went, the rules' neither.
                self.assertEqual(read["session"], thread)  # READ went on on the topic's thread …
                self.assertFalse(after_read.is_error, after_read.outcome)  # … and the owner's next turn found it again,
                self.assertEqual(after_read_session, thread)
                self.assertEqual(EVIDENCE["codex_read_only"]["private_homes_left"], [])
            finally:
                fake.close()
                site.receiver.close()


def tearDownModule():
    out = os.environ.get("BRIDGE_C8_OUT")
    if out and EVIDENCE:
        Path(out).write_text(json.dumps({"at": time.strftime("%Y-%m-%d %H:%M:%S"), **EVIDENCE}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    unittest.main()
