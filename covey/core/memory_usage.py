"""Which memory lines the turns used and carried (docs/butler.md 记忆), so what is never of use goes first
and in time out of the way: ``<state>/memory-usage.json`` {schema_version, refs: {ref: {n, last}}}, ``ref`` as core/memory.py
names an item ("principle:<id>", "project:<project>:<id>", "candidate:<id>", …), ``n`` how many turns used it, ``last`` when
the latest did. The Bridge writes it once a turn has ended (app/memory_learn.py: the numbers the turn's checkpoint lists in
``memory_used`` and the ones its reply names, of the items the turn carried); core/memory.py orders by it and the nightly
整理记忆 (app/memory_tidy.py) lets what nobody used for ``IDLE_DAYS`` go to the history. At most ``KEPT`` refs, the ones used
longest ago dropped first. Written whole under its own lock; a read takes none; a file that cannot be read is read as empty
and written over by the next note (it only ever orders and ages, nothing is lost with it).

The counts the ranking of core/memory_rank.py takes are new, and an older release rewrites memory-usage.json with its own
keys, so they lie beside it in ``memory/usage.json`` (core/memstore.py usage_path) {version, refs: {ref: {e, t, h}}}:
``e`` how many turns carried the line's whole text (``carried``), ``t`` how many listed only its title (``listed``),
``h`` the strong signals that it was of use, weighed as core/memory_rank.py SIGNAL_WEIGHTS says (``signal``: shown with
``covey memory show``, a question it answered before, the owner confirming it, a checkpoint's ``memory_used``), ``last``
when it was last used: a checkpoint's ``memory_used``, ``show``, a question it answered before (``USES``). Carried whole
or listed by title is no use, nor the owner confirming it (T908: a line carried every turn and never of use stayed for
good). ``counts`` reads both files as one: an old ref (``principle:p…``) is read as the entry that has it among its aliases
(memory/codes.json), its ``n`` added to the entry's and the later ``last`` kept; 整理记忆 ages the 资料 and the 决定 by it
(``idle``; never a 规矩). A ``last`` an older release wrote when a turn carried the line is read as it is: it ages out
within IDLE_DAYS. Standard library and core only."""
from __future__ import annotations

import time
from pathlib import Path

from covey.core import memory_rank, memstore
from covey.core.locks import shared_file_lock
from covey.core.state import read_object, write_json

FILE = "memory-usage.json"
SCHEMA = 1
KEPT = 2000
IDLE_DAYS = 30  # A 资料 or a 决定 no turn used (``USES``) for this long is put away by 整理记忆 (app/memory_tidy.py).


def path(state_dir) -> Path:
    return Path(state_dir) / FILE


def _number(value) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def read(state_dir) -> dict:
    """ref -> {n, last} of every line some turn used; {} when there is no record or it cannot be read."""
    refs = read_object(path(state_dir)).get("refs")
    found = {}
    for ref, row in (refs.items() if isinstance(refs, dict) else ()):
        if isinstance(ref, str) and isinstance(row, dict):
            found[ref] = {"n": int(_number(row.get("n"))), "last": _number(row.get("last"))}
    return found


def last_used(state_dir) -> dict:
    """ref -> when a turn last used it (core/memory.py items ``used``)."""
    return {ref: row["last"] for ref, row in read(state_dir).items() if row["last"]}


def note(state_dir, refs, *, now=None) -> int:
    """``refs`` used by one turn now: each one's count up by one, its ``last`` now. How many were noted."""
    refs = list(dict.fromkeys(str(ref) for ref in refs or () if ref))
    if not refs:
        return 0
    stamp = time.time() if now is None else now
    with shared_file_lock(path(state_dir)):
        found = read(state_dir)
        for ref in refs:
            row = found.get(ref) or {"n": 0, "last": 0.0}
            found[ref] = {"n": row["n"] + 1, "last": stamp}
        kept = dict(sorted(found.items(), key=lambda pair: -pair[1]["last"])[:KEPT])
        write_json(path(state_dir), {"schema_version": SCHEMA, "refs": kept}, ensure_ascii=False)
    return len(refs)


def carry(state_dir, old, new) -> bool:
    """The use of the line ``old`` goes to ``new`` (a candidate turned principle, a project's or a topic's line made global): the
    later ``last``, the counts added, ``old`` gone, so what was in use does not start from nothing and drop out of the room.
    Whether there was anything to carry."""
    old, new = str(old or ""), str(new or "")
    if not old or not new or old == new:
        return False
    with shared_file_lock(path(state_dir)):
        found = read(state_dir)
        row = found.pop(old, None)
        if row is None:
            return False
        kept = found.get(new) or {"n": 0, "last": 0.0}
        found[new] = {"n": kept["n"] + row["n"], "last": max(kept["last"], row["last"])}
        write_json(path(state_dir), {"schema_version": SCHEMA, "refs": found}, ensure_ascii=False)
    return True


def idle(usage, ref, written_at, *, now=None, days=IDLE_DAYS) -> bool:
    """Whether the line ``ref`` (written at ``written_at``) has gone ``days`` days with no turn using it: written that long
    ago and not used since. ``usage`` is ``read``'s."""
    stamp = time.time() if now is None else now
    last = max(_number(written_at), (usage.get(ref) or {}).get("last", 0.0))
    return bool(last) and stamp - last >= days * 86400


COUNTS = ("e", "t", "h")


USES = ("memory_used", "show", "dedup")  # The signals that move a line's ``last``: it was of use (not carried, listed, confirmed).


def _v2(state_dir) -> dict:
    refs = read_object(memstore.usage_path(state_dir)).get("refs")
    found = {}
    for ref, row in (refs.items() if isinstance(refs, dict) else ()):
        if isinstance(ref, str) and isinstance(row, dict):
            found[ref] = {**{key: _number(row.get(key)) for key in COUNTS}, "last": _number(row.get("last"))}
    return found


def counts(state_dir) -> dict:
    """ref -> {e, t, h, n, last} of every line some turn carried or used: memory/usage.json and memory-usage.json read as
    one, an old ref counted on the entry it became (memory/codes.json aliases)."""
    aliases = memstore.codes(state_dir)["aliases"]
    found = {}
    for ref, row in read(state_dir).items():
        mine = found.setdefault(aliases.get(ref, ref), {**dict.fromkeys(COUNTS, 0.0), "n": 0, "last": 0.0})
        mine["n"] += row["n"]
        mine["last"] = max(mine["last"], row["last"])
    for ref, row in _v2(state_dir).items():
        mine = found.setdefault(aliases.get(ref, ref), {**dict.fromkeys(COUNTS, 0.0), "n": 0, "last": 0.0})
        for key in COUNTS:
            mine[key] += row[key]
        mine["last"] = max(mine["last"], row["last"])
    return found


def idle_view(state_dir) -> dict:
    """``counts`` as ``idle`` reads it, each entry's row under its old refs too (a project's line by the ref
    core/project_memory.py gives it, a candidate's ``candidate:<id>``)."""
    found = counts(state_dir)
    for old, new in memstore.codes(state_dir)["aliases"].items():
        if new in found and old not in found:
            found[old] = found[new]
    return found


def _bump(state_dir, refs, key, amount, now=None, *, used=False) -> int:
    refs = list(dict.fromkeys(str(ref) for ref in refs or () if ref))
    if not refs or not amount:
        return 0
    stamp = time.time() if now is None else now
    path = memstore.usage_path(state_dir)
    with shared_file_lock(path):
        found = _v2(state_dir)
        for ref in refs:
            row = found.setdefault(ref, {**dict.fromkeys(COUNTS, 0.0), "last": 0.0})
            row[key] = round(row[key] + amount, 3)
            if used:
                row["last"] = max(row["last"], stamp)
        kept = dict(sorted(found.items(), key=lambda pair: -(pair[1]["e"] + pair[1]["h"]))[:KEPT * 2])
        write_json(path, {"version": memstore.VERSION, "refs": kept}, ensure_ascii=False, separators=(",", ":"))
    return len(refs)


def carried(state_dir, refs) -> int:
    """``refs`` whose whole text one turn carried: each one's ``e`` up by one (not a use: ``last`` stays)."""
    return _bump(state_dir, refs, "e", 1)


def listed(state_dir, refs) -> int:
    """``refs`` one turn listed by title only: each one's ``t`` up by one."""
    return _bump(state_dir, refs, "t", 1)


def signal(state_dir, refs, kind) -> int:
    """A strong sign ``kind`` (core/memory_rank.py SIGNAL_WEIGHTS: show, dedup, confirm, memory_used) that ``refs`` were of
    use: each one's ``h`` up by its weight, and its ``last`` now for one of ``USES``. ValueError for another kind."""
    if kind not in memory_rank.SIGNAL_WEIGHTS:
        raise ValueError(f"signal {kind!r}")
    return _bump(state_dir, refs, "h", memory_rank.SIGNAL_WEIGHTS[kind], used=kind in USES)


def carry_v2(state_dir, old, new) -> bool:
    """The counts of ``old`` in memory/usage.json go to ``new`` (an entry moved): added, ``old`` gone."""
    old, new = str(old or ""), str(new or "")
    if not old or not new or old == new:
        return False
    path = memstore.usage_path(state_dir)
    with shared_file_lock(path):
        found = _v2(state_dir)
        row = found.pop(old, None)
        if row is None:
            return False
        mine = found.setdefault(new, {**dict.fromkeys(COUNTS, 0.0), "last": 0.0})
        for key in COUNTS:
            mine[key] += row[key]
        mine["last"] = max(mine.get("last", 0.0), row.get("last", 0.0))
        write_json(path, {"version": memstore.VERSION, "refs": found}, ensure_ascii=False, separators=(",", ":"))
    return True
