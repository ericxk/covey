"""FORM_TRANSITIONS (covey/core/statuses.py) and the tests' check of it (support/form_transitions.py)."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from covey.app.form_cards import publish_form
from covey.core.state import write_json
from support import form_transitions

TOKEN = "0123456789abcdef01234567"
TESTS = Path(__file__).resolve().parent


def off_table(root) -> None:
    """A done -> publishing change of a launch form record, made by covey."""
    path = Path(root) / "launch-forms" / (TOKEN + ".json")
    write_json(path, {"status": "done"})  # The test's own record: not checked.
    publish_form(path, {"status": "done"}, lambda: ("card", "om_1"))


def run_inner(test):
    """The result of running ``test`` (a function of a TestCase) as a test of its own."""
    case = type("Inner", (unittest.TestCase,), {"test": test})("test")
    result = unittest.TestResult()
    case.run(result)
    return result


class CheckTests(unittest.TestCase):
    def test_a_change_outside_the_table_fails_its_test(self):
        with tempfile.TemporaryDirectory() as root:
            result = run_inner(lambda case: off_table(root))

        self.assertEqual(len(result.failures), 1)
        self.assertIn(f"launch-forms/{TOKEN}.json: done -> publishing by app/form_cards.py:publish_form",
                      result.failures[0][1])
        self.assertEqual(form_transitions.found, [])  # Reported once, in that test.

    def test_a_change_after_the_last_test_fails_the_run(self):
        with tempfile.TemporaryDirectory() as root:
            last = type("Last", (unittest.TestCase,), {
                "test": lambda case: None, "tearDownClass": classmethod(lambda cls: off_table(root))})
            result = unittest.TestSuite([last("test")]).run(unittest.TestResult())

        self.assertEqual(result.failures, [])
        self.assertEqual(len(result.errors), 1)
        self.assertIn(f"launch-forms/{TOKEN}.json: done -> publishing", result.errors[0][1])
        self.assertEqual(form_transitions.found, [])

    def test_a_change_after_the_run_ends_the_process_with_status_1(self):
        script = textwrap.dedent("""
            import sys, tempfile
            import support, test_form_transitions
            test_form_transitions.off_table(tempfile.mkdtemp())
            print("done")
        """)
        with tempfile.TemporaryDirectory() as temp:
            ran = subprocess.run([sys.executable, "-B", "-c", script], cwd=TESTS, capture_output=True, text=True,
                                 timeout=120, env={**os.environ, "TMPDIR": temp})

        self.assertEqual(ran.returncode, 1, ran.stderr)
        self.assertEqual(ran.stdout, "done\n")
        self.assertIn(f"launch-forms/{TOKEN}.json: done -> publishing", ran.stderr)

    def test_changes_in_the_table_and_a_tests_own_records_pass(self):
        def test(case):
            with tempfile.TemporaryDirectory() as root:
                path = Path(root) / "rewind-forms" / (TOKEN + ".json")
                publish_form(path, {}, lambda: ("card", "om_1"))  # '' -> publishing -> ready
                write_json(path, {"status": "undone"})  # ready -> undone, by the test itself
                write_json(Path(root) / "rewind-forms" / ".index", {"status": "x"})  # Not a record.

        result = run_inner(test)

        self.assertEqual((result.failures, result.errors), ([], []))
        self.assertEqual(form_transitions.found, [])


class TableTests(unittest.TestCase):
    def test_the_table_names_the_statuses_the_code_tests(self):
        from covey.app.launch import CLOSED
        from covey.core.statuses import WORKTREE_STATUS
        from covey.app.workspace_manager import MANAGING
        from covey.core.statuses import FORM_TRANSITIONS, OPEN_FORMS

        self.assertEqual(set(FORM_TRANSITIONS), {"launch-forms", "switch-forms", "rewind-forms"})
        self.assertLessEqual({CLOSED, WORKTREE_STATUS, MANAGING, *OPEN_FORMS},
                             set(FORM_TRANSITIONS["launch-forms"]))
        for kind, table in FORM_TRANSITIONS.items():
            with self.subTest(kind):
                self.assertIn("", table)  # A new record's first status.
                self.assertLessEqual(set().union(*table.values()), set(table) - {""})  # Each status is a key.
                self.assertNotIn("", set().union(*table.values()))  # Nothing goes back to no record.


if __name__ == "__main__":
    unittest.main()
