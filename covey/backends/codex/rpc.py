"""The Codex App Server process and its JSON-RPC over stdio (AppServer): requests and their replies, server
requests answered, supplements steered in; a failed exchange is an RpcError and is never replayed."""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import time

from covey.core.process import device_of, spawn_process, stop_group
from covey.core.reply_state import ReplyError, failure_text
from covey.core.result import is_identifier
from covey.core.steer import SteerNotReady, SteerRefused
from covey.core.ttft import codex_marker


class RpcError(ReplyError):
    """A failed exchange with the app server, or a failure the turn ends with (``code``)."""


def failed(code):
    """The RpcError that ends the turn with ``code``, worded as Codex's failure text."""
    return RpcError(failure_text(code), code)


class AppServer:
    device = False  # True for an app server on a device (ctx.device), see __init__.

    def __init__(self, argv, cwd, env, ctx=None):
        # On a device the process is a DeviceProcess (core/device_link.py): no pid, no group of ours.
        self.device = bool(device_of(ctx))
        self.process = spawn_process(
            argv,
            ctx=ctx,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.events = queue.Queue()
        self.pending = {}
        self.lock = threading.RLock()
        self.counter = 0
        self.input_lock = threading.RLock()
        self.accepting_input = True
        self.reply_state = None
        self.input_ids = []
        self.control_error = False
        self.cleanup_confirmed = False
        self.thread_id = ""
        self.turn_id = ""
        self.closed = False
        self.user_requests = set()
        self.pause_for_question = ""
        self.paused_question_proof = None
        self._question_request_id = None
        self._paused_question = None
        if self.device:  # While the link is down, the longer patience counts if a question waits for the user.
            self.process.set_hold(lambda: bool(self.user_requests) or bool(self.pause_for_question))
        self.readers = [threading.Thread(target=self._read, daemon=True),
                        threading.Thread(target=self._stderr, daemon=True)]
        for reader in self.readers:
            reader.start()

    def _stderr(self):
        for _ in self.process.stderr:
            pass  # Human stderr may contain prompts, tool output or reasoning.

    def _read(self):
        try:
            for line in self.process.stdout:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if "method" in row:
                    row = codex_marker(row) or row
                    method = row["method"]
                    if (method.endswith(("Delta", "/delta")) and method != "item/agentMessage/delta") or "reasoning" in method.lower():
                        continue
                    row["_received_at"] = time.time()
                    self.events.put(row)
                elif "id" in row:
                    with self.lock:
                        waiter = self.pending.get(row["id"])
                    if waiter:
                        waiter.put(row)
        finally:
            self.closed = True
            with self.lock:
                for waiter in self.pending.values():
                    waiter.put({"error": {"message": "App Server connection closed"}})
            self.events.put({"method": "bridge/closed"})

    def send(self, row):
        with self.lock:
            if self.closed:
                raise RpcError("App Server connection closed")
            try:
                self.process.stdin.write(json.dumps(row, ensure_ascii=False) + "\n")
                self.process.stdin.flush()
            except (OSError, ValueError) as exc:
                raise RpcError("App Server write failed") from exc

    def request(self, method, params, timeout=30):
        with self.lock:
            self.counter += 1
            rid = self.counter
            waiter = queue.Queue()
            self.pending[rid] = waiter
        try:
            self.send({"id": rid, "method": method, "params": params})
            try:
                row = waiter.get(timeout=timeout)
            except queue.Empty as exc:
                raise RpcError(f"{method} response timed out; delivery is unknown") from exc
            if "error" in row:
                raise RpcError("Codex RPC 请求失败")
            return row.get("result", {})
        finally:
            with self.lock:
                self.pending.pop(rid, None)

    def reject_question(self, request_id, reason):
        self.send({"id": request_id, "error": {"code": -32602, "message": reason}})

    def wait_for_answer(self, question):
        with self.input_lock:
            rid = getattr(self, "_question_request_id", None)
            if (not self.accepting_input or not request_identifier(rid) or rid not in self.user_requests
                    or not isinstance(question, str) or not question.strip() or not self.reply_state):
                raise failed("root_incomplete")
            captured = {"run_id": self.reply_state.run_id, "generation": self.reply_state.generation,
                        "session_id": self.thread_id, "turn_id": self.turn_id, "request_ids": [rid]}
            prior = getattr(self, "_paused_question", None)
            if prior and all(prior[key] == captured[key] for key in ("run_id", "generation", "session_id", "turn_id")):
                captured["request_ids"] = list(dict.fromkeys([*prior["request_ids"], rid]))
            try:
                self.request("turn/interrupt", {"threadId": self.thread_id, "turnId": self.turn_id}, timeout=10)
            except Exception:
                self.control_error = True
                raise
            self.pause_for_question = question
            self._paused_question = captured

    def answer(self, rid, answers):
        with self.input_lock:
            if not self.accepting_input or rid not in self.user_requests:
                raise RpcError("当前问题已不可回答")
            if self.reply_state:
                self.reply_state.invalidate()
            try:
                self.send({"id": rid, "result": {"answers": answers}})
                self.input_ids.append("answer:" + str(rid))
            except Exception:
                self.control_error = True
                raise

    def steer(self, text, message_id):
        with self.input_lock:
            if self.device and self.process.offline:  # Before the request: a failure there sets control_error.
                raise SteerNotReady("设备连接中断，补充没有发出")
            if not self.accepting_input or not self.turn_id:
                raise SteerRefused("任务尚未就绪或已经结束，补充没有发出")
            if self.reply_state:
                self.reply_state.invalidate()
            try:
                result = self.request("turn/steer", {
                    "threadId": self.thread_id, "expectedTurnId": self.turn_id,
                    "input": [{"type": "text", "text": text}],
                    "clientUserMessageId": message_id}, timeout=15)
                if not isinstance(result, dict) or result.get("turnId") != self.turn_id:
                    raise RpcError("Codex 补充输入的归属未确认")
                self.input_ids.append("steer:" + message_id)
                return result
            except Exception:
                self.control_error = True
                raise

    CLOSE_GRACE = 5

    def close(self):
        with self.input_lock:
            self.accepting_input = False
            self.turn_id = ""
        if self.process.poll() is None:
            try:
                self.process.stdin.close()
                self.process.wait(timeout=self.CLOSE_GRACE)
            except (OSError, subprocess.TimeoutExpired):
                # Commands and MCP servers the app server started share its process group.
                stop_group(self.process, self.CLOSE_GRACE)
        for reader in self.readers:
            reader.join(timeout=1)
        self.cleanup_confirmed = (type(self.process.poll()) is int
                                  and not any(t.is_alive() for t in self.readers))
        if self.device:  # Only an app server the device reported ended, and left nothing, is cleaned up.
            self.cleanup_confirmed = self.process.close() and self.cleanup_confirmed
        return self.cleanup_confirmed


def request_identifier(value):
    return type(value) is int or is_identifier(value)
