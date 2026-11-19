#!/usr/bin/env python3
"""Where the time of the live flows goes, from the Bridge's millisecond timing lines.

    python3 -B scripts/bridge_timing.py --state "$STATE_DIR" --since 19:30 [--until 20:00]
    python3 -B scripts/bridge_timing.py bridge.log runs/<任务>/runner.log ...

Reads ``bridge.log`` (and its rotated copies) and every ``runs/*/runner.log`` under the state
directory, or the files given, and prints:

- one row per kind of flow (a new message's launch card, a follow-up's queued card, each kind
  of click, a job's hand-off, a runner's start): how many, then p50/max of Feishu's delivery
  delay (the event's own time to the consumer reading it), the Bridge's total, the Feishu API
  time inside it and the Bridge's local time (total minus API);
- each flow's phases: p50/max of each step (a phase's end minus the one before);
- clicks the Bridge forwarded to a task runner, joined by event: until the runner took the
  click, and until its first card write;
- 开始任务 joined to its job's hand-off and runner start: until the runner was spawned and
  until it made its first Feishu call;
- the Feishu calls by operation: p50/max wall time, the lark-cli process's (or SDK
  request's) own, Feishu's own from its Server-Timing header (``inner``: its service;
  ``origin``: its CDN edge's wait on that service; SDK requests only) and the body's bytes.

The line formats are in covey/core/timing.py; docs/operations.md 排障 has an example.
``--since``/``--until`` take HH:MM (on the newest log line's day) or an ISO time. Only the
timing lines' fields are read: never message text.
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from collections import defaultdict
from pathlib import Path

PREFIX = re.compile(r"^\[(?P<ts>[^\]]+)\] (?P<rest>.*)$")


def parse_fields(text: str) -> dict:
    return dict(item.split("=", 1) for item in text.split() if "=" in item)


def parse_line(line: str):
    """(time, kind, fields) of a timing line; None for any other line."""
    match = PREFIX.match(line.rstrip("\n"))
    if not match:
        return None
    rest = match["rest"]
    for kind in ("timing ", "feishu call ", "feishu calls "):
        if rest.startswith(kind):
            break
    else:
        return None
    try:
        at = dt.datetime.fromisoformat(match["ts"])
    except ValueError:
        return None
    fields = parse_fields(rest[len(kind):])
    if kind == "timing ":
        phases, seen = [], defaultdict(int)
        for item in filter(None, fields.get("phases", "").split(",")):
            name, _, ms = item.rpartition(":")
            seen[name] += 1
            if name and ms.lstrip("-").isdigit():
                phases.append((name if seen[name] == 1 else f"{name}#{seen[name]}", int(ms)))
        fields["phases"] = phases
    return at, kind.strip(), fields


def number(fields, key):
    value = fields.get(key, "")
    return int(value) if value.lstrip("-").isdigit() else None


def feishu_delay(fields):
    """``feishu_ms``. Before S33 a click's microsecond timestamp was read as milliseconds
    (``feishu_ms=-1791226253978466``); its stamp is ``t0`` minus that, so the delay still reads off."""
    value, t0 = number(fields, "feishu_ms"), number(fields, "t0")
    if value is None or value > -10 ** 11 or not t0:
        return value
    stamp = t0 - value
    while stamp >= 10 ** 14:
        stamp /= 1000
    return round(t0 - stamp)


def log_files(state: Path) -> list:
    """Oldest first: bridge.log.4 .. bridge.log.1, bridge.log, then each runner.log."""
    rotated = sorted(state.glob("bridge.log.*"), key=lambda p: -int(p.suffix[1:]) if p.suffix[1:].isdigit() else 0)
    main = [state / "bridge.log"] if (state / "bridge.log").exists() else []
    return rotated + main + sorted(state.glob("runs/*/runner.log"))


def read(paths) -> list:
    """(time, kind, fields, side) of every timing line; side is 'runner' for a runner.log."""
    rows = []
    for path in paths:
        side = "runner" if Path(path).name == "runner.log" else "bridge"
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    parsed = parse_line(line)
                    if parsed:
                        rows.append((*parsed, side))
        except OSError as exc:
            print(f"skipped {path}: {type(exc).__name__}", file=sys.stderr)
    return rows


def bound(value: str, latest: dt.datetime):
    if not value:
        return None
    if re.fullmatch(r"\d{1,2}:\d{2}", value):
        hour, minute = map(int, value.split(":"))
        return latest.replace(hour=hour, minute=minute, second=0, microsecond=0)
    parsed = dt.datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=latest.tzinfo)


def stats(values) -> str:
    values = sorted(v for v in values if v is not None)
    if not values:
        return "-"
    return f"{values[(len(values) - 1) // 2]}/{values[-1]}"  # The lower median: n=2 shows the smaller.


def group_of(fields, side) -> str:
    name = fields.get("flow", "?")
    kind = fields.get("kind", "")
    outcome = fields.get("outcome", "")
    label = ":".join(x for x in (name, kind, outcome) if x)
    return ("runner " if side == "runner" and name != "runner" else "") + label


def table(rows, header) -> str:
    widths = [max(len(str(row[i])) for row in [header, *rows]) for i in range(len(header))]
    lines = ["  ".join(str(cell).ljust(width) for cell, width in zip(row, widths)).rstrip() for row in [header, *rows]]
    return "\n".join([lines[0], "  ".join("-" * w for w in widths), *lines[1:]])


def flows_report(flows) -> list:
    groups = defaultdict(list)
    for at, fields, side in flows:
        groups[group_of(fields, side)].append(fields)
    out = ["Flows (ms, p50/max)",
           "first write: the flow's first card or message write ended; seen: Feishu's delay plus it, about when the "
           "owner sees what he did answered", table([
        (name, len(items), stats(feishu_delay(f) for f in items),
         stats(first_card_write(f["phases"]) for f in items), stats(seen(f) for f in items),
         stats(number(f, "total_ms") for f in items), stats(number(f, "api_ms") for f in items),
         stats((number(f, "total_ms") or 0) - (number(f, "api_ms") or 0) for f in items),
         stats(number(f, "calls") for f in items))
        for name, items in sorted(groups.items())],
        ("flow", "n", "feishu delay", "first write", "seen", "total", "feishu api", "bridge local", "calls"))]
    for name, items in sorted(groups.items()):
        steps, order = defaultdict(list), []
        for fields in items:
            previous = 0
            for phase, ms in fields["phases"]:
                if phase not in steps:
                    order.append(phase)
                steps[phase].append(ms - previous)
                previous = ms
        if order:
            out += ["", f"Phases of {name} (step ms, p50/max; n={len(items)})",
                    table([(phase, len(steps[phase]), stats(steps[phase])) for phase in order], ("phase", "n", "step"))]
    return out


def first_card_write(phases):
    """When the flow's first card or message write ended (ms into it), None without one: msg_get is a read."""
    return next((ms for phase, ms in phases if phase.startswith(("card_", "msg_")) and phase != "msg_get"), None)


def seen(fields):
    """Feishu's delay plus the first write: about when the owner sees what he did answered; None without either."""
    delay, first = feishu_delay(fields), first_card_write(fields["phases"])
    return None if delay is None or first is None else delay + first


def joins_report(flows) -> list:
    out = []
    runner_clicks = {f.get("event"): (at, f) for at, f, side in flows if side == "runner" and f.get("flow") == "click"}
    forwarded = defaultdict(list)
    for at, fields, side in flows:
        if side == "bridge" and fields.get("flow") == "click" and fields.get("outcome") == "forwarded":
            joined = runner_clicks.get(fields.get("event"))
            if joined:
                t0, runner = number(fields, "t0"), joined[1]
                pickup = number(runner, "t0") - t0 if number(runner, "t0") and t0 else None
                write = first_card_write(runner["phases"])
                forwarded[fields.get("kind", "?")].append((pickup, number(runner, "inbox_ms"),
                                                           pickup + write if pickup is not None and write is not None else None,
                                                           pickup + number(runner, "total_ms") if pickup is not None else None))
    if forwarded:
        out += ["", "Forwarded clicks: Bridge arrival to runner (ms, p50/max)", table([
            (kind, len(rows), stats(r[0] for r in rows), stats(r[1] for r in rows), stats(r[2] for r in rows),
             stats(r[3] for r in rows)) for kind, rows in sorted(forwarded.items())],
            ("kind", "n", "runner took it", "inbox wait", "first card write", "runner done"))]
    starts = {f.get("job"): f for _, f, side in flows if side == "bridge" and f.get("flow") == "job_start"}
    runners = {f.get("job"): f for _, f, side in flows if side == "runner" and f.get("flow") == "runner"}
    launches = []
    for at, fields, side in flows:
        if side != "bridge" or fields.get("kind") != "bridge_launch_submit" or not fields.get("jobs"):
            continue
        t0 = number(fields, "t0")
        job = fields["jobs"].split("+")[0]
        start, runner = starts.get(job), runners.get(job)
        spawned = dict(start["phases"]).get("spawned") if start else None
        launches.append((number(start, "t0") - t0 + spawned if start and spawned is not None and t0 else None,
                         number(runner, "t0") + number(runner, "total_ms") - t0 if runner and t0 else None))
    if launches:
        out += ["", "开始任务 to its task runner (ms from the click's arrival, p50/max)", table([
            ("bridge_launch_submit", len(launches), stats(r[0] for r in launches), stats(r[1] for r in launches))],
            ("click", "n", "runner spawned", "runner's first feishu call"))]
    return out


def calls_report(calls, totals) -> list:
    groups = defaultdict(list)
    for fields in calls:
        groups[(fields.get("op", "?"), fields.get("via", "?"))].append(fields)
    out = ["", "Feishu calls inside flows (ms, p50/max; inner, origin: Feishu's own, SDK only)", table([
        (op, via, len(items), stats(number(f, "ms") for f in items), stats(number(f, "proc_ms") for f in items),
         stats(number(f, "inner_ms") for f in items), stats(number(f, "origin_ms") for f in items),
         stats(number(f, "bytes") for f in items), sum(f.get("result") not in ("ok", "receipt") for f in items))
        for (op, via), items in sorted(groups.items())],
        ("op", "via", "n", "wall", "process", "inner", "origin", "bytes", "errors"))]
    merged = defaultdict(lambda: [0, 0, [], 0, [], []])
    for fields in totals:
        row = merged[fields.get("op", "?")]
        row[0] += number(fields, "n") or 0
        row[1] += number(fields, "errors") or 0
        row[2].append(number(fields, "ms_p50"))
        row[3] = max(row[3], number(fields, "ms_max") or 0)
        row[4].append(number(fields, "inner_p50"))
        row[5].append(number(fields, "origin_p50"))
    if merged:
        out += ["", "Feishu calls outside flows (per-minute summaries; median of each minute's p50, ms)", table([
            (op, n, errors, stats(p50s).split("/")[0], high, stats(inner).split("/")[0], stats(origin).split("/")[0])
            for op, (n, errors, p50s, high, inner, origin) in sorted(merged.items())],
            ("op", "n", "errors", "wall", "max", "inner", "origin"))]
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("files", nargs="*", type=Path, help="log files (default: the state directory's)")
    parser.add_argument("--state", type=Path, help="the Bridge's state directory")
    parser.add_argument("--since", default="", help="HH:MM on the newest line's day, or an ISO time")
    parser.add_argument("--until", default="")
    args = parser.parse_args(argv)
    if not args.files and not args.state:
        parser.error("give --state or log files")
    rows = sorted(read(args.files or log_files(args.state)), key=lambda row: row[0])
    if not rows:
        print("no timing lines (a Bridge from before S32, or nothing in range)")
        return 1
    latest = max(row[0] for row in rows)
    since, until = bound(args.since, latest), bound(args.until, latest)
    rows = [r for r in rows if (since is None or r[0] >= since) and (until is None or r[0] <= until)]
    flows = [(at, fields, side) for at, kind, fields, side in rows if kind == "timing"]
    calls = [fields for _, kind, fields, _ in rows if kind == "feishu call"]
    totals = [fields for _, kind, fields, _ in rows if kind == "feishu calls"]
    if not rows:
        print("no timing lines in range")
        return 1
    print(f"{len(flows)} flows, {len(calls)} calls in them, {rows[0][0].isoformat(timespec='seconds')} .. "
          f"{rows[-1][0].isoformat(timespec='seconds')}\n")
    print("\n".join(flows_report(flows) + joins_report(flows) + calls_report(calls, totals)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
