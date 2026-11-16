#!/usr/bin/env python3
"""Old entry name, kept for MCP configs an older release wrote and a running session may still hold: it runs covey_butler_mcp.py."""
import runpy
from pathlib import Path

if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).resolve().with_name("covey_butler_mcp.py")), run_name="__main__")
