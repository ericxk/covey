"""The main thread's tool channel (docs/main-thread.md). Standard library only.

The MCP server in the main thread's session (core/main_mcp.py through bin/covey_main_mcp.py) writes each tool
call as <state>/main-thread/requests/<id>.json and waits. The Bridge's MainTools loop (app/main_tools.py) claims it
(<id>.taken), runs it in the Bridge's own process (a task process changes only its own task's cards and records,
AGENTS.md rule 8) and writes <id>.answer.json; the server reads the answer and removes the three files.

Never run twice (rules 1 and 6): a request claimed but never answered (the Bridge restarted while running it) is
not run again; ``orphans`` gives it, and the Bridge answers UNCERTAIN so the main thread checks with topics_list
before it acts again. A request nobody claimed in STALE seconds, and an answer nobody read, are removed.
"""
from __future__ import annotations

import dataclasses
import json
import os
import time
import uuid
from pathlib import Path

DIR = Path("main-thread") / "requests"
STALE = 600.0
UNCERTAIN = {"ok": False, "text": "Bridge 在执行这次调用时重启了，结果不确定：先用 topics_list 或 topic_read 核对，别直接重做。",
             "data": {"uncertain": True}}


@dataclasses.dataclass(frozen=True)
class Request:
    id: str
    tool: str
    args: dict
    job_key: str
    created_at: float


def folder(state_dir) -> Path:
    path = Path(state_dir) / DIR
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _write(path: Path, value) -> None:
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False)
    os.replace(tmp, path)


def _read(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _request(path: Path):
    data = _read(path)
    if not isinstance(data, dict) or not isinstance(data.get("args"), dict):
        return None
    try:
        return Request(str(data["id"]), str(data["tool"]), data["args"], str(data.get("job_key") or ""),
                       float(data.get("created_at") or 0))
    except (KeyError, TypeError, ValueError):
        return None


def _remove(*paths: Path) -> None:
    for path in paths:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def submit(state_dir, tool: str, args: dict, *, job_key: str, timeout: float = 120.0, poll: float = 0.25,
           clock=time.time, sleep=time.sleep) -> dict:
    """The server's side: one call, its answer ({"ok", "text", "data"}). When no answer comes in ``timeout``
    seconds: an unclaimed request is withdrawn (nothing ran); a claimed one is left to the Bridge and the answer
    says the outcome is unknown."""
    base = folder(state_dir)
    request_id = uuid.uuid4().hex
    request, taken, answer_path = base / f"{request_id}.json", base / f"{request_id}.taken", base / f"{request_id}.answer.json"
    _write(request, {"id": request_id, "tool": tool, "args": args, "job_key": job_key, "created_at": clock()})
    deadline = clock() + timeout
    while clock() < deadline:
        answer = _read(answer_path)
        if isinstance(answer, dict):
            _remove(request, taken, answer_path)
            return answer
        sleep(poll)
    if taken.exists():
        return {"ok": False, "text": f"Bridge 已开始执行 {tool}，{int(timeout)} 秒内没有结果：先用 topics_list 或 topic_read 核对，别直接重做。",
                "data": {"uncertain": True}}
    _remove(request)
    return {"ok": False, "text": f"Bridge 在 {int(timeout)} 秒内没有接这次调用（可能在重启），没有执行；稍后再试。", "data": {}}


def pending(state_dir, *, now: float | None = None) -> list[Request]:
    """The Bridge's side: requests neither claimed nor answered, oldest first. Removes those older than STALE
    that nobody claimed, and answers older than STALE that nobody read."""
    now = time.time() if now is None else now
    base, found = folder(state_dir), []
    for path in base.glob("*.answer.json"):
        try:
            if now - path.stat().st_mtime > STALE:
                _remove(path, base / path.name.replace(".answer.json", ".json"), base / path.name.replace(".answer.json", ".taken"))
        except FileNotFoundError:
            continue
    for path in base.glob("*.json"):
        if path.name.endswith(".answer.json"):
            continue
        request = _request(path)
        if request is None:
            continue
        if (base / f"{request.id}.taken").exists() or (base / f"{request.id}.answer.json").exists():
            continue
        if now - request.created_at > STALE:
            _remove(path)
            continue
        found.append(request)
    return sorted(found, key=lambda request: request.created_at)


def claim(state_dir, request_id: str) -> bool:
    """True for the one caller that may run this request."""
    try:
        os.close(os.open(folder(state_dir) / f"{request_id}.taken", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        return True
    except FileExistsError:
        return False


def orphans(state_dir) -> list[Request]:
    """Requests claimed and never answered: at the Bridge's start, those a previous process was running."""
    base, found = folder(state_dir), []
    for taken in base.glob("*.taken"):
        request_id = taken.name[:-len(".taken")]
        if (base / f"{request_id}.answer.json").exists():
            continue
        request = _request(base / f"{request_id}.json")
        if request is not None:
            found.append(request)
        else:
            _remove(taken)
    return found


def answer(state_dir, request_id: str, result: dict) -> None:
    _write(folder(state_dir) / f"{request_id}.answer.json", result)
