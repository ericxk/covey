"""A topic's workspace on a device (A-SPEC §4.5): the value ``gpu1:/home/me/proj`` decoded in one place, kept in
topic.json and the job record as text, listed and checked without a hub path, allowed by the chat's space, refused by name
when the device cannot be used, and a child environment that holds nothing of the hub.

Off (no COVEY_DEVICES, or no device listed) every place here behaves as before; the second half of each class says so.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support import make_context, make_job
from support.fake_device import FakeDevice
from support.world import TopicFixture

from covey.app import knowledge_turn
from covey.app.access import Access
from covey.app.prompts import workspace_block
from covey.app.runner_spawn import dump_job, load_job
from covey.app.turn_delivery import outgoing_markdown
from covey.core import devices, process_env, spaces
from covey.core.checkpoint import audit_recovery
from covey.core.jobs import (carry_device, context_for_job, context_for_record, job_key, record_bridge_job_state,
                             find_job_record)
from covey.core.topics import context_for_topic, read_topic
from covey.core.workspaces import (add_workspace, directory_status, load_config, missing_workspaces, resolve_binding,
                                   resolve_selection, topic_context, visible_workspaces)

DOC = {"schema": 1, "control_dir": "/dev/shm/covey-ssh-ctx",
       "devices": {"gpu1": {"ssh": {"host": "gpu1.example"}, "harnesses": ["claude", "codex"],
                            "env": {"HTTPS_PROXY": "http://proxy.example:3128"}, "spaces": ["owner_private"]},
                   "gpu2": {"ssh": {"host": "gpu2.example"}, "harnesses": ["codex"],
                            "spaces": ["owner_private", "personal_groups", "team"]}}}


class DeviceCase(unittest.TestCase):
    """devices.json in a temporary directory and COVEY_DEVICES on (in os.environ, which every reader looks at)."""

    on = True

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "devices.json").write_text(json.dumps(DOC))
        env = {"COVEY_DEVICES_FILE": str(self.root / "devices.json"), "COVEY_DATA_HOME": str(self.root / "data")}
        if self.on:
            env["COVEY_DEVICES"] = "1"
        patch = mock.patch.dict(os.environ, env)
        patch.start()
        self.addCleanup(patch.stop)
        if not self.on:
            os.environ.pop("COVEY_DEVICES", None)
            os.environ.pop("LARK_BRIDGE_DEVICES", None)
        self.ctx = make_context(self.root, options={"harness_id": "claude"})
        self.state = self.ctx.state_dir


def configured(case, value="gpu1:/home/me/proj", name="gpu1-proj"):
    """The workspace config with ``name`` -> ``value`` as the default (the topic starts there)."""
    config = load_config(case.state, case.root)
    config["workspaces"][name] = value
    config["default"] = name
    (case.state / "workspace-config.json").write_text(json.dumps(config))


class DecodeTest(DeviceCase):
    def test_a_new_topic_in_a_device_workspace_stores_the_value_and_gets_the_stand_in(self):
        configured(self, "gpu1:/home//me/./proj/")  # Not canonical in the file: the topic gets the canonical value.
        ctx = topic_context(self.ctx, {"chat_id": "oc_c", "message_id": "om_1"}, lambda _: self.root)
        self.assertEqual((ctx.device, ctx.remote_workdir), ("gpu1", "/home/me/proj"))
        self.assertEqual(ctx.workdir.parent, self.state / "devices" / "gpu1" / "standin")
        self.assertFalse(ctx.workdir.exists())
        stored = json.loads((ctx.history_path.parent / "topic.json").read_text())
        self.assertEqual(stored["workdir"], "gpu1:/home/me/proj")  # The value, never the stand-in.

    def test_the_stored_value_comes_back_as_the_same_context(self):
        configured(self, "gpu1:~/work")
        first = topic_context(self.ctx, {"chat_id": "oc_c", "message_id": "om_1"}, lambda _: self.root)
        again = context_for_topic(self.ctx, {"chat_id": "oc_c", "message_id": "om_1"}, lambda _: self.root)
        self.assertEqual((again.device, again.remote_workdir, again.workdir),
                         (first.device, "~/work", first.workdir))  # ``~`` stays the device's, as text.

    def test_a_local_binding_clears_the_device_a_default_proposed(self):
        proposed = devices.with_binding(self.ctx, devices.bind_workdir(self.ctx, "gpu1:/a"), workspace_name="w")
        self.assertEqual((proposed.device, proposed.remote_workdir), ("gpu1", "/a"))
        local = devices.with_binding(proposed, devices.Binding(self.root))
        self.assertEqual((local.device, local.remote_workdir, local.workdir), ("", "", self.root))

    def test_a_device_that_devices_json_no_longer_lists_still_decodes_so_the_turn_can_be_refused_by_name(self):
        binding = devices.bind_workdir(self.ctx, "gpu9:/x", strict=False)
        self.assertEqual((binding.device, binding.remote_workdir), ("gpu9", "/x"))
        with self.assertRaises(devices.UnknownDevice):
            devices.bind_workdir(self.ctx, "gpu9:/x")

    def test_stored_value_is_the_one_spelling_for_a_context_a_job_and_a_record(self):
        ctx = devices.with_binding(self.ctx, devices.bind_workdir(self.ctx, "gpu1:/a/b"))
        job = carry_device(make_job(workdir=ctx.workdir), ctx)
        for source in (ctx, job, {"device": "gpu1", "remote_workdir": "/a/b", "workdir": "/stand"}):
            self.assertEqual(devices.stored_value(source), "gpu1:/a/b")
        self.assertEqual(devices.stored_value(self.ctx), str(self.ctx.workdir))


class LegacyDeviceTopicTest(DeviceCase):
    def test_a_topic_on_a_device_from_before_d2y4t_still_continues_there(self):
        """主人 D2Y4T retired the entries that start a topic on a device, not what runs one there: a topic bound to a device
        before keeps its device on its next message, the default workspace back on this machine or not."""
        configured(self, "gpu1:/home/me/proj")
        first = topic_context(self.ctx, {"chat_id": "oc_c", "message_id": "om_1"}, lambda _: self.root)
        self.assertEqual((first.device, first.remote_workdir), ("gpu1", "/home/me/proj"))
        config = load_config(self.state, self.root)
        config["default"] = next(name for name, value in config["workspaces"].items() if not value.startswith("gpu1:"))
        (self.state / "workspace-config.json").write_text(json.dumps(config))
        again = context_for_topic(self.ctx, {"chat_id": "oc_c", "message_id": "om_1"}, lambda _: self.root)
        self.assertEqual((again.device, again.remote_workdir, again.workdir), ("gpu1", "/home/me/proj", first.workdir))
        self.assertEqual(read_topic(again)["workdir"], "gpu1:/home/me/proj")


class OffDecodeTest(DeviceCase):
    on = False

    def test_off_a_device_looking_value_is_an_ordinary_relative_path(self):
        configured(self, "gpu1:/home/me/proj")
        topic = topic_context(self.ctx, {"chat_id": "oc_c", "message_id": "om_1"}, lambda _: self.root)
        self.assertEqual((topic.device, topic.remote_workdir), ("", ""))
        self.assertEqual(topic.workdir, Path("gpu1:/home/me/proj").expanduser().resolve())
        self.assertEqual(read_topic(topic)["workdir"], str(topic.workdir))

    def test_off_nothing_is_added_to_a_local_job_or_its_record(self):
        job = make_job(workdir=self.root, history_path=self.root / "h", session_path=self.root / "s",
                       sessions_dir=self.root / "d")
        self.assertEqual(carry_device(job, self.ctx), job)
        dumped = dump_job(job)
        self.assertNotIn('"device"', json.dumps(dumped))
        self.assertNotIn('"remote_workdir"', json.dumps(dumped))
        record_bridge_job_state(state_dir=self.state, backend="claude", job=job, status="running")
        record = find_job_record(self.state, job_key(job.message_id, job.job_id))
        self.assertNotIn("device", record)
        self.assertNotIn("remote_workdir", record)


class JobRecordTest(DeviceCase):
    def setUp(self):
        super().setUp()
        self.dctx = devices.with_binding(self.ctx, devices.bind_workdir(self.ctx, "gpu1:~/proj"), workspace_name="gpu1-proj")
        self.job = carry_device(make_job(workdir=self.dctx.workdir, history_path=self.root / "h",
                                         session_path=self.root / "s", sessions_dir=self.root / "d"), self.dctx)

    def test_the_handoff_carries_the_device_and_keeps_the_directory_text(self):
        data = json.loads(json.dumps(dump_job(self.job)))
        self.assertEqual(data["extras"]["device"], "gpu1")
        self.assertEqual(data["extras"]["remote_workdir"], "~/proj")  # Not a Path field: never resolved or normalised.
        back = load_job(data)
        self.assertEqual((back.device, back.remote_workdir, back.workdir), ("gpu1", "~/proj", self.dctx.workdir))
        self.assertEqual(Path(data["fields"]["workdir"]), self.dctx.workdir)  # The stand-in an old release runs in: absent.

    def test_the_record_has_the_device_only_when_set_and_gives_the_context_back(self):
        record_bridge_job_state(state_dir=self.state, backend="claude", job=self.job, status="running")
        record = find_job_record(self.state, job_key(self.job.message_id, self.job.job_id))
        self.assertEqual((record["device"], record["remote_workdir"]), ("gpu1", "~/proj"))
        record.update(topic_id="t", reply_to_message_id="r", history_path=str(self.root / "h"),
                      session_path=str(self.root / "s"))
        back = context_for_record(self.ctx, record)
        self.assertEqual((back.device, back.remote_workdir, back.workdir), ("gpu1", "~/proj", self.dctx.workdir))
        local = dict(record)
        local.pop("device"), local.pop("remote_workdir")
        self.assertEqual(context_for_record(self.dctx, local).device, "")  # A record without them is a local one.

    def test_context_for_job_follows_the_job_and_only_a_job_with_a_directory(self):
        self.job.model_settings_snapshot = True
        ctx = context_for_job(self.ctx, self.job)
        self.assertEqual((ctx.device, ctx.remote_workdir), ("gpu1", "~/proj"))
        local = make_job(workdir=self.root)
        self.assertEqual(context_for_job(self.ctx, local).device, "")

    def test_the_stand_in_keys_a_session_by_device_and_directory(self):
        a = devices.bind_workdir(self.ctx, "gpu1:/p")
        self.assertEqual(a.workdir, devices.bind_workdir(self.ctx, "gpu1:/p/").workdir)
        self.assertNotEqual(a.workdir, devices.bind_workdir(self.ctx, "gpu2:/p").workdir)
        self.assertNotEqual(a.workdir, devices.bind_workdir(self.ctx, "gpu1:/q").workdir)
        # What core/session_guard.py and core/saved_results.py key on: resolve() of a path that does not exist.
        self.assertEqual(str(Path(a.workdir).resolve()), str(a.workdir))


class WorkspacesTest(DeviceCase):
    def test_a_typed_device_value_is_checked_on_the_device_then_listed(self):
        with mock.patch.object(devices, "check_dir", return_value=("ok", "")) as check:
            name, binding = resolve_binding(self.state, self.root, "gpu1:/home/me/proj")
        self.assertEqual((name, binding.device, binding.value), ("gpu1-proj", "gpu1", "gpu1:/home/me/proj"))
        check.assert_called_once()
        self.assertEqual(check.call_args.args[1:3], ("gpu1", "/home/me/proj"))
        self.assertEqual(load_config(self.state, self.root)["workspaces"]["gpu1-proj"], "gpu1:/home/me/proj")
        with mock.patch.object(devices, "check_dir", return_value=("ok", "")):
            again, _ = resolve_binding(self.state, self.root, "gpu1-proj")
            self.assertEqual(resolve_selection(self.state, self.root, "gpu1-proj")[0], again)
            self.assertEqual(add_workspace(self.state, self.root, "gpu1:/home/me/proj"), ("gpu1-proj", False))

    def test_a_directory_the_device_does_not_have_or_a_device_that_does_not_answer_is_not_listed(self):
        for state, text in (("missing", "no directory"), ("offline", "silent")):
            with mock.patch.object(devices, "check_dir", return_value=(state, text)):
                with self.assertRaisesRegex(ValueError, text):
                    resolve_binding(self.state, self.root, "gpu1:/nope")
        self.assertNotIn("gpu1-nope", load_config(self.state, self.root)["workspaces"])
        with self.assertRaisesRegex(ValueError, "gpu7"):
            resolve_binding(self.state, self.root, "gpu7:/x")

    def test_the_directory_check_and_prune_leave_a_device_workspace_alone(self):
        configured(self)
        config = load_config(self.state, self.root)
        value = config["workspaces"]["gpu1-proj"]
        config["default"] = next(name for name in config["workspaces"] if name != "gpu1-proj")
        self.assertIsNone(directory_status([value])[value])  # None: not answered, never "gone".
        config["workspaces"]["gone"] = str(self.root / "no-such-dir")
        self.assertEqual(missing_workspaces(config), ["gone"])
        self.assertNotIn("gpu1-proj", missing_workspaces(config))

    def test_a_chat_sees_only_the_device_workspaces_its_space_allows(self):
        configured(self)
        config = load_config(self.state, self.root)
        seen = visible_workspaces(config, lambda alias: False)
        self.assertNotIn("gpu1-proj", seen["workspaces"])
        self.assertIn(seen["default"], seen["workspaces"])
        self.assertIs(visible_workspaces(config, lambda alias: True), config)

    def test_the_real_shim_answers_the_directory_check_in_five_seconds(self):
        device = FakeDevice()
        self.addCleanup(lambda: self.assertEqual(device.cleanup(), []))
        (device.home / "proj").mkdir()
        (device.home / "file").write_text("x")
        with mock.patch.object(devices, "open_transport", return_value=device.transport):
            ok = devices.check_dir(self.state, "gpu1", "~/proj")
            gone = devices.check_dir(self.state, "gpu1", "~/nope")
            plain = devices.check_dir(self.state, "gpu1", "~/file")
            unknown = devices.check_dir(self.state, "gpu7", "~/proj")
            device.faults.fail(1)
            offline = devices.check_dir(self.state, "gpu1", "~/proj")
        self.assertEqual(ok, ("ok", ""))
        self.assertEqual((gone[0], plain[0], unknown[0], offline[0]), ("missing", "missing", "unknown", "offline"))
        with mock.patch.object(devices, "open_transport", return_value=device.transport):
            stat = devices.stat_paths(self.state, "gpu1", "~/proj")
            (device.home / "proj" / "a.txt").write_text("abc")
            audit = audit_recovery({"checkpoint": {"artifacts": ["a.txt", "b.txt", "../file"]}}, "~/proj", stat_paths=stat)
        found = {item["path"].rsplit("/", 1)[-1]: item for item in audit["artifacts"]}
        self.assertEqual((found["a.txt"]["exists"], found["a.txt"]["bytes"], found["b.txt"]["exists"]), (True, 3, False))
        self.assertEqual(found["file"]["state"], "outside_workspace_not_inspected")
        self.assertEqual(audit["workdir"], "~/proj")

    def test_an_audit_that_could_not_ask_says_not_inspected_never_missing(self):
        def broken(paths):
            raise OSError("down")

        audit = audit_recovery({"artifacts": ["a.txt"]}, "~/proj", stat_paths=broken)
        self.assertEqual(audit["artifacts"], [{"path": "a.txt", "state": "device_not_inspected"}])


class OffWorkspacesTest(DeviceCase):
    on = False

    def test_off_a_device_looking_value_is_not_listed_or_decoded(self):
        with self.assertRaises(ValueError):
            resolve_binding(self.state, self.root, "gpu1:/home/me/proj")
        local = self.root / "here"
        local.mkdir()
        name, binding = resolve_binding(self.state, self.root, str(local))
        self.assertEqual((binding.workdir, binding.device, binding.value), (local.resolve(), "", str(local.resolve())))
        self.assertEqual(resolve_selection(self.state, self.root, name), (name, local.resolve()))
        self.assertTrue(directory_status([str(local)])[str(local)])
        self.assertFalse(directory_status(["gpu1:/x"])["gpu1:/x"])  # A relative path that does not exist, as before.


class AllowsDeviceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        path = Path(self.temp.name) / "devices.json"
        path.write_text(json.dumps(DOC))
        patch = mock.patch.dict(os.environ, {"COVEY_DEVICES": "1", "COVEY_DEVICES_FILE": str(path)})
        patch.start()
        self.addCleanup(patch.stop)
        self.access = Access(SimpleNamespace(state_dir=Path(self.temp.name), owner_open_id="ou_me", log=lambda _: None))
        self.access.spaces = spaces.Settings(personal_groups=frozenset({"oc_pg"}))
        self.access.owner_chat = lambda: "oc_dm"
        self.access.active = lambda chat: chat in ("oc_team", "oc_pg", "oc_proxy")
        self.access.proxy_chat = lambda chat: chat == "oc_proxy"

    def test_the_default_is_the_owners_private_chat_only(self):
        self.assertTrue(self.access.allows_device("oc_dm", "gpu1"))
        for chat in ("oc_pg", "oc_team", "oc_proxy", "oc_other", ""):
            self.assertFalse(self.access.allows_device(chat, "gpu1"), chat)

    def test_personal_groups_and_team_must_be_switched_on_by_name(self):
        for chat in ("oc_dm", "oc_pg", "oc_team"):
            self.assertTrue(self.access.allows_device(chat, "gpu2"), chat)
        self.assertFalse(self.access.allows_device("oc_proxy", "gpu2"))  # The proxy's groups: never.
        self.assertFalse(self.access.allows_device("oc_unbound", "gpu2"))

    def test_an_unknown_alias_and_a_device_object_are_both_answered(self):
        self.assertFalse(self.access.allows_device("oc_dm", "gpu9"))
        self.assertTrue(self.access.allows_device("oc_dm", devices.current().device("gpu1")))

    def test_with_team_spaces_off_a_team_device_is_still_not_for_a_group(self):
        self.access.spaces = spaces.Settings(allow_team_spaces=False)
        self.assertFalse(self.access.allows_device("oc_team", "gpu2"))

    def test_the_switch_off_allows_nothing(self):
        with mock.patch.dict(os.environ, {"COVEY_DEVICES": "0"}):
            self.assertFalse(self.access.allows_device("oc_dm", "gpu1"))


class ChildEnvTest(DeviceCase):
    def hub_environment(self):
        return mock.patch.dict(os.environ, {"LARK_APP_SECRET": "fake-secret", "FEISHU_TOKEN": "fake-feishu",
                                            "COVEY_DATA_HOME_X": "x", "ANTHROPIC_AUTH_TOKEN": "fake-token",
                                            "HOME": "/hub/home", "PATH": "/hub/bin", "HTTPS_PROXY": "http://hub-proxy"})

    def device_ctx(self):
        return devices.with_binding(self.ctx, devices.bind_workdir(self.ctx, "gpu1:/home/me/proj"))

    def test_a_device_starts_from_nothing_not_from_the_hubs_environment(self):
        profile = devices.device_dir(self.state, "gpu1")
        profile.mkdir(parents=True)
        (profile / "profile.json").write_text(json.dumps({"env": {"NO_PROXY": "internal.example"}}))
        with self.hub_environment():
            env = process_env.child_process_env(self.ctx.workdir, device=process_env.process_device(self.device_ctx()))
        self.assertEqual(env, {"PWD": "/home/me/proj", "NO_PROXY": "internal.example",
                               "HTTPS_PROXY": "http://proxy.example:3128"})  # devices.json's value over nothing of the hub's.
        for name in ("LARK_APP_SECRET", "FEISHU_TOKEN", "ANTHROPIC_AUTH_TOKEN", "HOME", "PATH"):
            self.assertNotIn(name, env)

    def test_hub_names_are_dropped_even_from_a_devices_own_environment_and_logged_by_name_only(self):
        spec = {"alias": "gpu1", "remote_workdir": "~/p", "env": {"LARK_APP_ID": "fake-id", "HTTPS_PROXY": "http://p.example"}}
        with self.assertLogs("covey.devices", level="INFO") as logs:
            env = process_env.child_process_env(None, device=spec)
        self.assertEqual(env, {"HTTPS_PROXY": "http://p.example", "PWD": "~/p"})
        text = "\n".join(logs.output)
        self.assertIn("LARK_APP_ID", text)
        self.assertNotIn("fake-id", text)
        self.assertNotIn("p.example", text)  # Values are never logged.

    def test_a_device_the_file_no_longer_lists_gets_an_empty_environment(self):
        ctx = devices.with_binding(self.ctx, devices.bind_workdir(self.ctx, "gpu9:/x", strict=False))
        with self.hub_environment():
            self.assertEqual(process_env.child_process_env(ctx.workdir, device=process_env.process_device(ctx)),
                             {"PWD": "/x"})

    def test_without_a_device_it_is_still_a_copy_of_the_hubs_environment(self):
        with self.hub_environment():
            env = process_env.child_process_env(self.root)
            self.assertEqual(env["LARK_APP_SECRET"], "fake-secret")
            self.assertEqual(env["PWD"], str(self.root.resolve()))
            self.assertIsNone(process_env.process_device(self.ctx))
            self.assertEqual(process_env.child_process_env(self.root, device=None), env)


class PromptTest(DeviceCase):
    def test_the_block_names_the_device_and_its_directory_never_the_stand_in(self):
        ctx = devices.with_binding(self.ctx, devices.bind_workdir(self.ctx, "gpu1:~/proj"), workspace_name="w")
        text = workspace_block("w", ctx.workdir, ctx.device, ctx.remote_workdir)
        self.assertIn("当前工作目录：~/proj（在设备 gpu1", text)
        self.assertNotIn("standin", text)
        self.assertNotIn(str(self.state), text)

    def test_the_local_block_is_what_it_was(self):
        text = workspace_block("w", self.root)
        self.assertIn(f"当前工作区：w\n当前工作目录：{self.root.resolve()}\n先读并遵守", text)
        self.assertEqual(text, workspace_block("w", self.root, "", ""))

    def test_the_history_command_is_not_offered_to_a_device_topic(self):
        ctx = devices.with_binding(self.ctx, devices.bind_workdir(self.ctx, "gpu1:/p"))
        seen = []
        with mock.patch.object(knowledge_turn.knowledge, "history_cmd", return_value="python3 /hub/history.py"), \
                mock.patch.object(knowledge_turn.knowledge, "enabled", return_value=False), \
                mock.patch.object(knowledge_turn, "knowledge_block",
                                  side_effect=lambda *a, **k: seen.append(k["history"]) or "") as block:
            knowledge_turn.block(ctx, make_job(), owner_turn=lambda: True, resumed=False)
            knowledge_turn.block(self.ctx, make_job(), owner_turn=lambda: True, resumed=False)
        self.assertEqual(seen, ["", "python3 /hub/history.py"])
        self.assertEqual(block.call_count, 2)

    def test_a_device_replys_local_image_paths_are_left_as_written(self):
        ctx = devices.with_binding(self.ctx, devices.bind_workdir(self.ctx, "gpu1:/p"))
        text = "![plot](/home/me/proj/plot.png)"
        with mock.patch("covey.app.turn_delivery.prepare_outgoing_markdown_images", return_value="UPLOADED") as prepare:
            self.assertEqual(outgoing_markdown(text, ctx), text)
            prepare.assert_not_called()
            self.assertEqual(outgoing_markdown(text, self.ctx), "UPLOADED")


class RefusalTest(TopicFixture, DeviceCase):
    """``Runs._refusal`` and ``ready`` for a context on a device; DeviceCase's environment, the fixture's app."""

    def setUp(self):
        DeviceCase.setUp(self)
        TopicFixture.setUp(self)
        self.ctx = make_context(self.root, options={"harness_id": "claude"})
        self.state = self.ctx.state_dir
        self.runs = self.app.runs
        self.runs.runtime.access.spaces = spaces.Settings()
        self.runs.runtime.access.owner_chat = lambda: "oc_dm"
        self.dctx = devices.with_binding(self.ctx, devices.bind_workdir(self.ctx, "gpu1:/home/me/proj"))
        self.job = make_job(chat_id="oc_dm")

    def refusal(self, ctx=None, job=None):
        return self.runs._refusal(ctx or self.dctx, None, job or self.job)

    def test_a_device_that_answers_in_an_allowed_chat_may_start(self):
        with mock.patch.object(devices, "check_dir", return_value=("ok", "")):
            self.assertIsNone(self.refusal())

    def test_the_reasons_are_named_on_the_card(self):
        cases = []
        with mock.patch.object(devices, "check_dir", return_value=("offline", "设备 gpu1 没有答复（Timeout）")):
            cases.append(self.refusal())
        with mock.patch.object(devices, "check_dir", return_value=("ok", "")):
            cases.append(self.refusal(job=make_job(chat_id="oc_team")))
            cases.append(self.refusal(devices.with_binding(self.ctx, devices.bind_workdir(self.ctx, "gpu2:/p"))))
            cases.append(self.refusal(devices.with_binding(
                self.ctx, devices.bind_workdir(self.ctx, "gpu9:/p", strict=False))))
        for result, words in zip(cases, ("连不上（Timeout）。这一轮没有开始，也不会改到本机跑", "不允许在这个聊天", "没有声明", "没有这台设备")):
            self.assertEqual(result.outcome.error_code, "workdir_missing")
            self.assertIn(words, result.failure_detail)
            self.assertIn(words, result.final_text if hasattr(result, "final_text") else result.failure_detail)

    def test_the_stand_in_is_never_looked_at_here(self):
        with mock.patch.object(devices, "check_dir", return_value=("ok", "")), \
                mock.patch("covey.app.runs.Path.is_dir", side_effect=AssertionError("hub path looked at")):
            self.assertIsNone(self.refusal())

    def test_off_a_missing_local_directory_is_refused_as_before_and_an_existing_one_is_not(self):
        gone = devices.with_binding(self.ctx, devices.Binding(self.root / "no-such-dir"))
        self.assertEqual(self.refusal(gone).outcome.error_code, "workdir_missing")
        self.assertIsNone(self.refusal(self.ctx))


class ContextTest(unittest.TestCase):
    def test_the_context_has_the_two_fields_with_empty_defaults(self):
        ctx = make_context(Path("/nonexistent"))
        self.assertEqual((ctx.device, ctx.remote_workdir), ("", ""))


if __name__ == "__main__":
    unittest.main()
