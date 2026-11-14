"""core/procfs.py: a process's /proc stat and the boot id, which process identities, task leftovers and the policy
stop read."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time
import unittest

from covey.core import procfs
from covey.core.process import process_identity


class StatTests(unittest.TestCase):
    def test_this_process(self):
        stat = procfs.stat(os.getpid())
        self.assertEqual((stat.state, stat.ppid, stat.pgrp, stat.session), ("R", os.getppid(), os.getpgrp(), os.getsid(0)))
        self.assertEqual(stat.name, Path("/proc/self/comm").read_text().rstrip("\n"))
        self.assertGreater(stat.rss_pages, 0)
        self.assertEqual(procfs.stat(str(os.getpid())), stat)

    def test_a_name_with_parentheses_and_spaces(self):
        # The command name is the text between the first "(" and the last ")": a process may name itself ") (".
        child = subprocess.Popen([sys.executable, "-c", "import sys, time; open('/proc/self/comm', 'w').write('a) b (c');"
                                  "print(flush=True); time.sleep(30)"], stdout=subprocess.PIPE, start_new_session=True)
        self.addCleanup(lambda: (child.kill(), child.wait(), child.stdout.close()))
        child.stdout.readline()
        # It prints, then goes to sleep: until it has, a loaded machine (a CI runner) still shows it running.
        deadline = time.monotonic() + 10
        while procfs.stat(child.pid).state != "S" and time.monotonic() < deadline:
            time.sleep(0.01)
        stat = procfs.stat(child.pid)
        self.assertEqual((stat.name, stat.state, stat.ppid, stat.pgrp, stat.session), ("a) b (c", "S", os.getpid(), child.pid,
                                                                                     child.pid))
        self.assertEqual(process_identity(child.pid)["start_ticks"], str(stat.start_ticks))

    def test_a_process_that_has_gone(self):
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        self.assertIsNone(procfs.stat(child.pid))
        self.assertIsNone(procfs.stat("self-not-a-pid"))

    def test_boot_id(self):
        self.assertEqual(procfs.boot_id(), Path("/proc/sys/kernel/random/boot_id").read_text().strip())
        self.assertEqual(process_identity(os.getpid())["boot_id"], procfs.boot_id())


if __name__ == "__main__":
    unittest.main()
