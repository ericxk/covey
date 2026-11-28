"""整理记忆, once a day (COVEY_MEMORY_LEARN=1 with COVEY_MEMORY=1 and 早晚汇总, docs/butler.md 记忆自己长): at the evening
digest (app/digest.py daily, the day's last time) what the memory holds is put in order by rules alone (no model), so a
project's 当前决定 are not a pile of every answer the owner ever gave.

For each project of the chat not done or cancelled, the ones with the most lines first (at most MAX_PROJECTS a night, the
others the next), of its lines a turn carries (core/memory.py describe: 当前决定 and 待核对参考; never a 长期约定 the owner
holds, never the style line):

1. What is not this project's matter goes (core/project_memory.py across): an answer whose question names an entry of another
   project's memory by its id (``FOREIGN``: a card that sorted out that project's memory, as a migration asks how each of its
   lines counts; what the owner chose holds in those lines), and an answer whose question a correction of some line names in
   its origin (``DONE``: it was carried out on that line).
2. 用到才留: a line written IDLE_DAYS ago that no turn used since (core/memory_usage.py idle) goes to the history.
3. What the rules say is done: core/project_memory.py settle (only the ids it was given, only those parts). Nothing is
   deleted: a line put away is read back by 查记忆 (core/memory_search.py) and 撤销 puts it back. What is put away is also one
   line of 记录 (core/memstore.py ``log_change``, with the entries as they were).

The record, <state>/memory-tidy.json: {schema_version, runs: [{id, slot, chat_id, at, projects: [{id, name, settled: [[entry,
before, why]], failed?}], undone?}]}, the last RUNS_KEPT; a slot is tidied once (an older release's run also lists the evening
candidates it let go, ``expired``: that feature is gone, 撤销 leaves them as they are). A record an older
release wrote (``cursors``, a ``rules`` list per project, why ``superseded``) is read the same: those fields are ignored, a
``superseded`` line counts as ``done``. ``view`` is what the evening digest card shows of a run (cards/digest.py
memory_elements): how many lines went away and why, with 撤销. Written under its own lock, never across a Feishu call
(rule 1); the owner's words never in a log line. Never raises into the digest."""
from __future__ import annotations

import secrets
import time
from pathlib import Path

from covey.core import memory, memory_usage, memstore, project_memory, projects
from covey.core.locks import resource_lock
from covey.core.state import read_object, write_json

FILE = "memory-tidy.json"
SCHEMA = 1
RUNS_KEPT = 14
MAX_PROJECTS = 4
TIDIED = (memory.DECISION, memory.REFERENCE)  # The parts 整理记忆 may put away.
IDLE, DONE, FOREIGN = "idle", "done", "foreign"  # Why a line went away.


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


def carried_lines(state_dir, project_id) -> list:
    """The project's lines a turn carries in the parts 整理记忆 may put away, each its entry with ``describe``'s ``part`` and its
    ``ref``."""
    tidied = []
    for row in project_memory.stored(state_dir, project_id):
        if row["deleted"] or row["kind"] == "style":
            continue
        facts = memory.describe(row)
        if facts["part"] in TIDIED:
            tidied.append({**row, "part": facts["part"], "ref": f"project:{project_id}:{row['id']}"})
    return tidied


def foreign(line, known, project_id) -> list:
    """The other projects an answer ``line``'s question (its id and the line up to the answer) names an entry of by its id
    (core/project_memory.py quoted, ``known`` its ids); [] for a line that is not an answer, or whose answer alone names one
    (that is the owner choosing to follow another project's line here)."""
    if line.get("source") != "owner_answer":
        return []
    asked = f"{(line.get('origin') or {}).get('item') or ''} {str(line.get('text') or '').split('→', 1)[0]}"
    return project_memory.quoted(known, project_id, asked)


def carried(line, done, project_id) -> bool:
    """Whether an answer ``line``'s question was carried out on a line other than itself (``done`` is core/project_memory.py
    carried_out's)."""
    origin = line.get("origin") or {}
    if line.get("source") != "owner_answer" or not origin.get("question"):
        return False
    refs = set()
    for key in (origin.get("item"), origin.get("index")):
        if key:
            refs |= done.get((origin["question"], key), set())
    return bool(refs - {f"{project_id}:{line['id']}"})


class MemoryTidy:
    """The digest's hook (app/digest.py ``memory_tidy``, set by app/unified.py): ``runtime`` for the log."""

    def __init__(self, runtime):
        self.runtime = runtime

    def view(self, ctx, chat, slot, now=None) -> dict:
        """The run of ``slot`` as the evening digest shows it, tidied first when it was not (the module's docstring); {} when
        nothing was put in order. Never raises."""
        try:
            with lock(ctx.state_dir):
                run = next((run for run in load(ctx.state_dir)["runs"] if run.get("slot") == slot), None)
            if run is None:
                run = self.run(ctx, chat, slot, now)
            return view_of(run)
        except Exception as exc:  # noqa: BLE001 - the digest goes out without it
            self.runtime.log(f"memory tidy failed: {type(exc).__name__}")
            return {}

    def run(self, ctx, chat, slot, now=None) -> dict:
        now = time.time() if now is None else now
        state = Path(ctx.state_dir)
        usage = memory_usage.idle_view(state)  # Both use files: a turn carrying a line now writes only memory/usage.json.
        found = []
        live = [record for record in projects.records(state, chat) if record.get("state") not in (projects.DONE, projects.CANCELLED)]
        sized = sorted(((record, carried_lines(state, record["id"])) for record in live), key=lambda item: -len(item[1]))
        memories = project_memory.across(state)
        ids, done = project_memory.ids(memories), project_memory.carried_out(memories)
        for record, tidied in sized[:MAX_PROJECTS]:
            if not tidied:
                continue
            pid = record["id"]
            changes = []
            for line in tidied:
                reason = (FOREIGN if foreign(line, ids, pid) else DONE if carried(line, done, pid)
                          else IDLE if memory_usage.idle(usage, line["ref"], line["at"], now=now) else "")
                if reason:
                    changes.append({"id": line["id"], "valid": "done", "why": reason})
            why = {change["id"]: change["why"] for change in reversed(changes)}
            before_logged = self._snapshots(state, pid, changes)
            settled = project_memory.settle(state, pid, changes, now=now) if changes else []
            self._record(state, pid, settled, why, before_logged, now)
            found.append({"id": pid, "name": str(record.get("name") or ""),
                          "settled": [[entry, before, why.get(entry, DONE)] for entry, before in settled]})
            self.runtime.log(f"memory tidy project={pid} put_away={len(settled)}")
        run = {"id": "t" + secrets.token_hex(4), "slot": slot, "chat_id": chat, "at": now,
               "projects": [item for item in found if item["settled"]]}
        with lock(state):
            data = load(state)
            if not any(old.get("slot") == slot for old in data["runs"]):
                data["runs"].append(run)
                _save(state, data)
        return run

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
                continue
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
        for _, _, why in item.get("settled") or ():
            away[why if why in away else DONE] += 1
    if not sum(away.values()):
        return {}
    return {"run": run.get("id") or "", "away": away, "undone": bool(run.get("undone"))}
