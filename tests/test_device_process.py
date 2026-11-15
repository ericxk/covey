"""DeviceProcess (covey/core/device_link.py) and where the backends start a process (core/process.py spawn_process).

A device is a temporary HOME running the real shim (tests/support/fake_device.py); the Agent is a scripted local program
(tests/support/fake_agent.py, fake_harness.py, fixtures/harness/claude_jsonl.py, support/fake_app_server.py). The backends'
own classes (``ClaudeProcess`` through ``ClaudeBackend.run``, ``AppServer``) run a turn on it. Each test ends by closing
its processes and failing on anything of the device left running."""
from __future__ import annotations

import dataclasses
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from covey.backends.claude.bridge import ClaudeBackend
from covey.backends.claude.control import ClaudeControl
from covey.backends.claude.process import ClaudeProcess
from covey.backends.codex import appserver
from covey.backends.codex.rpc import AppServer, RpcError
from covey.core import device_link as dl
from covey.core import process as hub_process
from covey.core.harness import BridgeContext, RunCallbacks, RunInputs, RunRequest
from covey.core.steer import SteerNotReady
from support import REPO, make_context
from support.fake_backends import CLAUDE_JSONL, RUN
from support.fake_device import FakeDevice

AGENT = REPO / "tests" / "support" / "fake_agent.py"
APP_SERVER = REPO / "tests" / "support" / "fake_app_server.py"


@dataclasses.dataclass
class DeviceContext(BridgeContext):
    """The context of a turn on a device: the fields A4 adds to BridgeContext (``getattr`` reads them either way)."""
    device: str = ""
    remote_workdir: str = ""


def wait_for(predicate, timeout=15.0, step=0.02):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(step)
    return bool(predicate())


class Lines:
    """What a process's stdout yields, collected by a thread of its own."""

    def __init__(self, stream):
        self.stream, self.items, self.done = stream, [], threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            for line in self.stream:
                self.items.append(json.loads(line))
        except (OSError, ValueError):
            pass
        finally:
            self.done.set()

    def wait(self, count, timeout=15.0):
        return wait_for(lambda: len(self.items) >= count, timeout)

    def ns(self):
        return [item["n"] for item in self.items if "n" in item]


class DeviceCase(unittest.TestCase):
    patience, waiting = 6.0, 30.0

    def setUp(self):
        self.device = FakeDevice()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.processes = []
        self.addCleanup(self.finish)
        self.backoff = mock.patch.object(dl, "BACKOFF", (0.05, 0.1, 0.2))
        self.backoff.start()
        self.addCleanup(self.backoff.stop)
        self.link = dl.Link("gpu1", self.device.transport, SimpleNamespace(python=sys.executable, login_shell=False, env={}),
                            {}, self.patience, self.waiting, self.state)

    def finish(self):
        self.device.faults.latency, self.device.faults.fail_count = 0.0, 0
        for process in self.processes:
            try:
                process.close()
            except Exception:
                pass
        left = self.device.cleanup()
        self.tmp.cleanup()
        self.assertEqual(left, [], "processes of the device were left running")

    def ctx(self, **changes):
        base = make_context(self.root, workspace_name="unit", model="fixture-model", timeout=20,
                            backend_cmd=["backend"], workdir=self.state / "devices" / "gpu1" / "standin" / "abc")
        fields = {field.name: getattr(base, field.name) for field in dataclasses.fields(BridgeContext)}
        fields.update({"device": "gpu1", "remote_workdir": str(self.device.home), **changes})
        return DeviceContext(**fields)

    def spawn(self, argv, *, text=False, run=None, **kwargs):
        process = dl.DeviceProcess(argv, ctx=kwargs.pop("ctx", None) or self.ctx(), env=kwargs.pop("env", {}),
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=text,
                                   run_dir=run if run is not None else self.root / "run", link=self.link, **kwargs)
        self.processes.append(process)
        self.device.tasks.append(process.task)
        return process

    def agent(self, *flags, **kwargs):
        return self.spawn([sys.executable, str(AGENT), *flags], **kwargs)

    def say(self, process, obj):
        process.send_frame(json.dumps({"say": obj}), time.monotonic() + 10, durable=False)


class PopenFaceTest(DeviceCase):
    def test_a_process_like_a_popen(self):
        process = self.agent()
        lines = Lines(process.stdout)
        self.assertTrue(lines.wait(1))
        self.assertIsNone(process.pid)  # os.killpg(None, ...) is a TypeError: nothing signals the hub's own group.
        self.assertIsNone(process.poll())
        self.assertEqual(process.state(), "up")
        self.assertFalse(process.offline)
        with self.assertRaises(io.UnsupportedOperation):
            process.stdin.fileno()
        self.assertEqual(process.stdin.write(json.dumps({"a": 1}) + "\n"), len(json.dumps({"a": 1})) + 1)
        process.stdin.flush()
        self.assertTrue(lines.wait(2))
        self.assertEqual(lines.items[1], {"echo": '{"a": 1}'})
        process.stdin.write(json.dumps({"exit": 3}) + "\n")
        process.stdin.flush()
        self.assertEqual(process.wait(15), 3)
        self.assertEqual(process.returncode, 3)
        self.assertTrue(lines.done.wait(5))  # EOF once it ended.
        self.assertEqual(process.state(), "exited")
        self.assertTrue(process.close())
        with self.assertRaises(subprocess.TimeoutExpired):
            self.agent().wait(0.1)

    def test_text_mode_reads_and_writes_lines_like_the_app_server_does(self):
        process = self.agent(text=True)
        first = process.stdout.readline()
        self.assertIn("ready", json.loads(first))
        process.stdin.write(json.dumps({"b": 2}) + "\n")
        process.stdin.flush()
        self.assertEqual(json.loads(process.stdout.readline()), {"echo": '{"b": 2}'})
        process.stdin.close()  # End of input: the Agent ends by itself.
        self.assertEqual(process.wait(15), 0)
        self.assertTrue(process.stdin.closed)
        with self.assertRaises(ValueError):
            process.stdin.write("x\n")

    def test_a_signal_group_of_its_own_is_used_by_core_process(self):
        class Own:
            calls = []

            def poll(self):
                return None

            def signal_group(self, signum):
                self.calls.append(signum)

        hub_process.signal_group(Own(), signal.SIGTERM)
        self.assertEqual(Own.calls, [signal.SIGTERM])
        process = self.agent("--count", "1000", "--delay", "0.01")
        hub_process.signal_group(process, signal.SIGTERM)
        self.assertEqual(process.wait(15), -signal.SIGTERM)

    def test_no_device_is_plain_popen(self):
        for ctx in (None, SimpleNamespace(), SimpleNamespace(device=""), SimpleNamespace(device=mock.Mock())):
            with self.subTest(ctx=ctx):
                with mock.patch("covey.core.process.subprocess.Popen") as popen:
                    process = hub_process.spawn_process(["cmd", "a"], ctx=ctx, cwd="/x", env={"A": "b"},
                                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                                                        bufsize=1)
                self.assertIs(process, popen.return_value)
                popen.assert_called_once_with(["cmd", "a"], cwd="/x", env={"A": "b"}, start_new_session=True,
                                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None, text=True,
                                              bufsize=1)
        real = hub_process.spawn_process([sys.executable, "-c", "pass"], ctx=None)
        self.assertIsInstance(real, subprocess.Popen)
        real.wait()


class SpawnRequestTest(DeviceCase):
    def test_the_environment_and_command_the_device_gets(self):
        hub_env = {"PATH": "/hub/bin", "HOME": "/hub/home", "PWD": "/hub/standin", "COVEY_TASK_DIR": "/hub/run",
                   "LARK_BRIDGE_DATA_HOME": "/hub/data", "ANTHROPIC_API_KEY": "sk-hub", "ANTHROPIC_AUTH_TOKEN": "t",
                   "ANTHROPIC_MODEL": "m", "CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS": "1", "CLAUDE_CONFIG_DIR": "/hub/c",
                   "CLAUDE_CODE_TMPDIR": "/hub/tmp", "FEISHU_APP_SECRET": "s", "CODEX_HOME": "/hub/codex", "LANG": "C.UTF-8"}
        profile = {"tools": {"claude": {"found": True, "path": sys.executable}, "codex": {"found": False}},
                   "env": {"HTTPS_PROXY": "http://proxy.example:3128"}}
        self.link.profile = profile
        self.link.device = SimpleNamespace(python=sys.executable, login_shell=True, env={"NO_PROXY": "internal"})
        ctx = self.ctx(backend_cmd=["/hub/bin/claude"])
        with self.assertLogs("covey.core.device_link", "INFO") as logged:
            process = self.spawn(["/hub/bin/claude", "-c", "pass"], ctx=ctx, env=hub_env)
        spec = json.loads((self.device.task_dir(process.task) / "spec.json").read_text())["spec"]
        self.assertEqual(spec["argv"], [sys.executable, "-c", "pass"])
        self.assertEqual(spec["cwd"], str(self.device.home))
        self.assertEqual(spec["shell"], "login")
        self.assertEqual(spec["env"], {"ANTHROPIC_MODEL": "m", "CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS": "1",
                                       "LANG": "C.UTF-8", "HTTPS_PROXY": "http://proxy.example:3128",
                                       "NO_PROXY": "internal"})
        text = "\n".join(logged.output)
        self.assertIn("ANTHROPIC_API_KEY", text)  # Named in the log, never with a value.
        self.assertNotIn("sk-hub", text)
        process.close()

    def test_the_command_of_a_tool_the_profile_did_not_find_is_its_bare_name(self):
        self.assertEqual(dl.device_argv(["/hub/bin/codex", "app-server"], SimpleNamespace(backend_cmd=["/hub/bin/codex"]),
                                        {"tools": {"codex": {"found": False, "path": ""}}}), ["codex", "app-server"])
        self.assertEqual(dl.device_argv(["python", "x"], SimpleNamespace(backend_cmd=["python"]), {}), ["python", "x"])
        self.assertEqual(dl.device_argv(["claude", "-p"], SimpleNamespace(backend_cmd=["other"]), {}), ["claude", "-p"])

    def test_a_context_without_a_remote_directory_or_an_unreachable_device_is_a_spawn_error(self):
        with self.assertRaises(ValueError):
            dl.DeviceProcess(["x"], ctx=self.ctx(remote_workdir=""), link=self.link)
        self.device.faults.fail(5)
        with self.assertRaises(dl.DeviceSpawnError):
            self.agent()
        self.assertIsInstance(dl.DeviceSpawnError("x"), OSError)

    def test_the_checkpoint_is_mirrored_to_the_hub_and_seed_files_are_written(self):
        device_path = self.device.home / "cp.json"
        hub_path = self.root / "hub-checkpoint.json"
        process = self.spawn(self.device.harness("--stay", "--checkpoint", str(device_path)), checkpoint={
            "path": str(device_path), "hub_path": hub_path, "poll": 0.1}, files=[
            {"path": str(self.device.home / "seed.txt"), "text": "seed", "mode": 0o600}])
        self.assertEqual((self.device.home / "seed.txt").read_text(), "seed")
        lines = Lines(process.stdout)
        process.stdin.write(json.dumps({"cp": '{"requires_user": false}'}) + "\n")
        process.stdin.flush()
        self.assertTrue(wait_for(lambda: hub_path.exists() and hub_path.read_text() == '{"requires_user": false}'))
        self.assertEqual(hub_path.stat().st_mode & 0o777, 0o600)
        process.stdin.write(json.dumps({"exit": 0}) + "\n")
        process.stdin.flush()
        self.assertEqual(process.wait(15), 0)
        self.assertTrue(lines.done.wait(5))


class LinkTest(DeviceCase):
    def test_killing_the_attach_loses_and_repeats_no_line(self):
        process = self.agent("--count", "60", "--delay", "0.02")
        lines = Lines(process.stdout)
        for _ in range(3):
            self.assertTrue(lines.wait(len(lines.items) + 8, 10))
            self.device.kill_attaches()
        self.assertTrue(lines.done.wait(30))
        self.assertEqual(lines.ns(), list(range(60)))
        self.assertEqual(process.wait(5), 0)
        self.assertGreaterEqual(len(self.device.transport.attachments), 2)

    def test_a_lost_frame_is_a_gap_and_the_link_attaches_again_from_its_offset(self):
        process = self.agent("--count", "30", "--delay", "0.02")
        lines = Lines(process.stdout)
        self.assertTrue(lines.wait(3))
        self.device.faults.drop(2)
        self.assertTrue(lines.done.wait(30))
        self.assertEqual(lines.ns(), list(range(30)))
        self.assertEqual(self.device.faults.dropped, 2)
        self.assertGreater(len(self.device.transport.attachments), 1)

    def test_the_offset_is_saved_to_device_json(self):
        process = self.agent("--count", "120", "--delay", "0.005")
        lines = Lines(process.stdout)
        self.assertTrue(lines.done.wait(30))
        process.wait(5)
        record = dl.read_record(self.root / "run")
        self.assertEqual(record["device"], "gpu1")
        self.assertEqual(record["task"], process.task)
        self.assertEqual(record["tasks"], [process.task])
        self.assertEqual(record["state"], "exited")
        journal = (self.device.task_dir(process.task) / "journal").stat().st_size
        self.assertEqual(record["offset"], journal)  # The exit record was the last one.
        self.assertEqual(record["exit"]["code"], 0)
        self.assertEqual((self.root / "run" / "device.json").stat().st_mode & 0o777, 0o600)

    def test_the_offset_is_saved_every_50_records_or_2_seconds(self):
        process = self.agent()
        lines = Lines(process.stdout)
        self.assertTrue(lines.wait(1))
        process.send_frame(json.dumps({"say": {"idle": True}}), time.monotonic() + 10)
        self.assertTrue(lines.wait(2))
        process._save(force=True)

        def frames(count):
            for _ in range(count):
                start = process._offset
                self.assertTrue(process._handle({"k": "out", "o": start, "e": start + 10, "d": "x"}))

        with mock.patch.object(dl, "write_json", wraps=dl.write_json) as saved:
            process._saved_at = time.monotonic()
            frames(49)
            self.assertEqual(saved.call_count, 0)
            frames(1)
            self.assertEqual(saved.call_count, 1)  # The 50th record.
            frames(3)
            self.assertEqual(saved.call_count, 1)
            process._saved_at = time.monotonic() - 3
            frames(1)  # Two seconds since the last save.
            self.assertEqual(saved.call_count, 2)
        self.assertEqual(dl.read_record(self.root / "run")["offset"], process._offset)

    def test_a_task_stopped_from_outside_ends_the_process(self):
        process = self.agent()
        lines = Lines(process.stdout)
        self.assertTrue(lines.wait(1))
        self.device.transport.call("reap", task=process.task, grace=1)
        self.assertTrue(lines.done.wait(15))
        self.assertEqual(process.wait(5), -signal.SIGTERM)
        self.assertEqual(process.state(), "exited")
        self.assertEqual(process.exit_info["reason"], "stopped")

    def test_a_task_the_device_does_not_have_is_gone(self):
        process = self.agent()
        lines = Lines(process.stdout)
        self.assertTrue(lines.wait(1))
        pgid = json.loads((self.device.task_dir(process.task) / "state.json").read_text())["pgid"]
        shutil.rmtree(self.device.task_dir(process.task))
        self.device.kill_attaches()
        try:
            self.assertEqual(process.wait(20), dl.GONE)
            self.assertEqual((process.state(), process.reason), ("gone", "no_task"))
            self.assertTrue(lines.done.wait(5))
            self.assertFalse(process.cleanup_confirmed)
        finally:
            os.killpg(pgid, signal.SIGKILL)


class InputTest(DeviceCase):
    def test_send_frame_waits_for_the_ack_and_numbers_the_lines(self):
        process = self.agent()
        lines = Lines(process.stdout)
        self.assertEqual(process.send_frame(json.dumps({"k": 1}), time.monotonic() + 10), "acked")
        self.assertEqual(process.send_frame(json.dumps({"k": 2}), time.monotonic() + 10), "acked")
        self.assertTrue(lines.wait(3))
        self.assertEqual([item.get("echo") for item in lines.items[1:]], ['{"k": 1}', '{"k": 2}'])
        details = process.details()
        self.assertEqual((details["seq"], details["acked"]), (2, 2))
        with self.assertRaises(ValueError):
            process.send_frame("two\nlines")

    def test_offline_a_plain_frame_is_refused_and_nothing_is_sent(self):
        process = self.agent()
        lines = Lines(process.stdout)
        self.assertTrue(lines.wait(1))
        self.device.faults.fail(10 ** 6)
        self.device.kill_attaches()
        self.assertTrue(wait_for(lambda: process.offline))
        with self.assertRaises(dl.DeviceOffline):
            process.send_frame(json.dumps({"lost": True}), time.monotonic() + 1)
        self.assertEqual(process.details()["seq"], 0)  # No number was used up.
        self.device.faults.fail_count = 0
        self.assertTrue(wait_for(lambda: not process.offline))
        self.assertEqual(process.send_frame(json.dumps({"after": 1}), time.monotonic() + 10), "acked")
        self.assertTrue(lines.wait(2))
        self.assertEqual([item.get("echo") for item in lines.items[1:]], ['{"after": 1}'])

    def test_a_supplement_while_the_link_is_down_is_steer_not_ready_on_both_backends(self):
        process = self.agent()
        self.device.faults.fail(10 ** 6)
        self.device.kill_attaches()
        self.assertTrue(wait_for(lambda: process.offline))
        control = ClaudeControl(SimpleNamespace(device=True, process=process), None, lambda *a: None)
        control.accepting_steer = True
        with self.assertRaises(SteerNotReady):
            control.steer("请再看看", "evt-1")
        self.assertFalse(control.control_error)  # Nothing was sent, so nothing is uncertain.
        rpc = object.__new__(AppServer)
        rpc.input_lock, rpc.device, rpc.process = threading.RLock(), True, process
        rpc.accepting_input, rpc.turn_id, rpc.reply_state, rpc.control_error = True, "turn-1", None, False
        with self.assertRaises(SteerNotReady):
            rpc.steer("补充", "msg-1")
        self.assertFalse(rpc.control_error)
        self.device.faults.fail_count = 0

    def test_an_answer_is_kept_while_the_link_is_down_and_delivered_once_in_order(self):
        process = self.agent()
        lines = Lines(process.stdout)
        self.assertTrue(lines.wait(1))
        self.device.faults.fail(10 ** 6)
        self.device.kill_attaches()
        self.assertTrue(wait_for(lambda: process.offline))
        results = [process.send_frame(json.dumps({"type": "control_response", "response": {
            "subtype": "success", "request_id": "r%d" % number, "response": {}}}), time.monotonic() + 1)
            for number in (1, 2, 3)]
        self.assertEqual(results, ["queued"] * 3)
        self.assertEqual(len(lines.items), 1)
        self.device.faults.fail_count = 0
        self.assertTrue(lines.wait(4, 20))
        time.sleep(0.5)
        echoes = [json.loads(item["echo"])["response"]["request_id"] for item in lines.items[1:]]
        self.assertEqual(echoes, ["r1", "r2", "r3"])
        self.assertTrue(wait_for(lambda: process.details()["acked"] == 3))

    def test_the_request_ledger_does_not_deliver_an_answered_request_again(self):
        process = self.agent()
        lines = Lines(process.stdout)
        self.assertTrue(lines.wait(1))
        request = {"type": "control_request", "request_id": "ask-1", "request": {"subtype": "can_use_tool"}}
        self.say(process, request)
        self.say(process, {"marker": 1})
        self.assertTrue(lines.wait(3))
        self.assertEqual(process.details()["seq"], 2)
        process._save(force=True)
        self.assertEqual(dl.read_record(self.root / "run")["requests"]["ask-1"]["state"], "open")
        answer = json.dumps({"type": "control_response", "response": {"subtype": "success", "request_id": "ask-1",
                                                                      "response": {"behavior": "allow"}}})
        self.assertEqual(process.send_frame(answer, time.monotonic() + 10), "acked")
        with self.assertRaises(dl.DeviceRequestClosed):  # Only an open request takes an answer.
            process.send_frame(answer, time.monotonic() + 10)
        self.assertEqual(process.details()["seq"], 3)
        self.assertTrue(lines.wait(4))  # ready, the request, the marker, the echo of the answer.
        # The journal is read from the start again (a Bridge that lost its saved offset): the answered request is
        # not handed to the reader a second time, everything else is.
        before = len(lines.items)
        with process._cond:
            process._offset = 0
        self.device.kill_attaches()
        self.assertTrue(wait_for(lambda: len(lines.items) >= before + 3, 20))
        time.sleep(0.3)
        rows = lines.items
        self.assertEqual([row.get("request_id") for row in rows if row.get("type") == "control_request"], ["ask-1"])
        self.assertGreaterEqual(len([row for row in rows if row.get("marker")]), 2)
        process._save(force=True)
        self.assertEqual(dl.read_record(self.root / "run")["requests"]["ask-1"]["state"], "answered")

    def test_a_cancelled_request_is_closed_and_a_json_rpc_answer_is_checked_too(self):
        process = self.agent()
        lines = Lines(process.stdout)
        self.assertTrue(lines.wait(1))
        self.say(process, {"id": 7, "method": "item/tool/requestUserInput", "params": {}})
        self.say(process, {"type": "control_request", "request_id": "c1", "request": {}})
        self.say(process, {"type": "control_cancel_request", "request_id": "c1"})
        self.assertTrue(lines.wait(4))
        with self.assertRaises(dl.DeviceRequestClosed):
            process.send_frame(json.dumps({"type": "control_response", "response": {"request_id": "c1"}}))
        self.assertEqual(process.send_frame(json.dumps({"id": 7, "result": {"answers": {}}}), time.monotonic() + 10),
                         "acked")
        with self.assertRaises(dl.DeviceRequestClosed):
            process.send_frame(json.dumps({"id": 7, "result": {"answers": {}}}))
        self.assertEqual(process.details()["seq"], 4)


class PatienceTest(DeviceCase):
    patience, waiting = 1.5, 6.0

    def lose_the_device(self, process):
        self.device.faults.fail(10 ** 6)
        self.device.kill_attaches()
        self.assertTrue(wait_for(lambda: process.state() == "down"))

    def test_past_the_patience_the_process_is_lost(self):
        process = self.agent()
        lines = Lines(process.stdout)
        self.assertTrue(lines.wait(1))
        began = time.monotonic()
        self.lose_the_device(process)
        self.assertEqual(process.wait(15), dl.LOST)
        self.assertGreaterEqual(time.monotonic() - began, self.patience - 0.5)
        self.assertLess(time.monotonic() - began, self.patience + 5)
        self.assertTrue(process.lost)
        self.assertEqual(process.state(), "lost")
        self.assertEqual(process.reason, "device_lost")
        self.assertTrue(lines.done.wait(5))  # EOF: the reader ends.
        self.assertFalse(process.cleanup_confirmed)  # The Agent may still run there.
        record = dl.read_record(self.root / "run")
        self.assertEqual((record["state"], record["lost"]), ("lost", True))
        self.assertFalse(process.close())
        with self.assertRaises(dl.DeviceClosed):
            process.send_frame(json.dumps({"x": 1}))
        # Back again, the device's process is stopped by reap, which is how the app settles a lost one.
        self.device.faults.fail_count = 0
        self.assertTrue(process.reap(1.0))
        self.assertTrue(process.cleanup_confirmed)
        self.assertTrue(wait_for(lambda: (self.device.task_dir(process.task) / "exit.json").exists()))

    def test_while_a_question_waits_the_longer_patience_counts(self):
        process = self.agent()
        lines = Lines(process.stdout)
        self.assertTrue(lines.wait(1))
        waiting = threading.Event()
        waiting.set()
        process.set_hold(waiting.is_set)
        began = time.monotonic()
        self.lose_the_device(process)
        time.sleep(self.patience + 1.0)  # Past the short patience: still not lost.
        self.assertEqual(process.state(), "down")
        self.assertIsNone(process.poll())
        self.assertGreater(process.details()["down_seconds"], self.patience)
        waiting.clear()  # The question was answered or gone: the short patience applies to what has gone by.
        self.assertEqual(process.wait(10), dl.LOST)
        self.assertEqual(process.state(), "lost")
        self.assertLess(time.monotonic() - began, self.waiting)
        self.device.faults.fail_count = 0

    def test_the_longer_patience_runs_out_too(self):
        self.link.waiting_patience = 2.5
        process = self.agent()
        lines = Lines(process.stdout)
        self.assertTrue(lines.wait(1))
        process.set_hold(lambda: True)
        began = time.monotonic()
        self.lose_the_device(process)
        self.assertEqual(process.wait(15), dl.LOST)
        self.assertGreaterEqual(time.monotonic() - began, 2.0)
        self.device.faults.fail_count = 0

    def test_a_short_outage_is_not_lost_and_the_process_goes_on(self):
        process = self.agent("--count", "40", "--delay", "0.05")
        lines = Lines(process.stdout)
        self.assertTrue(lines.wait(2))
        self.device.faults.fail(3)
        self.device.kill_attaches()
        self.assertTrue(lines.done.wait(30))
        self.assertEqual(lines.ns(), list(range(40)))
        self.assertEqual(process.wait(5), 0)
        self.assertFalse(process.lost)


class StopTest(DeviceCase):
    def test_stop_group_ends_the_agent_through_the_device(self):
        process = self.agent()
        lines = Lines(process.stdout)
        self.assertTrue(lines.wait(1))
        self.assertTrue(hub_process.stop_group(process, 5))
        self.assertEqual(process.returncode, -signal.SIGTERM)
        self.assertTrue(lines.done.wait(5))
        self.assertTrue(process.close())

    def test_an_agent_that_ignores_term_is_killed_after_the_grace(self):
        process = self.spawn(self.device.harness("--stay", "--ignore-term"))
        lines = Lines(process.stdout)
        self.assertTrue(lines.wait(1))
        self.assertTrue(hub_process.stop_group(process, 1))
        self.assertEqual(process.returncode, -signal.SIGKILL)

    def test_a_stop_asked_while_the_link_is_down_is_sent_when_it_is_back(self):
        process = self.agent()
        lines = Lines(process.stdout)
        self.assertTrue(lines.wait(1))
        self.device.faults.fail(10 ** 6)
        self.device.kill_attaches()
        self.assertTrue(wait_for(lambda: process.offline))
        process.signal_group(signal.SIGKILL)
        self.assertIsNone(process.poll())
        self.device.faults.fail_count = 0
        self.assertEqual(process.wait(20), -signal.SIGKILL)

    def test_reap_stops_what_the_task_left_behind(self):
        pidfile = self.device.home / "pids"
        process = self.spawn(self.device.harness("--stay", "--grandchild", "--pidfile", str(pidfile)))
        lines = Lines(process.stdout)
        self.assertTrue(lines.wait(1))
        child = int(Path(str(pidfile) + ".child").read_text())
        os.kill(child, 0)
        self.assertTrue(process.reap(1.0))
        self.assertTrue(wait_for(lambda: not Path("/proc/%d" % child).exists() or
                                 Path("/proc/%d/stat" % child).read_text().split()[2] == "Z"))
        self.assertTrue(process.reaped)
        self.assertTrue(process.close())

    def test_close_reaps_what_an_ended_agent_left(self):
        pidfile = self.device.home / "pids2"
        process = self.spawn(self.device.harness("--grandchild", "--count", "1", "--pidfile", str(pidfile)))
        lines = Lines(process.stdout)
        self.assertTrue(lines.done.wait(15))
        self.assertEqual(process.wait(5), 0)
        self.assertEqual(process.exit_info["left"], 1)
        self.assertFalse(process.cleanup_confirmed)
        self.assertTrue(process.close())  # The leftover is reaped there.


class ClaudeOnADeviceTest(DeviceCase):
    """``ClaudeBackend.run`` on the fixture CLI of tests/fixtures/harness/claude_jsonl.py, the way the Bridge runs it."""

    def run_turn(self, scenario, hook=None, **changes):
        ctx = self.ctx(options={"transport": "stream-json"}, backend_cmd=[sys.executable, str(CLAUDE_JSONL), scenario],
                       timeout=30, **changes)
        events, seen = [], []

        def emit(kind, data, control):
            events.append((kind, data))
            if hook:
                hook(kind, data, control)

        def watch(process):
            if process is not None:
                self.processes.append(process)
                self.device.tasks.append(process.task)
                seen.append(process)

        with mock.patch.object(dl, "resolve", return_value=self.link), \
                mock.patch.dict(os.environ, {"COVEY_TASK_DIR": str(self.root / "run")}):
            answer = ClaudeBackend().run(RunRequest(ctx, "prompt", callbacks=RunCallbacks(
                event=emit, process=watch, verified_reply=lambda reply: None, session=lambda sid: None),
                inputs=RunInputs(threading.Event(), run_id=RUN)))
        return answer, events, seen

    def test_a_turn_runs_to_its_end_on_the_device(self):
        answer, events, seen = self.run_turn("success")
        self.assertFalse(answer.is_error, answer.text)
        self.assertEqual(answer.text, "Complete answer")
        self.assertEqual(len(seen), 1)
        self.assertIsInstance(seen[0], dl.DeviceProcess)
        self.assertIsNone(seen[0].pid)
        self.assertIsNotNone(seen[0].poll())
        record = dl.read_record(self.root / "run")
        self.assertEqual(record["state"], "exited")
        self.assertEqual(record["device"], "gpu1")
        # The CLI got the device's working directory, not the stand-in.
        argv = json.loads((self.device.task_dir(seen[0].task) / "spec.json").read_text())["spec"]
        self.assertEqual(argv["cwd"], str(self.device.home))
        self.assertNotIn("login_shell", json.dumps(argv))

    def test_a_native_question_is_answered_through_the_ledger(self):
        def hook(kind, data, control):
            if kind == "question":
                self.assertTrue(control.verify_question_request(data["live_question_proof"]))
                control.answer(data["request_id"], {"0": {"answers": ["A", "B"]}})
                with self.assertRaises(ValueError):
                    control.answer(data["request_id"], {"0": {"answers": ["A"]}})

        answer, events, seen = self.run_turn("question", hook)
        self.assertFalse(answer.is_error, answer.text)
        self.assertEqual(answer.text, "Answered correctly")
        self.assertTrue(any(kind == "question_resolved" for kind, _ in events))
        ledger = dl.read_record(self.root / "run")["requests"]
        self.assertEqual({entry["state"] for entry in ledger.values()}, {"answered"})

    def test_a_question_answered_while_the_link_is_down_is_delivered_when_it_is_back(self):
        outage = {}

        def hook(kind, data, control):
            if kind != "question":
                return
            process = seen_process[0]
            self.device.faults.fail(10 ** 6)
            self.device.kill_attaches()
            self.assertTrue(wait_for(lambda: process.offline))
            outage["held"] = control.transport.process.state()
            threading.Timer(1.0, lambda: setattr(self.device.faults, "fail_count", 0)).start()
            control.answer(data["request_id"], {"0": {"answers": ["A", "B"]}})

        seen_process = []
        original = ClaudeProcess.__init__

        def remember(this, *args, **kwargs):
            original(this, *args, **kwargs)
            seen_process.append(this.process)

        with mock.patch.object(ClaudeProcess, "__init__", remember):
            answer, events, seen = self.run_turn("question", hook)
        self.assertEqual(outage["held"], "down")
        self.assertFalse(answer.is_error, (answer.text, answer.outcome.error_code))
        self.assertEqual(answer.text, "Answered correctly")

    def test_stopping_the_turn_stops_the_cli_on_the_device(self):
        cancel = threading.Event()
        ctx = self.ctx(options={"transport": "stream-json"}, backend_cmd=[sys.executable, str(CLAUDE_JSONL), "cancel"],
                       timeout=30)
        seen = []

        def watch(process):
            if process is not None:
                self.processes.append(process)
                self.device.tasks.append(process.task)
                seen.append(process)
                threading.Timer(1.5, cancel.set).start()

        with mock.patch.object(dl, "resolve", return_value=self.link):
            answer = ClaudeBackend().run(RunRequest(ctx, "prompt", callbacks=RunCallbacks(
                process=watch, verified_reply=lambda reply: None, session=lambda sid: None),
                inputs=RunInputs(cancel, run_id=RUN)))
        self.assertTrue(answer.is_error)
        self.assertIsNotNone(seen[0].poll())
        self.assertTrue(wait_for(lambda: (self.device.task_dir(seen[0].task) / "exit.json").exists()))

    def test_a_process_of_claude_on_a_device_has_no_pid_to_sweep(self):
        with mock.patch.object(dl, "resolve", return_value=self.link), \
                mock.patch("covey.backends.claude.process.process_identity") as identity, \
                mock.patch("covey.backends.claude.process.cleanup_group") as sweep:
            process = ClaudeProcess([sys.executable, str(CLAUDE_JSONL), "success"], self.ctx(timeout=20), {})
            self.processes.append(process.process)
            self.device.tasks.append(process.process.task)
            process.policy_stop = True
            process.close()
        identity.assert_not_called()
        sweep.assert_not_called()
        self.assertIsNone(process.identity)


class AppServerOnADeviceTest(DeviceCase):
    def test_a_round_with_an_app_server_on_the_device(self):
        ctx = self.ctx(options={})
        with mock.patch.object(dl, "resolve", return_value=self.link):
            rpc = AppServer([sys.executable, str(APP_SERVER)], ctx.workdir, {}, ctx)
        self.processes.append(rpc.process)
        self.device.tasks.append(rpc.process.task)
        self.assertTrue(rpc.device)
        self.assertEqual(rpc.request("initialize", {})["cwd"], str(self.device.home))
        thread = rpc.request("thread/start", {"cwd": ctx.remote_workdir})["thread"]["id"]
        rpc.thread_id = thread
        rpc.turn_id = rpc.request("turn/start", {"threadId": thread})["turn"]["id"]
        self.assertEqual(rpc.events.get(timeout=10)["method"], "turn/started")
        asked = rpc.events.get(timeout=10)
        self.assertEqual(asked["method"], "item/tool/requestUserInput")
        rpc.user_requests.add(asked["id"])
        self.assertEqual(rpc.steer("补充一点", "msg-1")["turnId"], "turn-1")
        rpc.answer(asked["id"], {"q1": {"answers": ["x"]}})
        done = rpc.events.get(timeout=10)
        self.assertEqual(done["method"], "turn/completed")
        self.assertIn("answered", done["params"]["turn"]["items"][0]["text"])
        self.assertTrue(rpc.close())  # Input ended, the server ended, the device holds nothing of it.
        self.assertEqual(rpc.process.returncode, 0)
        with self.assertRaises(RpcError):
            rpc.send({"id": 1, "method": "x"})

    def test_while_a_question_waits_the_app_server_asks_for_the_longer_patience(self):
        ctx = self.ctx(options={})
        with mock.patch.object(dl, "resolve", return_value=self.link):
            rpc = AppServer([sys.executable, str(APP_SERVER)], ctx.workdir, {}, ctx)
        self.processes.append(rpc.process)
        self.device.tasks.append(rpc.process.task)
        self.assertFalse(rpc.process._hold())
        rpc.user_requests.add(900)
        self.assertTrue(rpc.process._hold())
        rpc.user_requests.clear()
        rpc.pause_for_question = "q"
        self.assertTrue(rpc.process._hold())
        rpc.close()

    def test_a_write_to_a_link_that_is_down_is_an_rpc_error(self):
        ctx = self.ctx(options={})
        with mock.patch.object(dl, "resolve", return_value=self.link):
            rpc = AppServer([sys.executable, str(APP_SERVER)], ctx.workdir, {}, ctx)
        self.processes.append(rpc.process)
        self.device.tasks.append(rpc.process.task)
        self.device.faults.fail(10 ** 6)
        self.device.kill_attaches()
        self.assertTrue(wait_for(lambda: rpc.process.offline))
        with self.assertRaises(RpcError):
            rpc.send({"id": 5, "method": "initialize", "params": {}})
        self.device.faults.fail_count = 0
        rpc.close()

    def test_the_thread_parameters_name_the_devices_paths_only(self):
        ctx = self.ctx(options={})
        local = appserver.thread_params(dataclasses.replace(ctx, device="", remote_workdir=""), None)[0]
        self.assertEqual(local["cwd"], str(ctx.workdir))
        self.assertEqual(local["config"]["mcp_servers.bridge_workspace.command"], sys.executable)
        with mock.patch.object(dl, "resolve", return_value=self.link):
            rpc = AppServer([sys.executable, str(APP_SERVER)], ctx.workdir, {}, ctx)
        self.processes.append(rpc.process)
        self.device.tasks.append(rpc.process.task)
        params, fork = appserver.thread_params(ctx, None, "rules", rpc.process)
        self.assertIsNone(fork)
        config = params["config"]
        task_dir = rpc.process.task_dir
        self.assertEqual(params["cwd"], str(self.device.home))
        self.assertEqual(config["sandbox_workspace_write.writable_roots"], [task_dir])
        self.assertEqual(config["mcp_servers.bridge_workspace.command"], sys.executable)  # The fake device's python.
        args = config["mcp_servers.bridge_workspace.args"]
        reader = Path(args[1])
        self.assertTrue(reader.is_file() and reader.name == "workspace_reader.py", args)
        self.assertEqual(reader.parent.parent, self.device.home / ".covey" / "shim")
        self.assertEqual(args[2:], ["--root", str(self.device.home), "--extra-root", task_dir + "/incoming"])
        text = json.dumps(params)
        for hub_thing in (str(self.state), str(self.root), "covey_workspace_reader", str(REPO), "incoming_media",
                          "handoff"):
            self.assertNotIn(hub_thing, text.replace(str(self.device.home), ""), hub_thing)
        rpc.close()


if __name__ == "__main__":
    unittest.main()


class CheckpointFrameTest(unittest.TestCase):
    """A checkpoint the device read mid-write (empty or cut short) does not replace the hub's good copy: the next
    look brings the whole file (seen as a flaky mirror test under load, 2026-11-15)."""

    def test_an_empty_or_cut_short_checkpoint_keeps_the_hubs_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            hub = Path(tmp) / "hub.json"
            link = object.__new__(dl.DeviceProcess)
            link.alias, link.checkpoint = "box", {"hub_path": str(hub)}
            link._checkpoint({"d": '{"phase": "一"}'})
            self.assertEqual(hub.read_text(), '{"phase": "一"}')
            for torn in ("", '{"phase": "二'):
                link._checkpoint({"d": torn})
                self.assertEqual(hub.read_text(), '{"phase": "一"}', repr(torn))
            link._checkpoint({"d": '{"phase": "二"}'})
            self.assertEqual(hub.read_text(), '{"phase": "二"}')
