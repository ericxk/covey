"""agy's private home for one turn that takes the Bridge's tools (``options["mcp_config"]``, the butler's).

agy reads its MCP servers from ``~/.gemini/config/mcp_config.json`` alone (and from its plugins): ``--settings``
refuses ``mcpServers`` and no flag names another file. A turn with tools therefore runs with ``HOME`` a directory of
its own under ``<state>/tmp/agy-home-*`` (owner-only, removed when the turn ends) in which every entry of the real home
is a symbolic link to it, ``.gemini`` and its ``config`` too but for ``config/mcp_config.json``: that one file is this
turn's, the owner's own servers (when the file reads as JSON) with the Bridge's beside them. The login, the
conversations and everything else agy or a command writes go through the links to where they always are; only a new
file at the top of the home (or of those two directories) stays in the private one and goes with it.

Each Bridge server waits for its tools longer than core/butler_requests.py waits for an answer (``TOOL_SECONDS``),
and lists its tools eagerly (``tools.<name>.eager``): without it agy names an MCP tool in a file the model must read
first, the way Claude Code's tool search did, under which a Bridge tool was once called with no arguments. The names
are what the server answers to ``tools/list`` before the turn (``tool_names``); when it says nothing in time, the
tools are still there, only not eager.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from covey.core.state import ensure_private_dir

TOOL_SECONDS = 150  # Above core/butler_requests.py's 120 seconds.
LIST_SECONDS = 10  # How long the server may take to list its tools before the turn.
GEMINI, CONFIG, MCP_FILE = ".gemini", "config", "mcp_config.json"
PROTOCOL = "2025-06-18"


def tool_names(server, env, timeout=LIST_SECONDS) -> list:
    """The names a stdio MCP server ``server`` ({command, args, env}) lists, started with ``env`` and its own env on
    top; [] when it does not answer in ``timeout`` or not as MCP. Its stdin is closed after the three messages, so it
    ends."""
    lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": {"name": "covey", "version": "1"}}},
             {"jsonrpc": "2.0", "method": "notifications/initialized"},
             {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}]
    try:
        done = subprocess.run([server["command"], *(server.get("args") or ())],
                              input="".join(json.dumps(line) + "\n" for line in lines),
                              env={**env, **(server.get("env") or {})}, capture_output=True, text=True,
                              timeout=timeout, start_new_session=True, check=False)
    except (OSError, subprocess.SubprocessError, KeyError, TypeError):
        return []
    for row in done.stdout.splitlines():
        try:
            answer = json.loads(row)
        except ValueError:
            continue
        if isinstance(answer, dict) and answer.get("id") == 2:
            tools = (answer.get("result") or {}).get("tools") or []
            return [tool["name"] for tool in tools if isinstance(tool, dict) and isinstance(tool.get("name"), str)]
    return []


def agy_servers(config, env) -> dict:
    """``config`` (an --mcp-config: {"mcpServers": {name: {command, args, env}}}) as agy's mcp_config.json entries."""
    servers = {}
    for name, server in ((config or {}).get("mcpServers") or {}).items():
        eager = {tool: {"eager": True} for tool in tool_names(server, env)}
        servers[name] = {"command": server["command"], "args": list(server.get("args") or ()),
                         "env": dict(server.get("env") or {}), "timeoutSeconds": TOOL_SECONDS,
                         **({"tools": eager} if eager else {})}
    return servers


def linked(source: Path, target: Path, keep: str) -> None:
    """``target`` (made, empty) with a link to every entry of ``source`` but ``keep``."""
    try:
        entries = list(os.scandir(source))
    except OSError:
        return  # No such directory yet: agy makes what it needs in the private one.
    for entry in entries:
        if entry.name != keep:
            os.symlink(entry.path, target / entry.name)


class PrivateHome:
    """``with PrivateHome(state_dir, home, config, env) as path``: the turn's HOME, removed on the way out. ``home`` is
    the real one, ``config`` the turn's --mcp-config, ``env`` the turn's environment (the servers start with it).
    ``note``: why the owner's own servers are not there, "" when they are (or there are none)."""

    def __init__(self, state_dir, home, config, env):
        self.state_dir, self.home, self.config, self.env = Path(state_dir), Path(home), config, env
        self.path = None
        self.note = ""

    def __enter__(self) -> Path:
        tmp = self.state_dir / "tmp"
        ensure_private_dir(tmp)
        self.path = Path(tempfile.mkdtemp(prefix="agy-home-", dir=str(tmp)))  # mkdtemp is 0700.
        try:
            real = self.home / GEMINI
            linked(self.home, self.path, GEMINI)
            (self.path / GEMINI / CONFIG).mkdir(parents=True, mode=0o700)
            linked(real, self.path / GEMINI, CONFIG)
            linked(real / CONFIG, self.path / GEMINI / CONFIG, MCP_FILE)
            own = self.owner_servers(real / CONFIG / MCP_FILE)
            data = {"mcpServers": {**own, **agy_servers(self.config, self.env)}}
            target = self.path / GEMINI / CONFIG / MCP_FILE
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self.path

    def owner_servers(self, path: Path) -> dict:
        try:
            text = path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            self.note = f"mcp_config.json unreadable: {type(exc).__name__}"
            return {}
        if not text.strip():
            return {}  # agy leaves an empty file when the owner has none.
        try:
            servers = json.loads(text).get("mcpServers") or {}
            return servers if isinstance(servers, dict) else {}
        except (ValueError, AttributeError):
            # agy takes comments and trailing commas; this reader does not, and leaves them out for this turn.
            self.note = "mcp_config.json is not plain JSON: the owner's own MCP servers are left out of this turn"
            return {}

    def __exit__(self, *exc) -> None:
        if self.path is not None:
            shutil.rmtree(self.path, ignore_errors=True)  # Links are removed, never followed.
            self.path = None
