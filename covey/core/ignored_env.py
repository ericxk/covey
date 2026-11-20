"""Backend parameters the environment no longer sets, and the two whose old values a service must not keep.

The backends once took these defaults from the environment (deprecated, then no longer
read). A service registered with one still has it in its manifest's environment, so the Bridge names
each at startup with what to write instead (``ignored_env``). Two of them restricted permissions; without
them the backend's default is the open one, so a value that restricted is refused (``loosened``): the startup
line says so, covey_doctor fails, deploy_bridge.sh --check-only and bootstrap_harness_bridge.sh stop.
Standard library only: the doctor and the deploy script import it.
"""

from __future__ import annotations

import os

from covey.core import env as cenv
from covey.core import extensions
from covey.core.process_env import FALSE_VALUES
from covey.core.roster import NAMES

# name -> (Harness, the option that replaces it). A switch's option depends on its value: (when on, when off),
# None where the value is the default already.
IGNORED_ENV = {
    "LARK_BRIDGE_CODEX_MODEL": ("codex", "--model"),
    "LARK_BRIDGE_CODEX_REASONING_EFFORT": ("codex", "--reasoning-effort"),
    "LARK_BRIDGE_CODEX_SANDBOX": ("codex", "--sandbox"),
    "LARK_BRIDGE_CODEX_CMD": ("codex", "--codex-cmd"),
    "LARK_BRIDGE_TIMEOUT": ("codex", "--codex-timeout"),
    "LARK_CLAUDE_BRIDGE_MODEL": ("claude", "--model"),
    "LARK_CLAUDE_BRIDGE_EFFORT": ("claude", "--effort"),
    "LARK_CLAUDE_BRIDGE_CLAUDE_CMD": ("claude", "--claude-cmd"),
    "LARK_CLAUDE_BRIDGE_TIMEOUT": ("claude", "--claude-timeout"),
    "LARK_CLAUDE_BRIDGE_SKIP_PERMISSIONS": ("claude", (None, "--no-skip-permissions")),
    "LARK_CLAUDE_BRIDGE_LOGIN_SHELL": ("claude", ("--claude-login-shell", None)),
    "LARK_DSH_BRIDGE_CMD": ("dsh", "--dsh-cmd"),
    "LARK_PI_BRIDGE_CMD": ("pi", "--pi-cmd"),
    **extensions.ignored_env(),  # An extension's Harnesses.
}
CODEX_SANDBOX = "danger-full-access"  # backends/codex/bridge.py's DEFAULT_SANDBOX.


def where(harness):
    """Where a Harness's options go: the default Harness's on the service command line (harnesses.json's entry
    for it is never read), the others' in harnesses.json."""
    return "the service command line" if harness == NAMES[0] else "harnesses.json"


def loosened(environ=None):
    """``NAME=value: what it would do; what to write instead`` for each permission variable whose value
    restricted what its backend may do, now that the backend runs with its open default."""
    environ = os.environ if environ is None else environ
    lines = []
    sandbox = cenv.get("LARK_BRIDGE_CODEX_SANDBOX", None, environ)  # Under either spelling.
    if sandbox is not None and sandbox.strip() != CODEX_SANDBOX:
        lines.append(f"LARK_BRIDGE_CODEX_SANDBOX={sandbox} is no longer read, so Codex would run with "
                     f"{CODEX_SANDBOX}; unset it and put --sandbox {sandbox.strip() or '<mode>'} in "
                     f"{where('codex')}")
    skip = environ.get("LARK_CLAUDE_BRIDGE_SKIP_PERMISSIONS")
    if skip is not None and skip.strip().lower() in FALSE_VALUES:
        lines.append(f"LARK_CLAUDE_BRIDGE_SKIP_PERMISSIONS={skip} is no longer read, so Claude would skip its "
                     f"permission prompts; unset it and put --no-skip-permissions in {where('claude')}")
    return lines


def ignored_env(environ=None):
    """One startup line for each variable of IGNORED_ENV that is set, saying what to write instead; a loosened
    permission's line says what it would do (``loosened``)."""
    environ = os.environ if environ is None else environ
    loose = {line.split("=", 1)[0]: line for line in loosened(environ)}
    lines = []
    for name, (harness, option) in IGNORED_ENV.items():
        if not cenv.has(name, environ):
            continue
        if name in loose:
            lines.append(f"env {loose[name]} (covey_doctor fails until then)")
            continue
        if isinstance(option, tuple):
            option = option[str(cenv.get(name, "", environ)).strip().lower() in FALSE_VALUES]
        lines.append(f"env {name} is no longer read; " + (
            f"put {option} in {where(harness)}" if option else "unset it: its value is the default"))
    return lines
