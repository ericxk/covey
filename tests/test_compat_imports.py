"""The old package name harness_bridge keeps working for one release (harness_bridge.py at the repo root)."""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

SCRIPT = """
import sys
import harness_bridge.core.state as old
from harness_bridge.app import unified
from harness_bridge import core
import covey.core.state as new
import covey.app.unified
assert old is new and unified is covey.app.unified and core is covey.core
assert new.__name__ == "covey.core.state" and new.__spec__.name == "covey.core.state"
try:
    import harness_bridge.core.no_such_module
except ModuleNotFoundError:
    print("missing-ok")
"""


class OldImportNameTests(unittest.TestCase):

    def test_old_names_are_the_same_modules_and_warn_once(self):
        done = subprocess.run([sys.executable, "-B", "-c", SCRIPT], cwd=REPO, capture_output=True, text=True,
                              timeout=60, env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(REPO)})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("missing-ok", done.stdout)
        self.assertEqual(done.stderr.count("old name of the covey package"), 1, done.stderr)


if __name__ == "__main__":
    unittest.main()
