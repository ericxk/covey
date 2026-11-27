"""Where a LOOK turn may write (core/harness.py LOOK, app/tool_limits.py ``look_roots``): its own checkpoint file and the
topic's outputs directory, nothing else; one check every path gate of it shares (``refusal``), and the write tool
``workspace_write_file`` the Codex and Antigravity turns write through (``write``, ``serve``).

The check (W1-C, contract C6): the target made absolute against the call's working directory, then resolved
(``os.path.realpath``: ``..`` and every symbolic link of an existing part followed); refused when any part of it, as given
or as resolved, is ``.git`` (a link in the outputs directory to a repository's ``.git/config`` included), when it is in a
shared read worktree (``<数据根>/worktrees/_read/``, core/topic_repos.py ``read_worktrees``) or is an existing file with
other hard links (one more name of a file elsewhere); allowed only when it is the checkpoint file itself or inside the
outputs directory, both resolved the same way. Anything that goes wrong refuses.

Its users: Claude Code's PreToolUse hook (backends/claude/tool_guard.py ``--mode look``), the workspace server Codex runs
(backends/codex/workspace_reader.py, its ``--allow-file``/``--allow-dir``) and the stdio MCP server of an Antigravity
turn's private home (bin/covey_look_write.py, backends/agy/bridge.py ``Homes``). Standard library only: the hook runs it
before each tool call.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import tempfile

GIT = ".git"
READ_PARTS = ("worktrees", "_read")  # core/topic_repos.py read_worktrees: <数据根>/worktrees/_read (tested equal).
DEVICES = ("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty")  # What a shell may write to that is no file.
MAX_BYTES = 4 * 1024 * 1024
TOOL_NAME = "workspace_write_file"
TOOL = {
    "name": TOOL_NAME,
    "description": ("Write a UTF-8 text file. This turn is read-only: only this turn's checkpoint file and the topic's "
                    "outputs directory (both named in the request) may be written; any other path, a path through .git, "
                    "and the shared read-only worktrees are refused. The whole file is replaced (append=true adds to its "
                    "end); missing directories under the outputs directory are made."),
    "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"},
                                                     "append": {"type": "boolean"}},
                    "required": ["path", "content"], "additionalProperties": False},
    "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
}
WHERE = "只能写本轮检查点 {file} 和本话题产物目录 {dir}"


def _absolute(target, cwd) -> str:
    path = os.path.expanduser(str(target))
    if not os.path.isabs(path):
        path = os.path.join(cwd or os.getcwd(), path)
    return os.path.normpath(path)


def _parts(path) -> list[str]:
    return [part for part in str(path).split(os.sep) if part]


def in_read_tree(path) -> bool:
    """Whether ``path`` (absolute) is in a shared read worktree: its parts hold ``worktrees`` then ``_read``."""
    parts = _parts(path)
    return any(parts[at:at + 2] == list(READ_PARTS) for at in range(len(parts) - 1))


def _inside(path, root) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def refusal(target, *, allow_file="", allow_dir="", cwd="") -> str:
    """Why a LOOK turn may not write ``target`` (a path, relative to ``cwd``); '' when it may: see the module docstring."""
    try:
        if not str(target or "").strip():
            return "这一轮只读：没给要写的文件。"
        given = _absolute(target, cwd)
        if given in DEVICES:
            return ""
        where = WHERE.format(file=allow_file or "（没有）", dir=allow_dir or "（没有）")
        resolved = os.path.realpath(given)
        if GIT in _parts(given) or GIT in _parts(resolved):
            return f"这一轮只读：不能写 .git 里的文件（{given}），{where}。"
        if in_read_tree(given) or in_read_tree(resolved):
            return f"这一轮只读：{given} 在各话题共用的只读 worktree 里，不能写；{where}。"
        file = os.path.realpath(_absolute(allow_file, cwd)) if allow_file else ""
        folder = os.path.realpath(_absolute(allow_dir, cwd)) if allow_dir else ""
        if folder and (GIT in _parts(folder) or in_read_tree(folder)):
            folder = ""  # An outputs directory there is none.
        if not (resolved == file or (folder and _inside(resolved, folder))):
            return f"这一轮只读：{given} 不在可写的地方，{where}；要改别处的，在回复里写清楚改什么，等主人下一句。"
        try:
            info = os.lstat(resolved)
        except FileNotFoundError:
            return ""
        if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
            return f"这一轮只读：{given} 还有别的硬链接（别处文件的另一个名字），不能写；{where}。"
        return ""
    except Exception as exc:  # noqa: BLE001 - a gate that cannot tell refuses
        return f"这一轮只读：检查写入位置时出错（{type(exc).__name__}），这次没有写。"


def write(path, content, *, allow_file="", allow_dir="", cwd="", append=False) -> dict:
    """``workspace_write_file``: ``content`` as the whole of ``path`` (or added to its end) when ``refusal`` allows it;
    ValueError with the reason when it does not. Written to a temporary file in the same directory and renamed over the
    target (never through a link at its place, never into another name of the same file)."""
    if not isinstance(content, str):
        raise ValueError("content must be text")
    data = content.encode("utf-8")
    reason = refusal(path, allow_file=allow_file, allow_dir=allow_dir, cwd=cwd)
    if reason:
        raise ValueError(reason)
    target = os.path.realpath(_absolute(path, cwd))
    if target in DEVICES:
        raise ValueError("这一轮只读：不能写设备文件。")
    parent = os.path.dirname(target)
    os.makedirs(parent, exist_ok=True)
    if os.path.realpath(parent) != parent:  # A link swapped in after the check.
        raise ValueError("这一轮只读：写入位置在检查之后变了，这次没有写。")
    old = b""
    if append:
        try:
            fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            fd = None
        if fd is not None:
            with os.fdopen(fd, "rb") as stream:
                old = stream.read(MAX_BYTES + 1)
    if len(old) + len(data) > MAX_BYTES:
        raise ValueError("文件超过 4 MiB，这次没有写。")
    fd, temp = tempfile.mkstemp(dir=parent, prefix=".look-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(old + data)
        try:
            os.chmod(temp, stat.S_IMODE(os.lstat(target).st_mode))
        except FileNotFoundError:
            os.chmod(temp, 0o644)
        os.replace(temp, target)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise
    return {"path": target, "bytes": len(old) + len(data)}


def call(name, args, *, allow_file="", allow_dir="", cwd="") -> dict:
    """``workspace_write_file`` called with ``args``; ValueError for another tool, a wrong argument or a refused path."""
    if name != TOOL_NAME or not isinstance(args, dict) or set(args) - set(TOOL["inputSchema"]["properties"]):
        raise ValueError("Unknown tool or argument")
    if not isinstance(args.get("path"), str) or not isinstance(args.get("content"), str):
        raise ValueError("path and content must be text")
    return write(args["path"], args["content"], allow_file=allow_file, allow_dir=allow_dir, cwd=cwd,
                 append=args.get("append") is True)


def answer(row, tools, invoke):
    """The JSON-RPC answer of an MCP stdio server to ``row`` (a request), or None for a notification: ``tools`` listed,
    ``invoke(name, arguments)`` for a call (its ValueError and OSError are the tool's error, an OSError by its class only:
    its words can name paths outside)."""
    if not isinstance(row, dict) or "id" not in row:
        return None
    method, params = row.get("method"), row.get("params") or {}
    if method == "initialize":
        requested = params.get("protocolVersion") if isinstance(params, dict) else None
        result = {"protocolVersion": requested if requested in {"2024-11-05", "2025-03-26", "2025-06-18"} else "2024-11-05",
                  "capabilities": {"tools": {}}, "serverInfo": {"name": "bridge-workspace", "version": "1.0"}}
    elif method == "tools/list":
        result = {"tools": tools}
    elif method == "ping":
        result = {}
    elif method == "tools/call":
        try:
            if not isinstance(params, dict):
                raise ValueError("Tool parameters must be an object")
            output = invoke(params.get("name"), params.get("arguments", {}))
            result = {"content": [{"type": "text", "text": json.dumps(output, ensure_ascii=False)}], "isError": False}
        except (ValueError, OSError) as exc:
            message = str(exc) if isinstance(exc, ValueError) else f"File write failed ({type(exc).__name__})"
            result = {"content": [{"type": "text", "text": message}], "isError": True}
    else:
        return {"jsonrpc": "2.0", "id": row["id"], "error": {"code": -32601, "message": "Unsupported method"}}
    return {"jsonrpc": "2.0", "id": row["id"], "result": result}


def serve(allow_file, allow_dir, source=sys.stdin, destination=sys.stdout, cwd="") -> None:
    """The stdio MCP server of ``workspace_write_file`` alone (an Antigravity LOOK turn's: bin/covey_look_write.py)."""
    for line in source:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        reply = answer(row, [TOOL], lambda name, args: call(name, args, allow_file=allow_file, allow_dir=allow_dir,
                                                            cwd=cwd))
        if reply is not None:
            destination.write(json.dumps(reply, ensure_ascii=False) + "\n")
            destination.flush()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="covey_look_write.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("--allow-file", default="", help="the turn's checkpoint file")
    parser.add_argument("--allow-dir", default="", help="the topic's outputs directory")
    args = parser.parse_args(argv)
    serve(args.allow_file, args.allow_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
