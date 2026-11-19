"""What a turn tells the web page beside its words (docs/butler.md 网页工作台, COVEY_WEB=1; data contract v3): the steps of its process,
the checkpoint's plan, the token figures, the time it took and the notes of the memory feature. Pure shaping of a job record and its trace
(core/trace_store.py) into the optional fields of a ``turn`` item; app/web_sync.py asks for them and keeps the result of an ended turn.

- ``steps``: the newest ``STEPS_KEPT`` steps of the main task as {id, kind, label, started_at, ended_at, status}; ``kind`` is by the step's
  title (``STEP_KINDS``), a running step's ``ended_at`` is None. A note (the progress note) is no step: it is the turn's body.
  A subtask is one step from its start to its end, in its own words (data contract v8, ``steps_of``).
- ``plan``: the checkpoint's ``steps`` ({title, state}, core/checkpoint.py ``checkpoint_steps``); once the turn has ended, a step left
  ``doing`` is ``todo`` (it was not done, and nothing does it now).
- ``usage`` {input, output, cache_read, total}, ``seconds``, ``speed``, ``context`` {used, limit}: as the cards show them (cards/facts.py
  ``usage_figures``, core/metrics.py ``run_seconds``). While the turn runs ``seconds`` moves in steps of ``RUNNING_STEP`` and ``speed`` is a
  whole number, so a quiet minute does not change the item's ``rev`` every second.
- ``notes``: the record's ``memory_notes`` as written by the memory feature ({kind, text, scope, entry_id}); none without the field.

A field is left out when nothing is known; the page draws what it gets. Nothing here reads more than the trace file and the checkpoint of
one turn, and never raises."""
from __future__ import annotations

import time
from pathlib import Path

from covey.cards import facts
from covey.core.checkpoint import checkpoint_steps, read_checkpoint
from covey.core.look import WAITS
from covey.core.metrics import run_seconds, timings_of
from covey.core.trace_store import load_trace

STEPS_KEPT = 80
RUNNING_STEP = 10  # Seconds: the 用时 of a running turn moves by this much.
LABEL_CHARS = 80
NOTE_KINDS = ("remembered", "voided", "undone", "skipped_ask", "reference")
NOTES_KEPT = 20
STEP_KINDS = {"读取文件": "read", "浏览目录": "read", "修改文件": "edit", "写入文件": "edit", "执行命令": "command", "后台命令": "command",
              "启动后台命令": "command", "更新检查点": "command", "查找文件": "search", "搜索文件": "search", "分配子任务": "subagent", "子任务": "subagent",
              "读取网页": "web", "搜索网页": "web"}
STEP_STATES = {"running": "running", "done": "done", "failed": "failed", "stopped": "failed"}
AGENT_STATES = {"running": "running", "pendingInit": "running", "completed": "done", "errored": "failed", "interrupted": "failed",
                "cancelled": "failed", "shutdown": "done"}
SUBTASK = "子任务"
SAID_BY = {"owner": "你说的", "owner_answer": "你说的", "owner_fix": "你说的", "lead": "它总结的"}
RESULT_WORDS = {"完成": "做完了", "失败": "失败了", "中断": "中断了", "已停止": "停住了"}


def step_kind(title) -> str:
    title = str(title or "")
    return STEP_KINDS.get(title) or ("think" if "思考" in title else "other")


def steps_of(snapshot) -> list:
    """The newest STEPS_KEPT steps of the snapshot's main task, oldest first.

    A subtask is one step (data contract v8): the ``agent-<id>`` step the trace keeps for it, with kind ``subagent``, the
    subtask's own words (``agent_words``) and its end from the agent's state; the ``分配子任务`` call that started it is left
    out, as it is the same subtask (one started in the background ends that call in a tenth of a second). 2026-11-19 T488:
    two subtasks ran 35 of the turn's 46 minutes, and the page drew 子任务：子任务 at 0 and 分配子任务：Agent at 0.1 s."""
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    steps = snapshot.get("steps")
    if not isinstance(steps, dict):
        return []
    agents = snapshot.get("agents") if isinstance(snapshot.get("agents"), dict) else {}
    ended = snapshot.get("ended_at") if isinstance(snapshot.get("ended_at"), (int, float)) else None
    order = [key for key in (snapshot.get("step_order") or list(steps)) if key in steps]
    out = []
    for key in order:
        step = steps[key]
        if not isinstance(step, dict) or ("agent-" + str(key)) in steps:
            continue
        agent = agents.get(str(key)[len("agent-"):]) if str(key).startswith("agent-") else None
        if isinstance(agent, dict):
            out.append(agent_step(key, step, agent, ended))
            continue
        if step.get("status") == "note":
            continue
        title = "调用工具" if step.get("title") == "tool" else str(step.get("title") or "")
        detail = " ".join(str(step.get("detail") or "").split())
        status = STEP_STATES.get(step.get("status"), "done")
        out.append({"id": str(key), "kind": step_kind(step.get("title")), "label": (f"{title}：{detail}" if detail else title)[:LABEL_CHARS],
                    "started_at": step.get("started") if isinstance(step.get("started"), (int, float)) else 0,
                    "ended_at": None if status == "running" else step.get("ended") if isinstance(step.get("ended"), (int, float)) else None,
                    "status": status})
    return out[-STEPS_KEPT:]


def agent_words(agent) -> str:
    """What a subtask is about: its name unless that is the placeholder (Claude Code names it by the Agent call's
    description, Codex by its path), else the prompt Codex started it with, else 子任务."""
    for value in (agent.get("name"), agent.get("description")):
        words = " ".join(str(value or "").split())
        if words and not words.startswith(SUBTASK):
            return words
    return SUBTASK


def agent_step(key, step, agent, ended) -> dict:
    """The step of one subtask, from its agent state; one still live when the trace has ended ends with it."""
    status = AGENT_STATES.get(agent.get("status"), "done")
    end = agent.get("ended_at") if isinstance(agent.get("ended_at"), (int, float)) else None
    if status == "running" and ended is not None:
        status = "done"
    if status != "running" and end is None:
        end = ended
    start = agent.get("started_at") if isinstance(agent.get("started_at"), (int, float)) else step.get("started")
    return {"id": str(key), "kind": "subagent", "label": f"{SUBTASK}：{agent_words(agent)}"[:LABEL_CHARS],
            "started_at": start if isinstance(start, (int, float)) else 0, "ended_at": None if status == "running" else end, "status": status}


def plan_of(record) -> list:
    path = str((record or {}).get("checkpoint_path") or "")
    if not path:
        return []
    return checkpoint_steps(read_checkpoint(Path(path)))


def figures(record, snapshot, running, now) -> dict:
    """usage, seconds, speed and context of the turn, each only when known."""
    out = {}
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    seconds = run_seconds(timings_of(record), snapshot, now)
    if seconds is not None:
        out["seconds"] = float(int(seconds // RUNNING_STEP * RUNNING_STEP)) if running else round(seconds, 1)
    if snapshot:
        shown = facts.usage_figures(snapshot)
        counts = shown["shown"]
        have = {name: counts.get(key) for name, key in (("input", "input_tokens"), ("output", "output_tokens"), ("cache_read", "cache_read_tokens"))
                if type(counts.get(key)) is int}
        if have:
            out["usage"] = {"input": have.get("input", 0), "output": have.get("output", 0), "cache_read": have.get("cache_read", 0),
                            "total": have.get("input", 0) + have.get("output", 0)}
        speed = shown["speed"]
        if isinstance(speed, (int, float)) and speed > 0:
            out["speed"] = round(speed) if running else round(speed, 1)
        used = shown["context"]
        if type(used) is int and used > 0:
            window = shown["window"]
            out["context"] = {"used": used, "limit": window if type(window) is int and window > 0 else 0}
    return out


def ended_figures(state, record) -> dict:
    """``figures`` of the ended turn the job ``record`` made, its trace read from disk; {} when it cannot be read."""
    try:
        snapshot = load_trace(Path(state), record) or {}
        return figures(record, snapshot, False, time.time())
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def notes_of(record) -> list:
    rows = (record or {}).get("memory_notes")
    out = []
    for row in rows if isinstance(rows, list) else ():
        if isinstance(row, dict) and row.get("kind") in NOTE_KINDS and str(row.get("text") or "").strip():
            out.append({"kind": row["kind"], "text": str(row["text"])[:300], "scope": str(row.get("scope") or "")[:40],
                        "entry_id": str(row.get("entry_id") or "")[:40]})
    return out[-NOTES_KEPT:]


def extras(state, record, running, now=None, snapshot=None) -> dict:
    """The optional fields of the turn the job ``record`` made (its trace read from disk, unless the caller has ``snapshot``)."""
    now = time.time() if now is None else now
    try:
        snapshot = load_trace(Path(state), record) or {} if snapshot is None else snapshot
    except (OSError, ValueError, KeyError, TypeError):
        snapshot = {}
    out = {}
    try:
        steps, plan = steps_of(snapshot), plan_of(record)
        if steps:
            out["steps"] = steps
        if plan and not running and record.get("status") not in WAITS:
            # The turn has ended: a step its checkpoint left ``doing`` was not done, and nothing does it now.
            plan = [{**step, "state": "todo"} if step.get("state") == "doing" else step for step in plan]
        if plan:
            out["plan"] = plan
        out.update(figures(record, snapshot, running, now))
        notes = notes_of(record)
        if notes:
            out["notes"] = notes
    except (OSError, ValueError, KeyError, TypeError):
        return out
    return out


IMAGES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg")
VIDEOS = (".mp4", ".mov", ".webm", ".mkv", ".avi")


def library_kind(row) -> str:
    """A library entry's kind for the page: link, image, video or file (the library's own kinds are file, path, link, delivery)."""
    if row.get("kind") == "link":
        return "link"
    name = str(row.get("name") or row.get("ref") or "").lower()
    return "image" if name.endswith(IMAGES) else "video" if name.endswith(VIDEOS) else "file"


def said_by(source) -> str:
    return SAID_BY.get(str(source or ""), "")


def event_words(text) -> str:
    """A ``thread_end`` receipt ("T834 的任务 #5 完成：…") as the page's grey line ("T834 做完了"); '' for a text it cannot read."""
    head = str(text or "").split("：", 1)[0].split()
    if len(head) >= 4 and head[1] == "的任务" and head[3] in RESULT_WORDS:
        return f"{head[0]} {RESULT_WORDS[head[3]]}"
    return ""
