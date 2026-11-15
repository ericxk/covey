"""Browsing a device's directories, the self-check and what the hub knows of a device (A7).

Everything runs against a fake device: the real shim (covey/core/device_shim.py) under a temporary HOME, through
``tests/support/fake_device.py``. The directory browser lists, searches and moves on the device, never on the hub
(``tests/support/device_guard.py`` watches the whole run); ``alias:path`` typed in the search box takes it there; the
self-check runs a real minimal request on the device and says in words what is wrong; the readiness check reads the
device's cached ``hello``; Codex's model list is the device's own.
"""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import exec_tempdir, make_context
from support.cards import selects
from support.device_guard import DeviceGuard
from support.fake_device import FakeDevice
from support.world import LaunchCardCase, PickerCase

import test_workspaces as tw
from covey.app import availability
from covey.app.directory_picker import DirectoryCache, device_hop, listed_paths, step
from covey.backends.codex.bridge import CodexBackend
from covey.cards.directory import DEVICE_PLACEHOLDER, SEARCH_PLACEHOLDER, directory_card
from covey.core.text import literal
from covey.core import devices, harness_probe
from covey.core.model_settings import model_catalog
from covey.core.workspaces import load_config
from covey.service import device_check, doctor

DOC = {"schema": 1, "control_dir": "/dev/shm/covey-ssh-browse",
       "devices": {"gpu1": {"title": "GPU 1", "ssh": {"host": "gpu1.example"}, "harnesses": ["claude", "codex"],
                            "spaces": ["owner_private"]},
                   "gpu2": {"ssh": {"host": "gpu2.example"}, "harnesses": ["codex"], "spaces": ["team"]}}}


class DeviceEnv:
    """devices.json in a temporary directory, COVEY_DEVICES on, a fake device behind every ``open_transport``."""

    def start_devices(self, root):
        (root / "devices.json").write_text(json.dumps(DOC))
        patch = mock.patch.dict(os.environ, {"COVEY_DEVICES": "1", "COVEY_DEVICES_FILE": str(root / "devices.json")})
        patch.start()
        self.addCleanup(patch.stop)
        self.device = FakeDevice()
        self.addCleanup(lambda: self.assertEqual(self.device.cleanup(), []))
        opened = mock.patch.object(devices, "open_transport", side_effect=lambda *a, **k: self.device.transport)
        opened.start()
        self.addCleanup(opened.stop)
        self.config = devices.parse(json.dumps(DOC))

    def make_tree(self):
        home = self.device.home
        for relative in ("proj/src/alpha", "proj/docs", "proj/.hidden/alpha", "proj/node_modules/alpha", "other/deep/er/alpha4"):
            (home / relative).mkdir(parents=True)
        (home / "proj" / "file.txt").write_text("x")
        (home / "proj" / "to-docs").symlink_to(home / "proj" / "docs")
        (home / "proj" / "to-file").symlink_to(home / "proj" / "file.txt")


class ListAndSearchTest(DeviceEnv, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.start_devices(self.root)
        self.make_tree()

    def test_a_listing_is_the_devices_with_its_home_expanded_there(self):
        found = devices.list_dirs(self.root, "gpu1", "~/proj")
        self.assertEqual(found["code"], "")
        self.assertEqual(found["path"], str(self.device.home / "proj"))
        self.assertEqual([(e["name"], e["link"]) for e in found["entries"]],
                         [(".hidden", False), ("docs", False), ("node_modules", False), ("src", False),
                          ("to-docs", True), ("to-file", True)])  # Files are not listed; links are, whatever they point to.
        self.assertEqual(devices.list_dirs(self.root, "gpu1", "~")["path"], str(self.device.home))

    def test_the_ways_a_listing_fails_are_told_apart(self):
        self.assertEqual(devices.list_dirs(self.root, "gpu1", "~/nowhere")["code"], "not_found")
        self.assertEqual(devices.list_dirs(self.root, "gpu1", "~/proj/file.txt")["code"], "not_found")
        self.device.faults.fail(1)
        offline = devices.list_dirs(self.root, "gpu1", "~/proj")
        self.assertEqual((offline["code"], offline["entries"]), ("offline", []))
        self.assertEqual(devices.list_dirs(self.root, "gpu1", "~/proj")["code"], "")  # The next one is fine.

    def test_a_search_is_the_pickers_walk_done_on_the_device(self):
        found = devices.search_dirs(self.root, "gpu1", "~/proj", " ALPHA ")
        self.assertEqual((found["code"], found["path"]), ("", str(self.device.home / "proj")))
        self.assertEqual([e["name"] for e in found["entries"]], ["src/alpha"])  # Not .hidden, not node_modules.
        self.assertEqual([e["name"] for e in devices.search_dirs(self.root, "gpu1", "~/proj", ".hid")["entries"]], [".hidden"])
        deep = devices.search_dirs(self.root, "gpu1", "~", "alpha")
        self.assertEqual([e["name"] for e in deep["entries"]], ["proj/src/alpha", "other/deep/er/alpha4"][:1],
                         "alpha4 is four levels down, beyond the depth of 3")
        links = devices.search_dirs(self.root, "gpu1", "~/proj", "to-")
        self.assertEqual([(e["name"], e["link"]) for e in links["entries"]], [("to-docs", True), ("to-file", True)])
        self.assertEqual(devices.search_dirs(self.root, "gpu1", "~/nowhere", "x")["code"], "not_found")

    def test_a_search_that_stops_early_says_so(self):
        found = devices.search_dirs(self.root, "gpu1", "~", "alpha", limits={"directories": 2})
        self.assertTrue(found["truncated"])
        self.assertEqual(found["scanned"], 2)

    def test_the_hub_looks_at_no_path_of_the_device(self):
        with DeviceGuard(self.root / "state" / "devices" / "gpu1" / "standin", device_root=self.device.home) as guard:
            devices.list_dirs(self.root, "gpu1", "~/proj")
            devices.search_dirs(self.root, "gpu1", "~", "alpha")
            devices.codex_root(self.root, "gpu1")
        guard.assert_untouched(self)


class TypedAddressTest(DeviceEnv, unittest.TestCase):
    """``step``: the typed ``alias:path``, and that a device view does not take the hub's ``~`` or directories."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.start_devices(Path(self.temp.name))

    def allow(self, *aliases):
        return lambda alias: "" if alias in aliases else "设备 %s 不允许在这个聊天里使用（devices.json 的 spaces）" % alias

    def test_alias_and_path_takes_the_view_to_the_device(self):
        view = {"path": "/home/user", "query": "", "revision": 1}
        self.assertEqual(step(view, "search", "gpu1:/srv//work/./x/", allow=self.allow("gpu1")), "refresh")
        self.assertEqual((view["device"], view["path"], view["device_title"]), ("gpu1", "/srv/work/x", "GPU 1"))
        self.assertEqual(step(view, "search", "gpu1:", allow=self.allow("gpu1")), "refresh")
        self.assertEqual(view["path"], "~")
        self.assertEqual(device_hop("gpu1:~/a"), ("gpu1", "~/a"))
        self.assertIsNone(device_hop("alpha"))
        self.assertIsNone(device_hop("note: x"))

    def test_a_device_the_chat_may_not_use_or_does_not_exist_is_refused_on_the_card(self):
        view = {"path": "/home/user", "query": "", "revision": 1}
        self.assertEqual(step(view, "search", "gpu2:/x", allow=self.allow("gpu1")), "show")
        self.assertIn("gpu2 不允许", view["notice"])
        self.assertNotIn("device", view)
        self.assertEqual(step(view, "search", "gpu1:/x"), "show")  # No gate given: no device.
        self.assertNotIn("device", view)

    def test_the_hubs_home_is_never_expanded_for_a_device(self):
        view = {"path": "/srv/a/b", "device": "gpu1", "query": "", "revision": 1}
        with mock.patch.object(os.path, "expanduser", side_effect=AssertionError("the hub's ~")):
            self.assertEqual(step(view, "search", "~/sub"), "refresh")
            self.assertEqual(view["path"], "~/sub")
            self.assertEqual(step(view, "up", None), "read")
            self.assertEqual(view["path"], "~")
            self.assertEqual(step(view, "up", None), "read")
            self.assertEqual(view["path"], "/")
            self.assertEqual(step(view, "search", "relative/dir"), "read")  # A name search, as on the hub.
        self.assertEqual(view["query"], "relative/dir")

    def test_a_choice_in_a_device_view_is_the_workspace_value_with_the_alias(self):
        view = {"path": "/srv/a", "device": "gpu1", "query": "", "revision": 1, "entries": [{"name": "p", "link": False}]}
        self.assertEqual(step(view, "use", None), ("choose", "gpu1:/srv/a"))
        self.assertEqual(step(view, "pick", 0), ("choose", "gpu1:/srv/a/p"))
        local = {"path": "/srv/a", "query": "", "revision": 1}
        self.assertEqual(step(local, "use", None), ("choose", "/srv/a"))

    def test_near_is_not_for_a_device_view(self):
        view = {"path": "/srv/a", "device": "gpu1", "query": "", "revision": 1}
        self.assertIsNone(step(view, "near", None, near="/x/y"))

    def test_the_cache_key_carries_the_device(self):
        (self.device.home / "only-on-device").mkdir()
        cache = DirectoryCache()
        here = Path(self.temp.name) / "hub-dir"
        (here / "only-on-hub").mkdir(parents=True)
        local = cache.request(str(here)).result(5)
        self.assertEqual([e["name"] for e in local["entries"]], ["only-on-hub"])
        self.assertNotIn("path", local)
        # The fake device is this machine, so the same path text lists the same directory: the key tells them apart.
        remote = cache.request(str(here), device="gpu1", state_dir=Path(self.temp.name)).result(5)
        self.assertEqual(remote["path"], str(here))
        self.assertEqual([c for c, _ in self.device.transport.log if c == "ls"], ["ls"], "the device was asked once")
        self.assertEqual(sorted(cache.cache, key=str), sorted([str(here), ("dev", "gpu1", str(here))], key=str))
        cache.request(str(here)).result(5)
        cache.request(str(here), device="gpu1", state_dir=Path(self.temp.name)).result(5)
        self.assertEqual([c for c, _ in self.device.transport.log if c == "ls"], ["ls"], "both answers came from the cache")
        again = cache.request("~", device="gpu1", state_dir=Path(self.temp.name)).result(5)
        self.assertEqual([e["name"] for e in again["entries"] if not e["name"].startswith(".")], ["only-on-device"])
        self.assertEqual(again["path"], str(self.device.home))

    def test_marks_of_the_saved_list_are_per_device(self):
        config = {"workspaces": {"here": "/srv/a", "gpu1-a": "gpu1:/srv/a", "gpu2-a": "gpu2:/srv/a"}}
        self.assertEqual(listed_paths(config), {"/srv/a": "here"})
        self.assertEqual(listed_paths(config, "gpu1"), {"/srv/a": "gpu1-a"})


class CardTest(DeviceEnv, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.start_devices(Path(self.temp.name))

    def text(self, view, **kw):
        return json.dumps(directory_card("t" * 24, {"revision": 1, "entries": [], "query": "", **view}, **kw), ensure_ascii=False)

    def test_the_browser_says_which_device_it_is_on_and_how_to_type_one(self):
        local = self.text({"path": "/srv/a"})
        self.assertNotIn("设备", local)
        self.assertIn(SEARCH_PLACEHOLDER, local)
        self.assertNotIn(DEVICE_PLACEHOLDER, local)
        offered = self.text({"path": "/srv/a", "offer": True})
        self.assertIn(DEVICE_PLACEHOLDER, offered)
        on = self.text({"path": "/srv/a", "device": "gpu1", "device_title": "GPU 1"})
        self.assertIn("设备 GPU 1（gpu1）· /srv/a", on)
        self.assertIn(DEVICE_PLACEHOLDER, on)


class DevicePickerTest(DeviceEnv, PickerCase):
    """The launch card's 其他目录… browser, on a fake device through the real picker."""

    def setUp(self):
        super().setUp()
        self.start_devices(self.root)
        self.make_tree()
        self.prepare()
        self.app.runtime.access.remember_owner_chat("chat")

    def press(self, verb, **fields):
        self.picker.process(self.ctx, {**self.open_event, "action_name":
                            f"bridge_dir_{self.token}:{self.record()['picker']['revision']}:{verb}",
                            "form_value": {"query": "", **fields}})

    def text(self):
        return json.dumps(self.current_card(), ensure_ascii=False)

    def open_picker(self):
        self.picker.process(self.ctx, self.open_event)
        self.assertNotIn("device", self.record()["picker"])

    def test_typing_alias_and_path_lists_the_device_and_the_card_says_so(self):
        self.open_picker()
        self.assertIn(DEVICE_PLACEHOLDER, self.text())
        with DeviceGuard(self.root / "state" / "devices" / "gpu1" / "standin", device_root=self.device.home) as guard:
            self.press("search", query="gpu1:~/proj")
            picker = self.record()["picker"]
            self.assertEqual((picker["device"], picker["path"]), ("gpu1", str(self.device.home / "proj")))
            self.assertEqual([e["name"] for e in picker["entries"]],
                             [".hidden", "docs", "node_modules", "src", "to-docs", "to-file"])
            self.assertIn(literal(f"设备 GPU 1（gpu1）· {self.device.home}/proj"), self.text())
            self.press("search", query="alpha")
            self.assertEqual([e["name"] for e in self.record()["picker"]["entries"]], ["src/alpha"])
            self.press("clear")
            index = next(i for i, e in enumerate(self.record()["picker"]["entries"]) if e["name"] == "src")
            self.press(f"enter{index}")
            self.assertEqual(self.record()["picker"]["path"], str(self.device.home / "proj" / "src"))
            self.press("up")
            self.assertEqual(self.record()["picker"]["path"], str(self.device.home / "proj"))
            self.press("search", query="~/other")
            self.assertEqual(self.record()["picker"]["path"], str(self.device.home / "other"))
            self.assertEqual([e["name"] for e in self.record()["picker"]["entries"]], ["deep"])
        guard.assert_untouched(self)

    def test_using_a_device_directory_registers_alias_and_path_and_goes_back_to_the_launch_card(self):
        self.open_picker()
        self.press("search", query="gpu1:~/proj")
        self.press("use")
        self.assertEqual(self.record()["status"], "replaced")
        config = load_config(self.ctx.state_dir, self.work)
        self.assertEqual(config["workspaces"]["gpu1-proj"], f"gpu1:{self.device.home}/proj")
        self.assertEqual(selects(self.current_card())["workspace"]["initial_option"], "gpu1-proj")
        self.assertIn("已选择工作区：gpu1&#45;proj", self.text())

    def test_a_chat_the_device_is_not_for_cannot_go_there(self):
        self.open_picker()
        with mock.patch.object(self.app.runtime.access, "allows_device", return_value=False):
            self.press("search", query="gpu1:~/proj")
        self.assertNotIn("device", self.record()["picker"])
        self.assertIn("不允许在这个聊天里使用", self.text())
        self.assertIn("project", self.text())  # Still on the hub's directory.

    def test_an_unknown_alias_is_said_not_searched(self):
        self.open_picker()
        self.press("search", query="gpu9:/x")
        self.assertIn("gpu9 不在 devices.json 里", self.text())

    def test_a_device_that_does_not_answer_is_a_recoverable_error(self):
        self.open_picker()
        self.device.faults.fail(1)
        self.press("search", query="gpu1:~/proj")
        picker = self.record()["picker"]
        self.assertIn("设备 gpu1 没有答复", picker["error"])
        self.assertIn("设备 gpu1 没有答复", self.text())
        self.press("refresh")
        self.assertEqual(self.record()["picker"]["error"], "")
        self.assertEqual(len(self.record()["picker"]["entries"]), 6)

    def test_a_missing_device_directory_reads_like_a_missing_directory(self):
        self.open_picker()
        self.press("search", query="gpu1:~/nowhere")
        self.assertIn("路径不存在", self.text())

    def test_the_browser_opens_on_the_device_of_the_selected_workspace(self):
        value = f"gpu1:{self.device.home}/proj"
        config = load_config(self.ctx.state_dir, self.work)
        config["workspaces"]["gpu1-proj"] = value
        (self.ctx.state_dir / "workspace-config.json").write_text(json.dumps(config))
        self.open_event["form_value"]["workspace"] = "gpu1-proj"
        self.picker.process(self.ctx, self.open_event)
        picker = self.record()["picker"]
        self.assertEqual((picker["device"], picker["path"]), ("gpu1", f"{self.device.home}/proj"))
        self.assertEqual(len(picker["entries"]), 6)
        self.assertTrue(picker["offer"])


class OffTest(PickerCase):
    """With devices off nothing of this shows: the browser, the card and the cache key are the old ones."""

    def test_the_browser_stays_what_it_was_and_gpu1_colon_is_a_name_search(self):
        for name in ("COVEY_DEVICES", "LARK_BRIDGE_DEVICES"):
            self.assertNotIn(name, os.environ)
        self.prepare()
        (self.work / "gpu1:x").mkdir()
        self.picker.process(self.ctx, self.open_event)
        picker = self.record()["picker"]
        self.assertNotIn("offer", picker)
        self.assertNotIn("device", picker)
        self.assertIn(SEARCH_PLACEHOLDER, json.dumps(self.current_card(), ensure_ascii=False))
        self.picker.process(self.ctx, {**self.open_event, "action_name": f"bridge_dir_{self.token}:{picker['revision']}:search",
                                       "form_value": {"query": "gpu1:/x"}})
        after = self.record()["picker"]
        self.assertNotIn("device", after)
        self.assertEqual((after["query"], after["results"]), ("gpu1:/x", True))
        self.assertEqual(after["path"], str(self.work))
        self.assertTrue(all(isinstance(key, str) or key[0] != "dev" for key in self.picker.cache.cache))


class LaunchCardLabelTest(DeviceEnv, LaunchCardCase):
    def setUp(self):
        super().setUp()
        self.start_devices(self.root)

    def test_the_launch_card_names_the_device_of_the_chosen_workspace(self):
        from covey.app.launch import build_launch_card
        config = load_config(self.ctx.state_dir, self.work)
        config["workspaces"]["gpu1-proj"] = "gpu1:/home/me/proj"
        (self.ctx.state_dir / "workspace-config.json").write_text(json.dumps(config))
        self.app.runtime.access.remember_owner_chat("chat")
        self.prepare_card()
        self.assertIn("gpu1-proj · GPU 1", json.dumps(self.card, ensure_ascii=False))  # The row's option.
        self.assertNotIn("设备 GPU 1", json.dumps(self.card, ensure_ascii=False))  # Not chosen: no label in the summary.
        draft = {"workspace": "gpu1-proj", "model": self.record()["selected_model"]}
        chosen = build_launch_card(self.child.topics, self.target, draft=draft)
        self.assertIn("工作区 gpu1-proj · 设备 GPU 1", json.dumps(chosen, ensure_ascii=False))
        self.assertIn("gpu1-proj · GPU 1", json.dumps(chosen, ensure_ascii=False))

    def test_a_local_workspace_gets_no_label(self):
        self.prepare_card()
        self.assertNotIn("设备", json.dumps(self.card, ensure_ascii=False))


class ManagerDeviceTest(DeviceEnv, LaunchCardCase):
    """The workspace card: a status line per device, the device tag of its rows, the browser on a device."""

    prepare = tw.WorkspaceManagerTests.prepare
    workspaces = tw.WorkspaceManagerTests.workspaces
    menu = tw.WorkspaceManagerTests.menu
    standalone = tw.WorkspaceManagerTests.standalone
    config_of = tw.WorkspaceManagerTests.config
    updates = tw.WorkspaceManagerTests.updates
    shown = tw.WorkspaceManagerTests.shown
    text = tw.WorkspaceManagerTests.text
    click = tw.WorkspaceManagerTests.click
    record = tw.WorkspaceManagerTests.record

    def setUp(self):
        super().setUp()
        self.start_devices(self.root)
        self.make_tree()

    def test_the_card_has_a_line_for_each_device_it_may_use_from_the_last_check(self):
        self.prepare()
        self.standalone()
        self.click("refresh")
        text = self.text()
        self.assertIn(literal("设备 GPU 1（gpu1）：还没自检：在 Bridge 所在机器上运行 covey_devices.py check gpu1"), text)
        self.assertNotIn("gpu2", text)  # Its spaces are team: not for the owner's private chat.
        devices.write_json(devices.device_dir(self.ctx.state_dir, "gpu1") / "profile.json", {
            "tools": {"claude": {"found": True, "version": "2.1.310 (Claude Code)"}},
            "check": {"at": __import__("time").time() - 300, "ok": True, "failed": "", "text": ""}})
        self.click("refresh")
        self.assertIn(literal("设备 GPU 1（gpu1）：自检通过（5 分钟前） · claude 2.1.310"), self.text())

    def test_adding_a_workspace_browses_the_device_and_adds_alias_and_path(self):
        self.prepare()
        self.standalone()
        self.click("browse")
        self.assertIn(DEVICE_PLACEHOLDER, self.text())
        with DeviceGuard(self.root / "state" / "devices" / "gpu1" / "standin", device_root=self.device.home) as guard:
            self.click("dir-search", query="gpu1:~/proj")
            browse = self.record()["manager"]["browse"]
            self.assertEqual((browse["device"], browse["path"]), ("gpu1", str(self.device.home / "proj")))
            self.assertIn("设备 GPU 1（gpu1）", self.text())
            self.click("dir-use")
        guard.assert_untouched(self)
        self.assertEqual(self.config_of()["workspaces"]["gpu1-proj"], f"gpu1:{self.device.home}/proj")
        self.assertIn("✓ 已添加 gpu1&#45;proj", self.text())
        self.assertIn("此目录已在列表", self.text())  # Marked as listed: by its path on the device.
        self.click("dir-back")
        self.assertIn("设备 gpu1", self.text())  # The row's tag.
        self.assertIn(literal(f"gpu1:{self.device.home}/proj"), self.text())


class AvailabilityTest(DeviceEnv, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.start_devices(self.root)
        self.ctx = make_context(self.root, device="gpu1", remote_workdir="/srv/p", backend_cmd=["claude"])
        self.hello = {"tools": {"claude": {"found": True, "login": "ok"}, "codex": {"found": True, "login": "ok"}}}

    def test_the_hubs_commands_are_not_looked_for_on_a_device(self):
        devices.write_hello(self.ctx.state_dir, "gpu1", self.hello)
        with mock.patch.object(availability.shutil, "which", return_value=None), \
                mock.patch.object(availability.subprocess, "run", side_effect=AssertionError("ran on the hub")):
            check = availability.check_or_raise("claude", self.ctx)
            self.assertEqual(check["status"], "configured")
            self.assertEqual(availability.local_check("codex", self.ctx)["scope"], "device_preflight; model access not probed")

    def test_what_the_cached_hello_says_decides(self):
        write = lambda tools: devices.write_hello(self.ctx.state_dir, "gpu1", {"tools": tools})
        write({"claude": {"found": False}})
        self.assertEqual(availability.local_check("claude", self.ctx)["reason"], "executable_missing")
        with self.assertRaisesRegex(ValueError, "不可用"):
            availability.check_or_raise("claude", self.ctx)
        write({"claude": {"found": True, "login": "no"}})
        with self.assertRaisesRegex(ValueError, "尚未登录"):
            availability.check_or_raise("claude", self.ctx)
        write({"claude": {"found": True, "login": "ok"}})
        self.assertEqual(availability.check_or_raise("claude", self.ctx)["status"], "configured")

    def test_the_last_minimal_request_that_found_no_login_blocks_too(self):
        devices.write_hello(self.ctx.state_dir, "gpu1", self.hello)
        devices.write_json(devices.device_dir(self.ctx.state_dir, "gpu1") / "profile.json",
                           {"probe": {"claude": {"ok": False, "reason": "not_logged_in"}}})
        self.assertEqual(availability.local_check("claude", self.ctx)["scope"], "device_probe")
        with self.assertRaisesRegex(ValueError, "尚未登录"):
            availability.check_or_raise("claude", self.ctx)
        self.assertEqual(availability.check_or_raise("codex", self.ctx)["status"], "configured")

    def test_no_recent_hello_does_not_stop_a_task(self):
        check = availability.check_or_raise("claude", self.ctx)
        self.assertEqual((check["status"], check["reason"]), ("unknown", "device_not_checked"))
        devices.write_hello(self.ctx.state_dir, "gpu1", self.hello, now=1.0)  # Long ago.
        self.assertEqual(availability.check_or_raise("claude", self.ctx)["status"], "unknown")

    def test_a_local_context_is_checked_as_before(self):
        local = make_context(self.root, backend_cmd=["definitely-not-installed-xyz"])
        self.assertEqual(availability.local_check("claude", local)["reason"], "executable_missing")

    def test_the_two_probe_cache_reads_take_the_devices_environment(self):
        seen = []
        with mock.patch.object(availability, "child_process_env", side_effect=lambda *a, **k: seen.append(k) or {}), \
                mock.patch.object(availability, "probe_enabled", return_value=True), \
                mock.patch.object(availability.harness_probe, "read_cache", return_value=None), \
                mock.patch.object(availability.harness_probe, "cached_probe", return_value={"ok": True}), \
                mock.patch.object(availability.shutil, "which", return_value="/bin/true"), \
                mock.patch.object(availability.subprocess, "run", side_effect=OSError):
            availability.local_check("codex", make_context(self.root, backend_cmd=["codex"]))
            gateway = mock.Mock()
            gateway.harnesses = {"codex": mock.Mock(context=self.ctx)}
            gateway.runtime = mock.Mock()
            availability.sample(gateway, self.ctx)
        self.assertEqual(seen[0], {"device": None})
        self.assertEqual(seen[-1]["device"]["alias"], "gpu1")


class CodexModelsTest(DeviceEnv, unittest.TestCase):
    ROW = {"slug": "gpt-device", "visibility": "list", "display_name": "Device GPT"}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.start_devices(self.root)
        (self.device.home / ".codex").mkdir()
        (self.device.home / ".codex" / "models_cache.json").write_text(json.dumps({"models": [
            self.ROW, {"slug": "hidden", "visibility": "hide"}]}))
        self.hub_home = self.root / "hub-codex"
        self.hub_home.mkdir()
        (self.hub_home / "models_cache.json").write_text(json.dumps({"models": [
            {"slug": "gpt-hub", "visibility": "list"}]}))
        env = mock.patch.dict(os.environ, {"CODEX_HOME": str(self.hub_home)})
        env.start()
        self.addCleanup(env.stop)

    def test_a_device_turn_lists_the_devices_models_not_the_hubs(self):
        ctx = make_context(self.root, device="gpu1", remote_workdir="/srv/p")
        self.assertEqual(sorted(CodexBackend().model_catalog(ctx)), ["gpt-device"])
        local = make_context(self.root)
        self.assertEqual(sorted(CodexBackend().model_catalog(local)), ["gpt-hub"])
        self.assertEqual(sorted(model_catalog(devices.codex_root(ctx.state_dir, "gpu1"))), ["gpt-device"])

    def test_the_copy_is_kept_for_a_while_and_refreshed_on_request(self):
        root = devices.codex_root(self.root, "gpu1")
        calls = []
        real = self.device.transport.call
        self.device.transport.call = lambda cmd, **f: calls.append(cmd) or real(cmd, **f)
        (self.device.home / ".codex" / "models_cache.json").write_text(json.dumps({"models": [
            {"slug": "gpt-newer", "visibility": "list"}]}))
        self.assertEqual(sorted(model_catalog(devices.codex_root(self.root, "gpu1"))), ["gpt-device"])
        self.assertNotIn("get", calls)
        self.assertEqual(sorted(model_catalog(devices.codex_root(self.root, "gpu1", refresh=True))), ["gpt-newer"])
        self.assertTrue(root.is_dir())

    def test_a_device_that_does_not_answer_leaves_the_old_copy_or_none(self):
        self.device.faults.fail(1)
        gone = tempfile.TemporaryDirectory()
        self.addCleanup(gone.cleanup)
        self.assertEqual(model_catalog(devices.codex_root(Path(gone.name), "gpu1")), {})
        devices.codex_root(self.root, "gpu1")
        self.device.faults.fail(1)
        self.assertEqual(sorted(model_catalog(devices.codex_root(self.root, "gpu1", refresh=True))), ["gpt-device"])


class ClaudeModelsTest(DeviceEnv, unittest.TestCase):
    def test_the_claude_catalog_goes_through_spawn_process_so_it_is_the_devices(self):
        from covey.backends.claude import models
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        ctx = make_context(Path(self.temp.name), device="gpu1", remote_workdir="/srv/p", backend_cmd=["claude"])
        spawned = []

        def fake_spawn(argv, *, ctx=None, **kwargs):
            spawned.append(getattr(ctx, "device", ""))
            raise RuntimeError("stop here")

        with mock.patch("covey.backends.claude.process.spawn_process", fake_spawn):
            with self.assertRaises(RuntimeError):
                models.model_catalog(mock.Mock(), ctx, {}, refresh=True)
        self.assertEqual(spawned, ["gpu1"], "the catalog process is started for the device, not the hub")


class SelfCheckTest(DeviceEnv, unittest.TestCase):
    """The flow ``covey_devices.py check`` and the card share: connect, push, hello, profile, a real request each."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.start_devices(self.root)
        self.bin = exec_tempdir()
        self.addCleanup(self.bin.cleanup)
        self.device.transport.env["PATH"] = "%s:%s" % (self.bin.name, self.device.transport.env["PATH"])
        self.tools = Path(self.bin.name)

    def tool(self, name, body):
        path = self.tools / name
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(0o755)
        if not os.access(path, os.X_OK):
            self.skipTest("no directory here where a script may be executed (run through scripts/test_map.py)")

    def good_tools(self):
        self.tool("claude", 'case "$1" in --version) echo "2.1.310 (Claude Code)";; auth) echo \'{"loggedIn": true}\';; '
                            '*) echo ok;; esac')
        self.tool("codex", 'case "$1" in --version) echo "codex-cli 0.164.0";; login) echo "Logged in";; *) echo ok;; esac')

    def check(self, **kw):
        return devices.check_device(self.config, "gpu1", state_dir=self.state, transport=self.device.transport, **kw)

    def test_a_good_device_passes_with_a_real_request_each_and_says_so(self):
        self.good_tools()
        result = self.check(now=lambda: 4000.0)
        self.assertTrue(result.ok, result.lines())
        self.assertEqual([s.name for s in result.steps], ["connect", "shim", "hello", "profile", "tools", "probe:claude",
                                                            "probe:codex"])
        self.assertIn("reader", result.steps[1].detail, "Codex is declared: its reader is pushed too")
        self.assertEqual(result.explain(), "gpu1 自检通过：claude 2.1.310 · codex 0.164.0；claude、codex 的最小请求都通过了")
        profile = devices.read_profile(self.state, "gpu1")
        self.assertEqual((profile["probe"]["claude"]["ok"], profile["probe"]["codex"]["reason"]), (True, "ok"))
        self.assertEqual(profile["check"], {"at": 4000.0, "ok": True, "failed": "", "text": result.explain()})
        self.assertEqual(devices.read_hello(self.state, "gpu1", now=4001.0)["shim"], result.hello["shim"])
        self.assertEqual(devices.status_text(self.state, "gpu1", now=4300.0), "自检通过（5 分钟前） · claude 2.1.310 · codex 0.164.0")

    def test_the_request_runs_with_the_profiles_environment_on_the_device(self):
        self.tool("claude", 'case "$1" in --version) echo "2.1.310";; auth) echo \'{"loggedIn": true}\';; '
                            '*) echo "proxy=$HTTPS_PROXY";; esac')
        self.tool("codex", 'case "$1" in --version) echo "codex-cli 0.1";; login) echo ok;; *) echo ok;; esac')
        config = devices.parse(json.dumps({**DOC, "devices": {"gpu1": {**DOC["devices"]["gpu1"],
                                                                         "env": {"HTTPS_PROXY": "http://proxy.example:3128"}}}}))
        seen = []
        real = self.device.transport.call
        self.device.transport.call = lambda cmd, **f: seen.append((cmd, f)) or real(cmd, **f)
        devices.self_check(config, "gpu1", state_dir=self.state, transport=self.device.transport, probe=devices.probe_harness)
        sent = [f for cmd, f in seen if cmd == "exec" and "-p" in f["argv"]]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["env"], {"HTTPS_PROXY": "http://proxy.example:3128"})
        self.assertEqual((sent[0]["timeout"], sent[0]["cwd"]), (60, "~"))
        self.assertEqual(sent[0]["argv"][1:3], ["-p", harness_probe.PROMPT])

    def test_without_a_proxy_a_network_failure_says_where_to_put_one(self):
        self.tool("claude", 'case "$1" in --version) echo "2.1.310";; auth) echo \'{"loggedIn": true}\';; '
                            '*) echo "connect ECONNREFUSED" >&2; exit 1;; esac')
        self.tool("codex", 'case "$1" in --version) echo "codex-cli 0.1";; login) echo ok;; *) echo ok;; esac')
        result = self.check()
        self.assertFalse(result.ok)
        self.assertEqual(result.failed.name, "probe:claude")
        self.assertEqual(result.explain(), (
            "gpu1 上的 claude 连不上模型服务，而且环境档案里没有代理（HTTPS_PROXY）。设备出网要代理的话，在设备的 "
            "~/.claude/settings.json 的 env 里设 HTTPS_PROXY，或写进 devices.json 里这台设备的 env，再自检。"))
        self.assertEqual(devices.read_profile(self.state, "gpu1")["probe"]["claude"]["reason"], "network")
        self.assertIn("自检未通过", devices.status_text(self.state, "gpu1"))
        self.assertIn("没有代理", devices.status_text(self.state, "gpu1"))

    def test_with_a_proxy_the_same_failure_says_the_proxy_does_not_work(self):
        self.tool("claude", 'case "$1" in --version) echo "2.1.310";; auth) echo \'{"loggedIn": true}\';; '
                            '*) echo "proxy error 502" >&2; exit 1;; esac')
        config = devices.parse(json.dumps({**DOC, "devices": {"gpu1": {**DOC["devices"]["gpu1"], "harnesses": ["claude"],
                                                                         "env": {"HTTPS_PROXY": "http://proxy.example:3128"}}}}))
        result = devices.check_device(config, "gpu1", state_dir=self.state, transport=self.device.transport)
        self.assertIn("已带代理（HTTPS_PROXY）仍然不通", result.explain())
        self.assertNotIn("proxy.example", result.explain())

    def test_not_logged_in_timeout_and_other_failures_each_have_their_words(self):
        for alias, harness, reason, expect in (
                ("g", "claude", "not_logged_in", "gpu1 上的 claude 没有登录：在设备上运行 claude，用 /login 登录，再自检。"),
                ("g", "codex", "not_logged_in", "gpu1 上的 codex 没有登录：在设备上运行 codex login，再自检。"),
                ("g", "claude", "timeout", "gpu1 上的 claude 在 60 秒内没有答复（出网慢，或被代理卡住）。"),
                ("g", "claude", "not_installed", "gpu1 上没有 claude（PATH 和常见安装目录里都找不到）。"),
                ("g", "codex", "other", "gpu1 上的 codex 最小请求失败：boom")):
            self.assertEqual(devices.explain_probe("gpu1", harness, {"reason": reason, "detail": "boom"}, {}), expect)

    def test_a_probe_that_times_out_is_a_timeout_not_a_hang(self):
        self.tool("claude", 'case "$1" in --version) echo "2.1.310";; auth) echo \'{"loggedIn": true}\';; *) sleep 30;; esac')
        self.tool("codex", 'case "$1" in --version) echo "codex-cli 0.1";; login) echo ok;; *) echo ok;; esac')
        profile = self.check(now=lambda: 1.0).profile
        answer = devices.probe_harness(self.device.transport, self.config.device("gpu1"), "claude", profile, timeout=1)
        self.assertEqual((answer["ok"], answer["reason"]), (False, "timeout"))

    def test_an_unreachable_device_stops_at_connect_and_keeps_an_older_profile(self):
        self.good_tools()
        self.check(now=lambda: 10.0)
        self.device.faults.fail(1)
        result = self.check(now=lambda: 20.0)
        self.assertEqual([s.name for s in result.steps], ["connect"])
        self.assertIn("连不上 gpu1", result.explain())
        self.assertIn("BatchMode", result.explain())
        profile = devices.read_profile(self.state, "gpu1")
        self.assertEqual((profile["check"]["ok"], profile["check"]["failed"], profile["check"]["at"]), (False, "connect", 20.0))
        self.assertIn("claude", profile["tools"], "what was learned before stays")
        self.assertIn("自检未通过", devices.status_text(self.state, "gpu1", now=30.0))

    def test_credentials_in_a_proxy_are_refused_and_only_whitelisted_names_come_in(self):
        self.good_tools()
        settings = self.device.home / ".claude"
        settings.mkdir()
        (settings / "settings.json").write_text(json.dumps({"env": {
            "HTTPS_PROXY": "http://user:secret@proxy.example:3128", "NO_PROXY": "localhost",
            "ANTHROPIC_API_KEY": "sk-not-a-real-key"}}))
        profile = self.check().profile
        self.assertEqual(profile["env"], {"NO_PROXY": "localhost"})
        self.assertEqual(sorted((r["name"], r["reason"]) for r in profile["env_rejected"]), [("HTTPS_PROXY", "credentials")])
        self.assertEqual(profile["settings_env_dropped"], 1)
        self.assertNotIn("secret", json.dumps(profile))
        self.assertNotIn("sk-not-a-real-key", json.dumps(profile))

    def test_a_missing_tool_is_told_by_name(self):
        self.tool("codex", 'case "$1" in --version) echo "codex-cli 0.1";; login) echo ok;; *) echo ok;; esac')
        with mock.patch.dict(self.device.transport.env, {"PATH": "%s:/usr/bin:/bin" % self.bin.name, "HOME": str(self.device.home)}):
            result = self.check()
        self.assertEqual(result.failed.name, "tools")
        self.assertIn("gpu1 上没装 claude。", result.explain())

    def test_the_check_touches_nothing_of_the_hub_but_its_own_records(self):
        self.good_tools()
        with DeviceGuard(self.state / "devices" / "gpu1" / "standin", device_root=self.device.home) as guard:
            self.check()
        guard.assert_untouched(self)


class CommandTest(DeviceEnv, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.start_devices(self.root)

    def run_main(self, *argv):
        out = io.StringIO()
        with mock.patch.object(device_check.sys, "stdout", out):
            code = device_check.main(["--state-dir", str(self.state), *argv])
        return code, out.getvalue()

    def test_check_runs_the_self_check_and_exits_by_its_result(self):
        passed = devices.CheckResult("gpu1", [devices.Step("connect", True)], profile={"tools": {}})
        with mock.patch.object(devices, "self_check", return_value=passed) as run:
            code, out = self.run_main("check", "gpu1")
        self.assertEqual(code, 0)
        self.assertIs(run.call_args.kwargs["probe"], devices.probe_harness)
        self.assertEqual(run.call_args.kwargs["state_dir"], self.state.resolve())
        self.assertIn("ok  connect", out)
        failed = devices.CheckResult("gpu1", [devices.Step("connect", False, "boom")])
        with mock.patch.object(devices, "self_check", return_value=failed):
            code, out = self.run_main("check", "gpu1", "--no-probe", "--json")
        self.assertEqual(code, 2)
        payload = json.loads(out)
        self.assertEqual((payload["ok"], payload["steps"][0]["detail"]), (False, "boom"))
        self.assertIn("连不上 gpu1", payload["text"])

    def test_off_and_unknown_aliases_exit_3_with_a_reason(self):
        code, out = self.run_main("check", "gpu9")
        self.assertEqual(code, 3)
        self.assertIn("不在 devices.json 里", out)
        with mock.patch.dict(os.environ, {"COVEY_DEVICES": "0"}):
            code, out = self.run_main("check", "gpu1")
        self.assertEqual(code, 3)
        self.assertIn("多设备是关的", out)

    def test_list_prints_the_last_check_of_each_device_and_asks_none(self):
        code, out = self.run_main("list")
        self.assertEqual(code, 0)
        self.assertIn("gpu1（GPU 1）· claude、codex：还没自检", out)
        self.assertIn("gpu2", out)

    def test_the_doctor_reports_devices_only_when_there_are_some(self):
        row = doctor.devices_row(self.state)
        self.assertEqual(row["status"], "unverified")
        self.assertEqual(sorted(row["devices"]), ["gpu1", "gpu2"])
        self.assertIn("还没自检", row["devices"]["gpu1"]["status"])
        devices.write_json(devices.device_dir(self.state, "gpu1") / "profile.json",
                           {"check": {"at": 1.0, "ok": True, "failed": "", "text": ""}})
        devices.write_json(devices.device_dir(self.state, "gpu2") / "profile.json",
                           {"check": {"at": 1.0, "ok": True, "failed": "", "text": ""}})
        self.assertEqual(doctor.devices_row(self.state)["status"], "ready")
        with mock.patch.dict(os.environ, {"COVEY_DEVICES": "0"}):
            self.assertIsNone(doctor.devices_row(self.state))


if __name__ == "__main__":
    unittest.main()
