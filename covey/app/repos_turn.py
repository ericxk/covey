"""The repositories' part of a turn: the dynamic repository block at the prompt's tail, and settling a task's requests.

``tail`` is the block app/runs.py puts after the checkpoint note each turn (never in the cached rules): the
repositories the topic works with (core/topic_repos.py works_with: not one its steps only read) and whether their rules
were read, a repository's rules inlined (its first
``RULES_INLINE_BYTES``) when the butler or the lead declared it and its rules were not read yet, or when the last turn
wrote to it without reading them, and a reminder when the last turn changed a repository's main checkout instead of
the topic's worktree. Rules inlined (or printed by ``covey repo use``) whole count as read; a head cut short does not:
they stay unread, so a turn that then writes the repository without reading them gets them again (T900 W0-4). A
repository read in the latest main branch's read worktree (core/repomap.py ``read_worktree``) is shown there, with its
commit and time, and its rules come from it (T900 W0-5); a write into one (the last turn's steps, or changes git shows
in it now) is no write of the topic's, and its next turn is told to move the change to its own worktree (W1-C P1-9). A
topic from before the home shows its repositories only once
one was declared or asked for (``covey repo use``); its inlined rules and reminders it gets all the same. ``settle`` folds a task's
``runs/<任务键>/repos.json`` and the repositories its tool calls touched (core/topic_repos.py ``touched``) into
``topic.json`` when its turn ends, and the repositories it used into the repo map. ``repos_list`` is the Bridge tool's
answer (docs/configuration.md 主目录与常用路径).

The devices' part sits beside it (COVEY_DEVICES=1, core/device_access.py): ``start_devices`` writes what ``covey
device`` needs of the turn into its run directory before the turn runs (its limit and its chat's device space, asked of
app/access.py), and ``settle_devices`` folds the task's ``devices.jsonl`` into ``topic.json`` ``devices``, puts its calls
on the task card's process panel (设备) and, once the answer is out, reaps on each device what a cut-short run left there.
A turn that used no device reads one missing file and does nothing else.
"""
from __future__ import annotations

import os
import time

from covey.app import home_turn
from covey.core import device_access, home, repomap, topic_repos
from covey.core.harness import TOOL_LIMIT
from covey.core.state import read_json
from covey.core.topics import home_start, topic_file, update_devices, update_repos

RULES_INLINE_BYTES = 4096
WRITE_KEYS = ("worktree", "branch", "base", "base_oid")  # A ``covey repo use`` request's, as the topic keeps them.
READ_KEYS = ("base_at", "stale")  # A read worktree's besides (core/repomap.py ``read_worktree``).
CHECKOUT_WRITES = "checkout_writes"  # A topic.json repos entry's: the last turn changed the repository's main checkout.
# A topic.json repos entry's: the read worktrees (core/repomap.py ``read_worktree``) the last turn wrote into. Never a write
# of the topic's (W1-C P1-9): the entry keeps its mode, and the next turn is told.
READ_WRITES = "read_writes"


def _topic(ctx, job) -> tuple[str, dict]:
    topic = getattr(job, "topic_id", "") or ctx.topic_id
    if not topic:
        return "", {}
    try:
        data = read_json(topic_file(ctx.state_dir, topic))
    except (OSError, ValueError):
        return topic, {}
    return topic, data if isinstance(data, dict) else {}


def inline_due(repo: topic_repos.TopicRepo) -> bool:
    """Whether a turn inlines ``repo``'s rules: not read yet, their head not shown since a turn last wrote it without them
    (``rules_inlined_at``), and declared (the butler or the lead named it) or written without them (``rules_miss``)."""
    return not repo.rules_read_at and not repo.rules_inlined_at and (repo.source == "declared" or repo.rules_miss > 0)


def _map(ctx) -> dict:
    return {os.path.normpath(repo.path): repo for repo in repomap.read(ctx.state_dir)}


def _rules_of(entry, mapped) -> tuple:
    found = mapped.get(os.path.normpath(entry.path))
    return found.rules if found is not None else ("AGENTS.md",)


def _rules(entry, mapped, limit) -> tuple[str, str, bool]:
    """``entry``'s rules file head (core/repomap.py ``rules_head``): its worktree's, else the repository's."""
    rules = _rules_of(entry, mapped)
    found = repomap.rules_head(topic_repos.where(entry), rules, limit)
    return found if found[0] or not entry.worktree else repomap.rules_head(entry.path, rules, limit)


def _row(entry, mapped) -> str:
    name, _, _ = _rules(entry, mapped, 0)
    read = (f"规范 {name} ✓已读" if entry.rules_read_at else f"规范 {name} 未读，动它之前先读") if name else "没有规范文件"
    if entry.mode != "write":
        if topic_repos.is_read_worktree(entry.worktree):
            if not os.path.isdir(entry.worktree):
                return (f"- {entry.name}（读，{entry.path}；之前的只读 worktree 已清理，要读最新主干再跑 covey repo use {entry.name}；"
                        f"{read}）")
            stale = "；当时 fetch 没成功，可能落后" if entry.stale else ""
            return f"- {entry.name}（读，最新主干的只读 worktree {entry.worktree}，{repomap.read_words(topic_repos.to_dict(entry))}{stale}；{read}）"
        return f"- {entry.name}（读，{entry.path}；{read}）"
    if entry.worktree and os.path.normpath(entry.worktree) != os.path.normpath(entry.path):
        return f"- {entry.name}（写，worktree {entry.worktree}" + (f"，分支 {entry.branch}" if entry.branch else "") + f"；{read}）"
    found = mapped.get(os.path.normpath(entry.path))
    if not entry.worktree and found is not None and found.write_mode == "worktree":
        return f"- {entry.name}（写，worktree 还没建：先 covey repo use {entry.name} --write；{read}）"
    return f"- {entry.name}（写，原地改 {entry.path}；{read}）"


def tail(ctx, job) -> str:
    """The repository block of ``job``'s turn; '' when the topic has nothing to say about its repositories."""
    if getattr(ctx, "device", ""):
        return ""
    _, data = _topic(ctx, job)
    entries = topic_repos.of_topic(data)
    if not entries:
        return ""
    mapped = _map(ctx)
    raw = {str(item.get("name")): item for item in data.get("repos") or () if isinstance(item, dict)}
    parts = []
    shown = [entry for entry in entries if topic_repos.works_with(entry)]  # Not one its steps only read.
    if shown and (home_start(data) or any(entry.source in ("declared", "cli") for entry in entries)):
        # A topic that keeps off this machine's files (app/home_turn.py local) is not told how to change another.
        more = "\n要改别的仓库：covey repo use <仓库> --write。" if home_turn.local(data) else ""
        parts.append("本话题用到的仓库：\n" + "\n".join(_row(entry, mapped) for entry in shown) + more)
    for entry in entries:
        if raw.get(entry.name, {}).get(CHECKOUT_WRITES):
            parts.append(f"上一轮改到了 {entry.name} 的主检出（{entry.path}）。之后改用 `covey repo use {entry.name} --write` "
                         "打印的 worktree，主检出里的改动先提交或挪过去。")
    parts += read_tree_notes(entries, raw)
    for entry in entries:
        if not inline_due(entry):
            continue
        name, text, cut = _rules(entry, mapped, RULES_INLINE_BYTES)
        if not name:
            continue
        why = "上一轮改了它却没读它的规范" if entry.rules_miss else "这个仓库是派活时点名的"
        parts.append(f"{entry.name} 的规范（{name} 开头{'，后面还有，动手前读全文' if cut else ''}；{why}，先照它做）：\n"
                     f"<<<\n{text.rstrip()}\n>>>")
    return "\n\n".join(parts)


def read_tree_notes(entries, raw) -> list[str]:
    """What a turn is told of the shared read worktrees its topic wrote into (``READ_WRITES``) or reads in with changes in
    them now (``repomap.read_changes``): every topic reading that commit reads those files, so the change is moved to the
    topic's own worktree and taken out of the read one (W1-C P1-9)."""
    notes = []
    for entry in entries:
        trees = [str(tree) for tree in raw.get(entry.name, {}).get(READ_WRITES) or () if isinstance(tree, str)]
        if entry.mode != "write" and topic_repos.is_read_worktree(entry.worktree):
            trees.append(entry.worktree)
        for tree in dict.fromkeys(trees):
            count = read_changes_of(tree)
            if count == 0 or (count is None and tree not in (raw.get(entry.name, {}).get(READ_WRITES) or ())):
                continue  # Clean (taken out already), or nothing known of it.
            what = f"现在有 {count} 处没提交的改动" if count else "上一轮往里写了文件"
            notes.append(f"{entry.name} 的只读 worktree {tree} {what}。它是各话题共用的最新主干，只能读：别的话题读到的会是改过的"
                         f"内容。要改代码用 `covey repo use {entry.name} --write` 打印的 worktree，把改动挪过去，再撤掉只读 worktree "
                         f"里的改动（`git -C {tree} status` 查看）。")
    return notes


def read_changes_of(tree) -> int | None:
    """``repomap.read_changes`` of ``tree``, None for one gone or one git cannot tell."""
    return repomap.read_changes(tree) if tree and os.path.isdir(tree) else None


def _workdir_repo(data) -> dict | None:
    """The repository of a topic from before the home, from its workdir: read, its rules counted as read (the
    Harness starts in it and reads them itself)."""
    workdir = str(data.get("workdir") or "")
    if not workdir.startswith("/"):
        return None
    top, linked = topic_repos.checkout_of(workdir)
    if not top:
        return None
    main = top
    if linked:
        git = topic_repos.repo_of(top)
        main = os.path.dirname(git) if git else top
    now = time.time()
    return {"name": os.path.basename(main), "path": main, "mode": "read", "worktree": top if linked else "",
            "source": "touched", "first_at": now, "rules_read_at": now, "rules_miss": 0}


def start_of(ctx, job, data) -> str:
    """Where the Harness process of ``job``'s turn started, which its tool calls' relative paths are of: its workdir, or for
    a Harness that starts a topic keeping off this machine's files in its outputs directory (``external_cwd``,
    core/home.py external_start) that directory."""
    if getattr(getattr(job, "harness_capabilities", None), "external_cwd", False):
        found = home.external_start(home_turn.external(ctx, data, make=False))
        if found:
            return found
    return str(ctx.workdir)


def settle(ctx, job, task_dir, *, later=False):
    """Fold what ``job``'s turn did with repositories into its topic: the ``covey repo use`` requests of its task
    (core/topic_repos.py ``requests``; their rules were printed, so read), the repositories its steps touched (rules
    read or not, a main checkout changed) and those whose rules ``tail`` inlined (read now); a repository written
    without its rules gets ``rules_miss`` and its rules inlined next turn. Then the repo map takes the ones it used:
    at once, or with ``later`` by the function returned (app/worker.py calls it once the answer is out: the map's file
    lock waits on nothing before it). None when there is nothing to do later."""
    if getattr(ctx, "device", ""):
        return None
    topic, data = _topic(ctx, job)
    if not topic or not data:
        return None
    now = time.time()
    asked = topic_repos.requests(task_dir) if task_dir else []
    repos = repomap.read(ctx.state_dir)
    where = home.of(ctx)
    found = topic_repos.touched(getattr(job, "tool_trace", None), repos, workdir=start_of(ctx, job, data), home=where,
                                skip=topic_repos.skip_roots(ctx.state_dir, where), now=now)
    listed, patterns = {os.path.normpath(repo.path) for repo in repos}, repomap.exclude(ctx.state_dir)
    found = [repo for repo in found
             if os.path.normpath(repo.path) in listed or not repomap.excluded(os.path.basename(repo.path), patterns)]
    mapped = {os.path.normpath(repo.path): repo for repo in repos}
    # What ``tail`` inlined this turn: read when whole, only shown when cut short (a head is no reading).
    inlined = {entry.name: _rules(entry, mapped, RULES_INLINE_BYTES)[2]
               for entry in topic_repos.of_topic(data) if inline_due(entry)}
    legacy = None if "repos" in data or home_start(data) else _workdir_repo(data)
    number = data.get("topic_number") if type(data.get("topic_number")) is int else 0
    own_trees = os.path.normpath(str(topic_repos.topic_worktrees(number, topic))) + os.sep
    own_top = topic_repos.checkout_of(str(ctx.workdir))[0]

    def ours(worktree) -> bool:  # A worktree of this topic's (or its own workdir): never another topic's.
        return bool(worktree) and (os.path.normpath(worktree).startswith(own_trees) or worktree == own_top)

    worktree_mode = {os.path.normpath(repo.path): repo.write_mode for repo in repos}
    own = os.path.normpath(str(ctx.workdir))

    def change(entries):
        by_name = {str(item.get("name")): item for item in entries}
        if legacy is not None and legacy["name"] not in by_name:
            made = dict(legacy)  # A copy each call: core/topics.py update_repos may call ``change`` twice.
            entries.append(made)
            by_name[made["name"]] = made
        for item in entries:
            item.pop(CHECKOUT_WRITES, None)
            item.pop(READ_WRITES, None)
            if item.get("name") in inlined:
                item["rules_inlined_at" if inlined[item["name"]] else "rules_read_at"] = now
        for request in asked:
            name = str(request.get("name") or "")
            if name and request.get("device") and repomap.device_path(request.get("path"))[0] == request["device"]:
                # A device's path (core/repomap.py ``_use_device``): recorded with its device, nothing of this machine's.
                item = by_name.setdefault(name, {"name": name, "path": request["path"], "device": request["device"],
                                                 "mode": "read", "source": "cli", "first_at": request.get("at") or now})
                if item not in entries:
                    entries.append(item)
                if request.get("mode") == "write":
                    item["mode"] = "write"
                continue
            if not name or not str(request.get("path") or "").startswith("/"):
                continue
            item = by_name.get(name)
            if item is None:
                item = {"name": name, "path": request["path"], "mode": "read", "source": "cli", "first_at": request.get("at") or now}
                entries.append(item)
                by_name[name] = item
            if request.get("mode") == "write":
                item["mode"] = "write"
                for key in READ_KEYS:
                    item.pop(key, None)
                for key in WRITE_KEYS:
                    if request.get(key):
                        item[key] = request[key]
            elif item.get("mode") != "write":
                # A read: the read worktree it printed (or none: the repository itself) replaces an earlier read's. A topic
                # writing the repository keeps its own worktree.
                for key in WRITE_KEYS + READ_KEYS:
                    if request.get(key):
                        item[key] = request[key]
                    else:
                        item.pop(key, None)
            if request.get("rules_file") and not item.get("rules_read_at"):
                # A head cut short (``rules_cut``, from this release on) was shown, not read.
                item["rules_inlined_at" if request.get("rules_cut") else "rules_read_at"] = request.get("at") or now
        for repo in found:
            item = by_name.get(repo.name)
            if item is None:
                item = {"name": repo.name, "path": repo.path, "mode": "read", "source": "touched", "first_at": now}
                entries.append(item)
                by_name[repo.name] = item
            if repo.worktree and repo.worktree != repo.path and not item.get("worktree") and ours(repo.worktree):
                item["worktree"] = repo.worktree
            if repo.mode == "write" and topic_repos.is_read_worktree(repo.worktree):
                # A write into a shared read worktree (P1-9): no write of the topic's, said next turn (``read_tree_notes``).
                item[READ_WRITES] = list(dict.fromkeys([*(item.get(READ_WRITES) or ()), repo.worktree]))
            elif repo.mode == "write":
                item["mode"] = "write"
            if repo.rules_read_at:
                item["rules_read_at"] = repo.rules_read_at
        for repo in found:
            item = by_name[repo.name]
            if repo.mode != "write" or topic_repos.is_read_worktree(repo.worktree):
                continue
            if not item.get("rules_read_at"):
                item["rules_miss"] = int(item.get("rules_miss") or 0) + 1
                item.pop("rules_inlined_at", None)  # Written without them: their head again next turn.
            checkout = not repo.worktree
            in_place = worktree_mode.get(os.path.normpath(repo.path)) == "inplace" or (
                item.get("worktree") and os.path.normpath(item["worktree"]) == os.path.normpath(repo.path))
            if checkout and not in_place and own != os.path.normpath(repo.path) and not own.startswith(os.path.normpath(repo.path) + os.sep):
                item[CHECKOUT_WRITES] = 1
        return entries

    entries = update_repos(ctx.state_dir, topic, change)
    names = {repo.name for repo in found} | {str(request.get("name")) for request in asked}
    listed_now = [repo for repo in map(topic_repos.from_dict, entries) if repo is not None]
    used = [repo for repo in listed_now if repo.name in names]
    # A device's path this turn asked for: its ``last_used`` in the map too (it is always listed there: ``use`` finds it by name).
    used += [topic_repos.TopicRepo(name=str(item["name"]), path=str(item["path"]), mode=str(item.get("mode") or "read"))
             for item in entries if isinstance(item, dict) and item.get("device") and item.get("name") in names
             and repomap.device_path(item.get("path"))[0]]
    # The read worktrees the topic reads in: a turn of it keeps them (core/repomap.py READ_IDLE counts from its last).
    reading = [repo.worktree for repo in listed_now if repo.mode != "write" and topic_repos.is_read_worktree(repo.worktree)]

    def note() -> None:
        for tree in reading:
            repomap.touch_read(tree, now)
        if not used:
            return
        try:
            repomap.note_used(ctx.state_dir, used, now=now)
            repomap.render(ctx.state_dir, where)
        except OSError:
            pass
    if later:
        return note if used or reading else None
    note()
    return None


def start_devices(ctx, job, access, log=None) -> None:
    """With COVEY_DEVICES on and a device listed (core/device_access.py ``listed``), before the turn runs: its limit (core/harness.py TOOL_LIMIT) and its chat's device space
    (app/access.py ``device_space``, rule 11) into its run directory, for ``covey device`` (core/device_access.py
    ``note_turn``). Nothing for a turn on a device, or one without a run directory; a failure is logged and the turn
    runs (``covey device`` then refuses it)."""
    if getattr(ctx, "device", "") or not device_access.listed():
        return
    try:
        task_dir = topic_repos.task_dir_of(ctx.state_dir, job)
        if task_dir is None or not task_dir.is_dir():
            return
        topic = getattr(job, "topic_id", "") or ctx.topic_id or ""
        device_access.note_turn(task_dir, limit=ctx.options.get(TOOL_LIMIT) or "",
                                space=access.device_space(getattr(job, "chat_id", "")), topic=topic,
                                outputs=home_turn.outputs_of(ctx))
    except Exception as exc:  # noqa: BLE001 - the turn runs; only covey device refuses it
        if log:
            log(f"device turn of job #{getattr(job, 'job_id', '')} not noted: {type(exc).__name__}")


def settle_devices(ctx, job, task_dir, *, later=False, stopped=False, log=None):
    """Once the turn has ended: its task's ``devices.jsonl`` (core/device_access.py) into ``topic.json`` ``devices``,
    its calls onto the task card's process panel (a 设备 note), and the reap of what a run cut short left on a device
    (``reap_turn``; with ``stopped``, the owner stopped the turn, the jobs it started are stopped too): at once, or with
    ``later`` by the function returned (after the answer is out: a reap waits on the device). None when the task used no
    device."""
    rows = device_access.read_audit(task_dir) if task_dir else []
    if not rows:
        return None
    topic = getattr(job, "topic_id", "") or ctx.topic_id
    if topic:
        update_devices(ctx.state_dir, topic, lambda entries: device_access.fold(rows, entries))
    text = device_access.trace_text(rows)
    trace = getattr(job, "tool_trace", None)
    if text and trace is not None:
        trace.note("devices", text, title="设备")

    def reap() -> None:
        device_access.reap_turn(ctx.state_dir, task_dir, stop_jobs=stopped, log=log)
    if later:
        return reap
    reap()
    return None


def repos_list(ctx) -> dict:
    """The Bridge tool ``repos_list``'s answer: the home, where the map is, each repository of it (whether this topic
    uses it, and how) and the topic's repositories the map does not list (``unmapped``)."""
    where = home.of(ctx)
    _, data = _topic(ctx, None)
    used = {entry.name: entry for entry in topic_repos.of_topic(data)}
    rows = []
    for repo in repomap.read(ctx.state_dir):
        entry = used.pop(repo.name, None)
        rows.append({"name": repo.name, "path": repo.path, "note": repo.note, "status": repo.status,
                     "write_mode": repo.write_mode, "branch": repo.branch, "kind": repo.kind,
                     **({"device": repo.device} if repo.device else {}),
                     **({"used": entry.mode, "worktree": entry.worktree} if entry else {})})
    return {"home": str(where), "map": str(repomap.rendered_path(ctx.state_dir, where)), "repos": rows,
            "unmapped": [{"name": e.name, "path": e.path, "used": e.mode, "worktree": e.worktree} for e in used.values()]}


def write_dirs(ctx, job) -> list[tuple[str, str]]:
    """(name, directory) of each repository ``job``'s topic writes to: its worktree, or the repository itself when it is
    written in place; the turn's diff takes a snapshot of each before it starts (core/diff.py). One whose worktree is
    not made yet is left out: its main checkout may hold other topics' changes."""
    if getattr(ctx, "device", ""):
        return []
    _, data = _topic(ctx, job)
    entries = [entry for entry in topic_repos.of_topic(data) if entry.mode == "write"]
    if not entries:
        return []
    mapped = _map(ctx)
    found = []
    for entry in entries:
        listed = mapped.get(os.path.normpath(entry.path))
        if entry.worktree or (listed is not None and listed.write_mode == "inplace"):
            found.append((entry.name, topic_repos.where(entry)))
    return found
