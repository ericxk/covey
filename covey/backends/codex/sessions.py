"""Codex rollout session store for the Lark bridge.

Sessions are Codex rollout transcripts under ``~/.codex/sessions``. This
module only locates that store.
"""

from __future__ import annotations

from pathlib import Path

from covey.core.paths import codex_home



def rollout_root() -> Path:
    return codex_home() / "sessions"


class CodexSessionStore:
    """`covey.core.harness.SessionStore` over local Codex rollout transcripts."""

    def __init__(self, root: Path | None = None):
        self.root = root

    def sessions_dir(self, workdir: Path) -> Path:
        return self.root if self.root is not None else rollout_root()

    def transcript(self, workdir: Path, session_id: str) -> Path | None:
        """The rollout file of one thread: sessions/YYYY/MM/DD/rollout-<time>-<id>.jsonl."""
        return next(iter(sorted(self.sessions_dir(workdir).glob(f"*/*/*/rollout-*-{session_id}.jsonl"))), None)
