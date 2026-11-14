"""Current-invocation root and subagent usage and arrival-time latency, without model text."""
import json
import time
from pathlib import Path

from covey.core.state import epoch_seconds
from covey.core.ttft import FirstToken
from covey.core.usage import SUBTASK_REPORT_SECONDS, ActiveGenerationClock, SubtaskReporter

AGENT_TOOLS = {"Agent", "Task"}
TOOL_BLOCKS = {"tool_use", "server_tool_use"}
NATIVE_FIELDS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")
MAX_LEVEL = 20
MTIME_MARGIN = 600
ENDED_READS = 3  # Reads of an ended subagent's transcript while the turn runs, at most.


def _after(stamp, since):
    at = epoch_seconds(str(stamp))
    return at is not None and at >= since


class ClaudeSubtaskUsage:
    """This invocation's subagent requests at every nesting level, per subagent, each request once.

    A subagent's assistant rows reach the root stream with ``parent_tool_use_id`` (the Agent call that
    started it) and the message's usage as it was when the row was written. Claude Code writes a row as
    each content block ends and replaces the usage at ``message_delta``, so only a row with
    ``stop_reason`` surely holds the final output count; subagents get no stream_event rows. The
    transcript of a subagent, written after that replacement, is read as soon as the subagent ends
    (``finished``: repo review S1, a running turn's background subagents read 1.1 万 output for 24.1 万),
    and read again while it still lacks a final count; ``settle`` reads them all once the CLI has exited.
    Requests are keyed by message id: a block written again or a transcript row counts once, a retried
    request is a new message and counts. The result row's totals are never added."""

    def __init__(self, emit, interval=SUBTASK_REPORT_SECONDS, locate=None):
        self.reporter = SubtaskReporter(emit, interval)
        self.messages = {}
        self.calls = {}  # Agent call id -> (the agent that made it, None for the main task; its name)
        self.depths = {}
        self.read = None  # the transcripts: None not read yet, True read, False missing or unreadable
        self.dirty = False
        # () -> (the session's subagents directory or None, the turn's start or None): where an ended
        # subagent's transcript is, once the session is known (backends/claude/argv.py subagents_dir).
        self.locate = locate
        self.paths = {}  # agent -> its transcript, once found
        # Subagents that ended while the turn runs -> reads left: read again at later reports (a row written
        # just after the notification) until their counts are final.
        self.ended = {}
        self.reread_at = None

    def accept(self, row, now):
        if row.get("type") != "assistant":
            return
        message = row.get("message") if isinstance(row.get("message"), dict) else {}
        owner = row.get("parent_tool_use_id")
        for block in message.get("content") if isinstance(message.get("content"), list) else []:
            if (isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") in AGENT_TOOLS
                    and isinstance(block.get("id"), str) and block["id"] not in self.calls):
                inputs = block.get("input") if isinstance(block.get("input"), dict) else {}
                self.calls[block["id"]] = (owner, str(inputs.get("description") or "子任务")[:80])
                self.dirty = True
        if isinstance(owner, str) and owner and row.get("is_api_error_message") is not True:
            self.count(owner, message, recorded=False)
        self.publish(now)

    def count(self, agent, message, recorded):
        mid, usage = message.get("id"), message.get("usage")
        if not isinstance(mid, str) or not mid or not isinstance(usage, dict):
            return
        entry = self.messages.setdefault(mid, {"agent": agent, "final": False, "recorded": False})
        for field in NATIVE_FIELDS:
            value = usage.get(field)
            if type(value) is int and value > entry.get(field, -1):
                entry[field] = value
                self.dirty = True
        for flag, value in (("final", message.get("stop_reason") is not None), ("recorded", recorded)):
            if value and not entry[flag]:
                entry[flag] = self.dirty = True

    def level(self, agent):
        if agent in self.depths:
            return self.depths[agent]
        level, caller = 1, self.calls.get(agent, (None,))[0]
        while caller is not None and level < MAX_LEVEL:
            level, caller = level + 1, self.calls.get(caller, (None,))[0]
        return level

    def report(self):
        agents, pending = {}, 0
        for entry in self.messages.values():
            key = entry["agent"]
            row = agents.get(key)
            if row is None:
                row = agents[key] = {"name": self.calls.get(key, (None, "子任务"))[1], "level": self.level(key),
                                     "input_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0, "output_tokens": 0}
            # A request seen only in the stream without stop_reason may lack its final output count;
            # one the transcript recorded has all there is, also when its stream was cut off.
            if not (entry["final"] or entry["recorded"]) or "input_tokens" not in entry or "output_tokens" not in entry:
                pending += 1
            row["input_tokens"] += sum(entry.get(field, 0) for field in NATIVE_FIELDS[:3])
            row["cache_read_tokens"] += entry.get("cache_read_input_tokens", 0)
            row["cache_write_tokens"] += entry.get("cache_creation_input_tokens", 0)
            row["output_tokens"] += entry.get("output_tokens", 0)
        # With the transcripts read, an Agent call without one made no request (it never started).
        unreported = 0 if self.read else sum(1 for call in self.calls if call not in agents)
        return {"agents": agents, "unreported": unreported, "pending": pending}

    def publish(self, now, force=False):
        if not (self.messages or self.calls):
            return
        if self.ended and not force and (self.reread_at is None or not isinstance(now, (int, float))
                                         or now >= self.reread_at):
            self.reread_at = now + self.reporter.interval if isinstance(now, (int, float)) else None
            self.read_ended()
        if not (self.dirty or force):
            return
        if force or self.reporter.due(now):
            self.dirty = False
            self.reporter.publish(self.report(), now, force)

    def finished(self, agent, now):
        """Subagent ``agent`` ended (its Agent call's result, or its task notification): its transcript is
        read now, for the final counts its stream rows lack, and reported. Display-only: nothing raises."""
        self.ended[agent] = ENDED_READS
        self.read_ended()
        self.dirty = True
        self.publish(now, force=True)

    def read_ended(self):
        """The transcripts of the subagents that ended while the turn runs and still lack a final count,
        each read at most ENDED_READS times."""
        if self.locate is None:
            self.ended.clear()
            return
        self._drop_settled()
        if not self.ended:
            return
        try:
            directory, since = self.locate()
            if directory is None or since is None or not Path(directory).is_dir():
                return
            for agent in list(self.ended):
                self.ended[agent] -= 1
                if self.ended[agent] <= 0:
                    del self.ended[agent]
                path = self.paths.get(agent) or next(
                    (path for found, path in self._transcripts(Path(directory), since) if found == agent), None)
                if path is not None:
                    self._read_file(agent, path, since)
                    self.dirty = True
        except Exception:
            return
        self._drop_settled()

    def _drop_settled(self):
        """Ended subagents whose transcript gave every count are not read again."""
        unsettled = {entry["agent"] for entry in self.messages.values() if not (entry["final"] or entry["recorded"])}
        recorded = {entry["agent"] for entry in self.messages.values() if entry["recorded"]}
        for agent in [a for a in self.ended if a not in unsettled and a in recorded]:
            del self.ended[agent]

    def settle(self, directory, since, now=None):
        """Once the CLI has exited: the subagents' requests since ``since`` (this invocation; a resumed
        session's earlier turns are older) from ``<session>/subagents/agent-*.jsonl``. A request cut off
        when the run ended keeps what Claude Code recorded of it; its cost tracker leaves it out."""
        if self.messages or self.calls:
            try:
                self.read = directory is not None and since is not None and self._read(Path(directory), since)
            except Exception:
                self.read = False
            self.dirty = True
        self.publish(time.time() if now is None else now, force=True)

    def _read(self, directory, since):
        if not directory.is_dir():
            return False
        for agent, path in self._transcripts(directory, since):
            self._read_file(agent, path, since)
        return True

    def _transcripts(self, directory, since):
        """(agent, transcript) of each subagent of this run in ``directory``, its meta noted."""
        for path in sorted(directory.glob("agent-*.jsonl")):
            # Only skips what no request of this run can be in: file times are coarse, or a file
            # server's clock, so the margin; each request's own timestamp decides.
            if path.stat().st_mtime < since - MTIME_MARGIN:
                continue
            try:
                meta = json.loads(path.with_name(path.name[:-len(".jsonl")] + ".meta.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                meta = {}
            meta = meta if isinstance(meta, dict) else {}
            agent = meta.get("toolUseId") if isinstance(meta.get("toolUseId"), str) and meta["toolUseId"] else path.stem
            if type(meta.get("spawnDepth")) is int and 0 < meta["spawnDepth"] <= MAX_LEVEL:
                self.depths.setdefault(agent, meta["spawnDepth"])
            if agent not in self.calls and isinstance(meta.get("description"), str) and meta["description"]:
                self.calls[agent] = (None, meta["description"][:80])
            self.paths[agent] = path
            yield agent, path

    def _read_file(self, agent, path, since):
        with path.open("rb") as handle:
            for line in handle:
                if b'"assistant"' not in line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue  # A torn last line.
                if (isinstance(row, dict) and row.get("type") == "assistant" and row.get("isApiErrorMessage") is not True
                        and isinstance(row.get("message"), dict) and _after(row.get("timestamp"), since)):
                    self.count(agent, row["message"], recorded=True)


class ClaudeTurnUsage:
    """The turn's main-task requests, each once, and the context of the latest: its input (uncached,
    cache read and cache write) and output, against the window the result row gives its model."""

    def __init__(self, emit):
        self.emit = emit
        self.start = self.clock = self.message_id = None
        self.messages = {}
        self.ttft = FirstToken(emit, 'claude_delta')
        self.last = None
        self.context = self.window = self.model = None
        self.outside = set()  # Requests before the turn (a compaction): counted, but not generation or context.

    def arm(self, now):
        if self.start is None:
            self.start, self.clock = now, ActiveGenerationClock(now)

    def update(self, mid, usage, now, *, context=True):
        """``context``: the request is the main task's (a compaction before the turn is not)."""
        if not isinstance(mid, str) or not mid or not isinstance(usage, dict):
            return
        counts = self.messages.setdefault(mid, {})
        for field in ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens', 'output_tokens'):
            value = usage.get(field)
            if type(value) is int and value >= 0:
                counts[field] = max(counts.get(field, 0), value)
        if not context:
            self.outside.add(mid)
        elif all(k in counts for k in NATIVE_FIELDS[:3]):
            self.context = sum(counts.get(k, 0) for k in NATIVE_FIELDS)
        self.publish(now)

    def publish(self, now):
        if not self.messages or not all('output_tokens' in m for m in self.messages.values()):
            return
        result = {'output_tokens': sum(m['output_tokens'] for m in self.messages.values())}
        if all('input_tokens' in m for m in self.messages.values()):
            # Anthropic reports the uncached, cache-read and cache-write buckets
            # separately. Do not add the nested cache_creation breakdown again.
            result['input_tokens'] = sum(sum(m.get(k, 0) for k in
                ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens'))
                for m in self.messages.values())
        for native, public in (("cache_read_input_tokens", "cache_read_tokens"),
                               ("cache_creation_input_tokens", "cache_write_tokens")):
            if all(native in m for m in self.messages.values()):
                result[public] = sum(m[native] for m in self.messages.values())
            else:
                result[public] = None
        if self.clock is not None:
            seconds = self.clock.sample(now)
            if seconds > 0:
                outside = sum(self.messages[mid].get('output_tokens', 0) for mid in self.outside)
                result.update(measured_output_tokens=result['output_tokens'] - outside, generation_seconds=seconds)
        for key, value in (('context_tokens', self.context), ('context_window', self.window)):
            if value:
                result[key] = value
        if self.model:
            result['model'] = self.model  # The model Claude Code ran (its init row): the answer card's 输入 names it.
        if result != self.last:
            self.last = result
            self.emit('token_usage', result.copy())

    def accept(self, row, now):
        if row.get('parent_tool_use_id') is not None:
            return
        kind = row.get('type')
        message = row.get('message') or {}
        if kind == 'system' and row.get('subtype') == 'init' and isinstance(row.get('model'), str):
            self.model = row['model']
        elif kind == 'result':
            self.read_window(row, now)
        if kind == 'stream_event':
            event = row.get('event') or {}
            if event.get('type') == 'message_start':
                message = event.get('message') or {}
                self.message_id = message.get('id')
                if self.clock is not None:
                    self.clock.unblock('idle', now)
                self.update(self.message_id, message.get('usage'), now)
            elif event.get('type') == 'message_delta':
                self.update(self.message_id, event.get('usage'), now)
            delta = event.get('delta') or {}
            field = {'text_delta': 'text', 'thinking_delta': 'thinking'}.get(delta.get('type'))
            if field and isinstance(delta.get(field), str) and delta[field] and not self.ttft.done:
                self.ttft.output(self.start, now)
            elif event.get('type') == 'content_block_start' and (event.get('content_block') or {}).get('type') in TOOL_BLOCKS:
                self.ttft.skip()  # A tool call came first: no TTFT, not one that counts the tool's run.
        if kind == 'assistant':
            self.update(message.get('id'), message.get('usage'), now)
            content = message.get('content') if isinstance(message.get('content'), list) else []
            if any(isinstance(block, dict) and block.get('type') in TOOL_BLOCKS for block in content):
                self.ttft.skip()
        if self.clock is not None:
            for block in message.get('content', []) if isinstance(message.get('content'), list) else []:
                if not isinstance(block, dict):
                    continue
                if kind == 'assistant' and block.get('type') == 'tool_use' and block.get('id'):
                    self.clock.block('tool:' + block['id'], now)
                elif kind == 'user' and block.get('type') == 'tool_result' and block.get('tool_use_id'):
                    self.clock.unblock('tool:' + block['tool_use_id'], now)
            if kind == 'result':
                # Delivery, process shutdown and background settling are not generation.
                self.clock.block('idle', now)

    def read_window(self, row, now):
        """The main model's context window from a result row's per-model usage (``modelUsage``: 1,000,000
        for Opus 5.5 on 2.1.310), published with the counts."""
        models = row.get('modelUsage')
        models = models if isinstance(models, dict) else {}
        windows = {name: entry.get('contextWindow') for name, entry in models.items() if isinstance(entry, dict)}
        window = windows.get(self.model) if self.model in windows else (
            next(iter(windows.values())) if len(windows) == 1 else None)
        if type(window) is int and window > 0 and window != self.window:
            self.window = window
            self.publish(now)
