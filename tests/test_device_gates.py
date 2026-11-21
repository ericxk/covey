"""The gates of a turn on a device (A-SPEC 4.5, 5.1, 6.2): what a device's topic cannot do is switched off, compaction
never reaches a session file that is not here, the checkpoint is mirrored to the hub, and a whole turn of Claude and of
Codex, run through the worker on a fake device (the real shim under a temporary HOME), touches nothing of the hub that
would answer wrongly without a word (tests/support/device_guard.py).

The positive controls matter as much as the checks: each "never" has a local twin that does it, so a check that cannot
fail is found out.
"""
from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from support import REPO, make_context, make_job, quiet
from support.device_guard import DeviceGuard
from support.fake_backends import CLAUDE_JSONL
from support.fake_device import FakeDevice
from support.fake_lark import MemoryTransport, install_card_transport
from support.world import build_claude_app, harness_app
from support.worker_seams import leave_out_side_work

from covey.app import compact_runner, device_files, idle_compaction, turn_delivery, worker
from covey.backends.claude import compaction as claude_compaction
from covey.backends.claude import runtime as claude_runtime
from covey.backends.claude.bridge import ClaudeBackend
from covey.backends.codex import rpc as codex_rpc
from covey.backends.codex.bridge import CODEX_SPEC, CodexBackend
from covey.backends.codex.sessions import CodexSessionStore
from covey.core import device_link as dl
from covey.core import devices, spaces
from covey.core.harness import (DEVICE_OFF, HarnessCapabilities, RunCallbacks, RunInputs, RunRequest,
                                backend_capabilities)
from covey.core.jobs import BridgeReplyJob, carry_device, find_job_record, job_key
from covey.core.task_processes import TASK_DIR_ENV
from covey.core.workspaces import directory_status, load_config, main as workspaces_main, missing_workspaces
from covey.app.task_card import StreamingReply

APP_SERVER = REPO / "tests" / "support" / "fake_app_server.py"
DEVICE_PYTHON = "/opt/device/bin/python3"  # The device's interpreter as its profile names it: nothing of the hub's.
HUB_ENV = {"LARK_APP_SECRET": "hub-secret-lark", "LARK_BRIDGE_TOKEN": "hub-token-bridge", "FEISHU_APP_ID": "hub-feishu-id",
           "FEISHU_VERIFY_TOKEN": "hub-feishu-verify", "COVEY_HUB_NOTE": "hub-covey-note"}
DOC = {"schema": 1, "control_dir": "/dev/shm/covey-ssh-gates",
       "devices": {"gpu1": {"ssh": {"host": "gpu1.example"}, "harnesses": ["claude", "codex"],
                            "spaces": ["owner_private"]}}}
REMOVED = {"native_fork", "compaction", "turn_export", "redaction", "rewind", "command_stop", "bridge_tools"}


def checkpoint_text(**fields):
    return json.dumps({"phase": "done", "completed": [], "artifacts": [], "external_tasks": [], "next_step": "",
                       "requires_user": False, **fields}, ensure_ascii=False)


class Capabilities(unittest.TestCase):
    def ctx(self, **fields):
        return make_context(Path("/nonexistent-root"), **fields)

    def test_a_device_turns_off_exactly_the_seven_that_need_the_hubs_files_or_its_processes(self):
        self.assertEqual(set(DEVICE_OFF), REMOVED)
        for backend in (ClaudeBackend(), CodexBackend()):
            local = backend_capabilities(backend, self.ctx())
            remote = backend_capabilities(backend, self.ctx(device="gpu1", remote_workdir="/p"))
            for name in REMOVED:
                self.assertFalse(getattr(remote, name), (type(backend).__name__, name))
            # Everything else is what it is on this machine: steering, questions, images, retries.
            self.assertEqual({k: v for k, v in dataclasses.asdict(remote).items() if k not in REMOVED},
                             {k: v for k, v in dataclasses.asdict(local).items() if k not in REMOVED})

    def test_the_local_twin_still_has_them(self):
        claude = backend_capabilities(ClaudeBackend(), self.ctx())
        self.assertTrue(all(getattr(claude, name) for name in REMOVED))
        codex = backend_capabilities(CodexBackend(), self.ctx())
        self.assertTrue(codex.native_fork and codex.turn_export)
        self.assertEqual(backend_capabilities(ClaudeBackend(), self.ctx(device="")), claude)  # "" is this machine.

    def test_a_context_without_the_device_fields_is_local(self):
        self.assertEqual(backend_capabilities(ClaudeBackend(), object()), HarnessCapabilities(
            questions=True, image_bytes=claude_caps().image_bytes, image_frame=claude_caps().image_frame, steering=True,
            native_fork=True, rate_limit_retry=True, incomplete_retry=True, fault_retry=True, steer_reads=True, compaction=True,
            command_stop=True, turn_export=True, redaction=True, rewind=True, bridge_tools=True, read_only=True))


def claude_caps():
    return backend_capabilities(ClaudeBackend(), make_context(Path("/x")))


class Compaction(unittest.TestCase):
    """Nothing compacts on a device: the session file is the device's, the process has no pid."""

    def request(self, ctx, **options):
        return RunRequest(dataclasses.replace(ctx, options={**ctx.options, **options}), "", "sid-1",
                          callbacks=RunCallbacks(), inputs=RunInputs(threading.Event()))

    def test_compact_before_turn_returns_at_once_on_a_device_and_asks_local_ones(self):
        ctx = make_context(Path("/x"), device="gpu1", remote_workdir="/p")
        spawn = mock.Mock()
        with mock.patch.object(claude_compaction, "reason") as reason:
            self.assertFalse(claude_compaction.compact_before_turn(
                self.request(ctx, compact_first=True), {}, spawn, lambda *a: None, lambda: None, mock.Mock()))
            reason.assert_not_called()
        spawn.assert_not_called()
        with mock.patch.object(claude_compaction, "reason", return_value=None) as reason:  # The twin.
            claude_compaction.compact_before_turn(self.request(make_context(Path("/x")), compact_first=True), {}, spawn,
                                                  lambda *a: None, lambda: None, mock.Mock())
            reason.assert_called_once()

    def test_the_backend_does_not_compact_a_device_session_either(self):
        ctx = make_context(Path("/x"), device="gpu1", remote_workdir="/p")
        with mock.patch("covey.backends.claude.runtime.compact_only") as only:
            self.assertFalse(ClaudeBackend().compact_session(self.request(ctx)))
            only.assert_not_called()
            ClaudeBackend().compact_session(self.request(make_context(Path("/x"))))
            only.assert_called_once()

    def test_the_idle_compaction_never_starts_for_a_device_turn(self):
        ctx = make_context(Path("/x"), device="gpu1", remote_workdir="/p")
        job = make_job(topic_id="t1")
        app, gateway = mock.Mock(), mock.Mock()
        app.compaction.request.return_value = {"at": 1}
        gateway.runtime.detached.return_value = True
        launch = mock.Mock()
        with mock.patch.object(idle_compaction.contract, "traits_of", return_value=mock.Mock(own_turn=True)), \
                mock.patch.object(idle_compaction, "context_for_job", return_value=ctx), \
                mock.patch.object(idle_compaction.threading, "Thread") as thread:
            idle_compaction.after_runner(gateway, app, job, ctx, launch)
            thread.assert_not_called()
            self.assertIsNone(idle_compaction.start(gateway, app, job, ctx, launch))
        launch.spawn.assert_not_called()

    def test_the_compaction_runner_leaves_a_device_session_alone_and_clears_its_mark(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            request_path = directory / "request.json"
            mark = directory / idle_compaction.MARK
            mark.write_text(json.dumps({"request_path": str(request_path), "started_at": 1, "topic_id": "t1",
                                        "handoff": {"script_path": "x", "argv": [], "harness": "claude", "job": {}},
                                        "request": {"at": 1}}))
            ctx = make_context(directory, device="gpu1", remote_workdir="/p")
            app = mock.Mock()
            app.harness.context = ctx
            app.sessions.read_session_id.return_value = "sid-1"
            gateway = mock.Mock()
            with mock.patch.object(compact_runner, "UnifiedBridgeApp", return_value=gateway), \
                    mock.patch.object(compact_runner.runner_ipc, "RunnerLink"), \
                    mock.patch.object(compact_runner.runner_spawn, "load_job", return_value=make_job(topic_id="t1")), \
                    mock.patch.object(compact_runner, "context_for_job", return_value=ctx):
                gateway.apps = {"claude": app}
                status = compact_runner.compact_main(directory)
            self.assertEqual(status, idle_compaction.NOTHING)
            app.backend.compact_session.assert_not_called()
            self.assertFalse(mark.exists())


class Checkpoint(unittest.TestCase):
    """The mirror: the prompt names the device's path, the spawn request carries it and the seed."""

    def setUp(self):
        self.device = FakeDevice()
        self.addCleanup(self.cleanup)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ctx = make_context(self.root, device="gpu1", remote_workdir=str(self.device.home))
        profile = devices.device_dir(self.ctx.state_dir, "gpu1")
        profile.mkdir(parents=True)
        (profile / "profile.json").write_text(json.dumps({"home": str(self.device.home)}))
        self.hub_file = self.root / "state" / "checkpoints" / "om_1-1.json"
        self.hub_file.parent.mkdir(parents=True)
        self.hub_file.write_text(checkpoint_text(next_step="seed"))

    def cleanup(self):
        self.assertEqual(self.device.cleanup(), [])

    def test_the_mirror_is_a_stable_absolute_path_on_the_device(self):
        mirror = devices.checkpoint_mirror(self.ctx.state_dir, "gpu1", self.hub_file)
        self.assertEqual(mirror["hub_path"], str(self.hub_file))
        self.assertEqual(mirror["path"], "%s/.covey/checkpoints/%s/om_1-1.json" % (self.device.home,
                                                                                   devices.hub_id(self.ctx.state_dir)))
        self.assertEqual(devices.checkpoint_mirror(self.ctx.state_dir, "gpu1", self.hub_file), mirror)
        self.assertEqual(devices.device_checkpoint_path(self.ctx, self.hub_file), mirror["path"])

    def test_without_a_known_home_the_path_starts_at_the_homes_tilde(self):
        (devices.device_dir(self.ctx.state_dir, "gpu1") / "profile.json").unlink()
        self.assertTrue(devices.device_checkpoint_path(self.ctx, self.hub_file).startswith("~/.covey/checkpoints/"))

    def test_the_context_carries_the_mirror_and_the_seed_only_on_a_device(self):
        ctx = devices.spawn_extras(self.ctx, str(self.hub_file))
        extra = ctx.options["device_spawn"]
        self.assertEqual(extra["checkpoint"], devices.checkpoint_mirror(self.ctx.state_dir, "gpu1", self.hub_file))
        self.assertEqual(extra["files"], [{"path": extra["checkpoint"]["path"], "text": self.hub_file.read_text(),
                                           "mode": 0o600}])
        local = dataclasses.replace(self.ctx, device="", remote_workdir="")
        self.assertIs(devices.spawn_extras(local, str(self.hub_file)), local)
        self.assertIs(devices.spawn_extras(self.ctx, ""), self.ctx)
        self.assertNotIn("device_spawn", self.ctx.options)  # The original is not touched.

    def test_a_checkpoint_the_hub_cannot_read_is_not_seeded_but_still_mirrored(self):
        ctx = devices.spawn_extras(self.ctx, str(self.root / "nowhere.json"))
        self.assertEqual(ctx.options["device_spawn"]["files"], [])
        self.assertTrue(ctx.options["device_spawn"]["checkpoint"]["path"].endswith("/nowhere.json"))

    def test_the_note_names_the_shown_path_and_the_local_note_is_what_it_was(self):
        from covey.core.checkpoint import checkpoint_note
        self.assertEqual(checkpoint_note(self.hub_file), f"本任务检查点文件：{self.hub_file}")
        self.assertEqual(checkpoint_note(self.hub_file, None, shown="/dev/x.json"), "本任务检查点文件：/dev/x.json")
        text = checkpoint_note(self.hub_file, {"a": 1}, shown="/dev/x.json")
        self.assertTrue(text.startswith("本任务检查点文件：/dev/x.json\n上次任务状态"))
        self.assertNotIn(str(self.hub_file), text)

    def test_the_device_seeds_the_file_and_mirrors_a_change_to_the_hub(self):
        ctx = devices.spawn_extras(self.ctx, str(self.hub_file))
        mirror = ctx.options["device_spawn"]["checkpoint"]
        script = ("import json,sys,time\n"
                  "p=sys.argv[1]\nprint(open(p).read(),flush=True)\n"
                  "d=json.load(open(p)); d['next_step']='done on device'; open(p,'w').write(json.dumps(d))\n"
                  "time.sleep(2.5)\n")
        link = dl.Link("gpu1", self.device.transport, mock.Mock(python=sys.executable, login_shell=False, env={}), {},
                       6.0, 30.0, self.ctx.state_dir)
        process = dl.DeviceProcess([sys.executable, "-c", script, mirror["path"]], ctx=ctx, env={}, stdout=-1,
                                   run_dir=self.root / "run", link=link)
        self.device.tasks.append(process.task)
        try:
            first = json.loads(process.stdout.readline())
            self.assertEqual(first["next_step"], "seed")  # The Agent found the seed where the prompt said.
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and "device" not in self.hub_file.read_text():
                time.sleep(0.1)
            self.assertEqual(json.loads(self.hub_file.read_text())["next_step"], "done on device")
        finally:
            process.close()
            process.stdout.close()
        self.assertTrue((self.device.home / ".covey" / "checkpoints" / devices.hub_id(self.ctx.state_dir)
                         / "om_1-1.json").is_file())


class DeviceTurn:
    """A whole turn through the worker on a fake device, watched by DeviceGuard. ``harness`` is the Harness under test."""

    harness = "claude"
    previous = {"checkpoint": {"phase": "earlier", "artifacts": ["out/result.txt", "~/notes/n.md"]}}

    def setUp(self):
        self.device = FakeDevice()
        self.addCleanup(self.finish)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "devices.json").write_text(json.dumps(DOC))
        env = {"COVEY_DEVICES": "1", "COVEY_DEVICES_FILE": str(self.root / "devices.json"),
               "COVEY_DATA_HOME": str(self.root / "data"), TASK_DIR_ENV: str(self.root / "run"), **HUB_ENV}
        self.stack.enter_context(mock.patch.dict(os.environ, env))
        self.stack.enter_context(mock.patch.object(devices, "open_transport", return_value=self.device.transport))
        self.stack.enter_context(mock.patch.object(dl, "BACKOFF", (0.05, 0.1, 0.2)))
        self.state = self.root / "state"
        profile = devices.device_dir(self.state, "gpu1")
        profile.mkdir(parents=True)
        (profile / "profile.json").write_text(json.dumps({"home": str(self.device.home), "python_exe": DEVICE_PYTHON}))
        (self.root / "run").mkdir()
        self.standin = devices.devices_dir(self.state) / "gpu1" / "standin"
        self.watched = []
        self.build()

    def finish(self):
        for process in self.watched:
            with contextlib.suppress(Exception):
                process.close()
        self.assertEqual(self.device.cleanup(), [], "processes of the device were left running")

    # ---- the app and its contexts
    def build(self):
        if self.harness == "claude":
            self.app = build_claude_app(ClaudeBackend())
            options = {"harness_id": "claude", "transport": "stream-json"}
            backend_cmd = [sys.executable, str(CLAUDE_JSONL), "success"]
        else:
            self.app = harness_app(CODEX_SPEC, CodexBackend(), CodexSessionStore(self.root / "codex-sessions"))
            quiet(self.app)
            options = {"harness_id": "codex"}
            backend_cmd = [sys.executable, str(APP_SERVER), "--plain"]
        base = make_context(self.root, options=options, backend_cmd=backend_cmd, timeout=120, model="fixture-model")
        runtime = self.app.runtime
        runtime.owner_open_id = "ou_user"
        runtime.access.spaces = spaces.Settings()
        runtime.access.owner_chat = lambda: "oc_dm"
        self.local = dataclasses.replace(base, topic_id="topic-local", workspace_name="local")
        self.ctx = devices.with_binding(base, devices.bind_workdir(base, "gpu1:%s" % self.device.home),
                                        workspace_name="gpu1-proj", topic_id="topic-dev")
        self.app.cards.streaming = False
        self.app.sessions.record_session_from_worker = mock.Mock()
        self.app.cards.finish_result_controls = mock.Mock()

    def job(self, ctx, number=1, text="please work on the project", **fields):
        job = BridgeReplyJob(job_id=number, user_text=text, chat_id="oc_dm",
                             message_id="om_%s_%d" % (ctx.topic_id, number), timeout=0, topic_id=ctx.topic_id,
                             reply_to_message_id="om_root", model="fixture-model", effort="high",
                             model_settings_snapshot=True, workspace_name=ctx.workspace_name, workdir=ctx.workdir,
                             history_path=ctx.history_path, session_path=ctx.session_path,
                             sessions_dir=ctx.sessions_dir, progress=StreamingReply("result-card", "om-result"), **fields)
        return carry_device(job, ctx)

    def play(self, job, ctx=None):
        """Run ``job`` through ``app.process_reply_job`` under a guard. Kept: ``guard``, ``prompts``, ``requests`` (the
        Codex app server's requests), ``elements`` (what was put on the task card) and the device processes."""
        ctx = ctx or self.ctx
        self.elements, self.prompts, self.requests = [], [], []
        guard = self.guard = DeviceGuard(self.standin, device_root=self.device.home)
        tx = MemoryTransport()
        stack = contextlib.ExitStack()
        with stack:
            stack.enter_context(guard)
            leave_out_side_work(stack, diff=False)
            guard.watch(worker, "capture_job_diff_snapshot")
            guard.watch(turn_delivery, "finalize_job_diff")
            install_card_transport(stack, tx)
            self.app.messenger.send_message = tx.send_text
            self.app.cards.set_task_element = lambda c, j, element, **kw: self.elements.append(element) or True
            backend, original_prompt = self.app.backend, self.app.runs._prompt
            real_run, real_request = backend.run, codex_rpc.AppServer.request

            def run(request):
                inner = request.callbacks.process

                def seen(process):
                    if process is not None and getattr(process, "task", None):
                        self.watched.append(process)
                        self.device.tasks.append(process.task)
                    return inner(process)

                return real_run(dataclasses.replace(request, callbacks=dataclasses.replace(request.callbacks, process=seen)))

            def prompt(live, job, session_id):
                result = original_prompt(live, job, session_id)
                self.prompts.append(result[0])
                return result

            def request(this, method, params, *args, **kwargs):
                self.requests.append((method, params))
                return real_request(this, method, params, *args, **kwargs)

            stack.enter_context(mock.patch.object(backend, "run", run))
            stack.enter_context(mock.patch.object(self.app.runs, "_prompt", prompt))
            stack.enter_context(mock.patch.object(codex_rpc.AppServer, "request", request))
            self.app.process_reply_job(job, ctx)
        return find_job_record(ctx.state_dir, job_key(job.message_id, job.job_id))

    def spec(self, process):
        return json.loads((self.device.task_dir(process.task) / "spec.json").read_text())["spec"]

    def element_ids(self, value=None):
        """Every ``element_id`` in the elements put on the task card (nested ones too)."""
        found, todo = set(), list(self.elements if value is None else [value])
        while todo:
            item = todo.pop()
            if isinstance(item, dict):
                if item.get("element_id"):
                    found.add(item["element_id"])
                todo.extend(item.values())
            elif isinstance(item, list):
                todo.extend(item)
        return found

    def ran_on_the_device(self, record):
        self.assertEqual(record["status"], "done", record.get("status_message"))
        self.assertEqual(len(self.watched), 1)
        process = self.watched[0]
        self.assertIsInstance(process, dl.DeviceProcess)
        self.assertIsNone(process.pid)  # Nothing here has a pid to look at, signal or sweep.
        self.assertEqual(self.spec(process)["cwd"], str(self.device.home))
        return process

    # ---- the whole turn
    def test_a_whole_turn_leaves_the_hubs_stand_in_alone_and_asks_the_device_instead(self):
        job = self.job(self.ctx, recovery_context=self.previous)
        record = self.play(job)
        self.ran_on_the_device(record)
        self.guard.assert_untouched(self)
        self.assertEqual(self.guard.git_runs(), [], "a turn on a device starts no git here")
        self.assertEqual(self.guard.calls, {"capture_job_diff_snapshot": 0, "finalize_job_diff": 0})
        self.assertFalse(record.get("diff_json_path"), "no diff snapshot of a directory the hub cannot see")
        stats = [fields for command, fields in self.device.transport.log if command == "stat"]
        recovery = [fields for fields in stats if fields.get("paths") == ["out/result.txt", "~/notes/n.md"]]
        self.assertEqual(len(recovery), 1, "audit_recovery looks at the artifacts through the device")
        self.assertEqual(recovery[0]["base"], str(self.device.home))
        self.assertEqual(recovery[0]["root"], str(self.device.home))
        self.assertEqual([command for command, _ in self.device.transport.log].count("spawn"), 1)
        # Every process the hub started for the turn is the device's program (here through ``sh -c``, over ssh ``ssh``).
        self.assertTrue(self.guard.spawned)
        for item in self.guard.spawned:
            self.assertEqual(item["argv"][:2], ["sh", "-c"], item)
            self.assertIn("/.covey/shim/", item["argv"][2])
        self.assertTrue(record["recovery_audit"], "the audit is on the record")
        self.assertNotIn("device_not_inspected", json.dumps(record["recovery_audit"]))

    def test_the_spawn_request_has_nothing_of_the_hub(self):
        record = self.play(self.job(self.ctx))
        spec = self.spec(self.ran_on_the_device(record))
        self.assertEqual([name for name in spec["env"] if name.startswith(("LARK_", "FEISHU_", "COVEY_"))], [])
        text = json.dumps(spec)
        for name, value in HUB_ENV.items():
            self.assertNotIn(value, text, name)
        for hub in (str(self.state), str(self.root), str(self.standin)):
            self.assertNotIn(hub, text.replace(str(CLAUDE_JSONL), "").replace(str(APP_SERVER), ""), hub)
        self.assertEqual(spec["cwd"], str(self.device.home))

    def test_the_prompt_names_the_devices_checkpoint_and_nothing_of_the_hub(self):
        job = self.job(self.ctx, recovery_context=self.previous)
        self.ran_on_the_device(self.play(job))
        mirror = devices.checkpoint_mirror(self.state, "gpu1", job.checkpoint_path)
        prompt = self.prompts[0]
        self.assertIn("本任务检查点文件：" + mirror["path"], prompt)
        self.assertNotIn(job.checkpoint_path, prompt)
        self.assertIn("（在设备 gpu1 上", prompt)
        history = knowledge_history()
        for hub in (str(self.state), str(self.root), "incoming_media", "history.py", "standin", history):
            if hub:
                self.assertNotIn(hub, prompt, hub)

    # ---- attachments (A-SPEC 5.2): a message's files are put on the device, the prompt names the copies
    def attachments(self):
        """A picture and a PDF saved in the topic's incoming_media as a received message lists them: (picture, pdf, text)."""
        media = self.ctx.history_path.parent / "incoming_media"
        media.mkdir(parents=True, exist_ok=True)
        pic, doc = media / "shot.png", media / "spec.pdf"
        pic.write_bytes(b"\x89PNG\r\n\x1a\n device pixels")
        doc.write_bytes(b"%PDF-1.4 device document")
        text = ("please look at these\n收到的附件（已下载到本地）：\n- 图片 1: `%s`\n- 文件 1（spec.pdf）: `%s`\n"
                "请在需要理解图片或文件内容时读取上述本地路径。" % (pic, doc))
        return pic, doc, text

    def play_with_attachments(self, text):
        puts, seen = [], []
        real_put, self.read_images_seen = self.device.transport.put, seen
        real_read = claude_runtime.read_images

        def put(path, *args, **kwargs):
            puts.append(path)
            return real_put(path, *args, **kwargs)

        def read(attachments, **kwargs):
            seen.append([dict(item) for item in attachments])
            return real_read(attachments, **kwargs)

        self.notes = []
        real_note = self.app.runs._bridge_note
        with mock.patch.object(self.device.transport, "put", put), \
                mock.patch.object(claude_runtime, "read_images", read), \
                mock.patch.object(self.app.runs, "_bridge_note",
                                  lambda ctx, job, key, line: self.notes.append((key, line)) or real_note(ctx, job, key, line)):
            record = self.play(self.job(self.ctx, text=text))
        self.puts = puts
        return record

    def staged_dir(self, record):
        return "%s/.covey/incoming/%s/%s" % (self.device.home, devices.hub_id(self.state), record["key"])

    def test_the_attachments_are_put_on_the_device_and_the_prompt_never_names_the_hubs_copy(self):
        pic, doc, text = self.attachments()
        record = self.play_with_attachments(text)
        self.ran_on_the_device(record)
        directory = self.staged_dir(record)
        prompt = self.prompts[0]
        self.assertIn("`%s/shot.png`" % directory, prompt)
        self.assertIn("`%s/spec.pdf`" % directory, prompt)
        for hub in ("incoming_media", str(self.root), str(pic), str(doc)):
            self.assertNotIn(hub, prompt, hub)
        self.assertEqual((Path(directory) / "spec.pdf").read_bytes(), doc.read_bytes())
        self.assertEqual((Path(directory) / "shot.png").read_bytes(), pic.read_bytes())
        self.assertEqual(sorted(self.puts), sorted(["%s/shot.png" % directory, "%s/spec.pdf" % directory]),
                         "the files went through the shim")
        # The hub used no file of the device itself, and none of its stand-in; only the shim did (guard.device_root).
        self.guard.assert_untouched(self)
        self.assertEqual(self.notes, [], "nothing was left out: no line on the card")

    def test_a_file_over_the_limit_is_left_out_with_a_line_on_the_card_and_never_a_hub_path_in_the_prompt(self):
        pic, doc, text = self.attachments()
        with mock.patch.object(device_files, "MAX_FILE", pic.stat().st_size):  # The picture fits, the PDF does not.
            record = self.play_with_attachments(text)
        self.ran_on_the_device(record)
        directory = self.staged_dir(record)
        prompt = self.prompts[0]
        self.assertIn("`%s/shot.png`" % directory, prompt)
        self.assertNotIn("spec.pdf`", prompt.split("这些附件没有传到设备")[0])
        for hub in ("incoming_media", str(self.root)):
            self.assertNotIn(hub, prompt, hub)
        self.assertTrue((Path(directory) / "shot.png").is_file())
        self.assertFalse((Path(directory) / "spec.pdf").exists())
        self.assertEqual([key for key, _ in self.notes], ["bridge-device-files"])
        self.assertIn("1 个附件没有传到设备 gpu1：spec.pdf", self.notes[0][1])
        self.guard.assert_untouched(self)

    def test_the_image_the_hub_reads_for_claude_has_its_device_path_for_codex(self):
        pic, doc, text = self.attachments()
        record = self.play_with_attachments(text)
        self.ran_on_the_device(record)
        directory = self.staged_dir(record)
        if self.harness == "claude":  # Claude's image is the hub's file as base64; the device path rides along unused.
            self.assertEqual([[(item["path"], item["device_path"]) for item in items] for items in self.read_images_seen],
                             [[(str(pic), directory + "/shot.png")]])
        else:
            starts = [params for method, params in self.requests if method == "turn/start"]
            self.assertEqual([item for item in starts[0]["input"] if item["type"] == "localImage"],
                             [{"type": "localImage", "path": directory + "/shot.png"}])
            self.assertEqual(self.read_images_seen, [])
            config = [params for method, params in self.requests if method == "thread/start"][0]["config"]
            args = config["mcp_servers.bridge_workspace.args"]
            self.assertIn(directory, [args[i + 1] for i, arg in enumerate(args[:-1]) if arg == "--extra-root"])

    # ---- a message into the running turn (A6's open leak): the supplement and an answer's riders name the device's copies
    class Control:
        """What a running task's backend control is to the Bridge: it keeps what it is sent."""
        supports_steer, pause_for_question, awaiting_answer, remote = True, "", False, False

        def __init__(self):
            self.steered = []

        def steer(self, text, event_id):
            self.steered.append(text)

    def running(self, ctx=None, number=3):
        ctx = ctx or self.ctx
        job = self.job(ctx, number=number)
        job.execution_started = True
        job.backend_control = self.Control()
        self.app.cards.set_task_element = lambda *a, **k: True
        self.said = []
        self.app.messenger.reply = lambda ctx, chat, text, key, **kw: self.said.append(text) or True
        return job

    def test_a_supplement_with_attachments_puts_the_files_on_the_device_and_names_the_copies(self):
        pic, doc, text = self.attachments()
        job = self.running()
        with DeviceGuard(self.standin, device_root=self.device.home) as guard:
            self.app.steering.deliver_steer(self.ctx, job, "also " + text, "mid-1")
        sent = job.backend_control.steered
        self.assertEqual(len(sent), 1)
        directory = "%s/.covey/incoming/%s/%s" % (self.device.home, devices.hub_id(self.state), job_key(job.message_id, job.job_id))
        self.assertIn("`%s/shot.png`" % directory, sent[0])
        self.assertIn("`%s/spec.pdf`" % directory, sent[0])
        for hub in ("incoming_media", str(self.root), str(pic), str(doc)):
            self.assertNotIn(hub, sent[0], hub)
        self.assertEqual((Path(directory) / "spec.pdf").read_bytes(), doc.read_bytes())
        guard.assert_untouched(self)
        self.assertEqual(self.said, [], "nothing was left out: no line for the user")

    def test_a_supplements_file_over_the_limit_is_left_out_with_a_line_and_never_a_hub_path(self):
        pic, doc, text = self.attachments()
        job = self.running()
        with mock.patch.object(device_files, "MAX_FILE", pic.stat().st_size):
            self.app.steering.deliver_steer(self.ctx, job, text, "mid-2")
        sent = job.backend_control.steered[0]
        self.assertIn("shot.png`", sent)
        self.assertNotIn("incoming_media", sent)
        self.assertIn("这些附件没有传到设备 gpu1", sent)
        self.assertEqual(len(self.said), 1)
        self.assertIn("1 个附件没有传到设备 gpu1：spec.pdf", self.said[0])

    def test_a_supplement_the_task_cannot_take_yet_is_held_with_the_device_paths(self):
        pic, doc, text = self.attachments()
        job = self.running()
        job.backend_control = None  # Starting: the backend has no control yet.
        try:
            self.app.steering.deliver_steer(self.ctx, job, text, "mid-3")
            self.assertEqual(list(job.held_steers), ["mid-3"])
            held = job.held_steers["mid-3"]
            self.assertIn("/.covey/incoming/", held)
            self.assertNotIn("incoming_media", held)
        finally:
            job.held_steers.clear()

    def test_a_local_task_gets_its_supplement_as_written_and_nothing_is_put_anywhere(self):
        pic, doc, text = self.attachments()
        (self.root / "work").mkdir(exist_ok=True)
        local = dataclasses.replace(self.local, workdir=self.root / "work", history_path=self.ctx.history_path)
        job = self.running(local)
        puts = []
        with mock.patch.object(self.device.transport, "put", lambda *a, **k: puts.append(a)):
            self.app.steering.deliver_steer(local, job, text, "mid-4")
        self.assertEqual(job.backend_control.steered, [text])
        self.assertEqual(puts, [])

    def test_the_riders_of_a_native_answer_are_put_on_the_device_too(self):
        from covey.app.answers import answer_message
        pic, doc, text = self.attachments()
        files = text.split("\n", 1)[1]
        record = {"questions": [{"id": "q1", "question": "Which?"}], "attachments": files, "supplement": "see " + files}
        answers = {"q1": {"answers": ["A"]}}
        notes = [{"text": "another note\n" + files, "display": "another note"}]
        shown, said, delivered = answer_message(record, answers, "", notes)
        self.assertIn("incoming_media", json.dumps(delivered))
        job = self.running()
        sent, staged = self.app.questions._device_message(self.ctx, "oc_dm", job, "tok", record, said, delivered)
        self.assertNotIn("incoming_media", sent + json.dumps(staged))
        self.assertIn("/.covey/incoming/", json.dumps(staged))
        self.assertEqual(staged["q1"]["answers"][0], "A")
        self.assertEqual(self.said, [])

    def test_the_seed_and_the_mirror_go_into_the_spawn_request(self):
        job = self.job(self.ctx)
        seen = []
        real = devices.spawn_extras
        self.stack.enter_context(mock.patch.object(devices, "spawn_extras",
                                                   lambda ctx, path: seen.append(real(ctx, path)) or seen[-1]))
        self.ran_on_the_device(self.play(job))
        extra = seen[0].options["device_spawn"]
        mirror = devices.checkpoint_mirror(self.state, "gpu1", job.checkpoint_path)
        self.assertEqual(extra["checkpoint"], mirror)
        self.assertEqual(json.loads(extra["files"][0]["text"])["phase"], worker.SEED_PHASE)
        self.assertEqual(self.spec(self.watched[0])["checkpoint"], {"path": mirror["path"], "poll": 1.0})
        self.assertEqual(self.spec(self.watched[0])["files"], [{"path": mirror["path"]}])  # The shim stores no seed text.
        self.assertTrue(Path(mirror["path"]).is_file(), "the device has the seed where the prompt says")

    def test_the_buttons_that_need_the_hubs_files_are_not_on_the_card(self):
        self.ran_on_the_device(self.play(self.job(self.ctx)))
        self.assertFalse(self.element_ids() & {"rewind_action", "export_action"}, self.element_ids())
        self.assertIn("switch_action", self.element_ids())  # What a device topic keeps: 换配置.

    def test_the_local_twin_has_the_buttons_so_the_check_can_fail(self):
        (self.root / "work").mkdir()
        local = dataclasses.replace(self.local, workdir=self.root / "work")
        record = self.play(self.job(local), local)
        self.assertEqual(record["status"], "done", record.get("status_message"))
        self.assertEqual(self.watched, [])
        self.assertIn("export_action", self.element_ids())
        if self.harness == "claude":
            self.assertIn("rewind_action", self.element_ids())

    def test_redaction_is_refused_on_a_device_not_sent_unredacted(self):
        with mock.patch("covey.app.runs.topic_redaction", return_value=True):
            refused = self.app.runs._refusal(self.ctx, None, self.job(self.ctx))
            self.assertEqual(refused.outcome.error_code, "redaction_failed")
            self.assertIn("设备 gpu1", refused.failure_detail)
            local = self.app.runs._refusal(dataclasses.replace(self.local, workdir=self.root), None, self.job(self.local))
            if self.harness == "claude":  # Its twin on this machine runs through the term relay.
                self.assertIsNone(local)
            else:
                self.assertNotIn("设备", local.failure_detail)

    def test_directory_status_and_prune_leave_the_device_workspace_alone(self):
        value = "gpu1:%s" % self.device.home
        local = self.root / "work"
        local.mkdir()
        data = load_config(self.state, self.root)
        data["workspaces"].update({"local": str(local), "gpu1-proj": value})
        data["default"] = "local"
        (self.state / "workspace-config.json").write_text(json.dumps(data))
        with DeviceGuard(self.standin, device_root=self.device.home) as guard:
            self.assertEqual(directory_status([value]), {value: None})
            self.assertEqual(missing_workspaces(load_config(self.state, self.root)), [])
            with mock.patch.object(sys, "argv", ["covey_workspaces", "--state-dir", str(self.state), "prune"]), \
                    contextlib.redirect_stdout(io.StringIO()):
                workspaces_main()
        guard.assert_untouched(self)
        self.assertEqual(load_config(self.state, self.root)["workspaces"]["gpu1-proj"], value)


def knowledge_history():
    """The hub's history command text, as the knowledge block would give it ('' when there is none)."""
    from covey.core import knowledge
    try:
        return knowledge.history_cmd() or ""
    except Exception:  # noqa: BLE001 - only a hub path to look for in the prompt
        return ""


class NotLookedForOnTheHub(unittest.TestCase):
    """What the device's Agent lists in its checkpoint are paths on the device: a hub file that happens to have the same
    path must neither be offered as an artifact nor sent as an image. (They are looked at through the shim:
    tests/test_device_files.py.)"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.checkpoint = self.root / "cp.json"
        self.hub_file = self.root / "plot.png"
        self.hub_file.write_bytes(b"\x89PNG hub file")
        self.checkpoint.write_text(json.dumps({"artifacts": [str(self.hub_file), "rel.txt"],
                                               "show_images": [str(self.hub_file)]}))

    def record(self, **fields):
        return {"checkpoint_path": str(self.checkpoint), "workdir": str(self.root), "result_file_path": str(self.root / "r.md"),
                **fields}

    def test_a_device_records_artifacts_are_listed_and_judged_as_the_devices_not_resolved_on_the_hub(self):
        from covey.app import artifact_policy as artifacts
        from covey.core import checkpoint as paths
        record = self.record(device="gpu1", remote_workdir="/p")
        self.assertEqual(paths.artifact_paths(record), [str(self.hub_file), "rel.txt", str(self.root / "r.md")])
        self.assertEqual(paths.artifact_paths(self.record()), paths.artifact_paths(record))
        with DeviceGuard(self.root / "standin") as guard:
            self.assertEqual(artifacts.drive_paths(record), [str(self.hub_file), "rel.txt", str(self.root / "r.md")])
        self.assertNotIn(str(self.hub_file), guard.stats + guard.opened)

    def test_a_device_turn_sends_images_like_a_local_one_looking_for_them_on_the_device(self):
        ctx = make_context(self.root)
        job = make_job(checkpoint_path=str(self.checkpoint))
        cards = mock.Mock()
        kwargs = dict(cards=cards, runtime=mock.Mock(), record_job_state=mock.Mock(), send_text=mock.Mock(), prefix="p")
        self.assertIsNotNone(worker._task_monitor(ctx, job, **kwargs).images)
        self.assertIsNotNone(worker._task_monitor(dataclasses.replace(ctx, device="gpu1", remote_workdir="/p"), job,
                                                  **kwargs).images)


class ClaudeTurn(DeviceTurn, unittest.TestCase):
    harness = "claude"


class CodexTurn(DeviceTurn, unittest.TestCase):
    harness = "codex"

    def test_the_thread_parameters_have_the_devices_paths_only(self):
        job = self.job(self.ctx)
        process = self.ran_on_the_device(self.play(job))
        starts = [params for method, params in self.requests if method in ("thread/start", "thread/resume")]
        self.assertEqual(len(starts), 1)
        text = json.dumps(starts[0])
        for hub in (str(self.state), str(self.root), sys.executable, str(REPO), "incoming_media", "handoff", "standin"):
            self.assertNotIn(hub, text.replace(str(self.device.home), ""), hub)
        config = starts[0]["config"]
        self.assertEqual(starts[0]["cwd"], str(self.device.home))
        mirror = devices.checkpoint_mirror(self.state, "gpu1", job.checkpoint_path)
        roots = config["sandbox_workspace_write.writable_roots"]
        self.assertEqual(roots, [str(process.task_dir), mirror["path"].rpartition("/")[0]])
        self.assertEqual(config["mcp_servers.bridge_workspace.command"], DEVICE_PYTHON)


class GuardTest(unittest.TestCase):
    """The guard sees what it is for: each use of a path it forbids is recorded."""

    def test_it_records_an_open_a_stat_a_listing_and_a_git_in_the_standin(self):
        with tempfile.TemporaryDirectory() as tmp:
            standin = Path(tmp) / "standin"
            (standin / "abc").mkdir(parents=True)
            (standin / "abc" / "f").write_text("x")
            with DeviceGuard(standin) as guard:
                (standin / "abc" / "f").read_text()
                (standin / "abc").is_dir()
                os.listdir(standin / "abc")
                subprocess.run(["git", "status"], cwd=standin / "abc", capture_output=True)
                Path(standin / "abc").resolve()
            touched = guard.standin_touched()
            self.assertEqual(sorted(touched), ["list", "open", "stat", "subprocess"])
            self.assertEqual(len(guard.git_in_standin()), 1)
            self.assertEqual(guard.git_runs(), ["git status"])
            with self.assertRaises(AssertionError):
                guard.assert_untouched(self)

    def test_a_resolve_alone_is_not_a_use_and_a_guard_that_is_closed_records_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            standin = Path(tmp) / "standin" / "key"
            with DeviceGuard(standin.parent) as guard:
                standin.resolve()
                self.assertTrue(guard.resolved)
            self.assertEqual(guard.standin_touched(), {})
            before = len(guard.opened)
            (Path(tmp) / "after").write_text("x")
            self.assertEqual(len(guard.opened), before)

    def test_the_devices_own_files_are_off_limits_to_the_hub(self):
        with tempfile.TemporaryDirectory() as tmp:
            with DeviceGuard(Path(tmp) / "standin", device_root=tmp) as guard:
                (Path(tmp) / "home-file").write_text("x")
            self.assertIn("open", guard.device_files_touched())

    def test_it_counts_calls_and_lets_them_through(self):
        class Box:
            @staticmethod
            def thing(value):
                return value + 1

        with DeviceGuard("/nowhere") as guard:
            guard.watch(Box, "thing")
            self.assertEqual(Box.thing(1), 2)
        self.assertEqual(guard.calls, {"thing": 1})


if __name__ == "__main__":
    unittest.main()
