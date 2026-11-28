"""Codex 原生快路 (F2b, core/device_native.py, backends/codex/device_home.py): the 体检's exec-server handshake, which
devices a Codex turn names as its environments (G1-G3, a topic that keeps off this machine), the persistent device home
and its environments.toml, and the app server's parameters (thread/start, thread/fork and every turn/start name the same
list; a topic that kept no device is as before).

``NativeRealCli`` (opt-in, BRIDGE_C8_REAL_CLI=1 and an installed codex, no model call: a scripted local Responses API) runs
the real CLI through CodexBackend.run: U6 (a thread started in the owner's home goes on in the device home and back, its
environments named every turn) and U7 (Codex's own ``forbidden`` rule against a command on a device environment), the
device simulated with bwrap as research-codex.md E4. BRIDGE_C8_OUT names a JSON file the evidence is written to.
"""
from __future__ import annotations

import dataclasses
import json
import os
import shutil
import stat
import tempfile
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support import make_context
from covey.backends.codex import appserver, device_home
from covey.backends.codex.bridge import CodexBackend
from covey.core import device_access, device_native, device_probe, devices
from covey.core.device_transport import LocalTransport
from covey.core.harness import CONSULT, EXTERNAL, LEAD, READ, TOOL_LIMIT, RunCallbacks, RunInputs, RunRequest
from covey.core.run_dir import DEVICE_TURN

ON = os.environ.get("BRIDGE_C8_REAL_CLI") == "1"
CODEX = os.environ.get("BRIDGE_C8_CODEX") or shutil.which("codex") or ""
BWRAP = shutil.which("bwrap") or ""
EVIDENCE = {}

DEVICES = {"schema": 1, "devices": {
    "gpu1": {"ssh": {"host": "me@gpu1"}, "access": "write"},
    "box": {"ssh": {"host": "me@box"}, "access": "read"},
    "team": {"ssh": {"host": "me@team"}, "access": "write", "spaces": ["team"]}}}
READY = {"ok": True, "same": True, "why": "", "executor": "0.172.0", "os": "linux", "home": "/home/me",
         "path": "/home/me/.local/bin/codex"}


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="codex-native-", ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.task = self.state / "runs" / "task-1"
        self.task.mkdir(parents=True)
        path = self.root / "devices.json"
        path.write_text(json.dumps(DEVICES))
        self.environ = {"COVEY_DEVICES": "1", "COVEY_DEVICES_FILE": str(path)}
        patcher = mock.patch.dict(os.environ, self.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        self.config = devices.current()
        for alias in DEVICES["devices"]:
            self.profile(alias)

    def profile(self, alias, **record):
        folder = devices.device_dir(self.state, alias)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "profile.json").write_text(json.dumps({"home": "/home/me", "env": {}, "exec_server": {**READY, **record}}))

    def turn(self, limit="", space="owner_private"):
        device_access.note_turn(self.task, limit=limit, space=space, topic="topic-1")
        return device_native.turn_of(self.state, self.task, "topic-1")


class ProbeTest(Case):
    def test_a_handshake_is_read_with_its_version_capabilities_and_clean_end(self):
        answer = json.dumps({"id": 1, "result": {"environmentInfo": {
            "executorVersion": "0.172.0", "platformOs": "macos", "userHomeDir": "file:///Users/me",
            "capabilities": {"a": True, "b": False, "c": True}}}})
        record = device_native.read_probe({"path": "/x/codex", "version": "codex-cli 0.172.0", "answer": answer, "exit": 0},
                                          hub="0.172.0", hub_capabilities=["a", "d"], now=5.0)
        self.assertEqual((record["executor"], record["os"], record["home"]), ("0.172.0", "macos", "/Users/me"))
        self.assertEqual(record["capabilities"], ["a", "c"])
        self.assertEqual(record["differs"], ["c", "d"])
        self.assertTrue(record["ok"] and record["same"])
        self.assertEqual(record["why"], "")
        older = device_native.read_probe({"path": "/x/codex", "answer": answer.replace("0.172.0", "0.164.0"), "exit": 0},
                                         hub="0.172.0")
        self.assertIn("版本一致才挂", older["why"])
        self.assertIn("没有 codex", device_native.read_probe({"path": ""}, hub="0.172.0")["why"])
        self.assertIn("没有答复", device_native.read_probe({"path": "/x/codex", "timed_out": True}, hub="0.172.0")["why"])
        unclean = device_native.read_probe({"path": "/x/codex", "answer": answer, "exit": None}, hub="0.172.0")
        self.assertFalse(unclean["ok"])
        self.assertIn("没有干净退出", unclean["why"])

    def test_the_probe_waits_for_the_answer_before_it_closes_stdin(self):
        """codex-cli 0.172.0's exec-server ends at its stdin's end before it answers: the probe on a device (the real
        script, through the device program's exec) answers all the same. A codex here that answers like it."""
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "codex"
        fake.write_text("#!/bin/sh\n" 'if [ "$1" = "--version" ]; then echo "codex-cli 0.172.0"; exit 0; fi\n'
                        'read line; sleep 0.3; echo \'{"id":1,"result":{"environmentInfo":{"executorVersion":"0.172.0",'
                        '"platformOs":"linux","capabilities":{"x":true}}}}\'; cat >/dev/null; exit 0\n')
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        home = self.root / "devhome"
        home.mkdir()
        from support import exec_tempdir
        with exec_tempdir() as runnable:
            shutil.copy2(fake, Path(runnable) / "codex")
            transport = LocalTransport(home, env={"PATH": runnable + os.pathsep + os.environ.get("PATH", "")})
            devices.push_file(transport, devices.shim_bytes())
            device = self.config.device("gpu1")
            record = device_native.handshake(transport, device, {}, hub="0.172.0", hub_probe={}, now=lambda: 1.0)
        self.assertEqual((record["executor"], record["exit"], record["why"]), ("0.172.0", 0, ""), record)
        self.assertTrue((home / ".covey" / "codex-exec").is_dir())  # Its own CODEX_HOME, never the device's ~/.codex.
        self.assertFalse((home / ".codex").exists())

    def test_a_read_only_device_is_never_run_on_and_the_step_never_fails_a_checkup(self):
        transport = mock.Mock()
        record = device_native.handshake(transport, self.config.device("box"), {}, hub="0.172.0", hub_probe={})
        self.assertIn("只读设备", record["why"])
        transport.call.assert_not_called()
        result = device_probe.CheckResult("gpu1", [], checkup=True)
        with mock.patch.object(device_native, "handshake", return_value={"ok": False, "why": "设备上没有 codex"}):
            device_probe.native_step(transport, self.config.device("gpu1"), {}, result, lambda: 1.0)
        self.assertTrue(result.ok)
        self.assertTrue(result.steps[-1].skipped)
        self.assertIn("Codex 原生快路不可用", result.explain())
        nothing = device_probe.CheckResult("box", [], checkup=True)
        device_probe.native_step(transport, self.config.device("box"), {}, nothing, lambda: 1.0)
        self.assertEqual((nothing.steps, nothing.exec_server), ([], None))

    def test_ready_needs_write_a_clean_handshake_and_the_hubs_version(self):
        gpu1, box = self.config.device("gpu1"), self.config.device("box")
        self.assertEqual(device_native.ready(gpu1, devices.read_profile(self.state, "gpu1"), "0.172.0"), "")
        self.assertIn("Hub 是 0.173.0", device_native.ready(gpu1, devices.read_profile(self.state, "gpu1"), "0.173.0"))
        self.assertEqual(device_native.ready(box, devices.read_profile(self.state, "box"), "0.172.0"), "只读设备")
        self.assertIn("体检", device_native.ready(gpu1, {}, "0.172.0"))
        self.assertEqual(device_native.version_of("codex-cli 0.172.0\n"), "0.172.0")


class AttachTest(Case):
    """Which devices a turn names (G1-G3, ``ready``) and which a turn's end keeps."""

    def data(self, *aliases):
        return {device_native.KEY: [{"alias": alias, "cwd": "/home/me", "at": 1.0} for alias in aliases]}

    def test_a_topic_that_kept_nothing_names_no_environments(self):
        self.assertIsNone(device_native.environments(self.turn(), self.config, {}, workdir="/w", hub="0.172.0"))

    def test_local_first_then_each_kept_device_this_turn_may_write_to(self):
        found = device_native.environments(self.turn(), self.config, self.data("gpu1", "box", "team", "gone"),
                                           workdir="/w", hub="0.172.0")
        # box is read-only (G2), team is not for the owner's private chat (G1), gone is not in devices.json.
        self.assertEqual(found, [{"environmentId": "local", "cwd": "/w"}, {"environmentId": "gpu1", "cwd": "/home/me"}])
        self.assertEqual(device_native.environments(self.turn(space="team"), self.config, self.data("team"), workdir="/w",
                                                    hub="0.172.0")[1:], [{"environmentId": "team", "cwd": "/home/me"}])

    def test_only_local_for_a_reading_turn_an_external_topic_another_version_or_no_turn_file(self):
        data, local = self.data("gpu1"), [{"environmentId": "local", "cwd": "/w"}]
        for limit in (READ, LEAD, CONSULT, "proxy"):
            with self.subTest(limit=limit):
                self.assertEqual(device_native.environments(self.turn(limit), self.config, data, workdir="/w",
                                                            hub="0.172.0"), local)
        self.assertEqual(device_native.environments(self.turn(), self.config, data, workdir="/w", local=False,
                                                    hub="0.172.0"), local)
        self.assertEqual(device_native.environments(self.turn(), self.config, data, workdir="/w", hub="0.173.0"), local)
        self.assertEqual(device_native.environments(None, self.config, data, workdir="/w", hub="0.172.0"), local)
        (self.task / DEVICE_TURN).unlink(missing_ok=True)
        unknown = device_native.turn_of(self.state, self.task, "topic-1")
        self.assertEqual(device_native.environments(unknown, self.config, data, workdir="/w", hub="0.172.0"), local)

    def test_a_turns_end_keeps_a_used_ready_writable_device_once(self):
        rows = [{"device": "gpu1", "op": "run"}, {"device": "box", "op": "get"}, {"device": "team", "op": "run"},
                {"device": "gpu1", "op": "ls"}]
        added = device_native.due(self.turn(), self.config, rows, {}, hub="0.172.0", now=9.0)
        self.assertEqual(added, [{"alias": "gpu1", "cwd": "/home/me", "at": 9.0}])
        self.assertEqual(device_native.due(self.turn(), self.config, rows, self.data("gpu1"), hub="0.172.0"), [])
        self.assertEqual(device_native.due(self.turn(READ), self.config, rows, {}, hub="0.172.0"), [])
        self.assertEqual(device_native.due(self.turn(), self.config, rows, {}, local=False, hub="0.172.0"), [])
        refused = [{"device": "gpu1", "op": "run", "refused": "no"}]
        self.assertEqual(device_native.due(self.turn(), self.config, refused, {}, hub="0.172.0"), [])
        declared = {"repos": [{"name": "proj", "path": "gpu1:/data/proj", "device": "gpu1", "mode": "write"}]}
        self.assertEqual([item["alias"] for item in device_native.due(self.turn(), self.config, [], declared,
                                                                      hub="0.172.0")], ["gpu1"])

    def test_the_cwd_is_the_devices_named_home_else_its_own(self):
        named = dataclasses.replace(self.config.device("gpu1"), home="~/work")
        self.assertEqual(device_native.device_home(named, {"home": "/home/me"}), "/home/me/work")
        self.assertEqual(device_native.device_home(dataclasses.replace(named, home="/srv/x/"), {}), "/srv/x")
        self.assertEqual(device_native.device_home(self.config.device("gpu1"), {"exec_server": {"home": "/h"}}), "/h")

    def test_the_audit_line_and_the_task_cards_words(self):
        turn = self.turn()
        device_native.note_attached(turn, [{"environmentId": "local", "cwd": "/w"},
                                           {"environmentId": "gpu1", "cwd": "/home/me"}])
        rows = device_access.read_audit(self.task)
        self.assertEqual([(row["device"], row["op"], row["path"]) for row in rows], [("gpu1", "native", "/home/me")])
        self.assertIn("gpu1：Codex 原生快路挂着（/home/me）", device_access.trace_text(rows))
        self.assertEqual(device_access.fold(rows)[0]["ops"], {"native": 1})
        self.assertEqual(device_native.used(rows, {}), [])  # Its own line is no use of the device.


class EnvironmentsFileTest(Case):
    def test_ssh_with_coveys_own_control_path_and_the_devices_codex(self):
        found = device_native.entries(self.state, self.config, "0.172.0")
        self.assertEqual([item["id"] for item in found], ["gpu1", "team"])  # box is read-only.
        args = found[0]["args"]
        self.assertEqual(found[0]["program"], "ssh")
        self.assertIn("ControlPath=%s/%%C.sock" % self.config.control_dir, args)
        self.assertIn("BatchMode=yes", args)
        remote = args[-1]
        self.assertEqual(args[-3:-1], ["me@gpu1", "--"])
        self.assertIn("PATH=/home/me/.local/bin:\"$PATH\"", remote)
        self.assertIn('CODEX_HOME="$HOME"/.covey/codex-exec', remote)
        self.assertTrue(remote.endswith("/home/me/.local/bin/codex exec-server --listen stdio"))
        self.assertEqual(device_native.entries(self.state, self.config, "0.173.0"), [])
        text = device_native.toml_text(found)
        self.assertIn("include_local = true", text)
        self.assertNotIn("default", text.split("\n", 1)[1])
        import tomllib
        parsed = tomllib.loads(text)
        self.assertEqual([item["id"] for item in parsed["environments"]], ["gpu1", "team"])
        self.assertEqual(parsed["environments"][0]["args"], args)

    def test_secrets_and_the_bridges_names_never_go_to_the_device(self):
        device = dataclasses.replace(self.config.device("gpu1"), env={"PATH": "/opt/bin:/usr/bin", "API_TOKEN": "x",
                                                                      "COVEY_STATE": "/s", "HTTPS_PROXY": "http://p:1"})
        remote = device_native.remote_command(device, devices.read_profile(self.state, "gpu1"))
        self.assertIn("PATH=/home/me/.local/bin:/opt/bin:/usr/bin", remote)
        self.assertIn("HTTPS_PROXY=http://p:1", remote)
        self.assertNotIn("API_TOKEN", remote)
        self.assertNotIn("COVEY_STATE", remote)


class DeviceHomeTest(Case):
    def test_every_entry_linked_its_own_environments_toml_and_the_real_home_untouched(self):
        real = self.root / "codex-home"
        (real / "sessions").mkdir(parents=True)
        (real / "auth.json").write_text("{}")
        (real / "config.toml").write_text('model = "m"\n')
        (real / "environments.toml").write_text("# the owner's own\n")
        (real / "rules").mkdir()
        (real / "rules" / "default.rules").write_text('prefix_rule(pattern=["ls"], decision="allow")\n')
        before = sorted(str(p.relative_to(real)) for p in real.rglob("*"))
        env = device_home.prepare(self.state, {"CODEX_HOME": str(real), "X": "1"}, [{"id": "gpu1", "program": "ssh",
                                                                                     "args": ["me@gpu1"]}])
        home = Path(env["CODEX_HOME"])
        self.assertEqual(home, self.state / "codex-devices" / "gpu1")
        self.assertEqual(env["X"], "1")
        self.assertEqual(sorted(p.name for p in home.iterdir()),
                         ["auth.json", "config.toml", "environments.toml", "rules", "sessions"])
        self.assertFalse((home / "rules" / "default.rules").is_symlink())  # Codex reads no linked rule file.
        self.assertEqual((home / "rules" / "default.rules").read_text(), 'prefix_rule(pattern=["ls"], decision="allow")\n')
        self.assertEqual(os.readlink(home / "sessions"), str(real / "sessions"))
        self.assertFalse((home / "environments.toml").is_symlink())
        self.assertIn('id = "gpu1"', (home / "environments.toml").read_text())
        self.assertEqual(stat.S_IMODE(home.stat().st_mode), 0o700)
        self.assertFalse((home / "rules").is_symlink())
        self.assertIn('decision="forbidden"', (home / "rules" / "covey-devices.rules").read_text())
        # Refreshed: an entry the real home got, one it lost, a file the app server made here, the list changed.
        (real / "auth.json").unlink()
        (real / "models_cache.json").write_text("{}")
        (home / "made-here.log").write_text("x")
        device_home.prepare(self.state, {"CODEX_HOME": str(real)}, [{"id": "gpu1", "program": "ssh",
                                                                     "args": ["-p", "2222", "me@gpu1"]}])
        self.assertEqual(sorted(p.name for p in home.iterdir()),
                         ["config.toml", "environments.toml", "made-here.log", "models_cache.json", "rules", "sessions"])
        self.assertIn('"2222"', (home / "environments.toml").read_text())
        both = device_home.path_of(self.state, ["team", "gpu1", "gpu1"])
        self.assertEqual(both.name, "gpu1+team")  # One home per set of devices.
        self.assertEqual((real / "environments.toml").read_text(), "# the owner's own\n")
        self.assertEqual(sorted(str(p.relative_to(real)) for p in real.rglob("*")),
                         sorted([*before, "models_cache.json"]) if "auth.json" not in before else
                         sorted([p for p in before if p != "auth.json"] + ["models_cache.json"]))


class AppServerParamsTest(Case):
    ENVS = [{"environmentId": "local", "cwd": "/w"}, {"environmentId": "gpu1", "cwd": "/home/me"}]

    def ctx(self, **options):
        return make_context(self.root, options=options)

    def sent(self, ctx):
        sent = {}
        rpc = SimpleNamespace(thread_id="thread-1", request=lambda method, params, timeout=None: sent.update(params)
                              or {"turn": {"id": "turn-1"}})
        appserver.start_turn(rpc, SimpleNamespace(inputs=SimpleNamespace(attachments=())), ctx, "hi", None)
        return sent

    def test_a_topic_without_devices_is_as_before(self):
        params, _ = appserver.thread_params(self.ctx(), None)
        self.assertNotIn("environments", params)
        sent = self.sent(self.ctx())
        self.assertNotIn("environments", sent)
        self.assertEqual(sent["input"][0]["text"].split("\n\n")[0], "hi")
        self.assertNotIn("设备环境", sent["input"][0]["text"])

    def test_start_and_fork_and_every_turn_name_the_same_list_resume_does_not(self):
        ctx = self.ctx(environments=self.ENVS)
        self.assertEqual(appserver.thread_params(ctx, None)[0]["environments"], self.ENVS)
        # thread/resume and thread/fork take none (codex-cli 0.172.0): the turn/start names them.
        self.assertNotIn("environments", appserver.thread_params(self.ctx(environments=self.ENVS, fork_session="s-1"),
                                                                 None)[0])
        self.assertNotIn("environments", appserver.thread_params(ctx, "thread-1")[0])
        sent = self.sent(ctx)
        self.assertEqual(sent["environments"], self.ENVS)
        self.assertIn("本话题挂着设备环境：gpu1（/home/me）", sent["input"][0]["text"])
        self.assertIn("covey device job", sent["input"][0]["text"])
        local = self.sent(self.ctx(environments=self.ENVS[:1], **{TOOL_LIMIT: READ}))
        self.assertEqual(local["environments"], self.ENVS[:1])
        self.assertNotIn("设备环境", local["input"][0]["text"])


class BackendRunTest(Case):
    """CodexBackend.run picks the turn's environments and home from topic.json (core/device_native.py)."""

    def setUp(self):
        super().setUp()
        self.real = self.root / "codex-home"
        self.real.mkdir()
        (self.real / "auth.json").write_text("{}")
        self.topic = self.root / "topic.json"

    def run_turn(self, data, **options):
        self.topic.write_text(json.dumps(data))
        ctx = make_context(self.root, topic_id="topic-1", history_path=self.root / "history.jsonl", options=options)
        seen = []
        backend = CodexBackend()
        request = RunRequest(ctx, "hi", "thread-1")
        env = {"CODEX_HOME": str(self.real), "PATH": os.environ.get("PATH", "")}
        with mock.patch("covey.backends.codex.bridge.child_process_env", return_value=env), \
                mock.patch.dict(os.environ, {"COVEY_TASK_DIR": str(self.task)}), \
                mock.patch.object(device_native, "hub_version", return_value="0.172.0"), \
                mock.patch.object(backend, "_run", side_effect=lambda request, ctx, env: seen.append((ctx, env)) or "done"):
            self.assertEqual(backend.run(request), "done")
        return seen[0]

    def test_a_topic_that_kept_nothing_runs_as_before(self):
        self.turn()
        ctx, env = self.run_turn({})
        self.assertNotIn("environments", ctx.options)
        self.assertEqual(env["CODEX_HOME"], str(self.real))
        self.assertFalse((self.state / "codex-devices").exists())

    def test_a_kept_device_runs_the_turn_in_the_device_home_with_local_first(self):
        self.turn()
        ctx, env = self.run_turn({device_native.KEY: [{"alias": "gpu1", "cwd": "/home/me"}]})
        self.assertEqual(ctx.options["environments"], [{"environmentId": "local", "cwd": str(self.root)},
                                                       {"environmentId": "gpu1", "cwd": "/home/me"}])
        self.assertEqual(env["CODEX_HOME"], str(self.state / "codex-devices" / "gpu1"))
        toml = (self.state / "codex-devices" / "gpu1" / "environments.toml").read_text()
        self.assertIn('id = "gpu1"', toml)
        self.assertNotIn('id = "team"', toml)  # Ready, but not this topic's: never connected.
        self.assertFalse((self.real / "environments.toml").exists())
        self.assertEqual([row["op"] for row in device_access.read_audit(self.task)], ["native"])

    def test_a_reading_turn_names_local_alone_in_its_reading_home(self):
        self.turn(READ)
        with mock.patch("covey.backends.codex.bridge.ReadingHome") as home:
            home.return_value.__enter__.return_value = {"CODEX_HOME": "/absolute/reading"}
            ctx, env = self.run_turn({device_native.KEY: [{"alias": "gpu1", "cwd": "/home/me"}]}, **{TOOL_LIMIT: READ})
        self.assertEqual(ctx.options["environments"], [{"environmentId": "local", "cwd": str(self.root)}])
        self.assertEqual(env, {"CODEX_HOME": "/absolute/reading"})
        self.assertEqual(device_access.read_audit(self.task), [])

    def test_the_device_switch_off_is_as_before_whatever_the_topic_kept(self):
        self.turn()
        with mock.patch.dict(os.environ, {"COVEY_DEVICES": "0"}):
            ctx, env = self.run_turn({device_native.KEY: [{"alias": "gpu1", "cwd": "/home/me"}]})
        self.assertNotIn("environments", ctx.options)
        self.assertEqual(env["CODEX_HOME"], str(self.real))

    def test_an_external_topic_names_local_alone(self):
        self.turn()
        ctx, env = self.run_turn({device_native.KEY: [{"alias": "gpu1", "cwd": "/home/me"}]}, **{EXTERNAL: {"dir": "/o"}})
        self.assertEqual(len(ctx.options["environments"]), 1)
        self.assertEqual(env["CODEX_HOME"], str(self.real))

    def test_a_turns_end_keeps_the_device_it_used(self):
        self.turn()
        device_access.audit(device_native.turn_of(self.state, self.task, "topic-1"), {"device": "gpu1", "op": "run"})
        self.topic.write_text(json.dumps({}))
        ctx = make_context(self.root, topic_id="topic-1", history_path=self.root / "history.jsonl")
        with mock.patch.dict(os.environ, {"COVEY_TASK_DIR": str(self.task)}), \
                mock.patch.object(device_native, "hub_version", return_value="0.172.0"), \
                mock.patch("covey.backends.codex.bridge.update_repos") as update:
            CodexBackend.keep_devices(ctx)
        change = update.call_args.args[2]
        self.assertEqual([item["alias"] for item in change([])], ["gpu1"])
        self.assertEqual(update.call_args.kwargs, {"key": device_native.KEY})


# ---------------------------------------------------------------- the real CLI (opt-in)


class Model:
    """A scripted Responses API: a turn whose input names a step runs that step's commands through ``exec_command`` (with
    ``environment_id`` when the tool has it), then ends; any other turn just answers."""

    def __init__(self, steps):
        self.steps, self.calls = steps, []
        model = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(404)
                self.end_headers()

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))))
                model.calls.append(body)
                if not self.path.endswith("/responses"):
                    self.send_response(404)
                    self.end_headers()
                    return
                events = [("response.created", {"type": "response.created", "response": {"id": "resp-n"}})]
                for index, item in enumerate(model.items(body)):
                    events += [("response.output_item.added", {"type": "response.output_item.added", "output_index": index,
                                                               "item": item}),
                               ("response.output_item.done", {"type": "response.output_item.done", "output_index": index,
                                                              "item": item})]
                events.append(("response.completed", {"type": "response.completed", "response": {
                    "id": "resp-n", "status": "completed", "output": [],
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
    def tool(body, name):
        for tool in body.get("tools") or []:
            if tool.get("name") == name:
                return tool
        return {}

    @staticmethod
    def outputs(body) -> list:
        return [(item.get("call_id"), item.get("output") if isinstance(item.get("output"), str)
                 else json.dumps(item.get("output"), ensure_ascii=False))
                for item in body.get("input") or [] if str(item.get("type", "")).endswith("_output")]

    def items(self, body):
        latest = json.dumps([item for item in body.get("input") or [] if item.get("role") == "user"][-1:],
                            ensure_ascii=False)
        step = next((name for name in self.steps if name in latest), None)
        if step is None:
            return [{"type": "message", "role": "assistant", "id": "m-plain",
                     "content": [{"type": "output_text", "text": "plain answer"}]}]
        done = len([call for call, _ in self.outputs(body) if str(call).startswith(step)])
        if done < len(self.steps[step]):
            command, where = self.steps[step][done]
            args = {"cmd": command, "yield_time_ms": 5000}
            if "environment_id" in json.dumps(self.tool(body, "exec_command").get("parameters") or {}):
                args["environment_id"] = where
            return [{"type": "function_call", "name": "exec_command", "call_id": f"{step}{done}",
                     "arguments": json.dumps(args)}]
        return [{"type": "message", "role": "assistant", "id": "m-" + step,
                 "content": [{"type": "output_text", "text": step + " done."}]}]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@unittest.skipUnless(ON and CODEX and BWRAP, "opt-in: BRIDGE_C8_REAL_CLI=1, an installed Codex and bwrap")
class NativeRealCli(Case):
    def test_u6_u7_a_thread_goes_on_across_homes_with_its_environments_and_a_forbidden_rule_on_the_device(self):
        root = self.root
        work, backing, view = root / "work", root / "remote-backing", root / "host-view"
        for folder in (work, backing, view):
            folder.mkdir()
        (backing / "marker.txt").write_text("ON-THE-DEVICE\n")
        (backing / "victim.txt").write_text("a plain rm is no G4 command\n")
        (backing / "victim-dir").mkdir()
        (work / "local-dir").mkdir()
        model = Model({
            "STEP-ENV": [("cat marker.txt; pwd", "gpu1"), ("ls", "local")],
            "STEP-RULE": [("rm -rf victim-dir", "gpu1"), ("rm -rf local-dir", "local"), ("touch touched.txt", "gpu1"),
                          ("rm victim.txt", "gpu1"), ("rm -rf victim-dir; echo rm-exit=$?", "gpu1")],
        })
        home = root / "codex-home"
        (home / "rules").mkdir(parents=True)
        # The owner's own rules: an allow rule for rm (Covey's forbidden one wins over it) and a forbidden touch.
        (home / "rules" / "default.rules").write_text('prefix_rule(pattern=["rm"], decision="allow")\n'
                                                      'prefix_rule(pattern=["touch"], decision="forbidden")\n')
        (home / "config.toml").write_text(
            'model = "n-model"\nmodel_provider = "nfake"\n'
            '[model_providers.nfake]\nname = "n"\n'
            f'base_url = "http://127.0.0.1:{model.port}/v1"\nenv_key = "N_FAKE_KEY"\nwire_api = "responses"\n'
            'requires_openai_auth = false\nsupports_websockets = false\nrequest_max_retries = 0\nstream_max_retries = 0\n')
        env = {**os.environ, "CODEX_HOME": str(home), "N_FAKE_KEY": "key-local-only",
               "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}
        hub = device_native.version_of(os.popen(f"{CODEX} --version").read())
        self.profile("gpu1", executor=hub, home=str(view), path=CODEX)
        self.profile("team", executor="0.0.1")  # Ready elsewhere, never this turn's: not in environments.toml.
        bwrapped = [BWRAP, "--dev-bind", "/", "/", "--bind", str(backing), str(view), "--", CODEX, "exec-server", "--listen",
                    "stdio"]
        from covey.core.topics import topic_file
        topic = topic_file(self.state, "topic-1")
        topic.parent.mkdir(parents=True, exist_ok=True)
        topic.write_text("{}")
        base = make_context(root, workdir=work, default_workdir=work, state_dir=self.state, topic_id="topic-1",
                            history_path=topic.parent / "history.jsonl", backend_cmd=[CODEX], model="n-model", effort="",
                            timeout=180)
        sessions = []

        def run(ctx, prompt, session_id=None):
            callbacks = RunCallbacks(session=sessions.append)
            calls = len(model.calls)
            with mock.patch("covey.backends.codex.bridge.child_process_env", return_value=env), \
                    mock.patch.dict(os.environ, {"COVEY_TASK_DIR": str(self.task)}), \
                    mock.patch("covey.core.devices.ssh_argv", return_value=bwrapped):
                result = CodexBackend().run(RunRequest(ctx, prompt, session_id, callbacks=callbacks,
                                                       inputs=RunInputs(threading.Event(), run_id=str(uuid.uuid4()))))
            these = model.calls[calls:]
            schema = json.dumps(Model.tool(these[0], "exec_command").get("parameters") or {}) if these else ""
            return {"error": result.is_error, "text": result.text, "session": sessions[-1] if sessions else "",
                    "multi": "environment_id" in schema, "outputs": Model.outputs(these[-1]) if these else [],
                    "context": [item for item in json.dumps(these[-1].get("input") if these else [],
                                                            ensure_ascii=False).split("\\n") if "<environment" in item]}

        try:
            self.turn()
            first = run(base, "The topic's first question.")
            self.assertFalse(first["error"], first)
            thread = first["session"]
            # The turn used gpu1 through covey device: its end keeps it (keep_devices).
            device_access.audit(device_native.turn_of(self.state, self.task, "topic-1"), {"device": "gpu1", "op": "run"})
            with mock.patch.dict(os.environ, {"COVEY_TASK_DIR": str(self.task)}):
                CodexBackend.keep_devices(base)
            kept = json.loads(topic.read_text())[device_native.KEY]
            self.assertEqual([(item["alias"], item["cwd"]) for item in kept], [("gpu1", str(view))])
            native = run(base, "STEP-ENV: look on the device.", thread)
            self.turn(READ)
            reading = run(dataclasses.replace(base, options={TOOL_LIMIT: READ}), "A member asks.", thread)
            self.turn()
            rule = run(base, "STEP-RULE: try to delete.", thread)
            # A thread started in the device home (a topic that kept a device before its thread: a new session) goes on
            # in the owner's own home once the topic keeps nothing (an older release, the field removed).
            fresh = run(base, "STEP-ENV: a new thread with the device.")
            data = json.loads(topic.read_text())
            topic.write_text(json.dumps({key: value for key, value in data.items() if key != device_native.KEY}))
            back = run(base, "The owner's home again.", fresh["session"])
            toml = (self.state / "codex-devices" / "gpu1" / "environments.toml").read_text()
            EVIDENCE["native"] = {"first": first, "native": native, "reading": reading, "rule": rule, "thread": thread,
                                  "fresh": fresh, "back": back,
                                  "environments_toml_ids": [line for line in toml.splitlines() if line.startswith("id =")],
                                  "real_home_has_environments": (home / "environments.toml").exists(),
                                  "kept": {"device_dir_after_compound": (backing / "victim-dir").exists(),
                                           "local_dir": (work / "local-dir").exists(),
                                           "plain_rm_file": (backing / "victim.txt").exists(),
                                           "touched": (backing / "touched.txt").exists()},
                                  "device_home_own": sorted(p.name for p in (self.state / "codex-devices" / "gpu1").iterdir()
                                                            if not p.is_symlink())}
            # U6: one thread across the owner's home, the device home and a reading home.
            self.assertEqual([native["session"], reading["session"], rule["session"]], [thread] * 3)
            self.assertTrue(fresh["multi"])
            self.assertFalse(back["error"], back)
            self.assertEqual(back["session"], fresh["session"])  # Started in the device home, resumed in the owner's.
            self.assertFalse(back["multi"])
            self.assertFalse(first["multi"])  # No environments named: the tools as before.
            self.assertTrue(native["multi"])  # local and gpu1.
            self.assertFalse(reading["multi"])  # local alone.
            self.assertIn("ON-THE-DEVICE", native["outputs"][0][1])
            self.assertIn(str(view), native["outputs"][0][1])
            self.assertEqual(EVIDENCE["native"]["environments_toml_ids"], ['id = "gpu1"'])
            self.assertFalse(EVIDENCE["native"]["real_home_has_environments"])
            self.assertFalse([line for line in native["context"] if "team" in line])
            # U7: Codex's forbidden rules hold on the device environment: Covey's G4 (rm -rf) over the owner's allow rule,
            # the owner's own forbidden touch; this machine's rm -rf in the same topic too (a rule names no environment).
            words = [text for call, text in rule["outputs"] if str(call).startswith("STEP-RULE")]
            self.assertIn("rejected", words[0])
            self.assertIn("rejected", words[1])
            self.assertIn("rejected", words[2])
            self.assertTrue(EVIDENCE["native"]["kept"]["local_dir"])
            self.assertFalse(EVIDENCE["native"]["kept"]["touched"])  # The owner's rule, copied into the device home.
            self.assertFalse(EVIDENCE["native"]["kept"]["plain_rm_file"])  # A plain rm is not G4's.
            self.assertIn("covey device", words[0])  # The rule's words, for the model.
        finally:
            model.close()
            out = os.environ.get("BRIDGE_C8_OUT")
            if out:
                Path(out).write_text(json.dumps(EVIDENCE, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    unittest.main()
