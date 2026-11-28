"""The repo map (常用路径, ``<state>/repos.json``): the git repositories and plain directories this deployment works in, on
this machine or on a device, and the ``covey repo`` command.

Each entry has a name, a path, what it is for (``note``), a status (``frozen`` and ``readonly`` ones are never
written to) and how a topic writes to it (``write_mode``: ``worktree`` makes the topic's own worktree under
``<数据根>/worktrees/T<号>-<id8>/<仓库>/``, ``inplace`` edits the checkout; a home Covey made defaults to ``inplace``).
``kind`` is ``git`` (the default: an entry written before kinds were) or ``dir``, a directory with no ``.git``; a ``dir``
is always written in place. A device's path is written ``<别名>:/abs/path`` (core/devices.py ``DEVICE_VALUE``), its
``device`` the alias, always in place: this machine never touches it (``covey repo use`` reads its rules head with the
device's ``get``; ``--write`` only records it). A release before kinds reads a device's entry as one it passes over
(``_passed_over``: no absolute path) and writes it back as it was.
Reading a ``worktree`` repository is in a detached worktree of its latest main branch (``read_worktree``), never in its
main checkout, which may be many commits behind.
Read without ``LOCK`` (rule 2); written whole under its own file lock. ``repos.md`` is the map as a Harness reads it:
in a home Covey made it is ``<home>/repos.md``, else ``<state>/repos.md`` (a taken-over home gets no file). ``main`` is
bin/covey_repo.py (``list``, ``use``, ``status``, ``prune-plan``): a task finds its context in ``COVEY_STATE``,
``COVEY_HOME``, ``COVEY_TOPIC`` and its run directory (``COVEY_TASK_DIR``), and writes only that run directory's
``repos.json`` (core/topic_repos.py), which the Bridge folds into the topic when the turn ends
(docs/configuration.md 主目录与仓库地图).
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from covey.core import devices as device_list
from covey.core import env as cenv
from covey.core import home as homes
from covey.core import state as cstate
from covey.core import topic_repos
from covey.core.file_lock import file_lock
from covey.core.state import write_private_text

REPOS_FILE = "repos.json"
RENDERED_FILE = "repos.md"  # The map as the Harness reads it; rendered, never edited, never backed up.
STATUSES = ("active", "readonly", "frozen")
WRITE_MODES = ("worktree", "inplace")
KINDS = ("git", "dir")
ADDED_BY = ("migrate", "discover", "touched", "owner")
NOT_YET = "这个命令在这一版还没有启用（常用路径开发中）。"
EXCLUDE = ("*-data/", "node_modules/", ".venv/")
RULES_BYTES = 4096  # Of a repository's rules file, what `use` prints and a turn inlines (app/repos_turn.py RULES_INLINE_BYTES).
GIT_SECONDS = 20  # A git call of the command: a fetch that takes longer is given up (the local branch is the base then).
FETCH_SECONDS = 20
STATUS_SECONDS = 5
DEVICE_SECONDS = 8  # A device's ``get`` of the rules head ``covey repo use`` prints: no answer by then, no head.
PRUNE_IDLE = 24 * 3600  # A worktree untouched this long, clean and merged, is listed by prune-plan.
READ_IDLE = 24 * 3600  # A read worktree no turn used this long is removed, when clean, as `use` reads that repository again.
# Beside a read worktree, ``<提交前 12 位>.used``: its mtime is when a turn last used it. Written only once the worktree's
# checkout is complete (``read_worktree``, under its lock), so it also says the worktree may be read: a worktree without it is
# being checked out by another topic (P1-10) and is never handed out by the lock-free path.
READ_USED = ".used"
READ_SPARES = 3  # A read worktree with changes is never read again: the next of ``<12 位>``, ``<12 位>-2``, ``<12 位>-3`` is.
STATUS_WORDS = {"active": "可用", "readonly": "只读", "frozen": "已冻结"}
KIND_WORDS = {"git": "仓库", "dir": "目录"}
ACCESS_WORDS = {"write": "可写", "read": "只读"}


@dataclass(frozen=True)
class Repo:
    name: str
    path: str
    note: str = ""
    status: str = "active"  # STATUSES
    write_mode: str = "worktree"  # WRITE_MODES
    branch: str = ""
    device: str = ""
    rules: tuple = ("AGENTS.md",)
    added_by: str = "owner"  # ADDED_BY
    last_used: float = 0.0
    extra: dict = field(default_factory=dict)  # Keys a newer release wrote, kept as they are.
    kind: str = "git"  # KINDS


# The fields whose value a newer release may write that this one cannot read: read as the default, written back as it was
# (``to_json``) unless something here set it.
UNREAD_VALUES = {"status": STATUSES, "write_mode": WRITE_MODES, "kind": KINDS}


def device_path(path) -> tuple[str, str]:
    """(alias, the absolute path on it) of a device's entry path ``<别名>:/abs/path``; ('', '') for any other path."""
    found = device_list.DEVICE_VALUE.match(str(path or "").strip())
    if not found or not found.group(2).startswith("/"):
        return "", ""
    return found.group(1), device_list.normalize_remote(found.group(2))


def _readable(item) -> bool:
    """Whether ``read`` takes this ``repos.json`` entry: a name, and an absolute path here or a device's ``<别名>:/abs``."""
    if not isinstance(item, dict) or not str(item.get("name") or "").strip():
        return False
    where = str(item.get("path") or "")
    return where.startswith("/") or bool(device_path(where)[0])


def path(state_dir) -> Path:
    return Path(state_dir) / REPOS_FILE


def _data(state_dir) -> dict:
    try:
        data = json.loads(path(state_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def read(state_dir) -> list[Repo]:
    """The repositories of ``repos.json``; [] when it is missing or unreadable."""
    return _repos(_data(state_dir))


def _repos(data) -> list[Repo]:
    found = []
    for item in (data.get("repos") if isinstance(data, dict) else None) or []:
        if not _readable(item):
            continue
        known = {key: item[key] for key in Repo.__dataclass_fields__ if key in item and key != "extra"}
        rules = known.get("rules")
        rules = [rules] if isinstance(rules, str) else rules if isinstance(rules, (list, tuple)) else ()
        known["rules"] = tuple(str(rule) for rule in rules if str(rule).strip()) or ("AGENTS.md",)
        extra = {k: v for k, v in item.items() if k not in Repo.__dataclass_fields__}
        for key, values in UNREAD_VALUES.items():  # A newer release's value: read as the default, kept to write back.
            if key in known and known[key] not in values:
                extra[key] = known.pop(key)
        alias, where = device_path(known.get("path"))
        if alias:  # A device's: its alias from the path, written in place (no worktree on a device).
            known.update(path=f"{alias}:{where}", device=alias, write_mode="inplace")
        else:
            known["device"] = ""
        if known.get("kind") == "dir":
            known["write_mode"] = "inplace"
        try:
            found.append(Repo(**known, extra=extra))
        except TypeError:
            continue
    return found


def exclude(state_dir) -> tuple:
    found = _data(state_dir).get("exclude")
    return tuple(str(item) for item in found) if isinstance(found, list) else EXCLUDE


def find(repos, key: str) -> Repo | None:
    """The repository named ``key``, or whose path is ``key``."""
    key = str(key or "").rstrip("/")
    return next((repo for repo in repos if repo.name == key or repo.path.rstrip("/") == key), None)


def to_json(repo: Repo) -> dict:
    """``repo`` as repos.json holds it; a status or write mode this release could not read (``_repos`` kept it in ``extra``)
    is written back as it was while the repository still has the default in its place."""
    data = {key: value for key, value in asdict(repo).items() if key != "extra"}
    data["rules"] = list(repo.rules)
    for key in UNREAD_VALUES:
        if key in repo.extra and getattr(repo, key) == Repo.__dataclass_fields__[key].default:
            data[key] = repo.extra[key]
    return {**repo.extra, **data}


class Unreadable(cstate.Unreadable, ValueError):
    """``repos.json`` is there but cannot be read (``str``: "read", or "format" for JSON that is no object): never
    written over. core/state.py Unreadable (an OSError) and, as before, a ValueError."""


@contextlib.contextmanager
def _locked(state_dir):
    with file_lock(Path(state_dir) / (REPOS_FILE + ".lock")):
        yield


def _raw(state_dir) -> dict:
    """What ``repos.json`` holds, {} when it is missing; Unreadable when it is there and cannot be read."""
    try:
        return cstate.read_for_write(path(state_dir))
    except cstate.Unreadable as exc:
        raise Unreadable("format" if exc.__cause__ is None else "read") from None


def _passed_over(raw) -> list:
    """The entries of ``raw`` that ``read`` leaves out (no name, no absolute path here or on a device: a newer release's).
    A release before device entries passed those over the same way, so a rollback keeps them as they are."""
    return [item for item in (raw.get("repos") if isinstance(raw.get("repos"), list) else ()) if not _readable(item)]


def write(state_dir, repos, *, excluded=None, raw=None) -> None:
    """``repos.json`` replaced whole with ``repos`` (the caller holds ``update``'s lock or is the only writer). What the
    file held besides (``raw``, read now when not given: its other keys, ``exclude``, the entries ``read`` passes over)
    is written back as it was."""
    raw = _data(state_dir) if raw is None else raw
    kept = raw.get("exclude")
    data = {**{key: value for key, value in raw.items() if key not in ("version", "repos", "exclude")},
            "version": raw.get("version") or 1, "repos": [to_json(repo) for repo in repos] + _passed_over(raw),
            "exclude": list(excluded if excluded is not None else kept if isinstance(kept, list) else EXCLUDE)}
    write_private_text(path(state_dir), json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def update(state_dir, change, *, start=None) -> list[Repo]:
    """``change(repos) -> repos`` applied to ``repos.json`` under its file lock; the repositories written (unchanged
    ones are not written again). Every writer of the map comes here (the turn's ``note_used``, the settings page,
    app/web_settings.py ``edit_repos``). ``start()``: the repositories to begin from while the file is missing, and the
    file is made then even when ``change`` keeps them; without it a missing file stays missing unless ``change`` adds
    one. A file that is there and cannot be read raises Unreadable and is left as it is."""
    with _locked(state_dir):
        raw = _raw(state_dir)
        there = path(state_dir).exists()
        before = _repos(raw) if there else list(start() if start is not None else ())
        after = list(change(list(before)))
        if after != before or (not there and start is not None):
            write(state_dir, after, raw=raw)
    return after


def replace(repo: Repo, **changes) -> Repo:
    return Repo(**{**asdict(repo), **changes})


# ---- the map as the Harness reads it ------------------------------------------------------------------------------


def rendered_path(state_dir, home) -> Path:
    """Where ``repos.md`` is: in a home Covey made, the home; else the state directory (a taken-over home gets no file)."""
    return Path(home) / RENDERED_FILE if home and homes.seeded(home) else Path(state_dir) / RENDERED_FILE


def _cell(text) -> str:
    """``text`` as one cell of a Markdown table: one line, no ``|``."""
    return " ".join(str(text or "").split()).replace("|", "/") or "—"


def device_rows(config=None) -> list[dict]:
    """The devices ``repos.md`` lists at its end: ``{alias, title, note, access, runs}`` of each device of devices.json
    (``runs`` false for one only watched: no ssh), by alias; [] with devices off or a devices.json that cannot be read."""
    try:
        config = config if config is not None else device_list.current()
    except device_list.DeviceConfigError:
        return []
    found = [(alias, device, True) for alias, device in config.devices.items()]
    found += [(alias, device, False) for alias, device in getattr(config, "watched", {}).items()]
    return [{"alias": alias, "title": device.title, "note": device.note, "access": device.access, "runs": runs}
            for alias, device, runs in sorted(found)]


def render_md(repos, *, home="", devices=()) -> str:
    """``repos.md``: the home, a row per entry (仓库 or 目录, here or on a device) and, with devices listed (``device_rows``),
    the 常用设备 table. Read by a Harness on demand, never part of the rules a turn starts with."""
    lines = ["# 常用路径", "", "由 Covey 从 repos.json 生成，不要手改；改用途、状态用设置里的「常用路径」页。", ""]
    if home:
        lines += [f"主目录：`{home}`", ""]
    if not repos:
        lines += ["（还没有常用路径。）", ""]
    else:
        lines += ["| 名字 | 路径 | 设备 | 类型 | 用途 | 状态 | 怎么改 | 规范 |", "| --- | --- | --- | --- | --- | --- | --- | --- |"]
        for repo in sorted(repos, key=lambda r: r.name.lower()):
            where = device_path(repo.path)[1] if repo.device else repo.path
            how = "在设备上" if repo.device else "原地改" if repo.write_mode == "inplace" else "按话题建 worktree"
            lines.append(f"| {_cell(repo.name)} | `{where}` | {_cell(repo.device or '本机')} | {KIND_WORDS.get(repo.kind, repo.kind)} | "
                         f"{_cell(repo.note)} | {STATUS_WORDS.get(repo.status, repo.status)} | {how} | {'、'.join(repo.rules)} |")
        lines += ["", "要改：`covey repo use <名字> --write`；只读：`covey repo use <名字>`；本话题用到的：`covey repo status`。", ""]
    if devices:
        lines += ["## 常用设备", "", "| 别名 | 名字 | 用途 | 可写还是只读 |", "| --- | --- | --- | --- |"]
        for row in devices:
            access = ACCESS_WORDS.get(row["access"], "没设") if row["runs"] else "只能看状况，不能执行"
            lines.append(f"| {row['alias']} | {_cell(row['title'])} | {_cell(row['note'])} | {access} |")
        lines += ["", "设备上的路径本机读不到；只读的设备只能看，不能改。", ""]
    return "\n".join(lines)


def render(state_dir, home="") -> Path | None:
    """Write ``repos.md`` where ``rendered_path`` says, when its text changed; the path, None when it cannot be written."""
    target = rendered_path(state_dir, home)
    text = render_md(read(state_dir), home=str(home or ""), devices=device_rows())
    try:
        if not target.is_file() or target.read_text(encoding="utf-8") != text:
            write_private_text(target, text)
    except OSError:
        return None
    return target


# ---- finding repositories -----------------------------------------------------------------------------------------


def excluded(name: str, patterns) -> bool:
    """Whether a directory named ``name`` matches one of ``patterns`` (``exclude``: ``*-data/``, ``node_modules/`` …)."""
    import fnmatch
    return any(fnmatch.fnmatch(name + "/", pattern) or fnmatch.fnmatch(name, pattern.rstrip("/")) for pattern in patterns)


def discover(home, patterns=EXCLUDE) -> list[Repo]:
    """The git repositories one level under ``home``: a directory with a ``.git`` directory (a ``.git`` file is a
    worktree of another, not listed), its name not matching ``patterns``."""
    found = []
    try:
        entries = sorted(Path(home).iterdir())
    except OSError:
        return []
    for entry in entries:
        if entry.name.startswith(".") or excluded(entry.name, patterns):
            continue
        try:
            if entry.is_symlink() or not entry.is_dir() or not (entry / ".git").is_dir():
                continue
        except OSError:
            continue
        found.append(Repo(name=entry.name, path=str(entry), added_by="discover"))
    return found


def _workspaces(state_dir) -> tuple[dict, dict]:
    """(name -> directory of workspace-config.json's workspaces, name -> note of main-thread.json's workspace_notes)."""
    try:
        config = json.loads((Path(state_dir) / "workspace-config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        config = {}
    try:
        settings = json.loads((Path(state_dir) / "main-thread.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        settings = {}
    spaces = config.get("workspaces") if isinstance(config, dict) else None
    notes = settings.get("workspace_notes") if isinstance(settings, dict) else None
    return ({str(k): str(v) for k, v in spaces.items() if isinstance(v, str)} if isinstance(spaces, dict) else {},
            {str(k): str(v) for k, v in notes.items() if isinstance(v, str)} if isinstance(notes, dict) else {})


def migrate_from_workspaces(state_dir, home, *, write_mode="worktree") -> list[Repo]:
    """The repositories the old workspace list names (``workspace-config.json``; a device's ``alias:/path`` value and a
    directory that is no git repository's top are left out), with their notes (``main-thread.json``
    ``workspace_notes``); a note that says the repository is frozen (``冻结``) makes it ``frozen``."""
    spaces, notes = _workspaces(state_dir)
    found = []
    for name, directory in spaces.items():
        if not directory.startswith("/") or not (Path(directory) / ".git").is_dir():
            continue
        note = notes.get(name, "")
        found.append(Repo(name=name, path=str(Path(directory)), note=note, status="frozen" if "冻结" in note else "active",
                          write_mode=write_mode, added_by="migrate"))
    return found


def migrate_v1(state_dir, workdir) -> str:
    """The repo map's step of the migration (core/migrate.py), once: ``repos.json`` from the old workspace list and the
    git repositories one level under the home (``workdir``); repositories of a home Covey made are written in place
    (``inplace``), a taken-over home's in a worktree each. The old files stay. Then ``repos.md``. The log line."""
    if path(state_dir).exists():
        render(state_dir, workdir)
        return "repos_v1: repos.json already there"
    home = homes.home_dir(workdir)
    mode = "inplace" if homes.seeded(home) else "worktree"
    repos = migrate_from_workspaces(state_dir, home, write_mode=mode)
    names = {repo.name for repo in repos}
    paths = {os.path.normpath(repo.path) for repo in repos}
    for repo in discover(home):
        if repo.name not in names and os.path.normpath(repo.path) not in paths:
            repos.append(replace(repo, write_mode=mode))
    with _locked(state_dir):
        if not path(state_dir).exists():
            write(state_dir, repos, excluded=list(EXCLUDE))
    render(state_dir, home)
    return f"repos_v1: repos.json written with {len(repos)} repositories ({mode})"


def note_used(state_dir, used, *, now=None) -> None:
    """Take the repositories a turn used (TopicRepos) into the map: ``last_used`` of those listed, and the ones it wrote
    that the map does not list (``added_by`` touched); one it only read the map does not take in. Nothing before the map is
    there."""
    now = time.time() if now is None else now
    used = list(used)
    if not used or not path(state_dir).exists():
        return  # Before the map is made (migrate_v1): a map of the touched ones alone would stop it being made.

    def change(repos):
        listed = {os.path.normpath(repo.path): index for index, repo in enumerate(repos)}
        names = {repo.name for repo in repos}
        for item in used:
            index = listed.get(os.path.normpath(item.path))
            if index is not None:
                repos[index] = replace(repos[index], last_used=now)
                continue
            if item.mode != "write":
                continue  # Only read (a draft, a fixture, something looked at): not the map's.
            name = item.name if item.name not in names else f"{item.name}-{len(repos)}"
            repos.append(Repo(name=name, path=item.path, added_by="touched", last_used=now))
            listed[os.path.normpath(item.path)] = len(repos) - 1
            names.add(name)
        return repos

    try:
        update(state_dir, change)
    except Unreadable:
        return  # A map that cannot be read is the owner's to mend (the settings page says so); a turn leaves it.


# ---- a repository's rules ------------------------------------------------------------------------------------------


def rules_head(directory, rules=("AGENTS.md",), limit=RULES_BYTES) -> tuple[str, str, bool]:
    """(file name, its first ``limit`` bytes cut at a line, cut short) of the first rules file in ``directory`` among
    ``rules``, AGENTS.md and CLAUDE.md; ('', '', False) when it has none."""
    for name in dict.fromkeys([*rules, *topic_repos.RULE_FILES]):
        try:
            with open(Path(directory) / name, "rb") as handle:
                data = handle.read(limit + 1)
        except OSError:
            continue
        cut = len(data) > limit
        text = data[:limit].decode("utf-8", errors="ignore")
        if cut and "\n" in text:
            text = text[:text.rindex("\n") + 1]
        return name, text, cut
    return "", "", False


# ---- git -----------------------------------------------------------------------------------------------------------


def _git(args, cwd, timeout=GIT_SECONDS) -> tuple[int, str, str]:
    """(exit status, stdout, stderr) of one git command, never asking for a password; at ``timeout`` its whole process
    group is stopped (a fetch's ssh holds the pipes after git itself is gone), and it answers 124."""
    env = {"GIT_SSH_COMMAND": "ssh -o BatchMode=yes", **os.environ, "GIT_TERMINAL_PROMPT": "0"}  # A deployer's own ssh command wins.
    try:
        proc = subprocess.Popen(["git", *args], cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=env, start_new_session=True)
    except OSError as exc:
        return 255, "", type(exc).__name__
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired, OSError, ValueError):
            proc.communicate(timeout=5)
        return 124, "", f"git {args[0] if args else ''} 超过 {timeout} 秒没有结束"
    return proc.returncode, (out or "").strip(), (err or "").strip()


def _oid(cwd, ref="HEAD") -> str:
    code, out, _ = _git(["rev-parse", "--verify", "--quiet", ref + "^{commit}"], cwd)
    return out if code == 0 else ""


def _base(repo: Repo) -> tuple[str, str]:
    """(the ref a new worktree starts from, its commit): the remote's main branch after a fetch (``FETCH_SECONDS``),
    else the local one, else the checkout's HEAD."""
    ref, oid, _ = _latest(repo)
    return ref, oid


def _latest(repo: Repo) -> tuple[str, str, str]:
    """``_base``'s (ref, commit) and why it may not be the remote's latest: '' when ``origin/<主干>`` was fetched just
    now, else the fetch's failure (its exit status only: git's words may hold the remote's address) or the branch
    that was not found."""
    branch = repo.branch
    if not branch:
        code, out, _ = _git(["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"], repo.path)
        branch = out.split("/", 1)[1] if code == 0 and "/" in out else ""
    if not branch:
        return "HEAD", _oid(repo.path), "没有找到远端主干（origin/HEAD）"
    code, _, _ = _git(["fetch", "--quiet", "origin", branch], repo.path, timeout=FETCH_SECONDS)
    why = "" if code == 0 else (f"git fetch 超过 {FETCH_SECONDS} 秒没有结束" if code == 124 else f"git fetch 没成功（退出码 {code}）")
    for ref in (f"origin/{branch}", branch):
        oid = _oid(repo.path, ref)
        if oid:
            return ref, oid, why if ref.startswith("origin/") else (why or f"没有 origin/{branch}")
    return "HEAD", _oid(repo.path), why or f"没有 origin/{branch}"


def _has_origin(cwd) -> bool:
    """Whether the repository has a remote ``origin``: one without has no main branch to be behind of (read where it is)."""
    code, out, _ = _git(["remote"], cwd)
    return code == 0 and "origin" in out.split()


def _commit_time(cwd, oid) -> float:
    code, out, _ = _git(["show", "-s", "--format=%ct", oid], cwd)
    return float(out) if code == 0 and out.isdigit() else 0.0


def when(at) -> str:
    """A commit time as the prompt shows it: ``2026-11-27 18:20``; '' for none."""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(at)) if at else ""


def _writable(directory) -> bool:
    """Whether this process can make a file in ``directory`` (a sandbox's read-only roots answer no)."""
    try:
        fd, name = tempfile.mkstemp(prefix=".covey-probe-", dir=str(directory))
    except OSError:
        return False
    os.close(fd)
    with contextlib.suppress(OSError):
        os.unlink(name)
    return True


class RepoError(Exception):
    """Why ``covey repo use`` cannot do what it was asked, in the owner's words; ``code`` its exit status."""

    def __init__(self, text, code=1, *, later=False):
        super().__init__(text)
        self.code = code
        self.later = later  # The sandbox of this turn cannot write it: the next turn can (its writable roots).


def worktree_for(repo: Repo, number, topic_id, *, root=None) -> dict:
    """The topic's worktree of ``repo``, made when missing: ``{worktree, branch, base, base_oid, created}``.

    ``<root>/T<号>-<id8>/<仓库>/`` on branch ``topic/T<号>-<id8>`` from the remote's main branch (``_base``); one already
    there (this topic's earlier turn) is used again. Under a lock per topic and repository, so two commands do not make
    it twice. A ``git worktree add`` that fails takes back the branch it made (it is made before the checkout)."""
    folder = topic_repos.topic_worktrees(number, topic_id, root)
    dest = folder / repo.name
    branch = topic_repos.branch_name(number, topic_id)
    main_git = topic_repos.repo_of(repo.path) or str(Path(repo.path) / ".git")
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RepoError(f"建不了 worktree 目录 {folder}：{exc.strerror or exc}", later=True) from None
    try:
        lock = open(folder / f".{repo.name}.lock", "a+")
    except OSError as exc:
        raise RepoError(f"建不了 worktree 目录 {folder}：{exc.strerror or exc}", later=True) from None
    with lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if (dest / ".git").exists():
            oid = _oid(dest)
            if not oid or os.path.realpath(topic_repos.repo_of(str(dest)) or "") != os.path.realpath(main_git):
                raise RepoError(f"{dest} 已经在了，但不是 {repo.name} 的可用 worktree；先看一眼再处理")
            code, current, _ = _git(["rev-parse", "--abbrev-ref", "HEAD"], dest)
            return {"worktree": str(dest), "branch": current if code == 0 else branch, "base": "", "base_oid": oid,
                    "created": False}
        if not _writable(main_git):
            raise RepoError(f"这一轮的沙箱写不了 {main_git}，建不了 worktree", later=True)
        ref, oid = _base(repo)
        if not oid:
            raise RepoError(f"{repo.name} 找不到可以开分支的提交")
        had, existed = bool(_oid(repo.path, f"refs/heads/{branch}")), dest.exists()
        args = ["-c", "checkout.workers=8", "worktree", "add", *(["-b", branch, str(dest), oid] if not had else [str(dest), branch])]
        for attempt in range(2):
            code, _, err = _git(args, repo.path, timeout=300)
            if code == 0 or attempt or ".lock" not in err:
                break
            _undo(repo, dest, branch, had, existed)  # Another topic's git held a lock of the same repository: once more.
            time.sleep(1)
        if code != 0:
            _undo(repo, dest, branch, had, existed)
            later = "Read-only file system" in err or "Permission denied" in err
            raise RepoError(f"建 worktree 失败：{(err.splitlines() or ['git 退出码 %d' % code])[-1][:300]}", later=later)
        base_oid = oid if not had else (_oid(dest) or oid)
        return {"worktree": str(dest), "branch": branch, "base": f"{ref}@{base_oid[:8]}", "base_oid": base_oid,
                "created": True}


def _used_marker(tree) -> Path:
    tree = Path(tree)
    return tree.parent / (tree.name + READ_USED)


def touch_read(tree, now=None, *, make=False) -> None:
    """Mark the read worktree ``tree`` used now (``READ_IDLE`` counts from it); a marker that cannot be written is left.
    Only ``make`` (the checkout of ``tree`` is complete: ``read_worktree`` under its lock) writes a marker that is not there:
    a topic touching its tree while another one checks it out again must not mark a half checkout readable."""
    if not tree or not Path(tree).is_dir():
        return
    now = time.time() if now is None else now
    marker = _used_marker(tree)
    with contextlib.suppress(OSError):
        if make:
            marker.touch()
        os.utime(marker, (now, now))


def _usable(tree: Path, oid: str, main_git: str) -> bool:
    """Whether ``tree`` is a worktree of the repository whose ``.git`` is ``main_git`` with ``oid`` checked out."""
    return ((tree / ".git").is_file() and _oid(tree) == oid
            and os.path.realpath(topic_repos.repo_of(str(tree)) or "") == os.path.realpath(main_git))


def read_changes(tree) -> int | None:
    """How many uncommitted changes (``git status --porcelain``, untracked files included) the read worktree ``tree`` has:
    a read worktree is every topic's latest main branch, so any is a write some turn made into it (P1-9). None when git
    cannot tell (a slow status, a broken tree)."""
    return _changes(tree) if tree and Path(tree).is_dir() else None


def _ready(tree: Path, oid: str, main_git: str) -> bool:
    """Whether the read worktree ``tree`` may be handed out without the lock: ``oid`` checked out, its checkout complete
    (its ``.used`` marker, P1-10) and nothing changed in it (P1-9; a status git cannot tell counts as unchanged)."""
    return _used_marker(tree).exists() and _usable(tree, oid, main_git) and not read_changes(tree)


def _read_names(oid: str) -> list[str]:
    """The directories a commit's read worktree may be in, in order (``READ_SPARES``)."""
    return [oid[:12]] + [f"{oid[:12]}-{index}" for index in range(2, READ_SPARES + 1)]


def _prune_read(repo: Repo, folder: Path, keep: str, now: float) -> None:
    """Remove ``repo``'s read worktrees in ``folder`` no turn used for ``READ_IDLE`` (by their marker, else their
    directory), ``keep`` aside. ``git worktree remove`` without ``--force``: one with changes or untracked files stays
    (``covey repo prune-plan`` does not list it either; the owner sees it in the folder); never anything but a worktree."""
    try:
        trees = [entry for entry in folder.iterdir() if entry.is_dir() and not entry.name.startswith(".")]
    except OSError:
        return
    for tree in trees:
        if tree.name == keep:
            continue
        marker = _used_marker(tree)
        try:
            used = marker.stat().st_mtime if marker.exists() else tree.stat().st_mtime
        except OSError:
            continue
        if now - used < READ_IDLE or not (tree / ".git").is_file():
            continue
        code, _, _ = _git(["worktree", "remove", str(tree)], repo.path, timeout=120)
        if code == 0:
            with contextlib.suppress(OSError):
                marker.unlink()


def read_worktree(repo: Repo, *, root=None, now=None) -> dict:
    """The detached worktree of ``repo``'s latest main branch, for reading: ``{worktree, base, base_oid, base_at, stale,
    created}``.

    ``<root>/_read/<仓库>/<提交前 12 位>/`` (core/topic_repos.py ``read_worktrees``) at ``origin/<主干>`` after a fetch
    (``_latest``); one already there for that commit (another topic's, an earlier turn's) is used again without writing
    anything, once its checkout is complete (its ``.used`` marker: a topic coming while another checks it out waits for the
    lock instead of reading a directory with only ``.git`` in it, P1-10). One some turn wrote into (changes in
    ``git status``) is not read again: the next free name (``_read_names``) gets a clean one, the changed one stays for
    the owner to look at (P1-9). ``stale``: why it may be behind (its fetch failed: the remote branch as last fetched), ''
    when fetched now. Made under a lock per repository; then the repository's read worktrees no turn used for
    ``READ_IDLE`` are removed (``_prune_read``). Raises RepoError (``later``: this turn's sandbox cannot write it) when
    there is none to read."""
    now = time.time() if now is None else now
    ref, oid, stale = _latest(repo)
    if not oid or ref == "HEAD":
        raise RepoError(stale or f"{repo.name} 找不到远端主干的提交")
    folder = topic_repos.read_worktrees(root) / repo.name
    main_git = topic_repos.repo_of(repo.path) or str(Path(repo.path) / ".git")
    made = {"base": f"{ref}@{oid[:8]}", "base_oid": oid, "base_at": _commit_time(repo.path, oid), "stale": stale,
            "created": False}
    for name in _read_names(oid):  # Another topic's, or an earlier turn's: nothing to write (a read-only sandbox too).
        if _ready(folder / name, oid, main_git):
            touch_read(folder / name, now)
            return {"worktree": str(folder / name), **made}
    try:
        folder.mkdir(parents=True, exist_ok=True)
        lock = open(folder / ".lock", "a+")
    except OSError as exc:
        raise RepoError(f"建不了只读 worktree 目录 {folder}：{exc.strerror or exc}", later=True) from None
    with lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        dest = None
        for name in _read_names(oid):
            tree = folder / name
            if tree.exists() and not (tree / ".git").exists():
                shutil.rmtree(tree, ignore_errors=True)  # What an add cut short left (no worktree): only ever made here.
                _git(["worktree", "prune"], repo.path)
            if not tree.exists():
                dest = dest or tree
                continue
            if not _usable(tree, oid, main_git) or read_changes(tree):
                continue  # Another commit's, or changed by some turn: left as it is.
            # Complete: no checkout runs while the lock is held (one from before the marker said so gets it now).
            touch_read(tree, now, make=True)
            _prune_read(repo, folder, tree.name, now)
            return {"worktree": str(tree), **made}
        if dest is None:
            raise RepoError(f"{repo.name} {oid[:8]} 的只读 worktree 都有改动（{folder}），没有干净的可读")
        if not _writable(main_git):
            raise RepoError(f"这一轮的沙箱写不了 {main_git}，建不了只读 worktree", later=True)
        args = ["-c", "checkout.workers=8", "worktree", "add", "--detach", str(dest), oid]
        for attempt in range(2):
            code, _, err = _git(args, repo.path, timeout=300)
            if code == 0 or attempt or ".lock" not in err:
                break
            time.sleep(1)  # Another topic's git held a lock of the same repository: once more.
        if code != 0:
            shutil.rmtree(dest, ignore_errors=True)
            _git(["worktree", "prune"], repo.path)
            later = "Read-only file system" in err or "Permission denied" in err
            raise RepoError(f"建只读 worktree 失败：{(err.splitlines() or ['git 退出码 %d' % code])[-1][:300]}", later=later)
        touch_read(dest, now, make=True)  # Its checkout is complete: from now on the lock-free path may hand it out.
        _prune_read(repo, folder, dest.name, now)
    return {"worktree": str(dest), **made, "created": True}


# ---- the command ---------------------------------------------------------------------------------------------------


def _context() -> dict:
    return {"state": cenv.get("COVEY_STATE", ""), "home": cenv.get("COVEY_HOME", ""), "topic": cenv.get("COVEY_TOPIC", "")}


def _topic(state, topic) -> dict:
    if not state or not topic:
        return {}
    try:
        data = json.loads((Path(state) / "topics" / topic / "topic.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _resolve_repo(repos, key, home) -> Repo | None:
    """The map's repository ``key`` names, or the git repository at the path ``key`` (relative to the home) that the
    map does not list yet."""
    found = find(repos, key)
    if found is not None:
        return found
    target = Path(key) if key.startswith("/") else Path(home or os.getcwd()) / key
    top, linked = topic_repos.checkout_of(target)
    if not top or linked:
        return None
    found = next((repo for repo in repos if os.path.normpath(repo.path) == os.path.normpath(top)), None)
    return found or Repo(name=Path(top).name, path=top, added_by="touched")


def _undo(repo: Repo, dest: Path, branch: str, had: bool, existed: bool) -> None:
    """What a failed ``git worktree add`` left: the branch it made before the checkout, a half checkout in ``dest``
    (only when ``dest`` was not there before it), the worktree's record."""
    if not had and _oid(repo.path, f"refs/heads/{branch}"):
        _git(["branch", "-D", branch], repo.path)
    if not existed:
        shutil.rmtree(dest, ignore_errors=True)
    _git(["worktree", "prune"], repo.path)


def use(context, key, *, write=False, now=None) -> dict:
    """``covey repo use``: what it prints, as data. Raises RepoError."""
    now = time.time() if now is None else now
    state, topic = context.get("state", ""), context.get("topic", "")
    repos = read(state) if state else []
    repo = _resolve_repo(repos, key, context.get("home", ""))
    if repo is None:
        names = "、".join(r.name for r in repos[:20]) or "（还没有）"
        raise RepoError(f"常用路径里没有 {key}，也不是一个 git 仓库的路径。常用路径有：{names}", code=2)
    out = {"name": repo.name, "path": repo.path, "mode": "write" if write else "read", "status": repo.status,
           "write_mode": repo.write_mode, "worktree": "", "branch": "", "base": "", "base_oid": "", "note": "",
           "kind": repo.kind, "device": repo.device}
    if write and repo.status != "active":
        raise RepoError(f"{KIND_WORDS.get(repo.kind, '仓库')} {repo.name} {STATUS_WORDS.get(repo.status, repo.status)}，不能改。", code=3)
    if repo.device:
        return _use_device(state, repo, out, write, now)
    directory = repo.path
    if not write and repo.write_mode != "inplace" and _has_origin(repo.path):
        # Read in the latest main branch, not the main checkout (T900 W0-5); without one, the checkout, said to be maybe behind.
        try:
            made = read_worktree(repo, now=now)
        except RepoError as exc:
            out["note"] = f"没能建最新主干的只读 worktree（{str(exc).rstrip('。')}），读的是仓库目录本身，可能落后，未能确认是最新主干。"
            out["stale"] = 1
        else:
            out.update({key: made[key] for key in ("worktree", "base", "base_oid", "base_at")}, created=made["created"],
                       stale=int(bool(made["stale"])))
            directory = made["worktree"]
            if made["stale"]:
                out["note"] = f"{made['stale']}：这是本机上次取到的 {made['base'].split('@')[0]}，可能落后，未能确认是最新主干。"
    elif write and repo.write_mode == "inplace":
        out.update(worktree=repo.path, base_oid="" if repo.kind == "dir" else _oid(repo.path))
        if not _writable(repo.path):
            out["note"] = (f"这一轮的沙箱写不了这个{KIND_WORDS.get(repo.kind, '仓库')}；已记下，下一轮它会在可写范围里，"
                           "这一轮先只读。")
    elif write:
        data = _topic(state, topic)
        number = data.get("topic_number") if type(data.get("topic_number")) is int else 0
        known = next((entry for entry in topic_repos.of_topic(data) if entry.name == repo.name and entry.worktree
                      and os.path.normpath(entry.worktree) != os.path.normpath(repo.path)
                      and not topic_repos.is_read_worktree(entry.worktree)), None)
        try:
            if known is not None and (Path(known.worktree) / ".git").exists():  # This topic's, from an earlier turn.
                made = {"worktree": known.worktree, "branch": known.branch, "base": known.base,
                        "base_oid": _oid(known.worktree), "created": False}
            else:
                made = worktree_for(repo, number, topic or "topic")
        except RepoError as exc:
            if not exc.later:
                raise
            out["note"] = f"{exc}。已记下，下一轮这个仓库会在可写范围里，这一轮先只读。"
        else:
            out.update({key: made[key] for key in ("worktree", "branch", "base", "base_oid")})
            out["created"] = made["created"]
            directory = made["worktree"]
    name, text, cut = rules_head(directory, repo.rules)
    out["rules"] = {"file": name, "text": text, "cut": cut}
    task_dir = topic_repos.task_dir_of(state) if state else None
    if task_dir is not None and task_dir.is_dir():
        topic_repos.add_request(task_dir, {"at": now, "name": repo.name, "path": repo.path, "mode": out["mode"],
                                           "worktree": out["worktree"], "branch": out["branch"], "base": out["base"],
                                           "base_oid": out["base_oid"], "rules_file": name, "rules_cut": cut,
                                           "pending": write and bool(out["note"]), "created": bool(out.get("created")),
                                           **({"base_at": out["base_at"]} if out.get("base_at") else {}),
                                           **({"stale": 1} if out.get("stale") else {})})
    return out


DEVICE_HINT = "这条路径在设备 {title}上，本机读不到；设备上的读写命令还没有开通，现在只能登记它、看它的规范开头。"


def _use_device(state, repo, out, write, now) -> dict:
    """``use`` of a device's entry: nothing on this machine; the rules head through the device's ``get`` (RULES_BYTES at most,
    when devices are on and the alias can be reached), and the request recorded with its ``device`` (``--write`` only
    records: no worktree on a device, no diff of it)."""
    alias, where = device_path(repo.path)
    try:
        config = device_list.current()
    except device_list.DeviceConfigError:
        config = None
    device = (config.devices.get(alias) or config.watched.get(alias)) if config is not None else None
    if write and device is not None and device.access == "read":
        raise RepoError(f"设备 {alias} 是只读的（设置里「设备」页可以改），{repo.name} 不能改。", code=3)
    title = f"{device.title}（{alias}）" if device is not None and device.title != alias else alias
    out.update(device=alias, remote_path=where, note=DEVICE_HINT.format(title=title))
    name, text, cut = "", "", False
    if not device_list.enabled():
        out["rules_note"] = "多设备没开（COVEY_DEVICES），没能读它的规范开头。"
    elif config is None or alias not in config.devices:
        out["rules_note"] = f"设备 {alias} 不在 devices.json 里，或没有 ssh，没能读它的规范开头。"
    else:
        names = list(dict.fromkeys([*repo.rules, *topic_repos.RULE_FILES]))
        name, text, cut, why = device_list.read_head(state, alias, where, names, limit=RULES_BYTES, timeout=DEVICE_SECONDS,
                                                     config=config)
        if why:
            out["rules_note"] = f"没能读它的规范开头：{why}。"
    out["rules"] = {"file": name, "text": text, "cut": cut}
    task_dir = topic_repos.task_dir_of(state) if state else None
    if task_dir is not None and task_dir.is_dir():
        topic_repos.add_request(task_dir, {"at": now, "name": repo.name, "path": repo.path, "device": alias, "mode": out["mode"],
                                           "worktree": "", "branch": "", "base": "", "base_oid": "", "rules_file": name,
                                           "rules_cut": cut, "pending": False, "created": False})
    return out


def _show_use(out) -> str:
    word = KIND_WORDS.get(out.get("kind") or "git", "仓库")
    if out.get("device"):
        lines = [f"{word} {out['name']}（{'写' if out['mode'] == 'write' else '读'}，设备 {out['device']}）",
                 f"设备上的路径：{out.get('remote_path') or out['path']}", out.get("note") or ""]
        if out.get("rules_note"):
            lines.append(out["rules_note"])
        rules = out.get("rules") or {}
        if rules.get("file"):
            lines += ["", f"先照它的规范做（{rules['file']} 开头{'，后面还有' if rules.get('cut') else ''}）：", "-----",
                      rules["text"].rstrip(), "-----"]
        elif not out.get("rules_note"):
            lines.append(f"这个{word}没有 AGENTS.md 或 CLAUDE.md。")
        return "\n".join(line for line in lines if line is not None)
    lines = [f"{word} {out['name']}（{'写' if out['mode'] == 'write' else '读'}）",
             f"{'仓库目录' if word == '仓库' else word}：{out['path']}"]
    if out["mode"] != "write" and out.get("worktree") and out["worktree"] != out["path"]:
        lines.append(f"在这里读：{out['worktree']}（最新主干的只读 worktree，{read_words(out)}；detached，不要在这里改）")
    elif out.get("worktree") and out["worktree"] != out["path"]:
        lines.append(f"在这里改：{out['worktree']}（{'新建' if out.get('created') else '本话题已有'}的 worktree）")
        if out.get("branch"):
            lines.append(f"分支：{out['branch']}" + (f"（基于 {out['base']}）" if out.get("base") else ""))
    elif out.get("worktree"):
        lines.append(f"这个{word}原地改：直接在{word}目录里改。")
    if out.get("note"):
        lines.append(out["note"])
    rules = out.get("rules") or {}
    if rules.get("file"):
        lines += ["", f"先照这个{word}的规范做（{rules['file']} 开头{'，后面还有，要用时读全文' if rules.get('cut') else ''}）：",
                  "-----", rules["text"].rstrip(), "-----"]
    else:
        lines.append(f"这个{word}没有 AGENTS.md 或 CLAUDE.md。")
    return "\n".join(lines)


def read_words(item) -> str:
    """``origin/main 的提交 5994b3c2，提交于 2026-11-27 18:20`` of a read worktree (``use``'s output or a topic's entry)."""
    ref, _, short = str(item.get("base") or "").partition("@")
    short = short or str(item.get("base_oid") or "")[:8]
    at = when(item.get("base_at") or 0)
    return f"{ref or '主干'} 的提交 {short}" + (f"，提交于 {at}" if at else "")


def _changes(directory) -> int | None:
    code, out, _ = _git(["status", "--porcelain"], directory, timeout=STATUS_SECONDS)
    return len(out.splitlines()) if code == 0 else None


def status(context) -> list[dict]:
    """``covey repo status``: the topic's repositories (``topic.json``, and this task's requests not folded in yet),
    each with its directory, branch and uncommitted changes."""
    data = _topic(context.get("state", ""), context.get("topic", ""))
    seen = {}
    for repo in topic_repos.of_topic(data):
        seen[repo.name] = {"name": repo.name, "mode": repo.mode, "path": repo.path, "worktree": repo.worktree,
                           "branch": repo.branch, "rules_read": bool(repo.rules_read_at)}
    for item in data.get("repos") or () if isinstance(data.get("repos"), list) else ():
        if isinstance(item, dict) and item.get("device") and device_path(item.get("path"))[0] and str(item.get("name") or ""):
            seen.setdefault(str(item["name"]), {"name": str(item["name"]), "mode": "write" if item.get("mode") == "write" else "read",
                                                "path": str(item["path"]), "worktree": "", "branch": "", "rules_read": True,
                                                "device": str(item["device"])})
    task_dir = topic_repos.task_dir_of(context["state"]) if context.get("state") else None
    for item in topic_repos.requests(task_dir) if task_dir else ():
        row = seen.setdefault(str(item.get("name")), {"name": item.get("name"), "mode": "read", "path": item.get("path", ""),
                                                      "worktree": "", "branch": "", "rules_read": True,
                                                      **({"device": str(item["device"])} if item.get("device") else {})})
        if item.get("mode") == "write":
            row["mode"] = "write"
        for key in ("worktree", "branch"):
            if item.get(key):
                row[key] = item[key]
    for row in seen.values():
        local = not row.get("device") and (row["worktree"] or row["path"])
        row["changes"] = _changes(row["worktree"] or row["path"]) if local and os.path.isdir(row["worktree"] or row["path"]) else None
    return list(seen.values())


def _show_status(rows) -> str:
    if not rows:
        return "本话题还没有用到仓库。要用：covey repo use <仓库> [--write]。"
    lines = []
    for row in rows:
        where = row["worktree"] or row["path"]
        if row.get("device"):
            lines.append(f"- {row['name']}（{'写' if row['mode'] == 'write' else '读'}）设备 {where}；设备上的改动这里查不了")
            continue
        changes = "未知" if row["changes"] is None else (f"{row['changes']} 个文件没提交" if row["changes"] else "没有未提交的改动")
        lines.append(f"- {row['name']}（{'写' if row['mode'] == 'write' else '读'}）{where}"
                     + (f"，分支 {row['branch']}" if row["branch"] else "") + f"；{changes}")
    return "本话题用到的仓库：\n" + "\n".join(lines)


def _worktree_row(top: Path) -> dict:
    code, branch, _ = _git(["rev-parse", "--abbrev-ref", "HEAD"], top, timeout=STATUS_SECONDS)
    changes = _changes(top)
    main = topic_repos.repo_of(str(top))
    upstream = ""
    for ref in ("origin/HEAD", "origin/main", "origin/master", "main", "master"):
        if _oid(top, ref):
            upstream = ref
            break
    merged = bool(upstream) and _git(["merge-base", "--is-ancestor", "HEAD", upstream], top, timeout=STATUS_SECONDS)[0] == 0
    try:
        idle = time.time() - max((top / name).stat().st_mtime for name in (".git", "."))
    except OSError:
        idle = 0.0
    main_dir = str(Path(main).parent) if main else ""
    row = {"path": str(top), "repo": main_dir, "branch": branch if code == 0 else "", "changes": changes,
           "merged": merged, "idle_hours": round(idle / 3600, 1)}
    row["prunable"] = bool(changes == 0 and merged and idle >= PRUNE_IDLE and main_dir)
    if row["prunable"]:
        row["commands"] = [f"git -C {main_dir} worktree remove {top}"] + (
            [f"git -C {main_dir} branch -d {row['branch']}"] if row["branch"] and row["branch"] != "HEAD" else [])
    return row


def prune_plan(legacy="", root=None) -> list[dict]:
    """``covey repo prune-plan``, read only: every topic worktree under ``<数据根>/worktrees/T*/`` (and with ``legacy``
    each worktree one level under that directory: a ``.git`` file), whether it is clean, merged and idle a day, and
    the commands that would remove it. Nothing is run."""
    rows = []
    base = Path(root) if root is not None else topic_repos.topic_worktrees(0, "x").parent
    tops = []
    try:
        for folder in sorted(base.iterdir()):
            if folder.is_dir() and folder.name.startswith("T"):
                tops += [entry for entry in sorted(folder.iterdir()) if (entry / ".git").is_file()]
    except OSError:
        pass
    if legacy:
        try:
            tops += [entry for entry in sorted(Path(legacy).iterdir())
                     if not entry.is_symlink() and entry.is_dir() and (entry / ".git").is_file()]
        except OSError:
            pass
    for top in tops:
        row = _worktree_row(top)
        row["legacy"] = bool(legacy) and top.parent == Path(legacy)
        rows.append(row)
    return rows


def _show_prune(rows) -> str:
    if not rows:
        return "没有找到 worktree。"
    ready = [row for row in rows if row["prunable"]]
    lines = [f"共 {len(rows)} 个 worktree，{len(ready)} 个可以清理（干净、已合入、一天没动）。下面只是清单和命令，没有执行：", ""]
    for row in rows:
        why = "可以清理" if row["prunable"] else "保留：" + "、".join(filter(None, [
            "有未提交的改动" if row["changes"] else ("状态未知" if row["changes"] is None else ""),
            "" if row["merged"] else "没合入主干", "" if row["idle_hours"] >= PRUNE_IDLE / 3600 else "最近动过"]))
        lines.append(f"- {row['path']}（{row['branch'] or '?'}）{why}")
    commands = [command for row in ready for command in row.get("commands", ())]
    if commands:
        lines += ["", "要清理时照着跑（先确认）：", *commands]
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="covey_repo.py", description="常用路径：列出、声明和查看本话题用到的仓库和目录")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("list", help="常用路径")
    use_parser = sub.add_parser("use", help="声明本话题用到一个仓库")
    use_parser.add_argument("repo")
    use_parser.add_argument("--write", action="store_true", help="要改：建或复用本话题的 worktree")
    sub.add_parser("status", help="本话题用到的仓库")
    prune = sub.add_parser("prune-plan", help="只读：列出可以清理的 worktree")
    prune.add_argument("--legacy", default="", help="另看这个目录下的旧 worktree")
    args, rest = parser.parse_known_args(argv)
    if "--json" in rest:  # Also after the subcommand: covey repo list --json.
        args.json, rest = True, [item for item in rest if item != "--json"]
    if rest:
        parser.error("unrecognized arguments: " + " ".join(rest))
    context = _context()
    if args.action == "list":
        repos = read(context["state"]) if context["state"] else []
        used = {repo.name for repo in topic_repos.of_topic(_topic(context["state"], context["topic"]))}
        if args.json:
            print(json.dumps([{**asdict(repo), "used": repo.name in used} for repo in repos], ensure_ascii=False))
        elif not repos:
            print("常用路径是空的。")
        else:
            for repo in repos:
                print(f"{repo.name}\t{KIND_WORDS.get(repo.kind, repo.kind)}\t{STATUS_WORDS.get(repo.status, repo.status)}\t"
                      f"{repo.path}\t{repo.note}" + ("\t（本话题用到）" if repo.name in used else ""))
        return 0
    if args.action == "use":
        try:
            out = use(context, args.repo, write=args.write)
        except RepoError as exc:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False) if args.json else str(exc),
                  file=sys.stdout if args.json else sys.stderr)
            return exc.code
        print(json.dumps(out, ensure_ascii=False) if args.json else _show_use(out))
        return 0
    if args.action == "status":
        rows = status(context)
        print(json.dumps(rows, ensure_ascii=False) if args.json else _show_status(rows))
        return 0
    rows = prune_plan(args.legacy)
    print(json.dumps(rows, ensure_ascii=False) if args.json else _show_prune(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
