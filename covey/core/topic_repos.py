"""The repositories a topic uses (``topic.json`` ``repos``), the repository of a path, and a task's requests to use one.

``repo_of`` (moved here from app/repo_peers.py, which keeps the name) finds the repository of a path: the nearest
ancestor holding ``.git``, a linked worktree counting as the repository its ``.git`` file names; cached for ``TTL``
seconds, at most ``DEPTH`` levels up. A task asks for a repository through ``covey repo use`` (bin/covey_repo.py), which
writes only the task's own ``runs/<任务键>/repos.json`` (core/run_dir.py ``REPOS``) with the worktree's ``base_oid``;
the Bridge folds it into the topic when the turn settles (app/repos_turn.py ``settle``), and core/diff.py makes the diff
of a worktree made in the middle of a turn from that base. ``touched`` reads which repositories a turn's tool calls
(core/trace.py) read or wrote and whether they read a repository's rules; ``writable_roots`` is what a Codex turn in
``workspace-write`` may write beside its checkpoints (docs/configuration.md 主目录与仓库地图).
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import threading
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from covey.core import env as cenv
from covey.core.run_dir import REPOS
from covey.core.state import write_private_text
from covey.core.task_processes import TASK_DIR_ENV

TTL = 10 * 60
DEPTH = 12
MODES = ("read", "write")
SOURCES = ("declared", "cli", "touched", "checkpoint")
RULE_FILES = ("AGENTS.md", "CLAUDE.md")  # Read before a repository's own ``rules`` are known (a repository not in the map).
# The step titles of core/protocol.py STEP_TITLES (and Codex's own) by what they do to a path.
READ_TITLES = frozenset({"读取文件", "搜索文件", "查找文件", "浏览目录"})
WRITE_TITLES = frozenset({"修改文件", "写入文件"})
COMMAND_TITLES = frozenset({"执行命令", "后台命令", "更新检查点"})
ABSOLUTE = re.compile(r"(?<![\w.~$@-])(/[^\s'\"`;|&<>(),]+)")
CD = re.compile(r"(?:^|[;&|(]|&&|\|\|)\s*(?:cd|pushd)\s+(['\"]?)([^\s'\";&|)]+)\1")
GIT_C = re.compile(r"\bgit\s+-C\s+(['\"]?)([^\s'\";&|)]+)\1")
GIT_WRITE = re.compile(r"\bgit\b(?:\s+-[Cc]\s+\S+)*\s+(?:commit|apply|am|merge|rebase|cherry-pick|revert|mv|rm|restore|"
                       r"checkout\s+--|reset\s+--hard|stash(?!\s+list))\b")
IN_PLACE = re.compile(r"\b(?:sed|perl)\s+(?:-[a-zA-Z]*\s+)*-[a-zA-Z]*i")
REDIRECT = re.compile(r"(?<![0-9&<>])>{1,2}\s*(['\"]?)([^\s'\";&|<>)]+)\1")
READERS = re.compile(r"\b(?:cat|sed|head|tail|less|more|bat|nl|awk)\b")

cache: dict[str, tuple[float, str]] = {}  # repo_of's: path -> (when, repository); app/repo_peers.py names it too.
_lock = threading.Lock()


@dataclass(frozen=True)
class TopicRepo:
    name: str
    path: str
    mode: str = "read"  # MODES
    worktree: str = ""
    branch: str = ""
    base: str = ""  # "<remote branch>@<short oid>" as shown.
    base_oid: str = ""  # The full commit the worktree started from: the diff's base when no snapshot was taken.
    source: str = "touched"  # SOURCES
    first_at: float = 0.0
    rules_read_at: float = 0.0
    rules_miss: int = 0
    # A head of its rules cut short was shown (inlined, or printed by ``covey repo use``): no reading, its rules stay unread,
    # and it is not shown again until a turn writes it without them (app/repos_turn.py ``inline_due``).
    rules_inlined_at: float = 0.0
    base_at: float = 0.0  # A read worktree's (``read_worktrees``): the commit time of ``base_oid``.
    stale: int = 0  # A read worktree's: 1 when its fetch failed, the remote branch as last fetched (it may be behind).


_FIELDS = {item.name: item for item in fields(TopicRepo)}


def from_dict(item) -> TopicRepo | None:
    """A ``topic.json`` ``repos`` entry as a TopicRepo; keys a newer release wrote are left out, a malformed one is None."""
    if not isinstance(item, dict) or not str(item.get("name") or "").strip() or not str(item.get("path") or "").startswith("/"):
        return None
    known = {}
    for key, spec in _FIELDS.items():
        if key not in item:
            continue
        value = item[key]
        if spec.type in ("float", "int"):
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                continue
            value = int(value) if spec.type == "int" else float(value)
        elif not isinstance(value, str):
            continue
        known[key] = value
    if known.get("mode") not in MODES:
        known["mode"] = "read"
    if known.get("source") not in SOURCES:
        known["source"] = "touched"
    return TopicRepo(**known)


def to_dict(repo: TopicRepo) -> dict:
    return asdict(repo)


def of_topic(data) -> list[TopicRepo]:
    """The repositories ``topic.json`` (``data``) lists, in its order; [] for none."""
    found = data.get("repos") if isinstance(data, dict) else None
    return [repo for repo in map(from_dict, found if isinstance(found, list) else ()) if repo is not None]


def where(repo: TopicRepo) -> str:
    """The directory a topic works in for ``repo``: its worktree, else the repository itself."""
    return repo.worktree or repo.path


def repo_of(workdir, now=None) -> str:
    """The repository of ``workdir`` as a path ('' for none): its ``.git`` directory's, or for a linked worktree the
    main one's, which its ``.git`` file names."""
    key = str(workdir or "")
    if not key.startswith("/"):
        return ""
    now = time.time() if now is None else now
    with _lock:
        hit = cache.get(key)
        if hit and now - hit[0] < TTL:
            return hit[1]
    found = _find(Path(key))
    with _lock:
        if len(cache) > 256:
            cache.clear()
        cache[key] = (now, found)
    return found


def _find(path: Path) -> str:
    try:
        for level, directory in enumerate([path, *path.parents]):
            if level > DEPTH or str(directory) == "/":
                return ""
            git = directory / ".git"
            if git.is_dir():
                return str(git.resolve())
            if git.is_file():
                return _linked(git)
    except (OSError, ValueError):
        return ""
    return ""


def _linked(git: Path) -> str:
    """The main repository's ``.git`` of a worktree's ``.git`` file ("gitdir: <main>/.git/worktrees/<name>"), else its own path."""
    try:
        text = git.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    if not text.startswith("gitdir:"):
        return ""
    target = Path(text[len("gitdir:"):].strip())
    if not target.is_absolute():
        target = git.parent / target
    parts = target.resolve().parts
    if "worktrees" not in parts:
        return str(target.resolve())
    last = len(parts) - 1 - parts[::-1].index("worktrees")  # <main>/.git/worktrees/<name>: the main may be under a "worktrees" too.
    return str(Path(*parts[:last]))


def checkout_of(path) -> tuple[str, bool]:
    """(the top of the checkout ``path`` is in, whether it is a linked worktree): the nearest ancestor with ``.git``
    (a directory: the repository itself; a file: a worktree of it); ('', False) for none."""
    try:
        start = Path(os.path.normpath(str(path)))
        for level, directory in enumerate([start, *start.parents]):
            if level > DEPTH or str(directory) == "/":
                return "", False
            git = directory / ".git"
            if git.is_dir():
                return str(directory), False
            if git.is_file():
                return str(directory), True
    except (OSError, ValueError):
        pass
    return "", False


def requests_path(task_dir) -> Path:
    return Path(task_dir) / REPOS


def requests(task_dir) -> list[dict]:
    """The ``covey repo use`` requests a task wrote (``{at, name, mode, worktree, branch, base_oid}`` each); [] for none."""
    try:
        data = json.loads(requests_path(task_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    found = data.get("requests") if isinstance(data, dict) else None
    return [item for item in found if isinstance(item, dict)] if isinstance(found, list) else []


def add_request(task_dir, item: dict) -> None:
    """Append ``item`` to the task's ``repos.json``: only its own task's commands write it, under a lock beside it (two
    commands at once each keep theirs)."""
    Path(task_dir).mkdir(parents=True, exist_ok=True)
    with open(Path(task_dir) / (REPOS + ".lock"), "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        found = requests(task_dir)
        found.append(item)
        write_private_text(requests_path(task_dir),
                           json.dumps({"version": 1, "requests": found}, ensure_ascii=False, indent=2) + "\n")


def task_dir_of(state_dir, job=None) -> Path | None:
    """The run directory of ``job``'s task (``<state>/runs/<任务键>/``, the one its runner exports as ``COVEY_TASK_DIR``);
    without a job, the task this process runs (``COVEY_TASK_DIR``); None when neither is known."""
    if job is not None and (getattr(job, "message_id", "") or getattr(job, "job_id", "")):
        from covey.core.jobs import job_key
        from covey.core.run_dir import run_dir
        return run_dir(Path(state_dir), job_key(str(getattr(job, "message_id", "") or ""), getattr(job, "job_id", "")))
    named = cenv.get(TASK_DIR_ENV, "")
    return Path(named) if named else None


def topic_worktrees(number, topic_id, root=None) -> Path:
    """``<数据根>/worktrees/T<号>-<id8>``: the directory of a topic's worktrees, one per repository under it."""
    if root is None:
        from covey.core.paths import component_dir
        root = component_dir("worktrees", preserve_legacy=False)
    id8 = str(topic_id or "")[:8] or "topic"
    return Path(root) / (f"T{number}-{id8}" if number else id8)


READ_DIR = "_read"


def read_worktrees(root=None) -> Path:
    """``<数据根>/worktrees/_read``: the detached worktrees ``covey repo use`` reads a repository's latest main branch in,
    ``<仓库>/<提交前 12 位>/`` under it, shared by every topic reading that commit (core/repomap.py ``read_worktree``)."""
    return topic_worktrees(0, "x", root).parent / READ_DIR


def is_read_worktree(path, root=None) -> bool:
    """Whether ``path`` is one of ``read_worktrees``: never a topic's own worktree to write in."""
    return bool(path) and os.path.normpath(str(path)).startswith(os.path.normpath(str(read_worktrees(root))) + os.sep)


def branch_name(number, topic_id) -> str:
    """The branch of a topic's worktrees: ``topic/T<号>-<id8>`` (the same in each repository)."""
    return "topic/" + topic_worktrees(number, topic_id, root="/").name


def writable_roots(topic_data, *, task_dir=None, worktrees=None, frozen=(), inplace=()) -> list[str]:
    """What a Codex turn in ``workspace-write`` may write beside its checkpoints: the task's run directory (``covey repo
    use`` writes its requests there), the topic's worktrees directory (``worktrees``, made here so a repository's
    worktree made in the first turn is inside it), and for each repository the topic writes to (``mode`` write, its path
    not in ``frozen``) the main repository's ``.git``, its worktree, and the repository itself only when it is written
    in place (its path in ``inplace``, or its worktree the repository). A repository asked for in the middle of a turn
    is in the next turn's (the roots are fixed when a turn starts)."""
    roots = []
    if task_dir:
        roots.append(str(task_dir))
    if worktrees is not None:
        try:
            Path(worktrees).mkdir(parents=True, exist_ok=True)
            roots.append(str(worktrees))
        except OSError:
            pass
    frozen = {os.path.normpath(str(path)) for path in frozen}
    inplace = {os.path.normpath(str(path)) for path in inplace}
    for repo in of_topic(topic_data):
        if repo.mode != "write" or os.path.normpath(repo.path) in frozen:
            continue
        git = repo_of(repo.path)
        if git:
            roots.append(git)
        if os.path.normpath(repo.path) in inplace or (repo.worktree and os.path.normpath(repo.worktree) == os.path.normpath(repo.path)):
            roots.append(repo.path)
        elif repo.worktree and (worktrees is None or not os.path.normpath(repo.worktree).startswith(os.path.normpath(str(worktrees)) + os.sep)):
            roots.append(repo.worktree)
    return list(dict.fromkeys(roots))


# ---- what a turn touched -------------------------------------------------------------------------------------------


def _steps(trace):
    """Every step of ``trace`` (a ToolTrace or its snapshot), its subtasks' too."""
    stack = [trace]
    while stack:
        node = stack.pop()
        if node is None:
            continue
        if isinstance(node, dict):
            steps, children = node.get("steps") or {}, (node.get("children") or {}).values()
        else:
            steps, children = getattr(node, "steps", None) or {}, (getattr(node, "children", None) or {}).values()
        for step in steps.values() if isinstance(steps, dict) else ():
            if isinstance(step, dict):
                yield step
        stack.extend(children)


def _command(detail: str) -> str:
    """The command of a step's detail ("<purpose> · <command>", or the command alone)."""
    return detail.split(" · ", 1)[1] if " · " in detail else detail


def _resolve(token: str, base) -> str:
    token = token.strip().strip("'\"").rstrip(",:")
    if not token:
        return ""
    if token.startswith("~"):
        token = os.path.expanduser(token)
    if token.startswith("/"):
        return os.path.normpath(token)
    return os.path.normpath(os.path.join(str(base), token)) if base else ""


def paths_of(trace, *, workdir=None) -> list[tuple[str, str]]:
    """(path, what) of each path ``trace``'s steps name: ``write`` (a file changed, a commit, a redirect into it),
    ``rules`` (a repository's AGENTS.md or CLAUDE.md read) or ``read``. Relative paths are taken from ``workdir``."""
    found = []
    for step in _steps(trace):
        title, detail = str(step.get("title") or ""), str(step.get("detail") or "")
        if not detail:
            continue
        if title in READ_TITLES or title in WRITE_TITLES:
            what = "write" if title in WRITE_TITLES else "read"
            for part in detail.split(", "):
                path = _resolve(part.split(":", 1)[0], workdir)
                if path:
                    found.append((path, "rules" if what == "read" and Path(path).name in RULE_FILES else what))
            continue
        if title in COMMAND_TITLES:
            found.extend(_command_paths(_command(detail), workdir))
    return found


QUOTED = re.compile(r'\'[^\']*\'|"(?:[^"\\]|\\.)*"')


def _command_paths(command: str, workdir) -> list[tuple[str, str]]:
    found = []
    bare = QUOTED.sub("''", command)  # A '>' or 'git commit' inside quotes (python -c "a > b", echo "git commit") is no write.
    here = workdir
    for match in CD.finditer(command):
        target = _resolve(match.group(2), here)
        if target:
            found.append((target, "read"))
            here = target
    gits = [_resolve(match.group(2), workdir) for match in GIT_C.finditer(command)]
    found.extend((path, "read") for path in gits if path)
    for path in ABSOLUTE.findall(command):
        found.append((os.path.normpath(path), "read"))
    if GIT_WRITE.search(bare):
        target = next((path for path in reversed(gits) if path), "") or here
        if target:
            found.append((str(target), "write"))
    for match in REDIRECT.finditer(bare):
        target = _resolve(match.group(2), here)
        if target and not target.startswith("/dev/"):
            found.append((target, "write"))
    if IN_PLACE.search(bare):
        try:
            words = shlex.split(command, posix=True)
        except ValueError:
            words = command.split()
        for word in words[1:]:
            if not word.startswith("-") and ("/" in word or "." in word) and not word.startswith(("s/", "s|", "s#")):
                target = _resolve(word, here)
                if target and os.path.basename(target):
                    found.append((target, "write"))
    if READERS.search(command):
        for word in re.findall(r"[^\s'\";&|<>]*(?:AGENTS|CLAUDE)\.md", command):
            target = _resolve(word, here)
            if target:
                found.append((target, "rules"))
        for word in re.findall(r"(?<![\w/.~-])([\w.-]+/[^\s'\";&|<>()]*)", command):
            target = _resolve(word, here)
            if target:
                found.append((target, "read"))
    return found


def touched(trace, repos, *, workdir=None, home=None, skip=(), now=None) -> list[TopicRepo]:
    """The repositories a turn's tool calls (``trace``) touched, by the repo map ``repos`` (core/repomap.py ``Repo``s):
    one TopicRepo per repository and checkout (``worktree`` the linked worktree's top, '' for the repository's own
    checkout), ``mode`` write when a step changed a file there, ``rules_read_at`` ``now`` when a step read its rules.
    A git repository the map does not list counts when it is under ``home`` and not under any of ``skip`` (the data
    root): it comes back with its directory's name and ``source`` touched, for the map to take in."""
    now = time.time() if now is None else now
    known = sorted({(form, repo.name): (form, repo) for repo in repos
                    for form in (os.path.normpath(repo.path), os.path.realpath(repo.path))}.values(),
                   key=lambda item: -len(item[0]))  # A repository listed by a link is found by its real path too.
    by_git = {os.path.realpath(os.path.join(path, ".git")): repo for path, repo in known}
    home = os.path.normpath(str(home)) if home else ""
    skip = tuple(os.path.normpath(str(path)) for path in skip if path)
    tops: dict[str, tuple[str, bool]] = {}
    found: dict[tuple[str, str], dict] = {}
    for path, what in paths_of(trace, workdir=workdir):
        key = _match(path, known, by_git, tops, home, skip)
        if key is None:
            continue
        name, main, worktree, rules = key
        entry = found.setdefault((main, worktree), {"name": name, "path": main, "worktree": worktree, "mode": "read",
                                                    "rules": rules, "read": False})
        if what == "write":
            entry["mode"] = "write"
        elif what == "rules" and Path(path).name in entry["rules"] and os.path.dirname(path) in (main, worktree):
            entry["read"] = True
    return [TopicRepo(name=item["name"], path=item["path"], mode=item["mode"], worktree=item["worktree"],
                      source="touched", first_at=now, rules_read_at=now if item["read"] else 0.0)
            for item in found.values()]


def _match(path, known, by_git, tops, home, skip):
    """(name, repository path, worktree top or '', its rule files) of ``path``; None when it is in no repository the
    map lists or may take in."""
    for root, repo in known:
        if path == root or path.startswith(root + os.sep):
            return repo.name, root, "", tuple(dict.fromkeys([*repo.rules, *RULE_FILES]))
    directory = path if os.path.isdir(path) else os.path.dirname(path)
    if directory not in tops:
        tops[directory] = checkout_of(directory)
    top, linked = tops[directory]
    if not top:
        return None
    if linked:
        git = repo_of(top)
        repo = by_git.get(os.path.realpath(git)) if git else None
        if repo is not None:
            return repo.name, os.path.normpath(repo.path), top, tuple(dict.fromkeys([*repo.rules, *RULE_FILES]))
        return None
    if not home or not (top == home or top.startswith(home + os.sep)) or top == home:
        return None
    if any(top == root or top.startswith(root + os.sep) for root in skip):
        return None
    return os.path.basename(top), top, "", RULE_FILES
