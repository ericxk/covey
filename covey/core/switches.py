"""Which of the optional switches (docs/configuration.md 开关速查) a service has set, for the people who help with it: the 复制诊断
block (app/diag_summary.py) and the 设置 card's 诊断 tab (service/doctor.py, app/doctor_panel.py, cards/doctor_card.py).

Read only, and limited on purpose: a name is shown only when it is one of ``KNOWN`` (the switches that table names; a test keeps
the two the same) and not credential-like (``SECRET_NAME``), a value only when it is short, plain and unchanged by core/text.py
``redact`` (a path, an address or anything long reads 已设). The environment is a service manifest's ``environment`` (the names
it stores, core/env.py ``spellings``) or the Bridge's own. Standard library only (the doctor imports it)."""
from __future__ import annotations

import os
import re

from covey.core import env as cenv
from covey.core.text import redact

KNOWN = (
    "COVEY_BUTLER", "COVEY_PROJECTS", "COVEY_PROJECT_TIDY", "COVEY_STANDING_ANSWERS", "COVEY_BOARD",
    "COVEY_SOURCE_WATCH", "COVEY_TOPIC_STEER", "COVEY_QUICK_ANSWER_SECONDS", "COVEY_QUIET_HOURS",
    "COVEY_RELAY_MERGE_SECONDS", "COVEY_APP_CHECK", "COVEY_CLAUDE_HOOKS", "COVEY_WELCOME", "COVEY_STRANGER_REPLY",
    "COVEY_INVITE_URL", "COVEY_DOWN_NOTICE", "COVEY_OWNER_SEND", "COVEY_PROXY_REPLY_CHECK", "COVEY_DIGEST_TIMES",
    "COVEY_PINNED_OVERVIEW", "COVEY_PINNED_TOPICS", "COVEY_MEMORY", "COVEY_KNOWLEDGE", "COVEY_BUTLER_TRACKS",
    "COVEY_DISPATCH_CARD", "COVEY_GROUP_MEMBERS", "COVEY_REVIEW", "COVEY_PROJECT_AUTOWAKE", "COVEY_PROJECT_ASK",
    "COVEY_PROJECT_MEMORY", "COVEY_PROJECT_TRIAGE", "COVEY_PROJECT_UPGRADE", "COVEY_PROXY_PROFILE",
    "COVEY_JUDGE_GATEWAY", "COVEY_WEB", "COVEY_WEB_CONFIG", "COVEY_DEVICES", "COVEY_NODE_LISTEN",
    "COVEY_GROUP_ALL_MESSAGES", "COVEY_HISTORY_CMD", "COVEY_BUTLER_RULES", "COVEY_PATROL", "COVEY_WORKSPACE_GISTS",
    "COVEY_CARD_TRANSPORT", "COVEY_EVENT_TRANSPORT", "COVEY_LOCK_ROOT", "COVEY_REVISIT",
)
SECRET_NAME = re.compile(r"SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|AUTH|COOKIE|(^|_)KEY($|_)")
PLAIN = re.compile(r"[A-Za-z0-9_.:,=+-]{1,32}")
LONG_RUN = re.compile(r"[A-Za-z0-9]{12,}")  # No switch word is this long; a key or an id is.
HIDDEN = "已设"
EMPTY = "空"


def shown_value(value) -> str:
    """``value`` when it is short, plain and no secret; else HIDDEN (EMPTY for an empty one)."""
    text = "" if value is None else str(value).strip()
    if not text:
        return EMPTY
    if PLAIN.fullmatch(text) and not LONG_RUN.search(text) and redact(text) == text:
        return text
    return HIDDEN


def shown(environ=None) -> list:
    """[(name, value as shown)] of the ``KNOWN`` switches ``environ`` (a service manifest's environment; default the process's)
    sets, in the table's order. Never raises."""
    environ = os.environ if environ is None else environ
    found = []
    try:
        for name in KNOWN:
            if SECRET_NAME.search(name):
                continue
            value = next((environ[spelling] for spelling in cenv.spellings(name) if spelling in environ), None)
            if value is not None:
                found.append((name, shown_value(value)))
    except (AttributeError, TypeError):
        return []
    return found


def words(pairs) -> str:
    """The pairs as "NAME=value" apart by "；"; "" without one."""
    return "；".join(f"{name}={value}" for name, value in pairs)
