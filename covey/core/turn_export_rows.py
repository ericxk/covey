"""The rows of a turn export (下载请求与结果, backends/claude/export.py and backends/codex/export.py): Claude Code's
transcripts and Codex's rollouts are both JSONL whose rows carry an ISO ``timestamp``."""
from __future__ import annotations

import json
from pathlib import Path

from covey.core.state import epoch_seconds

MARGIN = 5  # Seconds around the turn: its first request starts just after the process does.


def read_rows(path):
    rows = []
    with Path(path).open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except ValueError:
                row = {"unparsed": line.rstrip("\n")}
            rows.append(row if isinstance(row, dict) else {"unparsed": line.rstrip("\n")})
    return rows


def stamp(row):
    """A row's timestamp in epoch seconds, or None. Any JSON value goes through str() first, as it always has, so a
    number such as 20261105 reads as a date."""
    return epoch_seconds(str(row.get("timestamp")))


def in_window(when, since, until):
    """Whether epoch seconds ``when`` (None: no time) fall in the turn from ``since`` to ``until``, MARGIN around."""
    return when is not None and since - MARGIN <= when <= until + MARGIN


def window_rows(rows, since, until):
    """The rows from the first to the last one stamped in the window, every row between them as written."""
    inside = [i for i, row in enumerate(rows) if in_window(stamp(row), since, until)]
    return rows[inside[0]:inside[-1] + 1] if inside else []
