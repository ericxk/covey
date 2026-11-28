"""The machine's lark-cli brought along when the Bridge is upgraded (docs/deploy.md 日常升级): scripts/upgrade.sh and an
extension's installer run ``update`` after the deploy's pre-check went through and before the new Bridge starts, so the new
Bridge and the web page's publish run with the newest lark-cli; ``note`` says the same without changing anything (--dry-run,
--check-only, --status).

Only npm's global install is updated (``lark-cli update``, which checks the newest version, runs ``npm install -g``, checks
the new binary and syncs its own lark-* Skills): the lark-cli the service's PATH finds first must be the one under
``npm root -g`` of that PATH. A copy put in front of PATH, or one installed some other way, is said and left alone, since
``lark-cli update`` would install npm's global one instead and say the service's is new when it is not.

An update that does not go through (no network, no write access, npm's registry behind) keeps the lark-cli there and never
stops the upgrade; only a lark-cli that ran before and does not run after (BROKEN) does, before the running Bridge is
replaced. Standard library only."""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import time
from pathlib import Path

SECONDS = 600  # From scratch its Skills sync took over two minutes; an npm install that cannot reach its registry, one.
VERSION_SECONDS = 30  # `lark-cli --version` and `npm root -g`: local, a second or so.
STOP_SECONDS = 10  # After SIGTERM to the update's process group, before SIGKILL.
WORDS = "[lark-cli]"
DONE, CURRENT, PLANNED, SKIPPED, FAILED, BROKEN = "done", "current", "planned", "skipped", "failed", "broken"
PACKAGE = ("@larksuite", "cli")


def _quiet(run, argv, env, seconds=VERSION_SECONDS) -> str:
    """The stdout of ``argv`` (stripped), '' when it cannot run, fails or takes over ``seconds``."""
    try:
        done = run(argv, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=seconds)
    except (OSError, subprocess.SubprocessError):
        return ""
    return (done.stdout or "").strip() if done.returncode == 0 else ""


def find(env, run=subprocess.run) -> dict:
    """The lark-cli the service's PATH (``env``) runs: {"path", "real", "global", "why"}, "global" when it is npm's global
    install under ``npm root -g`` of the same PATH, "why" saying why not; {} when that PATH has no lark-cli."""
    where = str((env or {}).get("PATH") or "")
    path = shutil.which("lark-cli", path=where)
    if not path:
        return {}
    real = os.path.realpath(path)
    found = {"path": path, "real": real, "global": False, "why": ""}
    npm = shutil.which("npm", path=where)
    if not npm:
        found["why"] = "服务的 PATH 里没有 npm，更新不了"
        return found
    root = _quiet(run, [npm, "root", "-g"], env)
    package = os.path.join(os.path.realpath(root), *PACKAGE) if root else ""
    if package and real.startswith(package + os.sep):
        found["global"] = True
    else:
        found["why"] = f"不是 npm 全局装的那份（npm root -g 是 {root or '读不出'}），lark-cli update 换的是全局那份、不是它"
    return found


def binary(found) -> str:
    """What runs for ``--version`` without side effects: npm's package runs its binary through scripts/run.js, which
    downloads the binary when it is missing, so the package's bin/lark-cli itself when it is there."""
    real = Path(found["real"])
    if real.name == "run.js" and real.parent.name == "scripts":
        native = real.parent.parent / "bin" / "lark-cli"
        return str(native) if native.is_file() else ""
    return found["real"]


def version(found, env, run=subprocess.run) -> str:
    """The version ``lark-cli --version`` says ("1.0.100"), '' when it does not run. Local: no network, nothing written."""
    exe = binary(found)
    words = _quiet(run, [exe, "--version"], env) if exe else ""
    match = re.search(r"\d+\.\d+\.\d+[\w.+-]*", words)
    return match.group(0) if match else ""


def note(env, say, run=subprocess.run) -> str:
    """For --dry-run, --check-only and --status: which lark-cli the service runs, its version and what an upgrade will do
    with it, said with ``say``. Changes nothing: PLANNED (an upgrade runs ``lark-cli update``), SKIPPED."""
    found = find(env, run)
    if not found:
        say(f"{WORDS} 服务的 PATH 里没有 lark-cli：升级时跳过这一步（机器人收发消息要用它，用 ./install 检查）")
        return SKIPPED
    now = version(found, env, run) or "版本读不出"
    if not found["global"]:
        say(f"{WORDS} 服务用的是 {found['path']}（{now}），{found['why']}：升级时不动它")
        return SKIPPED
    say(f"{WORDS} {found['path']}（npm 全局，{now}）：升级时会运行 lark-cli update 更新到最新，失败不影响升级")
    return PLANNED


def _stop(child) -> None:
    """SIGTERM to the update's process group (npm and its children with it), SIGKILL after STOP_SECONDS."""
    for sig, wait in ((signal.SIGTERM, STOP_SECONDS), (signal.SIGKILL, 5)):
        try:
            os.killpg(child.pid, sig)
        except OSError:
            return
        try:
            child.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


def _answer(path) -> dict:
    """The JSON object ``lark-cli update --json`` printed (its last object), {} without one."""
    try:
        text = Path(path).read_text(errors="replace")
    except OSError:
        return {}
    start = text.find("{")
    try:
        found = json.loads(text[start:]) if start >= 0 else {}
    except ValueError:
        return {}
    return found if isinstance(found, dict) else {}


def update(data, env, say, run=subprocess.run, popen=subprocess.Popen, seconds=SECONDS) -> str:
    """``lark-cli update`` on the npm global lark-cli the service runs, its outcome said with ``say`` on lines that start
    "[lark-cli]" and logged under <data>/deploy-runs/<time>-lark-cli/: DONE (with the versions before and after), CURRENT,
    SKIPPED (none, or not npm's global one), FAILED (the one there kept, the upgrade goes on), BROKEN (it ran before the
    update and does not now: the caller stops before the running Bridge is replaced)."""
    found = find(env, run)
    if not found:
        say(f"{WORDS} 服务的 PATH 里没有 lark-cli：跳过（机器人收发消息要用它，用 ./install 检查）")
        return SKIPPED
    before = version(found, env, run)
    if not found["global"]:
        say(f"{WORDS} 服务用的是 {found['path']}（{before or '版本读不出'}），{found['why']}：没有替你更新。"
            "要跟着升级，就把它从服务的 PATH 里去掉、改用 npm 全局的 lark-cli")
        return SKIPPED
    folder = Path(data) / "deploy-runs" / f"{time.strftime('%Y%m%d-%H%M%S')}-lark-cli"
    folder.mkdir(parents=True, exist_ok=True)
    log, out = folder / "lark-cli.log", folder / "update.json"
    argv = [found["path"], "update", "--json"]
    command = shlex.join(argv)
    say(f"{WORDS} 更新 {found['path']}（现在 {before or '版本读不出'}），最多 {seconds // 60} 分钟；日志 {log}")
    code, error = None, ""
    with log.open("w") as err, out.open("w") as answer:
        err.write(f"$ {command}\nbefore={before}\n")
        err.flush()
        try:
            child = popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=answer, stderr=err, start_new_session=True)
        except OSError as exc:
            child, code, error = None, 127, f"{type(exc).__name__}: {exc}"
        if child is not None:
            try:
                code = child.wait(timeout=seconds)
            except subprocess.TimeoutExpired:
                _stop(child)
    after = version(found, env, run)
    result = _answer(out)
    with log.open("a") as err:
        err.write(f"\nexit={code} after={after}\n")
    if before and not after:
        say(f"{WORDS} 更新后跑不起来了（更新前是 {before}）：先装回原来的版本 npm install -g @larksuite/cli@{before}，"
            f"再升级；日志 {log}")
        return BROKEN
    action = result.get("action")
    if code == 0 and result.get("ok") and action == "already_up_to_date":
        say(f"{WORDS} 已经是最新（{after or before}）")
        return CURRENT
    if code == 0 and result.get("ok") and action == "updated" and after and after != before:
        skills = "，lark Skills 也同步了" if result.get("skills_action") == "synced" else ""
        say(f"{WORDS} 更新好了：{before or '?'} → {after}{skills}（日志 {log}）")
        return DONE
    problem = result.get("error") if isinstance(result.get("error"), dict) else {}
    if code is None:
        why = f"超过 {seconds} 秒没结束，停掉了"
    elif error:
        why = error
    elif problem:
        why = " ".join(str(problem.get(key) or "") for key in ("message", "hint")).strip()
        why = f"{why}（{problem['type']}）" if problem.get("type") else why
    elif code == 0 and action == "updated":
        why = f"lark-cli 说更新好了，服务用的这份还是 {after or '?'}"
    elif action == "manual_required":
        why = "lark-cli 认为它不是 npm 装的，要手工更新"
    else:
        why = f"退出码 {code}" + (f"，{action}" if action else "")
    say(f"{WORDS} 没更新上，还用原来的 {after or before or '?'}（不影响这次升级）：{why[:300]}\n    日志 {log}\n"
        f"    单独重跑：{command}")
    return FAILED
