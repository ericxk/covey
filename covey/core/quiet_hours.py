"""The quiet hours at night, when a card that can wait is held until they are over: a 整理专项 proposal the real-time look
found (app/tidy_proposals.py), a node alert that is not severe (app/node_alerts.py). 夜里攒到早上, docs/usage.md 早晚汇总.

The hours are COVEY_QUIET_HOURS, ``HH:MM-HH:MM`` (``23:00-08:30``, in the 早晚汇总's time zone, Asia/Shanghai); unset,
those of COVEY_DIGEST_TIMES from its last time of the day to its first (``09:30,21:30``: 21:30 to 09:30) when it lists two
times or more; ``off`` or ``0``, or a value that is neither, none: every card at once, as before. The start is in them,
the end is not.

Standard library only."""
from __future__ import annotations

import datetime
import re

from covey.core import env as cenv

ENV = "COVEY_QUIET_HOURS"
DIGEST_ENV = "COVEY_DIGEST_TIMES"
ZONE = datetime.timezone(datetime.timedelta(hours=8))  # As app/digest.py ZONE: Asia/Shanghai, no summer time.
TIME = re.compile(r"([01]?\d|2[0-3]):([0-5]\d)")
OFF = frozenset({"off", "0", "no", "none"})


def _minute(text):
    match = TIME.fullmatch(str(text or "").strip())
    return int(match.group(1)) * 60 + int(match.group(2)) if match else None


def window(environ=None):
    """(start, end) of the quiet hours in minutes after midnight, or None when there are none (see the module)."""
    raw = str(cenv.get(ENV, "", environ) or "").strip()
    if raw:
        if raw.lower() in OFF:
            return None
        start, dash, end = raw.replace("～", "-").replace("~", "-").replace("–", "-").partition("-")
        first, last = _minute(start), _minute(end) if dash else None
        return (first, last) if first is not None and last is not None and first != last else None
    times = sorted({minute for minute in (_minute(part) for part in str(cenv.get(DIGEST_ENV, "", environ) or "").split(","))
                    if minute is not None})
    return (times[-1], times[0]) if len(times) >= 2 else None


def quiet(now, environ=None) -> bool:
    """Whether ``now`` (epoch seconds) is in the quiet hours."""
    hours = window(environ)
    if hours is None:
        return False
    local = datetime.datetime.fromtimestamp(now, ZONE)
    minute, (start, end) = local.hour * 60 + local.minute, hours
    return start <= minute < end if start < end else minute >= start or minute < end


def ends(now, environ=None):
    """When the quiet hours ``now`` is in end (epoch seconds), None when it is not in them."""
    if not quiet(now, environ):
        return None
    end = window(environ)[1]
    local = datetime.datetime.fromtimestamp(now, ZONE)
    at = local.replace(hour=end // 60, minute=end % 60, second=0, microsecond=0)
    return (at if at > local else at + datetime.timedelta(days=1)).timestamp()


def words(now, environ=None) -> str:
    """"到 09:30" for a log line or a record: when the quiet hours ``now`` is in end; "" when it is not in them."""
    at = ends(now, environ)
    return "" if at is None else "到 " + datetime.datetime.fromtimestamp(at, ZONE).strftime("%H:%M")
