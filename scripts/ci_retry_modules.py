#!/usr/bin/env python3
"""The test modules CI runs once more after a failed full run, from the last line of the test ledger.

用法：python3 scripts/ci_retry_modules.py <ledger.jsonl>
Prints the failed modules' paths (tests/test_x.py), space separated, and exits 0; exits 1 with nothing printed
when a retry would not settle the run: the ledger line does not name every failure (scripts/test_map.py keeps
FAILED ids at most and one per test), a name is not a test module of tests/, or more than LIMIT modules failed.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LIMIT = 4  # More failed modules than this is not a slow runner.
MODULE = re.compile(r"(?:^|[ (])(test_\w+)\.")


def modules(entry: dict) -> list[str] | None:
    failed = entry.get("failed") or []
    if not failed or len(failed) != entry.get("failures", 0) + entry.get("errors", 0):
        return None
    found = set()
    for test in failed:
        match = MODULE.search(test)
        if not match or not (REPO / "tests" / f"{match.group(1)}.py").is_file():
            return None
        found.add(f"tests/{match.group(1)}.py")
    return sorted(found) if len(found) <= LIMIT else None


def main(argv: list[str]) -> int:
    lines = [line for line in Path(argv[1]).read_text(encoding="utf-8").splitlines() if line.strip()]
    picked = modules(json.loads(lines[-1])) if lines else None
    if not picked:
        return 1
    print(" ".join(picked))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
