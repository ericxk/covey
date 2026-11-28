"""The device channel (covey/core/device_access.py) and ``covey device`` (covey/core/device_cli.py) over a real device
program under a temporary HOME (core/device_transport.py LocalTransport): the guards G1-G7, the audit, the jobs,
and the turn's two ends in app/repos_turn.py. No ssh, no real device."""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from covey.app import repos_turn
from covey.core import device_access as access
from covey.core import device_cli, device_reap, devices
from covey.core import device_transport as dt
from covey.core.device_transport import LocalTransport
from covey.core.harness import LEAD, READ, TOOL_LIMIT
from covey.core.run_dir import DEVICE_TURN, DEVICES

TOPIC = "abcdef0123456789abcdef0123456789"
CONFIG = {"schema": 1, "devices": {
    "gpu1": {"title": "GPU 机", "ssh": {"host": "gpu1"}, "access": "write", "note": "训练",
             "env": {"PATH": "/usr/local/bin:/usr/bin:/bin", "COVEY_STATE": "/hub/state", "MY_TOKEN": "t0ken",
                     "LARK_APP": "x", "LANG_KEEP": "1"}},
    "peer": {"title": "同事的机器", "ssh": {"host": "peer"}, "access": "read"},
    "team": {"ssh": {"host": "team"}, "access": "write", "spaces": ["team"]},
    "plain": {"ssh": {"host": "plain"}},
    "watched": {"title": "只装了节点"}}}


class ChannelCase(unittest.TestCase):
    """A state directory with one task's run directory and a device HOME the device program runs in."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.state = self.root / "state"
        self.task = self.state / "runs" / "om_a-1"
        self.task.mkdir(parents=True)
        self.outputs = self.root / "outputs"
        self.device_home = self.root / "device"
        self.device_home.mkdir()
        self.config = devices.parse(json.dumps(CONFIG))
        self.links = {}
        self.addCleanup(self.finish)

    def finish(self):
        for link in self.links.values():
            for task in sorted((self.device_home / ".covey" / "tasks").glob("*/*")):
                with contextlib.suppress(dt.DeviceError):
                    link.call("reap", task=task.name, grace=0.5)
            break
        self.temp.cleanup()

    def connect(self, config, alias, state_dir):
        if alias not in self.links:
            link = LocalTransport(self.device_home, alias=alias, hub="hub1")
            devices.push_file(link, dt.shim_bytes())
            self.links[alias] = link
        return self.links[alias]

    def turn(self, *, limit="", space="owner_private"):
        access.note_turn(self.task, limit=limit, space=space, topic=TOPIC, outputs=str(self.outputs))
        return access.turn_from_env({"COVEY_STATE": str(self.state), "COVEY_TASK_DIR": str(self.task)})

    def channel(self, **turn):
        return access.Channel(self.turn(**turn), config=self.config, connect=self.connect)

    def audit(self):
        return access.read_audit(self.task)


class TurnTest(ChannelCase):
    def test_the_turn_file_says_the_limit_and_the_space_and_none_refuses_everything(self):
        turn = self.turn(limit=READ, space="personal_groups")
        self.assertEqual((turn.limit, turn.space, turn.topic, turn.outputs, turn.held, turn.known),
                         (READ, "personal_groups", TOPIC, str(self.outputs), True, True))
        (self.task / DEVICE_TURN).unlink()
        old = access.turn_from_env({"COVEY_STATE": str(self.state), "COVEY_TASK_DIR": str(self.task), "COVEY_TOPIC": TOPIC})
        self.assertFalse(old.known)
        channel = access.Channel(old, config=self.config, connect=self.connect)
        with self.assertRaisesRegex(access.Refused, "任务进程比设备通道旧"):
            channel.run("gpu1", ["true"])
        self.assertEqual({row["usable"] for row in channel.list()}, {False})

    def test_outside_a_task_is_the_hubs_terminal_and_without_a_state_nothing(self):
        terminal = access.turn_from_env({"COVEY_STATE": str(self.state)})
        self.assertEqual((terminal.task_dir, terminal.space, terminal.held), (None, access.TERMINAL_SPACE, False))
        self.assertEqual(access.mark_of(terminal), "")
        with self.assertRaises(access.Refused):
            access.turn_from_env({})

    def test_a_mark_is_the_topic_and_the_task(self):
        turn = self.turn()
        self.assertRegex(access.mark_of(turn), r"tabcdef01-[0-9a-f]{10}\Z")
        self.assertTrue(dt.NAME.match(access.mark_of(turn)) if hasattr(dt, "NAME") else True)


class GuardsTest(ChannelCase):
    def test_g1_only_listed_devices_in_the_chats_they_are_for(self):
        channel = self.channel()
        with self.assertRaisesRegex(access.Refused, "没有设备 nope"):
            channel.run("nope", ["true"])
        with self.assertRaisesRegex(access.Refused, "没有 ssh"):
            channel.ls("watched")
        with self.assertRaisesRegex(access.Refused, "不给这个聊天用"):
            channel.run("team", ["true"])
        proxy = self.channel(space="")
        with self.assertRaisesRegex(access.Refused, "代答群"):
            proxy.ls("gpu1")
        rows = {row["alias"]: row for row in self.channel(space="team").list()}
        self.assertEqual((rows["team"]["usable"], rows["gpu1"]["usable"], rows["watched"]["runs"]), (True, False, False))

    def test_g2_a_read_only_device_reads_and_runs_only_reading_commands(self):
        channel = self.channel()
        (self.device_home / "notes.txt").write_text("hello\n")
        self.assertEqual(channel.run("peer", ["cat", str(self.device_home / "notes.txt")])["out"], "hello\n")
        self.assertEqual(channel.run("plain", ["uname", "-s"])["code"], 0, "no access written: read-only")
        for argv in (["rm", "-f", "x"], ["sh", "-c", "ls"], ["git", "push"], ["find", ".", "-delete"],
                     ["cat", "/home/me/.ssh/id_ed25519"], ["tail", "-f", "log"]):
            with self.subTest(argv=argv), self.assertRaisesRegex(access.Refused, "只读"):
                channel.run("peer", argv)
        with self.assertRaisesRegex(access.Refused, "shell"):
            channel.run("peer", sh="ls")
        source = self.root / "local.txt"
        source.write_text("x")
        with self.assertRaisesRegex(access.Refused, "只读"):
            channel.put("peer", source, str(self.device_home / "y"))
        with self.assertRaisesRegex(access.Refused, "只读"):
            channel.job_start("peer", ["sleep", "1"])
        self.assertEqual(channel.get("peer", str(self.device_home / "notes.txt"))["bytes"], 6)
        self.assertEqual([item["name"] for item in channel.ls("peer", str(self.device_home))["entries"]],
                         [".covey", "notes.txt"])

    def test_g3_a_turn_held_to_reading_reads_every_device_only_and_gets_only_into_its_outputs(self):
        for limit in (READ, LEAD, "look"):  # "look": an older release's read-only first turn across an upgrade.
            with self.subTest(limit=limit):
                channel = self.channel(limit=limit)
                self.assertEqual(channel.run("gpu1", ["uname"])["code"], 0)
                with self.assertRaisesRegex(access.Refused, "这一轮只读"):
                    channel.run("gpu1", ["touch", "x"])
                with self.assertRaisesRegex(access.Refused, "这一轮只读"):
                    channel.job_start("gpu1", ["sleep", "1"])
                (self.device_home / "a.txt").write_text("a")
                self.assertTrue(channel.get("gpu1", str(self.device_home / "a.txt"))["path"].startswith(str(self.outputs)))
                with self.assertRaisesRegex(access.Refused, "产物目录"):
                    channel.get("gpu1", str(self.device_home / "a.txt"), str(self.root / "elsewhere.txt"))
        self.assertEqual({row["writable"] for row in self.channel(limit=READ).list()}, {False})

    def test_g5_the_device_gets_no_bridge_name_and_no_secret_and_get_refuses_secrets(self):
        channel = self.channel()
        found = channel.run("gpu1", ["env"])["out"]
        self.assertNotIn("COVEY_STATE=", found)
        self.assertNotIn("MY_TOKEN", found)
        self.assertNotIn("LARK_APP", found)
        self.assertIn("LANG_KEEP=1", found)
        hub = {"HOME": "/h", "PATH": "/p", "COVEY_STATE": "s", "LARK_BRIDGE_X": "1", "FEISHU_TOKEN": "t", "GITHUB_TOKEN": "t",
               "SSH_AUTH_SOCK": "/tmp/agent", "OPENAI_API_KEY": "k", "HTTPS_PROXY": "http://proxy:1"}
        self.assertEqual(access.ssh_env(hub), {"HOME": "/h", "PATH": "/p", "SSH_AUTH_SOCK": "/tmp/agent",
                                               "HTTPS_PROXY": "http://proxy:1"})
        for path in ("~/.ssh/id_rsa", "/srv/app/.env", "/srv/app/.env.local", "~/.codex/auth.json", "~/.config/gh/hosts.yml",
                     "/x/credentials.json", "/x/server.pem"):
            with self.subTest(path=path), self.assertRaisesRegex(access.Refused, "钥匙"):
                channel.get("gpu1", path)
        with self.assertRaisesRegex(access.Refused, "钥匙"):
            channel.put("gpu1", __file__, "~/.ssh/authorized_keys")

    def test_put_replaces_a_file_only_as_it_was_fetched_or_with_overwrite(self):
        channel = self.channel()
        remote = self.device_home / "proj" / "train.py"
        remote.parent.mkdir()
        remote.write_text("v1\n")
        remote.chmod(0o750)
        fetched = channel.get("gpu1", str(remote))
        local = Path(fetched["path"])
        local.write_text("v2\n")
        with self.assertRaisesRegex(access.Refused, "--write"):
            channel.put("gpu1", local, str(remote))
        remote.write_text("someone else\n")
        with self.assertRaisesRegex(access.Refused, "变了"):
            channel.put("gpu1", local, str(remote), if_sha=fetched["sha256"])
        self.assertEqual(remote.read_text(), "someone else\n")
        channel.put("gpu1", local, str(remote), overwrite=True)
        self.assertEqual((remote.read_text(), remote.stat().st_mode & 0o777), ("v2\n", 0o750), "its mode stays")
        again = channel.get("gpu1", str(remote))
        Path(again["path"]).write_text("v3\n")
        self.assertFalse(channel.put("gpu1", again["path"], str(remote), if_sha=again["sha256"])["same"])
        self.assertEqual(remote.read_text(), "v3\n")
        new = self.device_home / "proj" / "new.txt"
        channel.put("gpu1", again["path"], str(new))
        self.assertEqual(new.read_text(), "v3\n")


class AuditTest(ChannelCase):
    def test_every_call_is_a_line_and_a_run_starts_with_one(self):
        channel = self.channel()
        channel.run("gpu1", ["sh", "-c", "echo out; exit 4"], approved="q1")
        with contextlib.suppress(access.Refused):
            channel.run("peer", ["rm", "x"])
        channel.run("gpu1", ["echo", "--password=not-a-real-one-9f3"])
        rows = self.audit()
        runs = [row for row in rows if row["op"] == "run" and not row.get("refused")]
        self.assertEqual([row["phase"] for row in runs], ["start", "end", "start", "end"])
        self.assertEqual((runs[1]["code"], runs[1]["approved"], runs[1]["dir"]), (4, "q1", "~"))
        self.assertTrue(runs[1]["mark"])
        self.assertNotIn("not-a-real-one-9f3", json.dumps(rows))
        self.assertTrue(any(row.get("refused") and row["device"] == "peer" for row in rows))
        text = access.trace_text(rows)
        self.assertIn("gpu1：run `sh -c 'echo out; exit 4'`，退出 4", text)
        self.assertIn("peer：run 被拦下", text)

    def test_a_terminal_writes_its_audit_beside_the_devices_records(self):
        channel = access.Channel(access.turn_from_env({"COVEY_STATE": str(self.state)}), config=self.config,
                                 connect=self.connect)
        channel.run("gpu1", ["true"])
        self.assertTrue((self.state / "devices" / access.TERMINAL_AUDIT).is_file())
        self.assertFalse((self.task / DEVICES).exists())

    def test_fold_keeps_a_device_a_line_with_its_jobs_and_the_files_it_fetched(self):
        channel = self.channel()
        (self.device_home / "a.txt").write_text("a")
        got = channel.get("gpu1", str(self.device_home / "a.txt"))
        job = channel.job_start("gpu1", ["true"])["job"]
        folded = access.fold(self.audit())
        self.assertEqual(len(folded), 1)
        self.assertEqual((folded[0]["alias"], folded[0]["jobs"], folded[0]["files"]),
                         ("gpu1", [job], {str(self.device_home / "a.txt"): got["sha256"]}))
        again = access.fold(self.audit(), folded)
        self.assertEqual(again[0]["calls"], 2 * folded[0]["calls"])
        self.assertEqual(access.fetched_sha(None, folded, "gpu1", str(self.device_home / "a.txt")), got["sha256"])
        self.assertEqual(access.fetched_sha(self.task, [], "gpu1", str(self.device_home / "a.txt")), got["sha256"])


class ReapTest(ChannelCase):
    def cut_short(self, channel):
        """A run whose ``covey device`` was killed: its start line written, the command left running on the device."""
        mark = access.mark_of(channel.turn)
        link = self.connect(self.config, "gpu1", self.state)
        access.audit(channel.turn, {"device": "gpu1", "op": "run", "call": "c1", "phase": "start", "mark": mark})
        proc = link._popen(link.remote("exec"), stdin=__import__("subprocess").PIPE)
        proc.stdin.write(dt.encode(link.request("exec", {"argv": [sys.executable, "-c", "import time; time.sleep(60)"],
                                                        "mark": mark, "timeout": 120})))
        proc.stdin.flush()
        folder = self.device_home / ".covey" / "marks" / "hub1" / mark
        deadline = time.time() + 10
        while time.time() < deadline and not list(folder.glob("*.json")):
            time.sleep(0.05)
        record = json.loads(next(folder.glob("*.json")).read_text())
        proc.kill()
        proc.wait(5)
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            pipe.close()
        return record

    def test_the_turns_end_reaps_a_run_cut_short_and_only_that(self):
        channel = self.channel()
        channel.run("gpu1", ["true"])
        record = self.cut_short(channel)
        self.assertTrue(Path("/proc/%d" % record["pid"]).exists())
        self.assertEqual(access.unanswered(self.audit()), {"gpu1": access.mark_of(channel.turn)})
        found = access.reap_turn(self.state, self.task, config=self.config, connect=self.connect)
        self.assertEqual(found, {"gpu1": "reaped"})
        deadline = time.time() + 5
        while time.time() < deadline and Path("/proc/%d" % record["pid"]).exists():
            state = Path("/proc/%d/stat" % record["pid"]).read_text().rsplit(")", 1)[1].split()[0]
            if state in "ZX":
                break
            time.sleep(0.05)
        self.assertEqual(device_reap.entries(self.state, "gpu1"), {})

    def test_a_device_away_at_the_turns_end_is_left_to_the_devices_thread(self):
        channel = self.channel()
        record = self.cut_short(channel)

        def away(config, alias, state_dir):
            raise dt.Unreachable("no route")
        found = access.reap_turn(self.state, self.task, config=self.config, connect=away)
        self.assertEqual(found, {"gpu1": "pending"})
        entry = device_reap.entries(self.state, "gpu1")[access.MARK_KEY + access.mark_of(channel.turn)]
        self.assertEqual((entry["reason"], entry["mark"]), (device_reap.TURN_END, access.mark_of(channel.turn)))
        done, kept = device_reap.replay(self.state, "gpu1", self.connect(self.config, "gpu1", self.state))
        self.assertEqual((done, kept), ([access.MARK_KEY + access.mark_of(channel.turn)], []))
        self.assertIsNone(json.loads(json.dumps(record)).get("missing"))

    def test_a_turn_without_devices_reads_one_file_and_connects_nowhere(self):
        def never(*args):
            raise AssertionError("connected")
        self.assertEqual(access.reap_turn(self.state, self.task, connect=never), {})
        self.channel().run("gpu1", ["true"])
        self.assertEqual(access.reap_turn(self.state, self.task, config=self.config, connect=never), {})


class JobTest(ChannelCase):
    def test_a_job_outlives_its_call_its_log_reads_by_offset_and_stop_stops_its_group(self):
        channel = self.channel()
        started = channel.job_start("gpu1", ["sh", "-c", "echo one; echo two >&2; sleep 0.4; echo three"])
        self.assertRegex(started["job"], r"job-[0-9a-f]{10}\Z")
        deadline = time.time() + 15
        while time.time() < deadline and channel.job_status("gpu1", started["job"])["state"] != "exited":
            time.sleep(0.1)
        first = channel.job_log("gpu1", started["job"])
        self.assertEqual((first["text"], first["ended"], first["exit"]["code"]), ("one\ntwo\nthree\n", True, 0))
        rest = channel.job_log("gpu1", started["job"], first["next"])
        self.assertEqual((rest["text"], rest["next"]), ("", first["next"]))
        long = channel.job_start("gpu1", ["sleep", "60"], cwd=str(self.device_home))
        stopped = channel.job_stop("gpu1", long["job"])
        self.assertTrue(stopped["exited"])
        with self.assertRaisesRegex(access.Refused, "任务号"):
            channel.job_log("gpu1", "../x")

    def test_the_owner_stopping_the_turn_stops_the_jobs_it_started(self):
        channel = self.channel()
        job = channel.job_start("gpu1", ["sleep", "60"])["job"]
        self.assertEqual(access.reap_turn(self.state, self.task, config=self.config, connect=self.connect), {})
        self.assertEqual(channel.job_status("gpu1", job)["state"], "running", "a turn that ended keeps its jobs")
        found = access.reap_turn(self.state, self.task, stop_jobs=True, config=self.config, connect=self.connect)
        self.assertIn("stopped " + job, found["gpu1"])
        self.assertEqual(channel.job_status("gpu1", job)["state"], "exited")


class CommandLineTest(ChannelCase):
    def cli(self, *argv, task=True, on=True):
        env = {"COVEY_DEVICES": "1" if on else "0", "COVEY_DEVICES_FILE": str(self.root / "devices.json"), "COVEY_STATE": str(self.state),
               **({"COVEY_TASK_DIR": str(self.task)} if task else {})}
        (self.root / "devices.json").write_text(json.dumps(CONFIG))
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env), mock.patch.object(access, "_ssh_link", self.connect), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            devices._CACHE.clear()
            code = device_cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_run_passes_the_commands_output_and_exit_code_and_options_after_the_alias(self):
        self.turn()
        work = self.device_home / "w"
        work.mkdir()
        code, out, err = self.cli("run", "gpu1", "--dir", str(work), "--timeout", "30", "--", "sh", "-c", "pwd; exit 5")
        self.assertEqual((code, out.strip()), (5, str(work)))
        self.assertIn("退出 5", err)
        code, out, _ = self.cli("run", "gpu1", "--json", "--", "echo", "hi")
        self.assertEqual((code, json.loads(out)["out"]), (0, "hi\n"))
        code, _, err = self.cli("run", "gpu1", "--timeout", "1", "--", "sleep", "30")
        self.assertEqual(code, device_cli.TIMED_OUT)

    def test_a_refusal_is_exit_3_with_its_reason_and_the_switch_off_says_so(self):
        self.turn()
        code, _, err = self.cli("run", "peer", "--", "rm", "-rf", "/")
        self.assertEqual(code, device_cli.REFUSED)
        self.assertIn("只读", err)
        code, _, err = self.cli("list", on=False)
        self.assertEqual(code, device_cli.REFUSED)
        self.assertIn("COVEY_DEVICES", err)

    def test_list_get_put_and_a_job_from_the_command_line(self):
        self.turn()
        code, out, _ = self.cli("list")
        self.assertEqual(code, 0)
        self.assertIn("gpu1\tGPU 机\t训练\t可写", out)
        self.assertIn("peer\t同事的机器\t—\t只读", out)
        remote = self.device_home / "cfg.txt"
        remote.write_text("a\n")
        code, out, _ = self.cli("get", "gpu1", str(remote))
        local = self.outputs / "devices" / "gpu1" / "cfg.txt"
        self.assertEqual((code, local.read_text()), (0, "a\n"))
        local.write_text("b\n")
        code, out, err = self.cli("put", "gpu1", str(local), str(remote))
        self.assertEqual((code, remote.read_text()), (0, "b\n"), err)
        code, out, _ = self.cli("job", "start", "gpu1", "--dir", str(self.device_home), "--", "echo", "done")
        self.assertEqual(code, 0)
        job = out.split("任务 ")[1].split("（")[0]
        deadline = time.time() + 10
        while time.time() < deadline and "已结束" not in self.cli("job", "log", "gpu1", job)[2]:
            time.sleep(0.1)
        code, out, err = self.cli("job", "log", "gpu1", job)
        self.assertEqual((code, out), (0, "done\n"))
        self.assertIn("下次从偏移", err)

    def test_the_covey_command_forwards_device(self):
        from covey.core import home
        self.assertIn('device) shift; exec "{python}" -B "{bin}/covey_device.py"', home.SHIM_TEXT)


DANGEROUS = [
    ["rm", "-rf", "build"], ["rm", "-r", "x"], ["/bin/rm", "-Rf", "/"], ["rm", "--recursive", "x"], ["sudo", "rm", "-rf", "/opt/x"],
    ["sudo", "-u", "root", "rm", "-fr", "x"], ["env", "FOO=1", "rm", "-r", "x"], ["timeout", "10", "rm", "-rf", "x"],
    ["nohup", "kill", "123"], ["sh", "-c", "cd ~/p && rm -rf build"], ["bash", "-lc", "make; git push --force origin main"],
    ["/bin/sh", "-c", "echo $(rm -rf x)"], ["sh", "-c", "echo `pkill python`"], ["kill", "-9", "123"], ["pkill", "-f", "train.py"],
    ["killall", "python"], ["systemctl", "restart", "nginx"], ["systemctl", "--user", "stop", "x"],
    ["sudo", "systemctl", "disable", "--now", "x"], ["launchctl", "unload", "x.plist"], ["launchctl", "bootout", "gui/501/x"],
    ["reboot"], ["shutdown", "-h", "now"], ["poweroff"], ["docker", "rm", "-f", "c1"], ["docker", "stop", "c1"],
    ["docker", "system", "prune", "-af"], ["docker", "container", "rm", "x"], ["docker", "compose", "down"],
    ["docker-compose", "down"], ["podman", "rmi", "img"], ["git", "push", "-f"], ["git", "push", "origin", "+main"],
    ["git", "push", "--force-with-lease"], ["git", "push", "origin", ":old"], ["git", "push", "--delete", "origin", "x"],
    ["git", "reset", "--hard", "HEAD~1"], ["git", "-C", "/x", "reset", "--hard"], ["git", "clean", "-fdx"],
    ["git", "branch", "-D", "x"], ["apt", "remove", "x"], ["sudo", "apt-get", "purge", "-y", "x"], ["brew", "uninstall", "x"],
    ["pip", "uninstall", "-y", "torch"], ["python3", "-m", "pip", "uninstall", "x"], ["conda", "env", "remove", "-n", "x"],
    ["npm", "uninstall", "-g", "x"], ["yum", "reinstall", "x"], ["find", ".", "-name", "*.o", "-delete"],
    ["find", ".", "-exec", "rm", "-rf", "{}", ";"], ["xargs", "-I", "{}", "rm", "-rf", "{}"], ["sh", "-c", "ls | xargs rm -rf"],
    ["ssh", "other", "rm -rf /x"], ["eval", "rm -rf x"], ["mkfs.ext4", "/dev/sdb"], ["dd", "if=/dev/zero", "of=/dev/sda"],
    ["crontab", "-r"], ["kubectl", "delete", "pod", "x"], ["sh", "-c", "if true; then rm -rf x; fi"],
    ["sh", "-c", "for f in *; do rm -rf $f; done"], ["sh", "-ec", "FOO=1 sudo -E rm -rf x"], ["bash", "-c", "rm -rf 'unclosed"],
    ["watch", "-n", "1", "pkill", "x"], ["uv", "pip", "uninstall", "x"], ["pacman", "-Rns", "x"],
]
SAFE = [
    ["ls", "-la"], ["rm", "x.o"], ["rm", "-f", "x.o"], ["make", "test"], ["git", "push"], ["git", "push", "origin", "main"],
    ["git", "push", "-u", "origin", "feat"], ["git", "reset", "HEAD~1"], ["git", "clean", "-n"], ["git", "clean", "-nfd"],
    ["git", "status"], ["git", "branch", "-d", "x"], ["kill", "-0", "123"], ["kill", "-l"], ["pgrep", "python"],
    ["systemctl", "status", "nginx"], ["systemctl", "is-active", "x"], ["launchctl", "list"], ["docker", "ps"],
    ["docker", "run", "--rm", "img"], ["docker", "build", "-t", "x", "."], ["docker", "compose", "up", "-d"], ["apt", "install", "x"],
    ["pip", "install", "-e", "."], ["npm", "uninstall", "x"], ["npm", "install"], ["sh", "-c", "cd ~/p && make -j8 2>&1 | tail -n 50"],
    ["bash", "-lc", "python train.py > log.txt 2>&1"], ["find", ".", "-maxdepth", "2", "-name", "*.py"], ["sh", "-c", "echo rm -rf x"],
    ["python", "-c", "print(1)"], ["command", "-v", "rm"], ["nvidia-smi"], ["shutdown", "-c"], ["sh", "script.sh"],
    ["tar", "czf", "a.tgz", "dir"], ["sh", "-c", "grep -r foo . > out.txt"], ["service", "nginx", "status"], ["brew", "install", "x"],
]
OTHER_TOPIC = "0123456789abcdef0123456789abcdef"


class DangerTest(ChannelCase):
    """G4 (主人 D9D2G ``--approved``, T930 设计第 8 节): a dangerous ``run`` or ``job start`` is refused until the owner himself
    answered a question of this topic; ``--approved <答案号>`` is checked against the Bridge's question records."""

    def question(self, token, qid="wipe_build", *, topic=TOPIC, party="owner", status="answered", answers=("删吧（推荐）",),
                 kind="answer", channel="card"):
        record = {"token": token, "topic_id": topic, "chat_id": "oc_owner", "status": status, "created_at": time.time() - 60,
                  "questions": [{"id": "other", "question": "别的"}, {"id": qid, "question": "删掉 gpu1 上的 build？"}],
                  "answers": {"other": {"answers": ["随便"]}, qid: {"answers": list(answers)}},
                  "answer_by": {"party": party, "channel": channel} if party else {}, "response_kind": kind}
        if status == "answered":
            record["answered_at"] = time.time()
        folder = self.state / "questions"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{token}.json").write_text(json.dumps(record, ensure_ascii=False))
        return record

    def build(self):
        work = self.device_home / "proj"
        (work / "build").mkdir(parents=True, exist_ok=True)
        (work / "build" / "a.o").write_text("x")
        return work

    def test_danger_reads_through_wrappers_and_shells_and_lets_ordinary_commands_be(self):
        self.assertEqual([argv for argv in DANGEROUS if not access.danger(argv)], [])
        self.assertEqual([(argv, access.danger(argv)) for argv in SAFE if access.danger(argv)], [])
        self.assertEqual(access.danger(["sudo", "rm", "-rf", "/x"]), "rm -r（递归删除）")
        self.assertIn("git push", access.danger(["sh", "-c", "git push -f origin main"]))

    def test_a_dangerous_run_is_refused_until_the_owner_answered_and_runs_with_his_answer(self):
        work = self.build()
        ch = self.channel()
        with self.assertRaises(access.Refused) as caught:
            ch.run("gpu1", ["rm", "-rf", "build"], cwd=str(work))
        self.assertIn("先用问题卡问主人", str(caught.exception))
        self.assertIn("--approved", str(caught.exception))
        self.assertTrue((work / "build").is_dir())  # Never asked of the device.
        refused = self.audit()[-1]
        self.assertEqual((refused["op"], refused["danger"]), ("run", "rm -r（递归删除）"))
        self.question("a1b2c3d4e5f60718")
        found = ch.run("gpu1", ["rm", "-rf", "build"], cwd=str(work), approved="wipe_build")
        self.assertEqual(found["code"], 0)
        self.assertFalse((work / "build").exists())
        end = [row for row in self.audit() if row.get("phase") == "end"][-1]
        self.assertEqual({key: end["approved"][key] for key in ("answer", "question", "index", "id", "channel", "said", "danger")},
                         {"answer": "wipe_build", "question": "a1b2c3d4e5f60718", "index": 2, "id": "wipe_build", "channel": "card",
                          "said": "删吧（推荐）", "danger": "rm -r（递归删除）"})
        self.assertIn("主人已同意（wipe_build）", access.trace_text(self.audit()))

    def test_the_answer_number_may_be_the_records_token_or_token_and_index(self):
        work = self.build()
        self.question("a1b2c3d4e5f60718")
        ch = self.channel()
        for given in ("a1b2c3d4e5f60718-2", "a1b2c3d4e5f60718"):
            (work / "build").mkdir(exist_ok=True)
            self.assertEqual(ch.run("gpu1", ["rm", "-r", "build"], cwd=str(work), approved=given)["code"], 0)
        with self.assertRaisesRegex(access.Refused, "选了不回答"):
            self.question("b1b2c3d4e5f60718", answers=["用户选择不回答此题（不表示同意或授权）"])
            ch.run("gpu1", ["rm", "-r", "build"], cwd=str(work), approved="b1b2c3d4e5f60718-2")

    def test_a_forged_foreign_or_not_owners_answer_lets_nothing_run(self):
        work = self.build()
        ch = self.channel()
        cases = [
            ("no_such_question", None, "本话题里没有 id 是「no_such_question」的问题"),
            ("c1b2c3d4e5f60718", dict(topic=OTHER_TOPIC), "不是本话题的问题"),
            ("ffffffffffffffff", None, "没有答案号 ffffffffffffffff"),
            ("lead_said", dict(party="lead"), "不是主人本人答的（负责人答的）"),
            ("butler_said", dict(party="butler", channel="tool"), "不是主人本人答的（管家答的）"),
            ("member_said", dict(party=""), "不是主人本人答的（不是主人的人答的）"),
            ("waiting", dict(status="pending"), "主人还没答"),
            ("set_aside", dict(status="set_aside", kind="set_aside"), "主人还没答"),
            ("feedback", dict(kind="feedback"), "只写了补充说明"),
            ("skipped", dict(answers=["用户选择不回答此题（不表示同意或授权）"]), "选了不回答"),
        ]
        for number, (given, made, words) in enumerate(cases):
            with self.subTest(given=given):
                if made is not None:
                    token = given if access.QUESTION_TOKEN.fullmatch(given) else "%015x%d" % (number, number % 10)
                    self.question(token, given, **made)
                with self.assertRaises(access.Refused) as caught:
                    ch.run("gpu1", ["rm", "-rf", "build"], cwd=str(work), approved=given)
                self.assertIn(words, str(caught.exception))
                self.assertTrue((work / "build").is_dir())
                self.assertEqual(self.audit()[-1].get("approved"), given)

    def test_the_latest_owners_answer_of_the_id_counts_and_a_topics_terminal_has_none(self):
        work = self.build()
        self.question("d1b2c3d4e5f60718", party="lead")
        self.question("e1b2c3d4e5f60718")  # Answered later, by the owner.
        self.assertEqual(self.channel().run("gpu1", ["rm", "-rf", "build"], cwd=str(work), approved="wipe_build")["code"], 0)
        terminal = access.Channel(access.turn_from_env({"COVEY_STATE": str(self.state)}), config=self.config, connect=self.connect)
        with self.assertRaisesRegex(access.Refused, "不在话题的任务里"):
            terminal.run("gpu1", ["pkill", "-f", "x"], approved="wipe_build")
        self.assertEqual(terminal.run("gpu1", ["echo", "ok"])["out"], "ok\n")  # Nothing dangerous: as before.

    def test_a_dangerous_job_is_refused_too_and_a_read_only_device_refuses_it_before(self):
        ch = self.channel()
        with self.assertRaisesRegex(access.Refused, "先用问题卡问主人"):
            ch.job_start("gpu1", sh="sleep 1; systemctl restart nginx")
        self.assertEqual(self.audit()[-1]["danger"], "systemctl restart（停、重启或关掉服务）")
        self.question("f1b2c3d4e5f60718", "restart_it")
        started = ch.job_start("gpu1", ["sh", "-c", "kill -HUP 999999 || true"], approved="restart_it")
        self.assertTrue(started["job"].startswith("job-"))
        row = [row for row in self.audit() if row.get("op") == "job start" and row.get("job")][-1]
        self.assertEqual(row["approved"]["id"], "restart_it")
        with self.assertRaisesRegex(access.Refused, "只读"):
            ch.run("peer", ["rm", "-rf", "/tmp/x"], approved="restart_it")

    def test_the_command_line_says_how_to_ask_and_takes_approved_before_the_command(self):
        work = self.build()
        self.turn()
        cli = CommandLineTest.cli
        code, _, err = cli(self, "run", "gpu1", "--dir", str(work), "--", "rm", "-rf", "build")
        self.assertEqual(code, device_cli.REFUSED)
        self.assertIn("--approved <那道题的 id>", err)
        self.question("a9b2c3d4e5f60718")
        code, _, err = cli(self, "run", "gpu1", "--dir", str(work), "--approved", "wipe_build", "--", "rm", "-rf", "build")
        self.assertEqual(code, 0, err)
        self.assertFalse((work / "build").exists())
        code, out, err = cli(self, "job", "start", "gpu1", "--approved", "wipe_build", "--", "kill", "-HUP", "999999")
        self.assertEqual(code, 0, err)
        self.assertIn("已在 gpu1 上起任务", out)


class TurnEndsTest(ChannelCase):
    def ctx(self):
        return SimpleNamespace(state_dir=self.state, topic_id=TOPIC, device="", options={TOOL_LIMIT: LEAD},
                               workdir=self.root, default_workdir=self.root, history_path=self.state / "topics" / TOPIC / "h")

    def test_the_turns_start_writes_its_limit_and_its_chats_space_only_with_a_device_listed(self):
        job = SimpleNamespace(topic_id=TOPIC, chat_id="oc_owner", job_id=1)
        seen = SimpleNamespace(device_space=lambda chat: "owner_private" if chat == "oc_owner" else "")
        listing = self.root / "devices.json"
        listing.write_text(json.dumps({"schema": 1, "devices": {}}))
        for switch in ("0", "1"):  # Off, and on with no device: as before.
            with self.subTest(switch=switch), \
                    mock.patch.object(repos_turn.topic_repos, "task_dir_of", lambda state, j: self.task), \
                    mock.patch.dict(os.environ, {"COVEY_DEVICES": switch, "COVEY_DEVICES_FILE": str(listing)}):
                devices._CACHE.clear()
                repos_turn.start_devices(self.ctx(), job, seen)
                self.assertFalse(access.listed())
                self.assertFalse((self.task / DEVICE_TURN).exists())
        listing.write_text(json.dumps(CONFIG))
        with mock.patch.object(repos_turn.topic_repos, "task_dir_of", lambda state, j: self.task), \
                mock.patch.dict(os.environ, {"COVEY_DEVICES": "1", "COVEY_DEVICES_FILE": str(listing)}):
            devices._CACHE.clear()
            repos_turn.start_devices(self.ctx(), job, seen)
        written = json.loads((self.task / DEVICE_TURN).read_text())
        self.assertEqual((written["limit"], written["space"], written["topic"]), (LEAD, "owner_private", TOPIC))

    def test_the_turns_end_folds_the_audit_into_the_topic_and_notes_it_on_the_card(self):
        topic_dir = self.state / "topics" / TOPIC
        topic_dir.mkdir(parents=True)
        (topic_dir / "topic.json").write_text(json.dumps({"topic_id": TOPIC}))
        self.channel().run("gpu1", ["uname", "-s"])
        noted = []
        job = SimpleNamespace(topic_id=TOPIC, tool_trace=SimpleNamespace(note=lambda *a, **k: noted.append((a, k))))
        later = repos_turn.settle_devices(self.ctx(), job, self.task, later=True)
        self.assertTrue(callable(later))
        data = json.loads((topic_dir / "topic.json").read_text())
        self.assertEqual(data["devices"][0]["alias"], "gpu1")
        self.assertIn("gpu1：run `uname -s`，退出 0", noted[0][0][1])
        self.assertEqual(noted[0][1], {"title": "设备"})
        self.assertIsNone(repos_turn.settle_devices(self.ctx(), job, self.root / "none", later=True))


if __name__ == "__main__":
    unittest.main()
