"""The silent-failure check for a turn on a device (A-SPEC 6.2).

A hub path used on a device's behalf does not fail loudly everywhere: ``git`` in a directory that does not exist says
"no changes", ``stat`` says "no such artifact", ``open`` of a stand-in under the hub's state directory raises only
where somebody reads it. ``DeviceGuard`` watches a whole turn and records what it did to the hub's file system and
processes, so a test can assert that none of it touched the stand-in (``<state>/devices/<alias>/standin``) and that
nothing of the hub reached the device.

    with DeviceGuard(standin_root) as guard:
        ...run the turn...
    guard.assert_untouched()

What is recorded (by every thread, for as long as the guard is open):
- ``open`` (an audit event: ``open``, ``Path.open``, ``read_text`` ...), ``os.scandir``/``os.listdir`` and ``subprocess``
  (``subprocess.Popen``: executable, argv, cwd) through ``sys.addaudithook``, which cannot be removed, so one hook
  stays installed and does nothing while no guard is open;
- ``os.stat`` (``Path.is_dir``, ``exists`` ... end there; they raise no audit event) and ``os.lstat``, by pass-through
  wrappers installed for the guard's lifetime. A call made by ``Path.resolve`` or ``os.path.realpath`` is kept apart
  (``resolved``): the session keys of core/session_guard.py and core/saved_results.py resolve the stand-in on purpose
  (it does not exist, and resolving it is harmless); every other ``stat`` is a use;
- calls of named functions (``watch``: ``capture_job_diff_snapshot``, ``finalize_job_diff``), counted and passed through.
"""
from __future__ import annotations

import contextlib
import os
import pathlib
import shlex
import sys
import threading
from pathlib import Path
from unittest import mock

_lock = threading.Lock()
_active: list = []
_installed = False


def _hook(event, args):
    if not _active or event not in ("open", "subprocess.Popen", "os.scandir", "os.listdir", "os.chdir"):
        return
    with _lock:
        guards = list(_active)
    for guard in guards:
        guard._record(event, args)


def _install():
    global _installed
    with _lock:
        if _installed:
            return
        _installed = True
    sys.addaudithook(_hook)


def text(value):
    """``value`` as text when it names a path (a str, bytes or PathLike); '' for a file descriptor and the rest."""
    if isinstance(value, bytes):
        return os.fsdecode(value)
    if isinstance(value, (str, os.PathLike)):
        return os.fspath(value) if isinstance(value, os.PathLike) else value
    return ""


class DeviceGuard:
    """Records, from ``__enter__`` to ``__exit__``, what the process does to ``standin`` (the hub's stand-in directories
    of devices, ``<state>/devices``) and every subprocess it starts."""

    def __init__(self, standin, *, device_root=None):
        """``standin``: the stand-in root. ``device_root``: where a fake device keeps its files on this machine; the
        hub touching a path under it would answer right here and wrongly on a real device."""
        self.standin = str(standin)
        self.device_root = str(device_root) if device_root else ""
        self.resolved: list[str] = []
        self.opened: list[str] = []
        self.stats: list[str] = []
        self.listed: list[str] = []
        self.spawned: list[dict] = []
        self.calls: dict[str, int] = {}
        self._stack = None

    # ---- recording
    def _record(self, event, args):
        if event == "open":
            self.opened.append(text(args[0]))
        elif event in ("os.scandir", "os.listdir"):
            self.listed.append(text(args[0]))
        elif event == "subprocess.Popen":
            executable, argv, cwd, _ = args
            self.spawned.append({"executable": text(executable), "argv": [text(item) for item in argv or ()],
                                 "cwd": text(cwd)})

    def __enter__(self):
        _install()
        self._stack = contextlib.ExitStack()
        real_stat, real_lstat = os.stat, os.lstat

        def note(path):
            frame, depth = sys._getframe(2), 0
            while frame is not None and depth < 6:
                if frame.f_code.co_name in ("resolve", "realpath"):
                    self.resolved.append(text(path))
                    return
                frame, depth = frame.f_back, depth + 1
            self.stats.append(text(path))

        def stat(path, *args, **kwargs):
            note(path)
            return real_stat(path, *args, **kwargs)

        def lstat(path, *args, **kwargs):
            note(path)
            return real_lstat(path, *args, **kwargs)

        self._stack.enter_context(mock.patch.object(os, "stat", stat))
        self._stack.enter_context(mock.patch.object(os, "lstat", lstat))
        accessor = getattr(pathlib, "_NormalAccessor", None)
        if accessor is not None:  # Python 3.10: pathlib took os.stat when imported, and Path.is_dir() goes there.
            self._stack.enter_context(mock.patch.object(accessor, "stat", staticmethod(stat)))
            self._stack.enter_context(mock.patch.object(accessor, "lstat", staticmethod(lstat)))
        with _lock:
            _active.append(self)
        return self

    def __exit__(self, *exc):
        with _lock:
            if self in _active:
                _active.remove(self)
        self._stack.close()
        return False

    def watch(self, module, name):
        """Count the calls of ``module.name`` (and let them through) while the guard is open."""
        real = getattr(module, name)

        def counted(*args, **kwargs):
            self.calls[name] = self.calls.get(name, 0) + 1
            return real(*args, **kwargs)

        self._stack.enter_context(mock.patch.object(module, name, counted))
        self.calls.setdefault(name, 0)

    # ---- what a test asks
    @staticmethod
    def beneath(root, paths):
        root = root.rstrip("/")
        return sorted({path for path in paths if root and (path == root or path.startswith(root + "/"))})

    def under(self, paths):
        """The recorded ``paths`` that are in the stand-in's tree."""
        return self.beneath(self.standin, paths)

    def standin_touched(self):
        """Everything done to the stand-in: ``{"open": [...], "stat": [...], "list": [...], "subprocess": [...]}``; only
        the non-empty kinds. A subprocess counts when its cwd, its executable or an argument is in the stand-in."""
        found = {"open": self.under(self.opened), "stat": self.under(self.stats),
                 "list": self.under(self.listed),
                 "subprocess": [shlex.join(item["argv"]) + " (cwd %s)" % item["cwd"] for item in self.spawned
                                if self.under([item["cwd"], item["executable"], *item["argv"]])]}
        return {kind: items for kind, items in found.items() if items}

    def device_files_touched(self):
        """What the hub itself opened, listed or stat-ed under the fake device's files (never a thing it does: the
        device's paths are the device's)."""
        root = self.device_root
        found = {"open": self.beneath(root, self.opened), "stat": self.beneath(root, self.stats),
                 "list": self.beneath(root, self.listed),
                 "cwd": self.beneath(root, [item["cwd"] for item in self.spawned])}
        return {kind: items for kind, items in found.items() if items}

    def git_in_standin(self):
        """Every ``git`` process started with the stand-in as its cwd or ``-C`` directory."""
        found = []
        for item in self.spawned:
            if Path(item["executable"] or (item["argv"] or [""])[0]).name != "git" \
                    and Path((item["argv"] or [""])[0]).name != "git":
                continue
            args = item["argv"]
            where = [item["cwd"]] + [args[i + 1] for i, arg in enumerate(args[:-1]) if arg == "-C"]
            if self.under(where):
                found.append(shlex.join(args))
        return found

    def git_runs(self):
        """Every ``git`` process the turn started, in any directory (a turn on a device starts none)."""
        return [shlex.join(item["argv"]) for item in self.spawned if Path((item["argv"] or [""])[0]).name == "git"]

    def assert_untouched(self, case):
        """Fail ``case`` when the stand-in was opened, listed, stat-ed or used as the directory of a process."""
        case.assertEqual(self.standin_touched(), {}, "the hub's stand-in directory was used")
        case.assertEqual(self.git_in_standin(), [])
        if self.device_root:
            case.assertEqual(self.device_files_touched(), {}, "the hub used a path of the device itself")
