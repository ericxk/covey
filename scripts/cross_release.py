#!/usr/bin/env python3
"""A Bridge and a task runner of two releases, talking through runs/<任务键>/ (AGENTS.md rule 10).

    python3 -B scripts/cross_release.py run --old <git ref> --out <dir> [--jobs N] [--scenarios a,b] [--variants a,b]
    python3 -B scripts/cross_release.py compare <dir> [--show N]

``run`` exports ``--old`` with git archive into <dir>/trees/, and runs each scenario below with the
new release (this checkout's working tree) and the old one: each Bridge phase is its own process
importing covey from one tree, and it starts a real task runner (runner_main of another
tree, in its own process and session, as detached._spawn starts one) over a state dir in <dir>; the
two talk only through <state>/runs/<job>/ and the job record. Replaced in each process: the backend's
run (scripted by command files), every Feishu call (logged, nothing sent; the PATH has no lark-cli,
and a connection to anything but loopback is refused and logged), and the Bridge's backend startup
check. The environment is cleared (env -i style): HOME, TMPDIR and the state dir are in <dir>, so
nothing reaches a live state dir or Feishu. <dir> is required and refused inside this checkout, $HOME
or /tmp. One JSON per run lands in <dir>/runs/.

``compare`` says whether every cross-release run equals its same-release runs, and whether each run's
Bridge and runner were the releases it names; exit status 0 when all are equal, 1 otherwise. Before a
change to shared core or to the runs/ protocol, run it against the live release (docs/testing.md).

Variants:
  main        handoff (prestarted runner), runner.json, wake, steer (ok, refused, failed, held), answer, card click,
              requests/replies (ok and failed), Bridge killed; next Bridge process adopts (notify_stale_jobs), bridge_restarted,
              steer through the adopted control, re-queued jobs, finish: done.json, release_done, exit.json, finish_detached.
  plain       main's first part with no prestart: start_runner spawns the runner; finish without a restart.
  stop        a stop click forwarded with forward_card_action.
  sigterm     SIGTERM to the runner: exit "interrupted", finish_detached's sigterm cause, automatic restart, recovery card.
  killed      SIGKILL to the runner: no exit.json, finish_detached, the notice when no card can be written.
  downkilled  Bridge killed, then the runner killed: the next Bridge finds it dead (report_unfinished from notify_stale_jobs).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TMP = Path("/tmp")  # Never an output: it is the system disk's.
SID = "11111111-1111-4111-8111-111111111111"
TOOL = "cross_release"  # meta.json's marker: a directory holding it is one of this script's.
# name: (Bridge that starts the runner, the runner's release, the Bridge after the restart)
SCENARIOS = {"base-old": ("old", "old", "old"), "base-new": ("new", "new", "new"),
             "new-bridge-old-runner": ("new", "old", "new"), "old-bridge-new-runner": ("old", "new", "old"),
             "deploy-old-to-new": ("old", "old", "new"), "rollback-new-to-old": ("new", "new", "old")}
# Each scenario against the same-release runs whose code it mixes; base-new against base-old says
# whether the two releases behave the same here at all.
PAIRS = [("new-bridge-old-runner", "base-old"), ("new-bridge-old-runner", "base-new"),
         ("old-bridge-new-runner", "base-old"), ("old-bridge-new-runner", "base-new"),
         ("deploy-old-to-new", "base-old"), ("deploy-old-to-new", "base-new"),
         ("rollback-new-to-old", "base-new"), ("rollback-new-to-old", "base-old"), ("base-new", "base-old")]
VARIANTS = ("main", "plain", "stop", "sigterm", "killed", "downkilled")
WAIT = 240.0  # One step under a loaded machine (load average ~450 on 64 CPUs).


def guard_network(log) -> None:
    """Only loopback and Unix sockets: any other connection of this process is refused and logged."""
    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex

    def allowed(sock, address):
        if sock.family not in (socket.AF_INET, socket.AF_INET6):
            return True
        host = str(address[0] if isinstance(address, tuple) else address)
        return host in {"localhost", "::1"} or host.startswith("127.")

    def connect(self, address):
        if not allowed(self, address):
            log("network.refused", address=str(address))
            raise OSError(101, "cross_release: no network")
        return real_connect(self, address)

    def connect_ex(self, address):
        if not allowed(self, address):
            log("network.refused", address=str(address))
            return 101
        return real_connect_ex(self, address)
    socket.socket.connect, socket.socket.connect_ex = connect, connect_ex


# Both sides ---------------------------------------------------------------------------------------------------------

def append(path: Path, value) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, (json.dumps(value, ensure_ascii=False, default=repr, sort_keys=False) + "\n").encode())
    finally:
        os.close(fd)


def use_tree(tree: str) -> None:
    """Put ``tree`` first on sys.path. A tree from before the rename (package harness_bridge) is imported under the
    new name too, so the scenarios below name covey modules for both."""
    sys.path.insert(0, tree)
    if (Path(tree) / "covey").is_dir() or not (Path(tree) / "harness_bridge").is_dir():
        return
    import importlib.abc
    import importlib.machinery

    class Alias(importlib.abc.MetaPathFinder, importlib.abc.Loader):
        def find_spec(self, fullname, path=None, target=None):
            if fullname.startswith("covey."):
                return importlib.machinery.ModuleSpec(fullname, self, is_package=hasattr(
                    importlib.import_module("harness_bridge" + fullname[5:]), "__path__"))
            return None

        def create_module(self, spec):
            return importlib.import_module("harness_bridge" + spec.name[5:])

        def exec_module(self, module):
            pass

    sys.modules["covey"] = importlib.import_module("harness_bridge")
    sys.meta_path.insert(0, Alias())


def read_lines(path: Path) -> list:
    try:
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    except OSError:
        return []


def stub_feishu(log, card_result) -> None:
    """Every Feishu call of this process: logged, nothing sent. ``card_result``: what creating a card returns."""
    from covey.app.cards import Cards
    from covey.feishu.messenger import Messenger

    def send_message(self, profile, chat, text, key, **kw):
        log("send_message", chat=chat, text=text, key=key, reply_to=kw.get("reply_to_message_id"))
        return True

    def create_and_send(self, profile, chat, card, key, **kw):
        log("create_and_send_card", chat=chat, card=card, key=key, reply_to=kw.get("reply_to_message_id"))
        return card_result

    def refuse(name):
        def call(self, *args, **kw):
            log(name)
            return None
        return call
    Messenger.send_message = send_message
    Messenger.create_and_send_card = create_and_send
    Messenger.recall = lambda self, *a, **k: True
    for name in ("cardkit", "send_card", "create_card", "put_card", "write_card", "publish_card"):
        setattr(Messenger, name, refuse(name))
    Cards.create_streaming_reply = lambda self, *a, **k: None


# The task runner's process ------------------------------------------------------------------------------------------

def tree_modules() -> list:
    """The tree's modules that hold the Bridge side of the runs/ protocol, newest home first:
    app/runner_ipc.py and app/runner_spawn.py since they were split out of app/detached.py, and
    app/detached.py itself (the only home in older releases)."""
    import importlib
    found = []
    for name in ("runner_ipc", "runner_spawn", "detached"):
        try:
            found.append(importlib.import_module("covey.app." + name))
        except ModuleNotFoundError as error:
            if error.name != "covey.app." + name:
                raise
    return found


class TreeNames:
    """Looks a name up in the first of tree_modules() that has it, so one script drives both trees."""

    def __getattr__(self, name):
        for module in tree_modules():
            if hasattr(module, name):
                return getattr(module, name)
        raise AttributeError(name)


def runner_entry(tree: str, scratch: str) -> int:
    """bin/covey_runner.py of the scenario's shim: runner_main of ``tree`` with the scripted backend."""
    use_tree(tree)
    scratch_dir = Path(scratch)
    events = scratch_dir / "runner-events.jsonl"

    def log(what, **fields):
        append(events, {"what": what, **fields})

    guard_network(log)
    stub_feishu(lambda what, **fields: log("feishu." + what, **fields), ("", ""))
    from covey.app import runner
    from covey.app.runs import Runs
    from covey.core.harness import BackendRuntime, HarnessCapabilities, JobCancelled, RunResult
    from covey.core.steer import SteerNotReady, SteerRefused

    links = []
    RunnerLink = TreeNames().RunnerLink
    link_init = RunnerLink.__init__

    def init(self, directory):
        link_init(self, directory)
        links.append(self)
    RunnerLink.__init__ = init

    class Control:
        pause_for_question = ""
        user_requests = frozenset()
        supports_steer = True
        awaiting_answer = False
        not_ready = 1

        def steer(self, text, event_id):
            log("backend.steer", text=text, event_id=event_id)
            if text == "steer-refuse":
                raise SteerRefused("这条补充不收")
            if text == "steer-boom":
                raise RuntimeError("boom")
            if text == "steer-notready" and self.not_ready:
                self.not_ready = 0
                raise SteerNotReady("还没准备好")

        def answer(self, request_id, answers):
            log("backend.answer", request_id=request_id, answers=answers)

        def reject_question(self, request_id, reason):
            log("backend.reject_question", request_id=request_id, reason=reason)

        def wait_for_answer(self, question):
            log("backend.wait_for_answer")

        def close(self):
            log("backend.close")

    def uploading():
        """An upload that outlives the job by 2 s (a thread name the runner waits for): the runner is alive
        after done.json, so the Bridge's job_done and release_done run every time, not only when its poll is lucky."""
        import threading
        threading.Thread(target=time.sleep, args=(2.0,), daemon=True, name="artifact-upload").start()

    class Backend(BackendRuntime):
        def capabilities(self, ctx):
            return HarnessCapabilities(steering=True)

        def prompt_runtime_note(self, ctx):
            return ""

        def run(self, request):
            ctx = request.context
            log("backend.run", prompt_has_request="请做这件事" in request.prompt, session=request.session_id,
                topic=ctx.topic_id, model=ctx.model, effort=ctx.effort, workdir=str(ctx.workdir),
                binding=ctx.options.get("binding_revision"), run_id_set=bool(request.inputs.run_id))
            request.observers.control(Control())
            request.observers.session(SID)
            while True:
                if request.inputs.cancel_event.is_set():
                    uploading()
                    log("backend.cancelled")
                    raise JobCancelled("任务已停止")
                for path in sorted((scratch_dir / "cmd").glob("*.json")):
                    command = json.loads(path.read_text())
                    path.unlink()
                    log("backend.command", do=command["do"])
                    if command["do"] == "finish":
                        uploading()
                        return RunResult("这是答复正文", SID)
                    if command["do"] in {"enqueue", "enqueue_bad"}:
                        from types import SimpleNamespace
                        target = ctx if command["do"] == "enqueue" else SimpleNamespace(
                            topic_id="no-such-topic", model=ctx.model, effort=ctx.effort, work_mode=ctx.work_mode)
                        try:
                            made = links[-1].enqueue(self.name, target, "后续任务", "chat", command["message_id"],
                                                     {"why": "cross"}, title="标题", display="显示")
                            log("backend.enqueued", job_id=made.job_id)
                        except Exception as exc:  # noqa: BLE001
                            log("backend.enqueue_failed", type=type(exc).__name__, text=str(exc))
                time.sleep(0.05)

    real_generate = Runs.generate_reply_for_worker

    def generate(self, ctx, job, session_id, progress):
        backend = Backend()
        backend.name = self.harness.spec.name
        self.harness.backend = backend
        log("job", job_id=job.job_id, user_text=job.user_text, chat=job.chat_id, message=job.message_id,
            topic=job.topic_id, harness=job.harness_id, reply_to=job.reply_to_message_id, title=job.title,
            binding=job.binding_revision, recovery=job.recovery_context, workdir=str(job.workdir),
            history=str(job.history_path), session_path=str(job.session_path), sender=job.sender_id,
            mode=job.work_mode, snapshot=job.model_settings_snapshot, relaunched=job.runner_relaunched,
            cancelled=job.cancel_event.is_set(), detached_run_dir=job.detached_run_dir)
        return real_generate(self, ctx, job, session_id, progress)
    Runs.generate_reply_for_worker = generate
    return runner.runner_main()


# The Bridge's process -----------------------------------------------------------------------------------------------

def attempt(fn, *args, **kw):
    try:
        return ["returned", fn(*args, **kw)]
    except BaseException as exc:  # noqa: BLE001
        return [type(exc).__name__, str(exc)]


def read_exit_file(directory):
    try:
        return json.loads((Path(directory) / "exit.json").read_text())
    except (OSError, ValueError):
        return {}


class Side:
    """One Bridge process: its gateway over the scratch state dir, and what it finds, in order."""

    def __init__(self, tree, shim, scratch, phase, variant):
        self.scratch, self.phase, self.variant = Path(scratch), phase, variant
        self.state = self.scratch / "state"
        self.work = self.scratch / "work"
        self.events_path = self.scratch / f"bridge-{phase}-events.jsonl"
        self.facts = {}
        use_tree(tree)
        card = ("", "") if variant == "killed" else ("card-recover", "om-recover")
        guard_network(lambda what, **fields: append(self.events_path, {"what": what, **fields}))
        stub_feishu(lambda what, **fields: append(self.events_path, {"what": "feishu." + what, **fields}), card)
        from covey.app import detached
        from covey.app.unified import UnifiedBridgeApp
        import importlib
        for name in ("job_dispatch", "unified"):  # Where process_reply_job reads it: app/job_dispatch.py since r3.
            try:
                module = importlib.import_module("covey.app." + name)
            except ModuleNotFoundError as error:
                if error.name != "covey.app." + name:
                    raise
                continue
            if hasattr(module, "check_or_raise"):
                module.check_or_raise = lambda *a, **k: None
        self.detached = TreeNames()
        for module in tree_modules():  # Set where they are read; this machine's load, not the protocol.
            for name in ("ACK_TIMEOUT", "ANSWER_TIMEOUT"):
                if hasattr(module, name):
                    setattr(module, name, 120.0)
        import covey
        self.code = str(Path(covey.__file__).parents[1]) if getattr(covey, "__file__", None) else str(Path(detached.__file__).parents[2])
        self.gateway = UnifiedBridgeApp(script_path=Path(shim) / "bin" / "covey_bridge.py")
        argv = ["--workdir", str(self.work), "--state-dir", str(self.state), "--profile", "test-bot"]
        self.gateway.runtime.cli_argv = list(argv)
        self.ctx = self.gateway.build_context(self.gateway.build_parser().parse_args(argv))
        self.logs = []
        self.gateway.runtime.log_sink = self.logs.append
        self.gateway.runtime.owner_open_id = "owner"
        for app in self.gateway.apps.values():
            app.harness.started = True
            app.models.prepare_submission_context = lambda ctx: ctx
            app.submission.selection_card = lambda *a, **k: None

    def fact(self, name, value):
        self.facts[name] = value
        append(self.events_path, {"what": "fact", "name": name, "value": value})

    def wait(self, what, check):
        deadline = time.monotonic() + WAIT
        while time.monotonic() < deadline:
            value = check()
            if value:
                return value
            time.sleep(0.05)
        raise TimeoutError(what)

    def runner_event(self, what, count=1, **match):
        def found():
            rows = [r for r in read_lines(self.scratch / "runner-events.jsonl") if r["what"] == what
                    and all(r.get(k) == v for k, v in match.items())]
            return rows[-1] if len(rows) >= count else None
        return self.wait(f"runner event {what} {match}", found)

    def command(self, n, **fields):
        folder = self.scratch / "cmd"
        folder.mkdir(exist_ok=True)
        temp = folder / f"{n:03}.tmp"
        temp.write_text(json.dumps(fields))
        temp.rename(folder / f"{n:03}.json")

    def topic(self, message_id):
        from covey.core.workspaces import bind_topic
        event = {"chat_id": "chat", "sender_id": "owner", "message_id": message_id, "root_id": "", "content": "task"}
        app, target = self.gateway.topics.route(self.ctx, event)
        bind_topic(target, target.workspace_name, self.work)
        topic = json.loads((self.state / "topics" / target.topic_id / "topic.json").read_text())
        try:
            from covey.app.topics import topic_target
        except ImportError:  # Releases before topic_target (a4bd710) build it as their serve_requests does.
            return app, app.submission.event_context(app.harness.context, {"chat_id": "chat", "message_id": topic["root_id"]})
        return app, topic_target(app, "chat", topic)

    def submit(self, app, target, message_id, text):
        job = app.submission.enqueue_chat_job(target, text, "chat", message_id, prompt_content=text, title="题")
        assert job is not None, "not queued"
        return job, self.gateway.runtime.jobs.queue.get_nowait()

    def run_dir_files(self, directory):
        return sorted(str(p.relative_to(directory)) for p in Path(directory).rglob("*") if p.is_file())

    def record(self, key):
        return json.loads((self.state / "jobs" / f"{key}.json").read_text())

    def start(self):
        import threading
        from covey.core.jobs import upsert_job_record
        from covey.core.topics import remember_topic_message
        from covey.app import submission
        detached, runtime = self.detached, self.gateway.runtime
        runtime.jobs.counter = 6
        app, target = self.topic("task")
        self.app, self.target = app, target
        real_prestart = submission.prestart_runner
        if self.variant == "plain":
            submission.prestart_runner = lambda *a, **k: None
        made, job = self.submit(app, target, "task", "请做这件事")
        assert made is job
        submission.prestart_runner = lambda *a, **k: None
        self.fact("job_id", job.job_id)
        self.fact("queued_file", json.loads((self.state / "queued" / "task.json").read_text())["harness"])
        self.fact("prestarted", job.early_runner is not None)
        remember_topic_message(self.state, target.topic_id, "chat", "card-7")
        upsert_job_record(self.state, "task", card_message_id="card-7")
        # A job that waits in queued/ over the restart: written by this Bridge, read by the next.
        app2, target2 = self.topic("other")
        self.submit(app2, target2, "other", "另一个任务")
        self.fact("queued_other", sorted(json.loads((self.state / "queued" / "other.json").read_text())))
        submission.prestart_runner = real_prestart
        self.job = job
        self.thread = threading.Thread(target=self.gateway.process_reply_job, args=(job, self.ctx), daemon=True, name="scheduler")
        self.thread.start()
        directory = self.directory = self.wait("run dir", lambda: job.detached_run_dir)
        self.fact("run_dir_is_absolute_under_state", str(directory) == str(self.state / "runs" / "task"))
        self.runner_event("backend.run")
        handoff = json.loads((directory / "handoff.json").read_text())
        self.fact("handoff_keys", sorted(set(handoff) - {"prestart"}))
        self.fact("handoff_job_keys", sorted(handoff["job"]))
        self.fact("handoff_fields", {k: handoff["job"]["fields"].get(k) for k in sorted(handoff["job"]["fields"])})
        self.fact("handoff_extras", handoff["job"]["extras"])
        self.fact("handoff_top", {k: handoff[k] for k in ("schema_version", "harness", "argv", "owner", "script_path")})
        self.fact("queued_file_gone", not (self.state / "queued" / "task.json").exists())
        self.fact("runner_event_job", self.runner_event("job"))
        self.fact("runner_event_run", self.runner_event("backend.run"))
        self.wait("runner.json rewritten by the runner", lambda: json.loads((directory / "runner.json").read_text()).get("release")
                  != str(Path(runtime.script_path).resolve().parents[1]))
        self.fact("runner_json", json.loads((directory / "runner.json").read_text()))
        self.fact("alive", detached.alive(directory))
        self.fact("runner_info_key", (detached.runner_info(directory) or {}).get("key"))
        self.fact("live_keys", sorted(detached.live_keys(self.state)))
        self.wait("wake.json", (directory / "wake.json").exists)
        self.fact("wake_json_keys", sorted(json.loads((directory / "wake.json").read_text())))
        self.fact("wake_sent", detached.wake_runner(directory))
        time.sleep(0.3)
        self.fact("alive_after_wake", detached.alive(directory))
        self.fact("control_type", [type(job.backend_control).__name__, job.backend_control.supports_steer])
        if self.variant in {"main", "plain"}:
            self.exchange(job, target)
        if self.variant == "plain":
            self.finish_here()
        elif self.variant == "stop":
            raw = {"kind": "bridge_job_stop", "job_id": job.job_id}
            event = {"operator_id": "owner", "chat_id": "chat", "message_id": "card-7", "event_id": "stop-1", "action_value": raw}
            self.fact("stop_forwarded", detached.forward_card_action(runtime, target, event, raw))
            self.runner_event("backend.cancelled")
            self.settle()
        elif self.variant == "sigterm":
            os.kill(detached.runner_info(directory)["pid"], signal.SIGTERM)
            self.runner_event("backend.cancelled")
            self.settle()
        elif self.variant == "killed":
            os.kill(detached.runner_info(directory)["pid"], signal.SIGKILL)
            self.settle()
        self.leave()

    def exchange(self, job, target):
        """Every message of the run directory but the last: inbox, acks, requests, replies."""
        detached, directory = self.detached, self.directory
        control = job.backend_control
        self.fact("steer_ok", attempt(control.steer, "steer-ok", "ev-1"))
        self.fact("steer_ok_seen", self.runner_event("backend.steer", text="steer-ok"))
        self.fact("steer_refused", attempt(control.steer, "steer-refuse", "ev-2"))
        self.fact("steer_failed", attempt(control.steer, "steer-boom", "ev-3"))
        self.fact("steer_held", attempt(control.steer, "steer-notready", "ev-4"))
        self.runner_event("backend.steer", count=2, text="steer-notready")  # Sent once the task can take it.
        self.fact("steer_slash", attempt(control.steer, "/compact", "ev-5"))
        self.fact("acks_left", self.run_dir_files(directory / "acks"))
        token = "00112233445566ff"
        (self.state / "questions").mkdir(exist_ok=True, mode=0o700)
        (self.state / "questions" / f"{token}.json").write_text(json.dumps(
            {"token": token, "job_id": job.job_id, "topic_id": target.topic_id, "chat_id": "chat", "status": "cancelled"}))
        self.fact("answer", attempt(detached.forward_answer, self.gateway.runtime, target, "chat", token, {"q": "a"}, "ev-a"))
        raw = {"kind": "no_such_kind", "job_id": job.job_id}
        event = {"operator_id": "owner", "chat_id": "chat", "message_id": "card-7", "event_id": "c-1", "action_value": raw}
        self.fact("click_forwarded", detached.forward_card_action(self.gateway.runtime, target, event, raw))
        self.wait("inbox read", lambda: not list((directory / "inbox").glob("*.json")))
        self.command(1, do="enqueue", message_id="cont-1")
        self.fact("enqueued", self.runner_event("backend.enqueued"))
        self.fact("continuation_record", {k: self.record("cont-1").get(k) for k in ("job_id", "status", "topic_id", "chat_id", "recovery_context", "title")})
        self.command(2, do="enqueue_bad", message_id="cont-2")
        self.fact("enqueue_refused", self.runner_event("backend.enqueue_failed"))
        self.fact("requests_left", self.run_dir_files(directory / "requests") + self.run_dir_files(directory / "replies"))

    def settle(self):
        self.wait("scheduler thread", lambda: not self.thread.is_alive())
        self.wait("runner exit", lambda: not self.gateway.runtime.jobs.finishing)
        for thread in __import__("threading").enumerate():
            if thread.name == "runner-exit":
                thread.join(WAIT)

    def finish_here(self):
        self.command(5, do="finish")
        self.settle()

    def leave(self):
        directory = getattr(self, "directory", None)
        if self.variant in {"main", "downkilled"} and self.phase == "start":
            return  # The record and files are looked at after the next Bridge.
        record = self.record("task")
        self.fact("record", record)
        self.fact("run_dir_files", self.run_dir_files(directory))
        self.fact("exit", read_exit_file(directory))
        self.fact("done_json", json.loads((directory / "done.json").read_text()) if (directory / "done.json").exists() else None)
        self.fact("alive_at_end", self.detached.alive(directory))
        self.fact("runner_events", read_lines(self.scratch / "runner-events.jsonl"))
        self.fact("runner_log", (directory / "runner.log").read_text().splitlines())

    def adopt(self):
        import threading
        from covey.core.jobs import max_job_id
        runtime = self.gateway.runtime
        runtime.jobs.counter = max_job_id(self.state)
        self.fact("counter", runtime.jobs.counter)
        self.gateway.notify_stale_jobs(self.ctx)
        self.fact("adopted_keys", sorted(runtime.adopted_keys))
        queued = []
        while not runtime.jobs.queue.empty():
            queued.append(runtime.jobs.queue.get_nowait())
        self.fact("requeued", [[j.job_id, j.message_id, j.user_text, str(j.detached_run_dir or "")] for j in queued])
        self.fact("queued_files", sorted(p.name for p in (self.state / "queued").glob("*.json")))
        self.directory = self.state / "runs" / "task"
        job = next((j for j in queued if j.message_id == "task"), None)
        if self.variant == "downkilled":
            self.fact("stale_record_seen", job is None)
            self.fact("record_after_restart", self.record("task"))
            self.leave_common()
            return
        self.job = job
        self.fact("adopted_control", [type(job.backend_control).__name__, job.backend_control.supports_steer])
        self.thread = threading.Thread(target=self.gateway.process_reply_job, args=(job, self.ctx), daemon=True, name="scheduler")
        self.thread.start()
        self.wait("bridge_restarted read", lambda: not list((self.directory / "inbox").glob("*.json")))
        self.fact("steer_after_adopt", attempt(job.backend_control.steer, "steer-adopted", "ev-9"))
        self.fact("steer_after_adopt_seen", self.runner_event("backend.steer", text="steer-adopted"))
        self.command(6, do="finish")
        self.settle()
        self.leave_common()

    def leave_common(self):
        directory = self.directory
        self.fact("record", self.record("task"))
        self.fact("run_dir_files", self.run_dir_files(directory))
        self.fact("exit", read_exit_file(directory))
        self.fact("done_json", json.loads((directory / "done.json").read_text()) if (directory / "done.json").exists() else None)
        self.fact("alive_at_end", self.detached.alive(directory))
        self.fact("runner_events", read_lines(self.scratch / "runner-events.jsonl"))
        self.fact("runner_log", (directory / "runner.log").read_text().splitlines())

    def save(self, error=None):
        out = {"phase": self.phase, "error": error, "code": self.code, "facts": self.facts, "log": self.logs,
               "feishu": [r for r in read_lines(self.events_path) if r["what"].startswith(("feishu.", "network."))]}
        (self.scratch / f"bridge-{self.phase}.json").write_text(json.dumps(out, ensure_ascii=False, default=repr))


def bridge_entry(tree, shim, scratch, phase, variant):
    side = Side(tree, shim, scratch, phase, variant)
    error = None
    try:
        side.start() if phase == "start" else side.adopt()
    except BaseException:  # noqa: BLE001
        import traceback
        error = traceback.format_exc()
    side.save(error)
    os._exit(1 if error else 0)  # As a killed Bridge: no cleanup, its runner goes on.


# The orchestrator ---------------------------------------------------------------------------------------------------

SHIM = """import importlib.util, sys
spec = importlib.util.spec_from_file_location("cross_release", {script!r})
module = importlib.util.module_from_spec(spec)
sys.modules["cross_release"] = module
spec.loader.exec_module(module)
raise SystemExit(module.runner_entry({tree!r}, {scratch!r}))
"""


def scrubbed_env(scratch: Path) -> dict:
    return {"PATH": "/usr/bin:/bin", "HOME": str(scratch / "home"), "TMPDIR": str(scratch / "tmp"), "LANG": "C.UTF-8",
            "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1", "COVEY_FEISHU_DIRECT": "0"}


def kill_all(scratch: Path) -> None:
    """Every task runner left that names this scratch dir (a prestarted runner that got no job), and only those."""
    me = os.getpid()
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit() or int(entry.name) == me:
            continue
        try:
            cmdline = Path(entry.path, "cmdline").read_bytes().decode(errors="replace")
        except OSError:
            continue
        if str(scratch) + "/" in cmdline.replace("\0", " ") and ("covey_runner.py" in cmdline or "lark_task_runner.py" in cmdline):
            try:
                os.kill(int(entry.name), signal.SIGKILL)
            except OSError:
                pass


def run_one(out: Path, trees: dict, stamp: str, scenario: str, variant: str) -> str:
    bridge_a, runner_tree, bridge_b = SCENARIOS[scenario]
    scratch = out / "scratch" / f"{scenario}-{variant}"
    shutil.rmtree(scratch, ignore_errors=True)
    for name in ("state", "work", "home", "tmp", "cmd", "shim/bin"):
        (scratch / name).mkdir(parents=True)
    shim = scratch / "shim"
    for name in ("covey_runner.py", "lark_task_runner.py"):  # The new and the old release's runner path.
        (shim / "bin" / name).write_text(SHIM.format(script=str(Path(__file__).resolve()),
                                                     tree=trees[runner_tree], scratch=str(scratch)))
    for name in ("covey_bridge.py", "lark_harness_bridge.py"):
        (shim / "bin" / name).write_text("# Only its path is used: a runner's handoff names it.\n")
    result = {"scenario": scenario, "variant": variant, "trees": [bridge_a, runner_tree, bridge_b], "stamp": stamp}
    env = scrubbed_env(scratch)

    def phase(name, tree):
        try:
            done = subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()), "bridge", trees[tree], str(shim),
                                   str(scratch), name, variant], env=env, cwd=scratch, capture_output=True, text=True,
                                  timeout=WAIT * 6)
            stderr, code = done.stderr, done.returncode
        except subprocess.TimeoutExpired as exc:
            stderr, code = str(exc.stderr or ""), "timeout"
        path = scratch / f"bridge-{name}.json"
        data = json.loads(path.read_text()) if path.exists() else {"error": "no output"}
        data["stderr"] = stderr[-3000:]
        data["returncode"] = code
        result[name] = data
        return code == 0
    ok = False
    try:
        ok = phase("start", bridge_a)
        if ok and variant == "downkilled":
            info = json.loads((scratch / "state/runs/task/runner.json").read_text())
            os.kill(info["pid"], signal.SIGKILL)
            time.sleep(0.5)
        if ok and variant in {"main", "downkilled"}:
            ok = phase("adopt", bridge_b)
    except Exception as exc:  # noqa: BLE001 - a run that broke still writes its JSON, as a failed run.
        ok = False
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        kill_all(scratch)
    result["ok"] = ok
    (out / "runs").mkdir(exist_ok=True)
    (out / "runs" / f"{scenario}-{variant}.json").write_text(json.dumps(result, ensure_ascii=False, indent=1, default=repr))
    if ok:
        shutil.rmtree(scratch, ignore_errors=True)  # A failed run's files stay for a look.
    return f"{scenario} {variant}: {'ok' if ok else 'FAILED'}"


def refused_out(out: Path) -> str:
    """Why ``out`` cannot hold a run: inside this checkout, $HOME or /tmp, or a directory of something else."""
    for given in {out.absolute(), out.resolve()}:
        for root, name in ((REPO, "this checkout"), (Path.home(), "$HOME"), (TMP, "/tmp")):
            for base in {root.absolute(), root.resolve()}:
                if given == base or base in given.parents:
                    return f"{out} is inside {name}"
    if out.exists() and any(out.iterdir()) and own_meta(out) is None:
        return f"{out} is not empty and holds no earlier run of this script (meta.json)"
    return ""


def own_meta(out: Path):
    """<out>/meta.json when this script wrote it (its marker, or the keys every run wrote), else None."""
    try:
        meta = json.loads((out / "meta.json").read_text())
    except (OSError, ValueError):
        return None
    if isinstance(meta, dict) and (meta.get("tool") == TOOL or {"old_commit", "new_head", "trees"} <= set(meta)):
        return meta
    return None


def git(*args) -> str:
    return subprocess.run(["git", "-C", str(REPO), *args], check=True, capture_output=True, text=True).stdout.strip()


def export(ref: str, out: Path) -> tuple[str, Path]:
    """The tree of ``ref`` (git archive) under <out>/trees/: (commit, path)."""
    commit = git("rev-parse", "--verify", ref + "^{commit}")
    tree = out / "trees" / ("old-" + commit[:12])
    if not (tree / "covey").is_dir() and not (tree / "harness_bridge").is_dir():
        shutil.rmtree(tree, ignore_errors=True)
        tree.mkdir(parents=True)
        archive = subprocess.Popen(["git", "-C", str(REPO), "archive", "--format=tar", commit], stdout=subprocess.PIPE)
        with tarfile.open(fileobj=archive.stdout, mode="r|") as tar:
            tar.extractall(tree, filter="data")
        if archive.wait() != 0:
            raise SystemExit(f"git archive {commit} failed")
    return commit, tree.resolve()


def run_all(args) -> int:
    out = Path(args.out).absolute()
    why = refused_out(out)
    if why:
        print(f"refused: {why}", file=sys.stderr)
        return 2
    scenarios = args.scenarios.split(",") if args.scenarios else list(SCENARIOS)
    variants = args.variants.split(",") if args.variants else list(VARIANTS)
    unknown = sorted(set(scenarios) - set(SCENARIOS)) + sorted(set(variants) - set(VARIANTS))
    if unknown:
        print(f"unknown scenario or variant: {', '.join(unknown)}", file=sys.stderr)
        return 2
    out.mkdir(parents=True, exist_ok=True)
    commit, old = export(args.old, out)
    trees = {"new": str(REPO), "old": str(old)}
    dirty = bool(git("status", "--porcelain", "--", "covey", "bin"))
    # An earlier run's results never count for this one: runs/ starts empty, and each run carries the stamp.
    shutil.rmtree(out / "runs", ignore_errors=True)
    stamp = f"{time.time_ns()}-{os.getpid()}"
    meta = {"tool": TOOL, "stamp": stamp, "old_ref": args.old, "old_commit": commit, "new_head": git("rev-parse", "HEAD"),
            "new_dirty": dirty, "trees": trees, "scenarios": scenarios, "variants": variants}
    (out / "meta.json").write_text(json.dumps(meta, indent=1) + "\n")
    print(f"old {args.old} = {commit[:12]} in {old}; new = {REPO} at {meta['new_head'][:12]}{' with changes' if dirty else ''}",
          flush=True)
    failed = 0
    with ThreadPoolExecutor(args.jobs) as pool:
        for line in pool.map(lambda pair: run_one(out, trees, stamp, *pair), [(s, v) for s in scenarios for v in variants]):
            failed += line.endswith("FAILED")
            print(line, flush=True)
    return 1 if failed else 0


# Comparing ----------------------------------------------------------------------------------------------------------

TIME_KEYS = {"at", "started_at", "finished_at", "done_at", "created_at", "updated_at", "deadline", "interrupted_at",
             "auto_retry_at", "cancel_requested_at", "pid", "start_ticks", "boot_id", "stream_replacements"}
PATTERNS = [(re.compile(r"\d{4}-\d\d-\d\dT[\d:.]+(?:Z|[+-]\d\d:\d\d)?"), "<T>"), (re.compile(r"\bpid=\d+"), "pid=<P>"),
            (re.compile(r"\b(inbox_ms|t0|total_ms|api_ms|calls|elapsed_ms|queued_ms)=-?\d+"), r"\1=<N>"),
            (re.compile(r"\bphases=\S*"), "phases=<P>"),
            (re.compile(r"\b\d{19}-[0-9a-f]{8}\b"), "<OP>"), (re.compile(r"\b\d\d:\d\d(?::\d\d)?\b"), "<HM>"),
            (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b(?<!" + SID + ")"), "<UUID>")]
TIMING_LINE = re.compile(r"(?:\[[^\]]*\] )?timing ")
# Fields whose values are per run but whose presence matters: compared as present.
VOLATILE_RECORD = {"timings", "latency_ms", "revision", "card_update_failures", "token_rate", "context_tokens",
                   "elapsed_seconds", "last_progress_age_seconds", "long_task_summary"}


def normal(value, scratch, trees, key=None):
    if isinstance(value, dict):
        return {k: normal(v, scratch, trees, k) for k, v in value.items()}
    if isinstance(value, list):
        return [normal(v, scratch, trees) for v in value]
    if isinstance(value, bool) or value is None:
        return value
    if key in TIME_KEYS and value not in ("", 0):
        return "<VAR>"
    if isinstance(value, (int, float)):
        return "<NUM>" if value >= 1_000_000_000 else value
    text = str(value).replace(str(scratch), "<D>")
    for tree in trees:
        text = text.replace(tree, "<TREE>")
    for pattern, by in PATTERNS:
        text = pattern.sub(by, text)
    return text


def tidy(data, scratch, trees):
    out = {}
    for phase in ("start", "adopt"):
        if phase not in data:
            continue
        section = normal({k: data[phase].get(k) for k in ("error", "facts", "log", "feishu")}, scratch, trees)
        facts = section["facts"] or {}
        for name in ("record", "record_after_restart"):
            if isinstance(facts.get(name), dict):
                for field in VOLATILE_RECORD & set(facts[name]):
                    facts[name][field] = "<PRESENT>"
        # Order of lines and events of different threads is not the protocol, nor are timing lines (their
        # counts, such as journal=jobs:N, vary from run to run within one release).
        section["log"] = sorted(line for line in section["log"] or [] if not line.startswith("timing "))
        if isinstance(facts.get("runner_log"), list):
            facts["runner_log"] = sorted(line for line in facts["runner_log"] if not TIMING_LINE.match(line))
        if isinstance(facts.get("runner_events"), list):
            facts["runner_events"] = sorted(facts["runner_events"], key=lambda row: json.dumps(row, sort_keys=True))
        out[phase] = section
    return out


def leaves(value, prefix=""):
    if isinstance(value, dict):
        for k in sorted(value):
            yield from leaves(value[k], f"{prefix}.{k}" if prefix else str(k))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from leaves(v, f"{prefix}[{i}]")
        if not value:
            yield prefix, "[]"
    else:
        yield prefix, value


def difference(a, b):
    x, y = dict(leaves(a)), dict(leaves(b))
    return {k: (x.get(k, "<absent>"), y.get(k, "<absent>")) for k in sorted(set(x) | set(y)) if x.get(k, "<absent>") != y.get(k, "<absent>")}


def compare(args) -> int:
    out = Path(args.dir).absolute()
    meta = own_meta(out)
    if meta is None:
        print(f"{out}: no run here (meta.json)", file=sys.stderr)
        return 2
    trees = meta["trees"]
    loaded, stale = {}, []
    for path in sorted((out / "runs").glob("*.json")):
        data = json.loads(path.read_text())
        if data.get("stamp") != meta.get("stamp"):  # Left by another run (meta.json of before the stamp has none).
            stale.append(path.name)
            continue
        scratch = out / "scratch" / f"{data['scenario']}-{data['variant']}"
        loaded[(data["scenario"], data["variant"])] = (data, tidy(data, scratch, sorted(trees.values(), key=len, reverse=True)))
    print(f"old {meta['old_ref']} = {meta['old_commit'][:12]}; new = {meta['new_head'][:12]}"
          f"{' with uncommitted changes' if meta['new_dirty'] else ''}")
    failed = sorted(f"{k[0]} {k[1]}" for k, v in loaded.items() if not v[0].get("ok"))
    print("runs", len(loaded), "failed", failed)
    # Every run asked for must be there: a run that never wrote its JSON is not a pass.
    missing = [f"{s} {v}" for s in meta.get("scenarios", SCENARIOS) for v in meta.get("variants", VARIANTS)
               if (s, v) not in loaded]
    print("runs missing:", missing)
    print("runs of another run (not this meta.json's stamp):", stale)
    wrong = []
    for (scenario, variant), (data, _) in loaded.items():
        bridge_a, runner_tree, bridge_b = (trees[name] for name in SCENARIOS[scenario])
        facts = {**(data.get("start", {}).get("facts") or {}), **(data.get("adopt", {}).get("facts") or {})}
        if (data.get("start", {}).get("code") != bridge_a or (facts.get("runner_json") or {}).get("release") != runner_tree
                or ("adopt" in data and data["adopt"].get("code") != bridge_b)):
            wrong.append(f"{scenario} {variant}")
    print("runs whose Bridge or runner was not the release they name:", sorted(wrong))
    facts_of = [{**(v[0].get("start", {}).get("facts") or {}), **(v[0].get("adopt", {}).get("facts") or {})}
                for v in loaded.values()]
    exchanged = {name: sum(name in facts for facts in facts_of)
                 for name in ("handoff_fields", "runner_json", "wake_sent", "steer_ok", "steer_refused", "steer_failed", "steer_held",
                              "answer", "click_forwarded", "enqueued", "enqueue_refused", "stop_forwarded", "adopted_keys",
                              "steer_after_adopt", "requeued", "done_json", "exit", "record_after_restart")}
    print("runs that exercised each exchange:", exchanged)
    logs = [v[0].get("start", {}).get("log", []) + v[0].get("adopt", {}).get("log", []) for v in loaded.values()]
    print("done line (release_done) in runs:", sum(any("task runner done" in line for line in log) for log in logs))
    network = sum(1 for v in loaded.values() for phase in ("start", "adopt")
                  for row in v[0].get(phase, {}).get("feishu", []) if row["what"].startswith("network."))
    network += sum(1 for v in loaded.values() for phase in ("start", "adopt")
                   for row in (v[0].get(phase, {}).get("facts") or {}).get("runner_events", [])
                   if isinstance(row, dict) and str(row.get("what", "")).startswith("network."))
    print("connections refused (not loopback):", network)
    different = compared = 0
    for variant in VARIANTS:
        for scenario, baseline in PAIRS:
            if (scenario, variant) not in loaded or (baseline, variant) not in loaded:
                continue
            found = difference(loaded[(baseline, variant)][1], loaded[(scenario, variant)][1])
            compared += 1
            different += bool(found)
            print(f"{scenario} vs {baseline} [{variant}]: {len(found)} differences")
            for key, (x, y) in list(found.items())[:args.show]:
                print("   ", key, "\n       -", str(x)[:220], "\n       +", str(y)[:220])
    equal = not failed and not wrong and not different and not missing and not stale and compared
    print("all equal" if equal else f"NOT EQUAL: {len(failed)} failed, {len(missing)} missing, {len(stale)} of another run, "
          f"{len(wrong)} on the wrong release, {different} of {compared} pairs with differences")
    return 0 if equal else 1


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["bridge"]:
        bridge_entry(*argv[1:6])
        return 0
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run the scenarios against an old release")
    run.add_argument("--old", required=True, help="git ref of the old release (e.g. the live one)")
    run.add_argument("--out", required=True, help="a directory of its own, outside this checkout, $HOME and /tmp")
    run.add_argument("--jobs", type=int, default=4)
    run.add_argument("--scenarios", default="", help=f"comma-separated, of {', '.join(SCENARIOS)}")
    run.add_argument("--variants", default="", help=f"comma-separated, of {', '.join(VARIANTS)}")
    cmp_ = sub.add_parser("compare", help="compare the runs in a directory")
    cmp_.add_argument("dir")
    cmp_.add_argument("--show", type=int, default=12, help="differences shown per pair")
    args = parser.parse_args(argv)
    return run_all(args) if args.command == "run" else compare(args)


if __name__ == "__main__":
    sys.exit(main())
