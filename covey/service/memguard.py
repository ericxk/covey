#!/usr/bin/env python3
"""Memory watchdog: stop runaway search tools before they exhaust the machine (docs/operations.md 内存看门狗).

Standalone and standard library only: ``enable`` copies this file to the local disk (INSTALLED) and runs it from
there, so a hung NAS never stops it; the Manager starts it again when it is not running (service/manager.py).

The worker scans /proc every few seconds and applies these rules (thresholds come from the config; by default
they scale with the container's memory limit):

  search_rss   search tool (rg, grep, find, ...) above the kill threshold: capture evidence, then SIGKILL
  orphan       search tool adopted by PID 1 whose stdout pipe has no reader: capture, then SIGKILL
  big_process  any other process crossing an alert level: capture and alert, never signalled
  container    container anonymous memory above container_alert_ratio: alert with the top processes
  long_search  search tool running longer than search_long_run_seconds: alert, never signalled

The two rules that kill only capture and alert unless ``kill_enabled`` (off by default, ``enable --kill on``).
With ``rg_guard`` on it keeps an address-space wrapper in front of Codex's vendored rg and puts it back after a
Codex reinstall restores the plain binary. Alerts are files in <data_dir>/outbox/ that the Bridge sends: to the
topic of the task whose marker (COVEY_TASK_DIR, core/task_processes.py) the process carries, otherwise to
the owner's private chat (app/memguard_alerts.py). Of a process's environment only that marker is read.

Subcommands: status | enable | disable | uninstall | daemon | run | rg-guard {status,install,uninstall}
"""
import argparse
import collections
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import resource
import select
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time

OWNER = 'lark-bridge-memguard'
HOME = Path.home()
INSTALLED = HOME / '.local/libexec/lark-bridge/memguard.py'
CONFIG = HOME / '.config/lark-bridge-service/memguard.json'
GUIDE = 'harness-bridge docs/operations.md 内存看门狗'
TASK_DIR_ENV = 'COVEY_TASK_DIR'  # core/task_processes.py; this file imports nothing of the package.
TASK_DIR_ENVS = (TASK_DIR_ENV, 'LARK_BRIDGE_TASK_DIR')  # The second is what a task runner of an older release sets.
WRAPPER_MARK = 'memory-guard rg wrapper'

MIB = 1 << 20
GIB = 1 << 30
PAGE = os.sysconf('SC_PAGE_SIZE')
HZ = os.sysconf('SC_CLK_TCK')

SEARCH_NAMES = ['rg', 'grep', 'egrep', 'fgrep', 'zgrep', 'find', 'fd', 'fdfind', 'ag', 'ack',
                'ugrep', 'tree', 'du', 'locate']
FIND_ACTIONS = {'-exec', '-execdir', '-ok', '-okdir', '-delete', '-fprint', '-fprint0', '-fprintf', '-fls'}
STATUS_KEYS = {'State', 'PPid', 'Threads', 'VmPeak', 'VmSize', 'VmHWM', 'VmRSS', 'RssAnon', 'RssFile',
               'RssShmem', 'VmSwap', 'voluntary_ctxt_switches', 'nonvoluntary_ctxt_switches'}
CAPTURE_RE = re.compile(r'^\d{8}T\d{6}Z-[a-z_]+-.+\.json$')

DEFAULTS = {
    'owner': OWNER,
    'enabled': True,
    'data_dir': None,
    'data_mount': None,
    'interval_seconds': 2.0,
    'search_names': SEARCH_NAMES,
    'find_names': ['find'],
    'only_comms': None,
    'kill_enabled': False,
    # Thresholds in MiB; None: these shares of the container's memory limit, or the fixed values without a limit.
    'search_kill_mib': None,
    'search_kill_ratio': 0.035,
    'alert_levels_mib': None,
    'alert_ratios': [0.14, 0.28, 0.56],
    'orphan_min_age_seconds': 120,
    'orphan_reaper_pids': [1],
    'search_long_run_seconds': 7200,
    'container_alert_ratio': 0.8,
    'container_rearm_ratio': 0.7,
    'capture_max_files': 300,
    'capture_max_mib': 256,
    'outbox_max_files': 200,
    'self_as_mib': 1024,
    'rg_guard': {'enabled': False, 'target': None, 'as_mib': 4096, 'check_seconds': 60,
                 'settle_seconds': 120, 'max_reinstalls_per_hour': 3},
}
FIXED_KILL_MIB, FIXED_LEVELS_MIB = 8192, [32768, 65536, 131072]


def thresholds(cfg, limit):
    """(search kill MiB, alert levels MiB): the config's, else its ratios of ``limit`` bytes, else the fixed ones."""
    known = bool(limit) and limit < 1 << 60
    kill = cfg['search_kill_mib'] or (max(1024, int(limit * cfg['search_kill_ratio']) // MIB) if known
                                      else FIXED_KILL_MIB)
    levels = cfg['alert_levels_mib'] or ([int(limit * r) // MIB for r in cfg['alert_ratios']] if known
                                         else FIXED_LEVELS_MIB)
    return kill, sorted(levels)


# ---------- /proc and cgroup helpers ----------

def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='milliseconds')


def read_bytes(path, limit=65536):
    try:
        with open(path, 'rb') as f:
            return f.read(limit)
    except OSError:
        return None


def read_text(path, limit=65536):
    raw = read_bytes(path, limit)
    return None if raw is None else raw.decode('utf-8', 'replace')


def readlink(path):
    try:
        return os.readlink(path)
    except OSError:
        return None


def int_or(text):
    try:
        return int((text or '').strip())
    except ValueError:
        return None


def parse_kv(text):
    out = {}
    for line in (text or '').splitlines():
        parts = line.split()
        if len(parts) >= 2:
            key = parts[0].rstrip(':')
            try:
                out[key] = int(parts[1])
            except ValueError:
                out[key] = parts[1]
    return out


def parse_fdinfo(text):
    info = {}
    for line in (text or '').splitlines():
        key, _, value = line.partition(':')
        try:
            if key == 'flags':
                info['flags'] = int(value.strip(), 8)
            elif key in ('pos', 'mnt_id', 'ino'):
                info[key] = int(value.strip())
        except ValueError:
            pass
    return info


def parse_stat(path, pid):
    raw = read_text(path, 4096)
    if not raw:
        return None
    try:
        comm = raw[raw.index('(') + 1:raw.rindex(')')]
        f = raw[raw.rindex(')') + 2:].split()
        return {'pid': pid, 'comm': comm, 'state': f[0], 'ppid': int(f[1]), 'pgrp': int(f[2]),
                'utime': int(f[11]), 'stime': int(f[12]), 'threads': int(f[17]),
                'starttime': int(f[19]), 'rss': int(f[21]) * PAGE}
    except (ValueError, IndexError):
        return None


def proc_stat(pid):
    return parse_stat(f'/proc/{pid}/stat', pid)


def uptime():
    return float((read_text('/proc/uptime', 128) or '0').split()[0])


def read_cgroup():
    v1 = '/sys/fs/cgroup/memory'
    if os.path.exists(v1 + '/memory.usage_in_bytes'):
        stat = parse_kv(read_text(v1 + '/memory.stat'))
        oom = parse_kv(read_text(v1 + '/memory.oom_control'))
        return {'usage': int_or(read_text(v1 + '/memory.usage_in_bytes')),
                'limit': int_or(read_text(v1 + '/memory.limit_in_bytes')),
                'anon': stat.get('total_rss', stat.get('rss')),
                'cache': stat.get('total_cache', stat.get('cache')),
                'failcnt': int_or(read_text(v1 + '/memory.failcnt')), 'oom_kill': oom.get('oom_kill')}
    v2 = '/sys/fs/cgroup'
    stat = parse_kv(read_text(v2 + '/memory.stat'))
    events = parse_kv(read_text(v2 + '/memory.events'))
    return {'usage': int_or(read_text(v2 + '/memory.current')), 'limit': int_or(read_text(v2 + '/memory.max')),
            'anon': stat.get('anon'), 'cache': stat.get('file'), 'oom_kill': events.get('oom_kill')}


def cg_ratio(cg):
    limit, anon = cg.get('limit'), cg.get('anon')
    if not limit or anon is None or limit >= 1 << 60:
        return None
    return anon / limit


def gib(n):
    return f'{(n or 0) / GIB:.1f} GiB'


def mount_of(path):
    """(mount point, file system type) holding ``path``, from mountinfo only: never stat a hard NFS mount that may
    be hung. None for a relative path or unreadable mountinfo."""
    path = Path(path)
    if not path.is_absolute():
        return None
    best = None
    try:
        lines = Path('/proc/self/mountinfo').read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        left, _, right = line.partition(' - ')
        fields = left.split()
        if len(fields) < 5:
            continue
        point = Path(fields[4].replace('\\040', ' '))
        if path.is_relative_to(point) and (best is None or len(str(point)) > len(str(best[0]))):
            best = (point, right.split()[0] if right else '')
    return None if best is None else [str(best[0]), best[1]]


# Command lines are kept for evidence but obvious credentials are masked first; environ is never read.
SECRET_KV = re.compile(r'(?i)([\w.-]*(?:token|secret|passw(?:or)?d|api[_-]?key|access[_-]?key|auth|cookie|'
                       r'credential)[\w.-]*\s*[=:])(.+)')
SECRET_FLAG = re.compile(r'(?i)^--?[\w-]*(?:token|secret|password|passwd|api-?key|access-?key|cookie|credential)')
LONG = re.compile(r'[A-Za-z0-9_\-]{40,}')


def redact_argv(argv):
    out, prev = [], ''
    for arg in argv:
        if SECRET_FLAG.search(prev) and '=' not in prev:
            out.append('<redacted>')
        else:
            out.append(LONG.sub('<long>', SECRET_KV.sub(lambda m: m.group(1) + '<redacted>', arg)))
        prev = arg
    return out


def short_cmd(argv, limit=300):
    if not argv:
        return '?'
    text = ' '.join(shlex.quote(a) for a in argv)
    return text if len(text) <= limit else text[:limit] + '…'


class Reader:
    """Reads files that take the target's mmap lock (cmdline) in a helper thread with a deadline."""

    def __init__(self, max_inflight=4):
        self.lock = threading.Lock()
        self.inflight = 0
        self.max_inflight = max_inflight

    def read(self, path, limit=8192, timeout=1.5):
        with self.lock:
            if self.inflight >= self.max_inflight:
                return None
            self.inflight += 1
        box = {}

        def work():
            try:
                box['value'] = read_bytes(path, limit)
            finally:
                with self.lock:
                    self.inflight -= 1

        t = threading.Thread(target=work, name='reader', daemon=True)
        t.start()
        t.join(timeout)
        return box.get('value')


# ---------- output: evidence and alerts on the data dir ----------

class Writer(threading.Thread):
    """Writes evidence to the NAS data dir off the kill path; drops items while NAS is not mounted."""

    def __init__(self, data_dir, max_files, max_mib, mount=None, outbox_max=200):
        super().__init__(name='writer', daemon=True)
        self.dir = Path(data_dir) if data_dir else None
        self.mount = mount  # As at enable: a data dir whose mount went away is not written (no local fallback).
        self.q = queue.Queue(maxsize=256)
        self.max_files, self.max_bytes, self.outbox_max = max_files, max_mib * MIB, outbox_max
        self.stats = {'written': 0, 'dropped': 0, 'errors': 0, 'last_error': None, 'alerts': 0}

    def put(self, kind, name, payload):
        try:
            self.q.put_nowait((kind, name, payload))
        except queue.Full:
            self.stats['dropped'] += 1

    def stop(self, timeout=5):
        try:
            self.q.put(None, timeout=timeout)
        except queue.Full:
            return
        self.join(timeout)

    def run(self):
        while True:
            item = self.q.get()
            if item is None:
                return
            try:
                self.stats['written' if self.write(*item) else 'dropped'] += 1
            except Exception as e:
                self.stats['errors'] += 1
                self.stats['last_error'] = f'{type(e).__name__}: {e}'[:300]

    def write(self, kind, name, payload):
        if self.dir is None or (self.mount is not None and mount_of(self.dir) != self.mount):
            return False
        if kind == 'capture':
            d = self.dir / 'captures'
            d.mkdir(parents=True, exist_ok=True)
            fd = os.open(d / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as f:
                json.dump(payload, f, ensure_ascii=False)
            self.prune(d)
        elif kind == 'line':
            p = self.dir / name
            if p.exists() and p.stat().st_size > 16 * MIB:
                os.replace(p, p.with_suffix('.1.jsonl'))
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, 'a') as f:
                f.write(json.dumps(payload, ensure_ascii=False) + '\n')
        elif kind == 'alert':
            # One file each, for the Bridge to send and delete (app/memguard_alerts.py); written whole, then renamed.
            d = self.dir / 'outbox'
            d.mkdir(parents=True, exist_ok=True)
            if len([e for e in os.scandir(d) if e.name.endswith('.json')]) >= self.outbox_max:
                return False  # No Bridge is taking them; alerts.jsonl still has every one.
            tmp = d / f'.{name}.tmp'
            tmp.write_text(json.dumps(payload, ensure_ascii=False))
            os.replace(tmp, d / name)
            self.stats['alerts'] += 1
        elif kind == 'status':
            tmp = self.dir / f'.status.{os.getpid()}.tmp'
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1))
            os.replace(tmp, self.dir / 'status.json')
        return True

    def prune(self, d):
        files = sorted((e for e in os.scandir(d) if CAPTURE_RE.match(e.name)), key=lambda e: e.name)
        sizes = [e.stat().st_size for e in files]
        total = sum(sizes)
        while files and (len(files) > self.max_files or total > self.max_bytes):
            os.unlink(files.pop(0).path)
            total -= sizes.pop(0)


# ---------- rg wrapper ----------

def wrapper_text(real, as_mib):
    return f'''#!/bin/sh
# {WRAPPER_MARK}: caps rg's address space so a runaway walk aborts within seconds instead of
# exhausting the container. Details: {GUIDE}
# RG_GUARD_AS_MIB overrides the cap in MiB (0 disables it); RG_GUARD_TIMEOUT adds a wall-clock limit.
real={shlex.quote(str(real))}
if [ ! -x "$real" ]; then
    echo "rg: memory-guard wrapper cannot find $real; reinstall Codex or run bin/covey_memguard.py rg-guard install" >&2
    exit 127
fi
as_mib=${{RG_GUARD_AS_MIB:-{as_mib}}}
case $as_mib in
    ''|*[!0-9]*) as_mib={as_mib} ;;
esac
[ "$as_mib" -gt 0 ] && ulimit -v $((as_mib * 1024)) 2>/dev/null
# core_pattern pipes to a host handler, which ignores a zero core limit; one byte makes the kernel skip the dump.
pre=
if [ -x /usr/bin/prlimit ]; then
    pre="/usr/bin/prlimit --core=1 --"
else
    ulimit -c 0 2>/dev/null
fi
case ${{RG_GUARD_TIMEOUT:-0}} in
    ''|0|*[!0-9]*) exec $pre "$real" "$@" ;;
esac
exec $pre /usr/bin/timeout --foreground --kill-after=10 "$RG_GUARD_TIMEOUT" "$real" "$@"
'''


def rg_state(target):
    head = read_bytes(target, 4096)
    if head is None:
        return 'missing'
    if head.startswith(b'\x7fELF'):
        return 'elf'
    if WRAPPER_MARK.encode() in head:
        return 'wrapped' if os.access(Path(target).parent / '.real' / 'rg', os.X_OK) else 'wrapped_missing_real'
    return 'unknown'


def runs_ok(cmd):
    env = {k: v for k, v in os.environ.items() if not k.startswith('RG_GUARD_')}
    try:
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=15, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0 and b'ripgrep' in r.stdout


def settled(target, seconds):
    """True once a reinstall has stopped touching the binary and its directory."""
    now = time.time()
    try:
        return all(now - os.stat(p).st_ctime >= seconds for p in (target, Path(target).parent))
    except OSError:
        return False


def write_wrapper(target, real, as_mib):
    wtmp = target.parent / f'.rg.memguard.{os.getpid()}.tmp'
    wtmp.write_text(wrapper_text(real, as_mib))
    os.chmod(wtmp, 0o755)
    os.replace(wtmp, target)


def rg_install(target, as_mib):
    """Wrap a plain rg, or refresh an existing wrapper whose text is out of date."""
    target = Path(target)
    state = rg_state(target)
    real = target.parent / '.real' / 'rg'
    if state == 'wrapped':
        if target.read_text() == wrapper_text(real, as_mib):
            return {'result': 'already_wrapped'}
        old = target.read_bytes()
        write_wrapper(target, real, as_mib)
        if runs_ok([str(target), '--version']):
            return {'result': 'updated', 'real': str(real), 'as_mib': as_mib}
        wtmp = target.parent / f'.rg.memguard.{os.getpid()}.tmp'
        wtmp.write_bytes(old)
        os.chmod(wtmp, 0o755)
        os.replace(wtmp, target)
        return {'result': 'update_rolled_back'}
    if state != 'elf':
        return {'result': f'skipped_{state}'}
    real.parent.mkdir(exist_ok=True)
    os.chmod(real.parent, 0o755)
    tmp = real.parent / f'.rg.{os.getpid()}.tmp'
    if os.path.lexists(tmp):
        tmp.unlink()
    os.link(target, tmp)
    os.replace(tmp, real)
    if not runs_ok([str(real), '--version']):
        return {'result': 'real_binary_check_failed', 'real': str(real)}
    write_wrapper(target, real, as_mib)
    if runs_ok([str(target), '--version']):
        return {'result': 'installed', 'real': str(real), 'as_mib': as_mib}
    rtmp = target.parent / f'.rg.restore.{os.getpid()}.tmp'
    os.link(real, rtmp)
    os.replace(rtmp, target)
    return {'result': 'rolled_back', 'reason': 'wrapper --version failed'}


def rg_uninstall(target):
    target = Path(target)
    state = rg_state(target)
    real = target.parent / '.real' / 'rg'
    if state != 'wrapped':
        return {'result': f'not_wrapped_{state}'}
    rtmp = target.parent / f'.rg.restore.{os.getpid()}.tmp'
    os.link(real, rtmp)
    os.replace(rtmp, target)
    real.unlink()
    try:
        real.parent.rmdir()
    except OSError:
        pass
    return {'result': 'restored', 'state': rg_state(target)}


# ---------- worker ----------

class Guard:
    def __init__(self, cfg):
        self.cfg = cfg
        self.search = set(cfg['search_names'])
        self.find_names = set(cfg['find_names'])
        # Tests restrict the per-process rules to their own throwaway command names.
        self.only = set(cfg['only_comms']) if cfg.get('only_comms') is not None else None
        self.kill_mib, levels = thresholds(cfg, read_cgroup().get('limit'))
        self.kill_bytes = self.kill_mib * MIB
        self.levels = [level * MIB for level in levels]
        self.protected = {1, os.getpid(), os.getppid()}
        self.reapers = set(cfg['orphan_reaper_pids'])
        self.reader = Reader()
        self.writer = Writer(cfg['data_dir'], cfg['capture_max_files'], cfg['capture_max_mib'], cfg.get('data_mount'),
                             cfg['outbox_max_files'])
        self.writer.start()
        self.seen = {}
        self.seq = 0
        self.container_armed = True
        self.events = collections.deque(maxlen=20)
        self.counters = collections.Counter()
        self.scans = 0
        self.last_scan = {}
        self.errors = 0
        self.last_error = None
        self.rg = {'state': None, 'checked': -1e9, 'reinstalls': collections.deque(), 'broken_alerted': False}
        self.last_status_write = -1e9

    # --- evidence ---

    def cmdline(self, pid):
        raw = self.reader.read(f'/proc/{pid}/cmdline')
        if raw is None:
            return None
        return redact_argv([a.decode('utf-8', 'replace') for a in raw.split(b'\0') if a])

    def threads(self, pid, limit=64):
        try:
            tids = sorted(int(t) for t in os.listdir(f'/proc/{pid}/task'))
        except OSError:
            return []
        out = []
        for tid in tids[:limit]:
            d = f'/proc/{pid}/task/{tid}'
            st = parse_stat(d + '/stat', tid) or {}
            out.append({'tid': tid, 'comm': st.get('comm'), 'state': st.get('state'),
                        'wchan': read_text(d + '/wchan', 128),
                        'syscall': (read_text(d + '/syscall', 256) or '').strip()})
        return out

    def fds(self, pid, limit=256):
        try:
            names = sorted(os.listdir(f'/proc/{pid}/fd'), key=int)
        except (OSError, ValueError):
            return {'total': None, 'items': []}
        items = []
        for n in names[:limit]:
            info = parse_fdinfo(read_text(f'/proc/{pid}/fdinfo/{n}', 1024))
            item = {'fd': int(n), 'target': readlink(f'/proc/{pid}/fd/{n}'), 'pos': info.get('pos')}
            if info.get('flags', 0) & os.O_DIRECTORY:
                item['dir'] = True
            items.append(item)
        return {'total': len(names), 'items': items}

    def ancestors(self, ppid, depth=12):
        out = []
        pid = ppid
        while pid > 1 and len(out) < depth:
            st = proc_stat(pid)
            if not st:
                break
            argv = self.cmdline(pid)
            out.append({'pid': pid, 'comm': st['comm'], 'exe': readlink(f'/proc/{pid}/exe'),
                        'cmd': short_cmd(argv, 300) if argv is not None else None,
                        'run_dir': run_dir_of(argv)})
            pid = st['ppid']
        return out

    def capture(self, st, rule, detail, resample):
        pid = st['pid']
        d = f'/proc/{pid}'
        t0 = time.monotonic()
        status = read_text(d + '/status', 8192) or ''
        argv = self.cmdline(pid)
        cap = {'schema': 1, 'time': now_iso(), 'host': socket.gethostname(), 'rule': rule, 'detail': detail,
               'process': st, 'exe': readlink(d + '/exe'), 'cwd': readlink(d + '/cwd'),
               'argv': argv, 'cmd': short_cmd(argv, 2000) if argv is not None else None,
               'status': {k: v.strip() for k, _, v in (line.partition(':') for line in status.splitlines())
                          if k in STATUS_KEYS},
               'io': parse_kv(read_text(d + '/io', 2048)), 'wchan': read_text(d + '/wchan', 128),
               'oom_score': int_or(read_text(d + '/oom_score', 64)),
               'threads': self.threads(pid), 'fds': self.fds(pid), 'task_dir': self.task_dir(pid)}
        dirs = [item for item in cap['fds']['items'] if item.get('dir')]
        if resample and dirs:
            # Directory read positions one second apart show whether a walk is advancing or looping.
            time.sleep(1.0)
            for item in dirs:
                item['pos_after_1s'] = parse_fdinfo(read_text(f'{d}/fdinfo/{item["fd"]}', 1024)).get('pos')
            cap['io_after_1s'] = parse_kv(read_text(d + '/io', 2048))
            cap['rss_after_1s'] = (proc_stat(pid) or {}).get('rss')
        cap['ancestors'] = self.ancestors(st['ppid'])
        cap['container'] = read_cgroup()
        cap['pressure'] = {k: (read_text(f'/proc/pressure/{k}', 512) or '').strip() for k in ('memory', 'io', 'cpu')}
        cap['capture_seconds'] = round(time.monotonic() - t0, 3)
        return cap

    def save_capture(self, cap, label):
        self.seq += 1
        ts = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        name = f'{ts}-{cap["rule"]}-{re.sub(r"[^A-Za-z0-9._-]", "_", label)[:32]}-{self.seq}.json'
        self.writer.put('capture', name, cap)
        return str(Path(self.cfg['data_dir'] or '.') / 'captures' / name)

    def record(self, kind, rule, st, extra):
        ev = {'time': now_iso(), 'kind': kind, 'rule': rule}
        if st:
            ev.update({'pid': st['pid'], 'comm': st['comm'], 'rss_mib': st['rss'] // MIB, 'age_s': st.get('age')})
        ev.update(extra)
        self.events.append(ev)
        self.counters[f'{kind}:{rule}'] += 1
        self.writer.put('line', 'events.jsonl', ev)

    def task_dir(self, pid):
        """The task marker in ``pid``'s environment (the run directory of the Bridge task that started it), the only
        entry of it read; None without one or when it cannot be read in time."""
        raw = self.reader.read(f'/proc/{pid}/environ', limit=1 << 20)
        prefixes = [(name + '=').encode() for name in TASK_DIR_ENVS]
        for entry in (raw or b'').split(b'\0'):
            for prefix in prefixes:
                if entry.startswith(prefix):
                    return entry[len(prefix):].decode('utf-8', 'replace')
        return None

    def alert(self, rule, text, st=None, task=None):
        """alerts.jsonl keeps every alert; the outbox file is the Bridge's to send (to the task's topic if any).
        ``task``: the marker read before the process was signalled (a capture's)."""
        self.counters['alerts'] += 1
        stamp = now_iso()
        task = task or (self.task_dir(st['pid']) if st else None)
        self.writer.put('line', 'alerts.jsonl', {'time': stamp, 'rule': rule, 'task_dir': task, 'text': text})
        self.seq += 1
        name = f'{dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")}-{os.getpid()}-{self.seq}.json'
        self.writer.put('alert', name, {'schema': 1, 'id': name[:-5], 'time': stamp, 'rule': rule, 'text': text,
                                        'task_dir': task, 'pid': st['pid'] if st else None,
                                        'comm': st['comm'] if st else None})

    def describe(self, st, cap, path):
        chain = ' ← '.join(a['comm'] for a in cap['ancestors'][:6]) or ('已被 PID 1 收养，原父进程已退出'
                                                                      if st['ppid'] == 1 else '?')
        run = next((a['run_dir'] for a in cap['ancestors'] if a.get('run_dir')), None)
        cg = cap['container']
        lines = [f'进程：{st["comm"]} PID {st["pid"]}，RSS {gib(st["rss"])}，已运行 {st.get("age", 0):.0f} 秒',
                 f'命令：{cap["cmd"] or "?"}', f'目录：{cap["cwd"] or "?"}',
                 f'来源：{chain}' + (f'（run {run}）' if run else '')]
        if cg_ratio(cg) is not None:
            lines.append(f'容器匿名内存：{gib(cg["anon"])} / {gib(cg["limit"])}')
        lines.append(f'现场：{path}')
        return '\n'.join(lines)

    # --- rules ---

    def scan(self):
        t0 = time.monotonic()
        up = uptime()
        cg = read_cgroup()
        procs = []
        for name in os.listdir('/proc'):
            if name.isdigit():
                st = proc_stat(int(name))
                if st and st['state'] != 'Z':
                    st['age'] = round(up - st['starttime'] / HZ, 1)
                    procs.append(st)
        live, orphans = set(), []
        for st in procs:
            key = (st['pid'], st['starttime'])
            live.add(key)
            if st['pid'] in self.protected or (self.only is not None and st['comm'] not in self.only):
                continue
            memo = self.seen.setdefault(key, {})
            if memo.get('killed'):
                continue
            named = st['comm'] in self.search
            if st['rss'] >= self.kill_bytes and (named or self.search_by_exe(st, memo)):
                self.kill(st, 'search_rss', {'threshold_mib': self.kill_mib}, memo, cg)
            elif named:
                if self.orphan_due(st, memo):
                    orphans.append((st, memo))
                self.check_long(st, memo)
            else:
                self.check_big(st, memo)
        if orphans:
            self.reap_orphans(orphans, procs, cg)
        for key in [k for k in self.seen if k not in live]:
            del self.seen[key]
        self.check_container(cg, procs)
        self.scans += 1
        self.last_scan = {'time': now_iso(), 'procs': len(procs), 'ms': round((time.monotonic() - t0) * 1000, 1),
                          'container_anon_ratio': None if cg_ratio(cg) is None else round(cg_ratio(cg), 4)}

    def search_by_exe(self, st, memo):
        if 'search_exe' not in memo:
            exe = os.path.basename(readlink(f'/proc/{st["pid"]}/exe') or '')
            argv = self.cmdline(st['pid']) or ['']
            memo['search_exe'] = exe in self.search or os.path.basename(argv[0]) in self.search
        return memo['search_exe']

    def kill(self, st, rule, detail, memo, cg):
        pid = st['pid']
        ratio = cg_ratio(cg)
        cap = self.capture(st, rule, detail, resample=ratio is None or ratio < 0.9)
        cur = proc_stat(pid)
        outcome = {'rss_before_signal': cur['rss'] if cur else None}
        if not self.cfg['kill_enabled']:
            outcome['result'] = 'not_enabled'
        elif not cur or cur['starttime'] != st['starttime'] or cur['state'] == 'Z':
            outcome['result'] = 'exited_before_kill'
        else:
            try:
                os.kill(pid, signal.SIGKILL)
                outcome['result'] = 'killed' if wait_gone(pid, st['starttime'], 5.0) else 'kill_pending'
            except ProcessLookupError:
                outcome['result'] = 'exited_before_kill'
            except PermissionError:
                outcome['result'] = 'permission_denied'
        memo['killed'] = True
        cap['kill'] = outcome
        path = self.save_capture(cap, st['comm'])
        self.record('kill', rule, st, {'result': outcome['result'], 'capture': path, 'cmd': cap['cmd']})
        if outcome['result'] == 'not_enabled':
            title = ('【内存看门狗】发现失控搜索进程，没有终止（本机没有开启终止）' if rule == 'search_rss'
                     else '【内存看门狗】发现输出已无人读取的孤儿搜索进程，没有终止（本机没有开启终止）')
        else:
            title = ('【内存看门狗】已终止失控搜索进程' if rule == 'search_rss'
                     else '【内存看门狗】已回收孤儿搜索进程（输出管道已无人读取）') + f'（{outcome["result"]}）'
        self.alert(rule, title + '\n' + self.describe(st, cap, path), st, cap['task_dir'])

    def orphan_due(self, st, memo):
        if st['ppid'] not in self.reapers or st['age'] < self.cfg['orphan_min_age_seconds']:
            return False
        now = time.monotonic()
        if now - memo.get('orphan_checked', -1e9) < 30:
            return False
        memo['orphan_checked'] = now
        return True

    def reap_orphans(self, orphans, procs, cg):
        targets = {}
        for st, memo in orphans:
            target = readlink(f'/proc/{st["pid"]}/fd/1')
            if target and target.startswith('pipe:['):
                targets.setdefault(target, []).append((st, memo))
        if not targets:
            return
        readers = pipe_readers(set(targets), procs)
        for target, group in targets.items():
            for st, memo in group:
                if readers.get(target, set()) - {st['pid']}:
                    continue
                if st['comm'] in self.find_names:
                    argv = self.cmdline(st['pid'])
                    if argv is None or FIND_ACTIONS.intersection(argv):
                        continue
                self.kill(st, 'orphan', {'stdout': target, 'ppid': st['ppid']}, memo, cg)

    def check_long(self, st, memo):
        if memo.get('long') or st['age'] < self.cfg['search_long_run_seconds']:
            return
        memo['long'] = True
        argv = self.cmdline(st['pid'])
        cwd = readlink(f'/proc/{st["pid"]}/cwd')
        self.record('alert', 'long_search', st, {'cmd': short_cmd(argv)})
        self.alert('long_search', f'【内存看门狗】搜索进程已运行 {st["age"] / 3600:.1f} 小时（未处理）\n'
                   f'进程：{st["comm"]} PID {st["pid"]}，RSS {gib(st["rss"])}\n命令：{short_cmd(argv)}\n目录：{cwd or "?"}', st)

    def check_big(self, st, memo):
        crossed = [level for level in self.levels if st['rss'] >= level]
        if not crossed or crossed[-1] <= memo.get('alert_level', 0):
            return
        memo['alert_level'] = crossed[-1]
        level = f'{crossed[-1] / GIB:g}'
        cap = self.capture(st, 'big_process', {'level_gib': level}, resample=False)
        path = self.save_capture(cap, st['comm'])
        self.record('alert', 'big_process', st, {'level_gib': level, 'capture': path, 'cmd': cap['cmd']})
        nxt = [f'{lv / GIB:g}' for lv in self.levels if lv > crossed[-1]]
        self.alert('big_process', f'【内存看门狗】进程内存超过 {level} GiB 告警线（未处理，只记录现场）\n'
                   + self.describe(st, cap, path) + (f'\n超过 {nxt[0]} GiB 时再次提醒。' if nxt else ''), st,
                   cap['task_dir'])

    def check_container(self, cg, procs):
        ratio = cg_ratio(cg)
        if ratio is None:
            return
        if ratio < self.cfg['container_rearm_ratio']:
            self.container_armed = True
        if ratio < self.cfg['container_alert_ratio'] or not self.container_armed:
            return
        self.container_armed = False
        top = sorted(procs, key=lambda p: -p['rss'])[:10]
        cap = {'schema': 1, 'time': now_iso(), 'rule': 'container', 'container': cg,
               'pressure': {k: (read_text(f'/proc/pressure/{k}', 512) or '').strip() for k in ('memory', 'io', 'cpu')},
               'top': [{k: p.get(k) for k in ('pid', 'comm', 'ppid', 'state', 'rss', 'threads', 'age')} for p in top]}
        path = self.save_capture(cap, 'all')
        self.record('alert', 'container', None, {'ratio': round(ratio, 4), 'capture': path})
        lines = [f'{p["comm"]} PID {p["pid"]} {gib(p["rss"])}' for p in top[:5]]
        self.alert('container', f'【内存看门狗】容器匿名内存已达 {ratio:.0%}（{gib(cg["anon"])} / {gib(cg["limit"])}）\n'
                   f'占用最多：' + '；'.join(lines) + f'\n现场：{path}')

    def maintain_rg(self):
        rc = self.cfg['rg_guard']
        if not rc.get('enabled'):
            self.rg['state'] = 'disabled'
            return
        now = time.monotonic()
        if now - self.rg['checked'] < rc['check_seconds']:
            return
        self.rg['checked'] = now
        target = Path(rc['target'])
        state = rg_state(target)
        self.rg['state'] = state
        if state == 'wrapped_missing_real' and not self.rg['broken_alerted']:
            self.rg['broken_alerted'] = True
            self.record('rg_guard', 'broken', None, {'target': str(target)})
            self.alert('rg_guard', f'【内存看门狗】rg 包装找不到原始程序，rg 当前不可用：{target}\n'
                       f'修复：重新安装 @openai/codex，看门狗会自动补装包装。')
        if state == 'wrapped' and read_text(target, 8192) != wrapper_text(target.parent / '.real' / 'rg', rc['as_mib']):
            result = rg_install(target, rc['as_mib'])
            self.record('rg_guard', 'update', None, {'target': str(target), **result})
            return
        if state != 'elf' or not settled(target, rc['settle_seconds']):
            return
        hist, t = self.rg['reinstalls'], time.time()
        while hist and t - hist[0] > 3600:
            hist.popleft()
        if len(hist) >= rc['max_reinstalls_per_hour']:
            self.rg['state'] = 'elf_reinstall_limited'
            return
        hist.append(t)
        self.rg['broken_alerted'] = False
        result = rg_install(target, rc['as_mib'])
        self.rg['state'] = rg_state(target)
        self.record('rg_guard', 'install', None, {'target': str(target), **result})

    def heartbeat(self, fd):
        hb = {'time': now_iso(), 'pid': os.getpid(), 'scans': self.scans, 'last_scan': self.last_scan,
              'counters': dict(self.counters), 'recent': list(self.events)[-8:], 'rg_guard': self.rg['state'],
              'writer': dict(self.writer.stats), 'kill_enabled': self.cfg['kill_enabled'],
              'thresholds_mib': {'search_kill': self.kill_mib, 'alert_levels': [lv // MIB for lv in self.levels]},
              'errors': self.errors, 'last_error': self.last_error}
        if fd is not None:
            try:
                os.write(fd, (json.dumps(hb, ensure_ascii=False, default=str) + '\n').encode())
            except (BlockingIOError, BrokenPipeError):
                pass
        if time.monotonic() - self.last_status_write >= 30:
            self.last_status_write = time.monotonic()
            self.writer.put('status', 'status.json', hb)
        return hb

    def fail(self, exc):
        self.errors += 1
        self.last_error = f'{now_iso()} {type(exc).__name__}: {exc}'[:400]
        self.writer.put('line', 'events.jsonl', {'time': now_iso(), 'kind': 'error', 'error': self.last_error})


def run_dir_of(argv):
    if argv and '--run-dir' in argv[:-1]:
        return os.path.basename(argv[argv.index('--run-dir') + 1].rstrip('/'))
    return None


def wait_gone(pid, starttime, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = proc_stat(pid)
        if not st or st['starttime'] != starttime or st['state'] == 'Z':
            return True
        time.sleep(0.1)
    return False


def pipe_readers(targets, procs):
    """Map each pipe target to the PIDs holding it open for reading."""
    found = {}
    for st in procs:
        pid = st['pid']
        try:
            names = os.listdir(f'/proc/{pid}/fd')
        except OSError:
            continue
        for n in names:
            link = readlink(f'/proc/{pid}/fd/{n}')
            if link in targets:
                flags = parse_fdinfo(read_text(f'/proc/{pid}/fdinfo/{n}', 512)).get('flags')
                if flags is None or flags & os.O_ACCMODE != os.O_WRONLY:
                    found.setdefault(link, set()).add(pid)
    return found


# ---------- config, control socket, supervisor ----------

def load_config(path):
    raw = json.loads(Path(path).read_text())
    if raw.get('owner') != OWNER:
        raise SystemExit(f'{path}: owner is {raw.get("owner")!r}, expected {OWNER}')
    cfg = json.loads(json.dumps(DEFAULTS))
    for k, v in raw.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    return cfg


def socket_name(config_path):
    digest = hashlib.sha1(str(Path(config_path).absolute()).encode()).hexdigest()[:12]
    return f'\0memory-guard-{os.getuid()}-{digest}'


def query(config_path, request=b'status', timeout=5):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(socket_name(config_path))
        s.sendall(request + b'\n')
        chunks = []
        while True:
            b = s.recv(65536)
            if not b:
                break
            chunks.append(b)
    except OSError:
        return None
    finally:
        s.close()
    return json.loads(b''.join(chunks) or b'null')


def limit_self(as_mib):
    # Soft limit only: the supervisor's children must stay free to pick their own cap.
    _, hard = resource.getrlimit(resource.RLIMIT_AS)
    soft = as_mib * MIB if hard == resource.RLIM_INFINITY else min(as_mib * MIB, hard)
    try:
        resource.setrlimit(resource.RLIMIT_AS, (soft, hard))
    except (ValueError, OSError):
        pass


def set_pdeathsig():
    import ctypes
    ctypes.CDLL(None, use_errno=True).prctl(1, int(signal.SIGTERM), 0, 0, 0)  # PR_SET_PDEATHSIG


def serve(sock, state):
    while True:
        try:
            conn, _ = sock.accept()
        except OSError:
            return
        with conn:
            try:
                conn.settimeout(3)
                _, uid, _ = struct.unpack('3i', conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                                                struct.calcsize('3i')))
                if uid != os.getuid():
                    continue
                req = conn.recv(64).strip()
                if req == b'stop':
                    state['stopping'] = True
                    reply = {'ok': True, 'stopping': True}
                else:
                    reply = {k: v for k, v in state.items() if k != 'beat_mono'}
                    reply['heartbeat_age_s'] = round(time.monotonic() - state['beat_mono'], 1)
                conn.sendall(json.dumps(reply, ensure_ascii=False, default=str).encode())
            except OSError:
                pass


def cmd_daemon(args):
    cfg = load_config(args.config)
    if not cfg.get('enabled'):
        return 0
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(socket_name(args.config))
    except OSError:
        return 0  # another supervisor for this config already runs in this container
    sock.listen(4)
    limit_self(256)
    me = Path(__file__).resolve()
    state = {'owner': OWNER, 'pid': os.getpid(), 'started_at': now_iso(), 'config': str(args.config),
             'version': hashlib.sha256(me.read_bytes()).hexdigest()[:16], 'worker': None, 'restarts': 0,
             'stalls': 0, 'stopping': False, 'heartbeat': None, 'beat_mono': time.monotonic()}
    threading.Thread(target=serve, args=(sock, state), name='control', daemon=True).start()
    signal.signal(signal.SIGTERM, lambda *_: state.update(stopping=True))
    env = dict(os.environ, MALLOC_ARENA_MAX='2')
    backoff = 5
    while not state['stopping']:
        started = time.monotonic()
        p = subprocess.Popen([sys.executable, '-B', str(me), 'run', '--config', str(args.config),
                              '--parent-pid', str(os.getpid())],
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             close_fds=True, env=env)
        state['worker'] = {'pid': p.pid, 'started_at': now_iso()}
        state['beat_mono'] = time.monotonic()
        buf = b''
        while not state['stopping']:
            ready, _, _ = select.select([p.stdout], [], [], 5)
            if ready:
                chunk = os.read(p.stdout.fileno(), 65536)
                if not chunk:
                    break
                buf += chunk
                *lines, buf = buf.split(b'\n')
                for line in reversed(lines):
                    try:
                        state['heartbeat'] = json.loads(line)
                        state['beat_mono'] = time.monotonic()
                        break
                    except ValueError:
                        continue
            if p.poll() is not None:
                break
            if time.monotonic() - state['beat_mono'] > 90:
                state['stalls'] += 1
                p.kill()
                break
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
        p.stdout.close()
        if state['stopping']:
            break
        state['restarts'] += 1
        backoff = 5 if time.monotonic() - started > 60 else min(backoff * 2, 300)
        deadline = time.monotonic() + backoff
        while time.monotonic() < deadline and not state['stopping']:
            time.sleep(1)
    sock.close()
    return 0


def cmd_run(args):
    cfg = load_config(args.config)
    if args.parent_pid:
        set_pdeathsig()
        if os.getppid() != args.parent_pid:
            return 1
    limit_self(cfg['self_as_mib'])
    threading.stack_size(1 << 20)
    guard = Guard(cfg)
    stop = threading.Event()
    for s in (signal.SIGTERM, signal.SIGINT):
        signal.signal(s, lambda *_: stop.set())
    fd = None
    if args.parent_pid:
        fd = sys.stdout.fileno()
        os.set_blocking(fd, False)
    guard.record('worker', 'start', None, {'version': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16],
                                           'search_kill_mib': guard.kill_mib, 'kill_enabled': cfg['kill_enabled'],
                                           'alert_levels_mib': [lv // MIB for lv in guard.levels]})
    end = time.monotonic() + args.duration if args.duration else None
    hb = None
    while not stop.is_set():
        t0 = time.monotonic()
        for step in (guard.scan, guard.maintain_rg):
            try:
                step()
            except Exception as e:
                guard.fail(e)
        hb = guard.heartbeat(fd)
        if end is not None and time.monotonic() >= end:
            break
        stop.wait(max(0.2, cfg['interval_seconds'] - (time.monotonic() - t0)))
    guard.record('worker', 'stop', None, {})
    guard.writer.stop()
    if fd is None:
        print(json.dumps(hb, ensure_ascii=False, default=str))
    return 0


# ---------- installation ----------

def codex_rg():
    """Codex's vendored rg, first on PATH in Codex's sessions and the Bridge's (an npm install of @openai/codex),
    found from the codex on PATH; None without one."""
    codex = shutil.which('codex')
    if not codex:
        return None
    package = Path(codex).resolve().parents[1]  # <prefix>/lib/node_modules/@openai/codex/bin/codex.js
    found = sorted(package.glob('node_modules/@openai/codex-*/vendor/*/codex-path/rg'))
    return found[0] if found else None


def atomic_write(path, data, mode):
    path = Path(path)
    if path.exists() and OWNER.encode() not in path.read_bytes():
        raise SystemExit(f'refusing to overwrite {path}: not installed by {OWNER}')
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f'.{path.name}.{os.getpid()}.tmp'
    tmp.write_bytes(data if isinstance(data, bytes) else data.encode())
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def stop_daemon(timeout=30):
    if query(CONFIG) is None:
        return False
    query(CONFIG, b'stop')
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if query(CONFIG, timeout=2) is None:
            return True
        time.sleep(1)
    raise SystemExit('daemon did not stop in time')


def spawn_daemon(python=None):
    """Start the installed copy's supervisor in a session of its own: it outlives whoever started it. Without a
    task's marker: started from a task's shell, it would be counted among what that task left running."""
    env = {k: v for k, v in os.environ.items() if k not in TASK_DIR_ENVS}
    return subprocess.Popen([python or sys.executable, '-B', str(INSTALLED), 'daemon', '--config', str(CONFIG)],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True, close_fds=True, env=env)


def start_daemon():
    spawn_daemon()
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        st = query(CONFIG, timeout=2)
        if st and st.get('heartbeat'):
            return st
        time.sleep(1)
    return query(CONFIG)


_ensured = {'checked': -1e9, 'started': -1e9, 'child': None}


def ensure(check_every=30, start_every=60):
    """What the Manager calls on each pass (service/manager.py): every ``check_every`` seconds, start the installed
    watchdog when it is enabled and not running, at most once every ``start_every`` seconds. 'running', 'started',
    'disabled', 'not_installed' or 'waiting'."""
    now = time.monotonic()
    if now - _ensured['checked'] < check_every:
        return 'waiting'
    _ensured['checked'] = now
    if _ensured['child'] is not None and _ensured['child'].poll() is not None:
        _ensured['child'] = None  # Reaped: one that found another supervisor running exits at once.
    if not INSTALLED.exists() or not CONFIG.exists():
        return 'not_installed'
    if query(CONFIG, timeout=2) is not None:
        return 'running'
    try:
        cfg = json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        return 'not_installed'
    if cfg.get('owner') != OWNER or not cfg.get('enabled'):
        return 'disabled'
    if now - _ensured['started'] < start_every:
        return 'waiting'
    _ensured['started'] = now
    _ensured['child'] = spawn_daemon()
    return 'started'


def cmd_enable(args):
    cfg = json.loads(CONFIG.read_text()) if CONFIG.exists() else {'owner': OWNER}
    if cfg.get('owner') != OWNER:
        raise SystemExit(f'{CONFIG} belongs to {cfg.get("owner")!r}')
    data_dir = Path(args.data_dir or cfg.get('data_dir') or args.default_data_dir or '')
    if not data_dir.is_absolute():
        raise SystemExit('--data-dir must be an absolute path')
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not os.access(data_dir, os.W_OK):
        raise SystemExit(f'{data_dir} is not writable')
    cfg.update(enabled=True, data_dir=str(data_dir), data_mount=mount_of(data_dir))
    if args.kill is not None:
        cfg['kill_enabled'] = args.kill == 'on'
    if args.search_kill_mib is not None:
        cfg['search_kill_mib'] = args.search_kill_mib or None
    if args.alert_levels_mib is not None:
        cfg['alert_levels_mib'] = [int(v) for v in args.alert_levels_mib.split(',') if v.strip()] or None
    if args.rg_guard is not None:
        rg = cfg.setdefault('rg_guard', {})
        rg['enabled'] = args.rg_guard == 'on'
        if rg['enabled'] and not rg.get('target'):
            target = codex_rg()
            if target is None:
                raise SystemExit('--rg-guard on: no Codex vendored rg found from the codex on PATH')
            rg['target'] = str(target)
    # Write every effective setting so the thresholds are visible in the config file.
    full = json.loads(json.dumps(DEFAULTS))
    for k, v in cfg.items():
        if isinstance(v, dict) and isinstance(full.get(k), dict):
            full[k].update(v)
        else:
            full[k] = v
    plan = {'install': [str(INSTALLED), str(CONFIG)], 'config': full}
    if args.dry_run:
        print(json.dumps(plan, ensure_ascii=False, indent=1))
        return 0
    source = Path(__file__).resolve().read_bytes()
    stop_daemon()
    atomic_write(INSTALLED, source, 0o644)
    atomic_write(CONFIG, json.dumps(full, ensure_ascii=False, indent=1) + '\n', 0o600)
    print(json.dumps({'installed': plan['install'], 'daemon': start_daemon()}, ensure_ascii=False, indent=1,
                     default=str))
    return 0


def cmd_disable(args):
    cfg = json.loads(CONFIG.read_text())
    if cfg.get('owner') != OWNER:
        raise SystemExit(f'{CONFIG} belongs to {cfg.get("owner")!r}')
    cfg['enabled'] = False
    atomic_write(CONFIG, json.dumps(cfg, ensure_ascii=False, indent=1) + '\n', 0o600)
    print(json.dumps({'disabled': True, 'stopped': stop_daemon()}))
    return 0


def cmd_uninstall(args):
    stopped = stop_daemon() if CONFIG.exists() else False
    removed = []
    for path in (INSTALLED, CONFIG):
        if path.exists() and OWNER.encode() in path.read_bytes():
            path.unlink()
            removed.append(str(path))
    # The rg wrapper needs nothing of this tool; `rg-guard uninstall` puts Codex's binary back.
    print(json.dumps({'stopped': stopped, 'removed': removed}))
    return 0


def rg_target(config_path=CONFIG):
    try:
        target = load_config(config_path)['rg_guard'].get('target')
    except (OSError, ValueError, SystemExit):
        target = None
    return Path(target) if target else codex_rg()


def cmd_status(args):
    target = args.config or CONFIG
    live = query(target)
    files = {str(p): p.exists() for p in (INSTALLED, CONFIG)}
    rg = rg_target(target)
    print(json.dumps({'running': live is not None, 'daemon': live, 'files': files,
                      'rg_guard': {'target': str(rg) if rg else None, 'state': rg_state(rg) if rg else 'missing'}},
                     ensure_ascii=False, indent=1, default=str))
    return 0


def cmd_rg_guard(args):
    target = Path(args.target) if args.target else rg_target()
    if target is None:
        raise SystemExit('no rg to guard: give --target (no Codex vendored rg found from the codex on PATH)')
    if args.action == 'status':
        out = {'target': str(target), 'state': rg_state(target)}
    elif args.action == 'install':
        out = rg_install(target, args.as_mib)
    else:
        out = rg_uninstall(target)
    print(json.dumps(out, ensure_ascii=False))
    return 0


def main(argv=None, default_data_dir=None):
    """``default_data_dir``: where ``enable`` puts the data without --data-dir (bin/covey_memguard.py passes
    <service root>/memory-guard)."""
    ap = argparse.ArgumentParser(prog='covey_memguard.py', description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('status')
    p.add_argument('--config')
    p = sub.add_parser('enable')
    p.add_argument('--data-dir')
    p.add_argument('--kill', choices=['on', 'off'], help='end runaway search tools (off: capture and alert only)')
    p.add_argument('--rg-guard', choices=['on', 'off'], help="wrap Codex's vendored rg with an address-space cap")
    p.add_argument('--search-kill-mib', type=int, help='0: a share of the container limit (the default)')
    p.add_argument('--alert-levels-mib', help='comma-separated; empty: shares of the container limit')
    p.add_argument('--dry-run', action='store_true')
    sub.add_parser('disable')
    sub.add_parser('uninstall')
    p = sub.add_parser('daemon')
    p.add_argument('--config', required=True)
    p = sub.add_parser('run')
    p.add_argument('--config', required=True)
    p.add_argument('--parent-pid', type=int, default=0)
    p.add_argument('--duration', type=float, default=0, help='seconds; 0 runs until signalled')
    p = sub.add_parser('rg-guard')
    p.add_argument('action', choices=['status', 'install', 'uninstall'])
    p.add_argument('--target')
    p.add_argument('--as-mib', type=int, default=DEFAULTS['rg_guard']['as_mib'])
    args = ap.parse_args(argv)
    args.default_data_dir = default_data_dir
    handlers = {'status': cmd_status, 'enable': cmd_enable, 'disable': cmd_disable, 'uninstall': cmd_uninstall,
                'daemon': cmd_daemon, 'run': cmd_run, 'rg-guard': cmd_rg_guard}
    return handlers[args.cmd](args)


if __name__ == '__main__':
    default = None
    if sys.argv[1:2] == ['enable']:
        try:  # From the repository (bin/covey_memguard.py): the data goes under the service root by default.
            from covey.core.paths import service_root
            default = str(service_root() / 'memory-guard')
        except ImportError:
            pass  # The installed copy: its config names the data dir.
    sys.exit(main(default_data_dir=default))
