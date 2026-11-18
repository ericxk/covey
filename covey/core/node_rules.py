"""The rules that turn a node's samples into alerts (docs/architecture.md "节点"), as pure functions.

``evaluate(state, sample, now)`` takes a node's rule state (``<state>/nodes/<alias>/alerts.json``) and one sample and
returns the new state and the events it raised: ``{"rule", "kind": "raise"|"clear"|"once", ...}``. The gateway
(app/node_gateway.py) calls it for each sample as it arrives; ``offline`` is not here, the Bridge's sender works it
out from ``link.json`` (app/node_alerts.py).

- ``mem``: the working set at MEM_RAISE percent of the limit or more for MEM_HOLD seconds raises; under MEM_CLEAR
  clears.
- ``oom``: the container's ``oom_kill`` count grew (a process was killed for memory): one event, ``once``.
- ``disk:<path>``: a disk at DISK_RAISE percent full raises, under DISK_CLEAR clears.
- ``write:<path>``: a write probe that failed raises (the error says why: quota exceeded, hung), one that passed clears.

A raised event carries ``top``: the sample's processes using the most memory, each with its task when it has one.
"""
from __future__ import annotations

MEM_RAISE, MEM_CLEAR, MEM_HOLD = 90.0, 85.0, 60.0
DISK_RAISE, DISK_CLEAR = 90.0, 85.0
TOP = 5


def top(sample):
    return [{k: p.get(k) for k in ("pid", "comm", "rss", "cpu", "task") if p.get(k) is not None}
            for p in (sample.get("procs") or [])[:TOP]]


def evaluate(state, sample, now):
    """(new state, events) for ``sample`` received at ``now``."""
    state = {k: dict(v) if isinstance(v, dict) else v for k, v in (state or {}).items()}
    events = []

    def raise_(rule, **detail):
        entry = state.get(rule) or {}
        if entry.get("raised"):
            return
        state[rule] = {"raised": True, "since": now}
        events.append(dict({"rule": rule, "kind": "raise", "at": now, "top": top(sample)}, **detail))

    def clear(rule, **detail):
        entry = state.get(rule) or {}
        if entry.get("raised"):
            state[rule] = {"raised": False, "since": now}
            events.append(dict({"rule": rule, "kind": "clear", "at": now, "raised_at": entry.get("since")}, **detail))
        elif "over" in entry:
            state[rule] = {"raised": False}

    mem = sample.get("mem") or {}
    pct = mem.get("pct")
    if isinstance(pct, (int, float)):
        entry = state.get("mem") or {}
        if pct >= MEM_RAISE:
            if not entry.get("raised"):
                over = entry["over"] if isinstance(entry.get("over"), (int, float)) else now
                state["mem"] = {"raised": False, "over": over}
                if now - over >= MEM_HOLD:
                    raise_("mem", pct=pct, used=mem.get("used"), limit=mem.get("limit"))
        elif pct < MEM_CLEAR:
            clear("mem", pct=pct)
        elif "over" in entry and not entry.get("raised"):
            state["mem"] = {"raised": False}

    oom = mem.get("oom_kill")
    if isinstance(oom, int):
        seen = (state.get("oom") or {}).get("count")
        if isinstance(seen, int) and oom > seen and (state.get("oom") or {}).get("boot") == sample.get("boot"):
            events.append({"rule": "oom", "kind": "once", "at": now, "count": oom - seen, "pct": pct,
                           "top": top(sample)})
        state["oom"] = {"count": oom, "boot": sample.get("boot")}

    for disk in sample.get("disk") or []:
        if not isinstance(disk, dict) or not disk.get("path"):
            continue
        rule = "disk:" + disk["path"]
        full = disk.get("pct")
        if isinstance(full, (int, float)):
            if full >= DISK_RAISE:
                raise_(rule, path=disk["path"], pct=full, free=disk.get("free"))
            elif full < DISK_CLEAR:
                clear(rule, path=disk["path"], pct=full)
        probe = disk.get("write")
        if isinstance(probe, dict):
            rule = "write:" + disk["path"]
            if probe.get("ok") is False:
                raise_(rule, path=disk["path"], error=probe.get("error"))
            elif probe.get("ok"):
                clear(rule, path=disk["path"])
    return state, events
