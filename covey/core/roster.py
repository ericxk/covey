"""The Harness backends the unified bot routes, in one table.

A new backend is added here first; the per-backend sets used for routing,
reply validation and question proofs are derived from this table.
scripts/setup_harness_bridge.sh probes the same CLIs (tests/test_structure.py checks it).
"""

from __future__ import annotations

from dataclasses import dataclass

from covey.core.extensions import ExtensionError, manifests


@dataclass(frozen=True)
class RosterEntry:
    module: str  # Defines <ID>_SPEC and registers the backend.
    store: str  # Session store class, in store_module.
    store_module: str = ""  # The module defining the store class; "" is ``module``.
    live_questions: bool = False  # Keeps the turn open while a native question waits for its answer.
    paused_questions: bool = False  # Interrupts the turn on a question and resumes it with the answer.
    after: str = ""  # An extension's place in NAMES: right after this Harness (the card lists keep that order); "" appends.


CORE_HARNESSES = {
    "codex": RosterEntry("covey.backends.codex.bridge", "CodexSessionStore",
                         "covey.backends.codex.sessions", paused_questions=True),
    "claude": RosterEntry("covey.backends.claude.bridge", "ClaudeSessionStore",
                          "covey.backends.claude.sessions", live_questions=True),
    # DeepSeek Harness, on Acme's Anthropic Messages models (Acme's Responses models are a Codex provider).
    "dsh": RosterEntry("covey.backends.dsh.bridge", "DshSessionStore"),
    # Pi, on GPT models from the Codex login or Acme; questions go through the checkpoint like dsh's.
    "pi": RosterEntry("covey.backends.pi.bridge", "PiSessionStore"),
}
# A Harness is the agent program; where its models come from is a provider (core/providers.py), never a
# Harness of its own. Removed Harnesses by ID, with the name their topics showed. Old topics, jobs and
# saved results keep naming them; app/unified.py refuses to route those topics, and
# any other ID no Harness here has.
CORE_RETIRED = {
    "neeko": "Neeko",  # The OpenCode-based Neeko.
    # Acme wrappers, replaced by direct personal-SK access (Codex and dsh).
    "codex-w": "Codex-W",
    "claude-w": "Claude-W",
    # Neeko CLI v3 over the Codex protocol; Neeko is no longer maintained (2026-11-02).
    "neeko-next": "Neeko Next",
    # Claude Code on 豆包工作, a Harness until 2026-11-03 and then a provider until it was removed (2026-11-05).
    "doubao": "豆包",
    # Kiro, retired 2026-11-01 (45a1f5e); its topics showed the generic notice until 2026-11-05.
    "kiro": "Kiro",
}


def merged(core: dict, core_retired: dict, found) -> tuple[dict, dict]:
    """(HARNESSES, RETIRED) of the core tables and the enabled extensions' manifests (core/extensions.py), the core
    first and every order kept. A name the core or another extension already has raises ExtensionError."""
    harnesses, retired = dict(core), dict(core_retired)
    owners = dict.fromkeys(harnesses, "the core") | dict.fromkeys(retired, "the core")
    for extension in found:
        for name in [*extension.harnesses, *extension.retired_harnesses]:
            if name in owners:
                raise ExtensionError(extension.package, f"Harness name {name!r} is already used by {owners[name]}")
            owners[name] = extension.package
        for name, fields in extension.harnesses.items():
            entry = RosterEntry(**fields)
            if entry.after in harnesses:
                items = list(harnesses.items())
                at = [key for key, _ in items].index(entry.after) + 1
                harnesses = dict(items[:at] + [(name, entry)] + items[at:])
            else:
                harnesses[name] = entry
        retired.update(extension.retired_harnesses)
    return harnesses, retired


HARNESSES, RETIRED = merged(CORE_HARNESSES, CORE_RETIRED, manifests())
NAMES = tuple(HARNESSES)  # The first one is the default route.
LIVE_QUESTIONS = frozenset(name for name, h in HARNESSES.items() if h.live_questions)
PAUSED_QUESTIONS = frozenset(name for name, h in HARNESSES.items() if h.paused_questions)
