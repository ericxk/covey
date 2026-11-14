"""Resolve service storage independently of the execution workspace.

Path resolution is read-only. Existing installations keep their original store
unless an operator explicitly supplies --state-dir or the backend STATE_DIR.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
import covey.core.env as cenv


REPO_ROOT = Path(__file__).resolve().parents[2]
BIN_DIR = REPO_ROOT / "bin"


def expand_user(text) -> Path | None:
    """``text`` as a path, a leading ``~`` or ``~user`` expanded; None when it cannot be one: pathlib raises
    RuntimeError ("Could not determine home directory") for ``~`` of no user, such as "~$预算表.docx", the name of an
    Office lock file, which a topic's history held in backquotes and which failed every turn of T480 and T481 at
    their start (2026-11-11); a NUL byte is no path either."""
    text = str(text)
    if "\0" in text:
        return None
    try:
        return Path(text).expanduser()
    except RuntimeError:
        return None


def tool_root() -> Path:
    return REPO_ROOT


def entry_script(name: str) -> Path:
    """An entry script in bin/. Package modules are never run as scripts:
    their directory would land on sys.path and shadow the standard library."""
    path = BIN_DIR / name
    if path.parent != BIN_DIR or path.suffix != ".py":
        raise ValueError("entry scripts live directly in bin/")
    return path


def data_home(repo_root: Path | None = None) -> Path:
    root = (repo_root or tool_root()).resolve()
    override = cenv.get("COVEY_DATA_HOME", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    # Stay on the same storage volume as the tool checkout, outside its source.
    return root.with_name(root.name + "-data")


def warn_legacy(path: Path, home: Path | None = None) -> None:
    """One stderr line: ``path``, under a legacy <checkout>/.local-state, was resolved (a default computed early,
    such as an argparse default or a backend's DEFAULT_PYTHON, may still be overridden). The owner archives those
    directories first; the legacy branch goes in a later release (compat_report: local-state)."""
    print(f"warning: legacy .local-state path resolved: {path}; move it under the data home "
          f"({home or data_home()}); a later release no longer reads it", file=sys.stderr, flush=True)


def component_dir(name: str, *, repo_root: Path | None = None, preserve_legacy: bool = True,
                  legacy_root: Path | None = None) -> Path:
    if not name or Path(name).name != name or name in {".", ".."}:
        raise ValueError("storage component must be a directory name")
    root = (repo_root or tool_root()).resolve()
    # Even an explicit DATA_HOME is not permission to orphan existing sessions.
    # Old installed Skill bootstraps used their installation root for state.
    roots = [legacy_root.resolve(), root] if legacy_root is not None else [root]
    # compat(43e99b4): an install that kept its data in <code>/.local-state; retire when harnesses.json names no
    # path there and no checkout that runs Bridge tools has one (compat_report: local-state)
    for candidate in roots if preserve_legacy else ():
        legacy = candidate / ".local-state" / name
        if legacy.exists() or legacy.is_symlink():
            # Resolve the old location without requiring an optional runtime
            # to be available just to parse --help, --stop or an explicit override.
            warn_legacy(legacy, data_home(root))
            return legacy.resolve()
    return data_home(root) / name


def service_root(repo_root: Path | None = None) -> Path:
    """The Manager's root (manifests, manager.json): COVEY_SERVICE_ROOT, else
    <data-home>/bridge-services. Never a legacy .local-state copy."""
    override = cenv.get("COVEY_SERVICE_ROOT")
    return Path(override) if override else component_dir("bridge-services", repo_root=repo_root,
                                                         preserve_legacy=False)


def main() -> None:
    parser = argparse.ArgumentParser(prog="covey_paths.py", description=__doc__)
    parser.add_argument("component", nargs="?",
                        help="e.g. codex-lark-bridge or <name>-runtime; service-root is the Manager's root")
    parser.add_argument("--new", action="store_true", help="show the external path without legacy fallback")
    parser.add_argument("--legacy-root", type=Path, help="previous installed Skill root, if different from the checkout")
    args = parser.parse_args()
    if args.component == "service-root":
        print(service_root())
        return
    print(component_dir(args.component, preserve_legacy=not args.new, legacy_root=args.legacy_root)
          if args.component else data_home())


def codex_home() -> Path:
    """Codex's home: CODEX_HOME, else ~/.codex. Its rollouts (backends/codex/sessions.py) and the login Pi
    borrows (backends/pi/home.py) are under it."""
    home = os.environ.get("CODEX_HOME", "").strip()
    return Path(home).expanduser() if home else Path.home() / ".codex"


if __name__ == "__main__":
    main()
