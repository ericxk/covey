"""What the shared bridge promises for every backend, proven once per invariant.

BackendContractTests runs one real turn per live backend (support.backend_drivers)
through the worker, saved results, retry, outbox and session guard; only the model
boundary, Git discovery and the Feishu transport are simulated. HarnessContractTests
and SharedResultGateTests hold the Harness interface and the worker's result gate
against fake backends and deliberately inconsistent results.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import dataclasses
import io
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support import make_context
from support.sessions import write_session_id
from support import extension_enabled, requires_extension
from support.backend_drivers import DRIVERS, QUESTION, point_session_at_other, seed_checkpoint_question
from support.worker_seams import leave_out_side_work
from support.fake_lark import MemoryTransport, install_card_transport
from support.world import harness_app
from support.records import write_topic

import covey.core.session_guard as guards
from covey.app.outbox import tick
from covey.core.result import STATUS_MESSAGES
from covey.cards.review import answer_lead, answer_summary
from covey.backends.claude.bridge import ClaudeBackend
from covey.backends.claude.events import ClaudeEventAdapter
from covey.backends.codex.bridge import CodexBackend
from covey.backends.codex.events import CodexEventAdapter
from covey.backends.dsh.bridge import DshEventAdapter
from covey.core.harness import HarnessRegistry, JobCancelled, RunInputs, RunRequest, RunResult
from covey.core.jobs import BridgeReplyJob, find_job_record
from covey.core.protocol import HarnessEvent
from covey.core.result import ExecutionOutcome, VerifiedRootReply
from covey.core.saved_results import _index, has_saved_reply, load_authorized_reply
from covey.core.session_guard import session_is_blocked
from covey.core.attachments import native_image_attachments
from covey.core.trace import ToolTrace
from covey.feishu.transport import Failure, set_failure


# Whether another process could take a lock file now: what a task runner's next candidate would meet.
LOCK_PROBE = """
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDWR)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    print("free")
except BlockingIOError:
    print("held")
"""


def bodies(run, since=0):
    return [event for event in run.tx.events[since:] if event["kind"] in {"text", "file"}]


class BackendContractTests(unittest.TestCase):
    def cases(self, scenario, resumes=(True,)):
        for driver in DRIVERS:
            if scenario in driver.scenarios:
                for resume in resumes:
                    yield driver, resume

    def assert_saved_candidate(self, run, record, status):
        self.assertEqual(record["execution_status"], status)
        self.assertIs(record["verified_reply_authorized"], True)
        self.assertEqual(record["result_retry_policy"], "saved_delivery_only")
        self.assertEqual(run.authorize.call_count, 1)
        # Until the turn ends, the reply is an unauthorized candidate and its session is held.
        self.assertTrue(run.candidates)
        for row in run.candidates:
            self.assertEqual((row["state"], row["authorized"], row["sid_blocked"]), ("candidate", False, True))
        reply, outcome = load_authorized_reply(run.ctx, record, run.driver.name, run.job.chat_id)
        self.assertEqual(reply.text, run.body)
        self.assertEqual(reply.session_id, run.sid)
        self.assertEqual(reply.run_id, run.job.bridge_run_id)
        self.assertEqual(outcome.reply_fingerprint, reply.fingerprint)
        self.assertEqual(outcome.job_status, status)
        return reply, outcome

    def assert_redelivered_once(self, run, status):
        """The saved body goes out once more on retry; nothing runs, no session changes."""
        first = bodies(run)
        self.assertTrue(first)
        self.assertFalse(any(event["ok"] for event in first))
        calls, cas, protections = run.backend_run.call_count, run.cas.call_count, run.finalize_session.call_count
        run.tx.ok = True
        before = len(run.tx.events)
        self.assertIn("重新投递", run.app.controls.retry_topic(run.ctx, run.job.chat_id, "om-retry-one", job_id=run.job.job_id))
        sent = [event for event in bodies(run, before) if event["ok"]]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["text"], first[-1]["text"])
        if "key" in sent[0]:
            self.assertEqual((sent[0]["key"], sent[0]["chat"], sent[0]["reply_to"]),
                             (first[-1]["key"], run.job.chat_id, run.ctx.reply_to_message_id))
        record = find_job_record(run.ctx.state_dir, run.job.message_id)
        self.assertIs(record["delivery_ok"], True)
        self.assertEqual(record["execution_status"], status)
        count = len(run.tx.events)
        self.assertIn("已送达", run.app.controls.retry_topic(run.ctx, run.job.chat_id, "om-retry-two", job_id=run.job.job_id))
        self.assertEqual(len(run.tx.events), count)
        self.assertEqual((run.backend_run.call_count, run.cas.call_count, run.finalize_session.call_count),
                         (calls, cas, protections))
        self.assertTrue(run.app.runtime.jobs.queue.empty())
        return record, sent[0]["text"]

    def assert_no_question(self, run):
        run.defer_question.assert_not_called()
        run.deliver_question.assert_not_called()
        self.assertFalse(list((run.ctx.state_dir / "questions").glob("*.json")))
        self.assertFalse(any(QUESTION in event.get("text", "") for event in run.tx.events))

    def test_a_saved_reply_is_redelivered_once_without_rerunning_the_model(self):
        for driver, resume in self.cases("complete", resumes=(False, True)):
            with self.subTest(driver, resume=resume), driver.run(self, "complete", resume=resume) as run:
                record = run.process()
                self.assertEqual(record["status"], "failed")  # Only the first send failed.
                self.assertIs(record["delivery_ok"], False)
                _, outcome = self.assert_saved_candidate(run, record, "done")
                self.assertTrue(outcome.resume_allowed)
                self.assertFalse(session_is_blocked(run.ctx, driver.name, run.sid))
                self.assertEqual(run.app.sessions.capture_session(run.ctx)[1], run.sid)
                self.assertTrue(any(call.args[3:] == (run.sid if resume else None, run.sid)
                                    for call in run.cas.call_args_list))
                record, text = self.assert_redelivered_once(run, "done")
                self.assertEqual(record["status"], "done")
                self.assertEqual(text, run.body)
                self.assertEqual(run.app.sessions.capture_session(run.ctx)[1], run.sid)
                self.assert_no_question(run)

    def test_a_partial_reply_goes_out_with_its_notice_and_the_session_stays_sealed(self):
        for driver, _ in self.cases("partial"):
            with self.subTest(driver), driver.run(self, "partial", on_candidate=seed_checkpoint_question) as run:
                record = run.process()
                _, outcome = self.assert_saved_candidate(run, record, "failed")
                self.assertEqual(outcome.status, "failed")
                self.assertFalse(outcome.resume_allowed)
                self.assertTrue(session_is_blocked(run.ctx, driver.name, run.sid))
                self.assertIsNone(run.app.sessions.read_session_id(run.ctx.session_path))
                _, text = self.assert_redelivered_once(run, "failed")
                self.assertTrue(text.endswith(run.body))
                self.assertIn("未全部完成", text)
                self.assertIn("不可续接", text)
                for secret in run.secrets:
                    self.assertNotIn(secret, text)
                self.assert_no_question(run)
                # A stale pointer cannot reopen the sealed session, even after redelivery.
                write_session_id(run.ctx.session_path, run.sid)
                self.assertIsNone(run.app.sessions.capture_session(run.ctx)[1])
                self.assertTrue(session_is_blocked(run.ctx, driver.name, run.sid))
                driver.check_sealed(run)
                self.assertEqual(run.backend_run.call_count, 1)

    def test_a_partial_failure_never_clears_a_newer_session_pointer(self):
        for driver, _ in self.cases("partial"):
            with self.subTest(driver), driver.run(self, "partial", on_candidate=point_session_at_other) as run:
                record = run.process()
                self.assert_saved_candidate(run, record, "failed")
                self.assertEqual(run.app.sessions.read_session_id(run.ctx.session_path), driver.other_sid)
                self.assertTrue(session_is_blocked(run.ctx, driver.name, run.sid))
                self.assertFalse(session_is_blocked(run.ctx, driver.name, driver.other_sid))
                self.assert_redelivered_once(run, "failed")
                self.assertEqual(run.app.sessions.read_session_id(run.ctx.session_path), driver.other_sid)

    def test_a_failed_turn_never_authorizes_or_sends_its_text(self):
        for driver, _ in self.cases("failed"):
            with self.subTest(driver), driver.run(self, "failed", send_ok=True) as run:
                record = run.process()
                self.assertEqual(record["execution_status"], "failed")
                self.assertFalse(record.get("verified_reply_authorized", False))
                self.assertEqual(run.authorize.call_count, 0)
                self.assertTrue(run.secrets)
                for secret in run.secrets:
                    self.assertNotIn(secret, record["final_text"])
                    for event in run.tx.events:
                        self.assertNotIn(secret, event.get("text", ""))
                notices = [event for event in run.tx.events if event["kind"] == "text" and event["ok"]]
                self.assertTrue(notices and all(event["text"].strip() for event in notices))
                self.assertTrue(session_is_blocked(run.ctx, driver.name, run.sid))
                self.assertIsNone(run.app.sessions.read_session_id(run.ctx.session_path))
                with self.assertRaises((ValueError, OSError)):  # OSError: nothing was ever saved.
                    load_authorized_reply(run.ctx, record, driver.name, run.job.chat_id)
                count = len(bodies(run))
                response = run.app.controls.retry_topic(run.ctx, run.job.chat_id, "om-retry", job_id=run.job.job_id)
                tick(run.app, run.ctx)  # Nor does the outbox publish anything.
                self.assertEqual(len(bodies(run)), count)
                for event in run.tx.events:
                    self.assertFalse(any(secret in event.get("text", "") for secret in run.secrets))
                self.assertEqual(run.backend_run.call_count, 1)
                if has_saved_reply(run.ctx, record):  # The preserved text is refused, not redelivered.
                    self.assertIn("不会重新执行", response)
                    self.assertTrue(run.app.runtime.jobs.queue.empty())
                else:  # Nothing was saved: retry is a fresh run of the request.
                    self.assertIn("重新入队", response)

    def test_a_redelivery_sends_without_holding_the_verified_results_lock(self):
        """Every task runner takes that one lock for each candidate and each turn's end: a redelivery holds
        it only to check the saved reply, never across the Feishu send (rule 1)."""
        driver = next(d for d in DRIVERS if "partial" in d.scenarios)
        with driver.run(self, "partial") as run:
            send = run.app.messenger.send_message

            def ambiguous_send(*args, **kwargs):
                value = send(*args, **kwargs)
                set_failure(Failure("network_timeout", True, True))
                return value

            run.app.messenger.send_message = ambiguous_send
            run.process()
            write_topic(run.ctx, {"chat_id": run.job.chat_id, "harness_id": driver.name})
            fresh = harness_app(run.app.spec, run.app.backend, type(run.app.store)())
            run.tx.ok = True
            index = _index(run.ctx.state_dir, find_job_record(run.ctx.state_dir, run.job.message_id)["key"])
            lock, seen = index.parent / ".locks" / (index.name + ".lock"), []

            def probing_send(*args, **kwargs):
                probe = subprocess.run([sys.executable, "-c", LOCK_PROBE, str(lock)], capture_output=True, text=True,
                                       timeout=30)
                seen.append(probe.stdout.strip())
                return run.tx.send_text(*args, **kwargs)

            fresh.messenger.send_message = probing_send
            fresh.artifacts.send_result_file = run.tx.send_file
            fresh.cards.replace_record_card = mock.Mock(return_value=True)
            fresh.harness.context = run.ctx
            tick(SimpleNamespace(runtime=fresh.runtime, apps={fresh.spec.name: fresh}), run.ctx, now=time.time() + 1000)
            self.assertEqual(seen, ["free"])
            after = find_job_record(run.ctx.state_dir, run.job.message_id)
            self.assertEqual((after["delivery_ok"], after["outbox_state"]), (True, "delivered"))

    def test_a_saved_reply_goes_as_the_same_answer_card_on_every_send(self):
        """The answer card is made of the saved reply, the topic and the turn's status alone: the outbox's
        retry after a send that may have landed carries the same card under the same key, so its receipt
        lets it through and Feishu's uuid keeps it one message."""
        driver = next(d for d in DRIVERS if "partial" in d.scenarios)
        with driver.run(self, "partial") as run:
            send = run.app.messenger.send_message

            def ambiguous_send(*args, **kwargs):
                value = send(*args, **kwargs)
                set_failure(Failure("network_timeout", True, True))
                return value

            run.app.messenger.send_message = ambiguous_send
            run.process()
            write_topic(run.ctx, {"chat_id": run.job.chat_id, "harness_id": driver.name})
            fresh = harness_app(run.app.spec, run.app.backend, type(run.app.store)())
            run.tx.ok = True
            fresh.messenger.send_message = run.tx.send_text
            fresh.artifacts.send_result_file = run.tx.send_file
            fresh.cards.replace_record_card = mock.Mock(return_value=True)
            fresh.harness.context = run.ctx
            tick(SimpleNamespace(runtime=fresh.runtime, apps={fresh.spec.name: fresh}), run.ctx, now=time.time() + 1000)
            first, retry = [event for event in bodies(run) if event["kind"] == "text" and event["text"].endswith(run.body)]
            # The same key and text, the same answer in the card; what follows it (the answer card's 产物 and
            # 输入/输出, cards/answer_card.py) is drawn as it stands at each send: one message whichever lands.
            self.assertEqual((retry["key"], retry["text"], retry["card"]["body"]["elements"][0]),
                             (first["key"], first["text"], first["card"]["body"]["elements"][0]))
            self.assertEqual(first["card"]["config"]["summary"]["content"],
                             answer_summary("#7", "failed", answer_lead(run.body)))
            self.assertTrue(first["card"]["body"]["elements"][0]["content"].endswith(run.body))

    def test_after_a_restart_the_outbox_sends_only_the_authorized_reply(self):
        for driver, _ in self.cases("partial"):
            with self.subTest(driver), driver.run(self, "partial") as run:
                send = run.app.messenger.send_message

                def ambiguous_send(*args, **kwargs):  # The send may or may not have landed.
                    value = send(*args, **kwargs)
                    set_failure(Failure("network_timeout", True, True))
                    return value

                run.app.messenger.send_message = ambiguous_send
                run.process()
                self.assertEqual(find_job_record(run.ctx.state_dir, run.job.message_id)["outbox_state"], "pending")
                pointer = run.app.sessions.read_session_id(run.ctx.session_path)
                write_topic(run.ctx, {"chat_id": run.job.chat_id, "harness_id": driver.name})
                fresh = harness_app(run.app.spec, run.app.backend, type(run.app.store)())
                run.tx.ok = True
                fresh.messenger.send_message = run.tx.send_text
                fresh.artifacts.send_result_file = run.tx.send_file
                fresh.cards.replace_record_card = mock.Mock(return_value=True)
                before = len(run.tx.events)
                fresh.harness.context = run.ctx
                tick(SimpleNamespace(runtime=fresh.runtime, apps={fresh.spec.name: fresh}), run.ctx, now=time.time() + 1000)
                after = find_job_record(run.ctx.state_dir, run.job.message_id)
                self.assertEqual((after["delivery_ok"], after["outbox_state"]), (True, "delivered"))
                self.assertEqual(after["execution_status"], "failed")
                self.assertFalse(after["native_resume_allowed"])
                sent = [event for event in bodies(run, before) if event["ok"]]
                self.assertEqual(len(sent), 1)
                self.assertTrue(sent[0]["text"].endswith(run.body))
                self.assertEqual(fresh.sessions.read_session_id(run.ctx.session_path), pointer)
                count = len(run.tx.events)
                tick(fresh, run.ctx, now=time.time() + 2000)
                self.assertEqual(len(run.tx.events), count)
                self.assertEqual(run.backend_run.call_count, 1)
                self.assertTrue(fresh.runtime.jobs.queue.empty())


class HarnessContractTests(unittest.TestCase):
    def test_only_the_native_transports_remain(self):
        # Existing argv may still name the native transport; the retired ones no longer parse.
        for backend, flag, native, retired in [(ClaudeBackend(), "--claude-transport", "stream-json", "cli"),
                                               (CodexBackend(), "--codex-transport", "app-server", "exec")]:
            parser = argparse.ArgumentParser()
            backend.add_cli_args(parser)
            self.assertEqual(backend.build_config(parser.parse_args([flag, native])).options["transport"], native)
            self.assertEqual(backend.build_config(parser.parse_args([])).options["transport"], native)
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                parser.parse_args([flag, retired])
            self.assertTrue(backend.capabilities(None).questions)

    def test_the_removed_magpie_option_still_parses_and_is_ignored(self):
        # The Magpie provider is gone (2026-11-10): an old command line's --codex-magpie-url parses, does nothing
        # and gets one line at the start; COVEY_MAGPIE_URL is not read.
        backend = CodexBackend()
        parser = argparse.ArgumentParser()
        backend.add_cli_args(parser)
        self.assertNotIn("magpie", parser.format_help())
        with tempfile.TemporaryDirectory() as root:
            for argv, env, ignored in ((["--codex-magpie-url", "http://127.0.0.1:3425/v1"], "", True),
                                       (["--codex-magpie-url="], "", False),
                                       ([], "http://127.0.0.1:3425/v1", False)):
                with self.subTest(argv=argv, env=env), mock.patch.dict("os.environ", {"COVEY_MAGPIE_URL": env}):
                    options = backend.build_config(parser.parse_args(argv)).options
                    self.assertFalse([key for key in options if "magpie" in key and key != "ignored_magpie_url"])
                    ctx = make_context(Path(root), options=options)
                    lines = [line for line in backend.startup_lines(ctx) if "no longer used" in line]
                    self.assertEqual(len(lines), int(ignored))
                    self.assertTrue(all(line.startswith("--codex-magpie-url") for line in lines))

    def test_a_codex_job_on_a_magpie_model_is_refused_before_codex_starts(self):
        from support.fake_backends import codex_server
        with tempfile.TemporaryDirectory() as root:
            ctx = make_context(Path(root), model="magpie/relay/model", backend_cmd=["never-run-codex"],
                               options={"transport": "app-server"})
            with codex_server() as server, self.assertRaisesRegex(ValueError, "已下线的供应商「Magpie」.*换配置"):
                CodexBackend().run(RunRequest(ctx, "probe"))
            server.assert_not_called()

    def test_the_removed_claude_profiles_option_still_parses_and_is_ignored(self):
        # Claude gateway profiles are gone (2026-11-14): an old command line's --claude-profiles parses, does
        # nothing and gets one line at the start.
        backend = ClaudeBackend()
        parser = argparse.ArgumentParser()
        backend.add_cli_args(parser)
        self.assertNotIn("claude-profiles", parser.format_help())
        with tempfile.TemporaryDirectory() as root:
            for argv, ignored in ((["--claude-profiles", "/absolute/claude-profiles.json"], True), ([], False)):
                with self.subTest(argv=argv):
                    options = backend.build_config(parser.parse_args(argv)).options
                    self.assertFalse([key for key in options if "profiles" in key and key != "ignored_claude_profiles"])
                    lines = [line for line in backend.startup_lines(make_context(Path(root), options=options))
                             if "no longer used" in line]
                    self.assertEqual(len(lines), int(ignored))
                    self.assertTrue(all(line.startswith("--claude-profiles") for line in lines))

    def test_a_claude_job_on_a_profile_model_is_refused_before_claude_starts(self):
        with tempfile.TemporaryDirectory() as root:
            ctx = make_context(Path(root), model="profile/west/opus", backend_cmd=["never-run-claude"],
                               options={"transport": "stream-json"})
            with self.assertRaisesRegex(ValueError, "已下线的供应商「Claude 网关档位」.*换配置"):
                ClaudeBackend().run(RunRequest(ctx, "probe"))

    def test_registry_unknown_and_duplicate(self):
        registry = HarnessRegistry()
        registry.register("claude", ClaudeBackend)
        self.assertIsInstance(registry.create("claude"), ClaudeBackend)
        with self.assertRaises(ValueError):
            registry.create("absent")
        with self.assertRaises(ValueError):
            registry.register("claude", CodexBackend)

    def test_cancelled_run_never_starts_backend(self):
        cancel = threading.Event()
        cancel.set()
        backend = ClaudeBackend()
        with mock.patch("covey.backends.claude.runtime.run_stream") as run, self.assertRaises(JobCancelled):
            backend.run(RunRequest(SimpleNamespace(options={}), "prompt", inputs=RunInputs(cancel)))
        run.assert_not_called()

    def test_image_inputs_only_downloaded_referenced_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            media = root / "media"
            media.mkdir()
            image = media / "photo.png"
            image.write_bytes(b"png")
            outside = root / "outside.png"
            outside.write_bytes(b"png")
            (media / "link.png").symlink_to(outside)
            text = f"`{image}` `{image}` `{outside}` `{media / 'link.png'}`"
            self.assertEqual(native_image_attachments(text, media), [{"path": str(image), "mime": "image/png"}])

    def test_words_in_backquotes_that_are_no_path_never_fail_the_turn(self):
        # T480/T481 (2026-11-11): a history holding `~$预算表.docx`, an Office lock file's name, failed every turn at
        # its start: pathlib's expanduser raised RuntimeError("Could not determine home directory").
        from covey.app.runs import Runs
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            media = root / "incoming_media"
            media.mkdir()
            image = media / "photo.png"
            image.write_bytes(b"png")
            text = (f"同事发的 `~$预算表.docx`、`~nosuchuser/a.png`、`~`、`a\x00b.png`、`{'x' * 5000}.png` "
                    f"和截图 `{image}`")
            self.assertEqual(native_image_attachments(text, media), [{"path": str(image), "mime": "image/png"}])
            ctx = SimpleNamespace(history_path=root / "history.jsonl", state_dir=root / "state")
            self.assertEqual(Runs._image_attachments(ctx, text), [{"path": str(image), "mime": "image/png"}])

    def test_an_image_over_a_harness_limit_is_left_out_and_said(self):
        # The owner, 2026-11-05: 跳过并提示. Claude Code ended a turn with a 5 MB+ image with protocol_error.
        from covey.app.runs import Runs
        from covey.core.attachments import MAX_IMAGE_BYTES, encoded_size, fit_images
        from covey.core.harness import HarnessCapabilities
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            files = {}
            for name, size in (("a.png", 10), ("big.png", 30), ("c.png", 10), ("d.png", 3)):
                (root / name).write_bytes(b"x" * size)
                files[name] = {"path": str(root / name), "mime": "image/png"}
            a, big, c, d = files.values()
            kept, skipped = fit_images([a, big, c], limit=20)
            self.assertEqual((kept, [(s["path"], s["size"], s["reason"]) for s in skipped]),
                             ([a, c], [(big["path"], 30, "image")]))
            # The request's room: a fits beside 5 bytes of text, c no longer does; a later smaller one still may.
            kept, skipped = fit_images([a, c, d], frame=5 + encoded_size(10) + encoded_size(3), used=5)
            self.assertEqual((kept, [s["reason"] for s in skipped]), ([a, d], ["frame"]))
            self.assertEqual(fit_images([a], limit=0, frame=0), ([a], []))

            logs, events = [], []
            runs = SimpleNamespace(runtime=SimpleNamespace(log=logs.append), interaction=SimpleNamespace(
                structured_harness_event=lambda ctx, job, kind, data, rpc, **kw: events.append((kind, data, kw))))
            job = SimpleNamespace(job_id=7, tool_trace=object())
            prompt, kept = Runs._fit_images(runs, None, job, HarnessCapabilities(image_bytes=20), "看图", [big, a])
            self.assertEqual(kept, [a])
            self.assertTrue(prompt.startswith("看图\n\n[这些图片没有作为图片附上"))
            self.assertIn(f"`{big['path']}`（0.0 MB，超过单张 0.0 MB 的上限）", prompt)
            self.assertEqual(logs, ["images not attached job=#7 skipped=1 kept=1 sizes=30 reasons=image"])
            (kind, data, kw), = events
            self.assertEqual((kind, data["title"], kw), ("progress_note", "机器人", {"from_backend": False}))
            self.assertIn("1 张图片没有直接交给模型：big.png", data["text"])  # A name, not the path, on the card.
            # The note itself can take the room of an image kept: then that one is left out too.
            prompt, kept = Runs._fit_images(runs, None, job, HarnessCapabilities(image_bytes=20, image_frame=1 + 4),
                                            "p", [d, big])
            self.assertEqual(kept, [])
            self.assertIn(f"`{d['path']}`", prompt)
            # A Harness that declares no limit gets every image, as before; nothing is said.
            self.assertEqual(Runs._fit_images(runs, None, job, HarnessCapabilities(), "p", [big]), ("p", [big]))
            self.assertEqual(len(events), 2)
        claude = ClaudeBackend().capabilities(SimpleNamespace(model="claude/opus"))
        self.assertEqual((claude.image_bytes, claude.image_frame), (MAX_IMAGE_BYTES, 8 * 1024 * 1024 - 4096))
        # Claude Code's own models take questions, steering and forks.
        self.assertTrue(claude.questions and claude.steering and claude.native_fork)
        self.assertFalse(hasattr(claude, "redaction"))  # 敏感词脱敏 was taken out (T947).

    def test_quoted_and_forwarded_images_use_native_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a = root / "quoted.png"
            b = root / "forwarded.png"
            a.write_bytes(b"png")
            b.write_bytes(b"png")
            text = f"[被回复图片: {a}]\n[图片: {b}]"
            self.assertEqual([r["path"] for r in native_image_attachments(text, root)], [str(a), str(b)])

    def test_vendor_tools_share_event_lifecycle_without_reasoning(self):
        events = []
        emit = lambda k, d: events.append((k, d))
        adapter = CodexEventAdapter(emit)
        adapter.item("item/started", {"id": "x", "type": "commandExecution", "command": "pwd"}, 1)
        adapter.item("item/completed", {"id": "x", "type": "commandExecution", "exitCode": 1}, 3)
        self.assertEqual(events[-1][1]["status"], "failed")
        self.assertEqual(events[-1][1]["detail"], "pwd")
        claude = ClaudeEventAdapter(emit)
        claude.accept(
            {
                "message": {
                    "content": [
                        {"type": "thinking", "thinking": "private"},
                        {"type": "tool_use", "id": "c", "name": "Read", "input": {"file_path": "/tmp/a"}},
                    ]
                }
            }
        )
        claude.accept(
            {"message": {"content": [{"type": "tool_result", "tool_use_id": "c", "content": "SECRET_OUTPUT"}]}}
        )
        self.assertEqual(events[-1][1]["status"], "done")
        self.assertNotIn("SECRET_OUTPUT", str(events))
        self.assertNotIn("private", str(events))
        with self.assertRaises(ValueError):
            HarnessEvent("tool", {}, 2).deliver(mock.Mock())

    def test_progress_failures_never_fail_a_turn_but_input_failures_do(self):
        broken = mock.Mock(side_effect=OSError("disk full"))
        for kind in ("tool", "progress_note", "agent_state", "answer_snapshot", "trace_finished"):
            with self.assertLogs("covey.core.protocol", level="WARNING") as logs:
                HarnessEvent(kind, {}).deliver(broken)
            self.assertIn("OSError", logs.output[0])
            self.assertNotIn("disk full", logs.output[0])
        for kind in ("question", "cancel_questions", "steer_read"):
            with self.assertRaises(OSError):
                HarnessEvent(kind, {}).deliver(broken)


class StepDriver:
    """One adapter and the rows that start and end its steps; ``events`` is what it published.
    ``repeat`` is a start repeated while the step runs (a compaction's keepalive where there is one),
    ``stray`` an end it cannot place, ``fetch`` a tool given a URL (None: no such tool)."""
    URL = "https://example.invalid/doc?token=PRIVATE#part"

    def __init__(self):
        self.events, self.now = [], 100.0
        self.adapter = self.make(lambda kind, data: self.events.append((kind, copy.deepcopy(data))))

    def tick(self):
        self.now += 1
        return self.now

    def repeat(self):
        self.start("again")
        self.start("again")


class ClaudeSteps(StepDriver):
    def make(self, emit):
        return ClaudeEventAdapter(emit)

    def tool(self, key, name, inputs):
        self.adapter.accept({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": key, "name": name, "input": inputs}]}})

    def start(self, key):
        self.tool(key, "Read", {"file_path": "/w/a.py"})

    def end(self, key):
        self.adapter.accept({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": key, "content": "ok"}]}})

    def repeat(self):  # Claude Code repeats status: compacting every 30 s while one compaction runs.
        for _ in range(2):
            self.adapter.accept({"type": "system", "subtype": "status", "status": "compacting"})

    def stray(self):
        self.end("nobody")
        self.adapter.accept({"type": "system", "subtype": "status", "status": None, "compact_result": "failed"})

    def fetch(self, key):
        self.tool(key, "WebFetch", {"url": self.URL})


class CodexSteps(StepDriver):
    fetch = None  # Codex runs no fetch tool of its own: a URL comes only inside a command or a search.

    def make(self, emit):
        return CodexEventAdapter(emit, "main")

    def start(self, key):
        self.adapter.item("item/started", {"id": key, "type": "commandExecution", "command": "ls"}, self.tick())

    def end(self, key):
        self.adapter.item("item/completed", {"id": key, "type": "commandExecution", "command": "ls", "exitCode": 0},
                          self.tick())

    def stray(self):  # item/completed carries the whole item: only one with no id, or of no step's type, is stray.
        self.adapter.item("item/completed", {"type": "commandExecution", "exitCode": 0}, self.tick())
        self.adapter.item("item/completed", {"id": "m", "type": "agentMessage", "text": "x"}, self.tick())


class DshSteps(StepDriver):
    def make(self, emit):
        return DshEventAdapter(emit)

    def start(self, key, tool="Read", inputs=None):
        self.adapter.accept({"type": "tool_call", "callId": key, "tool": tool, "input": inputs or {"path": "a.py"}},
                            self.tick())

    def end(self, key):
        self.adapter.accept({"type": "tool_result", "callId": key, "status": "completed"}, self.tick())

    def stray(self):
        self.end("nobody")

    def fetch(self, key):
        self.start(key, "WebFetch", {"url": self.URL})


class MasoSteps(StepDriver):
    def make(self, emit):
        from covey_internal.maso.progress import ProgressReporter  # The company extension.

        self.workdir = tempfile.TemporaryDirectory()
        self.ctx = SimpleNamespace(agent_id="root", stop_reason="completed")
        reporter = ProgressReporter("run", "root", self.workdir.name, lambda kind, **data: emit(kind, data),
                                    clock=self.tick)
        reporter.observe("start", self.ctx)
        return reporter

    def call(self, key, name="Read", args=None):
        return SimpleNamespace(id=key, function={"name": name, "arguments": json.dumps(args or {"file_path": "a.py"})})

    def start(self, key):
        self.adapter.observe("tool_start", self.ctx, self.call(key))

    def end(self, key):
        self.adapter.observe("tool_result", self.ctx, self.call(key), {}, True)

    def stray(self):
        self.end("nobody")

    def fetch(self, key):
        self.adapter.observe("tool_start", self.ctx, self.call(key, "WebFetch", {"url": self.URL}))


class StepLifecycleContractTests(unittest.TestCase):
    def test_every_adapter_keeps_one_step_lifecycle(self):
        """An ended step never runs again, a start repeated while it runs is that one step, an end the
        adapter cannot place publishes nothing, and a URL's query and fragment never reach a detail."""
        for driver in (ClaudeSteps, CodexSteps, DshSteps, *([MasoSteps] if extension_enabled() else [])):
            with self.subTest(driver.__name__):
                d, trace = driver(), ToolTrace()
                if isinstance(d, MasoSteps):
                    self.addCleanup(d.workdir.cleanup)
                tools = lambda since: [data for kind, data in d.events[since:] if kind == "tool"]
                d.start("s")
                d.end("s")
                ended = len(d.events)
                d.start("s")
                for data in tools(0):
                    trace.accept("tool", data)
                self.assertEqual([data["status"] for data in tools(0)][-1], "done")
                self.assertEqual(tools(ended), [])
                self.assertEqual([step["status"] for step in trace.steps.values()], ["done"])

                since = len(d.events)
                d.stray()  # Before repeat leaves a compaction open, which its end would then close.
                self.assertEqual(tools(since), [])

                since = len(d.events)
                d.repeat()
                self.assertTrue(tools(since))
                self.assertEqual(len({data["id"] for data in tools(since)}), 1)
                for data in tools(since):
                    trace.accept("tool", data)
                self.assertEqual(len(trace.steps), 2)

                if d.fetch is not None:
                    since = len(d.events)
                    d.fetch("f")
                    self.assertTrue(tools(since))
                    for data in tools(since):
                        self.assertNotIn("PRIVATE", str(data))
                        self.assertNotIn("?", data.get("detail", ""))
                        self.assertNotIn("#", data.get("detail", ""))


SID = "00000000-0000-4000-8000-000000000055"


UNVERIFIED = "SYNTHETIC_UNVERIFIED_ROOT_MUST_NOT_BE_DELIVERED"


def shared_worker(stack):
    from covey_internal.maso.bridge import MASO_SPEC, MasoBackend  # The company extension.
    from covey_internal.maso.sessions import MasoSessionStore

    """The real shared worker over a MASO app; backend.run is for each test to replace."""
    root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="typed-result-gate-")))
    ctx = dataclasses.replace(make_context(root, profile="synthetic-maso"),
        topic_id="typed-gate-topic", reply_to_message_id="om-typed-gate")
    ctx.state_dir.mkdir(mode=0o700)
    write_session_id(ctx.session_path, SID)
    backend = MasoBackend()
    app = harness_app(MASO_SPEC, backend, MasoSessionStore())
    tx = MemoryTransport(card_ok=False, text_ok=True, file_ok=True)
    app.runtime.log_sink = mock.Mock()
    app.messenger.send_message, app.artifacts.send_result_file = tx.send_text, tx.send_file
    app.cards.streaming = False
    app.cards.finish_result_controls = mock.Mock()
    app.cards.stream = dataclasses.replace(app.cards.stream,
        card_limit=20000, replacement_limit=0,
        send_text_message=tx.send_text)
    install_card_transport(stack, tx)
    leave_out_side_work(stack)
    return ctx, backend, app, tx


def typed_job(ctx, text):
    return BridgeReplyJob(job_id=31, user_text=text,
        chat_id="chat-typed-gate", message_id="om-typed-gate-job", topic_id=ctx.topic_id,
        reply_to_message_id=ctx.reply_to_message_id, timeout=0,
        model=ctx.model, effort=ctx.effort, model_settings_snapshot=True, progress=None)


@requires_extension()  # Its worker is the MASO one.
class SharedResultGateTests(unittest.TestCase):
    def test_completed_flags_without_a_bound_root_never_release_or_record_a_session(self):
        for mode in ("no_reply_empty_fingerprint", "no_reply_nonempty_fingerprint",
                     "reply_empty_fingerprint", "reply_wrong_fingerprint"):
            with self.subTest(mode=mode), contextlib.ExitStack() as stack:
                ctx, backend, app, tx = shared_worker(stack)
                finalizer = stack.enter_context(mock.patch.object(app.sessions, "finalize_session_from_worker", wraps=app.sessions.finalize_session_from_worker))
                recorder = stack.enter_context(mock.patch.object(app.sessions, "record_session_from_worker", wraps=app.sessions.record_session_from_worker))
                import covey.app.turn_settle as turn_settle
                authorizer = stack.enter_context(mock.patch.object(turn_settle, "authorize_verified_reply", wraps=turn_settle.authorize_verified_reply))
                inputs = []

                def invalid_backend_result(request):
                    request.callbacks.session(SID)
                    reply = None
                    if mode.startswith("reply_"):
                        reply = VerifiedRootReply("maso", request.inputs.run_id, SID,
                            ("synthetic-user",), ("synthetic-root",), UNVERIFIED)
                    request.callbacks.verified_reply(reply)
                    fingerprint = "" if "empty_fingerprint" in mode and "nonempty" not in mode else "f" * 64
                    if reply is not None:
                        self.assertNotEqual(fingerprint, reply.fingerprint)
                    outcome = ExecutionOutcome("completed", identity_confirmed=True,
                        cleanup_confirmed=True, protection_confirmed=True, resume_allowed=True,
                        reply_fingerprint=fingerprint)
                    inputs.append((reply, outcome))
                    return RunResult(UNVERIFIED, SID, public_reply=reply, outcome=outcome)

                backend.run = mock.Mock(side_effect=invalid_backend_result)
                job = typed_job(ctx, "Exercise an invalid typed backend result.")
                guard_path = guards._path(ctx, "maso", SID)
                removed = []
                original_unlink = Path.unlink

                def unlink(path, *args, **kwargs):
                    if path == guard_path:
                        removed.append(str(path))
                    return original_unlink(path, *args, **kwargs)

                with mock.patch.object(Path, "unlink", autospec=True, side_effect=unlink):
                    app.process_reply_job(job, ctx)
                backend.run.assert_called_once()
                self.assertEqual(len(inputs), 1)
                self.assertEqual(inputs[0][1].status, "completed")
                self.assertTrue(inputs[0][1].resume_allowed)
                self.assertTrue(inputs[0][1].delivery_authorized)
                self.assertEqual(removed, [], "final failure must not hide an earlier protection unlink")
                self.assertFalse(any(len(call.args) > 5 and call.args[5] is True
                                     for call in finalizer.call_args_list))
                recorder.assert_not_called()
                self.assertTrue(guards.session_is_blocked(ctx, "maso", SID))
                self.assertIsNone(app.sessions.read_session_id(ctx.session_path))
                record = find_job_record(ctx.state_dir, job.message_id)
                self.assertEqual(record["status"], "failed")
                self.assertIsNot(record.get("native_resume_allowed"), True)
                self.assertIsNot(record.get("verified_reply_authorized"), True)
                if mode.startswith("no_reply_"):
                    authorizer.assert_not_called()
                self.assertNotIn(UNVERIFIED, str(record.get("final_text", "")))
                self.assertNotIn(UNVERIFIED, repr(tx.events))
                for output in (str(record.get("final_text", "")), repr(tx.events)):
                    self.assertNotIn("已保留可验证的本轮结果", output)
                if ctx.history_path.exists():
                    history = ctx.history_path.read_text()
                    self.assertNotIn(UNVERIFIED, history)
                    self.assertNotIn("已保留可验证的本轮结果", history)
                self.assertTrue(app.runtime.jobs.queue.empty())

    def test_a_throttled_run_says_why_and_how_to_go_on(self):
        with contextlib.ExitStack() as stack:
            ctx, backend, app, tx = shared_worker(stack)
            # The card with no automatic retry pending. The extension's Harness declares rate_limit_retry as every
            # Harness does (test_auto_retry), and what a pending retry says is test_auto_retry's.
            stack.enter_context(mock.patch("covey.app.auto_retry.applies", return_value=False))

            def throttled(request):
                request.callbacks.session(SID)
                request.callbacks.verified_reply(None)
                # What MASO's bridge returns when the root failed on an upstream rate limit.
                outcome = ExecutionOutcome("failed", "rate_limited", identity_confirmed=True,
                                           cleanup_confirmed=True, protection_confirmed=True)
                return RunResult("MASO 本轮未全部完成，原生会话不可续接", SID, True, None, outcome)

            backend.run = mock.Mock(side_effect=throttled)
            job = typed_job(ctx, "Continue after the answer.")
            app.process_reply_job(job, ctx)
            record = find_job_record(ctx.state_dir, job.message_id)
            self.assertEqual(record["status"], "failed")
            self.assertEqual(record["status_message"], STATUS_MESSAGES["rate_limited"])
            text = record["final_text"]
            for token in ("模型服务限流", "token 速率", "额度", "与任务内容无关", "怎么继续",
                          "在本话题直接发一条消息", "🔁 换配置"):
                self.assertIn(token, text)
            self.assertNotIn("原生会话不可续接", text)
            self.assertNotIn("未取得可交付的已验证公开回复", text)
            # The guidance holds: the next message starts a new native session.
            self.assertIsNone(app.sessions.read_session_id(ctx.session_path))

    def test_a_routes_own_rate_limit_words_replace_the_shared_detail_on_the_card(self):
        from covey.backends.gateway.connection import busy_text
        from support import acme as acme_gateway
        BUSY_TEXT = busy_text(acme_gateway())
        with contextlib.ExitStack() as stack:
            ctx, backend, app, tx = shared_worker(stack)

            def busy(request):
                request.callbacks.session(SID)
                request.callbacks.verified_reply(None)
                # What Codex's Acme route and dsh return when Acme stayed busy for the whole window.
                outcome = ExecutionOutcome("failed", "rate_limited", identity_confirmed=True,
                                           cleanup_confirmed=True, protection_confirmed=True)
                return RunResult(BUSY_TEXT, SID, True, None, outcome, BUSY_TEXT)

            backend.run = mock.Mock(side_effect=busy)
            job = typed_job(ctx, "Continue after the answer.")
            app.process_reply_job(job, ctx)
            text = find_job_record(ctx.state_dir, job.message_id)["final_text"]
            self.assertTrue(text.startswith("本轮任务未全部完成。" + BUSY_TEXT), text)
            self.assertIn("60 分钟", text)
            self.assertNotIn("token 速率", text)  # Not the shared "rate or quota" guess.
            for token in ("怎么继续", "在本话题直接发一条消息", "🔁 换配置"):
                self.assertIn(token, text)
            self.assertNotIn("原生会话不可续接", text)

    def test_policy_stop_survives_worker_delivery_and_cannot_be_retried(self):
        from covey.backends.claude.policy import policy_signal
        from covey.app.outbox import retry_label
        for cleanup in (True, False):
            with self.subTest(cleanup=cleanup), contextlib.ExitStack() as stack:
                ctx, backend, app, tx = shared_worker(stack)
                policy = policy_signal({"type": "assistant", "parent_tool_use_id": "child-7", "message": {
                    "stop_reason": "refusal", "stop_details": {"type": "refusal", "category": "frontier_llm"}}})

                def stopped(request):
                    request.callbacks.session(SID)
                    request.callbacks.verified_reply(None)
                    outcome = ExecutionOutcome("cancelled" if cleanup else "unknown",
                        "policy_refusal" if cleanup else "cleanup_unconfirmed", identity_confirmed=True,
                        cleanup_confirmed=cleanup, protection_confirmed=True, policy_event=policy)
                    return RunResult(outcome.failure_reply(), SID, True, None, outcome)

                backend.run = mock.Mock(side_effect=stopped)
                job = typed_job(ctx, "Read the project.")
                app.process_reply_job(job, ctx)
                record = find_job_record(ctx.state_dir, job.message_id)
                self.assertEqual(record["policy_stop"], policy)
                self.assertEqual(record["auto_retry_state"], "cancelled")
                self.assertIn("frontier_llm", record["final_text"])
                self.assertIn("不会自动重试", record["final_text"])
                self.assertEqual(retry_label(record), "")
                self.assertFalse(record.get("native_resume_allowed"))
                self.assertTrue(app.runtime.jobs.queue.empty())
                self.assertIsNone(app.sessions.read_session_id(ctx.session_path))
                # A stale retry action cannot requeue a stopped policy task.
                app.controls.retry_topic(ctx, job.chat_id, "stale-policy-click", job_id=job.job_id)
                self.assertEqual(backend.run.call_count, 1)
                self.assertTrue(app.runtime.jobs.queue.empty())

    def test_a_reply_cut_off_by_the_background_wait_limit_is_sent_with_its_notice(self):
        with contextlib.ExitStack() as stack:
            ctx, backend, app, tx = shared_worker(stack)

            def cut_off(request):
                request.callbacks.session(SID)
                reply = VerifiedRootReply("maso", request.inputs.run_id, SID, ("synthetic-user",),
                                          ("synthetic-root",), "Step 475 is done; next check after 23:00.")
                request.callbacks.verified_reply(reply)
                # What Claude's stream returns when the wait ran out with only background tasks left.
                outcome = ExecutionOutcome("timed_out", "background_timeout", identity_confirmed=True,
                                           cleanup_confirmed=True, protection_confirmed=True,
                                           reply_fingerprint=reply.fingerprint)
                return RunResult(reply.text, SID, True, reply, outcome)

            backend.run = mock.Mock(side_effect=cut_off)
            job = typed_job(ctx, "How is it going?")
            app.process_reply_job(job, ctx)
            record = find_job_record(ctx.state_dir, job.message_id)
            self.assertEqual(record["status"], "failed")
            self.assertEqual(record["status_message"], STATUS_MESSAGES["background_timeout"])
            self.assertIs(record["verified_reply_authorized"], True)
            notice = ExecutionOutcome("timed_out", "background_timeout").notice()
            sent = [text for kind, text, ok in tx.events if kind == "body_text" and ok]
            self.assertIn(notice + "\n\nStep 475 is done; next check after 23:00.", sent)
            # The next session sees it: the reply is in the topic history.
            self.assertIn("Step 475 is done", ctx.history_path.read_text())
            self.assertIsNone(app.sessions.read_session_id(ctx.session_path))

    def test_a_stopped_task_still_sends_its_last_complete_reply(self):
        with contextlib.ExitStack() as stack:
            ctx, backend, app, tx = shared_worker(stack)
            job = typed_job(ctx, "Watch the run.")

            def stopped(request):
                request.callbacks.session(SID)
                reply = VerifiedRootReply("maso", request.inputs.run_id, SID, ("synthetic-user",),
                                          ("synthetic-root",), "Still waiting on the background check.")
                request.callbacks.verified_reply(reply)
                job.cancel_event.set()  # The user pressed 停止 while only background work ran.
                outcome = ExecutionOutcome("cancelled", "cancelled", identity_confirmed=True,
                                           cleanup_confirmed=True, protection_confirmed=True,
                                           reply_fingerprint=reply.fingerprint)
                return RunResult(reply.text, SID, True, reply, outcome)

            backend.run = mock.Mock(side_effect=stopped)
            app.process_reply_job(job, ctx)
            record = find_job_record(ctx.state_dir, job.message_id)
            self.assertEqual(record["status"], "cancelled")
            self.assertEqual(record["status_message"], STATUS_MESSAGES["cancelled"])
            notice = ExecutionOutcome("cancelled", "cancelled").notice()
            sent = [text for kind, text, ok in tx.events if kind == "body_text" and ok]
            self.assertIn(notice + "\n\nStill waiting on the background check.", sent)
            self.assertIn("Still waiting on the background check.", ctx.history_path.read_text())
