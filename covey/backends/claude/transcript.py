"""One row of a Claude Code session transcript (a JSONL file), read the way every reader of it does."""
import json


def json_row(line):
    """The row ``line`` (bytes or text) holds as a dict; None for a torn or garbled line and for one that is no object."""
    try:
        row = json.loads(line)
    except ValueError:
        return None
    return row if isinstance(row, dict) else None
