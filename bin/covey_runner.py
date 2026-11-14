#!/usr/bin/env python3
"""Runs one bridge job in its own process; the bridge starts it, never a person."""

import sys
from pathlib import Path


def main():
    # First on sys.path, as in covey_bridge.py: the cwd may hold another checkout.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from covey.app.runner import runner_main
    return runner_main()


if __name__ == "__main__":
    raise SystemExit(main())
