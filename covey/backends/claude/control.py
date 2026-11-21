"""The control side of a running Claude CLI (ClaudeControl): the prompt and supplements it is sent, the root
AskUserQuestion it asks and its answer, the other tool permission requests denied, and the turn's SDK hook callbacks
answered (backends/claude/hooks.py), under one control lock."""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid

from covey.backends.claude import command_stop
from covey.core.steer import SteerNotReady, keeps_running, refuse_steer, steer_text


def normalized_questions(raw):
    if not isinstance(raw, list) or not 1 <= len(raw) <= 4:
        raise ValueError("Invalid Claude questions")
    result = []
    for i, q in enumerate(raw):
        if (
            not isinstance(q, dict)
            or not isinstance(q.get("question"), str)
            or not q["question"].strip()
            or q.get("isSecret")
        ):
            raise ValueError("Invalid or secret Claude question")
        options = q.get("options")
        if not isinstance(options, list) or not 2 <= len(options) <= 4:
            raise ValueError("Invalid Claude question options")
        if any(not isinstance(o, dict) or not isinstance(o.get("label"), str) or not o["label"] for o in options):
            raise ValueError("Invalid Claude question option")
        result.append(
            {
                "id": str(i),
                "question": q["question"],
                "header": str(q.get("header") or "补充信息"),
                "multiple": q.get("multiSelect") is True,
                "options": [{"label": o["label"], "description": str(o.get("description") or "")} for o in options],
            }
        )
    if len({q["question"] for q in result}) != len(result):
        raise ValueError("Ambiguous Claude question keys")
    return result


class ClaudeControl:
    native_blocking_questions = True
    supports_steer = True

    @property
    def awaiting_answer(self):
        """A native question keeps the turn open until it is answered; the turn takes no supplement meanwhile."""
        return bool(self.user_requests)

    def __init__(self, process, state, emit, hooks=None):
        self.transport, self.reply_state, self.emit = process, state, emit
        self.hooks = hooks  # The turn's SDK hook callbacks (backends/claude/hooks.py TurnHooks), or None.
        self.control_lock = threading.RLock()
        self.user_requests = set()
        self.questions = {}
        self.answered = {}
        self.calls = {}
        self.accepting_steer = False
        self.closing = False
        self.steers = {}  # input uuid -> the bridge's event id, until Claude replays it
        self.prompt_id = ""  # The turn's own prompt, until Claude replays it (prompt_read).
        self.control_error = False
        self.foreground = lambda: []  # The commands the main task waits on (run_stream: its event adapter's).
        if getattr(process, "device", False) is True:
            # The CLI runs on a device: its patience while the link is down is the longer one for as long as a
            # question waits for the user (core/device_link.py).
            process.process.set_hold(lambda: bool(self.user_requests))

    @property
    def offline(self):
        """The CLI runs on a device and the link to it is down."""
        transport = getattr(self, "transport", None)
        return getattr(transport, "device", False) is True and bool(transport.process.offline)

    @property
    def session_id(self):
        return self.reply_state.session_id or ""

    def send_user(self, content, input_id):
        self.reply_state.submit(input_id)
        try:
            self.transport.send(
                {
                    "type": "user",
                    "uuid": input_id,
                    "session_id": self.session_id or "default",
                    "parent_tool_use_id": None,
                    "message": {"role": "user", "content": content},
                }
            )
        except Exception:
            self.control_error = True
            raise

    def steer(self, text, event_id):
        with self.control_lock:
            if self.offline:  # Before send_user: a failed send would set control_error and end the turn.
                raise SteerNotReady("设备连接中断，补充没有发出")
            refuse_steer(text, waiting=bool(self.user_requests), closing=self.closing, ready=self.accepting_steer,
                         label="Claude")
            input_id = str(uuid.uuid4())
            self.send_user(steer_text(text), input_id)
            self.steers[input_id] = event_id

    def stop_command(self, note=""):
        """Stop the shell commands the main task waits on, so the supplements sent to it are read now
        (app/steer_interrupt.py); ``note`` goes in first, as a supplement of its own, saying why the command ended.
        Returns {"stopped": [commands]}, {"kept": command} for one never stopped (core/steer.py ``keeps_running``),
        or {"reason": why nothing was stopped}."""
        with self.control_lock:
            if getattr(self.transport, "device", False) is True:
                return {"reason": "任务在设备上运行，不能从这里停命令"}
            if self.closing:
                return {"reason": "任务正在收尾"}
            if self.user_requests:
                return {"reason": "任务正在等待回答问题"}
            commands = list(self.foreground())
            if not commands:
                return {"reason": "模型现在没有在等命令"}
            kept = next((command for command in commands if keeps_running(command)), None)
            if kept is not None:
                return {"kept": kept}
            found = command_stop.shells(self.transport.process.pid, commands)
            if not found:
                return {"reason": "没找到这条命令的进程"}
            if note:
                self.send_user(steer_text(note), str(uuid.uuid4()))
        command_stop.stop(found)  # Outside the lock: it waits for the shells to exit.
        return {"stopped": commands}

    def verify_question_request(self, proof):
        rid = proof.get("request_id") if isinstance(proof, dict) else None
        pending = self.questions.get(rid)
        return bool(
            pending
            and pending["proof"] == proof
            and rid in self.user_requests
            and self.accepting_steer
            and not self.control_error
            and not self.transport.cancel.is_set()
            and self.transport.process.poll() is None
            and not self.transport.stop.is_set()
            and (self.transport.deadline is None or time.monotonic() < self.transport.deadline)
            and proof["generation"] == self.reply_state.generation
            and proof["session_id"] == self.session_id
        )

    def response(self, rid, value):
        try:
            self.transport.send(
                {"type": "control_response", "response": {"subtype": "success", "request_id": rid, "response": value}}
            )
        except Exception:
            self.control_error = True
            raise

    def answer(self, request_id, answers):
        with self.control_lock:
            pending = self.questions.get(request_id)
            if not pending or not self.verify_question_request(pending["proof"]):
                raise ValueError("Claude 问题已失效")
            selected = {}
            for q in pending["questions"]:
                values = answers.get(q["id"], {}).get("answers")
                if (
                    not isinstance(values, list)
                    or not values
                    or not all(isinstance(v, str) and v.strip() for v in values)
                ):
                    raise ValueError("Claude 回答不完整")
                selected[q["question"]] = ", ".join(values)
            self.response(request_id, {"behavior": "allow", "updatedInput": {**pending["input"], "answers": selected}})
            self.user_requests.discard(request_id)
            self.answered[pending["proof"]["call_id"]] = request_id

    def reject_question(self, request_id, reason):
        with self.control_lock:
            self.response(request_id, {"behavior": "deny", "message": reason})
            self.user_requests.discard(request_id)
            self.questions.pop(request_id, None)

    def wait_for_answer(self, question):
        pass  # The CLI's pending permission request already suspends this tool.

    def accept(self, row):
        with self.control_lock:
            if row.get("type") == "user" and row.get("uuid") in self.steers:
                # Claude replays a queued input when it takes it into the conversation.
                self.emit("steer_read", {"event_id": self.steers.pop(row["uuid"])})
            elif row.get("type") == "user" and self.prompt_id and row.get("uuid") == self.prompt_id:
                # The prompt is in the session now: the turn a ↩️ 回退 would drop starts here (app/rewind.py).
                self.emit("prompt_read", {"uuid": self.prompt_id, "session_id": row.get("session_id") or self.session_id})
                self.prompt_id = ""
            message = row.get("message")
            message = message if isinstance(message, dict) else {}
            if row.get("session_id") == self.session_id and row.get("parent_tool_use_id") is None:
                for block in message.get("content", []) if isinstance(message.get("content"), list) else []:
                    if not isinstance(block, dict):
                        continue
                    if row.get("type") == "assistant" and block.get("type") == "tool_use":
                        self.calls[block.get("id")] = (row.get("uuid") or message.get("id"), block)
                    elif block.get("type") == "tool_result" and block.get("tool_use_id") in self.answered:
                        rid = self.answered.pop(block["tool_use_id"])
                        self.questions.pop(rid, None)
                        if not block.get("is_error"):
                            self.emit("question_resolved", {"requestId": rid})
            if row.get("type") == "control_cancel_request":
                rid = row.get("request_id")
                self.user_requests.discard(rid)
                self.questions.pop(rid, None)
                self.emit("question_cancelled", {"requestId": rid})
                return
            if row.get("type") != "control_request":
                return
            rid, request = row.get("request_id"), row.get("request")
            if not isinstance(request, dict):
                raise ValueError("Invalid Claude control request payload")
            if not isinstance(rid, str) or not rid or rid in self.questions:
                raise ValueError("Invalid Claude control request")
            if request.get("subtype") == "hook_callback":
                # A hook callback the turn registered (backends/claude/hooks.py); {} goes on as without one.
                self.response(rid, self.hooks.answer(request.get("callback_id"), request.get("input"))
                              if self.hooks is not None else {})
                return
            if (
                request.get("subtype") != "can_use_tool"
                or request.get("tool_name") != "AskUserQuestion"
                or request.get("agent_id")
            ):
                self.response(
                    rid,
                    {
                        "behavior": "deny",
                        "message": "This bridge only collects root AskUserQuestion answers; other permission requests are denied.",
                    },
                )
                return
            call_id = request.get("tool_use_id")
            known = self.calls.get(call_id)
            original = request.get("input")
            if not known or not known[0] or not isinstance(original, dict) or known[1].get("input") != original:
                self.response(
                    rid, {"behavior": "deny", "message": "Question does not match this run's root tool call."}
                )
                raise ValueError("Unbound Claude question")
            questions = normalized_questions(original.get("questions"))
            proof = {
                "version": 1,
                "backend": self.reply_state.ctx.options.get("harness_id", "claude"),
                "run_id": self.reply_state.run_id,
                "generation": self.reply_state.generation,
                "session_id": self.session_id,
                "request_id": rid,
                "request_session_id": self.session_id,
                "message_id": known[0],
                "call_id": call_id,
                "questions_sha256": hashlib.sha256(
                    json.dumps(questions, ensure_ascii=False, sort_keys=True).encode()
                ).hexdigest(),
            }
            self.questions[rid] = {"proof": proof, "questions": questions, "input": original}
            self.user_requests.add(rid)
            self.emit(
                "question",
                {"request_id": rid, "questions": questions, "isBlocking": True, "live_question_proof": proof},
            )
