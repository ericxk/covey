"""The tests' check of form record statuses against FORM_TRANSITIONS (covey/core/statuses.py).

``install`` (from this package's ``__init__``, before any test module loads covey) wraps
``core.state.write_private_text``: each write of a launch, 换配置 or 回退 form record that covey
makes is compared with the record on disk before it, and a status change the table does not list fails the
test it happened in (or the next test, for a write from setUpClass or from a thread that outlived its test).
One made after the last test (tearDownClass, tearDownModule, a thread still running) is an error of the run,
and one made after the run (a thread outliving it) ends the process with exit status 1.
A record a test writes itself (no covey frame between the test and the write) is a fixture and is
not checked. A form record written by a module that bound the unwrapped function, before ``install``, still
reaches ``core.store.note_change`` and fails the same way. The Bridge itself does not consult the table yet.

``BRIDGE_FORM_TRANSITIONS_LOG`` names a file that gets one JSON line per status change seen (checked or not).
"""
from __future__ import annotations

import atexit
import json
import os
import sys
import threading
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[2] / "covey"
SKIPPED = {str(PACKAGE / "core" / "state.py"), str(PACKAGE / "core" / "store.py")}
NO_RECORD = ""
UNREADABLE = "?"

found: list[str] = []  # Changes outside the table not yet failed in a test.
current_test = ""  # The id of the test running, for the log.
_guard = threading.Lock()
_writing = threading.local()
_table: dict = {}


def status(text) -> str | None:
    """A form record's status: NO_RECORD for none, UNREADABLE for one that is not a JSON object."""
    try:
        record = json.loads(text)
    except ValueError:
        return UNREADABLE
    return record.get("status") if isinstance(record, dict) else UNREADABLE


def on_disk(path: Path) -> str | None:
    try:
        return status(path.read_text())
    except FileNotFoundError:
        return NO_RECORD
    except OSError:
        return UNREADABLE


def writer() -> str:
    """The three innermost covey frames making this write (not core/state.py or core/store.py, nor
    form_cards.save_record), '' when a test writes it itself."""
    frames, frame = [], sys._getframe(2)
    while frame is not None and len(frames) < 3:
        name, function = frame.f_code.co_filename, frame.f_code.co_name
        if name.startswith(str(PACKAGE)) and name not in SKIPPED and function != "save_record":
            frames.append(f"{Path(name).relative_to(PACKAGE)}:{function}")
        frame = frame.f_back
    return " < ".join(frames)


def form_kind(path: Path) -> str:
    """The FORM_TRANSITIONS key of a form record's path, '' for any other file."""
    if path.parent.name in _table and path.suffix == ".json" and not path.name.startswith("."):
        return path.parent.name
    return ""


def note(kind, path, old, new, by) -> None:
    if old == new or not by:
        return
    allowed = new in _table[kind].get(old, ())
    log = os.environ.get("BRIDGE_FORM_TRANSITIONS_LOG")
    if log:
        line = {"kind": kind, "from": old, "to": new, "writer": by, "allowed": allowed, "test": current_test}
        with _guard, open(log, "a") as handle:
            handle.write(json.dumps(line, ensure_ascii=False) + "\n")
    if not allowed:
        with _guard:
            found.append(f"{kind}/{path.name}: {old or '(no record)'} -> {new} by {by}")


def check() -> None:
    """Fail on the changes outside the table since the last check."""
    with _guard:
        seen, found[:] = list(found), []
    if seen:
        raise AssertionError("form status changes FORM_TRANSITIONS does not list "
                             "(covey/core/statuses.py):\n  " + "\n  ".join(seen))


def install() -> None:
    from covey.core import state, store
    try:
        from covey.core.statuses import FORM_TRANSITIONS
    except ImportError:  # Before core/statuses.py the table was in core/store.py.
        FORM_TRANSITIONS = getattr(store, "FORM_TRANSITIONS", None)

    # Without the table (an older tree, as scripts/test_map.py prove runs these tests on) nothing is checked.
    if getattr(state.write_private_text, "form_check", False) or FORM_TRANSITIONS is None:
        return
    _table.update(FORM_TRANSITIONS)
    unchecked, journal = state.write_private_text, store.note_change

    def write_private_text(path, content):
        kind = form_kind(Path(path))
        if not kind:
            return unchecked(path, content)
        old = on_disk(Path(path))
        _writing.path = Path(path)
        try:
            unchecked(path, content)
        finally:
            _writing.path = None
        note(kind, Path(path), old, status(content), writer())

    def note_change(folder, name):
        path = Path(folder) / name
        kind = form_kind(path)
        if kind and getattr(_writing, "path", None) != path and writer():
            with _guard:
                found.append(f"{kind}/{name}: written past the check by {writer()}")
        return journal(folder, name)

    write_private_text.form_check = True
    state.write_private_text, store.note_change = write_private_text, note_change

    run = unittest.TestCase.run

    def checked_run(self, result=None):
        global current_test
        current_test = self.id()
        self.addCleanup(check)  # The first added runs last: after the test's own cleanups.
        return run(self, result)

    unittest.TestCase.run = checked_run
    suite_run = unittest.TestSuite.run

    def checked_suite_run(self, result, debug=False):
        outermost = not getattr(result, "_testRunEntered", False)
        suite_run(self, result, debug)
        if outermost:  # After the last tearDownClass and tearDownModule.
            try:
                check()
            except AssertionError:
                result.addError(unittest.suite._ErrorHolder("form status changes after the last test"), sys.exc_info())
        return result

    unittest.TestSuite.run = checked_suite_run
    atexit.register(report)


def report() -> None:
    """At exit, changes outside the table made after the run end the process with status 1: test_map does
    not show test output, but a child's exit status is an error. os._exit skips the atexit handlers left."""
    if found:
        print("form status changes FORM_TRANSITIONS does not list, after the last test:\n  " + "\n  ".join(found),
              file=sys.stderr, flush=True)
        sys.stdout.flush()
        os._exit(1)
