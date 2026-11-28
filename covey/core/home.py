"""The deployment's home (主目录): where every topic starts, and where its outputs go (``<state>/home.json``).

The home is the service's ``--workdir``: one way to start, no second path (docs/configuration.md 主目录与仓库地图). A
taken-over directory gets no file written into it; a home Covey made (``seed_home``: a new install's ``<数据根>/home``)
has a short AGENTS.md (and the same CLAUDE.md), the rendered repo map, ``.ignore``, ``.skills`` and ``tasks/``, and a
mark (``SEED_MARK``) that says so. ``home.json`` holds the templates of a topic's run and session directories and
whether the Harness's cache variables point into the run directory; a taken-over home without one shows no outputs
directory (its own AGENTS.md says where outputs go). ``env`` is what every Harness process gets: ``COVEY_HOME``,
``COVEY_TOPIC``, ``COVEY_STATE`` and ``<数据根>/bin`` (where ``ensure_shim`` puts the ``covey`` command) first on
``PATH``. Next to it ``ensure_shim`` writes the search shims (``SEARCH_SHIMS``): ``rg``, ``find``, ``du`` ... that refuse a
recursive search of the whole home for every Harness (docs/harnesses.md 主目录搜索垫片).
"""
from __future__ import annotations

import datetime
import json
import os
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from covey.core.paths import BIN_DIR, data_home
from covey.core.state import write_private_text

HOME_FILE = "home.json"
RUN_SCOPES = ("topic_day", "turn")
TEMPLATE_VARS = ("home", "date", "no", "slug", "topic8", "run")
SEED_MARK = ".covey-home"  # In a home Covey made (seed_home); a taken-over directory never gets one.
SHIM = "covey"  # <数据根>/bin/covey: ``covey repo …`` and ``covey memory …`` (ensure_shim).
SEARCH_SHIMS = ("rg", "find", "fd", "du", "tree", "grep", "ls")  # <数据根>/bin/<name>: the search shims (SEARCH_SHIM_TEXT).
SHIM_MARK = "Covey 的搜索垫片"  # In every search shim: ensure_shim never overwrites a file without it.
SLUG_CHARS = 24
CACHE_VARS = ("TMPDIR", "XDG_CACHE_HOME", "PIP_CACHE_DIR", "UV_CACHE_DIR", "npm_config_cache")
# The home's AGENTS.md (and CLAUDE.md, the same text) in a home Covey made: ≤60 lines, the home's few rules.
AGENTS_TEXT = """# 主目录

这是 Covey 的主目录：每个话题都从这里启动。代码仓库在仓库地图里（`repos.md`，由 Covey 生成，不要手改）。

## 约定

1. 进一个仓库干活之前，先读它的 `AGENTS.md` 或 `CLAUDE.md`，照它的规矩做。
2. 要改仓库：`covey repo use <名字> --write`，按话题建（或复用）worktree，在打印的目录里改；只读看：`covey repo use <名字>`。
3. 不要在主目录根递归搜索（`rg`、`grep -r`、`find`、`ls -R`、`du`）：先查仓库地图，只搜具体的仓库或子目录。
4. 任务的输出、日志、临时文件放 `tasks/runs/` 下本话题的目录，不要散落在主目录根。
5. 自己的 Skill 放 `.skills/`。
"""
IGNORE_TEXT = """# Covey 主目录：搜索时跳过数据、依赖和产物
*-data/
tasks/runs/
node_modules/
.venv/
__pycache__/
*.log
"""
SHIM_TEXT = """#!/bin/sh
# Covey 的命令入口（Covey 生成，升级时重写）：covey repo …、covey memory …
case "$1" in
  repo) shift; exec "{python}" -B "{bin}/covey_repo.py" "$@" ;;
  memory) shift; exec "{python}" -B "{bin}/covey_memory.py" "$@" ;;
  *) echo "用法：covey repo list|use|status|prune-plan … ；covey memory …" >&2; exit 2 ;;
esac
"""


@dataclass(frozen=True)
class HomeConfig:
    runs_template: str = "{home}/tasks/runs/{date}-T{no}-{slug}/{run}"
    sessions_template: str = "{home}/tasks/sessions/{date}-T{no}-{slug}"
    cache_env: bool = False
    run_scope: str = "topic_day"  # RUN_SCOPES


def path(state_dir) -> Path:
    return Path(state_dir) / HOME_FILE


def read(state_dir) -> HomeConfig:
    """``home.json``'s settings over the defaults; the defaults when it is missing or unreadable."""
    try:
        data = json.loads(path(state_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return HomeConfig()
    if not isinstance(data, dict):
        return HomeConfig()
    known = {key: data[key] for key in HomeConfig.__dataclass_fields__ if key in data}
    if not all(isinstance(known.get(key, ""), str) for key in ("runs_template", "sessions_template", "run_scope")):
        return HomeConfig()
    if known.get("run_scope", "topic_day") not in RUN_SCOPES:
        known.pop("run_scope")
    known["cache_env"] = bool(known.get("cache_env", False))
    return HomeConfig(**known)


def configured(state_dir) -> bool:
    """Whether ``home.json`` is there: only then a topic's rules name its outputs directory."""
    return path(state_dir).is_file()


def write(state_dir, config: HomeConfig) -> None:
    write_private_text(path(state_dir), json.dumps({"version": 1, **asdict(config)}, ensure_ascii=False, indent=2) + "\n")


def home_dir(workdir) -> Path:
    """The home of a deployment whose service runs with ``--workdir`` ``workdir``."""
    return Path(workdir)


def of(ctx) -> Path:
    """The home of ``ctx``'s deployment: its service's ``--workdir`` (``default_workdir``)."""
    return home_dir(getattr(ctx, "default_workdir", None) or ctx.workdir)


def render(template: str, **values) -> str:
    """``template`` with ``TEMPLATE_VARS`` filled; a variable not given stays as written."""
    out = str(template)
    for name in TEMPLATE_VARS:
        if name in values:
            out = out.replace("{" + name + "}", str(values[name]))
    return out


def seeded(home) -> bool:
    """Whether Covey made ``home`` (``seed_home``): a taken-over directory is never written into."""
    return (Path(home) / SEED_MARK).is_file()


def seed_home(home) -> list[str]:
    """Make the home's few files (AGENTS.md, CLAUDE.md, .ignore, .skills with the Harnesses' links to it, tasks/) in
    ``home`` when it is missing, empty or one Covey made; a file already there is kept. A directory with anything else
    in it is taken over: nothing is written and [] comes back. The paths made, relative to ``home``."""
    home = Path(home)
    try:
        if home.exists() and not seeded(home) and any(home.iterdir()):
            return []
    except OSError:
        return []
    made = []
    home.mkdir(parents=True, exist_ok=True)
    files = {SEED_MARK: json.dumps({"version": 1, "seeded_at": int(time.time())}) + "\n", "AGENTS.md": AGENTS_TEXT,
             "CLAUDE.md": AGENTS_TEXT, ".ignore": IGNORE_TEXT}
    for name, text in files.items():
        target = home / name
        if not target.exists():
            write_private_text(target, text)
            made.append(name)
    for folder in (".skills", "tasks/sessions", "tasks/runs", "tasks/shared"):
        if not (home / folder).is_dir():
            (home / folder).mkdir(parents=True, exist_ok=True)
            made.append(folder + "/")
    for link in (".claude/skills", ".agents/skills"):
        target = home / link
        if not target.exists() and not target.is_symlink():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to("../.skills")
            made.append(link)
    return made


def slug(title) -> str:
    """A short name of ``title`` for a directory: its latin letters and digits, lower case, joined by '-', at most
    ``SLUG_CHARS``; ``topic`` when it has none (a Chinese title)."""
    words = re.findall(r"[a-z0-9]+", str(title or "").lower())
    out = "-".join(words)[:SLUG_CHARS].strip("-")
    return out or "topic"


def _day(when) -> str:
    return datetime.datetime.fromtimestamp(float(when)).strftime("%Y%m%d")


def topic_dir(config: HomeConfig, home, *, number, topic_id, title="", started=None) -> str:
    """The topic's outputs directory: ``runs_template`` up to its ``{run}`` part, ``{date}`` the topic's start day."""
    template = config.runs_template
    head = template[:template.index("{run}")].rstrip("/") if "{run}" in template else template
    return render(head, home=str(home), date=_day(started if started else time.time()), no=number or 0,
                  slug=slug(title), topic8=str(topic_id or "")[:8])


def run_dir_for(config: HomeConfig, home, *, number, topic_id, title="", started=None, task_key="", now=None) -> str:
    """A run's outputs directory (``runs_template``): ``{run}`` is ``YYYYMMDD-HHMM-<task key's first 4>``; with
    ``run_scope`` ``topic_day`` every run of the topic on one day shares ``YYYYMMDD``."""
    now = time.time() if now is None else now
    if config.run_scope == "topic_day":
        run = _day(now)
    else:
        run = datetime.datetime.fromtimestamp(now).strftime("%Y%m%d-%H%M") + "-" + re.sub(r"[^0-9A-Za-z]", "", task_key)[:4]
    return render(config.runs_template, home=str(home), date=_day(started if started else now), no=number or 0,
                  slug=slug(title), topic8=str(topic_id or "")[:8], run=run)


def task_roots(config: HomeConfig, home) -> tuple[str, ...]:
    """The roots of the home's run and session directories (``runs_template``, ``sessions_template`` up to their first
    variable after ``{home}``; a seed home's ``<home>/tasks/runs`` and ``<home>/tasks/sessions``): a git repository under
    one is a draft or a fixture of a topic's, never one the repo map takes in (core/topic_repos.py ``skip_roots``). A root
    that is not below the home (a template that puts runs straight in it) is left out."""
    base = os.path.normpath(str(home))
    found = []
    for template in (config.runs_template, config.sessions_template):
        rendered = render(template, home=base)
        head = rendered[:rendered.index("{")] if "{" in rendered else rendered
        root = os.path.normpath(head if head.endswith("/") else os.path.dirname(head))
        if root.startswith(base + os.sep) and root not in found:
            found.append(root)
    return tuple(found)


def in_work_tree(directory) -> bool:
    """Whether ``directory`` is in a git work tree (``git rev-parse --is-inside-work-tree``); when git cannot be run, whether
    it or a directory above it holds ``.git``."""
    import subprocess

    try:
        done = subprocess.run(["git", "-C", str(directory), "rev-parse", "--is-inside-work-tree"], capture_output=True,
                              text=True, timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        here = Path(directory)
        return any((folder / ".git").exists() for folder in (here, *here.parents))
    return done.returncode == 0 and done.stdout.strip() == "true"


def external_start(found) -> str:
    """Where the process of a Harness with no setting that keeps the home's instruction files out (Antigravity,
    ``HarnessCapabilities.external_cwd``) starts for a topic that keeps off this machine's files: ``found`` is the turn's
    ``options[EXTERNAL]`` (core/harness.py), and the answer its outputs directory when the home is in no git work tree;
    '' when it starts in its workdir as before (no such topic, no directory, or a home in a work tree, where it reads the
    instruction files up to the repository's top from wherever it starts: tests/test_matrix.py GAPS)."""
    if not isinstance(found, dict) or not found.get("dir") or not found.get("home"):
        return ""
    return "" if in_work_tree(found["home"]) else str(found["dir"])


def external_cwd(ctx) -> str:
    """Where the process of a Harness that declares ``external_cwd`` starts for ``ctx``'s turn: ``external_start`` of its
    ``options[EXTERNAL]``, else its workdir."""
    from covey.core.harness import EXTERNAL

    return external_start(ctx.options.get(EXTERNAL)) or str(ctx.workdir)


def cache_env(run_dir) -> dict:
    """The cache variables (``CACHE_VARS``) under ``run_dir``'s tmp/ and cache/, once both are made and writable;
    {} when they are not (never quietly /tmp: the caller says so)."""
    run = Path(run_dir)
    try:
        for folder in ("tmp", "cache"):
            (run / folder).mkdir(parents=True, exist_ok=True)
            probe = run / folder / f".covey-probe-{os.getpid()}"
            probe.write_text("")
            probe.unlink()
    except OSError:
        return {}
    cache = str(run / "cache")
    return {"TMPDIR": str(run / "tmp"), "XDG_CACHE_HOME": cache, "PIP_CACHE_DIR": cache + "/pip",
            "UV_CACHE_DIR": cache + "/uv", "npm_config_cache": cache + "/npm"}


SEARCH_SHIM_TEXT = """#!/bin/sh
# Covey 的搜索垫片（Covey 生成，升级时重写）：Harness 进程的 PATH 里，这个目录在最前。
# 不限深度地递归搜索主目录（或它的上级）时拒绝；别的命令、判定出错、解释器起不来，一律原样执行真程序。
name=${0##*/}
shimdir=${0%/*}
quick=0
real() {
  oifs=$IFS
  IFS=:
  for d in $PATH; do
    [ -n "$d" ] || d=.
    [ "$d" = "$shimdir" ] && continue
    [ -f "$d/$name" ] && [ -x "$d/$name" ] || continue
    [ "$d/$name" -ef "$0" ] && continue
    IFS=$oifs
    exec "$d/$name" "$@"
  done
  IFS=$oifs
  echo "$name: command not found" >&2
  exit 127
}
[ -n "$COVEY_HOME" ] || real "$@"
[ -f "{bin}/covey_tool_guard.py" ] || real "$@"
case "$name" in
  grep) skip=1
    for a; do case "$a" in --recursive|--dereference-recursive|--directories=recurse|recurse) skip=0 ;; --*) ;; -*[rR]*|-d*) skip=0 ;; esac; done
    [ "$skip" = 1 ] && real "$@" ;;
  ls) skip=1
    for a; do case "$a" in --recursive) skip=0 ;; --*) ;; -*R*) skip=0 ;; esac; done
    [ "$skip" = 1 ] && real "$@" ;;
esac
h=$(cd -P -- "$COVEY_HOME" 2>/dev/null && pwd -P)
if [ -n "$h" ]; then
  # Python is for what the text alone cannot settle: a start directory that is the home or above it, or a path that is.
  here=$(pwd -P)
  case "$h/" in "$here"/*) ;; *) quick=1 ;; esac
  [ "$here" = / ] && quick=0
  for a; do
    case "$a" in
      /*//*|/*/.|/*/./*|*..*|~*|*=/*|*\\$*) quick=0 ;;
      "$h"/?*) ;;
      /*) quick=0 ;;
    esac
  done
  [ "$quick" = 1 ] && real "$@"
fi
"{python}" -I -S -B "{bin}/covey_tool_guard.py" --mode shim "$name" "$@"
[ "$?" = 3 ] && exit 2
real "$@"
"""


def bin_dir(root=None) -> Path:
    """``<数据根>/bin``: the ``covey`` command (``ensure_shim``), first on every Harness process's ``PATH``."""
    return Path(root or data_home()) / "bin"


def shim_text(tool_bin=None, python=None) -> str:
    return SHIM_TEXT.format(python=python or _python(), bin=str(tool_bin or BIN_DIR))


def search_shim_text(tool_bin=None, python=None) -> str:
    # Not ``format``: the sh text is full of ``${...}``.
    return SEARCH_SHIM_TEXT.replace("{python}", python or _python()).replace("{bin}", str(tool_bin or BIN_DIR))


def _python() -> str:
    import sys
    return sys.executable or "python3"


def ensure_shim(root=None, *, tool_bin=None, force=False) -> Path | None:
    """Write ``<数据根>/bin/covey`` (``SHIM_TEXT``) and the search shims (``SEARCH_SHIMS``, ``SEARCH_SHIM_TEXT``) naming
    this release's bin/ when one is missing, names a bin/ that is gone, or ``force`` (a deploy points them at the new
    release). A file in bin/ without ``SHIM_MARK`` that is not ``covey`` is never overwritten. The ``covey`` path; None
    when it cannot be written (a search shim that cannot be is skipped: the search is then just not guarded)."""
    tool_bin = Path(tool_bin or BIN_DIR)
    target = bin_dir(root) / SHIM
    try:
        _write_shim(target, shim_text(tool_bin), tool_bin, "covey_repo.py", force)
    except OSError:
        return None
    for name in SEARCH_SHIMS:
        try:
            _write_shim(target.parent / name, search_shim_text(tool_bin), tool_bin, "covey_tool_guard.py", force,
                        mark=SHIM_MARK)
        except OSError:
            continue
    return target


def _write_shim(target, text, tool_bin, script, force, mark="") -> None:
    if target.exists() and not target.is_symlink() and mark:
        if mark not in target.read_text(encoding="utf-8", errors="replace"):
            return  # Not ours.
    if not force and target.is_file():
        named = re.search(r'"([^"]+)/' + re.escape(script) + '"', target.read_text(encoding="utf-8", errors="replace"))
        if named and Path(named.group(1), script).is_file():
            return
    target.parent.mkdir(parents=True, exist_ok=True)
    write_private_text(target, text)
    target.chmod(0o755)


TURN_ENV = ("COVEY_HOME", "COVEY_TOPIC", "COVEY_STATE")  # What ``env`` adds for a turn: never a service's setting.


def env(ctx) -> dict:
    """What the Harness process of ``ctx``'s turn gets on top of the Bridge's environment: ``COVEY_HOME`` (the home),
    ``COVEY_TOPIC`` (the topic, '' for none), ``COVEY_STATE`` (the state directory) and ``PATH`` with ``<数据根>/bin``
    first. Nothing for a device's turn: its process has the device's environment, never the hub's names."""
    if getattr(ctx, "device", ""):
        return {}
    first = str(bin_dir())
    rest = [part for part in os.environ.get("PATH", "").split(os.pathsep) if part and part != first]
    return {"COVEY_HOME": str(of(ctx)), "COVEY_TOPIC": str(getattr(ctx, "topic_id", "") or ""),
            "COVEY_STATE": str(ctx.state_dir), "PATH": os.pathsep.join([first, *rest])}


def migrate_v1(state_dir, workdir) -> str:
    """The home's step of the migration (core/migrate.py): a home Covey made gets ``home.json``'s defaults once; a
    taken-over one gets none (its own AGENTS.md says where outputs go) and nothing written into it. The log line."""
    home = home_dir(workdir)
    if configured(state_dir):
        return f"home_v1: home.json already there ({home})"
    if not seeded(home):
        return f"home_v1: taken-over home {home}, no home.json written"
    write(state_dir, HomeConfig())
    return f"home_v1: home.json written with the defaults ({home})"
