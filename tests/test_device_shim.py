"""The device shim (covey/core/device_shim.py) run for real under a temporary HOME: real supervisors (double fork,
setsid), real signals, a scripted fake Harness as the child (tests/support/fake_harness.py). Nothing here touches the
real ~/.covey; each test ends by reaping its tasks and failing on any process of its device left running."""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from support import REPO, exec_tempdir
from support.shim_client import SHIM, DeviceShim

HARNESS = REPO / "tests" / "support" / "fake_harness.py"
PY = sys.executable


def load_compat():
    spec = importlib.util.spec_from_file_location("check_shim_compat", REPO / "scripts" / "check_shim_compat.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def decoded(record):
    """The bytes an out or cp record carries."""
    return base64.b64decode(record["b64"]) if "b64" in record else record.get("d", "").encode("utf-8")


class ShimCase(unittest.TestCase):
    """A fresh device (a temporary HOME) per test, cleaned up with a check for stray processes."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name).resolve()
        self.device = DeviceShim(self.home)
        self.addCleanup(self.finish)

    def finish(self):
        left = self.device.cleanup()
        self.temp.cleanup()
        self.assertEqual(left, [], "processes of the device were left running")

    def harness(self, *flags):
        return [PY, str(HARNESS), *flags]

    def state(self, task):
        return json.loads((self.device.task_dir(task) / "state.json").read_text())

    def journal(self, task):
        path = self.device.task_dir(task) / "journal"
        return [json.loads(line) for line in path.read_text().splitlines() if line]

    def lines_of(self, task):
        """The Agent's output lines (out records, pieces of a long line joined) as strings."""
        lines, part = [], b""
        for record in self.journal(task):
            if record["k"] == "out":
                part += decoded(record)
                if not record.get("more"):
                    lines.append(part.decode("utf-8", "replace"))
                    part = b""
        return lines

    def wait_until(self, predicate, timeout=20.0, what="the condition"):
        deadline = time.time() + timeout
        while time.time() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.05)
        self.fail("timed out waiting for %s" % what)

    def wait_echo(self, task, text, timeout=20.0):
        self.wait_until(lambda: any(text in line for line in self.lines_of(task)), timeout, "an echo of %s" % text)


class CommandLineTest(ShimCase):
    def test_the_first_line_is_the_hi_frame_and_its_id_is_the_files_hash(self):
        reply = self.device.call("hello", proto=1)
        digest = hashlib.sha256(SHIM.read_bytes()).hexdigest()[:16]
        self.assertEqual(reply.hi, {"k": "hi", "proto": 1, "shim": digest})
        self.assertTrue(reply.ok)
        self.assertEqual((reply["shim"], reply["proto"], reply["min_proto"]), (digest, 1, 1))

    def test_hello_reports_the_device(self):
        reply = self.device.call("hello", hub="hub1", want=["definitely-not-installed", "Bad Name"], proto=1)
        self.assertEqual(reply["home"], str(self.home))
        self.assertEqual(reply["python"][:2], list(sys.version_info[:2]))
        self.assertEqual(reply["tools"], {"definitely-not-installed": {"found": False}})
        self.assertTrue(reply["boot"])
        self.assertIsInstance(reply["home_network"], bool)
        self.assertGreater(reply["disk"]["free"], 0)
        self.assertAlmostEqual(reply["time"], time.time(), delta=60)

    def test_hello_lists_only_proxy_names_of_the_claude_settings_env(self):
        (self.home / ".claude").mkdir()
        (self.home / ".claude" / "settings.json").write_text(json.dumps(
            {"env": {"HTTPS_PROXY": "http://proxy.example:3128", "ANTHROPIC_AUTH_TOKEN": "fake-token-value"}}))
        reply = self.device.call("hello")
        self.assertEqual(reply["settings_env"], {"HTTPS_PROXY": "http://proxy.example:3128"})
        self.assertEqual(reply["settings_env_dropped"], 1)
        self.assertNotIn("fake-token-value", json.dumps(reply.head))

    def test_a_tool_on_the_path_is_found_with_its_version(self):
        with exec_tempdir() as bin_dir:  # /dev/shm is noexec: a tool to run is written where it may be run.
            tool = Path(bin_dir) / "fakeagent"
            tool.write_text("#!/bin/sh\necho 'fakeagent 9.9.9'\n")
            tool.chmod(0o755)
            if not os.access(tool, os.X_OK):
                self.skipTest("no directory here where a script may be executed (run through scripts/test_map.py)")
            self.device.env["PATH"] = "%s:%s" % (bin_dir, self.device.env["PATH"])
            info = self.device.call("hello", want=["fakeagent"])["tools"]["fakeagent"]
            self.assertEqual((info["found"], info["path"], info["version"]), (True, str(tool), "fakeagent 9.9.9"))

    def test_antigravitys_sign_in_is_a_file_under_the_home_looked_at_never_read(self):
        with exec_tempdir() as bin_dir:
            tool = Path(bin_dir) / "agy"
            tool.write_text("#!/bin/sh\necho '1.5.14'\n")
            tool.chmod(0o755)
            if not os.access(tool, os.X_OK):
                self.skipTest("no directory here where a script may be executed (run through scripts/test_map.py)")
            self.device.env["PATH"] = "%s:%s" % (bin_dir, self.device.env["PATH"])
            info = self.device.call("hello", want=["agy"])["tools"]["agy"]
            self.assertEqual((info["found"], info["version"], info["login"]), (True, "1.5.14", "no"))
            self.assertEqual(info["login_detail"], "~/.gemini/antigravity-cli/antigravity-oauth-token not found")
            token = self.home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
            token.parent.mkdir(parents=True)
            token.write_text("fake-sign-in-for-the-test")
            reply = self.device.call("hello", want=["agy"])
            self.assertEqual((reply["tools"]["agy"]["login"], reply["tools"]["agy"]["login_detail"]), ("ok", ""))
            self.assertNotIn("fake-sign-in-for-the-test", json.dumps(reply.head))

    def test_an_old_hub_protocol_and_bad_requests_are_answered_with_codes(self):
        self.assertEqual(self.device.call("hello", proto=0).err, "bad_proto")
        self.assertEqual(self.device.call("nonsense").err, "bad_request")
        self.assertEqual(self.device.call("send", hub="..", task="t", seq=1, d="x").err, "bad_request")
        mismatch = subprocess.run(self.device.argv("hello"), input=b'{"cmd":"status"}\n', stdout=subprocess.PIPE,
                                  env=self.device.env, timeout=30)
        self.assertEqual(json.loads(mismatch.stdout.splitlines()[1])["err"], "bad_request")
        garbage = subprocess.run(self.device.argv("hello"), input=b"this is not json\n", stdout=subprocess.PIPE,
                                 env=self.device.env, timeout=30)
        lines = garbage.stdout.splitlines()
        self.assertEqual(json.loads(lines[0])["k"], "hi")
        self.assertEqual(json.loads(lines[1])["err"], "bad_request")

    def test_a_task_that_is_not_there(self):
        self.assertEqual(self.device.call("status", task="nope").err, "no_task")
        self.assertEqual(self.device.call("send", task="nope", seq=1, d="x").err, "no_task")
        self.assertEqual(self.device.call("reap", task="nope").err, "no_task")
        self.assertEqual(self.device.call("reap", pgid=2 ** 22 - 7).err, "not_found")


class SpawnTest(ShimCase):
    def test_spawn_is_idempotent_by_task_and_a_different_spec_conflicts(self):
        argv = self.harness("--pidfile", str(self.home / "pid"))
        first = self.device.spawn("t1", argv)
        self.assertTrue(first.ok, first.head)
        self.assertFalse(first["existed"])
        again = self.device.spawn("t1", argv)
        self.assertTrue(again.ok)
        self.assertTrue(again["existed"])
        self.assertEqual((again["pgid"], again["pid"], again["child"]), (first["pgid"], first["pid"], first["child"]))
        other = self.device.spawn("t1", self.harness("--count", "3"))
        self.assertEqual(other.err, "conflict")
        self.assertEqual(len(self.lines_of("t1")), 1, "the Agent started once")
        self.assertEqual(int((self.home / "pid").read_text()), first["child"])

    def test_two_spawns_at_once_start_one_agent(self):
        argv = self.harness("--pidfile", str(self.home / "pid"))
        replies = []
        threads = [threading.Thread(target=lambda: replies.append(self.device.spawn("race", argv))) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        self.assertEqual([r.ok for r in replies], [True] * 3, [r.head for r in replies])
        self.assertEqual(sorted(r["existed"] for r in replies), [False, True, True])
        self.assertEqual(len({r["child"] for r in replies}), 1)

    def test_the_group_id_is_the_supervisors_own_not_its_pid(self):
        # Phase 0 E7: after the double fork the group leader (the first fork) is gone, so pid and pgid differ.
        reply = self.device.spawn("t1", self.harness())
        state = self.state("t1")
        self.assertEqual(state["pgid"], reply["pgid"])
        self.assertNotEqual(state["pgid"], state["pid"])
        self.assertEqual(os.getpgid(state["pid"]), state["pgid"])
        self.assertEqual(os.getsid(state["pid"]), state["pgid"], "setsid made a session of its own")
        self.assertEqual(os.getpgid(state["child"]), state["pgid"], "the Agent is in the supervisor's group")
        self.assertNotEqual(self.pid_parent(state["pid"]), os.getpid(), "the supervisor is nobody's child here")
        for key in ("boot", "proto", "shim", "spec_sha", "start", "child_start"):
            self.assertTrue(state[key], key)
        self.assertEqual(state["proto"], 1)
        self.assertEqual(state["spec_sha"], reply["spec_sha"])

    @staticmethod
    def pid_parent(pid):
        return int(Path("/proc/%d/stat" % pid).read_text().rsplit(")", 1)[1].split()[1])

    def test_the_agent_gets_cwd_env_and_the_task_dir_marker(self):
        work = self.home / "work"
        work.mkdir()
        self.device.spawn("t1", self.harness(), cwd=str(work), env={"FAKE_VAR": "yes", "HOME": None})
        ready = json.loads(self.wait_until(lambda: self.lines_of("t1"), what="the Agent's first line")[0])
        self.assertEqual(ready["cwd"], str(work))
        self.assertEqual(ready["task_dir"], str(self.device.task_dir("t1")))
        self.assertEqual(ready["var"], "yes")
        self.assertIsNone(ready["home"], "a null in env removes the variable")

    @unittest.skipUnless(shutil.which("bash"), "needs bash")
    def test_a_login_shell_wraps_the_command(self):
        self.device.spawn("t1", self.harness(), shell="login")
        self.wait_until(lambda: self.lines_of("t1"), what="the Agent behind bash -ic")
        self.assertIn("ready", json.loads(self.lines_of("t1")[0]))

    def test_seed_files_are_written_before_the_agent_starts(self):
        seed = self.home / "deep" / "seed.json"
        reply = self.device.spawn("t1", self.harness(), files=[
            {"path": str(seed), "text": "héllo"}, {"path": "incoming/blob.bin", "b64": base64.b64encode(b"\x00\xff").decode()}])
        self.assertTrue(reply.ok, reply.head)
        self.assertEqual(seed.read_text(encoding="utf-8"), "héllo")
        self.assertEqual((self.device.task_dir("t1") / "incoming" / "blob.bin").read_bytes(), b"\x00\xff")

    def test_a_spawn_that_cannot_start_says_why(self):
        self.assertEqual(self.device.spawn("t1", ["/no/such/binary"]).err, "spawn_failed")
        self.assertEqual(self.device.wait_exit("t1")["reason"], "spawn_failed")
        self.assertEqual(self.device.spawn("t2", self.harness(), cwd=str(self.home / "missing")).err, "bad_cwd")
        self.assertEqual(self.device.call("status", task="t2").err, "no_task", "nothing was left behind by a bad cwd")
        self.assertEqual(self.device.spawn("t3", []).err, "bad_request")
        self.assertEqual(self.device.spawn("t3", ["x"], env={"A=B": "1"}).err, "bad_request")
        self.assertEqual(self.device.call("spawn", task="t3", argv=["x"], shell="zsh").err, "bad_request")

    def test_an_agent_that_exits_leaves_exit_json_and_an_exit_record(self):
        self.device.spawn("t1", self.harness("--count", "2", "--delay", "0.01"))
        ended = self.device.wait_exit("t1")
        self.assertEqual((ended["code"], ended["reason"], ended["sig"]), (0, "exit", None))
        last = self.journal("t1")[-1]
        self.assertEqual((last["k"], last["code"]), ("exit", 0))
        self.assertEqual([json.loads(l).get("n") for l in self.lines_of("t1")[1:]], [0, 1])
        status = self.device.call("status", task="t1")
        self.assertEqual((status["state"], status["supervisor"]["alive"]), ("exited", False))

    def test_a_deadline_stops_the_agent(self):
        self.device.spawn("t1", self.harness(), deadline=time.time() + 1.0)
        ended = self.device.wait_exit("t1", 30)
        self.assertEqual((ended["reason"], ended["sig"]), ("deadline", 15))


class AttachTest(ShimCase):
    def test_attach_continues_from_an_offset_and_a_killed_attach_loses_and_repeats_nothing(self):
        self.device.spawn("t1", self.harness("--count", "60", "--delay", "0.02"))
        first = self.device.attach("t1")
        head = first.until(lambda f: f.get("k") == "att")
        self.assertTrue(head[-1]["ok"])
        taken = []
        while len([r for r in taken if r["k"] == "out"]) < 8:
            frame = first.next()
            if "o" in frame:
                taken.append(frame)
        first.kill()  # What ssh dying looks like from the device.
        processed = taken[-1]["e"]
        second = self.device.attach("t1", processed)
        rest = second.records()
        everything = taken + rest
        self.assertEqual(everything[-1]["k"], "exit")
        offsets = [(r["o"], r["e"]) for r in everything]
        self.assertEqual([o for o, _ in offsets][1:], [e for _, e in offsets][:-1], "records follow each other exactly")
        numbers = [json.loads(r["d"]).get("n") for r in everything if r["k"] == "out"]
        self.assertEqual([n for n in numbers if n is not None], list(range(60)))
        again = self.device.attach("t1", 0).records()
        self.assertEqual([(r["o"], r["e"], r["k"]) for r in again], [(r["o"], r["e"], r["k"]) for r in
                                                                         self.device.attach("t1", 0).records()])
        self.assertEqual(len(again), len(everything))

    def test_attach_to_an_offset_past_the_journal_or_a_missing_task_is_refused(self):
        self.device.spawn("t1", self.harness("--count", "1"))
        self.device.wait_exit("t1")
        frames = self.device.attach("t1", 10 ** 9)
        self.assertEqual(frames.until(lambda f: "ok" in f)[-1]["err"], "bad_offset")
        frames = self.device.attach("nothing")
        self.assertEqual(frames.until(lambda f: "ok" in f)[-1]["err"], "no_task")

    def test_the_hub_sends_input_and_signals_over_the_attach(self):
        self.device.spawn("t1", self.harness())
        frames = self.device.attach("t1")
        frames.send({"op": "send", "seq": 1, "d": '{"say":"one"}'})
        reply = frames.until(lambda f: f.get("k") == "r")[-1]
        self.assertEqual((reply["op"], reply["seq"], reply["r"], reply["ok"]), ("send", 1, "applied", True))
        frames.send({"op": "send", "seq": 1, "d": '{"say":"one"}'})
        self.assertEqual(frames.until(lambda f: f.get("k") == "r")[-1]["r"], "dup")
        frames.until(lambda f: f.get("k") == "ack" and f["seq"] == 1)
        frames.send({"op": "sig", "sig": "EOF"})  # The fake Agent leaves when its stdin ends.
        records = frames.records()
        self.assertEqual(records[-1]["k"], "exit")
        self.assertEqual(records[-1]["code"], 0)
        self.assertEqual(sum('"echo"' in line and "say" in line for line in self.lines_of("t1")), 1, "delivered once")

    def test_a_term_signal_over_the_attach_reaches_the_agent_group(self):
        self.device.spawn("t1", self.harness())
        frames = self.device.attach("t1")
        frames.send({"op": "sig", "sig": "TERM"})
        records = frames.records()
        self.assertEqual((records[-1]["k"], records[-1]["sig"]), ("exit", 15))
        self.device.spawn("t2", self.harness())
        frames = self.device.attach("t2")
        frames.send({"op": "sig", "sig": "BOGUS"})
        reply = frames.until(lambda f: f.get("k") == "r")[-1]
        self.assertEqual((reply["ok"], reply["err"]), (False, "bad_request"))
        self.assertEqual(self.device.call("status", task="t2")["state"], "running")

    def test_detach_and_a_closed_stdin_end_the_attach_process(self):
        self.device.spawn("t1", self.harness())
        frames = self.device.attach("t1")
        frames.until(lambda f: f.get("k") == "out")
        frames.send({"op": "detach"})
        self.assertEqual(frames.process.wait(15), 0)
        frames = self.device.attach("t1")
        frames.until(lambda f: f.get("k") == "out")
        frames.process.stdin.close()
        self.assertEqual(frames.process.wait(15), 0)
        self.assertEqual(self.device.call("status", task="t1")["state"], "running", "the Agent lives on")

    def test_an_exit_json_without_its_journal_record_is_replayed_as_a_synthetic_exit(self):
        self.device.spawn("t1", self.harness("--count", "1", "--delay", "0.01"))
        self.device.wait_exit("t1")
        journal = self.device.task_dir("t1") / "journal"
        data = journal.read_bytes()
        cut = data.rstrip(b"\n").rfind(b"\n") + 1
        journal.write_bytes(data[:cut])
        records = self.device.attach("t1", 0).records()
        self.assertEqual((records[-1]["k"], records[-1].get("synth"), records[-1]["code"]), ("exit", True, 0))
        self.assertEqual(records[-1]["o"], cut)

    def test_heartbeats_are_not_records(self):
        # A record frame has an offset; {"k":"hb"} never does, and is not written to the journal.
        self.device.spawn("t1", self.harness())
        frames = self.device.attach("t1")
        got = frames.until(lambda f: f.get("k") == "hb", timeout=15)
        self.assertIsNotNone(got, "a heartbeat within 15 seconds")
        self.assertNotIn("o", got[-1])
        # Each journal line is one record; no record is a heartbeat. (A substring check for "hb" failed when the random
        # temp directory name, which records carry, had those two letters: CI 3.11 on 8e07496.)
        kinds = [json.loads(line)["k"] for line in (self.device.task_dir("t1") / "journal").read_text().splitlines()
                 if line.strip()]
        self.assertTrue(kinds)
        self.assertNotIn("hb", kinds)


class SendTest(ShimCase):
    def test_send_is_deduplicated_by_seq(self):
        self.device.spawn("t1", self.harness())
        self.assertEqual(self.device.call("send", task="t1", seq=1, d="one")["r"], "applied")
        self.assertEqual(self.device.call("send", task="t1", seq=1, d="one")["r"], "dup")
        self.assertEqual(self.device.call("send", task="t1", seq=1, d="different").err, "conflict")
        self.wait_echo("t1", "one")
        self.assertEqual(self.device.call("send", task="t1", seq=1, d="one")["r"], "dup", "after delivery too")
        self.assertEqual(self.device.call("send", task="t1", seq=2, d="two")["r"], "applied")
        self.wait_echo("t1", "two")
        echoes = [json.loads(line)["echo"] for line in self.lines_of("t1") if "echo" in line]
        self.assertEqual(echoes, ["one", "two"])
        self.assertEqual((self.device.task_dir("t1") / "seq").read_text(), "2")
        status = self.device.call("status", task="t1")
        self.assertEqual((status["seq"], status["inbox_max"]), (2, 2))
        acks = [r["seq"] for r in self.journal("t1") if r["k"] == "ack"]
        self.assertEqual(acks, [1, 2])

    def test_inputs_reach_the_agent_in_seq_order_even_when_they_arrive_out_of_order(self):
        self.device.spawn("t1", self.harness())
        self.device.call("send", task="t1", seq=2, d="second")
        time.sleep(0.5)
        self.assertFalse(any("second" in line for line in self.lines_of("t1")), "2 waits for 1")
        self.device.call("send", task="t1", seq=1, d="first")
        self.wait_echo("t1", "second")
        echoes = [json.loads(line)["echo"] for line in self.lines_of("t1") if "echo" in line]
        self.assertEqual(echoes, ["first", "second"])

    def test_a_bad_input_and_an_input_to_an_ended_task(self):
        self.device.spawn("t1", self.harness("--count", "1", "--delay", "0.01"))
        self.device.wait_exit("t1")
        self.assertEqual(self.device.call("send", task="t1", seq=5, d="late").err, "exited")
        self.assertEqual(self.device.call("send", task="t1", seq=0, d="x").err, "bad_request")
        self.assertEqual(self.device.call("send", task="t1", seq=1, d="two\nlines").err, "bad_request")
        self.assertEqual(self.device.call("send", task="t1", seq=1, d=5).err, "bad_request")

    def test_a_line_longer_than_one_pipe_buffer_is_delivered_whole(self):
        self.device.spawn("t1", self.harness())
        text = "y" * 300000
        self.device.call("send", task="t1", seq=1, d=text)
        self.wait_echo("t1", "y" * 1000, 30)
        self.assertEqual(len(json.loads(self.wait_until(lambda: [l for l in self.lines_of("t1") if "echo" in l],
                                                          what="the echo")[0])["echo"]), 300000)


class StopAndReapTest(ShimCase):
    def test_stop_sends_term_to_the_group_and_the_agent_leaves(self):
        self.device.spawn("t1", self.harness("--pidfile", str(self.home / "pid"), "--grandchild"))
        self.wait_until(lambda: (self.home / "pid.child").exists(), what="the grandchild")
        grandchild = int((self.home / "pid.child").read_text())
        reply = self.device.call("stop", task="t1", grace=10)
        self.assertTrue(reply["exited"], reply.head)
        ended = self.device.wait_exit("t1")
        self.assertEqual((ended["reason"], ended["sig"]), ("stopped", 15))
        self.assertTrue(self.gone(grandchild), "a descendant in the group got the TERM too")
        self.assertEqual(self.journal("t1")[-1]["k"], "exit")

    def test_stop_escalates_to_kill_after_the_grace_when_the_agent_ignores_term(self):
        self.device.spawn("t1", self.harness("--ignore-term"))
        self.wait_until(lambda: self.lines_of("t1"), what="the Agent")
        began = time.time()
        reply = self.device.call("stop", task="t1", grace=1)
        took = time.time() - began
        self.assertTrue(reply["exited"], reply.head)
        ended = self.device.wait_exit("t1")
        self.assertEqual((ended["reason"], ended["sig"]), ("stopped", 9))
        self.assertGreaterEqual(took, 1.0, "TERM came first and the supervisor waited the grace")
        self.assertEqual(self.device.call("stop", task="t1")["exited"], True, "stop can be repeated")

    def test_stop_without_a_supervisor_stops_the_group_directly(self):
        self.device.spawn("t1", self.harness())
        os.kill(self.state("t1")["pid"], signal.SIGKILL)
        self.wait_until(lambda: self.device.call("status", task="t1")["state"] == "lost", what="state lost")
        reply = self.device.call("stop", task="t1", grace=2)
        self.assertEqual((reply["via"], reply["exited"], reply["after"]), ("reap", True, []))

    def test_reap_finds_leftover_members_by_the_task_marker_when_the_agent_and_supervisor_are_gone(self):
        # The fake Agent leaves when its stdin closes, so only the sleeping descendant is left in the group: it is
        # recognised as the task's by COVEY_TASK_DIR in its environment.
        self.device.spawn("t1", self.harness("--pidfile", str(self.home / "pid"), "--grandchild"))
        self.wait_until(lambda: (self.home / "pid.child").exists(), what="the grandchild")
        grandchild = int((self.home / "pid.child").read_text())
        os.kill(self.state("t1")["pid"], signal.SIGKILL)
        self.wait_until(lambda: self.gone(int((self.home / "pid").read_text())), what="the Agent to end on EOF")
        info = self.device.call("reap", task="t1", grace=2)
        self.assertEqual((info["before"], info["after"], info.get("refused")), ([grandchild], [], None))
        self.assertTrue(self.gone(grandchild))

    def test_reap_works_when_the_supervisor_is_already_dead_and_writes_exit_json(self):
        self.device.spawn("t1", self.harness("--pidfile", str(self.home / "pid"), "--grandchild", "--stay"))
        self.wait_until(lambda: (self.home / "pid.child").exists(), what="the grandchild")
        state = self.state("t1")
        os.kill(state["pid"], signal.SIGKILL)
        status = self.wait_until(lambda: (lambda s: s if s["state"] == "lost" else None)(
            self.device.call("status", task="t1")), what="the supervisor to be seen dead")
        self.assertTrue(status["group_alive"])
        self.assertFalse(status["supervisor"]["alive"])
        self.assertFalse((self.device.task_dir("t1") / "exit.json").exists())
        frames = self.device.attach("t1")
        gone = frames.until(lambda f: f.get("k") == "gone")[-1]
        self.assertEqual((gone["why"], gone["group_alive"]), ("supervisor_lost", True))
        info = self.device.call("reap", task="t1", grace=2)
        self.assertTrue(info.ok, info.head)
        self.assertIn(state["child"], info["before"])
        self.assertEqual(info["after"], [])
        self.assertTrue(info["wrote_exit"])
        exit_json = json.loads((self.device.task_dir("t1") / "exit.json").read_text())
        self.assertEqual((exit_json["reason"], exit_json["reaped"]), ("reaped", True))
        self.assertEqual(self.journal("t1")[-1]["k"], "exit")
        self.assertTrue(self.gone(state["child"]))
        self.assertTrue(self.gone(int((self.home / "pid.child").read_text())))
        again = self.device.call("reap", task="t1")
        self.assertEqual((again.ok, again["before"], again["after"]), (True, [], []), "reap can be repeated")
        self.assertNotIn("wrote_exit", again.head)

    def test_reap_by_group_id_and_with_a_live_supervisor(self):
        self.device.spawn("t1", self.harness("--ignore-term"))
        self.wait_until(lambda: self.lines_of("t1"), what="the Agent")
        state = self.state("t1")
        info = self.device.call("reap", pgid=state["pgid"], hub=self.device.hub, grace=1)
        self.assertTrue(info.ok, info.head)
        self.assertEqual(info["via"], "supervisor")
        self.assertEqual(info["after"], [])
        ended = self.device.wait_exit("t1")
        self.assertEqual((ended["reason"], ended["sig"]), ("stopped", 9))
        self.assertNotIn("wrote_exit", info.head, "the supervisor wrote it")

    def test_reap_never_kills_a_group_that_is_not_the_tasks(self):
        # state.json names a group id that now belongs to someone else (the id was recycled): refuse.
        decoy = subprocess.Popen(["sleep", "300"], start_new_session=True)
        self.addCleanup(lambda: (decoy.kill(), decoy.wait()))
        tdir = self.device.task_dir("ghost")
        tdir.mkdir(parents=True)
        (tdir / "spec.json").write_text('{"spec_sha":"x","created":1}')
        (tdir / "state.json").write_text(json.dumps({"pid": decoy.pid + 100000, "start": "1", "pgid": decoy.pid,
                                                     "child": 99999999, "boot": self.device.call("hello")["boot"]}))
        info = self.device.call("reap", task="ghost", grace=1)
        self.assertEqual((info["refused"], info["after"]), ("identity", [decoy.pid]))
        self.assertIsNone(decoy.poll(), "the decoy still runs")
        self.assertFalse((tdir / "exit.json").exists())

    def test_a_rebooted_device_is_told_by_the_boot_id(self):
        self.device.spawn("t1", self.harness())
        state = self.state("t1")
        tampered = dict(state, boot="boot-before-the-restart")
        (self.device.task_dir("t1") / "state.json").write_text(json.dumps(tampered))
        status = self.device.call("status", task="t1")
        self.assertEqual((status["state"], status["rebooted"], status["supervisor"]["alive"]), ("lost", True, False))
        frames = self.device.attach("t1")
        gone = frames.until(lambda f: f.get("k") == "gone")[-1]
        self.assertEqual(gone["why"], "rebooted")
        info = self.device.call("reap", task="t1")
        self.assertEqual(info["refused"], "identity", "another boot's group ids mean nothing")
        (self.device.task_dir("t1") / "state.json").write_text(json.dumps(state))

    @staticmethod
    def gone(pid):
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                if Path("/proc/%d/stat" % pid).read_text().rsplit(")", 1)[1].split()[0] in "ZX":
                    return True
            except OSError:
                return True
            time.sleep(0.05)
        return False


class CheckpointTest(ShimCase):
    def cp_records(self, task):
        return [r for r in self.journal(task) if r["k"] == "cp"]

    def test_a_changed_checkpoint_becomes_a_cp_record_and_the_seed_does_not(self):
        cp = self.home / "cp" / "checkpoint.json"
        self.device.spawn("t1", self.harness("--checkpoint", str(cp)), files=[{"path": str(cp), "text": "seed"}],
                          checkpoint={"path": str(cp), "poll": 0.2})
        self.device.call("send", task="t1", seq=1, d=json.dumps({"cp": "first"}))
        records = self.wait_until(lambda: self.cp_records("t1"), what="a cp record")
        self.assertEqual((records[0]["d"], records[0]["size"]), ("first", 5))
        self.device.call("send", task="t1", seq=2, d=json.dumps({"cp": "second, longer"}))
        records = self.wait_until(lambda: len(self.cp_records("t1")) >= 2 and self.cp_records("t1"), what="the 2nd")
        self.assertEqual([r["d"] for r in records], ["first", "second, longer"])
        cp.unlink()
        gone = self.wait_until(lambda: len(self.cp_records("t1")) >= 3 and self.cp_records("t1"), what="the removal")
        self.assertTrue(gone[-1]["gone"])

    def test_the_checkpoint_is_compared_once_more_after_the_agent_exits(self):
        cp = self.home / "checkpoint.json"
        # A poll interval longer than the test: only the comparison after the exit can notice the write.
        self.device.spawn("t1", self.harness("--checkpoint", str(cp)), checkpoint={"path": str(cp), "poll": 60})
        self.device.call("send", task="t1", seq=1, d=json.dumps({"cp": "written at the very end"}))
        self.device.call("send", task="t1", seq=2, d=json.dumps({"exit": 0}))
        self.device.wait_exit("t1")
        kinds = [r["k"] for r in self.journal("t1")]
        self.assertIn("cp", kinds)
        self.assertLess(kinds.index("cp"), kinds.index("exit"), "the cp record comes before exit")
        self.assertEqual(self.cp_records("t1")[0]["d"], "written at the very end")

    def test_a_checkpoint_over_the_limit_is_only_marked_and_binary_goes_through_b64(self):
        cp = self.home / "checkpoint.json"
        self.device.spawn("t1", self.harness(), checkpoint={"path": str(cp), "poll": 0.2})
        cp.write_bytes(b"\xff\xfe binary")
        record = self.wait_until(lambda: self.cp_records("t1"), what="the binary cp")[0]
        self.assertEqual(base64.b64decode(record["b64"]), b"\xff\xfe binary")
        cp.write_bytes(b"x" * (1 << 20))
        records = self.wait_until(lambda: len(self.cp_records("t1")) >= 2 and self.cp_records("t1"), what="1 MiB")
        self.assertEqual(len(records[1]["d"]), 1 << 20, "exactly the limit is still carried")
        cp.write_bytes(b"x" * ((1 << 20) + 1))
        records = self.wait_until(lambda: len(self.cp_records("t1")) >= 3 and self.cp_records("t1"), what="over")
        self.assertTrue(records[2]["over"])
        self.assertNotIn("d", records[2])
        self.assertEqual(records[2]["size"], (1 << 20) + 1)


class OutputTest(ShimCase):
    def test_output_that_is_not_utf8_goes_as_b64_and_text_as_d(self):
        self.device.spawn("t1", self.harness("--binary", "--count", "1", "--delay", "0.01"))
        self.device.wait_exit("t1")
        outs = [r for r in self.journal("t1") if r["k"] == "out"]
        binary = [r for r in outs if "b64" in r]
        self.assertEqual([base64.b64decode(r["b64"]) for r in binary], [b"\xff\xfe\x80 not utf-8"])
        self.assertTrue(all("d" in r for r in outs if "b64" not in r))
        self.assertTrue(all(isinstance(r["t"], float) for r in outs))

    def test_a_very_long_line_is_split_in_pieces_the_hub_can_join(self):
        size = 5 * (1 << 20)
        self.device.spawn("t1", self.harness("--big", str(size), "--count", "1", "--delay", "0.01"))
        self.device.wait_exit("t1")
        records = self.device.attach("t1", 0).records(60)
        pieces = [r for r in records if r["k"] == "out" and len(decoded(r)) > 1000]
        self.assertEqual([bool(r.get("more")) for r in pieces], [True, True, False])
        self.assertEqual(sum(len(decoded(r)) for r in pieces), size)
        self.assertTrue(all((r["e"] - r["o"]) < (16 << 20) for r in records))

    def test_err_log_stops_at_one_mebibyte(self):
        self.device.spawn("t1", self.harness("--stderr", str(1500000), "--count", "1", "--delay", "0.01"))
        self.device.wait_exit("t1")
        size = (self.device.task_dir("t1") / "err.log").stat().st_size
        self.assertGreater(size, 1 << 20)
        self.assertLess(size, (1 << 20) + 200)
        self.assertIn("full", (self.device.task_dir("t1") / "err.log").read_text()[-200:])


class FileTest(ShimCase):
    def test_put_and_get_with_sha256_and_without_rewriting_the_same_content(self):
        target = self.home / "a" / "b" / "file.bin"
        data = bytes(range(256)) * 40
        digest = hashlib.sha256(data).hexdigest()
        reply = self.device.call("put", data=data, path=str(target), len=len(data), sha256=digest)
        self.assertEqual((reply.ok, reply["same"], reply["done"]), (True, False, True), reply.head)
        self.assertEqual(target.read_bytes(), data)
        self.assertEqual(oct(target.stat().st_mode & 0o777), "0o600")
        self.assertFalse(list(target.parent.glob("*part*")))
        before = target.stat().st_mtime_ns
        time.sleep(0.02)
        same = self.device.call("put", data=data, path=str(target), len=len(data), sha256=digest)
        self.assertEqual((same["same"], same["written"]), (True, 0))
        self.assertEqual(target.stat().st_mtime_ns, before, "same content is not rewritten")
        got = self.device.call("get", path=str(target))
        self.assertEqual((got["len"], got["size"], got["eof"], got["sha256"]), (len(data), len(data), True, digest))
        self.assertEqual(got.body, data)

    def test_a_put_whose_hash_is_wrong_writes_nothing(self):
        target = self.home / "bad.bin"
        reply = self.device.call("put", data=b"payload", path=str(target), len=7, sha256="0" * 64)
        self.assertEqual(reply.err, "sha_mismatch")
        self.assertFalse(target.exists())
        self.assertFalse(list(self.home.glob("bad.bin*")))
        short = subprocess.run(self.device.argv("put"), env=self.device.env, stdout=subprocess.PIPE, timeout=60,
                               input=json.dumps({"cmd": "put", "path": str(target), "len": 100,
                                                 "sha256": "0" * 64}).encode() + b"\nonly a few bytes")
        self.assertEqual(json.loads(short.stdout.splitlines()[1])["err"], "io", "stdin closed in the body")
        self.assertFalse(target.exists())

    def test_big_files_move_in_segments_by_offset_and_limit(self):
        data = os.urandom(300000)
        whole = hashlib.sha256(data).hexdigest()
        target = self.home / "big.bin"
        step = 100000
        for offset in range(0, len(data), step):
            part = data[offset:offset + step]
            reply = self.device.call("put", data=part, path=str(target), len=len(part), offset=offset,
                                     total=len(data), total_sha256=whole, sha256=hashlib.sha256(part).hexdigest())
            self.assertTrue(reply.ok, reply.head)
            self.assertEqual(reply["done"], offset + step >= len(data))
            if offset == 0:
                self.assertFalse(target.exists(), "nothing is in place before the last segment")
        self.assertEqual(target.read_bytes(), data)
        collected, offset = b"", 0
        while True:
            got = self.device.call("get", path=str(target), offset=offset, limit=70000)
            self.assertEqual(hashlib.sha256(got.body).hexdigest(), got["sha256"])
            collected += got.body
            offset += got["len"]
            if got["eof"]:
                break
        self.assertEqual(collected, data)
        empty = self.device.call("get", path=str(target), offset=len(data) + 5)
        self.assertEqual((empty["len"], empty["eof"]), (0, True))
        # A segmented put of what is already in place is skipped at its first segment.
        skipped = self.device.call("put", data=data[:step], path=str(target), len=step, offset=0, total=len(data),
                                   total_sha256=whole, sha256=hashlib.sha256(data[:step]).hexdigest())
        self.assertTrue(skipped["same"])

    def test_a_segment_at_the_wrong_offset_or_a_wrong_total_hash_is_refused(self):
        target = self.home / "seg.bin"
        first, second = b"a" * 10, b"b" * 10
        whole = hashlib.sha256(first + second).hexdigest()
        self.device.call("put", data=first, path=str(target), len=10, total=20, total_sha256=whole,
                         sha256=hashlib.sha256(first).hexdigest())
        skipped = self.device.call("put", data=second, path=str(target), len=10, offset=5, total=20,
                                   total_sha256=whole, sha256=hashlib.sha256(second).hexdigest())
        self.assertEqual(skipped.err, "bad_offset")
        wrong = self.device.call("put", data=second, path=str(target), len=10, offset=10, total=20,
                                 total_sha256="f" * 64, sha256=hashlib.sha256(second).hexdigest())
        self.assertEqual(wrong.err, "sha_mismatch")
        self.assertFalse(target.exists())
        self.assertFalse(list(self.home.glob("seg.bin*")), "the partial file is removed")

    def test_limits_and_errors_of_get_and_put(self):
        self.assertEqual(self.device.call("get", path=str(self.home / "missing")).err, "not_found")
        self.assertEqual(self.device.call("get", path=str(self.home)).err, "not_file")
        self.assertEqual(self.device.call("get", path="~/x", offset=-1).err, "bad_request")
        too_big = self.device.call("put", path=str(self.home / "x"), len=(64 << 20) + 1, sha256="0" * 64)
        self.assertEqual(too_big.err, "too_large")
        (self.home / "hello.txt").write_text("hi")
        self.assertEqual(self.device.call("get", path="~/hello.txt").body, b"hi", "~ is the device's home")

    def test_ls_lists_expands_home_and_reports_the_kinds(self):
        (self.home / "dir").mkdir()
        (self.home / "dir" / "inner").mkdir()
        (self.home / "dir" / "f.txt").write_text("12345")
        os.symlink(self.home / "dir" / "inner", self.home / "dir" / "to-inner")
        os.symlink(self.home / "dir" / "f.txt", self.home / "dir" / "to-file")
        reply = self.device.call("ls", path="~/dir")
        self.assertEqual(reply["path"], str(self.home / "dir"))
        self.assertEqual(reply["home"], str(self.home))
        kinds = {e["name"]: (e["kind"], e.get("target_dir")) for e in reply["entries"]}
        self.assertEqual(kinds, {"f.txt": ("file", None), "inner": ("dir", None), "to-file": ("link", False),
                                 "to-inner": ("link", True)})
        self.assertEqual([e["name"] for e in reply["entries"]], sorted(kinds))
        self.assertEqual({e["name"]: e["size"] for e in reply["entries"]}["f.txt"], 5)
        limited = self.device.call("ls", path="~/dir", limit=2)
        self.assertEqual((len(limited["entries"]), limited["truncated"], limited["count"]), (2, True, 4))
        self.assertEqual(self.device.call("ls", path="~/nowhere").err, "not_found")
        self.assertEqual(self.device.call("ls", path="~/dir/f.txt").err, "not_dir")
        self.assertIn("dir", [e["name"] for e in self.device.call("ls")["entries"]], "no path means the home")


class StatTest(ShimCase):
    def setUp(self):
        super().setUp()
        # The root sits under a directory called .ssh: names above the root are the deployer's, not judged.
        self.root = self.home / ".ssh" / "proj"
        self.outside = self.home / "outside"
        for path in (self.root / "sub" / ".ssh", self.root / ".git", self.outside):
            path.mkdir(parents=True)
        for path in (self.root / "a.txt", self.root / ".env", self.root / ".env.local", self.root / ".git" / "config",
                     self.root / "sub" / ".ssh" / "key", self.root / "sub" / "ok.txt", self.outside / "secret"):
            path.write_text("x" * 7)
        os.symlink(self.outside / "secret", self.root / "link_out")
        os.symlink(self.root / "a.txt", self.root / "link_in")
        os.symlink(self.root / ".env", self.root / "sneaky")
        self.names = [".git", ".ssh", ".env", ".env.*", "auth.json"]

    def stat(self, *paths, **fields):
        """The results keyed by the path as asked."""
        reply = self.device.call("stat", paths=list(paths), root=str(self.root), **fields)
        self.assertTrue(reply.ok, reply.head)
        return dict(zip(paths, reply["results"]))

    def test_exists_size_and_a_path_inside_the_root(self):
        found = self.stat("a.txt", "sub", "nope", deny_names=self.names)
        self.assertEqual((found["a.txt"]["exists"], found["a.txt"]["size"], found["a.txt"]["is_file"]), (True, 7, True))
        self.assertTrue(found["sub"]["is_dir"])
        self.assertEqual((found["nope"]["exists"], found["nope"]["escapes"], found["nope"]["denied"]),
                         (False, False, False))
        self.assertFalse(found["a.txt"]["escapes"] or found["a.txt"]["denied"], "the root's own .ssh is not judged")
        self.assertEqual(found["a.txt"]["realpath"], str((self.root / "a.txt").resolve()))
        self.assertFalse(self.stat("link_in", deny_names=self.names)["link_in"]["escapes"])
        self.assertTrue(self.stat("link_in")["link_in"]["is_link"])

    def test_a_path_that_leaves_the_root_escapes(self):
        found = self.stat("../outside/secret", "link_out", "../../etc/hosts", str(self.outside / "secret"),
                          "sub/../a.txt", "sub/../../x")
        self.assertEqual({k: v["escapes"] for k, v in found.items()}, {
            "../outside/secret": True, "link_out": True, "../../etc/hosts": True, str(self.outside / "secret"): True,
            "sub/../a.txt": False, "sub/../../x": True})
        self.assertEqual(found["link_out"]["realpath"], str((self.outside / "secret").resolve()),
                         "a symlink out of the root: the real path is what is judged")

    def test_denied_names_are_judged_below_the_root_on_the_path_and_where_a_link_leads(self):
        found = self.stat(".env", ".env.local", ".git/config", "sub/.ssh/key", "sub/ok.txt", "sneaky", "link_in",
                          deny_names=self.names)
        self.assertEqual({k: v["denied"] for k, v in found.items()}, {
            ".env": True, ".env.local": True, ".git/config": True, "sub/.ssh/key": True, "sub/ok.txt": False,
            "sneaky": True, "link_in": False})

    def test_denied_directories_include_what_is_inside_and_links_into_them(self):
        found = self.stat("link_out", "a.txt", str(self.outside), deny_dirs=[str(self.outside)])
        self.assertTrue(found["link_out"]["denied"])
        self.assertTrue(found[str(self.outside)]["denied"])
        self.assertFalse(found["a.txt"]["denied"])

    def test_relative_paths_use_base_and_tilde_is_the_devices_home(self):
        reply = self.device.call("stat", paths=["ok.txt", "~/outside/secret"], root=str(self.root),
                                 base=str(self.root / "sub"))
        results = reply["results"]
        self.assertEqual((results[0]["path"], results[0]["exists"]), (str(self.root / "sub" / "ok.txt"), True))
        self.assertEqual((results[1]["path"], results[1]["exists"], results[1]["escapes"]),
                         (str(self.outside / "secret"), True, True))
        self.assertEqual(self.device.call("stat", paths="x").err, "bad_request")
        without_root = self.device.call("stat", paths=["~/outside/secret"], deny_names=["secret"])
        self.assertEqual((without_root["results"][0]["escapes"], without_root["results"][0]["denied"]), (False, True))


class ExecTest(ShimCase):
    def run_exec(self, *argv, **fields):
        return self.device.call("exec", argv=list(argv), **fields)

    def test_exec_returns_the_exit_code_and_both_streams(self):
        reply = self.run_exec(PY, "-c", "import sys; print('out'); sys.stderr.write('err'); sys.exit(3)")
        self.assertEqual((reply["code"], reply["out"], reply["stderr"], reply["timed_out"], reply["truncated"]),
                         (3, "out\n", "err", False, False))

    def test_exec_runs_in_cwd_with_the_merged_env_and_stdin(self):
        work = self.home / "w"
        work.mkdir()
        script = "import os,sys; print(os.getcwd(), os.environ.get('XV'), os.environ.get('HOME'), sys.stdin.read())"
        reply = self.run_exec(PY, "-c", script, cwd=str(work), env={"XV": "1", "HOME": None}, input="typed")
        self.assertEqual(reply["out"].strip(), "%s 1 None typed" % work)

    def test_exec_stops_a_command_that_runs_past_its_time(self):
        began = time.time()
        reply = self.run_exec(PY, "-c", "import time; print('start', flush=True); time.sleep(60)", timeout=1.0)
        self.assertTrue(reply["timed_out"])
        self.assertLess(time.time() - began, 20)
        self.assertEqual(reply["out"], "start\n")
        self.assertIn(reply["code"], (-15, -9))

    def test_exec_kills_the_whole_group_of_a_timed_out_command(self):
        marker = self.home / "grandchild.pid"
        script = ("import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
                  "open(sys.argv[1],'w').write(str(p.pid)); time.sleep(60)")
        self.run_exec(PY, "-c", script, str(marker), timeout=1.5)
        pid = int(marker.read_text())
        self.assertTrue(StopAndReapTest.gone(pid))

    def test_exec_limits_the_output_and_marks_it(self):
        reply = self.run_exec(PY, "-c", "print('z' * 100000)", max_out=1000)
        self.assertTrue(reply["truncated"])
        self.assertEqual(len(reply["out"]), 1000)
        self.assertEqual(reply["code"], 0)
        whole = self.run_exec(PY, "-c", "print('z' * 100)", max_out=1000)
        self.assertFalse(whole["truncated"])

    def test_exec_sends_non_utf8_output_as_b64(self):
        reply = self.run_exec(PY, "-c", "import sys; sys.stdout.buffer.write(b'\\xff\\xfeok')")
        self.assertNotIn("out", reply.head)
        self.assertEqual(base64.b64decode(reply["out_b64"]), b"\xff\xfeok")

    def test_exec_of_a_missing_program_and_bad_arguments(self):
        self.assertEqual(self.run_exec("/no/such/program").err, "spawn_failed")
        self.assertEqual(self.device.call("exec", argv=[]).err, "bad_request")
        self.assertEqual(self.device.call("exec", argv="ls").err, "bad_request")
        self.assertEqual(self.run_exec("true", input="x" * 70000).err, "too_large")
        self.assertTrue(self.run_exec("true", retry=True).ok, "retry is the hub's flag: accepted, nothing more")


class MarkTest(ShimCase):
    """An ``exec`` with a ``mark`` (core/device_access.py G7): its group kept under ~/.covey/marks while it runs, and
    ``reap`` with the mark stops what is left of it once the hub's connection went away."""

    MARK = "tabcdef12-0123456789"

    def marks(self):
        return self.home / ".covey" / "marks" / self.device.hub / self.MARK

    def start(self, *argv):
        """An exec with the mark whose client (the hub's ssh) is then killed: the shim process, as ssh drops it."""
        line = json.dumps({"cmd": "exec", "id": "x", "hub": self.device.hub, "mark": self.MARK, "argv": list(argv),
                           "timeout": 120}).encode() + b"\n"
        process = subprocess.Popen(self.device.argv("exec"), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, env=self.device.env, start_new_session=True)
        process.stdin.write(line)
        process.stdin.flush()
        self.addCleanup(lambda: process.poll() is None and process.kill())
        records = self.wait_until(lambda: list(self.marks().glob("*.json")), what="the mark's record")
        return process, json.loads(records[0].read_text())

    def test_a_marked_exec_keeps_its_group_while_it_runs_and_says_its_mark(self):
        reply = self.device.call("exec", hub=self.device.hub, mark=self.MARK,
                                 argv=[PY, "-c", "import os; print(os.environ['COVEY_DEVICE_MARK'])"])
        self.assertEqual(reply["out"].strip(), "%s/%s" % (self.device.hub, self.MARK))
        self.assertEqual(list(self.marks().glob("*.json")) if self.marks().exists() else [], [], "gone once it ended")

    def test_reap_by_mark_stops_a_command_whose_connection_went_away(self):
        process, record = self.start(PY, "-c", "import time; time.sleep(60)")
        process.kill()  # The hub's side is gone; the command, in a session of its own, still runs.
        process.wait(5)
        self.assertTrue(Path("/proc/%d" % record["pid"]).exists())
        answer = self.device.call("reap", mark=self.MARK, grace=1.0)
        self.assertTrue(answer.ok, answer.head)
        self.assertEqual((answer["groups"], answer["after"], answer["refused"]), ([record["pgid"]], [], []))
        self.assertTrue(StopAndReapTest.gone(record["pid"]))
        self.assertFalse(self.marks().exists(), "the mark's records go with its groups")

    def test_reap_by_mark_never_kills_a_group_that_is_no_longer_the_commands(self):
        other = subprocess.Popen([PY, "-c", "import time; time.sleep(60)"], start_new_session=True, env=self.device.env)
        self.addCleanup(lambda: (other.kill(), other.wait()))
        self.marks().mkdir(parents=True)
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        (self.marks() / ("%d.json" % other.pid)).write_text(json.dumps(
            {"pid": other.pid, "pgid": other.pid, "start": "not-its-start", "boot": boot}))
        answer = self.device.call("reap", mark=self.MARK, grace=0.5)
        self.assertEqual((answer["groups"], answer["refused"]), ([], [other.pid]))
        self.assertIsNone(other.poll(), "a recycled group id is left alone")

    def test_reap_of_a_mark_with_nothing_left_is_an_empty_answer(self):
        answer = self.device.call("reap", mark=self.MARK)
        self.assertEqual((answer.ok, answer["groups"], answer["after"]), (True, [], []))
        self.assertEqual(self.device.call("reap", mark="bad mark").err, "bad_request")


class GcTest(ShimCase):
    def make_shim_dirs(self):
        own = hashlib.sha256(SHIM.read_bytes()).hexdigest()[:16]
        shims = self.home / ".covey" / "shim"
        for name in (own, "olddead", "oldused", "recent"):
            (shims / name).mkdir(parents=True)
            (shims / name / "shim.py").write_text("# stand-in\n")
        shutil.copy(SHIM, shims / own / "shim.py")
        old = time.time() - 3 * 86400
        for name in (own, "olddead", "oldused"):
            os.utime(shims / name, (old, old))
        return shims, shims / own / "shim.py"

    def test_gc_removes_old_ended_tasks_and_unreferenced_shims_but_not_referenced_ones(self):
        shims, own = self.make_shim_dirs()
        device = DeviceShim(self.home, shim=own)
        self.addCleanup(device.cleanup)
        for name in ("oldend", "freshend"):
            device.spawn(name, self.harness("--count", "1", "--delay", "0.01"))
            device.wait_exit(name)
        device.spawn("alive", self.harness())
        month = time.time() - 30 * 86400
        for name in ("oldend", "alive"):  # An old running task is kept all the same: it is not over.
            for f in (device.task_dir(name) / "exit.json", device.task_dir(name) / "spec.json"):
                if f.exists():
                    os.utime(f, (month, month))
        ghost = device.task_dir("ghost")  # An ended task that recorded an old shim dir, recent enough to stay.
        ghost.mkdir(parents=True)
        (ghost / "spec.json").write_text("{}")
        (ghost / "exit.json").write_text("{}")
        (ghost / "state.json").write_text(json.dumps({"shim": str(shims / "oldused" / "shim.py"), "pid": 1}))
        reply = device.call("gc", keep_days=7)
        self.assertTrue(reply.ok, reply.head)
        self.assertEqual(reply["removed_tasks"], ["hub1/oldend"])
        self.assertEqual(sorted(p.name for p in device.tasks_root.iterdir()), ["alive", "freshend", "ghost"])
        self.assertEqual(reply["removed_shims"], ["olddead"])
        self.assertEqual(sorted(reply["kept_shims"]), sorted([own.parent.name, "oldused", "recent"]))
        self.assertTrue((shims / "oldused").is_dir(), "a shim directory a task still names is kept")
        self.assertTrue((shims / "recent").is_dir(), "a new one may be mid-push")
        self.assertFalse((shims / "olddead").exists())
        self.assertEqual(device.call("status", task="alive")["state"], "running")

    def test_a_task_remembers_its_own_shim_so_a_newer_shim_can_serve_it(self):
        shims, own = self.make_shim_dirs()
        device = DeviceShim(self.home, shim=own)
        self.addCleanup(device.cleanup)
        device.spawn("t1", self.harness())
        self.assertEqual(self.state("t1")["shim"], str(own))
        self.assertEqual(device.call("status", task="t1")["shim"], str(own))
        # A second copy of the shim (a newer release) answers for the task the first one started.
        newer = shims / "newerone"
        newer.mkdir()
        shutil.copy(SHIM, newer / "shim.py")
        other = DeviceShim(self.home, shim=newer / "shim.py")
        self.assertEqual(other.call("status", task="t1")["state"], "running")
        self.assertEqual(other.call("send", task="t1", seq=1, d="from the newer shim")["r"], "applied")
        self.wait_echo("t1", "from the newer shim")
        self.assertEqual(self.state("t1")["shim"], str(own))


class CompatibilityTest(unittest.TestCase):
    def setUp(self):
        self.compat = load_compat()

    def test_the_shim_passes_the_check_and_the_script_exits_zero(self):
        self.assertEqual(self.compat.check(SHIM.read_text(), str(SHIM)), [])
        done = subprocess.run([sys.executable, "-B", str(REPO / "scripts" / "check_shim_compat.py")],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual((done.returncode, done.stderr), (0, ""), done.stdout)

    def test_newer_syntax_and_library_use_are_findings(self):
        bad = {
            "match": "from __future__ import annotations\nmatch x:\n    case 1:\n        pass\n",
            "except*": "from __future__ import annotations\ntry:\n    pass\nexcept* ValueError:\n    pass\n",
            "zip strict": "from __future__ import annotations\nlist(zip([], [], strict=True))\n",
            "bit_count": "from __future__ import annotations\n(5).bit_count()\n",
            "dataclass slots": "from __future__ import annotations\nfrom dataclasses import dataclass\n"
                               "@dataclass(slots=True)\nclass A:\n    x: int\n",
            "typing Self": "from __future__ import annotations\nfrom typing import Self\n",
            "tomllib": "from __future__ import annotations\nimport tomllib\n",
            "isinstance union": "from __future__ import annotations\nisinstance(1, int | str)\n",
            "traceback one argument": "from __future__ import annotations\nimport traceback\n"
                                      "traceback.format_exception(ValueError())\n",
            "file_digest": "from __future__ import annotations\nimport hashlib\nhashlib.file_digest(f, 'sha256')\n",
            "pairwise": "from __future__ import annotations\nfrom itertools import pairwise\n",
            "Popen process_group": "from __future__ import annotations\nimport subprocess\n"
                                   "subprocess.Popen(['x'], process_group=0)\n",
            "sys.exception": "from __future__ import annotations\nimport sys\nsys.exception()\n",
            "covey import": "from __future__ import annotations\nfrom covey.core import state\n",
            "relative import": "from __future__ import annotations\nfrom . import sibling\n",
            "third party": "from __future__ import annotations\nimport requests\n",
            "no future import": "import os\n",
            "ExceptionGroup": "from __future__ import annotations\nExceptionGroup('x', [ValueError()])\n",
        }
        for name, source in bad.items():
            with self.subTest(name):
                findings = self.compat.check(source, "x.py", standard=self.standard())
                self.assertTrue(findings, "no finding for: " + name)
                self.assertTrue(all(f.startswith("x.py:") for f in findings), findings)

    def standard(self):
        names = self.compat.stdlib_names()
        return names if names is not None else {"os", "sys", "json", "hashlib", "itertools", "dataclasses", "typing",
                                                "subprocess", "traceback", "tomllib"}

    def test_what_python_39_has_is_not_flagged(self):
        fine = ("from __future__ import annotations\nimport os, sys, shlex\nfrom typing import Optional\n"
                "def f(x: int | None = None) -> list[str]:\n    return [os.path.join('a', 'b')]\n"
                "isinstance(1, (int, str))\nlist(zip([], []))\nshlex.join(['a'])\n'abc'.removeprefix('a')\n"
                "sys.exit(0) if False else None\n")
        self.assertEqual(self.compat.check(fine, "ok.py", standard=self.standard()), [])

    def test_the_shim_imports_only_the_standard_library_and_nothing_of_covey(self):
        import ast
        tree = ast.parse(SHIM.read_text())
        tops = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                tops |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0)
                tops.add(node.module.split(".")[0])
        self.assertNotIn("covey", tops)
        names = getattr(sys, "stdlib_module_names", None)
        if names is not None:
            self.assertEqual(sorted(tops - set(names) - {"__future__"}), [])

    @unittest.skipUnless(shutil.which("python3.9"), "no python3.9 on this machine: the check above stands in")
    def test_the_shim_answers_hello_on_python_39(self):
        with tempfile.TemporaryDirectory() as home:
            device = DeviceShim(Path(home))
            done = subprocess.run([shutil.which("python3.9"), "-B", str(SHIM), "hello"], input=b'{"cmd":"hello"}\n',
                                  stdout=subprocess.PIPE, env=device.env, timeout=60)
            self.assertTrue(json.loads(done.stdout.splitlines()[1])["ok"])


class QueueSanityTest(unittest.TestCase):
    def test_the_attach_reader_reports_a_silent_stream_as_empty_not_hung(self):
        from support.shim_client import Frames
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, start_new_session=True)
        frames = Frames(process)
        try:
            with self.assertRaises(queue.Empty):
                frames.next(0.2)
        finally:
            frames.close()


if __name__ == "__main__":
    unittest.main()
