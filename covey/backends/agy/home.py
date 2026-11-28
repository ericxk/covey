"""agy's private home for one turn that takes the Bridge's tools (``options["mcp_config"]``, the butler's), or the bridge
rules at the system level (app/rules_sent.py: ``rules``, written as the home's ``~/.gemini/GEMINI.md``).

agy reads its MCP servers from ``~/.gemini/config/mcp_config.json`` alone (and from its plugins): ``--settings``
refuses ``mcpServers`` and no flag names another file. A turn with tools therefore runs with ``HOME`` a directory of
its own under ``<state>/tmp/agy-home-*`` (owner-only, removed when the turn ends) in which every entry of the real home
is a symbolic link to it, ``.gemini`` and its ``config`` too but for ``config/mcp_config.json``: that one file is this
turn's, the owner's own servers (when the file reads as JSON) with the Bridge's beside them. The login, the
conversations and everything else agy or a command writes go through the links to where they always are; only a new
file at the top of the home (or of those two directories) stays in the private one and goes with it.

The directory has the same name at each turn of a topic (``home_name``): agy writes the HOME it runs in into the start
of its prompt (the App Data Directory and every skill's path), so a name of its own each turn made the prompt differ from
the previous turn's there and the model's cache served none of the conversation after it (T776: the butler's and the
leads' first request of a turn read nothing from the cache, at any interval; an idle compaction neither). A turn finds
it under a lock (``<name>.lock`` beside it); when another turn of the topic holds it (an idle compaction being stopped),
the turn takes a name of its own as before.

Each Bridge server waits for its tools longer than core/butler_requests.py waits for an answer (``TOOL_SECONDS``),
and lists the tools a turn works with eagerly (``tools.<name>.eager``): agy gives the model those as tools of their own,
and names the others in its prompt, in a file the model must read before it calls one, the way Claude Code's tool
search did, under which a Bridge tool was once called with no arguments. The tools are what the server answers to
``tools/list`` before the turn (``listed_tools``); when it says nothing in time, they are still there, only not eager.

Not all of them, though: agy fits a turn's skills and MCP tools in one budget and, over it, drops the largest whole,
from the declarations and from the list in its prompt alike, without a word (1.5.16: 62 skills and the butler's 21
tools, and topic_start, 5.4 KB, was gone; the butler forked a finished topic for each new matter instead, T750). Eager
are therefore the tools a turn works with, those that hand work to topics first and core/butler_tool_specs.py
SECONDARY last (``eager_names``), until they would take more than ``EAGER_BYTES`` (``COVEY_AGY_EAGER_BYTES``); agy names
the rest, which costs it a line each.
backends/agy/bridge.py ``left_out`` says after the turn when agy dropped some all the same.

The rules: agy puts ``~/.gemini/GEMINI.md`` and ``~/.gemini/config/GEMINI.md`` into its system instruction (its
``<user_rules>``), read again at every turn, after a ``/compact`` too (agy 1.5.18, measured 11-28 against a local fake
model service). The private home's ``GEMINI.md`` is the owner's own (when there is one) and the rules after it; past
``RULES_BYTES`` (agy cuts a rules file there, by lines) the rest goes to ``config/GEMINI.md``, after the owner's own.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from covey.core import env as cenv
from covey.core.butler_tool_specs import HANDING, SECONDARY
from covey.core.file_lock import open_lock
from covey.core.state import ensure_private_dir

TOOL_SECONDS = 150  # Above core/butler_requests.py's 120 seconds.
LIST_SECONDS = 10  # How long the server may take to list its tools before the turn.
GEMINI, CONFIG, MCP_FILE, RULES_FILE = ".gemini", "config", "mcp_config.json", "GEMINI.md"
# agy's own state (its conversations) under ~/.gemini: made in the real home when it is not there yet, so a first turn's
# conversation goes through the link to where the next turn finds it, not into the private home that goes with the turn.
STATE_DIR = "antigravity-cli"
RULES_BYTES = 23000  # Of one rules file: agy 1.5.18 cuts one at 24000 bytes, by lines.
PROTOCOL = "2025-06-18"
# What a server's eager tools may take (``tool_size``). The butler's that work with topics take 14.7 KB with the
# projects on; on 1.5.16 that and the others by name fit beside 62 skills (40 KB of them), all 21 eager did not.
EAGER_ENV = "COVEY_AGY_EAGER_BYTES"
EAGER_BYTES = 15000


def listed_tools(server, env, timeout=LIST_SECONDS) -> list:
    """The tools ({name, description, inputSchema}) a stdio MCP server ``server`` ({command, args, env}) lists, started
    with ``env`` and its own env on top; [] when it does not answer in ``timeout`` or not as MCP. Its stdin is closed
    after the three messages, so it ends."""
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
            return [tool for tool in tools if isinstance(tool, dict) and isinstance(tool.get("name"), str)]
    return []


def tool_size(tool) -> int:
    """About what ``tool`` takes of agy's budget: its description and input schema (compact JSON), in UTF-8 bytes."""
    schema = json.dumps(tool.get("inputSchema") or {}, ensure_ascii=False, separators=(",", ":"))
    return len(str(tool.get("description") or "").encode()) + len(schema.encode())


def eager_names(tools, room) -> list:
    """The names of ``tools`` agy is to load up front: those that hand work to topics (HANDING), then the others but
    SECONDARY, then SECONDARY, each in the server's order; one that would take the eager ones past ``room`` bytes
    (``tool_size``) is left out (it takes none of the room) and the smaller ones after it still go in."""
    names, used = [], 0
    for tool in sorted(tools, key=lambda tool: (tool["name"] in SECONDARY, tool["name"] not in HANDING)):  # Stable.
        size = tool_size(tool)
        if used + size > room:
            continue
        used += size
        names.append(tool["name"])
    return names


def eager_room(env) -> int:
    """EAGER_BYTES, or COVEY_AGY_EAGER_BYTES of ``env`` when it is a whole number (0: every tool by name only)."""
    value = str(cenv.get(EAGER_ENV, "", env) or "").strip()
    return int(value) if value.isdigit() else EAGER_BYTES


def agy_servers(config, env) -> tuple[dict, dict]:
    """``config`` (an --mcp-config: {"mcpServers": {name: {command, args, env}}}) as agy's mcp_config.json entries, and
    the names of the tools each server listed ({name: [tool]})."""
    servers, listed, room = {}, {}, eager_room(env)
    for name, server in ((config or {}).get("mcpServers") or {}).items():
        tools = listed_tools(server, env)
        listed[name] = [tool["name"] for tool in tools]
        eager = {tool: {"eager": True} for tool in eager_names(tools, room)}
        servers[name] = {"command": server["command"], "args": list(server.get("args") or ()),
                         "env": dict(server.get("env") or {}), "timeoutSeconds": TOOL_SECONDS,
                         **({"tools": eager} if eager else {})}
    return servers, listed


def linked(source: Path, target: Path, keep) -> None:
    """``target`` (made, empty) with a link to every entry of ``source`` but ``keep`` (a name, or several)."""
    keep = (keep,) if isinstance(keep, str) else tuple(keep)
    try:
        entries = list(os.scandir(source))
    except OSError:
        return  # No such directory yet: agy makes what it needs in the private one.
    for entry in entries:
        if entry.name not in keep:
            os.symlink(entry.path, target / entry.name)


def rules_parts(rules: str) -> tuple[str, str]:
    """``rules`` as (what fits ``RULES_BYTES`` in ``GEMINI.md``, the rest for ``config/GEMINI.md``), cut between lines."""
    head, size = [], 0
    lines = str(rules or "").splitlines(keepends=True)
    for index, line in enumerate(lines):
        size += len(line.encode("utf-8"))
        if size > RULES_BYTES and head:
            return "".join(head), "".join(lines[index:])
        head.append(line)
    return "".join(head), ""


def owners_rules(path: Path) -> str:
    """The owner's own rules file at ``path`` (followed through its link), '' when there is none or it cannot be read."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def write_private(path: Path, text: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)


def home_name(key, home) -> str:
    """The private home's name for ``key`` (a topic) over the real ``home``: the same at each of its turns."""
    return "agy-home-" + hashlib.sha256(f"{key}\0{home}".encode()).hexdigest()[:16]


class PrivateHome:
    """``with PrivateHome(state_dir, home, config, env, key, rules) as path``: the turn's HOME, removed on the way out.
    ``home`` is the real one, ``config`` the turn's --mcp-config (None: the owner's MCP servers as they are), ``env`` the
    turn's environment (the servers start with it), ``key`` the topic whose turns share the name (``home_name``; "": a
    name of its own), ``rules`` the bridge rules for its GEMINI.md ('' none).
    ``note``: why the owner's own servers are not there, "" when they are (or there are none). ``listed``: the tools
    each of ``config``'s servers listed, {server: [name]}."""

    def __init__(self, state_dir, home, config, env, key="", rules=""):
        self.state_dir, self.home, self.config, self.env = Path(state_dir), Path(home), config, env
        self.key = str(key or "")
        self.rules = str(rules or "")  # The bridge rules, at the system level (the module's docstring); '' none.
        self.path = None
        self.lock = None  # The descriptor that holds the topic's name while the turn runs.
        self.note = ""
        self.listed = {}

    def claim(self, tmp: Path):
        """The topic's home, made empty, with its lock held; None without a key or when another turn holds it."""
        if not self.key:
            return None
        name = home_name(self.key, self.home)
        try:
            lock = open_lock(tmp / f"{name}.lock")
        except (OSError, ValueError):
            return None
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            path = tmp / name
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)  # What a turn that was killed left; links are removed, never followed.
            os.mkdir(path, 0o700)
        except OSError:
            os.close(lock)
            return None
        self.lock = lock
        return path

    def __enter__(self) -> Path:
        tmp = self.state_dir / "tmp"
        ensure_private_dir(tmp)
        self.path = self.claim(tmp) or Path(tempfile.mkdtemp(prefix="agy-home-", dir=str(tmp)))  # mkdtemp is 0700.
        try:
            real = self.home / GEMINI
            try:
                (real / STATE_DIR).mkdir(parents=True, mode=0o700, exist_ok=True)
            except OSError:
                pass  # A home that cannot take it: agy makes it where it can, as before.
            first, rest = rules_parts(self.rules)
            linked(self.home, self.path, GEMINI)
            (self.path / GEMINI / CONFIG).mkdir(parents=True, mode=0o700)
            linked(real, self.path / GEMINI, (CONFIG, RULES_FILE) if first else CONFIG)
            linked(real / CONFIG, self.path / GEMINI / CONFIG,
                   (*((MCP_FILE,) if self.config else ()), *((RULES_FILE,) if rest else ())))
            if self.config:
                own = self.owner_servers(real / CONFIG / MCP_FILE)
                servers, self.listed = agy_servers(self.config, self.env)
                data = {"mcpServers": {**own, **servers}}
                write_private(self.path / GEMINI / CONFIG / MCP_FILE, json.dumps(data, ensure_ascii=False, indent=2))
            if first:
                write_private(self.path / GEMINI / RULES_FILE,
                              "\n\n".join(part for part in (owners_rules(real / RULES_FILE), first) if part))
            if rest:
                write_private(self.path / GEMINI / CONFIG / RULES_FILE,
                              "\n\n".join(part for part in (owners_rules(real / CONFIG / RULES_FILE), rest) if part))
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
        if self.lock is not None:  # After the removal: the next turn of the topic makes it again.
            os.close(self.lock)
            self.lock = None
