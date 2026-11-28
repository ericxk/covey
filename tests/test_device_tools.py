"""The device channel's reading tools for a turn whose shell cannot run ``covey device`` (covey/core/device_tools.py):
over the real device program under a temporary HOME (LocalTransport), through the server that carries them (Codex's
workspace reader, and the same without a workspace for Antigravity and Claude Code), the guards and the audit as the
channel has them, when a turn gets them (``mount``) and how each Harness's read-only turns start their server. No ssh, no
device."""
from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import make_context
from covey.backends.agy import bridge as agy_bridge
from covey.backends.agy import home as agy_home
from covey.backends.claude.argv import NO_MCP, stream_argv
from covey.backends.claude.bridge import ClaudeBackend
from covey.backends.codex import appserver, events, workspace_reader
from covey.core import device_access as access
from covey.core import device_cli, device_tools, devices
from covey.core import device_transport as dt
from covey.core.device_transport import LocalTransport
from covey.core.harness import CONSULT, EXTERNAL, LEAD, PROXY, READ, TOOL_LIMIT

TOPIC = "abcdef0123456789abcdef0123456789"
CONFIG = {"schema": 1, "devices": {
    "gpu1": {"title": "GPU 机", "ssh": {"host": "gpu1"}, "access": "write"},
    "peer": {"title": "同事的机器", "ssh": {"host": "peer"}, "access": "read"},
    "team": {"ssh": {"host": "team"}, "access": "write", "spaces": ["team"]}}}


class Case(unittest.TestCase):
    """A state directory with one task's run directory (its turn file written as a lead's turn in the owner's private
    chat), devices.json, and the HOME the device program runs in."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.state = self.root / "state"
        self.task = self.state / "runs" / "om_a-1"
        self.task.mkdir(parents=True)
        self.outputs = self.root / "outputs"
        self.device_home = self.root / "device"
        (self.device_home / "proj").mkdir(parents=True)
        (self.device_home / "proj" / "train.log").write_text("step 1\nstep 2\nloss 0.5\n")
        (self.device_home / ".ssh").mkdir()
        (self.device_home / ".ssh" / "id_ed25519").write_text("not a real key\n")
        (self.device_home / "keys").symlink_to(self.device_home / ".ssh")
        (self.root / "devices.json").write_text(json.dumps(CONFIG))
        patch = mock.patch.dict(os.environ, {"COVEY_DEVICES": "1", "COVEY_DEVICES_FILE": str(self.root / "devices.json"),
                                             "COVEY_TASK_DIR": str(self.task)})
        patch.start()
        self.addCleanup(patch.stop)
        devices._CACHE.clear()
        self.links = {}
        self.note(LEAD)

    def note(self, limit, space="owner_private"):
        access.note_turn(self.task, limit=limit, space=space, topic=TOPIC, outputs=str(self.outputs))

    def connect(self, config, alias, state_dir):
        if alias not in self.links:
            link = LocalTransport(self.device_home, alias=alias, hub="hub1")
            devices.push_file(link, dt.shim_bytes())
            self.links[alias] = link
        return self.links[alias]

    def calls(self, serve, *calls):
        """The answers (by id) of a stdio server ``serve(source, destination)`` to tools/list and ``calls``."""
        rows = [{"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                *({"jsonrpc": "2.0", "id": at, "method": "tools/call", "params": {"name": name, "arguments": args}}
                  for at, (name, args) in enumerate(calls, 2))]
        out = io.StringIO()
        serve(io.StringIO("".join(json.dumps(row) + "\n" for row in rows)), out)
        return {row["id"]: row["result"] for row in map(json.loads, out.getvalue().splitlines())}

    def codex_server(self, source, destination):
        workspace_reader.serve(self.root, source, destination, state=str(self.state), run_dir=str(self.task),
                               connect=self.connect)

    def devices_server(self, source, destination):
        """Antigravity's and Claude Code's: no workspace, the device tools alone."""
        workspace_reader.serve(None, source, destination, state=str(self.state), run_dir=str(self.task),
                               connect=self.connect)

    def audit(self):
        return access.read_audit(self.task)


def result(answer):
    return json.loads(answer["content"][0]["text"]) if not answer["isError"] else answer["content"][0]["text"]


class ToolsTest(Case):
    def test_both_servers_list_the_device_tools_and_read_a_device_through_the_channel(self):
        for serve in (self.codex_server, self.devices_server):
            with self.subTest(server=serve.__name__):
                found = self.calls(serve, ("device_list", {}), ("device_ls", {"device": "gpu1", "path": "~/proj"}),
                                   ("device_stat", {"device": "peer", "paths": ["~/proj/train.log", "~/none"]}),
                                   ("device_get", {"device": "gpu1", "path": "~/proj/train.log", "start_line": 2}))
                names = [tool["name"] for tool in found[1]["tools"]]
                workspace = ["workspace_read_file", "workspace_list_directory"] if serve == self.codex_server else []
                self.assertEqual(names, [*workspace, *device_tools.NAMES])
                rows = {row["alias"]: row for row in result(found[2])["devices"]}
                self.assertEqual((rows["gpu1"]["usable"], rows["gpu1"]["writable"]), (True, False), "a lead's turn reads only")
                self.assertFalse(rows["team"]["usable"], "a device of another chat (G1)")
                self.assertEqual([entry["name"] for entry in result(found[3])["entries"]], ["train.log"])
                stat = result(found[4])["results"]
                self.assertEqual([(item["exists"], item.get("size")) for item in stat], [(True, 23), (False, None)])
                page = result(found[5])
                self.assertEqual((page["content"], page["total_lines"], page["next_line"]), ("2: step 2\n3: loss 0.5", 3, None))
        self.assertEqual(sorted({row["op"] for row in self.audit()}), ["get", "list", "ls", "stat"], "each call audited")
        self.assertFalse(self.outputs.exists(), "reading by lines writes nothing")

    def test_a_turn_without_devices_has_its_old_tools_only(self):
        found = self.calls(lambda source, out: workspace_reader.serve(self.root, source, out),
                           ("device_ls", {"device": "gpu1"}))
        self.assertEqual([tool["name"] for tool in found[1]["tools"]], ["workspace_read_file", "workspace_list_directory"])
        self.assertIn("Unknown tool", result(found[2]))
        found = self.calls(self.devices_server, ("workspace_read_file", {"path": "x"}))
        self.assertIn("Unknown tool", result(found[2]), "no workspace, no workspace tool")

    def test_save_fetches_into_the_topics_outputs_only(self):
        found = self.calls(self.devices_server, ("device_get", {"device": "peer", "path": "~/proj/train.log", "save": True}))
        saved = result(found[2])
        self.assertEqual(Path(saved["path"]), self.outputs / "devices" / "peer" / "train.log")
        self.assertEqual(Path(saved["path"]).read_text(), "step 1\nstep 2\nloss 0.5\n")
        self.assertEqual(self.audit()[-1]["local"], saved["path"])

    def test_secrets_are_refused_as_given_and_through_a_link(self):
        found = self.calls(self.codex_server, ("device_get", {"device": "gpu1", "path": "~/.ssh/id_ed25519"}),
                           ("device_get", {"device": "gpu1", "path": "~/keys/known_hosts"}),
                           ("device_get", {"device": "gpu1", "path": "~/keys/config", "save": True}))
        self.assertTrue(all(found[at]["isError"] for at in (2, 3, 4)))
        self.assertIn("钥匙", result(found[2]))
        self.assertIn("钥匙", result(found[4]), "a link to ~/.ssh is ~/.ssh")
        self.assertFalse(self.outputs.exists())
        self.assertTrue(any(row.get("refused") for row in self.audit()))

    def test_a_chat_the_device_is_not_for_and_a_task_without_its_turn_file_are_refused(self):
        found = self.calls(self.codex_server, ("device_ls", {"device": "team"}))
        self.assertTrue(found[2]["isError"])
        self.assertIn("不给这个聊天用", result(found[2]))
        (self.task / "device-turn.json").unlink()
        found = self.calls(self.codex_server, ("device_ls", {"device": "gpu1"}))
        self.assertIn("没有设备通道的记录", result(found[2]))

    def test_text_only_inline_and_wrong_arguments_say_so(self):
        (self.device_home / "proj" / "blob.bin").write_bytes(b"\x00\x01\x02")
        found = self.calls(self.codex_server, ("device_get", {"device": "gpu1", "path": "~/proj/blob.bin"}),
                           ("device_get", {"device": "gpu1"}), ("device_ls", {"device": "gpu1", "depth": 3}),
                           ("device_get", {"device": "gpu1", "path": "~/proj/none.txt"}))
        self.assertIn("save=true", result(found[2]))
        self.assertIn("Missing argument", result(found[3]))
        self.assertIn("Unknown tool or argument", result(found[4]))
        self.assertIn("没有答复或出错", result(found[5]))

    def test_codex_names_the_device_tools_on_the_task_card(self):
        self.assertEqual(set(events.DEVICE_TOOLS), set(device_tools.NAMES))


class MountTest(Case):
    def ctx(self, **options):
        return make_context(self.root, state_dir=self.state, topic_id=TOPIC, options=options)

    def test_a_held_turn_of_this_machine_in_a_local_topic_with_a_device_for_its_chat_gets_them(self):
        args, env = device_tools.mount(self.ctx(**{TOOL_LIMIT: LEAD}), (LEAD,))
        self.assertEqual(args, ["--state", str(self.state), "--run-dir", str(self.task)])
        self.assertEqual((env["COVEY_DEVICES"], env["COVEY_DEVICES_FILE"]), ("1", str(self.root / "devices.json")))
        self.assertLessEqual(set(env), {"COVEY_DEVICES", "COVEY_DEVICES_FILE", *device_tools.KEPT_ENV})

    def test_every_other_turn_gets_none(self):
        held = {TOOL_LIMIT: LEAD}
        self.assertEqual(device_tools.mount(self.ctx(), (LEAD,)), ([], {}), "a turn not held")
        self.assertEqual(device_tools.mount(self.ctx(**{TOOL_LIMIT: CONSULT}), (LEAD,)), ([], {}), "a limit not named")
        self.assertEqual(device_tools.mount(self.ctx(**held, **{EXTERNAL: {"dir": "/x"}}), (LEAD,)), ([], {}), "external")
        on_device = dataclasses.replace(self.ctx(**held), device="gpu1")
        self.assertEqual(device_tools.mount(on_device, (LEAD,)), ([], {}), "a device's turn")
        self.note(LEAD, space="")
        self.assertEqual(device_tools.mount(self.ctx(**held), (LEAD,)), ([], {}), "a chat no device is for")
        (self.task / "device-turn.json").unlink()
        self.assertEqual(device_tools.mount(self.ctx(**held), (LEAD,)), ([], {}), "no turn file")
        self.note(LEAD)
        with mock.patch.dict(os.environ, {"COVEY_TASK_DIR": ""}):
            self.assertEqual(device_tools.mount(self.ctx(**held), (LEAD,)), ([], {}), "no task runner")
        with mock.patch.dict(os.environ, {"COVEY_DEVICES": "0"}):
            self.assertEqual(device_tools.mount(self.ctx(**held), (LEAD,)), ([], {}), "the switch off")
        (self.root / "devices.json").write_text(json.dumps({"schema": 1, "devices": {"watched": {"title": "只装节点"}}}))
        self.assertEqual(device_tools.mount(self.ctx(**held), (LEAD,)), ([], {}), "no device with ssh")


class HarnessesTest(Case):
    """Each Harness's read-only turns start the server with the tools; every other turn's is what it was."""

    BRIDGE = {"mcpServers": {"bridge": {"command": "mcp", "args": []}}}

    def ctx(self, **options):
        return make_context(self.root, state_dir=self.state, topic_id=TOPIC, options=options)

    def test_codex_gives_them_to_lead_and_read_and_lets_them_run(self):
        for options in ({TOOL_LIMIT: LEAD, "mcp_config": self.BRIDGE}, {TOOL_LIMIT: READ}):
            with self.subTest(limit=options[TOOL_LIMIT]):
                self.note(options[TOOL_LIMIT])
                config = appserver.thread_params(self.ctx(**options), None)[0]["config"]
                args = config["mcp_servers.bridge_workspace.args"]
                self.assertEqual(args[-4:], ["--state", str(self.state), "--run-dir", str(self.task)])
                self.assertIn("--root", args, "beside its workspace tools")
                self.assertEqual(config["mcp_servers.bridge_workspace.env"]["COVEY_DEVICES"], "1")
                self.assertEqual(config["mcp_servers.bridge_workspace.default_tools_approval_mode"], "approve")
        for options in ({}, {TOOL_LIMIT: CONSULT}, {TOOL_LIMIT: PROXY}, {TOOL_LIMIT: LEAD, EXTERNAL: {"dir": "/x"}}):
            with self.subTest(options=options):
                self.note(options.get(TOOL_LIMIT, ""))
                config = appserver.thread_params(self.ctx(**options), None)[0]["config"]
                self.assertNotIn("--run-dir", config["mcp_servers.bridge_workspace.args"])
                self.assertNotIn("mcp_servers.bridge_workspace.env", config)
                self.assertNotIn("mcp_servers.bridge_workspace.default_tools_approval_mode", config)
                with mock.patch.dict(os.environ, {"COVEY_DEVICES": "0"}):
                    self.assertEqual(appserver.thread_params(self.ctx(**options), None)[0]["config"], config,
                                     "as with the switch off")

    def test_antigravity_gives_them_to_a_lead_after_its_bridge_tools(self):
        lead = self.ctx(**{TOOL_LIMIT: LEAD, "mcp_config": self.BRIDGE})
        config = agy_bridge.home_config(lead)
        self.assertEqual(list(config["mcpServers"]), ["bridge", "bridge_workspace"], "the Bridge's server first")
        server = config["mcpServers"]["bridge_workspace"]
        self.assertTrue(server["args"][1].endswith("covey_workspace_reader.py"))
        self.assertNotIn("--root", server["args"], "the device tools alone")
        self.assertEqual(server["args"][-2:], ["--run-dir", str(self.task)])
        self.assertIn("device_ls", agy_bridge.AntigravityBackend().prompt_runtime_note(lead))
        self.assertIsNone(agy_bridge.home_config(self.ctx()), "an ordinary turn has no private home")
        with mock.patch.dict(os.environ, {"COVEY_DEVICES": "0"}):
            self.assertEqual(agy_bridge.home_config(lead), self.BRIDGE, "as before with the switch off")
            self.assertNotIn("device_ls", agy_bridge.AntigravityBackend().prompt_runtime_note(lead))

    def test_antigravitys_servers_share_one_room(self):
        def tool(name, size):
            return {"name": name, "description": "x" * size, "inputSchema": {}}
        listed = {"a": [tool("topic_start", 600), tool("other", 300)], "b": [tool("device_ls", 50), tool("device_get", 80)]}
        config = {"mcpServers": {"a": {"command": "a"}, "b": {"command": "b"}}}
        with mock.patch.object(agy_home, "listed_tools", lambda server, env: listed[server["command"]]):
            servers, _ = agy_home.agy_servers(config, {agy_home.EAGER_ENV: str(602 + 302 + 52)})
        self.assertEqual(set(servers["a"]["tools"]), {"topic_start", "other"}, "the first keeps its whole room")
        self.assertEqual(set(servers["b"]["tools"]), {"device_ls"}, "the next takes only what is left")

    def test_claude_gives_them_to_lead_and_read(self):
        argv = stream_argv(ClaudeBackend(), self.ctx(**{TOOL_LIMIT: LEAD, "mcp_config": self.BRIDGE}))
        config = json.loads(argv[argv.index("--mcp-config") + 1])
        self.assertEqual(list(config["mcpServers"]), ["bridge", "bridge_workspace"])
        self.assertEqual(argv[argv.index("--allowedTools") + 1], "mcp__bridge,mcp__bridge_workspace")
        self.note(READ)
        argv = stream_argv(ClaudeBackend(), self.ctx(**{TOOL_LIMIT: READ}))
        config = json.loads(argv[argv.index("--mcp-config") + 1])
        self.assertEqual(list(config["mcpServers"]), ["bridge_workspace"])
        self.assertIn("--strict-mcp-config", argv)
        self.assertNotIn("bridge_workspace", " ".join(stream_argv(ClaudeBackend(), self.ctx())), "an ordinary turn")
        with mock.patch.dict(os.environ, {"COVEY_DEVICES": "0"}):
            argv = stream_argv(ClaudeBackend(), self.ctx(**{TOOL_LIMIT: READ}))
            at = argv.index("--strict-mcp-config")
            self.assertEqual(argv[at:at + 3], NO_MCP, "as before with the switch off")


class CommandLineHintTest(Case):
    def test_a_held_turn_whose_command_cannot_reach_a_device_is_told_of_the_tools(self):
        def unreachable(config, alias, state_dir):
            raise dt.Unreachable("ssh: connect to host gpu1: Operation not permitted")
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"COVEY_STATE": str(self.state)}), \
                mock.patch.object(access, "_ssh_link", unreachable), contextlib.redirect_stderr(err):
            code = device_cli.main(["ls", "gpu1"])
        self.assertEqual(code, device_cli.UNREACHABLE)
        self.assertIn("device_ls", err.getvalue())


if __name__ == "__main__":
    unittest.main()
