"""整理记忆, once a day while COVEY_MEMORY is on (docs/butler.md 记忆): the memory's 资料 and 决定 put in order by rules alone (no
model), so a project's 决定 are not a pile of every answer the owner ever gave. ``threads`` runs it (its own worker, none with
the memory off: the first ``tick`` of each day), and the evening digest shows that day's run (app/digest.py daily) with 撤销.

For each project not done or cancelled, the ones with the most such entries first (at most MAX_PROJECTS a day, the others
the next), of its entries in use (core/memstore.py entries: memory.json's lines and decisions.jsonl's) only the 资料 and the
决定 (``TIDIED``): never a 规矩 of anybody's, the owner's, the lead's (负责人记的, however long no turn used it), an older line of
the lead's that is a limit (``KEPT_KINDS``) or the style line:

1. What is not this project's matter goes (core/project_memory.py across): an answer whose question names an entry of another
   project's memory by its id (``FOREIGN``: a card that sorted out that project's memory, as a migration asks how each of its
   lines counts; what the owner chose holds in those lines), and an answer whose question a correction of some line names in
   its origin (``DONE``: it was carried out on that line).
2. 用到才留: an entry written IDLE_DAYS ago that no turn used since (core/memory_usage.py idle: a checkpoint's
   ``memory_used``, ``covey memory show``, a question it answered before; being carried is no use) goes to the history.
3. Nothing is deleted. A line of memory.json is put away by core/project_memory.py settle (only the ids it was given, only
   those parts) and is one line of 记录 with the others of its run (core/memstore.py ``log_change``, the entries as they
   were); a decision of decisions.jsonl is settled through core/memory_gate.py revise (each its own line, which 记录 leaves
   out as it does every answer's). Either is read back by 查记忆 (core/memory_search.py), and 撤销 puts them back.

The record, <state>/memory-tidy.json: {schema_version, runs: [{id, slot, chat_id, at, projects: [{id, name, settled: [[entry,
before, why]], decided?: [[ref, why]], failed?}], undone?}]}, the last RUNS_KEPT; a slot (``day``: the date) is tidied once
(an older release's slot was the evening digest's time; its run also lists the evening candidates it let go, ``expired``:
that feature is gone, 撤销 leaves them as they are). A record an older release wrote (``cursors``, a ``rules`` list per
project, why ``superseded``) is read the same: those fields are ignored, a ``superseded`` line counts as ``done``. ``view``
is what the evening digest card shows of the day's run (cards/digest.py memory_elements): how many entries went away and
why, with 撤销. Written under its own lock, never across a Feishu call (rule 1); the owner's words never in a log line.
Never raises into the digest or its worker."""
from __future__ import annotations

import secrets
import time
from pathlib import Path

from covey.core import memory, memory_gate, memory_usage, memstore, project_memory, projects
from covey.core.locks import resource_lock
from covey.core.loops import every
from covey.core.state import read_object, write_json

FILE = "memory-tidy.json"
SCHEMA = 1
RUNS_KEPT = 14
MAX_PROJECTS = 4
TIDIED = ("note", "decision")  # The kinds 整理记忆 may put away (core/memstore.py KINDS): never a rule.
KEPT_KINDS = ("constraint", "style")  # Lines of memory.json never put away, whatever they are read as: a limit someone set
# (负责人记的 too, one core/memstore.py reads as a 资料 when no turn carried it) and the style line.
IDLE, DONE, FOREIGN = "idle", "done", "foreign"  # Why a line went away.
TICK = 1800  # Seconds between the worker's looks at whether the day's run is done.


def path(state_dir) -> Path:
    return Path(state_dir) / FILE


def lock(state_dir):
    return resource_lock("memory-tidy", str(path(state_dir)))


def load(state_dir) -> dict:
    data = read_object(path(state_dir))
    runs = [run for run in data.get("runs") or () if isinstance(run, dict) and run.get("id")]
    return {"schema_version": SCHEMA, "runs": runs}


def _save(state_dir, data) -> None:
    write_json(path(state_dir), {"schema_version": SCHEMA, "runs": data["runs"][-RUNS_KEPT:]}, ensure_ascii=False, indent=1)


def day(now) -> str:
    """The slot of the day ``now`` is in (local time): a day is tidied once."""
    return time.strftime("%Y-%m-%d", time.localtime(now))


def carried_lines(state_dir, project_id) -> list:
    """The project's entries in use 整理记忆 may put away (``TIDIED``: its 资料 and 决定, never a 规矩), each {id, ref, kind, at,
    text, origin, legacy}: ``legacy`` for a line of memory.json (core/project_memory.py settle puts it away), else a decision
    of decisions.jsonl (core/memstore.py settle)."""
    rows = [row for row in project_memory.stored(state_dir, project_id) if not row["deleted"]]
    stored = {row["id"]: row for row in rows if row["kind"] not in KEPT_KINDS}
    held = {row["id"] for row in rows if row["kind"] in KEPT_KINDS}
    found = []
    for entry in memstore.entries(state_dir, scope="project", owner_key=project_id, kinds=TIDIED, statuses=("active",)):
        store, _, entry_id = memstore.parse_ref(entry.ref)
        legacy = entry_id in stored  # A line of memory.json (an old answer's id is its card's token and place).
        if entry_id in held or (not legacy and store != "decision"):
            continue  # A limit or the style line; or neither memory.json's nor decisions.jsonl's: nothing here puts it away.
        found.append({"id": entry_id, "ref": entry.ref, "kind": entry.kind, "at": entry.at, "text": entry.text,
                      "origin": dict(entry.origin or {}), "legacy": legacy})
    return found


def refs_of(line, project_id) -> list:
    """The refs a line's use is counted under: its own, and for a line of memory.json the one core/memory.py gave it."""
    return list(dict.fromkeys([line["ref"], *([f"project:{project_id}:{line['id']}"] if line.get("legacy") else [])]))


def foreign(line, known, project_id) -> list:
    """The other projects an answer ``line``'s question (its id and the line up to the answer) names an entry of by its id
    (core/project_memory.py quoted, ``known`` its ids); [] for a line that is not an answer (a decision), or whose answer alone
    names one (that is the owner choosing to follow another project's line here)."""
    if line.get("kind") != "decision":
        return []
    asked = f"{(line.get('origin') or {}).get('item') or ''} {str(line.get('text') or '').split('→', 1)[0]}"
    return project_memory.quoted(known, project_id, asked)


def carried(line, done, project_id) -> bool:
    """Whether an answer ``line``'s question was carried out on a line other than itself (``done`` is core/project_memory.py
    carried_out's)."""
    origin = line.get("origin") or {}
    if line.get("kind") != "decision" or not origin.get("question"):
        return False
    refs = set()
    for key in (origin.get("item"), origin.get("index")):
        if key:
            refs |= done.get((origin["question"], key), set())
    return bool(refs - {f"{project_id}:{line['id']}"})


class MemoryTidy:
    """整理记忆's worker (``threads``, app/unified.py features) and the digest's hook (app/digest.py ``memory_tidy``):
    ``runtime`` for the log and the shutdown."""

    def __init__(self, runtime):
        self.runtime = runtime

    def threads(self):
        """Its worker, only while COVEY_MEMORY is on (app/unified.py worker_targets): the day's run, once a day."""
        if not memory.enabled():
            return ()
        return (("memory-tidy", lambda ctx: every(self.runtime, TICK, self.tick, args=(ctx,), isolate="memory-tidy")),)

    def tick(self, ctx, now=None) -> dict:
        """The day's run, tidied first when it was not (the module's docstring); {} with the memory off. Never raises."""
        if not memory.enabled():
            return {}
        now = time.time() if now is None else now
        try:
            return self.run(ctx, None, day(now), now)
        except Exception as exc:  # noqa: BLE001 - the next tick tries again
            self.runtime.log(f"memory tidy failed: {type(exc).__name__}")
            return {}

    def view(self, ctx, chat, slot, now=None) -> dict:
        """The day's run as the evening digest shows it (``slot``, the digest's, is not the run's: a day is tidied once,
        whoever comes first), tidied first when it was not; {} when nothing was put in order. Never raises."""
        try:
            return view_of(self.tick(ctx, now))
        except Exception as exc:  # noqa: BLE001 - the digest goes out without it
            self.runtime.log(f"memory tidy failed: {type(exc).__name__}")
            return {}

    def run(self, ctx, chat, slot, now=None) -> dict:
        """The run of ``slot``: the one kept when there is one, else tidied now (every project not done or cancelled, of
        ``chat`` alone when given) and kept. Under the run's lock, so the worker and the digest never tidy the same slot twice."""
        now = time.time() if now is None else now
        state = Path(ctx.state_dir)
        with lock(state):
            kept = next((run for run in load(state)["runs"] if run.get("slot") == slot), None)
            if kept is not None:
                return kept
            run = self._tidy(state, chat, slot, now)
            data = load(state)
            data["runs"].append(run)
            _save(state, data)
        return run

    def _tidy(self, state, chat, slot, now) -> dict:
        usage = memory_usage.idle_view(state)  # Both use files (core/memory_usage.py counts).
        found = []
        live = [record for record in projects.records(state, chat or None)
                if record.get("state") not in (projects.DONE, projects.CANCELLED)]
        sized = sorted(((record, carried_lines(state, record["id"])) for record in live), key=lambda item: -len(item[1]))
        memories = project_memory.across(state)
        ids, done = project_memory.ids(memories), project_memory.carried_out(memories)
        for record, tidied in sized[:MAX_PROJECTS]:
            if not tidied:
                continue
            pid = record["id"]
            changes, decided = [], []
            for line in tidied:
                reason = (FOREIGN if foreign(line, ids, pid) else DONE if carried(line, done, pid)
                          else IDLE if all(memory_usage.idle(usage, ref, line["at"], now=now) for ref in refs_of(line, pid))
                          else "")
                if not reason:
                    continue
                if line["legacy"]:
                    changes.append({"id": line["id"], "valid": "done", "why": reason})
                else:
                    decided.append([line["ref"], reason])
            why = {change["id"]: change["why"] for change in reversed(changes)}
            before_logged = self._snapshots(state, pid, changes)
            settled = project_memory.settle(state, pid, changes, now=now) if changes else []
            self._record(state, pid, settled, why, before_logged, now)
            decided = [[ref, reason] for ref, reason in decided if self._settle(state, ref, reason)]
            found.append({"id": pid, "name": str(record.get("name") or ""),
                          "settled": [[entry, before, why.get(entry, DONE)] for entry, before in settled],
                          **({"decided": decided} if decided else {})})
            self.runtime.log(f"memory tidy project={pid} put_away={len(settled) + len(decided)}")
        return {"id": "t" + secrets.token_hex(4), "slot": slot, "chat_id": chat or "", "at": now,
                "projects": [item for item in found if item["settled"] or item.get("decided")]}

    def _settle(self, state, ref, reason) -> bool:
        """A decision of decisions.jsonl done (core/memory_gate.py revise: its own line of 记录, which the page leaves out as
        it does every answer's); whether it was."""
        try:
            memory_gate.revise(state, ref, "settle", proof=None, by="butler", source=f"tidy:{reason}")
            return True
        except (OSError, ValueError) as exc:
            self.runtime.log(f"memory tidy settle failed: {type(exc).__name__}")
            return False

    @staticmethod
    def _snapshots(state, pid, changes) -> dict:
        """{line id: (ref, core/memstore.py ``snapshot``)} of the lines ``changes`` name, before they are put away (the change
        log's ``before``)."""
        found = {}
        for change in changes:
            try:
                entry = memstore.find(state, f"project:{pid}:{change['id']}")
            except (OSError, ValueError):
                entry = None
            if entry is not None:
                found[change["id"]] = (entry.ref, memstore.snapshot(entry))
        return found

    def _record(self, state, pid, settled, why, before, now) -> None:
        """What was put away, one line in 记录 (core/memstore.py ``log_change``, as ``archive``) with the entries as they were and
        are, so that line's own 撤销 can put them back; never raises."""
        try:
            done = [entry for entry, _ in settled if entry in before]
            if not done:
                return
            after = {before[entry][0]: memstore.snapshot(memstore.find(state, before[entry][0])) for entry in done}
            memstore.log_change(state, who="butler", action="archive", refs=[before[entry][0] for entry in done],
                                title="整理记忆收起了几条", before={before[entry][0]: before[entry][1] for entry in done},
                                after=after, because={"reason": sorted({why.get(entry, DONE) for entry in done})}, now=now)
        except Exception as exc:  # noqa: BLE001 - the record is not the tidying
            self.runtime.log(f"memory tidy log failed: {type(exc).__name__}")

    def decide(self, ctx, run_id, op, *, now=None) -> dict | None:
        """A click on the digest's 整理记忆 (the owner's): ``op`` undo (撤销: every line the run put away back). The run as it is
        then; None when there is none. A run undone before, or any other ``op`` (the
        retired 确认长期 and 不对), is left as it is (a click delivered twice)."""
        state = Path(ctx.state_dir)
        with lock(state):
            run = next((run for run in load(state)["runs"] if run.get("id") == run_id), None)
        if run is None or op != "undo" or run.get("undone"):
            return run
        for item in run.get("projects") or ():
            try:
                project_memory.unsettle(state, item["id"], [(entry_id, before) for entry_id, before, _ in item.get("settled") or ()],
                                        now=now)
            except KeyError:
                pass
            for ref, _ in item.get("decided") or ():
                entry = memstore.find(state, ref)
                if entry is not None and entry.status == "done":  # Not one changed since (voided, answered anew).
                    memstore.restore(state, ref, who="owner", because={"source": "tidy_undo"}, now=now)
        with lock(state):
            data = load(state)
            for old in data["runs"]:
                if old.get("id") == run_id:
                    old["undone"] = True
                    run = old
            _save(state, data)
        self.runtime.log(f"memory tidy undo run={run_id}")
        return run


def view_of(run) -> dict:
    """What the digest card shows of ``run``: {run, away {idle, done, foreign}, undone}; {} when it put nothing in order. A line
    an older release put away as ``superseded`` counts as ``done``."""
    if not isinstance(run, dict):
        return {}
    away = {IDLE: 0, DONE: 0, FOREIGN: 0}
    for item in run.get("projects") or ():
        for why in [why for _, _, why in item.get("settled") or ()] + [why for _, why in item.get("decided") or ()]:
            away[why if why in away else DONE] += 1
    if not sum(away.values()):
        return {}
    return {"run": run.get("id") or "", "away": away, "undone": bool(run.get("undone"))}
