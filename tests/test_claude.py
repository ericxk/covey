"""Claude Code over stream-json: result verification, failure codes, background waits, events,
native questions and the model catalog; the installed CLI contract runs only when enabled.

The shared delivery invariants (saved redelivery, sealed sessions) are in test_backend_contract.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support import slow
from support.clock import FakeClock
from support.fake_backends import CLAUDE_JSONL, OTHER, RUN, SID, ScriptedClaudeProcess, assistant, codex_context, result
from support.worker_seams import leave_out_side_work
from support.fake_lark import OutboundTransport, install_card_transport
from support.world import harness_app

from covey.app.unified import UnifiedBridgeApp
from covey.backends.claude.bridge import AUTO_COMPACT_ENV, CLAUDE_SPEC, GATEWAY_MODELS_ENV, ClaudeBackend
from covey.backends.claude.rewind import FILE_CHECKPOINTS_ENV
from covey.backends.claude import argv as claude_argv, runtime, subagents
from covey.core import butler_settings
from covey.core import home
from covey.backends.claude.completion import ClaudeReplyState
from covey.backends.claude.events import ClaudeEventAdapter
from covey.backends.claude.models import GATEWAY_KEEP, keep_gateway_models, normalize_catalog
from covey.backends.claude.sessions import ClaudeSessionStore
from covey.core.harness import RunCallbacks, RunInputs, RunRequest, RunResult
from covey.core.jobs import BridgeReplyJob, find_job_record
from covey.core.process_env import child_process_env
from covey.core.serving import serve_thread


class ClaudeAlignmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ctx = dataclasses.replace(codex_context(self.root, "stream-json"), backend_cmd=["never-run-claude"])
        self.backend = ClaudeBackend()
        self.saved, self.events, self.processes, self.sessions = [], [], [], []
        self.cancel = threading.Event()

    def finish_rows(self, rows=None, rc=0, session=None):
        """One turn's rows fed straight into the completion state ``run_stream`` uses."""
        rows = rows if rows is not None else [{"type": "system", "subtype": "init", "session_id": SID},
                                              assistant(), result()]
        state = ClaudeReplyState(self.ctx, session, run_id=RUN, publish=self.saved.append)
        for row in [*rows, {"type": "system", "subtype": "session_state_changed", "session_id": SID,
                            "state": "idle"}]:
            state.accept(row)
        return state.finish(rc, cleanup=True)

    def run_native(self, scenario="success", event_hook=None, options=None, timeout=4):
        ctx = dataclasses.replace(
            self.ctx,
            timeout=timeout,
            options={"transport": "stream-json", **(options or {})},
            backend_cmd=[sys.executable, str(CLAUDE_JSONL), scenario],
        )

        def emit(k, d, c):
            self.events.append((k, d))
            if event_hook:
                event_hook(k, d, c)

        answer = self.backend.run(
            RunRequest(
                ctx,
                "prompt",
                callbacks=RunCallbacks(
                    event=emit,
                    process=self.processes.append,
                    verified_reply=self.saved.append,
                    session=self.sessions.append,
                ),
                inputs=RunInputs(self.cancel, run_id=RUN, attachments=tuple(ctx.options.get("attachments", []))),
            )
        )
        self.assertTrue(all(p.poll() is not None for p in self.processes))
        return answer

    def test_verified_result_completes_the_turn(self):
        answer = self.finish_rows()
        self.assertIsInstance(answer, RunResult)
        self.assertFalse(answer.is_error)
        self.assertEqual(answer.public_reply.text, "Complete answer")
        self.assertTrue(answer.outcome.resume_allowed)
        self.assertEqual(answer.public_reply.backend, "claude")

    def test_failure_categories_and_private_diagnostics(self):
        for row, category in [
            (result(subtype="error_max_turns", is_error=True, errors=["PRIVATE_ERROR_SENTINEL"]), "max_turns"),
            (result(subtype="error_max_budget_usd", is_error=True), "budget_exceeded"),
            (result(terminal_reason="aborted_tools"), "cancelled"),
            (result(terminal_reason="aborted_streaming"), "cancelled"),
            (result(is_error=True, api_error_status=403), "authentication_error"),
            (result(is_error=True, api_error_status=429), "rate_limited"),
            (result(is_error=True, api_error_status=402), "quota_exhausted"),
            (result(is_error=True, api_error_status=451), "model_blocked"),
            (result(result=""), "output_missing"),
            (result(stop_reason="max_tokens"), "root_incomplete"),
        ]:
            with self.subTest(category=category):
                answer = self.finish_rows([{"type": "system", "subtype": "init", "session_id": SID}, assistant(), row])
                self.assertTrue(answer.is_error)
                self.assertIsNone(answer.public_reply)
                self.assertEqual(answer.outcome.error_code, category)
                self.assertNotIn("PRIVATE", answer.text)
        diag = self.ctx.state_dir / "claude-diagnostics" / (RUN + ".json")
        self.assertEqual(diag.stat().st_mode & 0o777, 0o600)

    def test_identity_and_complete_text_required(self):
        initial = {"type": "system", "subtype": "init", "session_id": SID}
        for rows in (
            [assistant(), result()],
            [initial, assistant(), result(session_id=OTHER)],
            [initial, assistant(parent_tool_use_id="child"), result()],
            [initial, assistant("wrong body"), result()],
            [initial, assistant(), result(uuid="")],
            [initial, result()],
            [initial, assistant(), result(is_error="false")],
        ):
            with self.subTest(rows=rows):
                answer = self.finish_rows(rows)
                self.assertTrue(answer.is_error)
                self.assertIsNone(answer.public_reply)
        self.assertTrue(self.finish_rows(session=OTHER).is_error)

    def test_public_sections_preserved_without_child_thinking_or_tools(self):
        rows = [
            {"type": "system", "subtype": "init", "session_id": SID},
            assistant("Introduction", "one"),
            assistant("CHILD_ONLY", "child", parent_tool_use_id="task"),
            {
                "type": "assistant",
                "session_id": SID,
                "uuid": "hidden",
                "message": {"content": [{"type": "thinking", "thinking": "PRIVATE_REASONING"}]},
            },
            assistant(),
            result(),
        ]
        answer = self.finish_rows(rows)
        self.assertEqual(answer.text, "Introduction\n\nComplete answer")

    def test_nonzero_exit_never_authorizes_the_body(self):
        self.assertIsNone(self.finish_rows(rc=7).public_reply)

    def test_native_subprocess_success_and_failure_cleanup(self):
        for scenario, code in [
            ("success", ""),
            ("foreign", "identity_mismatch"),
            ("aborted", "cancelled"),
            ("errors", "max_turns"),
            ("malformed", "protocol_error"),
            ("nonzero", "process_exit"),
        ]:
            with self.subTest(scenario=scenario):
                answer = self.run_native(scenario)
                self.assertEqual(answer.outcome.error_code, code)
                self.assertEqual(answer.outcome.resume_allowed, not bool(code))

    def test_native_question_multiselect_and_ack(self):
        def event(k, d, c):
            if k == "question":
                self.assertTrue(c.verify_question_request(d["live_question_proof"]))
                c.answer(d["request_id"], {"0": {"answers": ["A", "B"]}})
                with self.assertRaises(ValueError):
                    c.answer(d["request_id"], {"0": {"answers": ["A"]}})

        answer = self.run_native("question", event)
        self.assertFalse(answer.is_error)
        self.assertEqual(answer.text, "Answered correctly")
        self.assertTrue(any(k == "question_resolved" for k, d in self.events))
        self.assertNotIn("PRIVATE_TOOL_RESULT", str(self.events))

    def test_shared_callbacks_do_not_hold_control_lock(self):
        def event(kind, data, control):
            if kind != "question":
                return
            acquired = threading.Event()

            def concurrent_action():
                with control.control_lock:
                    acquired.set()

            action = threading.Thread(target=concurrent_action, daemon=True)
            action.start()
            action.join(1)
            self.assertTrue(acquired.is_set(), "Callback holds the control lock and can deadlock shared persistence")
            control.answer(data["request_id"], {"0": {"answers": ["A", "B"]}})

        self.assertFalse(self.run_native("question", event).is_error)

    def test_catalog_cache_refresh_and_selection_validation(self):
        from covey.backends.claude.models import model_catalog

        process = mock.Mock()
        process.close.return_value = True
        process.initialize.return_value = {
            "models": [{"value": "a", "supportsEffort": True, "supportedEffortLevels": ["high", "max"]}]
        }
        with mock.patch("covey.backends.claude.models.ClaudeProcess", return_value=process) as launch:
            catalog = model_catalog(self.backend, self.ctx, {})
            catalog["a"]["supported_reasoning_levels"].clear()
            self.assertEqual(len(model_catalog(self.backend, self.ctx, {})["a"]["supported_reasoning_levels"]), 2)
            self.assertEqual(launch.call_count, 1)
            with mock.patch.object(
                self.backend, "model_catalog", return_value=model_catalog(self.backend, self.ctx, {})
            ):
                self.assertEqual(self.backend.normalize_model_settings(self.ctx, "a", "max"), ("a", "max"))
                for effort in ("xhigh", "ultracode"):  # No xhigh, so no ultracode either.
                    with self.subTest(effort=effort), self.assertRaises(ValueError):
                        self.backend.normalize_model_settings(self.ctx, "a", effort)
                with self.assertRaises(ValueError):
                    self.backend.normalize_model_settings(self.ctx, "old-model", "max")
            process.initialize.side_effect = RuntimeError("offline")
            for refresh in (True, False):
                with self.assertRaises(RuntimeError):
                    model_catalog(self.backend, self.ctx, {}, refresh=refresh)
            self.assertEqual(launch.call_count, 3)  # Refresh failure cannot reuse stale cache.
            process.initialize.side_effect = subprocess.TimeoutExpired("claude", 30)
            with self.assertRaisesRegex(RuntimeError, "超时"):
                model_catalog(self.backend, self.ctx, {}, refresh=True)

    def test_steer_waits_for_accepted_supplement(self):
        sent = []

        def event(k, d, c):
            if k == "answer_snapshot" and d["text"] == "First part" and not sent:
                sent.append(True)
                c.steer("Supplement", "event-1")

        answer = self.run_native("steer", event)
        self.assertFalse(answer.is_error)
        self.assertIn("Final after supplement", answer.text)
        self.assertEqual(len([b for b in answer.public_reply.boundary if b.startswith("input:")]), 2)

    def test_a_supplement_starting_with_a_slash_is_not_a_claude_command(self):
        # Claude Code runs a user message that starts with "/" as its own command (2026-10-26).
        from covey.backends.claude.control import ClaudeControl
        control = object.__new__(ClaudeControl)
        control.control_lock, control.user_requests, control.closing = threading.Lock(), set(), False
        control.accepting_steer, control.steers = True, {}
        control.send_user = mock.Mock()
        control.steer("/mnt/data/x 也看看", "e1")
        control.steer("普通补充", "e2")
        self.assertEqual([c.args[0] for c in control.send_user.call_args_list], ["用户补充：/mnt/data/x 也看看", "普通补充"])

    def test_unacknowledged_input_never_completes(self):
        answer = self.run_native("missing-ack", options={"completion_wait_seconds": 0.1})
        self.assertEqual(answer.outcome.error_code, "settle_timeout")
        self.assertIsNone(answer.public_reply)

    def test_cancel_and_timeout_reap_only_owned_process(self):
        def event(k, d, c):
            if k == "answer_snapshot":
                self.cancel.set()

        answer = self.run_native("cancel", event)
        self.assertEqual(answer.outcome.status, "cancelled")
        self.cancel.clear()
        answer = self.run_native("timeout", timeout=1)
        self.assertEqual(answer.outcome.status, "timed_out")

    def test_child_failure_handled_before_result_completes(self):
        answer = self.run_native("child-failed")
        self.assertEqual(answer.outcome.status, "completed")
        self.assertFalse(answer.is_error)
        self.assertEqual(answer.public_reply.text, "Complete answer")
        self.assertTrue(answer.outcome.resume_allowed)

    def test_failed_child_preserves_body_without_resume(self):
        answer = self.run_native("child-failed-late")
        self.assertEqual(answer.outcome.error_code, "child_error")
        self.assertEqual(answer.public_reply.text, "Complete answer")
        self.assertTrue(answer.outcome.delivery_authorized)
        self.assertFalse(answer.outcome.resume_allowed)

    def test_background_task_ending_after_the_result_waits_for_its_notification_turn(self):
        answer = self.run_native("background-late")
        self.assertEqual(answer.outcome.status, "completed")
        self.assertTrue(answer.public_reply.text.endswith("Final after background"))
        # The result that predates the task's end is never offered for delivery.
        self.assertTrue(all(r.text.endswith("Final after background") for r in self.saved if r is not None))

    def test_a_notification_turn_saying_the_reply_again_is_not_sent_twice(self):
        # 2026-11-16: after each of three background notifications the model wrote the same sentence, and the reply
        # carried it three times. Only a repeat right after the same text goes; a different text in between stays.
        note = {"kind": "task-notification"}
        idle = {"type": "system", "subtype": "session_state_changed", "session_id": SID, "state": "idle"}
        state = ClaudeReplyState(self.ctx, None)
        for row in ({"type": "system", "subtype": "init", "session_id": SID},
                    assistant("Done at 09:47", key="a1"), result(result="Done at 09:47", uuid="r1"), idle,
                    assistant("Done at 09:47", key="a2"), result(result="Done at 09:47", uuid="r2", origin=note), idle,
                    assistant("Done at 09:47", key="a3"), result(result="Done at 09:47", uuid="r3", origin=note), idle):
            state.accept(row)
        reply = state.capture()
        self.assertEqual((reply.text, reply.message_ids), ("Done at 09:47", ("a1",)))
        for row in (assistant("The watch ended too.", key="a4"), assistant("Done at 09:47", key="a5"),
                    result(result="Done at 09:47", uuid="r4", origin=note), idle):
            state.accept(row)
        reply = state.capture()
        self.assertEqual(reply.text, "Done at 09:47\n\nThe watch ended too.\n\nDone at 09:47")
        self.assertEqual(reply.message_ids, ("a1", "a4", "a5"))

    def test_task_end_after_the_result_is_settled_only_by_a_later_result(self):
        state = ClaudeReplyState(self.ctx, None)
        notification = {"type": "system", "subtype": "task_notification", "session_id": SID, "task_id": "bg",
                        "status": "completed"}
        for row in ({"type": "system", "subtype": "init", "session_id": SID},
                    {"type": "system", "subtype": "task_started", "session_id": SID, "task_id": "bg"},
                    assistant(), result(),
                    {"type": "system", "subtype": "session_state_changed", "session_id": SID, "state": "idle"},
                    notification):
            state.accept(row)
        self.assertTrue(state.ready())
        self.assertFalse(state.settled())
        # A notification Claude Code handled without a model turn settles it too.
        state.accept(result(origin={"kind": "task-notification"}, num_turns=0, result="", uuid="note"))
        self.assertTrue(state.settled())

    @slow('a real CLI that keeps running after its output ends; test_output_ending_before_the_exit_is_a_process_failure_not_a_timeout covers the outcome')
    def test_a_slow_exit_after_the_output_ends_is_not_reported_as_a_timeout(self):
        # The fixture stays 6 s after closing its output: well past this wait (3 s in the Bridge).
        with mock.patch("covey.backends.claude.runtime.EXIT_AFTER_EOF", 0.2):
            answer = self.run_native("slow-exit", timeout=0)
        self.assertEqual((answer.outcome.status, answer.outcome.error_code), ("failed", "process_exit"))

    def test_an_unwritable_diagnostic_never_loses_the_outcome(self):
        with mock.patch("covey.backends.claude.completion.write_private_text", side_effect=OSError("full")):
            answer = self.finish_rows(rc=7)
        self.assertEqual((answer.outcome.status, answer.outcome.error_code), ("failed", "process_exit"))

    @slow('the real CLI and clock; test_a_background_wait_lasts_until_the_stop_or_the_task_timeout covers it')
    def test_without_a_limit_a_background_task_is_waited_for_until_the_user_stops(self):
        import time
        stop = threading.Timer(1.5, self.cancel.set)
        started = time.monotonic()
        stop.start()
        self.addCleanup(stop.cancel)
        answer = self.run_native("background-running", timeout=0)
        self.assertGreaterEqual(time.monotonic() - started, 1.4)  # Nothing ended it before the stop.
        self.assertEqual((answer.outcome.status, answer.outcome.error_code), ("cancelled", "cancelled"))
        # Stopped while only the background task ran: that reply is still the answer.
        self.assertEqual(answer.public_reply.text, "Reply 1")
        self.assertEqual(answer.outcome.reply_fingerprint, answer.public_reply.fingerprint)
        self.assertTrue(answer.outcome.delivery_authorized)

    @slow('the real CLI and clock; test_a_background_wait_lasts_until_the_stop_or_the_task_timeout covers it')
    def test_the_last_reply_still_goes_out_when_the_task_timeout_ends_a_background_wait(self):
        answer = self.run_native("background-running", timeout=2)
        self.assertEqual((answer.outcome.status, answer.outcome.error_code), ("timed_out", "background_timeout"))
        self.assertEqual(answer.public_reply.text, "Reply 1")
        self.assertTrue(answer.outcome.delivery_authorized)
        self.assertFalse(answer.outcome.resume_allowed)

    def test_the_last_reply_still_goes_out_when_a_configured_wait_runs_out(self):
        answer = self.run_native("background-running", options={"completion_wait_seconds": 0.5}, timeout=20)
        self.assertEqual((answer.outcome.status, answer.outcome.error_code), ("timed_out", "background_timeout"))
        self.assertEqual(answer.public_reply.text, "Reply 1")
        self.assertEqual(answer.outcome.reply_fingerprint, answer.public_reply.fingerprint)
        self.assertTrue(answer.outcome.delivery_authorized)
        self.assertFalse(answer.outcome.resume_allowed)
        self.assertEqual(self.saved[-1], answer.public_reply)

    def test_every_run_appends_the_bridge_rules_to_the_system_prompt(self):
        import functools
        from covey.backends.claude import runtime
        from covey.backends.claude.argv import stream_argv
        from covey.core.completion import CompletionBudget
        clock, argvs, sent = FakeClock(), [], []
        rules = "You are Claude Code, replying to the user through Feishu.\n\nConstraints for this bridge:\n- 规则"

        def launch(argv, ctx, _env, cancel=None, observe=None):
            argvs.append(argv)
            process = ScriptedClaudeProcess(clock, ctx, "success", cancel=cancel)
            process.send = lambda row, send=process.send: (sent.append(row), send(row))
            return process
        # The system channel, for when Claude declares system_instructions again (off since 2026-11-02:
        # test_harness's test_claude_and_codex_carry_the_rules_in_the_message).
        # A new session, a resumed one, and a fork of another: the rules are passed on every invocation.
        for sid, fork in ((None, ""), (SID, ""), (None, OTHER)):
            with self.subTest(resumed=bool(sid), fork=bool(fork)):
                ctx = dataclasses.replace(self.ctx, options={"transport": "stream-json",
                                                             **({"fork_session": fork} if fork else {})})
                with mock.patch.object(runtime, "ClaudeProcess", launch), mock.patch.object(
                        runtime, "CompletionBudget", functools.partial(CompletionBudget, clock=clock.monotonic)):
                    self.backend.run(RunRequest(ctx, "本轮消息", sid, inputs=RunInputs(self.cancel, run_id=RUN),
                                                instructions=rules))
                argv = argvs[-1]
                self.assertEqual(argv[argv.index("--append-system-prompt") + 1], rules)
                self.assertEqual("--fork-session" in argv, bool(fork))
                user = [row for row in sent if row.get("type") == "user"][-1]
                self.assertNotIn("Constraints for this bridge", json.dumps(user, ensure_ascii=False))
                self.assertIn("本轮消息", json.dumps(user, ensure_ascii=False))
        self.assertNotIn("--append-system-prompt", stream_argv(self.backend, self.ctx))
        self.assertNotIn("--append-system-prompt", stream_argv(self.backend, self.ctx, catalog=True,
                                                                instructions=rules))

    def run_scripted(self, scenario, *, options=None, timeout=0, stop_at=None, event_hook=None):
        """run_native with the CLI replaced by ScriptedClaudeProcess and the wait on its fake clock."""
        import functools
        from covey.backends.claude import control, runtime
        from covey.core.completion import CompletionBudget
        clock = FakeClock()
        stop = None if stop_at is None else clock.monotonic() + stop_at
        with mock.patch.object(runtime, "ClaudeProcess",
                               lambda _argv, ctx, _env, cancel=None, observe=None: ScriptedClaudeProcess(
                                   clock, ctx, scenario, stop_at=stop, cancel=cancel)), \
                mock.patch.object(runtime, "time", clock), mock.patch.object(control, "time", clock), \
                mock.patch.object(runtime, "CompletionBudget", functools.partial(CompletionBudget, clock=clock.monotonic)):
            return self.run_native(scenario, event_hook, options=options, timeout=timeout), clock

    def test_each_reply_restarts_the_configured_wait_until_the_total_limit(self):
        # The background-chain run below, on a fake clock: replies 1.2 s apart each restart
        # the configured 2 s wait, so the third reply is the one sent ...
        for options, expected in (({"completion_wait_seconds": 2.0}, "Reply 3"),
                                  # ... and a total limit counted from the first reply ends it after the second.
                                  ({"completion_wait_seconds": 2.0, "completion_total_seconds": 1.8}, "Reply 2")):
            with self.subTest(**options):
                answer, _ = self.run_scripted("background-chain", options=options)
                self.assertEqual(answer.outcome.error_code, "background_timeout")
                self.assertEqual(answer.public_reply.text, expected)
                self.assertTrue(answer.outcome.delivery_authorized)

    def test_a_background_wait_lasts_until_the_stop_or_the_task_timeout(self):
        # Fake-clock twins of the real-process tests below.
        answer, clock = self.run_scripted("background-running", stop_at=600)
        self.assertGreaterEqual(clock.monotonic() - 1000, 600)  # No limit: nothing ended it before the stop.
        self.assertEqual((answer.outcome.status, answer.outcome.error_code), ("cancelled", "cancelled"))
        self.assertEqual(answer.public_reply.text, "Reply 1")
        self.assertEqual(answer.outcome.reply_fingerprint, answer.public_reply.fingerprint)
        self.assertTrue(answer.outcome.delivery_authorized)
        self.cancel.clear()
        answer, _ = self.run_scripted("background-running", timeout=2)
        self.assertEqual((answer.outcome.status, answer.outcome.error_code), ("timed_out", "background_timeout"))
        self.assertEqual(answer.public_reply.text, "Reply 1")
        self.assertTrue(answer.outcome.delivery_authorized)
        self.assertFalse(answer.outcome.resume_allowed)

    def test_a_cli_that_does_not_exit_after_a_settled_result_still_delivers_it(self):
        for scenario, text in (("settled-hang", "Complete answer"), ("late-turn-hang", "Late reply")):
            with self.subTest(scenario=scenario):
                self.saved.clear()
                answer, clock = self.run_scripted(scenario)
                self.assertGreaterEqual(clock.monotonic() - 1000, 10)  # The settle wait ran out.
                self.assertEqual((answer.outcome.status, answer.outcome.error_code), ("completed", ""))
                self.assertTrue(answer.public_reply.text.endswith(text))
                self.assertEqual(answer.outcome.reply_fingerprint, answer.public_reply.fingerprint)
                self.assertTrue(answer.outcome.delivery_authorized)
                self.assertTrue(answer.outcome.resume_allowed)
                self.assertEqual(self.saved[-1], answer.public_reply)

    def test_a_settle_wait_that_ends_on_an_api_error_says_which(self):
        answer, _ = self.run_scripted("rate-limited-hang")
        self.assertEqual((answer.outcome.status, answer.outcome.error_code), ("failed", "rate_limited"))
        self.assertIsNone(answer.public_reply)
        diag = json.loads((self.ctx.state_dir / "claude-diagnostics" / (RUN + ".json")).read_text())
        self.assertEqual((diag["error_code"], diag["api_error_status"]), ("rate_limited", 429))

    def test_a_supplement_queued_during_a_failed_turn_gets_its_own_turn(self):
        # Task #1556: the supplement waited in the CLI while the gateway's 502s were retried; closing input on
        # the failed result stopped the CLI 10 s into the supplement's turn, as a settle_timeout.
        sent = []

        def event(k, d, c):
            if k == "answer_snapshot" and d["text"] == "First part" and not sent:
                sent.append(True)
                c.steer("Supplement", "event-1")

        answer, clock = self.run_scripted("queued-after-failure", event_hook=event)
        self.assertEqual(sent, [True])
        self.assertGreaterEqual(clock.monotonic() - 1000, 35)  # The supplement's 30 s turn ran to its end.
        self.assertEqual((answer.outcome.status, answer.outcome.error_code), ("completed", ""))
        self.assertTrue(answer.public_reply.text.endswith("Final after supplement"))
        self.assertIn(("steer_read", {"event_id": "event-1"}), self.events)
        # A CLI that never takes the supplement up still ends on the failed turn after the settle wait.
        sent.clear()
        self.events.clear()
        answer, clock = self.run_scripted("queue-lost-after-failure", event_hook=event)
        self.assertLess(clock.monotonic() - 1000, 20)
        self.assertEqual((answer.outcome.status, answer.outcome.error_code), ("failed", "server_fault"))  # The API 502.
        self.assertIsNone(answer.public_reply)

    def test_a_settle_wait_with_no_finished_turn_is_still_a_settle_timeout(self):
        answer, _ = self.run_scripted("unfinished-hang")
        self.assertEqual((answer.outcome.status, answer.outcome.error_code), ("timed_out", "settle_timeout"))
        self.assertIsNone(answer.public_reply)

    def test_a_cli_stopped_after_a_settled_result_without_confirmed_cleanup_sends_nothing(self):
        with mock.patch.object(ScriptedClaudeProcess, "close", return_value=False):
            answer, _ = self.run_scripted("settled-hang")
        self.assertEqual((answer.outcome.status, answer.outcome.error_code), ("unknown", "cleanup_unconfirmed"))
        self.assertFalse(answer.outcome.delivery_authorized)

    def test_every_rate_limit_ends_as_rate_limited(self):
        init = {"type": "system", "subtype": "init", "session_id": SID}
        gave_up = {"type": "assistant", "session_id": SID, "uuid": "api-error", "error": "rate_limit",
                   "message": {"id": "api-error", "content": [{"type": "text", "text": "API Error: 429"}]}}
        text = 'API Error: 429 {"type":"error","error":{"type":"rate_limit_error"}}'
        for name, rows, rc in (
                ("result text", [init, result(is_error=True, terminal_reason="api_error", result=text)], 0),
                ("result errors", [init, result(subtype="error_during_execution", is_error=True, errors=[text])], 0),
                ("assistant error, no result", [init, gave_up], 1),
                ("assistant error, failed result", [init, gave_up, result(is_error=True, result="API Error")], 0)):
            with self.subTest(name):
                answer = self.finish_rows(rows, rc=rc)
                self.assertEqual((answer.outcome.status, answer.outcome.error_code), ("failed", "rate_limited"))
        # Overloaded (529) is not a rate limit, as in the other backends: a passing server fault.
        overloaded = result(is_error=True, terminal_reason="api_error", api_error_status=529,
                            result='API Error: 529 {"type":"error","error":{"type":"overloaded_error"}}')
        self.assertEqual(self.finish_rows([init, overloaded]).outcome.error_code, "server_fault")
        # A rate limit whose cleanup is unconfirmed still says rate limit; nothing is sent.
        state = ClaudeReplyState(self.ctx, None, run_id=RUN, publish=self.saved.append)
        for row in (init, result(is_error=True, terminal_reason="api_error", api_error_status=429)):
            state.accept(row)
        outcome = state.finish(None, cleanup=False).outcome
        self.assertEqual((outcome.status, outcome.error_code, outcome.cleanup_confirmed), ("unknown", "rate_limited", False))
        diag = json.loads((self.ctx.state_dir / "claude-diagnostics" / (RUN + ".json")).read_text())
        self.assertIs(diag["cleanup_confirmed"], False)

    def test_output_ending_before_the_exit_is_a_process_failure_not_a_timeout(self):
        answer, _ = self.run_scripted("slow-exit")
        self.assertEqual((answer.outcome.status, answer.outcome.error_code), ("failed", "process_exit"))

    @slow("the real CLI process and clock; the fake-clock test above covers the renewal")
    def test_a_configured_wait_restarts_at_each_reply_until_the_total_limit(self):
        # Replies about 0.6 s apart (#577 watched a run like this for an hour); each one restarts
        # the configured 1 s wait, so the third reply is the one sent.
        answer = self.run_native("background-chain:0.6", options={"completion_wait_seconds": 1.0}, timeout=30)
        self.assertEqual(answer.outcome.error_code, "background_timeout")
        self.assertEqual(answer.public_reply.text, "Reply 3")
        # A total limit counted from the first reply ends it earlier, after the second.
        answer = self.run_native("background-chain:0.6", options={"completion_wait_seconds": 1.0,
                                                                  "completion_total_seconds": 0.9}, timeout=30)
        self.assertEqual(answer.outcome.error_code, "background_timeout")
        self.assertEqual(answer.public_reply.text, "Reply 2")

    def test_wait_budget_renews_from_each_reply_within_its_limits(self):
        from covey.core.completion import CompletionBudget
        now = [100.0]
        budget = CompletionBudget(None, 60, total_seconds=150, clock=lambda: now[0])
        budget.renew()  # The first reply starts it.
        self.assertEqual(budget.deadline, 160)
        now[0] = 150
        budget.renew()
        self.assertEqual(budget.deadline, 210)
        now[0] = 200
        budget.renew()
        self.assertEqual(budget.deadline, 250)  # Never past the total from the first reply.
        task = CompletionBudget(230, None, total_seconds=1000, clock=lambda: now[0])
        task.renew()
        self.assertEqual(task.deadline, 230)  # Nor past the task deadline.
        plain = CompletionBudget(clock=lambda: now[0])
        plain.start()
        plain.start()
        self.assertEqual(plain.deadline, 200 + 3600)  # MASO's default, without renewal, as before.
        unlimited = CompletionBudget(default_wait=None, clock=lambda: now[0])  # Claude's
        unlimited.renew()
        now[0] = 10 ** 9
        unlimited.renew()
        self.assertIsNone(unlimited.deadline)
        self.assertFalse(unlimited.expired())

    def test_the_prompt_tells_claude_how_background_tasks_keep_the_task_open(self):
        native = dataclasses.replace(self.ctx, timeout=0, options={"transport": "stream-json"})
        note, = self.backend.prompt_notes(native)
        self.assertIn("直到它们结束，没有时限", note)
        self.assertIn("不要在后台留下服务", note)
        self.assertIn("等它出来再结束这一轮", note)
        limited = dataclasses.replace(native, timeout=1800)
        note, = self.backend.prompt_notes(limited)
        self.assertIn("桥接器最多等到任务超时（30 分钟）", note)
        self.assertIn("最后一条完整的回复", note)
        self.assertIn("每条回复都要能单独成立", note)
        configured = dataclasses.replace(native, options={"transport": "stream-json", "completion_wait_seconds": 7200})
        self.assertIn("桥接器最多等到每次回复后 2 小时", self.backend.prompt_notes(configured)[0])

    def test_native_child_text_does_not_replace_root(self):
        answer = self.run_native("child")
        self.assertEqual(answer.text, "Complete answer")
        self.assertNotIn("CHILD_ONLY", [d["text"] for k, d in self.events if k == "answer_snapshot"])

    def test_subagent_usage_settles_from_the_transcript_before_the_trace_ends(self):
        # The stream's rows hold the start-of-stream output (1); the transcript has the final 40 and an
        # earlier turn's request, which this turn does not count. The same message twice counts once.
        config = self.root / "claude-config"
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(config)}):
            self.run_native("subagents")
        kinds = [kind for kind, _ in self.events]
        reports = [data for kind, data in self.events if kind == "subtask_usage"]
        self.assertLess(len(kinds) - 1 - kinds[::-1].index("subtask_usage"), kinds.index("trace_finished"))
        self.assertEqual(reports[-1], {"agents": {"agent-call": {
            "name": "检查", "level": 1, "input_tokens": 112, "cache_read_tokens": 100, "cache_write_tokens": 10,
            "output_tokens": 40}}, "unreported": 0, "pending": 0})

    def test_a_subagent_that_ends_is_counted_from_its_transcript_while_the_turn_runs(self):
        # Repo review S1: the final counts came only once the CLI had exited (#1184 read 1.1 万 for 24.1 万).
        config = self.root / "claude-config"
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(config)}):
            self.run_native("subagent-ends")
        closing = [kind for kind, _ in self.events].index("cancel_questions")  # The CLI being closed.
        early = [data for kind, data in self.events[:closing] if kind == "subtask_usage"]
        self.assertEqual((early[-1]["agents"]["agent-call"]["output_tokens"], early[-1]["pending"]), (40, 0))

    def test_native_image_uses_message_block(self):
        path = self.root / "image.png"
        path.write_bytes(b"image")
        answer = self.run_native("image", options={"attachments": [{"path": str(path), "mime": "image/png"}]})
        self.assertFalse(answer.is_error)

    def test_model_catalog_filters_account_and_uses_real_efforts(self):
        raw = {
            "account": {"token": "PRIVATE"},
            "models": [
                {
                    "value": "a",
                    "displayName": "A",
                    "supportsEffort": True,
                    "supportedEffortLevels": ["high", "xhigh", "max"],
                    "apiKey": "PRIVATE",
                },
                {"value": "b", "supportsEffort": False, "supportedEffortLevels": ["max"]},
                {"value": "c", "supportsEffort": True, "supportedEffortLevels": ["low", "high", "max"]},
            ],
        }
        catalog = normalize_catalog(raw)
        self.assertNotIn("PRIVATE", str(catalog))
        # ultracode is xhigh with workflow orchestration: offered exactly where xhigh is.
        self.assertEqual([r["effort"] for r in catalog["a"]["supported_reasoning_levels"]],
                         ["high", "xhigh", "max", "ultracode"])
        self.assertEqual(catalog["b"]["supported_reasoning_levels"], [])
        self.assertEqual([r["effort"] for r in catalog["c"]["supported_reasoning_levels"]], ["low", "high", "max"])
        self.assertEqual(catalog["a"]["default_reasoning_level"], "")

    def test_a_level_this_release_does_not_know_is_left_off_its_model_not_the_whole_list(self):
        # T766: one unknown value took every Claude model off a new install's launch card (no older list to fall back on).
        catalog = normalize_catalog({"models": [
            {"value": "a", "supportsEffort": True, "supportedEffortLevels": ["invented", "high", 3, "max"]},
            {"value": "b", "supportsEffort": True, "supportedEffortLevels": "high"},
            {"value": "c", "supportsEffort": True, "supportedEffortLevels": ["low"]}]})
        self.assertEqual(list(catalog), ["a", "b", "c"])
        self.assertEqual([r["effort"] for r in catalog["a"]["supported_reasoning_levels"]], ["high", "max"])
        self.assertEqual(catalog["b"]["supported_reasoning_levels"], [])
        self.assertEqual([r["effort"] for r in catalog["c"]["supported_reasoning_levels"]], ["low"])
        with self.assertRaises(ValueError):  # A list that is not one of models still says so.
            normalize_catalog({"models": [{"value": ""}]})

    def test_a_probe_without_the_gateways_models_keeps_the_last_ones_seen_for_a_day(self):
        # 2026-11-03: Claude Code listed the gateway's Opus 5 in some probes only; Opus 5 came and went in the menus.
        # The gateway's ids are full model ids, its own (claude-opus-5) or prefixed by the gateway.
        ctx = SimpleNamespace(state_dir=self.root / "state")
        aliases = {"default": {"display_name": "Default"}, "opus": {"display_name": "Opus 5.5"}}
        listed = {**aliases, "claude-opus-5": {"display_name": "Claude Opus 5"}}
        self.assertEqual(keep_gateway_models(ctx, aliases, now=1000), aliases)  # Nothing seen yet.
        self.assertEqual(keep_gateway_models(ctx, listed, now=2000), listed)
        self.assertEqual(keep_gateway_models(ctx, aliases, now=2000 + 3600), listed)
        self.assertEqual(keep_gateway_models(ctx, aliases, now=2000 + GATEWAY_KEEP + 1), aliases)  # Too old.
        # A probe that lists them again is what counts: one the gateway dropped goes.
        newer = {**aliases, "claude/claude-fable-5": {"display_name": "Claude Fable 5"}}  # A prefixed id counts too.
        keep_gateway_models(ctx, newer, now=5000)
        self.assertEqual(keep_gateway_models(ctx, aliases, now=6000), newer)

    def test_a_topic_on_a_removed_gateways_model_is_told_so_before_claude_code_starts(self):
        # A "<gateway>/<model>" id not on the account's menu must not reach the account's Claude Code; one the
        # account's gateway lists (claude/claude-opus-5, e8c34ee) runs.
        gone = dataclasses.replace(self.ctx, model="gone-gateway/claude-opus-5-5")
        listed = dataclasses.replace(self.ctx, model="claude/claude-opus-5")
        with mock.patch("covey.backends.claude.bridge.model_catalog",
                        return_value={"opus": {}, "claude/claude-opus-5": {}}), \
                mock.patch("covey.backends.claude.bridge.redacted_stream", return_value="ran") as stream:
            with self.assertRaisesRegex(ValueError, "模型所在网关已下线.*换配置"):
                ClaudeBackend().run(RunRequest(gone, "hi"))
            stream.assert_not_called()
            self.assertEqual(ClaudeBackend().run(RunRequest(listed, "hi")), "ran")

    def test_a_gateway_model_runs_as_is_when_the_menu_cannot_be_read(self):
        # Only a menu read that answers says a gateway is gone: when it fails, the id goes to Claude Code as is,
        # and Claude Code says whether it has that model.
        ctx = dataclasses.replace(self.ctx, model="gone-gateway/claude-opus-5-5")
        for error in (RuntimeError("Claude 模型目录查询超时，请稍后刷新"), OSError("no claude"), ValueError("bad menu")):
            with self.subTest(error=type(error).__name__), \
                    mock.patch("covey.backends.claude.bridge.model_catalog", side_effect=error), \
                    mock.patch("covey.backends.claude.bridge.redacted_stream", return_value="ran") as stream:
                self.assertFalse(ClaudeBackend().retired_gateway(ctx))
                self.assertEqual(ClaudeBackend().run(RunRequest(ctx, "hi")), "ran")
                self.assertEqual(stream.call_args.args[1].context.model, "gone-gateway/claude-opus-5-5")

    def test_a_job_on_a_removed_providers_model_names_it_and_never_starts_claude_code(self):
        # TRAE and 豆包 were removed for good (the owner, 2026-11-05); a job queued before, or one of a topic still
        # on their model, says which provider went, even when the menu cannot be read or still lists the id.
        for model, name in (("traex/claude-opus-5-5", "TRAE"), ("traex/meilin/claude-opus-5-5", "TRAE"),
                            ("doubao/claude-opus-5-5", "豆包"), ("kiro-shared/claude-opus-5-5", "Kiro 共享网关")):
            for catalog in ({"options": {"side_effect": RuntimeError("probe timed out")}},
                            {"options": {"return_value": {model: {}}}}):
                with self.subTest(model=model, catalog=catalog), \
                        mock.patch("covey.backends.claude.bridge.model_catalog", **catalog["options"]), \
                        mock.patch("covey.backends.claude.bridge.redacted_stream") as stream:
                    with self.assertRaisesRegex(ValueError, f"已下线的供应商「{name}」.*换配置"):
                        ClaudeBackend().run(RunRequest(dataclasses.replace(self.ctx, model=model), "hi"))
                    stream.assert_not_called()

    def test_the_removed_trae_options_still_parse_and_do_nothing(self):
        # The live harnesses.json passes both (rule 10): they parse, stay out of --help and change no option.
        def config(*argv):
            parser = argparse.ArgumentParser()
            ClaudeBackend().add_cli_args(parser)
            return parser, ClaudeBackend().build_config(parser.parse_args(list(argv)))
        parser, old = config("--claude-transport", "stream-json", "--traex-ppe-env", "ppe_trae_eval", "--traex-site", "i18n-tt")
        _, plain = config()
        self.assertEqual(old, plain)
        self.assertEqual(old.options, {"transport": "stream-json"})
        self.assertNotIn("traex", parser.format_help())

    def test_stream_delta_and_child_tools_keep_root_separate(self):
        events = []
        adapter = ClaudeEventAdapter(lambda k, d: events.append((k, d)))
        adapter.accept(
            {
                "type": "stream_event",
                "event": {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "PRIVATE"}},
            }
        )
        adapter.accept(
            {
                "type": "stream_event",
                "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Root"}},
            }
        )
        adapter.accept(
            {
                "type": "assistant",
                "parent_tool_use_id": "child",
                "message": {"content": [{"type": "tool_use", "id": "childtool", "name": "Read", "input": {}}]},
            }
        )
        self.assertEqual([d["text"] for k, d in events if k == "answer_snapshot"], ["Root"])
        self.assertNotIn("PRIVATE", str(events))

    def test_step_details_show_greps_pattern_and_a_url_without_its_query(self):
        # A guard for the shared core.text.tool_detail: Claude keeps its own key order (pattern before path).
        adapter = ClaudeEventAdapter(lambda k, d: None)
        for tid, name, inputs in (("g", "Grep", {"pattern": "def main", "path": "src"}),
                                  ("w", "WebFetch", {"url": "https://example.invalid/doc?token=PRIVATE#part"}),
                                  ("t", "TodoWrite", {"todos": []})):
            adapter.accept({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": tid, "name": name, "input": inputs}]}})
        self.assertEqual([adapter.tools[k]["detail"] for k in "gwt"],
                         ["def main", "https://example.invalid/doc", "TodoWrite"])

    def test_shell_tasks_are_command_steps_not_child_agents(self):
        # Event order recorded from Claude Code 2.1.300 stream-json.
        events = []
        adapter = ClaudeEventAdapter(lambda k, d: events.append((k, dict(d))))

        def tool_use(tid, **inputs):
            adapter.accept({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": tid, "name": "Bash", "input": inputs}]}})

        def result(tid, is_error=False):
            adapter.accept({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": tid, "is_error": is_error, "content": "x"}]}})

        def task(subtype, tid, **fields):
            adapter.accept({"type": "system", "subtype": subtype, "tool_use_id": tid, "task_id": "t-" + tid, **fields})

        tool_use("bg", command="python3 -m unittest", description="Run full test suite", run_in_background=True)
        task("task_started", "bg", task_type="local_bash", is_backgrounded=True, description="Run full test suite")
        result("bg")
        self.assertEqual(adapter.tools["bg"]["status"], "running")
        task("task_notification", "bg", status="completed")
        tool_use("fg", command="sleep 25; exit 1", description="Wait then fail")
        task("task_started", "fg", task_type="local_bash", is_backgrounded=False, description="Wait then fail")
        task("task_notification", "fg", status="failed")
        result("fg", is_error=True)

        self.assertFalse([d for k, d in events if k == "agent_state"])
        tools = {d["id"]: d for k, d in events if k == "tool"}
        self.assertEqual((tools["bg"]["name"], tools["bg"]["status"]), ("background", "done"))
        self.assertEqual(tools["bg"]["detail"], "Run full test suite · python3 -m unittest")
        self.assertEqual((tools["fg"]["name"], tools["fg"]["status"]), ("bash", "failed"))

    def test_child_agents_are_named_by_their_task_description(self):
        events = []
        adapter = ClaudeEventAdapter(lambda k, d: events.append((k, dict(d))))
        adapter.accept({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "a1", "name": "Agent",
                        "input": {"description": "Survey entry scripts", "prompt": "..."}}]}})
        adapter.accept({"type": "system", "subtype": "task_started", "tool_use_id": "a1", "task_type": "local_agent",
                        "description": "Survey entry scripts"})
        agents = [d for k, d in events if k == "agent_state"]
        self.assertTrue(agents and all(a["name"] == "Survey entry scripts" for a in agents))

    def test_a_background_agent_and_command_reach_the_trace_as_tasks_that_keep_the_turn_open(self):
        # The status line's 还在等后台 (core/progress.py background_tasks) reads them from the trace.
        from covey.core import progress
        from covey.core.trace import task_trace
        trace = task_trace()
        adapter = ClaudeEventAdapter(lambda k, d: trace.accept(k, dict(d)))
        adapter.accept({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "a1", "name": "Agent",
                        "input": {"description": "Count files", "prompt": "..."}}]}})  # No run_in_background: Claude Code decides.
        adapter.accept({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "a1", "content": "x"}],
                        }, "tool_use_result": {"status": "async_launched", "isAsync": True, "agentId": "a8c3"}})
        adapter.accept({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "bg", "name": "Bash",
                        "input": {"command": "python3 -m unittest", "description": "Run full test suite",
                                  "run_in_background": True}}]}})
        adapter.accept({"type": "system", "subtype": "task_started", "tool_use_id": "bg", "task_id": "t-bg",
                        "task_type": "local_bash", "is_backgrounded": True, "description": "Run full test suite"})
        self.assertEqual(sorted(progress.background_tasks(trace)), ["Count files", "Run full test suite"])
        adapter.accept({"type": "system", "subtype": "task_notification", "tool_use_id": "bg", "task_id": "t-bg",
                        "status": "completed"})
        adapter.accept({"type": "system", "subtype": "task_notification", "tool_use_id": "a1", "task_id": "t1",
                        "status": "completed"})
        self.assertEqual(progress.background_tasks(trace), [])

    def test_a_subagent_claude_code_runs_in_the_background_runs_until_its_notification(self):
        # S31 K6 (Claude Code 2.1.310): two Explore agents were launched in the background without
        # run_in_background; their rows read 做完 · 0.0 秒 at launch, before their commands ran.
        from covey.core.trace import task_trace
        from covey.cards.trace_view import TraceView
        launched = "Async agent launched successfully. (This tool call returned at once)"
        for shape in ("structured", "text", "foreground"):
            with self.subTest(shape=shape):
                events, trace = [], task_trace()
                adapter = ClaudeEventAdapter(lambda k, d: (events.append((k, dict(d))), trace.accept(k, dict(d))))
                adapter.accept({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "a1", "name": "Agent",
                                "input": {"description": "Count files", "prompt": "...", "subagent_type": "Explore"}}]}})
                result = {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "a1",
                          "content": [{"type": "text", "text": "45" if shape == "foreground" else launched}]}]}}
                if shape == "structured":
                    result["message"]["content"][0]["content"] = "x"
                    result["tool_use_result"] = {"status": "async_launched", "isAsync": True, "agentId": "a8c3"}
                adapter.accept(result)
                states = [d for k, d in events if k == "agent_state"]
                tools = [d for k, d in events if k == "tool"]
                self.assertEqual(tools[-1]["status"], "done")  # The call itself returned.
                self.assertEqual(trace.steps["a1"]["title"], "分配子任务")
                if shape == "foreground":
                    self.assertEqual(states[-1]["status"], "completed")
                    continue
                self.assertEqual(states[-1]["status"], "running")
                self.assertNotIn("ended_at", states[-1])
                self.assertIn(" · Count files · 执行中 · ", TraceView(trace.snapshot()).agent_element("a1")["header"]["title"]["content"])
                adapter.accept({"type": "system", "subtype": "task_notification", "tool_use_id": "a1",
                                "task_id": "t1", "status": "completed"})
                self.assertEqual(adapter.agents["a1"]["status"], "completed")
                self.assertIn(" · Count files · 做完 · ", TraceView(trace.snapshot()).agent_element("a1")["header"]["title"]["content"])


class ClaudeSubagentTests(unittest.TestCase):
    """Every turn's claude runs its subagents on Sonnet and Explore on Haiku unless main-thread.json says "same"
    (backends/claude/subagents.py)."""
    setUp, run_native = ClaudeAlignmentTests.setUp, ClaudeAlignmentTests.run_native

    def spawned(self, **options):
        """(argv, env) of each claude a whole turn of the fixture starts."""
        seen, real = [], runtime.ClaudeProcess

        def spawn(argv, ctx, env, **kwargs):
            seen.append((argv, env))
            return real(argv, ctx, env, **kwargs)
        with mock.patch.object(runtime, "ClaudeProcess", spawn), \
                mock.patch.dict(os.environ, {}, clear=False) as environ:
            environ.pop(subagents.MODEL_ENV, None)
            answer = self.run_native(options=options)
        self.assertFalse(answer.is_error, answer.text)
        self.assertTrue(seen)
        return seen

    def explore(self, argv):
        return json.loads(argv[argv.index("--agents") + 1]) if "--agents" in argv else None

    def test_a_topic_turn_gets_sonnet_subagents_and_a_haiku_explore_by_default(self):
        for argv, env in self.spawned():
            self.assertEqual(env[subagents.MODEL_ENV], "sonnet")
            explore = self.explore(argv)["Explore"]
            self.assertEqual(explore["model"], "haiku")
            self.assertEqual(explore["tools"], ["Read", "Grep", "Glob", "Bash"])
            self.assertIn("Never edit", explore["prompt"])

    def test_same_adds_nothing(self):
        butler_settings.update(self.ctx.state_dir, subagents="same")
        for argv, env in self.spawned():
            self.assertNotIn(subagents.MODEL_ENV, env)
            self.assertNotIn("--agents", argv)

    def test_a_turn_on_haiku_keeps_its_subagents_on_haiku(self):
        self.ctx = dataclasses.replace(self.ctx, model="haiku")
        for argv, env in self.spawned():
            self.assertNotIn(subagents.MODEL_ENV, env)
            self.assertEqual(self.explore(argv)["Explore"]["model"], "haiku")
        self.ctx = dataclasses.replace(self.ctx, model="sonnet")
        self.assertEqual(self.spawned()[0][1][subagents.MODEL_ENV], "sonnet")

    def test_the_options_override_the_setting_for_the_subagents_model(self):
        # "light": the retry model for every subagent, and no Haiku Explore (the built-in one follows the variable).
        for argv, env in self.spawned(subagent_model="claude-opus-4-8"):
            self.assertEqual(env[subagents.MODEL_ENV], "claude-opus-4-8")
            self.assertNotIn("--agents", argv)
        butler_settings.update(self.ctx.state_dir, subagents="same")
        argv, env = self.spawned(subagent_model="claude-opus-4-8")[0]
        self.assertEqual(env[subagents.MODEL_ENV], "claude-opus-4-8")
        self.assertNotIn("--agents", argv)
        butler_settings.update(self.ctx.state_dir, subagents="light")
        # "": none, even the Bridge's own environment's.
        self.assertNotIn(subagents.MODEL_ENV, claude_argv.turn_env(dataclasses.replace(
            self.ctx, options={"subagent_model": ""}), {subagents.MODEL_ENV: "opus"}))

    def test_the_catalog_probe_gets_no_explore(self):
        self.assertNotIn("--agents", claude_argv.stream_argv(self.backend, self.ctx, catalog=True))


class ClaudeAutoCompactWindowTests(unittest.TestCase):
    """--claude-auto-compact-window sets the compaction window of the claude child, and nothing else."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.backend = ClaudeBackend()
        self.parser = argparse.ArgumentParser()
        self.backend.add_cli_args(self.parser)

    def options(self, *argv):
        return self.backend.build_config(self.parser.parse_args(list(argv))).options

    def child_env(self, *argv):
        """The environment run() hands the claude child, and the one it would get without the option."""
        ctx = dataclasses.replace(codex_context(self.root, "stream-json"), options=self.options(*argv))
        seen = []
        with mock.patch("covey.backends.claude.runtime.run_stream",
                        side_effect=lambda backend, request, env: seen.append(env) or RunResult("ok", SID)):
            self.backend.run(RunRequest(ctx, "hi"))
        return seen[0], {**child_process_env(ctx.workdir), **home.env(ctx)}  # The home's names too (core/home.py env).

    def test_argv_takes_only_a_positive_token_count(self):
        self.assertEqual(self.options("--claude-auto-compact-window", "150000")["auto_compact_window"], 150000)
        self.assertEqual(self.options(), {"transport": "stream-json"})
        self.assertEqual(self.options("--claude-transport", "stream-json"), {"transport": "stream-json"})
        for bad in ("0", "-5", "150k"):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                self.parser.parse_args(["--claude-auto-compact-window", bad])

    def test_child_env_gets_the_window_only_when_given(self):
        checkpoints = {FILE_CHECKPOINTS_ENV: "1"}  # Every child keeps file backups for ↩️ 回退.
        with mock.patch.dict(os.environ, {AUTO_COMPACT_ENV: "999"}):
            os.environ.pop(GATEWAY_MODELS_ENV, None)
            env, plain = self.child_env("--claude-auto-compact-window", "150000")
            self.assertEqual(env, {**plain, GATEWAY_MODELS_ENV: "1", AUTO_COMPACT_ENV: "150000", **checkpoints})
            # Unset, the child inherits the Bridge's environment as before, its own value included.
            env, plain = self.child_env()
            self.assertEqual(env, {**plain, GATEWAY_MODELS_ENV: "1", **checkpoints})
            self.assertEqual(env[AUTO_COMPACT_ENV], "999")
        with mock.patch.dict(os.environ):
            os.environ.pop(AUTO_COMPACT_ENV, None)
            os.environ.pop(GATEWAY_MODELS_ENV, None)
            env, plain = self.child_env()
            self.assertEqual(env, {**plain, GATEWAY_MODELS_ENV: "1", **checkpoints})
            self.assertNotIn(AUTO_COMPACT_ENV, env)

    def test_child_env_lists_the_gateway_models_unless_the_bridge_names_its_own_value(self):
        for own in ("", "0", "1"):
            with self.subTest(own), mock.patch.dict(os.environ, {GATEWAY_MODELS_ENV: own}):
                env, plain = self.child_env()
                self.assertEqual(env, {**plain, FILE_CHECKPOINTS_ENV: "1"})
                self.assertEqual(env[GATEWAY_MODELS_ENV], own)
        # The catalog probe gets the same environment as a run (models.py: model_catalog).
        with mock.patch.dict(os.environ), mock.patch("covey.backends.claude.bridge.model_catalog") as probe:
            os.environ.pop(GATEWAY_MODELS_ENV, None)
            self.backend.refresh_model_catalog(codex_context(self.root, "stream-json"))
            self.assertEqual(probe.call_args.args[2][GATEWAY_MODELS_ENV], "1")

    def test_harness_config_args_reach_a_rebuilt_bridge(self):
        # A task runner rebuilds the Bridge from its argv, which names the same --harness-config.
        work = self.root / "project"
        work.mkdir()
        config = self.root / "harnesses.json"
        argv = ["--workdir", str(work), "--state-dir", str(self.root / "state"), "--profile", "test-bot",
                "--harness-config", str(config)]
        for args, expected in ((["--claude-transport", "stream-json"], None),
                               (["--claude-transport", "stream-json", "--claude-auto-compact-window", "200000"],
                                200000)):
            config.write_text(json.dumps({"harnesses": {"claude": {"args": args}}}))
            gateway = UnifiedBridgeApp(script_path=Path(__file__).parents[1] / "bin/covey_bridge.py")
            gateway.runtime.log_sink = lambda message: None
            gateway.build_context(gateway.build_parser().parse_args(argv))
            self.assertEqual(gateway.harnesses["claude"].context.options.get("auto_compact_window"), expected)


class ClaudeCompactionTests(unittest.TestCase):
    """A resumed session compacted before the turn (backends/claude/compaction.py): when the Bridge asks
    (compact_first) or its context reached --claude-compact-between-turns; and Claude Code's own."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / "claude-config"
        self.events, self.processes, self.saved = [], [], []

    def run_turn(self, scenario="success", session=SID, **options):
        ctx = dataclasses.replace(codex_context(self.root, "stream-json"), timeout=8,
                                  options={"transport": "stream-json", **options},
                                  backend_cmd=[sys.executable, str(CLAUDE_JSONL), scenario])
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.config)}):
            answer = ClaudeBackend().run(RunRequest(ctx, "prompt", session, callbacks=RunCallbacks(
                event=lambda k, d, c: self.events.append((k, dict(d))), process=self.processes.append,
                verified_reply=self.saved.append), inputs=RunInputs(threading.Event(), run_id=RUN)))
        return answer

    def transcript(self, *rows):
        store = ClaudeSessionStore(self.config / "projects").sessions_dir(self.root)
        store.mkdir(parents=True, exist_ok=True)
        (store / f"{SID}.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))

    def steps(self):
        return [d for k, d in self.events if k == "tool" and d["name"] == "context_compaction"]

    def test_asked_compacts_the_session_in_a_process_of_its_own_then_runs_the_turn(self):
        answer = self.run_turn(compact_first=True)
        self.assertFalse(answer.is_error)
        self.assertEqual(answer.public_reply.text, "Complete answer")
        argv = json.loads((self.root / "compact-argv.json").read_text())
        self.assertEqual(argv[argv.index("--resume") + 1], SID)
        self.assertEqual(len([p for p in self.processes if p is not None]), 2)
        running, done = self.steps()
        self.assertEqual((running["status"], done["status"]), ("running", "done"))
        self.assertEqual(done["detail"], "按任务卡上的安排 · 压缩前 41.2 万 token → 摘要 9,800 token")
        # Its model request is the turn's; it is not the context the next turn resumes with.
        usage = [d for k, d in self.events if k == "token_usage"][-1]
        self.assertEqual((usage["input_tokens"], usage["output_tokens"]), (410002, 1900))
        self.assertNotIn("context_tokens", usage)
        kinds = [k for k, _ in self.events]
        self.assertLess(kinds.index("tool"), kinds.index("answer_snapshot"))

    def test_the_command_carries_the_instructions_it_was_given_and_only_those(self):
        # The butler's (app/butler.py COMPACT_INSTRUCTIONS) on one line; a topic's compaction has none.
        for instructions, expected in (("摘要不超过\n约 2000 token", "/compact 摘要不超过 约 2000 token"),
                                       (None, "/compact"), ("  ", "/compact")):
            with self.subTest(instructions=instructions):
                options = {"compact_first": True}
                if instructions is not None:
                    options["compact_instructions"] = instructions
                self.assertFalse(self.run_turn(**options).is_error)
                self.assertEqual((self.root / "compact-command.txt").read_text(), expected)

    def test_the_threshold_reads_where_the_last_turn_ended(self):
        big = {"type": "assistant", "message": {"usage": {"input_tokens": 2, "cache_read_input_tokens": 350000,
                                                          "cache_creation_input_tokens": 1000, "output_tokens": 500}}}
        side = {**big, "isSidechain": True, "message": {"usage": {**big["message"]["usage"], "cache_read_input_tokens": 1}}}
        boundary = {"type": "system", "subtype": "compact_boundary", "compactMetadata": {"postTokens": 8000}}
        for rows, threshold, compacts in (([big, side], 300000, True), ([big], 400000, False),
                                          ([big, boundary], 300000, False), ([], 300000, False)):
            with self.subTest(rows=len(rows), threshold=threshold):
                self.events.clear()
                (self.root / "compact-argv.json").unlink(missing_ok=True)
                self.transcript(*rows)
                answer = self.run_turn(compact_between_turns=threshold)
                self.assertFalse(answer.is_error)
                self.assertEqual((self.root / "compact-argv.json").exists(), compacts)
                if compacts:
                    self.assertEqual(self.steps()[0]["detail"], "上一轮结束时上下文 35.2 万 token，超过 30.0 万")

    def test_a_failed_compaction_leaves_the_turn_on_the_session_as_it_was(self):
        answer = self.run_turn("compact-error", compact_first=True)
        self.assertFalse(answer.is_error)
        self.assertEqual(answer.public_reply.text, "Complete answer")
        self.assertEqual(self.steps()[-1]["status"], "failed")
        self.assertEqual(self.steps()[-1]["detail"], "按任务卡上的安排 · Claude Code 报告压缩失败，本轮按原上下文继续")

    def test_compacting_alone_runs_one_process_and_shows_nothing(self):
        """compact_session (the butler's idle compaction, app/idle_compaction.py): /compact on the session with
        the turn's argv and nothing after it; no step, no usage, no answer; a failure is False."""
        for scenario, expected in (("success", True), ("compact-error", False)):
            with self.subTest(scenario=scenario):
                self.processes.clear()
                ctx = dataclasses.replace(codex_context(self.root, "stream-json"), timeout=8,
                                          options={"transport": "stream-json", "compact_first": True,
                                                   "compact_instructions": "短一点"},
                                          backend_cmd=[sys.executable, str(CLAUDE_JSONL), scenario])
                with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.config)}):
                    done = ClaudeBackend().compact_session(RunRequest(ctx, "", SID, callbacks=RunCallbacks(
                        event=lambda k, d, c: self.events.append((k, dict(d))), process=self.processes.append),
                        inputs=RunInputs(threading.Event())))
                self.assertIs(done, expected)
                argv = json.loads((self.root / "compact-argv.json").read_text())
                self.assertEqual(argv[argv.index("--resume") + 1], SID)
                self.assertEqual((self.root / "compact-command.txt").read_text(), "/compact 短一点")
                self.assertEqual(len([p for p in self.processes if p is not None]), 1)
                self.assertEqual(self.events, [])

    def test_a_new_session_has_nothing_to_compact(self):
        self.run_turn(session=None, compact_first=True, compact_between_turns=1)
        self.assertFalse((self.root / "compact-argv.json").exists())
        self.assertEqual(self.steps(), [])

    def test_claude_codes_own_compaction_is_a_step_of_the_turn(self):
        events = []
        adapter = ClaudeEventAdapter(lambda k, d: events.append((k, dict(d))))
        adapter.accept({"type": "system", "subtype": "status", "status": "compacting", "session_id": SID})
        adapter.accept({"type": "system", "subtype": "status", "status": None, "compact_result": "success"})
        adapter.accept({"type": "system", "subtype": "compact_boundary", "session_id": SID,
                        "compact_metadata": {"trigger": "auto", "pre_tokens": 967000, "post_tokens": 52000}})
        steps = [d for k, d in events if k == "tool"]
        self.assertEqual([s["status"] for s in steps], ["running", "done"])
        self.assertEqual(steps[-1]["detail"], "Claude Code 自动压缩 · 压缩前 96.7 万 token → 摘要 5.20 万 token")

    def test_claude_codes_compacting_repeats_are_the_same_step(self):
        # Claude Code 2.1.318 resends status: compacting every 30 s until the compaction ends.
        events = []
        adapter = ClaudeEventAdapter(lambda k, d: events.append((k, dict(d))))
        for _ in range(3):
            adapter.accept({"type": "system", "subtype": "status", "status": "compacting", "session_id": SID})
        adapter.accept({"type": "system", "subtype": "compact_boundary", "session_id": SID,
                        "compact_metadata": {"trigger": "auto", "pre_tokens": 991782, "post_tokens": 25671}})
        adapter.accept({"type": "system", "subtype": "status", "status": "compacting", "session_id": SID})
        steps = [d for k, d in events if k == "tool"]
        self.assertEqual([(s["id"], s["status"]) for s in steps],
                         [("compact-auto-1", "running"), ("compact-auto-1", "done"), ("compact-auto-2", "running")])

    def test_the_turn_reports_its_last_requests_context_and_the_models_window(self):
        events = []
        adapter = ClaudeEventAdapter(lambda k, d: events.append((k, dict(d))))
        adapter.accept({"type": "system", "subtype": "init", "session_id": SID, "model": "claude/opus"})
        for mid, cached in (("m1", 1000), ("m2", 5000)):
            adapter.accept({"type": "stream_event", "session_id": SID, "event": {"type": "message_start", "message": {
                "id": mid, "usage": {"input_tokens": 10, "cache_read_input_tokens": cached,
                                     "cache_creation_input_tokens": 50, "output_tokens": 1}}}})
            adapter.accept({"type": "stream_event", "session_id": SID, "event": {"type": "message_delta",
                                                                                 "usage": {"output_tokens": 200}}})
        adapter.accept({**result(), "modelUsage": {"claude/opus": {"contextWindow": 1000000},
                                                   "claude/haiku": {"contextWindow": 200000}}})
        usage = [d for k, d in events if k == "token_usage"][-1]
        self.assertEqual((usage["context_tokens"], usage["context_window"]), (5260, 1000000))
        self.assertEqual(usage["input_tokens"], 6120)

    def test_the_window_reported_is_the_smaller_of_the_models_and_the_compaction_window(self):
        # --claude-auto-compact-window decides where the session really compacts (app/prompts.py context_high).
        def window(compact):
            events = []
            adapter = ClaudeEventAdapter(lambda k, d: events.append((k, dict(d))))
            adapter.usage.compact_window = compact
            adapter.accept({"type": "system", "subtype": "init", "session_id": SID, "model": "claude/opus"})
            adapter.accept({"type": "assistant", "session_id": SID, "message": {
                "id": "m1", "usage": {"input_tokens": 10, "output_tokens": 5}, "content": []}})
            adapter.accept({**result(), "modelUsage": {"claude/opus": {"contextWindow": 1000000}}})
            return [d for k, d in events if k == "token_usage"][-1].get("context_window")
        self.assertEqual(window(None), 1000000)
        self.assertEqual(window(400000), 400000)
        self.assertEqual(window(2000000), 1000000)

    def test_the_turn_hands_its_compaction_window_to_the_usage_it_reports(self):
        seen = []

        class Adapter(ClaudeEventAdapter):
            def __init__(self, emit):
                super().__init__(emit)
                seen.append(self)
        for options, expected in (({"auto_compact_window": 300000}, 300000), ({}, None)):
            seen.clear()
            tmp = tempfile.TemporaryDirectory()
            self.addCleanup(tmp.cleanup)
            ctx = dataclasses.replace(codex_context(Path(tmp.name), "stream-json"), timeout=8,
                                      options={"transport": "stream-json", **options},
                                      backend_cmd=[sys.executable, str(CLAUDE_JSONL), "success"])
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(Path(tmp.name) / "cfg")}), \
                    mock.patch.object(runtime, "ClaudeEventAdapter", Adapter):
                ClaudeBackend().run(RunRequest(ctx, "prompt", SID, callbacks=RunCallbacks(
                    event=lambda k, d, c: None, process=lambda p: None, verified_reply=lambda r: None),
                    inputs=RunInputs(threading.Event(), run_id=RUN)))
            self.assertEqual(seen[0].usage.compact_window, expected)

    def test_the_compaction_request_is_counted_but_is_not_generation(self):
        from covey.backends.claude.usage import ClaudeTurnUsage
        events = []
        usage = ClaudeTurnUsage(lambda k, d: events.append(dict(d)))
        usage.update("compact:1", {"input_tokens": 2, "cache_read_input_tokens": 410000,
                                   "cache_creation_input_tokens": 0, "output_tokens": 1900}, 100.0, context=False)
        usage.arm(100.0)
        usage.update("m1", {"input_tokens": 10, "cache_read_input_tokens": 9000, "cache_creation_input_tokens": 50,
                            "output_tokens": 100}, 102.0)
        self.assertEqual((events[-1]["output_tokens"], events[-1]["measured_output_tokens"]), (2000, 100))
        self.assertEqual(events[-1]["context_tokens"], 9160)

    def test_argv_takes_the_threshold(self):
        parser = argparse.ArgumentParser()
        backend = ClaudeBackend()
        backend.add_cli_args(parser)
        options = backend.build_config(parser.parse_args(["--claude-compact-between-turns", "300000"])).options
        self.assertEqual(options["compact_between_turns"], 300000)
        self.assertNotIn("compact_between_turns", backend.build_config(parser.parse_args([])).options)
        self.assertTrue(backend.capabilities(codex_context(self.root, "stream-json")).compaction)


class ClaudeHandoffSummaryTests(unittest.TestCase):
    """The handoff summary of 🔁 换配置 (backends/claude/handoff.py): the old model on a fork of the old session."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / "claude-config"
        self.store = ClaudeSessionStore(self.config / "projects").sessions_dir(self.root)
        self.store.mkdir(parents=True)
        (self.store / f"{SID}.jsonl").write_text('{"old": true}\n')

    def context(self, scenario, **fields):
        ctx = dataclasses.replace(codex_context(self.root, "stream-json"), model="claude-opus-4-8", effort="max",
                                  skip_permissions=True, backend_cmd=[sys.executable, str(CLAUDE_JSONL), scenario],
                                  options={"transport": "stream-json", "mcp_config": {"mcpServers": {"b": {}}}})
        return dataclasses.replace(ctx, **fields)

    def summarize(self, scenario="handoff", redact=False, run_id="", **fields):
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.config)}):
            return ClaudeBackend().handoff_summary(self.context(scenario, **fields), SID, "[Bridge 交接请求] 写摘要",
                                                   redact=redact, run_id=run_id)

    def argv(self):
        return json.loads((self.root / "handoff-argv.json").read_text())

    def test_the_old_model_summarizes_a_fork_of_the_old_session_which_stays_as_it_was(self):
        # The fork's file appears while it runs; it is removed after, the old one is untouched.
        (self.store / f"{OTHER}.jsonl").write_text("{}\n")
        self.assertEqual(self.summarize(), ("目标：修好 X。下一步：跑测试。", "写好了"))
        argv = self.argv()
        self.assertEqual(argv[argv.index("--resume") + 1: argv.index("--resume") + 3], [SID, "--fork-session"])
        self.assertEqual((argv[argv.index("--model") + 1], argv[argv.index("--effort") + 1]), ("claude-opus-4-8", "max"))
        # No tool runs without asking, and the butler's Bridge tools are not loaded.
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertNotIn("--mcp-config", argv)
        self.assertEqual((self.root / "handoff-prompt.txt").read_text(), "[Bridge 交接请求] 写摘要")
        self.assertEqual((self.store / f"{SID}.jsonl").read_text(), '{"old": true}\n')
        self.assertFalse((self.store / f"{OTHER}.jsonl").exists())

    def test_a_tool_the_model_tries_is_denied(self):
        self.assertEqual(self.summarize("handoff-tool")[1], "写好了")
        self.assertEqual(json.loads((self.root / "handoff-permission.json").read_text())["behavior"], "deny")

    def test_a_failure_or_a_timeout_gives_no_text(self):
        self.assertEqual(self.summarize("handoff-error"), ("", "Claude Code 报告失败"))
        from covey.backends.claude.handoff import summarize
        started = time.monotonic()
        backend, ctx = ClaudeBackend(), self.context("handoff-hang")
        self.assertEqual(summarize(backend, ctx, SID, "[Bridge 交接请求]", backend.child_env(ctx), seconds=1),
                         ("", "超过 1 秒"))
        self.assertLess(time.monotonic() - started, 15)
        missing = self.summarize(workdir=self.root / "gone")
        self.assertEqual(missing[0], "")

    def test_a_model_that_says_nothing_after_the_question_gives_up_at_the_first_sign_limit(self):
        """T884 A12: a gateway that is down: Claude Code answers initialize, then nothing of the model comes (only its
        own init row): the summary is given up after FIRST_SECONDS, not SUMMARY_SECONDS."""
        from covey.backends.claude import handoff
        self.assertEqual(handoff.FIRST_SECONDS, 30)
        backend, ctx = ClaudeBackend(), self.context("handoff-hang")
        started = time.monotonic()
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.config)}):
            self.assertEqual(handoff.summarize(backend, ctx, SID, "[Bridge 交接请求]", backend.child_env(ctx), seconds=20,
                                               first_seconds=0.5), ("", "原模型 0.5 秒没有回应（网关可能断开）"))
        self.assertLess(time.monotonic() - started, 15)

    def test_a_removed_providers_or_gateways_model_is_never_asked_for_a_summary(self):
        from covey.core.providers import RETIRED_PREFIXES
        prefix, name = next(iter(RETIRED_PREFIXES.items()))
        self.assertEqual(self.summarize(model=prefix + "claude-opus-4-8"),
                         ("", f"原来的模型来自已下线的供应商「{name}」，没有发送"))
        self.assertFalse((self.root / "handoff-argv.json").exists())
        with mock.patch.object(ClaudeBackend, "retired_gateway", return_value=True):
            self.assertEqual(self.summarize(), ("", "原来的模型所在网关已下线，没有发送"))
        self.assertFalse((self.root / "handoff-argv.json").exists())

    def test_a_redacted_session_is_summarized_through_the_relay_or_not_at_all(self):
        from covey.backends.claude import redaction
        settings = {"ANTHROPIC_BASE_URL": "http://127.0.0.1:9"}

        runs = []

        @contextlib.contextmanager
        def relay(ctx, env, **kwargs):
            runs.append(kwargs.get("run_id"))
            yield dataclasses.replace(ctx, options={**ctx.options, redaction.SETTINGS_ENV: settings}), env
        with mock.patch.object(redaction, "turn", relay):
            self.assertEqual(self.summarize(redact=True, run_id="handoff-r3")[1], "写好了")
        self.assertEqual(runs, ["handoff-r3"])  # Its own row in the relay's counts.
        argv = self.argv()
        # The relay's env, beside the home-search hook of a turn started from the home (backends/claude/argv.py home_hooks).
        home = claude_argv.home_hooks(self.context("handoff"))
        self.assertEqual(json.loads(argv[argv.index("--settings") + 1]), {"env": settings, "hooks": home})
        (self.root / "handoff-argv.json").unlink()
        # Its turns went out as they were, but the topic has 敏感词脱敏 on now: through the relay as well.
        topic = codex_context(self.root, "stream-json").state_dir / "topics" / "t-handoff"
        topic.mkdir(parents=True)
        (topic / "topic.json").write_text('{"redaction": true}')
        with mock.patch.object(redaction, "turn", relay):
            self.assertEqual(self.summarize(topic_id="t-handoff")[1], "写好了")
        argv = self.argv()
        self.assertEqual(json.loads(argv[argv.index("--settings") + 1]), {"env": settings, "hooks": home})
        (self.root / "handoff-argv.json").unlink()
        (topic / "topic.json").write_text("not json")  # Its setting unknown: not sent.
        self.assertEqual(self.summarize(topic_id="t-handoff"), ("", "敏感词中继不可用，没有发送"))
        self.assertFalse((self.root / "handoff-argv.json").exists())
        ctx = codex_context(self.root, "stream-json")
        (ctx.state_dir / "redaction").mkdir(parents=True, exist_ok=True)
        (ctx.state_dir / "redaction" / "config.json").write_text("not json")
        self.assertEqual(self.summarize(redact=True), ("", "敏感词中继不可用，没有发送"))
        self.assertFalse((self.root / "handoff-argv.json").exists())


class ClaudeOneShotTests(unittest.TestCase):
    """ClaudeBackend.one_shot and turn_hooked (T884 E2): the 辅助模型's one question to Claude Code goes through the
    extensions' claude_turn hooks (covey-internal's protocol-normalization relay) and its --settings point at the relay
    a hook gave."""

    def test_the_settings_take_the_relay_a_hook_gave_and_no_user_hook(self):
        from covey.backends.claude import bridge as claude_bridge, redaction
        ctx = codex_context(Path("/nonexistent-root"), "stream-json")
        relay = {"ANTHROPIC_BASE_URL": "http://127.0.0.1:9"}
        entered, left = [], []

        class Hook:
            @staticmethod
            @contextlib.contextmanager
            def turn(ctx, env, *, run_id=""):
                entered.append(run_id)
                try:
                    yield dataclasses.replace(ctx, options={**ctx.options, redaction.SETTINGS_ENV: relay}), {**env, "X": "1"}
                finally:
                    left.append(run_id)
        backend = ClaudeBackend()
        with mock.patch.object(claude_bridge.extensions, "hooks", return_value=[("covey_internal", Hook)]):
            with backend.one_shot(ctx, {"A": "b"}, run_id="assist-1") as (hooked, env, settings):
                self.assertEqual(settings, {"disableAllHooks": True, "env": relay})
                self.assertEqual(env, {"A": "b", "X": "1"})
                self.assertEqual(hooked.options[redaction.SETTINGS_ENV], relay)
                self.assertEqual(left, [])
        self.assertEqual((entered, left), (["assist-1"], ["assist-1"]))  # The relay ends with the question.
        with mock.patch.object(claude_bridge.extensions, "hooks", return_value=[]):
            with backend.one_shot(ctx, {}) as (same, _, settings):
                self.assertEqual(settings, {"disableAllHooks": True})
                self.assertIs(same, ctx)


class InstalledClaudeContractTests(unittest.TestCase):
    def exercise(self, question=False, policy=""):
        with tempfile.TemporaryDirectory(prefix="claude-installed-contract-") as tmp:
            root = Path(tmp)
            config = root / "config"
            config.mkdir()
            calls = []
            question_input = {
                "questions": [
                    {
                        "question": "Choose contract input",
                        "header": "Input",
                        "multiSelect": True,
                        "options": [{"label": "A", "description": "First"}, {"label": "B", "description": "Second"}],
                    }
                ]
            }

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def do_POST(self):
                    raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                    if self.headers.get("Content-Encoding") == "gzip":
                        raw = gzip.decompress(raw)
                    body = json.loads(raw)
                    if self.path.startswith("/v1/messages/count_tokens"):
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(b'{"input_tokens":100}')
                        return
                    if not self.path.startswith("/v1/messages"):
                        self.send_response(404)
                        self.end_headers()
                        return
                    calls.append(body)
                    ask = question and len(calls) == 1
                    content = (
                        {"type": "tool_use", "id": "tool-contract", "name": "AskUserQuestion", "input": {}}
                        if ask
                        else {"type": "text", "text": ""}
                    )
                    delta = (
                        {"type": "input_json_delta", "partial_json": json.dumps(question_input)}
                        if ask
                        else {"type": "text_delta", "text": "Contract OK"}
                    )
                    frames = [
                        (
                            "message_start",
                            {
                                "message": {
                                    "id": "msg-contract-" + str(len(calls)),
                                    "type": "message",
                                    "role": "assistant",
                                    "model": body["model"],
                                    "content": [],
                                    "stop_reason": None,
                                    "stop_sequence": None,
                                    "usage": {"input_tokens": 100, "output_tokens": 1},
                                }
                            },
                        ),
                        ("content_block_start", {"index": 0, "content_block": content}),
                        ("content_block_delta", {"index": 0, "delta": delta}),
                        ("content_block_stop", {"index": 0}),
                        (
                            "message_delta",
                            {
                                "delta": {"stop_reason": "tool_use" if ask else "end_turn", "stop_sequence": None},
                                "usage": {"output_tokens": 8},
                            },
                        ),
                        ("message_stop", {}),
                    ]
                    if policy == "fallback":
                        frames.insert(1, ("content_block_start", {"index": 2, "content_block": {
                            "type": "fallback", "from": {"model": "claude-opus-5-5"},
                            "to": {"model": "claude-opus-5"},
                            "trigger": {"type": "refusal", "category": "frontier_llm"}}}))
                        frames.insert(2, ("content_block_stop", {"index": 2}))
                    elif policy:
                        frames[-2][1]["delta"].update(stop_reason="refusal",
                            stop_details={"type": "refusal", "category": policy})
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    for kind, value in frames:
                        self.wfile.write(
                            ("event: " + kind + "\ndata: " + json.dumps({"type": kind, **value}) + "\n\n").encode()
                        )
                    self.wfile.flush()

            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = serve_thread(server)
            thread.start()
            env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "CLAUDE_"))}
            env.update(
                ANTHROPIC_API_KEY="key-local-contract-only",
                ANTHROPIC_BASE_URL=f"http://127.0.0.1:{server.server_port}",
                CLAUDE_CONFIG_DIR=str(config),
                CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
                DISABLE_TELEMETRY="1",
                NO_PROXY="127.0.0.1,localhost",
                no_proxy="127.0.0.1,localhost",
            )
            cmd = [
                shutil.which("claude"),
                "--strict-mcp-config",
                "--mcp-config",
                '{"mcpServers":{}}',
                "--setting-sources",
                "",
                "--tools",
                "AskUserQuestion" if question else "",
                "--no-session-persistence",
            ]
            ctx = dataclasses.replace(
                codex_context(root, "stream-json"), backend_cmd=cmd, model="claude-sonnet-4-6", effort="", timeout=25
            )
            events = []

            def event(k, d, c):
                events.append((k, d))
                if k == "question":
                    c.answer(d["request_id"], {"0": {"answers": ["A", "B"]}})

            try:
                with mock.patch("covey.backends.claude.bridge.child_process_env", return_value=env):
                    answer = ClaudeBackend().run(
                        RunRequest(
                            ctx,
                            "Respond to this local protocol check.",
                            callbacks=RunCallbacks(event=event),
                            inputs=RunInputs(threading.Event(), run_id=RUN),
                            instructions="Bridge rules marker S18-APPENDED-RULES.\n- 第二行规则",
                        )
                    )
                if policy:
                    self.assertEqual(answer.outcome.error_code,
                                     "policy_fallback" if policy == "fallback" else "policy_refusal")
                    self.assertIsNone(answer.public_reply)
                    self.assertFalse(answer.outcome.resume_allowed)
                    self.assertTrue(answer.outcome.cleanup_confirmed)
                    return
                self.assertFalse(answer.is_error, (answer.outcome, [k for k, d in events], len(calls)))
                self.assertEqual(answer.text, "Contract OK")
                self.assertTrue(answer.outcome.resume_allowed)
                self.assertEqual(len(calls), 2 if question else 1)
                # --append-system-prompt reaches the API as system text, not as the user turn.
                for call in calls:
                    system = call.get("system")
                    system = system if isinstance(system, str) else "\n".join(b.get("text", "") for b in system or [])
                    self.assertIn("S18-APPENDED-RULES.\n- 第二行规则", system)
                    self.assertNotIn("S18-APPENDED-RULES", json.dumps(call.get("messages"), ensure_ascii=False))
                usage = [d for k, d in events if k == 'token_usage'][-1]
                self.assertEqual(usage['input_tokens'], 100 * len(calls))
                self.assertEqual(usage['output_tokens'], 8 * len(calls))
                self.assertGreater(usage['generation_seconds'], 0)
                first = [d for k, d in events if k == 'first_token']
                # A tool-first response deliberately has no text TTFT; the later answer
                # must not count the time spent answering the native question.
                self.assertEqual(len(first), 0 if question else 1)
                if first:
                    self.assertEqual(first[0]['ttft_source'], 'claude_delta')
                    self.assertGreaterEqual(first[0]['ttft_seconds'], 0)
                if question:
                    self.assertTrue(any(k == "question_resolved" for k, d in events))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(2)

    def test_installed_cli_completion(self):
        self.exercise()

    def test_installed_cli_native_multiselect(self):
        self.exercise(question=True)

    def test_installed_cli_upstream_policy_stop(self):
        for policy in ("cyber", "frontier_llm", "fallback"):
            with self.subTest(policy=policy):
                self.exercise(policy=policy)


class ClaudeLiveQuestionDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))

    def prepare(self, scenario="success", send_ok=False, harness_name="claude"):
        self.ctx = dataclasses.replace(
            codex_context(self.root, "stream-json"),
            topic_id="claude-topic",
            reply_to_message_id="om-root",
            backend_cmd=[sys.executable, str(CLAUDE_JSONL), scenario],
            timeout=8,
        )
        self.ctx = dataclasses.replace(self.ctx, options={**self.ctx.options, "harness_id": harness_name})
        self.ctx.state_dir.mkdir(mode=0o700)
        self.backend = ClaudeBackend()
        self.app = harness_app(dataclasses.replace(CLAUDE_SPEC, name=harness_name), self.backend, ClaudeSessionStore())
        self.tx = OutboundTransport(send_ok)
        self.app.runtime.log_sink = mock.Mock()
        self.app.messenger.send_message = self.tx.send_text
        self.app.artifacts.send_result_file = self.tx.send_file
        self.app.cards.streaming = False
        self.app.cards.stream = dataclasses.replace(
            self.app.cards.stream, send_text_message=self.tx.send_text
        )
        install_card_transport(self.stack, self.tx)
        leave_out_side_work(self.stack, closing=False)
        self.calls = self.stack.enter_context(mock.patch.object(self.backend, "run", wraps=self.backend.run))
        self.job = BridgeReplyJob(
            job_id=9,
            user_text="Synthetic task",
            chat_id="claude-chat",
            message_id="om-claude-task",
            topic_id=self.ctx.topic_id,
            reply_to_message_id="om-root",
            timeout=8,
            model=self.ctx.model,
            effort=self.ctx.effort,
            model_settings_snapshot=True,
        )
        self.app.runtime.jobs.register(self.job)

    def wait(self, predicate):
        end = time.monotonic() + 5
        while time.monotonic() < end:
            if predicate():
                return
            time.sleep(0.02)
        self.fail("Synthetic worker did not reach the expected state")

    def test_question_delivery_retry_then_native_answer(self):
        self.question_delivery_retry_then_native_answer("claude")

    def question_delivery_retry_then_native_answer(self, harness_name):
        self.prepare("question", harness_name=harness_name)
        token = hashlib.sha256(f"{self.job.message_id}-question-request".encode()).hexdigest()[:16]
        path = self.ctx.state_dir / "questions" / (token + ".json")
        worker = threading.Thread(target=self.app.process_reply_job, args=(self.job, self.ctx), daemon=True)
        worker.start()

        def cleanup():
            self.job.cancel_event.set()
            worker.join(5)
            self.assertFalse(worker.is_alive())

        self.addCleanup(cleanup)
        self.wait(lambda: path.exists() and json.loads(path.read_text()).get("display_pending"))
        with self.assertRaises(ValueError):
            self.app.questions.submit_answer(
                self.ctx, self.job.chat_id, token, {"option_0": ["0", "1"]}, "answer-before-delivery"
            )
        record = json.loads(path.read_text())
        forged = {**record, "live_question_proof": {**record["live_question_proof"], "session_id": "foreign"}}
        with self.assertRaises(ValueError):
            self.app.questions.native.live_control(self.ctx, self.job, forged)
        with self.assertRaises(ValueError):
            self.app.questions.native.live_control(dataclasses.replace(self.ctx, topic_id="other"), self.job, record)
        # Paint a card through the actual retry path; only the outbound boundary
        # is replaced, so the persistent display gate and answer commit stay real.
        self.app.messenger.publish_card = mock.Mock(return_value=True)
        self.app.controls.retry_topic(self.ctx, self.job.chat_id, "retry-question")
        self.assertFalse(json.loads(path.read_text())["display_pending"])
        self.app.questions.submit_answer(self.ctx, self.job.chat_id, token, {"option_0": ["0", "1"]}, "answer-after-delivery")
        worker.join(5)
        self.assertFalse(worker.is_alive())
        record = find_job_record(self.ctx.state_dir, self.job.message_id)
        self.assertEqual(record["execution_status"], "done", self.app.runtime.log_sink.call_args_list)
        self.assertEqual(record["final_text"], "Answered correctly")
        self.assertEqual(self.calls.call_count, 1)
        self.assertEqual(json.loads(path.read_text())["delivery_state"], "delivered")
        with self.assertRaises(ValueError):
            self.app.questions.native.live_control(self.ctx, None, json.loads(path.read_text()))


TRANSPORT_PRIVATE_SENTINEL = "PRIVATE_PROMPT_TOKEN_SENTINEL"


class ClaudeTransportDiagnosticsTests(unittest.TestCase):
    def run_failure(self, *, stage, error, saved=True):
        with tempfile.TemporaryDirectory() as folder:
            ctx = dataclasses.replace(codex_context(Path(folder), "stream-json"),
                backend_cmd=["never-run-claude"], options={"transport": "stream-json"})
            process = mock.Mock()
            process.prelude = []
            process.deadline = None
            process.process.poll.return_value = 1
            process.close.return_value = True
            process.write_size, process.write_offset = 20000, 8192
            if stage == "initialize":
                process.initialize.side_effect = error
            elif stage == "send_input":
                process.send.side_effect = error
            else:
                process.receive.side_effect = error
            with mock.patch("covey.backends.claude.runtime.ClaudeProcess", return_value=process):
                result = ClaudeBackend().run(RunRequest(ctx, TRANSPORT_PRIVATE_SENTINEL, None,
                    inputs=RunInputs(threading.Event(), run_id=RUN)))
            self.assertTrue(result.is_error)
            self.assertFalse(result.outcome.resume_allowed)
            raw = (ctx.state_dir / "claude-diagnostics" / (RUN + ".json")).read_text()
            self.assertNotIn(TRANSPORT_PRIVATE_SENTINEL, raw)
            data = json.loads(raw)
            self.assertEqual(data["error_code"], "transport_error")
            if not saved:
                return data.get("transport")
            self.assertEqual(data["transport"]["stage"], stage)
            self.assertEqual(data["transport"]["process_exit_code"], 1)
            self.assertEqual(data["transport"]["write_size"], 20000)
            self.assertEqual(data["transport"]["write_offset"], 8192)
            return data["transport"]

    def test_initialize_failure_is_distinct(self):
        result = self.run_failure(stage="initialize", error=RuntimeError("Claude initialization failed"))
        self.assertEqual(result["reason"], "initialize_rejected")

    def test_partial_input_is_not_replayed(self):
        result = self.run_failure(stage="send_input",
            error=RuntimeError("Claude input delivery is uncertain; do not replay"))
        self.assertEqual(result["reason"], "input_write_timeout")

    def test_os_error_keeps_errno_without_its_message(self):
        result = self.run_failure(stage="receive_events", error=OSError(11, TRANSPORT_PRIVATE_SENTINEL))
        self.assertEqual(result["errno"], 11)
        self.assertEqual(result["reason"], "os_error")

    def failure_events(self, error, *, stage="initialize", tail=(), exit_code=1):
        """The ``backend_failure`` events of a run that ends on ``error`` at ``stage`` (the CLI's stderr ``tail``)."""
        import collections
        events = []
        with tempfile.TemporaryDirectory() as folder:
            ctx = dataclasses.replace(codex_context(Path(folder), "stream-json"),
                backend_cmd=["never-run-claude"], options={"transport": "stream-json"})
            process = mock.Mock()
            process.prelude, process.deadline = [], None
            process.process.poll.return_value = exit_code
            process.close.return_value = True
            process.write_size, process.write_offset = 149, 149
            process.stderr_tail = collections.deque(tail)
            getattr(process, "initialize" if stage == "initialize" else "send").side_effect = error
            with mock.patch("covey.backends.claude.runtime.ClaudeProcess", return_value=process):
                ClaudeBackend().run(RunRequest(ctx, TRANSPORT_PRIVATE_SENTINEL, None, callbacks=RunCallbacks(
                    event=lambda kind, data, control: events.append((kind, data))),
                    inputs=RunInputs(threading.Event(), run_id=RUN)))
        return [data for kind, data in events if kind == "backend_failure"]

    def test_a_transport_failure_says_what_ended_it_how_the_cli_exited_and_what_it_wrote_to_stderr(self):
        # 2026-11-15: a fork's CLI exited 1 before it answered initialize; stderr went nowhere, the log had nothing.
        found = self.failure_events(RuntimeError("Unexpected Claude initialization event"),
                                    tail=["No conversation found with session ID: x", "exit"])
        self.assertEqual(len(found), 1, found)
        data = found[0]
        self.assertEqual((data["status"], data["code"], data["stage"], data["exception"], data["reason"],
                          data["process_exit_code"]),
                         ("failed", "transport_error", "initialize", "RuntimeError", "initialize_unexpected_event", 1))
        self.assertEqual(data["stderr"], ["No conversation found with session ID: x", "exit"])

    def failed_run(self, error, *, stage="initialize", tail=(), exit_code=1):
        """The RunResult of a run that ends on ``error`` at ``stage``, the CLI's stderr ``tail``."""
        import collections
        with tempfile.TemporaryDirectory() as folder:
            ctx = dataclasses.replace(codex_context(Path(folder), "stream-json"),
                backend_cmd=["never-run-claude"], options={"transport": "stream-json"})
            process = mock.Mock()
            process.prelude, process.deadline = [], None
            process.process.poll.return_value = exit_code
            process.close.return_value = True
            process.write_size, process.write_offset = 149, 149
            process.stderr_tail = collections.deque(tail)
            getattr(process, "initialize" if stage == "initialize" else "send").side_effect = error
            with mock.patch("covey.backends.claude.runtime.ClaudeProcess", return_value=process):
                return ClaudeBackend().run(RunRequest(ctx, TRANSPORT_PRIVATE_SENTINEL, None, callbacks=RunCallbacks(
                    event=lambda kind, data, control: None), inputs=RunInputs(threading.Event(), run_id=RUN)))

    def test_what_the_gateway_or_the_cli_said_on_stderr_reaches_the_card_when_no_result_row_said_why(self):
        """T872: Claude Code ended before a result row (the gateway dropped, the conversation was gone): the stderr line
        that says why is the card's 技术详情 (backend_said, then failure_said), not only the log; secrets hidden."""
        from covey.core import failure
        for line in ("claude gateway: ssh tunnel failed; inspect private runtime/ssh.log", "claude gateway: RemoteDisconnected",
                     "claude gateway: TimeoutError", "claude gateway: tunnel operation is busy; retry after it finishes",
                     "No conversation found with session ID: b22daa79-4d01-44e0-8687-147cf972b658"):
            with self.subTest(line=line):
                result = self.failed_run(RuntimeError("Unexpected Claude initialization event"), tail=["starting", line, "bye"])
                self.assertEqual(result.outcome.error_code, "transport_error")
                key = line.split(";")[0].split(":")[0]
                self.assertIn(key, result.backend_said)
                view = failure.failure_view({"job_id": 1, "topic_id": "t", "status": "failed", "backend": "claude",
                                             "failure_code": "transport_error", "failure_said": result.backend_said})
                self.assertIn("Claude Code 原话：" + key, view["detail"])
                self.assertNotIn(TRANSPORT_PRIVATE_SENTINEL, result.backend_said)
        # A secret on that line is hidden, the reason kept; with no stderr, the transport's reason, stage and exit code.
        result = self.failed_run(RuntimeError("Unexpected Claude initialization event"),
                                 tail=["claude gateway: RemoteDisconnected token=abc123SECRET"])
        self.assertIn("claude gateway: RemoteDisconnected token=", result.backend_said)
        self.assertNotIn("abc123SECRET", result.backend_said)
        result = self.failed_run(RuntimeError("Unexpected Claude initialization event"), tail=[], exit_code=1)
        self.assertEqual(result.backend_said, "initialize_unexpected_event，阶段 initialize，退出码 1")

    def test_the_failure_event_holds_neither_the_exceptions_message_nor_the_prompt(self):
        found = self.failure_events(OSError(11, TRANSPORT_PRIVATE_SENTINEL), stage="send_input")
        self.assertEqual(found[0]["errno"], 11)
        self.assertNotIn(TRANSPORT_PRIVATE_SENTINEL, json.dumps(found))

    def test_a_run_that_completed_or_was_stopped_leaves_no_failure_event(self):
        from covey.backends.claude.process import failure_event
        from covey.core.result import ExecutionOutcome
        for status, code in (("completed", ""), ("cancelled", "cancelled")):
            self.assertIsNone(failure_event(ExecutionOutcome(status, code), None, "receive_events", mock.Mock(), 0))
        data = failure_event(ExecutionOutcome("failed", "process_exit"), None, "receive_events", SimpleNamespace(), 2)
        self.assertEqual((data["exit_code"], data["stderr"]), (2, []))

    def test_credentials_in_stderr_are_hidden_and_a_session_id_is_not(self):
        from covey.backends.claude.process import scrub
        session = "b22daa79-4d01-44e0-8687-147cf972b658"
        line = scrub(f"401 ANTHROPIC_API_KEY=sk-ant-abcdefgh123456 Authorization: Bearer abcdefghijkl session {session} "
                     "cred hunter2hunter2", {"GATEWAY_TOKEN": "hunter2hunter2", "LANG": "C"})
        for secret in ("sk-ant-abcdefgh123456", "abcdefghijkl", "hunter2hunter2"):
            self.assertNotIn(secret, line)
        self.assertIn(session, line)
        self.assertLessEqual(len(scrub("x " * 1000)), 400)

    def test_the_bridge_writes_the_failure_as_log_lines_of_the_task_runner(self):
        from covey.app.runs import log_backend_failure
        lines = []
        log_backend_failure(lines.append, 20, {"status": "failed", "code": "transport_error", "stage": "initialize",
                                               "exception": "RuntimeError", "reason": "initialize_unexpected_event",
                                               "process_exit_code": 1, "stderr": ["fatal: bad fork", 7, "second"]})
        self.assertEqual(lines[0], "backend failure job=#20 status=failed code=transport_error stage=initialize "
                                   "exception=RuntimeError reason=initialize_unexpected_event process_exit_code=1")
        self.assertEqual(lines[1:], ["backend stderr job=#20 | fatal: bad fork", "backend stderr job=#20 | second"])
        lines.clear()
        log_backend_failure(lines.append, 21, {"status": "failed", "stderr": []})
        self.assertEqual(lines[-1], "backend stderr job=#21 | (nothing)")
        log_backend_failure(lines.append, 22, "not an object")  # Never raises.

    def test_the_real_process_keeps_the_last_lines_of_its_stderr_and_never_blocks_on_it(self):
        from covey.backends.claude.process import STDERR_LINES, ClaudeProcess
        with tempfile.TemporaryDirectory() as folder:
            ctx = dataclasses.replace(codex_context(Path(folder), "stream-json"), workdir=Path(folder))
            code = ("import sys\nfor i in range(500): sys.stderr.write('noise %d token=abc12345678\\n' % i)\n"
                    "sys.stderr.write('fatal: bad fork\\n'); sys.exit(1)")
            process = ClaudeProcess([sys.executable, "-c", code], ctx, dict(os.environ))
            process.process.wait(timeout=20)
            process.close()
            self.assertEqual(len(process.stderr_tail), STDERR_LINES)
            self.assertEqual(process.stderr_tail[-1], "fatal: bad fork")
            self.assertNotIn("abc12345678", " ".join(process.stderr_tail))

    def test_details_that_cannot_be_saved_leave_the_failure_as_it_was(self):
        # Before the guard, a failed write turned the transport failure into the job's own exception.
        with mock.patch("covey.core.state.write_private_text", side_effect=OSError(28, "full")):
            self.assertIsNone(self.run_failure(stage="receive_events", error=OSError(32, "pipe"), saved=False))
