#!/usr/bin/env python3
"""Write the .pyc files of a release directory, so no process the service starts compiles Covey again.

    PYTHON -B -I scripts/precompile_release.py RELEASE_DIR

The service runs every Python process with -B and PYTHONDONTWRITEBYTECODE=1 (Bridge, task runners, the butler's
MCP server), so a release without __pycache__ is compiled from source again at each start (about 0.4 s of
`import covey.app.runner`). Run this with the interpreter the service runs (a .pyc carries the interpreter's version
tag; another version just ignores it) after the release is complete: scripts/deploy_bridge.sh does it for every
release it bootstraps from, assemble_release.py --precompile PYTHON after assembling.

Compiles covey/, extensions/ (without tests/ and dev/) and harness_bridge.py. Files whose .pyc is up to date are
skipped, so a second run is fast. Never fails the caller: a compile error is a warning on stderr and ``"ok": false``
in the JSON line printed last, the exit code is 0 (bad arguments: 2). Standard library only.
"""
from __future__ import annotations

import compileall
import json
import os
import sys
import time
from pathlib import Path

SKIP_DIRS = {"tests", "dev", "__pycache__"}  # Not imported by the service. Matched below the release only: it may sit in /dev/shm.


def precompile(release: Path) -> dict:
    """Compile the release's code in place: {"ok", "python", "seconds", "failed": [...]}."""
    started = time.monotonic()
    release = release.resolve()
    failed: list[str] = []
    for name in ("covey", "extensions"):
        bad = 0
        for folder, dirs, files in os.walk(release / name):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            for file in sorted(files):
                if file.endswith(".py"):
                    try:
                        bad += not compileall.compile_file(os.path.join(folder, file), quiet=1)
                    except Exception as exc:  # Never the caller's problem: a release without pyc is only slower.
                        bad += 1
                        failed.append(f"{file}: {exc}")
        if bad:
            failed.append(name)
    shim = release / "harness_bridge.py"
    try:
        if shim.is_file() and not compileall.compile_file(str(shim), quiet=1):
            failed.append(shim.name)
    except Exception as exc:
        failed.append(f"{shim.name}: {exc}")
    return {"ok": not failed, "python": sys.executable, "version": sys.version.split()[0],
            "seconds": round(time.monotonic() - started, 2), "failed": failed}


def main(argv: list[str]) -> int:
    if len(argv) != 1 or not Path(argv[0]).is_dir():
        print("usage: precompile_release.py RELEASE_DIR", file=sys.stderr)
        return 2
    info = precompile(Path(argv[0]))
    if not info["ok"]:
        print(f"precompile_release: warning, not compiled: {info['failed']}", file=sys.stderr)
    print(json.dumps(info, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
