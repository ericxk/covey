"""Backend doubles: generic fake backends, the Codex App Server RPC, synthetic
Claude CLI rows and MASO SDK messages.

Every double replaces the model process only; the adapters and the shared
worker that consume them are the production code.
"""
from __future__ import annotations

import queue
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support import FIXTURES, make_context
from covey.core.harness import BackendRuntime, HarnessCapabilities, RunCallbacks, RunInputs, RunRequest, RunResult
from covey.backends.codex import appserver
from covey.backends.codex.rpc import AppServer
from covey.backends.codex.bridge import CodexBackend

SID = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
RUN = "33333333-3333-4333-8333-333333333333"
TURN = "turn-current"
SECRET = "PRIVATE_ERROR_SENTINEL"


class FakeBackend(BackendRuntime):
    """Answers "ok" to every prompt and records it."""

    def __init__(self, note: str = "") -> None:
        self.prompts: list[str] = []
        self.note = note

    def run(self, request):
        self.prompts.append(request.prompt)
        return RunResult("ok", None)

    def prompt_runtime_note(self, ctx) -> str:
        return self.note


class InstructedBackend(FakeBackend):
    """A FakeBackend with a system channel: also records each turn's ``request.instructions``
    and, given ``sid``, reports that native session as the turn's."""

    def __init__(self, note: str = "", sid: str | None = None) -> None:
        super().__init__(note)
        self.instructions: list[str] = []
        self.sid = sid

    def capabilities(self, ctx):
        return HarnessCapabilities(system_instructions=True)

    def run(self, request):
        self.instructions.append(request.instructions)
        if self.sid:
            request.observers.session(self.sid)
        self.prompts.append(request.prompt)
        return RunResult("ok", self.sid)


class RecordingBackend(BackendRuntime):
    """Answers "完成" and records (ctx, prompt, session_id); keeps or mints a session. It declares no
    capabilities until a test gives some (``declare(questions=True)``): what every caller then reads."""

    def __init__(self):
        self.calls = []
        self.declared = HarnessCapabilities()

    def declare(self, **capabilities):
        self.declared = HarnessCapabilities(**capabilities)

    def capabilities(self, ctx):
        return self.declared

    def run(self, request):
        self.calls.append((request.context, request.prompt, request.session_id))
        return RunResult("完成", request.session_id or str(uuid.uuid4()))

    def prompt_runtime_note(self, ctx):
        return ""


# Codex App Server.

def codex_context(root, transport):
    return make_context(root, workspace_name="unit", model="fixture-model", timeout=10,
                        backend_cmd=["never-run-codex"], options={"transport": transport})


def agent(mid="answer", text="Verified answer", phase="final_answer"):
    return {"type": "agentMessage", "id": mid, "text": text, "phase": phase}


def turn(items=None, **extra):
    return {"id": TURN, "status": "completed", "error": None, "itemsView": "full",
            "items": [agent()] if items is None else items, **extra}


def finished(value=None, **extra):
    return {"method": "turn/completed", "params": {"threadId": SID,
        "turn": turn() if value is None else value, **extra}}


class AppServerProcess:
    def __init__(self, rc=None):
        self.returncode = rc

    def poll(self):
        return self.returncode


def codex_server(rpc=None, *, factory=None):
    """The Codex app-server process replaced: each ``AppServer(argv, cwd, env)`` the backend starts is
    ``rpc``, or what ``factory(argv, cwd, env)`` returns. A patch to use with ``with`` (``as server``:
    the class mock) or ``stack.enter_context``; its calls are checked against AppServer's signature."""
    answer = {"side_effect": factory} if factory else {"return_value": rpc} if rpc is not None else {}
    return mock.patch.object(appserver, "AppServer", mock.create_autospec(AppServer, **answer))


class FakeRpc(AppServer):
    """The App Server client with its process replaced by a fixed queue of rows."""

    def __init__(self, rows, *, snapshot=None, close_error=False, exit_code=0, start_sid=SID):
        self.process = AppServerProcess()
        self.events = queue.Queue()
        for row in rows:
            self.events.put(row)
        self.events.put({"method": "bridge/closed"})
        self.input_lock = threading.RLock()
        self.accepting_input = True
        self.reply_state = None
        self.input_ids = []
        self.control_error = False
        self.cleanup_confirmed = False
        self.thread_id = self.turn_id = ""
        self.user_requests = set()
        self.pause_for_question = ""
        self.snapshot = snapshot
        self.close_error = close_error
        self.exit_code = exit_code
        self.start_sid = start_sid
        self.requests = []
        self.sent = []
        self.closed = False

    def request(self, method, params, timeout=30):
        self.requests.append((method, params))
        if method in {"thread/start", "thread/resume"}:
            return {"thread": {"id": self.start_sid}}
        if method == "turn/start":
            return {"turn": {"id": TURN}}
        if method == "turn/steer":
            return {"turnId": TURN}
        if method == "thread/read":
            return self.snapshot
        return {}

    def send(self, row):
        self.sent.append(row)

    def close(self):
        self.accepting_input = False
        self.turn_id = ""
        if self.close_error:
            raise OSError(SECRET)
        self.process.returncode = self.exit_code
        self.closed = True
        self.cleanup_confirmed = True
        return True


class CodexRunCase(unittest.TestCase):
    """Runs the real CodexBackend against a FakeRpc; records events and saved replies."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.saved = []
        self.events = []
        self.cancel = threading.Event()

    def run_rpc(self, rpc, *, event_hook=None, control_hook=None, session=None, save_hook=None):
        def emit(kind, data, control):
            self.events.append((kind, data))
            if event_hook:
                event_hook(kind, data, control)
        callbacks = RunCallbacks(event=emit, control=control_hook or (lambda _: None),
            verified_reply=save_hook or self.saved.append)
        with codex_server(rpc):
            result = CodexBackend().run(RunRequest(codex_context(self.root, "app-server"), "private prompt",
                session, callbacks=callbacks, inputs=RunInputs(self.cancel, run_id=RUN)))
        self.assertTrue(rpc.closed or rpc.close_error)
        return result


# Claude Code stream-json.

CLAUDE_JSONL = FIXTURES / "harness/claude_jsonl.py"


def assistant(text="Complete answer", key="answer", **extra):
    return {
        "type": "assistant",
        "session_id": SID,
        "uuid": key,
        "message": {"id": key, "content": [{"type": "text", "text": text}]},
        **extra,
    }


def result(**extra):
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "session_id": SID,
        "uuid": "result-id",
        "result": "Complete answer",
        "terminal_reason": "completed",
        **extra,
    }


class ScriptedClaudeProcess:
    """``ClaudeProcess`` replaced by the rows claude_jsonl.py writes for ``scenario``, on a FakeClock.

    - ``success``: one turn that replies "Complete answer".
    - ``background-running`` / ``background-chain``: each turn starts a background task and
      replies "Reply n"; the task's end ``interval`` seconds later wakes the next turn
      (1 or 3 turns) and the last task never ends.
    - ``slow-exit``: ``success``, then the output ends but the CLI does not exit.
    - ``*-hang``: the CLI never exits after its input closes (Claude Code's settle wait runs out):
      ``settled-hang`` after ``success``; ``late-turn-hang`` after ``success`` and one more turn that
      replies "Late reply" once input closed; ``unfinished-hang`` after ``success`` and a turn that
      never ends; ``rate-limited-hang`` after a turn that ends on an API 429.
    - ``queued-after-failure``: as task #1556 ran on a gateway: the turn shows "First part", fails with
      an API 502 5 s later, and only then does the CLI take up a supplement sent meanwhile; that
      turn replies "Final after supplement" 30 s later. ``queue-lost-after-failure``: the same, but the
      CLI never takes the supplement up.

    ``receive()`` checks cancel and the task deadline as the real one does, returns the
    next row that is due, and otherwise lets 0.1 s pass. ``stop_at`` sets the task's cancel
    event at that clock time, as a person pressing stop would.
    """

    def __init__(self, clock, ctx, scenario, *, interval=1.2, stop_at=None, cancel=None):
        import subprocess

        self.clock, self.scenario, self.interval, self.stop_at = clock, scenario, interval, stop_at
        self.cancel = cancel or threading.Event()
        self.deadline = clock.monotonic() + ctx.timeout if ctx.timeout > 0 else None
        self.prelude = []

        def wait(timeout=None):
            if scenario == "slow-exit":
                raise subprocess.TimeoutExpired("claude", timeout)
            return 0
        # A hung CLI ends only when the Bridge stops it (SIGTERM).
        hangs = scenario.endswith("-hang") or scenario == "queue-lost-after-failure"
        self.process = SimpleNamespace(poll=lambda: -15 if hangs else 0, wait=wait)
        self.rows = []  # (due, row), in order
        self.rows_sent = False
        self.ended = False

    def initialize(self):
        return {}

    def check(self):
        import subprocess
        from covey.core.harness import JobCancelled

        if self.stop_at is not None and self.clock.monotonic() >= self.stop_at:
            self.cancel.set()
        if self.cancel.is_set():
            raise JobCancelled("Claude task cancelled")
        if self.deadline is not None and self.clock.monotonic() >= self.deadline:
            raise subprocess.TimeoutExpired("claude", 0)

    def send(self, row):
        if row.get("type") != "user":
            return
        now = self.clock.monotonic()
        if self.scenario.endswith("after-failure") and self.rows_sent:
            if self.scenario == "queued-after-failure":
                self.queue_after_failure(row)
            return
        self.rows_sent = True
        self.rows += [(now, {"type": "system", "subtype": "init", "session_id": SID}),
                      (now, {**row, "session_id": SID, "isReplay": True})]
        running, idle = ({"type": "system", "subtype": "session_state_changed", "session_id": SID, "state": state}
                         for state in ("running", "idle"))
        if self.scenario.endswith("after-failure"):
            self.failed_at = now + 5
            self.rows += [(now, running), (now, assistant("First part", "progress"))] + [(self.failed_at, row) for row in (
                {"type": "assistant", "session_id": SID, "uuid": "api-error", "isApiErrorMessage": True,
                 "error": "server_error", "message": {"id": "api-error", "content": [
                     {"type": "text", "text": "API Error: 502 upstream response could not be completed."}]}},
                result(uuid="result-1", is_error=True, terminal_reason="api_error", stop_reason="stop_sequence",
                       api_error_status=502, result="API Error: 502"), idle)]
            return
        if self.scenario == "rate-limited-hang":
            self.rows += [(now, row) for row in (
                running, {"type": "assistant", "session_id": SID, "uuid": "api-error", "error": "rate_limit",
                          "message": {"id": "api-error", "content": [{"type": "text", "text": "API Error: 429"}]}},
                result(uuid="result-1", is_error=True, terminal_reason="api_error", stop_reason="stop_sequence",
                       api_error_status=429, result="API Error: 429"), idle)]
            return
        if not self.scenario.startswith("background-"):
            self.rows += [(now, row) for row in (running, assistant("Complete answer", "answer-1"),
                                                 result(uuid="result-1"), idle)]
            return
        for n in range(1, (3 if self.scenario == "background-chain" else 1) + 1):
            due = now + (n - 1) * self.interval
            if n > 1:
                self.rows.append((due, {"type": "system", "subtype": "task_notification", "session_id": SID,
                                        "task_id": f"bg-{n - 1}", "tool_use_id": f"bg-{n - 1}-call",
                                        "status": "completed"}))
            self.rows += [(due, row) for row in (
                {"type": "system", "subtype": "session_state_changed", "session_id": SID, "state": "running"},
                {"type": "assistant", "session_id": SID, "uuid": f"wait-{n}", "message": {"id": f"wait-{n}",
                 "content": [{"type": "tool_use", "id": f"bg-{n}-call", "name": "Bash", "input": {}}]}},
                {"type": "system", "subtype": "task_started", "session_id": SID, "task_id": f"bg-{n}",
                 "tool_use_id": f"bg-{n}-call"},
                assistant(f"Reply {n}", f"answer-{n}"),
                result(uuid=f"result-{n}", result=f"Reply {n}"),
                {"type": "system", "subtype": "session_state_changed", "session_id": SID, "state": "idle"})]

    def queue_after_failure(self, row):
        """The supplement waits in the CLI's queue until the failed turn ends, then starts a turn."""
        due, done = self.failed_at + 0.1, self.failed_at + 30
        self.rows += [(due, {**row, "session_id": SID, "isReplay": True}),
                      (due, {"type": "system", "subtype": "session_state_changed", "session_id": SID, "state": "running"}),
                      (due, {"type": "stream_event", "session_id": SID, "event": {"type": "message_start"}}),
                      (done, assistant("Final after supplement", "answer-2")),
                      (done, result(uuid="result-2", result="Final after supplement")),
                      (done, {"type": "system", "subtype": "session_state_changed", "session_id": SID, "state": "idle"})]
        self.rows.sort(key=lambda item: item[0])

    def receive(self, timeout=0.1):
        self.check()
        if self.rows and self.rows[0][0] <= self.clock.monotonic():
            return self.rows.pop(0)[1]
        if self.ended:
            return {"type": "bridge_eof"}
        self.clock.advance(timeout)
        return None

    def end_input(self):
        late = {"late-turn-hang": [assistant("Late reply", "late"), result(uuid="result-late", result="Late reply")],
                "unfinished-hang": []}.get(self.scenario)
        if late is not None:
            now = self.clock.monotonic()
            self.rows += [(now, row) for row in (
                {"type": "system", "subtype": "session_state_changed", "session_id": SID, "state": "running"},
                {"type": "stream_event", "session_id": SID, "event": {"type": "message_start"}}, *late)]
            if late:
                self.rows.append((now, {"type": "system", "subtype": "session_state_changed", "session_id": SID,
                                        "state": "idle"}))
        self.ended = not (self.scenario.endswith("-hang") or self.scenario == "queue-lost-after-failure")

    def close(self):
        self.ended = True
        return True


# MASO SDK.

class Unreadable:
    def __getattribute__(self, name):
        raise AssertionError("non-public data was accessed")


def maso_message(message_id, text, *, tools=False, role="assistant", finish_reason="end_turn"):
    return SimpleNamespace(id=message_id, role=role, content=text,
                           tool_calls=[Unreadable()] if tools else [],
                           extra={"finish_reason": finish_reason, "reasoning_content": Unreadable()})
