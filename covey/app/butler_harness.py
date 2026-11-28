"""Which Harness the butler runs on (docs/butler.md 管家用哪个 Harness), and with it a project's lead (app/projects.py
step_lead, app/project_loop.py): the one the settings name (core/butler_settings.py Settings.harness) when they name one;
else Claude Code (core/butler.py HARNESS) when it is enabled here and takes the Bridge's tools (core/harness.py
bridge_tools), as before; else the first Harness in the roster's order (core/roster.py NAMES) that is enabled and takes them
(Codex, then Antigravity). A Harness the settings name that is not enabled or cannot take the tools is not replaced: the
owner chose it, and the start says so (app/butler.py harness_note).

``drawn`` is the settings as the cards draw them: an empty ``harness`` shows the one the butler runs on now. It is never
saved, so a deployment that names none follows what is enabled (Claude Code installed later takes over again).

A model and budget saved with no Harness named are Claude Code's (the 设置 card saves Claude Code as '', and a model
picked for another Harness saves that Harness with it, app/butler_panel.py ``changes``): on another Harness the butler runs
on its own default model instead (``model_of``)."""
from __future__ import annotations

import dataclasses
from pathlib import Path

from covey.core import butler as contract
from covey.core import butler_settings, devices
from covey.core.harness import backend_capabilities
from covey.core.roster import NAMES
from covey.core.workspaces import load_config


def takes_tools(app) -> bool:
    """Whether ``app`` (a Harness app of the Bridge, None for one not enabled) can run the butler: it takes the Bridge's
    tools on this machine."""
    harness = getattr(app, "harness", None)
    if app is None or harness is None:
        return False
    return bool(backend_capabilities(app.backend, harness.context).bridge_tools)


# The Harnesses a butler with none named falls back to after Claude Code, in this order, before the roster's others
# (DeepSeek Harness takes the Bridge's tools too since T700, but a deployment that had Codex or Antigravity keeps it).
FALLBACKS = ("codex", "agy")


def default_name(apps) -> str:
    """The Harness the butler runs on when its settings name none: Claude Code when it can, else Codex, then
    Antigravity (FALLBACKS), then the first Harness of the roster's order (then any other enabled one) that can; Claude
    Code when none can (the start says it is missing)."""
    if takes_tools(apps.get(contract.HARNESS)):
        return contract.HARNESS
    order = [*FALLBACKS, *NAMES, *(name for name in apps if name not in NAMES)]
    return next((name for name in order if name != contract.HARNESS and takes_tools(apps.get(name))), contract.HARNESS)


def name_of(settings, apps) -> str:
    """The butler's Harness under ``settings`` (core/butler_settings.py Settings): theirs, or ``default_name``."""
    return settings.harness or default_name(apps)


def chosen(state_dir, apps) -> tuple:
    """(name, app) of the Harness the butler runs on now: the app None when that Harness is not enabled here or cannot
    take the Bridge's tools."""
    name = name_of(butler_settings.load(state_dir), apps)
    app = apps.get(name)
    return name, (app if takes_tools(app) else None)


def model_of(settings, name) -> tuple:
    """(model, budget) the butler runs with on the Harness ``name``: the settings', but for a fallback's (no Harness named,
    ``name`` not Claude Code: what they say is Claude Code's), its own default ('', '')."""
    if not settings.harness and name != contract.HARNESS:
        return "", ""
    return settings.model, settings.effort


def drawn(settings, apps):
    """``settings`` with an empty ``harness`` as the one the butler runs on now (``default_name``), and its model as that
    Harness runs it (``model_of``): what the 设置 card and the web page draw and compute a model choice with. Never saved:
    a change saves the fields it changed alone (and the Harness with a model, app/butler_panel.py ``changes``)."""
    if settings.harness:
        return settings
    name = default_name(apps)
    model, effort = model_of(settings, name)
    return dataclasses.replace(settings, harness=name, model=model, effort=effort)


def workspace_of(state_dir, default_workdir, settings) -> tuple:
    """(name, directory) of where a butler (or a lead) that has not run yet starts: the workspace an older release's
    settings saved, while it is listed still; else the home, the service's ``--workdir`` (``default_workdir``,
    docs/configuration.md 主目录与仓库地图), under the name the workspace list gives that directory (its own name when the
    list has none). One that runs already keeps its directory (app/butler.py keep_topic)."""
    config = load_config(state_dir, default_workdir)
    if settings.workspace in config["workspaces"] and not devices.is_device_value(config["workspaces"][settings.workspace]):
        return settings.workspace, str(Path(config["workspaces"][settings.workspace]).expanduser().resolve())
    home = Path(default_workdir).expanduser().resolve()
    name = next((space for space, where in config["workspaces"].items()
                 if not devices.is_device_value(where) and Path(where).expanduser().resolve() == home), home.name)
    return name, str(home)


def unwritten_topic(state_dir, apps) -> dict:
    """What the butler's topic.json says before its first message writes it (app/butler.py keep_topic): the Harness it runs
    on (``chosen``), the settings' model and budget, and their workspace. What a project's lead starts from where the
    butler has not run yet (app/projects.py step_lead). {} when no enabled Harness can run it."""
    name, app = chosen(state_dir, apps)
    if app is None:
        return {}
    settings = butler_settings.load(state_dir)
    space, workdir = workspace_of(state_dir, app.harness.context.default_workdir, settings)
    model, effort = model_of(settings, name)
    data = {"harness_id": name, "model": model, "effort": effort, "workspace_name": space, "workdir": workdir}
    return data
