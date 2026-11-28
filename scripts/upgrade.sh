#!/bin/bash
# 日常升级（docs/deploy.md 日常升级）：把 ./install 装的机器人换到上游的新提交。真正的部署还是 scripts/deploy_bridge.sh
# （docs/deploy.md 升级与回滚），这里替你做它前后那几步：
#
#   scripts/upgrade.sh [--dry-run] [--state-dir DIR] [--ref REV] [--no-fetch] [--no-web] [--allow-dropped-config]
#   scripts/upgrade.sh --status [--json] [--state-dir DIR] [--ref REV]
#
#   1. 检出要干净（没提交的改动不会被部署，先提交或 git stash），git fetch 上游（--no-fetch 不取）；要升到的提交（--ref，默认
#      当前分支的上游，没有上游时 origin/main）必须包含检出的 HEAD：检出里有它没有的提交时停下，从不强制拉取、不重置。
#   2. 找机器人：服务根（COVEY_SERVICE_ROOT，或数据根下的 bridge-services）里唯一一个，几个时用 --state-dir 指定。它的现网
#      release（直接从检出运行的，是检出的 HEAD）已经是这个提交、数据根 extensions/ 下的个人扩展（docs/deploy.md 个人扩展）
#      也和现网带的一样时说“已经是最新”就结束；现网 release 带扩展（--ext-repo 装的）时停下（按扩展自己的升级说明做）。
#   3. 核对依赖：服务清单里的 python3 是 3.10+，服务的 PATH 里有 lark-cli（没有只提示）。
#   4. 照服务清单载入环境（清单里的变量原样，终端里别的本项目变量不带进去，所以开关和代理都不变；BASH_ENV、ENV 也不带：
#      bash 会先执行它们指向的文件，改了 PATH 预检就报 env_value_diff），从清单的启动命令抄出
#      --workdir、--profile、--app-id、--state-dir、--harness-config 和其余参数，先跑 deploy_bridge.sh --check-only；
#      --dry-run 到此为止，什么都不换（只说服务用的 lark-cli 和升级时会怎么更新它）。
#   5. lark-cli：默认不动，只说服务用的是哪个、什么版本。服务清单设了 COVEY_LARK_CLI_UPDATE=1、服务的 PATH 找到的是
#      npm 全局装的 lark-cli 时运行 lark-cli update，新 Bridge 起来就用新版
#      （covey/service/lark_cli_upgrade.py，日志在 <数据根>/deploy-runs/<时间>-lark-cli/）；没网、没写权限、更新失败都只提示，
#      原来的照用、升级照常；更新后跑不起来了才停下，在跑的 Bridge 不动。别的办法装的（PATH 前面另放的一份）只提示，不动。
#      Bridge 已经是最新时也做这一步。
#   6. 部署：机器人重启（几秒内收不到消息和点击，在跑的任务不中断、由新进程接管），日志在 <数据根>/deploy-runs/<时间>/。
#      成功后检出快进（git merge --ff-only）到这个提交；还有任务进程用着检出里的代码时先不快进，下次运行时补上。
#      最后列“升级后要你补的”：这一版起默认打开、你的清单没设的开关，诊断（covey_doctor）列出的要你手工补的事。
#   7. 网页：用 web/deploy/selfhost/build.sh 自托管的网页跟着换成新 release 的 web/（--restart，Manager 拉起新版；
#      covey/service/web_upgrade.py），Bridge 已经是最新时也补上；别的办法部署的网页只提示。--no-web 不动网页。
#      最后写 <数据根>/upgrade.json：网页「版本与更新」和定时自动升级（COVEY_AUTO_UPGRADE）照它查新版本、升级（扩展的安装器
#      写的不覆盖）。
#
# --status 只查不换（git fetch，不部署、不动检出）：现网、要升到的提交、新提交、网页要不要更新、为什么现在不能升（不带 --json
# 时还说服务用的 lark-cli 和它的版本）；--json 时最后一行是给 Bridge 的 JSON（app/self_upgrade.py）。
#
# 退出码：0 升级好了（或已经是最新、--dry-run 检查都过了、--status 查完），1 某项检查没过（部署前停下的什么都没换），2 参数
# 不对或找不到机器人，3 另一个部署正在跑（等它结束再运行），4 Bridge 升级好了、网页没更新上（不用回滚 Bridge）。
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec python3 -I - "$ROOT" "$@" <<'PY'
import argparse, json, os, re, shutil, subprocess, sys, time
from pathlib import Path

ROOT = Path(sys.argv[1])
sys.path.insert(0, str(ROOT))
from covey.core.paths import data_home, service_root  # noqa: E402
from covey.service import lark_cli_upgrade, personal_extensions, web_upgrade  # noqa: E402
from covey.service.manager import persistable  # noqa: E402

KNOWN = ("--harness", "--state-dir", "--workdir", "--profile", "--app-id", "--harness-config")
# The processes that run code from the checkout a service ran from before its first upgrade: a task runner the new Bridge
# adopts keeps reading it until it ends, so the checkout is fast-forwarded only once none is left (docs/deploy.md 第 6 步).
USERS = ("covey_runner.py", "covey_bridge.py", "covey_butler_mcp.py", "lark_task_runner.py", "lark_harness_bridge.py",
         "bridge_main_mcp.py", "covey_main_mcp.py")
PRE_WORDS = {
    "restart_blockers": "有任务重启会被打断：等它们结束再升级（python3 -B bin/covey_service.py status --state-dir {state} 的 restart_blockers）",
    "creating_worktree": "有启动卡正在建独立 worktree：等它建完再升级",
    "proxy_sending": "有代答批次正在发：等它发完再升级",
    "env_missing": "服务清单里有、这次没带上的变量：{value}",
    "env_extra": "这次多带了服务清单里没有的变量：{value}",
    "env_value_diff": "和服务清单的值不一样的变量：{value}",
    "permission_env": "清单里留着已不生效的权限变量：{value}；按 docs/troubleshooting.md「权限变量让诊断或部署失败」去掉后再升级",
    "command_diff": "这次要注册的启动命令和现网的不一样：照 docs/deploy.md「升级与回滚」手工部署",
    "rollback": "要部署的提交没包含现网的提交：{value}",
    "extension_env": "清单或终端里有 COVEY_EXTENSIONS / COVEY_EXTENSION_PATH：{value}",
    "personal_problems": "数据根 extensions/ 下的个人扩展不能打包：{value}",
    "core_lock_mismatch": "扩展的 core.lock 和要部署的核心提交对不上：{value}",
    "extension_checks": "扩展自检没过（在要部署的 release 里试导入、跑各包的检查）：{value}",
    "dropped_config": ("升级后这些配置不再生效（现网还在用）：{value}。看过、确认可以不要了，再加 --allow-dropped-config 运行"
                       "（docs/deploy.md「升级不丢配置」）"),
}


CHANGES = 20  # The newest commits --status lists.
NOISE = ("Test map rebuilt",)  # Commits that change no behaviour, left out of what --status says is new.
UPGRADER = "upgrade.json"  # Under the data home: how app/self_upgrade.py checks and runs this script.
outlook = {"live": "", "latest": "", "bridge": False, "web": None, "count": 0, "changes": [], "blocked": ""}  # --status


def say(text=""):
    print(text, flush=True)


def report():
    """--status's end: what it found in words, and with --json the line app/self_upgrade.py reads."""
    say(f"现网 {outlook['live'] or '?'}；升级会部署 {outlook['latest'] or '?'}" + ("" if outlook["bridge"] else "（Bridge 已经是这一版）"))
    web = outlook["web"]
    if web:
        say("网页：" + ("不是 build.sh 建的，升级不动它" if web["manual"] else "已经是这一版" if web["current"] else "升级时会跟着更新"))
    say(f"{outlook['count']} 个新提交" + ("：" if outlook["changes"] else ""))
    for row in outlook["changes"][:10]:
        say(f"    {row['sha']} {row['subject'][:100]}")
    if outlook["blocked"]:
        say(f"现在不能升级：{outlook['blocked']}")
    if args.json:
        say(json.dumps(outlook, ensure_ascii=False))
    sys.exit(0)


def stop(code, why, next_step=""):
    if args.status:  # What would stop an upgrade now is what --status says blocks it.
        outlook["blocked"] = outlook["blocked"] or (why + (f"（{next_step}）" if next_step else ""))[:300]
        report()
    say(f"升级停在这里：{why}")
    if next_step:
        say(f"下一步：{next_step}")
    sys.exit(code)


ap = argparse.ArgumentParser(prog="upgrade.sh", description="把 ./install 装的机器人升级到上游的新提交（docs/deploy.md 日常升级）")
ap.add_argument("--dry-run", action="store_true", help="只取新提交、跑部署前的检查，不部署、不动检出")
ap.add_argument("--state-dir", help="要升级的机器人的状态目录（服务根下只有一个机器人时不用给）")
ap.add_argument("--ref", help="升级到哪个提交（默认当前分支的上游，没有上游时 origin/main）")
ap.add_argument("--no-fetch", action="store_true", help="不 git fetch，只用本地已有的提交")
ap.add_argument("--no-web", action="store_true", help="不动网页（默认：build.sh 自托管的网页跟着换成新 release 的）")
ap.add_argument("--status", action="store_true", help="只查升级会换什么（git fetch，不部署、不动检出）")
ap.add_argument("--json", action="store_true", help="和 --status 一起：最后一行输出 JSON（网页「版本与更新」、定时自动升级读它）")
ap.add_argument("--allow-dropped-config", action="store_true",
                help="新版本不再用现网的某些配置时也照常升级（先看 --dry-run 列出的是哪些）")
ap.add_argument("--repo", default=str(ROOT), help=argparse.SUPPRESS)  # The checkout to upgrade (tests); this one by default.
args = ap.parse_args(sys.argv[2:])
repo = Path(args.repo).resolve()


def git(*argv, timeout=120):
    try:
        return subprocess.run(["git", *argv], cwd=repo, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, "", f"git {argv[0]} timed out after {timeout} s")


def short(sha):
    return sha[:8]


# ---- 1. the checkout and the commit to go to
if not shutil.which("git"):
    stop(2, "找不到 git")
if git("rev-parse", "--git-dir").returncode:
    stop(2, f"{repo} 不是 git 检出")
dirty = [line for line in git("status", "--porcelain", "--untracked-files=no").stdout.splitlines() if line.strip()]
if dirty:  # Each line is "XY path": the leading space of X matters, so the output is not stripped.
    why = "检出里有没提交的改动：" + "、".join(line[3:] for line in dirty[:5])
    if not args.status:  # --status goes on: what an upgrade would deploy is still worth saying.
        stop(1, why, "先提交或 git stash，再运行 scripts/upgrade.sh（升级部署的是提交，不含没提交的改动）")
    outlook["blocked"] = why + "：先提交或 git stash"
ref = args.ref
if not ref:
    upstream = git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    ref = upstream.stdout.strip() if upstream.returncode == 0 and upstream.stdout.strip() else "origin/main"
if not args.no_fetch:
    remotes = git("remote").stdout.split()
    remote = ref.split("/", 1)[0] if "/" in ref and ref.split("/", 1)[0] in remotes else None
    fetched = git("fetch", "--quiet", *([remote] if remote else []))
    if fetched.returncode:
        stop(1, "git fetch 没成功：" + " ".join((fetched.stderr or fetched.stdout).split())[-300:],
             "检查网络或代理后再试；只用本地已有的提交时加 --no-fetch")
target = git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").stdout.strip()
if not target:
    stop(2, f"找不到 {ref}", "用 --ref 指定要升级到的提交或分支（比如 origin/main）")
head = git("rev-parse", "HEAD").stdout.strip()
if git("merge-base", "--is-ancestor", head, target).returncode:
    ahead = git("rev-list", "--count", f"{target}..{head}").stdout.strip() or "?"
    why = f"检出里有 {ref} 没有的提交（{ahead} 个）：升级会部署 {ref}，不含它们"
    if not args.status:
        stop(1, why, "要部署检出现在的提交：scripts/upgrade.sh --ref HEAD；要跟上游一致，先自己处理这些提交（本脚本不强制拉取、不重置）")
    outlook["blocked"] = outlook["blocked"] or why
newer = git("rev-list", "--count", f"{head}..{target}").stdout.strip() or "0"
outlook["latest"] = short(target)


def commits(since, until):
    """[{repo, sha, subject, at}] of ``since..until``, newest first, without NOISE; [] when either is unknown here."""
    if not since or not until or git("cat-file", "-e", f"{since}^{{commit}}").returncode:
        return []
    rows = []
    for line in git("log", "--format=%H%x09%ct%x09%s", f"{since}..{until}").stdout.splitlines():
        sha, at, subject = (line.split("\t", 2) + ["", ""])[:3]
        if subject and not subject.startswith(NOISE):
            rows.append({"repo": "covey", "sha": sha[:8], "subject": subject, "at": int(at) if at.isdigit() else 0})
    return rows

# ---- 2. the service
root = Path(service_root())
manifests = []
for path in sorted((root / "services").glob("*.json")):
    try:
        found = json.loads(path.read_text())
    except (OSError, ValueError):
        continue
    if isinstance(found, dict) and found.get("state_dir") and isinstance(found.get("command"), list):
        manifests.append(found)
if args.state_dir:
    wanted = os.path.realpath(os.path.expanduser(args.state_dir))
    manifests = [m for m in manifests if os.path.realpath(m["state_dir"]) == wanted]
    if not manifests:
        stop(2, f"服务根 {root} 里没有状态目录是 {wanted} 的机器人", "看 python3 -B bin/covey_service.py status --state-dir <状态目录>")
elif len(manifests) > 1:
    stop(2, f"服务根 {root} 里有 {len(manifests)} 个机器人",
         "用 --state-dir 指定要升级的那个：" + "、".join(m["state_dir"] for m in manifests))
if not manifests:
    stop(2, f"服务根 {root} 里没有注册的机器人",
         "还没装就先 ./install；数据根不在默认位置时先 export COVEY_DATA_HOME=<数据根>（或 COVEY_SERVICE_ROOT）")
manifest = manifests[0]
state = manifest["state_dir"]
if manifest.get("desired_state") != "running":
    stop(1, "这个机器人停着", f"先 python3 -B bin/covey_service.py start --state-dir {state}，再升级")
saved = manifest.get("environment") if isinstance(manifest.get("environment"), dict) else {}
data = Path(saved.get("LARK_BRIDGE_DATA_HOME") or saved.get("COVEY_DATA_HOME") or data_home()).expanduser()
command = [str(part) for part in manifest["command"]]
# The service's own environment, for the pre-check, the deploy and lark-cli. Not BASH_ENV or sh's ENV either: bash runs the
# file they name before deploy_bridge.sh's first line (some ssh setups point BASH_ENV at .bashrc), and the PATH it changes is
# not the manifest's.
env = {key: value for key, value in os.environ.items() if not persistable(key) and key not in ("BASH_ENV", "ENV")}
env.update({key: value for key, value in saved.items() if isinstance(value, str)})
entry = Path(command[2]) if len(command) > 2 else None
live_id, live_core, with_extensions = None, None, False
try:
    rel = entry.resolve().relative_to((data / "releases").resolve()) if entry else None
except (ValueError, OSError):
    rel = None
if rel is not None and len(rel.parts) > 1:
    live_id = rel.parts[0]
    try:
        rows = [line.split() for line in (data / "releases" / live_id / "RELEASE_MANIFEST").read_text().splitlines()]
    except OSError:
        rows = []
    live_core = next((row[1] for row in rows if row[:1] == ["core"] and len(row) > 1), None)
    with_extensions = any(row[:1] == ["extension"] for row in rows) or ("-" in live_id and not rows)
    if live_core is None and re.fullmatch(r"[0-9a-f]{40}", live_id):
        live_core = live_id
outlook["live"] = (short(live_id) if re.fullmatch(r"[0-9a-f]{40}", live_id) else live_id) if live_id else f"检出 {short(head)}"
live_commit = live_core if live_id is not None else head
# The data home's personal extensions (covey/service/personal_extensions.py): a commit there the live release was not packed
# with is an upgrade too, with the core where it is.
mine_found = personal_extensions.discover(data)
personal_now = [found["commit"] for found in mine_found["repos"]]
personal_live = [found["commit"] for found in personal_extensions.in_release(data / "releases" / live_id)] if live_id else []
personal_changed = personal_now != personal_live
outlook["bridge"] = live_commit != target or personal_changed
rows = commits(live_commit, target)
outlook["count"], outlook["changes"] = len(rows), rows[:CHANGES]
if with_extensions:
    stop(1, f"这个机器人的现网 release（{live_id}）带着扩展",
         "按扩展自己的升级说明做（它底下也用 scripts/deploy_bridge.sh，要加 --ext-repo）")


def release_dir(commit):
    """Where deploy_bridge.sh puts the release of ``commit`` (a core-only release is named by its commit, one with personal
    extensions by its 8 characters and theirs)."""
    return data / "releases" / ("-".join([commit[:8], *(sha[:8] for sha in personal_now)]) if personal_now else commit)


if not args.no_web:
    outlook["web"] = web_upgrade.view(manifest, release_dir(target))


def write_upgrader():
    """<data>/upgrade.json for app/self_upgrade.py (the page's 版本与更新, COVEY_AUTO_UPGRADE): this script's --status --json and
    a run, for this bot. Not over one an extension's installer wrote; a failure to write is said and stops nothing."""
    path = data / UPGRADER
    try:
        by = json.loads(path.read_text()).get("by")
    except (OSError, ValueError, AttributeError):
        by = None
    if by not in (None, "", "covey"):
        return
    script = str(repo / "scripts" / "upgrade.sh")
    where = ["--state-dir", state]
    record = {"by": "covey", "status": [script, "--status", "--json", *where], "run": [script, *where]}
    try:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
        tmp.replace(path)
    except OSError as exc:
        say(f"没写成 {path}（{type(exc).__name__}）：网页上不能点一下升级、定时自动升级也不会跑；终端里的 scripts/upgrade.sh 照常能用")


def web_step(release):
    """The page after the Bridge (covey/service/web_upgrade.py); 4 when an update was tried and did not go through, else 0."""
    if args.no_web:
        return 0
    return 4 if web_upgrade.update(data, manifest, release, say) == web_upgrade.FAILED else 0


def lark_cli_step():
    """The machine's lark-cli before the new Bridge starts (covey/service/lark_cli_upgrade.py, step 5): an update that does
    not go through is said and the upgrade goes on; a lark-cli that ran before it and does not now stops here, the running
    Bridge left as it is."""
    if lark_cli_upgrade.update(data, env, say) == lark_cli_upgrade.BROKEN:
        stop(1, "lark-cli 更新后跑不起来了，Bridge 没换（原来的还在跑）",
             "照上面 [lark-cli] 那行装回原来的版本，再运行 scripts/upgrade.sh")


def in_use():
    """Whether a process still runs code from the checkout (USERS): only checked for a service that ran from it."""
    if live_id is not None:
        return False
    proc = Path("/proc")
    if not proc.is_dir():
        return True  # No /proc to look in (macOS): fast-forward the next time, once the service runs from a release.
    prefix = str(repo) + "/bin/"
    for item in proc.glob("[0-9]*/cmdline"):
        try:
            argv = item.read_bytes().split(b"\0")
        except OSError:
            continue
        if any(a.decode(errors="replace").startswith(prefix) and a.decode(errors="replace").endswith(USERS) for a in argv):
            return True
    return False


def fast_forward():
    if head == target:
        return
    if in_use():
        say(f"检出先不快进：还有任务进程用着检出里的代码。它们结束后运行 git -C {repo} merge --ff-only {short(target)}，"
            "或下次运行 scripts/upgrade.sh 时自动补上")
        return
    merged = git("merge", "--ff-only", "--quiet", target)
    if merged.returncode:
        say(f"检出没能快进到 {short(target)}（{' '.join((merged.stderr or merged.stdout).split())[-200:]}）：机器人已经是新版本，"
            f"检出自己运行 git -C {repo} merge --ff-only {short(target)}")
    else:
        say(f"检出已快进到 {short(target)}")


say("升级 Covey")
say(f"    检出      {repo}（{short(head)}）")
say(f"    升到      {ref} = {short(target)}" + (f"（比检出新 {newer} 个提交）" if newer != "0" else ""))
say(f"    机器人    {state}（现网：" + (f"release {live_id}" if live_id else "直接从检出运行") + "）")
if args.status:
    if not args.json:  # The page's checks (--json) need no lark-cli: a check is a fetch, nothing more.
        lark_cli_upgrade.note(env, say)
    report()
if personal_changed:
    say("    个人扩展  " + ("、".join(f"{Path(found['path']).name}（{short(found['commit'])}）" for found in mine_found["repos"])
                         or "（数据根 extensions/ 下已经没有）") + "：和现网带的不一样，这次一起换")
if live_commit == target and not personal_changed:  # A service run from the checkout runs its HEAD.
    say(f"已经是最新（{short(target)}），不用升级。")
    if args.dry_run:
        lark_cli_upgrade.note(env, say)
        sys.exit(0)
    lark_cli_step()
    fast_forward()
    write_upgrader()
    sys.exit(web_step(data / "releases" / live_id if live_id is not None else repo))

# ---- 3. dependencies
python = command[0] if command else "python3"
try:
    version = subprocess.run([python, "-c", "import sys; print(sys.version_info >= (3, 10))"], capture_output=True,
                             text=True, timeout=30).stdout.strip()
except (OSError, subprocess.SubprocessError):
    version = ""
if version != "True":
    stop(1, f"服务用的 Python 不能用或低于 3.10：{python}", "装好 Python 3.10+，用它重新运行 ./install 注册服务，再升级")
if not shutil.which("lark-cli", path=saved.get("PATH", "")):
    say("提示：服务的 PATH 里找不到 lark-cli（机器人收发消息要用它）；升级照常做，之后用 ./install 检查")

# ---- 4. the pre-check
values, options, rest = {}, [], command[3:]
index, sandboxed = 0, False
while index < len(rest):
    word = rest[index]
    value = rest[index + 1] if index + 1 < len(rest) else None
    if word in KNOWN and word not in values and value is not None:
        values[word] = value
        index += 2
    elif word == "--sandbox" and not sandboxed and value == "danger-full-access":  # The one bootstrap adds.
        sandboxed = True
        index += 2
    else:
        options.append(word)
        index += 1
missing = [name for name in ("--workdir", "--profile", "--app-id") if name not in values]
if missing:
    stop(2, "从服务清单的启动命令里读不出 " + "、".join(missing), "照 docs/deploy.md「升级与回滚」手工部署")
stamp = time.strftime("%Y%m%d-%H%M%S")
deploy = [str(repo / "scripts" / "deploy_bridge.sh"), "--repo", str(repo), "--commit", target,
          "--workdir", values["--workdir"], "--profile", values["--profile"], "--app-id", values["--app-id"],
          "--state-dir", state, "--harness-config", values.get("--harness-config") or str(data / "harnesses.json")]
tail = ["--", *options] if options else []
if args.allow_dropped_config:
    deploy.append("--allow-dropped-config")


def run_deploy(*extra, run_dir):
    try:
        done = subprocess.run(["bash", *deploy, "--run-dir", str(run_dir), *extra, *tail], env=env, capture_output=True,
                              text=True, timeout=1800)
    except subprocess.TimeoutExpired:
        stop(1, "部署脚本半小时没结束", f"看 {run_dir} 里的日志")
    try:
        return done.returncode, json.loads(done.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return done.returncode, {"ok": False, "error": " ".join((done.stderr or done.stdout).split())[-300:] or f"exit {done.returncode}"}


def explain(code, result, run_dir):
    """Say why a deploy did not go through and exit with its code (1, or 3 for a deploy running already)."""
    if code == 3:
        holder = result.get("deploy_running") or {}
        stop(3, "另一个部署正在跑" + (f"（pid {holder.get('pid')}，{holder.get('started')} 开始）" if holder.get("pid") else ""),
             "等它结束再运行 scripts/upgrade.sh")
    pre = result.get("pre") or {}
    found = []
    for key, words in PRE_WORDS.items():
        value = pre.get(key)
        if value:
            shown = "、".join(map(str, value)) if isinstance(value, list) else json.dumps(value, ensure_ascii=False)[:300]
            found.append(words.format(value=shown, state=state))
    if any("HTTPS_PROXY" in str(pre.get(key)) for key in ("env_missing", "env_value_diff")):
        found.append("清单里存的是大写的 HTTPS_PROXY：照 docs/deploy.md「升级与回滚」的已知限制改存成小写的 https_proxy 再升级")
    for name, command in (pre.get("extension_disable") or {}).items():
        found.append(f"拦下升级的是扩展 {name}（个人扩展，或数据根还启用着、release 里已经没有的包）：改好之前可以先关掉它再升级（这个部署不再启用它，改好后把 disable 换成 enable"
                     f"、再升级一次）：{command}")
    if pre.get("shell_startup_note"):
        found.append("这个终端设了 " + "、".join(pre.get("shell_startup_env") or []) + "：去掉后重跑（env -u BASH_ENV -u ENV …）")
    for line in found:
        say(f"    {line}")
    stop(code or 1, result.get("error") or "部署没成功", f"日志在 {run_dir}；处理好再运行 scripts/upgrade.sh")


runs = data / "deploy-runs"
check_dir = runs / f"{stamp}-check"
code, result = run_deploy("--check-only", run_dir=check_dir)
if code or not result.get("ok"):
    say("部署前的检查没过：")
    explain(code, result, check_dir)
say("部署前的检查都过了。")
for line in (result.get("pre") or {}).get("personal_notes") or []:
    say(f"    提示：{line}")
if args.dry_run:
    lark_cli_upgrade.note(env, say)
    say("--dry-run：什么都没换。去掉 --dry-run 就会部署 " + short(target) + "。")
    sys.exit(0)

# ---- 5. lark-cli, before the new Bridge starts with it
lark_cli_step()

# ---- 6. the deploy
say("部署中：机器人会重启，几秒内收不到消息和卡片点击（之后收下的会照常处理）；在跑的任务不中断，新进程接管它们。")
deploy_dir = runs / stamp
code, result = run_deploy(run_dir=deploy_dir)
if code or not result.get("ok"):
    say("部署没成功：")
    explain(code, result, deploy_dir)
post = result.get("post") or {}
release = result.get("release_id") or Path(str(result.get("release") or target)).name
say(f"升级好了：机器人第 {post.get('generation') or '?'} 代，从 release {short(release) if len(release) == 40 else release} 运行。")
for found in result.get("personal_extensions") or []:
    say(f"    个人扩展 {Path(found['path']).name} {short(found['commit'])}：{'、'.join(found['packages'])}")
if live_id is None:
    say("以后改开关用 python3 -B bin/covey_service.py set（docs/configuration.md「改开关三步」）；不要再用 ./install 改，"
        "它会把服务注册回检出目录。")
todo = [f"这一版起默认打开（你的服务清单没设）：{'、'.join(switches_on)}；不要哪个就 python3 -B bin/covey_service.py set "
        f"--state-dir {state} <名字>=0（docs/configuration.md「默认已开」）"
        for switches_on in [(result.get("pre") or {}).get("switches_on") or []] if switches_on]
todo += list(post.get("owner_todo") or [])
if todo:
    say("升级后要你补的：")
    for number, line in enumerate(todo, 1):
        say(f"    {number}. {line}")
fast_forward()
write_upgrader()
sys.exit(web_step(data / "releases" / release))
PY
