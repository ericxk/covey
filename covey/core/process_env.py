"""Environment for backend child processes, and on/off switches read from the environment."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from covey.core import env as cenv

log = logging.getLogger("covey.devices")
# What a task on a device never gets from here, whoever put it in the device's environment: the hub's own settings,
# tokens and service wiring (the names are the hub's, a device has its own login).
HUB_ONLY = ("LARK_", "FEISHU_", "COVEY_", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CODEX_HOME",
            "GITHUB_TOKEN", "GH_TOKEN")


def hub_only(name: str) -> bool:
    return name.startswith(HUB_ONLY)


def child_process_env(workdir: Path | None = None, *, device: dict | None = None) -> dict[str, str]:
    """The environment of a backend's child process. On this machine a copy of ours, ``PWD`` the resolved ``workdir``.

    With ``device`` (``process_device``: ``alias``, ``remote_workdir``, ``env``), nothing of ours: the start is an
    empty dict, ``PWD`` is the device's directory text (never resolved here) and the rest is what the device's
    profile and devices.json give (proxies, certificate paths). Names that are the hub's (``HUB_ONLY``) are dropped
    even there; every name that goes is logged, never a value."""
    if device is not None:
        env, dropped = {}, []
        for name, value in sorted((device.get("env") or {}).items()):
            if hub_only(name):
                dropped.append(name)
            else:
                env[name] = str(value)
        if device.get("remote_workdir"):
            env["PWD"] = str(device["remote_workdir"])
        log.info("device %s environment: %s%s", device.get("alias", ""), ", ".join(sorted(env)) or "(none)",
                 "; dropped: " + ", ".join(dropped) if dropped else "")
        return env
    env = os.environ.copy()
    if workdir is not None:
        # subprocess cwd does not update inherited PWD for tools reading env.
        env["PWD"] = str(Path(workdir).resolve())
    return env


def process_device(ctx) -> dict | None:
    """``child_process_env``'s ``device`` for a backend's context: None on this machine, else the device's alias, its
    directory and the environment its profile and devices.json give (core/devices.py ``effective_env``). A device
    that devices.json no longer lists gives an empty environment (the turn is refused before it runs)."""
    alias = getattr(ctx, "device", "")
    if not alias:
        return None
    from covey.core import devices

    env = {}
    try:
        env = devices.effective_env(devices.current().device(alias), devices.read_profile(ctx.state_dir, alias))
    except devices.UnknownDevice:
        pass
    return {"alias": alias, "remote_workdir": getattr(ctx, "remote_workdir", ""), "env": env}


FALSE_VALUES = frozenset({"0", "false", "no", "off"})


def env_flag(name: str, default: bool, environ=None) -> bool:
    """Unset gives the default; 0/false/no/off in any case turn a switch off, any other value on. ``environ`` as in
    ``cenv.get``."""
    raw = cenv.get(name, None, environ)
    return default if raw is None else raw.strip().lower() not in FALSE_VALUES
