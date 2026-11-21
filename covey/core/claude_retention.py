"""How long Claude Code keeps its session transcripts, read from its settings (never changed here).

Claude Code deletes transcripts older than ``cleanupPeriodDays`` (default ``DEFAULT_DAYS``) for its whole config dir; an old
topic then cannot resume its session and 回退一步 cannot read its transcript. ``check`` reads that one setting from
``$CLAUDE_CONFIG_DIR/settings.json``, else ``~/.claude/settings.json`` (the user-level file only: a project's, a local one and a
managed policy can set it too, and are not read), and ``hint`` is the one-line fix the doctor's 诊断 tab (service/doctor.py,
cards/doctor_card.py) and ``./install`` (service/installer.py) say when it is missing or below ``MIN_DAYS``. Read only.
Standard library only (the doctor and the installer import it)."""
from __future__ import annotations

import json
import os
from pathlib import Path

SETTING = "cleanupPeriodDays"  # Claude Code's own name; checked against its settings schema (2.1.310): positive integer, default 30.
DEFAULT_DAYS = 30
MIN_DAYS = 90  # Below this, a topic left for a quarter loses its session.
SUGGEST_DAYS = 3650
OK, LOW, MISSING, UNREADABLE = "ok", "low", "missing", "unreadable"


def settings_path(environ=None) -> Path:
    """The user-level settings.json of the Claude Code that runs with ``environ`` (default: this process's)."""
    environ = os.environ if environ is None else environ
    home = str(environ.get("CLAUDE_CONFIG_DIR") or "").strip()
    if home:
        return Path(home).expanduser() / "settings.json"
    user = str(environ.get("HOME") or "").strip()
    return (Path(user) if user else Path.home()) / ".claude" / "settings.json"


def check(environ=None) -> dict:
    """{state, days, path}: ``state`` OK (``days`` is MIN_DAYS or more), LOW (less), MISSING (no file, or no usable number in it:
    Claude Code then keeps DEFAULT_DAYS) or UNREADABLE (not JSON, or not readable: it cannot be told). ``days`` the effective
    number, None when UNREADABLE. Never raises."""
    path = settings_path(environ)
    found = {"path": str(path)}
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {**found, "state": MISSING, "days": DEFAULT_DAYS}
    except (OSError, ValueError):
        return {**found, "state": UNREADABLE, "days": None}
    try:
        data = json.loads(text) if text.strip() else {}
    except ValueError:
        return {**found, "state": UNREADABLE, "days": None}
    value = data.get(SETTING) if isinstance(data, dict) else None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return {**found, "state": MISSING, "days": DEFAULT_DAYS}
    return {**found, "state": OK if value >= MIN_DAYS else LOW, "days": value}


def needs_hint(found) -> bool:
    return bool(found) and found.get("state") in (MISSING, LOW)


def hint(found) -> str:
    """The one line: what Claude Code does with old sessions and the fix; '' when nothing needs saying."""
    if not needs_hint(found):
        return ""
    return (f"Claude Code 只保留最近 {found['days']} 天的会话，更早的话题不能接着做、也不能回退一步；"
            f"想留久一点，请自己在 {found['path']} 里加一行 \"{SETTING}\": {SUGGEST_DAYS}（Covey 不会替你改）")
