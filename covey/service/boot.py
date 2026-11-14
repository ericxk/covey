#!/usr/bin/env python3
"""Small standalone boot watchdog; may run as a strictly gated PID-1 thread.

Only code/config are installed locally. Runtime data and manager logs stay on
the configured mount. No imports from the NAS are needed while waiting for it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import struct
import subprocess
import sys
import threading
import time


def eligible(config, pid, argv, env):
    return (pid == 1 and bool(argv) and argv[0] == config.get("pid1_entrypoint")
            and env.get("MERLIN_JOB_ID") == config.get("devbox_id") # privacy: allow MERLIN_JOB_ID 是托管平台自带的环境变量，按它认开发机
            and bool(config.get("devbox_id")) and config.get("enabled") is True)


def mounted(root, mountpoint):
    # mountinfo is local kernel state, so an unavailable hard NFS cannot block
    # this check. An actual stalled NAS is detected by manager control lag.
    if not Path(root).is_relative_to(mountpoint):
        return False
    try:
        for line in Path("/proc/self/mountinfo").read_text().splitlines():
            parts = line.split()
            if parts[4].replace("\\040", " ") == mountpoint:
                return True
    except (OSError, IndexError):
        pass
    return False


def manager_log(root):
    """The Manager's log on the mount, opened for appending (the Manager rotates it); /dev/null
    when it cannot be opened, so a log problem never keeps the Manager from starting."""
    try:
        Path(root).mkdir(parents=True, exist_ok=True, mode=0o700)
        return os.open(Path(root) / 'manager.log', os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    except OSError:
        return subprocess.DEVNULL


def process_matches(identity):
    if not identity:
        return False
    try:
        proc = Path('/proc') / str(identity['pid'])
        fields = (proc / 'stat').read_text().rsplit(')', 1)[1].split()
        return (proc.stat().st_uid == os.getuid() and fields[0] != 'Z'
                and fields[19] == identity['start_ticks']
                and Path('/proc/sys/kernel/random/boot_id').read_text().strip() == identity['boot_id'])
    except (OSError, KeyError, IndexError):
        return False


def _ask(address):
    """What the health server (``serve``) at ``address`` answers, read to its end as core/process.py's query
    reads; None without a JSON answer within 2 s."""
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(2)
        try:
            sock.connect(address)
            chunks, size = [], 0
            while size < 65536:
                part = sock.recv(4096)
                if not part:
                    return json.loads(b''.join(chunks))
                chunks.append(part)
                size += len(part)
        except (OSError, ValueError):
            pass
    return None


def manager_health(root):
    key = hashlib.sha256(root.encode()).hexdigest()[:24]
    return _ask('\0lark-bridge-' + str(os.getuid()) + '-manager-' + key)


def boot_address(config_path):
    name = hashlib.sha256(str(config_path).encode()).hexdigest()[:24]
    return '\0lark-bridge-boot-' + str(os.getuid()) + '-' + name


def boot_health(config_path):
    return _ask(boot_address(config_path))


def init_identity():
    try:
        return {'start_ticks': Path('/proc/1/stat').read_text().rsplit(')', 1)[1].split()[19],
                'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip()}
    except (OSError, IndexError):
        return None


def serve(sock, snapshot, stop):
    """Answer each connection on a listening health socket with ``snapshot()`` as JSON until ``stop``,
    to this user's processes only. The Bridge's and the Manager's health servers use it too."""
    sock.listen(4)
    sock.settimeout(.5)
    while not stop.is_set():
        try:
            conn, _ = sock.accept()
        except socket.timeout:
            continue
        except OSError:
            return
        with conn:
            conn.settimeout(1)
            try:
                _, uid, _ = struct.unpack('3i', conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                if uid == os.getuid():
                    conn.sendall(json.dumps(snapshot()).encode())
            except (OSError, ValueError):
                pass


class _ManagerWatch:
    """What the boot watchdog remembers between ticks about the Manager it keeps alive."""

    def __init__(self):
        self.child = None
        self.failures = 0
        self.spawned_at = 0
        self.stalled = {}
        self.last_identity, self.last_response = None, time.monotonic()

    def check(self, cfg, status):
        """One look at the Manager: note a healthy one, signal a stalled or silent one, start a missing one."""
        health = manager_health(cfg['root'])
        if health:
            self._healthy(cfg, health, status)
        elif (self.last_identity and process_matches(self.last_identity)
              and time.monotonic() - self.last_response > cfg.get('watchdog_seconds', 120)):
            # Covers a frozen process or failed health thread. Signal
            # only the exact manager observed through its local socket.
            status['state'] = 'manager_unresponsive'
            if self.last_identity['pid'] != 1:
                self._signal(self.last_identity['pid'])
        elif self.child is None or self.child.poll() is not None:
            self._start(cfg, status)
        elif time.monotonic() - self.spawned_at > cfg.get('watchdog_seconds', 120):
            self.child.kill()  # Still our unreaped child, not a stale PID file.

    def _healthy(self, cfg, health, status):
        status['state'], status['error'] = 'watching', None
        identity = health.get('process')
        if process_matches(identity):
            self.last_identity, self.last_response = identity, time.monotonic()
        if health.get('lag', 0) > cfg.get('watchdog_seconds', 120) and process_matches(identity):
            status['state'] = 'manager_stalled'
            # Only the manager is restarted; verified children are
            # adopted afterwards. Never signal the platform PID 1.
            if identity['pid'] != 1:
                self._signal(identity['pid'])
        else:
            self.stalled.clear()
        self.failures = 0

    def _signal(self, pid):
        """SIGTERM a stalled Manager, SIGKILL it once it has been stalled for 20 s."""
        since = self.stalled.setdefault(pid, time.monotonic())
        os.kill(pid, signal.SIGKILL if time.monotonic() - since > 20 else signal.SIGTERM)

    def _start(self, cfg, status):
        status['state'] = 'starting_manager'
        # A binding with no health response can be an existing manager
        # starting or blocked. Its singleton prevents duplicate owners.
        log = manager_log(cfg['root'])
        try:
            self.child = subprocess.Popen(cfg['command'], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                          start_new_session=True,
                                          env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1',
                                               'COVEY_REQUIRED_MOUNT': cfg['mountpoint'],
                                               'LARK_BRIDGE_REQUIRED_MOUNT': cfg['mountpoint']})
        finally:
            if log != subprocess.DEVNULL:
                os.close(log)
        self.spawned_at = time.monotonic()
        status['recoveries'] += 1
        self.failures += 1


def watch(config_path, *, stop=None, interval=10):
    stop = stop or threading.Event()
    singleton = socket.socket(socket.AF_UNIX)
    try:
        singleton.bind(boot_address(config_path))
    except OSError:
        singleton.close()
        return
    watchdog = _ManagerWatch()
    status = {'pid': os.getpid(), 'mode': 'pid1' if os.getpid() == 1 else 'standalone',
              'init_identity': init_identity(), 'last_tick': time.monotonic(),
              'state': 'starting', 'recoveries': 0, 'error': None}
    health_stop = threading.Event()
    threading.Thread(target=serve, args=(singleton, lambda: {**status, 'lag': time.monotonic() - status['last_tick']},
                                          health_stop), daemon=True).start()
    try:
        while not stop.is_set():
            try:
                status['last_tick'] = time.monotonic()
                cfg = json.loads(Path(config_path).read_text())
                if not cfg.get('enabled'):
                    return
                current_job = hosting_job()
                if current_job != cfg['devbox_id']:
                    return
                if not mounted(cfg['root'], cfg['mountpoint']):
                    status['state'] = 'waiting_mount'
                    stop.wait(interval)
                    continue
                watchdog.check(cfg, status)
            except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
                status['state'], status['error'] = 'retrying', type(exc).__name__
                watchdog.failures += 1
            stop.wait(min(60, interval * 2 ** min(max(0, watchdog.failures - 1), 4)))
    finally:
        health_stop.set()
        singleton.close()


def hosting_job():
    """The hosting platform job id of the host this watchdog runs on, read from PID 1's environment. In PID 1 itself,
    self is authoritative even inside a PID namespace whose /proc mount belongs to its parent namespace.
    Raises OSError when PID 1's environment cannot be read (another user's init): the host is then unknown."""
    env_path = '/proc/self/environ' if os.getpid() == 1 else '/proc/1/environ'
    raw = Path(env_path).read_bytes().split(b'\0')
    return next((x.split(b'=', 1)[1].decode() for x in raw if x.startswith(b'MERLIN_JOB_ID=')), '')  # privacy: allow 同上，读 PID 1 的环境


def supervise(config_path, *, stop=None, interval=10):
    """Keep the watchdog loop alive even after an unexpected Python exception."""
    stop = stop or threading.Event()
    while not stop.is_set():
        try:
            watch(config_path, stop=stop, interval=interval)
            return  # disabled, wrong host or an existing singleton owner
        except Exception:
            # Keep this independent of NAS, application imports and logging.
            stop.wait(interval)


def pid1_start(config_path=None):
    config_path = config_path or str(Path.home() / '.config/lark-bridge-service/boot.json')
    try:
        config = json.loads(Path(config_path).read_text())
        if eligible(config, os.getpid(), sys.argv, os.environ):
            # The supervisor's outer watchdog lives in the platform PID 1.
            # It is not a second ordinary daemon that can disappear unnoticed.
            threading.Thread(target=supervise, args=(config_path,), daemon=True,
                             name='lark-bridge-service-watchdog').start()
    except (OSError, ValueError):
        pass  # Never interfere with platform startup.


def main():
    # install.py --activate runs the installed hook file this way until the next boot.
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config', type=Path)
    args = parser.parse_args()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    supervise(args.config, stop=stop)


if __name__ == '__main__':
    main()
