"""When a model limit resets, and what a passing server fault looks like, read off Claude Code's own output
(app/auto_retry.py plans a retry from it; backends/claude/completion.py reads the rows). Standard library only.

- ``row_reset``: the reset of a limit a row says: an assistant row's ``quotaLimits.resetsAt`` (epoch seconds, a
  rejected subscription limit) or a ``rate_limit_event``'s ``rate_limit_info.resetsAt`` when its ``status`` is
  ``rejected`` (a warning's time is the window's end, not a stop).
- ``text_reset``: the same from its words, "resets 4:20pm (Area/City)" or "resets Sep 28, 4pm (UTC)". A time without
  a time zone is never read: it is the local time of a machine this one may not be on. A time of day alone is the
  next one, except on a 5-hour limit's line when tomorrow's is more than five hours off: then the limit has just
  reset and the time is returned as it is (past).
- ``evidence``: the few fields of a row worth keeping to see the real format, no more than ``EVIDENCE_CHARS``.
- ``fault_in``: whether an API error's status or words name a passing server fault: 529/overloaded, 500, 502, 503,
  504, a connection reset, a stream idle timeout.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
from zoneinfo import ZoneInfo

FAULT_STATUSES = frozenset({500, 502, 503, 504, 529})
FAULT_WORDS = re.compile(r"overloaded_error|\bECONNRESET\b|stream idle timeout|connection reset"
                         r"|API Error: (?:500|502|503|504|529)\b", re.I)
EVIDENCE_KEYS = ("type", "subtype", "error", "isApiErrorMessage", "quotaLimits", "rate_limit_info", "api_error_status",
                 "terminal_reason", "is_error")
EVIDENCE_CHARS = 2000
TEXT_CHARS = 400
FIVE_HOURS = 5 * 3600
MONTHS = {name: number for number, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
RESET_WORDS = re.compile(
    r"\bresets (?:at )?(?:(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]* (\d{1,2})(?:, (\d{4}))?(?:,| at) )?"
    r"(\d{1,2})(?::(\d{2}))?\s?([ap]m) \(([A-Za-z]+(?:/[A-Za-z0-9_+\-]+)*)\)", re.I)
FIVE_HOUR_WORDS = re.compile(r"\b(?:5-hour|session) limit\b", re.I)


def _epoch(value):
    """Epoch seconds of a number (milliseconds past 1e12) or an ISO time; None otherwise."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value == value and value > 0:
        return float(value) / 1000 if value > 1e12 else float(value)
    if isinstance(value, str) and value.strip():
        try:
            when = _dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return when.timestamp() if when.tzinfo else None
    return None


def row_reset(row):
    """The reset of the limit ``row`` (a stream row) says, in epoch seconds; None when it names none."""
    if not isinstance(row, dict):
        return None
    if row.get("type") == "rate_limit_event":
        info = row.get("rate_limit_info")
        return _epoch(info.get("resetsAt")) if isinstance(info, dict) and info.get("status") == "rejected" else None
    quota = row.get("quotaLimits")
    return _epoch(quota.get("resetsAt")) if isinstance(quota, dict) and quota.get("status") in (None, "rejected") else None


def row_text(row):
    """The words of an assistant row (its text blocks) or a result row, at most TEXT_CHARS."""
    message = row.get("message") if isinstance(row.get("message"), dict) else {}
    content = message.get("content")
    parts = [b.get("text") for b in content if isinstance(b, dict) and isinstance(b.get("text"), str)] \
        if isinstance(content, list) else []
    text = "\n".join(parts) or (row.get("result") if isinstance(row.get("result"), str) else "")
    return text[:TEXT_CHARS * 4]


def text_reset(text, now):
    """The reset ``text`` speaks of (see the module docstring), in epoch seconds after ``now`` (the time the text was
    written); None when the words name no time with a zone."""
    found = RESET_WORDS.search(text or "")
    if found is None:
        return None
    month, day, year, hour, minute, meridiem, name = found.groups()
    if name == "Local":
        return None
    try:
        zone = ZoneInfo(name)
    except (ValueError, OSError, LookupError):
        return None
    hour, minute = int(hour), int(minute or 0)
    if not 1 <= hour <= 12 or minute > 59:
        return None
    hour = hour % 12 + (12 if meridiem.lower() == "pm" else 0)
    here = _dt.datetime.fromtimestamp(now, zone)
    try:
        if not month:
            at = here.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if at.timestamp() <= now - 60:
                tomorrow = (here + _dt.timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)
                before = text[:found.start()].rsplit("\n", 1)[-1]
                if tomorrow.timestamp() <= now + FIVE_HOURS or not FIVE_HOUR_WORDS.search(before):
                    at = tomorrow
            return at.timestamp()
        at = _dt.datetime(int(year or here.year), MONTHS[month.lower()], int(day), hour, minute, tzinfo=zone)
        if not year and at.timestamp() < now - 3600:
            at = at.replace(year=at.year + 1)
        return at.timestamp()
    except ValueError:
        return None


def evidence(row):
    """The fields of ``row`` worth keeping to see the real format of a limit or a fault, with its words cut short;
    {} for no dict. Never the whole row: it can carry the model's text and tool results."""
    if not isinstance(row, dict):
        return {}
    kept = {key: row[key] for key in EVIDENCE_KEYS if key in row}
    text = row_text(row)
    if text:
        kept["text"] = text[:TEXT_CHARS]
    if len(json.dumps(kept, ensure_ascii=False, default=str)) > EVIDENCE_CHARS:
        kept = {key: kept[key] for key in ("type", "error", "api_error_status") if key in kept}
    return kept


def fault_in(status, text):
    """Whether an API error with this HTTP ``status`` (or None) and these words is a passing server fault."""
    return status in FAULT_STATUSES or bool(FAULT_WORDS.search(str(text or "")))
