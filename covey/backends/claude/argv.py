"""How a turn's Claude CLI is started: its argv (stream_argv), its environment (turn_env) and where the
session's subagent transcripts are (subagents_dir). Which models its subagents run on is backends/claude/subagents.py."""

from __future__ import annotations

import json

from covey.backends.claude import redaction, subagents
from covey.backends.claude.sessions import ClaudeSessionStore, claude_projects_root


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
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
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
        if ctx.options.get("mcp_config"):
            # The butler's turn (the app sets it): its Bridge tools beside the user's own MCP servers, each
            # allowed without asking. Every other run has none of this.
            config = ctx.options["mcp_config"]
            argv += ["--mcp-config", json.dumps(config, ensure_ascii=False, separators=(",", ":")),
                     "--allowedTools", ",".join("mcp__" + name for name in config.get("mcpServers", {}))]
        if ctx.options.get("tools"):
            # A project's lead (app/project_lead.py LEAD_TOOLS): Claude Code's own tools narrowed to these; the MCP tools
            # above are not among the built-in ones --tools names, so they stay.
            argv += ["--tools", ",".join(ctx.options["tools"])]
        # Its subagents' read-only Explore on Haiku (backends/claude/subagents.py), unless the setting says "same" or
        # the app sets the subagents' model (a retry after a refusal).
        argv += subagents.argv(ctx)
        # A redacted topic's turn, its compaction included, talks to the model through the term relay.
        argv += redaction.settings_argv(ctx)
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
    backends/claude/subagents.py says."""
    child = {**subagents.turn_env(ctx, env), "CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS": "1",
             "CLAUDE_CODE_SDK_READS_SESSION_STATE": "1"}
    if ctx.options.get("mcp_config"):
        child["ENABLE_TOOL_SEARCH"] = "false"
    return child
