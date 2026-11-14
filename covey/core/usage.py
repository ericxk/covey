"""Numeric-only output throughput; never infer token counts from text."""
import copy
import math
import time

# A subtask_usage report is kept whole in the process record, so a busy subtask must not
# rewrite it on every request: at most one report per this many seconds, the last one forced.
SUBTASK_REPORT_SECONDS = 10.0


def number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def rate(tokens, seconds):
    if type(tokens) is not int or tokens < 0 or not number(seconds) or seconds <= 0:
        return None
    try:
        value = tokens / seconds
    except OverflowError:
        return None
    return value if math.isfinite(value) else None


class ActiveGenerationClock:
    """Observed root-turn time excluding the union of tools and user waits."""
    def __init__(self, start=None):
        self.last = time.time() if start is None else start
        self.seconds = 0.0
        self.blocked = set()

    def sample(self, now=None):
        now = time.time() if now is None else now
        now = max(self.last, now) if number(now) else self.last
        if not self.blocked:
            self.seconds += now - self.last
        self.last = now
        return self.seconds

    def block(self, key, now=None):
        self.sample(now)
        self.blocked.add(key)

    def unblock(self, key, now=None):
        self.sample(now)
        self.blocked.discard(key)


class TurnTokens:
    """A turn's token_usage summed over its model requests (pi's, dsh's): output, input with the cache
    reads and writes beside it, and the output whose generation was timed, with that time. With
    ``max_rate`` (token/s) a request timed faster than it, or at no time at all, was timed wrong."""
    def __init__(self, max_rate=None):
        self.max_rate = max_rate
        self.output = self.input = self.read = self.write = 0
        self.measured_tokens, self.measured_seconds = 0, 0.0

    def add(self, output, input, read, write, seconds=None, counted=True):
        """One request: each count added when a non-negative int, and its output with its generation
        ``seconds`` (None: not timed) when ``counted``. False, adding nothing, when ``output`` is no int."""
        if type(output) is not int:
            return False
        for name, value in (("output", output), ("input", input), ("read", read), ("write", write)):
            setattr(self, name, getattr(self, name) + (value if type(value) is int and value >= 0 else 0))
        if counted and seconds is not None and output >= 0 and (
                self.max_rate is None or seconds > 0 and output <= self.max_rate * seconds):
            self.measured_tokens += output
            self.measured_seconds += seconds
        return True

    def payload(self, **extra):
        """The token_usage event's data: the turn's counts, then ``extra``."""
        return {"output_tokens": self.output, "input_tokens": self.input + self.read + self.write,
                "cache_read_tokens": self.read, "cache_write_tokens": self.write,
                "measured_output_tokens": self.measured_tokens, "generation_seconds": self.measured_seconds, **extra}


class SubtaskReporter:
    """Sends a backend's ``subtask_usage`` report: the turn's subtasks at every level, by subtask
    (``agents``: name, level and the four token counts), those that ran without usage
    (``unreported``) and requests whose final output count is unknown (``pending``)."""
    def __init__(self, emit, interval=SUBTASK_REPORT_SECONDS):
        self.emit, self.interval = emit, interval
        self.last = self.sent_at = None

    def due(self, now):
        return self.sent_at is None or not number(now) or now - self.sent_at >= self.interval

    def publish(self, report, now, force=False):
        if report == self.last or not (force or self.due(now)):
            return False
        self.last, self.sent_at = report, now
        self.emit("subtask_usage", copy.deepcopy(report))
        return True

