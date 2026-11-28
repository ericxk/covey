"""The small model that judges a message the rules left open (app/track_judge.py), against fake child processes: a normal
answer, a hang past the hard limit, bad output, an index out of range, low confidence
, and the ``cmd:`` channel."""
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

from covey.app import track_judge
from covey.app.track_judge import Verdict
from covey.core.track_route import Message, Quote, LiveJob

NOW = 1_800_000_000.0

# One script plays both channels. FAKE_REPLY is the text it answers (wrapped in Claude Code's JSON envelope unless
# FAKE_RAW is set); FAKE_MODE: "hang" (a grandchild too, pid in FAKE_PIDS), "exit" (status 3, stderr in FAKE_STDERR),
# "error" (the envelope says is_error). It writes its argv, working directory and standard input to FAKE_RECORD.
FAKE = """#!{python}
import json, os, subprocess, sys, time
mode = os.environ.get("FAKE_MODE", "")
if os.environ.get("FAKE_RECORD"):
    with open(os.environ["FAKE_RECORD"], "a") as f:
        f.write(json.dumps({{"argv": sys.argv[1:], "cwd": os.getcwd(), "stdin": sys.stdin.read()}}) + "\\n")
else:
    sys.stdin.read()
if mode == "hang":
    child = subprocess.Popen(["sleep", "30"])
    open(os.environ["FAKE_PIDS"], "w").write(f"{{os.getpid()}} {{child.pid}}")
    time.sleep(30)
if mode == "exit":
    sys.stderr.write(os.environ.get("FAKE_STDERR", "boom"))
    sys.exit(3)
reply = os.environ.get("FAKE_REPLY", "")
if os.environ.get("FAKE_RAW"):
    print(reply)
elif mode == "error":
    print(json.dumps({{"is_error": True, "result": reply}}))
else:
    print(json.dumps({{"is_error": False, "result": reply}}))
"""


def reply(route="merge", target=1, confidence=0.9):
    return json.dumps({"route": route, "target": target, "confidence": confidence})


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = exec_tempdir()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.script = self.dir / "fake-judge"
        self.script.write_text(FAKE.format(python=sys.executable))
        self.script.chmod(0o755)
        self.record = self.dir / "record.jsonl"
        self.pids = self.dir / "pids"
        patch = mock.patch.dict(os.environ, {"FAKE_RECORD": str(self.record), "FAKE_PIDS": str(self.pids)})
        patch.start()
        self.addCleanup(patch.stop)
        for name in ("COVEY_TRACK_JUDGE", "COVEY_TRACK_JUDGE_MS", "LARK_BRIDGE_TRACK_JUDGE", "LARK_BRIDGE_TRACK_JUDGE_MS",
                     "COVEY_TRACK_JUDGE_SLIM"):
            os.environ.pop(name, None)
        self.addCleanup(setattr, track_judge, "_refused_flag", False)
        track_judge._refused_flag = False
        self.logs = []
        self.wrapped = []
        script = str(self.script)

        def wrap(argv, ctx):
            self.wrapped.append(list(argv))
            return [script, *argv[1:]]  # The Harness's own wrapping: here, the fake in place of the command.

        self.backend = SimpleNamespace(wrap_argv=wrap, child_env=lambda ctx: os.environ.copy())
        self.ctx = SimpleNamespace(backend_cmd=["claude"], state_dir=self.dir / "state")
        self.live = (LiveJob(1, "j1", "running", "整理对比表", NOW - 300, NOW - 200),
                     LiveJob(2, "j2", "paused", "翻译 README", NOW - 100, NOW - 90))

    def ask(self, text="再加一列", live=None, *, quote="", question="", **kwargs):
        message = Message(text, self.live if live is None else live, Quote(text=quote), now=NOW)
        return track_judge.judge(message, question=question, backend=self.backend, ctx=self.ctx,
                                 log=self.logs.append, **kwargs)

    def recorded(self):
        if not self.record.exists():
            return []
        return [json.loads(line) for line in self.record.read_text().splitlines()]

    def say(self, reply_text, **env):
        patch = mock.patch.dict(os.environ, {"FAKE_REPLY": reply_text, **env})
        patch.start()
        self.addCleanup(patch.stop)


class ClaudeChannelTest(Base):
    def test_a_confident_merge_names_the_track_of_the_task_it_chose(self):
        self.say(reply("merge", 2, 0.9))
        verdict = self.ask()
        self.assertEqual((verdict.route, verdict.target, verdict.why), ("merge", 2, "ok"))
        self.assertEqual(self.ask().target, 2)

    def test_the_index_counts_the_tasks_oldest_first(self):
        self.say(reply("merge", 1, 0.9))
        newer_first = tuple(reversed(self.live))
        self.assertEqual(self.ask(live=newer_first).target, 1)
        sent = json.loads(self.recorded()[0]["stdin"])
        self.assertEqual([task["request"] for task in sent["tasks"]], ["整理对比表", "翻译 README"])
        self.assertEqual([task["index"] for task in sent["tasks"]], [1, 2])

    def test_new_is_new(self):
        self.say(reply("new", None, 0.95))
        verdict = self.ask()
        self.assertEqual((verdict.route, verdict.target, verdict.why), ("new", None, "ok"))

    def test_the_argv_is_the_minimal_one_through_the_backends_wrapping(self):
        self.say(reply("new", None, 0.9))
        self.ask("这是一条私密消息", question="问什么", quote="被引用的话")
        wrapped = self.wrapped[0]
        self.assertEqual(wrapped[0], "claude")
        for flag in ("-p", "--no-session-persistence", "--strict-mcp-config", "--tools"):
            self.assertIn(flag, wrapped)
        argv = self.recorded()[0]["argv"]  # What ran is what wrap_argv returned.
        pairs = dict(zip(argv, argv[1:]))
        self.assertEqual((pairs["--model"], pairs["--output-format"], pairs["--max-turns"], pairs["--tools"]),
                         ("haiku", "json", "1", ""))
        self.assertEqual(pairs["--mcp-config"], '{"mcpServers":{}}')
        self.assertEqual(json.loads(pairs["--settings"]), {"disableAllHooks": True})
        self.assertEqual(pairs["--system-prompt"], track_judge.SYSTEM)
        self.assertNotIn("这是一条私密消息", " ".join(argv))  # The message goes on standard input.

    def test_what_the_model_is_given(self):
        self.say(reply("new", None, 0.9))
        self.ask("x" * 500, question="问" * 400, quote="引" * 301)
        record = self.recorded()[0]
        self.assertEqual(Path(record["cwd"]).resolve(), (self.dir / "state" / "track-judge").resolve())
        sent = json.loads(record["stdin"])
        self.assertEqual(sorted(sent), ["message", "question", "quote", "tasks"])
        self.assertEqual(len(sent["message"]), track_judge.CLIP + 1)
        self.assertTrue(sent["message"].endswith("…"))
        self.assertEqual(len(sent["question"]), track_judge.CLIP + 1)
        self.assertEqual(sent["tasks"][1], {"index": 2, "phase": "paused", "request": "翻译 README"})

    def test_no_question_and_no_quote_are_left_out_and_only_the_newest_three_tasks_are_listed(self):
        self.say(reply("new", None, 0.9))
        many = tuple(LiveJob(i, f"j{i}", "running", f"任务 {i}", NOW - 1000 + i * 10, 0) for i in range(1, 5)) + (
            LiveJob(5, "j5", "done", "没在做", NOW, 0),)
        self.ask(live=many)
        sent = json.loads(self.recorded()[0]["stdin"])
        self.assertEqual(sorted(sent), ["message", "tasks"])
        self.assertEqual([task["request"] for task in sent["tasks"]], ["任务 2", "任务 3", "任务 4"])

    def test_a_merge_picks_a_track_among_the_listed_newest_tasks(self):
        self.say(reply("merge", 3, 0.9))
        many = tuple(LiveJob(i, f"j{i}", "running", f"任务 {i}", NOW - 1000 + i * 10, 0) for i in range(1, 5))
        self.assertEqual(self.ask(live=many).target, 4)

    def test_the_code_fence_and_words_around_the_json_are_fine(self):
        self.say('Sure:\n```json\n{"route":"merge","target":1,"confidence":0.8}\n```')
        self.assertEqual(self.ask().target, 1)
        self.say('{"x":1} then {"route":"merge","target":2,"confidence":0.75} done')
        self.assertEqual(self.ask().target, 2)

    def test_the_hard_limit_kills_the_whole_process_group(self):
        self.say(reply(), FAKE_MODE="hang")
        os.environ["COVEY_TRACK_JUDGE_MS"] = "300"
        started = time.monotonic()
        verdict = self.ask()
        elapsed = time.monotonic() - started
        self.assertEqual((verdict.route, verdict.target, verdict.why), ("new", None, "timeout"))
        self.assertLess(elapsed, 2.5)
        self.assertGreaterEqual(verdict.ms, 250)
        pids = [int(pid) for pid in self.pids.read_text().split()]
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and any(Path(f"/proc/{pid}").exists() and
                                                  "Z" not in (Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0])
                                                  for pid in pids):
            time.sleep(0.05)
        for pid in pids:
            alive = Path(f"/proc/{pid}/stat").exists() and Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
            self.assertFalse(alive, f"process {pid} still runs")

    def test_a_process_that_does_not_start_is_new(self):
        self.backend.wrap_argv = lambda argv, ctx: [str(self.dir / "missing"), *argv]
        verdict = self.ask()
        self.assertEqual((verdict.route, verdict.why), ("new", "spawn_failed"))

    def test_an_error_exit_is_new_and_its_stderr_is_logged(self):
        self.say(reply(), FAKE_MODE="exit", FAKE_STDERR="login expired")
        verdict = self.ask()
        self.assertEqual((verdict.route, verdict.why), ("new", "exit_nonzero"))
        self.assertIn("login expired", self.logs[-1])

    def test_an_error_envelope_is_new(self):
        self.say("API Error", FAKE_MODE="error")
        self.assertEqual(self.ask().why, "model_error")

    def test_output_that_is_not_the_envelope_is_new(self):
        self.say("not json at all", FAKE_RAW="1")
        self.assertEqual(self.ask().why, "bad_output")

    def test_bad_json_in_the_answer_is_new(self):
        for text in ("", "merge into 1", '{"route":"merge","target":1', "[1, 2]", '{"target":1,"confidence":0.9}',
                     '{"route":"maybe","target":1,"confidence":0.9}', '{"route":"merge","target":1}',
                     '{"route":"merge","target":1,"confidence":"high"}', '{"route":"merge","target":1,"confidence":true}',
                     '{"route":"merge","target":1,"confidence":1.5}', '{"route":"merge","target":1,"confidence":-0.1}',
                     '{"route":"merge","target":"1","confidence":0.9}', '{"route":"merge","target":null,"confidence":0.9}',
                     '{"route":"merge","target":1.0,"confidence":0.9}', '{"route":"merge","target":true,"confidence":0.9}'):
            self.say(text)
            verdict = self.ask()
            self.assertEqual((verdict.route, verdict.target, verdict.why), ("new", None, "bad_output"), text)

    def test_an_index_out_of_the_list_is_new(self):
        for target in (0, 3, -1, 99):
            self.say(reply("merge", target, 0.99))
            verdict = self.ask()
            self.assertEqual((verdict.route, verdict.target, verdict.why), ("new", None, "out_of_range"), target)

    def test_low_confidence_is_new_and_the_line_is_exactly_seven_tenths(self):
        self.say(reply("merge", 1, 0.69))
        verdict = self.ask()
        self.assertEqual((verdict.route, verdict.target, verdict.why, verdict.confidence), ("new", None, "low_confidence", 0.69))
        self.say(reply("merge", 1, 0.7))
        self.assertEqual(self.ask().route, "merge")

    def test_new_needs_no_confidence_or_target(self):
        self.say('{"route":"new","confidence":0.1}')
        self.assertEqual((self.ask().route, self.ask().why), ("new", "ok"))

    def test_off_starts_nothing(self):
        self.say(reply("merge", 1, 0.99))
        for value in ("off", "OFF", "0", "false", "no"):
            os.environ["COVEY_TRACK_JUDGE"] = value
            self.assertEqual(self.ask().why, "off", value)
        self.assertEqual(self.recorded(), [])

    def test_unset_and_empty_and_the_old_name_mean_claude(self):
        self.say(reply("merge", 1, 0.9))
        self.assertEqual(self.ask().route, "merge")
        os.environ["COVEY_TRACK_JUDGE"] = " claude "
        self.assertEqual(self.ask().route, "merge")
        del os.environ["COVEY_TRACK_JUDGE"]
        os.environ["LARK_BRIDGE_TRACK_JUDGE"] = "off"
        self.assertEqual(self.ask().why, "off")

    def test_a_config_the_module_does_not_know_is_new_with_a_log_line(self):
        for value in ("gpt", "cmd:relative/path", "cmd:"):
            os.environ["COVEY_TRACK_JUDGE"] = value
            verdict = self.ask()
            self.assertEqual((verdict.route, verdict.why), ("new", "bad_config"), value)
        self.assertEqual(self.recorded(), [])

    def test_no_task_or_no_words_starts_nothing(self):
        self.say(reply("merge", 1, 0.99))
        self.assertEqual(self.ask(live=()).why, "no_tasks")
        self.assertEqual(self.ask("  ").why, "no_tasks")
        done = (LiveJob(1, "j1", "done", "x"),)
        self.assertEqual(self.ask(live=done).why, "no_tasks")
        self.assertEqual(self.recorded(), [])

    def test_without_a_backend_the_claude_channel_is_new(self):
        message = Message("再加一列", self.live, now=NOW)
        verdict = track_judge.judge(message, log=self.logs.append)
        self.assertEqual((verdict.route, verdict.why), ("new", "no_backend"))

    def test_an_exception_in_the_backend_is_new(self):
        self.backend.child_env = mock.Mock(side_effect=RuntimeError("boom"))
        verdict = self.ask()
        self.assertEqual((verdict.route, verdict.why), ("new", "error"))
        self.assertIn("RuntimeError", self.logs[-1])

    def test_a_claude_that_refuses_the_flag_is_asked_without_it_from_then_on(self):
        self.say(reply(), FAKE_MODE="exit", FAKE_STDERR="error: unknown option '--no-session-persistence'")
        self.assertEqual(self.ask().why, "exit_nonzero")
        self.assertTrue(track_judge._refused_flag)
        self.say(reply("merge", 1, 0.9), FAKE_MODE="")
        self.assertEqual(self.ask().route, "merge")
        self.assertIn("--no-session-persistence", self.recorded()[0]["argv"])
        self.assertNotIn("--no-session-persistence", self.recorded()[1]["argv"])

    def test_another_failure_does_not_drop_the_flag(self):
        self.say(reply(), FAKE_MODE="exit", FAKE_STDERR="login expired")
        self.ask()
        self.assertFalse(track_judge._refused_flag)


class LogTest(Base):
    def test_one_line_per_call_with_the_milliseconds_and_never_the_text(self):
        self.say(reply("merge", 2, 0.9))
        self.ask("这是一条私密消息", question="私密问题", quote="私密引用")
        self.say("bad")
        self.ask("这是一条私密消息")
        self.assertEqual(len(self.logs), 2)
        for line in self.logs:
            self.assertTrue(line.startswith("track judge "), line)
            self.assertRegex(line, r" ms=\d+( |$)")
            self.assertNotIn("私密", line)
        self.assertRegex(self.logs[0], r"^track judge route=merge target=2 conf=0\.90 why=ok ms=\d+ via=claude$")
        self.assertRegex(self.logs[1], r"^track judge failed reason=bad_output ms=\d+ via=claude$")


class CommandChannelTest(Base):
    def setUp(self):
        super().setUp()
        os.environ["COVEY_TRACK_JUDGE"] = "cmd:" + str(self.script)

    def test_the_command_gets_one_json_object_and_answers_one_json_line(self):
        self.say(reply("merge", 2, 0.85), FAKE_RAW="1")
        verdict = self.ask("再加一列", question="问什么", quote="引用")
        self.assertEqual((verdict.route, verdict.target, verdict.why), ("merge", 2, "ok"))
        record = self.recorded()[0]
        self.assertEqual(record["argv"], [])
        sent = json.loads(record["stdin"])
        self.assertEqual((sent["message"], sent["question"], sent["quote"]), ("再加一列", "问什么", "引用"))
        self.assertEqual([task["index"] for task in sent["tasks"]], [1, 2])
        self.assertEqual(self.wrapped, [])  # The command is not the Harness's: nothing to wrap.
        self.assertTrue(self.logs[-1].endswith("via=cmd"), self.logs[-1])

    def test_the_command_needs_no_backend(self):
        self.say(reply("new", None, 0.9), FAKE_RAW="1")
        message = Message("再加一列", self.live, now=NOW)
        self.assertEqual(track_judge.judge(message, log=self.logs.append).route, "new")

    def test_the_same_checks_apply_to_its_answer(self):
        for text, why in (("not json", "bad_output"), (reply("merge", 5, 0.9), "out_of_range"),
                          (reply("merge", 1, 0.2), "low_confidence")):
            self.say(text, FAKE_RAW="1")
            verdict = self.ask()
            self.assertEqual((verdict.route, verdict.why), ("new", why), text)

    def test_a_failing_command_is_new(self):
        self.say(reply(), FAKE_MODE="exit", FAKE_STDERR="nope")
        self.assertEqual(self.ask().why, "exit_nonzero")

    def test_a_missing_command_is_new(self):
        os.environ["COVEY_TRACK_JUDGE"] = "cmd:" + str(self.dir / "missing")
        self.assertEqual(self.ask().why, "spawn_failed")

    def test_the_hard_limit_applies_and_kills_the_group(self):
        self.say(reply(), FAKE_MODE="hang")
        os.environ["COVEY_TRACK_JUDGE_MS"] = "300"
        started = time.monotonic()
        self.assertEqual(self.ask().why, "timeout")
        self.assertLess(time.monotonic() - started, 2.5)
        pid = int(self.pids.read_text().split()[1])
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and Path(f"/proc/{pid}/stat").exists() and \
                Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z":
            time.sleep(0.05)
        stat = Path(f"/proc/{pid}/stat")
        self.assertTrue(not stat.exists() or stat.read_text().rsplit(")", 1)[1].split()[0] == "Z")


class SettingsTest(unittest.TestCase):
    def test_the_hard_limit_defaults_to_six_seconds_and_is_clamped(self):
        for value, ms in ((None, 6000), ("", 6000), ("abc", 6000), ("1500", 1500), ("50", track_judge.MIN_MS),
                          ("999999", track_judge.MAX_MS), (" 2500 ", 2500)):
            env = {} if value is None else {"COVEY_TRACK_JUDGE_MS": value}
            self.assertEqual(track_judge.limit_ms(env), ms, value)

    def test_the_mode(self):
        for value, expected in ((None, "claude"), ("", "claude"), ("Claude", "claude"), ("off", "off"), ("cmd:/usr/bin/judge", "cmd:/usr/bin/judge"),
                                ("cmd:  /usr/bin/judge ", "cmd:/usr/bin/judge"), ("cmd:judge", "bad"), ("other", "bad")):
            env = {} if value is None else {"COVEY_TRACK_JUDGE": value}
            self.assertEqual(track_judge.mode(env), expected, value)

    def test_the_default_limit_is_the_documented_six_seconds_and_the_confidence_line_seven_tenths(self):
        # Claude Code's one-shot takes about 3.4 s from a cold start: no process is kept ready any more (T898).
        self.assertEqual((track_judge.DEFAULT_MS, track_judge.CONFIDENCE_MIN), (6000, 0.7))

    def test_a_verdict_is_new_unless_told_otherwise(self):
        self.assertEqual(Verdict("new").target, None)


class SlimTest(Base):
    """COVEY_TRACK_JUDGE_SLIM: every Claude Code one-shot of the 辅助模型 (moved here from the standby's tests, T898)."""

    def test_the_slim_options_are_in_the_argv_unless_switched_off_or_refused(self):
        argv = track_judge.claude_argv(self.backend, self.ctx, True)
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "user")
        self.assertIn("--disable-slash-commands", argv)
        with mock.patch.dict(os.environ, {"COVEY_TRACK_JUDGE_SLIM": "0"}):
            argv = track_judge.claude_argv(self.backend, self.ctx, True)
        self.assertNotIn("--setting-sources", argv)
        self.addCleanup(setattr, track_judge, "_refused_slim", False)
        track_judge.refused(track_judge.Failed("exit_nonzero", "code=1 error: unknown option '--disable-slash-commands'"))
        self.assertNotIn("--disable-slash-commands", track_judge.claude_argv(self.backend, self.ctx, True))

    def test_another_failure_keeps_them(self):
        track_judge.refused(track_judge.Failed("exit_nonzero", "code=1 boom"))
        self.assertIn("--disable-slash-commands", track_judge.claude_argv(self.backend, self.ctx, True))


if __name__ == "__main__":
    unittest.main()
