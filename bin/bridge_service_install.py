#!/usr/bin/env python3
"""Old entry name, kept for service manifests, boot hooks, crontabs and scripts that name it: it runs covey_service_install.py."""
import runpy
from pathlib import Path

if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).resolve().with_name("covey_service_install.py")), run_name="__main__")
