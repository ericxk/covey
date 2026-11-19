"""scripts/ci_retry_modules.py: which failed test modules CI runs once more, read from the ledger's last line;
none when the line does not name every failure, names something not a module of tests/, or too many failed.
Ledger files under TMPDIR, no test suite runs here."""

import importlib.util
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from support import REPO

_spec = importlib.util.spec_from_file_location("ci_retry_modules", REPO / "scripts" / "ci_retry_modules.py")
retry = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(retry)


def entry(*failed, failures=None, errors=0):
    return {"failed": list(failed), "failures": len(failed) if failures is None else failures, "errors": errors}


class ModulesTest(unittest.TestCase):
    def test_the_failed_tests_modules_once_each(self):
        self.assertEqual(retry.modules(entry("test_steer_interrupt.StopTests.test_a", "test_detached_runs.Restart.test_b",
                                             "test_detached_runs.Restart.test_c")),
                         ["tests/test_detached_runs.py", "tests/test_steer_interrupt.py"])
        self.assertEqual(retry.modules(entry("setUpClass (test_detached_runs.Restart)", failures=0, errors=1)),
                         ["tests/test_detached_runs.py"])

    def test_no_retry_unless_every_failure_is_named_and_a_module_of_tests(self):
        self.assertIsNone(retry.modules({"failures": 1, "errors": 0}))
        self.assertIsNone(retry.modules(entry("test_detached_runs.Restart.test_b", failures=25)))  # FAILED cut it.
        self.assertIsNone(retry.modules(entry("test_no_such_module.A.test_b")))
        self.assertIsNone(retry.modules(entry("test_detached_runs (exit -9)", failures=0, errors=1)))
        many = [f"{name}.A.test_b" for name in ("test_steer", "test_proxy", "test_web_sync", "test_ttft", "test_pi")]
        self.assertIsNone(retry.modules(entry(*many)))

    def test_main_prints_the_last_lines_modules(self):
        with tempfile.TemporaryDirectory() as temp:
            ledger = Path(temp) / "ledger.jsonl"
            ledger.write_text(json.dumps(entry("test_pi.A.test_b")) + "\n" + json.dumps(entry("test_ttft.A.test_b"))
                              + "\n\n")
            out = StringIO()
            with redirect_stdout(out):
                self.assertEqual(retry.main(["ci_retry_modules.py", str(ledger)]), 0)
            self.assertEqual(out.getvalue(), "tests/test_ttft.py\n")
            ledger.write_text(json.dumps({"failures": 0, "errors": 0}) + "\n")
            self.assertEqual(retry.main(["ci_retry_modules.py", str(ledger)]), 1)


if __name__ == "__main__":
    unittest.main()
