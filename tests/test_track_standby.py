"""The Claude Code kept ready for the track judge (app/track_standby.py, ``track_judge.warm``), against a fake that speaks
stream-json: started and primed with an invented message before the question, used for one question, killed after;
not started unless asked (switch, channel, ``COVEY_TRACK_JUDGE_WARM=0``); killed at the Bridge's stop, when idle and on
a hang; a dead one is a one-shot; the owner's words are in the one question only and never in a log line."""
from __future__ import annotations

import json
import os
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support import exec_tempdir

from covey.app import track_judge, track_standby
from covey.core.track_route import LiveJob, Message, Quote

NOW = 1_800_000_000.0
SECRET = "把这件私密的事记下来"

# Plays both ways: with --input-format stream-json it answers every user line with a result event (and writes the
# lines it got to FAKE_RECORD), without it one JSON object like the one-shot. FAKE_MODE: "hang" (a grandchild too, pids in
# FAKE_PIDS) on the second user line, "die" exits at once with FAKE_STDERR on standard error.
FAKE = """#!{python}
import json, os, subprocess, sys, time
mode = os.environ.get("FAKE_MODE", "")
reply = os.environ.get("FAKE_REPLY", "")
stream = "--input-format" in sys.argv
open(os.environ["FAKE_PIDS"] + ".self", "a").write(f"{{os.getpid()}}\\n")
if mode == "die":
    sys.stderr.write(os.environ.get("FAKE_STDERR", "boom"))
    sys.exit(2)
def result(text):
    return json.dumps({{"type": "result", "subtype": "success", "is_error": False, "result": text}})
if not stream:
    sys.stdin.read()
    print(json.dumps({{"is_error": False, "result": reply}}))
    sys.exit(0)
seen = 0
for line in sys.stdin:
    seen += 1
    with open(os.environ["FAKE_RECORD"], "a") as f:
        f.write(json.dumps({{"argv": sys.argv[1:], "cwd": os.getcwd(), "line": json.loads(line)}}) + "\\n")
    if mode == "hang" and seen == 2:
        child = subprocess.Popen(["sleep", "30"])
        open(os.environ["FAKE_PIDS"], "w").write(f"{{os.getpid()}} {{child.pid}}")
        time.sleep(30)
    print(result("not a verdict" if seen == 1 else reply), flush=True)
"""


def reply(route="merge", target=1, confidence=0.9):
    return json.dumps({"route": route, "target": target, "confidence": confidence})


def alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:  # A zombie is gone for our purpose.
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = exec_tempdir()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.script = self.dir / "fake-claude"
        self.script.write_text(FAKE.format(python=sys.executable))
        self.script.chmod(0o755)
        self.record, self.pids = self.dir / "record.jsonl", self.dir / "pids"
        patch = mock.patch.dict(os.environ, {"FAKE_RECORD": str(self.record), "FAKE_PIDS": str(self.pids),
                                             "FAKE_REPLY": reply()})
        patch.start()
        self.addCleanup(patch.stop)
        for name in ("COVEY_TRACK_JUDGE", "COVEY_TRACK_JUDGE_MS", "COVEY_TRACK_JUDGE_WARM", "LARK_BRIDGE_TRACK_JUDGE",
                     "LARK_BRIDGE_TRACK_JUDGE_MS", "LARK_BRIDGE_TRACK_JUDGE_WARM"):
            os.environ.pop(name, None)
        track_standby.shutdown()
        self.addCleanup(track_standby.shutdown)
        track_judge._refused_flag = False
        self.addCleanup(setattr, track_judge, "_refused_flag", False)
        script = str(self.script)
        self.backend = SimpleNamespace(wrap_argv=lambda argv, ctx: [script, *argv[1:]], child_env=lambda ctx: os.environ.copy())
        self.ctx = SimpleNamespace(backend_cmd=["claude"], state_dir=self.dir / "state")
        self.live = (LiveJob(1, "j1", "running", "整理对比表", NOW - 300, NOW - 200),
                     LiveJob(2, "j2", "paused", "翻译 README", NOW - 100, NOW - 90))
        self.logs = []

    def ask(self, text=SECRET):
        return track_judge.judge(Message(text, self.live, Quote(), now=NOW), backend=self.backend, ctx=self.ctx,
                                 log=self.logs.append)

    def lines(self):
        return [json.loads(line) for line in self.record.read_text().splitlines()] if self.record.exists() else []

    def started(self):
        path = Path(str(self.pids) + ".self")
        return [int(pid) for pid in path.read_text().split()] if path.exists() else []

    def ready(self, timeout=5):
        """The standby has answered its priming."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not self.lines():
            time.sleep(0.02)
        self.assertTrue(self.lines(), "the standby never read its priming message")
        time.sleep(0.1)

    def gone(self, pid, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and alive(pid):
            time.sleep(0.05)
        return not alive(pid)


class StandbyTest(Base):
    def test_a_standby_is_primed_with_an_invented_message_and_answers_one_question(self):
        self.assertTrue(track_judge.warm(self.backend, self.ctx))
        self.ready()
        verdict = self.ask()
        self.assertEqual((verdict.route, verdict.target, verdict.why), ("merge", 1, "ok"))
        lines = self.lines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["line"]["message"]["content"], track_standby.PRIME)
        self.assertNotIn(SECRET, json.dumps(lines[0]))
        self.assertEqual(json.loads(lines[1]["line"]["message"]["content"])["message"], SECRET)
        self.assertEqual(len(self.started()), 1, "the question did not start a process of its own")

    def test_the_argv_is_the_one_shots_with_stream_json_in_and_out(self):
        track_judge.warm(self.backend, self.ctx)
        self.ready()
        argv = self.lines()[0]["argv"]
        pairs = dict(zip(argv, argv[1:]))
        self.assertEqual((pairs["--model"], pairs["--input-format"], pairs["--output-format"], pairs["--max-turns"],
                          pairs["--tools"]), ("haiku", "stream-json", "stream-json", "1", ""))
        for flag in ("-p", "--verbose", "--no-session-persistence", "--strict-mcp-config"):
            self.assertIn(flag, argv)
        self.assertEqual(Path(self.lines()[0]["cwd"]).resolve(), (self.dir / "state" / "track-judge").resolve())

    def test_the_standby_is_gone_after_its_question_and_the_next_one_comes_from_warm(self):
        track_judge.warm(self.backend, self.ctx)
        self.ready()
        first = self.started()[0]
        self.ask()
        self.assertTrue(self.gone(first), "a used standby is killed")
        self.assertIsNone(track_standby.take(track_judge.standby_target(self.backend, self.ctx)[3]))
        self.assertTrue(track_judge.warm(self.backend, self.ctx))
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and len(self.started()) < 2:
            time.sleep(0.02)
        self.assertEqual(len(self.started()), 2)

    def test_warm_while_one_is_live_starts_nothing_more(self):
        self.assertTrue(track_judge.warm(self.backend, self.ctx))
        self.assertFalse(track_judge.warm(self.backend, self.ctx))
        self.assertFalse(track_judge.warm(self.backend, self.ctx))
        self.ready()
        self.assertEqual(len(self.started()), 1)

    def test_a_question_that_comes_before_the_priming_is_over_waits_for_it(self):
        track_judge.warm(self.backend, self.ctx)
        self.assertEqual(self.ask().target, 1)
        self.assertEqual(len(self.lines()), 2)

    def test_without_a_standby_the_question_is_asked_of_a_one_shot(self):
        verdict = self.ask()
        self.assertEqual((verdict.route, verdict.why), ("merge", "ok"))
        self.assertEqual(self.lines(), [])  # The stream branch of the fake was never used.
        self.assertEqual(len(self.started()), 1)

    def test_a_standby_that_died_is_a_one_shot_and_nothing_is_wrong(self):
        with mock.patch.dict(os.environ, {"FAKE_MODE": "die"}):
            track_judge.warm(self.backend, self.ctx)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not self.started():
                time.sleep(0.02)
            time.sleep(0.3)
        verdict = self.ask()
        self.assertEqual((verdict.route, verdict.why), ("merge", "ok"))
        self.assertEqual(len(self.started()), 2)

    def test_a_standby_that_hangs_is_killed_with_its_children_at_the_hard_limit(self):
        with mock.patch.dict(os.environ, {"FAKE_MODE": "hang", "COVEY_TRACK_JUDGE_MS": "400"}):
            track_judge.warm(self.backend, self.ctx)
            self.ready()
            started = time.monotonic()
            verdict = self.ask()
        self.assertEqual((verdict.route, verdict.target, verdict.why), ("new", None, "timeout"))
        self.assertLess(time.monotonic() - started, 2.5)
        pids = [int(pid) for pid in self.pids.read_text().split()]
        for pid in pids:
            self.assertTrue(self.gone(pid), f"{pid} is still running")
        self.assertIn("track judge failed reason=timeout", self.logs[-1])

    def test_the_log_line_is_the_one_shots_and_has_no_word_of_the_owner(self):
        track_judge.warm(self.backend, self.ctx)
        self.ready()
        self.ask()
        self.assertEqual(len(self.logs), 1)
        self.assertRegex(self.logs[0], r"^track judge route=merge target=1 conf=0\.90 why=ok ms=\d+ via=claude$")
        self.assertNotIn(SECRET, "\n".join(self.logs))

    def test_a_redacted_topic_uses_no_standby(self):
        track_judge.warm(self.backend, self.ctx)
        self.ready()
        verdict = track_judge.judge(Message(SECRET, self.live, Quote(), now=NOW), redacted=True, backend=self.backend,
                                    ctx=self.ctx, log=self.logs.append)
        self.assertEqual(verdict.why, "redacted")
        self.assertEqual(len(self.lines()), 1)  # The priming only.


class StopTest(Base):
    def test_shutdown_kills_the_standby(self):
        track_judge.warm(self.backend, self.ctx)
        self.ready()
        pid = self.started()[0]
        self.assertTrue(alive(pid))
        track_standby.shutdown()
        self.assertTrue(self.gone(pid))

    def test_the_bridges_stop_shuts_it_down(self):
        from covey.app.butler import Butler
        track_judge.warm(self.backend, self.ctx)
        self.ready()
        pid = self.started()[0]
        Butler.close()
        self.assertTrue(self.gone(pid))

    def test_an_idle_standby_kills_itself(self):
        argv, env, cwd, signature = track_judge.standby_target(self.backend, self.ctx)
        standby = track_standby.Standby(argv, env, cwd, signature, idle_s=0.3)
        self.addCleanup(standby.kill)
        self.assertTrue(alive(standby.proc.pid))
        self.assertTrue(self.gone(standby.proc.pid))

    def test_warm_replaces_one_that_is_too_old(self):
        track_judge.warm(self.backend, self.ctx)
        self.ready()
        first = self.started()[0]
        with mock.patch.object(track_standby, "MAX_AGE_S", 0):
            self.assertTrue(track_judge.warm(self.backend, self.ctx))
        self.assertTrue(self.gone(first))

    def test_a_standby_whose_argv_changed_is_not_used(self):
        track_judge.warm(self.backend, self.ctx)
        self.ready()
        self.assertIsNone(track_standby.take(("another", "signature")))
        self.assertEqual(self.ask().why, "ok")  # Still asked, of a one-shot.


class NothingStartedTest(Base):
    def assert_nothing(self):
        self.assertFalse(track_judge.warm(self.backend, self.ctx))
        time.sleep(0.2)
        self.assertEqual(self.started(), [])
        self.assertIsNone(track_standby._current)

    def test_warm_off(self):
        for value in ("0", "off", "false", "no"):
            with mock.patch.dict(os.environ, {"COVEY_TRACK_JUDGE_WARM": value}):
                self.assert_nothing()

    def test_the_judge_off(self):
        with mock.patch.dict(os.environ, {"COVEY_TRACK_JUDGE": "off"}):
            self.assert_nothing()

    def test_a_command_channel(self):
        with mock.patch.dict(os.environ, {"COVEY_TRACK_JUDGE": f"cmd:{self.script}"}):
            self.assert_nothing()

    def test_a_bad_channel(self):
        with mock.patch.dict(os.environ, {"COVEY_TRACK_JUDGE": "nonsense"}):
            self.assert_nothing()

    def test_no_backend(self):
        self.assertFalse(track_judge.warm(None, self.ctx))
        self.assertFalse(track_judge.warm(self.backend, None))
        self.assertEqual(self.started(), [])

    def test_warm_off_asks_a_one_shot_as_before(self):
        with mock.patch.dict(os.environ, {"COVEY_TRACK_JUDGE_WARM": "0"}):
            self.assertEqual(self.ask().why, "ok")
        self.assertEqual(self.lines(), [])
        self.assertEqual(len(self.started()), 1)

    def test_warm_never_raises(self):
        broken = SimpleNamespace(wrap_argv=mock.Mock(side_effect=RuntimeError("no")), child_env=lambda ctx: {})
        self.assertFalse(track_judge.warm(broken, self.ctx))
        self.assertFalse(track_judge.warm(SimpleNamespace(wrap_argv=lambda argv, ctx: ["/no/such/command"],
                                                          child_env=lambda ctx: os.environ.copy()), self.ctx))


if __name__ == "__main__":
    unittest.main()
