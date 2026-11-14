"""Numeric first-output timing; never retain streamed model content."""
from covey.core.usage import number

# compat: the extension backend's source name below is what it wrote into saved records and what its task processes still send.
SOURCES = {"codex_delta", "codex_item", "maso_native", "claude_delta", "pi_delta"}  # privacy: allow 旧记录里扩展后端写的来源名（规则 10）


def elapsed(start, first, end=None):
    if not number(start) or not number(first) or first < start:
        return None
    if end is not None and (not number(end) or first > end):
        return None
    return first - start


def codex_marker(row):
    if row.get("method") not in {"item/agentMessage/delta", "item/reasoning/textDelta", "item/reasoning/summaryTextDelta"}:
        return None
    p = row.get("params")
    if not isinstance(p, dict) or not isinstance(p.get("delta"), str) or not p["delta"]:
        return None
    return {"method": "bridge/modelToken", "params": {k:p.get(k) for k in ("threadId", "turnId")}}


class FirstToken:
    """One turn's TTFT, given once: its first model output timed from the request's send, and none when a
    tool call came first (that would count the tool's run). ``emit(kind, data)`` hears the first_token event;
    ``source`` is its ttft_source (SOURCES)."""

    def __init__(self, emit, source):
        self.emit, self.source, self.done = emit, source, False

    def skip(self):
        """No TTFT this turn: a tool call came first."""
        self.done = True

    def output(self, start, now, source=None):
        """Model output at ``now``: the first one gives the TTFT. One that cannot be timed (``start`` unknown
        or after ``now``) leaves it to a later output."""
        if self.done:
            return
        delay = elapsed(start, now)
        if delay is not None:
            self.done = True
            self.emit("first_token", {"ttft_seconds": delay, "ttft_source": source or self.source})
