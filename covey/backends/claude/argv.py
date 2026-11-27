"""How a turn's Claude CLI is started: its argv (stream_argv), its environment (turn_env) and where the
session's subagent transcripts are (subagents_dir). Which models its subagents run on is backends/claude/subagents.py."""

from __future__ import annotations

import json
from pathlib import Path

from covey.backends.claude import redaction, subagents, tool_guard
from covey.backends.claude.sessions import ClaudeSessionStore, claude_projects_root
from covey.core.harness import CONSULT, LEAD, LOOK, LOOK_ROOTS, PROXY, READ, TOOL_LIMIT, TOOL_NOTES
from covey.core.home import home_dir

# No MCP server at all: the catalog's probe, and a turn held to READ, PROXY or CONSULT (none of the owner's MCP servers).
NO_MCP = ["--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']
# A READ, CONSULT or LEAD turn's tools (core/harness.py): a project lead's (app/project_lead.py) keeps its Bridge tools.
READ_TOOLS = ("Read", "Grep", "Glob")
# What a LOOK turn goes without where its writes are not held (a device's: ``look_hooks``): Claude Code's tools that change files.
FILE_TOOLS = ("Edit", "Write", "NotebookEdit")
NO_AUTO_MEMORY = "CLAUDE_CODE_DISABLE_AUTO_MEMORY"  # Claude Code's auto memory off for the one process (turn_env).
# Claude Code hooks an extension's ``claude_turn`` (core/extensions.py) gives the turn: event name -> matcher groups, as
# Claude Code's settings write them. Each package puts its own here; backends/claude/bridge.py merges every package's by
# event (merge_hooks) and they go into the turn's one --settings, never on a 代答 turn (its hooks are its tool guard's alone)
# or a CONSULT one (it keeps nothing it was given). A hook's command runs where that Claude Code runs: on the device, for a
# device's turn. After Wyu981011/covey 50fc367e (its second half).
TURN_HOOKS = "claude_turn_hooks"


def merge_hooks(*given) -> dict:
    """The hooks tables of ``given`` (each {event: [matcher group, ...]} or empty) merged by event: an event's groups in the
    order given, none replacing another's. ValueError for a table of another shape."""
    merged = {}
    for hooks in given:
        if not hooks:
            continue
        if not isinstance(hooks, dict) or not all(isinstance(event, str) and isinstance(groups, (list, tuple))
                                                  for event, groups in hooks.items()):
            raise ValueError(f"{TURN_HOOKS} must map a Claude Code hook event to a list of matcher groups, got {hooks!r}")
        for event, groups in hooks.items():
            merged.setdefault(event, []).extend(groups)
    return merged


def home_hooks(ctx) -> dict:
    """The home-search hook (backends/claude/tool_guard.py) of a turn started from the home, the service's --workdir
    (core/home.py home_dir): no Bash, Grep or Glob call of it walks the whole home unbounded. Empty for a turn in another
    directory, or on a device (the hook would run there, where this machine's script is not)."""
    if getattr(ctx, "device", "") or not getattr(ctx, "default_workdir", None) or not getattr(ctx, "workdir", None):
        return {}
    home = home_dir(ctx.default_workdir)
    try:
        started_there = Path(ctx.workdir).resolve() == Path(home).resolve()
    except (OSError, RuntimeError):
        return {}
    return tool_guard.home_search_settings(home) if started_there else {}


def look_hooks(ctx) -> dict:
    """The look hook (backends/claude/tool_guard.py ``--mode look``) of a LOOK turn whose writes are held to its checkpoint
    file and the topic's outputs directory (``options[LOOK_ROOTS]`` held, app/tool_limits.py look_roots; W1-C): its edit
    tools and its shell's obvious writes go nowhere else, in its subagents too. Empty for any other turn, and on a device
    (the hook would run there, where this machine's script is not: that turn goes without its file tools instead)."""
    roots = ctx.options.get(LOOK_ROOTS) or {}
    if ctx.options.get(TOOL_LIMIT) != LOOK or getattr(ctx, "device", "") or not roots.get("held"):
        return {}
    return tool_guard.look_settings(roots.get("file", ""), roots.get("dir", ""))


def stream_argv(backend, ctx, session_id=None, *, catalog=False, instructions=""):
    argv = [
        *ctx.backend_cmd,
        "-p",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--replay-user-messages",
        "--permission-prompt-tool",
        "stdio",
    ]
    if catalog:
        # Catalog discovery submits no prompt and loads no project MCP servers,
        # tools or hooks. The regular runtime keeps the user's CLI configuration.
        argv += [
            *NO_MCP,
            "--settings",
            '{"disableAllHooks":true}',
            "--tools",
            "",
        ]
    else:
        if session_id:
            argv += ["--resume", session_id]
            if ctx.options.get("resume_at"):
                # ↩️ 回退: the session's chain up to this entry only; the turn hangs its messages under it.
                argv += ["--resume-session-at", ctx.options["resume_at"]]
        elif ctx.options.get("fork_session"):
            argv += ["--resume", ctx.options["fork_session"]]
            if ctx.options.get("fork_at"):
                # A track of the butler (core/butler_tracks.py): the fork's chain ends at this entry, so a turn of
                # the source that is still running (its question already written) is not carried over.
                argv += ["--resume-session-at", ctx.options["fork_at"]]
            argv += ["--fork-session"]
        if instructions:
            # On every invocation; a resumed session keeps its first one's (app/sessions.py marks those).
            argv += ["--append-system-prompt", instructions]
        if ctx.model:
            argv += ["--model", ctx.model]
        if ctx.effort:
            argv += ["--effort", ctx.effort]
        if ctx.skip_permissions:
            argv += ["--dangerously-skip-permissions"]
        limit = ctx.options.get(TOOL_LIMIT)
        if ctx.options.get("mcp_config") and limit != CONSULT:
            # The butler's turn (the app sets it): its Bridge tools beside the user's own MCP servers, each
            # allowed without asking. Every other run has none of this.
            config = ctx.options["mcp_config"]
            argv += ["--mcp-config", json.dumps(config, ensure_ascii=False, separators=(",", ":")),
                     "--allowedTools", ",".join("mcp__" + name for name in config.get("mcpServers", {}))]
        if limit == CONSULT:
            # A 协作 ask (core/harness.py CONSULT): Read, Grep and Glob and no MCP server at all, whatever the options
            # carry (the Bridge's tools, a lead's tool list): what the turn was given before it was held to this is dropped
            # here, the last word before the CLI starts. No shell, so no write and no network; no AskUserQuestion either.
            argv += ["--tools", ",".join(READ_TOOLS), *NO_MCP]
        elif limit in (READ, PROXY):
            # Its tools narrowed (core/harness.py TOOL_LIMIT), with none of the owner's MCP servers; a turn with Bridge
            # tools of its own (the butler's, a lead's) is never one of these.
            argv += ["--tools", ",".join(READ_TOOLS if limit == READ else tool_guard.TOOLS)]
            argv += [] if ctx.options.get("mcp_config") else NO_MCP
        elif limit == LOOK and not look_hooks(ctx):
            # Its writes not held (a device's): no tool that changes files, in its subagents too; its shell stays for
            # read-only commands. Held, its tools stay and the look hook below checks where they write.
            argv += ["--disallowedTools", ",".join(FILE_TOOLS)]
        elif limit == LEAD:
            # A project's lead (core/harness.py LEAD, app/project_lead.py): Claude Code's own tools narrowed to reading; the
            # MCP tools above (its Bridge tools) are not among the built-in ones --tools names, so they stay.
            argv += ["--tools", ",".join(READ_TOOLS)]
        # Its subagents' read-only Explore on Haiku (backends/claude/subagents.py), unless the setting says "same" or
        # the app sets the subagents' model (a retry after a refusal).
        argv += subagents.argv(ctx)
        # One --settings: a redacted topic's turn, its compaction included, talks to the model through the term relay;
        # a 代答 turn's shell and edit tools go through its PreToolUse hook (backends/claude/tool_guard.py); a turn started
        # from the home has the home-search hook (home_hooks), a held LOOK turn the look hook (look_hooks), ahead of the
        # extensions' (TURN_HOOKS), in any but a CONSULT one.
        settings = redaction.settings(ctx)
        if limit == PROXY:
            settings["hooks"] = tool_guard.settings(ctx.options.get(TOOL_NOTES, ""))
        elif limit != CONSULT:
            hooks = merge_hooks(home_hooks(ctx), look_hooks(ctx), ctx.options.get(TURN_HOOKS))
            if hooks:
                settings["hooks"] = hooks
        argv += ["--settings", json.dumps(settings, ensure_ascii=False)] if settings else []
    return backend.wrap_argv(argv, ctx)


def subagents_dir(ctx, session_id, env):
    """The directory of the session's subagent transcripts; None before the session is known or while
    no subagent has written one."""
    if not session_id:
        return None
    # Under either of the workdir's directories, the session's transcript there or not.
    for directory in ClaudeSessionStore(claude_projects_root(env)).project_dirs(ctx.workdir):
        candidate = directory / session_id / "subagents"
        if candidate.is_dir():
            return candidate
    return None


def turn_env(ctx, env):
    """The environment of a turn's Claude Code: ``env`` with the session state events on. The butler's turn
    (its Bridge tools in ``ctx.options["mcp_config"]``) loads every tool with the session instead of finding them
    through tool search, as Persona's assistant does: a Bridge tool called beside that search went out with no
    arguments and was refused (the first call of topic_start and of topic_send). Its subagents' model as
    backends/claude/subagents.py says. A 代答 turn and a redacted one (through the term relay) neither read nor write
    Claude Code's own memory (``NO_AUTO_MEMORY``): the repository's MEMORY.md is every topic's of that workspace, and what
    such a turn said must reach none of them, nor what they noted reach it (docs/proxy.md, docs/architecture.md 敏感词脱敏)."""
    child = {**subagents.turn_env(ctx, env), "CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS": "1",
             "CLAUDE_CODE_SDK_READS_SESSION_STATE": "1"}
    if ctx.options.get("mcp_config") and ctx.options.get(TOOL_LIMIT) != CONSULT:
        child["ENABLE_TOOL_SEARCH"] = "false"
    if ctx.options.get(TOOL_LIMIT) == PROXY or redaction.settings(ctx):
        child[NO_AUTO_MEMORY] = "1"
    return child
