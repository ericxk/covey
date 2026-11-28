"""The optional switches (docs/configuration.md 开关速查): what is on unset, what a new service starts with, the 演示套餐, and
which a service has set.

``DEFAULTS`` are the switches on unless a service sets them off (0, off, false, no), each with the value it holds unset;
each is read where its feature is (core/projects.py and the rest, a test keeps the two the same). ``INSTALL`` is what
./install writes into a new service's manifest (service/installer.py), ``SHOWCASE`` the 演示套餐 on top of it
(`./install --showcase`, `covey_service.py set --preset showcase` through ``preset_changes``), whichever Harnesses work.

Which a service has set is for the people who help with it: the 复制诊断 block (app/diag_summary.py) and the 设置 card's 诊断
tab (service/doctor.py, app/doctor_panel.py, cards/doctor_card.py). Read only, and limited on purpose: a name is shown only when it is one of ``KNOWN`` (the switches that table names; a test keeps
the two the same) and not credential-like (``SECRET_NAME``), a value only when it is short, plain and unchanged by core/text.py
``redact`` (a path, an address or anything long reads 已设). The environment is a service manifest's ``environment`` (the names
it stores, core/env.py ``spellings``) or the Bridge's own. Standard library only (the doctor imports it)."""
from __future__ import annotations

import os
import re

from covey.core import env as cenv
from covey.core.text import redact

KNOWN = (
    "COVEY_BUTLER", "COVEY_PROJECTS", "COVEY_PROJECT_TIDY", "COVEY_REVISIT", "COVEY_MEMORY",
    "COVEY_MEMORY_LEARN", "COVEY_KNOWLEDGE", "COVEY_PROJECT_AUTOWAKE", "COVEY_PROJECT_ASK", "COVEY_PROJECT_MEMORY",
    "COVEY_PROJECT_TRIAGE",
    "COVEY_LEDGER_RECONCILE", "COVEY_DIGEST_TIMES", "COVEY_PINNED_OVERVIEW", "COVEY_PINNED_TOPICS",
    "COVEY_BUTLER_TRACKS", "COVEY_DISPATCH_CARD", "COVEY_PROJECT_UPGRADE", "COVEY_BUTLER_RULES",
    "COVEY_WORKSPACE_GISTS", "COVEY_EVENT_TRANSPORT", "COVEY_CARD_TRANSPORT", "COVEY_SOURCE_WATCH", "COVEY_TOPIC_STEER",
    "COVEY_QUICK_ANSWER_SECONDS", "COVEY_QUIET_HOURS", "COVEY_RELAY_MERGE_SECONDS", "COVEY_APP_CHECK",
    "COVEY_CLAUDE_HOOKS", "COVEY_SYSTEM_RULES", "COVEY_WELCOME", "COVEY_AUTO_RETRY_LONG", "COVEY_LIMIT_NOTICE", "COVEY_QUOTA",
    "COVEY_STRANGER_REPLY", "COVEY_INVITE_URL", "COVEY_DOWN_NOTICE", "COVEY_OWNER_SEND", "COVEY_PROXY_REPLY_CHECK",
    "COVEY_GROUP_MEMBERS", "COVEY_PROXY_PROFILE", "COVEY_WEB", "COVEY_WEB_CONFIG", "COVEY_WEB_SELFHOST",
    "COVEY_WEB_SELFHOST_DIR", "COVEY_WEB_SELFHOST_PORT", "COVEY_WEB_SELFHOST_PUBLIC", "COVEY_WEB_SELFHOST_PUBLIC_PORT",
    "COVEY_WEB_SELFHOST_BIND", "COVEY_DEVICES", "COVEY_NODE_LISTEN", "COVEY_GROUP_ALL_MESSAGES", "COVEY_HISTORY_CMD",
    "COVEY_LOCK_ROOT", "COVEY_AUTO_UPGRADE", "COVEY_COLLAB",
)
# On unset (T813, the owner's call on the default-off switches; COVEY_MEMORY_LEARN by T908 M6): the value each holds then
# (docs/configuration.md 默认已开).
DEFAULTS = {"COVEY_PROJECTS": "1", "COVEY_PROJECT_TIDY": "suggest", "COVEY_PROJECT_AUTOWAKE": "1", "COVEY_PROJECT_ASK": "1",
            "COVEY_PROJECT_MEMORY": "1", "COVEY_PROJECT_TRIAGE": "1", "COVEY_PROJECT_UPGRADE": "butler",
            "COVEY_REVISIT": "1", "COVEY_MEMORY": "1", "COVEY_MEMORY_LEARN": "1", "COVEY_KNOWLEDGE": "1",
            "COVEY_PINNED_OVERVIEW": "1",
            "COVEY_PINNED_TOPICS": "1", "COVEY_DIGEST_TIMES": "09:30,21:30", "COVEY_DISPATCH_CARD": "1",
            "COVEY_BUTLER_TRACKS": "1", "COVEY_BUTLER_RULES": "1", "COVEY_WORKSPACE_GISTS": "3"}
# What ./install writes into a new service's manifest where the shell sets neither spelling (service/installer.py
# default_switches; docs/configuration.md 默认已开): the Feishu SDK, which a service registered before may not have in
# its Python, so its code default stays lark-cli.
INSTALL = {"COVEY_EVENT_TRANSPORT": "sdk", "COVEY_CARD_TRANSPORT": "sdk"}
# The ones of INSTALL that need the Feishu SDK (pyproject.toml's ``feishu-sdk``) in the service's Python: ./install writes them
# only where it imports (service/installer.py feishu_sdk), else the events and cards stay on lark-cli as before.
SDK_SWITCHES = ("COVEY_EVENT_TRANSPORT", "COVEY_CARD_TRANSPORT")
# 演示套餐 (docs/configuration.md 演示套餐): INSTALL and what a demo shows on top of it. `./install --showcase` writes it as
# INSTALL is written; `covey_service.py set --preset showcase` turns it on in a registered service (preset_changes).
SHOWCASE = {**INSTALL, "COVEY_COLLAB": "1"}
PRESETS = {"showcase": SHOWCASE}
OFF_VALUES = ("", "0", "off", "false", "no")  # A preset turns on a switch set to one of these (core/process_env.py's and empty).
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


def holds(name, set_now) -> str:
    """What switch ``name`` holds where ``set_now`` ({name: value as ``shown`` gives it}) is a service's: its value as set,
    its DEFAULTS value where it is not set or empty, '' for neither."""
    value = set_now.get(name)
    if value is None or value == EMPTY:
        return DEFAULTS.get(name, "")
    return str(value)


def is_on(name, set_now) -> bool:
    """Whether switch ``name`` is on where ``set_now`` is a service's (``holds``, not one of OFF_VALUES)."""
    return holds(name, set_now).strip().lower() not in OFF_VALUES


def words(pairs) -> str:
    """The pairs as "NAME=value" apart by "；"; "" without one."""
    return "；".join(f"{name}={value}" for name, value in pairs)


def preset_changes(name, environ) -> dict:
    """{switch: value} of the preset ``name`` (PRESETS) that ``environ`` (a service manifest's environment, any spelling) does
    not set or sets to an off value (OFF_VALUES): what `covey_service.py set --preset` turns on. A switch set to anything else
    (the owner's own digest times, ``COVEY_PROJECT_TIDY=auto``) stays. KeyError for a preset there is not."""
    found = {}
    for switch, value in PRESETS[name].items():
        current = next((environ[spelling] for spelling in cenv.spellings(switch) if spelling in environ), None)
        if current is None or str(current).strip().lower() in OFF_VALUES:
            found[switch] = value
    return found
