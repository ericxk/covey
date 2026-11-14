"""Independent session revision/CAS fixtures using synthetic private state only."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import select
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
import covey.core.session_state as session_state
from support.sessions import clear_session_id, write_session_id

PATTERN = re.compile(r"sid-[a-z]+")
OLD = "sid-original"
FIRST = "sid-first"
SECOND = "sid-second"


class _SessionFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="session-cas-fixture-")
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "topic" / "native_session_id"
        self.path.parent.mkdir()

    def capture(self):
        return session_state.capture_session_state(self.path, PATTERN)

class SessionCASTests(_SessionFixture):
    def test_legacy_session_without_revision_defaults_to_zero(self):
        self.path.write_text(OLD)
        self.assertEqual(self.capture(), (0, OLD))

    def test_missing_session_defaults_to_zero_and_none(self):
        self.assertEqual(self.capture(), (0, None))

    def test_write_advances_revision_even_for_same_session(self):
        write_session_id(self.path, OLD)
        first = self.capture()
        write_session_id(self.path, OLD)
        self.assertEqual(self.capture(), (first[0] + 1, OLD))

    def test_clear_existing_and_empty_each_advance_revision(self):
        write_session_id(self.path, OLD)
        revision, _ = self.capture()
        self.assertEqual(clear_session_id(self.path, PATTERN), OLD)
        self.assertEqual(self.capture(), (revision + 1, None))
        self.assertIsNone(clear_session_id(self.path, PATTERN))
        self.assertEqual(self.capture(), (revision + 2, None))

    def test_cas_can_invalidate_reused_original_session(self):
        write_session_id(self.path, OLD)
        revision, sid = self.capture()
        self.assertTrue(session_state.compare_and_set_session(self.path, PATTERN, revision, sid, None))
        self.assertEqual(self.capture(), (revision + 1, None))
        self.assertFalse(session_state.compare_and_set_session(self.path, PATTERN, revision, sid, None))

    def test_cas_can_commit_normal_new_session(self):
        write_session_id(self.path, OLD)
        revision, sid = self.capture()
        self.assertTrue(session_state.compare_and_set_session(self.path, PATTERN, revision, sid, FIRST))
        self.assertEqual(self.capture(), (revision + 1, FIRST))

    def test_old_run_cannot_clear_user_new_session(self):
        write_session_id(self.path, OLD)
        revision, sid = self.capture()
        clear_session_id(self.path, PATTERN)
        write_session_id(self.path, SECOND)
        current = self.capture()
        self.assertFalse(session_state.compare_and_set_session(self.path, PATTERN, revision, sid, None))
        self.assertEqual(self.capture(), current)

    def test_reset_to_same_sid_does_not_revive_old_revision(self):
        write_session_id(self.path, OLD)
        revision, sid = self.capture()
        clear_session_id(self.path, PATTERN)
        write_session_id(self.path, OLD)
        current = self.capture()
        self.assertFalse(session_state.compare_and_set_session(self.path, PATTERN, revision, sid, FIRST))
        self.assertEqual(self.capture(), current)

    def test_cas_checks_expected_native_session_not_revision_alone(self):
        write_session_id(self.path, OLD)
        revision, _ = self.capture()
        self.assertFalse(session_state.compare_and_set_session(self.path, PATTERN, revision, SECOND, None))
        self.assertEqual(self.capture(), (revision, OLD))

    def test_invalid_new_session_does_not_change_state(self):
        write_session_id(self.path, OLD)
        before = self.capture()
        with self.assertRaises(ValueError):
            session_state.compare_and_set_session(self.path, PATTERN, *before, "../not-a-session")
        self.assertEqual(self.capture(), before)

    def test_boolean_and_float_revision_cannot_alias_integer_revision(self):
        write_session_id(self.path, OLD)
        revision, sid = self.capture()
        for invalid in (True, float(revision)):
            with self.subTest(kind=type(invalid).__name__):
                before = self.capture()
                try:
                    accepted = session_state.compare_and_set_session(self.path, PATTERN, invalid, sid, FIRST)
                except (TypeError, ValueError):
                    accepted = False
                self.assertFalse(accepted)
                self.assertEqual(self.capture(), before)

    def test_malformed_revision_fails_closed(self):
        self.path.write_text(OLD)
        self.path.with_name(self.path.name + ".revision").write_text("not-a-revision")
        with self.assertRaises(ValueError):
            self.capture()
        self.assertEqual(self.path.read_text(), OLD)

    def test_other_topic_is_not_modified(self):
        other = self.path.parent / "other_backend_session_id"
        write_session_id(self.path, OLD)
        write_session_id(other, SECOND)
        before_other = session_state.capture_session_state(other, PATTERN)
        revision, sid = self.capture()
        self.assertTrue(session_state.compare_and_set_session(self.path, PATTERN, revision, sid, None))
        self.assertEqual(session_state.capture_session_state(other, PATTERN), before_other)


def _cas_worker(arguments):
    path, revision, replacement, hold = arguments
    path = Path(path)
    original_write = session_state.write_private_text
    held = False

    def controlled_write(target, content, *args, **kwargs):
        nonlocal held
        if hold == "hold" and not held:
            held = True
            print("before-first-write", flush=True)
            if sys.stdin.readline().strip() != "release":
                raise RuntimeError("fixture release was not received")
        return original_write(target, content, *args, **kwargs)

    print("ready", flush=True)
    with mock.patch.object(session_state, "write_private_text", side_effect=controlled_write):
        accepted = session_state.compare_and_set_session(path, PATTERN, int(revision), OLD, replacement)
    print(json.dumps({"accepted": accepted, "replacement": replacement}), flush=True)


class CrossProcessCASTests(_SessionFixture):
    """Run separately under the existing guard, not the subprocess-free suite."""

    def _line(self, process, timeout=4):
        data = bytearray()
        while not data.endswith(b"\n") and len(data) < 4096:
            ready, _, _ = select.select([process.stdout], [], [], timeout)
            self.assertTrue(ready, "private CAS fixture did not reach its barrier")
            part = os.read(process.stdout.fileno(), 1)
            self.assertTrue(part, "private CAS worker exited before its barrier")
            data.extend(part)
        self.assertTrue(data.endswith(b"\n"), "private CAS fixture response is too long")
        return data.decode().strip()

    def _spawn(self, revision, replacement, hold):
        process = subprocess.Popen(
            [sys.executable, "-B", str(Path(__file__).resolve()), "--cas-worker",
             str(self.path), str(revision), replacement, hold],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
        def cleanup():
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=4)
        self.addCleanup(cleanup)
        return process

    def test_cross_process_check_and_write_are_one_locked_operation(self):
        write_session_id(self.path, OLD)
        revision, _ = self.capture()
        first = self._spawn(revision, FIRST, "hold")
        self.assertEqual(self._line(first), "ready")
        self.assertEqual(self._line(first), "before-first-write")
        second = self._spawn(revision, SECOND, "go")
        self.assertEqual(self._line(second), "ready")
        # The first writer has passed its check but has not changed any bytes.
        # A correctly shared lock prevents the second process from winning here.
        select.select([second.stdout], [], [], 0.75)
        first.stdin.write("release\n")
        first.stdin.flush()
        first_out, first_err = first.communicate(timeout=4)
        second_out, second_err = second.communicate(timeout=4)
        self.assertEqual(first.returncode, 0, first_err[-200:])
        self.assertEqual(second.returncode, 0, second_err[-200:])
        results = [json.loads(text.strip().splitlines()[-1]) for text in (first_out, second_out)]
        self.assertEqual([row["accepted"] for row in results], [True, False])
        self.assertEqual(self.capture(), (revision + 1, FIRST))


if __name__ == "__main__":
    if sys.argv[1:2] == ["--cas-worker"]:
        _cas_worker(sys.argv[2:])
    else:
        unittest.main(argv=[sys.argv[0], *sys.argv[1:]])
