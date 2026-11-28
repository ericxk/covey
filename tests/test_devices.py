"""Devices (covey/core/devices.py) and their transport (covey/core/device_transport.py).

Config, switch and workspace values need no machine at all. The transport is tested against the real shim run under
a temporary HOME (``LocalTransport``, through tests/support/fake_device.py); ssh is never started except as a fake
script that runs the remote command here. Each device test ends by reaping its tasks and failing on any process of
the device left running."""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from covey.core import device_transport as dt
from covey.core import device_probe, devices
from support import REPO, exec_tempdir
from support.fake_device import FakeDevice

ON = {"COVEY_DEVICES": "1"}


def config_text(**changes):
    """devices.json's text: the example of docs/configuration.md with ``changes`` applied to the top level."""
    document = {"schema": 1,
                "defaults": {"patience_minutes": 15, "waiting_patience_hours": 6, "connect_seconds": 5},
                "control_dir": "/dev/shm/covey-ssh-test",
                "devices": {"gpu1": {
                    "title": "GPU 1",
                    "ssh": {"host": "gpu1.example", "options": ["-J", "jump.example", "-p", "2222"], "python": "python3"},
                    "harnesses": ["claude", "codex"],
                    "env": {"HTTPS_PROXY": "http://proxy.example:3128"},
                    "login_shell": False,
                    "spaces": ["owner_private"]}}}
    document.update(changes)
    return json.dumps(document)


def short_dir(case):
    """A directory with a short path (the control directory's limit is 41 bytes), removed at cleanup."""
    root = "/dev/shm" if os.path.isdir("/dev/shm") and os.access("/dev/shm", os.W_OK) else "/tmp"
    path = tempfile.mkdtemp(prefix="cv", dir=root)
    case.addCleanup(shutil.rmtree, path, True)
    return Path(path)


def problems(text):
    with unittest.TestCase().assertRaises(devices.DeviceConfigError) as raised:
        devices.parse(text)
    return str(raised.exception)


class SwitchTest(unittest.TestCase):
    def test_only_one_turns_it_on_under_either_name(self):
        self.assertTrue(devices.enabled({"COVEY_DEVICES": "1"}))
        self.assertTrue(devices.enabled({"LARK_BRIDGE_DEVICES": "1"}))
        self.assertTrue(devices.enabled({"COVEY_DEVICES": " 1 "}))
        for value in ("", "0", "true", "yes", "2"):
            self.assertFalse(devices.enabled({"COVEY_DEVICES": value}), value)
        self.assertFalse(devices.enabled({}))
        self.assertFalse(devices.enabled({"COVEY_DEVICES": "0", "LARK_BRIDGE_DEVICES": "1"}), "the new name wins")

    def test_the_file_is_the_one_named_or_devices_json_in_the_data_home(self):
        with tempfile.TemporaryDirectory() as temp:
            self.assertEqual(devices.config_path({"COVEY_DATA_HOME": temp}), Path(temp).resolve() / "devices.json")
            self.assertEqual(devices.config_path({"LARK_BRIDGE_DATA_HOME": temp}), Path(temp).resolve() / "devices.json")
            self.assertEqual(devices.config_path({"COVEY_DATA_HOME": temp, "COVEY_DEVICES_FILE": "/absolute/d.json"}),
                             Path("/absolute/d.json"))
            self.assertEqual(devices.config_path({"LARK_BRIDGE_DEVICES_FILE": "/absolute/old.json"}),
                             Path("/absolute/old.json"))

    def test_off_nothing_is_read_made_or_started(self):
        with tempfile.TemporaryDirectory() as temp:
            broken = Path(temp) / "devices.json"
            broken.write_text("this is not json")
            state = Path(temp) / "state"
            threads = threading.active_count()
            environ = {"COVEY_DEVICES_FILE": str(broken), "COVEY_DATA_HOME": temp}
            with mock.patch.object(Path, "read_text", side_effect=AssertionError("a file was read")):
                config = devices.load(environ)
                self.assertFalse(config)
                self.assertEqual(config.devices, {})
                binding = devices.bind_workdir(SimpleNamespace(state_dir=state), "gpu1:/home/me/proj", environ=environ)
                self.assertEqual(devices.current(environ).devices, {})
            self.assertEqual((binding.device, binding.workdir, binding.value),
                             ("", Path("gpu1:/home/me/proj"), "gpu1:/home/me/proj"), "not parsed: an ordinary path")
            self.assertFalse(state.exists(), "no <state>/devices/ and no state directory")
            self.assertEqual(threading.active_count(), threads)
            self.assertEqual(sorted(path.name for path in Path(temp).iterdir()), ["devices.json"])

    def test_on_without_a_file_or_without_devices_is_the_same_as_off(self):
        with tempfile.TemporaryDirectory() as temp:
            missing = devices.load({**ON, "COVEY_DATA_HOME": temp})
            self.assertFalse(missing)
            self.assertIn("does not exist", missing.note)
            (Path(temp) / "devices.json").write_text(json.dumps({"schema": 1, "devices": {}}))
            empty = devices.load({**ON, "COVEY_DATA_HOME": temp})
            self.assertFalse(empty)
            self.assertEqual(empty.note, "no devices listed")
            binding = devices.bind_workdir(Path(temp) / "state", "/home/me/proj", environ={**ON, "COVEY_DATA_HOME": temp})
            self.assertEqual((binding.device, binding.workdir), ("", Path("/home/me/proj")))
            self.assertFalse((Path(temp) / "state").exists())

    def test_the_cached_configuration_follows_the_file(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "d.json"
            environ = {**ON, "COVEY_DEVICES_FILE": str(path)}
            path.write_text(config_text())
            first = devices.current(environ)
            self.assertIs(devices.current(environ), first)
            time.sleep(0.01)
            path.write_text(config_text(devices={"gpu2": {"ssh": {"host": "gpu2.example"}}}))
            self.assertEqual(list(devices.current(environ).devices), ["gpu2"])


class ConfigTest(unittest.TestCase):
    def test_the_example_is_read_with_its_defaults_filled_in(self):
        config = devices.parse(config_text())
        device = config.device("gpu1")
        self.assertEqual((device.title, device.host, device.python), ("GPU 1", "gpu1.example", "python3"))
        self.assertEqual(device.options, ("-J", "jump.example", "-p", "2222"))
        self.assertEqual((device.harnesses, device.spaces, device.login_shell), (("claude", "codex"), ("owner_private",), False))
        self.assertEqual(device.env, {"HTTPS_PROXY": "http://proxy.example:3128"})
        self.assertEqual((config.patience_minutes, config.waiting_patience_hours, config.connect_seconds), (15, 6, 5))
        self.assertEqual(config.control_dir, Path("/dev/shm/covey-ssh-test"))
        minimal = devices.parse(json.dumps({"schema": 1, "devices": {"a": {"ssh": {"host": "h"}}}})).device("a")
        self.assertEqual((minimal.title, minimal.harnesses, minimal.spaces, minimal.options, minimal.python),
                         ("a", ("claude", "codex"), ("owner_private",), (), "python3"))

    def test_a_device_may_name_its_home_which_is_read_as_written_and_never_resolved_here(self):
        entry = {"ssh": {"host": "h"}}
        parsed = devices.parse(json.dumps({"schema": 1, "devices": {
            "a": {**entry, "home": "~/covey-home"}, "b": {**entry, "home": "/data/me/home"}, "c": entry}}))
        self.assertEqual([devices.home_of(parsed.device(alias)) for alias in "abc"], ["~/covey-home", "/data/me/home", ""])
        for bad in ("relative/dir", 7, "~other/x", "/a\nb", " "):
            text = problems(json.dumps({"schema": 1, "devices": {"a": {**entry, "home": bad}}}))
            self.assertIn("devices.a.home: must be an absolute path on the device", text, repr(bad))

    def test_access_and_note_are_read_and_checked_so_a_file_a_later_release_wrote_still_parses(self):
        # T930 F0: ``access`` and ``note`` are known (read, never written here), so rolling back from the release that
        # writes them keeps devices.json readable; any other unknown key still fails the whole file.
        entry = {"ssh": {"host": "h"}}
        parsed = devices.parse(json.dumps({"schema": 1, "devices": {
            "a": {**entry, "access": "write", "note": " 主人的 GPU 机 "}, "b": {**entry, "access": "read", "note": "跑"
                                                                              + "测" * 59}, "c": entry}}))
        self.assertEqual([(parsed.device(alias).access, parsed.device(alias).note) for alias in "abc"],
                         [("write", "主人的 GPU 机"), ("read", "跑" + "测" * 59), ("", "")])
        for key, bad, words in (("access", "admin", "devices.a.access: must be write or read"),
                                ("access", ["read"], "devices.a.access: must be write or read"),
                                ("access", True, "devices.a.access: must be write or read"),
                                ("note", "x" * 61, "devices.a.note: must be one line of at most 60 characters"),
                                ("note", "第一行\n第二行", "devices.a.note: must be one line of at most 60 characters"),
                                ("note", 7, "devices.a.note: must be one line of at most 60 characters"),
                                ("owner", "me", "devices.a: unknown keys owner")):
            self.assertIn(words, problems(json.dumps({"schema": 1, "devices": {"a": {**entry, key: bad}}})), (key, bad))

    def test_a_device_declares_the_harnesses_that_run_on_a_device_claude_and_codex_unless_it_names_them(self):
        from covey.core.roster import DEVICE_HARNESSES, NAMES
        self.assertEqual(devices.harness_names(), DEVICE_HARNESSES)
        self.assertTrue({"claude", "codex", "agy"} <= set(DEVICE_HARNESSES))
        self.assertNotIn("dsh", DEVICE_HARNESSES)
        self.assertEqual(devices.DEFAULT_HARNESSES, ("claude", "codex"))  # What a device without the key meant before.
        entry = {"ssh": {"host": "h"}}
        named = devices.parse(json.dumps({"schema": 1, "devices": {"a": {**entry, "harnesses": ["agy", "claude"]}}}))
        self.assertEqual(named.device("a").harnesses, ("agy", "claude"))
        text = problems(json.dumps({"schema": 1, "devices": {"a": {**entry, "harnesses": ["claude", "dsh"]}}}))
        self.assertIn("devices.a.harnesses: must be a non-empty list from %s (dsh cannot run on a device)"
                      % ", ".join(DEVICE_HARNESSES), text)
        self.assertIn("dsh", NAMES)
        text = problems(json.dumps({"schema": 1, "devices": {"a": {**entry, "harnesses": ["gemini"]}}}))
        self.assertIn("must be a non-empty list from %s\n" % ", ".join(DEVICE_HARNESSES), text + "\n")
        self.assertNotIn("cannot run on a device", text)  # Not a Harness of this Bridge at all: no reason to give.

    def test_an_alias_is_a_lowercase_name_of_at_most_24_characters(self):
        entry = {"ssh": {"host": "h"}}
        for alias in ("gpu1", "a", "a-b-c", "x" * 24):
            devices.parse(json.dumps({"schema": 1, "devices": {alias: entry}}))
        for alias in ("GPU1", "1gpu", "gpu_1", "x" * 25, "gpu.1", "-gpu", ""):
            self.assertIn("alias must match", problems(json.dumps({"schema": 1, "devices": {alias: entry}})), alias)

    def test_every_problem_is_reported_not_only_the_first(self):
        text = problems(json.dumps({
            "schema": 2, "control_dir": "relative/dir", "defaults": {"patience_minutes": 0, "nonsense": 1},
            "devices": {"gpu1": {"ssh": {"host": "-oProxyCommand=x"}, "harnesses": ["claude", "gemini"],
                                 "spaces": ["everyone"], "env": {"HTTPS_PROXY": "http://user:pw@proxy.example:3128"},
                                 "login_shell": "yes", "unknown": 1},
                        "gpu2": {"title": "no ssh", "ssh": "gpu2.example"}}}))
        for expected in ("schema: must be 1", "control_dir: must be an absolute path", "defaults.patience_minutes",
                         "defaults: unknown key 'nonsense'", "devices.gpu1.ssh.host", "devices.gpu1.harnesses",
                         "devices.gpu1.spaces", "HTTPS_PROXY holds a login", "devices.gpu1.login_shell",
                         "devices.gpu1: unknown keys unknown", "devices.gpu2.ssh: must be {"):
            self.assertIn(expected, text)
        self.assertNotIn("pw@", text, "a password never appears in a message")

    def test_the_ssh_section_is_checked(self):
        def bad(ssh):
            return problems(config_text(devices={"gpu1": {"ssh": ssh}}))

        self.assertIn("ssh.host", bad({}))
        self.assertIn("ssh.host", bad({"host": "two words"}))
        self.assertIn("ssh.host", bad({"host": ""}))
        self.assertIn("ssh.options", bad({"host": "h", "options": "-J x"}))
        self.assertIn("ssh.options", bad({"host": "h", "options": ["-J", 5]}))
        self.assertIn("ssh.python", bad({"host": "h", "python": ""}))
        self.assertIn("unknown keys proxy", bad({"host": "h", "proxy": "x"}))
        for host in ("gpu1", "me@gpu1.example", "192.0.2.5", "fe80::1"):
            devices.parse(config_text(devices={"gpu1": {"ssh": {"host": host}}}))

    def test_control_dir_must_leave_room_for_a_unix_socket_path(self):
        limit = devices.CONTROL_DIR_MAX
        self.assertEqual(limit, 104 - (1 + 40 + 5 + 17), "104 bytes of sun_path less /<40 hex>.sock and ssh's temp suffix")
        fits = "/dev/shm/" + "d" * (limit - len("/dev/shm/"))
        self.assertEqual(len(fits), limit)
        devices.parse(config_text(control_dir=fits))
        self.assertIn("at most %d bytes" % limit, problems(config_text(control_dir=fits + "d")))
        self.assertIn("absolute", problems(config_text(control_dir="covey-ssh")))
        self.assertIn("must be a path", problems(config_text(control_dir="")))

    def test_the_default_control_dir_is_short_and_on_local_disk(self):
        chosen = devices.default_control_dir()
        self.assertEqual(chosen, Path("/tmp/covey-ssh-%d" % os.getuid()))
        self.assertLessEqual(len(os.fsencode(str(chosen))), devices.CONTROL_DIR_MAX)
        self.assertEqual(devices.parse(json.dumps({"schema": 1})).control_dir, chosen)

    def test_the_same_alias_twice_and_bad_json_are_refused(self):
        self.assertIn("more than once", problems('{"schema":1,"devices":{"a":{"ssh":{"host":"h"}},"a":{"ssh":{"host":"h"}}}}'))
        self.assertIn("not JSON", problems("{"))
        self.assertIn("must be a JSON object", problems("[]"))

    def test_load_names_the_file_in_its_errors(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "devices.json"
            path.write_text("{")
            with self.assertRaisesRegex(devices.DeviceConfigError, "devices.json"):
                devices.load(ON, path=path)

    def test_numbers_must_be_numbers_in_range(self):
        for key, value in (("patience_minutes", True), ("patience_minutes", "15"), ("connect_seconds", 0),
                           ("connect_seconds", 61), ("waiting_patience_hours", 169)):
            self.assertIn("defaults." + key, problems(config_text(defaults={key: value})), (key, value))
        config = devices.parse(config_text(defaults={"patience_minutes": 2.5}))
        self.assertEqual((config.patience_minutes, config.connect_seconds), (2.5, 5))


class BindWorkdirTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / "state"
        path = Path(self.temp.name) / "devices.json"
        path.write_text(config_text(devices={"gpu1": {"ssh": {"host": "h1"}}, "gpu2": {"ssh": {"host": "h2"}}}))
        self.environ = {**ON, "COVEY_DEVICES_FILE": str(path)}
        self.ctx = SimpleNamespace(state_dir=self.state)

    def bind(self, value):
        return devices.bind_workdir(self.ctx, value, environ=self.environ)

    def test_a_device_value_becomes_a_stand_in_that_does_not_exist(self):
        bound = self.bind("gpu1:/home/me/proj")
        self.assertEqual((bound.device, bound.remote_workdir, bound.value), ("gpu1", "/home/me/proj", "gpu1:/home/me/proj"))
        self.assertEqual(bound.workdir.parent, self.state / "devices" / "gpu1" / "standin")
        self.assertRegex(bound.workdir.name, r"^[0-9a-f]{12}$")
        self.assertFalse(bound.workdir.exists())
        self.assertFalse(self.state.exists(), "decoding makes no directory")
        self.assertEqual(bound.workdir, devices.standin(self.state, "gpu1", "/home/me/proj"))

    def test_a_device_with_a_home_still_reads_the_old_alias_path_values(self):
        path = Path(self.temp.name) / "devices.json"
        path.write_text(config_text(devices={"gpu1": {"ssh": {"host": "h1"}, "home": "~/home"}}))
        bound = self.bind("gpu1:/home/me/proj")
        self.assertEqual((bound.device, bound.remote_workdir), ("gpu1", "/home/me/proj"))
        self.assertTrue(devices.is_device_value("gpu1:~/work", self.environ))
        self.assertEqual(devices.home_of(devices.load(self.environ).device("gpu1")), "~/home")

    def test_the_value_round_trips_and_the_stand_in_is_stable(self):
        for value in ("gpu1:/home/me/proj", "gpu2:/srv/data", "gpu1:~/work", "gpu1:~", "gpu1:/"):
            first = self.bind(value)
            again = self.bind(first.value)
            self.assertEqual(again, first, value)
            self.assertEqual(self.bind(value).workdir, first.workdir)
        self.assertNotEqual(self.bind("gpu1:/a").workdir, self.bind("gpu1:/b").workdir)
        self.assertNotEqual(self.bind("gpu1:/a").workdir, self.bind("gpu2:/a").workdir, "the alias is part of the key")
        self.assertNotEqual(self.bind("gpu1:/a").workdir, devices.standin(self.state, "gpu1", "/a/"), "unnormalised differs")

    def test_equivalent_spellings_are_one_workspace(self):
        canonical = self.bind("gpu1:/home/me/proj")
        for spelling in ("gpu1:/home/me//proj/", "gpu1:/home/me/./proj", "gpu1:/home/me/x/../proj", " gpu1:/home/me/proj "):
            self.assertEqual(self.bind(spelling), canonical, spelling)
        self.assertEqual(self.bind("gpu1:~/a/../b").remote_workdir, "~/b")
        self.assertEqual(self.bind("gpu1:~/..").remote_workdir, "~", "cannot climb above the home")
        self.assertEqual(self.bind("gpu1:/..").remote_workdir, "/")
        self.assertEqual(self.bind("gpu1:").remote_workdir, "~", "an alias alone is the device's home")

    def test_local_values_and_look_alikes_stay_local(self):
        for value in ("/home/me/proj", "relative/dir", "gpu1", "GPU1:/x", "gpu1:relative", "C:/x", "gpu1:/a\nb", "~/x"):
            bound = self.bind(value)
            self.assertEqual((bound.device, bound.workdir, bound.value), ("", Path(value), value), value)

    def test_an_alias_that_is_not_listed_is_refused_loudly(self):
        with self.assertRaises(devices.UnknownDevice):
            self.bind("nope:/home/me")

    def test_the_context_may_be_the_state_directory_itself(self):
        self.assertEqual(devices.bind_workdir(self.state, "gpu1:/x", environ=self.environ).workdir,
                         devices.standin(self.state, "gpu1", "/x"))


class SshCommandTest(unittest.TestCase):
    def setUp(self):
        self.config = devices.parse(config_text())
        self.device = self.config.device("gpu1")

    def test_the_command_line_has_the_options_the_design_names_in_order(self):
        self.assertEqual(devices.ssh_argv(self.device, self.config, remote="python3 ~/.covey/shim/x/shim.py hello"), [
            "ssh", "-T", "-o", "BatchMode=yes", "-o", "ControlMaster=auto", "-o", "ControlPersist=600",
            "-o", "ControlPath=/dev/shm/covey-ssh-test/%C.sock", "-o", "ServerAliveInterval=15",
            "-o", "ConnectTimeout=5", "-o", "LogLevel=ERROR",
            "-J", "jump.example", "-p", "2222", "gpu1.example", "--", "python3 ~/.covey/shim/x/shim.py hello"])

    def test_the_connect_timeout_comes_from_the_defaults_and_ours_come_before_the_devices_options(self):
        config = devices.parse(config_text(defaults={"connect_seconds": 9}, devices={"d": {"ssh": {
            "host": "me@d.example", "options": ["-o", "BatchMode=no", "-i", "/absolute/key"]}}}))
        argv = devices.ssh_argv(config.device("d"), config)
        self.assertIn("ConnectTimeout=9", argv)
        self.assertLess(argv.index("BatchMode=yes"), argv.index("BatchMode=no"), "ssh keeps the first value of an option")
        self.assertEqual(argv[-1], "me@d.example")
        self.assertNotIn("--", argv)

    def test_a_control_request_replaces_the_command(self):
        argv = devices.ssh_argv(self.device, self.config, op="exit")
        self.assertEqual(argv[-3:], ["-O", "exit", "gpu1.example"][-3:])
        self.assertLess(argv.index("-p"), argv.index("-O"))
        with self.assertRaises(ValueError):
            devices.ssh_argv(self.device, self.config, op="forward")

    def test_the_transport_built_for_a_device_uses_it_and_makes_its_directories_on_first_use(self):
        with tempfile.TemporaryDirectory() as temp:
            control = short_dir(self) / "c"
            config = devices.parse(config_text(control_dir=str(control)))
            state = Path(temp) / "state"
            self.assertFalse(control.exists())
            transport = devices.open_transport(config, "gpu1", state, ssh="/absolute/ssh")
            self.assertEqual(stat.S_IMODE(control.stat().st_mode), 0o700)
            self.assertIsInstance(transport, dt.SshTransport)
            self.assertEqual(transport.ssh_argv("x")[0], "/absolute/ssh")
            self.assertEqual(transport.ssh_argv("x")[-2:], ["--", "x"])
            self.assertEqual((transport.alias, transport.shim, transport.python),
                             ("gpu1", dt.shim_id(dt.shim_bytes()), "python3"))
            self.assertEqual(transport.hub, (state / "devices" / "hub-id").read_text().strip())
            self.assertRegex(transport.hub, r"^[a-z0-9]{12}$")
            self.assertEqual(devices.hub_id(state), transport.hub, "one id per hub, kept")
            self.assertEqual(devices.open_transport(config, "gpu1", state).hub, transport.hub)
            with self.assertRaises(devices.UnknownDevice):
                devices.open_transport(config, "nope", state)

    def test_the_remote_command_runs_the_pushed_shim_with_the_devices_python(self):
        transport = dt.LocalTransport("/absolute/home", shim="0123456789abcdef", python="/opt/py thon/bin/python3")
        self.assertEqual(transport.remote("hello"), "'/opt/py thon/bin/python3' ~/.covey/shim/0123456789abcdef/shim.py hello")
        self.assertEqual(transport.remote("status", "/home/me/.covey/shim/old one/shim.py"),
                         "'/opt/py thon/bin/python3' '/home/me/.covey/shim/old one/shim.py' status")
        self.assertEqual(dt.word("~/a b"), "~/'a b'")
        self.assertEqual(dt.word("~"), "~")


class FakeSsh:
    """A fake ``ssh`` script that runs the remote command here under a given HOME, for the SshTransport glue."""

    def __init__(self, directory, home):
        self.log = Path(directory) / "ssh.log"
        self.path = Path(directory) / "ssh"
        self.path.write_text('#!%s\nimport json, os, subprocess, sys\n'
                             'args = sys.argv[1:]\n'
                             'open(%r, "a").write(json.dumps(args) + "\\n")\n'
                             'if "-O" in args:\n'
                             '    sys.exit(0 if args[args.index("-O") + 1] in ("check", "exit") else 1)\n'
                             'if "--" not in args:\n    sys.exit(2)\n'
                             'remote = args[args.index("--") + 1]\n'
                             'os.environ["HOME"] = %r\n'
                             'sys.exit(subprocess.call(["sh", "-c", remote]))\n' % (sys.executable, str(self.log), str(home)))
        self.path.chmod(0o755)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]


class DeviceCase(unittest.TestCase):
    """A fake device per test, cleaned up with a check for stray processes."""

    def setUp(self):
        self.device = FakeDevice()
        self.transport = self.device.transport
        self.addCleanup(self.finish)

    def finish(self):
        left = self.device.cleanup()
        self.assertEqual(left, [], "processes of the device were left running")

    def wait_until(self, predicate, timeout=20.0, what="the condition"):
        deadline = time.time() + timeout
        while time.time() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.05)
        self.fail("timed out waiting for %s" % what)

    def outs(self, frames, count, timeout=20.0):
        """Read frames until ``count`` out records came; every record read."""
        records, deadline = [], time.time() + timeout
        while len([r for r in records if r["k"] == "out"]) < count:
            frame = frames.read_frame(max(0.05, deadline - time.time()))
            self.assertIsNotNone(frame, "the connection ended early")
            if "o" in frame and frame.get("k") != "hb":
                records.append(frame)
        return records


class PushTest(DeviceCase):
    def setUp(self):
        self.device = FakeDevice(push=False)
        self.transport = self.device.transport
        self.addCleanup(self.finish)
        self.data = dt.shim_bytes()
        self.digest = dt.shim_id(self.data)
        self.target = self.device.home / ".covey" / "shim" / self.digest / "shim.py"

    def test_a_first_push_writes_the_file_named_by_its_hash_and_is_private(self):
        self.assertEqual(devices.push_file(self.transport, self.data), ("pushed", "~/.covey/shim/%s/shim.py" % self.digest))
        self.assertEqual(self.target.read_bytes(), self.data)
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o600)
        self.assertEqual([p.name for p in self.target.parent.iterdir()], ["shim.py"], "no temporary file left")
        self.assertEqual(self.transport.hello(("claude",))["shim"], self.digest, "the pushed copy is the one that answers")

    def test_a_file_that_is_there_is_not_pushed_again(self):
        devices.push_file(self.transport, self.data)
        before = self.target.stat()
        self.assertEqual(devices.push_file(self.transport, self.data)[0], "cached")
        self.assertEqual(devices.push_file(self.transport, self.data, ttl=0)[0], "present",
                         "past the ttl the device is asked; the answer is present")
        after = self.target.stat()
        self.assertEqual((after.st_ino, after.st_mtime_ns), (before.st_ino, before.st_mtime_ns), "not written again")

    def test_a_changed_file_goes_to_a_new_directory_and_the_old_one_stays_for_its_tasks(self):
        devices.push_file(self.transport, self.data)
        changed = self.data + b"\n# a newer shim\n"
        state, path = devices.push_file(self.transport, changed)
        self.assertEqual(state, "pushed")
        self.assertNotIn(self.digest, path)
        self.assertEqual(sorted(p.name for p in (self.device.home / ".covey" / "shim").iterdir()),
                         sorted([self.digest, dt.shim_id(changed)]))

    def test_a_truncated_copy_is_replaced(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_bytes(self.data[:100])
        self.assertEqual(devices.push_file(self.transport, self.data)[0], "pushed")
        self.assertEqual(self.target.read_bytes(), self.data)

    def test_other_files_are_named_by_their_own_hash(self):
        reader = b"print('reader')\n"
        state, path = devices.push_file(self.transport, reader, "workspace_reader.py")
        self.assertEqual((state, path), ("pushed", "~/.covey/shim/%s/workspace_reader.py" % dt.shim_id(reader)))
        self.assertEqual((self.device.home / ".covey" / "shim" / dt.shim_id(reader) / "workspace_reader.py").read_bytes(), reader)
        for bad in ("../x.py", "a b", ".hidden", "", "x/y"):
            with self.assertRaises(ValueError):
                devices.push_file(self.transport, reader, bad)

    def test_a_short_transfer_leaves_no_file_and_is_not_reported_as_pushed(self):
        script = devices.push_script(self.digest, "shim.py", len(self.data))
        code, out, err = self.transport.shell(script, self.data[:100])
        self.assertEqual((code, out.strip()), (1, "short"))
        self.assertFalse(self.target.exists())
        self.assertEqual([p.name for p in self.target.parent.iterdir()], [], "the temporary file is removed")
        code, out, err = self.transport.shell(script, self.data + b"trailing bytes that are never read")
        self.assertEqual(out.strip(), "pushed")
        self.assertEqual(self.target.read_bytes(), self.data)

    def test_a_push_that_cannot_write_says_so(self):
        (self.device.home / ".covey").write_text("a file where the directory should be")
        with self.assertRaises(dt.LinkError):
            devices.push_file(self.transport, self.data)


class LocalTransportTest(DeviceCase):
    def test_hello_says_what_the_device_is(self):
        hello = self.transport.hello(("claude", "no-such-tool"))
        self.assertEqual(hello["home"], str(self.device.home))
        self.assertEqual(hello["hub"], "hub1")
        self.assertEqual(hello["tools"]["no-such-tool"], {"found": False})
        self.assertEqual((hello["proto"], hello["min_proto"]), (1, 1))

    def test_a_shim_that_needs_a_newer_protocol_is_refused(self):
        with mock.patch.object(dt, "PROTO", 0):
            with self.assertRaises(dt.BadProto):
                self.transport.hello()

    def test_spawn_status_send_stop_and_reap(self):
        answer = self.device.spawn("t1", self.device.harness())
        self.assertFalse(answer["existed"])
        self.assertGreater(answer["pgid"], 1)
        self.assertTrue(self.device.spawn("t1", self.device.harness())["existed"], "spawn is idempotent by task")
        with self.assertRaises(dt.Conflict):
            self.device.spawn("t1", self.device.harness("--count", "1"))
        status = self.transport.call("status", task="t1")
        self.assertEqual(status["pgid"], answer["pgid"])
        self.assertEqual(self.transport.call("send", task="t1", seq=1, d="hello")["r"], "applied")
        self.assertEqual(self.transport.call("send", task="t1", seq=1, d="hello")["r"], "dup")
        with self.assertRaises(dt.Conflict):
            self.transport.call("send", task="t1", seq=1, d="other")
        link = self.transport.attach("t1")
        for frame in link:
            if frame.get("k") == "out" and "echo" in frame.get("d", ""):
                break
        else:
            self.fail("the input was not echoed")
        stopped = self.transport.call("stop", task="t1", grace=2, patience=30)
        self.assertTrue(stopped["exited"])
        reaped = self.transport.call("reap", task="t1", grace=1)
        self.assertEqual(reaped["after"], [], "nothing of the task is left")
        self.assertTrue(reaped["before"] is not None)
        with self.assertRaises(dt.Exited):
            self.transport.call("send", task="t1", seq=2, d="late")

    def test_attach_continues_from_the_processed_offset_after_the_connection_dies(self):
        self.device.spawn("t1", self.device.harness("--count", "40", "--delay", "0.02"))
        first = self.transport.attach("t1")
        self.assertEqual((first.att["from"], first.hi["proto"]), (0, 1))
        taken = self.outs(first, 8)
        self.assertEqual(self.device.kill_attaches(), 1)
        self.wait_until(lambda: not first.alive, what="the connection to end")
        processed = taken[-1]["e"]
        second = self.transport.attach("t1", processed)
        rest = []
        for frame in second:
            if "o" in frame and frame.get("k") != "hb":
                rest.append(frame)
        everything = taken + rest
        self.assertEqual(everything[-1]["k"], "exit")
        self.assertEqual([r["o"] for r in everything][1:], [r["e"] for r in everything][:-1], "no gap and no repeat")
        numbers = [json.loads(r["d"])["n"] for r in everything if r["k"] == "out" and '"n"' in r["d"]]
        self.assertEqual(numbers, list(range(40)))

    def test_attach_over_the_connection_sends_input_and_signals(self):
        self.device.spawn("t1", self.device.harness())
        link = self.transport.attach("t1")
        link.send({"op": "send", "seq": 1, "d": '{"say":"one"}'})
        replies = []
        for frame in link:
            replies.append(frame)
            if frame.get("k") == "ack":
                break
        self.assertIn(("r", "applied"), [(f.get("k"), f.get("r")) for f in replies])
        link.send({"op": "sig", "sig": "EOF"})
        records = [f for f in link if "o" in f and f.get("k") != "hb"]
        self.assertEqual(records[-1]["k"], "exit")
        self.assertEqual(records[-1]["code"], 0)

    def test_close_from_another_thread_ends_a_blocked_reader_and_is_repeatable(self):
        self.device.spawn("t1", self.device.harness("--stay"))
        link = self.transport.attach("t1")
        self.outs(link, 1)
        result = []
        reader = threading.Thread(target=lambda: result.append(link.read_frame(timeout=30)))
        reader.start()
        time.sleep(0.3)
        link.close()
        reader.join(10)
        self.assertFalse(reader.is_alive(), "the reader came back")
        self.assertEqual(result, [None])
        link.close()
        with self.assertRaises(dt.Closed):
            link.send({"op": "detach"})
        self.assertIsNone(link.read_frame(0.1))

    def test_a_read_that_waits_too_long_raises_timeout_and_heartbeats_are_frames(self):
        self.device.spawn("t1", self.device.harness("--stay"))
        link = self.transport.attach("t1")
        self.outs(link, 1)
        with self.assertRaises(TimeoutError):
            link.read_frame(0.3)
        self.assertEqual(link.read_frame(10)["k"], "hb")
        link.close()

    def test_errors_come_back_as_the_class_of_their_code(self):
        with self.assertRaises(dt.NoTask):
            self.transport.call("status", task="nothing")
        with self.assertRaises(dt.NoTask):
            self.transport.attach("nothing")
        self.device.spawn("t1", self.device.harness("--count", "1"))
        with self.assertRaises(dt.BadOffset):
            self.transport.attach("t1", 10 ** 9)
        with self.assertRaises(dt.NotFound):
            self.transport.get("~/missing")
        with self.assertRaises(dt.NotDir):
            self.transport.call("ls", path=str(self.device.task_dir("t1") / "spec.json"))
        with self.assertRaises(dt.BadRequest):
            self.transport.call("nonsense")
        try:
            self.transport.call("status", task="nothing")
        except dt.ShimError as exc:
            self.assertEqual((exc.code, type(exc).__name__, isinstance(exc, dt.DeviceError)), ("no_task", "NoTask", True))
            self.assertIn("nothing", exc.msg)

    def test_the_error_table_covers_every_code_of_the_shims_protocol_and_an_unknown_code_is_a_shim_error(self):
        codes = ["bad_request", "bad_proto", "no_task", "conflict", "exited", "spawn_failed", "bad_cwd", "bad_offset",
                 "not_found", "not_file", "not_dir", "permission", "too_large", "sha_mismatch", "no_pgid", "io",
                 "timeout", "gone", "internal"]
        self.assertEqual(sorted(dt.ERRORS), sorted(codes))
        for code in codes:
            error = dt.error_for(code, "text")
            self.assertEqual((error.code, error.msg, isinstance(error, dt.ShimError)), (code, "text", True))
            self.assertIs(type(error), dt.ERRORS[code])
        unknown = dt.error_for("from_the_future", "x")
        self.assertIs(type(unknown), dt.ShimError)
        self.assertEqual(unknown.code, "from_the_future")
        self.assertFalse(isinstance(dt.Unreachable("x"), dt.ShimError))
        self.assertTrue(issubclass(dt.Unreachable, dt.LinkError))

    def test_put_and_get_a_file_whole_in_segments_and_without_rewriting_the_same_content(self):
        target = str(self.device.home / "data" / "file.bin")
        data = os.urandom(1000)
        first = self.transport.put_file(target, data)
        self.assertEqual((first["same"], first["written"]), (False, 1000))
        self.assertEqual(self.transport.put_file(target, data)["same"], True)
        head, body = self.transport.get(target)
        self.assertEqual((body, head["size"], head["eof"]), (data, 1000, True))
        self.assertEqual(self.transport.get_file(target), data)
        self.assertEqual(self.transport.get_file("~/data/file.bin", segment=300), data, "~ is the device's home")
        segmented = os.urandom(2500)
        other = str(self.device.home / "data" / "big.bin")
        answer = self.transport.put_file(other, segmented, segment=1000, mode=0o640)
        self.assertEqual((answer["done"], answer["written"]), (True, 500))
        self.assertEqual(Path(other).read_bytes(), segmented)
        self.assertEqual(stat.S_IMODE(Path(other).stat().st_mode), 0o640)
        self.assertEqual(self.transport.get_file(other, segment=700), segmented)
        self.assertEqual(self.transport.put_file(other, b"")["written"], 0)
        self.assertEqual(self.transport.get_file(other), b"")
        with self.assertRaises(dt.TooLarge):
            self.transport.get_file(target, max_bytes=999)

    def test_a_put_of_text_with_newlines_and_a_get_of_binary_survive_the_framing(self):
        target = str(self.device.home / "odd.bin")
        data = b"line one\n\n{\"k\":\"hi\"}\n\xff\xfe\x00 end\n"
        self.transport.put_file(target, data)
        self.assertEqual(self.transport.get_file(target), data)

    def test_a_segment_past_the_limit_is_refused_before_anything_is_sent(self):
        with mock.patch.object(dt, "DATA_MAX", 10):
            with self.assertRaises(dt.TooLarge):
                self.transport.put("~/x", b"x" * 11)

    def test_ls_stat_and_exec_go_through_call(self):
        (self.device.home / "proj").mkdir()
        (self.device.home / "proj" / "a.txt").write_text("a")
        listing = self.transport.call("ls", path="~/proj")
        self.assertEqual([e["name"] for e in listing["entries"]], ["a.txt"])
        stats = self.transport.call("stat", paths=["a.txt", "missing"], root="~/proj")
        self.assertEqual([r["exists"] for r in stats["results"]], [True, False])
        done = self.transport.call("exec", argv=["sh", "-c", "echo hi; exit 3"], timeout=10, patience=30)
        self.assertEqual((done["code"], done["out"].strip()), (3, "hi"))

    def test_the_request_ids_are_distinct_and_the_hub_is_sent(self):
        first, second = self.transport.request("status", {}), self.transport.request("status", {})
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(first["hub"], "hub1")
        self.assertEqual(self.transport.request("reap", {"pgid": 5, "hub": None})["hub"], None)


class FramingTest(unittest.TestCase):
    def test_the_hi_frame_may_come_after_noise_but_not_after_too_much(self):
        hi = b'{"k":"hi","proto":1,"shim":"abc"}\n'
        frame, used = dt.scan_hi(bytearray(b"Welcome to the box\nmotd { not json\n{\"k\":\"other\"}\n" + hi + b'{"ok":true}\n'))
        self.assertEqual(frame, {"k": "hi", "proto": 1, "shim": "abc"})
        self.assertEqual(bytes(b"Welcome to the box\nmotd { not json\n{\"k\":\"other\"}\n" + hi)[used - len(hi):used], hi)
        self.assertEqual(dt.scan_hi(bytearray(b"half a line")), (None, 0))
        self.assertEqual(dt.scan_hi(bytearray(b"")), (None, 0))
        with self.assertRaises(dt.NoHi):
            dt.scan_hi(bytearray(b"x" * (dt.JUNK_MAX + 1)))
        with self.assertRaises(dt.NoHi):
            dt.scan_hi(bytearray((b"y" * 1000 + b"\n") * 70 + hi))

    def test_an_answer_is_parsed_after_the_hi_frame_with_its_body(self):
        body = b"abc\n\xff"
        import hashlib
        head = json.dumps({"ok": True, "len": len(body), "sha256": hashlib.sha256(body).hexdigest(), "eof": True})
        out = b"noise\n" + b'{"k":"hi","proto":1,"shim":"s"}\n' + head.encode() + b"\n" + body + b"\n"
        answer = dt.parse_answer(out, b"", 0, body=True)
        self.assertEqual((answer.hi["shim"], answer.head["len"], answer.body), ("s", 5, body))
        with self.assertRaises(dt.BadFrame):
            dt.parse_answer(out[:-3], b"", 0, body=True)
        with self.assertRaises(dt.BadFrame):
            dt.parse_answer(out[:-1] + b"X", b"", 0, body=True)
        tampered = out.replace(b"abc", b"abd")
        with self.assertRaises(dt.ShaMismatch):
            dt.parse_answer(tampered, b"", 0, body=True)

    def test_what_went_wrong_before_an_answer_is_told_by_the_exit_code(self):
        with self.assertRaises(dt.Unreachable) as raised:
            dt.parse_answer(b"", b"ssh: connect to host x port 22: Connection refused\n", 255)
        self.assertIn("Connection refused", str(raised.exception))
        with self.assertRaises(dt.NoHi):
            dt.parse_answer(b"python3: can't open file\n", b"", 2)
        with self.assertRaises(dt.NoHi):
            dt.parse_answer(b'{"k":"hi","proto":1}\n', b"", 0)
        with self.assertRaises(dt.BadFrame):
            dt.parse_answer(b'{"k":"hi","proto":1}\nnot json\n', b"", 0)
        with self.assertRaises(dt.BadFrame):
            dt.parse_answer(b'{"k":"hi","proto":1}\n[1]\n', b"", 0)
        with self.assertRaises(dt.NoTask):
            dt.parse_answer(b'{"k":"hi","proto":1}\n{"ok":false,"err":"no_task","msg":"x"}\n', b"", 0)

    def test_a_frame_is_one_ascii_line_within_the_limit(self):
        self.assertEqual(dt.encode({"cmd": "x", "d": "é\u2603"}), b'{"cmd":"x","d":"\\u00e9\\u2603"}\n')
        with mock.patch.object(dt, "LINE_MAX", 50):
            with self.assertRaises(dt.BadFrame):
                dt.encode({"d": "x" * 60})


class ScriptedTransport(dt.LocalTransport):
    """A transport whose command is a shell script of the test's, for what a real shim never does."""

    def __init__(self, script, **kwargs):
        super().__init__(tempfile.gettempdir(), **kwargs)
        self.script = script

    def _popen(self, remote, *, stdin):
        return subprocess.Popen(["sh", "-c", self.script], stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=True)


class ChannelFailureTest(unittest.TestCase):
    def assertGone(self, pid):
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
            except OSError:
                return
            time.sleep(0.05)
        self.fail("process %d is still running" % pid)

    def test_noise_before_the_hi_frame_is_dropped_for_calls_and_attaches(self):
        device = FakeDevice()
        self.addCleanup(self.finish, device)

        class Noisy(dt.LocalTransport):
            def remote(self, cmd, shim_path=None):
                return "echo 'Last login: yesterday'; echo 'motd {'; " + super().remote(cmd, shim_path)

        noisy = Noisy(device.home, hub="hub1")
        self.assertEqual(noisy.hello(("claude",))["home"], str(device.home))
        device.spawn("t1", device.harness("--stay"))
        link = noisy.attach("t1")
        try:
            self.assertEqual(link.hi["k"], "hi")
            self.assertEqual(link.read_frame(10)["k"], "out")
        finally:
            link.close()

    def finish(self, device):
        self.assertEqual(device.cleanup(), [])

    def test_more_than_64_kib_of_noise_means_no_hi_frame(self):
        transport = ScriptedTransport("head -c 70000 /dev/zero | tr '\\0' x; echo; echo '{\"k\":\"hi\",\"proto\":1}'; echo '{\"ok\":true}'")
        with self.assertRaises(dt.NoHi):
            transport.call("hello")

    def test_ssh_failing_to_connect_is_unreachable_and_carries_its_message(self):
        transport = ScriptedTransport("echo 'ssh: Could not resolve hostname gpu1' >&2; exit 255")
        with self.assertRaisesRegex(dt.Unreachable, "Could not resolve"):
            transport.call("hello")
        with self.assertRaises(dt.Unreachable):
            transport.attach("t")
        # On a fast machine ssh is gone before the attach request is written (CI): the broken pipe is judged by the
        # exit code too, not reported as a closed connection.
        with mock.patch.object(dt.Attachment, "send_raw", side_effect=dt.Closed("the attach connection is closed")):
            with self.assertRaisesRegex(dt.Unreachable, "Could not resolve"):
                transport.attach("t")
        self.assertEqual(transport.shell("true")[0], 255, "a plain script reports its exit code and leaves the judging to the caller")

    def test_a_missing_python_or_shim_is_no_hi(self):
        transport = ScriptedTransport("echo 'python3: can not open file shim.py' >&2; exit 2")
        with self.assertRaisesRegex(dt.NoHi, "can not open"):
            transport.call("hello")

    def test_a_call_that_outlasts_its_patience_is_killed(self):
        pidfile = short_dir(self) / "pid"
        transport = ScriptedTransport("echo $$ > %s; exec sleep 30" % pidfile)
        started = time.monotonic()
        with self.assertRaises(dt.LinkTimeout):
            transport.call("hello", patience=0.5)
        self.assertLess(time.monotonic() - started, 10)
        with self.assertRaises(dt.LinkTimeout):
            transport.attach("t", patience=0.5)
        self.assertGone(int(pidfile.read_text()))

    def test_garbage_after_the_attach_answer_is_a_bad_frame(self):
        transport = ScriptedTransport("echo '{\"k\":\"hi\",\"proto\":1}'; echo '{\"ok\":true,\"k\":\"att\",\"from\":0,\"size\":0}'; "
                                      "echo '{\"o\":0,\"e\":5,\"k\":\"out\",\"d\":\"x\"}'; echo 'not json'; sleep 5")
        link = transport.attach("t")
        try:
            self.assertEqual(link.read_frame(10)["k"], "out")
            with self.assertRaises(dt.BadFrame):
                link.read_frame(10)
        finally:
            link.close()

    def test_a_line_past_the_limit_and_a_half_line_at_the_end(self):
        script = ("echo '{\"k\":\"hi\",\"proto\":1}'; echo '{\"ok\":true,\"k\":\"att\"}'; "
                  "echo '{\"o\":0,\"e\":5,\"k\":\"out\",\"d\":\"x\"}'; printf '{\"o\":5,\"e\":9,\"k\":\"ou'")
        link = ScriptedTransport(script).attach("t")
        self.assertEqual(link.read_frame(10)["d"], "x")
        self.assertIsNone(link.read_frame(10), "a connection that ends in a half line lost it: the frame is not delivered")
        link.close()
        long_line = "echo '{\"k\":\"hi\",\"proto\":1}'; echo '{\"ok\":true}'; head -c 5000 /dev/zero | tr '\\0' x; echo"
        with mock.patch.object(dt, "LINE_MAX", 1000):
            link = ScriptedTransport(long_line).attach("t")
            try:
                with self.assertRaises(dt.BadFrame):
                    link.read_frame(10)
            finally:
                link.close()

    def test_the_attach_answer_that_is_an_error_closes_the_process(self):
        pidfile = short_dir(self) / "pid"
        transport = ScriptedTransport("echo $$ > %s; echo '{\"k\":\"hi\",\"proto\":1}'; "
                                      "echo '{\"ok\":false,\"err\":\"bad_offset\",\"msg\":\"m\"}'; exec sleep 30" % pidfile)
        with self.assertRaises(dt.BadOffset):
            transport.attach("t")
        self.assertGone(int(pidfile.read_text()))


class SshTransportTest(unittest.TestCase):
    """SshTransport through a fake ssh that runs the command here: the argv, the master requests and the glue."""

    def setUp(self):
        self.device = FakeDevice(push=False)
        self.addCleanup(self.finish)
        self.bin = exec_tempdir()
        self.addCleanup(self.bin.cleanup)
        self.fake = FakeSsh(self.bin.name, self.device.home)
        if not os.access(self.fake.path, os.X_OK):
            self.skipTest("no directory here where a script may be executed (run through scripts/test_map.py)")
        control = short_dir(self)
        self.config = devices.parse(config_text(control_dir=str(control / "c")))
        self.state = control / "state"
        self.transport = devices.open_transport(self.config, "gpu1", self.state, ssh=str(self.fake.path))
        self.transport.python = sys.executable

    def finish(self):
        self.assertEqual(self.device.cleanup(), [])

    def test_self_check_style_traffic_goes_through_the_ssh_command_line(self):
        state, _ = devices.push_file(self.transport, dt.shim_bytes())
        self.assertEqual(state, "pushed")
        hello = self.transport.hello(("claude",))
        self.assertEqual(hello["hub"], self.transport.hub)
        calls = self.fake.calls()
        self.assertTrue(all(call[call.index("--") - 1] == "gpu1.example" for call in calls), calls)
        self.assertTrue(all("ControlPath=%s/%%C.sock" % self.config.control_dir in call for call in calls))
        self.assertTrue(calls[-1][-1].endswith("shim.py hello"), calls[-1])

    def test_the_master_is_asked_and_ended_with_control_requests(self):
        self.assertTrue(self.transport.master_alive())
        self.transport.close()
        ops = [call[call.index("-O") + 1] for call in self.fake.calls() if "-O" in call]
        self.assertEqual(ops, ["check", "exit"])
        self.assertEqual(self.fake.calls()[0][-1], "gpu1.example")
        self.assertNotIn("--", self.fake.calls()[0])


class FaultInjectionTest(DeviceCase):
    def test_latency_delays_calls_and_frames(self):
        self.device.faults.latency = 0.4
        started = time.monotonic()
        self.transport.hello(("claude",))
        self.assertGreaterEqual(time.monotonic() - started, 0.4)
        self.device.faults.latency = 0.0

    def test_a_failed_call_raises_unreachable_and_the_next_one_works(self):
        self.device.faults.fail(2)
        with self.assertRaises(dt.Unreachable):
            self.transport.hello()
        with self.assertRaises(dt.Unreachable):
            self.transport.attach("t1")
        self.assertEqual(self.device.faults.failed, 2)
        self.assertEqual(self.transport.hello(("claude",))["hub"], "hub1")

    def test_dropped_frames_leave_a_gap_in_the_offsets_the_hub_can_see(self):
        self.device.spawn("t1", self.device.harness("--count", "12", "--delay", "0.05"))
        link = self.transport.attach("t1")
        records = self.outs(link, 2)
        self.device.faults.drop(2)
        records += [f for f in link if "o" in f and f.get("k") != "hb"]
        self.assertEqual(self.device.faults.dropped, 2)
        ends, starts = [r["e"] for r in records][:-1], [r["o"] for r in records][1:]
        gaps = [(e, o) for e, o in zip(ends, starts) if e != o]
        self.assertEqual(len(gaps), 1, "one stretch of records is missing: the offsets are not contiguous")
        self.assertEqual(records[-1]["k"], "exit")

    def test_killing_the_attaches_ends_them_like_a_dead_ssh(self):
        self.device.spawn("t1", self.device.harness("--stay"))
        link = self.transport.attach("t1")
        self.outs(link, 1)
        self.assertEqual(self.device.kill_attaches(), 1)
        self.assertFalse(link.alive)
        self.assertIsNone(link.read_frame(5))
        self.assertEqual(self.device.kill_attaches(), 0)
        self.assertEqual(self.transport.call("status", task="t1")["pgid"] > 1, True, "the task did not notice")


class ProfileTest(unittest.TestCase):
    def test_only_whitelisted_names_and_no_logins_are_accepted(self):
        kept, rejected = devices.accept_env({
            "HTTPS_PROXY": "http://proxy.example:3128", "no_proxy": "localhost,.example",
            "HTTP_PROXY": "http://user:secret@proxy.example:3128", "ALL_PROXY": "user:secret@proxy.example:1080",
            "SSL_CERT_FILE": "/etc/ssl/ca.pem", "ANTHROPIC_AUTH_TOKEN": "fake-token", "PATH": "/bin",
            "https_proxy": "http://proxy.example:3128\nHost: x"})
        self.assertEqual(kept, {"HTTPS_PROXY": "http://proxy.example:3128", "no_proxy": "localhost,.example",
                                "SSL_CERT_FILE": "/etc/ssl/ca.pem"})
        self.assertEqual(sorted((r["name"], r["reason"]) for r in rejected), sorted([
            ("HTTP_PROXY", "credentials"), ("ALL_PROXY", "credentials"), ("ANTHROPIC_AUTH_TOKEN", "name"),
            ("PATH", "name"), ("https_proxy", "value")]))
        self.assertNotIn("secret", json.dumps(rejected), "a rejected value is never kept")
        self.assertEqual(devices.accept_env(None), ({}, []))

    def test_the_profile_merges_process_then_settings_then_login_and_keeps_no_secret(self):
        config = devices.parse(config_text())
        hello = {"shim": "abc", "os": "Linux", "machine": "x86_64", "python": [3, 12, 4], "home": "/home/me",
                 "home_network": False, "settings_env_dropped": 2,
                 "proxy": {"HTTPS_PROXY": "http://process.example:1", "NO_PROXY": "localhost"},
                 "settings_env": {"HTTPS_PROXY": "http://settings.example:2", "HTTP_PROXY": "http://u:p@bad.example:3"},
                 "tools": {"claude": {"found": True, "path": "/p/claude", "version": "2.1", "login": "ok",
                                      "login_detail": "Logged in as someone@example.com"},
                           "codex": {"found": False}}}
        profile = devices.build_profile(config.device("gpu1"), hello, login_env={"HTTPS_PROXY": "http://login.example:3"},
                                        now=1000.0)
        self.assertEqual(profile["env"], {"HTTPS_PROXY": "http://login.example:3", "NO_PROXY": "localhost"})
        self.assertEqual(profile["env_rejected"], [{"name": "HTTP_PROXY", "reason": "credentials", "source": "settings"}])
        self.assertEqual(profile["tools"]["claude"], {"found": True, "path": "/p/claude", "version": "2.1", "login": "ok"})
        self.assertEqual(profile["tools"]["codex"], {"found": False})
        self.assertNotIn("u:p", json.dumps(profile))
        self.assertNotIn("someone@example.com", json.dumps(profile), "login detail text is not copied")
        self.assertEqual((profile["alias"], profile["at"], profile["shim"], profile["settings_env_dropped"]),
                         ("gpu1", 1000.0, "abc", 2))
        # devices.json's env is applied over the profile's.
        self.assertEqual(devices.effective_env(config.device("gpu1"), profile),
                         {"HTTPS_PROXY": "http://proxy.example:3128", "NO_PROXY": "localhost"})
        self.assertEqual(devices.effective_env(config.device("gpu1"), None), {"HTTPS_PROXY": "http://proxy.example:3128"})

    def test_env_lines_keep_only_the_whitelisted_names(self):
        text = "PATH=/bin\nHTTPS_PROXY=http://p.example:1\nFOO=bar\nbroken line\nno_proxy=a,b\nSSL_CERT_FILE=/x=y\n"
        self.assertEqual(devices.parse_env_lines(text), {"HTTPS_PROXY": "http://p.example:1", "no_proxy": "a,b",
                                                         "SSL_CERT_FILE": "/x=y"})

    def test_the_hello_cache_is_read_only_while_fresh(self):
        with tempfile.TemporaryDirectory() as temp:
            devices.write_hello(temp, "gpu1", {"os": "Linux"}, now=1000.0)
            self.assertEqual(devices.read_hello(temp, "gpu1", now=1100.0), {"os": "Linux"})
            self.assertIsNone(devices.read_hello(temp, "gpu1", now=1000.0 + devices.HELLO_TTL + 1))
            self.assertEqual(devices.read_hello(temp, "gpu1", max_age=None, now=10 ** 9), {"os": "Linux"})
            self.assertIsNone(devices.read_hello(temp, "gpu2"))
            (Path(temp) / "devices" / "gpu1" / "hello.json").write_text("{broken")
            self.assertIsNone(devices.read_hello(temp, "gpu1"))
            self.assertEqual(devices.read_profile(temp, "gpu2"), {})


class SelfCheckTest(unittest.TestCase):
    def setUp(self):
        self.device = FakeDevice(push=False)
        self.addCleanup(self.finish)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / "state"
        self.config = devices.parse(config_text(devices={"gpu1": {"ssh": {"host": "h"}, "harnesses": ["claude"]}}))

    def finish(self):
        self.assertEqual(self.device.cleanup(), [])

    def with_tools(self):
        bin_dir = exec_tempdir()
        self.addCleanup(bin_dir.cleanup)
        tool = Path(bin_dir.name) / "claude"
        tool.write_text("#!/bin/sh\necho 'claude 9.9.9 (fake)'\n")
        tool.chmod(0o755)
        if not os.access(tool, os.X_OK):
            self.skipTest("no directory here where a script may be executed (run through scripts/test_map.py)")
        self.device.transport.env["PATH"] = "%s:%s" % (bin_dir.name, self.device.transport.env["PATH"])

    def test_a_good_device_passes_every_step_and_leaves_its_records(self):
        self.with_tools()
        probes = []

        def probe(transport, device, harness, profile):
            probes.append((transport.alias, device.alias, harness, sorted(profile["env"])))
            return {"ok": True, "reason": "ok", "detail": ""}

        result = device_probe.self_check(self.config, "gpu1", state_dir=self.state, transport=self.device.transport,
                                    probe=probe, now=lambda: 5000.0)
        self.assertTrue(result.ok, result.lines())
        self.assertEqual([s.name for s in result.steps], ["connect", "shim", "hello", "profile", "tools", "probe:claude"])
        self.assertEqual(probes, [("local", "gpu1", "claude", [])])
        self.assertEqual(result.hello["shim"], dt.shim_id(dt.shim_bytes()))
        self.assertIn("claude 9.9.9", result.steps[4].detail)
        self.assertEqual(devices.read_hello(self.state, "gpu1", now=5001.0)["shim"], result.hello["shim"])
        profile = devices.read_profile(self.state, "gpu1")
        self.assertEqual((profile["tools"]["claude"]["version"], profile["probe"]["claude"]["ok"], profile["at"]),
                         ("claude 9.9.9 (fake)", True, 5000.0))
        self.assertEqual(len(result.lines()), 6)
        again = device_probe.self_check(self.config, "gpu1", state_dir=self.state, transport=self.device.transport)
        self.assertTrue(again.ok)
        self.assertIn("cached", again.steps[1].detail, "the shim is not pushed twice")
        self.assertTrue(again.steps[-1].skipped, "no probe supplied: skipped, not failed")
        self.assertFalse((self.state / "devices" / "hub-id").exists(), "a given transport needs no hub id")

    def test_a_device_that_cannot_be_reached_stops_at_the_first_step_and_writes_nothing(self):
        self.device.faults.fail(1)
        result = device_probe.self_check(self.config, "gpu1", state_dir=self.state, transport=self.device.transport)
        self.assertFalse(result.ok)
        self.assertEqual([s.name for s in result.steps], ["connect"])
        self.assertEqual(result.failed.name, "connect")
        self.assertIn("could not connect", result.failed.detail)
        self.assertFalse(self.state.exists())

    def test_a_missing_tool_fails_the_tools_step_but_the_profile_is_kept(self):
        real = self.device.transport.hello

        def without_claude(*args, **kwargs):
            return dict(real(*args, **kwargs), tools={"claude": {"found": False}})

        with mock.patch.object(self.device.transport, "hello", without_claude):
            result = device_probe.self_check(self.config, "gpu1", state_dir=self.state, transport=self.device.transport,
                                        probe=lambda *args: {"ok": False, "reason": "not_installed", "detail": "no claude"})
        self.assertFalse(result.ok)
        failed = [s.name for s in result.steps if not s.ok and not s.skipped]
        self.assertEqual(failed, ["tools", "probe:claude"])
        self.assertIn("not installed", result.steps[4].detail)
        self.assertIn("not_installed: no claude", result.steps[5].detail)
        self.assertEqual(devices.read_profile(self.state, "gpu1")["tools"]["claude"], {"found": False})

    def test_a_shim_that_is_not_the_one_pushed_is_a_failure(self):
        self.with_tools()
        with mock.patch.object(devices, "shim_id", return_value="0" * 16):
            result = device_probe.self_check(self.config, "gpu1", state_dir=self.state, transport=self.device.transport)
        self.assertEqual(result.failed.name, "hello")

    def test_a_login_shell_device_imports_the_login_shells_proxy(self):
        self.with_tools()
        config = devices.parse(config_text(devices={"gpu1": {"ssh": {"host": "h"}, "harnesses": ["claude"],
                                                              "login_shell": True}}))
        self.device.transport.env["SHELL"] = "/bin/sh"
        self.device.transport.env["ENV"] = ""
        result = device_probe.self_check(config, "gpu1", state_dir=self.state, transport=self.device.transport)
        self.assertTrue(result.ok, result.lines())
        self.assertEqual(result.profile["env"], {})
        self.assertEqual(result.steps[3].name, "profile")

    def test_a_check_needs_a_state_dir_or_a_transport_and_a_listed_device(self):
        with self.assertRaises(ValueError):
            device_probe.self_check(self.config, "gpu1")
        with self.assertRaises(devices.UnknownDevice):
            device_probe.self_check(self.config, "nope", transport=self.device.transport)


class ListTest(unittest.TestCase):
    """F1 (T930 5.2): devices.json as the settings page writes it: ``ssh`` optional (a machine only watched), ``access`` and ``note``
    written, the whole file checked and written back under its lock with what the change did not touch kept."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "devices.json"
        self.env = {**ON, "COVEY_DEVICES_FILE": str(self.path)}

    def test_an_entry_without_ssh_is_only_watched_never_a_device_to_run_on(self):
        config = devices.parse(json.dumps({"schema": 1, "devices": {
            "gpu1": {"ssh": {"host": "h"}, "access": "write", "note": "训练"}, "pi": {"title": "树莓派", "access": "read"}}}))
        self.assertEqual((list(config.devices), list(config.watched)), (["gpu1"], ["pi"]))
        self.assertEqual((config.watched["pi"].host, config.watched["pi"].title, config.devices["gpu1"].note), ("", "树莓派", "训练"))
        with self.assertRaises(devices.UnknownDevice):
            config.device("pi")  # Nothing opens a link to it, nothing binds a workspace on it.
        self.assertIn("devices.pi.ssh: must be {", problems(json.dumps({"schema": 1, "devices": {"pi": {"ssh": "h"}}})))

    def test_update_writes_the_whole_file_under_its_lock_and_keeps_what_it_did_not_touch(self):
        self.path.write_text(json.dumps({"schema": 1, "defaults": {"connect_seconds": 7}, "devices": {
            "gpu1": {"ssh": {"host": "h", "options": ["-p", "22"]}, "env": {"HTTPS_PROXY": "http://proxy.example:3128"},
                     "harnesses": ["claude"], "home": "~/work"}}}))
        locked = []
        real = devices.file_lock

        def counted(path, **kwargs):
            locked.append(Path(path).name)
            return real(path, **kwargs)
        with mock.patch.object(devices, "file_lock", side_effect=counted):
            config = devices.update(lambda raw: {**raw, "devices": {**raw["devices"], "gpu1": {**raw["devices"]["gpu1"],
                                                                                             "access": "write", "note": "训练机"}}},
                                    environ=self.env)
        self.assertEqual(locked, ["devices.json.lock"])
        raw = json.loads(self.path.read_text())
        self.assertEqual(raw["defaults"], {"connect_seconds": 7})
        self.assertEqual(raw["devices"]["gpu1"], {"ssh": {"host": "h", "options": ["-p", "22"]},
                                                  "env": {"HTTPS_PROXY": "http://proxy.example:3128"}, "harnesses": ["claude"],
                                                  "home": "~/work", "access": "write", "note": "训练机"})
        self.assertEqual((config.devices["gpu1"].access, config.devices["gpu1"].note), ("write", "训练机"))
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(devices.current(self.env).devices["gpu1"].note, "训练机")  # The next call reads it.

    def test_an_update_that_makes_the_file_invalid_or_a_file_that_cannot_be_read_writes_nothing(self):
        self.path.write_text(json.dumps({"schema": 1, "devices": {"gpu1": {"ssh": {"host": "h"}}}}))
        before = self.path.read_text()
        for bad in ({"access": "all"}, {"note": "x" * 61}, {"note": "两\n行"}, {"ssh": {"host": "-oProxyCommand=x"}}):
            with self.assertRaises(devices.Unwritable, msg=bad):
                devices.update(lambda raw, bad=bad: {**raw, "devices": {"gpu1": {**raw["devices"]["gpu1"], **bad}}}, environ=self.env)
            self.assertEqual(self.path.read_text(), before)
        self.path.write_text("{not json")
        with self.assertRaises(devices.Unwritable):
            devices.update(lambda raw: raw, environ=self.env)
        self.assertEqual(self.path.read_text(), "{not json")

    def test_a_missing_file_starts_as_an_empty_list(self):
        devices.update(lambda raw: {**raw, "devices": {"pi": {"title": "pi", "access": "read"}}}, environ=self.env)
        self.assertEqual(json.loads(self.path.read_text()), {"schema": 1, "devices": {"pi": {"title": "pi", "access": "read"}}})

    def test_an_ssh_address_is_split_as_devices_json_keeps_it_and_one_that_runs_something_here_is_refused(self):
        self.assertEqual(devices.split_ssh("me@box -p 2222 -J jump"), ("me@box", ["-p", "2222", "-J", "jump"]))
        self.assertEqual(devices.split_ssh("box"), ("box", []))
        for bad in ("", "a b", "-oProxyCommand=sh me@x", "me@x -o LocalCommand=id", "'unterminated", "-p 22"):
            with self.assertRaises(ValueError, msg=bad):
                devices.split_ssh(bad)
        device = devices.parse(json.dumps({"schema": 1, "devices": {"b": {"ssh": {"host": "me@box", "options": ["-p", "22"]}}}})).devices["b"]
        self.assertEqual(devices.ssh_text(device), "-p 22 me@box")
        self.assertEqual(devices.split_ssh(devices.ssh_text(device)), ("me@box", ["-p", "22"]))


class DeviceLookTest(unittest.TestCase):
    """What the 常用路径 page and ``covey repo use`` ask of a device: whether a path is a directory and a repository
    (``inspect_dir``), and the head of its rules (``read_head``), through the shim under a temporary HOME."""

    def setUp(self):
        self.device = FakeDevice()
        self.addCleanup(lambda: self.assertEqual(self.device.cleanup(), []))
        self.config = devices.parse(config_text(devices={"gpu1": {"ssh": {"host": "h"}}, "pi": {"title": "pi"}}))
        self.state = short_dir(self)
        patcher = mock.patch.object(devices, "open_transport", return_value=self.device.transport)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_directory_on_the_device_is_a_repository_with_git_and_a_plain_one_without(self):
        (self.device.home / "proj" / ".git").mkdir(parents=True)
        (self.device.home / "notes").mkdir()
        (self.device.home / "file.txt").write_text("x")
        look = lambda where: devices.inspect_dir(self.state, "gpu1", where, config=self.config)  # noqa: E731
        self.assertEqual(look(str(self.device.home / "proj")), ("ok", "git", ""))
        self.assertEqual(look(str(self.device.home / "notes")), ("ok", "dir", ""))
        self.assertEqual(look(str(self.device.home / "file.txt"))[0:2], ("missing", ""))
        self.assertEqual(look(str(self.device.home / "nowhere"))[0], "missing")
        self.assertEqual(devices.inspect_dir(self.state, "pi", "/x", config=self.config)[0], "unknown")  # Watched only: no ssh.
        self.device.faults.fail(1)
        self.assertEqual(look(str(self.device.home / "proj"))[0], "offline")

    def test_the_rules_head_is_read_with_the_shims_get_cut_at_a_line(self):
        proj = self.device.home / "proj"
        proj.mkdir()
        (proj / "CLAUDE.md").write_text("第一行\n" * 10)
        found = devices.read_head(self.state, "gpu1", str(proj), ["AGENTS.md", "CLAUDE.md"], limit=20, config=self.config)
        self.assertEqual(found[0], "CLAUDE.md")
        self.assertTrue(found[2] and found[1].endswith("\n") and len(found[1].encode()) <= 20)
        self.assertEqual(found[3], "")
        self.assertEqual(devices.read_head(self.state, "gpu1", str(proj), ["AGENTS.md"], config=self.config), ("", "", False, ""))


class CheckupTest(SelfCheckTest):
    """The settings page's 体检 (core/device_probe.py ``checkup``): ssh, the shim, hello, no Harness asked; the profile a turn reads
    keeps everything else it had."""

    def test_a_good_device_passes_three_steps_and_gets_a_first_profile(self):
        result = device_probe.checkup(self.config, "gpu1", state_dir=self.state, transport=self.device.transport, now=lambda: 7000.0)
        self.assertTrue(result.ok, result.lines())
        self.assertEqual([s.name for s in result.steps], ["connect", "shim", "hello"])
        self.assertIn("体检通过", result.explain())
        profile = devices.read_profile(self.state, "gpu1")
        self.assertEqual((profile["check"]["ok"], profile["check"]["steps"], profile["check"]["at"]), (True, 3, 7000.0))
        self.assertIn("体检通过", device_probe.status_text(self.state, "gpu1", now=7060.0))

    def test_a_checkup_keeps_the_profile_a_full_check_wrote(self):
        devices.device_dir(self.state, "gpu1").mkdir(parents=True)
        (devices.device_dir(self.state, "gpu1") / "profile.json").write_text(json.dumps({"env": {"HTTPS_PROXY": "http://p:1"}, "tools": {}}))
        device_probe.checkup(self.config, "gpu1", state_dir=self.state, transport=self.device.transport)
        profile = devices.read_profile(self.state, "gpu1")
        self.assertEqual(profile["env"], {"HTTPS_PROXY": "http://p:1"})
        self.assertTrue(profile["check"]["ok"])

    def test_a_device_that_cannot_be_reached_records_nothing_new(self):
        self.device.faults.fail(1)
        result = device_probe.checkup(self.config, "gpu1", state_dir=self.state, transport=self.device.transport)
        self.assertEqual((result.ok, result.failed.name), (False, "connect"))
        self.assertIn("连不上 gpu1", result.explain())
        self.assertFalse((self.state / "devices" / "gpu1" / "profile.json").exists())


class ModuleHygieneTest(unittest.TestCase):
    def test_the_transport_imports_nothing_of_covey_and_the_off_import_starts_nothing(self):
        source = (REPO / "covey" / "core" / "device_transport.py").read_text()
        self.assertNotIn("covey.", source.replace("covey/core", "").replace("~/.covey", "").replace("ensure", ""))
        code = ("import threading, sys\nbefore = threading.active_count()\nimport covey.core.devices\n"
                "assert threading.active_count() == before\nprint('ok')")
        done = subprocess.run([sys.executable, "-B", "-c", code], cwd=REPO, capture_output=True, text=True, timeout=60,
                              env={**os.environ, "PYTHONPATH": str(REPO)})
        self.assertEqual(done.stdout.strip(), "ok", done.stderr)


if __name__ == "__main__":
    unittest.main()
