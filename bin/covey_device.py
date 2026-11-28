#!/usr/bin/env python3
"""Stable entry path; the implementation is covey.core.device_cli."""
import runpy
import sys
from pathlib import Path

if __name__ == "__main__":
    # First on sys.path: an empty PYTHONPATH entry puts the cwd ahead of anything
    # appended, and the cwd may hold another checkout of this package.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    runpy.run_module("covey.core.device_cli", run_name="__main__", alter_sys=True)
