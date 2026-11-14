#!/bin/bash
# Deploy Covey from a git checkout, following docs/operations.md#部署新版本 steps 1-4.
# Prints one JSON line on stdout (exit 0: ok, 1: a check failed, 2: bad arguments); command logs go to the run dir.
#
#   deploy_bridge.sh --workdir DIR --profile NAME [--app-id ID] [--state-dir DIR] [--harness-config FILE]
#                    [--check-only] [--repo CHECKOUT] [--commit REV] [--run-dir DIR] [--allow-command-change]
#                    [-- BRIDGE_OPTION ...]
#
# Names as in docs/operations.md: COVEY_DATA_HOME (required), APP_ID and STATE_DIR when --app-id and
# --state-dir are absent (STATE_DIR defaults to $COVEY_DATA_HOME/harness-$APP_ID), the service root from
# bin/covey_paths.py service-root, --harness-config defaults to $COVEY_DATA_HOME/harnesses.json.
# The options after -- go to bootstrap as they are (e.g. --sandbox workspace-write). The run dir defaults to
# $TMPDIR/bridge-deploy-<time>; its tmp/ becomes TMPDIR. HTTPS_PROXY is unset (a task shell inherits the Manager's).
#
# Pre-checks (abort on any): status.restart_blockers empty, no launch form creating_worktree, no 代答 batch with
# status sending, the manifest's environment == runtime_environment() (names and values), neither of them nor the
# manifest of another running service in the service root keeping a permission variable the backends no longer
# read with a value that restricted them (core/ignored_env.py loosened: LARK_BRIDGE_CODEX_SANDBOX other than
# danger-full-access, LARK_CLAUDE_BRIDGE_SKIP_PERMISSIONS off; the services covey_doctor --runtime-only fails on), and
# the Bridge command bootstrap would register == the manifest's (python, arguments; --allow-command-change lets
# it differ).
# --check-only stops there. Then: git archive REV into releases/<sha>, bootstrap from the release.
# Post-checks: a new `bridge started … release=<sha>` line, two `ready event_key=` after it,
# /proc/<pid>/cmdline runs the new release, covey_doctor --runtime-only exits 0, (reported only) what the
# Feishu app lacks (bin/covey_app_setup.py check),
# status.health.workers.proxy is true (or absent), every task runner alive before the deploy is adopted.
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
unset HTTPS_PROXY
exec python3 -I - "$ROOT" "$@" <<'PY'
import argparse, glob, json, os, re, shutil, subprocess, sys, time
from pathlib import Path

result = {"ok": False}


def finish(code=None, **kw):
    result.update(kw)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    sys.exit(code if code is not None else 0 if result["ok"] else 1)


ap = argparse.ArgumentParser(prog="deploy_bridge.sh")
ap.add_argument("--check-only", action="store_true")
ap.add_argument("--repo", default=sys.argv[1])
ap.add_argument("--commit", default="HEAD")
ap.add_argument("--run-dir")
ap.add_argument("--allow-command-change", action="store_true")
ap.add_argument("--app-id", default=os.environ.get("APP_ID"))
ap.add_argument("--state-dir", default=os.environ.get("STATE_DIR"))
ap.add_argument("--workdir")
ap.add_argument("--profile")
ap.add_argument("--harness-config")
ap.add_argument("bridge_options", nargs="*")
args = ap.parse_args(sys.argv[2:])

# COVEY_DATA_HOME, or the old name LARK_BRIDGE_DATA_HOME (core/env.py): either one does.
DATA_HOME = os.environ.get("COVEY_DATA_HOME") or os.environ.get("LARK_BRIDGE_DATA_HOME")
if not DATA_HOME:
    finish(2, error="set COVEY_DATA_HOME (docs/operations.md)")
DATA = Path(DATA_HOME)
missing = [name for name, value in (("--app-id or APP_ID", args.app_id), ("--workdir", args.workdir),
                                    ("--profile", args.profile)) if not value]
if missing:
    finish(2, error=f"missing {', '.join(missing)}")
if not Path(args.workdir).is_dir():
    finish(2, error=f"--workdir {args.workdir} is not a directory")
if not args.run_dir and not os.environ.get("TMPDIR"):
    finish(2, error="set --run-dir or TMPDIR (the run dir holds the logs)")
S = Path(args.state_dir or DATA / f"harness-{args.app_id}")
HARNESS_CONFIG = args.harness_config or str(DATA / "harnesses.json")
run = Path(args.run_dir or Path(os.environ["TMPDIR"]) / f"bridge-deploy-{time.strftime('%Y%m%d-%H%M%S')}").absolute()
(run / "tmp").mkdir(parents=True, exist_ok=True)
os.environ["TMPDIR"] = str(run / "tmp")
BOOTSTRAP = ["--workdir", args.workdir, "--profile", args.profile, "--app-id", args.app_id, "--state-dir", str(S),
             "--harness-config", HARNESS_CONFIG, *args.bridge_options]
# What bootstrap_harness_bridge.sh registers after the entry script, from the arguments above.
BRIDGE_ARGS = ["--harness", "unified", "--state-dir", str(S), "--workdir", args.workdir, "--profile", args.profile,
               "--app-id", args.app_id, "--sandbox", "danger-full-access",
               "--harness-config", HARNESS_CONFIG, *args.bridge_options]
result.update(run_dir=str(run), state_dir=str(S), bootstrap=BOOTSTRAP)


def sh(cmd, cwd=None, timeout=120, log=None):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if log:
        (run / log).write_text(f"$ {' '.join(cmd)}\nexit={p.returncode}\n--- stdout\n{p.stdout}\n--- stderr\n{p.stderr}")
    return p


def json_of(cmd, cwd, what):
    p = sh(cmd, cwd=cwd, timeout=60)
    try:
        return json.loads(p.stdout)
    except ValueError:
        finish(error=f"{what} failed (exit {p.returncode}): {(p.stderr or p.stdout).strip()[-300:]}")


def status(code_dir):
    return json_of(["python3", "-B", "bin/covey_service.py", "status", "--state-dir", str(S)], code_dir, "status")


def runners():
    """pid -> job number of live task runners whose --run-dir is under this bot's runs/."""
    found = {}
    for proc in glob.glob("/proc/[0-9]*/cmdline"):
        try:
            argv = Path(proc).read_bytes().split(b"\0")
        except OSError:
            continue
        argv = [a.decode(errors="replace") for a in argv]
        if not any(a.endswith("/covey_runner.py") for a in argv) or "--run-dir" not in argv:
            continue
        run_dir = Path(argv[argv.index("--run-dir") + 1])
        if run_dir.parent != S / "runs":
            continue
        try:
            job = json.loads((run_dir / "runner.json").read_text()).get("job_id")
        except (OSError, ValueError):
            job = None
        found[int(proc.split("/")[2])] = job
    return found


def log_after_start(commit):
    """bridge.log lines after the last `bridge started` line of this release (None if not there yet)."""
    try:
        lines = (S / "bridge.log").read_text(errors="replace").splitlines()
    except OSError:
        return None, None
    for i in range(len(lines) - 1, -1, -1):
        if "bridge started pid=" in lines[i]:
            return (lines[i], lines[i + 1:]) if f"release={commit}" in lines[i] else (None, None)
    return None, None


repo = Path(args.repo).absolute()
commit = sh(["git", "rev-parse", "--verify", f"{args.commit}^{{commit}}"], cwd=repo).stdout.strip()
if not commit:
    finish(2, error=f"{args.commit} is not a commit in {repo}")
release = DATA / "releases" / commit
result.update(commit=commit, release=str(release))

# ---- pre-checks
pre = {}
before = status(repo)
pre["restart_blockers"] = before.get("restart_blockers")
pre["creating_worktree"] = [Path(p).name for p in glob.glob(str(S / "launch-forms" / "*.json"))
                            if '"status": "creating_worktree"' in Path(p).read_text(errors="replace")]
sending = []
for p in glob.glob(str(S / "proxy" / "batches" / "*.json")):
    try:
        if json.loads(Path(p).read_text()).get("status") == "sending":
            sending.append(Path(p).stem)
    except (OSError, ValueError):
        sending.append(Path(p).stem + ":unreadable")
pre["proxy_sending"] = sending
service_root = Path(sh(["python3", "-B", "bin/covey_paths.py", "service-root"], cwd=repo).stdout.strip())
manifests, others = [], {}
for p in glob.glob(str(service_root / "services" / "*.json")):
    try:
        found = json.loads(Path(p).read_text())
        if found.get("state_dir") == str(S.resolve()):
            manifests.append(p)
        elif found.get("desired_state") == "running":
            # Another running service: covey_doctor --runtime-only after the deploy checks its environment too.
            env = found.get("environment")
            others[str(found.get("id") or Path(p).stem)] = env if isinstance(env, dict) else {}
    except (OSError, ValueError, AttributeError):
        pass
if len(manifests) != 1:
    finish(pre=pre, error=f"expected one manifest for {S} in {service_root}/services, found {manifests}")
manifest = json.loads(Path(manifests[0]).read_text())
env_code = ("import json; from covey.service.manager import runtime_environment; "
            "print(json.dumps(runtime_environment()))")
mine = json_of(["python3", "-B", "-c", env_code], repo, "runtime_environment()")
# A manifest keeps the old LARK_BRIDGE_ names (runtime_environment() saves them so); a COVEY_ one is read as its old name.
theirs = {("LARK_BRIDGE_" + k[len("COVEY_"):] if k.startswith("COVEY_") else k): v
          for k, v in (manifest.get("environment") or {}).items()}
pre["env_names"] = len(theirs)
pre["env_missing"] = sorted(set(theirs) - set(mine))
pre["env_extra"] = sorted(set(mine) - set(theirs))
pre["env_value_diff"] = sorted(k for k in set(mine) & set(theirs) if mine[k] != theirs[k])
loose_code = ("import json, sys; from covey.core.ignored_env import loosened; "
              "print(json.dumps(sorted(set(loosened(json.loads(sys.argv[1])) + loosened(json.loads(sys.argv[2]))))"
              " + [f'service {name}: {line}' for name, env in sorted(json.loads(sys.argv[3]).items())"
              " for line in loosened(env)]))")
pre["permission_env"] = json_of(["python3", "-B", "-c", loose_code, json.dumps(theirs), json.dumps(mine),
                                 json.dumps(others)], repo, "loosened()")
live = manifest.get("command") or []
planned = [shutil.which("python3") or "python3", "-B"]
same = live[:2] == planned and live[3:] == BRIDGE_ARGS
pre["command_diff"] = {} if same else {"live": live[:2] + live[3:], "planned": planned + BRIDGE_ARGS}
old = before.get("status") or {}
result.update(pre=pre, old={"generation": old.get("generation"), "pid": (old.get("process") or {}).get("pid")})
blocked = [k for k in ("restart_blockers", "creating_worktree", "proxy_sending",
                       "env_missing", "env_extra", "env_value_diff", "permission_env", "command_diff") if pre[k]
           and not (k == "command_diff" and args.allow_command_change)]
if blocked:
    finish(error=f"pre-checks failed: {blocked}")
if args.check_only:
    finish(ok=True, note="check only, nothing deployed")

# ---- pack and bootstrap
live_runners = runners()
result["runners_before"] = {str(pid): job for pid, job in sorted(live_runners.items())}
if not (release / "bin" / "covey_bridge.py").exists():
    release.mkdir(parents=True, exist_ok=True)
    pack = subprocess.run(f"git archive {commit} | tar -x -C '{release}'", shell=True, cwd=repo,
                          capture_output=True, text=True, timeout=300)
    if pack.returncode:
        finish(error=f"git archive failed: {pack.stderr[-500:]}")
boot = sh(["./scripts/bootstrap_harness_bridge.sh", *BOOTSTRAP], cwd=release, timeout=300, log="bootstrap.log")
result["bootstrap_exit"] = boot.returncode
if boot.returncode:
    finish(error="bootstrap failed, see bootstrap.log")

# ---- post-checks
post = {}
deadline = time.time() + 240
started, after = None, None
while time.time() < deadline:
    started, after = log_after_start(commit)
    if started and sum("ready event_key=" in line for line in after) >= 2:
        break
    time.sleep(3)
post["started_line"] = started
post["ready_lines"] = sum("ready event_key=" in line for line in after or [])
pending = {}
while started and time.time() < deadline:
    _, after = log_after_start(commit)
    text = "\n".join(after or [])
    pending = {pid: job for pid, job in live_runners.items()
               if f"adopted task runner job=#{job} pid={pid}" not in text and os.path.exists(f"/proc/{pid}")}
    if not pending:
        break
    time.sleep(3)
text = "\n".join(after or [])
post["adopted"] = sorted(int(m) for m in re.findall(r"adopted task runner job=#(\d+)", text))
post["not_adopted_alive"] = {str(pid): job for pid, job in pending.items()}
post["ended_meanwhile"] = sorted(job for pid, job in live_runners.items() if not os.path.exists(f"/proc/{pid}"))
now = status(release)
st = now.get("status") or {}
pid = (st.get("process") or {}).get("pid")
try:
    cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
except (OSError, TypeError):
    cmdline = []
post.update(generation=st.get("generation"), pid=pid, health=(st.get("health") or {}).get("status"),
            cmdline_release=any(a.decode().startswith(str(release) + "/") for a in cmdline))
workers = (st.get("health") or {}).get("workers") or {}
post["workers_proxy"] = workers.get("proxy") if "proxy" in workers else "absent"
doctor = sh(["python3", "-B", "bin/covey_doctor.py", "--runtime-only"], cwd=release, timeout=180, log="doctor.log")
post["doctor_exit"] = doctor.returncode
# The Feishu app's configuration (docs/operations.md 自动补齐飞书应用): reported, never a failed deploy; the Bridge
# itself offers the owner what is missing.
check = sh(["python3", "-B", "bin/covey_app_setup.py", "check", "--profile", args.profile, "--app-id", args.app_id,
            "--json"], cwd=release, timeout=120, log="app-check.log")
try:
    post["app_config"] = json.loads(check.stdout.strip().splitlines()[-1])
except (ValueError, IndexError, AttributeError):
    post["app_config"] = {"exit": check.returncode}
post["errors_after_start"] = [line[:300] for line in after or []
                              if "Traceback" in line or " failed" in line or "ERROR" in line][:10]
ok = (bool(started) and post["ready_lines"] >= 2 and post["cmdline_release"] and doctor.returncode == 0
      and post["workers_proxy"] in (True, "absent") and not pending)
finish(ok=ok, post=post)
PY
