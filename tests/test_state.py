"""The small shared state helpers of core/state.py: JSON reads, record times and line logs."""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
import re
import stat
import tempfile
import unittest

from covey.core.state import epoch_seconds, read_json, read_object
from covey.core.logs import line_log

LINE = re.compile(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d (.*)\n")


class ReadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)

    def file(self, name, body):
        path = self.folder / name
        path.write_bytes(body)
        return path

    def test_read_json_gives_the_default_only_for_a_missing_file(self):
        missing = self.folder / "missing.json"
        with self.assertRaises(FileNotFoundError):
            read_json(missing)
        self.assertIsNone(read_json(missing, None))
        self.assertEqual(read_json(missing, {}), {})
        self.assertEqual(read_json(self.file("list.json", b"[1]"), {}), [1])
        with self.assertRaises(ValueError):
            read_json(self.file("bad.json", b"{"), {})
        with self.assertRaises(IsADirectoryError):
            read_json(self.folder, {})

    def test_read_object_is_empty_for_anything_but_an_object(self):
        self.assertEqual(read_object(self.file("ok.json", b'{"a": 1}')), {"a": 1})
        for name, body in [("list", b"[]"), ("number", b"1"), ("null", b"null"), ("text", b'"s"'),
                           ("bad", b"{"), ("empty", b""), ("bytes", b'{"a": "\xff"}')]:
            with self.subTest(name):
                self.assertEqual(read_object(self.file(name + ".json", body)), {})
        self.assertEqual(read_object(self.folder / "missing.json"), {})
        self.assertEqual(read_object(self.folder), {})


class EpochSecondsTests(unittest.TestCase):
    def test_utc_with_z_or_an_offset_and_local_time_without_one(self):
        utc = dt.datetime(2026, 11, 5, 12, tzinfo=dt.timezone.utc).timestamp()
        self.assertEqual(epoch_seconds("2026-11-05T12:00:00Z"), utc)
        self.assertEqual(epoch_seconds("2026-11-05T20:00:00+08:00"), utc)
        self.assertEqual(epoch_seconds("2026-11-05T12:00:00"), dt.datetime(2026, 11, 5, 12).timestamp())
        self.assertEqual(epoch_seconds("1970-01-01T00:00:00Z"), 0.0)

    def test_aware_wants_a_time_zone(self):
        self.assertEqual(epoch_seconds("2026-11-05T12:00:00Z", aware=True), 1793880000.0)
        self.assertIsNone(epoch_seconds("2026-11-05T12:00:00", aware=True))

    def test_none_for_what_is_not_a_time_string(self):
        for value in (None, 0, 1.5, 20261105, True, [], {}, b"2026-11-05", "", "soon", "2026-13-01",
                      "0001-01-01T00:00:00"):  # The last is out of range as local time.
            with self.subTest(value=value):
                self.assertIsNone(epoch_seconds(value))


class LineLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "gateway" / "gateway.log"

    def lines(self, path):
        return [LINE.fullmatch(line).group(1) for line in path.read_text().splitlines(keepends=True)]

    def test_lines_go_after_the_local_time_in_a_private_file(self):
        log = line_log(self.path, max_bytes=1000)
        log("first")
        log("second")
        self.assertEqual(self.lines(self.path), ["first", "second"])
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)

    def test_a_file_larger_than_max_bytes_is_renamed_before_the_next_line(self):
        log = line_log(self.path, max_bytes=41, keep=2)
        log("a" * 20)  # 41 bytes with the time and the newline: as large as max_bytes, not larger.
        log("b")
        log("c")
        self.assertEqual((self.lines(self.path), self.lines(Path(f"{self.path}.1"))), (["c"], ["a" * 20, "b"]))
        log("d" * 30)
        log("e")
        self.assertEqual([self.lines(Path(f"{self.path}{suffix}")) for suffix in ("", ".1", ".2")],
                         [["e"], ["c", "d" * 30], ["a" * 20, "b"]])

    def test_one_older_file_by_default(self):
        log = line_log(self.path, max_bytes=10)
        for text in "abc":
            log(text * 20)
        self.assertEqual((self.lines(self.path), self.lines(Path(f"{self.path}.1"))), (["c" * 20], ["b" * 20]))
        self.assertFalse(Path(f"{self.path}.2").exists())

    def test_live_rotation_keeps_the_file_another_process_holds(self):
        log = line_log(self.path, max_bytes=41, keep=2, live=True)
        log("a" * 20)  # 41 bytes: as large as max_bytes, so it rotates before the next line.
        inode = self.path.stat().st_ino
        log("b")
        self.assertEqual(self.path.stat().st_ino, inode)
        self.assertEqual((self.lines(self.path), self.lines(Path(f"{self.path}.1"))), (["b"], ["a" * 20]))
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_an_os_error_is_dropped(self):
        blocker = Path(self.tmp.name) / "file"
        blocker.write_text("")
        for live in (False, True):
            with self.subTest(live=live):
                line_log(blocker / "x.log", max_bytes=10, live=live)("lost")
        if os.geteuid():  # root writes anyway
            folder = self.path.parent
            folder.mkdir(mode=0o500)
            self.addCleanup(folder.chmod, 0o700)
            line_log(self.path, max_bytes=10)("lost")
            self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
