import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

from support import REPO
from support.cards import text_of
from covey.cards.elements import find_element
from support.fake_backends import SID, RecordingBackend
from support.processes import leftover_check
from support.world import UnifiedCase, wait_for
from support.records import run_files
from covey.app.outbox import retry_label
from covey.app.questions import recover_question_delivery
from covey.app import detached, runner_ipc, runner_spawn
from covey.app import runner as task_runner
from covey.app.task_card import StreamingReply
from covey.core import background, run_dir as rd, trace_store
from covey.feishu.sdk import warm_sdk as sdk_warm
from covey.core.harness import JobCancelled, RunResult
from covey.core.jobs import SIGTERM_STATUS, BridgeReplyJob, find_job_record, recover_stale_jobs, upsert_job_record
from covey.core.result import ExecutionOutcome, VerifiedRootReply
from covey.core.task_processes import TASK_DIR_ENV
from covey.core.topics import remember_topic_message
from covey.service.manager import process_identity
from covey.app.runtime import Runtime

setUpModule, tearDownModule = leftover_check()


def snapshot(steps, **fields):
    return {"schema_version": 1, "steps": steps, "step_order": list(steps), "total": len(steps),
            "agents": {}, "children": {}, **fields}


@contextlib.contextmanager
def failing_reads(path, count):
    """The next ``count`` reads of ``path`` fail as a read that met a replace of the file did on ByteFuse (T740): the file
    not there, then cut short (its JSON does not parse), by turns. Yields the reads made of it, True for each that failed."""
    real, reads = Path.read_text, []

    def read_text(self, *args, **kwargs):
        if Path(self) != Path(path):
            return real(self, *args, **kwargs)
        reads.append(reads.count(True) < count)
        if not reads[-1]:
            return real(self, *args, **kwargs)
        if reads.count(True) % 2:
            raise FileNotFoundError(2, "No such file or directory", str(self))
        return real(self, *args, **kwargs)[:3]
    with mock.patch.object(Path, "read_text", read_text):
        yield reads


@contextlib.contextmanager
def slow_replaces(path, window):
    """A replace of ``path`` off the main thread takes ``window`` seconds, and a read of ``path`` meanwhile fails as one did
    on ByteFuse (T740). Yields the names of the threads that replaced ``path``, one entry a replace."""
    real_replace, real_read, replacing, made = Path.replace, Path.read_text, threading.Event(), []

    def replace(self, target):
        if Path(target) != Path(path):
            return real_replace(self, target)
        made.append(threading.current_thread().name)
        if threading.current_thread() is threading.main_thread():
            return real_replace(self, target)
        replacing.set()
        try:
            time.sleep(window)
            return real_replace(self, target)
        finally:
            replacing.clear()

    def read_text(self, *args, **kwargs):
        if Path(self) == Path(path) and replacing.is_set():
            raise FileNotFoundError(2, "No such file or directory", str(self))
        return real_read(self, *args, **kwargs)
    with mock.patch.object(Path, "replace", replace), mock.patch.object(Path, "read_text", read_text):
        yield made


class SteadyReadTests(unittest.TestCase):
    """runner.json read again when a read fails for a moment (core/run_dir.py read_steady, T740)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name) / "state"

    def test_a_read_of_runner_json_that_fails_for_a_moment_is_made_again(self):
        # T740 (ByteFuse): a read that met the runner's replace of runner.json failed, and the Bridge took the runner for
        # dead: its watch ended at once and a second runner was started for the same job.
        directory = rd.run_dir(self.state, "task-7")
        live = RunnerProcess(directory)
        self.addCleanup(live.stop)
        with failing_reads(directory / rd.RUNNER, 2) as reads:
            self.assertTrue(rd.runner_alive(directory))
        self.assertEqual(reads, [True, True, False])
        with failing_reads(directory / rd.RUNNER, 2):
            self.assertEqual((rd.runner_info(directory) or {}).get("pid"), live.process.pid)

    def test_a_run_directory_that_is_not_there_is_not_waited_for_nor_a_torn_file_for_ever(self):
        torn = run_files(self.state, "torn")
        (torn / rd.RUNNER).write_text("{")
        with mock.patch.object(rd.time, "sleep") as slept:
            self.assertFalse(rd.runner_alive(rd.run_dir(self.state, "none")))
            self.assertIsNone(rd.runner_info(rd.run_dir(self.state, "none")))
            slept.assert_not_called()  # A job about to run asks about a directory not made yet (prestart_runner).
            self.assertFalse(rd.runner_alive(torn))
        self.assertEqual([c.args[0] for c in slept.call_args_list], list(rd.READ_PAUSES))

    def test_wake_json_still_names_a_live_runner_whose_runner_json_cannot_be_read(self):
        directory = rd.run_dir(self.state, "task-7")
        live = RunnerProcess(directory)
        self.addCleanup(live.stop)
        identity = process_identity(live.process.pid)
        (directory / rd.RUNNER).write_text("{")
        self.assertFalse(rd.runner_alive(directory))
        self.assertFalse(rd.runner_here(directory))
        run_files(directory, wake=identity)
        self.assertTrue(rd.runner_here(directory))
        self.assertTrue(rd.runner_here(directory, patient=False))
        live.stop()
        self.assertFalse(rd.runner_here(directory))  # Its pid, start time and boot: never a later process.

    def test_a_look_at_the_saved_results_index_that_misses_it_for_a_moment_is_made_again(self):
        directory = run_files(self.state, "task-7")
        answers = iter([False, True])
        with mock.patch.object(detached, "run_began", side_effect=lambda state, key: next(answers)), \
                mock.patch.object(detached.time, "sleep"):
            self.assertFalse(detached.unstarted({"status": "queued", "key": "task-7"}, directory, self.state))
        with mock.patch.object(detached, "run_began", return_value=False), mock.patch.object(detached.time, "sleep"):
            self.assertTrue(detached.unstarted({"status": "queued", "key": "task-7"}, directory, self.state))


class TraceStoreTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.addCleanup(trace_store.forget, self.state, "job")

    def test_each_save_appends_only_what_changed_and_replays_to_the_snapshot(self):
        steps = {f"s{i}": {"name": "bash", "detail": "x" * 200, "status": "done", "started": i} for i in range(50)}
        trace_store.save_trace(self.state, "job", snapshot(dict(steps), ended_at=None))
        path = trace_store.trace_path(self.state, "job")
        first = path.stat().st_size
        steps["s49"] = {**steps["s49"], "status": "failed"}
        steps["s50"] = {"name": "read", "detail": "y", "status": "running", "started": 50}
        trace_store.save_trace(self.state, "job", snapshot(dict(steps), ended_at=None))
        self.assertLess(path.stat().st_size - first, 700)  # Two steps, not the whole record again.
        trace_store.save_trace(self.state, "job", snapshot(dict(steps), ended_at=None))
        self.assertEqual(len(path.read_text().splitlines()), 2)  # Nothing changed, nothing written.
        del steps["s0"]
        child = snapshot({"c1": {"name": "bash", "status": "done", "started": 1}})
        trace_store.save_trace(self.state, "job", snapshot(dict(steps), ended_at=9.0, children={"agent": child}))
        loaded = trace_store.load_trace(self.state, {"key": "job"})
        self.assertEqual(list(loaded["steps"]), list(steps))
        self.assertEqual(loaded["steps"]["s49"]["status"], "failed")
        self.assertEqual(loaded["step_order"], list(steps))
        self.assertEqual(loaded["ended_at"], 9.0)
        self.assertEqual(loaded["children"]["agent"]["steps"]["c1"]["status"], "done")

    def test_another_process_continues_the_file_and_a_torn_line_is_ignored(self):
        trace_store.save_trace(self.state, "job", snapshot({"a": {"status": "running"}}))
        trace_store.forget(self.state, "job")  # A new process: no memory of what was written.
        trace_store.save_trace(self.state, "job", snapshot({"a": {"status": "done"}, "b": {"status": "running"}}))
        path = trace_store.trace_path(self.state, "job")
        lines = path.read_text().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertNotIn("full", json.loads(lines[1]))
        with path.open("a") as handle:
            handle.write('{"v": 1, "set": [[["s", "c"')  # A crash mid-write.
        loaded = trace_store.load_trace(self.state, {"key": "job"})
        self.assertEqual({k: v["status"] for k, v in loaded["steps"].items()}, {"a": "done", "b": "running"})

    def test_a_file_much_larger_than_its_data_is_compacted(self):
        with mock.patch.object(trace_store, "COMPACT_MIN_BYTES", 1000):
            for i in range(40):
                trace_store.save_trace(self.state, "job", snapshot({"a": {"status": "running", "n": i, "pad": "p" * 60}}))
        path = trace_store.trace_path(self.state, "job")
        self.assertLess(len(path.read_text().splitlines()), 15)
        self.assertEqual(trace_store.load_trace(self.state, {"key": "job"})["steps"]["a"]["n"], 39)

    def test_a_record_from_before_the_file_is_read_from_the_file_its_migration_wrote(self):
        old = snapshot({"a": {"status": "done"}}, started_at=5)
        record = {"key": "old", "trace_snapshot": old}
        self.assertIsNone(trace_store.load_trace(self.state, record))  # Only the file is read (core/migrate.py).
        self.assertTrue(trace_store.save_embedded(self.state, "old", old))
        self.assertEqual(trace_store.load_trace(self.state, record), {k: v for k, v in old.items() if k != "children"})
        self.assertFalse(trace_store.save_embedded(self.state, "old", snapshot({"b": {}})))  # A file is never replaced.
        self.assertIsNone(trace_store.load_trace(self.state, {"key": "none"}))


class QueuedSaveTests(unittest.TestCase):
    def test_a_supplement_joined_to_a_waiting_job_survives_a_restart(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            job = BridgeReplyJob(job_id=5, user_text="x", chat_id="c", message_id="m5", timeout=0, prompt_text="x")
            detached.remember_queued(state, "claude", job)
            job.prompt_text += "\n\n用户在任务开始前补充：\nmore"
            detached.update_queued(state, job)
            saved = json.loads(detached.queued_path(state, "m5").read_text())
            self.assertEqual(saved["harness"], "claude")
            self.assertTrue(runner_spawn.load_job(saved["job"]).prompt_text.endswith("more"))
            detached.update_queued(state, BridgeReplyJob(job_id=6, user_text="y", chat_id="c", message_id="m6", timeout=0))
            self.assertFalse(detached.queued_path(state, "m6").exists())  # Only a job that is waiting.


class SharedLockTests(unittest.TestCase):
    def test_another_process_waits_for_a_record_lock_and_one_thread_may_reenter(self):
        import tempfile
        from covey.core.locks import resource_lock
        with tempfile.TemporaryDirectory() as tmp:
            record = Path(tmp) / "jobs" / "a.json"
            holder = subprocess.Popen([sys.executable, "-c", (
                "import sys; sys.path.insert(0, sys.argv[2])\n"
                "from covey.core.locks import resource_lock\n"
                "with resource_lock('record', sys.argv[1]):\n"
                "    print('held', flush=True); sys.stdin.readline()\n"), str(record), str(REPO)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            self.addCleanup(holder.wait)
            self.addCleanup(holder.stdout.close)
            self.addCleanup(holder.stdin.close)
            self.assertEqual(holder.stdout.readline().strip(), "held")
            acquired = threading.Event()

            def take():
                with resource_lock("record", record), resource_lock("record", record):
                    acquired.set()
            taker = threading.Thread(target=take)
            taker.start()
            self.assertFalse(acquired.wait(0.3))  # Held by the other process ...
            holder.stdin.write("release\n")
            holder.stdin.flush()
            self.assertTrue(acquired.wait(10))  # ... until it lets go; the same thread re-enters.
            taker.join(10)
            self.assertEqual(list((record.parent / ".locks").iterdir()), [record.parent / ".locks" / "a.json.lock"])


class ReloadTests(unittest.TestCase):
    def test_question_feedback_is_interpreted_by_the_current_bridge_before_forwarding(self):
        for mode in ("feedback", "feedback_submit"):
            with self.subTest(mode=mode):
                self.assertFalse(runner_ipc.forward_card_action(Runtime(script_path="unit"), None, {},
                    {"kind": "bridge_question", "mode": mode, "token": "a" * 16}))


    def test_a_click_on_a_job_this_bridge_runs_goes_to_its_runner_without_reading_the_records(self):
        runtime = Runtime(script_path="unit")
        job = BridgeReplyJob(job_id=7, user_text="hi", chat_id="chat", message_id="m7", timeout=0, model="m",
                             effort="high", workdir=Path("/w"), history_path=Path("/h/history.jsonl"), topic_id="t")
        runtime.jobs.register(job)
        with tempfile.TemporaryDirectory() as tmp:
            job.detached_run_dir = Path(tmp)
            target = SimpleNamespace(state_dir=Path(tmp), topic_id="t")
            # A job this Bridge watches: runner_here says whether its runner is there (runner.json, wake.json, run lock).
            with mock.patch.object(runner_ipc, "here", return_value=True), \
                    mock.patch.object(runner_ipc, "alive", side_effect=AssertionError("runner.json alone asked")), \
                    mock.patch.object(runner_ipc, "write_inbox") as inbox, \
                    mock.patch.object(runner_ipc, "directory_store", side_effect=AssertionError("records read")):
                self.assertTrue(runner_ipc.forward_card_action(runtime, target, {"chat_id": "chat", "event_id": "e"},
                                                               {"kind": "bridge_job_stop", "job_id": 7}))
            inbox.assert_called_once_with(Path(tmp), {"kind": "card_event", "event": {"chat_id": "chat", "event_id": "e"}})
            # Another topic's or chat's click on that number, or a runner not alive, reads the records as before.
            for event, target_topic, is_alive in (({"chat_id": "other"}, "t", True), ({"chat_id": "chat"}, "u", True),
                                                  ({"chat_id": "chat"}, "t", False)):
                with mock.patch.object(runner_ipc, "here", return_value=is_alive), \
                        mock.patch.object(runner_ipc, "alive", return_value=is_alive), \
                        mock.patch.object(runner_ipc, "directory_store") as store:
                    store.return_value.query.return_value = []
                    self.assertFalse(runner_ipc.forward_card_action(
                        runtime, SimpleNamespace(state_dir=Path(tmp), topic_id=target_topic), event,
                        {"kind": "bridge_job_stop", "job_id": 7}))
                    store.return_value.query.assert_called_once()


class HandoffTests(unittest.TestCase):
    def test_a_job_survives_the_handoff(self):
        job = BridgeReplyJob(job_id=7, user_text="hi", chat_id="chat", message_id="m7", timeout=0, model="m",
                             effort="high", workdir=Path("/w"), history_path=Path("/h/history.jsonl"),
                             topic_id="t", history_snapshot=[{"role": "user", "content": "x"}],
                             timings={"queued": 1.0}, harness_id="claude", binding_revision=3)
        job.progress = StreamingReply(card_id="cid", message_id="cm", sequence=4, live_details=True,
                                      details_text="任务编号：#7")
        job.progress.streaming_mode = False
        job.topic_label = "T3"
        job.harness_capabilities = object()  # Not portable: the runner recomputes it.
        job.cancel_event.set()
        data = json.loads(json.dumps(runner_spawn.dump_job(job)))
        copy = runner_spawn.load_job(data)
        self.assertEqual((copy.job_id, copy.workdir, copy.history_path, copy.harness_id, copy.binding_revision),
                         (7, Path("/w"), Path("/h/history.jsonl"), "claude", 3))
        self.assertEqual((copy.progress.card_id, copy.progress.sequence, copy.progress.streaming_mode), ("cid", 4, False))
        # The runner goes on writing 任务详情's text alone on the card the bridge sent.
        self.assertEqual((copy.progress.live_details, copy.progress.details_text), (True, "任务编号：#7"))
        self.assertEqual((copy.topic_label, copy.history_snapshot, copy.timings), ("T3", job.history_snapshot, {"queued": 1.0}))
        self.assertTrue(copy.cancel_event.is_set())
        self.assertFalse(copy.execution_started)
        self.assertIsNone(copy.harness_capabilities)  # Not JSON: not carried.

    def test_declared_job_state_keeps_the_handoff_format_of_older_releases(self):
        """State that used to be set ad hoc travels in "extras", only once set: an older runner
        drops unknown "fields" but sets every extra, and an older bridge's extras fill the fields."""
        from dataclasses import fields
        job = BridgeReplyJob(job_id=8, user_text="hi", chat_id="chat", message_id="m8", timeout=0)
        state = {f.name for f in fields(job) if f.metadata.get("extra")}
        fresh = runner_spawn.dump_job(job)
        self.assertEqual(fresh["extras"], {})
        self.assertFalse(state & set(fresh["fields"]))
        job.bridge_run_id, job.checkpoint_question_deferred, job.held_steers["mid"] = "run-8", True, "later"
        job.legacy_marker = "kept"  # Still undeclared: carried as before.
        data = json.loads(json.dumps(runner_spawn.dump_job(job)))
        self.assertEqual(data["extras"], {"bridge_run_id": "run-8", "checkpoint_question_deferred": True,
                                          "held_steers": {"mid": "later"}, "legacy_marker": "kept"})
        self.assertFalse(state & set(data["fields"]))
        copy = runner_spawn.load_job(data)
        self.assertEqual((copy.bridge_run_id, copy.checkpoint_question_deferred, copy.held_steers, copy.legacy_marker),
                         ("run-8", True, {"mid": "later"}, "kept"))
        self.assertIsNone(copy.verified_result_key)

    def test_job_state_no_longer_declared_still_crosses_the_handoff(self):
        """trace_page and trace_paginated were declared job state until 2026-11-05: a handoff that
        carries them loads, and they go on in "extras", so a runner of either release reads them."""
        data = runner_spawn.dump_job(BridgeReplyJob(job_id=9, user_text="hi", chat_id="chat", message_id="m9", timeout=0))
        data["extras"].update(trace_page=2, trace_paginated=True)
        copy = runner_spawn.load_job(json.loads(json.dumps(data)))
        self.assertEqual((copy.trace_page, copy.trace_paginated), (2, True))
        self.assertEqual(runner_spawn.dump_job(copy)["extras"], {"trace_page": 2, "trace_paginated": True})


class RunnerEnvTests(unittest.TestCase):
    def test_a_runner_starts_in_a_cwd_it_cannot_read_and_its_task_keeps_the_pythonpath(self):
        bridge_path = os.pathsep + "/opt/toolbox"  # An empty entry, as the service environment has.
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(os.environ, {"PYTHONPATH": bridge_path}):
            env = runner_spawn.runner_env()
            self.assertEqual((env["PYTHONPATH"], env[runner_spawn.RUNNER_PYTHONPATH]), ("/opt/toolbox", bridge_path))
            gone = Path(root) / "gone"
            gone.mkdir()
            # Python starts in a cwd that no longer exists, as when the NAS cannot resolve it.
            started = subprocess.run(["sh", "-c", 'cd "$1" && rmdir "$1" && exec "$2" -B -c "print(\'started\')"',
                                      "sh", str(gone), sys.executable], env=env, capture_output=True, text=True,
                                     timeout=30)
            self.assertEqual(started.stdout.strip(), "started", started.stderr)
            # The runner puts the bridge's PYTHONPATH back first, for the commands its task runs.
            with mock.patch.dict(os.environ, env, clear=True):
                with self.assertRaises(OSError):
                    task_runner.runner_main(["--run-dir", root])  # No handoff here: it stops right after.
                self.assertEqual(os.environ["PYTHONPATH"], bridge_path)
                self.assertNotIn(runner_spawn.RUNNER_PYTHONPATH, os.environ)
        with mock.patch.dict(os.environ, {"PYTHONPATH": "/opt/a" + os.pathsep + "/opt/b"}):
            self.assertNotIn(runner_spawn.RUNNER_PYTHONPATH, runner_spawn.runner_env())

    def test_a_runner_marks_what_its_task_starts_with_its_run_directory(self):
        # The Harness and its commands inherit it, wherever they end up (core/task_processes.py).
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(os.environ, {TASK_DIR_ENV: "/elsewhere"}):
            with self.assertRaises(OSError):
                task_runner.runner_main(["--run-dir", root])  # No handoff here: it stops right after.
            self.assertEqual(os.environ[TASK_DIR_ENV], str(Path(root).absolute()))

    def test_a_runner_starts_in_its_run_directory_with_every_bridge_path_absolute(self):
        with tempfile.TemporaryDirectory() as root:
            state, work = Path(root) / "state", Path(root) / "work"
            runtime = SimpleNamespace(owner_open_id="", script_path=REPO / "bin" / "covey_bridge.py",
                                      cli_argv=["--harness-config", "harnesses.json", "--pairing-file=pair.json",
                                                "--profile", "p"])
            job = BridgeReplyJob(job_id=7, user_text="hi", chat_id="chat", message_id="m7", timeout=0)
            with mock.patch.object(runner_spawn.subprocess, "Popen", return_value=mock.Mock(pid=os.getpid())) as popen:
                directory = runner_spawn.start_runner(runtime, "codex", job, SimpleNamespace(state_dir=state,
                                                                                        default_workdir=work))
            self.assertEqual(popen.call_args.kwargs["cwd"], directory)
            # Relative paths are the bridge's, and the runner is told the directories the bridge resolved.
            here = Path.cwd()
            self.assertEqual(json.loads((directory / rd.HANDOFF).read_text())["argv"],
                             ["--harness-config", str(here / "harnesses.json"), "--pairing-file=" + str(here / "pair.json"),
                              "--profile", "p", "--state-dir", str(state), "--workdir", str(work)])


class PrestartTests(unittest.TestCase):
    """prestart_runner: a job about to run gets its runner while its card and record are written (S35)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state, self.logs = Path(tmp.name) / "state", []
        self.runtime = SimpleNamespace(owner_open_id="", script_path=REPO / "bin" / "covey_bridge.py",
                                       cli_argv=["--profile", "p"], detached=lambda: True, log=self.logs.append)
        self.ctx = SimpleNamespace(state_dir=self.state, default_workdir=Path(tmp.name))
        self.job = BridgeReplyJob(job_id=7, user_text="hi", chat_id="chat", message_id="m7", timeout=0)

    def popen(self, *exits):
        """Popen standing in for runners of this process's pid; ``exits``: each one's poll()."""
        made = [mock.Mock(pid=os.getpid(), **{"poll.return_value": code}) for code in exits]
        return mock.patch.object(runner_spawn.subprocess, "Popen", side_effect=made)

    def test_the_prestarted_runner_is_handed_the_job_and_named_in_its_handoff(self):
        with self.popen(None) as popen:
            runner_spawn.prestart_runner(self.runtime, self.job, self.ctx)
            command = popen.call_args.args[0]
            self.assertEqual(command[-2], "--await-handoff")
            nonce = command[-1]
            self.assertNotIn("early_runner", json.dumps(runner_spawn.dump_job(self.job)))  # Never saved.
            directory = runner_spawn.start_runner(self.runtime, "codex", self.job, self.ctx)
            self.assertEqual(popen.call_count, 1)  # No second runner.
        handoff = json.loads((directory / rd.HANDOFF).read_text())
        self.assertEqual(handoff["prestart"], nonce)
        self.assertEqual(json.loads((directory / rd.RUNNER).read_text())["pid"], os.getpid())
        self.assertEqual(runner_spawn.await_handoff(directory, nonce, wait=0), handoff)
        self.assertIsNone(runner_spawn.await_handoff(directory, "another", wait=0))  # Not its job.
        self.assertIsNone(self.job.early_runner)

    def test_a_prestarted_runner_that_ended_or_is_about_to_give_up_is_not_used(self):
        for number, (exits, age) in enumerate((((1, None), 0), ((None, None), runner_spawn.PRESTART_USE + 1))):
            job = BridgeReplyJob(job_id=number, user_text="hi", chat_id="chat", message_id=f"m{number}", timeout=0)
            with self.subTest(exits=exits, age=age), self.popen(*exits) as popen:
                runner_spawn.prestart_runner(self.runtime, job, self.ctx)
                job.early_runner.started -= age
                directory = runner_spawn.start_runner(self.runtime, "codex", job, self.ctx)
                self.assertEqual(popen.call_count, 2)
                self.assertNotIn("--await-handoff", popen.call_args.args[0])
                self.assertNotIn("prestart", json.loads((directory / rd.HANDOFF).read_text()))

    def test_no_prestart_beside_a_live_runner_or_outside_the_live_bridge(self):
        directory = rd.run_dir(self.state, "m7")
        live = RunnerProcess(directory)
        self.addCleanup(live.stop)
        with self.popen(None) as popen:
            runner_spawn.prestart_runner(self.runtime, self.job, self.ctx)
            self.runtime.detached = lambda: False
            runner_spawn.prestart_runner(self.runtime, self.job, self.ctx)
        popen.assert_not_called()
        with mock.patch.object(runner_spawn.subprocess, "Popen", side_effect=OSError(24, "too many")):
            self.runtime.detached = lambda: True
            live.stop()
            runner_spawn.prestart_runner(self.runtime, self.job, self.ctx)  # Never the submission's failure.
        self.assertEqual((self.job.early_runner, self.logs), (None, ["task runner prestart unavailable: OSError"]))

    def test_start_runner_never_clears_a_directory_whose_run_lock_a_runner_holds(self):
        # T814: neither runner.json nor wake.json named the live runner (not readable, or naming another process), and
        # start_runner cleared its directory and started a second runner of the job.
        directory = run_files(self.state, "m7", folders=(rd.INBOX,), handoff={"its": "own"})
        (directory / rd.INBOX / "1-a.json").write_text("{}")
        holder = LockHolder(directory)
        self.addCleanup(holder.stop)
        with self.popen(None) as popen, self.assertRaises(runner_spawn.RunnerAlive):
            runner_spawn.start_runner(self.runtime, "codex", self.job, self.ctx)
        popen.assert_not_called()
        self.assertEqual(json.loads((directory / rd.HANDOFF).read_text()), {"its": "own"})
        self.assertTrue((directory / rd.INBOX / "1-a.json").exists())

    def test_a_runner_catches_up_with_the_job_and_question_records_on_a_thread_from_its_start(self):
        # S34: its first query read 927 job records from NAS (2.3 s), before the backend started; S46: it
        # starts from the Bridge's .index instead (core/store.py), reading only what it later asks for.
        queried = []
        store = lambda folder: SimpleNamespace(catch_up=lambda: queried.append((threading.current_thread().name, folder.name)))
        with mock.patch.object(task_runner, "directory_store", store):
            task_runner.warm_records(self.state).join(5)
        self.assertEqual(queried, [(background.RECORDS, "jobs"), (background.RECORDS, "questions")])

    def test_a_prestarted_runner_without_a_handoff_for_it_leaves_having_run_nothing(self):
        directory = run_files(self.state, "m7", handoff={"prestart": "someone-else", "job": {}})
        with mock.patch.object(runner_spawn, "PRESTART_WAIT", 0.1):
            self.assertEqual(task_runner.runner_main(["--run-dir", str(directory), "--await-handoff", "mine"]), 0)
        self.assertEqual(sorted(p.name for p in directory.iterdir()), [rd.HANDOFF])

    def written(self, fail=None):
        """runner_spawn's write_json, recording each file's name, failing the one named ``fail``."""
        real, names = runner_spawn.write_json, []

        def write(path, value, **dumps):
            names.append(Path(path).name)
            if Path(path).name == fail:
                raise OSError(5, "Input/output error")
            return real(path, value, **dumps)
        return mock.patch.object(runner_spawn, "write_json", write), names

    def test_the_bridge_names_a_prestarted_runner_before_its_handoff(self):
        # T740: written after the handoff, runner.json was replaced at once by the Bridge and by the runner taking the
        # handoff; on ByteFuse the Bridge's read of it then failed, and it took the runner for dead.
        writes, names = self.written()
        with self.popen(None), writes:
            runner_spawn.prestart_runner(self.runtime, self.job, self.ctx)
            directory = runner_spawn.start_runner(self.runtime, "codex", self.job, self.ctx)
        self.assertEqual([name for name in names if name in (rd.RUNNER, rd.HANDOFF)], [rd.RUNNER, rd.HANDOFF])
        # What the runner finds on taking its handoff: itself, so it does not write the file again (app/runner.py).
        self.assertTrue(runner_spawn.registered(directory, os.getpid(), REPO, 7, "m7", "codex"))
        self.assertFalse(runner_spawn.registered(directory, os.getpid(), REPO, 8, "m7", "codex"))  # Another job's.

    def test_a_handoff_the_bridge_could_not_write_ends_the_prestarted_runner_and_its_record(self):
        writes, _ = self.written(fail=rd.HANDOFF)
        with self.popen(None), writes, mock.patch.object(runner_spawn, "_abandon") as abandon:
            runner_spawn.prestart_runner(self.runtime, self.job, self.ctx)
            early = self.job.early_runner
            with self.assertRaises(OSError):
                runner_spawn.start_runner(self.runtime, "codex", self.job, self.ctx)
        abandon.assert_called_once_with(early.process)  # The Bridge reports the job not started: no runner may take it.
        self.assertFalse((rd.run_dir(self.state, "m7") / rd.RUNNER).exists())

    def test_a_live_runner_in_the_run_directory_is_left_alone(self):
        # T740: relaunching, start_runner cleared runner.json and the inbox of a directory whose runner was alive, and
        # handed its job to a second runner.
        directory = rd.run_dir(self.state, "m7")
        live = RunnerProcess(directory, key="m7")
        self.addCleanup(live.stop)
        run_files(directory, handoff={"its": "own"})
        rd.post(directory / rd.INBOX, {"kind": "steer", "text": "for the live one"})
        before = {name: (directory / name).read_text() for name in (rd.RUNNER, rd.HANDOFF)}
        raised = None
        with self.popen(None) as popen:
            try:
                runner_spawn.start_runner(self.runtime, "codex", self.job, self.ctx)
            except Exception as exc:  # noqa: BLE001 - which one is asserted below
                raised = exc
        popen.assert_not_called()
        self.assertEqual({name: (directory / name).read_text() for name in before}, before)
        self.assertEqual(len(list((directory / rd.INBOX).glob("*.json"))), 1)
        self.assertEqual((type(raised).__name__, raised.info["pid"]), ("RunnerAlive", live.process.pid))

    def test_a_prestarted_runner_a_stopped_bridge_left_waiting_does_not_hold_up_its_job(self):
        # Its Bridge named it in runner.json and stopped before writing its handoff: it runs nothing and leaves in
        # PRESTART_WAIT, so the job's next start does not wait for it.
        directory = rd.run_dir(self.state, "m7")
        waiting = RunnerProcess(directory, key="m7")
        self.addCleanup(waiting.stop)
        with self.popen(None) as popen:
            runner_spawn.start_runner(self.runtime, "codex", self.job, self.ctx)
        popen.assert_called_once()
        self.assertNotIn("prestart", json.loads((directory / rd.HANDOFF).read_text()))  # Not the one it waits for.
        self.assertEqual(json.loads((directory / rd.RUNNER).read_text())["pid"], os.getpid())


@unittest.skipUnless(hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"),
                     "no pidfd here (macOS, or a Python built without it): the runner looks every 0.3 s instead")
class WakeTests(unittest.TestCase):
    """A bridge's inbox write wakes its runner at once (S37: it looked every 0.3 s, 0.25 s a click)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = run_files(Path(tmp.name), folders=[rd.INBOX])
        import signal
        self.addCleanup(signal.signal, runner_ipc.WAKE_SIGNAL, signal.getsignal(runner_ipc.WAKE_SIGNAL))

    def test_an_inbox_write_wakes_a_runner_that_takes_the_signal(self):
        import select
        if threading.current_thread() is not threading.main_thread():
            self.skipTest("signal handlers are set on the main thread")
        fd = runner_ipc.accept_wake(self.directory)
        self.addCleanup(runner_ipc.release_wake, fd)
        self.assertIsNotNone(fd)
        self.assertEqual(json.loads((self.directory / rd.WAKE).read_text())["pid"], os.getpid())
        runner_ipc.write_inbox(self.directory, {"kind": "card_event"})
        self.assertTrue(select.select([fd], [], [], 5)[0])
        runner_ipc.await_wake(fd, 0)  # What woke it is read away.
        self.assertFalse(select.select([fd], [], [], 0)[0])

    def test_only_the_process_its_wake_file_names_alive_is_signalled(self):
        self.assertFalse(runner_ipc.wake_runner(self.directory))  # No wake.json: an older release's runner.
        run_files(self.directory, wake={"pid": os.getpid(), "start_ticks": "0", "boot_id": "x"})
        with mock.patch.object(runner_ipc.signal, "pidfd_send_signal") as send:
            self.assertFalse(runner_ipc.wake_runner(self.directory))  # Another process had that pid.
        send.assert_not_called()

    def test_a_runner_off_the_main_thread_polls_instead(self):
        result = []
        thread = threading.Thread(target=lambda: result.append(runner_ipc.accept_wake(self.directory)))
        thread.start()
        thread.join(5)
        self.assertEqual(result, [None])
        self.assertFalse((self.directory / rd.WAKE).exists())



class RunnerFinishTests(unittest.TestCase):
    def test_a_supplement_held_at_the_end_is_settled_before_exit_json(self):
        # ACCEPT-344-A L7: one held as the job left the registry died with the runner; settled before exit.json,
        # the Bridge's finish_detached queues the butler's.
        order = []
        app, job = mock.Mock(), mock.Mock(interrupted=False)
        app.steering.settle_held_at_end.side_effect = lambda ctx, held: order.append("settle")
        setup = task_runner._Setup(mock.Mock(), app, job, mock.Mock(), None, None, None)
        served = threading.Thread(target=lambda: None)
        served.start()
        served.join()
        inbox = task_runner._Inbox(set(threading.enumerate()), threading.Event(), served, None)
        with mock.patch.object(task_runner, "mark_done", side_effect=lambda *a: order.append("done")), \
                mock.patch.object(task_runner, "mark_exit", side_effect=lambda *a: order.append("exit")), \
                mock.patch.object(task_runner.runner_ipc, "take_inbox", side_effect=lambda *a: order.append("inbox")), \
                mock.patch.object(task_runner.runner_ipc, "release_wake"), \
                mock.patch.object(task_runner, "context_for_job", return_value="ctx"), \
                mock.patch.object(task_runner, "configure_card_locks"), \
                mock.patch.object(task_runner.timing, "end"), mock.patch.object(task_runner.timing, "flush"), \
                mock.patch.object(task_runner.timing, "configure"):
            task_runner._finish(setup, Path("/nonexistent"), inbox, "finished")
        self.assertEqual(order, ["done", "inbox", "settle", "exit"])
        app.steering.settle_held_at_end.assert_called_once_with("ctx", job)


class LockHolder:
    """Another process that holds ``directory``'s run lock as the task runner running its job does (core/run_dir.py
    hold_run_lock: flock LOCK_EX on runner.lock, opened as core/file_lock.py open_lock opens it), written out here from
    the protocol, not by calling it; its command line names the directory, as a runner's does."""

    def __init__(self, directory):
        code = ("import fcntl, sys, time; from pathlib import Path; from covey.core.file_lock import open_lock; "
                "fd = open_lock(Path(sys.argv[1]) / 'runner.lock'); fcntl.flock(fd, fcntl.LOCK_EX); "
                "print(True, flush=True); time.sleep(120)")
        self.process = subprocess.Popen([sys.executable, "-B", "-c", code, str(directory)], stdout=subprocess.PIPE,
                                        text=True, cwd=REPO, start_new_session=True,
                                        env={**os.environ, "PYTHONPATH": str(REPO)})
        if self.process.stdout.readline().strip() != "True":
            self.stop()
            raise AssertionError("the stand-in runner did not take the run lock")

    def stop(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait()
        self.process.stdout.close()


class RunLockTests(unittest.TestCase):
    """The run lock (T814): one task runner of a job runs it, whatever the Bridge read, or failed to read, of its files."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = run_files(Path(tmp.name) / "state", "m7")
        self.addCleanup(rd.release_run_lock, self.directory)

    def holder(self):
        holder = LockHolder(self.directory)
        self.addCleanup(holder.stop)
        return holder

    def test_a_second_runner_of_the_job_is_turned_away_and_a_dead_runners_lock_holds_up_nothing(self):
        holder = self.holder()
        self.assertTrue(rd.run_lock_held(self.directory))
        started = time.monotonic()
        self.assertIs(rd.hold_run_lock(self.directory, wait=0.3), False)
        self.assertGreaterEqual(time.monotonic() - started, 0.3)  # It tried the whole wait, not once.
        self.assertNotIn(str(self.directory), rd._held)
        holder.process.send_signal(signal.SIGKILL)  # However a runner ends, the OS lets go of its lock.
        holder.process.wait()
        self.assertFalse(rd.run_lock_held(self.directory))
        self.assertIs(rd.hold_run_lock(self.directory, wait=0), True)
        # Its holder asked about its own lock says yes without opening the file again (on NFS that would let go of it).
        with mock.patch.object(rd, "open_lock", side_effect=AssertionError("opened")):
            self.assertTrue(rd.run_lock_held(self.directory))
        rd.release_run_lock(self.directory)
        self.assertFalse(rd.run_lock_held(self.directory))

    def test_the_bridges_look_makes_no_lock_file_and_never_turns_a_starting_runner_away(self):
        self.assertFalse(rd.run_lock_held(self.directory))
        self.assertFalse((self.directory / rd.RUN_LOCK).exists())  # A directory no runner with the lock started in.
        self.assertIs(rd.hold_run_lock(self.directory, wait=0), True)
        rd.release_run_lock(self.directory)
        stop, looks = threading.Event(), []

        def look():
            while not stop.is_set():
                looks.append(rd.run_lock_held(self.directory))
                time.sleep(0.001)
        looker = threading.Thread(target=look, name="bridge-looks")
        looker.start()
        try:
            wait_for(self, lambda: len(looks) > 20)
            self.assertIs(rd.hold_run_lock(self.directory, wait=2), True)  # The looks hold the lock a moment only.
            wait_for(self, lambda: looks[-1] is True)
        finally:
            stop.set()
            looker.join(5)
        self.assertIn(False, looks)

    def test_runner_here_counts_a_held_run_lock_when_neither_file_names_a_live_runner(self):
        run_files(self.directory, runner={"pid": 999999999, "job_id": 7, "key": "m7"})  # Named there, and gone.
        self.assertFalse(rd.runner_here(self.directory))
        holder = self.holder()
        self.assertFalse(rd.runner_alive(self.directory))  # runner.json alone says gone ...
        self.assertTrue(rd.runner_here(self.directory))  # ... its run lock says otherwise.
        self.assertTrue(rd.runner_here(self.directory, patient=False))
        self.assertEqual(rd.live_dirs(self.directory.parents[1], rd.runner_here), [self.directory])
        self.assertEqual(rd.live_dirs(self.directory.parents[1]), [])
        holder.stop()
        self.assertFalse(rd.runner_here(self.directory))


class RunnerProcess:
    """A stand-in runner: a live process whose command line names the run directory."""

    def __init__(self, directory, key="task-7"):
        run_files(directory, folders=(rd.INBOX, rd.ACKS))
        self.process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", str(directory)],
                                        start_new_session=True)
        identity = process_identity(self.process.pid)
        run_files(directory, runner={**identity, "job_id": 7, "key": key})

    def stop(self):
        self.process.kill()
        self.process.wait()


class BridgeSideTests(UnifiedCase):

    def running_job(self, *, runner=True, key="task-7", job_id=7, status="running"):
        app, ctx = self.route("task")
        upsert_job_record(ctx.state_dir, key, job_id=job_id, topic_id=ctx.topic_id, chat_id="chat", message_id=key,
                          status=status, backend=app.spec.name, kind="chat", card_message_id="card-7",
                          card_id="cid-7", reply_to_message_id="task", workdir=str(self.work),
                          history_path=str(ctx.history_path), session_path=str(ctx.session_path))
        remember_topic_message(ctx.state_dir, ctx.topic_id, "chat", "card-7")
        directory = rd.run_dir(ctx.state_dir, key)
        if runner:
            stub = RunnerProcess(directory, key)
            self.addCleanup(stub.stop)
            job = BridgeReplyJob(job_id=job_id, user_text="hi", chat_id="chat", message_id=key, timeout=0,
                                 topic_id=ctx.topic_id, harness_id=app.spec.name, reply_to_message_id="task")
            run_files(directory, handoff={"harness": app.spec.name, "job": runner_spawn.dump_job(job)})
            return app, ctx, directory, stub
        return app, ctx, directory, None

    def click(self, **value):
        self.app.handle_card_action_event({"operator_id": "owner", "chat_id": "chat", "message_id": "card-7",
                                           "event_id": "e-" + str(time.time_ns()), "action_value": value}, self.ctx)

    def test_a_runner_a_sigterm_ended_continues_by_itself_once(self):
        # 2026-11-02 22:09: a container's restart SIGTERMed every task runner; the Bridge may see one end first.
        app, ctx, directory, _ = self.running_job(runner=False)
        upsert_job_record(ctx.state_dir, "task-7", status_message=SIGTERM_STATUS)
        job = BridgeReplyJob(job_id=7, user_text="hi", chat_id="chat", message_id="task-7", timeout=0,
                             topic_id=ctx.topic_id, harness_id=app.spec.name, reply_to_message_id="task")
        with mock.patch.object(type(app.controls), "show_recovery_card", return_value=True) as recovery:
            detached.finish_detached(self.app, app, job, ctx, directory)
        record = find_job_record(ctx.state_dir, "task-7")
        self.assertEqual((record["status"], record["interrupted_by"], record["auto_retry_state"], record["auto_retry_kind"]),
                         ("interrupted", "sigterm", "pending", "restarted"))
        self.assertEqual(recovery.call_args.args[1]["auto_retry_state"], "pending")

    def test_a_runner_gone_mid_turn_has_its_stop_relayed_and_its_pending_relay_taken(self):
        # W1-F step 6 (T891 B4): a turn the butler handed on whose runner was killed left the top level silent; the Bridge
        # relays its 停住了 once nothing goes on by itself, and takes a 做完了 the runner left waiting for its chat.
        app, ctx, directory, _ = self.running_job(runner=False)
        job = BridgeReplyJob(job_id=7, user_text="hi", chat_id="chat", message_id="task-7", timeout=0,
                             topic_id=ctx.topic_id, harness_id=app.spec.name, reply_to_message_id="task")
        with mock.patch.object(type(app.controls), "show_recovery_card", return_value=True), \
                mock.patch.object(type(app.controls), "relay_unfinished") as relayed, \
                mock.patch.object(type(app.controls), "take_left_relay") as taken:
            detached.finish_detached(self.app, app, job, ctx, directory)
        self.assertEqual(find_job_record(ctx.state_dir, "task-7")["status"], "interrupted")
        self.assertEqual([call.args[1] for call in relayed.call_args_list], ["task-7"])
        self.assertEqual([call.args[1].message_id for call in taken.call_args_list], ["task-7"])

    def test_a_restart_relays_the_stop_of_a_turn_it_left_with_nothing_to_go_on(self):
        app, ctx, directory, _ = self.running_job(runner=False)
        with mock.patch.object(type(app.controls), "show_recovery_card", return_value=True), \
                mock.patch.object(type(app.controls), "relay_unfinished") as relayed:
            self.app.notify_stale_jobs(self.ctx)
        self.assertIn("task-7", [call.args[1] for call in relayed.call_args_list])

    def test_a_click_on_a_job_with_a_live_runner_goes_to_its_inbox(self):
        app, ctx, directory, _ = self.running_job()
        with mock.patch.object(type(app.controls), "cancel_reply_job_from_card") as local:
            self.click(kind="bridge_job_stop", job_id=7)
            self.click(kind="bridge_trace", topic_id=ctx.topic_id, job_id=7, verb="latest")
        local.assert_not_called()
        sent = [json.loads(p.read_text()) for p in sorted((directory / rd.INBOX).glob("*.json"))]
        self.assertEqual([s["event"]["action_value"]["kind"] for s in sent], ["bridge_job_stop", "bridge_trace"])
        self.assertTrue(all(s["kind"] == "card_event" for s in sent))
        self.assertEqual(app.messenger.send_message.call_count, 0)

    def test_without_a_live_runner_the_bridge_handles_the_click(self):
        app, ctx, directory, _ = self.running_job(runner=False)
        self.click(kind="bridge_job_stop", job_id=7)
        self.assertFalse((directory / rd.INBOX).exists())
        self.assertIn("当前不在运行或队列中", str(app.messenger.send_message.call_args))

    def test_a_restart_adopts_live_runners_and_interrupts_only_the_rest(self):
        app, ctx, directory, stub = self.running_job()
        upsert_job_record(ctx.state_dir, "orphan", job_id=8, topic_id=ctx.topic_id, chat_id="chat", message_id="orphan",
                          status="running", backend=app.spec.name, kind="chat")
        with mock.patch.object(type(app.controls), "show_recovery_card", return_value=True):
            self.app.notify_stale_jobs(self.ctx)
        self.assertEqual(find_job_record(ctx.state_dir, "task-7")["status"], "running")
        orphan = find_job_record(ctx.state_dir, "orphan")
        # A restart left it unfinished: 检查并继续 once by itself, a minute later (app/auto_retry.py RESTARTED).
        self.assertEqual((orphan["status"], orphan["interrupted_by"], orphan["auto_retry_state"], orphan["auto_retry_kind"]),
                         ("interrupted", "restart", "pending", "restarted"))
        adopted = self.app.runtime.jobs.queue.get_nowait()
        self.assertEqual((adopted.job_id, adopted.detached_run_dir), (7, directory))
        self.assertIs(self.app.runtime.jobs.find(7), adopted)
        # Watching it keeps the topic busy until the runner ends; then a job left running is interrupted.
        shutdown = self.fast_polling()
        watcher = threading.Thread(target=self.app.process_reply_job, args=(adopted, self.ctx), name="runner-watch")
        with mock.patch.object(type(app.controls), "show_recovery_card", return_value=True) as recovery:
            watcher.start()
            self.assertTrue(shutdown.wait_rounds("runner-watch", 3))  # Rounds that found the runner alive ...
            self.assertTrue(watcher.is_alive())  # ... kept watching.
            self.click(kind="bridge_job_stop", job_id=7)  # Forwarded while alive...
            stub.stop()
            watcher.join(10)
        self.assertFalse(watcher.is_alive())
        record = find_job_record(ctx.state_dir, "task-7")
        self.assertEqual(record["status"], "interrupted")
        self.assertNotIn("auto_retry_state", record)  # Its runner just ended, no restart or SIGTERM: the user's call.
        recovery.assert_called_once()
        self.assertIsNone(self.app.runtime.jobs.find(7))
        # ...and a click the runner never took is handled by the bridge once the runner is gone.
        self.assertEqual(list((directory / rd.INBOX).glob("*.json")), [])
        self.assertIn("当前不在运行或队列中", str(app.messenger.send_message.call_args))

    def unstarted_job(self):
        app, ctx, directory, _ = self.running_job(runner=False, status="queued")
        upsert_job_record(ctx.state_dir, "task-7", card_sequence=5)
        job = BridgeReplyJob(job_id=7, user_text="hi", chat_id="chat", message_id="task-7", timeout=0,
                             topic_id=ctx.topic_id, harness_id=app.spec.name, reply_to_message_id="task")
        job.progress = StreamingReply(card_id="cid-7", message_id="card-7", sequence=3)
        app.runtime.jobs.register(job)
        return app, ctx, job

    def launches(self, app, ctx, job, *outcomes, during_delay=None):
        """Run the job with stand-in runners that end at once, each first doing what `outcomes` names."""
        handoffs = []

        def start(gateway, name, job, ctx):
            directory = run_files(ctx.state_dir, job.message_id)
            (directory / rd.EXIT).unlink(missing_ok=True)
            handoffs.append(json.loads(json.dumps(runner_spawn.dump_job(job))))
            outcome = outcomes[len(handoffs) - 1]
            if outcome == "raise":  # Popen's EAGAIN, a handoff write the NAS refused, no thread for its reaper.
                raise OSError(11, "Resource temporarily unavailable")
            if outcome == "began":
                upsert_job_record(ctx.state_dir, job.message_id, status="running")
            elif outcome == "done":
                upsert_job_record(ctx.state_dir, job.message_id, status="done")
                run_files(directory, exit={"status": "finished"})
            subprocess.run([sys.executable, "-c", "pass", str(directory)], check=True)
            run_files(directory, runner={"pid": 0, "job_id": job.job_id, "key": job.message_id})
            return directory
        def delay(seconds):
            if during_delay:
                during_delay(rd.run_dir(ctx.state_dir, job.message_id))
            return False
        with mock.patch.object(detached, "start_runner", start), mock.patch.object(detached, "RELAUNCH_DELAY", 0), \
                mock.patch.object(self.app.runtime.shutdown, "wait", side_effect=delay), \
                mock.patch.object(type(app.controls), "show_recovery_card", return_value=True) as recovery:
            detached.run_detached(self.app, app, job, self.app.harnesses[app.spec.name].context)
        self.assertIsNone(app.runtime.jobs.find(7))
        return handoffs, recovery, find_job_record(ctx.state_dir, "task-7")

    def test_a_runner_that_ends_before_its_job_starts_is_started_once_more(self):
        app, ctx, job = self.unstarted_job()
        handoffs, recovery, record = self.launches(app, ctx, job, "crashed", "done")
        self.assertEqual((len(handoffs), record["status"]), (2, "done"))
        recovery.assert_not_called()
        # The second runner writes the same card, past any sequence the record holds.
        self.assertEqual(handoffs[1]["progress"]["card_id"], "cid-7")
        self.assertGreater(handoffs[1]["progress"]["sequence"], 5)
        self.assertTrue(handoffs[1]["extras"]["runner_relaunched"])  # A later bridge never starts a third.

    def test_a_supplement_sent_while_waiting_to_relaunch_is_handed_back_not_deleted(self):
        app, ctx, job = self.unstarted_job()
        def steer(directory):
            runner_ipc.write_inbox(directory, {"kind": "steer", "text": "also check docs", "event_id": "om_s"})
        handoffs, recovery, record = self.launches(app, ctx, job, "crashed", during_delay=steer)
        self.assertEqual((len(handoffs), record["status"]), (1, "interrupted"))
        recovery.assert_called_once()

    def first_runs_save(self, app, ctx, job):
        """The first run of ``job`` saves its first generation (its initial revocation, as a backend's ReplyState does)."""
        from covey.core.saved_results import observe_verified_reply
        first = runner_spawn.load_job(runner_spawn.dump_job(job))
        first.bridge_run_id = "first-run"
        observe_verified_reply(SimpleNamespace(state_dir=ctx.state_dir, workdir=self.work), first, app.spec.name, None)

    def test_a_job_whose_record_moved_on_during_the_wait_is_not_started_again(self):
        # T740: the record was read once, before RELAUNCH_DELAY: the runner that only looked gone had begun the job by
        # the end of it, and a second runner was started all the same.
        app, ctx, job = self.unstarted_job()
        began = lambda directory: upsert_job_record(ctx.state_dir, job.message_id, status="running")
        handoffs, recovery, record = self.launches(app, ctx, job, "crashed", "done", during_delay=began)
        self.assertEqual((len(handoffs), record["status"], record.get("runner_start_failed")), (1, "interrupted", False))
        recovery.assert_called_once()

    def test_a_job_whose_runner_saved_its_results_is_not_started_again(self):
        # T740: the second runner's first save met the first one's run id in the job's index (storage_failed), and its
        # failure card took the reply key of the first one, which then finished the job.
        app, ctx, job = self.unstarted_job()
        handoffs, recovery, record = self.launches(app, ctx, job, "crashed", "done",
                                                   during_delay=lambda directory: self.first_runs_save(app, ctx, job))
        self.assertEqual((len(handoffs), record["status"], record.get("runner_start_failed")), (1, "interrupted", False))
        self.assertEqual(record["result_run_id"], "first-run")

    def test_a_job_whose_runner_wrote_done_json_is_not_started_again(self):
        app, ctx, job = self.unstarted_job()
        handoffs, recovery, record = self.launches(app, ctx, job, "crashed", "done",
                                                   during_delay=lambda directory: run_files(directory, done={"status": "x"}))
        self.assertEqual((len(handoffs), record["status"], record.get("runner_start_failed")), (1, "interrupted", False))

    def test_a_runner_found_alive_after_the_wait_is_watched_again_not_settled_or_joined(self):
        # T740's trap: relaunch_unstarted finding the runner alive must not end in finish_detached, which would mark its
        # job interrupted, show a recovery card and run the turn's end while the runner works on.
        app, ctx, job = self.unstarted_job()
        stubs = []

        def alive_after_all(directory):
            if stubs:
                return
            stubs.append(RunnerProcess(directory, job.message_id))
            self.addCleanup(stubs[0].stop)

            def finish():
                upsert_job_record(ctx.state_dir, job.message_id, status="done")
                run_files(directory, exit={"status": "finished"})
                stubs[0].stop()
            threading.Timer(0.5, finish).start()
        handoffs, recovery, record = self.launches(app, ctx, job, "crashed", "done", during_delay=alive_after_all)
        self.assertEqual((len(handoffs), record["status"]), (1, "done"))
        recovery.assert_not_called()
        self.assertTrue(any(f"task runner of job #{job.job_id} still alive: watched again" in line for line in self.logs))

    def test_a_runner_that_only_looked_gone_is_watched_again_never_started_twice_or_settled(self):
        # T740: watch_until_exit took the live runner for gone, as each read of its runner.json failed for a moment.
        app, ctx, directory, stub = self.running_job(status="queued")
        job = runner_spawn.load_job(json.loads((directory / rd.HANDOFF).read_text())["job"])
        job.detached_run_dir = directory
        app.runtime.jobs.register(job)
        shutdown = self.fast_polling()
        start = mock.Mock(side_effect=OSError(11, "Resource temporarily unavailable"))
        returned = threading.Event()
        watcher = threading.Thread(target=lambda: (detached.run_detached(self.app, app, job, ctx), returned.set()),
                                   name="runner-watch", daemon=True)
        # Three reads: every one watch_until_exit makes in one look (READ_PAUSES); the next one finds the runner.
        with failing_reads(directory / rd.RUNNER, 3) as reads, mock.patch.object(detached, "start_runner", start), \
                mock.patch.object(type(app.controls), "show_recovery_card", return_value=True) as recovery:
            watcher.start()
            wait_for(self, lambda: returned.is_set() or len(reads) > 3)
            self.assertTrue(returned.is_set() or shutdown.wait_rounds("runner-watch", 3))
            self.assertFalse(returned.is_set())  # Still watching the live runner ...
            self.assertEqual(find_job_record(ctx.state_dir, "task-7")["status"], "queued")  # ... its job not settled.
            upsert_job_record(ctx.state_dir, "task-7", status="done")
            run_files(directory, exit={"status": "finished"})
            stub.stop()
            self.assertTrue(returned.wait(10))
        start.assert_not_called()
        recovery.assert_not_called()
        self.assertEqual(find_job_record(ctx.state_dir, "task-7")["status"], "done")
        self.assertTrue(any("task runner of job #7 still alive: watched again" in line for line in self.logs))
        self.assertIsNone(app.runtime.jobs.find(7))

    def watching(self, app, ctx, job):
        """``job`` watched in run_detached on a thread named runner-watch: (the thread, set once run_detached returned)."""
        returned = threading.Event()
        watcher = threading.Thread(target=lambda: (detached.run_detached(self.app, app, job, ctx), returned.set()),
                                   name="runner-watch", daemon=True)
        return watcher, returned

    def test_a_runner_that_looked_gone_after_its_job_began_is_looked_at_again_before_its_job_is_settled(self):
        # Its record says running, so it is not started again, and finish_detached would mark it interrupted.
        app, ctx, directory, stub = self.running_job(status="running")
        job = runner_spawn.load_job(json.loads((directory / rd.HANDOFF).read_text())["job"])
        job.detached_run_dir = directory
        app.runtime.jobs.register(job)
        shutdown = self.fast_polling()
        watcher, returned = self.watching(app, ctx, job)
        # Six reads fail: watch_until_exit's look and relaunch_unstarted's first one; the look a moment later finds it.
        with failing_reads(directory / rd.RUNNER, 6) as reads, \
                mock.patch.object(type(app.controls), "show_recovery_card", return_value=True) as recovery:
            watcher.start()
            wait_for(self, lambda: returned.is_set() or len(reads) > 6)
            self.assertTrue(returned.is_set() or shutdown.wait_rounds("runner-watch", 3))
            self.assertFalse(returned.is_set())
            self.assertEqual(find_job_record(ctx.state_dir, "task-7")["status"], "running")
            upsert_job_record(ctx.state_dir, "task-7", status="done")
            run_files(directory, exit={"status": "finished"})
            stub.stop()
            self.assertTrue(returned.wait(10))
        recovery.assert_not_called()
        self.assertEqual(find_job_record(ctx.state_dir, "task-7")["status"], "done")

    def test_a_runner_that_looked_gone_while_it_uploads_keeps_its_end_until_it_exits(self):
        app, ctx, directory, stub = self.running_job(status="done")
        job = runner_spawn.load_job(json.loads((directory / rd.HANDOFF).read_text())["job"])
        job.detached_run_dir = directory
        app.runtime.jobs.register(job)
        rd.mark_done(directory, "finished")
        shutdown = self.fast_polling()
        watcher, returned = self.watching(app, ctx, job)
        ended = lambda: any("task runner ended job=#7" in line for line in self.logs)
        watcher.start()
        self.assertTrue(returned.wait(10))  # The topic is free; a thread watches the runner as it uploads.
        with failing_reads(directory / rd.RUNNER, 3) as reads:
            wait_for(self, lambda: len(reads) > 3)
            self.assertTrue(shutdown.wait_rounds("runner-exit", 3))
        self.assertFalse(ended())  # Its end (finish_detached) is not run while it lives.
        self.assertTrue(app.runtime.jobs.settling("task-7"))
        stub.stop()
        wait_for(self, lambda: not app.runtime.jobs.settling("task-7"))
        self.assertTrue(ended())

    def test_a_job_whose_run_directory_holds_a_live_runner_is_watched_not_started_twice(self):
        app, ctx, job = self.unstarted_job()
        directory = rd.run_dir(ctx.state_dir, job.message_id)
        stub = RunnerProcess(directory, job.message_id)
        self.addCleanup(stub.stop)
        run_files(directory, handoff={"harness": app.spec.name, "job": runner_spawn.dump_job(job), "its": "own"})
        shutdown = self.fast_polling()
        returned = threading.Event()
        watcher = threading.Thread(target=lambda: (detached.run_detached(self.app, app, job, ctx), returned.set()),
                                   name="runner-watch", daemon=True)
        with mock.patch.object(runner_spawn.subprocess, "Popen") as popen, \
                mock.patch.object(type(app.controls), "show_recovery_card", return_value=True) as recovery:
            watcher.start()
            self.assertTrue(returned.is_set() or shutdown.wait_rounds("runner-watch", 3))
            self.assertFalse(returned.is_set())
            self.assertEqual(json.loads((directory / rd.HANDOFF).read_text())["its"], "own")
            self.assertEqual(json.loads((directory / rd.RUNNER).read_text())["pid"], stub.process.pid)
            upsert_job_record(ctx.state_dir, job.message_id, status="done")
            run_files(directory, exit={"status": "finished"})
            stub.stop()
            self.assertTrue(returned.wait(10))
        popen.assert_not_called()
        recovery.assert_not_called()
        self.assertEqual(find_job_record(ctx.state_dir, job.message_id)["status"], "done")
        self.assertTrue(any("already alive" in line and "not started again" in line for line in self.logs))

    def test_a_restart_adopts_a_live_runner_whose_handoff_cannot_be_read_for_a_moment(self):
        app, ctx, directory, _ = self.running_job()
        with failing_reads(directory / rd.HANDOFF, 1):
            kept = detached.adopt_runners(self.app, ctx)
        self.assertEqual(kept, {"task-7"})
        self.assertEqual(self.app.runtime.jobs.queue.get_nowait().detached_run_dir, directory)

    def test_a_runner_whose_files_cannot_be_read_but_holds_its_run_lock_is_never_started_again_or_settled(self):
        # T814: the Bridge read the runner it had just handed a job as gone (pid=None) and started the job's runner once
        # more 2 s later while the first ran on; both wrote its card (300315 Duplicate ID again and again). Here runner.json cannot be read through every look, and there is no wake.json yet, as for a runner
        # still starting: only its run lock says it is there, and no file read decides that.
        app, ctx, job = self.unstarted_job()
        directory = run_files(ctx.state_dir, job.message_id, folders=(rd.INBOX, rd.ACKS),
                              handoff={"harness": app.spec.name, "job": runner_spawn.dump_job(job)},
                              runner={"pid": 999999999, "job_id": 7, "key": job.message_id})
        holder = LockHolder(directory)
        self.addCleanup(holder.stop)
        job.detached_run_dir = directory
        shutdown = self.fast_polling()
        watcher, returned = self.watching(app, ctx, job)
        with failing_reads(directory / rd.RUNNER, 10 ** 6), \
                mock.patch.object(runner_spawn.subprocess, "Popen", side_effect=OSError(11, "unavailable")) as popen, \
                mock.patch.object(type(app.controls), "show_recovery_card", return_value=True) as recovery:
            watcher.start()
            self.assertTrue(returned.is_set() or shutdown.wait_rounds("runner-watch", 3))
            self.assertFalse(returned.is_set())  # Still watching the live runner ...
            self.assertEqual(find_job_record(ctx.state_dir, job.message_id)["status"], "queued")  # ... its job not settled.
            self.assertTrue((directory / rd.RUNNER).exists())  # Its directory not cleared for a second one.
            upsert_job_record(ctx.state_dir, job.message_id, status="done")
            run_files(directory, exit={"status": "finished"})
            holder.stop()
            self.assertTrue(returned.wait(10))
        popen.assert_not_called()
        recovery.assert_not_called()
        self.assertEqual(find_job_record(ctx.state_dir, job.message_id)["status"], "done")
        self.assertFalse(any("relaunched" in line for line in self.logs))

    def test_a_restart_adopts_a_runner_its_run_lock_names_when_runner_json_names_another(self):
        # T814: runner.json may name a runner the Bridge started in the same moment that found the job's runner holding
        # the lock and ran nothing; a restart that went by runner.json alone left the live runner's job to be interrupted.
        app, ctx, directory, stub = self.running_job()
        stub.stop()
        holder = LockHolder(directory)
        self.addCleanup(holder.stop)
        self.assertEqual(detached.adopt_runners(self.app, ctx), {"task-7"})
        self.assertEqual(self.app.runtime.jobs.queue.get_nowait().detached_run_dir, directory)

    def test_a_click_goes_to_the_runner_this_bridge_watches_when_only_its_run_lock_names_it(self):
        app, ctx, directory, stub = self.running_job()
        stub.stop()
        holder = LockHolder(directory)
        self.addCleanup(holder.stop)
        job = runner_spawn.load_job(json.loads((directory / rd.HANDOFF).read_text())["job"])
        job.detached_run_dir = directory
        app.runtime.jobs.register(job)
        with mock.patch.object(type(app.controls), "cancel_reply_job_from_card") as local:
            self.click(kind="bridge_job_stop", job_id=7)
        local.assert_not_called()  # The Bridge did not take the click on the runner's card for itself.
        sent = [json.loads(p.read_text()) for p in sorted((directory / rd.INBOX).glob("*.json"))]
        self.assertEqual([s["event"]["action_value"]["kind"] for s in sent], ["bridge_job_stop"])

    def test_a_second_run_of_the_same_job_fails_at_its_saved_results_and_saves_nothing(self):
        # Why T740's second runner failed before its backend started: the job's index holds the first run's id. The rule
        # stays (a run never saves over another's generation); T740 keeps the second runner from starting instead.
        from covey.core.reply_state import ReplyError, ReplyState
        from covey.core.saved_results import observe_verified_reply
        app, ctx, job = self.unstarted_job()
        self.first_runs_save(app, ctx, job)
        second = runner_spawn.load_job(runner_spawn.dump_job(job))
        second.bridge_run_id = "second-run"
        saved_ctx = SimpleNamespace(state_dir=ctx.state_dir, workdir=self.work)
        state = ReplyState({"harness_id": app.spec.name}, "second-run",
                           lambda reply: observe_verified_reply(saved_ctx, second, app.spec.name, reply))
        with self.assertRaises(ReplyError) as raised:
            state.invalidate()
        self.assertEqual(raised.exception.code, "storage_failed")
        self.assertEqual(find_job_record(ctx.state_dir, "task-7")["result_run_id"], "first-run")

    def test_a_relaunched_runner_gets_no_run_id_of_the_first_and_no_saved_generation_to_meet(self):
        # The usual relaunch: the first runner ended before its job began, so it saved nothing; the second one's run id
        # comes when its own turn starts (app/worker.py), and its first save starts at generation 1.
        app, ctx, job = self.unstarted_job()
        handoffs, recovery, record = self.launches(app, ctx, job, "crashed", "done")
        self.assertEqual(len(handoffs), 2)
        self.assertIsNone(handoffs[1]["fields"].get("bridge_run_id"))
        self.assertNotIn("result_run_id", record)

    def test_a_steer_form_sent_after_the_handoff_is_closed_when_the_runner_ends(self):
        # S35 #930: a 补充 click while the job was handed over; the runner never knew the form it got.
        app, ctx, directory, stub = self.running_job(status="done")
        handoff = json.loads((directory / rd.HANDOFF).read_text())
        handoff["job"]["extras"]["steer_forms"] = ["form-handed"]  # The runner's to close.
        run_files(directory, handoff=handoff)
        job = runner_spawn.load_job(handoff["job"])
        job.steer_forms = ["form-handed", "form-late"]
        stub.stop()
        closed = []
        with mock.patch.object(app.steering, "close_steer_forms",
                               lambda ctx, job, held=True: closed.append((list(job.steer_forms), held, ctx.topic_id))):
            detached.finish_detached(self.app, app, job, ctx, directory)
        self.assertEqual(closed, [(["form-late"], False, ctx.topic_id)])

    def test_the_owners_remember_in_a_topic_turn_is_collected_once_the_runner_has_exited(self):
        # 知识条目: the Bridge, not the runner, writes a turn's `remember` (app/knowledge_collect.py), after the record
        # is settled; a write that fails leaves the rest of finish_detached as it was.
        from covey.core import knowledge
        app, ctx, directory, stub = self.running_job(status="done")
        checkpoint = Path(self.work) / "cp.json"
        checkpoint.write_text(json.dumps({"remember": [{"kind": "pitfall", "text": "rg 会扫 NAS", "scope": "owner"}]}))
        upsert_job_record(ctx.state_dir, "task-7", checkpoint_path=str(checkpoint), user_text="这个坑记一下",
                          sender_id="ou_owner", workspace_name="ws")
        job = runner_spawn.load_job(json.loads((directory / rd.HANDOFF).read_text())["job"])
        job.user_text, job.sender_id = "这个坑记一下", "ou_owner"
        stub.stop()
        with mock.patch.dict(os.environ, {knowledge.ENV: "1"}), \
                mock.patch.object(self.app.runtime.access, "owner_turn", return_value=True), \
                mock.patch.object(detached.butler_relay, "after_runner") as after:
            with mock.patch.object(knowledge, "remember", side_effect=OSError("NAS gone")):
                detached.finish_detached(self.app, app, job, ctx, directory)
            self.assertEqual(knowledge.entries(ctx.state_dir), [])
            after.assert_called_once()
            detached.finish_detached(self.app, app, job, ctx, directory)
        self.assertEqual([(e["text"], e["by"], e["source"]["job_key"]) for e in knowledge.entries(ctx.state_dir)],
                         [("rg 会扫 NAS", "topic", "task-7")])

    def stranded(self, **exit_fields):
        """A turn that ended (done) in a runner this Bridge handed it to, the runner gone while no Bridge watched it
        (exit.json written, its process ended): its end still due (turn-ends/). The app, context and run directory."""
        app, ctx, directory, stub = self.running_job(status="done")
        job = runner_spawn.load_job(json.loads((directory / rd.HANDOFF).read_text())["job"])
        detached.end_due(ctx.state_dir, job, self.logs.append)
        run_files(directory, exit={"status": "finished", "finished_at": time.time(), **exit_fields})
        stub.stop()
        return app, ctx, directory

    def test_a_turn_whose_runner_exits_in_a_restarts_gap_has_its_end_run_by_the_next_bridge_once(self):
        # T688: the Bridge alone runs a turn's end (after_job), once its runner has exited. A runner that exits in a
        # deploy's or a restart's gap, no Bridge watching it, would lose it (知识条目, 记忆, 代发草稿, 转交管家, a
        # project's receipt), which the runner used to run itself.
        from covey.core import knowledge
        app, ctx, job = self.unstarted_job()
        checkpoint = Path(self.work) / "cp.json"
        checkpoint.write_text(json.dumps({"remember": [{"kind": "pitfall", "text": "rg 会扫 NAS", "scope": "owner"}]}))
        upsert_job_record(ctx.state_dir, "task-7", checkpoint_path=str(checkpoint), user_text="这个坑记一下",
                          sender_id="ou_owner")
        runners = []

        def start(runtime, name, job, ctx):
            directory = rd.run_dir(ctx.state_dir, job.message_id)
            run_files(directory, handoff={"harness": name, "job": runner_spawn.dump_job(job)})
            runners.append(RunnerProcess(directory, job.message_id))
            self.addCleanup(runners[-1].stop)
            return directory
        with mock.patch.object(detached, "start_runner", start), \
                mock.patch.object(self.app.runtime.shutdown, "wait", return_value=True):  # The Bridge stops meanwhile.
            detached.run_detached(self.app, app, job, self.app.harnesses[app.spec.name].context)
        directory = rd.run_dir(ctx.state_dir, "task-7")
        upsert_job_record(ctx.state_dir, "task-7", status="done")  # Its turn ends; its runner exits, no Bridge up.
        run_files(directory, exit={"status": "finished", "finished_at": time.time()})
        runners[0].stop()
        bridge, start_ctx = self.bridge()  # The next Bridge starts.
        with mock.patch.dict(os.environ, {knowledge.ENV: "1"}), \
                mock.patch.object(bridge.runtime.access, "owner_turn", return_value=True):
            bridge.notify_stale_jobs(start_ctx)
            bridge.notify_stale_jobs(start_ctx)  # And the one after it: once.
        self.assertEqual([(e["text"], e["source"]["job_key"]) for e in knowledge.entries(ctx.state_dir)],
                         [("rg 会扫 NAS", "task-7")])
        self.assertEqual(sum("turn end run at start job=#7" in line for line in self.logs), 1)
        self.assertEqual(list((ctx.state_dir / detached.ENDS).glob("*.json")), [])

    def test_a_start_takes_the_pending_relay_of_a_runner_that_exited_while_no_bridge_watched(self):
        app, ctx, directory = self.stranded()
        with mock.patch.object(detached, "after_job"), \
                mock.patch.object(type(app.controls), "take_left_relay") as taken:
            self.app.notify_stale_jobs(self.ctx)
        self.assertEqual([call.args[1].message_id for call in taken.call_args_list], ["task-7"])

    def test_a_bridge_that_watched_its_runner_end_runs_the_end_and_no_start_runs_it_again(self):
        app, ctx, directory = self.stranded()
        job = runner_spawn.load_job(json.loads((directory / rd.HANDOFF).read_text())["job"])
        with mock.patch.object(detached, "after_job") as ended:
            detached.finish_detached(self.app, app, job, ctx, directory)
            self.assertFalse(detached.end_path(ctx.state_dir, "task-7").exists())
            self.app.notify_stale_jobs(self.ctx)
        ended.assert_called_once()

    def test_a_live_runners_end_waits_for_it_and_an_unknown_or_old_ones_is_dropped_unrun(self):
        app, ctx, directory, stub = self.running_job(status="running")
        job = runner_spawn.load_job(json.loads((directory / rd.HANDOFF).read_text())["job"])
        detached.end_due(ctx.state_dir, job, self.logs.append)
        old = BridgeReplyJob(job_id=8, user_text="x", chat_id="chat", message_id="old-8", timeout=0, topic_id=ctx.topic_id)
        run_files(ctx.state_dir, "old-8", handoff={"harness": app.spec.name, "job": runner_spawn.dump_job(old)},
                  exit={"status": "finished", "finished_at": time.time() - detached.ENDS_KEPT - 60})
        gone = BridgeReplyJob(job_id=9, user_text="x", chat_id="chat", message_id="gone-9", timeout=0, topic_id=ctx.topic_id)
        run_files(ctx.state_dir, "gone-9", handoff={"harness": "retired-harness", "job": runner_spawn.dump_job(gone)},
                  exit={"status": "finished", "finished_at": time.time()})
        for each in (old, gone):
            detached.end_due(ctx.state_dir, each, self.logs.append)
        detached.end_due(ctx.state_dir, BridgeReplyJob(job_id=10, user_text="x", chat_id="chat", message_id="lost-10",
                                                       timeout=0), self.logs.append)  # Its run directory is gone.
        with mock.patch.object(detached, "after_job") as ended:
            self.app.notify_stale_jobs(self.ctx)
        ended.assert_not_called()
        self.assertTrue(detached.end_path(ctx.state_dir, "task-7").exists())  # Adopted: finish_detached ends it.
        self.assertEqual(sorted(p.name for p in (ctx.state_dir / detached.ENDS).iterdir()),
                         [detached.end_path(ctx.state_dir, "task-7").name])
        self.assertTrue(any("turn end dropped job=#8: its runner ended too long ago" in line for line in self.logs))
        self.assertTrue(any("turn end dropped job=#9: its Harness is not in the roster" in line for line in self.logs))
        self.assertTrue(any("turn end not run at start job=#10: FileNotFoundError" in line for line in self.logs))

    def test_a_handed_job_keeps_its_end_due_until_the_bridge_has_run_it(self):
        app, ctx, job = self.unstarted_job()
        due = []
        with mock.patch.object(detached, "after_job",
                               side_effect=lambda *a: due.append(detached.end_path(ctx.state_dir, "task-7").exists())):
            handoffs, recovery, record = self.launches(app, ctx, job, "done")
        self.assertEqual((len(handoffs), record["status"], due), (1, "done", [True]))
        self.assertFalse(detached.end_path(ctx.state_dir, "task-7").exists())

    def test_a_job_whose_runner_fails_to_start_twice_says_nothing_ran(self):
        from covey.app.recovery_cards import recovery_card
        app, ctx, job = self.unstarted_job()
        handoffs, recovery, record = self.launches(app, ctx, job, "crashed", "crashed")
        self.assertEqual((len(handoffs), record["status"], record["runner_start_failed"]), (2, "interrupted", True))
        recovery.assert_called_once()
        card = recovery_card(record)
        # The task card's row (停住了), why in bold (任务进程没能启动), then what did not happen (T666: no band).
        head, lead, text = (element.get("content") or element for element in card["body"]["elements"][:3])
        self.assertNotIn("header", card)
        self.assertIn("<text_tag color='red'>停住了</text_tag>", json.dumps(head, ensure_ascii=False))
        self.assertIn("任务进程没能启动", lead)
        self.assertIn("没有执行任何操作", text)
        self.assertNotIn("尚无检查点", text_of(card))
        moved = recovery_card(record, moved_to=8)
        self.assertIn("↪ 已重新开始", text_of(moved))
        self.assertNotIn("&#35;8", text_of(moved))
        self.assertNotIn("recovery_actions", json.dumps(moved))

    def test_a_runner_that_cannot_be_started_ends_its_job_as_failed(self):
        from covey.app.recovery_cards import recovery_card
        from covey.core.scheduler import WorkspaceScheduler
        app, ctx, job = self.unstarted_job()
        detached.remember_queued(ctx.state_dir, app.spec.name, job)
        logs = []
        # The scheduler's worker, as the live Bridge runs it.
        scheduler = WorkspaceScheduler(self.app.runtime.shutdown, self.app.runtime.jobs.queue,
                                       lambda j: detached.run_detached(self.app, app, j, app.harness.context), logs.append)
        self.app.runtime.jobs.queue.put(job)
        self.app.runtime.jobs.queue.get_nowait()
        scheduler.active[job.message_id] = ("chat", ctx.topic_id)
        with mock.patch.object(detached, "start_runner", side_effect=OSError(11, "Resource temporarily unavailable")), \
                mock.patch.object(type(app.controls), "show_recovery_card", return_value=True) as recovery:
            scheduler.execute(job)
        self.assertEqual(logs, [])  # Handled, not left to the scheduler's log.
        self.assertEqual(scheduler.active, {})
        record = find_job_record(ctx.state_dir, "task-7")
        self.assertEqual((record["status"], record["runner_start_failed"], record["runner_start_error"]),
                         ("failed", True, "BlockingIOError: Resource temporarily unavailable"))
        self.assertIsNone(app.runtime.jobs.find(7))
        self.assertFalse(detached.queued_path(ctx.state_dir, "task-7").exists())
        recovery.assert_called_once()
        card = recovery_card(record)
        text = json.dumps(card, ensure_ascii=False)
        self.assertIn("<text_tag color='red'>停住了</text_tag>", text)  # The task card's row (T666: no band).
        self.assertIn("任务进程没能启动", text)
        # The exception's kind is in 技术详情, folded (T650: no exception name in the words the owner reads first).
        self.assertNotIn("BlockingIOError", json.dumps(card["body"]["elements"][:2], ensure_ascii=False))
        self.assertIn("启动出错 BlockingIOError: Resource temporarily unavailable",
                      json.dumps(find_element(card, "failure_details"), ensure_ascii=False))
        self.assertIn('"content": "检查并继续"', text)
        # Its stop button now finds it ended; nothing is left to hold a deploy.
        self.click(kind="bridge_job_stop", job_id=7)
        self.assertIn("当前不在运行或队列中", str(app.messenger.send_message.call_args))
        self.assertEqual(find_job_record(ctx.state_dir, "task-7")["status"], "failed")
        self.assertEqual(detached.restart_blockers(ctx.state_dir), [])

    def test_a_runner_that_cannot_be_started_again_ends_its_job_too(self):
        app, ctx, job = self.unstarted_job()
        handoffs, recovery, record = self.launches(app, ctx, job, "crashed", "raise")
        self.assertEqual((len(handoffs), record["status"], record["runner_start_failed"]), (2, "interrupted", True))
        recovery.assert_called_once()

    def test_a_runner_whose_start_could_not_be_recorded_is_ended_before_it_takes_the_job(self):
        app, ctx, job = self.unstarted_job()
        directory = rd.run_dir(ctx.state_dir, job.message_id)
        stub = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", str(directory)],
                                start_new_session=True)
        self.addCleanup(lambda: stub.poll() is None and (stub.kill(), stub.wait()))
        with mock.patch.object(runner_spawn, "_spawn", return_value=(stub, REPO)), \
                mock.patch.object(runner_spawn, "register_runner", side_effect=OSError(5, "Input/output error")):
            with self.assertRaises(OSError):
                runner_spawn.start_runner(self.app.runtime, app.spec.name, job, ctx)
        self.assertIsNotNone(stub.poll())  # Killed: the bridge would not have known it ran.
        self.assertFalse((directory / rd.HANDOFF).exists())

    def test_a_job_whose_runner_only_uploads_now_frees_its_topic(self):
        """The runner wrote done.json (its job ended, its uploads go on, up to 29 s in 2026-10): the job
        leaves the registry and its scheduler slot at once; the outbox, automatic retries and
        maintenance wait for the runner's exit, which is then settled as before."""
        from support.world import wait_for
        app, ctx, directory, stub = self.running_job(status="running")
        job = runner_spawn.load_job(json.loads((directory / rd.HANDOFF).read_text())["job"])
        job.detached_run_dir = directory
        app.runtime.jobs.register(job)
        rd.mark_done(directory, "finished")
        self.assertFalse(detached.job_done(ctx, job, directory))  # Its record has not ended: still its runner's.
        upsert_job_record(ctx.state_dir, "task-7", status="done")
        self.assertTrue(detached.job_done(ctx, job, directory))
        returned = threading.Event()
        threading.Thread(target=lambda: (detached.run_detached(self.app, app, job, ctx), returned.set()),
                         daemon=True).start()
        self.assertTrue(returned.wait(5))
        self.assertIsNone(stub.process.poll())  # It still uploads.
        self.assertIsNone(app.runtime.jobs.find(7))
        self.assertTrue(app.runtime.jobs.settling("task-7"))
        self.assertTrue(app.runtime.jobs.busy())
        self.click(kind="bridge_trace", topic_id=ctx.topic_id, job_id=7, verb="latest")
        self.assertEqual(len(list((directory / rd.INBOX).glob("*.json"))), 1)  # Its cards' clicks still go to it.
        stub.stop()
        wait_for(self, lambda: not app.runtime.jobs.settling("task-7"))
        self.assertTrue(any("task runner ended job=#7 status=done" in line for line in self.logs))
        self.assertFalse(app.runtime.jobs.busy())

    def test_a_runner_marks_its_job_done_before_its_uploads_end(self):
        before = set(threading.enumerate())
        order = []
        upload = threading.Thread(target=lambda: (time.sleep(0.3), order.append("uploaded")), name="artifact-upload")
        update = threading.Thread(target=lambda: (time.sleep(0.1), order.append("card")), name="card-updates")
        upload.start(), update.start()
        task_runner._wait_background(before, uploads=False)
        order.append("done")
        task_runner._wait_background(before)
        self.assertEqual(order, ["card", "done", "uploaded"])

    def test_a_runner_that_ends_after_its_job_began_is_not_started_again(self):
        app, ctx, job = self.unstarted_job()
        handoffs, recovery, record = self.launches(app, ctx, job, "began")
        self.assertEqual((len(handoffs), record["status"], record["runner_start_failed"]), (1, "interrupted", False))
        recovery.assert_called_once()
        from covey.app.recovery_cards import recovery_card
        card = recovery_card(record)
        self.assertNotIn("header", card)
        self.assertEqual(card["body"]["elements"][0]["element_id"], "task_head")  # T666: the task card's row, no band.
        self.assertIn("停住了", text_of(card["body"]["elements"][0]))
        self.assertIn("这一轮被打断了", text_of(card))

    def test_restart_blockers_name_only_jobs_a_restart_would_interrupt(self):
        app, ctx, directory, _ = self.running_job()
        # "stopped": a stop clicked on a job no runner took (its start failed before 2026-10-28) holds no deploy.
        for key, status in (("inproc", "running"), ("saved", "queued"), ("unsaved", "queued"), ("over", "done"),
                            ("stopped", "cancel_requested")):
            upsert_job_record(ctx.state_dir, key, job_id=len(key), status=status, backend=app.spec.name)
        detached.remember_queued(ctx.state_dir, app.spec.name,
                                 BridgeReplyJob(job_id=5, user_text="x", chat_id="chat", message_id="saved", timeout=0))
        self.assertEqual(sorted(b["key"] for b in detached.restart_blockers(ctx.state_dir)), ["inproc", "unsaved"])

    def test_a_restart_reads_only_the_run_directories_a_live_process_names(self):
        # Every finished run's runner.json read one by one took 3-8 s on NAS for 1700 of them, four looks a start: only
        # the directories a process of ours names in its argv are asked now (core/run_dir.py argv_dirs).
        app, ctx, directory, stub = self.running_job()
        for n in range(20):
            run_files(ctx.state_dir, f"ended-{n}", runner={"pid": os.getpid(), "key": f"ended-{n}"}, exit={})
        asked, real = [], rd.process_identity
        with mock.patch.object(rd, "process_identity", lambda pid: asked.append(pid) or real(pid)):
            self.assertEqual(rd.live_keys(ctx.state_dir), {"task-7"})
            self.assertEqual(detached.live_keys(ctx.state_dir), {"task-7"})
            self.assertEqual(rd.live_dirs(ctx.state_dir), [directory])
            self.assertEqual(detached.adopt_runners(self.app, self.ctx), {"task-7"})
        self.assertEqual(set(asked), {stub.process.pid})
        self.assertEqual(rd.argv_dirs(ctx.state_dir / rd.RUNS), {directory.name})

    def test_without_proc_or_with_a_test_of_its_own_every_run_directory_is_asked_as_before(self):
        app, ctx, directory, _ = self.running_job()
        ended = run_files(ctx.state_dir, "ended", runner={"pid": os.getpid(), "key": "ended"}, exit={})
        self.assertEqual(rd.live_keys(ctx.state_dir, lambda d: Path(d).name == ended.name), {"ended"})
        self.assertEqual(rd.live_dirs(ctx.state_dir, lambda d: True), sorted([directory, ended]))
        listdir = os.listdir

        def no_proc(path="."):
            if str(path) == "/proc":
                raise PermissionError(path)
            return listdir(path)
        with mock.patch.object(rd.os, "listdir", no_proc):
            self.assertIsNone(rd.argv_dirs(ctx.state_dir / rd.RUNS))
            self.assertEqual(rd.live_keys(ctx.state_dir), {"task-7"})
            self.assertEqual(rd.live_dirs(ctx.state_dir), [directory])

    def test_a_restart_settles_only_the_answered_questions_their_follow_up_moved_on_from(self):
        # Each answered question was locked and read twice at every start, 12 ms each on NAS for 1400 of them: one already
        # as its follow-up says is left alone, one whose follow-up went on is settled as before.
        from covey.app import questions as questions_module
        app, ctx, _, _ = self.running_job(runner=False, status="done")
        folder = ctx.state_dir / "questions"
        folder.mkdir(exist_ok=True)
        for token, job_id, delivery in (("a" * 16, 21, "done"), ("b" * 16, 22, "running")):
            upsert_job_record(ctx.state_dir, "answer-" + token, job_id=job_id, topic_id=ctx.topic_id, chat_id="chat",
                              message_id="answer-" + token, status="done", backend=app.spec.name)
            (folder / (token + ".json")).write_text(json.dumps({
                "token": token, "job_key": "task-7", "job_id": 7, "topic_id": ctx.topic_id, "chat_id": "chat",
                "status": "answered", "continuation_job_id": job_id, "delivery_state": delivery, "questions": []}))
        settled, real = [], questions_module._settle_question
        with mock.patch.object(questions_module, "_settle_question",
                               lambda path, change: settled.append(path.name) or real(path, change)):
            recover_question_delivery(self.ctx, [a.questions for a in self.app.apps.values()], self.app.runtime.log)
        self.assertEqual(settled, ["b" * 16 + ".json"])
        record = json.loads((folder / ("b" * 16 + ".json")).read_text())
        self.assertEqual((record["status"], record["delivery_state"]), ("answered", "done"))
        self.assertEqual(json.loads((folder / ("a" * 16 + ".json")).read_text())["delivery_state"], "done")

    def test_a_restart_leaves_the_questions_of_a_live_runner_alone(self):
        app, ctx, directory, _ = self.running_job()
        upsert_job_record(ctx.state_dir, "gone", job_id=8, topic_id=ctx.topic_id, chat_id="chat", status="running",
                          backend=app.spec.name)
        folder = ctx.state_dir / "questions"
        folder.mkdir(exist_ok=True)
        for token, key in (("a" * 16, "task-7"), ("b" * 16, "gone")):
            (folder / (token + ".json")).write_text(json.dumps({
                "token": token, "job_key": key, "job_id": 7, "topic_id": ctx.topic_id, "chat_id": "chat",
                "status": "submitting", "delivery_state": "awaiting_ack", "questions": []}))
        recover_question_delivery(self.ctx, [a.questions for a in self.app.apps.values()], self.app.runtime.log)
        status = {t: json.loads((folder / (t + ".json")).read_text())["status"] for t in ("a" * 16, "b" * 16)}
        self.assertEqual(status, {"a" * 16: "submitting", "b" * 16: "uncertain"})

    def test_maintenance_removes_only_old_directories_of_finished_runners(self):
        import os
        from covey.core.maintenance import maintain
        app, ctx, live, _ = self.running_job()
        old, recent = (run_files(ctx.state_dir, key, exit={}) for key in ("old", "recent"))
        stale = time.time() - 8 * 86400
        os.utime(old / rd.EXIT, (stale, stale))
        for path in live.rglob("*"):
            os.utime(path, (stale, stale))
        self.assertEqual(maintain(ctx.state_dir)["remove_runs"], [str(old)])

    def test_maintenance_drops_unread_fields_only_from_ended_records_without_a_live_runner(self):
        # S41: job records were 26 MB, 40% the history a prompt was built from; a runner's first read parses them all.
        from covey.core.jobs import job_path
        from covey.core.maintenance import _cleanup, plan_maintenance
        app, ctx, live, _ = self.running_job(status="done")  # Ended, but its task runner is still alive.
        unread = {"history_snapshot": [{"role": "user", "content": "earlier"}], "recent_questions": [{"id": "q"}],
                  "recovery_audit": {"checked": True}}
        upsert_job_record(ctx.state_dir, "task-7", **unread)
        for number, (key, status) in enumerate((("ended", "done"), ("broke", "failed"),
                                                ("choosing", "waiting_workspace"), ("busy", "running")), 20):
            upsert_job_record(ctx.state_dir, key, job_id=number, topic_id=ctx.topic_id, chat_id="chat", status=status,
                              final_text="the reply", prompt_text="the prompt", **unread)

        def read(key):
            return json.loads(job_path(ctx.state_dir, key).read_text())
        before = read("ended")
        plan = plan_maintenance(ctx.state_dir)
        self.assertEqual([key for key, _ in plan["slim_records"]], ["broke", "ended"])
        self.assertTrue(_cleanup(ctx.state_dir, plan, lambda: True, time.time()))
        self.assertEqual(read("ended"), {k: v for k, v in before.items() if k not in unread})  # updated_at too.
        self.assertEqual((read("broke")["final_text"], read("broke")["prompt_text"]), ("the reply", "the prompt"))
        self.assertFalse(set(unread) & set(read("broke")))
        for key in ("task-7", "choosing", "busy"):
            self.assertLessEqual(set(unread), set(read(key)), key)
        journal = ctx.state_dir / "jobs" / ".journal"
        self.assertTrue(journal.exists())
        with mock.patch("covey.core.maintenance_rules.JOURNAL_BYTES", journal.stat().st_size - 1):
            _cleanup(ctx.state_dir, plan_maintenance(ctx.state_dir), lambda: True, time.time())
        self.assertFalse(journal.exists())  # Started again by the next write; each store lists once.

    def test_maintenance_drops_the_fields_no_release_reads_from_ended_records(self):
        # Repo review R2: diff_*, topic_only, card_style and input_tokens_source were in 719, 91, 14 and 1
        # live records, read by no release; maintenance drops them with the S41 fields.
        from covey.core.jobs import job_path
        from covey.core.maintenance import _cleanup, plan_maintenance
        _, ctx, _, _ = self.running_job()
        dead = {"diff_doc_error": "", "diff_doc_id": "d", "diff_doc_url": "u", "diff_status": "uploaded",
                "topic_only": True, "card_style": "narrative", "input_tokens_source": "usage"}
        for number, (key, status) in enumerate((("ended", "done"), ("asking", "waiting_user")), 20):
            upsert_job_record(ctx.state_dir, key, job_id=number, topic_id=ctx.topic_id, chat_id="chat", status=status,
                              final_text="the reply", **dead)
        plan = plan_maintenance(ctx.state_dir)
        self.assertEqual([key for key, _ in plan["slim_records"]], ["ended"])
        self.assertTrue(_cleanup(ctx.state_dir, plan, lambda: True, time.time()))
        ended = json.loads(job_path(ctx.state_dir, "ended").read_text())
        self.assertEqual((set(dead) & set(ended), ended["final_text"]), (set(), "the reply"))
        self.assertLessEqual(set(dead), set(json.loads(job_path(ctx.state_dir, "asking").read_text())))

    def test_a_queued_message_steers_into_a_job_running_in_a_runner(self):
        app, ctx, directory, _ = self.running_job()
        running = BridgeReplyJob(job_id=7, user_text="hi", chat_id="chat", message_id="task-7", timeout=0,
                                 topic_id=ctx.topic_id, harness_id=app.spec.name)
        running.execution_started = True
        running.backend_control = runner_ipc.RemoteControl(directory, True)
        queued = BridgeReplyJob(job_id=9, user_text="more", chat_id="chat", message_id="more", timeout=0,
                                topic_id=ctx.topic_id, harness_id=app.spec.name)
        queued.execution_started = False
        app.runtime.jobs.register(running)
        app.runtime.jobs.register(queued)

        def runner_acks():
            for _ in range(100):
                ops = list((directory / rd.INBOX).glob("*.json"))
                if ops:
                    op = json.loads(ops[0].read_text())
                    ops[0].unlink()
                    self.assertEqual((op["kind"], op["text"]), ("steer", "more"))
                    rd.ack(directory, op["id"], True)
                    return
                time.sleep(0.05)
        responder = threading.Thread(target=runner_acks)
        responder.start()
        app.steering.steer_queued_job(ctx, "chat", queued, "click-steer")
        responder.join(5)
        self.assertTrue(queued.cancelled_before_start)
        self.assertIn("已作为补充送入正在执行的任务 #7", str(find_job_record(ctx.state_dir, "more")))
        with mock.patch.object(runner_ipc, "ACK_TIMEOUT", 0.3):
            with self.assertRaisesRegex(RuntimeError, "暂未确认送达") as caught:
                running.backend_control.steer("again", "click-2")
        self.assertEqual(Path(caught.exception.run_dir), directory)  # A later check reads the runner's ack there.


class RunnerTests(UnifiedCase):
    """runner_main in this process: the job's whole path, with a stop forwarded through the inbox."""

    def bridge_job(self):
        """Job #7 of a bound topic, queued, as the Bridge hands it on: (its Harness app, the job, the topic's context)."""
        app, ctx = self.route("task")
        from covey.core.topics import bind_topic
        bind_topic(ctx, ctx.workspace_name, self.work)
        job = BridgeReplyJob(job_id=7, user_text="hi", chat_id="chat", message_id="task", timeout=0,
                             topic_id=ctx.topic_id, harness_id=app.spec.name, workdir=self.work,
                             history_path=ctx.history_path, session_path=ctx.session_path, history_snapshot=[],
                             binding_revision=ctx.options.get("binding_revision", 1), reply_to_message_id="task")
        upsert_job_record(ctx.state_dir, "task", job_id=7, topic_id=ctx.topic_id, chat_id="chat", message_id="task",
                          status="queued", backend=app.spec.name, kind="chat")
        remember_topic_message(ctx.state_dir, ctx.topic_id, "chat", "card-7")
        upsert_job_record(ctx.state_dir, "task", card_message_id="card-7")
        return app, job, ctx

    def stopped_at_once(self):
        """The patches a runner_main in this process runs a job under: its backend stops it at once, nothing is sent."""
        def generate(self, ctx, job, session_id, progress):
            raise JobCancelled("任务已停止")
        from covey.app.runs import Runs
        from covey.feishu.messenger import Messenger
        stack = contextlib.ExitStack()
        for patch in (mock.patch.object(Runs, "generate_reply_for_worker", generate),
                      mock.patch.object(Runtime, "log", lambda self, message: None),
                      mock.patch.object(Messenger, "send_message", lambda self, *a, **k: True), mock.patch("signal.signal")):
            stack.enter_context(patch)
        return stack

    def test_a_runner_its_runner_json_names_already_leaves_the_file_alone(self):
        # T740: the runner wrote runner.json again on taking its handoff, in the moment its Bridge read the file.
        directory, ctx = self.handoff()
        app = self.app.apps[json.loads((directory / rd.HANDOFF).read_text())["harness"]]
        named = {**process_identity(os.getpid()), "release": str(REPO), "started_at": 1.0, "job_id": 7, "key": "task",
                 "harness": app.spec.name}
        run_files(directory, runner=named)  # As the Bridge writes it (runner_spawn.register_runner), time aside.
        with self.stopped_at_once():
            task_runner.runner_main(["--run-dir", str(directory)])
        self.assertEqual(json.loads((directory / rd.RUNNER).read_text()), named)
        self.assertEqual(find_job_record(ctx.state_dir, "task")["status"], "cancelled")

    def test_a_second_runner_of_a_job_runs_nothing_and_changes_nothing_in_its_run_directory(self):
        # T814: two runners of one job each wrote its card and record. The one that comes second finds the run lock held
        # and leaves before it writes anything; once the first is gone, a runner of the job takes the lock and runs it.
        directory, ctx = self.handoff()
        holder = LockHolder(directory)
        self.addCleanup(holder.stop)
        files = lambda: {str(p.relative_to(directory)): p.read_bytes() for p in directory.rglob("*") if p.is_file()}
        before, out = files(), io.StringIO()
        with self.stopped_at_once(), mock.patch.object(rd, "RUN_LOCK_WAIT", 0.2, create=True), \
                contextlib.redirect_stdout(out):
            self.assertEqual(task_runner.runner_main(["--run-dir", str(directory)]), 0)
        self.assertEqual(files(), before)  # No runner.json, wake.json, done.json or exit.json of its own.
        self.assertEqual(find_job_record(ctx.state_dir, "task")["status"], "queued")  # The job not run here.
        self.assertIn("task runner job=#7", out.getvalue())
        self.assertIn("ran nothing: another task runner holds its run directory", out.getvalue())
        holder.stop()
        with self.stopped_at_once():
            task_runner.runner_main(["--run-dir", str(directory)])
        self.assertEqual(find_job_record(ctx.state_dir, "task")["status"], "cancelled")
        self.assertEqual(json.loads((directory / rd.EXIT).read_text())["status"], "finished")
        self.assertFalse(rd.run_lock_held(directory))  # Let go of at its end.

    def test_a_prestarted_runner_and_its_bridge_never_replace_runner_json_at_once(self):
        # T740 (ByteFuse): the Bridge wrote runner.json after the handoff and the prestarted runner wrote it again on
        # taking it; a read of the file that met the second replace failed, the Bridge took the runner for dead, started
        # a second one 2 s later, and that one's failure card took the reply key of the first. Here the runner's replace
        # of runner.json is slow and fails the reads made meanwhile, as on ByteFuse.
        app, job, ctx = self.bridge_job()
        directory = run_files(ctx.state_dir, "task")  # As prestart_runner makes it.
        runtime = SimpleNamespace(owner_open_id="owner", script_path=REPO / "bin" / "covey_bridge.py",
                                  cli_argv=["--profile", "test-bot"])
        argv = ["--run-dir", str(directory), "--await-handoff", "n1"]
        runner = threading.Thread(target=task_runner.runner_main, name="runner", args=(argv,))
        # This process stands in for the runner's, whose command line names its run directory (runner_alive asks).
        real_bytes, cmdline = Path.read_bytes, Path(f"/proc/{os.getpid()}/cmdline")
        stand_in = lambda path: ("\0".join([sys.executable, "-B", "covey_runner.py", *argv]) + "\0").encode() \
            if Path(path) == cmdline else real_bytes(path)
        seen = []
        with self.stopped_at_once(), slow_replaces(directory / rd.RUNNER, 0.5) as replaced, \
                mock.patch.object(Path, "read_bytes", stand_in):
            runner.start()
            job.early_runner = SimpleNamespace(process=mock.Mock(pid=os.getpid(), **{"poll.return_value": None}),
                                               nonce="n1", started=time.monotonic())
            runner_spawn.start_runner(runtime, app.spec.name, job,
                                      SimpleNamespace(state_dir=ctx.state_dir, default_workdir=self.work))
            deadline = time.monotonic() + 30
            while not (directory / rd.EXIT).exists() and runner.is_alive() and time.monotonic() < deadline:
                seen.append(rd.runner_alive(directory))  # As the Bridge's watch_until_exit asks, from the handoff on.
                time.sleep(0.005)
            runner.join(30)
        self.assertFalse(runner.is_alive())
        self.assertEqual(json.loads((directory / rd.EXIT).read_text())["status"], "finished")
        self.assertTrue(seen)
        self.assertNotIn(False, seen)
        self.assertEqual(replaced, [threading.main_thread().name])  # The Bridge's write, before the handoff: the only one.
        self.assertEqual(find_job_record(ctx.state_dir, "task")["status"], "cancelled")

    def handoff(self):
        """Job #7's run directory with its handoff, as the Bridge leaves it for the runner."""
        app, job, ctx = self.bridge_job()
        argv = ["--workdir", str(self.work), "--state-dir", str(self.root / "state"), "--profile", "test-bot"]
        directory = run_files(ctx.state_dir, "task", folders=(rd.INBOX, rd.ACKS), handoff={
            "harness": app.spec.name, "argv": argv, "owner": "owner",
            "script_path": str(REPO / "bin/covey_bridge.py"),
            "job": runner_spawn.dump_job(job)})
        return directory, ctx

    def test_the_runner_runs_the_job_and_takes_a_forwarded_stop(self):
        directory, ctx = self.handoff()
        started = threading.Event()

        def generate(self, ctx, job, session_id, progress):
            started.set()
            job.cancel_event.wait(20)
            from covey.core.harness import JobCancelled
            raise JobCancelled("任务已停止")
        from covey.app.runs import Runs
        from covey.app.runtime import Runtime
        from covey.feishu.messenger import Messenger
        sent, logs = [], []
        with mock.patch.object(Runs, "generate_reply_for_worker", generate), \
                mock.patch.object(Runtime, "log", lambda self, message: logs.append(message)), \
                mock.patch.object(Messenger, "send_message", lambda self, *a, **k: sent.append(a[2]) or True), \
                mock.patch("signal.signal"):
            runner = threading.Thread(target=task_runner.runner_main, args=(["--run-dir", str(directory)],))
            runner.start()
            self.assertTrue(started.wait(20))
            runner_ipc.write_inbox(directory, {"kind": "card_event", "event": {
                "operator_id": "owner", "chat_id": "chat", "message_id": "card-7", "event_id": "stop-1",
                "action_value": {"kind": "bridge_job_stop", "job_id": 7}}})
            runner.join(30)
        self.assertFalse(runner.is_alive())
        record = find_job_record(ctx.state_dir, "task")
        self.assertEqual(record["status"], "cancelled")
        self.assertEqual(json.loads((directory / rd.EXIT).read_text())["status"], "finished")
        registered = json.loads((directory / rd.RUNNER).read_text())  # The runner records itself first.
        self.assertEqual((registered["pid"], registered["key"], registered["job_id"]), (os.getpid(), "task", 7))
        self.assertEqual(list((directory / rd.INBOX).glob("*.json")), [])
        self.assertTrue(any("停止" in text for text in sent))

    def test_a_sigterm_interrupts_the_job_and_leaves_it_to_the_bridge(self):
        # T331 #2407: a container's shutdown SIGTERMs every process. Read as the stop button, the task ended
        # "已停止" and its card offered only 重新投递结果.
        directory, ctx = self.handoff()
        started, handlers = threading.Event(), {}

        def generate(self, ctx, job, session_id, progress):
            started.set()
            job.cancel_event.wait(20)
            raise JobCancelled("任务已停止")
        from covey.app.runs import Runs
        from covey.feishu.messenger import Messenger
        sent = []
        with mock.patch.object(Runs, "generate_reply_for_worker", generate), \
                mock.patch.object(Runtime, "log", lambda self, message: None), \
                mock.patch.object(Messenger, "send_message", lambda self, *a, **k: sent.append(a[2]) or True), \
                mock.patch("signal.signal", lambda signum, handler: handlers.setdefault(signum, handler)):
            runner = threading.Thread(target=task_runner.runner_main, args=(["--run-dir", str(directory)],))
            runner.start()
            self.assertTrue(started.wait(20))
            handlers[signal.SIGTERM](signal.SIGTERM, None)
            runner.join(30)
        self.assertFalse(runner.is_alive())
        record = find_job_record(ctx.state_dir, "task")
        self.assertEqual((record["status"], record.get("final_text")), ("running", None))  # Nothing settled or sent.
        self.assertIn("SIGTERM", record["status_message"])
        self.assertEqual(json.loads((directory / rd.EXIT).read_text())["status"], "interrupted")
        self.assertFalse(any("停止" in text for text in sent))
        [marked] = recover_stale_jobs(ctx.state_dir, record["backend"])  # As the next Bridge's start does.
        self.assertEqual((marked["status"], retry_label(marked)), ("interrupted", "检查并继续"))

    def test_a_sigterm_after_a_stop_click_keeps_the_stop(self):
        stopped, running = (BridgeReplyJob(job_id=n, user_text="", chat_id="c", message_id=f"m{n}", timeout=0)
                            for n in (1, 2))
        stopped.cancel_event.set()
        for job in (stopped, running):
            task_runner.interrupt(job)
        self.assertEqual((stopped.interrupted, running.interrupted, running.cancel_event.is_set()), (False, True, True))

    def test_a_prestarted_runner_waits_for_the_handoff_that_names_it_then_runs_the_job(self):
        directory, ctx = self.handoff()
        handoff = json.loads((directory / rd.HANDOFF).read_text())

        def generate(self, ctx, job, session_id, progress):
            from covey.core.harness import JobCancelled
            raise JobCancelled("任务已停止")
        from covey.app.runs import Runs
        from covey.feishu.messenger import Messenger
        with mock.patch.object(Runs, "generate_reply_for_worker", generate), \
                mock.patch.object(Runtime, "log", lambda self, message: None), \
                mock.patch.object(Messenger, "send_message", lambda self, *a, **k: True), \
                mock.patch("signal.signal"):
            runner = threading.Thread(target=task_runner.runner_main,
                                      args=(["--run-dir", str(directory), "--await-handoff", "n1"],))
            runner.start()  # The handoff there names no runner (a bridge's own spawn): not taken.
            time.sleep(0.3)
            self.assertFalse((directory / rd.RUNNER).exists())
            run_files(directory, handoff={**handoff, "prestart": "n1"})
            runner.join(30)
        self.assertFalse(runner.is_alive())
        self.assertEqual(json.loads((directory / rd.RUNNER).read_text())["pid"], os.getpid())
        self.assertEqual(find_job_record(ctx.state_dir, "task")["status"], "cancelled")
        self.assertEqual(json.loads((directory / rd.EXIT).read_text())["status"], "finished")

    def test_the_runner_locks_its_cards_across_processes_while_it_runs(self):
        # Repo review E4: the bot locks the same cards; restored afterwards (runner_main also runs in a test process).
        directory, ctx = self.handoff()
        from covey.core import locks
        seen = []

        def generate(self, ctx, job, session_id, progress):
            seen.append(locks._cards)
            from covey.core.harness import JobCancelled
            raise JobCancelled("任务已停止")
        from covey.app.runs import Runs
        from covey.feishu.messenger import Messenger
        with mock.patch.object(Runs, "generate_reply_for_worker", generate), \
                mock.patch.object(Runtime, "log", lambda self, message: None), \
                mock.patch.object(Messenger, "send_message", lambda self, *a, **k: True), \
                mock.patch("signal.signal"):
            self.assertEqual(task_runner.runner_main(["--run-dir", str(directory)]), 0)
        self.assertEqual(seen, [ctx.state_dir / "cards"])
        self.assertIsNone(locks._cards)

    def test_the_runner_warms_its_sdk_client_at_start_and_a_failure_there_changes_nothing(self):
        directory, ctx = self.handoff()
        calls = []

        def client(profile):
            calls.append((threading.current_thread().name, profile))
            if threading.current_thread().name == background.WARMUP:
                time.sleep(0.3)  # Still building when the job ends: the runner waits for it.
            raise ValueError("SDK profile unavailable")

        def generate(self, ctx, job, session_id, progress):
            from covey.core.harness import JobCancelled
            raise JobCancelled("任务已停止")
        from covey.app.runs import Runs
        from covey.feishu.messenger import Messenger
        logs = []
        with mock.patch.object(Runs, "generate_reply_for_worker", generate), \
                mock.patch.object(Runtime, "log", lambda self, message: logs.append(message)), \
                mock.patch.object(Messenger, "send_message", lambda self, *a, **k: True), \
                mock.patch("signal.signal"), \
                mock.patch.dict(os.environ, {"COVEY_CARD_TRANSPORT": "sdk"}), \
                mock.patch("covey.feishu.sdk.client", client):
            task_runner.runner_main(["--run-dir", str(directory)])
            self.assertFalse(any(t.name == background.WARMUP for t in threading.enumerate()))
        self.assertEqual([c for c in calls if c[0] == background.WARMUP], [(background.WARMUP, "test-bot")])
        self.assertIn("CardKit SDK warmup unavailable: ValueError", logs)
        self.assertEqual(find_job_record(ctx.state_dir, "task")["status"], "cancelled")
        self.assertEqual(json.loads((directory / rd.EXIT).read_text())["status"], "finished")


class InterruptedTurnTests(UnifiedCase):
    """A SIGTERM (app/runner.py ``interrupt``) during a Claude turn, as a container's shutdown sends it."""

    def setUp(self):
        super().setUp()
        self.claude = self.app.apps["claude"]
        self.claude.cards.update_record_elements = mock.Mock(return_value=True)

    def turn(self, mid, run):
        """A Claude task in its own topic whose backend does ``run(request, job)``: its record."""
        self.receive(mid, "initial")
        _, ctx = self.route(mid)
        self.app.topics.select_harness(ctx, "chat", "claude")
        app, self.topic_ctx = self.route(mid)
        app.submission.start_workspace(self.topic_ctx, "chat", self.topic_ctx.workspace_name)
        job = self.app.runtime.jobs.queue.get_nowait()
        backend = self.claude.harness.backend = RecordingBackend()
        backend.run = lambda request: run(request, job)
        app.process_reply_job(job, app.harness.context)
        return find_job_record(self.ctx.state_dir, job.message_id)

    @staticmethod
    def reply(request, text):
        """Claude's root reply, published as its candidate."""
        reply = VerifiedRootReply("claude", request.inputs.run_id, SID, ("synthetic-user",), ("synthetic-root",), text)
        request.callbacks.verified_reply(reply)
        return reply

    def test_the_candidate_is_withdrawn_and_check_and_continue_runs_the_task_again(self):
        # Claude waits for its subtasks after a reply when the container stops. Its runtime answers the
        # stop by raising, or with that reply as the last complete one: neither is settled or authorized.
        def raised(request, job):
            self.reply(request, "Waiting for the subtasks.")
            task_runner.interrupt(job)
            raise JobCancelled("Claude task cancelled")

        def returned(request, job):
            reply = self.reply(request, "Waiting for the subtasks.")
            task_runner.interrupt(job)
            return RunResult(reply.text, SID, True, reply, ExecutionOutcome(
                "cancelled", "cancelled", True, True, True, reply_fingerprint=reply.fingerprint))
        for mid, run in (("raised", raised), ("returned", returned)):
            with self.subTest(mid):
                record = self.turn(mid, run)
                self.assertEqual((record["status"], record["verified_reply_state"], record.get("final_text")),
                                 ("running", "invalidated", None))
                [marked] = recover_stale_jobs(self.ctx.state_dir, "claude")  # As the next Bridge's start does.
                self.assertEqual(retry_label(marked), "检查并继续")
                self.claude.controls.task_action(self.topic_ctx, {"chat_id": "chat", "event_id": mid, "action_value": {
                    "kind": "bridge_job_retry", "topic_id": marked["topic_id"], "job_id": marked["job_id"]}})
                self.assertIn("本话题任务已重新入队", self.said()[-1])
                again = self.app.runtime.jobs.queue.get_nowait()
                self.assertEqual((again.user_text, again.recovery_context["previous_status"]), ("initial", "interrupted"))
        self.assertFalse(any("已停止" in text for text in self.said()))

    def test_a_sigterm_after_claude_finished_leaves_its_answer_to_be_delivered(self):
        def run(request, job):
            reply = self.reply(request, "The answer.")
            task_runner.interrupt(job)  # After the backend finished, before the worker settled the turn.
            return RunResult(reply.text, SID, False, reply, ExecutionOutcome(
                "completed", "", True, True, True, reply_fingerprint=reply.fingerprint))
        record = self.turn("task", run)
        self.assertEqual((record["status"], record["execution_status"], record["delivery_ok"]), ("done", "done", True))
        self.assertIn("The answer.", "\n".join(self.said()))


class SDKWarmupTests(unittest.TestCase):
    """feishu/sdk.py warm_sdk: a task runner's SDK client, built on a thread while its job starts (``client``)."""

    def test_a_card_write_during_the_warmup_gets_the_one_client_it_builds(self):
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError:
            self.skipTest("cryptography not installed")
        from covey.feishu import sdk
        builds, warmed = [], []

        class Client:
            def __init__(self, app_id, secret, domain):
                time.sleep(0.3)  # lark_oapi's import and build: 2.7-3.7 s live (S32).
                builds.append(threading.current_thread().name)

            def warm(self):  # Its HTTP stack, a connection and the token (S48).
                warmed.append(threading.current_thread().name)
        core = SimpleNamespace(FEISHU_DOMAIN="https://open.feishu.cn", LARK_DOMAIN="https://open.larksuite.com")
        with tempfile.TemporaryDirectory() as tmp:
            home, data = Path(tmp) / "home", Path(tmp) / "data" / "lark-cli"
            (home / ".lark-cli").mkdir(parents=True)
            data.mkdir(parents=True)
            key, nonce = os.urandom(32), os.urandom(12)
            files = {
                home / ".lark-cli/config.json": json.dumps({"apps": [{"name": "named", "appId": "cli_abc", "brand": "feishu",
                    "appSecret": {"source": "keychain", "id": "appsecret:cli_abc"}}]}).encode(),
                data / "appsecret_cli_abc.enc": nonce + AESGCM(key).encrypt(nonce, b"not-a-real-secret", None),
                data / "master.key": key,
            }
            for path, body in files.items():
                path.write_bytes(body)
                path.chmod(0o600)
            logs = []
            with mock.patch.dict(os.environ, {"COVEY_CARD_TRANSPORT": "sdk", "LARKSUITE_CLI_DATA_DIR": str(data.parent)}), \
                    mock.patch.object(sdk.Path, "home", return_value=home), \
                    mock.patch.dict(sdk._clients, clear=True), \
                    mock.patch.object(sdk, "_core", return_value=core), mock.patch.object(sdk, "_Client", Client):
                before = set(threading.enumerate())
                warmup = sdk_warm("named", logs.append)
                written = sdk.client("named")  # A card write while the warm-up builds.
                task_runner._wait_background(before)  # The runner's exit waits for the warm-up.
                self.assertFalse(warmup.is_alive())
                self.assertIs(sdk.client("named"), written)
        self.assertEqual((warmup.name, len(builds), logs, warmed), (background.WARMUP, 1, [], [background.WARMUP]))

    def test_a_warmup_failure_is_logged_by_its_type_and_without_sdk_there_is_none(self):
        logs = []
        with mock.patch.dict(os.environ, {"COVEY_CARD_TRANSPORT": "sdk"}), \
                mock.patch("covey.feishu.sdk.client", side_effect=RuntimeError("https://open.feishu.cn/x?t=1")):
            sdk_warm("named", logs.append).join(5)
        self.assertEqual(logs, ["CardKit SDK warmup unavailable: RuntimeError"])
        with mock.patch.dict(os.environ, {"COVEY_CARD_TRANSPORT": "cli"}), \
                mock.patch("covey.feishu.sdk.client") as client:
            self.assertIsNone(sdk_warm("named", logs.append))
        client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
