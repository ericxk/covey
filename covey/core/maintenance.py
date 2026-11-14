"""Daily maintenance of a state directory: a verifiable backup, archival and bounded storage.

The Bridge backs up once a day, jobs running or not, and runs the rest once a day while no job
is registered (``maintenance_loop``); with the service stopped, ``covey_maintenance.py
--apply`` runs it all by hand. Without ``apply``,
``maintain`` only plans: it reads the state and changes nothing, so it is safe on a live one;
``--dry-run`` prints that plan item by item (``dry_run_report``). Each kind of stored data the plan bounds is
one rule of RULES (core/maintenance_rules.py): what it selects and how old, what the cleanup does with it, how its
dry-run lines read. The backup itself is core/backup.py, the stale records core/stale.py, the dry-run's wording
core/maintenance_report.py. The web login's caches are not in the plan: ``prune_web`` bounds them each hour
(``maintenance_loop``, core/web_profile.py WEB_CACHES); this module keeps the plan, ``maintain``, the loop and
the command line.

Only the steps that must not overlap a job hold the global LOCK, one short step at a time:
each history archive (also under HISTORY_LOCK) and each checkpoint compression. The backup
copies with no lock held: records are replaced whole and logs only appended, so each copied
file is a version that existed, and a JSONL copy ends at its last complete line. A finished
task's process record is gzipped under its writer's lock only (trace_store.compress_trace).
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime
from pathlib import Path

from covey.core.backup import (BACKUP_KEEP, back_up, create_backup, last_backup, old_backups, remove_backup,
                                        restore_backup, verify_backup)
from covey.core.loops import every
from covey.core.maintenance_report import Report, mib, tree_bytes
from covey.core.maintenance_rules import CLEANUP, DRY_RUN, REMOVALS, RULES, Run
from covey.core.migrate import migrate, pending
from covey.core.state import DAY, read_object, write_json
from covey.core.web_profile import WEB_CACHE_BYTES, web_caches, web_profile


def plan_maintenance(state, *, days=30, history_bytes=1024 * 1024, now=None):
    """What maintenance would do now, each rule's keys in RULES' order. Reads only: no lock, no write, no network."""
    now = time.time() if now is None else now
    run = Run(state, now, days, history_bytes=history_bytes)
    plan = {}
    for rule in RULES:
        if rule.keys:
            value = rule.select(run)
            plan.update(zip(rule.keys, value, strict=True) if len(rule.keys) > 1 else {rule.keys[0]: value})
    plan["backup"] = None
    return plan


def _cleanup(state, plan, idle, now, days=30):
    """Carry out the plan's archive, compression and removal steps (CLEANUP); False when a job started
    meanwhile (the rest then waits for the next day)."""
    run = Run(state, now, days, idle=idle)
    return all(rule.apply(run, plan) is not False for rule in CLEANUP)


def maintain(state, *, apply=False, days=30, history_bytes=1024 * 1024, backup_keep=BACKUP_KEEP,
             idle=None, close_stale=None, now=None, backup=True):
    """The maintenance plan for ``state`` (``plan_maintenance``); with ``apply``, carry it out.

    ``idle`` says whether no job is registered (the Bridge's registry; None offline): a step
    that must not overlap a job checks it under LOCK, and once it is False the rest of the
    cleanup waits for the next day. ``close_stale(plan)`` closes the stale records with a note
    on their cards and returns what it closed; offline they are only listed. ``backup``: False
    when today's was made already (``back_up``, while jobs ran)."""
    state = state.resolve()
    now = time.time() if now is None else now
    plan = plan_maintenance(state, days=days, history_bytes=history_bytes, now=now)
    if not apply:
        return plan
    plan["migrated"] = migrate(state)  # Old records (core/migrate.py): nothing once the Bridge's start did it.
    status = state / "maintenance-status.json"
    last_backup = now if backup else read_object(status).get("last_backup")
    if backup:
        plan["backup"] = str(create_backup(state))
    plan["deferred"] = not _cleanup(state, plan, idle, now, days)
    plan["remove_backups"] = old_backups(state, backup_keep, now=now)
    for name in plan["remove_backups"]:
        remove_backup(name)
    plan["closed"] = []
    # Recorded before the stale records are closed: a failure there never repeats today's backup.
    write_json(status, {"last_completed": now, "last_backup": last_backup, "plan": plan}, ensure_ascii=False)
    if close_stale and not plan["deferred"]:
        plan["closed"] = close_stale(plan)
        write_json(status, {"last_completed": now, "last_backup": last_backup, "plan": plan}, ensure_ascii=False)
    return plan


def dry_run_report(state, *, now=None):
    """What a maintenance run would do now, item by item, with sizes: each rule's part in DRY_RUN's order.
    Reads only, like ``plan_maintenance``: no lock, no backup, no status file, nothing written."""
    state = state.resolve()
    now = time.time() if now is None else now
    plan = plan_maintenance(state, now=now)
    out = Report(state, now)
    status = read_object(state / "maintenance-status.json")
    last = status.get("last_completed")
    backed = last_backup(status)
    when = lambda at: datetime.fromtimestamp(at).isoformat(timespec="seconds")
    lines = [f"维护预演 {state}",
             f"时间 {datetime.fromtimestamp(now).isoformat(timespec='seconds')}；只读，未写任何文件。",
             "上次备份 " + (when(backed) if backed else "无") + "，下次 "
             + (when(backed + DAY) if backed and backed + DAY > now else "Bridge 下一次检查时（每小时）")
             + "；有任务运行也照做（只读）。",
             "上次清理 " + (when(last) if last else "无")
             + "；Bridge 在距上次满一天且没有任务时执行，有任务开始时其余清理顺延。"]
    profile = web_profile(state)
    caches = web_caches(profile)
    if caches:
        size = sum(n for _, n in caches)
        lines.append(f"代答网页端登录 {out.rel(profile)}：缓存类目录共 {mib(size)}（上限 {mib(WEB_CACHE_BYTES)}，"
                     + ("超过，Bridge 在没有 sending 批次、浏览器不在用时删除" if size >= WEB_CACHE_BYTES else "未超过，不删")
                     + "）：" + "，".join(f"{folder.relative_to(profile)} {mib(n)}" for folder, n in caches)
                     + f"；其余（cookie、Local Storage、IndexedDB 等）{mib(max(0, tree_bytes(profile) - size))} 不动。")
    for rule in DRY_RUN:
        lines += rule.report(out, plan)
    todo = pending(state)
    lines.append("旧记录迁移（core/migrate.py，--apply 和 Bridge 启动时先做）："
                 + ("，".join(f"{name} 还没做完，内嵌执行过程待写成 traces/ 文件的记录 {count} 条"
                             for name, count in todo.items()) or "已做完"))
    return "\n".join(lines) + "\n"


def maintenance_loop(runtime, ctx, close_stale=None, prune_web=None):
    """Once a day back up the state directory, jobs running or not: the backup only reads (repo review E6: it
    waited for no job, and a day of long tasks went without one). Once a day while no job is registered,
    archive and clean it too. ``close_stale(plan)`` closes the records that waited longer than STALE_DAYS;
    ``prune_web()``, each hour, bounds the 代答 web login's caches (app/proxy_send.py, WEB_CACHES)."""
    def maintain_once():
        try:
            status = read_object(ctx.state_dir / "maintenance-status.json")
            now = time.time()
            backup_due = now - last_backup(status) >= DAY
            busy = runtime.jobs.busy()
            if not busy and now - float(status.get("last_completed") or 0) >= DAY:
                plan = maintain(ctx.state_dir, apply=True, idle=lambda: not runtime.jobs.busy(),
                                close_stale=close_stale, backup=backup_due)
                runtime.log(("idle maintenance backup/archive completed" if backup_due
                             else "idle maintenance archive completed (today's backup was made already)")
                            + (" (cleanup deferred: a job started)" if plan["deferred"] else ""))
                runtime.log("maintenance removed " + " ".join(
                    f"{name.removeprefix('remove_')}={len(plan[name])}" for name in REMOVALS)
                    + f" slimmed={len(plan['slim_diffs'])} traces_gzipped={len(plan['compress_traces'])}"
                    + f" records_slimmed={len(plan['slim_records'])}"
                    + f" closed={len(plan['closed'])}")
            elif backup_due:
                backup = back_up(ctx.state_dir)
                runtime.log(f"maintenance backup completed: {backup.name}"
                            + ("; jobs are running, the cleanup waits until none is" if busy else ""))
        except Exception as exc:  # noqa: BLE001 - isolate background/event failures
            runtime.log(f"maintenance deferred after error: {exc}")

    def prune():
        try:
            prune_web()
        except Exception as exc:  # noqa: BLE001 - the backup and cleanup do not depend on it
            runtime.log(f"web login cache prune failed: {type(exc).__name__}: {exc}")
    every(runtime, 3600, maintain_once, *([prune] if prune_web is not None else []))


def main():
    p = argparse.ArgumentParser(prog="covey_maintenance.py")
    p.add_argument("--state-dir", type=Path)
    p.add_argument("--apply", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="逐项列出一次维护会做什么，只读")
    p.add_argument("--verify", type=Path)
    p.add_argument("--restore", type=Path)
    p.add_argument("--destination", type=Path)
    a = p.parse_args()
    if a.verify:
        print(json.dumps({"verified_files": len(verify_backup(a.verify))}))
        return
    if a.restore:
        if not a.destination:
            p.error("--destination required")
        restore_backup(a.restore, a.destination)
        print("restored")
        return
    if not a.state_dir:
        p.error("--state-dir required")
    if a.dry_run:
        if a.apply:
            p.error("--dry-run 与 --apply 不能同时使用")
        print(dry_run_report(a.state_dir), end="")
        return
    pid = a.state_dir / "bridge.pid"
    if a.apply and pid.exists():
        try:
            os.kill(int(pid.read_text()), 0)
        except ProcessLookupError:
            pass
        else:
            p.error("服务运行时由内部空闲维护执行；外部命令只允许预览")
    print(json.dumps(maintain(a.state_dir, apply=a.apply), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
