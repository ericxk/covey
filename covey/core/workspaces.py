"""Local allowlist and immutable topic workspace bindings. LOCK and topic_lock live in core/locks.py
and the topic.json helpers in core/topics.py; the names stay importable from here."""

from __future__ import annotations

import argparse
import dataclasses
import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from covey.core.locks import LOCK, topic_lock
from covey.core.state import read_json, write_json
from covey.core.paths import component_dir, expand_user
from covey.core.topics import bind_topic, context_for_topic, proxy_topic, read_topic, workspace_locked

__all__ = ["LOCK", "RUN_MODES", "add_topic_worktree", "add_workspace", "bind_topic", "config_path", "directory_status",
           "load_config", "main", "missing_workspaces", "plan_topic_worktree", "proxy_topic", "read_topic",
           "remove_workspaces", "resolve_selection", "set_default_workspace", "topic_context", "topic_lock",
           "touch_workspace", "workspace", "workspace_locked", "worktree_root"]
RUN_MODES = {"direct": "原目录", "worktree": "独立 worktree"}


def worktree_root() -> Path:
    return component_dir("worktrees", preserve_legacy=False)


def _git(*args, cwd, timeout=300):
    try:
        proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"git 执行失败（{type(exc).__name__}）") from None
    if proc.returncode:
        detail = (proc.stderr or proc.stdout).strip()
        raise ValueError(detail.splitlines()[-1][:300] if detail else f"git 退出码 {proc.returncode}")
    return proc.stdout.strip()


def plan_topic_worktree(directory: Path, topic_id: str, root: Path | None = None) -> dict:
    """Name a topic worktree without creating it; refuses a directory outside a git repository.

    Fast (one rev-parse), so a card callback can call it before the slow checkout.
    """
    try:
        top = Path(_git("rev-parse", "--show-toplevel", cwd=directory, timeout=30)).resolve()
    except ValueError:
        raise ValueError("所选目录不在 git 仓库中，只能用原目录运行") from None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return {"top": str(top), "branch": f"bridge/{stamp}-{topic_id[:8]}",
            "dest": str((root or worktree_root()) / f"{top.name}-{stamp}-{topic_id[:8]}"),
            "subdir": str(directory.resolve().relative_to(top))}


def add_topic_worktree(plan: dict) -> tuple[Path, str]:
    """Check out a new branch at HEAD in its own worktree; the selected subdirectory maps into it.

    Uncommitted changes in the source checkout are not carried over. The NAS is
    latency-bound per file, so the checkout writes files in parallel.
    """
    dest = Path(plan["dest"])
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        _git("-c", "checkout.workers=8", "worktree", "add", "-b", plan["branch"], str(dest), "HEAD", cwd=plan["top"])
    except ValueError as exc:
        raise ValueError(f"创建 worktree 失败：{exc}") from None
    return dest / plan["subdir"], plan["branch"]


def config_path(state: Path) -> Path:
    return state / "workspace-config.json"


def load_config(state: Path, fallback: Path) -> dict:
    with LOCK:
        path = config_path(state)
        if not path.exists():
            data = {"default": fallback.name, "workspaces": {fallback.name: str(fallback.resolve())}}
            write_json(path, data, ensure_ascii=False, indent=2)
        data = read_json(path)
        if not isinstance(data.get("workspaces"), dict) or data.get("default") not in data["workspaces"]:
            raise ValueError("工作区配置需要有效的 default 和 workspaces")
        return data


def workspace(data: dict, name: str) -> Path:
    if name not in data["workspaces"]:
        raise ValueError("工作区不在本机允许列表中")
    path = Path(data["workspaces"][name]).expanduser().resolve()
    if not path.is_dir():
        raise ValueError(f"工作区目录不存在：{name}")
    return path


def resolve_selection(state: Path, fallback: Path, selection: str) -> tuple[str, Path]:
    """Resolve a saved name or register an explicitly supplied existing absolute path."""
    with LOCK:
        data = load_config(state, fallback)
        if selection in data["workspaces"]:
            return selection, workspace(data, selection)
        path = expand_user(selection)
        if path is None or not path.is_absolute():
            raise ValueError("未找到该工作区，可能已从列表移除；请重新选择工作区或填写绝对目录路径")
        path = path.resolve()
        if not path.is_dir():
            raise ValueError(f"工作区目录不存在：{path}")
        name, added = _register(data, path)
        if added:
            _save_config(state, data)
        return name, path


def _register(data: dict, path: Path) -> tuple[str, bool]:
    """The name a resolved directory is listed under, and whether it was just added: a new one is named
    after its directory, with -2, -3 on a clash. The caller holds LOCK and saves an addition."""
    for name, existing in data["workspaces"].items():
        if Path(existing).expanduser().resolve() == path:
            return name, False
    base = (path.name or "root")[:32]
    name, suffix = base, 2
    while name in data["workspaces"]:
        name = f"{base}-{suffix}"
        suffix += 1
    data["workspaces"][name] = str(path)
    return name, True


def add_workspace(state: Path, fallback: Path, text: str, timeout: float = 5) -> tuple[str, bool]:
    """List an existing directory given by its absolute path: (its name, whether it is new). The directory
    is checked before LOCK is taken and for at most ``timeout`` seconds, so a slow NAS holds up only this call."""
    path = expand_user(text.strip())
    if path is None or not path.is_absolute():
        raise ValueError("请填写目录的绝对路径，例如 /home/me/project")
    found = {}

    def check():
        try:
            resolved = path.resolve()
            found["path"] = resolved if resolved.is_dir() else None
        except OSError:
            found["path"] = None

    worker = threading.Thread(target=check, name="workspace-add", daemon=True)
    worker.start()
    worker.join(timeout)
    if "path" not in found:
        raise ValueError(f"目录 {timeout:g} 秒内没有答复，请稍后重试")
    if found["path"] is None:
        raise ValueError(f"目录不存在：{path}")
    with LOCK:
        data = load_config(state, fallback)
        name, added = _register(data, found["path"])
        if added:
            _save_config(state, data)
        return name, added


def _save_config(state: Path, data: dict) -> None:
    write_json(config_path(state), data, ensure_ascii=False, indent=2)


def directory_status(paths, timeout: float = 1.5) -> dict:
    """True/False per path, None when the NAS did not answer in time; never waits longer."""
    result = {}

    def check():
        for path in paths:
            try:
                result[path] = Path(path).expanduser().is_dir()
            except OSError:
                result[path] = False

    worker = threading.Thread(target=check, name="workspace-status", daemon=True)
    worker.start()
    worker.join(timeout)
    return {path: result.get(path) for path in paths}


def remove_workspaces(state: Path, fallback: Path, names) -> list[str]:
    """Drop entries from the list; directories and topics already bound to them stay as they are."""
    with LOCK:
        data = load_config(state, fallback)
        if data["default"] in names:
            raise ValueError(f"默认工作区 {data['default']} 不能移除，请先把其他工作区设为默认")
        removed = [name for name in data["workspaces"] if name in names]
        for name in removed:
            del data["workspaces"][name]
            data.get("recent", {}).pop(name, None)
        if removed:
            _save_config(state, data)
        return removed


def missing_workspaces(data: dict, status: dict | None = None) -> list[str]:
    """Names whose directory is confirmed gone; an unanswered check is never counted."""
    if status is None:
        status = directory_status(list(dict.fromkeys(data["workspaces"].values())))
    return [name for name, path in data["workspaces"].items()
            if status.get(path) is False and name != data["default"]]


def set_default_workspace(state: Path, fallback: Path, name: str) -> None:
    with LOCK:
        data = load_config(state, fallback)
        workspace(data, name)
        data["default"] = name
        _save_config(state, data)


def topic_context(ctx: Any, event: dict[str, Any], sessions_dir: Any, *, legacy_harness_id: str | None = None) -> Any:
    """``ctx`` in the topic of ``event``; a topic seen for the first time starts in the
    configured default workspace."""
    config = load_config(ctx.state_dir, ctx.default_workdir)
    default = config["default"]
    proposed = dataclasses.replace(
        ctx, workspace_name=default, workdir=Path(config["workspaces"][default]).expanduser().resolve()
    )
    return context_for_topic(proposed, event, sessions_dir, legacy_harness_id=legacy_harness_id)


def main():
    parser = argparse.ArgumentParser(prog="covey_workspaces.py", description="配置机器人允许使用的工作区，仅影响新话题")
    parser.add_argument("--state-dir", type=Path, required=True)
    sub = parser.add_subparsers(dest="action", required=True)
    add = sub.add_parser("add")
    add.add_argument("name")
    add.add_argument("path", type=Path)
    default = sub.add_parser("default")
    default.add_argument("name")
    remove = sub.add_parser("remove", help="从列表移除，不删除目录")
    remove.add_argument("names", nargs="+")
    sub.add_parser("prune", help="移除目录已不存在的工作区")
    sub.add_parser("list")
    args = parser.parse_args()
    with LOCK:
        data = load_config(args.state_dir, Path.cwd())
        if args.action == "add":
            path = args.path.expanduser().resolve()
            if not path.is_dir():
                parser.error("path must be an existing directory")
            if not args.name.strip() or len(args.name) > 40:
                parser.error("name must contain 1–40 characters")
            data["workspaces"][args.name] = str(path)
        elif args.action == "default":
            workspace(data, args.name)
            data["default"] = args.name
        elif args.action in ("remove", "prune"):
            names = args.names if args.action == "remove" else missing_workspaces(data)
            unknown = [name for name in names if name not in data["workspaces"]]
            if unknown:
                parser.error("unknown workspace: " + ", ".join(unknown))
            try:
                remove_workspaces(args.state_dir, Path.cwd(), names)
            except ValueError as exc:
                parser.error(str(exc))
            data = load_config(args.state_dir, Path.cwd())
        if args.action not in ("list", "remove", "prune"):
            write_json(config_path(args.state_dir), data, ensure_ascii=False, indent=2)
        print(json.dumps(data, ensure_ascii=False, indent=2))


def touch_workspace(state, fallback, name):
    import time

    with LOCK:
        data = load_config(state, fallback)
        if name in data["workspaces"]:
            data.setdefault("recent", {})[name] = time.time()
            write_json(config_path(state), data, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
