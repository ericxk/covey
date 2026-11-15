"""The memory watchdog's supervisor (service/memguard.py daemon) and the Manager starting it.

In a module of its own because these two tests take about half of test_memguard's time: run in parallel
with the rules, the wrapper and the alerts, the slowest module of a full run ends sooner.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import unittest
from unittest import mock

from support.memguard import GUARD, GuardCase, alive
from support.processes import leftover_check

from covey.service import manager, memguard

setUpModule, tearDownModule = leftover_check(timeout=5)



def gone(pid, seconds):
    """Whether ``pid`` stops running (a zombie counts as gone) within ``seconds``."""
    deadline = time.monotonic() + seconds
    while alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    return not alive(pid)

class SupervisorTests(GuardCase):
    def daemon(self, config):
        proc = subprocess.Popen([sys.executable, '-B', GUARD, 'daemon', '--config', str(config)],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True)
        self.children.append(proc)
        return proc

    def status(self, config, check, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            st = memguard.query(config, timeout=2)
            if st and check(st):
                return st
            time.sleep(0.3)
        return memguard.query(config, timeout=2)

    def test_the_supervisor_restarts_its_worker_runs_once_and_takes_it_along(self):
        config, _ = self.config(interval_seconds=1.0, only_comms=['mgt-none'], container_alert_ratio=2.0)
        first = self.daemon(config)
        st = self.status(config, lambda s: s.get('heartbeat'))
        worker = st['worker']['pid']
        os.kill(worker, signal.SIGKILL)
        st = self.status(config, lambda s: s['restarts'] >= 1 and s['worker']['pid'] != worker
                         and s['heartbeat_age_s'] < 3, 30)
        self.assertGreaterEqual(st['restarts'], 1)
        self.assertEqual(self.daemon(config).wait(15), 0)  # A second one finds the first and leaves.
        self.assertEqual(memguard.query(config)['pid'], first.pid)
        worker = memguard.query(config)['worker']['pid']
        memguard.query(config, b'stop')
        self.assertEqual(first.wait(30), 0)
        self.assertTrue(gone(worker, 20))
        again = self.daemon(config)
        worker = self.status(config, lambda s: s.get('heartbeat'))['worker']['pid']
        again.kill()
        again.wait()
        # It dies with its supervisor: SIGTERM from PR_SET_PDEATHSIG, then out of the scan it is in. 20 s, not 5: a
        # slow CI runner once took longer (Python 3.10, 2026-11-16).
        self.assertTrue(gone(worker, 20))

    def test_the_manager_starts_an_enabled_watchdog_that_is_not_running(self):
        config, _ = self.config(interval_seconds=1.0, only_comms=['mgt-none'], container_alert_ratio=2.0)
        installed = self.run_dir / 'libexec' / 'memguard.py'
        installed.parent.mkdir()
        shutil.copy2(GUARD, installed)
        logged = []
        with mock.patch.dict(os.environ, {memguard.TASK_DIR_ENV: str(self.run_dir / 'runs' / 'om_x')}), \
                mock.patch.object(memguard, 'INSTALLED', installed), mock.patch.object(memguard, 'CONFIG', config), \
                mock.patch.dict(memguard._ensured, {'checked': -1e9, 'started': -1e9, 'child': None}), \
                mock.patch.object(manager, 'say', logged.append):
            manager.Manager(self.run_dir).ensure_memguard()
            self.assertEqual(logged, ['memory watchdog: started'])
            child = memguard._ensured['child']
            self.children.append(child)
            st = self.status(config, lambda s: s.get('heartbeat'))
            self.assertEqual(st['pid'], child.pid)
            environ = Path(f'/proc/{child.pid}/environ').read_bytes()
            # Not one of a task's leftovers. A boolean: a failure must not print the environment.
            self.assertFalse(memguard.TASK_DIR_ENV.encode() in environ, 'the watchdog carries a task marker')
            memguard._ensured['checked'] = -1e9
            self.assertEqual(memguard.ensure(), 'running')
            self.assertEqual(memguard.ensure(), 'waiting')  # Checked again 30 s later.
            memguard.query(config, b'stop')
            child.wait(30)
            config.write_text(json.dumps({**json.loads(config.read_text()), 'enabled': False}))
            memguard._ensured['checked'] = -1e9
            self.assertEqual(memguard.ensure(), 'disabled')
        self.assertIsNone(memguard.query(config))


if __name__ == '__main__':
    unittest.main()
