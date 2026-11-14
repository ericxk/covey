"""Validate metadata-only policy observations and bind them to a single task interval."""
import hashlib
import math
import re

from covey.core.model_policy import valid_policy_event
from covey.core.state import epoch_seconds


def stamp(value):
    return epoch_seconds(value, aware=True)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()[:24] if isinstance(value, str) and value else ''


def valid_observation(row):
    return (isinstance(row, dict) and set(row) == {'id', 'session', 'request_time', 'time', 'event'}
            and isinstance(row['id'], str) and re.fullmatch(r'[a-zA-Z0-9_.:-]{1,160}', row['id'])
            and isinstance(row['session'], str) and re.fullmatch(r'[a-f0-9]{24}', row['session'])
            and stamp(row['request_time']) is not None and stamp(row['time']) is not None
            and stamp(row['time']) >= stamp(row['request_time'])
            and valid_policy_event(row['event']))


def match_task(row, records, now):
    """No time-nearest matching, no native-VPS-session -> client-session guessing.

    A resumed session can belong to several task intervals: only the currently
    running interval that contains this request is eligible. Ambiguity is retained.
    """
    if not valid_observation(row) or stamp(row['time']) > now + 30:
        return None, 'invalid_or_future_event'
    candidates = []
    for record in records:
        if record.get('backend') != 'claude' or record.get('status') not in {'running', 'cancel_requested'}:
            continue
        if digest(record.get('session_id')) != row['session']:
            continue
        timings = record.get('timings') or {}
        started = timings.get('backend_started') or timings.get('backend_requested') or timings.get('dispatched')
        if not isinstance(started, (int, float)) or not math.isfinite(started):
            started = stamp(record.get('created_at'))
        if started is not None and started <= stamp(row['request_time']) <= now + 30:
            candidates.append(record)
    if len(candidates) != 1:
        return None, 'ambiguous_task' if candidates else 'no_active_task_interval'
    return candidates[0], 'exact_session_and_interval'
