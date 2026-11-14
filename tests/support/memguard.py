"""The memory watchdog tests' shared case (test_memguard, test_memguard_supervisor): a temp dir with mgt-*
links to the interpreter, the processes a test started (killed afterwards) and a config whose rules only see
mgt-* processes."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest

from support import exec_tempdir

from covey.service import memguard

GUARD = str(Path(memguard.__file__).resolve())
# Forks a child whose stdout is a fresh pipe, optionally keeps a reader on the pipe, then exits so the child
# is adopted by PID 1 exactly like the rg left behind on 2026-11-03.
SPAWN = r'''
import json, os, sys
mode, prog, code, *args = sys.argv[1:]
r, w = os.pipe()
pid = os.fork()
if pid == 0:
    os.setsid()
    os.dup2(w, 1)
    if mode == 'file':
        fd = os.open(args.pop(0), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.dup2(fd, 1)
        os.close(fd)
    os.close(r)
    os.close(w)
    os.execv(prog, [prog, '-c', code] + args)
reader = 0
if mode in ('reader', 'chain'):
    reader = os.fork()
    if reader == 0:
        os.setsid()
        os.dup2(r, 0)
        nul = os.open('/dev/null', os.O_WRONLY)
        os.dup2(nul, 1)
        os.close(nul)
        os.close(r)
        os.close(w)
        if mode == 'reader':
            os.execv('/bin/sleep', ['sleep', '60'])
        os.execv(prog, [prog, '-c', 'import time; time.sleep(60)'])
os.close(r)
os.close(w)
print(json.dumps({'pid': pid, 'reader': reader}), flush=True)
'''



def alive(pid):
    st = memguard.proc_stat(pid)
    return st is not None and st['state'] != 'Z'


def kill_quiet(pid):
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, TypeError):
        pass



class GuardCase(unittest.TestCase):
    def setUp(self):
        self.bin = exec_tempdir()
        self.addCleanup(self.bin.cleanup)
        self.run_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.run_dir, True)
        for name in ('mgt-rg', 'mgt-find', 'mgt-bigpy'):
            (Path(self.bin.name) / name).symlink_to(sys.executable)
        self.pids, self.children = [], []
        self.addCleanup(self.stop_all)

    def stop_all(self):
        for pid in self.pids:
            kill_quiet(pid)
        for proc in self.children:
            kill_quiet(proc.pid)
            proc.wait(10)

    def prog(self, name):
        return str(Path(self.bin.name) / name)

    def config(self, **settings):
        data = self.run_dir / 'data'
        data.mkdir(exist_ok=True)
        cfg = {'owner': memguard.OWNER, 'enabled': True, 'data_dir': str(data), 'interval_seconds': 0.5,
               'search_names': ['mgt-rg', 'mgt-find'], 'find_names': ['mgt-find'],
               'only_comms': ['mgt-rg', 'mgt-find', 'mgt-bigpy'], 'kill_enabled': True,
               'search_kill_mib': 60, 'orphan_min_age_seconds': 2, 'search_long_run_seconds': 4,
               'alert_levels_mib': [100, 1000], 'container_alert_ratio': 1e-6, 'container_rearm_ratio': 1e-7,
               'rg_guard': {'enabled': False}, **settings}
        path = self.run_dir / 'config.json'
        path.write_text(json.dumps(cfg))
        return path, data

    def start(self, argv, env=None):
        proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        self.children.append(proc)
        return proc

    def spawn(self, mode, prog, code, *args, env=None):
        # stderr stays out of the pipe: the forked child would otherwise hold it open.
        out = subprocess.run([sys.executable, '-c', SPAWN, mode, prog, code, *args], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=15, env=env)
        found = json.loads(out.stdout)
        self.pids += [found['pid']] + ([found['reader']] if found['reader'] else [])
        return found

    def run_worker(self, config, seconds):
        out = subprocess.run([sys.executable, '-B', GUARD, 'run', '--config', str(config), '--duration', str(seconds)],
                             capture_output=True, text=True, timeout=90)
        self.assertEqual(out.returncode, 0, out.stderr[-500:])
        return json.loads(out.stdout.strip().splitlines()[-1])
