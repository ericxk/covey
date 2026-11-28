#!/usr/bin/env python3
"""批量合入：本机各话题的 worktree 把改完的分支交给它，攒成一批、整批跑一次全量，绿了快进推 main。

    python3 -B scripts/land.py submit --why "<原因>"   # 在话题的 worktree 里交卷，等结果（默认最多 540 秒）
    python3 -B scripts/land.py wait [名字]             # 接着等（默认等本 worktree 分支交的那一份）
    python3 -B scripts/land.py status                  # 队列、正在跑的一批、最近的结果

What it does (docs/testing.md 批量合入):

- The queue is in the clone's shared git directory (``git rev-parse --git-common-dir``), so every worktree of the
  clone sees it and nothing is pushed before a batch lands: ``refs/land/<name>`` holds what was handed in,
  ``covey-land/queue/<name>.json`` why and when, ``covey-land/results/<name>.json`` how it ended.
- One driver at a time (``covey-land/lock``). ``submit`` starts one, detached, when none runs; it ends when the
  queue is empty. A driver that was stopped half way leaves the queue as it was: the next one starts the batch again.
- A batch: every queued branch's own commits, in the order they were handed in, cherry-picked onto origin/main in a
  worktree of the driver's (``covey-land/batch``). Commits that only change tests/test_map.json are left out and
  where only the map conflicts main's map is kept: the map is refreshed once a batch, not once a branch. A branch
  whose commits conflict otherwise goes back (``kicked``) and the batch goes on without it.
- Then the full suite once for the batch: ``test_map.py run --full``, or ``build`` when the map is stale (it
  refreshes the map, committed with the batch), with test_map.py's retry of a few failed modules.
- Green: a fast-forward push of the batch to main. Main having moved meanwhile, the batch is moved onto it and
  pushed; where the new commits changed the batch's files too, the tests the batch's changes select run on the new
  main first. A move that conflicts, or those tests failing, starts the batch again.
- Red: what fails on main alone is main's own (named in the results, the batch lands). Otherwise the driver runs
  only the failed modules on longer prefixes of the batch (bisection), sends back the first branch that brings a
  failure in, with the tests it breaks, and starts the batch again without it. A failure that does not come back
  where the full run had it is a loaded machine's chance (named as ``flaky``; the batch lands).
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

SCRIPT = Path(__file__).resolve()
STATE = "covey-land"  # Under the shared git directory.
MAP = "tests/test_map.json"
WAIT = 540  # Seconds submit and wait wait by default: under a shell tool's ten minutes.
REMOTE, MAIN = "origin", "main"
# unittest's lines as test_map.py prints them: "FAIL: name (test_x.Case.test_y)", and a module whose child process
# ended without its report "ERROR: test_x (exit code 3)".
FAILED = re.compile(r"^(?:FAIL|ERROR): (?:.*?\((test_\w+(?:\.\w+)+)\)|(test_\w+) \()", re.M)


class LandError(Exception):
    """What stops a command, said to its caller."""


def is_ancestor(commit, of, cwd=None) -> bool:
    return subprocess.run(["git", "merge-base", "--is-ancestor", commit, of], cwd=cwd).returncode == 0


def git(*args, cwd=None, check=True, env=None) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=env)
    if check and done.returncode:
        raise LandError(f"git {' '.join(args)}: {(done.stderr or done.stdout).strip()[-800:]}")
    return done.stdout


def home(cwd=None) -> Path:
    root = Path(git("rev-parse", "--git-common-dir", cwd=cwd).strip())
    root = (Path(cwd or os.getcwd()) / root).resolve() if not root.is_absolute() else root
    path = root / STATE
    for sub in ("queue", "results"):
        (path / sub).mkdir(parents=True, exist_ok=True)
    return path


def name_of(branch: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", branch).strip("-.") or "branch"


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def read(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def write(path: Path, data: dict) -> None:
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n")
    temp.replace(path)


def queued(state: Path) -> list[dict]:
    entries = [entry for path in (state / "queue").glob("*.json") if (entry := read(path))]
    return sorted(entries, key=lambda entry: (entry.get("at", ""), entry["name"]))


def finish(state: Path, entry: dict, outcome: str, cwd=None, **facts) -> None:
    """The entry's result, its queue file and its ref gone (it is merged or back with its topic)."""
    write(state / "results" / f"{entry['name']}.json",
          {"name": entry["name"], "branch": entry.get("branch"), "handed_in": entry.get("commit"),
           "state": outcome, "at": now(), **facts})
    (state / "queue" / f"{entry['name']}.json").unlink(missing_ok=True)
    git("update-ref", "-d", f"refs/land/{entry['name']}", cwd=cwd, check=False)


# --- the batch ------------------------------------------------------------------------------------------------------


def worktree(state: Path, cwd, base: str) -> Path:
    """The driver's worktree, on ``base`` and clean."""
    path = state / "batch"
    if not (path / ".git").exists():
        git("worktree", "prune", cwd=cwd, check=False)
        git("worktree", "add", "--detach", "-f", str(path), base, cwd=cwd)
    git("cherry-pick", "--abort", cwd=path, check=False)
    git("checkout", "-q", "--detach", "-f", base, cwd=path)
    git("reset", "-q", "--hard", base, cwd=path)
    git("clean", "-q", "-fdx", cwd=path)
    return path


def own_commits(entry: dict, base: str, cwd) -> list[str]:
    """The entry's commits not on ``base``, oldest first, without those that only change the map."""
    commits = git("rev-list", "--reverse", "--no-merges", f"{base}..{entry['commit']}", cwd=cwd).split()
    return [commit for commit in commits
            if set(git("diff-tree", "--no-commit-id", "--name-only", "-r", commit, cwd=cwd).split()) != {MAP}]


def pick(batch: Path, commit: str) -> list[str]:
    """Cherry-pick ``commit`` onto the batch -> [] when it went on (or was already there), else the files that
    conflict (the pick undone)."""
    env = {**os.environ, "GIT_EDITOR": "true"}
    done = subprocess.run(["git", "cherry-pick", commit], cwd=batch, capture_output=True, text=True, env=env)
    if done.returncode == 0:
        return []
    conflicts = git("diff", "--name-only", "--diff-filter=U", cwd=batch).split()
    if not conflicts and "empty" in (done.stdout + done.stderr):
        git("cherry-pick", "--skip", cwd=batch, check=False)  # Already on main by another way.
        return []
    if conflicts == [MAP]:  # Only the map: main's is kept, the batch refreshes it.
        git("checkout", "--ours", "--", MAP, cwd=batch)
        git("add", "--", MAP, cwd=batch)
        if subprocess.run(["git", "cherry-pick", "--continue"], cwd=batch, capture_output=True, env=env).returncode:
            git("cherry-pick", "--skip", cwd=batch, check=False)
        return []
    git("cherry-pick", "--abort", cwd=batch, check=False)
    return conflicts or ["(cherry-pick: " + (done.stderr or done.stdout).strip()[-200:] + ")"]


def assemble(state: Path, entries: list[dict], base: str, cwd) -> tuple[Path, list[tuple[dict, str]]]:
    """The batch on ``base``: -> (its worktree, [(entry, the batch's commit after the entry's last one)]). An
    entry that conflicts is kicked and left out."""
    batch = worktree(state, cwd, base)
    applied = []
    for entry in entries:
        start = git("rev-parse", "HEAD", cwd=batch).strip()
        conflicts = []
        for commit in own_commits(entry, base, cwd):
            conflicts = pick(batch, commit)
            if conflicts:
                break
        if conflicts:
            git("reset", "-q", "--hard", start, cwd=batch)
            finish(state, entry, "kicked", cwd=cwd, reason="conflict", files=conflicts,
                   note="和 main 或这一批前面的分支冲突：rebase 到最新 main、解决冲突后重新交卷")
            continue
        applied.append((entry, git("rev-parse", "HEAD", cwd=batch).strip()))
    return batch, applied


# --- checks ---------------------------------------------------------------------------------------------------------


def suite(batch: Path, why: str, paths=(), diff=None) -> tuple[bool, set[str], str]:
    """The batch's tests -> (passed, the failed test ids, the output's tail). No paths: the full suite, ``build``
    when the map is stale; paths: those test modules (``run --force``); diff: the modules the changes since that
    commit select (``run --diff``)."""
    def run(*args):
        done = subprocess.run([sys.executable, "-B", "scripts/test_map.py", *args, "--why", why], cwd=batch,
                              capture_output=True, text=True)
        return done.returncode, done.stdout + done.stderr

    if diff:
        code, out = run("run", "--force", "--diff", diff)
    elif paths:
        code, out = run("run", "--force", *paths)
    else:
        code, out = run("run", "--full")  # No --force: on a stale map it refuses, and build refreshes the map.
        if code == 2 and "stale map" in out:
            code, out = run("build")
    # test_map.py prints the first run's failures, then, when it retried, the retried modules' own: the last say.
    last = out.rfind("retrying the failed module")
    final = out[last:] if last >= 0 else out
    return code == 0, {test or module for test, module in FAILED.findall(final)}, "\n".join(out.splitlines()[-30:])


def modules(tests) -> list[str]:
    return sorted({f"tests/{test.split('.')[0]}.py" for test in tests})


def culprit(batch: Path, applied, failed: set[str], why: str) -> tuple[int, set[str]] | None:
    """The first entry of ``applied`` whose commits bring in one of ``failed`` (failed modules only, run on longer
    and longer prefixes, by halves) -> (its index, the tests that fail there); None when they do not fail again
    where the batch said they would (a loaded machine's chance failure: the batch lands)."""
    paths = modules(failed)
    low, high, seen = 0, len(applied), set()  # The prefix up to low passes these tests, up to high fails them.
    while high - low > 1:
        middle = (low + high) // 2
        git("checkout", "-q", "--detach", "-f", applied[middle - 1][1], cwd=batch)
        _, found, _ = suite(batch, f"{why}：二分第 {middle}/{len(applied)} 个分支", paths)
        if found & failed:
            high, seen = middle, found & failed
        else:
            low = middle
    if not seen:  # Not seen failing since the full run: once more where it would be.
        git("checkout", "-q", "--detach", "-f", applied[high - 1][1], cwd=batch)
        _, found, _ = suite(batch, f"{why}：确认第 {high}/{len(applied)} 个分支", paths)
        seen = found & failed
    return (high - 1, seen) if seen else None


def drive(cwd=None, why_prefix="批量合入") -> int:
    """Batches until the queue is empty; 0, or 0 at once when another driver runs."""
    state = home(cwd)
    lock = open(state / "lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("another driver runs this queue")
        return 0
    try:
        while True:
            git("fetch", "-q", REMOTE, MAIN, cwd=cwd)
            base = git("rev-parse", f"{REMOTE}/{MAIN}", cwd=cwd).strip()
            entries = []
            for entry in queued(state):
                if is_ancestor(entry["commit"], base, cwd):
                    finish(state, entry, "merged", cwd=cwd, commit=base, note="已经在 main 上")
                elif not own_commits(entry, base, cwd):
                    finish(state, entry, "merged", cwd=cwd, commit=base, note="改动已经在 main 上")
                else:
                    entries.append(entry)
            if not entries:
                (state / "driver.json").unlink(missing_ok=True)
                return 0
            names = [entry["name"] for entry in entries]
            write(state / "driver.json", {"pid": os.getpid(), "at": now(), "base": base, "batch": names,
                                          "phase": "assembling"})
            batch, applied = assemble(state, entries, base, cwd)
            if not applied:
                continue
            names = [entry["name"] for entry, _ in applied]
            why = f"{why_prefix}：{'、'.join(names)}"
            write(state / "driver.json", {"pid": os.getpid(), "at": now(), "base": base, "batch": names,
                                          "phase": "full suite"})
            passed, failed, tail = suite(batch, why)
            notes = {}
            if not passed:
                if not failed:  # Red without a test id (a crash before any test): the batch cannot land.
                    for entry, _ in applied:
                        finish(state, entry, "kicked", cwd=cwd, reason="red", tail=tail,
                               note="整批全量红了，但认不出是哪个测试；单独跑全量看看")
                    continue
                git("checkout", "-q", "--detach", "-f", base, cwd=batch)
                _, on_main, _ = suite(batch, f"{why}：main 本身", modules(failed))
                if failed & on_main:
                    notes["mains_own"] = sorted(failed & on_main)
                failed -= on_main
                if failed:
                    write(state / "driver.json", {"pid": os.getpid(), "at": now(), "base": base, "batch": names,
                                                  "phase": "bisecting"})
                    found = culprit(batch, applied, failed, why)
                    if found:
                        index, breaks = found
                        finish(state, applied[index][0], "kicked", cwd=cwd, reason="tests", failed=sorted(breaks),
                               tail=tail, note="整批全量里这些测试失败，二分下来是这个分支带进来的；修好后重新交卷")
                        continue
                    notes["flaky"] = sorted(failed)  # Failed in the full run only, not again.
                git("checkout", "-q", "--detach", "-f", applied[-1][1], cwd=batch)
            if git("status", "--porcelain", "--", MAP, cwd=batch).strip():
                git("add", "--", MAP, cwd=batch)
                git("commit", "-q", "-m", f"Test map rebuilt for the batch of {', '.join(names)}", cwd=batch)
            landed = push(batch, base, cwd, why)
            if not landed:
                print("main moved under the batch, and moving it over did not hold: the batch starts again", flush=True)
                continue
            head, moved_onto = landed
            for entry, commit in applied:  # Moved onto a newer main, the entries' own commits have new ids.
                finish(state, entry, "merged", cwd=cwd, commit=head if moved_onto else commit, batch=names,
                       **({**notes, "moved_onto_newer_main": True} if moved_onto else notes))
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def changed(old, new, cwd) -> set[str]:
    return set(git("diff", "--name-only", old, new, cwd=cwd).split()) - {MAP}


def push(batch: Path, base: str, cwd, why: str, tries=3) -> tuple[str, bool] | None:
    """Push the batch to main -> (the main commit it is now, whether it was moved). Main having moved meanwhile, the
    batch is moved onto it and pushed: its own files are as tested, the others as their authors tested them (its map
    commit is left to the next batch); where both changed the same files, the tests the batch's changes select run
    again on the new main first. None when the move conflicts or those tests fail: the batch starts again."""
    moved_onto = False
    for _ in range(tries):
        if subprocess.run(["git", "push", "-q", REMOTE, f"HEAD:{MAIN}"], cwd=batch, capture_output=True).returncode == 0:
            return git("rev-parse", "HEAD", cwd=batch).strip(), moved_onto
        git("fetch", "-q", REMOTE, MAIN, cwd=cwd)
        moved = git("rev-parse", f"{REMOTE}/{MAIN}", cwd=cwd).strip()
        both = changed(base, moved, cwd) & changed(base, "HEAD", batch)
        if git("log", "-1", "--format=%s", cwd=batch).startswith("Test map rebuilt for the batch"):
            git("reset", "-q", "--hard", "HEAD~1", cwd=batch)
        rebased = subprocess.run(["git", "rebase", "-q", "--onto", moved, base], cwd=batch, capture_output=True,
                                 text=True, env={**os.environ, "GIT_EDITOR": "true"})
        if rebased.returncode:
            git("rebase", "--abort", cwd=batch, check=False)
            return None
        if both and not suite(batch, f"{why}：搬到新 main（同改了 {'、'.join(sorted(both)[:3])}）", diff=moved)[0]:
            return None
        base, moved_onto = moved, True
    return None


# --- commands -------------------------------------------------------------------------------------------------------


def current_name(cwd=None) -> str:
    branch = git("rev-parse", "--abbrev-ref", "HEAD", cwd=cwd).strip()
    if branch == "HEAD":
        raise LandError("HEAD is detached: hand in from a branch")
    return name_of(branch)


def driver_alive(state: Path) -> bool:
    try:
        with open(state / "lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(lock, fcntl.LOCK_UN)
        return False
    except OSError:
        return True


def start_driver(state: Path, cwd=None) -> None:
    if driver_alive(state):
        return
    log = open(state / "driver.log", "a")
    subprocess.Popen([sys.executable, "-B", str(SCRIPT), "drive"], cwd=cwd or os.getcwd(), stdin=subprocess.DEVNULL,
                     stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    log.close()


def submit(why: str, wait_seconds: int, cwd=None, spawn=True) -> int:
    state, name = home(cwd), current_name(cwd)
    dirty = [line for line in git("status", "--porcelain", cwd=cwd).splitlines() if line[3:] != MAP]
    if dirty:
        raise LandError("uncommitted changes (commit them first): " + "; ".join(dirty[:5]))
    git("fetch", "-q", REMOTE, MAIN, cwd=cwd)
    commit = git("rev-parse", "HEAD", cwd=cwd).strip()
    entry = {"name": name, "branch": git("rev-parse", "--abbrev-ref", "HEAD", cwd=cwd).strip(), "commit": commit,
             "why": why, "at": now(), "worktree": str(Path(cwd or os.getcwd()).resolve())}
    if git("rev-list", "--merges", f"{REMOTE}/{MAIN}..HEAD", cwd=cwd).strip():
        raise LandError("the branch has merge commits: rebase it on origin/main instead")
    if not own_commits(entry, f"{REMOTE}/{MAIN}", cwd):
        raise LandError(f"nothing to land: {name} has no commit that {REMOTE}/{MAIN} lacks")
    git("update-ref", f"refs/land/{name}", commit, cwd=cwd)
    (state / "results" / f"{name}.json").unlink(missing_ok=True)
    write(state / "queue" / f"{name}.json", entry)
    print(f"handed in {name} at {commit[:8]} ({len(queued(state))} in the queue)")
    if spawn:
        start_driver(state, cwd)
    return wait(name, wait_seconds, cwd)


def wait(name: str | None, seconds: int, cwd=None) -> int:
    """0 merged, 1 sent back, 3 not decided yet (wait again)."""
    state, name = home(cwd), name or current_name(cwd)
    deadline, said = time.monotonic() + seconds, None
    while True:
        result = read(state / "results" / f"{name}.json")
        if result:
            print(json.dumps(result, ensure_ascii=False, indent=1))
            return 0 if result["state"] == "merged" else 1
        driver = read(state / "driver.json") or {}
        line = (f"{name}: in the batch ({driver.get('phase')}: {', '.join(driver.get('batch', []))})"
                if name in driver.get("batch", []) else f"{name}: queued")
        if line != said:
            print(line, flush=True)
            said = line
        if not (state / "queue" / f"{name}.json").exists():
            print(f"{name}: not in the queue and no result: hand it in with submit")
            return 1
        if time.monotonic() >= deadline:
            print(f"{name}: not decided yet; `python3 -B scripts/land.py wait {name}` waits again")
            return 3
        if not driver_alive(state):
            start_driver(state, cwd)
        time.sleep(5)


def status(cwd=None) -> int:
    state = home(cwd)
    driver = read(state / "driver.json")
    print(f"driver: {'running ' + json.dumps(driver, ensure_ascii=False) if driver_alive(state) and driver else 'none'}")
    for entry in queued(state):
        print(f"queued  {entry['at']}  {entry['name']}  {entry['commit'][:8]}  {entry.get('why', '')}")
    results = sorted((read(path) or {} for path in (state / "results").glob("*.json")),
                     key=lambda result: result.get("at", ""), reverse=True)
    for result in results[:10]:
        print(f"{result.get('state', '?'):7} {result.get('at', '')}  {result.get('name')}  "
              f"{(result.get('commit') or '')[:8]}  {result.get('reason', '')}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    hand_in = commands.add_parser("submit", help="hand this worktree's branch in and wait for its batch")
    hand_in.add_argument("--why", required=True, help="what the branch changes (goes to the ledger)")
    hand_in.add_argument("--wait", type=int, default=WAIT, help=f"seconds to wait for the result (default {WAIT})")
    waiting = commands.add_parser("wait", help="wait for a branch handed in")
    waiting.add_argument("name", nargs="?", help="its name (default: this worktree's branch)")
    waiting.add_argument("--wait", type=int, default=WAIT, help=f"seconds to wait (default {WAIT})")
    commands.add_parser("status", help="the queue, the batch running and the latest results")
    commands.add_parser("drive", help="run the queue's batches until it is empty (submit starts this)")
    args = parser.parse_args(argv)
    try:
        if args.command == "submit":
            return submit(args.why, args.wait)
        if args.command == "wait":
            return wait(args.name, args.wait)
        if args.command == "status":
            return status()
        return drive()
    except LandError as error:
        print(f"land: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
