#!/usr/bin/env python3
"""Unified launcher; each topic selects its Harness on the launch card."""

import argparse
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--harness", required=True, choices=["unified"])
    _, remaining = parser.parse_known_args()
    # The target parser receives only its own arguments; task runners rebuild the app from them.
    # First on sys.path: an empty PYTHONPATH entry puts the cwd ahead of anything
    # appended, and the cwd may hold another checkout of this package.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from covey.app.unified import UnifiedBridgeApp
    return UnifiedBridgeApp(script_path=Path(__file__)).main(remaining)


if __name__ == "__main__":
    raise SystemExit(main())
