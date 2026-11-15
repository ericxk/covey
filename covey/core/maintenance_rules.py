"""The rules of daily maintenance: each kind of stored data the plan bounds (RULES), what it selects, how the
cleanup carries it out (CLEANUP, in that order) and its part of --dry-run (DRY_RUN), with the helpers they share."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import stat
import time
from functools import cached_property
from pathlib import Path

from covey.core.backup import backup_paths, backup_sets, old_backups
from covey.core.history_storage import HISTORY_LOCK
from covey.core.jobs import (FINISHED_UNREAD_FIELDS, job_file, read_job_record, slim_finished_job_record,
                                      upsert_job_record)
from covey.core.locks import LOCK, remove_unused_lock
from covey.core.maintenance_report import mib, stale_lines, tree_bytes
from covey.core.process import paths_in_use
from covey.core.proxy import BATCH_TRANSITIONS
from covey.core.run_dir import RUNS, runner_alive, runner_info
from covey.core.stale import STALE_DAYS, stale_records
from covey.core.state import DAY, read_json, read_object, write_private_text
from covey.core.statuses import NOT_ENDED
from covey.core.store import JOURNAL, JOURNALED, directory_store
from covey.core.trace_store import compress_trace, trace_path


# tmp/ and artifact-upload/ entries, runs/ of ended runners and record lock files, by last change.
TEMPORARY_DAYS = 7


JOURNAL_BYTES = 1024 * 1024  # About five days of record writes (core/store.py).


# job-diffs/: diff.md is uploaded to Drive with the task's artifacts; the local copy is bounded.
# Past DIFF_DAYS a diff goes even when its upload was never confirmed; the size cap never takes one.
DIFF_DAYS = 30


DIFF_BYTES = 200 * 1024 * 1024


# cards/ holds only the lock files of cards (core/locks.configure_card_locks): with no record beside them, each
# goes once no one holds it, and is made again by the next write.
LOCK_DIRS = ("jobs", "questions", "held-notes", "interaction-deliveries", "cards", "answer-cards")


# 代答 batches that ended (no state follows in BATCH_TRANSITIONS) go after this many days; open ones stay.
PROXY_BATCH_DAYS = 30


def archive_file(path):
    folder = path.parent / (path.name + ".archive")
    folder.mkdir(mode=0o700, exist_ok=True)
    raw = folder / (str(time.time_ns()) + ".jsonl")
    path.replace(raw)  # A crash here leaves a readable uncompressed archive.
    compressed = raw.with_suffix(raw.suffix + ".gz")
    temp = compressed.with_suffix(".tmp")
    with raw.open("rb") as source, gzip.open(temp, "wb") as target:
        shutil.copyfileobj(source, target)
    temp.chmod(0o600)
    temp.replace(compressed)
    raw.unlink()
    return str(compressed)


def _touched(path):
    """When ``path`` last changed, or for a regular file was last read too (as systemd-tmpfiles
    ages /tmp). A directory's read time is left out: this walk itself reads directories."""
    info = os.lstat(path)
    times = [info.st_mtime, info.st_ctime] + ([info.st_atime] if stat.S_ISREG(info.st_mode) else [])
    return max(times)


def _untouched_since(path, cutoff):
    """Nothing in ``path`` (a file or a whole tree, symlinks not followed) changed after ``cutoff``."""
    try:
        if _touched(path) >= cutoff:
            return False
        if path.is_symlink() or not path.is_dir():
            return True
        for folder, dirs, files in os.walk(path):
            for name in dirs + files:
                if _touched(os.path.join(folder, name)) >= cutoff:
                    return False
    except OSError:
        return False
    return True


def _fat_diff_index(path):
    """A diff.json from before each diff was stored once: it repeats the diff text diff.md holds."""
    with path.open("rb") as handle:
        return b'"diff_text"' in handle.read(64 * 1024)


# compat(30c3d17): diff.json files written before each diff was stored once, with _fat_diff_index and the plan's
# slim_diffs; retire when no job-diffs/*/diff.json repeats its diff text (compat_report: fat-diff-index)
def slim_diff_index(path):
    """Drop the repeated diff text from an older diff.json; its per-file stats stay."""
    data = read_json(path)
    for item in data.get("files", []):
        item.pop("diff_text", None)
        item.pop("inline_text", None)
    write_private_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def diff_uploaded(state, folder):
    """Whether the task's Drive upload took ``folder``/diff.md as it is now: its artifact-drive
    ledger (app.artifacts.Artifacts.upload_artifacts) holds the file with the same digest."""
    key = folder.name  # job-diffs/<job key>: a job key is already a slug.
    raw = read_object(state / "jobs" / (key + ".json")).get("diff_artifact_path")
    files = _upload_ledger(state, key).get("files")
    # Keyed by app.artifacts.artifact_key, the digest of the recorded path.
    entry = files.get(hashlib.sha256(raw.encode()).hexdigest()[:24]) if raw and isinstance(files, dict) else None
    if not isinstance(entry, dict) or not entry.get("sha256"):
        return False
    try:
        return hashlib.sha256((folder / "diff.md").read_bytes()).hexdigest() == entry["sha256"]
    except OSError:
        return False


def _upload_ledger(state, key):
    """The artifact-drive ledger of job ``key`` ({} if none), named by Voice.safe_key
    (feishu.delivery.safe_idempotency_key: a long key by its digest, after the Harness's prefix)."""
    name = re.sub(r"[^A-Za-z0-9_-]", "-", key)
    folder = state / "artifact-drive"
    paths = [folder / (name + ".json")] if len(name) <= 50 else \
        sorted(folder.glob("*-" + hashlib.sha1(key.encode()).hexdigest()[:24] + ".json"))
    return next((ledger for ledger in map(read_object, paths) if ledger), {})


def _finished(state, key):
    """Checked again just before a trace is gzipped: the task is not active and none of its task
    runners is alive (a job that starts now has its own key)."""
    record = read_job_record(job_file(state, key)) or {}
    run = state / RUNS / job_file(state, key).stem
    return record.get("status") not in NOT_ENDED and not runner_alive(run)


def _while_idle(idle, step=None, *args):
    """Run ``step`` under LOCK if no job is registered; False, and nothing run, otherwise.
    A submission registers its job under LOCK, so no job starts during the step."""
    with LOCK:
        if idle is not None and not idle():
            return False
        if step is not None:
            step(*args)
        return True


def _archive(path):
    with HISTORY_LOCK:
        if path.is_file():
            archive_file(path)


def _compress_checkpoint(state, key, path):
    if not path.is_file():
        return
    gz = path.with_suffix(".json.gz")
    with gzip.open(gz, "wb") as out:
        out.write(path.read_bytes())
    gz.chmod(0o600)
    upsert_job_record(state, key, checkpoint_path=str(gz))
    path.unlink()


def _remove_tree(path):
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)


FINISHED_BATCHES = frozenset(state for state, following in BATCH_TRANSITIONS.items() if not following)


def _ended_batch(path):
    return read_object(path).get("status") in FINISHED_BATCHES


class Run:
    """One maintenance pass over ``state``: what its rules read between them, once (the job records, the task
    runners in runs/), and ``idle`` for the cleanup (``_while_idle``)."""

    def __init__(self, state, now, days, *, history_bytes=None, idle=None):
        self.state, self.now, self.days, self.history_bytes, self.idle = state, now, days, history_bytes, idle

    def cutoff(self, days):
        """``days`` before now; None: the run's ``days``."""
        return self.now - (self.days if days is None else days) * DAY

    @cached_property
    def records(self):
        folder = self.state / "jobs"
        return [r for _, r in directory_store(folder).query()] if folder.is_dir() else []

    @cached_property
    def runners(self):
        """The job keys of the live task runners in runs/, and (directory, last change) of each ended one."""
        live, ended = set(), []
        folder = self.state / RUNS
        for run in sorted(folder.iterdir()) if folder.is_dir() else []:
            if not run.is_dir() or run.is_symlink():
                continue
            if runner_alive(run):
                info = runner_info(run)
                if isinstance(info, dict):
                    live.add(info.get("key"))
                continue
            # A finished task runner's directory: its record and process file stay in jobs/ and traces/.
            ended.append((run, max((p.stat().st_mtime for p in run.rglob("*") if p.is_file()),
                                   default=run.stat().st_mtime)))
        return live, ended

    @property
    def live(self):
        return self.runners[0]


class Rule:
    """One kind of stored data the plan bounds; RULES lists them all.

    ``select`` says what it would do now, reading only (``plan_maintenance``): the value of each of ``keys``.
    ``apply`` carries out what the plan holds for it (``_cleanup``), each item checked again; it returns False
    when a job started meanwhile, and the rest of the cleanup waits for the next day. ``report`` is its part of
    ``--dry-run``: by default ``title`` with the count and size, then each item with its size and age.
    ``days``, on a rule that selects by age: how long an item stays unchanged before the rule takes it (None:
    the run's ``days``, 30 by default)."""
    keys = ()  # The plan's keys it fills, in the plan's order.
    title = ""

    def select(self, run):
        return None

    def apply(self, run, plan):
        return True

    def names(self, plan):
        """The paths its dry-run section lists."""
        return [item if isinstance(item, str) else item[1] for item in plan[self.keys[0]]]

    def report(self, out, plan):
        return out.section(self.title, self.names(plan))


class _Locked(Rule):
    """A rule whose every item is one step under LOCK, taken only while no job is registered."""

    def apply(self, run, plan):
        return all(_while_idle(run.idle, self.step, run, item) for item in plan[self.keys[0]])


class History(_Locked):
    """A topic's history.jsonl and job-progress.jsonl, once at ``history_bytes`` or old, unless a job of it is
    active: archived (gzipped into <name>.archive/, still read)."""
    keys = ("archive",)
    days = None
    title = "归档历史（压缩进 .archive/）"

    def select(self, run):
        busy_topics = {r.get("topic_id") for r in run.records if r.get("status") in NOT_ENDED}
        found, cutoff = [], run.cutoff(self.days)
        for topic in (run.state / "topics").iterdir() if (run.state / "topics").exists() else []:
            if not topic.is_dir() or topic.name in busy_topics or topic.is_symlink():
                continue
            for name in ("history.jsonl", "job-progress.jsonl"):
                path = topic / name
                if (
                    path.is_file()
                    and not path.is_symlink()
                    and (path.stat().st_size >= run.history_bytes or path.stat().st_mtime < cutoff)
                ):
                    found.append(str(path))
        return found

    def step(self, run, name):
        _archive(Path(name))


class Checkpoints(_Locked):
    """The checkpoint of a job no longer active, in checkpoints/ and old: gzipped, its record pointed at the .gz."""
    keys = ("compress_checkpoints",)
    days = None
    title = "压缩检查点（转 .json.gz）"

    def select(self, run):
        found, cutoff = [], run.cutoff(self.days)
        for record in run.records:
            if record.get("status") in NOT_ENDED:
                continue
            path = Path(record.get("checkpoint_path") or "")
            if (
                path.suffix == ".json"
                and path.is_file()
                and path.resolve().is_relative_to(run.state / "checkpoints")
                and path.stat().st_mtime < cutoff
            ):
                found.append((record["key"], str(path)))
        return found

    def step(self, run, item):
        key, name = item
        _compress_checkpoint(run.state, key, Path(name))


class Traces(Rule):
    """The process record (traces/) of a finished task, no task runner of it alive, unchanged that long:
    gzipped under its writer's lock (trace_store.compress_trace). Kept as task records, not deleted."""
    keys = ("compress_traces",)
    days = None
    title = "压缩已结束任务的过程记录（traces/ 转 .jsonl.gz）"

    def select(self, run):
        folder = run.state / "traces"
        plain = {p.name for p in folder.glob("*.jsonl")} if folder.is_dir() else set()
        found, cutoff = [], run.cutoff(self.days)
        for record in run.records:
            key = record.get("key")
            if not key or key in run.live or record.get("status") in NOT_ENDED:
                continue
            path = trace_path(run.state, key)
            try:
                if path.name in plain and not path.is_symlink() and path.stat().st_mtime < cutoff:
                    found.append((key, str(path)))
            except OSError:
                continue  # Compressed or removed meanwhile.
        return sorted(found)

    def apply(self, run, plan):
        for key, _ in plan[self.keys[0]]:
            try:
                compress_trace(run.state, key, older_than=run.cutoff(self.days),
                               finished=lambda key=key: _finished(run.state, key))
            except OSError:
                pass  # The plain file stays readable; the next run tries again.


class SlimRecords(Rule):
    """A finished job's record that still has fields nobody reads once it ended (FINISHED_UNREAD_FIELDS): they go.
    Every task's first read of jobs/ parses each record whole: 26 MB, 0.3-0.8 s of a runner's start (S41)."""
    keys = ("slim_records",)
    title = "已结束任务记录删去不再读取的历史快照等字段"

    def select(self, run):
        return sorted((r["key"], str(job_file(run.state, r["key"]))) for r in run.records
                      if r.get("key") and r["key"] not in run.live and r.get("status")
                      and r["status"] not in NOT_ENDED
                      and any(name in r for name in FINISHED_UNREAD_FIELDS))

    def apply(self, run, plan):
        for key, _ in plan[self.keys[0]]:
            try:
                if _finished(run.state, key):
                    slim_finished_job_record(run.state, key)
            except OSError:
                pass  # Left whole; the next run tries again.


class Temporary(Rule):
    """An entry of the scratch folders with nothing in it changed or read that long, unless a process works in it
    or holds a file of it open (paths_in_use): removed."""
    keys = ("remove_temporary",)
    days = TEMPORARY_DAYS
    # proxy/shots: 代答's failed-step screenshots; main-thread/opened: which topic each main-thread topic_start opened
    # (app/main_topics.py), kept so the same call opens no second one; main-thread/relays: which topic turn's outcome
    # went back to the top level (app/main_relay.py), read for the RESULT_AGE the board and snapshot look back;
    # main-thread/dispatch: the 派发活卡片's records and conclusion notes (app/dispatch_cards.py); answer-cards: the cards an answer went out on, kept until their 产物 are uploaded (app/answer_cards.py).
    folders = ("tmp", "artifact-upload", "inbound-messages", "proxy/shots", "main-thread/opened", "main-thread/relays",
               "main-thread/dispatch", "answer-cards")
    title = "删除 tmp/、artifact-upload/、proxy/shots/ 条目"

    def select(self, run):
        found, cutoff = [], run.cutoff(self.days)
        for name in self.folders:
            folder = run.state / name
            old = [p for p in sorted(folder.iterdir()) if not p.is_symlink() and _untouched_since(p, cutoff)] \
                if folder.is_dir() else []
            used = paths_in_use(folder) if old else set()  # A process's cwd or open file: still in use.
            found += [str(p) for p in old if p not in used]
        return found

    def apply(self, run, plan):
        used = {}
        for name in plan[self.keys[0]]:
            path = Path(name)
            if path.parent not in used:
                used[path.parent] = paths_in_use(path.parent)
            if path not in used[path.parent] and _untouched_since(path, run.cutoff(self.days)):
                _remove_tree(path)


class Runs(Rule):
    """The runs/ directory of a task runner that is no longer alive, unchanged that long: removed."""
    keys = ("remove_runs",)
    days = TEMPORARY_DAYS
    title = "删除已结束任务进程的 runs/ 目录"

    def select(self, run):
        cutoff = run.cutoff(self.days)
        return [str(folder) for folder, newest in run.runners[1] if newest < cutoff]

    def apply(self, run, plan):
        for name in plan[self.keys[0]]:
            if not runner_alive(Path(name)):
                shutil.rmtree(name, ignore_errors=True)


class ProxyBatches(Rule):
    """A 代答 batch record that ended (no state follows in BATCH_TRANSITIONS) and is unchanged that long: removed.
    Open ones stay."""
    keys = ("remove_proxy_batches",)
    days = PROXY_BATCH_DAYS

    @property
    def title(self):
        return f"删除 {self.days} 天前已结束的代答批次"

    def select(self, run):
        folder = run.state / "proxy" / "batches"
        cutoff = run.cutoff(self.days)
        if not folder.is_dir():
            return []
        return [str(p) for p in sorted(folder.glob("*.json"))
                if not p.is_symlink() and _untouched_since(p, cutoff) and _ended_batch(p)]

    def apply(self, run, plan):
        for name in plan[self.keys[0]]:
            path = Path(name)
            if _ended_batch(path) and _untouched_since(path, run.cutoff(self.days)):
                path.unlink(missing_ok=True)


class Journals(Rule):
    """A store's journal (core/store.py) over JOURNAL_BYTES starts again: each store lists its directory once,
    then reads the new one. Not in the plan or the dry run."""

    def apply(self, run, plan):
        for name in JOURNALED:
            journal = run.state / name / JOURNAL
            try:
                if journal.stat().st_size > JOURNAL_BYTES:
                    journal.unlink()
            except OSError:
                pass


class Locks(Rule):
    """A lock file in LOCK_DIRS whose record is gone or unchanged that long: removed once no one holds it
    (locks.remove_unused_lock); the next write makes it again."""
    keys = ("remove_locks",)
    days = TEMPORARY_DAYS

    def select(self, run):
        found, cutoff = [], run.cutoff(self.days)
        for name in LOCK_DIRS:
            locks = run.state / name / ".locks"
            for lock in sorted(locks.glob("*.lock")) if locks.is_dir() else []:
                record = run.state / name / lock.name.removesuffix(".lock")
                try:
                    changed = record.stat().st_mtime
                except FileNotFoundError:
                    changed = 0
                if changed < cutoff:
                    found.append(str(record))
        return found

    def apply(self, run, plan):
        for name in plan[self.keys[0]]:
            remove_unused_lock(Path(name))

    def report(self, out, plan):
        by_dir = {}
        for name in plan["remove_locks"]:
            by_dir[Path(name).parent.name] = by_dir.get(Path(name).parent.name, 0) + 1
        return [f"删除锁文件：{len(plan['remove_locks'])} 个（.locks/ 里的空文件；"
                + "，".join(f"{k} {v}" for k, v in sorted(by_dir.items())) + "）"]


class Diffs(Rule):
    """job-diffs/ folders to remove: older than ``days``, then the oldest uploaded ones while over DIFF_BYTES; never
    one changed in the last day. The older diff.json files of the rest that repeat their diff text are slimmed."""
    keys = ("remove_diffs", "slim_diffs")
    days = DIFF_DAYS

    def select(self, run):
        remove, slim, rows = [], [], []
        folder = run.state / "job-diffs"
        for job in sorted(folder.iterdir()) if folder.is_dir() else []:
            if job.is_symlink() or not job.is_dir():
                continue
            files = [p for p in job.iterdir() if p.is_file() and not p.is_symlink()]
            changed = max((p.stat().st_mtime for p in files), default=job.stat().st_mtime)
            index = job / "diff.json"
            fat = index in files and _fat_diff_index(index)
            rows.append((changed, sum(p.stat().st_size for p in files if not (fat and p == index)), job, fat))
        total = sum(row[1] for row in rows)
        for changed, size, job, fat in sorted(rows, key=lambda row: row[0]):
            if changed < run.now - DAY and (changed < run.cutoff(self.days)
                                            or total > DIFF_BYTES and diff_uploaded(run.state, job)):
                remove.append(str(job))
                total -= size
            elif fat:
                slim.append(str(job / "diff.json"))
        return remove, slim

    def apply(self, run, plan):
        for name in plan["remove_diffs"]:
            shutil.rmtree(name, ignore_errors=True)
        for name in plan["slim_diffs"]:
            try:
                slim_diff_index(Path(name))
            except (OSError, ValueError):
                pass  # Removed or rewritten meanwhile; the next run looks again.

    def report(self, out, plan):
        state = out.state
        lines = [f"删除 job-diffs/：{len(plan['remove_diffs'])} 个，共 {mib(sum(tree_bytes(n) for n in plan['remove_diffs']))}"
                 f"（现有 {mib(tree_bytes(state / 'job-diffs'))}，上限 {mib(DIFF_BYTES)}）"]
        for name in plan["remove_diffs"]:
            uploaded = "云空间上传已确认" if diff_uploaded(state, Path(name)) else f"上传未确认，满 {self.days} 天"
            lines.append(f"  {out.rel(name)} {mib(tree_bytes(name))} {out.changed(name):.1f} 天未改动 {uploaded}")
        lines.append(f"精简旧 diff.json：{len(plan['slim_diffs'])} 个，现共 {mib(sum(tree_bytes(n) for n in plan['slim_diffs']))}")
        return lines


class Backups(Rule):
    """backups/ past the newest BACKUP_KEEP or BACKUP_BYTES, and what an interrupted backup left a day ago
    (``old_backups``). ``maintain`` removes them after the run's own backup."""
    keys = ("remove_backups",)

    def select(self, run):
        return old_backups(run.state, now=run.now)

    def report(self, out, plan):
        state = out.state
        files = [p for p in backup_paths(state) if os.path.isfile(p) and not os.path.islink(p)]
        previous = next(((base, size) for base, size, complete, _ in backup_sets(state) if complete), None)
        removed = old_backups(state, now=out.now, incoming=previous[1] if previous else 0)
        return [f"备份：{len(files)} 个文件，未压缩 {mib(sum(tree_bytes(p) for p in files))}"
                + (f"；上一份 {previous[0]} 共 {mib(previous[1])}" if previous else ""),
                f"  备份后删除旧备份 {len(removed)} 份（按上一份的大小估算新备份）："
                + "".join(f"\n    {out.rel(n)} "
                          f"{mib(sum(tree_bytes(p) for p in Path(n).parent.glob(Path(n).name.split('.', 1)[0] + '.*')))}"
                          for n in removed)]


class Stale(Rule):
    """Waiting jobs and pending questions with no activity that long (``stale_records``): the Bridge's stale closer
    (app/stale.py, ``close_stale``) closes them with a note on their cards; offline they are only listed."""
    keys = ("stale_jobs", "stale_questions", "stale_since", "stale_undelivered")
    days = STALE_DAYS

    def select(self, run):
        stale = stale_records(run.state, run.records, run.live, run.now, days=self.days)
        return stale["jobs"], stale["questions"], stale["since"], stale["undelivered"]

    def report(self, out, plan):
        state = out.state
        lines = [f"陈旧关闭（Bridge 执行时逐条再核对，已登记或任务进程在跑的跳过；离线 --apply 不关闭）："
                 f"问题 {len(plan['stale_questions'])} 个，"
                 f"任务 {len(plan['stale_jobs'])} 个"]
        lines += stale_lines(state, plan, out.now)
        folder = state / "questions"
        unconfirmed = [q for _, q in directory_store(folder).query(status="uncertain")] if folder.is_dir() else []
        lines.append(f"不关闭：送达未确认的问题 {len(unconfirmed)} 个（卡片保留“核查已保存回答并继续”）"
                     + "".join(f"\n  问题 {q.get('token')} 任务 #{q.get('job_id')}" for q in unconfirmed))
        return lines


HISTORY, CHECKPOINTS, TRACES, SLIM_RECORDS = History(), Checkpoints(), Traces(), SlimRecords()
TEMPORARY, RUNS_DIRS, PROXY_BATCHES, JOURNALS = Temporary(), Runs(), ProxyBatches(), Journals()
LOCKS, DIFFS, BACKUPS, STALE = Locks(), Diffs(), Backups(), Stale()


# Every rule, in the order of the plan's keys (then "backup", the run's own, named by maintain).
RULES = (HISTORY, CHECKPOINTS, TRACES, SLIM_RECORDS, TEMPORARY, RUNS_DIRS, PROXY_BATCHES, LOCKS, DIFFS, BACKUPS, STALE,
         JOURNALS)


class _NoJob(Rule):
    """Between the locked steps and the rest: the rest goes ahead only if no job is registered now."""

    def apply(self, run, plan):
        return _while_idle(run.idle)


# The order the cleanup carries them out in. The steps under LOCK first; after _NoJob, outside LOCK: nothing a
# job that starts now uses, and each item is checked again. Old backups go in maintain, stale records in close_stale.
CLEANUP = (HISTORY, CHECKPOINTS, _NoJob(), TEMPORARY, RUNS_DIRS, PROXY_BATCHES, TRACES, SLIM_RECORDS, JOURNALS, LOCKS,
           DIFFS)


# The plan's keys of what a cleanup removed, in RULES' order: maintenance_loop logs how many of each.
REMOVALS = tuple(key for rule in RULES for key in rule.keys if key.startswith("remove_"))
# The order --dry-run lists them in.
DRY_RUN = (BACKUPS, HISTORY, CHECKPOINTS, TRACES, SLIM_RECORDS, TEMPORARY, PROXY_BATCHES, RUNS_DIRS, LOCKS, DIFFS, STALE)
