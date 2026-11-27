#!/usr/bin/env python3
"""Stable entry path; the implementation is covey.backends.claude.tool_guard (a PreToolUse hook, and the search shims' check)."""
import sys

if __name__ == "__main__" and sys.argv[1:3] == ["--mode", "shim"]:
    # A search shim runs this before every ``rg``: load the one file, not the package, runpy and pathlib.
    try:
        import os
        from importlib.machinery import SourceFileLoader
        root = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
        module = SourceFileLoader("covey_tool_guard_shim", os.path.join(root, "covey", "backends", "claude", "tool_guard.py")).load_module()
        code = module.main(sys.argv[1:])
    except Exception:  # noqa: BLE001 - a fault in the shim's check lets the command run
        code = 0
    sys.exit(code)

import runpy  # noqa: E402
from pathlib import Path  # noqa: E402

if __name__ == "__main__":
    # First on sys.path: an empty PYTHONPATH entry puts the cwd ahead of anything
    # appended, and the cwd may hold another checkout of this package.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    runpy.run_module("covey.backends.claude.tool_guard", run_name="__main__", alter_sys=True)
