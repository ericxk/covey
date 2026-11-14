"""A turn's execution trace as data (ToolTrace): its tool steps, notes and subtasks from the backend's events, the
snapshot a task record keeps and restores, and the turn's token usage (turn_usage). Bounded and public: no tool output
or reasoning bodies. cards/trace_view.py draws it."""
import copy
import time
from covey.core.protocol import AGENT_ENDED, AGENT_LIVE, STEP_ENDED, STEP_TITLES
from covey.core.text import safe_text
from covey.core.ttft import SOURCES
from covey.core.usage import number, rate


TRACE_BYTES = 24000  # The process panel's size in bytes of JSON, which a trace's snapshot carries.
# A step that writes this task's checkpoint file (outside the workspace; the bridge rules require it).
# Codex's python heredoc doing so read as an opaque file edit under "不要修改任何文件" (S29 #867).
CHECKPOINT_STEP = "更新检查点"
CHECKPOINT_TOOLS = {"bash", "edit", "write", "multiedit"}  # Codex commands and file changes; Claude's tools.
USAGE_FIELDS = ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens")
# The main task's latest request (its input and output) and its model's window: the context the next
# turn resumes with (the 上下文 tile; the main thread's compaction threshold reads it, app/main_thread.py).
CONTEXT_FIELDS = ("context_tokens", "context_window")
SUBTASK_ROWS = 5  # Up to this many subtasks are listed one by one; more are summed per level.
SUBTASK_LEVELS = 10  # Deeper levels are summed into the last one.


def _count(value):
    return value if type(value) is int and value >= 0 else None


def reduce_subtask_usage(data):
    """A backend's ``subtask_usage`` report as the process record keeps it, bounded: per level, and
    each subtask when there are few. None when malformed. ``count`` is the subtasks with usage."""
    agents = data.get("agents")
    if not isinstance(agents, dict):
        return None
    levels, rows = {}, []
    for agent in agents.values():
        if not isinstance(agent, dict):
            return None
        level = agent.get("level")
        level = min(level, SUBTASK_LEVELS) if type(level) is int and level >= 1 else 1
        counts = {key: _count(agent.get(key)) for key in USAGE_FIELDS}
        for key in ("cache_read_tokens", "cache_write_tokens"):
            if counts[key] is not None and (counts["input_tokens"] is None or counts[key] > counts["input_tokens"]):
                counts[key] = None
        bucket = levels.setdefault(level, {"level": level, "count": 0, **{key: 0 for key in USAGE_FIELDS}})
        bucket["count"] += 1
        for key in USAGE_FIELDS:
            bucket[key] = None if bucket[key] is None or counts[key] is None else bucket[key] + counts[key]
        rows.append({"name": str(agent.get("name") or "子任务")[:80], "level": level, **counts})
    report = {"count": len(rows), "levels": [levels[level] for level in sorted(levels)],
              "unreported": _count(data.get("unreported")) or 0, "pending": _count(data.get("pending")) or 0}
    if len(rows) <= SUBTASK_ROWS:
        report["agents"] = sorted(rows, key=lambda row: row["level"])
    return report


def agent_ids(snapshot):
    """Every subtask a process snapshot knows, once: a nested one sits under its parent and the root."""
    seen, stack = set(), [snapshot]
    while stack:
        node = stack.pop()
        seen.update(node.get("agents") or {})
        stack.extend((node.get("children") or {}).values())
    return seen


def turn_usage(snapshot):
    """This turn's token figures from a process snapshot: the main task (the root's own counts), its
    subtasks at every level as the backend reported them, their sum, and what the sum lacks.

    ``reported`` is False for a snapshot written before subtask totals existed: its counts are the
    main task's only. Subtasks the snapshot shows that no report covers are ``unreported``. While the
    turn runs, a backend that reports subtasks is only named as still running, and ``subtasks`` (the
    ones the total is for) counts every subtask started so far, with usage or not: the trace knows of
    one before the next report, which comes at most every 10 s (S31 K6 read 主任务 for 16 s). After
    the turn it is the subtasks with usage."""
    report = snapshot.get("subtask_usage")
    reported = isinstance(report, dict)
    report = report if reported else {}
    main = {key: _count(snapshot.get(key)) for key in USAGE_FIELDS}
    levels = report.get("levels") if isinstance(report.get("levels"), list) else []
    count = report.get("count") if type(report.get("count")) is int else 0
    unreported = report["unreported"] if "unreported" in report else len(agent_ids(snapshot))
    total = dict(main)
    if count:
        for key in USAGE_FIELDS:
            parts = [main[key], *(level.get(key) for level in levels)]
            total[key] = sum(parts) if all(type(part) is int for part in parts) else None
    missing = []
    running = snapshot.get("ended_at") is None and (count or "unreported" in report)
    subtasks = max(count + unreported, len(agent_ids(snapshot))) if running else count
    if running:
        if subtasks:
            missing.append("运行中")
    else:
        if unreported:
            missing.append(f"{unreported} 个子任务未计入")
        if report.get("pending"):
            missing.append(f"{report['pending']} 次子任务请求的输出 token 未取到最终值")
    agents = report.get("agents") if isinstance(report.get("agents"), list) else None
    return {"reported": reported, "main": main, "total": total, "count": count, "unreported": unreported,
            "subtasks": subtasks, "levels": levels, "agents": agents, "missing": missing}


def task_trace():
    """A task's root trace. Its snapshot carries ``subtask_usage`` (empty until a report), which tells
    it from one written before subtask totals existed."""
    trace = ToolTrace()
    trace.subtask_usage = {}
    return trace


class ToolTrace:
    def __init__(self):
        self.steps = {}
        self.total = 0
        self.started_at = time.time()
        self.ended_at = None
        self.agents = {}
        self.children = {}
        self.public_progress = False
        # The task was stopped: steps still running when it ended read 已停止, not 结束状态未确认.
        self.stopped = False
        # This task's checkpoint file (the worker sets it): a step naming it reads CHECKPOINT_STEP.
        self.checkpoint = ""
        # The root's subtask usage report (reduce_subtask_usage); None in a child or an older snapshot.
        self.subtask_usage = None
        # A redacted topic's turn: {terms, hits, requests} from its ``redaction`` event; None otherwise.
        self.redaction = None
        # The id of the model the main task ran, when its backend says (token_usage ``model``: Claude's).
        self.model = None

    def snapshot(self):
        snapshot = {"schema_version": 1, "steps": copy.deepcopy(self.steps), "step_order": list(self.steps), "total": self.total, "started_at": self.started_at,
                    "ended_at": self.ended_at, "input_tokens": getattr(self, "input_tokens", None), "output_tokens": getattr(self, "output_tokens", None),
                    "cache_read_tokens": getattr(self, "cache_read_tokens", None),
                    "cache_write_tokens": getattr(self, "cache_write_tokens", None),
                    "measured_output_tokens": getattr(self, "measured_output_tokens", None),
                    "generation_seconds": getattr(self, "generation_seconds", None),
                    "ttft_seconds": getattr(self, "ttft_seconds", None),
                    "ttft_source": getattr(self, "ttft_source", None),
                    "trace_byte_budget": getattr(self, "trace_byte_budget", TRACE_BYTES),
                    "agents": copy.deepcopy(self.agents), "children": {sid: c.snapshot() for sid, c in self.children.items()}}
        if self.subtask_usage is not None:
            snapshot["subtask_usage"] = copy.deepcopy(self.subtask_usage)
        if self.redaction is not None:
            snapshot["redaction"] = dict(self.redaction)
        for key in CONTEXT_FIELDS:
            if getattr(self, key, None):
                snapshot[key] = getattr(self, key)
        if getattr(self, "model", None):
            snapshot["model"] = self.model
        if self.public_progress:
            snapshot["public_progress"] = True
        if self.stopped:
            snapshot["stopped"] = True
        return snapshot

    @classmethod
    def restore(cls, data):
        trace = cls()
        steps = copy.deepcopy(data.get("steps") or {})
        order = data.get("step_order", list(steps))
        trace.steps = {key: steps[key] for key in order if key in steps}
        trace.steps.update({key: value for key, value in steps.items() if key not in trace.steps})
        trace.total = data.get("total", len(trace.steps))
        trace.started_at = data.get("started_at", trace.started_at)
        trace.ended_at = data.get("ended_at")
        trace.input_tokens = data.get("input_tokens")
        for key in ("cache_read_tokens", "cache_write_tokens"):
            value = data.get(key)
            setattr(trace, key, value if type(value) is int and type(trace.input_tokens) is int and 0 <= value <= trace.input_tokens else None)
        trace.trace_byte_budget = data.get("trace_byte_budget", TRACE_BYTES)
        trace.output_tokens = data.get("output_tokens")
        trace.measured_output_tokens = data.get("measured_output_tokens")
        trace.generation_seconds = data.get("generation_seconds")
        for key in CONTEXT_FIELDS:
            value = data.get(key)
            setattr(trace, key, value if type(value) is int and value > 0 else None)
        trace.model = data["model"] if isinstance(data.get("model"), str) and data["model"] else None
        trace.accept("first_token", data)
        trace.agents = copy.deepcopy(data.get("agents") or {})
        trace.children = {sid: cls.restore(child) for sid, child in (data.get("children") or {}).items()}
        trace.public_progress = data.get("public_progress") is True
        trace.stopped = data.get("stopped") is True
        usage = data.get("subtask_usage")
        trace.subtask_usage = copy.deepcopy(usage) if isinstance(usage, dict) else None
        trace.accept("redaction", data.get("redaction") or {})
        return trace

    def tree(self):
        """This trace and each subtask below it, at any depth, once (a nested subtask is also the root's child)."""
        seen, stack = set(), [self]
        while stack:
            node = stack.pop()
            if id(node) not in seen:
                seen.add(id(node))
                yield node
                stack.extend(node.children.values())

    def holds(self, trace):
        """Whether ``trace`` is this one or a subtask below it, at any depth."""
        return any(node is trace for node in self.tree())

    def accept(self, kind, data):
        """Reduce a public event into the execution snapshot; independent of Feishu."""
        if kind == "tool":
            target = self.children.setdefault(data["child_id"], ToolTrace()) if data.get("child_id") else self
            changed = target.apply_normalized(data, checkpoint=self.checkpoint)
            enable_summary = data.get("public_progress") is True and not self.public_progress
            if enable_summary:
                self.public_progress = True
            return changed or enable_summary
        if kind == "progress_note":
            target = self.children.setdefault(data["child_id"], ToolTrace()) if data.get("child_id") else self
            return target.note(data["id"], data["text"], data.get("title", "进展说明"), data.get("received_at"))
        if kind == "agent_state":
            sid = data["id"]
            agent = {k: v for k, v in data.items() if k != "id"}
            parent_id = data.get("parent_id")
            parent = self.children.setdefault(parent_id, ToolTrace()) if parent_id and parent_id != sid else self
            child = self.children.setdefault(sid, ToolTrace())
            if parent is not self and child.holds(parent):
                # Under ``parent`` it would hold itself: every snapshot, and so every card update, then failed with
                # RecursionError (T272 #2611, 2026-11-03: Codex named subtasks parents of each other). It stays where it is.
                parent = self
            previous = parent.agents.get(sid, {})
            if previous == agent:
                return False
            if previous.get("status") in AGENT_ENDED and agent.get("status") in AGENT_LIVE and agent.get("started_at", 0) <= previous.get("started_at", 0):
                return False
            parent.sync_agents({sid: agent})
            if parent is not self:
                parent.children[sid] = child
                self.agents.pop(sid, None)
                self.steps.pop('agent-' + sid, None)
                self.total = len(self.steps)
            if data.get("status") in AGENT_ENDED:
                child.close(time.time())
            return True
        if kind == "first_token":
            seconds, source = data.get("ttft_seconds"), data.get("ttft_source")
            if getattr(self, "ttft_seconds", None) is not None or not number(seconds) or source not in SOURCES:
                return False
            self.ttft_seconds, self.ttft_source = seconds, source
            return True
        if kind == "token_usage":
            if isinstance(data.get("model"), str) and data["model"]:
                self.model = data["model"][:200]
            total = data.get("output_tokens")
            if type(total) is int and total >= 0:
                if total < (getattr(self, "output_tokens", None) or 0):
                    return False
                self.output_tokens = total
                if "input_tokens" in data:
                    incoming = data["input_tokens"]
                    # An incomplete cumulative count must not masquerade as a total.
                    if incoming is None:
                        self.input_tokens = None
                    elif type(incoming) is int and incoming >= (getattr(self, "input_tokens", None) or 0):
                        self.input_tokens = incoming
                for key in ("cache_read_tokens", "cache_write_tokens"):
                    value = data.get(key)
                    incoming = getattr(self, "input_tokens", None)
                    setattr(self, key, value if type(value) is int and type(incoming) is int and 0 <= value <= incoming else None)
                tokens, seconds = data.get("measured_output_tokens"), data.get("generation_seconds")
                if rate(tokens, seconds) is not None and tokens <= total:
                    self.measured_output_tokens, self.generation_seconds = tokens, seconds
                else:
                    self.measured_output_tokens = self.generation_seconds = None
                for key in CONTEXT_FIELDS:
                    value = data.get(key)
                    if type(value) is int and value > 0:
                        setattr(self, key, value)
                return True
        if kind == "subtask_usage":
            report = reduce_subtask_usage(data)
            if report is None or report == self.subtask_usage:
                return False
            self.subtask_usage = report
            return True
        if kind == "redaction":
            # Counts only: the terms themselves never reach a card.
            counts = {key: _count(data.get(key)) for key in ("terms", "hits", "requests")}
            if None in counts.values() or counts == self.redaction:
                return False
            self.redaction = counts
            return True
        if kind == "trace_finished":
            self.finish()
            return True
        return False

    def apply_normalized(self, event, checkpoint=""):
        key = event["id"]
        previous = self.steps.get(key)
        if previous and previous["status"] in STEP_ENDED and event["status"] == "running":
            return False
        name, detail = event["name"], str(event.get("detail") or "")
        title = STEP_TITLES.get(name, name)
        # The exact path only: in a command, or as a file tool's whole path (a change of several files keeps them).
        # A command keeps its body: it may do more than write the checkpoint, and the owner should see that.
        if checkpoint and name in CHECKPOINT_TOOLS and (checkpoint in detail if name == "bash" else detail == checkpoint):
            title, detail = CHECKPOINT_STEP, (detail if name == "bash" else "")
        step = {"title":title, "detail":safe_text(detail, None),
                "status":event["status"], "started":previous["started"] if previous else event["started_at"]}
        if event.get("ended_at") is not None:
            step["ended"] = event["ended_at"]
            step["elapsed"] = round(max(0, step["ended"] - step["started"]), 1)
        if event.get("exit_code") is not None:
            step["exit_code"] = event["exit_code"]
        if previous == step:
            return False
        if previous is None:
            self.total += 1
        self.steps[key] = step
        return True

    def note(self, key, text, title="进展说明", received_at=None):
        if not text:
            return False
        step = {"title": title, "detail": safe_text(text, None, markdown=title in {"进展说明", "子任务说明"}), "status": "note", "started": received_at if received_at is not None else time.time()}
        if key not in self.steps:
            self.total += 1
        elif self.steps[key].get("detail") == step["detail"]:
            return False
        self.steps[key] = step
        return True

    def sync_agents(self, agents):
        self.agents.update(agents)
        for sid, agent in agents.items():
            key = "agent-" + sid
            if key not in self.steps:
                self.note(key, agent.get("description", "" ) or "子任务", "子任务")
            self.steps[key]["status"] = "running" if agent["status"] in AGENT_LIVE else "done"
            self.steps[key]["agent_id"] = sid
            self.steps[key]["started"] = agent["started_at"]

    def finish(self, stopped=False, ended_at=None):
        """End the trace and each subtask below it, whose steps no event ends once the turn has: steps
        still running end unconfirmed. When the task was stopped the trace says so, and those steps read
        已停止; a flag, not a new step status, which earlier releases could not draw. A trace that has
        ended keeps its end; the others end at ``ended_at`` (now by default; a subtask, with the trace).
        Agents keep their states: an ended trace draws one still running as ended (TraceView)."""
        ended_at = ended_at or self.ended_at or time.time()
        for node in self.tree():
            node.close(ended_at, stopped)

    def close(self, ended_at, stopped=False):
        """``finish`` for this trace's own steps, not a subtask's."""
        self.ended_at = self.ended_at or ended_at
        self.stopped = self.stopped or stopped
        for step in self.steps.values():
            if step["status"] == "running":
                step["status"] = "unconfirmed"


def finish_record_trace(trace, record):
    """Freeze a restored terminal trace without changing durable execution data; a stopped task's
    (``cancelled``, shown as ⏹ 已停止) open steps read 已停止."""
    from datetime import datetime
    ended = record.get("interrupted_at")
    try:
        ended_at = datetime.fromisoformat(ended.replace("Z", "+00:00")).timestamp() if ended else None
    except (ValueError, TypeError):
        ended_at = None
    ended_at = ended_at or trace.ended_at or trace.started_at + max(0, record.get("elapsed_seconds", 0))
    trace.finish(record.get("status") == "cancelled", ended_at)
    return trace


def has_subtasks(usage):
    """Whether a turn's usage (``turn_usage``) has subtasks, reported or not: 主任务 then leads its main
    task's figures."""
    return bool(usage["count"] or usage["unreported"] or usage["subtasks"])
