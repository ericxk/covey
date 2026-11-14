"""Environment for backend child processes, and on/off switches read from the environment."""

from __future__ import annotations

import os
from pathlib import Path

from covey.core import env as cenv


def child_process_env(workdir: Path | None = None) -> dict[str, str]:
    env = os.environ.copy()
    if workdir is not None:
        # subprocess cwd does not update inherited PWD for tools reading env.
        env["PWD"] = str(Path(workdir).resolve())
    return env


FALSE_VALUES = frozenset({"0", "false", "no", "off"})


def env_flag(name: str, default: bool) -> bool:
    """Unset gives the default; 0/false/no/off in any case turn a switch off, any other value on."""
    raw = cenv.get(name)
    return default if raw is None else raw.strip().lower() not in FALSE_VALUES
