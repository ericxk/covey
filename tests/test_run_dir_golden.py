"""The runs/<任务键>/ protocol between a Bridge and its task runners, frozen in tests/golden/run_dir.json.

Every release's Bridge reads what every other release's runner writes, and back (AGENTS.md rule 10), so
the file names, JSON keys, file modes, op ids, message formats and ack semantics of core/run_dir.py,
and of the handoff, runner, wake and queued files app/runner_spawn.py, app/runner_ipc.py and
app/detached.py write through it, never change by accident. ``record`` drives them on fixed inputs with time and randomness pinned; the test compares the
result with the golden file. A deliberate protocol change: regenerate it with
``GOLDEN_RUN_DIR=update python3 -B -m unittest discover -s tests -p test_run_dir_golden.py``, review the diff, and run
scripts/cross_release.py against the live release (docs/testing.md).
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
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support import REPO, make_context, make_job
from covey.app import detached, runner_ipc, runner_spawn
from covey.core import deliveries, run_dir as rd
from covey.core.state import write_json
from covey.core.steer import SteerNotReady, SteerRefused

GOLDEN = REPO / "tests" / "golden" / "run_dir.json"
UPDATE = os.environ.get("GOLDEN_RUN_DIR") == "update"
NOW = 1793800000.25


class Pinned:
    """time.time_ns, secrets.token_hex and time.time with fixed, increasing values."""

    def __init__(self):
        self.ns, self.hex = 1793800000000000000, 0

    def time_ns(self):
        self.ns += 1000
        return self.ns

    def token_hex(self, n=32):
        self.hex += 1
        return f"{self.hex:0{2 * n}x}"[-2 * n:]

    def __enter__(self):
        self.patches = [mock.patch.object(rd.time, "time_ns", self.time_ns),
                        mock.patch.object(rd.secrets, "token_hex", self.token_hex),
                        mock.patch.object(rd.time, "time", lambda: NOW)]
        for patch in self.patches:
            patch.start()
        return self

    def __exit__(self, *exc):
        for patch in reversed(self.patches):
            patch.stop()


class Recorder:
    def __init__(self, scratch: Path):
        self.scratch = scratch
        # pid of a process identity seen while recording: its name and identity. A value is replaced by the
        # placeholder of the process whose record it is in, not looked up by itself: two processes started in
        # the same clock tick share a start_ticks value, and the child's would then read as the other's.
        self.processes = {}

    def norm(self, value, key=None, owner=None):
        if isinstance(value, dict):
            mine = self.processes.get(str(value.get("pid")))
            return {str(k): self.norm(v, str(k), mine) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.norm(v) for v in value]
        if owner and key in owner[1] and str(value) == str(owner[1][key]):
            return "<boot_id>" if key == "boot_id" else f"<{owner[0]}.{key}>"
        if isinstance(value, (bool, int, float)) or value is None:
            return value
        return str(value).replace(str(self.scratch), "<S>").replace(str(REPO), "<REPO>")

    def identity(self, value, name):
        """Placeholders for a process identity's values, which differ on every run."""
        if isinstance(value, dict) and value.get("pid"):
            self.processes[str(value["pid"])] = (name, {k: value[k] for k in ("pid", "start_ticks", "boot_id")
                                                        if value.get(k)})
        return value

    def outcome(self, fn, *args, **kwargs):
        try:
            return ["ok", self.norm(fn(*args, **kwargs))]
        except Exception as exc:  # noqa: BLE001
            return ["raised", type(exc).__name__, self.norm(str(exc))]

    def files(self, folder):
        """Every file and directory under ``folder``: name, mode and (JSON) content."""
        folder = Path(folder)
        if not folder.exists():
            return None
        found = []
        for path in sorted(folder.rglob("*")):
            name = str(path.relative_to(folder))
            mode = oct(path.stat().st_mode & 0o7777)
            if path.is_dir():
                found.append([name + "/", mode])
                continue
            try:
                content = json.loads(path.read_text())
            except ValueError:
                content = path.read_text()
            found.append([name, mode, self.norm(content)])
        return found


def made(path: Path, text: str) -> None:
    """A file the test itself puts in place, mode 0o600 whatever the caller's umask: the golden records
    every mode, and a mode in it must come from the code under test, not from the umask."""
    path.write_text(text)
    path.chmod(0o600)


def answer(folder: Path, reply_folder: Path, replies: list, result: list) -> threading.Thread:
    """A thread answering the next ``len(replies)`` messages in ``folder``, each with its reply (None:
    no answer); what each message said goes to ``result``."""
    def serve():
        pending = list(replies)
        deadline = time.monotonic() + 60
        while pending and time.monotonic() < deadline:
            for payload in rd.take(folder):
                result.append(payload)
                reply = pending.pop(0)
                if reply is not None:
                    rd.ack(reply_folder.parent, payload["id"], folder=reply_folder.name, **reply)
            time.sleep(0.01)
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return thread


def record(scratch: Path) -> dict:
    r = Recorder(scratch)
    out = {}

    # Names: every release looks for exactly these.
    out["names"] = {name: getattr(rd, name) for name in ("RUNS", "QUEUED", "HANDOFF", "RUNNER", "WAKE", "DONE", "EXIT",
                                                         "LOG", "INBOX", "ACKS", "REQUESTS", "REPLIES")}
    out["names"]["OP"] = rd.OP.pattern
    out["names"]["WAKE_SIGNAL"] = runner_ipc.WAKE_SIGNAL.name

    # run_dir and queued_path: absolute and relative state dirs, odd keys.
    keys = ["om_abc", "", "a.b", "../x", "ü-key", "x" * 200, ".hidden", "a/b", "-", "job.json"]
    rows = []
    here = os.getcwd()
    os.chdir(scratch)
    try:
        for state in (scratch / "abs-state", Path("rel-state")):
            for key in keys:
                for name in ("abs-state", "rel-state"):
                    shutil.rmtree(scratch / name, ignore_errors=True)
                rows.append([r.norm(state), key, r.outcome(rd.run_dir, state, key),
                             (scratch / state / "jobs").is_dir(), r.outcome(rd.queued_path, state, key)])
    finally:
        os.chdir(here)
    out["paths"] = rows

    # Messages: post, ack, await_reply, take.
    run = scratch / "run"
    for folder in (rd.INBOX, rd.ACKS, rd.REQUESTS, rd.REPLIES):
        (run / folder).mkdir(parents=True)
        (run / folder).chmod(0o700)
    with Pinned():
        ops = [rd.post(run / rd.INBOX, {"kind": "steer", "text": "中文", "id": "ignored"}),
               rd.post(run / rd.REQUESTS, {"kind": "enqueue_answer", "deadline": 1.5}),
               rd.post(run / rd.INBOX, {})]
        out["new_op"] = rd.new_op()
    out["ops"] = ops
    out["after_post"] = r.files(run)
    rd.ack(run, ops[0], True)
    rd.ack(run, ops[1], False, "错误", folder=rd.REPLIES, job_id=7)
    rd.ack(run, "x-1", True, held=True, error="e", refused=False)
    out["after_ack"] = r.files(run)
    out["await_hit"] = rd.await_reply(run / rd.ACKS / (ops[0] + ".json"), time.monotonic() + 5)
    made(run / rd.ACKS / "torn.json", "{")
    out["await_torn"] = rd.await_reply(run / rd.ACKS / "torn.json", time.monotonic() + 0.25)
    out["await_missing"] = rd.await_reply(run / rd.ACKS / "none.json", time.monotonic() + 0.25)
    out["await_past"] = rd.await_reply(run / rd.REPLIES / (ops[1] + ".json"), time.monotonic() - 1)
    made(run / rd.INBOX / "0-bad.json", "[1,")
    made(run / rd.INBOX / "1-list.json", "[1, 2]")
    made(run / rd.INBOX / "note.txt", "x")
    out["take"] = list(rd.take(run / rd.INBOX))
    out["take_missing"] = list(rd.take(run / "nothing"))
    out["after_take"] = r.files(run)
    out["op_time"] = [[repr(v), r.outcome(rd.op_time, v)] for v in
                      [None, "", "123-ab", "123", "abc-1", 123, 0, "0-x", "١٢٣-ab", "²-x", " 1-a", "-1-a", ops[0]]]

    # done.json and exit.json.
    done = scratch / "done"
    done.mkdir(mode=0o700)
    done.chmod(0o700)
    with Pinned():
        rd.mark_done(done, "finished")
        out["mark_done_missing"] = r.outcome(rd.mark_done, scratch / "missing-done" / "dir", "x")
        out["read_exit_none"] = rd.read_exit(done)
        rd.mark_exit(done, "error:RuntimeError")
        out["mark_exit_missing"] = r.outcome(rd.mark_exit, scratch / "missing-exit" / "dir", "x")
    out["done_files"] = r.files(done)
    out["read_exit"] = rd.read_exit(done)
    for name, text in (("torn", "{"), ("list", "[1]"), ("null", "null")):
        folder = scratch / ("exit-" + name)
        folder.mkdir()
        made(folder / rd.EXIT, text)
        out["read_exit_" + name] = r.outcome(rd.read_exit, folder)

    out.update(record_liveness(r, scratch))
    out.update(record_handoff(r, scratch))
    out.update(record_wake(r, scratch))
    out.update(record_bridge_side(r, scratch))
    out.update(record_runner_side(r, scratch))

    # The Bridge's late look at an acknowledgement it stopped waiting for (core/deliveries.py).
    late = scratch / "late"
    write_json(late / rd.ACKS / "11-ab.json", {"ok": True, "error": ""})
    made(late / rd.ACKS / "12-ab.json", "{")
    write_json(late / rd.INBOX / "12-ab.json", {"kind": "steer"})
    write_json(late / rd.INBOX / "13-ab.json", {"kind": "steer"})
    records = [{}, {"op": "11-ab"}, {"run_dir": str(late)}, {"op": "11-ab", "run_dir": str(late)},
               {"op": "11-AB", "run_dir": str(late)}, {"op": "11-ab\n", "run_dir": str(late)},
               {"op": "12-ab", "run_dir": str(late)}, {"op": "13-ab", "run_dir": str(late)},
               {"op": 11, "run_dir": str(late)}, {"op": "x-ab", "run_dir": str(late)}]
    out["late_before_exit"] = [r.outcome(deliveries._late_ack, x) for x in records]
    write_json(late / rd.EXIT, {"status": "finished"})
    out["late_after_exit"] = [r.outcome(deliveries._late_ack, x) for x in records]
    return r.norm(out)


def record_liveness(r: Recorder, scratch: Path) -> dict:
    """runner_info, runner_alive and live_keys over a live child of ours and runner.json files that are wrong."""
    out = {}
    runs = scratch / "state" / rd.RUNS
    child_dir = runs / "live"
    child_dir.mkdir(parents=True)
    script = "import time; time.sleep(120)"
    child = subprocess.Popen([sys.executable, "-c", script, "--run-dir", str(child_dir)])
    other = subprocess.Popen([sys.executable, "-c", script])
    try:
        deadline = time.monotonic() + 30  # Until both have exec'd: before that /proc shows our argv.
        while time.monotonic() < deadline and not all(
                script.encode() in Path(f"/proc/{p.pid}/cmdline").read_bytes() for p in (child, other)):
            time.sleep(0.02)
        identity = r.identity(runner_ipc.process_identity(child.pid), "child")
        other_identity = r.identity(runner_ipc.process_identity(other.pid), "other")
        cases = {
            "live": {**identity, "key": "live"},
            "no-key": dict(identity),
            "wrong-ticks": {**identity, "start_ticks": "1", "key": "wrong-ticks"},
            "wrong-boot": {**identity, "boot_id": "x", "key": "wrong-boot"},
            "pid-str": {**identity, "pid": str(child.pid), "key": "pid-str"},
            "argv-without-dir": {**other_identity, "key": "argv-without-dir"},
            "gone": {"pid": 999999999, "key": "gone"},
            "list": [1, 2],
            "empty-list": [],
        }
        for name, value in cases.items():
            write_json(runs / name / rd.RUNNER, value)
        (runs / "torn").mkdir()
        made(runs / "torn" / rd.RUNNER, "{")
        (runs / "none").mkdir()
        made(runs / "file.txt", "x")
        names = sorted(p.name for p in runs.iterdir())
        out["runner_info"] = {n: r.outcome(rd.runner_info, runs / n) for n in names}
        out["runner_alive"] = {n: r.outcome(rd.runner_alive, runs / n) for n in names}
        here = os.getcwd()
        os.chdir(scratch)
        try:
            out["runner_alive_relative"] = r.outcome(rd.runner_alive, child_dir.relative_to(scratch))
        finally:
            os.chdir(here)
        out["runner_alive_trailing"] = r.outcome(rd.runner_alive, str(child_dir) + "/")
        out["live_keys_with_list"] = r.outcome(lambda: sorted(rd.live_keys(scratch / "state")))
        shutil.move(str(runs / "list"), str(scratch / "list-aside"))
        out["live_keys"] = sorted(rd.live_keys(scratch / "state"))
        out["live_keys_detached"] = sorted(detached.live_keys(scratch / "state"))
        out["live_keys_given_alive"] = sorted(rd.live_keys(scratch / "state", lambda d: Path(d).name in
                                                           {"wrong-ticks", "no-key", "gone"}))
        out["live_keys_missing"] = sorted(rd.live_keys(scratch / "no-state"))
    finally:
        for process in (child, other):
            process.kill()
            process.wait()
    return out


def record_handoff(r: Recorder, scratch: Path) -> dict:
    """What a Bridge leaves in a run directory and queued/ when it hands a job to a runner."""
    out = {}
    state, work = scratch / "hand-state", scratch / "hand-work"
    me = r.identity(runner_ipc.process_identity(os.getpid()), "bridge")
    runtime = SimpleNamespace(owner_open_id="ou_owner", script_path=REPO / "bin" / "covey_bridge.py",
                              cli_argv=["--profile", "p", "--harness-config", "harnesses.json"])
    ctx = SimpleNamespace(state_dir=state, default_workdir=work)
    job = make_job(job_id=7, user_text="请做这件事", chat_id="oc_chat", message_id="om_task", model="m", effort="high",
                   workdir=work, topic_id="t1", harness_id="codex")
    job.topic_label = "T1"  # An undeclared attribute: carried in "extras".
    with mock.patch.object(runner_spawn.time, "time", lambda: NOW):
        detached.remember_queued(state, "codex", job)
        saved = json.loads(rd.queued_path(state, "om_task").read_text())
        out["queued_file"] = [[row[0], row[1]] for row in r.files(state / rd.QUEUED)]
        out["queued_keys"] = [sorted(saved), saved["schema_version"], saved["harness"], sorted(saved["job"])]
        job.user_text = "请做这件事（补充）"
        detached.update_queued(state, job)
        out["queued_updated_text"] = json.loads(rd.queued_path(state, "om_task").read_text())["job"]["fields"]["user_text"]
        directory = rd.run_dir(state, "om_task")
        for stale in (rd.RUNNER, rd.EXIT, rd.WAKE, rd.DONE, "runner.log"):
            write_json(directory / stale, {"stale": True})
        write_json(directory / rd.INBOX / "1-aa.json", {"stale": True})
        made(directory / rd.INBOX / "keep.txt", "x")
        popen = mock.Mock(pid=os.getpid(), **{"poll.return_value": None})
        with mock.patch.object(runner_spawn.subprocess, "Popen", return_value=popen) as started, \
                mock.patch.object(runner_spawn.secrets, "token_hex", lambda n: "ab" * n):
            here = os.getcwd()
            (scratch / "cwd").mkdir()
            os.chdir(scratch / "cwd")  # The Bridge's relative --harness-config is made absolute against it.
            try:
                out["start_runner"] = r.outcome(runner_spawn.start_runner, runtime, "codex", job, ctx)
            finally:
                os.chdir(here)
            command, kwargs = started.call_args.args[0], started.call_args.kwargs
        out["runner_command"] = [Path(command[0]).name == Path(sys.executable).name, *command[1:]]
        out["runner_popen"] = {"cwd": kwargs["cwd"], "start_new_session": kwargs["start_new_session"]}
        handoff = json.loads((directory / rd.HANDOFF).read_text())
        out["handoff_top"] = {k: v for k, v in handoff.items() if k != "job"}
        out["handoff_job_keys"] = sorted(handoff["job"])
        out["handoff_job_fields"] = sorted(handoff["job"]["fields"])
        out["handoff_job_values"] = {k: handoff["job"]["fields"][k] for k in (
            "job_id", "user_text", "chat_id", "message_id", "workdir", "topic_id", "harness_id", "execution_started")}
        out["handoff_job_extras"] = handoff["job"]["extras"]
        out["handoff_job_rest"] = {k: handoff["job"][k] for k in ("progress", "cancelled")}
        out["runner_json"] = json.loads((directory / rd.RUNNER).read_text())
        out["run_dir_after_handoff"] = [row[:2] for row in r.files(directory)]
        out["queued_after_handoff"] = [row[:2] for row in r.files(state / rd.QUEUED)]
        # A prestarted runner: named in its handoff by the nonce it was started with.
        job2 = make_job(job_id=8, user_text="x", chat_id="oc_chat", message_id="om_pre")
        runtime.detached = lambda: True
        with mock.patch.object(runner_spawn.subprocess, "Popen", return_value=popen) as started, \
                mock.patch.object(runner_spawn.secrets, "token_hex", lambda n: "cd" * n):
            runner_spawn.prestart_runner(runtime, job2, ctx)
            out["prestart_command_tail"] = started.call_args.args[0][3:]
            pre = runner_spawn.start_runner(runtime, "codex", job2, ctx)
        handoff2 = json.loads((pre / rd.HANDOFF).read_text())
        out["prestart_handoff_nonce"] = handoff2.get("prestart")
        out["await_handoff"] = [runner_spawn.await_handoff(pre, "cd" * 8, wait=0) == handoff2,
                                runner_spawn.await_handoff(pre, "other", wait=0)]
    out["bridge_identity_keys"] = sorted(me or {})
    return out


def record_wake(r: Recorder, scratch: Path) -> dict:
    """wake.json, written by a runner that takes the wake signal, and the Bridge's check before it sends one."""
    out = {}
    directory = scratch / "wake"
    directory.mkdir(mode=0o700)
    previous = signal.getsignal(runner_ipc.WAKE_SIGNAL)
    previous_fd = signal.set_wakeup_fd(-1)
    signal.set_wakeup_fd(previous_fd)
    fd = runner_ipc.accept_wake(directory)
    try:
        out["wake_accepted"] = fd is not None
        out["wake_file"] = r.files(directory)
        out["wake_sent"] = runner_ipc.wake_runner(directory)
        runner_ipc.await_wake(fd, 5)
        named = json.loads((directory / rd.WAKE).read_text())
        write_json(directory / rd.WAKE, {**named, "start_ticks": "1"})
        out["wake_sent_wrong_identity"] = runner_ipc.wake_runner(directory)
        made(directory / rd.WAKE, "{")
        out["wake_sent_torn"] = runner_ipc.wake_runner(directory)
        out["wake_sent_missing"] = runner_ipc.wake_runner(scratch / "no-wake")
    finally:
        runner_ipc.release_wake(fd)
        signal.signal(runner_ipc.WAKE_SIGNAL, previous)
        signal.set_wakeup_fd(previous_fd)
    return out


def record_bridge_side(r: Recorder, scratch: Path) -> dict:
    """How a Bridge reads a runner's acks to what it forwarded (supplements, answers, clicks) and replies
    to its requests."""
    out = {}
    directory = scratch / "bridge-run"
    for folder in (rd.INBOX, rd.ACKS, rd.REQUESTS, rd.REPLIES):
        (directory / folder).mkdir(parents=True)
    control = runner_ipc.RemoteControl(directory, True)
    replies = [{"ok": True, "error": ""}, {"ok": True, "held": True, "error": "还没准备好"},
               {"ok": False, "error": "这条补充不收", "refused": True}, {"ok": False, "error": "RuntimeError", "refused": False},
               {"ok": False, "error": "旧版拒绝"}, {"ok": False, "error": "RuntimeError"}, {"ok": False}, None]
    seen = []
    thread = answer(directory / rd.INBOX, directory / rd.ACKS, replies, seen)
    with mock.patch.object(runner_ipc, "ACK_TIMEOUT", 20.0), mock.patch.object(runner_ipc, "wake_runner", lambda d: False):
        results = []
        for number, reply in enumerate(replies):
            timeout = 20.0 if reply is not None else 0.3
            with mock.patch.object(runner_ipc, "ACK_TIMEOUT", timeout):
                results.append([reply, r.outcome(control.steer, f"补充{number}", f"ev-{number}")])
    thread.join(5)
    out["steer_acks"] = results
    out["steer_inbox"] = [{k: ("<OP>" if k == "id" else v) for k, v in p.items()} for p in seen]
    out["remote_control"] = {"remote": control.remote, "pause_for_question": control.pause_for_question,
                             "answer": r.outcome(control.answer, "q", {}), "close": control.close()}

    # A typed answer forwarded to the runner that asked the question.
    state = scratch / "fwd-state"
    token = "00112233445566ff"
    write_json(state / "questions" / f"{token}.json", {"token": token, "job_id": 7})
    runtime = SimpleNamespace(in_runner=False)
    ctx = SimpleNamespace(state_dir=state)
    replies = [{"ok": True}, {"ok": True, "local": True}, {"ok": False, "error": "已回答"}, {"ok": False}, None]
    seen = []
    thread = answer(directory / rd.INBOX, directory / rd.ACKS, replies, seen)
    results = []
    with mock.patch.object(runner_ipc, "runner_for", lambda *a, **k: directory), \
            mock.patch.object(runner_ipc, "wake_runner", lambda d: False):
        for number, reply in enumerate(replies):
            with mock.patch.object(runner_ipc, "ANSWER_TIMEOUT", 20.0 if reply is not None else 0.3):
                results.append([reply, r.outcome(runner_ipc.forward_answer, runtime, ctx, "oc_chat", token,
                                                  {"q": f"a{number}"}, f"ev-a{number}")])
        results.append(["bad token", r.outcome(runner_ipc.forward_answer, runtime, ctx, "oc_chat", "xyz", {}, "e")])
    thread.join(5)
    out["answer_acks"] = results
    out["answer_inbox"] = [{k: ("<OP>" if k == "id" else v) for k, v in p.items()} for p in seen]

    # A click on a job whose runner is alive: the whole event goes to its inbox, unanswered.
    from covey.core.jobs import upsert_job_record
    upsert_job_record(state, "om_task", job_id=7, topic_id="t1", chat_id="oc_chat", card_message_id="om_card")
    target = SimpleNamespace(state_dir=state, topic_id="t1")
    event = {"operator_id": "ou_owner", "chat_id": "oc_chat", "message_id": "om_card", "event_id": "ev-c"}
    with mock.patch.object(runner_ipc, "runner_for", lambda *a, **k: directory), \
            mock.patch.object(runner_ipc, "wake_runner", lambda d: False):
        out["click_forwarded"] = [
            runner_ipc.forward_card_action(runtime, target, event, {"kind": "no_such_kind", "job_id": 7}),
            runner_ipc.forward_card_action(runtime, target, event, {"kind": "no_such_kind"}),
            runner_ipc.forward_card_action(runtime, target, event, {"kind": "no_such_kind", "job_id": 99})]
    out["click_inbox"] = [{k: ("<OP>" if k == "id" else v) for k, v in p.items()} for p in rd.take(directory / rd.INBOX)]

    # A runner's request for a continuation, and the Bridge's reply.
    link = runner_ipc.RunnerLink(directory)
    task_ctx = SimpleNamespace(topic_id="t1", model="m", effort="high", work_mode="")
    replies = [{"ok": True, "job_id": 9}, {"ok": False, "error": "后续任务未能入队"}, {"ok": False}, None]
    seen = []
    thread = answer(directory / rd.REQUESTS, directory / rd.REPLIES, replies, seen)
    results = []
    with mock.patch.object(runner_ipc.time, "time", lambda: NOW):
        for number, reply in enumerate(replies):
            with mock.patch.object(runner_ipc, "ENQUEUE_TIMEOUT", 20.0 if reply is not None else -2.7):
                    made = r.outcome(lambda: vars(link.enqueue("codex", task_ctx, "后续", "oc_chat", f"om_c{number}",
                                                           {"why": "x"}, title="题", display="显示")))
            results.append([reply, made])
        task_ctx.work_mode = "focus"
        replies_mode = [{"ok": True, "job_id": 10}]
        thread.join(5)
        thread = answer(directory / rd.REQUESTS, directory / rd.REPLIES, replies_mode, seen)
        with mock.patch.object(runner_ipc, "ENQUEUE_TIMEOUT", 20.0):
            results.append([replies_mode[0], r.outcome(lambda: vars(link.enqueue("codex", task_ctx, "t", "oc_chat",
                                                                                 "om_c9", None)))])
    thread.join(5)
    out["enqueue_replies"] = results
    out["enqueue_requests"] = [{k: ("<OP>" if k == "id" else ("<DEADLINE>" if k == "deadline" else v))
                                for k, v in p.items()} for p in seen]

    # The Bridge serving requests: only live enqueue_answer ones; a failure is replied with its type.
    logs = []
    gateway = SimpleNamespace(apps={}, runtime=SimpleNamespace(log=logs.append))
    with Pinned():
        rd.post(directory / rd.REQUESTS, {"kind": "other"})
        rd.post(directory / rd.REQUESTS, {"kind": "enqueue_answer", "deadline": NOW - 10})
        failing = rd.post(directory / rd.REQUESTS, {"kind": "enqueue_answer", "deadline": NOW + 30, "topic_id": "nope",
                                                    "harness": "codex", "chat_id": "oc_chat"})
        with mock.patch.object(runner_ipc.time, "time", lambda: NOW), \
                mock.patch("covey.app.runner_ipc.requested", lambda *a: False):
            runner_ipc.serve_requests(gateway, None, make_job(message_id="om_task"), ctx, directory)
    out["serve_requests_replies"] = r.files(directory / rd.REPLIES)
    out["serve_requests_left"] = r.files(directory / rd.REQUESTS)
    out["serve_requests_failing_op"] = failing
    out["serve_requests_log"] = logs
    return out


def record_runner_side(r: Recorder, scratch: Path) -> dict:
    """How a runner takes its inbox and acknowledges each kind (take_inbox), with a fake app."""
    directory = scratch / "runner-run"
    for folder in (rd.INBOX, rd.ACKS):
        (directory / folder).mkdir(parents=True)
    calls = []
    job = make_job(job_id=7, message_id="om_task", topic_id="t1")
    registry = {"job": job}

    class Control:
        def steer(self, text, event_id):
            calls.append(["steer", text, event_id])
            if text == "refuse":
                raise SteerRefused("这条补充不收")
            if text == "boom":
                raise RuntimeError("boom")
            if text == "value":
                raise ValueError("说明给用户")
            if text == "later":
                raise SteerNotReady("还没准备好")

    def submit_answer(ctx, chat, token, fields, event_id):
        if fields.get("q") == "bad":
            raise ValueError("问题已结束")
        calls.append(["answer", chat, token, fields, event_id])

    stage = {"value": "running"}
    app = SimpleNamespace(
        harness=SimpleNamespace(context=make_context(scratch / "runner-ctx")),
        runtime=SimpleNamespace(jobs=SimpleNamespace(find=lambda job_id: registry["job"])),
        steering=SimpleNamespace(steer_stage=lambda j: stage["value"],
                                 hold_steer=lambda c, j, text, event_id: calls.append(["hold", text, event_id]),
                                 note_latest_request=lambda c, j, text, event_id: calls.append(["note", text])),
        questions=SimpleNamespace(submit_answer=submit_answer),
        interaction=SimpleNamespace(structured_harness_event=lambda c, j, kind, item, *a, **k: calls.append(
            ["note", kind, item["id"], item["title"], k])))
    logs = []
    gateway = SimpleNamespace(handle_card_action_event=lambda event, ctx: calls.append(["click", event]),
                              runtime=SimpleNamespace(log=logs.append))
    results = {}

    def deliver(name, *payloads, setup=None):
        with Pinned():
            ops = [rd.post(directory / rd.INBOX, payload) for payload in payloads]
        if setup:
            setup()
        runner_ipc.take_inbox(gateway, app, job, None, directory)
        acks = [directory / rd.ACKS / (op + ".json") for op in ops]  # Written before take_inbox returns.
        results[name] = {"acks": [r.norm(json.loads(path.read_text())) if path.exists() else None for path in acks],
                         "calls": list(calls), "left": r.files(directory / rd.INBOX)}
        for path in acks:
            path.unlink(missing_ok=True)
        calls.clear()

    job.backend_control = Control()
    deliver("steer", *({"kind": "steer", "text": t, "event_id": f"ev-{t}"} for t in ("ok", "refuse", "boom", "value",
                                                                                        "later")))
    deliver("steer_paused", {"kind": "steer", "text": "ok", "event_id": "ev-p"},
            setup=lambda: stage.update(value="paused"))
    stage["value"] = "running"
    job.backend_control = None
    deliver("steer_no_control", {"kind": "steer", "text": "ok", "event_id": "ev-n"})
    registry["job"] = None
    deliver("steer_ended", {"kind": "steer", "text": "ok", "event_id": "ev-e"})
    registry["job"] = job
    deliver("answer", {"kind": "answer", "chat_id": "oc_chat", "token": "t" * 16, "fields": {"q": "a"}, "event_id": "ev-a"},
            {"kind": "answer", "chat_id": "oc_chat", "token": "t" * 16, "fields": {"q": "bad"}, "event_id": "ev-b"},
            {"kind": "answer", "token": "t" * 16})
    deliver("click", {"kind": "card_event", "event": {"event_id": "ev-c", "message_id": "om_card"}}, {"kind": "card_event"})
    deliver("restart_no_trace", {"kind": "bridge_restarted", "at": NOW}, {"kind": "unknown"})
    job.tool_trace = {}
    deliver("restart", {"kind": "bridge_restarted", "at": NOW})
    return {"runner_take_inbox": results, "runner_take_inbox_log": logs}


def leaves(value, prefix=""):
    if isinstance(value, dict):
        for key in sorted(value):
            yield from leaves(value[key], f"{prefix}.{key}" if prefix else str(key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from leaves(item, f"{prefix}[{index}]")
        if not value:
            yield prefix, []
    else:
        yield prefix, value


class RunDirGoldenTests(unittest.TestCase):
    def test_the_run_directory_protocol_matches_the_golden_file(self):
        with tempfile.TemporaryDirectory() as root:
            scratch = Path(root).resolve()
            got = json.loads(json.dumps(record(scratch), ensure_ascii=False, default=str))
        if UPDATE:
            GOLDEN.parent.mkdir(exist_ok=True)
            GOLDEN.write_text(json.dumps(got, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
            self.skipTest(f"wrote {GOLDEN.relative_to(REPO)}")
        want = json.loads(GOLDEN.read_text())
        mine, theirs = dict(leaves(got)), dict(leaves(want))
        changed = [f"{key}: golden {theirs.get(key, '<absent>')!r}, now {mine.get(key, '<absent>')!r}"
                   for key in sorted(set(mine) | set(theirs)) if mine.get(key, "<absent>") != theirs.get(key, "<absent>")]
        if changed:
            self.fail("the runs/ protocol changed (every release reads it; AGENTS.md rule 10). If on purpose: "
                      "GOLDEN_RUN_DIR=update, review the diff, run scripts/cross_release.py.\n" + "\n".join(changed[:40]))


if __name__ == "__main__":
    unittest.main()
