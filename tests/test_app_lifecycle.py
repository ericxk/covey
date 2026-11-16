from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from support import REPO, extension_enabled, make_context, quiet, requires_extension
from support.fake_backends import FakeBackend
from support.fake_backends import RecordingBackend
from support.world import TopicFixture, build_claude_app

from covey.app import runner
from covey.app.task_card import StreamingReply
from covey.app.topics import DEFAULT
from covey.app.unified import UnifiedBridgeApp
from covey.core.harness import RunResult
from covey.core.jobs import BridgeReplyJob as ReplyJob, find_job_record
from covey.core.history_storage import append_history
from covey.core.topics import context_for_topic


class HistorySnapshotTest(unittest.TestCase):
    """Queue backlog must not leak later messages into an earlier job's prompt."""

    def test_worker_uses_enqueue_time_snapshot(self) -> None:
        backend = FakeBackend()
        app = build_claude_app(backend)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx = make_context(root)
            append_history(ctx.history_path, "user", "早期消息")
            job = ReplyJob(
                job_id=1,
                user_text="早期消息",
                chat_id="oc",
                message_id="om1",
                timeout=0,
                history_snapshot=[{"role": "user", "content": "早期消息"}],
            )
            append_history(ctx.history_path, "user", "后来的消息")
            app.runs.generate_reply_for_worker(ctx, job, None, None)
            prompt = backend.prompts[-1]
            self.assertIn("早期消息", prompt)
            self.assertNotIn("后来的消息", prompt)

    def test_missing_workdir_is_named_and_starts_no_backend(self) -> None:
        import dataclasses
        backend = FakeBackend()
        app = build_claude_app(backend)
        with tempfile.TemporaryDirectory() as tmp:
            ctx = dataclasses.replace(make_context(Path(tmp)), workdir=Path(tmp) / "renamed-away")
            job = ReplyJob(job_id=1, user_text="x", chat_id="oc", message_id="om1", timeout=0, history_snapshot=[])
            result = app.runs.generate_reply_for_worker(ctx, job, None, None)
        self.assertEqual(backend.prompts, [])
        self.assertTrue(result.is_error)
        self.assertEqual(result.outcome.error_code, "workdir_missing")
        self.assertIn("工作目录不存在", result.text)

    def test_worker_falls_back_to_fresh_history_without_snapshot(self) -> None:
        backend = FakeBackend()
        app = build_claude_app(backend)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx = make_context(root)
            append_history(ctx.history_path, "user", "唯一消息")
            job = ReplyJob(job_id=1, user_text="唯一消息", chat_id="oc", message_id="om1", timeout=0)
            app.runs.generate_reply_for_worker(ctx, job, None, None)
            self.assertIn("唯一消息", backend.prompts[-1])

    def test_flow_enqueue_snapshots_history_before_backlog_grows(self) -> None:
        backend = FakeBackend()
        app = build_claude_app(backend)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # A topic of this Harness, as every submission has.
            ctx = context_for_topic(make_context(root, options={"harness_id": app.spec.name}),
                                    {"chat_id": "oc", "message_id": "om1"}, lambda _: root)
            with (
                mock.patch.object(app.cards, "create_streaming_reply", return_value=None),
                mock.patch.object(app.messenger, "send_message", return_value=True),
            ):
                append_history(ctx.history_path, "user", "第一条消息")
                job = app.submission.enqueue_chat_job(ctx, "第一条消息", "oc", "om1")
                # a second message arrives while job #1 is still queued
                append_history(ctx.history_path, "user", "第二条消息")

            queued = app.runtime.jobs.queue.get_nowait()
            self.assertEqual(queued.job_id, job.job_id)
            assert queued.history_snapshot is not None
            snapshot_text = "\n".join(str(row.get("content")) for row in queued.history_snapshot)
            self.assertIn("第一条消息", snapshot_text)
            self.assertNotIn("第二条消息", snapshot_text)
            # The queued job carries it; its record does not (S41: never read back, 40% of jobs/).
            self.assertNotIn("history_snapshot", find_job_record(ctx.state_dir, queued.message_id))

            app.runs.generate_reply_for_worker(ctx, queued, None, None)
            prompt = backend.prompts[-1]
            self.assertIn("第一条消息", prompt)
            self.assertNotIn("第二条消息", prompt)


class ToolBackend(RecordingBackend):
    """Runs one tool step, so the job's process panel gets its own updater thread."""

    def run(self, request):
        for status in ("running", "completed"):
            request.emit("tool", {"id": "a", "name": "bash", "status": status, "detail": "ls", "started_at": time.time()})
        return RunResult("完成", request.session_id or str(uuid.uuid4()))


class TraceUpdaterLifetimeTest(TopicFixture, unittest.TestCase):
    """A job whose final card write did not happen leaves no process-panel updater behind, so a
    task runner exits right after delivery instead of waiting out its grace period."""

    def run_traced_job(self):
        self.app.harness.backend = ToolBackend()
        before = set(threading.enumerate())
        self.receive("om_1", "hello")
        job = self.work()
        self.assertIsNotNone(job.trace_card_updates)
        return before

    def assert_runner_exits_at_once(self, before):
        self.assertEqual([], [t.name for t in runner._background(before)])
        with mock.patch.object(runner, "UPDATE_GRACE", 5):
            started = time.monotonic()
            runner._wait_background(before)
        self.assertLess(time.monotonic() - started, 1)

    def test_no_task_card(self):
        self.assert_runner_exits_at_once(self.run_traced_job())

    def test_failed_final_card_write(self):
        self.app.cards.create_streaming_reply = mock.Mock(return_value=StreamingReply("card", "om_card"))
        self.app.messenger.write_card = mock.Mock(return_value=None)
        self.app.messenger.put_card = mock.Mock(return_value=None)
        self.assert_runner_exits_at_once(self.run_traced_job())


class StreamingCloseLifetimeTest(unittest.TestCase):
    """The write that ends a card's streaming mode finishes before a task runner exits."""

    def test_runner_waits_for_the_streaming_close(self):
        from covey.app.worker import close_streaming_reply_async

        before = set(threading.enumerate())
        release, closed = threading.Event(), []

        class Cards:
            def close_streaming_reply(self, ctx, progress):
                release.wait(2)
                closed.append(progress)

        progress = StreamingReply(card_id="card", message_id="om_card", element_id="reply", sequence=1)
        close_streaming_reply_async(None, progress, Cards())
        self.assertEqual(["card-close"], [t.name for t in runner._background(before)])
        threading.Timer(0.2, release.set).start()
        runner._wait_background(before)
        self.assertEqual([progress], closed)
        self.assertTrue(progress.disabled)


class StalePidTest(unittest.TestCase):
    def test_failed_startup_removes_pid_file(self) -> None:
        app = UnifiedBridgeApp(script_path=REPO / "bin/covey_bridge.py")
        quiet(app)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            argv = ["--state-dir", str(state_dir), "--workdir", str(root), "--profile", "unit"]
            with mock.patch.object(app, "init_target", side_effect=RuntimeError("auth unavailable")):
                with self.assertRaises(RuntimeError):
                    app.main(argv)
            self.assertFalse((state_dir / "bridge.pid").exists())

    def test_the_startup_check_at_startup_counts_for_the_first_job(self) -> None:
        app = UnifiedBridgeApp(script_path=REPO / "bin/covey_bridge.py")
        quiet(app)
        harness = app.harnesses[DEFAULT]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            argv = ["--state-dir", str(root / "state"), "--workdir", str(root), "--profile", "unit"]
            with mock.patch.object(app, "init_target"), mock.patch.object(harness.backend, "on_startup") as startup, \
                    mock.patch.object(app, "log_startup", side_effect=RuntimeError("stop after the check")):
                with self.assertRaisesRegex(RuntimeError, "stop after the check"):
                    app.main(argv)
                self.assertTrue(harness.started)
                harness.ensure_started(harness.context)  # What the first job does.
            startup.assert_called_once()


class StoppingTest(unittest.TestCase):
    """main's stopping steps, with every step of its start replaced; ``fail`` names the one that raises."""

    ORDER = ["shutdown", "drain", "cancel_all", "stop_children", "join", "pid removed", "close",
             "receipts", "card_locks", "flush", "timing"]

    def stop(self, fail):
        app = UnifiedBridgeApp(script_path=REPO / "bin/covey_bridge.py")
        quiet(app)
        calls = []

        def step(name, result=None):
            def call(*args, **kwargs):
                calls.append(name)
                if name == fail:
                    raise RuntimeError(name)
                return result
            return call

        def configure(name):
            return lambda value: step(name)() if value == "previous" else "previous"

        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            pid = lambda: calls.append("pid removed") if not (state_dir / "bridge.pid").exists() else None
            app.runtime.shutdown = mock.Mock(set=step("shutdown"))
            app.card_lanes = mock.Mock(drain=step("drain"))
            scheduler = mock.Mock(join=step("join"))
            stubs = [mock.patch("covey.app.unified.threading.Thread"),
                     mock.patch("covey.app.unified.signal.signal"),
                     mock.patch("covey.app.unified.instance_lock",
                                return_value=mock.Mock(close=lambda: (pid(), step("close")()))),
                     mock.patch("covey.app.unified.configure_receipts", configure("receipts")),
                     mock.patch("covey.app.unified.configure_card_locks", configure("card_locks")),
                     mock.patch("covey.core.timing.configure", configure("timing")),
                     mock.patch("covey.core.timing.flush", step("flush")),
                     mock.patch("covey.core.migrate.migrate"),
                     mock.patch("covey.feishu.sdk.lark_cli_direct"),
                     mock.patch("covey.feishu.sdk.enabled", return_value=False),
                     mock.patch.object(app, "init_target"), mock.patch.object(app.harnesses[DEFAULT], "ensure_started"),
                     mock.patch.object(app, "log_startup"),
                     mock.patch("covey.app.unified.recover_question_delivery"),
                     mock.patch.object(app, "notify_stale_jobs", step("notify_stale_jobs")),
                     mock.patch("covey.app.unified.supervised", return_value=scheduler),
                     mock.patch.object(app, "consume_loop", step("consume")),
                     mock.patch.object(app.runtime.jobs, "cancel_all", step("cancel_all")),
                     mock.patch.object(app, "stop_children", step("stop_children"))]
            for stub in stubs:
                stub.start()
                self.addCleanup(stub.stop)
            try:
                app.main(["--state-dir", str(state_dir), "--workdir", tmp, "--profile", "unit"])
            except RuntimeError as exc:
                return calls, str(exc)
            return calls, None

    def test_a_finished_start_stops_in_order(self):
        self.assertEqual(self.stop(None), (["notify_stale_jobs", "consume", *self.ORDER], None))

    def test_a_start_that_fails_before_the_workers_does_not_join_them(self):
        order = [name for name in self.ORDER if name != "join"]
        self.assertEqual(self.stop("notify_stale_jobs"), (["notify_stale_jobs", *order], "notify_stale_jobs"))

    def test_a_step_that_raises_ends_the_stop_there(self):
        # The steps after it (the pid file, the instance lock, the settings) stay undone, as the finally block left them.
        for fail in ("drain", "stop_children", "close"):
            with self.subTest(fail=fail):
                order = self.ORDER[:self.ORDER.index(fail) + 1]
                self.assertEqual(self.stop(fail), (["notify_stale_jobs", "consume", *order], fail))


class WorkerTargetsTest(unittest.TestCase):
    @requires_extension()
    def test_each_optional_feature_adds_its_worker_only_when_on(self):
        always = ["scheduler", "cards", "menu", "maintenance", "connections", "availability", "outbox", "policy",
                  "memguard", "proxy", "proxy-in"]
        on = {"COVEY_DASHBOARD": "127.0.0.1:0", "COVEY_PASEO_FLEET": "/absolute/paseo.json",
              "COVEY_PINNED_OVERVIEW": "1"}
        paseo = ["paseo"] if extension_enabled("covey_paseo") else []  # The Paseo extension's worker, with its fleet set.
        # The butler's workers and menu key, unless the deployment rules it out (docs/butler.md 私聊模式).
        main, ruled_out = ["main-tools", "main-search"], {"COVEY_BUTLER": "0"}
        app_setup = ["app-setup"]  # The Feishu app's check (app/app_setup.py), on unless COVEY_APP_CHECK=0.
        # 工作区 and 脱敏 are tabs of 设置 since 2026-11-09; their old keys open it there.
        menu = ["weekly_usage", "proxy_settings", "manage_groups", "bridge_settings", "manage_workspaces",
                "redaction_dashboard"]
        for env, extra in (({}, main + app_setup), (on, ["dashboard", "overview", *main, *app_setup, *paseo]),
                           (ruled_out, app_setup)):
            with self.subTest(env=sorted(env)), mock.patch.dict(os.environ, env):
                for name in set(on) - set(env):
                    os.environ.pop(name, None)
                app = UnifiedBridgeApp(script_path=REPO / "bin/covey_bridge.py")
                self.assertEqual([name for name, _ in app.worker_targets()], always + extra)
                self.assertEqual(list(app.menu.actions), menu + ([] if env is ruled_out else ["main_thread_settings"]))


if __name__ == "__main__":
    unittest.main()
