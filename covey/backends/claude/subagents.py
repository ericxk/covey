"""Claude Code's subagents on lighter models than the turn's (main-thread.json ``subagents``, the 设置 card's 新话题
tab; core/butler_settings.py SUBAGENTS). Without it, every subagent, even the built-in Explore, runs on the
turn's model (claude 2.1.318, measured 2026-11-10).

"light" (the default): every turn's claude, topics and the butler alike, its compaction included, gets
CLAUDE_CODE_SUBAGENT_MODEL=sonnet and, through ``--agents``, a read-only Explore on Haiku (EXPLORE) in place of the
built-in one; editing and decisions stay with the turn's model. A turn on Haiku keeps its subagents on Haiku (no
CLAUDE_CODE_SUBAGENT_MODEL): they are never bigger than the turn's model. "same": nothing is added, Claude Code's own
way. The catalog probe gets neither.

``ctx.options["subagent_model"]``, when the app sets it (a retry on another model after a refusal), wins over the
setting for CLAUDE_CODE_SUBAGENT_MODEL: that model, or "" for none (the Bridge's own environment's value removed too).
Such a turn gets no Haiku Explore either: the built-in Explore then follows CLAUDE_CODE_SUBAGENT_MODEL, so no subagent
of a retry runs on a current model that could refuse it again. A runner of an older release has none of this.
"""
from __future__ import annotations

import json

from covey.core import butler_settings

MODEL_ENV = "CLAUDE_CODE_SUBAGENT_MODEL"
LIGHT = "sonnet"  # The general-purpose subagent's model with "light".
OPTION = "subagent_model"  # ctx.options key that overrides the setting for MODEL_ENV.
EXPLORE = {"Explore": {
    "description": ("Read-only search agent: finds files, searches code, reads logs. Use it to locate things; it "
                    "reports paths, line numbers and short excerpts, never edits and never judges."),
    "prompt": ("你是只读的搜索助手。You only look things up: find files, search code, read logs and outputs. "
               "Report what you found as file paths with line numbers and short verbatim excerpts. Never edit, "
               "write or delete anything, never run commands that change state, and give no opinions, "
               "judgments or fixes: the caller decides."),
    "tools": ["Read", "Grep", "Glob", "Bash"],
    "model": "haiku",
}}


def mode(ctx) -> str:
    """The setting for ``ctx``'s Bridge: "light" where it cannot be read (a context without a state dir)."""
    state_dir = getattr(ctx, "state_dir", None)
    if not state_dir:
        return butler_settings.SUBAGENTS[0]
    try:
        return butler_settings.load(state_dir).subagents
    except (OSError, ValueError):
        return butler_settings.SUBAGENTS[0]


def light(ctx) -> bool:
    return mode(ctx) == "light"


def on_haiku(ctx) -> bool:
    return "haiku" in str(getattr(ctx, "model", "") or "").lower()


def turn_env(ctx, env: dict) -> dict:
    """``env`` with the subagents' model for the turn of ``ctx``."""
    options = getattr(ctx, "options", None) or {}
    if OPTION in options:
        chosen = str(options[OPTION] or "")
        if chosen:
            return {**env, MODEL_ENV: chosen}
        return {key: value for key, value in env.items() if key != MODEL_ENV}
    if light(ctx) and not on_haiku(ctx):
        return {**env, MODEL_ENV: LIGHT}
    return env


def argv(ctx) -> list[str]:
    """``--agents`` with EXPLORE for a turn's claude when the setting is "light" and the app sets no subagents' model
    (OPTION), else nothing."""
    options = getattr(ctx, "options", None) or {}
    if OPTION in options or not light(ctx):
        return []
    return ["--agents", json.dumps(EXPLORE, ensure_ascii=False, separators=(",", ":"))]
