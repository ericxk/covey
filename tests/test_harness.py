"""Harness plumbing: entrypoint defaults, the streamed subprocess, prompts, spec parity,
execution permissions, retired Harnesses (one test per rule that must survive) and
the setup script.
"""

from __future__ import annotations

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
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support import requires_extension, REPO as ROOT, exec_tempdir, make_context, quiet
from support.fake_backends import FakeBackend, InstructedBackend
from support.processes import wait_gone
from support.world import harness_app
from support.records import write_job, run_files, write_topic

import covey.backends.claude.bridge as claude
from covey.core import extensions
from covey.app.group_menu import KIND as GROUP_MENU_KIND
from covey.app.questions import recover_question_delivery
from covey.app import outbox
from covey.app.app import BridgeApp
from covey.app.availability import observe, snapshot
from covey.app.card_actions import ACTIONS
from covey.app.detached import queued_path, restart_blockers
from covey.cards.launch_config import SUBMIT_PREFIX
from covey.app.launch_models import available_models
from covey.app.harnesses import components
from covey.app.prompts import RESUME_REMINDERS
from covey.app.sessions import SYSTEM_RULES_DIR, mark_system_rules, started_with_system_rules
from covey.app.topics import RetiredHarness, retired_notice
from covey.app.unified import UnifiedBridgeApp
from covey.backends.claude.bridge import CLAUDE_SPEC, ClaudeBackend
from covey.backends.claude.argv import stream_argv
from covey.backends.claude.sessions import ClaudeSessionStore
from covey.backends.codex.bridge import CODEX_SPEC
from covey.cards.redaction_panel import ROTATE as ROTATE_KIND
from covey.cards.group_manager import KINDS as GROUP_KINDS
from covey.cards.butler_panel import KINDS as BUTLER_PANEL_KINDS
from covey.cards.redaction_panel import KINDS as REDACTION_KINDS
from covey.cards.node_panel import KINDS as NODE_PANEL_KINDS
from covey.cards.project_proposal import KINDS as TIDY_KINDS
from covey.cards.settings_hub import KINDS as SETTINGS_KINDS
from covey.cards.overview import DISMISS as OVERVIEW_DISMISS
from covey.cards.board import KINDS as BOARD_KINDS
from covey.cards.app_setup import SKIP as APP_SETUP_SKIP, START as APP_SETUP_START
from covey.cards.proxy_panel import KINDS as PROXY_PANEL_KINDS
from covey.cards.proxy_resend import RESEND as PROXY_RESEND, SUGGEST as PROXY_SUGGEST
from covey.cards.owner_send import DROP as OWNER_SEND_DROP, PICK as OWNER_SEND_PICK, SEND as OWNER_SEND
from covey.core.checkpoint import CHECKPOINT_RULES, checkpoint_note
from covey.core.harness import JobCancelled
from covey.core.jobs import BridgeReplyJob as ReplyJob, find_job_record, list_job_records
from covey.core.maintenance import maintain
from covey.core.process import signal_group, stop_group, stream_subprocess_lines
from covey.core.process_env import env_flag
from covey.core.roster import NAMES
from covey.core.session_guard import finish_native_session
from covey.core.result import VerifiedRootReply
from covey.core.saved_results import load_authorized_reply
from covey.core.state import write_private_text
from covey.core.topics import read_topic, remember_topic_message


class HarnessDefaultModelTest(unittest.TestCase):
    @requires_extension()
    def test_entrypoints_share_top_tier_defaults_whatever_the_ignored_variables_say(self) -> None:
        # core/ignored_env.py's variables no longer set a default (2026-11-10): a fresh interpreter with each set
        # to something else still has the literal ones, in the module constants and in every parser.
        from covey.core.ignored_env import IGNORED_ENV
        env = {**os.environ, **{name: "1" if name == "LARK_CLAUDE_BRIDGE_LOGIN_SHELL" else
                                "0" if name.endswith(("TIMEOUT", "SKIP_PERMISSIONS")) else
                                "7" if name.endswith("END_WAIT") else "from-env" for name in IGNORED_ENV}}
        probe = subprocess.run(
            [sys.executable, "-B", "-c", (
                f"import argparse, sys; sys.path.insert(0, {str(ROOT)!r}); "
                "from covey.backends.codex import bridge as codex; "
                "from covey.backends.claude import bridge as claude; "
                "print(codex.DEFAULT_MODEL, codex.DEFAULT_REASONING_EFFORT, codex.DEFAULT_SANDBOX, "
                "codex.DEFAULT_CODEX_CMD, codex.DEFAULT_CODEX_TIMEOUT); "
                "print(claude.DEFAULT_MODEL, claude.DEFAULT_EFFORT, claude.DEFAULT_CLAUDE_CMD, "
                "claude.DEFAULT_CLAUDE_TIMEOUT, claude.SKIP_PERMISSIONS_DEFAULT, claude.LOGIN_SHELL_DEFAULT); "
                "from covey.app.harnesses import components\n"
                "for name, keys in (('maso', ('model', 'maso_end_wait', 'maso_timeout')), ('dsh', ('dsh_cmd',)), "
                "('pi', ('pi_cmd',)), ('claude', ('no_skip_permissions', 'claude_login_shell'))):\n"
                "    parser = argparse.ArgumentParser(); components(name)[1].add_cli_args(parser)\n"
                "    args = parser.parse_args([]); print(*(getattr(args, key) for key in keys))")],
            text=True, capture_output=True, check=True, env=env)
        self.assertEqual(probe.stdout.splitlines(), [
            "gpt-6-astra max danger-full-access codex 0", "claude-fable-5 xhigh claude 0 True False",
            "gpt-6-astra_reasoning_xhigh None 0", "dsh", "pi", "False False"])


class HarnessSubprocessTest(unittest.TestCase):
    def test_env_flags_read_every_off_spelling(self) -> None:
        for raw in ("0", "false", "False", " OFF ", "no"):
            with mock.patch.dict(os.environ, {"LARK_TEST_FLAG": raw}):
                self.assertFalse(env_flag("LARK_TEST_FLAG", True), raw)
        for raw in ("1", "true", "yes", ""):
            with mock.patch.dict(os.environ, {"LARK_TEST_FLAG": raw}):
                self.assertTrue(env_flag("LARK_TEST_FLAG", False), raw)
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(env_flag("LARK_TEST_FLAG", True))
            self.assertFalse(env_flag("LARK_TEST_FLAG", False))

    def test_stream_subprocess_lines_terminates_on_cancel_event(self) -> None:
        cancel = threading.Event()
        lines: list[str] = []

        def on_line(line: str) -> None:
            lines.append(line)
            cancel.set()

        with self.assertRaises(JobCancelled):
            stream_subprocess_lines(
                [
                    sys.executable,
                    "-c",
                    "import time; print('started', flush=True); time.sleep(30)",
                ],
                stdin_text=None,
                timeout=0,
                on_line=on_line,
                cancel_event=cancel,
            )

        self.assertEqual(lines, ["started"])

    def test_cancel_event_terminates_child_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker, pid_file = Path(tmp) / "child-finished", Path(tmp) / "child-pid"
            child_code = (
                "import os, pathlib, time; "
                f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); "
                "time.sleep(1); "
                f"pathlib.Path({str(marker)!r}).write_text('done')"
            )
            parent_code = (  # "started" once the grandchild runs, so the kill finds it.
                "import pathlib, subprocess, sys, time\n"
                f"p=subprocess.Popen([sys.executable, '-c', {child_code!r}])\n"
                f"f=pathlib.Path({str(pid_file)!r})\n"
                "while not (f.exists() and f.read_text()): time.sleep(.01)\n"
                "print('started', flush=True)\n"
                "p.wait()"
            )
            cancel = threading.Event()

            def on_line(_line: str) -> None:
                cancel.set()

            with self.assertRaises(JobCancelled):
                stream_subprocess_lines(
                    [sys.executable, "-c", parent_code],
                    stdin_text=None,
                    timeout=0,
                    on_line=on_line,
                    cancel_event=cancel,
                )

            self.assertTrue(wait_gone(pid_file))  # Killed with the group, before it could finish.
            self.assertFalse(marker.exists())

    def test_a_raising_line_handler_stops_the_command_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker, pid_file = Path(tmp) / "child-finished", Path(tmp) / "child-pid"
            child_code = (f"import os, pathlib, time; pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); "
                          f"time.sleep(1); pathlib.Path({str(marker)!r}).write_text('done')")
            parent_code = (
                "import pathlib, subprocess, sys, time\n"
                f"subprocess.Popen([sys.executable, '-c', {child_code!r}])\n"
                f"f=pathlib.Path({str(pid_file)!r})\n"
                "while not (f.exists() and f.read_text()): time.sleep(.01)\n"
                "print('started', flush=True); time.sleep(30)"
            )

            def on_line(_line: str) -> None:
                raise ValueError("bad row")

            started = time.monotonic()
            with self.assertRaises(ValueError):
                stream_subprocess_lines([sys.executable, "-c", parent_code], stdin_text=None, timeout=0, on_line=on_line)
            self.assertLess(time.monotonic() - started, 10)
            self.assertTrue(wait_gone(pid_file))
            self.assertFalse(marker.exists())


class StoppedProcess:
    """A Popen stand-in: exits on the first signal in ``exits_on`` (after it is delivered)."""

    def __init__(self, exits_on=(), *, running=True):
        self.pid, self.exits_on, self.code = 4242, set(exits_on), None if running else 0
        self.waits, self.direct = [], []

    def poll(self):
        return self.code

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self.code is None:
            raise subprocess.TimeoutExpired("fake", timeout)
        return self.code

    def deliver(self, signum):
        if signum in self.exits_on:
            self.code = -signum

    def send_signal(self, signum):
        self.direct.append(signum)
        self.deliver(signum)


class ProcessGroupStopTest(unittest.TestCase):
    """One stop for every private process group: SIGTERM, a grace wait, SIGKILL, a second wait;
    never raises for a survivor, and signals the process itself when its group cannot be."""

    def stop(self, process, killpg=None):
        sent = []

        def fake_killpg(pid, signum):
            self.assertEqual(pid, process.pid)
            sent.append(signum)
            if killpg:
                killpg(signum)
            process.deliver(signum)
        with mock.patch("covey.core.process.os.killpg", side_effect=fake_killpg):
            return stop_group(process, 7), sent

    def test_an_exited_process_is_not_signalled(self):
        process = StoppedProcess(running=False)
        self.assertEqual(self.stop(process), (True, []))

    def test_sigterm_within_the_grace_is_enough(self):
        process = StoppedProcess({signal.SIGTERM})
        self.assertEqual(self.stop(process), (True, [signal.SIGTERM]))
        self.assertEqual(process.waits, [7])

    def test_sigkill_follows_an_ignored_sigterm(self):
        process = StoppedProcess({signal.SIGKILL})
        self.assertEqual(self.stop(process), (True, [signal.SIGTERM, signal.SIGKILL]))
        self.assertEqual(process.waits, [7, 7])

    def test_a_survivor_is_reported_not_raised(self):
        process = StoppedProcess()
        self.assertEqual(self.stop(process), (False, [signal.SIGTERM, signal.SIGKILL]))

    def test_a_gone_group_gets_no_direct_signal(self):
        process = StoppedProcess()

        def gone(_signum):
            process.code = -9  # The leader died with its group; only reaping is left.
            raise ProcessLookupError
        self.assertEqual(self.stop(process, gone), (True, [signal.SIGTERM]))
        self.assertEqual(process.direct, [])

    def test_a_group_that_refuses_signals_falls_back_to_the_process(self):
        process = StoppedProcess({signal.SIGTERM})

        def refused(_signum):
            raise PermissionError
        with mock.patch("covey.core.process.os.killpg", side_effect=lambda pid, signum: refused(signum)):
            self.assertTrue(stop_group(process, 1))
            signal_group(process, signal.SIGKILL)  # Exited: nothing more is sent.
        self.assertEqual(process.direct, [signal.SIGTERM])


class ClaudeBackendTest(unittest.TestCase):
    def test_login_shell_is_opt_in_by_option(self) -> None:
        # --claude-login-shell (round 2 D; LARK_CLAUDE_BRIDGE_LOGIN_SHELL no longer sets LOGIN_SHELL_DEFAULT):
        # the turn, the model catalog's probe and --rewind-files all start claude through it.
        import argparse
        from covey.backends.claude import models, rewind

        backend = claude.ClaudeBackend()
        for default, args, wrapped in ((False, [], False), (False, ["--claude-login-shell"], True),
                                       (True, [], True), (True, ["--claude-login-shell"], True)):
            with self.subTest(default=default, args=args), mock.patch.object(claude, "LOGIN_SHELL_DEFAULT", default):
                parser = argparse.ArgumentParser()
                backend.add_cli_args(parser)
                config = backend.build_config(parser.parse_args(args))
            self.assertEqual(config.options, {"transport": "stream-json", **({"login_shell": True} if wrapped else {})})
            ctx = make_context(Path("/absolute/w"), backend_cmd=["claude"], skip_permissions=False,
                               options=config.options)
            self.assertEqual(backend.wrap_argv(["claude", "-p", "hi"], ctx),
                             ["bash", "-ic", "claude -p hi"] if wrapped else ["claude", "-p", "hi"])
            self.assertEqual(stream_argv(backend, ctx)[0], "bash" if wrapped else "claude")
            self.assertIn(f"login_shell={wrapped} ", backend.startup_lines(ctx)[0])
            with mock.patch.object(rewind.subprocess, "run", side_effect=OSError) as run, self.assertRaises(ValueError):
                rewind.rewind_files(backend, ctx, "s", "p", {})
            self.assertEqual(run.call_args.args[0][0], "bash" if wrapped else "claude")
            probe = mock.Mock(**{"close.return_value": True, "initialize.side_effect": RuntimeError("offline")})
            with mock.patch.object(models, "ClaudeProcess", return_value=probe) as launch, self.assertRaises(RuntimeError):
                models.model_catalog(backend, ctx, {}, refresh=True)
            self.assertEqual(launch.call_args.args[0][0], "bash" if wrapped else "claude")
            self.assertEqual(launch.call_args.args[1].options, {"login_shell": True} if wrapped else {})

    def test_a_passing_login_check_is_reused_for_five_minutes(self) -> None:
        # The owner's choice (2026-10-26): `claude auth status` cost 1-2 s before every task.
        from covey.app import availability
        ctx = SimpleNamespace(backend_cmd=["claude-s34"], workdir=None)
        status = {"loggedIn": True}
        now = [1000.0]
        def run(argv, **_):
            return SimpleNamespace(stdout=json.dumps(status))
        with mock.patch.object(availability.subprocess, "run", side_effect=run) as checked, \
             mock.patch.object(availability.shutil, "which", return_value="/bin/claude-s34"), \
             mock.patch.object(availability.time, "monotonic", side_effect=lambda: now[0]), \
             mock.patch.dict(availability._logins, clear=True):
            availability.check_or_raise("claude", ctx)
            now[0] += 299
            availability.check_or_raise("claude", ctx)
            self.assertEqual(checked.call_count, 1)
            now[0] += 2  # Past five minutes: checked again.
            availability.check_or_raise("claude", ctx)
            self.assertEqual(checked.call_count, 2)
            during = []  # S35 #930: a task starting while the minute's watch re-ran it ran its own (2 s).
            checked.side_effect = lambda argv, **_: during.append(availability.check_or_raise("claude", ctx)) or run(argv)
            availability.local_check("claude", ctx)
            self.assertEqual((checked.call_count, during[0]["scope"]), (3, "local_preflight; login check reused"))
            checked.side_effect = run
            status["loggedIn"] = False  # The minute's watch sees the logout: the next task is refused.
            self.assertEqual(availability.local_check("claude", ctx)["reason"], "authentication")
            with self.assertRaisesRegex(ValueError, "尚未登录"):
                availability.check_or_raise("claude", ctx)
            self.assertEqual(checked.call_count, 5)


class TwoStagePromptTest(unittest.TestCase):
    def setUp(self) -> None:
        # Each case needs its own state directory: native session guards persist
        # on purpose, so a shared fixed path would leak claims between runs.
        self.root = Path(tempfile.mkdtemp(prefix="two-stage-prompt-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def _app(self) -> tuple[BridgeApp, FakeBackend]:

        backend = FakeBackend(note="unit sandbox note")
        return harness_app(CLAUDE_SPEC, backend, ClaudeSessionStore()), backend

    def _job(self, kind: str = "chat", prompt_text: str | None = None) -> ReplyJob:
        return ReplyJob(
            job_id=1,
            user_text="帮我检查代码",
            chat_id="oc",
            message_id="om",
            timeout=0,
            kind=kind,
            prompt_text=prompt_text,
        )

    def test_fresh_chat_gets_full_initial_prompt(self) -> None:
        app, backend = self._app()
        ctx = make_context(self.root)
        app.runs.generate_reply_for_worker(ctx, self._job(), None, None)
        prompt = backend.prompts[-1]
        self.assertIn("You are Claude Code, replying to the user through Feishu.", prompt)
        self.assertIn("unit sandbox note", prompt)
        self.assertIn("Recent conversation", prompt)
        self.assertIn("帮我检查代码", prompt)

    def test_resumed_chat_gets_slim_wrapper(self) -> None:
        app, backend = self._app()
        ctx = make_context(self.root)
        app.runs.generate_reply_for_worker(ctx, self._job(), "11111111-1111-4111-8111-111111111111", None)
        prompt = backend.prompts[-1]
        self.assertIn("Latest Feishu user message", prompt)
        self.assertIn("帮我检查代码", prompt)
        self.assertNotIn("Recent conversation", prompt)
        self.assertNotIn("You are Claude Code", prompt)

    def _turn(self, backend, sid=None, *, user_text="帮我检查代码", history=None, message_id="om", options=None):
        app = harness_app(CLAUDE_SPEC, backend, ClaudeSessionStore())
        quiet(app)
        ctx = make_context(self.root, options=options or {})
        job = ReplyJob(job_id=1, user_text=user_text, chat_id="oc", message_id=message_id, timeout=0,
                       history_snapshot=history)
        job.checkpoint_path = str(self.root / "checkpoint.json")
        app.runs.generate_reply_for_worker(ctx, job, sid, None)
        for claimed in job.native_session_claims:  # Settled as the worker does, so the next turn may resume it.
            finish_native_session(ctx, CLAUDE_SPEC.name, claimed, job.bridge_run_id, True)
        return backend.prompts[-1], job.checkpoint_path

    def test_a_backend_with_a_system_channel_gets_the_rules_there_on_every_turn(self) -> None:
        for sid in (None, "11111111-1111-4111-8111-111111111111"):
            with self.subTest(resumed=bool(sid)):
                if sid:  # A session that started with the rules at system level (see below for one that did not).
                    mark_system_rules(make_context(self.root), CLAUDE_SPEC.name, sid)
                backend = InstructedBackend(note="unit sandbox note")
                prompt, checkpoint = self._turn(backend, sid)
                rules = backend.instructions[-1]
                self.assertTrue(rules.startswith("You are Claude Code, replying to the user through Feishu."))
                for part in ("unit sandbox note", f"Current workdir: {self.root.resolve()}", "AGENTS.md / CLAUDE.md",
                             CHECKPOINT_RULES):
                    self.assertIn(part, rules)
                self.assertNotIn(checkpoint, rules)
                # The message has what changes per turn: the time, the user's message, the checkpoint file.
                self.assertIn("帮我检查代码", prompt)
                self.assertIn("本任务检查点文件：" + checkpoint, prompt)
                for part in ("Constraints for this bridge", "这是执行约定", "Current workdir", "AGENTS.md",
                             RESUME_REMINDERS.splitlines()[0]):
                    self.assertNotIn(part, prompt)
                self.assertEqual("Recent conversation" in prompt, sid is None)

    def test_a_backend_without_one_gets_them_in_the_first_message_and_the_contract_every_turn(self) -> None:
        backend = FakeBackend(note="unit sandbox note")
        first, checkpoint = self._turn(backend)
        self.assertTrue(first.startswith("You are Claude Code, replying to the user through Feishu."))
        for part in ("unit sandbox note", "Recent conversation", CHECKPOINT_RULES, "本任务检查点文件：" + checkpoint):
            self.assertIn(part, first)
        resumed, checkpoint = self._turn(FakeBackend(), "11111111-1111-4111-8111-111111111111")
        self.assertNotIn("Constraints for this bridge", resumed)
        for part in (RESUME_REMINDERS, f"Current workdir: {self.root.resolve()}", CHECKPOINT_RULES,
                     "本任务检查点文件：" + checkpoint):
            self.assertIn(part, resumed)

    def test_claude_and_codex_carry_the_rules_in_the_message(self) -> None:
        # Owner 2026-11-02: the model's own system prompt (developer instructions) stays as it is.
        from covey.backends.codex.bridge import CodexBackend
        for real in (ClaudeBackend, CodexBackend):
            with self.subTest(backend=real.__name__):
                backend = InstructedBackend(note="unit sandbox note")
                backend.capabilities = real().capabilities
                first, _ = self._turn(backend)
                self.assertEqual(backend.instructions[-1], "")
                self.assertTrue(first.startswith("You are Claude Code, replying to the user through Feishu."))
                for part in ("unit sandbox note", CHECKPOINT_RULES):
                    self.assertIn(part, first)
                resumed, _ = self._turn(backend, "11111111-1111-4111-8111-111111111111")
                self.assertEqual(backend.instructions[-1], "")
                self.assert_lean(resumed, False)

    def assert_lean(self, prompt, lean):
        for part in (RESUME_REMINDERS, f"Current workdir: {self.root.resolve()}", CHECKPOINT_RULES):
            (self.assertNotIn if lean else self.assertIn)(part, prompt)

    def test_a_new_session_is_marked_and_resumes_with_the_lean_message(self) -> None:
        sid, ctx = "11111111-1111-4111-8111-111111111111", make_context(self.root)
        first, _ = self._turn(InstructedBackend(sid=sid))
        self.assertTrue(started_with_system_rules(ctx, CLAUDE_SPEC.name, sid))
        self.assertNotIn(CHECKPOINT_RULES, first)
        backend = InstructedBackend(sid=sid)
        resumed, checkpoint = self._turn(backend, sid)
        self.assert_lean(resumed, True)
        self.assertIn("本任务检查点文件：" + checkpoint, resumed)
        self.assertIn(CHECKPOINT_RULES, backend.instructions[-1])

    def test_a_session_from_before_the_mark_resumes_with_the_contract_and_workspace(self) -> None:
        backend = InstructedBackend(sid="22222222-2222-4222-8222-222222222222")
        resumed, checkpoint = self._turn(backend, "22222222-2222-4222-8222-222222222222")
        self.assert_lean(resumed, False)
        self.assertIn("本任务检查点文件：" + checkpoint, resumed)
        self.assertNotIn("Constraints for this bridge", resumed)
        self.assertIn(CHECKPOINT_RULES, backend.instructions[-1])  # Sent anyway: harmless if ignored.
        self.assertFalse(started_with_system_rules(make_context(self.root), CLAUDE_SPEC.name,
                                                   "22222222-2222-4222-8222-222222222222"))

    def test_a_native_fork_inherits_its_source_mark(self) -> None:
        for marked in (True, False):
            with self.subTest(source_marked=marked):
                source, forked = str(uuid.uuid4()), str(uuid.uuid4())
                ctx = make_context(self.root)
                if marked:
                    mark_system_rules(ctx, CLAUDE_SPEC.name, source)
                first, _ = self._turn(InstructedBackend(sid=forked), options={"fork_session": source})
                self.assertIn("Recent conversation", first)
                self.assertEqual(started_with_system_rules(ctx, CLAUDE_SPEC.name, forked), marked)
                self.assert_lean(first.split("Latest user message", 1)[1], marked)
                resumed, _ = self._turn(InstructedBackend(sid=forked), forked)
                self.assert_lean(resumed, marked)

    def test_a_lost_or_unreadable_mark_means_the_fuller_message(self) -> None:
        sid, ctx = "33333333-3333-4333-8333-333333333333", make_context(self.root)
        mark_system_rules(ctx, CLAUDE_SPEC.name, sid)
        mark, = (self.root / "state" / SYSTEM_RULES_DIR).glob("*.json")
        for content in ("{not json", json.dumps({"backend": "claude", "session_id": "another"}), "[]"):
            with self.subTest(content=content):
                mark.write_text(content)
                resumed, _ = self._turn(InstructedBackend(sid=sid), sid)
                self.assert_lean(resumed, False)
        mark.unlink()
        self.assert_lean(self._turn(InstructedBackend(sid=sid), sid)[0], False)
        # A mark that cannot be read or written: the turn still runs, unmarked.
        shutil.rmtree(mark.parent)
        mark.parent.symlink_to(self.root)
        new = str(uuid.uuid4())
        self._turn(InstructedBackend(sid=new))
        self.assertFalse(started_with_system_rules(ctx, CLAUDE_SPEC.name, new))
        self.assert_lean(self._turn(InstructedBackend(sid=new), new)[0], False)

    def test_recent_conversation_leaves_out_the_message_it_states_as_latest(self) -> None:
        history = [{"role": "user", "content": "先看看日志", "message_id": "om_0"},
                   {"role": "assistant", "content": "日志正常"},
                   {"role": "user", "content": "帮我检查代码", "message_id": "om"}]
        prompt, _ = self._turn(InstructedBackend(), history=history)
        self.assertIn("Recent conversation:\nuser: 先看看日志\nassistant: 日志正常\n\nLatest user message:\n帮我检查代码",
                      prompt)
        self.assertEqual(prompt.count("帮我检查代码"), 1)
        # An answer continuation has no message ID of its own: its text is the last row.
        prompt, _ = self._turn(InstructedBackend(), user_text="用户补充回答：\n方案 A", message_id="answer-t",
                               history=[*history, {"role": "user", "content": "用户补充回答：\n方案 A"}])
        self.assertEqual(prompt.count("方案 A"), 1)
        self.assertIn("user: 帮我检查代码", prompt)

    def test_a_multi_line_message_does_not_indent_the_template(self) -> None:
        text = "第一行\n\n收到的附件（已下载到本地）：\n- 图片 1: `/abs/a.png`"
        history = [{"role": "assistant", "content": "多行回复\n第二行"}]
        for backend, sid in ((FakeBackend(), None), (FakeBackend(), "11111111-1111-4111-8111-111111111111"),
                             (InstructedBackend(), None)):
            with self.subTest(backend=type(backend).__name__, resumed=bool(sid)):
                prompt, _ = self._turn(backend, sid, user_text=text, history=history)
                parts = [prompt, *getattr(backend, "instructions", [])]
                self.assertFalse([line for part in parts for line in part.splitlines() if line.startswith(" " * 8)])
                self.assertIn("\nLatest user message:\n第一行\n" if sid is None else "\nLatest Feishu user message:\n第一行\n",
                              prompt)

    @requires_extension()
    def test_runtime_notes_state_each_harness_mode(self) -> None:
        def rules(name, **ctx_fields):
            spec, backend, store = components(name)
            ctx = make_context(self.root, **ctx_fields)
            return harness_app(spec, backend, store).runs.bridge_rules(ctx)
        self.assertIn("- You are running from a Feishu bridge with Codex sandbox mode: danger-full-access.",
                      rules("codex", options={"sandbox": "danger-full-access"}))
        self.assertIn("with Codex sandbox mode: danger-full-access.", rules("codex", options={"sandbox": "danger-full-access"}))
        self.assertIn("with Claude Code permission mode: bypass (--dangerously-skip-permissions).",
                      rules("claude", skip_permissions=True))
        self.assertIn("answers only AskUserQuestion and denies other permission requests.",
                      rules("claude", skip_permissions=False))
        maso = rules("maso")
        self.assertIn("\n- 当前运行于独立 MASO 进程，", maso)
        self.assertNotIn("。.", maso)
        self.assertNotIn("progress note", maso)

    def test_the_previous_status_leaves_out_answers_the_message_already_has(self) -> None:
        saved = {"questions": [{"id": "q1", "question": "Q"}], "answers": {"q1": {"answers": ["A"]}},
                 "delivery": "unconfirmed"}
        for prior in ({"phase": "等待用户", "saved_user_answers": saved, "bridge_preflight": {"artifacts": []}},
                      {"previous_status": "failed", "checkpoint": {"phase": "部署", "saved_user_answers": saved}}):
            note = checkpoint_note(Path("/abs/checkpoint.json"), prior)
            self.assertTrue(note.startswith("本任务检查点文件：/abs/checkpoint.json\n上次任务状态"))
            self.assertNotIn("saved_user_answers", note)
            self.assertNotIn("unconfirmed", note)
            self.assertIn("部署" if "checkpoint" in prior else "等待用户", note)
            self.assertIn("saved_user_answers", json.dumps(prior))  # the job keeps its record


class SpecParityTest(unittest.TestCase):
    def test_specs_only_differ_in_declared_config(self) -> None:

        specs = {"codex": CODEX_SPEC, "claude": CLAUDE_SPEC}
        self.assertEqual(
            {name: spec.idempotency_prefix for name, spec in specs.items()},
            {"codex": "codex-lark", "claude": "claude-lark"},
        )
        self.assertEqual(
            {name: spec.env_prefix for name, spec in specs.items()},
            {"codex": "COVEY", "claude": "LARK_CLAUDE_BRIDGE"},
        )
        self.assertEqual(
            {name: spec.session_filename for name, spec in specs.items()},
            {"codex": "codex_session_id", "claude": "claude_session_id"},
        )


class HarnessPermissionTests(unittest.TestCase):
    def test_claude_new_and_resume_apply_current_permissions(self):
        ctx = SimpleNamespace(backend_cmd=["backend"], workdir=Path.cwd(), model="", effort="", options={})
        for enabled in (True, False):
            ctx.skip_permissions = enabled
            for sid in (None, "original-session"):
                with self.subTest(enabled=enabled, sid=sid):
                    claude = ClaudeBackend()
                    argv = stream_argv(claude, ctx, sid)
                    self.assertEqual("--dangerously-skip-permissions" in argv, enabled)
                    if sid:
                        self.assertEqual(argv[argv.index("--resume") + 1], sid)


GONE = 'neeko'  # The OpenCode-based Neeko, removed.


NOTICE = '本话题使用的 Neeko 已下架，不能继续。请在主聊天发送新消息，选择其他模型开始新话题。'


SCRIPT = Path(__file__).parents[1] / 'bin/covey_bridge.py'


class RetiredHarnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.work = self.root / 'project'
        self.work.mkdir()
        self.app, self.ctx = self.build()
        self.logs = []
        self.app.runtime.log_sink = self.logs.append
        # A topic that ran on the removed Harness, as its records were left on disk.
        _, self.topic = self.app.topics.route(self.ctx, {'chat_id': 'chat', 'message_id': 'root'})
        data = read_topic(self.topic)
        data.update(harness_id=GONE, binding_revision=2, model='old-model', effort='high',
                    model_settings_snapshot=True, workspace_locked=True, harness_locked=True)
        write_topic(self.topic, data)
        write_private_text(self.topic.history_path.parent / (GONE + '_session_id'), 'ses_old')
        self.state = self.ctx.state_dir

    def build(self, *extra, log=lambda message: None):
        app = UnifiedBridgeApp(script_path=SCRIPT)
        app.runtime.log_sink = log
        args = app.build_parser().parse_args(['--workdir', str(self.work), '--state-dir', str(self.root / 'state'),
                                              '--profile', 'test-bot', *extra])
        ctx = app.build_context(args)
        app.runtime.owner_open_id = 'owner'
        app.messenger.send_message = mock.Mock(return_value=True)
        for each in app.apps.values():
            each.submission.selection_card = mock.Mock()
            each.cards.create_streaming_reply = mock.Mock(return_value=None)
        return app, ctx

    def record(self, key, job_id, status, **fields):
        record = {'message_id': key, 'job_id': job_id, 'status': status, 'kind': 'chat',
                  'backend': GONE, 'harness_id': GONE, 'binding_revision': 2, 'chat_id': 'chat',
                  'topic_id': self.topic.topic_id, 'reply_to_message_id': 'root', 'workdir': str(self.work),
                  'history_path': str(self.topic.history_path), 'session_path': str(self.topic.session_path),
                  'model': 'old-model', 'effort': 'high', **fields}
        return write_job(self.state, key, **record)

    def sent(self):
        return [(call.args[2], call.args[3]) for call in self.app.messenger.send_message.call_args_list]

    def test_message_in_the_topic_gets_the_notice_and_queues_nothing(self):
        event = {'chat_id': 'chat', 'sender_id': 'owner', 'message_id': 'reply-1', 'root_id': 'root', 'content': '继续'}
        self.app.handle_event(event, self.ctx)
        self.app.handle_event(event, self.ctx)  # A redelivered event is dropped on arrival.
        self.assertEqual(self.sent(), [(NOTICE, self.app.voice.key('retired-harness', 'reply-1'))])
        self.assertEqual(self.app.messenger.send_message.call_args.kwargs['reply_to_message_id'], 'root')
        self.assertTrue(self.app.runtime.jobs.queue.empty())
        self.assertIsNone(find_job_record(self.state, 'reply-1'))
        self.assertFalse(self.topic.history_path.exists())
        self.assertIn('message refused harness=neeko message=reply-1 outcome=retired_harness', self.logs)

    def test_every_old_card_click_gets_the_notice_and_reaches_no_handler(self):
        remember_topic_message(self.state, self.topic.topic_id, 'chat', 'om_card')
        # The 代答设置, 重新发送, 群管理, 旧敏感词看板链接卡, 管家 and 设置 cards' kinds (按建议回复 too) are not on task cards: they
        # never route to a topic (tests/test_proxy_panel.py, test_group_chat.py, test_redaction_panel.py,
        # test_butler_panel.py).
        private = PROXY_PANEL_KINDS | GROUP_KINDS | BUTLER_PANEL_KINDS | SETTINGS_KINDS | {PROXY_RESEND, PROXY_SUGGEST, ROTATE_KIND}
        private |= set(extensions.card_kinds())  # An enabled extension's cards (tests/test_extension_cards.py).
        private |= {OVERVIEW_DISMISS}  # 手上的事's 这些都不用回答了 (tests/test_pins.py).
        private |= BOARD_KINDS  # The 看板's 刷新 and 记为看过 (tests/test_digest.py).
        private |= {APP_SETUP_START, APP_SETUP_SKIP}  # 飞书应用待补 (tests/test_app_setup.py).
        private |= {GROUP_MENU_KIND}  # A personal group's menu card (tests/test_personal_spaces.py).
        private |= REDACTION_KINDS  # The 设置 card's 脱敏 tab (tests/test_redaction_panel.py).
        private |= NODE_PANEL_KINDS  # The 设置 card's 设备 tab (tests/test_nodes.py).
        private |= set(TIDY_KINDS)  # 整理专项's cards, the private chat's or a topic card's 移出 (tests/test_project_tidy.py).
        private |= {OWNER_SEND, OWNER_SEND_DROP, OWNER_SEND_PICK}  # The 代发草稿 card (tests/test_owner_send.py).
        clicks = [{'action_value': {'kind': kind, 'topic_id': self.topic.topic_id, 'job_id': 7, 'token': 'ab' * 8,
                                    'harness_id': GONE, 'binding_revision': 2}}
                  for kind in ACTIONS if kind not in private]
        clicks.append({'action_name': SUBMIT_PREFIX + 'a' * 24, 'action_value': {}})
        with mock.patch.object(BridgeApp, 'handle_card_action_event', autospec=True) as handler, \
                mock.patch('covey.app.runner_ipc.forward_card_action') as forward:
            for index, click in enumerate(clicks):
                self.app.handle_card_action_event({'operator_id': 'owner', 'chat_id': 'chat', 'message_id': 'om_card',
                                                   'event_id': f'ev_{index}', **click}, self.ctx)
            handler.assert_not_called()
            forward.assert_not_called()
        outcomes = [line for line in self.logs if line.startswith('card action')]
        self.assertEqual(len(outcomes), len(clicks))
        self.assertTrue(all('outcome=retired_harness' in line for line in outcomes), outcomes)
        self.assertEqual(self.sent(), [(NOTICE, self.app.voice.key('retired-harness', f'ev_{i}')) for i in range(len(clicks))])

    def test_route_and_components_name_the_missing_harness(self):
        with self.assertRaises(RetiredHarness) as raised:
            self.app.topics.route(self.ctx, {'chat_id': 'chat', 'message_id': 'reply-2', 'root_id': 'root'})
        self.assertIsInstance(raised.exception, ValueError)
        self.assertEqual((raised.exception.name, str(raised.exception)), (GONE, NOTICE))
        with self.assertRaisesRegex(ValueError, 'neeko'):
            components(GONE)
        # A topic of a Harness still in the roster routes as before.
        app, _ = self.app.topics.route(self.ctx, {'chat_id': 'chat', 'message_id': 'fresh'})
        self.assertIs(app, self.app.apps['codex'])

    def test_startup_recovery_ends_unfinished_jobs_once_and_skips_the_rest(self):
        self.record('old-done', 1, 'done', execution_status='done', delivery_ok=True)
        self.record('old-running', 2, 'running')
        self.record('old-queued', 3, 'queued')
        write_private_text(queued_path(self.state, 'old-queued'),
                           json.dumps({'schema_version': 1, 'harness': GONE, 'job': {'fields': {}}}))
        self.record('old-live', 4, 'running')
        live = run_files(self.state, 'old-live', runner={'pid': 1, 'key': 'old-live', 'harness': GONE},
                         handoff={'schema_version': 1, 'harness': GONE, 'job': {}})
        write_private_text(self.state / 'questions' / ('cd' * 8 + '.json'), json.dumps({
            'token': 'cd' * 8, 'topic_id': self.topic.topic_id, 'chat_id': 'chat', 'job_id': 2, 'job_key': 'old-running',
            'status': 'pending', 'native_live_question': True, 'request_id': 'que_1', 'questions': [],
            'live_question_proof': {'version': 1, 'backend': GONE}}))
        with mock.patch('covey.app.detached.alive', side_effect=lambda d: Path(d).name == live.name):
            self.assertEqual([b['key'] for b in restart_blockers(self.state)], ['old-running'])
            recover_question_delivery(self.ctx, [a.questions for a in self.app.apps.values()], self.app.runtime.log)
            self.app.notify_stale_jobs(self.ctx)
            self.app.notify_stale_jobs(self.ctx)  # The next restart sends nothing again.
            self.assertEqual(restart_blockers(self.state), [])
        status = {r['key']: (r['status'], r.get('status_message')) for r in list_job_records(self.state, limit=0)}
        self.assertEqual(status['old-running'], ('interrupted', NOTICE))
        self.assertEqual(status['old-queued'], ('interrupted', NOTICE))
        self.assertEqual(status['old-live'], ('running', None))  # Its older-release runner still owns it.
        self.assertEqual(status['old-done'], ('done', None))
        self.assertFalse(queued_path(self.state, 'old-queued').exists())
        self.assertEqual(self.app.runtime.jobs.queue.qsize(), 0)
        self.assertEqual(sorted(self.sent()), [('任务 #2 已中断。' + NOTICE, self.app.voice.key('retired-stale', 'old-running')),
                                               ('任务 #3 已中断。' + NOTICE, self.app.voice.key('retired-stale', 'old-queued'))])
        self.assertIn(f'task runner not adopted: {live.name} harness=neeko not in roster', self.logs)
        self.assertIn('queued job not restored: old-queued.json harness=neeko not in roster', self.logs)
        maintain(self.state)
        self.assertEqual(sum(r['status'] == 'interrupted' for r in list_job_records(self.state, limit=0)), 2)

    def test_outbox_leaves_a_saved_reply_of_the_removed_harness_unread(self):
        run_id = str(uuid.uuid4())
        reply = {'backend': GONE, 'run_id': run_id, 'session_id': 'ses_old', 'boundary': ['msg_1'],
                 'message_ids': ['msg_2'], 'text': 'DONE', 'generation': 1}
        key = 'e' * 64
        write_private_text(self.state / 'verified-results' / '.lock', '')
        write_private_text(self.state / 'verified-results' / (key + '.json'), json.dumps({
            'version': 1, 'state': 'authorized', 'reply': reply, 'generation': 1,
            'scope': {'backend': GONE, 'run_id': run_id, 'job_key': 'old-saved', 'job_id': 5, 'chat_id': 'chat',
                      'topic_id': self.topic.topic_id, 'workdir': str(self.work.resolve())}}))
        self.record('old-saved', 5, 'failed', execution_status='done', delivery_ok=False, outbox_version=1,
                    outbox_state='pending', outbox_next_at=0, verified_reply_authorized=True,
                    verified_reply_state='authorized', result_retry_policy='saved_delivery_only',
                    verified_result_key=key, result_run_id=run_id, verified_reply_generation=1)
        before = find_job_record(self.state, 'old-saved')
        outbox.tick(self.app, self.ctx, now=time.time())
        self.assertEqual(find_job_record(self.state, 'old-saved'), before)
        self.app.messenger.send_message.assert_not_called()
        # Read directly, its reply is refused by validation, never accepted under another backend.
        with self.assertRaisesRegex(ValueError, 'unsupported verified reply backend'):
            VerifiedRootReply.from_dict(reply)
        with self.assertRaisesRegex(ValueError, 'unsupported verified reply backend'):
            load_authorized_reply(self.topic, before, GONE, 'chat')

    def test_leftover_config_settings_and_catalog_files_are_ignored(self):
        config = self.root / 'harnesses.json'
        config.write_text(json.dumps({'harnesses': {GONE: {'args': ['--neeko-transport', 'cli']},
                                                    'claude': {'args': ['--claude-transport', 'stream-json']}}}))
        logs = []
        app, _ = self.build('--harness-config', str(config), log=logs.append)
        self.assertEqual(app.harnesses['claude'].context.options['transport'], 'stream-json')
        self.assertNotIn(GONE, app.apps)
        self.assertIn('harness config ignores Harnesses not in the roster: neeko', logs)
        write_private_text(self.state / 'launch-models' / (GONE + '.json'),
                           json.dumps({'updated_at': time.time(), 'models': {'m': {'display_name': 'm', 'levels': []}}}))
        write_private_text(self.state / 'model-settings' / (GONE + '.json'), json.dumps({'model': 'm', 'effort': ''}))
        self.assertFalse([key for key in available_models(app.topics, self.ctx) if key.startswith(GONE + ':')])
        observe(self.ctx, 'codex', 'm', status='ok')
        write_private_text(self.state / 'backend-health' / (GONE + '-old.json'), json.dumps({
            'backend': GONE, 'model': 'm', 'checked_at': time.time(), 'status': 'failed', 'error': ''}))
        self.assertEqual([row['backend'] for row in snapshot(self.state)], ['codex'])

    def test_a_harness_id_neither_routed_nor_named_retired_is_refused_without_its_name(self):
        # A Harness removed without a RETIRED entry: its topics get the generic notice, its live config is ignored.
        self.assertNotIn('gone', NAMES)
        self.assertEqual(retired_notice('gone'), NOTICE.replace('Neeko', 'Harness'))
        config = self.root / 'harnesses.json'
        config.write_text(json.dumps({'harnesses': {'gone': {'args': ['--gone-cmd', '/opt/gone/bin']}}}))
        logs = []
        app, _ = self.build('--harness-config', str(config), log=logs.append)
        self.assertNotIn('gone', app.apps)
        self.assertIn('harness config ignores Harnesses not in the roster: gone', logs)

    def test_a_kiro_topic_is_told_kiro_was_removed(self):
        # The Kiro Harness went on 2026-11-01; its topics named no Harness until RETIRED had it (the owner, 2026-11-05).
        self.assertNotIn('kiro', NAMES)
        self.assertEqual(retired_notice('kiro'), NOTICE.replace('Neeko', 'Kiro'))

    def test_neeko_next_is_retired_and_its_live_config_is_ignored(self):
        # Neeko CLI v3, removed 2026-11-02 (Neeko is no longer maintained). The live harnesses.json
        # still carries its options.
        self.assertNotIn('neeko-next', NAMES)
        self.assertEqual(retired_notice('neeko-next'), NOTICE.replace('Neeko', 'Neeko Next'))
        config = self.root / 'harnesses.json'
        config.write_text(json.dumps({'harnesses': {'neeko-next': {'args': [
            '--neeko-next-cmd', '/opt/neeko/bin/neeko-next', '--neeko-next-business', 'neeko-workspace-app']}}}))
        logs = []
        app, _ = self.build('--harness-config', str(config), log=logs.append)
        self.assertNotIn('neeko-next', app.apps)
        self.assertIn('harness config ignores Harnesses not in the roster: neeko-next', logs)


SETUP_SCRIPT = ROOT / "scripts/setup_harness_bridge.sh"


class SetupHarnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = exec_tempdir()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        scripts = self.root / "scripts"
        scripts.mkdir()
        shutil.copyfile(SETUP_SCRIPT, scripts / SETUP_SCRIPT.name)
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        shutil.copyfile(ROOT / "bin/covey_paths.py", bin_dir / "covey_paths.py")
        (self.root / "covey/core").mkdir(parents=True)
        for name in ("__init__.py", "core/__init__.py", "core/paths.py", "core/env.py"):
            shutil.copyfile(ROOT / "covey" / name, self.root / "covey" / name)
        package_dirs = extensions.package_dirs()  # Wherever the extension is: extensions/ here or a checkout beside the core.
        hook_source = next((d / "scripts/setup_hook.sh" for d in package_dirs if (d / "scripts/setup_hook.sh").is_file()), None)
        if hook_source:  # The extension's setup hook (MASO runtime detection) is what these tests exercise.
            shutil.copyfile(ROOT / "covey/core/extensions.py", self.root / "covey/core/extensions.py")
            hook = self.root / "extensions/covey_internal/scripts"
            hook.mkdir(parents=True)
            (self.root / "extensions/enabled").write_text("covey_internal\n")
            (self.root / "extensions/covey_internal/__init__.py").write_text("")
            shutil.copyfile(hook_source, hook / "setup_hook.sh")
        (bin_dir / "covey_maso_runtime.py").write_text("# Stub: runtime imports are tested separately.\n")
        self.bin = self.root / "fake-bin"
        self.bin.mkdir()
        for name in ("bash", "dirname", "python3"):
            (self.bin / name).symlink_to(shutil.which(name))
        self.stub("rsync", "exit 0")
        self.calls = self.root / "calls"
        for name, body in (("install_skills.sh", 'echo install-skills >> "$SETUP_TEST_CALLS"'),
                           ("bootstrap_harness_bridge.sh", 'printf "bootstrap <%s>\\n" "$@" >> "$SETUP_TEST_CALLS"')):
            path = scripts / name
            path.write_text("#!/bin/bash\n" + body + "\n")
            path.chmod(0o755)
        self.stub("lark-cli", '''echo "lark $*" >> "$SETUP_TEST_CALLS"
case "$*" in
  *"config show"*) exit "${SETUP_TEST_CONFIG_RC:-0}" ;;
  *"auth status"*) echo '{"appId":"cli_unit"}'; exit "${SETUP_TEST_AUTH_RC:-0}" ;;
esac
exit 0''')
        self.python = self.root / "isolated python"
        self.python.write_text('''#!/bin/bash
[ "$2" = --check-runtime ] || exit 91
echo "probe" >> "$SETUP_TEST_CALLS"
[ "${SETUP_TEST_PROBE_RC:-0}" = 0 ] || exit 1
echo '{"ok":true}'
''')
        self.python.chmod(0o755)

    def stub(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/bash\n" + body + "\n")
        path.chmod(0o755)

    def run_setup(self, *args, start=False, **env):
        base = {k: v for k, v in os.environ.items() if not k.startswith(("LARK_", "HARNESS_"))}
        return subprocess.run(
            ["/bin/bash", str(self.root / "scripts" / SETUP_SCRIPT.name),
             "--workdir", str(self.root), "--skip-bytedcli", *([] if start else ["--no-start"]), *args],  # privacy: allow 旧选项名，核心保留，扩展的安装钩子读它
            env={**base, "PATH": str(self.bin), "SETUP_TEST_CALLS": str(self.calls),
                 "COVEY_DATA_HOME": str(self.root / "data"), **env},
            capture_output=True, text=True, timeout=10,
        )

    def call_lines(self):
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def test_no_usable_harness_fails_before_install(self):
        result = self.run_setup("--non-interactive")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no usable Harness", result.stderr)
        self.assertEqual(self.call_lines(), [])

    def test_removed_single_harness_options_are_rejected(self):
        self.stub("codex", "exit 0")
        for args in (("--harness", "codex"), ("--maso-profile", "maso")):
            with self.subTest(args=args):
                result = self.run_setup(*args)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("unknown argument", result.stderr)
        self.assertEqual(self.call_lines(), [])

    @requires_extension()
    def test_broken_optional_maso_runtime_does_not_block_other_harnesses(self):
        self.stub("codex", "exit 0")
        result = self.run_setup("--non-interactive", LARK_MASO_BRIDGE_PYTHON=str(self.python), SETUP_TEST_PROBE_RC="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("available Harnesses: codex\n", result.stdout)

    @requires_extension()
    def test_verified_maso_runtime_counts_without_any_cli(self):
        result = self.run_setup("--non-interactive", LARK_MASO_BRIDGE_PYTHON=str(self.python))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("available Harnesses: maso", result.stdout)

    def test_dsh_is_detected_from_its_cli(self):
        self.stub("dsh", "exit 91")
        result = self.run_setup("--non-interactive", HOME=str(self.root / "home"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("available Harnesses: dsh", result.stdout)

    @requires_extension()
    def test_default_maso_runtime_uses_external_data_home(self):
        external = self.root / "data/maso-runtime/venv/bin/python"
        external.parent.mkdir(parents=True)
        shutil.copy2(self.python, external)
        result = self.run_setup("--non-interactive")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("available Harnesses: maso", result.stdout)

    def test_noninteractive_missing_auth_never_starts_login(self):
        self.stub("codex", "exit 0")
        result = self.run_setup("--non-interactive", SETUP_TEST_AUTH_RC="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("run auth login interactively", result.stderr)
        self.assertNotIn("auth login", self.calls.read_text())

    def test_noninteractive_missing_profile_never_initializes_app(self):
        self.stub("codex", "exit 0")
        result = self.run_setup("--non-interactive", SETUP_TEST_CONFIG_RC="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("lark-cli profile harness is missing", result.stderr)
        self.assertNotIn("config init", self.calls.read_text())

    def test_authenticated_no_start_does_not_claim_live_verification(self):
        self.stub("codex", "exit 0")
        result = self.run_setup("--non-interactive", "--profile", "my-bot")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("still need end-to-end verification", result.stdout)
        self.assertIn("lark --profile my-bot auth status --verify --json", self.call_lines())
        self.assertNotIn("auth login", self.calls.read_text())
        self.assertFalse(any(line.startswith("bootstrap") for line in self.call_lines()))

    def test_start_passes_resolved_app_and_options_without_shell_splitting(self):
        self.stub("codex", "exit 0")
        config = self.root / "harness config.json"
        config.write_text("{}")
        result = self.run_setup("--non-interactive", "--owner", "ou_owner", "--harness-config", str(config),
                                "--state-dir", str(self.root / "bot state"), start=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = [line[len("bootstrap <"):-1] for line in self.call_lines() if line.startswith("bootstrap")]
        self.assertEqual(argv, ["--workdir", str(self.root), "--profile", "harness", "--app-id", "cli_unit",
                                "--state-dir", str(self.root / "bot state"), "--owner", "ou_owner",
                                "--harness-config", str(config)])

    def test_mismatched_app_id_never_starts(self):
        self.stub("codex", "exit 0")
        result = self.run_setup("--non-interactive", "--app-id", "cli_other", start=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not --app-id cli_other", result.stderr)
        self.assertFalse(any(line.startswith("bootstrap") for line in self.call_lines()))
