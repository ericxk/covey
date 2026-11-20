"""本周用量: the bot menu's item (app/bot_menu.py) answered with a card of the last WINDOW_DAYS days' tasks.

The owner chose the items on a question card: the tasks and the topics they ran in, how the
tasks ended, each Harness's share of the tasks and of the tokens, and the tokens as input (cache hits
counted in it, as 任务详情 counts them), cache hits and output, a task's subtasks included. 本周 is the
last 7 × 24 hours, not the calendar week. A task is one job record created in the window that a backend
ran (core/jobs.py never_started leaves out launch cards and merged supplements), in any chat; a turn that
asked a question and the one that went on after the answer are two. The total and each Harness's token
share count the cache hits in (cards/weekly_usage.py total_tokens), as about nine tenths of the input. The counts
are read from the records at each click; nothing is stored. The card goes to the owner's private chat, with a record
of its own (<state>/FOLDER/<token>.json, app/menu_panel.py): a second menu click folds it in place, the next draws it
again with the figures of then.
Only the routed Harnesses are counted: a removed one's tasks are left out.
"""
from __future__ import annotations

import collections
import time

from covey.app.menu_panel import MenuPanel
from covey.cards.weekly_usage import folded_card, total_tokens, usage_card, usage_text
from covey.core import look
from covey.core.jobs import never_started, select_job_records
from covey.core.state import epoch_seconds
from covey.core.statuses import ENDED, NOT_ENDED

WINDOW_DAYS = 7
TOKEN_FIELDS = ("input_tokens", "cache_read_tokens", "output_tokens")
KIND = "weekly-usage"
FOLDER = "weekly-usage-cards"  # The cards' records (app/menu_panel.py).


def created_at(record):
    """When the job was created, in epoch seconds; None when its record does not say."""
    return epoch_seconds(record.get("created_at"))


def job_tokens(record):
    """The job's TOKEN_FIELDS with its subtasks' (subtask_usage per level, added as 任务详情 adds them), a lenient
    sum: a count that is not a non-negative int adds nothing. None when no part has an output count (stopped
    before the first response, a record from before counts)."""
    report = record.get("subtask_usage") if isinstance(record.get("subtask_usage"), dict) else {}
    levels = report.get("levels") if report.get("count") and isinstance(report.get("levels"), list) else []
    parts = [record, *(level for level in levels if isinstance(level, dict))]
    if not any(type(part.get("output_tokens")) is int for part in parts):
        return None
    return {key: sum(value for part in parts if type(value := part.get(key)) is int and value >= 0)
            for key in TOKEN_FIELDS}


def outcome(status):
    """A job status as the card counts it apart from ``ended``: done, continued, failed, cancelled, interrupted,
    active (any that a later one can follow) or other; the card reads ``continued`` (a turn after an answer)."""
    return status if status in ENDED else "active" if status in NOT_ENDED else "other"


def ended(record):
    """How the job ended as a state of docs/ui.md's word table (core/look.py STATES): 做完 (its result seen or not),
    停住了, 已停止 (the owner stopped it himself), 在做, 等你, 排队; ``idle`` for a status the table does not know."""
    return look.record_tone(record, seen=True)


def day_of(seconds):
    """The local date a job was created on, as the trend's label (``11-13``)."""
    return time.strftime("%m-%d", time.localtime(seconds))


def days(since, now):
    """The local dates from ``since`` to ``now``, oldest first: the trend's bars, the first and the last only part of
    a day."""
    found, at = [], since
    while at < now + 86400:
        label = day_of(min(at, now))
        if label not in found:
            found.append(label)
        at += 86400
    return found


def harness_of(record):
    """The Harness the card counts the job under."""
    return str(record.get("harness_id") or record.get("backend") or "")


def summarize(records, now, titles):
    """The card's figures from the job records ``records``, for the WINDOW_DAYS before ``now``. ``titles``
    names the routed Harnesses by ID; a job of any other is left out."""
    since = now - WINDOW_DAYS * 86400
    tasks = [r for r in records if (at := created_at(r)) is not None and since <= at <= now and not never_started(r)
             and harness_of(r) in titles]
    tokens = dict.fromkeys(TOKEN_FIELDS, 0)
    harnesses = collections.defaultdict(lambda: {"tasks": 0, "tokens": 0, "input": 0, "cache": 0, "untracked": 0})
    untracked = 0
    daily = {label: {"day": label, "tasks": 0, "tokens": 0} for label in days(since, now)}
    for record in tasks:
        name = harness_of(record)
        harnesses[name]["tasks"] += 1
        day = daily.setdefault(day_of(created_at(record)), {"day": day_of(created_at(record)), "tasks": 0, "tokens": 0})
        day["tasks"] += 1
        counts = job_tokens(record)
        if counts is None:
            untracked += 1
            harnesses[name]["untracked"] += 1
            continue
        day["tokens"] += total_tokens(counts)
        for key in TOKEN_FIELDS:
            tokens[key] += counts[key]
        harnesses[name]["tokens"] += total_tokens(counts)
        harnesses[name]["input"] += counts["input_tokens"]
        harnesses[name]["cache"] += counts["cache_read_tokens"]
    rows = sorted(({"title": titles.get(name, name), **row} for name, row in harnesses.items()),
                  key=lambda row: (-row["tasks"], -row["tokens"], row["title"]))
    return {"days": WINDOW_DAYS, "since": since, "until": now, "tasks": len(tasks),
            "topics": len({r.get("topic_id") for r in tasks if r.get("topic_id")}),
            "outcomes": collections.Counter(outcome(r.get("status")) for r in tasks),
            "ended": collections.Counter(ended(r) for r in tasks), "daily": list(daily.values()),
            "harnesses": rows, "tokens": tokens, "untracked": untracked}


class WeeklyUsage:
    """The 本周用量 menu item. ``runtime`` logs, ``messenger`` and ``voice`` answer, ``titles`` names each
    routed Harness on the card by ID."""

    def __init__(self, runtime, messenger, voice, titles):
        self.runtime, self.messenger, self.voice, self.titles = runtime, messenger, voice, titles
        self.menu = MenuPanel(self, KIND, FOLDER, "weekly-usage-card")

    def menu_items(self):
        """The bot menu's 用量 (app/unified.py menu)."""
        return {"weekly_usage": self.send}

    def menu_folds(self):
        """A second 用量 on its card, the newest in the chat (app/bot_menu.py)."""
        return {"weekly_usage": self.fold}

    def send(self, ctx, chat_id, source, shown=None):
        """The card, as a new message in ``chat_id``; ``source`` (the menu click's event_id) keys it and its record,
        ``shown`` hears its message id. The Markdown goes instead when the card does not."""
        summary = self.summary(ctx, source)
        self.menu.open(ctx, chat_id, source, lambda token, record: usage_card(summary), usage_text(summary), shown=shown)

    def fold(self, ctx, chat_id, source, message_id):
        """The card the click ``source`` sent folded, or drawn again with the figures of now (app/menu_panel.py
        toggle)."""
        return self.menu.toggle(ctx, chat_id, source, message_id,
                                lambda token, record: usage_card(self.summary(ctx, source)),
                                lambda token, record: folded_card())

    def summary(self, ctx, source):
        """The card's figures now; one log line."""
        now = time.time()
        since = now - WINDOW_DAYS * 86400
        # Only the window's records are copied; an older one, or one without a time, is left in the cache.
        records = select_job_records(ctx.state_dir, lambda r: (created_at(r) or 0) >= since, log=self.runtime.log)
        summary = summarize(records, now, self.titles)
        self.runtime.log(f"weekly usage tasks={summary['tasks']} untracked={summary['untracked']} "
                         f"event={source[-10:]}")
        return summary
