#!/usr/bin/env python3
"""core/look.py as the web page reads it: look.json (docs/ui.md 怎么开工), for the web workbench's tokens.

    python3 -B scripts/export_look.py              # print it
    python3 -B scripts/export_look.py --out FILE   # write it to FILE

The web page keeps a copy; its tokens are generated from this file, so a word or a colour changed in core/look.py is
changed on the page by exporting again."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from covey.core import look  # noqa: E402


def text() -> str:
    return json.dumps(look.export(), ensure_ascii=False, indent=2) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", help="write look.json here instead of printing it")
    args = parser.parse_args(argv)
    if args.out:
        Path(args.out).write_text(text(), encoding="utf-8")
    else:
        sys.stdout.write(text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
