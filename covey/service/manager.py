#!/usr/bin/env python3
"""Persistent single-host service management for all bridge entrypoints.

Manifests retain exact argv/state paths. A manager restart adopts its verified
children; a bridge restart never resubmits a model job. No credentials are copied
from the calling environment into manifests.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

from covey.core import env as cenv
from covey.core.paths import entry_script, service_root
from covey.core.file_lock import file_lock
from covey.service import memguard
from covey.service.boot import serve
from covey.core.process import (cleanup_group, instance_lock, is_owned, log_to, process_identity, query,
                                         service_key, terminate_owned)
from covey.core.state import read_json, write_durable_json as save
from covey.core.logs import LOG_BYTES, LOG_KEEP, rotate_live
from covey.core.task_processes import TASK_DIR_ENV


RUNTIME_ENV_KEYS = frozenset({"PATH", "LANG", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "DSH_HOME"})
# This project's own settings (LARK_BRIDGE_*, LARK_<HARNESS>_BRIDGE_*): what was set when the
# service was registered is what runs after a reboot. The Manager sets the excluded ones itself.
BRIDGE_SETTING = re.compile(r"LARK_(?:[A-Z0-9]+_)*BRIDGE_[A-Z0-9_]+|COVEY_[A-Z0-9_]+")
MANAGER_ENV_KEYS = frozenset({"LARK_BRIDGE_MANAGED", "LARK_BRIDGE_SERVICE_ROOT", "LARK_BRIDGE_REQUIRED_MOUNT",
                              "COVEY_MANAGED", "COVEY_SERVICE_ROOT", "COVEY_REQUIRED_MOUNT"})
# A task's marker (core/task_processes.py): a launch from a task's shell (deploying from a task) never saves it.
TASK_ENV_KEYS = frozenset({TASK_DIR_ENV, *cenv.spellings(TASK_DIR_ENV)})
SECRET_NAME = re.compile(r"SECRET|TOKEN|PASSWORD|CREDENTIAL|(^|_)KEY($|_)")
PROXY_ENV_KEYS = frozenset({"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                          "http_proxy", "https_proxy", "all_proxy", "no_proxy"})
# What the Manager gives a service's process as its output by default: a Bridge appends its own log to
# <state>/bridge.log as soon as it owns the state directory, so this holds only what came before (a start
# that failed). An older Manager rotates this file at each start, no longer bridge.log (repo review E3).
CONSOLE_LOG = "bridge-console.log"


def say(message):
    """One line of manager.log (the Manager's own output, ``log_to``). An output that cannot be written never
    stops what the Manager was about to do."""
    with contextlib.suppress(OSError, ValueError):
        print(f"[{datetime.datetime.now().astimezone().isoformat(timespec='milliseconds')}] {message}", flush=True)


def release_of(command):
    """The code folder a service's command runs from (a release's commit), or '-'."""
    script = next((Path(a) for a in command if str(a).endswith(".py")), None)
    return script.resolve().parents[1].name if script is not None and len(script.resolve().parents) > 1 else "-"


def persistable(key):
    return (key in RUNTIME_ENV_KEYS or key in PROXY_ENV_KEYS
            or (BRIDGE_SETTING.fullmatch(key) is not None and key not in MANAGER_ENV_KEYS | TASK_ENV_KEYS
                and not SECRET_NAME.search(key)))


def validate_environment(environment):
    if (not all(persistable(k) for k in environment)
            or not all(isinstance(v, str) and not any(c in v for c in "\r\n\0") for v in environment.values())):
        raise ValueError("only explicit non-secret runtime paths/settings can be persisted")
    for key in PROXY_ENV_KEYS & environment.keys():
        value = environment[key]
        if any(c in value for c in "\r\n\0"):
            raise ValueError("invalid proxy environment setting")
        if not value or key.lower() == "no_proxy":
            continue
        try:
            proxy = urlsplit(value)
            valid = (proxy.scheme in {"http", "https", "socks4", "socks4a", "socks5", "socks5h"}
                     and proxy.hostname and proxy.port != 0 and proxy.username is None and proxy.password is None
                     and proxy.path in {"", "/"} and not proxy.query and not proxy.fragment)
        except ValueError:
            valid = False
        if not valid:
            # Never include the value: a rejected proxy URL may contain credentials.
            raise ValueError("only non-secret proxy URLs without credentials can be persisted")


def runtime_environment(environ=None):
    environ = os.environ if environ is None else environ
    # COVEY_<X> is saved as LARK_BRIDGE_<X> (core/env.py canonical): an upgrade changes no manifest and the older
    # release a rollback starts reads the names it knows; the Manager mirrors both spellings into the service.
    settings = cenv.canonical({k: v for k, v in environ.items() if persistable(k)})
    validate_environment(settings)
    return settings


def service_environment(base, saved):
    """The environment of a service's process: ``base`` (the Manager's own) with the manifest's settings over it under
    either spelling, the Manager's marker, and both spellings of every setting (a task process of an older release
    reads only the LARK_BRIDGE_ names)."""
    env = dict(base)
    for name in saved:
        for spelling in cenv.spellings(name):
            env.pop(spelling, None)
    env.update(saved)
    env.update(COVEY_MANAGED="1", LARK_BRIDGE_MANAGED="1")
    return cenv.mirrored(env)


def manifest_path(root, key):
    """The manifest of service ``key`` (the service_key of its state directory): how it runs, as registered."""
    return root / "services" / (key + ".json")


def status_path(root, key):
    """What the Manager last found of service ``key``: its process, generation, failures and health."""
    return root / "status" / (key + ".json")


def error_path(root, key):
    """The error service ``key``'s passes end with, while they do."""
    return root / "errors" / (key + ".json")


def generation(state):
    """The generation the Manager (service_root()) runs the service of ``state`` in. Raises OSError when it is not
    registered there, ValueError, KeyError or TypeError when its manifest does not say."""
    return read_json(manifest_path(service_root(), service_key(state)))["generation"]


@contextlib.contextmanager
def config_lock(root: Path):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with file_lock(root / "config.lock") as fd:
        os.fchmod(fd, 0o600)
        yield


def register(root, state, command, *, workdir, profile="", log=None, environment=None, health_timeout=None):
    state, workdir = state.expanduser().resolve(), workdir.expanduser().resolve()
    if not command or not Path(command[0]).is_absolute():
        raise ValueError("service command must start with an absolute executable path")
    if not workdir.is_dir():
        raise ValueError("workdir does not exist")
    environment = environment or {}
    validate_environment(environment)
    key = service_key(state)
    with config_lock(root):
        path = manifest_path(root, key)
        path.parent.mkdir(exist_ok=True, mode=0o700)
        for other_path in path.parent.glob("*.json"):
            other = read_json(other_path, None)
            if other["id"] != key and other["profile"] == profile and other["desired_state"] == "running":
                raise ValueError("another managed bridge already owns this profile")
        old = read_json(path, {})
        manifest = {"schema_version": 1, "id": key, "state_dir": str(state),
                    "workdir": str(workdir), "command": list(command), "profile": profile,
                    "log": str((log or state / CONSOLE_LOG).resolve()), "environment": environment,
                    "desired_state": "running", "generation": int(old.get("generation", 0)) + 1,
                    "health_timeout": max(0, float(health_timeout if health_timeout is not None else old.get('health_timeout', 0)))}
        save(path, manifest)
    return manifest


def set_desired(root, state, desired, *, restart=False):
    with config_lock(root):
        path = manifest_path(root, service_key(state))
        manifest = read_json(path, None)
        if manifest is None:
            raise ValueError("service is not registered")
        manifest["desired_state"] = desired
        if restart:
            manifest["generation"] += 1
        save(path, manifest)


class Manager:
    def __init__(self, root, *, interval=2, base_delay=5, max_delay=300):
        self.root = root
        self.interval, self.base_delay, self.max_delay = interval, base_delay, max_delay
        self.stop = threading.Event()
        self.children = {}
        self.last_tick = time.monotonic()
        self.unresponsive = {}
        self.reported = {}  # The error last said for each service, for when it cannot be saved (a full disk).

    def health_server(self, sock):
        serve(sock, lambda: {"process": process_identity(os.getpid()), "lag": time.monotonic() - self.last_tick},
              self.stop)

    def reconcile_all(self):
        for path in sorted((self.root / "services").glob("*.json")):
            error = error_path(self.root, path.name.removesuffix(".json"))
            try:
                self.reconcile(read_json(path, None))
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, KeyError, TypeError) as exc:
                # A broken backend/config, or a manifest removed mid-pass, must not take other
                # services down: the boot watchdog would restart into the same file.
                if self.reported.get(path.name) != str(exc) and (read_json(error, None) or {}).get("error") != str(exc):
                    say(f"service {path.stem[:8]}: {exc}")  # Once, not at every pass while it lasts.
                    # A disk that takes no writes (a full quota) fails this save too: the error is said once
                    # all the same, and the Manager goes on supervising (it ended here instead).
                    with contextlib.suppress(OSError):
                        save(error, {"error": str(exc), "checked_at": time.time()})
                self.reported[path.name] = str(exc)
            else:
                self.reported.pop(path.name, None)
                with contextlib.suppress(OSError):  # It has ended: the same error again is news.
                    error.unlink()

    def reconcile(self, manifest):
        key = manifest["id"]
        path = status_path(self.root, key)
        status = read_json(path, {})
        identity = status.get("process")
        child = self.children.get(key)
        if child is not None:
            child.poll()  # reap exits, including during explicit stop
        alive = is_owned(identity)
        if alive and (manifest["desired_state"] != "running" or status.get("generation") != manifest["generation"]):
            say(f"service {key[:8]} pid={identity['pid']}: "
                + ("stopping" if manifest["desired_state"] != "running"
                   else f"generation {status.get('generation')} -> {manifest['generation']}, replacing it"))
            terminate_owned(identity)
            if child is not None:
                child.wait(timeout=5)
            alive = False
            status = {}
        now = time.time()
        if manifest["desired_state"] != "running":
            save(path, {"status": "stopped", "checked_at": now, "generation": manifest["generation"]})
            return
        if alive:
            self._check_running(manifest, key, path, status, identity, now)
            return
        if status.get("process"):
            cleanup_group(identity)
            # An unexpected exit (including code zero) is different from desired stop.
            failures, delay = self._backoff(status)
            status.update(status="backoff", process=None, failures=failures, retry_at=now + delay,
                          returncode=child.poll() if child is not None else None)
            say(f"service {key[:8]} pid={identity['pid']} exited rc={status['returncode']} (failure {failures}); "
                f"starting again in {delay:.0f}s")
            save(path, status)
            return
        if now < status.get("retry_at", 0) and status.get("generation") == manifest["generation"]:
            return
        self._start(manifest, key, path, status, now)

    def _backoff(self, status):
        """(failures, seconds to wait) once a service whose status is ``status`` has failed once more."""
        failures = int(status.get("failures", 0)) + 1
        return failures, min(self.max_delay, self.base_delay * 2 ** min(failures - 1, 12))

    def _check_running(self, manifest, key, path, status, identity, now):
        """Record a live child: its health, and replace it once it has been wholly unresponsive too long."""
        rotate_live(Path(manifest['log']), int(manifest.get('log_max_bytes', LOG_BYTES)), LOG_KEEP)
        status.update(status="running", checked_at=now)
        # A process being alive does not prove subscription readiness.
        # Consumer recovery happens inside the bridge, preserving active jobs.
        health = query('state-' + key, timeout=.5)
        if health and health.get('process') == identity:
            status['health'] = health
            self.unresponsive.pop(key, None)
        else:
            status['health'] = {'status': 'unavailable'}
            if manifest.get('health_timeout', 0):
                previous = self.unresponsive.get(key)
                if not previous or previous[0] != identity:
                    previous = self.unresponsive[key] = (identity, time.monotonic())
                if time.monotonic() - previous[1] > manifest['health_timeout']:
                    # Readiness/auth failures with a responsive health server
                    # do not kill model tasks. Only a wholly unresponsive
                    # process is replaced, using the normal crash backoff.
                    say(f"service {key[:8]} pid={identity['pid']}: no health response for "
                        f"{manifest['health_timeout']:.0f}s, replacing it")
                    terminate_owned(identity)
                    self.unresponsive.pop(key, None)
                    status.update(status='unresponsive', checked_at=time.time())
        if now - status.get("started_at", now) > 300:
            status["failures"] = 0
        save(path, status)

    def _start(self, manifest, key, path, status, now):
        """Spawn the service and record it as running, or as backing off when it is gone at once."""
        state = Path(manifest["state_dir"])
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Refuse takeover of an unmanaged bridge or a reused PID. BridgeApp also
        # holds a local singleton lock, closing the spawn/status-write race.
        try:
            previous_pid = int((state / "bridge.pid").read_text())
        except (OSError, ValueError):
            previous_pid = 0
        if previous_pid and process_identity(previous_pid):
            raise RuntimeError("state has a live unmanaged PID; stop/adopt its owner explicitly")
        log = Path(manifest["log"])
        log.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        rotate_live(log, int(manifest.get('log_max_bytes', LOG_BYTES)), LOG_KEEP)  # By size, not at each start.
        env = service_environment(os.environ, manifest["environment"])
        env.update(PYTHONDONTWRITEBYTECODE="1", TMPDIR=str(state / "tmp"), TMP=str(state / "tmp"), TEMP=str(state / "tmp"))
        (state / "tmp").mkdir(exist_ok=True, mode=0o700)
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            child = subprocess.Popen(manifest["command"], cwd=manifest["workdir"], env=env,
                                     stdin=subprocess.DEVNULL, stdout=fd, stderr=fd, start_new_session=True)
        finally:
            os.close(fd)
        self.children[key] = child
        identity = process_identity(child.pid)
        if identity is None:
            # The child may exit immediately. Keep a retry deadline even then.
            failures, delay = self._backoff(status)
            say(f"service {key[:8]} pid={child.pid} exited at once (failure {failures}); "
                f"starting again in {delay:.0f}s")
            save(path, {"status": "backoff", "generation": manifest["generation"], "failures": failures,
                        "retry_at": now + delay})
            return
        say(f"service {key[:8]} started pid={child.pid} generation={manifest['generation']} "
            f"release={release_of(manifest['command'])}")
        save(path, {"status": "running", "process": identity, "generation": manifest["generation"],
                    "started_at": now, "checked_at": now, "failures": status.get("failures", 0)})

    def ensure_memguard(self):
        """The memory watchdog runs on its own from a local copy once enabled (service/memguard.py); started again
        here when it is not running, so a container restart or a crash leaves it off for at most a minute."""
        try:
            outcome = memguard.ensure()
        except Exception as exc:  # noqa: BLE001 - never the services' problem
            outcome = f"check failed: {type(exc).__name__}"
        if outcome not in {"running", "disabled", "not_installed", "waiting"}:
            say(f"memory watchdog: {outcome}")

    def record(self):
        """Write manager.json (who the Manager is, for deploys and the status command); False when it cannot
        be written yet, and the next pass tries again."""
        try:
            save(self.root / "manager.json", {"process": process_identity(os.getpid()), "status": "running"})
        except OSError as exc:
            if self.reported.get('manager.json') != str(exc):
                say(f"manager.json not written: {exc}")
            self.reported['manager.json'] = str(exc)
            return False
        self.reported.pop('manager.json', None)
        return True

    def run(self):
        lock = instance_lock("manager-" + service_key(self.root))
        if lock is None:
            return 0
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        recorded = self.record()
        signal.signal(signal.SIGTERM, lambda *_: self.stop.set())
        signal.signal(signal.SIGINT, lambda *_: self.stop.set())
        threading.Thread(target=self.health_server, args=(lock,), daemon=True).start()
        say(f"manager started pid={os.getpid()} release={Path(__file__).resolve().parents[2].name}")
        try:
            while not self.stop.is_set():
                required_mount = cenv.get('COVEY_REQUIRED_MOUNT')
                if required_mount:
                    from covey.service.boot import mounted
                    if not mounted(str(self.root), required_mount):
                        self.last_tick = time.monotonic()
                        self.stop.wait(self.interval)
                        continue
                try:
                    recorded = recorded or self.record()
                    self.reconcile_all()
                    self.ensure_memguard()
                    rotate_live(self.root / 'manager.log', LOG_BYTES, LOG_KEEP)
                except OSError as exc:
                    # Storage that takes no writes or reads for a while (a full quota, a stalled mount) is no
                    # reason to stop supervising: the services go on and the next pass tries again.
                    if self.reported.get('') != str(exc):
                        say(f"manager pass failed: {exc}")
                    self.reported[''] = str(exc)
                else:
                    self.reported.pop('', None)
                self.last_tick = time.monotonic()
                self.stop.wait(self.interval)
        finally:
            # Services outlive a manager update and are adopted by its successor.
            say(f"manager stopping pid={os.getpid()}; its services go on")
            lock.close()
        return 0


def ensure_manager(root):
    probe = instance_lock("manager-" + service_key(root))
    if probe is None:
        return
    probe.close()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(root / "manager.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        subprocess.Popen([sys.executable, "-B", str(entry_script("covey_service.py")), "--root", str(root), "run"],
                         stdin=subprocess.DEVNULL, stdout=fd, stderr=fd, start_new_session=True)
    finally:
        os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="covey_service.py", description=__doc__)
    parser.add_argument("--root", type=Path, default=service_root())
    sub = parser.add_subparsers(dest="action", required=True)
    launch = sub.add_parser("launch", help="persist a service and start its manager")
    launch.add_argument("--state-dir", type=Path, required=True)
    launch.add_argument("--workdir", type=Path, required=True)
    launch.add_argument("--profile", default="")
    launch.add_argument("--log", type=Path)
    launch.add_argument("--health-timeout", type=float, default=120,
                        help="restart a bridge with no local health response after this many seconds; 0 disables")
    launch.add_argument("command", nargs=argparse.REMAINDER)
    for action in ("stop", "stop-if-registered", "start", "restart", "status"):
        p = sub.add_parser(action)
        p.add_argument("--state-dir", type=Path, required=True)
    run = sub.add_parser("run")
    run.add_argument("--interval", type=float, default=2, help=argparse.SUPPRESS)  # seconds between reconciles
    args = parser.parse_args(argv)
    root = args.root.expanduser().resolve()
    if args.action == "run":
        log_to(root / "manager.log")
        return Manager(root, interval=args.interval).run()
    if args.action == "launch":
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        paths = runtime_environment()
        register(root, args.state_dir, command, workdir=args.workdir, profile=args.profile, log=args.log,
                 environment=paths, health_timeout=args.health_timeout)
        ensure_manager(root)
    elif args.action == "status":
        # Only here: the Manager itself never loads the job records, so an import error there cannot stop it.
        from covey.core import run_dir
        key = service_key(args.state_dir)
        manifest = read_json(manifest_path(root, key), None)
        status = read_json(status_path(root, key), {})
        state = args.state_dir.resolve()
        print(json.dumps({"registered": manifest is not None, "desired_state": (manifest or {}).get("desired_state"),
                          "state_dir": str(state), "status": status,
                          "alive": is_owned(status.get("process")),
                          # Jobs a restart would interrupt now; tasks in their own runners are not among them.
                          "restart_blockers": run_dir.restart_blockers(state, run_dir.live_keys(state))}, ensure_ascii=False))
    else:
        if args.action == 'stop-if-registered':
            if not manifest_path(root, service_key(args.state_dir)).exists():
                return 0
            args.action = 'stop'
        set_desired(root, args.state_dir, "stopped" if args.action == "stop" else "running", restart=args.action == "restart")
        ensure_manager(root)
        if args.action == "stop":
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                status = read_json(status_path(root, service_key(args.state_dir)), {})
                if status.get("status") == "stopped":
                    break
                time.sleep(0.2)
            else:
                raise RuntimeError("service stop not confirmed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from None
