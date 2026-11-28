"""The device channel's reading tools for a turn whose shell cannot run ``covey device`` (外壳 B): ``device_list``,
``device_ls``, ``device_stat`` and ``device_get`` in its ``bridge_workspace`` MCP server.

Who needs them: Codex's read-only sandbox reaches no ssh socket and resolves no name (docs/architecture.md 设备通道,
VERIFY-F2A U2), so its LEAD and READ turns cannot; Antigravity's and Claude Code's LEAD and READ turns have no shell. The
Harness names its turns (``mount``'s ``limits``). The server is backends/codex/workspace_reader.py (bin/covey_workspace_reader.py)
in every Harness: Codex's workspace server with them beside its two reading tools, the other two's ``bridge_workspace`` with
them alone, no ``--root`` (``server``).

Every call is core/device_access.py's ``Channel``, as ``covey device`` is: G1 (the list and the chat's ``spaces``), G2 and
G3 (every device read-only to such a turn; ``device_get`` with ``save`` writes only into the topic's outputs directory),
G5 (no secret fetched) and G6 (each call a line of the task's ``devices.jsonl``, which the turn's end folds into
``topic.json`` ``devices`` and onto the task card) are its, nothing is checked again here. The server knows its turn by the
state directory and the task's run directory it is started with (``--state``, ``--run-dir``) and that directory's
``device-turn.json``; the switch and the list by the environment ``mount`` gives it, since a Harness may start an MCP
server with few of its own variables (Codex).

A turn gets them (``mount``) only with COVEY_DEVICES on, a device with ssh in devices.json its chat may use, its turn file
written, on this machine and in a local topic (not ``options[EXTERNAL]``): any other turn's server and tools are what they
were. Only core modules and the standard library.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from covey.core import device_access as access
from covey.core import device_transport as dt
from covey.core import devices, topic_repos
from covey.core.harness import EXTERNAL, TOOL_LIMIT
from covey.core.paths import entry_script
from covey.core.process import device_of

TEXT_MAX = 4 << 20  # What device_get reads without save (as workspace_read_file reads at most).
OUTPUT_MAX = 48 << 10  # Of one answer's lines or entries (as backends/codex/workspace_reader.py MAX_OUTPUT).
PAGE = 200
STAT_MAX = 50
KEPT_ENV = ("SSH_AUTH_SOCK",)  # The ssh client's agent, beside what a Harness gives its servers itself.
DEVICE = {"type": "string", "description": "alias from device_list"}
READING = {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True}
TOOLS = [
    {"name": "device_list", "description": "List the devices (devices.json) this turn may use. This turn is read-only: "
                                           "every device can only be read.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}, "annotations": READING},
    {"name": "device_ls", "description": "List a directory on a device (default its home).",
     "inputSchema": {"type": "object", "properties": {"device": DEVICE, "path": {"type": "string"},
                                                      "offset": {"type": "integer", "minimum": 0}},
                     "required": ["device"], "additionalProperties": False}, "annotations": READING},
    {"name": "device_stat", "description": "Whether paths exist on a device: type, size, mtime.",
     "inputSchema": {"type": "object", "properties": {"device": DEVICE, "paths": {"type": "array", "items": {"type": "string"},
                                                                                   "minItems": 1, "maxItems": STAT_MAX}},
                     "required": ["device", "paths"], "additionalProperties": False}, "annotations": READING},
    {"name": "device_get", "description": "Read a UTF-8 text file on a device by lines (follow next_line; under 4 MiB). "
                                          "save=true fetches the file into the topic's outputs directory instead.",
     "inputSchema": {"type": "object", "properties": {"device": DEVICE, "path": {"type": "string"},
                                                      "start_line": {"type": "integer", "minimum": 1},
                                                      "limit": {"type": "integer", "minimum": 1, "maximum": 500},
                                                      "save": {"type": "boolean"}},
                     "required": ["device", "path"], "additionalProperties": False},
     "annotations": {**READING, "readOnlyHint": False}},
]
NAMES = tuple(tool["name"] for tool in TOOLS)


def mount(ctx, limits) -> tuple[list, dict]:
    """``(args, env)`` of the tools for the ``bridge_workspace`` server of a turn of ``ctx`` held to one of ``limits``, in
    its task runner (core/topic_repos.py ``task_dir_of``: its run directory): ``--state``, ``--run-dir`` and the variables
    the server finds the devices by; ``([], {})`` when the turn gets none (see the module docstring)."""
    if ctx.options.get(TOOL_LIMIT) not in limits or ctx.options.get(EXTERNAL) or device_of(ctx):
        return [], {}
    task_dir = topic_repos.task_dir_of(ctx.state_dir)
    if task_dir is None or not devices.enabled():
        return [], {}
    try:
        config = devices.current()
    except devices.DeviceConfigError:
        return [], {}
    turn = access.turn_of(ctx.state_dir, task_dir)
    if not turn.known or not turn.space or not any(turn.space in device.spaces for device in config.devices.values()):
        return [], {}
    env = {devices.SWITCH: "1", devices.FILE_ENV: str(devices.config_path().absolute()),
           **{name: value for name, value in access.ssh_env().items() if name in KEPT_ENV}}
    return ["--state", str(ctx.state_dir), "--run-dir", str(task_dir)], env


def server(ctx, limits) -> dict:
    """The stdio MCP server (``{command, args, env}``) of ``mount``'s tools alone: bin/covey_workspace_reader.py without a
    workspace (an Antigravity or Claude Code turn's ``bridge_workspace``); {} when the turn gets none."""
    args, env = mount(ctx, limits)
    if not args:
        return {}
    return {"command": sys.executable, "args": ["-B", str(entry_script("covey_workspace_reader.py")), *args], "env": env}


class Tools:
    """The tools of one server: its turn and its channel made at the first call (the turn file is written before the
    Harness starts), the channel's connections kept for the next."""

    def __init__(self, state_dir, run_dir, *, config=None, connect=None):
        self.state_dir, self.run_dir = Path(state_dir), Path(run_dir)
        self.config, self.connect = config, connect
        self.channel = None

    def call(self, name, args) -> dict:
        """The answer of tool ``name``; ValueError with the reason, in words for the model, for anything refused or
        failed (a guard's, the device's, a wrong argument)."""
        spec = next((tool for tool in TOOLS if tool["name"] == name), None)
        if spec is None or not isinstance(args, dict) or set(args) - set(spec["inputSchema"]["properties"]):
            raise ValueError("Unknown tool or argument")
        missing = [name for name in spec["inputSchema"].get("required", ()) if name not in args]
        if missing:
            raise ValueError(f"Missing argument: {', '.join(missing)}")
        try:
            if self.channel is None:
                if not devices.enabled():
                    raise access.Refused("设备通道没开（服务的 COVEY_DEVICES 不是 1）")
                self.channel = access.Channel(access.turn_of(self.state_dir, self.run_dir), config=self.config,
                                              connect=self.connect)
            return getattr(self, name)(self.channel, **args)
        except access.Refused as exc:
            raise ValueError(str(exc)) from None
        except devices.DeviceConfigError as exc:
            raise ValueError(f"devices.json 有错：{exc}") from None
        except (dt.DeviceError, OSError) as exc:
            reason = devices.OFFLINE_REASONS.get(type(exc).__name__, type(exc).__name__)
            raise ValueError(f"设备 {args.get('device', '')} 没有答复或出错（{reason}）：{str(exc)[:300]}") from None

    def device_list(self, channel) -> dict:
        return {"devices": channel.list(), "limit": channel.turn.limit}

    def device_ls(self, channel, device, path="~", offset=0) -> dict:
        found = channel.ls(_text(device, "device"), _text(path, "path"))
        rows = [{key: item.get(key) for key in ("name", "kind", "size")} for item in found.get("entries") or []]
        page, end = _page(rows, _number(offset, "offset", 0, 50000), PAGE)
        return {"path": found.get("path"), "entries": page, "total": found.get("count"),
                "next_offset": end if end < len(rows) else None, "truncated": bool(found.get("truncated"))}

    def device_stat(self, channel, device, paths) -> dict:
        if not isinstance(paths, list) or not 1 <= len(paths) <= STAT_MAX:
            raise ValueError(f"paths must be a list of 1 to {STAT_MAX} paths")
        keys = ("path", "exists", "is_dir", "is_file", "is_link", "size", "mtime", "error")
        results = channel.stat(_text(device, "device"), [_text(path, "path") for path in paths])
        return {"results": [{key: item[key] for key in keys if key in item} for item in results]}

    def device_get(self, channel, device, path, start_line=1, limit=PAGE, save=False) -> dict:
        device, path = _text(device, "device"), _text(path, "path")
        if save is True:
            return {**channel.get(device, path), "saved": True}
        start, limit = _number(start_line, "start_line", 1, TEXT_MAX), _number(limit, "limit", 1, 500)
        data = channel.read(device, path, TEXT_MAX)
        try:
            if b"\0" in data:
                raise UnicodeDecodeError("utf-8", data, 0, 1, "nul")
            lines = data.decode("utf-8").splitlines()
        except UnicodeDecodeError:
            raise ValueError("不是 UTF-8 文本：要整个取回就加 save=true（放进本话题产物目录）") from None
        rows = [f"{number}: {line}" for number, line in enumerate(lines[start - 1:start - 1 + limit], start)]
        page, count = _page(rows, 0, limit)
        if rows and not page:
            raise ValueError("这一行超过了一次能读的长度：加 save=true 取回再读")
        end = start - 1 + count
        return {"device": device, "path": path, "total_lines": len(lines), "content": "\n".join(page),
                "next_line": end + 1 if end < len(lines) else None}


def _text(value, name) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ValueError(f"{name} must be nonempty text")
    return value


def _number(value, name, low, high) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"Invalid {name}")
    return value


def _page(rows, start, limit) -> tuple[list, int]:
    """``rows[start:start + limit]`` as far as OUTPUT_MAX lets them (JSON-encoded), and the index after the last."""
    page, used = [], 0
    for row in rows[start:start + limit]:
        size = len(json.dumps(row, ensure_ascii=False).encode())
        if used + size > OUTPUT_MAX:
            break
        page.append(row)
        used += size
    return page, start + len(page)
