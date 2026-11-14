"""Claude Code session store for the Lark bridge.

Sessions are local Claude Code transcripts under
``~/.claude/projects/<encoded-cwd>/<session-id>.jsonl``. This module only
locates that store for a workdir.
"""

from __future__ import annotations

import os
import re
from pathlib import Path



def encode_project_dir(workdir: Path) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "-", str(workdir))


def claude_projects_root(env=None) -> Path:
    """The projects store of the Claude Code that runs with ``env`` (default: this process's)."""
    home = str((os.environ if env is None else env).get("CLAUDE_CONFIG_DIR") or "").strip()
    return (Path(home).expanduser() if home else Path.home() / ".claude") / "projects"


class ClaudeSessionStore:
    """`covey.core.harness.SessionStore` over local Claude Code transcripts."""

    def __init__(self, root: Path | None = None):
        self.root = root

    def sessions_dir(self, workdir: Path) -> Path:
        return (self.root or claude_projects_root()) / encode_project_dir(workdir)

    def transcript(self, workdir: Path, session_id: str) -> Path | None:
        path = self.sessions_dir(workdir) / f"{session_id}.jsonl"
        return path if path.is_file() else None

    def project_dirs(self, workdir) -> list[Path]:
        """The workdir's directories as given and as the kernel reports it (symlinks resolved), which is how the
        CLI names its project."""
        return [self.sessions_dir(path) for path in dict.fromkeys((Path(workdir), Path(workdir).resolve()))]

    def locate(self, workdir, session_id: str) -> Path | None:
        """The session's transcript in the first of the workdir's directories that has it, or None."""
        for directory in self.project_dirs(workdir):
            path = directory / f"{session_id}.jsonl"
            if path.is_file():
                return path
        return None
