#!/bin/bash
# Deploy Covey from a git checkout, following docs/deploy.md#部署新版本 steps 1-4.
# Prints one JSON line on stdout (exit 0: ok, 1: a check failed, 2: bad arguments, 3: another deploy is running);
# command logs go to the run dir.
#
#   deploy_bridge.sh --workdir DIR --profile NAME [--app-id ID] [--state-dir DIR] [--harness-config FILE]
#                    [--check-only] [--repo CHECKOUT] [--commit REV] [--run-dir DIR] [--allow-command-change]
#                    [--ext-repo CHECKOUT [--ext-commit REV]]... [--allow-core-mismatch] [--allow-rollback]
#                    [--allow-extension-checks] [-- BRIDGE_OPTION ...]
#
# Names as in docs/operations.md: COVEY_DATA_HOME (required), APP_ID and STATE_DIR when --app-id and
# --state-dir are absent (STATE_DIR defaults to $COVEY_DATA_HOME/harness-$APP_ID), the service root from
# bin/covey_paths.py service-root, --harness-config defaults to $COVEY_DATA_HOME/harnesses.json.
# The options after -- go to bootstrap as they are (e.g. --sandbox workspace-write). The run dir defaults to
# $TMPDIR/bridge-deploy-<time>; its tmp/ becomes TMPDIR. HTTPS_PROXY is unset (a task shell inherits the Manager's).
#
# One deploy at a time: a deploy (not --check-only) takes the lock <service root>/deploy.lock (flock, released when
# this process exits, also on a crash) before its pre-checks and keeps it to the end. The lock file says who holds it
# (pid, host, commit, release, start, run dir, task). When another deploy holds it, the script exits 3 at once with
# "deploy_running" (that holder) and touches nothing: wait and run it again, each try a short command (that is the
# queue). --check-only takes no lock; it reports "pre.deploy_running" (the holder or null) and is not blocked by it.
# Pre-checks (abort on any): status.restart_blockers empty, no launch form creating_worktree, no 代答 batch with
# status sending, the manifest's environment == runtime_environment() (names and values), neither of them nor the
# manifest of another running service in the service root keeping a permission variable the backends no longer
# read with a value that restricted them (core/ignored_env.py loosened: LARK_BRIDGE_CODEX_SANDBOX other than
# danger-full-access, LARK_CLAUDE_BRIDGE_SKIP_PERMISSIONS off; the services covey_doctor --runtime-only fails on), and
# the Bridge command bootstrap would register == the manifest's (python, arguments; --allow-command-change lets
# it differ), and no rollback: the live release's commits (the manifest's entry script under
# $COVEY_DATA_HOME/releases/<id>/, its RELEASE_MANIFEST or the id) must be ancestors of --commit (and of each
# --ext-commit, in order, when the live release has as many extensions); "pre.rollback" says which is not, or which
# the checkout does not have (fetch it); --allow-rollback lets it go back on purpose. A live service not run from a
# release under releases/ is not compared ("pre.live_release" is null).
# --check-only stops there. Then: git archive REV into releases/<sha>, write its .pyc files (below), bootstrap from it.
# Precompiling: the service runs every Python process with -B, so a release without __pycache__ is compiled from source
# again at each process start. Before bootstrap, scripts/precompile_release.py runs under the python3 bootstrap
# registers (the one the pre-check compared with the manifest), also for a release that already exists. A compile
# failure is only a warning (result "precompile"), never a failed deploy; compiled files are skipped the next time.
# Extensions (docs/deploy.md 核心和扩展分开检出): each --ext-repo (repeatable; --ext-commit pairs with it in order, default
# HEAD) is an extension checkout laid out as scripts/export_extension.py writes it. The release is then assembled by
# scripts/assemble_release.py (core at --commit + the extensions, the shape of the old single repository) and its id
# is <core 8>-<extension 8>[-<extension 8>...] instead of <sha>. More pre-checks: the checkouts and commits exist; the
# core.lock of an extension names --commit (--allow-core-mismatch lets it differ); the manifest environment and this
# shell have neither COVEY_EXTENSION_PATH nor COVEY_EXTENSIONS (they would override the release's extensions/); and
# the enabled extensions' own checks pass ("pre.extension_checks", notes in "pre.extension_notes"): the release is
# assembled into the run dir's tmp/ (or the existing release used) and `bin/covey_plugins.py deploy-check` run in it
# with --harness-config, so an extension finds what this release would run with (an isolated interpreter whose
# source is not the revision the release pins, say); --allow-extension-checks lets a failing one go. A release whose
# core has no deploy-check (an older one, for a rollback) is not checked (a note says so).
# Without --ext-repo nothing changes.
# Post-checks: a new `bridge started … release=<id>` line, two `ready event_key=` after it,
# /proc/<pid>/cmdline runs the new release, covey_doctor --runtime-only exits 0, (reported only) what the
# Feishu app lacks (bin/covey_app_setup.py check),
# status.health.workers.proxy is true (or absent), every task runner alive before the deploy is adopted.
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
unset HTTPS_PROXY
exec python3 -I - "$ROOT" "$@" <<'PY'
import argparse, fcntl, glob, json, os, re, shutil, socket, subprocess, sys, time
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
ap.add_argument("--ext-repo", action="append", default=[])
ap.add_argument("--ext-commit", action="append", default=[])
ap.add_argument("--allow-core-mismatch", action="store_true")
ap.add_argument("--allow-rollback", action="store_true")
ap.add_argument("--allow-extension-checks", action="store_true")
ap.add_argument("--app-id", default=os.environ.get("APP_ID"))
ap.add_argument("--state-dir", default=os.environ.get("STATE_DIR"))
ap.add_argument("--workdir")
ap.add_argument("--profile")
ap.add_argument("--harness-config")
ap.add_argument("bridge_options", nargs="*")
args = ap.parse_args(sys.argv[2:])
if len(args.ext_commit) > len(args.ext_repo):
    finish(2, error="--ext-commit needs a --ext-repo before it")

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


def log_after_start(release_id):
    """bridge.log lines after the last `bridge started` line of this release (None if not there yet)."""
    try:
        lines = (S / "bridge.log").read_text(errors="replace").splitlines()
    except OSError:
        return None, None
    for i in range(len(lines) - 1, -1, -1):
        if "bridge started pid=" in lines[i]:
            return (lines[i], lines[i + 1:]) if f"release={release_id}" in lines[i] else (None, None)
    return None, None


repo = Path(args.repo).absolute()
commit = sh(["git", "rev-parse", "--verify", f"{args.commit}^{{commit}}"], cwd=repo).stdout.strip()
if not commit:
    finish(2, error=f"{args.commit} is not a commit in {repo}")
exts = []  # (checkout, full commit, core.lock's commit or None)
for number, ext_dir in enumerate(args.ext_repo):
    ext_path = Path(ext_dir).absolute()
    rev = args.ext_commit[number] if number < len(args.ext_commit) else "HEAD"
    if not ext_path.is_dir() or sh(["git", "rev-parse", "--git-dir"], cwd=ext_path).returncode:
        finish(2, error=f"--ext-repo {ext_dir} is not a git checkout")
    ext_sha = sh(["git", "rev-parse", "--verify", f"{rev}^{{commit}}"], cwd=ext_path).stdout.strip()
    if not ext_sha:
        finish(2, error=f"{rev} is not a commit in {ext_path}")
    lock = sh(["git", "show", f"{ext_sha}:core.lock"], cwd=ext_path)
    words = [ln.split()[0] for ln in lock.stdout.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    exts.append((ext_path, ext_sha, words[0] if lock.returncode == 0 and words else None))
release_id = "-".join([commit[:8], *(sha[:8] for _, sha, _ in exts)]) if exts else commit
release = DATA / "releases" / release_id
result.update(commit=commit, release=str(release))
if exts:
    result.update(ext_commits=[sha for _, sha, _ in exts], release_id=release_id)

service_root = Path(sh(["python3", "-B", "bin/covey_paths.py", "service-root"], cwd=repo).stdout.strip())


def deploy_lock():
    """(fd, None) with <service root>/deploy.lock held, or (None, what its holder wrote). On NFS flock is a POSIX
    lock, which any close of the file in this process drops: the holder never opens the file again."""
    path = service_root / "deploy.lock"
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError as exc:
        finish(error=f"cannot open {path}: {exc}")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        try:
            said = json.loads(os.pread(fd, 4096, 0) or b"null")
        except ValueError:
            said = None
        os.close(fd)
        return None, said if isinstance(said, dict) else {"holder": "unknown (it is starting)"}
    return fd, None


# ---- one deploy at a time
held, running = deploy_lock()
if args.check_only:
    if held is not None:
        os.close(held)  # A check only looks.
        held = None
elif held is not None:
    os.ftruncate(held, 0)
    os.pwrite(held, json.dumps({"pid": os.getpid(), "host": socket.gethostname(), "commit": commit,
                                "release_id": release_id, "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                                "run_dir": str(run), "task": os.environ.get("COVEY_TASK_DIR")},
                               ensure_ascii=False).encode(), 0)
else:
    finish(3, deploy_running=running, error="another deploy is running (deploy_running): wait and run this again")

# ---- pre-checks
pre = {"deploy_running": running}
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
# A manifest keeps the old LARK_BRIDGE_ names (runtime_environment() saves them so, a renamed variable under its old name,
# LARK_BRIDGE_MAIN_THREAD for COVEY_BUTLER); a COVEY_ one is read as its old name (core/env.py canonical).
canon_code = "import json, sys; from covey.core import env; print(json.dumps(env.canonical(json.loads(sys.argv[1]))))"
theirs = json_of(["python3", "-B", "-c", canon_code, json.dumps(manifest.get("environment") or {})], repo, "env.canonical()")
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
if exts:
    pre["core_lock_mismatch"] = [f"{path.name}: core.lock {lock} vs --commit {commit[:8]}" for path, _, lock in exts
                                 if lock and not commit.startswith(lock) and not lock.startswith(commit[:len(lock)])]
    pre["extension_env"] = sorted({name for env in (manifest.get("environment") or {}, mine) for name in env
                                   if name.removeprefix("COVEY_").removeprefix("LARK_BRIDGE_")
                                   in ("EXTENSION_PATH", "EXTENSIONS")})
    if pre["extension_env"]:
        pre["extension_env_note"] = ("COVEY_EXTENSION_PATH / COVEY_EXTENSIONS override the release's extensions/: "
                                     "unset them in the shell and re-register without them")


def extension_checks():
    """(problems, notes) of `covey_plugins.py deploy-check` run in the release this deploy would make: the existing
    one, else one assembled into the run dir's tmp/ and removed after (a check touches nothing outside the run dir)."""
    probe = release if (release / "bin" / "covey_bridge.py").exists() else run / "tmp" / "extension-check-release"
    try:
        if probe != release:
            shutil.rmtree(probe, ignore_errors=True)
            cmd = ["python3", "-B", "-I", str(Path(sys.argv[1]) / "scripts" / "assemble_release.py"), "--core", str(repo),
                   "--core-ref", commit, "--out", str(probe)]
            for path, sha, _ in exts:
                cmd += ["--ext", str(path), "--ext-ref", sha]
            built = sh(cmd, timeout=600, log="extension-check-assemble.log")
            if built.returncode:
                return [f"assembling the release to check failed: {(built.stderr or built.stdout).strip()[-300:]}"], []
        if "deploy-check" not in sh(["python3", "-B", "bin/covey_plugins.py", "--help"], cwd=probe).stdout:
            return [], ["this release's core has no covey_plugins.py deploy-check: extensions not checked"]
        done = sh(["python3", "-B", "bin/covey_plugins.py", "deploy-check", "--harness-config", HARNESS_CONFIG],
                  cwd=probe, timeout=300, log="extension-check.log")
        try:
            said = json.loads(done.stdout.strip().splitlines()[-1])
            return list(said.get("problems") or []), list(said.get("notes") or [])
        except (ValueError, IndexError, AttributeError):
            return [f"deploy-check failed (exit {done.returncode}): {(done.stderr or done.stdout).strip()[-300:]}"], []
    except subprocess.TimeoutExpired as exc:
        return [f"extension checks timed out: {exc}"], []
    finally:
        if probe != release:
            shutil.rmtree(probe, ignore_errors=True)


if exts:
    pre["extension_checks"], pre["extension_notes"] = extension_checks()


def live_commits(entry):
    """(release id, [core commit, extension commits...]) of the live entry script, (None, []) when it is not run
    from a release under $COVEY_DATA_HOME/releases/."""
    try:
        rel = Path(entry).resolve().relative_to((DATA / "releases").resolve())
    except (ValueError, OSError, TypeError):
        return None, []
    rid = rel.parts[0] if len(rel.parts) > 1 else None
    if not rid:
        return None, []
    try:
        lines = (DATA / "releases" / rid / "RELEASE_MANIFEST").read_text().splitlines()
        found = [ln.split()[1] for ln in lines if ln.split()[:1] in (["core"], ["extension"]) and len(ln.split()) > 1]
    except OSError:
        found = []
    return rid, found or rid.split("-")


def not_after(live_sha, checkout, new_sha, what):
    """Why deploying new_sha would go back from live_sha (None when it contains it)."""
    full = sh(["git", "rev-parse", "--verify", "--quiet", f"{live_sha}^{{commit}}"], cwd=checkout).stdout.strip()
    if not full:
        return f"{what}: the live {live_sha[:12]} is not in {checkout} (fetch it first)"
    if sh(["git", "merge-base", "--is-ancestor", full, new_sha], cwd=checkout).returncode:
        return f"{what}: {new_sha[:8]} does not contain the live {full[:8]} (an older or another line)"
    return None


live_id, live_shas = live_commits(live[2] if len(live) > 2 else None)
pre["live_release"] = live_id
pairs = [(live_shas[0], repo, commit, "core")] if live_shas else []
if live_shas and len(live_shas) - 1 == len(exts):
    pairs += [(sha, path, new, f"extension {path.name}") for sha, (path, new, _) in zip(live_shas[1:], exts)]
pre["rollback"] = [why for why in (not_after(*pair) for pair in pairs) if why]
old = before.get("status") or {}
result.update(pre=pre, old={"generation": old.get("generation"), "pid": (old.get("process") or {}).get("pid")})
blocked = [k for k in ("restart_blockers", "creating_worktree", "proxy_sending",
                       "env_missing", "env_extra", "env_value_diff", "permission_env", "command_diff",
                       "extension_env", "core_lock_mismatch", "extension_checks", "rollback") if pre.get(k)
           and not (k == "command_diff" and args.allow_command_change)
           and not (k == "extension_checks" and args.allow_extension_checks)
           and not (k == "rollback" and args.allow_rollback)
           and not (k == "core_lock_mismatch" and args.allow_core_mismatch)]
if blocked:
    finish(error=f"pre-checks failed: {blocked}")
if args.check_only:
    finish(ok=True, note="check only, nothing deployed")

# ---- pack and bootstrap
live_runners = runners()
result["runners_before"] = {str(pid): job for pid, job in sorted(live_runners.items())}
if not (release / "bin" / "covey_bridge.py").exists():
    release.mkdir(parents=True, exist_ok=True)
    if exts:
        cmd = ["python3", "-B", "-I", str(Path(sys.argv[1]) / "scripts" / "assemble_release.py"), "--core", str(repo),
               "--core-ref", commit, "--out", str(release)]
        for path, sha, _ in exts:
            cmd += ["--ext", str(path), "--ext-ref", sha]
        pack = sh(cmd, timeout=600, log="assemble.log")
        what = "assemble_release.py"
    else:
        pack = subprocess.run(f"git archive {commit} | tar -x -C '{release}'", shell=True, cwd=repo,
                              capture_output=True, text=True, timeout=300)
        what = "git archive"
    if pack.returncode:
        finish(error=f"{what} failed: {pack.stderr[-500:]}")
# The service never writes bytecode itself (-B, PYTHONDONTWRITEBYTECODE=1), so compile once here, with the interpreter
# bootstrap registers; a failure is a warning only.
try:
    comp = sh([planned[0], "-B", "-I", str(Path(sys.argv[1]) / "scripts" / "precompile_release.py"), str(release)],
              timeout=600, log="precompile.log")
    try:
        result["precompile"] = json.loads(comp.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        result["precompile"] = {"ok": False, "exit": comp.returncode, "failed": [(comp.stderr or comp.stdout)[-200:]]}
except (OSError, subprocess.TimeoutExpired) as exc:
    result["precompile"] = {"ok": False, "failed": [str(exc)]}
boot = sh(["./scripts/bootstrap_harness_bridge.sh", *BOOTSTRAP], cwd=release, timeout=300, log="bootstrap.log")
result["bootstrap_exit"] = boot.returncode
if boot.returncode:
    finish(error="bootstrap failed, see bootstrap.log")

# ---- post-checks
DOCTOR_SETTLE = 90
post = {}
deadline = time.time() + 240
started, after = None, None
while time.time() < deadline:
    started, after = log_after_start(release_id)
    if started and sum("ready event_key=" in line for line in after) >= 2:
        break
    time.sleep(3)
post["started_line"] = started
post["ready_lines"] = sum("ready event_key=" in line for line in after or [])
pending = {}
while started and time.time() < deadline:
    _, after = log_after_start(release_id)
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
# The transport watchdog's first look, seconds after the start, can find the event bus still connecting: the doctor
# says degraded and is ready a few seconds later (generations 444 and 446, 2026-11-20). Asked again for up to
# DOCTOR_SETTLE seconds before its answer counts; doctor_tries says how many it took.
doctor_until = time.time() + DOCTOR_SETTLE
tries = 0
while True:
    tries += 1
    doctor = sh(["python3", "-B", "bin/covey_doctor.py", "--runtime-only"], cwd=release, timeout=180, log="doctor.log")
    if doctor.returncode == 0 or time.time() >= doctor_until:
        break
    time.sleep(10)
post["doctor_exit"] = doctor.returncode
post["doctor_tries"] = tries
# The Feishu app's configuration (docs/feishu-app.md 自动补齐飞书应用): reported, never a failed deploy; the Bridge
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
