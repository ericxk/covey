"""What a task's commands leave running once its Harness has closed: found by the run directory in their
environment (core/task_processes.py), the search tools ended and the rest named on the card (app/leftovers.py)."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from support import exec_tempdir, make_context
from support.fake_backends import FakeBackend
from support.processes import leftover_check
from support.world import build_claude_app

from covey.app.leftovers import leftover_note, stop_task_leftovers
from covey.cards.trace_view import BRIDGE_NOTE
from covey.core.jobs import BridgeReplyJob
from covey.core.task_processes import (TASK_DIR_ENV, Leftover, find_leftovers, is_search, short_command,
                                                stop_leftovers)

setUpModule, tearDownModule = leftover_check()
SLEEP = shutil.which("sleep")


def alive(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] not in {"Z", "X"}
    except (OSError, IndexError):
        return False


class LeftoverCase(unittest.TestCase):
    def setUp(self):
        self.run_dir = Path(tempfile.mkdtemp()) / "runs" / "om_job"
        self.addCleanup(shutil.rmtree, self.run_dir.parents[1], True)
        self.pids, self.children = [], []
        self.addCleanup(self._stop_started)

    def _stop_started(self):
        for pid in self.pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for proc in self.children:
            proc.wait(5)

    def env(self, run_dir=None):
        return {**os.environ, TASK_DIR_ENV: str(run_dir or self.run_dir)}

    def child(self, argv, env=None):
        """A process of this test's, as a Harness still open is its runner's."""
        proc = subprocess.Popen(argv, env=env or dict(os.environ), stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.children.append(proc)
        self.pids.append(proc.pid)
        return proc.pid

    def runner(self, argv, env=None):
        """The runner: started before anything it is to find, as a task runner is long before its commands
        (start times are in clock ticks, 10 ms)."""
        pid = self.child(argv, env)
        time.sleep(0.05)
        return pid

    def orphan(self, argv, env=None):
        """A process whose parent has exited, adopted by PID 1, as a command left behind by a closed Harness."""
        out = subprocess.run(["sh", "-c", '"$@" </dev/null >/dev/null 2>&1 & echo $!', "sh", *argv],
                             env=env or dict(os.environ), capture_output=True, text=True, timeout=10,
                             start_new_session=True)
        pid = int(out.stdout.strip())
        self.pids.append(pid)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:  # Until the shell's fork has become the command.
            try:
                if Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")[0] == argv[0].encode():
                    break
            except OSError:
                break
            time.sleep(0.01)
        return pid


class FindTests(LeftoverCase):
    def test_marked_processes_outside_the_runner_are_found_and_only_search_tools_ended(self):
        root = self.runner([SLEEP, "60"])  # Unmarked.
        search = self.orphan([SLEEP, "61"], self.env())
        server = self.orphan(["python3", "-c", "import time; time.sleep(62)"], self.env())
        other_task = self.orphan([SLEEP, "63"], self.env(self.run_dir.parent / "om_other"))
        unmarked = self.orphan([SLEEP, "64"])

        stopped, running = stop_leftovers(self.run_dir, root=root, search=frozenset({"sleep"}))

        self.assertEqual([(item.pid, item.name, item.outcome) for item in stopped], [(search, "sleep", "stopped")])
        self.assertEqual(stopped[0].command, f"{SLEEP} 61")
        self.assertFalse(alive(search))
        self.assertEqual([(item.pid, item.search, item.outcome) for item in running], [(server, False, "")])
        self.assertTrue(all(alive(pid) for pid in (root, server, other_task, unmarked)))

    def test_the_runner_what_runs_under_it_and_what_started_before_it_are_left_out(self):
        before = self.child([SLEEP, "60"], self.env())
        # The runner itself is marked, and so is the command it still waits for (a card write, an open Harness).
        root = self.runner(["sh", "-c", f"{SLEEP} 61 & wait"], self.env())
        deadline = time.monotonic() + 5
        while not subprocess.run(["pgrep", "-P", str(root)], capture_output=True).stdout and time.monotonic() < deadline:
            time.sleep(0.01)

        self.assertEqual(find_leftovers(self.run_dir, root=root, search=frozenset({"sleep"})), [])
        self.assertEqual(stop_leftovers(self.run_dir, root=root, search=frozenset({"sleep"})), ([], []))
        self.assertTrue(alive(before) and alive(root))

    def test_a_search_tool_that_ignores_sigterm_is_killed(self):
        root = self.runner([SLEEP, "60"])
        stubborn = self.orphan(["python3", "-c", "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                                "time.sleep(60)"], self.env())

        started = time.monotonic()
        stopped, running = stop_leftovers(self.run_dir, root=root, search=frozenset({"python3"}), grace=0.3)

        self.assertEqual([(item.pid, item.outcome) for item in stopped], [(stubborn, "stopped")])
        self.assertEqual(running, [])
        self.assertFalse(alive(stubborn))
        self.assertLess(time.monotonic() - started, 3)


class ClassifyTests(unittest.TestCase):
    def test_a_find_is_a_search_only_while_it_acts_on_nothing(self):
        self.assertTrue(is_search("rg", None))
        self.assertTrue(is_search("find", ["find", ".", "-name", "*.py"]))
        for action in ("-delete", "-exec", "-execdir", "-fprint"):
            self.assertFalse(is_search("find", ["find", ".", action, "x"]), action)
        self.assertFalse(is_search("find", None))  # Arguments unread: it may delete.
        self.assertFalse(is_search("python3", ["python3", "train.py"]))

    def test_commands_are_shown_without_credentials_and_long_runs(self):
        shown = short_command(["curl", "--token", "s3cr3t", "API_KEY=abc123", "-H", "a" * 48, "x" * 300], limit=60)
        self.assertNotIn("s3cr3t", shown)
        self.assertNotIn("abc123", shown)
        self.assertIn("<long>", shown)
        self.assertLessEqual(len(shown), 60)


class NoteTests(LeftoverCase):
    def item(self, pid, name, command, *, search=True, outcome="stopped", rss=0):
        return Leftover(pid, 1, name, rss, command, search, outcome)

    def test_the_card_line_names_what_was_ended_and_what_still_runs(self):
        stopped = [self.item(11, "rg", "rg --files state", rss=3 << 30), self.item(12, "grep", "grep -r x .", outcome="pending")]
        running = [self.item(21, "python3", "python3 train.py", search=False, outcome="")]

        note = leftover_note(stopped, running, "00:12")

        self.assertEqual(note.splitlines(), [
            "机器人 00:12 结束了本轮命令留下的 2 个搜索进程：rg（PID 11，3.0 GiB）rg --files state；"
            "grep（PID 12，0.0 GiB）grep -r x .。其中 1 个还没退出，可能卡在 NAS 读写上。",
            "本轮命令留下的 1 个进程仍在运行，没有处理：python3（PID 21，0.0 GiB）python3 train.py。"])
        many = [self.item(30 + n, "sleep", "sleep 9", search=False, outcome="") for n in range(7)]
        self.assertEqual(leftover_note([], many, "00:13"),
                         "机器人 00:13 检查：本轮命令留下的 7 个进程仍在运行，没有处理："
                         + "；".join(f"sleep（PID {30 + n}，0.0 GiB）sleep 9" for n in range(5)) + "；等共 7 个。")

    def test_a_runner_ends_its_turns_leftover_search_and_notes_it_on_the_card(self):
        with exec_tempdir() as bin_dir:
            rg = Path(bin_dir) / "rg"
            # A process named rg, which is a search tool. Not a link to sleep: where coreutils is one
            # multi-call binary (uutils, newer Ubuntu), a link named rg exits at once, "rg: not found".
            rg.symlink_to(sys.executable)
            search = self.orphan([str(rg), "-c", "import time; time.sleep(60)"], self.env())
            events, log = [], []
            interaction = SimpleNamespace(structured_harness_event=lambda *args, **kwargs: events.append((args, kwargs)))
            runtime = SimpleNamespace(log=log.append)
            job = SimpleNamespace(job_id=7, tool_trace=object())

            stop_task_leftovers(runtime, interaction, "ctx", job)  # Not in a runner: nothing is marked.
            self.assertTrue(alive(search))
            self.assertEqual((events, log), ([], []))

            with mock.patch.dict(os.environ, {TASK_DIR_ENV: str(self.run_dir)}):
                stop_task_leftovers(runtime, interaction, "ctx", job)

        self.assertFalse(alive(search))
        self.assertEqual(len(events), 1, log)
        (ctx, noted_job, kind, data, rpc), kwargs = events[0]
        self.assertEqual((ctx, noted_job, kind, rpc, kwargs), ("ctx", job, "progress_note", None, {"from_backend": False}))
        self.assertEqual(data["title"], BRIDGE_NOTE)
        self.assertIn(f"结束了本轮命令留下的 1 个搜索进程：rg（PID {search}，", data["text"])
        self.assertEqual(log, [f"task leftovers job=#7 stopped=1 pending=0 running=0 pids={search}"])


class TurnTests(LeftoverCase):
    """The turn in a runner: once the backend returns, or fails, its Harness is closed and the turn's leftovers go."""

    def turn(self, failing):
        case = self

        class LeavesASearch(FakeBackend):
            def run(self, request):
                self.left = case.orphan([str(case.rg), "60"], case.env())
                if failing:
                    raise RuntimeError("the backend failed")
                return super().run(request)

        backend = LeavesASearch()
        app = build_claude_app(backend)
        job = BridgeReplyJob(job_id=3, user_text="x", chat_id="oc", message_id="om3", timeout=0, history_snapshot=[])
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(os.environ, {TASK_DIR_ENV: str(self.run_dir)}):
            ctx = make_context(Path(root))
            if failing:
                with self.assertRaises(RuntimeError):
                    app.runs.generate_reply_for_worker(ctx, job, None, None)
            else:
                self.assertEqual(app.runs.generate_reply_for_worker(ctx, job, None, None).text, "ok")
        return backend.left

    def test_a_turn_ends_the_search_its_commands_left_also_when_the_backend_fails(self):
        with exec_tempdir() as bin_dir:
            self.rg = Path(bin_dir) / "rg"
            self.rg.symlink_to(SLEEP)
            for failing in (False, True):
                self.assertFalse(alive(self.turn(failing)), failing)


if __name__ == "__main__":
    unittest.main()
