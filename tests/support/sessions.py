"""A topic's native session id written or cleared outright, for setting up a test. The Bridge itself
only changes it with core/state.py's compare_and_set_session; these take the same lock and bump the
same revision, without the compare."""
from __future__ import annotations

from pathlib import Path
from typing import Pattern

from covey.core.session_state import _clear_session_locked, _locked_session_state, _write_session_locked


def write_session_id(path: Path, session_id: str) -> None:
    with _locked_session_state(path):
        _write_session_locked(path, session_id)


def clear_session_id(path: Path, pattern: Pattern[str]) -> str | None:
    """Remove the session id at ``path``; the one it held, None when there was none."""
    with _locked_session_state(path):
        return _clear_session_locked(path, pattern)
