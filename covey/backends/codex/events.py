"""Codex App Server items as public Harness events."""

from __future__ import annotations

import time

from covey.backends.codex.usage import CodexTurnUsage
from covey.core.protocol import STEP_ENDED
from covey.core.steps import Steps
from covey.core.usage import SubtaskReporter

# The items published as steps, by type, and each one's step name.
STEP_NAMES = {"commandExecution": "bash", "fileChange": "edit", "webSearch": "websearch", "mcpToolCall": "调用工具",
              "dynamicToolCall": "调用工具", "plan": "更新计划"}
# The Bridge's own workspace tools (server bridge_workspace), named for what they do, their path as the detail.
WORKSPACE_TOOLS = {"workspace_read_file": "读取文件", "workspace_list_directory": "浏览目录"}
FAILED = {"failed", "declined", "errored", "interrupted", "cancelled"}


def apply_agent_event(agents, item, now=None):
    now = time.time() if now is None else now
    ids = item.get("receiverThreadIds") or []
    states = item.get("agentsStates") or {}
    if item.get("type") == "subAgentActivity":
        ids = [item["agentThreadId"]]
        status = {"started": "running", "interrupted": "interrupted", "completed": "completed"}.get(item["kind"])
        states = {ids[0]: {"status": status}} if status else {}
    for sid in dict.fromkeys([*ids, *states]):
        agent = agents.setdefault(
            sid,
            {"name": item.get("agentPath") or f"子任务 {len(agents) + 1}", "started_at": now, "status": "pendingInit"},
        )
        if item.get("agentPath"):
            agent["name"] = item["agentPath"]
        if item.get("tool") == "spawnAgent" and item.get("prompt"):
            agent["description"] = " ".join(item["prompt"].split())[:180]
        state = states.get(sid) or {}
        if state.get("status"):
            agent["status"] = state["status"]
        if state.get("message"):
            agent["summary"] = state["message"][:1000]
        if agent["status"] in {"completed", "errored", "interrupted", "shutdown"}:
            agent.setdefault("ended_at", now)
        elif agent["status"] == "running" and agent.pop("ended_at", None) is not None:
            agent["started_at"] = max(now, agent["started_at"] + 0.000001)  # Run again (resumeAgent): a new start.
    return agents


class CodexEventAdapter:
    def __init__(self, emit, main_thread=""):
        self.emit = emit
        # The turn's own thread is no subtask: a subtask that messages it (sendInput to /root) names it among the
        # receivers. T272 #2611 (2026-11-03) listed /root under each subtask until the two held each other.
        self.main_thread = main_thread
        self.steps = Steps(emit)
        self.agent_states = {}  # subtask thread -> its agent_state (apply_agent_event)
        self.child_usage = {}  # child thread -> CodexTurnUsage over its native cumulative counters
        self.reporter = SubtaskReporter(emit)

    def item(self, method, item, received_at=None, child_id=None):
        kind, key = item.get("type"), item.get("id")
        if kind not in STEP_NAMES or not key:
            return
        now = received_at or time.time()
        old = self.steps.get(key) or {}
        done = method == "item/completed"
        failed = (
            item.get("status") in FAILED
            or bool(item.get("error"))
            or item.get("success") is False
            or item.get("exitCode") not in (None, 0)
        )
        if old.get("status") in STEP_ENDED and not done:
            return  # Before reading the rest: a late start of an ended item is dropped whatever it holds.
        name, detail = STEP_NAMES[kind], item.get("command") or item.get("query") or item.get("tool") or ""
        if kind == "mcpToolCall" and item.get("server") == "bridge_workspace":
            name = WORKSPACE_TOOLS.get(item.get("tool"), name)
            args = item.get("arguments")
            if item.get("tool") in WORKSPACE_TOOLS and isinstance(args, dict) and isinstance(args.get("path"), str):
                detail = args["path"]
        if kind == "fileChange":
            detail = ", ".join(str(c.get("path", "")) for c in (item.get("changes") or []))
        if kind == "webSearch":
            detail = (item.get("action") or {}).get("query") or detail
        duration = item.get("durationMs")
        start = old.get("started_at", now - duration / 1000 if done and isinstance(duration, (int, float)) else now)
        self.steps.put({
            "id": key,
            "name": name,
            "detail": detail or old.get("detail", ""),
            "status": ("failed" if failed else "done") if done else "running",
            "started_at": start,
            "ended_at": now if done else None,
            "exit_code": item.get("exitCode"),
            "child_id": child_id,
        })

    def agents(self, item, received_at=None, parent_id=None):
        known = set(self.agent_states)
        apply_agent_event(self.agent_states, item, received_at)
        main = self.main_thread
        self.agent_states.pop(main, None)
        touched = set(item.get("receiverThreadIds") or []) | set(item.get("agentsStates") or {})
        if item.get("agentThreadId"):
            touched.add(item["agentThreadId"])
        touched.discard(main)
        # A sub-agent counts from its start, not from its first usage: until a report the card cannot
        # tell this backend from one that never reports subtasks (子任务未计入).
        self.publish_subtasks(received_at or time.time())
        for sid in touched:
            state = self.agent_states[sid]
            # Its parent is the thread it was first seen from (the one that spawned it): a later wait or message
            # from another thread does not move it, as moving it by each message made subtasks parents of each other.
            if parent_id and sid != parent_id and sid not in known:
                state["parent_id"] = parent_id
            self.emit("agent_state", {"id": sid, **state})
        tool = item.get("tool")
        if tool in {"wait", "waitAgent", "sendInput", "resumeAgent", "closeAgent"}:
            labels = {"wait": "等待子任务", "waitAgent": "等待子任务", "sendInput": "补充子任务", "resumeAgent": "恢复子任务", "closeAgent": "关闭子任务"}
            self.emit("progress_note", {"id": "collab-" + str(item.get("id") or tool),
                "text": labels[tool] + "：" + "、".join(item.get("receiverThreadIds") or []),
                "received_at": received_at, "child_id": parent_id})

    def child_tokens(self, sid, usage, received_at=None):
        """A child thread's ``thread/tokenUsage/updated``: its counters less what it had used before its
        first request seen here (a resumed child keeps its earlier turns), as CodexTurnUsage does."""
        if sid and isinstance(usage, dict):
            self.child_usage.setdefault(sid, CodexTurnUsage()).update(usage, True)
            self.publish_subtasks(received_at or time.time())

    def subtask_report(self):
        states = self.agent_states
        agents = {}
        for sid, counter in self.child_usage.items():
            counts = counter.last
            if not counts:
                continue
            level, parent = 1, states.get(sid, {}).get("parent_id")
            while parent and parent in states and level < 20:
                level, parent = level + 1, states[parent].get("parent_id")
            agents[sid] = {"name": states.get(sid, {}).get("name") or "子任务", "level": level,
                           "input_tokens": counts.get("input_tokens"), "cache_read_tokens": counts.get("cache_read_tokens"),
                           "cache_write_tokens": None, "output_tokens": counts["output_tokens"]}
        if not agents and not states:
            return None
        return {"agents": agents, "unreported": sum(1 for sid in states if sid not in agents), "pending": 0}

    def publish_subtasks(self, now, force=False):
        """Sub-agent usage, when the turn has sub-agents; those without any usage are unreported."""
        report = self.subtask_report()
        if report is not None:
            self.reporter.publish(report, now, force)

    def child(self, method, params, received_at=None):
        sid = params.get("threadId")
        item = params.get("item") or {}
        if not sid:
            return
        if method in {"item/started", "item/completed"}:
            self.item(method, item, received_at, sid)
            if item.get("type") in {"collabAgentToolCall", "subAgentActivity"}:
                self.agents(item, received_at, sid)
            elif method == "item/completed" and item.get("type") == "agentMessage" and item.get("text"):
                self.emit("progress_note", {"id": str(item.get("id")), "text": item["text"], "child_id": sid, "received_at": received_at})
        if method == "turn/completed":
            state = {"completed": "completed", "failed": "errored", "interrupted": "interrupted"}.get((params.get("turn") or {}).get("status"), "running")
            self.agents({"agentsStates": {sid: {"status": state}}}, received_at)
