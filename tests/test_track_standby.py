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
index = len(open(os.environ["FAKE_PIDS"] + ".self").read().split())
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
    if mode == "slow_first" and index == 1 and seen == 2:  # The first process started never answers the question.
        time.sleep(30)
    if mode == "bad_first" and index == 1 and seen == 2:
        print(result("not a verdict"), flush=True)
        continue
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
                     "LARK_BRIDGE_TRACK_JUDGE_MS", "LARK_BRIDGE_TRACK_JUDGE_WARM", "COVEY_TRACK_JUDGE_SLIM",
                     "COVEY_TRACK_JUDGE_HEDGE"):
            os.environ.pop(name, None)
        os.environ["COVEY_TRACK_JUDGE_HEDGE"] = "1"  # One standby unless a test says otherwise.
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

    def started_count(self, count, timeout=10):
        """``count`` processes have started (the fake writes its pid when it is up)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and len(self.started()) < count:
            time.sleep(0.02)
        return len(self.started())

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
        # The next one was started as this one was taken, not after its answer: a second question meanwhile finds it.
        self.assertFalse(track_judge.warm(self.backend, self.ctx))
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
        asked = [line for line in self.lines() if line["line"]["message"]["content"] != track_standby.PRIME]
        self.assertEqual(len(asked), 1)

    def test_a_second_question_while_the_first_is_asked_finds_the_next_standby(self):
        track_judge.warm(self.backend, self.ctx)
        self.ready()
        self.ask()
        self.assertEqual(self.started_count(2), 2)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and len(self.lines()) < 3:
            time.sleep(0.02)
        self.assertEqual(self.ask().target, 1)
        self.assertEqual(self.started_count(3), 3, "no one-shot in between: the second question took the standby")

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


class HedgeTest(Base):
    """COVEY_TRACK_JUDGE_HEDGE: several standbys get the same question, the first usable answer wins."""

    def setUp(self):
        super().setUp()
        os.environ["COVEY_TRACK_JUDGE_HEDGE"] = "2"

    def primed(self, count):
        track_judge.warm(self.backend, self.ctx)
        deadline = time.monotonic() + 20  # A loaded machine starts two interpreters slowly.
        while time.monotonic() < deadline and len(self.lines()) < count:
            time.sleep(0.02)
        self.assertEqual(len(self.lines()), count)

    def test_two_standbys_start_and_the_question_goes_to_them(self):
        self.primed(2)
        self.assertEqual(len(self.started()), 2)
        verdict = self.ask()
        self.assertEqual((verdict.route, verdict.target, verdict.why), ("merge", 1, "ok"))
        asked = [line for line in self.lines() if json.loads(line["line"]["message"]["content"]).get("message") == SECRET]
        self.assertGreaterEqual(len(asked), 1)  # The loser may be killed before it has read the question.
        self.assertEqual(self.started_count(4), 4, "the pair is refilled as the question is asked")

    def test_a_standby_that_never_answers_does_not_hold_the_answer_up(self):
        with mock.patch.dict(os.environ, {"FAKE_MODE": "slow_first"}):
            self.primed(2)
            started = time.monotonic()
            verdict = self.ask()
        self.assertEqual((verdict.route, verdict.target, verdict.why), ("merge", 1, "ok"))
        self.assertLess(time.monotonic() - started, 2.5)
        first = self.started()[0]
        self.assertTrue(self.gone(first), "the loser is killed")

    def test_an_unusable_answer_of_one_is_not_the_verdict_while_the_other_is_good(self):
        with mock.patch.dict(os.environ, {"FAKE_MODE": "bad_first"}):
            self.primed(2)
            verdict = self.ask()
        self.assertEqual((verdict.route, verdict.target, verdict.why), ("merge", 1, "ok"))

    def test_when_all_hang_the_hard_limit_is_new_and_every_process_group_is_gone(self):
        with mock.patch.dict(os.environ, {"FAKE_MODE": "hang", "COVEY_TRACK_JUDGE_MS": "400"}):
            self.primed(2)
            pids = self.started()
            started = time.monotonic()
            verdict = self.ask()
        self.assertEqual((verdict.route, verdict.target, verdict.why), ("new", None, "timeout"))
        self.assertLess(time.monotonic() - started, 2.5)
        self.assertEqual(len(pids), 2)
        for pid in pids:
            self.assertTrue(self.gone(pid))

    def test_every_answer_unusable_is_new_with_the_first_reason(self):
        with mock.patch.dict(os.environ, {"FAKE_REPLY": "not a verdict"}):
            self.primed(2)
            verdict = self.ask()
        self.assertEqual((verdict.route, verdict.why), ("new", "bad_output"))

    def test_warm_keeps_the_pair_and_refills_it_after_use(self):
        self.primed(2)
        self.assertFalse(track_judge.warm(self.backend, self.ctx))
        self.ask()
        self.assertEqual(self.started_count(4), 4)
        self.assertFalse(track_judge.warm(self.backend, self.ctx))

    def test_shutdown_kills_the_spares_too(self):
        self.primed(2)
        pids = self.started()
        track_standby.shutdown()
        for pid in pids:
            self.assertTrue(self.gone(pid))

    def test_the_count_is_clamped_and_one_means_no_spare(self):
        for raw, want in (("", 2), ("x", 2), ("0", 1), ("1", 1), ("3", 3), ("9", 3)):
            self.assertEqual(track_judge.hedge({"COVEY_TRACK_JUDGE_HEDGE": raw}), want, raw)


class SlimTest(Base):
    def test_the_slim_options_are_in_the_argv_unless_switched_off_or_refused(self):
        argv = track_judge.claude_argv(self.backend, self.ctx, True)
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "user")
        self.assertIn("--disable-slash-commands", argv)
        with mock.patch.dict(os.environ, {"COVEY_TRACK_JUDGE_SLIM": "0"}):
            argv = track_judge.claude_argv(self.backend, self.ctx, True)
        self.assertNotIn("--setting-sources", argv)
        self.addCleanup(setattr, track_judge, "_refused_slim", False)
        track_judge.refused(track_standby.Failed("exit_nonzero", "code=1 error: unknown option '--disable-slash-commands'"))
        self.assertNotIn("--disable-slash-commands", track_judge.claude_argv(self.backend, self.ctx, True))

    def test_another_failure_keeps_them(self):
        track_judge.refused(track_standby.Failed("exit_nonzero", "code=1 boom"))
        self.assertIn("--disable-slash-commands", track_judge.claude_argv(self.backend, self.ctx, True))


if __name__ == "__main__":
    unittest.main()
