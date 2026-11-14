"""Where the headless browser lives and how it starts (shared by ./install, feishu/install_browser.py, app_console.py and
web_client.py). ./install puts Chromium under ``<data home>/browsers`` and writes ``launch.json`` there when the browser
needs more than its download: ``executable`` (a Chrome or Chromium the machine already has) and ``libs`` (directories of
system libraries unpacked without root, for LD_LIBRARY_PATH of the browser process only). ``apply()`` turns that into the
environment of the browser process. Standard library only."""
from __future__ import annotations

import json
import os
from pathlib import Path

from covey.core import env as cenv

LAUNCH_FILE = "launch.json"
EXECUTABLE = "COVEY_BROWSER_EXECUTABLE"


def browsers_dir(environ=None):
    environ = os.environ if environ is None else environ
    if environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        return Path(environ["PLAYWRIGHT_BROWSERS_PATH"])
    from covey.core.paths import data_home

    here = data_home() / "browsers"
    return here if here.is_dir() else Path.home() / ".cache/ms-playwright"


def read(folder):
    try:
        row = json.loads((Path(folder) / LAUNCH_FILE).read_text())
    except (OSError, ValueError):
        return {}
    return row if isinstance(row, dict) else {}


def apply(environ=None):
    """Set PLAYWRIGHT_BROWSERS_PATH, the executable and LD_LIBRARY_PATH in ``environ`` (os.environ) from launch.json."""
    environ = os.environ if environ is None else environ
    folder = browsers_dir(environ)
    environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(folder))
    row = read(folder)
    if row.get("executable") and Path(row["executable"]).is_file():
        if not cenv.has(EXECUTABLE, environ):
            environ[EXECUTABLE] = row["executable"]
    libs = [d for d in row.get("libs") or [] if Path(d).is_dir()]
    if libs:
        old = [p for p in environ.get("LD_LIBRARY_PATH", "").split(os.pathsep) if p]
        environ["LD_LIBRARY_PATH"] = os.pathsep.join(dict.fromkeys(libs + old))
    return folder
