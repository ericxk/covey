"""A 协作 ask's Codex turn (core/harness.py CONSULT, docs/architecture.md 多参与者协作 · 能力与预检): read-only in a
CODEX_HOME of its own, so that nothing of the owner's Codex setup that runs outside the sandbox or writes on its own reaches
the turn. Every other turn runs as before.

- ``ConsultHome`` (``with ConsultHome(state_dir, env) as env``): a directory under <state>/tmp/codex-consult-* (owner-only,
  removed when the turn ends) in which every entry of the real CODEX_HOME is a symbolic link to it, except ``LEFT_OUT``:
  the exec-policy rules (an allow rule runs a command outside the sandbox), the hooks (they run outside it too), the
  plugins, the skills, the memories (made and read across sessions) and config.toml, whose place a config of its own takes
  (``minimal_config``: which model and provider, nothing else, so no MCP server, no project trust, no feature of the
  owner's). The login, the threads (sessions/, archived_sessions/, the session index, the state databases) go through the
  links to where they always are: a fork finds its source, and its rollout lands beside the others.
- ``FLAGS``: the app server's switches for such a turn on top: apps (the ChatGPT connectors' tools), plugins, memories
  (making and using), hooks, goals, image generation, web search and the non-blocking question off. backends/codex/appserver.py adds
  the read-only sandbox (its subagents inherit it) and leaves the Bridge's tools out (``bridge_servers``);
  backends/codex/turn.py refuses its every request for the owner's input (``rpc.no_questions``).

A turn held to READ (a group member's turn) or LEAD (a project lead's) is read-only on the topic's own thread, so it gets
less. ``READING_FLAGS`` turn off apps, plugins, memories and hooks and leave
the rest as for every turn (the question, web search, skills). A READ turn that resumes the thread also runs in a
``ReadingHome``: all but ``READING_LEFT_OUT`` (the rules, the hooks, the plugins and the memories) linked, the owner's
config.toml among them. A thread started or forked in such a home is recorded under it and cannot be resumed once it is
gone, so a READ or LEAD turn that starts the thread stays in the owner's home, where an
exec-policy allow rule still applies (backends/codex/bridge.py ``run``).
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from covey.core.state import ensure_private_dir

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 (pyproject requires-python >=3.10): tomllib is 3.11's; core/toml_compat.py is
    from covey.core import toml_compat as tomllib  # tomli, the same reader, kept in the tree.

# Never linked into the turn's home. "memories" covers the folder and its databases (memories_1.sqlite, memories.lock);
# history.jsonl is the owner's prompt history, which an ask's words would join.
LEFT_OUT = ("rules", "hooks", "hooks.json", "plugins", "skills", "memories", "config.toml", "history.jsonl")
# The app server ignores a key it does not know (codex-cli 0.171.0, C8): tests/test_collab_backends.py pins these words.
FLAGS = ("features.apps=false", "features.plugins=false", "features.remote_plugin=false", "features.memories=false",
         "memories.generate_memories=false", "memories.use_memories=false", "features.hooks=false",
         "features.goals=false", "features.image_generation=false", "features.default_mode_request_user_input=false",
         'web_search="disabled"')
# A READ or LEAD turn's: what runs outside the read-only sandbox (an exec-policy allow rule, a hook, a plugin's hook or MCP
# server) or writes on its own across sessions (the memories); "memories" again covers its databases.
READING_LEFT_OUT = ("rules", "hooks", "hooks.json", "plugins", "memories")
READING_FLAGS = ("features.apps=false", "features.plugins=false", "features.remote_plugin=false", "features.memories=false",
                 "memories.generate_memories=false", "memories.use_memories=false", "features.hooks=false",
                 "features.default_mode_request_user_input=true")
KEPT = ("model_provider", "model", "service_tier")  # Of the owner's config.toml, all a consult's own config keeps.
BUILT_IN = frozenset({"openai", "oss", "ollama", "lmstudio"})  # Providers Codex knows without a table of the owner's.


def real_home(env) -> Path:
    """The CODEX_HOME the turn would have used: the turn's environment's, else ~/.codex."""
    home = str((env or {}).get("CODEX_HOME") or os.environ.get("CODEX_HOME") or "").strip()
    return Path(home).expanduser() if home else Path.home() / ".codex"


def left_out(name: str) -> bool:
    return name in LEFT_OUT or name.startswith("memories")


def _toml_value(value):
    """``value`` as TOML (strings, numbers, booleans, flat tables and lists of them); None when it is anything else."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")\
            .replace("\t", "\\t") + '"'
    if isinstance(value, list):
        parts = [_toml_value(item) for item in value]
        return None if any(part is None for part in parts) else "[" + ", ".join(parts) + "]"
    if isinstance(value, dict):
        parts = [(key, _toml_value(item)) for key, item in value.items()]
        if any(part is None for _, part in parts):
            return None
        return "{" + ", ".join(f"{_toml_key(key)} = {part}" for key, part in parts) + "}"
    return None


def _toml_key(key) -> str:
    key = str(key)
    return key if key and all(c.isalnum() or c in "-_" for c in key) else _toml_value(key)


def minimal_config(text: str) -> str:
    """The config.toml of a consult's home from the owner's ``text``: ``KEPT`` and, for a provider of the owner's own
    (not ``BUILT_IN``), its ``model_providers`` table. '' for a config that does not read as TOML: the consult runs on
    Codex's defaults then. Python 3.10 reads it as 3.11+ does (core/toml_compat.py)."""
    try:
        data = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError):
        return ""
    lines = []
    for key in KEPT:
        value = _toml_value(data.get(key)) if key in data else None
        if value is not None:
            lines.append(f"{key} = {value}")
    provider = data.get("model_provider")
    table = ((data.get("model_providers") or {}).get(provider) if isinstance(provider, str) else None)
    if isinstance(table, dict) and provider not in BUILT_IN:
        lines.append(f"\n[model_providers.{_toml_key(provider)}]")
        for key, item in table.items():
            value = _toml_value(item)
            if value is not None:
                lines.append(f"{_toml_key(key)} = {value}")
    return "\n".join(lines) + "\n" if lines else ""


class ConsultHome:
    """``with ConsultHome(state_dir, env) as turn_env``: ``env`` with CODEX_HOME the turn's own home, removed on the
    way out (its links are removed, never followed)."""

    PREFIX = "codex-consult-"

    def __init__(self, state_dir, env):
        self.state_dir, self.env = Path(state_dir), dict(env or {})
        self.path = None

    @staticmethod
    def leaves_out(name: str) -> bool:
        return left_out(name)

    @staticmethod
    def own_config(real: Path):
        """The text of the home's own config.toml (``minimal_config`` of the owner's), or None to link the owner's."""
        try:
            text = (real / "config.toml").read_text(encoding="utf-8")
        except OSError:
            text = ""
        return minimal_config(text)

    def __enter__(self) -> dict:
        tmp = self.state_dir / "tmp"
        ensure_private_dir(tmp)
        self.path = Path(tempfile.mkdtemp(prefix=self.PREFIX, dir=str(tmp)))  # mkdtemp is 0700.
        try:
            real = real_home(self.env)
            try:
                entries = list(os.scandir(real))
            except OSError:
                entries = []
            for entry in entries:
                if not self.leaves_out(entry.name):
                    os.symlink(entry.path, self.path / entry.name)
            config = self.own_config(real)
            if config is not None:
                descriptor = os.open(self.path / "config.toml", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(config)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return {**self.env, "CODEX_HOME": str(self.path)}

    def __exit__(self, *exc) -> None:
        if self.path is not None:
            shutil.rmtree(self.path, ignore_errors=True)
            self.path = None


class ReadingHome(ConsultHome):
    """``with ReadingHome(state_dir, env) as turn_env``: a READ or LEAD turn's home, <state>/tmp/codex-read-*: every entry
    of the real CODEX_HOME linked but ``READING_LEFT_OUT``, the owner's config.toml among them; removed as a consult's."""

    PREFIX = "codex-read-"

    @staticmethod
    def leaves_out(name: str) -> bool:
        return name in READING_LEFT_OUT or name.startswith("memories")

    @staticmethod
    def own_config(real: Path):
        return None
