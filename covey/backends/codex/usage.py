"""A Codex turn's token counts from the App Server's cumulative counters."""


class CodexTurnUsage:
    """Difference native cumulative counters from this turn's pre-request totals."""
    def __init__(self):
        self.baseline = {}
        self.counts = {}
        self.started = False
        self.last = None

    def update(self, usage, current):
        total, last = usage.get("total") or {}, usage.get("last") or {}
        for native, public in (("inputTokens", "input_tokens"), ("outputTokens", "output_tokens"), ("cachedInputTokens", "cache_read_tokens")):
            value = total.get(native)
            if type(value) is not int or value < 0:
                continue
            if not current:
                if not self.started:
                    self.baseline[public] = value
                continue
            if public not in self.baseline and not self.started:
                previous = last.get(native)
                if type(previous) is int and 0 <= previous <= value:
                    self.baseline[public] = value - previous
            if public in self.baseline:
                count = value - self.baseline[public]
                if count >= self.counts.get(public, 0):
                    self.counts[public] = count
        if not current:
            return None
        self.started = True
        if "output_tokens" not in self.counts:
            return None
        result = {"input_tokens": self.counts.get("input_tokens"), "output_tokens": self.counts["output_tokens"]}
        if "cache_read_tokens" in self.counts:
            result["cache_read_tokens"] = self.counts["cache_read_tokens"]
        if result == self.last:
            return None
        self.last = result
        return result.copy()
