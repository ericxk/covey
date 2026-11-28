"""One-time migrations of old records, so the code that reads records needs no compat branch for them.

``migrate`` runs at the Bridge's start before any event is handled (app/unified.py) and in maintenance
``--apply`` (core/maintenance.py ``maintain``), so a deployer who skips releases is migrated before any reader
runs. Each step is marked done in <state>/migrations.json once every record it touches is done; from then on
it is one read of that file. Until then it runs again from the top: each file is written whole (an atomic
replace) under the lock its writer holds, and only when it is still missing, so a run cut short resumes where
it stopped. Nobody sees a change: the records keep what older releases read.

- embedded-trace: a job record from before traces/ (ff57531) embeds its process snapshot (``trace_snapshot``;
  ``tool_steps``, ``agents`` and ``child_steps`` were only ever written beside it). It goes to
  traces/<key>.jsonl (trace_store.save_embedded) unless load_trace finds one there; load_trace then reads
  only the file. No release since ff57531 writes ``trace_snapshot`` into a record.
- desk-retire: a front-desk topic (前台话题: one the butler opened with main-thread.json's desk_topics on in
  an older release, registered by an empty file <state>/main-thread/desk/<topic_id>) is frozen: its topic.json gets
  ``retired: "desk"`` (core/topics.py RETIRED_DESK), every field it had kept, and nothing goes into it from then on
  (core/topics.py retired_topic). The registry stays as it is; no current release adds to it.
- project-joined: a project's record says when each member topic came in (``joined``, core/projects.py add_member).
  An older record has none: a member gets the start of its first task the 专项卡 counted (``token_jobs``:
  the tasks that ended once it was in). With none counted, no task of it has ended since it came in: it gets the time of the
  migration, unless ``token_jobs`` is full (its oldest keys dropped), then the project's ``created_at``. The web page's
  专项报告 counts a member's turns from then, as the 专项卡 does; without it the turns of members from before they came in
  were counted too.
- memory_v2 (docs/operations.md 记忆的存储与迁移, ``RECURRING``): the global memory an older release keeps (principles.json,
  knowledge/) taken into memory/global.json and 待审核 (core/memstore.py sync_legacy), and each changed project's lines given
  their codes. A lead's line puts nothing in 待审核 any more: what an earlier run kept a rule stays one (负责人记的). The first run
  copies those files to backups/memory-v2-<time>/ first. Not done once and for all: what an older release writes after a
  rollback is taken at the next start, so each start compares a few stats (the global files, each project's memory.json)
  with what the last run saw and runs again only when one changed. No topic directory is read: a topic's lines get their
  codes the first time something reads them.

The steps of ``HOME_STEPS`` need the home, the service's ``--workdir``: they run at the Bridge's start, which passes it
(``migrate(..., workdir=)``), before anything else; maintenance ``--apply`` has none and leaves them to it, and its
``--dry-run`` (``pending``) does not list them (docs/configuration.md 主目录与仓库地图).
Each is W1's ``migrate_v1``: it writes a file only when it is missing, so an older release (which reads neither file) and a
run cut short are safe, and a step that raised is run again at the next start.

- home_v1: a home Covey made gets ``home.json``'s defaults (core/home.py migrate_v1); a taken-over one none.
- repos_v1: ``repos.json`` from the old workspace list and the git repositories one level under the home, then ``repos.md``
  (core/repomap.py migrate_v1). The old files stay; a workspace an older release adds after a rollback is not in the map
  (the settings page adds it).
"""
# compat(ff57531): the one place that reads a job record's embedded process snapshot; retire when every job record
# that embeds one has its traces/ file in every deployment (compat_report: embedded-trace)
from __future__ import annotations

import functools
import shutil
import time
from pathlib import Path

from covey.core import memstore, projects
from covey.core.jobs import find_job_record, read_job_record
from covey.core.locks import LOCK, resource_lock
from covey.core.state import epoch_seconds, read_object, write_json
from covey.core.trace_store import has_trace, save_embedded

MIGRATIONS = "migrations.json"
EMBEDDED_TRACE = "embedded-trace"
DESK_RETIRE = "desk-retire"
PROJECT_JOINED = "project-joined"
HOME_V1 = "home_v1"
REPOS_V1 = "repos_v1"
MEMORY_V2 = "memory_v2"
MEMORY_BACKUP = "memory-v2-"  # backups/memory-v2-<time>/: the memory files as the first memory_v2 run found them.
DESK_REGISTRY = Path("main-thread") / "desk"  # One empty file per front-desk topic id (older releases).
RETIRED, RETIRED_DESK = "retired", "desk"  # topic.json's key and value (core/topics.py RETIRED_DESK).
FAILED_KEYS = 20  # Keys named in the summary line per step.


def done(state_dir: Path) -> dict:
    """The steps marked done in ``state_dir`` (name -> what it did)."""
    return {name: value for name, value in read_object(Path(state_dir) / MIGRATIONS).items()
            if (name in STEPS or name in HOME_STEPS) and isinstance(value, dict) and value.get("done")}


def _mark(state_dir: Path, name: str, value: dict) -> None:
    path = Path(state_dir) / MIGRATIONS
    with resource_lock("migrations", str(path.absolute())):
        write_json(path, {**read_object(path), name: value}, ensure_ascii=False, indent=2, sort_keys=True)


def _embedded(state_dir: Path):
    """(key, snapshot) of each job record that embeds a process snapshot."""
    folder = Path(state_dir) / "jobs"
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else ():
        record = read_job_record(path) if not path.name.startswith(".") else None
        snapshot = (record or {}).get("trace_snapshot")
        if snapshot and isinstance(snapshot, dict) and record.get("key"):
            yield str(record["key"]), snapshot


def embedded_traces(state_dir: Path) -> tuple[int, list[str]]:
    """Write each embedded process snapshot to its file: (files written, keys of the records it could not
    write). A record it could not write shows an empty process panel until a later run writes it, so the
    summary line names it; one that fails for any reason (a full disk, a snapshot of an odd shape) does not
    stop the Bridge's start."""
    written, failed = 0, []
    for key, snapshot in _embedded(state_dir):
        try:
            written += save_embedded(Path(state_dir), key, snapshot)
        except Exception:
            failed.append(key)
    return written, failed


def pending(state_dir: Path) -> dict:
    """What the steps not marked done would still do (step -> records), reading only: maintenance --dry-run."""
    state_dir = Path(state_dir)
    finished = done(state_dir)
    return {name: still(state_dir) for name, (_, still) in STEPS.items()
            if name not in finished or (name in RECURRING and still(state_dir))}


def _unwritten(state_dir: Path) -> int:
    """The embedded snapshots without their traces/ file yet."""
    return sum(1 for key, _ in _embedded(state_dir) if not _has_file(state_dir, key))


def _has_file(state_dir: Path, key: str) -> bool:
    try:
        return has_trace(state_dir, key)
    except OSError:
        return False


def _desk_topics(state_dir: Path):
    """(topic id, its topic.json path) of each registered front-desk topic whose topic.json is there."""
    folder = Path(state_dir) / DESK_REGISTRY
    for entry in sorted(folder.iterdir()) if folder.is_dir() else ():
        name = entry.name
        if name.startswith(".") or "/" in name or not entry.is_file():
            continue
        path = Path(state_dir) / "topics" / name / "topic.json"
        if path.is_file():
            yield name, path


def retire_desk_topics(state_dir: Path) -> tuple[int, list[str]]:
    """Mark each registered front-desk topic's topic.json retired (``RETIRED``), once: (files written, ids of the
    topics it could not write). One already marked, or whose topic.json is gone or unreadable (no route reaches
    it), is left alone."""
    written, failed = 0, []
    for topic_id, path in _desk_topics(state_dir):
        try:
            with LOCK:  # The lock topic.json's writers hold (core/topics.py bind_topic).
                data = read_object(path)
                if not data or data.get(RETIRED) == RETIRED_DESK:
                    continue
                write_json(path, {**data, RETIRED: RETIRED_DESK}, ensure_ascii=False, indent=2)
            written += 1
        except Exception:
            failed.append(topic_id)
    return written, failed


def _desk_left(state_dir: Path) -> int:
    """The registered front-desk topics not marked retired yet."""
    return sum(1 for _, path in _desk_topics(state_dir)
               if (data := read_object(path)) and data.get(RETIRED) != RETIRED_DESK)


def steps(workdir=None) -> dict:
    """The steps a run takes, in order: ``STEPS``, then ``HOME_STEPS`` given ``workdir`` (the home), none of them without."""
    if not workdir:
        return dict(STEPS)
    return {**STEPS, **{name: (functools.partial(run, workdir=workdir), still) for name, (run, still) in HOME_STEPS.items()}}


def migrate(state_dir: Path, log=None, *, workdir=None) -> dict:
    """Run the steps not marked done in ``state_dir``; what each did. ``log`` gets one summary line when one ran,
    naming the first records a step could not write. ``workdir``: the home, for ``HOME_STEPS`` (the Bridge's start)."""
    state_dir = Path(state_dir)
    finished = done(state_dir)
    summary, unwritten = {}, {}
    started = time.monotonic()
    for name, (run, still) in steps(workdir).items():
        if name in finished and (name not in RECURRING or not still(state_dir)):
            continue
        written, failed = run(state_dir)
        summary[name] = {"written": written, "failed": len(failed)}
        if failed:
            unwritten[name] = failed
        else:
            _mark(state_dir, name, {"done": True, "at": int(time.time()), "written": written})
    if summary and log is not None:
        log("record migrations " + " ".join(f"{name}:" + ",".join(f"{key}={value}" for key, value in facts.items())
                                             for name, facts in summary.items())
            + f" elapsed_ms={(time.monotonic() - started) * 1000:.0f}"
            + "".join(f" {name}:failed_keys=" + ",".join(keys[:FAILED_KEYS])
                      + (f",+{len(keys) - FAILED_KEYS}" if len(keys) > FAILED_KEYS else "")
                      for name, keys in unwritten.items()))
    return summary


def _unjoined(state_dir: Path) -> list:
    """[(project record, its member topics with no ``joined`` time)] of the projects that have such a member."""
    found = []
    for record in projects.records(state_dir):
        missing = [topic for topic in record.get("threads") or () if projects.joined_at(record, topic) is None]
        if missing:
            found.append((record, missing))
    return found


def _joined_left(state_dir: Path) -> int:
    return len(_unjoined(state_dir))


def stamp_joined(state_dir: Path) -> tuple[int, list[str]]:
    """``joined`` for the members a project's record has no time for: (records written, ids of those it could not write)."""
    written, failed = 0, []
    for record, missing in _unjoined(state_dir):
        first = {}
        for key in record.get("token_jobs") or ():
            job = find_job_record(state_dir, str(key)) or {}
            topic, began = job.get("topic_id"), epoch_seconds(job.get("created_at"))
            if topic in missing and began and began < first.get(topic, float("inf")):
                first[topic] = began
        full = len(record.get("token_jobs") or ()) >= projects.TOKEN_JOBS_KEPT
        start = (record.get("created_at") if isinstance(record.get("created_at"), (int, float)) else 0) if full else time.time()

        def change(current, first=first, start=start):
            joined = current.get("joined") if isinstance(current.get("joined"), dict) else {}
            added = {topic: round(first.get(topic) or start, 3) for topic in current.get("threads") or ()
                     if projects.joined_at(current, topic) is None and (first.get(topic) or start)}
            if added:
                current["joined"] = {**joined, **added}
        try:
            projects.update(state_dir, record["id"], change)
            written += 1
        except Exception:  # noqa: BLE001 - tried again at the next start
            failed.append(str(record.get("id")))
    return written, failed


def _memory_left(state_dir: Path) -> int:
    """What memory_v2 would take: 1 for the global files an older release changed since the last run, one more for each
    project whose memory.json changed (a few stats, whatever the number of topics)."""
    seen = memstore.global_path(state_dir).is_file() and read_object(memstore.global_path(state_dir)).get("projects_seen")
    if not isinstance(seen, dict):
        return 1
    now = memstore.project_sources(state_dir)
    return int(memstore.legacy_changed(state_dir)) + sum(1 for pid, sig in now.items() if seen.get(pid) != sig)


MEMORY_FILES = ("principles.json", "knowledge", "memory-candidates.json", "memory-usage.json")


def _memory_backup(state_dir: Path) -> Path:
    """The memory files as they are (the global ones, each project's memory*.json and answers are untouched anyway) copied
    to backups/memory-v2-<time>/; its path."""
    target = state_dir / "backups" / (MEMORY_BACKUP + time.strftime("%Y%m%d-%H%M%S"))
    if not any((state_dir / name).exists() for name in MEMORY_FILES) and not memstore.project_sources(state_dir):
        return target  # A deployment with no memory yet: nothing to keep.
    target.mkdir(parents=True, exist_ok=True)
    for name in MEMORY_FILES:
        source = state_dir / name
        if source.is_dir():
            shutil.copytree(source, target / name, dirs_exist_ok=True)
        elif source.is_file():
            shutil.copy2(source, target / name)
    for pid in memstore.project_sources(state_dir):
        folder = projects.folder(state_dir) / pid
        for file in sorted(folder.glob("memory*.json")) if folder.is_dir() else ():
            (target / "projects" / pid).mkdir(parents=True, exist_ok=True)
            shutil.copy2(file, target / "projects" / pid / file.name)
    return target


def memory_v2(state_dir: Path, *, now=None) -> tuple[int, list[str]]:
    """The memory_v2 step (the module's docstring): (entries and review items written, the parts it could not do)."""
    state_dir = Path(state_dir)
    stamp = time.time() if now is None else now
    written, failed = 0, []
    first = not memstore.global_path(state_dir).is_file()
    if first:
        try:
            _memory_backup(state_dir)
        except OSError:
            return 0, ["backup"]  # Nothing is migrated before the files are kept.
    try:
        found = memstore.sync_legacy(state_dir, now=stamp)
        written += sum(found.values())
    except Exception:  # noqa: BLE001 - tried again at the next start
        failed.append("global")
    seen = read_object(memstore.global_path(state_dir)).get("projects_seen")
    seen = seen if isinstance(seen, dict) else {}
    sources = memstore.project_sources(state_dir)
    changed = [pid for pid, sig in sources.items() if seen.get(pid) != sig]
    for pid in changed:
        try:
            memstore.entries(state_dir, scope="project", owner_key=pid, statuses=None)  # Its codes given now.
            seen[pid] = memstore.project_sources(state_dir).get(pid)
        except Exception:  # noqa: BLE001 - tried again at the next start
            failed.append(pid)
    try:
        memstore.mark_migrated(state_dir, projects_seen={pid: seen[pid] for pid in sources if pid in seen})
    except OSError:
        failed.append("mark")
    return written, failed


def _home_left(state_dir: Path) -> int:
    """1 while ``home.json`` is missing (a taken-over home keeps it missing: the Bridge's start marks the step done)."""
    from covey.core import home
    return int(not home.configured(state_dir))


def home_v1(state_dir: Path, *, workdir) -> tuple[int, list[str]]:
    """core/home.py migrate_v1: (1 when it wrote ``home.json``, [] or the file when it raised)."""
    from covey.core import home
    had = home.configured(state_dir)
    try:
        home.migrate_v1(state_dir, workdir)
    except Exception:  # noqa: BLE001 - tried again at the next start
        return 0, ["home.json"]
    return int(not had and home.configured(state_dir)), []


def _repos_left(state_dir: Path) -> int:
    """1 while ``repos.json`` is missing."""
    from covey.core import repomap
    return int(not repomap.path(state_dir).exists())


def repos_v1(state_dir: Path, *, workdir) -> tuple[int, list[str]]:
    """core/repomap.py migrate_v1: (1 when it wrote ``repos.json``, [] or the file when it raised)."""
    from covey.core import repomap
    had = repomap.path(state_dir).exists()
    try:
        repomap.migrate_v1(state_dir, workdir)
    except Exception:  # noqa: BLE001 - tried again at the next start
        return 0, ["repos.json"]
    return int(not had and repomap.path(state_dir).exists()), []


# The steps in the order they run: name -> (what it does: files written and the keys of the records it could not
# write; what it would still do: records, reading only). A new step is one more row: of STEPS when it needs the state
# directory alone, of HOME_STEPS (its ``run`` given ``workdir=``) when it needs the home too.
STEPS = {EMBEDDED_TRACE: (embedded_traces, _unwritten), DESK_RETIRE: (retire_desk_topics, _desk_left),
         PROJECT_JOINED: (stamp_joined, _joined_left), MEMORY_V2: (memory_v2, _memory_left)}
HOME_STEPS = {HOME_V1: (home_v1, _home_left), REPOS_V1: (repos_v1, _repos_left)}
# Steps that run again whenever what they would do is not nothing (``still``), done or not: what an older release wrote
# after a rollback is taken again.
RECURRING = frozenset({MEMORY_V2})
