"""The Node: the program that stays on a device (and on the hub itself) and keeps one connection to its hub.

A single file with no import from covey, like core/device_shim.py: the hub serves its bytes (to the join command a
device owner pastes) or pushes them over ssh, and it runs as ``python3 ~/.covey/node/node.py <cmd>``. It must run
on Python 3.9 with the standard library only; ``scripts/check_shim_compat.py`` checks both (docs/architecture.md
"节点").

What it does, in one process (``run``):
- **samples** the machine every ``INTERVAL`` seconds (``BURST`` while memory is tight or growing fast, for at least
  ``BURST_HOLD``): CPU and memory of the container (cgroup v1 or v2; the machine's /proc when there is no limit),
  pressure, load, the disks of ``paths``, GPUs when ``nvidia-smi`` is there, and the processes using the most memory
  and CPU, each with the Covey task it belongs to (the ``COVEY_TASK_DIR`` of its environment, the only variable
  read). For the disks also: the processes that wrote the most (``writers``, from ``/proc/<pid>/io`` every
  WRITERS_EVERY), and on a disk DIRS_AT full or more how much it grew (``growth``) and its biggest places
  (``dirs``: counted in a thread, never entering another mount, DIRS_BUDGET seconds at most). Each sample is one JSON line in a local ring (``~/.covey/node/data/``: SEGMENT bytes a segment, RING bytes and
  KEEP_DAYS at most), so a hung or full network disk stops nothing and the hub can take what it missed.
- **dials the hub** (TLS, the hub's certificate pinned by the SHA-256 of its DER; never listens on a port): ``hello``
  with its key, then the samples the hub has not acknowledged (``welcome.ack``), then each new one. The hub may say
  ``revoke`` (the Node uninstalls itself) or name a newer version (the Node fetches it over the same pinned TLS and
  starts again). A lost connection is tried again after BACKOFF seconds, doubling to BACKOFF_MAX.

Commands: ``join`` (with a one-time token: ask the hub for this device's key, write config.json, install the
autostart, start), ``run`` (the daemon, foreground), ``ensure`` (start it unless it runs; what the login hook and
the hub's ssh call), ``status`` (one JSON line), ``uninstall`` (stop, remove the autostart and ~/.covey/node).
Exactly one daemon runs per home: it holds ``flock`` on ``lock``. ``COVEY_NODE_AUTOSTART=hook`` limits the autostart to
the login hook (no systemd unit, no launchd agent).

Wire (one JSON object a line, both ways, LINE_MAX at most): node -> hub ``{"k":"join",...}``, ``{"k":"hello",...}``,
``{"k":"s","seq":N,...}`` (a sample); hub -> node ``{"k":"welcome","ack":N,"version":"<sha16>"}``, ``{"k":"ack",
"seq":N}``, ``{"k":"revoke"}``, ``{"k":"err","err":"<code>"}``. Unknown keys and kinds are ignored; fields are only
added.
"""
from __future__ import annotations

import fcntl
import getpass
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time

PROTO = 1
INTERVAL = 10.0
BURST = 2.0
BURST_HOLD = 300.0
GROWTH_WINDOW = 60.0
GROWTH_FRACTION = 0.05  # Of the limit, within GROWTH_WINDOW: sample every BURST.
BURST_RATIO = 0.80  # Memory working set over the limit.
GPU_EVERY = 30.0
PROBE_EVERY = 600.0  # A write probe of each path in ``probe``.
PROBE_TIMEOUT = 10.0
SEGMENT = 16 << 20
RING = 256 << 20
KEEP_DAYS = 7
TOP_RSS = 20
TOP_CPU = 10
WIRE_PROCS = 8
LINE_MAX = 4 << 20
BACKOFF = 1.0
BACKOFF_MAX = 60.0
RECENT = 360  # Samples kept in memory as well.
BACKFILL_MAX = 8640  # Samples sent from the ring after a reconnect: a day at INTERVAL.
CONNECT_TIMEOUT = 10.0
ACK_IDLE = 120.0  # No line from the hub this long: the connection is dropped and dialled again.
LOG_MAX = 1 << 20
TASK_ENVS = (b"COVEY_TASK_DIR=", b"LARK_BRIDGE_TASK_DIR=")
WRITERS_EVERY = 60.0  # /proc/<pid>/io of every process read this often: who wrote the most since the last read.
TOP_WRITERS = 5
WRITERS_MIN = 16 << 20  # Bytes a process must have written since the last read to be named.
GROWTH_EVERY = 600.0  # One (time, used) point a disk this often, for how much it grew in GROWTH_SPANS.
GROWTH_SPANS = (3600, 86400)
DIRS_AT = 85.0  # A disk this full (percent) gets its biggest places counted, DIRS_EVERY at most and in a thread.
DIRS_EVERY = 1800.0
DIRS_BUDGET = 120.0  # Seconds a count may take; past them it stops and says ``partial``.
DIRS_TOP = 6
SHAPES_MAX = 2000  # Name shapes kept a folder of loose files.
HOOK_BEGIN = "# >>> covey node >>>"
HOOK_END = "# <<< covey node <<<"
LAUNCHD_LABEL = "com.covey.node"

HOME = os.path.expanduser("~")
ROOT = os.path.join(HOME, ".covey", "node")
PAGE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
HZ = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


def path(*parts):
    return os.path.join(ROOT, *parts)


def version():
    """The first 16 hex digits of this file's SHA-256, as the hub names versions."""
    with open(os.path.abspath(__file__), "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()[:16]


def log(message):
    target = path("node.log")
    try:
        if os.path.exists(target) and os.path.getsize(target) > LOG_MAX:
            os.replace(target, target + ".1")
        with open(target, "a") as handle:
            handle.write("%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), message))
    except OSError:
        pass


def read_text(name, limit=1 << 20):
    try:
        with open(name, "rb") as handle:
            return handle.read(limit).decode("utf-8", "replace")
    except OSError:
        return None


def int_or(text):
    try:
        return int(str(text).strip().split()[0])
    except (ValueError, IndexError, AttributeError):
        return None


def parse_kv(text):
    out = {}
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) >= 2:
            try:
                out[parts[0]] = int(parts[1])
            except ValueError:
                pass
    return out


def write_json(name, value, mode=0o600):
    tmp = "%s.%d.tmp" % (name, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as handle:
        json.dump(value, handle, sort_keys=True)
    os.replace(tmp, name)


def read_json(name):
    try:
        with open(name) as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def boot_id():
    text = read_text("/proc/sys/kernel/random/boot_id", 128)
    if text:
        return text.strip()
    try:
        out = subprocess.run(["sysctl", "-n", "kern.bootsessionuuid"], capture_output=True, text=True, timeout=5)
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


# ---------------------------------------------------------------- sampling

def cgroup_memory():
    """The container's memory: usage, limit, anon, file, inactive_file, failcnt, oom_kill (None where unknown)."""
    v1 = "/sys/fs/cgroup/memory"
    if os.path.exists(v1 + "/memory.usage_in_bytes"):
        stat = parse_kv(read_text(v1 + "/memory.stat"))
        oom = parse_kv(read_text(v1 + "/memory.oom_control"))
        return {"usage": int_or(read_text(v1 + "/memory.usage_in_bytes")),
                "limit": int_or(read_text(v1 + "/memory.limit_in_bytes")),
                "anon": stat.get("total_rss", stat.get("rss")), "file": stat.get("total_cache", stat.get("cache")),
                "inactive_file": stat.get("total_inactive_file", stat.get("inactive_file")),
                "swap": stat.get("total_swap", stat.get("swap")),
                "failcnt": int_or(read_text(v1 + "/memory.failcnt")), "oom_kill": oom.get("oom_kill")}
    v2 = "/sys/fs/cgroup"
    if os.path.exists(v2 + "/memory.current"):
        stat = parse_kv(read_text(v2 + "/memory.stat"))
        events = parse_kv(read_text(v2 + "/memory.events"))
        limit = read_text(v2 + "/memory.max") or ""
        return {"usage": int_or(read_text(v2 + "/memory.current")),
                "limit": None if limit.strip() == "max" else int_or(limit),
                "anon": stat.get("anon"), "file": stat.get("file"), "inactive_file": stat.get("inactive_file"),
                "swap": int_or(read_text(v2 + "/memory.swap.current")), "failcnt": events.get("max"),
                "oom_kill": events.get("oom_kill")}
    return {}


def machine_memory():
    """/proc/meminfo's total and available, in bytes; on macOS what sysctl and vm_stat say."""
    info = parse_kv((read_text("/proc/meminfo") or "").replace(":", ""))
    if info.get("MemTotal"):
        return {"total": info["MemTotal"] * 1024, "available": info.get("MemAvailable", info.get("MemFree", 0)) * 1024}
    try:
        total = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5).stdout)
        vm = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}
    pages = {}
    size = 4096
    for line in vm.splitlines():
        if "page size of" in line:
            size = int_or(line.split("page size of")[1]) or size
        name, _, value = line.partition(":")
        number = int_or(value.replace(".", ""))
        if number is not None:
            pages[name.strip()] = number
    free = sum(pages.get(k, 0) for k in ("Pages free", "Pages inactive", "Pages speculative", "Pages purgeable"))
    return {"total": total, "available": free * size}


def memory():
    """The working set against the limit that applies: the cgroup's when it has one, else the machine's."""
    cg = cgroup_memory()
    limit = cg.get("limit")
    if cg.get("usage") is not None and limit and limit < (1 << 60):
        used = max(0, cg["usage"] - (cg.get("inactive_file") or 0))
        out = {"scope": "cgroup", "limit": limit, "used": used, "anon": cg.get("anon"), "file": cg.get("file"),
               "swap": cg.get("swap"), "failcnt": cg.get("failcnt"), "oom_kill": cg.get("oom_kill")}
    else:
        mm = machine_memory()
        if not mm.get("total"):
            return {}
        out = {"scope": "machine", "limit": mm["total"], "used": mm["total"] - mm.get("available", 0),
               "anon": cg.get("anon"), "file": cg.get("file"), "oom_kill": cg.get("oom_kill")}
    out["pct"] = round(100.0 * out["used"] / out["limit"], 1) if out["limit"] else None
    return out


def pressure(kind):
    """``some`` and ``full`` avg10 of /proc/pressure/<kind>; {} where there is no PSI."""
    out = {}
    for line in (read_text("/proc/pressure/" + kind, 512) or "").splitlines():
        parts = line.split()
        for part in parts[1:]:
            if part.startswith("avg10="):
                try:
                    out[parts[0]] = float(part[6:])
                except ValueError:
                    pass
    return out


def cpu_counters():
    """(usage seconds, cores the container may use, throttled seconds): the cgroup's, else the machine's /proc/stat."""
    cores = float(os.cpu_count() or 1)
    usage = throttled = None
    for base in ("/sys/fs/cgroup/cpu,cpuacct", "/sys/fs/cgroup/cpu"):
        quota, period = int_or(read_text(base + "/cpu.cfs_quota_us")), int_or(read_text(base + "/cpu.cfs_period_us"))
        if quota is not None:
            if quota > 0 and period:
                cores = min(cores, quota / period)
            throttled = (parse_kv(read_text(base + "/cpu.stat")).get("throttled_time") or 0) / 1e9
            break
    for name in ("/sys/fs/cgroup/cpuacct/cpuacct.usage", "/sys/fs/cgroup/cpu,cpuacct/cpuacct.usage"):
        value = int_or(read_text(name))
        if value is not None:
            usage = value / 1e9
            break
    if usage is None and os.path.exists("/sys/fs/cgroup/cpu.stat"):
        stat = parse_kv(read_text("/sys/fs/cgroup/cpu.stat"))
        usage = (stat.get("usage_usec") or 0) / 1e6
        throttled = (stat.get("throttled_usec") or 0) / 1e6
        limit = (read_text("/sys/fs/cgroup/cpu.max") or "").split()
        if len(limit) == 2 and limit[0] != "max":
            try:
                cores = min(cores, int(limit[0]) / int(limit[1]))
            except (ValueError, ZeroDivisionError):
                pass
    if usage is None:
        fields = (read_text("/proc/stat", 4096) or "").splitlines()
        if fields and fields[0].startswith("cpu "):
            ticks = [int(x) for x in fields[0].split()[1:]]
            usage = (sum(ticks) - ticks[3] - (ticks[4] if len(ticks) > 4 else 0)) / HZ
    return usage, cores, throttled


def disks(paths):
    out = []
    for name in paths:
        try:
            st = os.statvfs(name)
        except OSError as exc:
            out.append({"path": name, "error": type(exc).__name__})
            continue
        total, free = st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize
        row = {"path": name, "total": total, "free": free,
               "pct": round(100.0 * (total - free) / total, 1) if total else None}
        if st.f_files:
            row["inodes_pct"] = round(100.0 * (st.f_files - st.f_favail) / st.f_files, 1)
        out.append(row)
    return out


def mount_points():
    """The mount points of this machine (``/proc/self/mountinfo``), or None where there is none (a Mac)."""
    text = read_text("/proc/self/mountinfo", 4 << 20)
    if text is None:
        return None
    out = set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) > 4:
            out.add(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), parts[4]))
    return out


def shape(name):
    """A file name with its ids and numbers as ``*``: build-artifacts-*.tar for every such archive."""
    return re.sub(r"\*+", "*", re.sub(r"[0-9a-fA-F]{8,}|[0-9]+", "*", name))


def biggest(root, budget=DIRS_BUDGET, top=DIRS_TOP, mounts=None, clock=time.monotonic):
    """The biggest places on the disk of ``root``, by the blocks they use: each directory two levels down (three for
    one holding a quarter of all, like a home), and the files lying loose in ``root`` or in a directory one level
    down (with their commonest name shape, as a folder of archives is often what filled a disk). Another mount under ``root`` is never entered (a network disk is not
    walked): the mount points of ``mounts`` (``mount_points()`` when None), else a directory on another device.
    Files are counted whatever device they say, as overlay reports its layers' devices for them."""
    started = clock()
    mounts = mount_points() if mounts is None else mounts
    root = os.path.abspath(root)
    try:
        dev = os.stat(root).st_dev
    except OSError as exc:
        return {"path": root, "error": type(exc).__name__}
    places, inner, loose, shapes = {}, {}, {}, {}
    partial, seen = False, 0
    stack = [(root, 0, None, None)]
    while stack:
        folder, depth, place, sub = stack.pop()
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for entry in entries:
            seen += 1
            if seen % 2000 == 0 and clock() - started > budget:
                partial = True
                break
            try:
                if entry.is_dir(follow_symlinks=False):
                    if mounts is not None and entry.path in mounts:
                        continue
                    if mounts is None and entry.stat(follow_symlinks=False).st_dev != dev:
                        continue
                    stack.append((entry.path, depth + 1, entry.path if depth == 1 else place,
                                  entry.path if depth == 2 else sub))
                    continue
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            used = getattr(st, "st_blocks", 0) * 512 or st.st_size
            if depth < 2:
                key = folder
                count = loose.setdefault(key, [0, 0])
                count[0] += used
                count[1] += 1
                kinds = shapes.setdefault(key, {})
                kind = shape(entry.name)
                if kind in kinds or len(kinds) < SHAPES_MAX:
                    kinds[kind] = kinds.get(kind, 0) + used
            else:
                places[place] = places.get(place, 0) + used
                if sub:
                    inner[sub] = inner.get(sub, 0) + used
        if partial:
            break
    total = sum(places.values()) + sum(c[0] for c in loose.values())
    children = {}
    for name, used in inner.items():
        children.setdefault(os.path.dirname(name), []).append({"path": name, "bytes": used})
    rows = []
    for name, used in places.items():
        if used > total / 4 and children.get(name):
            rows += sorted(children[name], key=lambda r: -r["bytes"])[:top]
        else:
            rows.append({"path": name, "bytes": used})
    for key, (used, count) in loose.items():
        row = {"path": key, "bytes": used, "files": count}
        kinds = shapes.get(key) or {}
        if kinds:
            kind = max(kinds, key=kinds.get)
            if kinds[kind] >= used / 2:
                row["shape"] = kind
        rows.append(row)
    rows.sort(key=lambda r: -r["bytes"])
    return {"path": root, "items": rows[:top], "partial": partial, "secs": round(clock() - started, 1)}


def gpus():
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        res = subprocess.run([exe, "--query-gpu=index,utilization.gpu,memory.used,memory.total,temperature.gpu",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in res.stdout.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 5:
            rows.append({"i": int_or(parts[0]), "util": int_or(parts[1]), "mem_used": (int_or(parts[2]) or 0) << 20,
                         "mem_total": (int_or(parts[3]) or 0) << 20, "temp": int_or(parts[4])})
    return rows


def read_environ_marker(pid, timeout=0.5):
    """The basename of the process's COVEY_TASK_DIR, read in a thread with a deadline (reading environ waits on
    the process's memory map, which a runaway process can hold)."""
    box = {}

    def work():
        try:
            with open("/proc/%d/environ" % pid, "rb") as handle:
                data = handle.read(1 << 18)
        except OSError:
            return
        for item in data.split(b"\0"):
            for prefix in TASK_ENVS:
                if item.startswith(prefix):
                    box["task"] = os.path.basename(item[len(prefix):].decode("utf-8", "replace").rstrip("/"))
                    return

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    thread.join(timeout)
    return box.get("task")


def processes_linux():
    out = []
    try:
        names = os.listdir("/proc")
    except OSError:
        return out
    for name in names:
        if not name.isdigit():
            continue
        raw = read_text("/proc/%s/stat" % name, 4096)
        if not raw:
            continue
        try:
            comm = raw[raw.index("(") + 1:raw.rindex(")")]
            f = raw[raw.rindex(")") + 2:].split()
            out.append({"pid": int(name), "comm": comm, "ppid": int(f[1]), "ticks": int(f[11]) + int(f[12]),
                        "start": int(f[19]), "rss": int(f[21]) * PAGE})
        except (ValueError, IndexError):
            continue
    return out


def processes_ps():
    try:
        res = subprocess.run(["ps", "-axo", "pid=,ppid=,rss=,time=,comm="], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return []
    out = []
    for line in res.stdout.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        seconds = 0.0
        for piece in parts[3].replace("-", ":").split(":"):
            try:
                seconds = seconds * 60 + float(piece)
            except ValueError:
                pass
        out.append({"pid": int_or(parts[0]), "ppid": int_or(parts[1]), "rss": (int_or(parts[2]) or 0) * 1024,
                    "ticks": int(seconds * HZ), "start": 0, "comm": os.path.basename(parts[4])})
    return out


class Sampler:
    """One sample a call; keeps what it needs between calls (CPU counters, per-process ticks, the burst state)."""

    def __init__(self, paths=(), probe=(), clock=time.time):
        self.paths, self.probe_paths, self.clock = list(paths), list(probe), clock
        self.last_cpu = None
        self.last_ticks = {}
        self.history = []  # (time, used) for GROWTH_WINDOW.
        self.burst_until = 0.0
        self.last_oom = None
        self.gpu_rows, self.gpu_at = None, 0.0
        self.probes, self.probe_at, self.probe_busy = {}, 0.0, set()
        self.linux = os.path.isdir("/proc/self")
        self.markers = {}  # (pid, start) -> task key or "", read once a process.
        self.boot = boot_id()
        self.written, self.writers, self.writers_at = {}, [], None  # (pid, start) -> write_bytes at writers_at.
        self.used = {}  # path -> [(time, used)], one point a GROWTH_EVERY.
        self.dirs, self.dirs_at, self.dirs_busy = {}, {}, set()  # By device: the last count, when, running.

    def interval(self):
        return BURST if self.clock() < self.burst_until else INTERVAL

    def sample(self):
        now = self.clock()
        out = {"t": round(now, 3), "proto": PROTO, "boot": self.boot}
        mem = memory()
        out["mem"] = mem
        out["mem"]["psi"] = pressure("memory")
        usage, cores, throttled = cpu_counters()
        cpu = {"cores": round(cores, 2), "load": list(os.getloadavg()) if hasattr(os, "getloadavg") else None,
               "psi": pressure("cpu")}
        if self.last_cpu and usage is not None:
            t0, u0, th0 = self.last_cpu
            span = max(now - t0, 1e-6)
            cpu["used"] = round(max(0.0, usage - u0) / span, 2)
            cpu["pct"] = round(100.0 * cpu["used"] / cores, 1) if cores else None
            if throttled is not None and th0 is not None:
                cpu["throttled_pct"] = round(100.0 * max(0.0, throttled - th0) / span, 1)
        self.last_cpu = (now, usage, throttled) if usage is not None else None
        out["cpu"] = cpu
        out["io"] = {"psi": pressure("io")}
        out["disk"] = disks(self.paths)
        self.probe(now, out["disk"])
        self.disk_extras(now, out["disk"])
        if now - self.gpu_at >= GPU_EVERY:
            self.gpu_rows, self.gpu_at = gpus(), now
        if self.gpu_rows is not None:
            out["gpu"] = self.gpu_rows
        out["procs"] = self.top(now)
        if self.writers:
            out["writers"] = self.writers
        self.update_burst(now, mem)
        out["burst"] = now < self.burst_until
        return out

    def top(self, now):
        rows = processes_linux() if self.linux else processes_ps()
        ticks = {}
        for row in rows:
            key = (row["pid"], row["start"])
            ticks[key] = (now, row["ticks"])
            before = self.last_ticks.get(key)
            row["cpu"] = round(100.0 * (row["ticks"] - before[1]) / HZ / max(now - before[0], 1e-6), 1) if before else 0.0
        self.last_ticks = ticks
        if self.linux and (self.writers_at is None or now - self.writers_at >= WRITERS_EVERY):
            self.count_writes(now, rows)
        chosen = {}
        for row in sorted(rows, key=lambda r: -r["rss"])[:TOP_RSS]:
            chosen[row["pid"]] = row
        for row in sorted(rows, key=lambda r: -r["cpu"])[:TOP_CPU]:
            chosen[row["pid"]] = row
        out = []
        for row in sorted(chosen.values(), key=lambda r: -r["rss"]):
            item = {"pid": row["pid"], "ppid": row["ppid"], "comm": row["comm"][:40], "rss": row["rss"], "cpu": row["cpu"]}
            if self.linux and self.task_of(row):
                item["task"] = self.task_of(row)
            out.append(item)
        if len(self.markers) > 4 * (TOP_RSS + TOP_CPU):
            alive = {(r["pid"], r["start"]) for r in rows}
            self.markers = {k: v for k, v in self.markers.items() if k in alive}
        return out

    def task_of(self, row):
        key = (row["pid"], row["start"])
        if key not in self.markers:
            self.markers[key] = read_environ_marker(row["pid"]) or ""
        return self.markers[key]

    def count_writes(self, now, rows):
        """``writers``: the processes that wrote the most since the last count, WRITERS_MIN at least (``write_bytes``
        of ``/proc/<pid>/io``, readable for this user's processes; it counts every disk, a network one too), with the
        seconds it covers."""
        written, grew = {}, []
        for row in rows:
            text = read_text("/proc/%d/io" % row["pid"], 4096)
            value = parse_kv(text or "").get("write_bytes:")
            if not isinstance(value, int):
                continue
            key = (row["pid"], row["start"])
            written[key] = value
            if key in self.written and value - self.written[key] >= WRITERS_MIN:
                grew.append((value - self.written[key], row))
        span = round(now - self.writers_at) if self.writers_at is not None else 0
        self.writers = []
        for amount, row in sorted(grew, key=lambda g: -g[0])[:TOP_WRITERS]:
            item = {"pid": row["pid"], "comm": row["comm"][:40], "bytes": amount, "secs": span}
            task = self.task_of(row)
            if task:
                item["task"] = task
            self.writers.append(item)
        self.written, self.writers_at = written, now

    def disk_extras(self, now, rows):
        """On each disk DIRS_AT full or more: ``growth`` (bytes in each of GROWTH_SPANS, over the seconds the points
        cover) and ``dirs`` (``biggest``, counted once a device and DIRS_EVERY at most, in a thread)."""
        devices = {}
        for row in rows:
            total, free = row.get("total"), row.get("free")
            if not isinstance(total, int) or not isinstance(free, int):
                continue
            used = total - free
            points = self.used.setdefault(row["path"], [])
            if not points or now - points[-1][0] >= GROWTH_EVERY:
                points.append((now, used))
                del points[:-(max(GROWTH_SPANS) // int(GROWTH_EVERY) + 2)]
            if not isinstance(row.get("pct"), (int, float)) or row["pct"] < DIRS_AT:
                continue
            growth = []
            for span in GROWTH_SPANS:
                base = next((p for p in points if now - p[0] <= span + GROWTH_EVERY / 2), None)
                if base and now - base[0] >= span / 2:
                    growth.append({"secs": round(now - base[0]), "bytes": used - base[1]})
            if growth:
                row["growth"] = growth
            try:
                dev = os.stat(row["path"]).st_dev
            except OSError:
                continue
            devices.setdefault(dev, []).append(row)
        for dev, same in devices.items():
            root = min((r["path"] for r in same), key=len)
            last = self.dirs_at.get(dev)
            if dev not in self.dirs_busy and (last is None or now - last >= DIRS_EVERY):
                self.dirs_at[dev] = now
                self.dirs_busy.add(dev)
                threading.Thread(target=self._count_dirs, args=(dev, root, now), daemon=True).start()
            if dev in self.dirs:
                for row in same:
                    row["dirs"] = self.dirs[dev]

    def _count_dirs(self, dev, root, now):
        try:
            result = biggest(root)
            result["at"] = round(now, 3)
            self.dirs[dev] = result
        finally:
            self.dirs_busy.discard(dev)

    def update_burst(self, now, mem):
        used, limit = mem.get("used"), mem.get("limit")
        if used is None or not limit:
            return
        self.history = [(t, u) for t, u in self.history if now - t <= GROWTH_WINDOW] + [(now, used)]
        grew = used - min(u for _, u in self.history)
        oom = (mem.get("oom_kill") or 0, mem.get("failcnt") or 0)
        bumped = self.last_oom is not None and oom > self.last_oom
        self.last_oom = oom
        if used >= BURST_RATIO * limit or grew >= GROWTH_FRACTION * limit or bumped:
            self.burst_until = now + BURST_HOLD

    def probe(self, now, rows):
        """A write probe of each ``probe`` path every PROBE_EVERY seconds, each in its own thread: a hung disk keeps
        its thread and is reported ``hung`` instead of being probed again."""
        if self.probe_paths and now - self.probe_at >= PROBE_EVERY:
            self.probe_at = now
            for name in self.probe_paths:
                if name in self.probe_busy:
                    self.probes[name] = {"ok": False, "error": "hung", "at": now}
                    continue
                self.probe_busy.add(name)
                threading.Thread(target=self._probe_one, args=(name,), daemon=True).start()
        by_path = {row["path"]: row for row in rows}
        for name, result in self.probes.items():
            row = by_path.get(name)
            if row is None:
                row = {"path": name}
                rows.append(row)
            row["write"] = result

    def _probe_one(self, name):
        target = os.path.join(name, ".covey-node-probe-%d" % os.getpid())
        started = time.time()
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, b"x")
                os.fsync(fd)
            finally:
                os.close(fd)
            os.unlink(target)
            result = {"ok": True}
        except OSError as exc:
            result = {"ok": False, "error": exc.strerror or type(exc).__name__}
        result["ms"] = round((time.time() - started) * 1000)
        result["at"] = round(started, 3)
        self.probes[name] = result
        self.probe_busy.discard(name)


# ---------------------------------------------------------------- the local ring

class Ring:
    """The samples on local disk: ``seg-<first seq>.jsonl`` files, the newest being written."""

    def __init__(self, folder, segment=SEGMENT, total=RING, keep_days=KEEP_DAYS):
        self.folder, self.segment, self.total, self.keep = folder, segment, total, keep_days * 86400
        os.makedirs(folder, mode=0o700, exist_ok=True)
        self.lock = threading.Lock()
        self.seq = self._last_seq()
        self.recent = []  # The newest samples, so the link reads no file while it keeps up.

    def _segments(self):
        out = []
        for name in os.listdir(self.folder):
            if name.startswith("seg-") and name.endswith(".jsonl"):
                first = int_or(name[4:-6])
                if first is not None:
                    out.append((first, os.path.join(self.folder, name)))
        return sorted(out)

    def _last_seq(self):
        segments = self._segments()
        if not segments:
            return 0
        last = segments[-1][0] - 1
        try:
            with open(segments[-1][1], "rb") as handle:
                for line in handle:
                    try:
                        last = max(last, int(json.loads(line)["seq"]))
                    except (ValueError, KeyError, TypeError):
                        continue
        except OSError:
            pass
        return last

    def append(self, sample):
        with self.lock:
            self.seq += 1
            sample = dict(sample, seq=self.seq)
            segments = self._segments()
            name = segments[-1][1] if segments else None
            if name is None or os.path.getsize(name) >= self.segment:
                name = os.path.join(self.folder, "seg-%d.jsonl" % self.seq)
            with open(name, "a") as handle:
                handle.write(json.dumps(sample, separators=(",", ":")) + "\n")
            self.recent = (self.recent + [sample])[-RECENT:]
            self._trim()
            return sample

    def _trim(self):
        segments = self._segments()
        now = time.time()
        sizes = [os.path.getsize(p) for _, p in segments]
        while len(segments) > 1 and (sum(sizes) > self.total or now - os.path.getmtime(segments[0][1]) > self.keep):
            try:
                os.unlink(segments[0][1])
            except OSError:
                pass
            segments.pop(0)
            sizes.pop(0)

    def since(self, seq, limit=BACKFILL_MAX):
        """Samples after ``seq``, oldest first, at most the newest ``limit``."""
        if seq >= self.seq:
            return []
        recent = self.recent
        if recent and recent[0]["seq"] <= seq + 1:
            return [row for row in recent if row["seq"] > seq][-limit:]
        found = []
        segments = self._segments()
        for i, (first, name) in enumerate(segments):
            nxt = segments[i + 1][0] if i + 1 < len(segments) else None
            if nxt is not None and nxt <= seq + 1:
                continue
            try:
                with open(name, "rb") as handle:
                    for line in handle:
                        try:
                            row = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(row, dict) and int(row.get("seq", 0)) > seq:
                            found.append(row)
            except OSError:
                continue
        return found[-limit:]


def wire_sample(sample):
    """What goes to the hub: the sample with fewer processes."""
    out = dict(sample)
    out["procs"] = (sample.get("procs") or [])[:WIRE_PROCS]
    out["k"] = "s"
    return out


# ---------------------------------------------------------------- the link

def tls_connect(hub, pin, timeout=CONNECT_TIMEOUT):
    """A TLS socket to ``hub`` ("host:port", "[v6]:port") whose certificate's DER hashes to ``pin``."""
    host, port = split_hub(hub)
    raw = socket.create_connection((host, port), timeout=timeout)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # Pinned below: a self-signed hub certificate, its hash carried by the join.
    sock = ctx.wrap_socket(raw)
    der = sock.getpeercert(binary_form=True) or b""
    if hashlib.sha256(der).hexdigest() != pin.replace(":", "").lower():
        sock.close()
        raise ssl.SSLError("the hub's certificate is not the pinned one")
    return sock


def split_hub(hub):
    hub = hub.strip()
    if hub.startswith("["):
        host, _, rest = hub[1:].partition("]")
        return host, int(rest.lstrip(":"))
    host, _, port = hub.rpartition(":")
    return host, int(port)


class Lines:
    def __init__(self, sock):
        self.sock, self.buf = sock, b""

    def send(self, obj):
        self.sock.sendall((json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8"))

    def read(self, timeout):
        self.sock.settimeout(max(timeout, 0.01))  # A zero timeout makes an SSL socket raise SSLWantReadError.
        while b"\n" not in self.buf:
            try:
                chunk = self.sock.recv(65536)
            except (socket.timeout, ssl.SSLWantReadError):
                return None
            if not chunk:
                raise EOFError("closed")
            self.buf += chunk
            if len(self.buf) > LINE_MAX:
                raise ValueError("line too long")
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line.decode("utf-8"))


def host_info():
    return {"hostname": socket.gethostname(), "os": platform.system(), "release": platform.release(),
            "machine": platform.machine(), "python": list(sys.version_info[:3]), "cpus": os.cpu_count(),
            "boot": boot_id(), "user": getpass.getuser(), "home": HOME}


class Link:
    """The connection to the hub, in its own thread: hello, backfill, then each new sample."""

    def __init__(self, config, ring, on_revoke, on_upgrade):
        self.config, self.ring, self.on_revoke, self.on_upgrade = config, ring, on_revoke, on_upgrade
        self.cond = threading.Condition()
        self.stopping = False
        self.status = {"connected": False}

    def notify(self):
        with self.cond:
            self.cond.notify_all()

    def run(self):
        delay = BACKOFF
        while not self.stopping:
            started = time.time()
            try:
                self.session()
            except Exception as exc:  # noqa: BLE001 - any failure means: dial again later
                self.status = {"connected": False, "error": "%s: %s" % (type(exc).__name__, str(exc)[:200]),
                               "at": time.time()}
                log("link: %s" % self.status["error"])
            if time.time() - started > 60:
                delay = BACKOFF
            time.sleep(delay)
            delay = min(delay * 2, BACKOFF_MAX)

    def session(self):
        sock = tls_connect(self.config["hub"], self.config["pin"])
        lines = Lines(sock)
        try:
            lines.send({"k": "hello", "proto": PROTO, "alias": self.config["alias"], "key": self.config["key"],
                        "version": version(), "info": host_info(), "last": self.ring.seq})
            welcome = lines.read(30)
            if not isinstance(welcome, dict) or welcome.get("k") != "welcome":
                if isinstance(welcome, dict) and welcome.get("k") == "revoke":
                    self.on_revoke()
                raise ValueError("no welcome: %r" % (welcome,))
            if welcome.get("version") and welcome["version"] != version():
                self.on_upgrade(welcome["version"])
            sent = int(welcome.get("ack") or 0)
            self.status = {"connected": True, "since": time.time(), "peer": self.config["hub"]}
            log("link: connected, hub has seq %d of %d" % (sent, self.ring.seq))
            last_heard = time.time()
            while not self.stopping:
                for row in self.ring.since(sent):
                    lines.send(wire_sample(row))
                    sent = row["seq"]
                msg = lines.read(0.2)
                while msg is not None:
                    last_heard = time.time()
                    if msg.get("k") == "revoke":
                        self.on_revoke()
                        return
                    msg = lines.read(0)
                if time.time() - last_heard > ACK_IDLE:
                    raise TimeoutError("the hub said nothing for %ds" % ACK_IDLE)
                with self.cond:
                    if self.ring.seq <= sent:
                        self.cond.wait(2.0)
        finally:
            sock.close()


# ---------------------------------------------------------------- the daemon

def take_lock():
    os.makedirs(ROOT, mode=0o700, exist_ok=True)
    handle = open(path("lock"), "a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def running():
    """Whether a daemon holds the lock (a lock we can take means none does)."""
    if not os.path.exists(path("lock")):
        return False
    handle = open(path("lock"), "a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    finally:
        handle.close()
    return False


def cmd_run(args):
    held = take_lock()
    if held is None:
        print(json.dumps({"ok": True, "running": "already"}))
        return 0
    config = read_json(path("config.json"))
    ring = Ring(path("data"))
    sampler = Sampler(config.get("paths") or [HOME, "/"], config.get("probe") or [])
    state = {"pid": os.getpid(), "started": time.time(), "version": version(), "boot": boot_id()}
    stop = threading.Event()

    def revoke():
        log("revoked by the hub: uninstalling")
        remove_autostart()
        write_json(path("revoked.json"), {"at": time.time()})
        stop.set()

    def upgrade(wanted):
        if fetch_version(config, wanted):
            log("upgraded to %s: starting again" % wanted)
            stop.set()
            state["restart"] = True

    link = Link(config, ring, revoke, upgrade) if config.get("hub") else None
    if link:
        threading.Thread(target=link.run, name="link", daemon=True).start()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    log("node started version %s pid %d" % (version(), os.getpid()))
    while not stop.is_set():
        try:
            ring.append(sampler.sample())
        except Exception as exc:  # noqa: BLE001 - a sample that fails is logged, the next is tried
            log("sample failed: %s: %s" % (type(exc).__name__, exc))
        if link:
            link.notify()
            state["link"] = link.status
        state["seq"], state["at"] = ring.seq, time.time()
        try:
            write_json(path("state.json"), state)
        except OSError:
            pass
        stop.wait(sampler.interval())
    if link:
        link.stopping = True
    held.close()
    if os.path.exists(path("revoked.json")):
        shutil.rmtree(ROOT, ignore_errors=True)
        return 0
    if state.get("restart"):
        os.execv(sys.executable, [sys.executable, path("node.py"), "run"])
    return 0


def fetch_version(config, wanted):
    """The hub's node.py of version ``wanted`` over the pinned TLS (GET /node.py), written in place."""
    try:
        sock = tls_connect(config["hub"], config["pin"])
        sock.sendall(b"GET /node.py HTTP/1.0\r\n\r\n")
        data = b""
        while True:
            chunk = sock.recv(1 << 16)
            if not chunk:
                break
            data += chunk
        sock.close()
        body = data.split(b"\r\n\r\n", 1)[1]
    except (OSError, IndexError, ssl.SSLError) as exc:
        log("upgrade failed: %s" % exc)
        return False
    if hashlib.sha256(body).hexdigest()[:16] != wanted:
        log("upgrade refused: the file is not version %s" % wanted)
        return False
    tmp = path("node.py.new")
    with open(tmp, "wb") as handle:
        handle.write(body)
    os.replace(tmp, path("node.py"))
    return True


def start_detached():
    """``run`` in a new session, detached from the caller (a login shell, an ssh command)."""
    if running():
        return "running"
    if not os.path.exists(path("config.json")):
        return "not_installed"
    pid = os.fork()
    if pid:
        os.waitpid(pid, 0)
        return "started"
    os.setsid()
    if os.fork():
        os._exit(0)
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(devnull, fd)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("COVEY_TASK", "LARK_BRIDGE_TASK", "CLAUDECODE"))}
    try:
        os.execve(sys.executable, [sys.executable, path("node.py"), "run"], env)
    finally:
        os._exit(1)


def cmd_ensure(args):
    print(json.dumps({"ok": True, "outcome": start_detached()}))
    return 0


def hook_text():
    exe = sys.executable
    return ("%s\n[ -f %s ] && (%s %s ensure >/dev/null 2>&1 &)\n%s\n"
            % (HOOK_BEGIN, shell_quote(path("node.py")), shell_quote(exe), shell_quote(path("node.py")), HOOK_END))


def shell_quote(text):
    return "'" + text.replace("'", "'\\''") + "'"


def install_autostart():
    """Every way this machine has: the login hook at the top of ~/.bashrc (and ~/.zshrc on a Mac), a systemd user
    unit where systemd answers, a launchd agent on macOS. Returns the ones written."""
    done = []
    for rc in (".bashrc", ".zshrc") if platform.system() == "Darwin" else (".bashrc",):
        name = os.path.join(HOME, rc)
        try:
            text = open(name).read() if os.path.exists(name) else ""
        except OSError:
            continue
        text = strip_hook(text)
        try:
            with open(name + ".covey-tmp", "w") as handle:
                handle.write(hook_text() + text)
            os.replace(name + ".covey-tmp", name)
            done.append(rc)
        except OSError:
            pass
    if os.environ.get("COVEY_NODE_AUTOSTART") == "hook":
        return done  # The login hook alone (tests: a temporary HOME must not reach the user's service manager).
    if platform.system() == "Darwin":
        agents = os.path.join(HOME, "Library", "LaunchAgents")
        os.makedirs(agents, exist_ok=True)
        plist = os.path.join(agents, LAUNCHD_LABEL + ".plist")
        with open(plist, "w") as handle:
            handle.write(PLIST % {"label": LAUNCHD_LABEL, "python": sys.executable, "node": path("node.py")})
        subprocess.run(["launchctl", "load", "-w", plist], capture_output=True, timeout=10)
        done.append("launchd")
    elif shutil.which("systemctl"):
        try:
            ok = subprocess.run(["systemctl", "--user", "is-system-running"], capture_output=True, text=True, timeout=5)
            if ok.stdout.strip() in ("running", "degraded", "starting"):
                unit_dir = os.path.join(HOME, ".config", "systemd", "user")
                os.makedirs(unit_dir, exist_ok=True)
                with open(os.path.join(unit_dir, "covey-node.service"), "w") as handle:
                    handle.write(UNIT % {"python": sys.executable, "node": path("node.py")})
                enabled = subprocess.run(["systemctl", "--user", "enable", "covey-node.service"],
                                         capture_output=True, timeout=10)
                if enabled.returncode == 0:
                    done.append("systemd")
        except (OSError, subprocess.SubprocessError):
            pass
    return done


def strip_hook(text):
    start, end = text.find(HOOK_BEGIN), text.find(HOOK_END)
    if start < 0 or end < start:
        return text
    return text[:start] + text[end + len(HOOK_END):].lstrip("\n")


def remove_autostart():
    for rc in (".bashrc", ".zshrc"):
        name = os.path.join(HOME, rc)
        try:
            text = open(name).read()
        except OSError:
            continue
        if HOOK_BEGIN in text:
            with open(name + ".covey-tmp", "w") as handle:
                handle.write(strip_hook(text))
            os.replace(name + ".covey-tmp", name)
    plist = os.path.join(HOME, "Library", "LaunchAgents", LAUNCHD_LABEL + ".plist")
    if os.path.exists(plist):
        subprocess.run(["launchctl", "unload", "-w", plist], capture_output=True, timeout=10)
        os.unlink(plist)
    unit = os.path.join(HOME, ".config", "systemd", "user", "covey-node.service")
    if os.path.exists(unit):
        subprocess.run(["systemctl", "--user", "disable", "covey-node.service"], capture_output=True, timeout=10)
        os.unlink(unit)


PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>%(label)s</string>
<key>ProgramArguments</key><array><string>%(python)s</string><string>%(node)s</string><string>run</string></array>
<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
</dict></plist>
"""

UNIT = """[Unit]
Description=Covey node
[Service]
ExecStart=%(python)s %(node)s run
Restart=always
RestartSec=10
[Install]
WantedBy=default.target
"""


def cmd_join(args):
    """``join <hub> <pin> <token> [title]``: this device's key from the hub, then config, autostart and the daemon."""
    if len(args) < 3:
        print(json.dumps({"ok": False, "err": "usage", "msg": "join <hub> <pin> <token> [title]"}))
        return 2
    hub, pin, token = args[0], args[1], args[2]
    os.makedirs(ROOT, mode=0o700, exist_ok=True)
    try:
        sock = tls_connect(hub, pin)
        lines = Lines(sock)
        lines.send({"k": "join", "proto": PROTO, "token": token, "info": host_info(),
                    "title": args[3] if len(args) > 3 else ""})
        answer = lines.read(30)
        sock.close()
    except (OSError, ValueError, EOFError, ssl.SSLError) as exc:
        print(json.dumps({"ok": False, "err": "link", "msg": "%s: %s" % (type(exc).__name__, exc)}))
        return 1
    if not isinstance(answer, dict) or answer.get("k") != "joined":
        print(json.dumps({"ok": False, "err": (answer or {}).get("err", "refused")}))
        return 1
    me = os.path.abspath(__file__)
    if me != path("node.py"):
        shutil.copyfile(me, path("node.py"))
    config = {"hub": hub, "pin": pin, "alias": answer["alias"], "key": answer["key"], "hub_id": answer.get("hub_id", ""),
              "paths": answer.get("paths") or [HOME, "/"], "probe": answer.get("probe") or []}
    write_json(path("config.json"), config)
    autostart = install_autostart() if answer.get("autostart", True) else []
    outcome = start_detached()
    print(json.dumps({"ok": True, "alias": answer["alias"], "autostart": autostart, "outcome": outcome}))
    return 0


def cmd_status(args):
    state = read_json(path("state.json"))
    config = read_json(path("config.json"))
    print(json.dumps({"ok": True, "installed": bool(config), "alias": config.get("alias", ""), "hub": config.get("hub", ""),
                      "running": running(), "version": version(), "state": state}))
    return 0


def cmd_uninstall(args):
    remove_autostart()
    state = read_json(path("state.json"))
    pid = state.get("pid")
    if pid and running():
        try:
            os.kill(int(pid), signal.SIGTERM)
        except OSError:
            pass
        for _ in range(50):
            if not running():
                break
            time.sleep(0.1)
    shutil.rmtree(ROOT, ignore_errors=True)
    print(json.dumps({"ok": True}))
    return 0


COMMANDS = {"join": cmd_join, "run": cmd_run, "ensure": cmd_ensure, "status": cmd_status, "uninstall": cmd_uninstall}


def main(argv):
    if len(argv) < 2 or argv[1] not in COMMANDS:
        print(json.dumps({"ok": False, "err": "usage", "msg": "commands: " + ", ".join(sorted(COMMANDS))}))
        return 2
    return COMMANDS[argv[1]](argv[2:])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
