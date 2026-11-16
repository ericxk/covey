"""Optional extension packages: which are enabled, where they are found, the data and features they add.

An extension is a Python package outside covey/. Its ``manifest`` module is plain data (dict, list and str
only, no import of covey): ``HARNESSES`` (name -> the fields of core/roster.py's RosterEntry),
``PROVIDERS`` (a list of dicts with the fields of core/providers.py's Provider), ``RETIRED_PREFIXES`` and
``RETIRED_HARNESSES``, and the small tables the core reads for them: ``WORK_MODES`` (mode id -> [label, hint]) with
``DEFAULT_WORK_MODE`` (core/defaults.py), ``AGENT_DIRS`` (hidden directories an agent keeps in a workspace,
app/artifacts.py), ``IGNORED_ENV`` (variable -> [Harness, option], core/ignored_env.py) and ``GATEWAYS`` (name -> the
fields of core/gateways.py's Gateway: model gateways for Codex, Pi and dsh); each may be left out.
Two more tables hold what only some deployments have: ``HOOKS`` (hook name -> the dotted name of a module of the
package, imported when the core asks for it: ``hook``; the names are the ``HOOK_NAMES`` below) and ``SETTINGS``
(name -> str, the ``SETTING_NAMES`` and the package's own ``<package>.<name>``: ``setting``). With no extension enabled,
``hook`` is None and ``setting`` the default the caller gives. ``CARD_KINDS`` (card button kind -> label) names the
button kinds the package's cards carry: the Bridge accepts them (app/card_actions.py) and the package's features claim
the clicks. Its optional ``features`` module has ``features(app)``, returning the
feature objects app/unified.py appends to its own; a feature object may also have ``handle_event(ctx, event, owner)``
(a top-level message it takes before the butler and topic routing: True when taken), ``claim(ctx, event, raw,
owner)`` (a card click before the core's controllers: the outcome, or None when it is not its own) and ``close()``
(when the Bridge stops), besides ``threads()`` and ``menu_items()``. The core never names an extension
(docs/architecture.md, 扩展); the one table that does is ``MOVED_ENV``, for variables of features that moved out.
Standard library only: core/roster.py imports this and an extension's isolated interpreter imports roster.
"""

from __future__ import annotations

import importlib
import os
import re
import sys
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

import covey.core.env as cenv

ENABLED_ENV = "COVEY_EXTENSIONS"  # Comma-separated package names; set to '' for none.
PATH_ENV = "COVEY_EXTENSION_PATH"  # os.pathsep-separated directories the packages are found in.
ROOT = Path(__file__).resolve().parents[2]
ENABLED_FILE = "extensions/enabled"  # Under the repository root: one package per line, '#' comments.
DEFAULT_DIR = "extensions"
PACKAGE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*")
HARNESS_FIELDS = {"module", "store", "store_module", "live_questions", "paused_questions", "after"}
# The hooks the core calls: what a module must have is in the file that calls it.
HOOK_NAMES = {
    "proxy_sender": "app/proxy.py: the 代答 sender (ProxySend, ProxyLogin and the helpers app/proxy_send.py names)",
    "dashboard_entry": "app/dashboard.py: the dashboard's fixed entry (alias, url, publish, REPUBLISH_AFTER)",
    "service_hosting": "service/install.py: other ways to host the Manager (MODES: name -> install(root, args); optional auto_mode() -> a mode name when it applies, for --mode auto)",
}
SETTING_NAMES = {
    "web_tenant_domain": "feishu/web_client.py: the Feishu tenant's host name, for the web login",
    "contact_notes": "app/artifacts.py: a regex of the paths the 代答 contacts' notes are kept in",
    "contact_notes_hint": "core/proxy.py: what the 代答 prompt says about creating a missing notes directory",
    "contact_notes_dir": "app/proxy_commands.py: the notes directory under a workdir, with {task} for the task id",
}
# compat: variables of features that moved into an extension -> its package; set without the package enabled, they are
# ignored and the Bridge logs one line for each (``unused_env``).
MOVED_ENV = {"COVEY_PASEO_FLEET": "covey_paseo"}
PROVIDER_FIELDS = {"key", "harness", "name", "prefix", "access", "menu"}


class ExtensionError(RuntimeError):
    """An enabled extension that cannot be loaded or collides with the core or another extension."""

    def __init__(self, package: str, message: str):
        super().__init__(f"extension {package!r}: {message}")
        self.package = package


@dataclass(frozen=True)
class Extension:
    package: str
    harnesses: dict
    providers: tuple
    retired_prefixes: dict
    retired_harnesses: dict
    work_modes: dict = field(default_factory=dict)  # mode id -> (label, hint)
    default_work_mode: str = ""
    agent_dirs: tuple = ()
    ignored_env: dict = field(default_factory=dict)  # variable -> (Harness, option or (when on, when off))
    hooks: dict = field(default_factory=dict)  # hook name -> dotted module name
    settings: dict = field(default_factory=dict)  # setting name -> str
    gateways: dict = field(default_factory=dict)  # name -> the fields of core/gateways.py's Gateway
    card_kinds: dict = field(default_factory=dict)  # card button kind -> label


def enabled(environ=None, root: Path = ROOT) -> list[str]:
    """The enabled package names, in order, without repeats: ``COVEY_EXTENSIONS`` when set (even to ''),
    else the lines of ``extensions/enabled`` under ``root`` (none when the file is missing)."""
    environ = os.environ if environ is None else environ
    if cenv.has(ENABLED_ENV, environ):
        names = cenv.get(ENABLED_ENV, "", environ).split(",")
    else:
        try:
            lines = (root / ENABLED_FILE).read_text().splitlines()
        except FileNotFoundError:
            return []
        names = [line.split("#", 1)[0] for line in lines]
    found = []
    for name in (n.strip() for n in names):
        if not name:
            continue
        if not PACKAGE_NAME.fullmatch(name):
            raise ExtensionError(name, "not a Python package name")
        if name not in found:
            found.append(name)
    return found


def search_path(environ=None, root: Path = ROOT) -> list[str]:
    """The directories the packages are found in: ``COVEY_EXTENSION_PATH``, else ``extensions/`` under ``root``."""
    environ = os.environ if environ is None else environ
    if cenv.has(PATH_ENV, environ):
        return [part for part in cenv.get(PATH_ENV, "", environ).split(os.pathsep) if part]
    return [str(root / DEFAULT_DIR)]


def package_dirs(environ=None, root: Path = ROOT) -> list[Path]:
    """The directories of the enabled packages that are found on the search path (the tests and tools find an
    extension's own files by them), in the enabled order."""
    found = []
    for package in enabled(environ, root):
        for directory in search_path(environ, root):
            candidate = Path(directory).joinpath(*package.split("."))
            if candidate.is_dir():
                found.append(candidate)
                break
    return found


def prepare_path(directories) -> None:
    """Put ``directories`` in front of sys.path, in their order, each once."""
    for directory in reversed(list(directories)):
        if directory not in sys.path:
            sys.path.insert(0, directory)


def _import(package: str, module: str):
    name = f"{package}.{module}"
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as error:
        if error.name == name:
            raise
        raise ExtensionError(package, f"cannot import {name}: {error}") from error
    except Exception as error:
        raise ExtensionError(package, f"cannot import {name}: {type(error).__name__}: {error}") from error


def _mapping(package: str, data, field: str) -> dict:
    if not isinstance(data, dict) or not all(isinstance(k, str) for k in data):
        raise ExtensionError(package, f"manifest {field} must be a dict")
    return dict(data)


def _fields(package: str, entry, allowed: set, what: str) -> dict:
    if not isinstance(entry, dict) or set(entry) - allowed:
        raise ExtensionError(package, f"{what} must be a dict with the fields {sorted(allowed)}, got {entry!r}")
    return dict(entry)


def read(package: str) -> Extension:
    """``package``'s manifest, checked for shape (not yet for collisions: core/roster.py, core/providers.py)."""
    try:
        manifest = _import(package, "manifest")
    except ModuleNotFoundError as error:
        raise ExtensionError(package, f"has no manifest module ({error})") from error
    harnesses = _mapping(package, getattr(manifest, "HARNESSES", {}), "HARNESSES")
    for name, fields in harnesses.items():
        _fields(package, fields, HARNESS_FIELDS, f"HARNESSES[{name!r}]")
        if not isinstance(fields.get("module"), str) or not isinstance(fields.get("store"), str):
            raise ExtensionError(package, f"HARNESSES[{name!r}] needs the strings module and store")
    providers = getattr(manifest, "PROVIDERS", [])
    if not isinstance(providers, (list, tuple)):
        raise ExtensionError(package, "manifest PROVIDERS must be a list")
    providers = tuple(_fields(package, p, PROVIDER_FIELDS, "a PROVIDERS item") for p in providers)
    for provider in providers:
        if not all(isinstance(provider.get(k), str) for k in ("key", "harness", "name", "prefix", "access")):
            raise ExtensionError(package, f"a PROVIDERS item needs the strings key, harness, name, prefix, "
                                          f"access, got {provider!r}")
    modes = _mapping(package, getattr(manifest, "WORK_MODES", {}), "WORK_MODES")
    if not all(isinstance(v, (list, tuple)) and len(v) == 2 and all(isinstance(t, str) for t in v)
               for v in modes.values()):
        raise ExtensionError(package, "manifest WORK_MODES must map a mode id to [label, hint]")
    default_mode = getattr(manifest, "DEFAULT_WORK_MODE", "")
    agent_dirs = getattr(manifest, "AGENT_DIRS", ())
    if not isinstance(default_mode, str) or (default_mode and default_mode not in modes):
        raise ExtensionError(package, "manifest DEFAULT_WORK_MODE must be one of WORK_MODES")
    if not isinstance(agent_dirs, (list, tuple)) or not all(isinstance(d, str) for d in agent_dirs):
        raise ExtensionError(package, "manifest AGENT_DIRS must be a list of names")
    ignored = _mapping(package, getattr(manifest, "IGNORED_ENV", {}), "IGNORED_ENV")
    if not all(isinstance(v, (list, tuple)) and len(v) == 2 for v in ignored.values()):
        raise ExtensionError(package, "manifest IGNORED_ENV must map a variable to [Harness, option]")
    hooks = _mapping(package, getattr(manifest, "HOOKS", {}), "HOOKS")
    settings = _mapping(package, getattr(manifest, "SETTINGS", {}), "SETTINGS")
    if set(hooks) - set(HOOK_NAMES) or not all(isinstance(v, str) for v in hooks.values()):
        raise ExtensionError(package, f"manifest HOOKS must map names of {sorted(HOOK_NAMES)} to module names")
    if any(name not in SETTING_NAMES and not name.startswith(package + ".") for name in settings) or not all(isinstance(v, str) for v in settings.values()):
        raise ExtensionError(package, f"manifest SETTINGS must map names of {sorted(SETTING_NAMES)} "
                                      f"or '{package}.<name>' to strings")
    kinds = _mapping(package, getattr(manifest, "CARD_KINDS", {}), "CARD_KINDS")
    if not all(isinstance(v, str) for v in kinds.values()):
        raise ExtensionError(package, "manifest CARD_KINDS must map a button kind to its label")
    return Extension(package, harnesses, providers,
                     _mapping(package, getattr(manifest, "RETIRED_PREFIXES", {}), "RETIRED_PREFIXES"),
                     _mapping(package, getattr(manifest, "RETIRED_HARNESSES", {}), "RETIRED_HARNESSES"),
                     {k: tuple(v) for k, v in modes.items()}, default_mode, tuple(agent_dirs),
                     {k: (v[0], tuple(v[1]) if isinstance(v[1], (list, tuple)) else v[1]) for k, v in ignored.items()},
                     hooks, settings, _mapping(package, getattr(manifest, "GATEWAYS", {}), "GATEWAYS"), kinds)


def load(environ=None, root: Path = ROOT) -> tuple[Extension, ...]:
    """The manifests of the enabled packages (uncached: ``manifests``). Importing one that is enabled but cannot be
    imported raises ExtensionError, so a Bridge with a broken extension does not start."""
    packages = enabled(environ, root)
    if not packages:
        return ()
    prepare_path(search_path(environ, root))
    return tuple(read(package) for package in packages)


@cache
def manifests() -> tuple[Extension, ...]:
    return load()


def features(app) -> list:
    """The feature objects of the enabled packages that have a ``features`` module: ``features(app)`` of each."""
    found = []
    for extension in manifests():
        try:
            module = _import(extension.package, "features")
        except ModuleNotFoundError as error:
            if error.name == f"{extension.package}.features":
                continue
            raise
        try:
            found += list(module.features(app))
        except Exception as error:
            raise ExtensionError(extension.package, f"features(app) failed: {type(error).__name__}: {error}") from error
    return found


def work_modes() -> tuple[dict, str]:
    """(mode id -> (label, hint), the default mode id) of the enabled packages, the first default winning."""
    modes, default = {}, ""
    for extension in manifests():
        modes.update(extension.work_modes)
        default = default or extension.default_work_mode
    return modes, default


def agent_dirs() -> frozenset:
    """The hidden directory names the enabled packages' agents keep in a workspace."""
    return frozenset(name for extension in manifests() for name in extension.agent_dirs)


def ignored_env() -> dict:
    """The enabled packages' ignored variables, merged into core/ignored_env.py's table."""
    return {name: value for extension in manifests() for name, value in extension.ignored_env.items()}


def card_kinds() -> dict:
    """The enabled packages' card button kinds (kind -> label), added to app/card_actions.py's table."""
    return {kind: label for extension in manifests() for kind, label in extension.card_kinds.items()}


def unused_env(environ=None) -> list[str]:
    """One line for each ``MOVED_ENV`` variable that is set while its package is not enabled."""
    environ = os.environ if environ is None else environ
    on = {extension.package for extension in manifests()}
    return [f"{name} is set but the {package} extension is not enabled: ignored (enable it with {ENABLED_ENV}, "
            f"the extension's README)" for name, package in MOVED_ENV.items()
            if package not in on and cenv.get(name, "", environ).strip()]


def hook(name: str):
    """The module of hook ``name`` from the first enabled package that has it, or None."""
    if name not in HOOK_NAMES:
        raise KeyError(name)
    for extension in manifests():
        if name in extension.hooks:
            module = extension.hooks[name]
            try:
                return importlib.import_module(module)
            except Exception as error:
                raise ExtensionError(extension.package, f"cannot import hook {name} ({module}): "
                                                        f"{type(error).__name__}: {error}") from error
    return None


def setting(name: str, default: str = "") -> str:
    """The value of setting ``name`` from the first enabled package that sets it, else ``default``."""
    if name not in SETTING_NAMES and "." not in name:
        raise KeyError(name)
    for extension in manifests():
        if name in extension.settings:
            return extension.settings[name]
    return default
