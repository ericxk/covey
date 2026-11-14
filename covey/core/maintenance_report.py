"""How maintenance's --dry-run (core/maintenance.py dry_run_report) words what it would do: sizes, ages, a
rule's section, and the lines for the stale records the Bridge would close."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from covey.core.stale import STALE_NOTE, UNDELIVERED_LAUNCH_DAYS
from covey.core.state import DAY, read_object
from covey.core.statuses import OPEN_FORMS, WORKTREE_STATUS
from covey.core.store import directory_store


def tree_bytes(path):
    """The size of a file, or of a whole tree (symlinks not followed)."""
    try:
        info = os.lstat(path)
    except OSError:
        return 0
    if not stat.S_ISDIR(info.st_mode):
        return info.st_size
    total = 0
    for folder, _, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(folder, name)).st_size
            except OSError:
                pass
    return total


def mib(size):
    return f"{size / 1048576:.1f} MiB" if size >= 1048576 else f"{size / 1024:.1f} KiB"


def stale_lines(state, plan, now):
    """What the Bridge's stale closer (app.stale) would do with the plan's records."""
    since = plan["stale_since"]
    age = lambda when: f"{(now - when) / DAY:.1f} 天无动静"

    def topic(record):
        number = read_object(state / "topics" / str(record.get("topic_id") or "-") / "topic.json").get("topic_number")
        return f"话题 {record.get('topic_id')}" + (f"（#{number}）" if number else "")

    undelivered = set(plan.get("stale_undelivered", ()))
    lines, asked = [], set()
    for token in plan["stale_questions"]:
        record = read_object(state / "questions" / (token + ".json"))
        asked.add(record.get("job_key"))
        card = f"问题卡标题改为“{STALE_NOTE}”" if record.get("card_id") else "没有问题卡，只改记录"
        lines.append(f"  问题 {token} 任务 #{record.get('job_id')} {topic(record)} "
                     f"{age(since['questions'][token])} → 转“已取消”；{card}")
    jobs = {key: read_object(state / "jobs" / (key + ".json")) for key in plan["stale_jobs"]}
    launches = {}
    for key, record in jobs.items():
        if record.get("status") == "waiting_workspace":
            launches.setdefault((record.get("topic_id"), record.get("chat_id")), []).append(key)
            continue
        card = ("它的问题卡已改（见上）" if key in asked else f"任务卡顶部状态行改为“{STALE_NOTE}”"
                if record.get("card_id") else "没有任务卡，只改记录")
        lines.append(f"  任务 #{record.get('job_id')} {key} waiting_user {topic(record)} "
                     f"{age(since['jobs'][key])} → 转“已取消”；{card}")
    folder = state / "jobs"
    for (topic_id, chat_id), keys in launches.items():
        waiting = {r["key"] for _, r in directory_store(folder).query(topic_id=topic_id, chat_id=chat_id)
                   if r.get("status") == "waiting_workspace"}
        forms = [form for path in sorted((state / "launch-forms").glob("*.json"))
                 if (form := read_object(path)).get("topic_id") == topic_id and form.get("chat_id") == chat_id]
        if not waiting <= set(keys):
            action = "不关闭：同一启动卡还有较新的消息在等"
        elif any(form.get("status") == WORKTREE_STATUS for form in forms):
            action = "不关闭：开始任务正在创建 worktree"
        elif any(form.get("status") in OPEN_FORMS for form in forms):
            cards = sum(1 for form in forms if form.get("status") in OPEN_FORMS and form.get("card_id"))
            action = f"启动卡转“已关闭”，{cards} 张整卡替换为“{STALE_NOTE}”（标题“已自动关闭”）"
        else:
            action = "话题里没有可开始的启动卡：只把记录转“已取消”"
        first = jobs[keys[0]]
        rule = f"启动卡未送达（{UNDELIVERED_LAUNCH_DAYS} 天规则） " if set(keys) & undelivered else ""
        lines.append(f"  任务 {', '.join('#' + str(jobs[k].get('job_id')) for k in keys)} waiting_workspace "
                     f"{topic(first)} {age(max(since['jobs'][k] for k in keys))} {rule}→ {action}")
    return lines


class Report:
    """What the rules' parts of ``--dry-run`` share: the state, now, and how a path and its age read."""

    def __init__(self, state, now):
        self.state, self.now = state, now

    def rel(self, name):
        return str(Path(name).relative_to(self.state)) if Path(name).is_relative_to(self.state) else str(name)

    def changed(self, name):
        return (self.now - os.lstat(name).st_mtime) / DAY if os.path.lexists(name) else 0.0

    def section(self, title, names):
        return [f"{title}：{len(names)} 个，共 {mib(sum(tree_bytes(n) for n in names))}",
                *(f"  {self.rel(n)} {mib(tree_bytes(n))} {self.changed(n):.1f} 天未改动" for n in names)]
