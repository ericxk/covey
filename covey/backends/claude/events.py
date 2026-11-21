"""Claude Code stream-json rows as public Harness events."""

from __future__ import annotations

import time

from covey.backends.claude.policy import CALLS_SHOWN, call_words
from covey.backends.claude.usage import ClaudeSubtaskUsage, ClaudeTurnUsage
from covey.core.model_policy import policy_where
from covey.core.steps import AnswerNotes, Steps
from covey.core.text import short_count, tool_detail

# A step's one-line detail: the command, path, pattern, URL or query, else the tool name.
DETAIL_KEYS = ("command", "file_path", "pattern", "url", "query")
COMPACT_SLOT = "compact-auto"  # Within-turn compactions: compact-auto-1, compact-auto-2, ...


def launched_in_background(block, row):
    """An Agent call's result says Claude Code started the subagent in the background, which it also
    does without ``run_in_background`` (S31 K6: two Explore agents read 已完成 · 0.0 秒 at launch).
    Its task notification ends it. The result's structured status is read, else its fixed opening."""
    result = row.get("tool_use_result")
    if isinstance(result, dict) and (result.get("isAsync") is True or result.get("status") == "async_launched"):
        return True
    content = block.get("content")
    if isinstance(content, list):
        content = next((part.get("text") for part in content if isinstance(part, dict) and part.get("type") == "text"), "")
    return isinstance(content, str) and content.startswith("Async agent launched")


def compacted_sizes(meta):
    """A compact_boundary's sizes: the context before (with the system prompt and tools) and the summary
    that replaced the conversation (without them), as their own counts say."""
    before, after = meta.get("pre_tokens"), meta.get("post_tokens")
    parts = ([f"压缩前 {short_count(before)} token"] if type(before) is int and before > 0 else []) + (
        [f"摘要 {short_count(after)} token"] if type(after) is int and after > 0 else [])
    return " → ".join(parts) or "已压缩"


class ClaudeEventAdapter:
    def __init__(self, emit):
        self.emit = emit
        self.steps = Steps(emit)
        self.notes = AnswerNotes(emit)  # Root text before a tool call.
        self.partial_text = ""
        self.agents = {}
        self.commands = {}
        # The main task's Bash calls by tool id: their command, and whether the call asked to run in the background
        # (foreground_commands).
        self.shells = {}
        self.usage = ClaudeTurnUsage(emit)
        self.subtasks = ClaudeSubtaskUsage(emit)
        # The last CALLS_SHOWN tool calls of the main task (None) and of each subagent, in words: what one was doing
        # when it was refused (policy_where).
        self.calls = {}

    @property
    def tools(self):
        return self.steps.steps

    def accept(self, row):
        now = row.get("_bridge_received_at", time.time())
        self.usage.accept(row, now)
        self.subtasks.accept(row, now)
        child = row.get("parent_tool_use_id")
        row_type = row.get("type")
        message = row.get("message")
        message = message if isinstance(message, dict) else {}
        if row_type == "stream_event" and child is None:
            self.stream_delta(row.get("event") or {})
            return
        if row_type == "system" and row.get("subtype") in {"task_started", "task_progress", "task_notification"}:
            self.system_task(row)
            return
        if row_type == "system" and child is None and row.get("subtype") in {"status", "compact_boundary"}:
            self.compaction(row)
            return
        texts = []
        for block in message.get("content", []) if isinstance(message.get("content"), list) else []:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "tool_use" and isinstance(block.get("id"), str):
                self.tool_started(block, row, child)
            elif kind == "tool_result" and block.get("tool_use_id") in self.steps:
                self.tool_finished(block, row)
            elif kind == "text" and isinstance(block.get("text"), str) and row_type in (None, "assistant"):
                texts.append(block["text"])
        if texts:
            key = row.get("uuid") or message.get("id") or str(time.time_ns())
            body = "\n".join(texts)
            if child is not None:
                self.emit("progress_note", {"id": key, "text": body, "child_id": child})
            else:
                self.notes.text(key, body)
        if row_type == "result" and child is None and self.usage.last is None and self.usage.start is None:
            usage = row.get("usage") or {}
            if isinstance(usage.get("output_tokens"), int):
                self.emit("token_usage", {"output_tokens": usage["output_tokens"]})

    def stream_delta(self, event):
        """A root partial-message event: the reply text so far."""
        if event.get("type") == "message_start":
            self.partial_text = ""
        delta = event.get("delta") or {}
        if delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
            self.partial_text = (self.partial_text + delta["text"])[-4 * 1024 * 1024:]
            self.emit("answer_snapshot", {"text": self.partial_text})

    def system_task(self, row):
        """A task_started/progress/notification row: a shell command step or a child agent."""
        key = row.get("tool_use_id") or row.get("task_id")
        if isinstance(key, str) and key:
            state = {"completed": "completed", "failed": "errored", "stopped": "interrupted"}.get(row.get("status"), "running")
            if row.get("task_type") == "local_bash" or key in self.commands:
                # Claude Code reports long and background shell commands as tasks. They are
                # command steps with no child process events, not child agents.
                self.command_task(key, row, state)
                return
            agent = self.agents.setdefault(key, {"id": key, "name": "子任务", "started_at": time.time()})
            if agent["name"] == "子任务" and row.get("description"):
                agent["name"] = str(row["description"])[:80]
            if agent.get("status") in {"completed", "errored", "interrupted"} and state == "running":
                return
            agent.update(status=state)
            if state != "running":
                agent["ended_at"] = time.time()
            self.emit("agent_state", agent.copy())
            if state != "running":
                self.subtasks.finished(key, agent["ended_at"])

    def compaction(self, row):
        """Claude Code compacting the context within the turn (near the model's window, or the configured
        auto-compact window): ``status: compacting``, then ``compact_boundary`` with its sizes (2.1.310).
        Claude Code repeats ``status: compacting`` every 30 s while one compaction runs (2.1.318), so a
        repeat while a step is running is that step, not a new one (a 329 s compaction showed 11 steps)."""
        if row.get("subtype") == "status" and row.get("status") == "compacting":
            self.steps.open_slot(COMPACT_SLOT, "context_compaction", time.time(), "Claude Code 自动压缩")
        elif row.get("subtype") == "status" and row.get("compact_result") not in (None, "success"):
            self.steps.close_slot(COMPACT_SLOT, False, time.time(), "Claude Code 自动压缩没有成功")
        elif row.get("subtype") == "compact_boundary":
            meta = row.get("compact_metadata") if isinstance(row.get("compact_metadata"), dict) else {}
            manual = meta.get("trigger") == "manual"
            # A boundary with no status row before it is a compaction of its own that started and ended now.
            self.steps.close_slot(COMPACT_SLOT, True, time.time(),
                                  ("" if manual else "Claude Code 自动压缩 · ") + compacted_sizes(meta),
                                  name="context_compaction")

    def policy_where(self, signal):
        """Who a refusal ``signal`` (backends/claude/policy.py policy_signal) stopped and what it was doing, in words:
        the subagent's description from the Agent call that started it, the last tool calls of the one refused."""
        child = signal.get("child_id") or None if signal.get("scope") == "subagent" else None
        name = str(self.agents.get(child, {}).get("name") or "") if child else ""
        calls = list(self.calls.get(child, {}).values())
        return policy_where(signal.get("scope"), "" if name == "子任务" else name, "、".join(calls[-CALLS_SHOWN:]))

    def tool_started(self, block, row, child):
        """A tool_use block; a root one first publishes the notes written before it."""
        if child is None:
            self.notes.to_notes()
        inputs = block.get("input")
        inputs = inputs if isinstance(inputs, dict) else {}
        calls = self.calls.setdefault(child, {})
        calls.pop(block["id"], None)
        calls[block["id"]] = call_words(block.get("name", ""), inputs)
        while len(calls) > CALLS_SHOWN:
            calls.pop(next(iter(calls)))
        detail = tool_detail(inputs, DETAIL_KEYS, block.get("name", ""))
        if self.steps.start(block["id"], block.get("name", "tool").lower(), str(detail), time.time(),
                            child_id=row.get("parent_tool_use_id")) is None:
            return  # It ended: a repeat of its call changes nothing.
        if block.get("name") == "Bash" and child is None and isinstance(inputs.get("command"), str):
            self.shells[block["id"]] = {"command": inputs["command"], "background": inputs.get("run_in_background") is True}
        if block.get("name") in {"Agent", "Task"}:
            agent = self.agents.setdefault(block["id"], {"id": block["id"],
                "name": str(inputs.get("description") or "子任务")[:80],
                "started_at": time.time(), "status": "running", "parent_id": child,
                "background": inputs.get("run_in_background") is True})
            self.emit("agent_state", agent.copy())

    def foreground_commands(self):
        """The commands of the main task's Bash calls running now that it waits on: not one started in the background
        or moved there (backends/claude/command_stop.py)."""
        found = []
        for key, shell in list(self.shells.items()):
            step = self.steps.get(key) or {}
            if step.get("status") not in (None, "running"):
                del self.shells[key]  # Ended: never waited on again.
                continue
            if (step.get("status") == "running" and step.get("name") == "bash" and not shell["background"]
                    and not self.commands.get(key, {}).get("background")):
                found.append(shell["command"])
        return found

    def tool_finished(self, block, row):
        """A tool_result block for a known tool."""
        if self.commands.get(block["tool_use_id"], {}).get("background") and not block.get("is_error"):
            return  # Only starts the command; its task notification ends the step.
        self.steps.finish(block["tool_use_id"], not block.get("is_error"), time.time())
        if block["tool_use_id"] in self.agents:
            agent = self.agents[block["tool_use_id"]]
            if not block.get("is_error") and launched_in_background(block, row):
                if not agent.get("background"):
                    agent["background"] = True
                    self.emit("agent_state", agent.copy())  # The trace says it keeps the task open (core/progress.py background_tasks).
            if block.get("is_error") or not agent.get("background"):
                agent.update(status="errored" if block.get("is_error") else "completed", ended_at=time.time())
                self.emit("agent_state", agent.copy())
                self.subtasks.finished(block["tool_use_id"], agent["ended_at"])

    def command_task(self, key, row, state):
        command = self.commands.setdefault(key, {"background": False})
        tool = self.steps.get(key)
        if tool is None:
            return
        if row.get("subtype") == "task_started" and row.get("is_backgrounded") is True:
            command["background"] = True
            self.steps.update(key, name="background")
        elif command["background"] and state != "running" and tool["status"] == "running":
            self.steps.finish(key, state == "completed", time.time())
