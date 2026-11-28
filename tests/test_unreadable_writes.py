"""RT-6 (W1-F, C10): a record that is there but cannot be read for a moment is never written over. A network disk's passing
error (EIO) or a torn file read as {} used to let the next write put only what it changed over the whole file: the settings
back to their defaults, the ledger, the 辅助模型's breakers, a 专项 or the memory emptied. Each write here reads with
core/state.py read_for_write, which raises Unreadable (an OSError) and writes nothing; the owner sees a Chinese line."""

from __future__ import annotations

import errno
import json
import threading
import unittest
from unittest import mock

from support.records import state_dir

from covey.core import assist, butler_settings, memstore, owner_ledger, projects, state
from covey.core.state import Unreadable

WRITERS = 20  # Concurrent writes per record (RT-6).


class Flaky:
    """core/state.py read_json that fails the first ``times`` reads of ``name`` with EIO, then reads."""

    def __init__(self, name, times=1):
        self.name, self.left, self.lock = name, times, threading.Lock()
        self.real = state.read_json

    def __call__(self, path, *args, **kwargs):
        with self.lock:
            fail = self.left > 0 and path.name == self.name
            if fail:
                self.left -= 1
        if fail:
            raise OSError(errno.EIO, "Input/output error")
        return self.real(path, *args, **kwargs)


def together(write, count=WRITERS):
    """``write(i)`` for i in range(count) on as many threads started at once: [what each raised, or None]."""
    start, raised = threading.Barrier(count), [None] * count

    def run(i):
        start.wait()
        try:
            write(i)
        except Exception as exc:  # noqa: BLE001 - the test looks at it
            raised[i] = exc
    threads = [threading.Thread(target=run, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    return raised


class Case(unittest.TestCase):
    def setUp(self):
        self.state = state_dir(self)
        memstore._cache.clear()
        assist.forget()
        self.addCleanup(assist.forget)

    def torn(self, path):
        """``path`` cut in half (a torn file); its bytes."""
        body = path.read_bytes()
        path.write_bytes(body[: len(body) // 2])
        return path.read_bytes()


class SettingsTest(Case):
    """main-thread.json (core/butler_settings.py): all its writes."""

    def setUp(self):
        super().setUp()
        butler_settings.update(self.state, enabled=True, harness="codex", compact_tokens=200000)
        self.path = butler_settings.path(self.state)

    def writes(self):
        return {"update": lambda: butler_settings.update(self.state, subagents="off"),
                "save": lambda: butler_settings.save(self.state, butler_settings.Settings()),
                "preset_harness": lambda: butler_settings.preset_harness(self.state, "agy"),
                "settle_mode": lambda: butler_settings.settle_mode(self.state)}

    def test_a_torn_file_refuses_every_write_and_stays_byte_for_byte(self):
        body = self.torn(self.path)
        for name, write in self.writes().items():
            with self.subTest(name):
                with self.assertRaises(Unreadable):
                    write()
                self.assertEqual(self.path.read_bytes(), body)
        raised = together(lambda i: butler_settings.update(self.state, compact_tokens=1000 * i))
        self.assertTrue(all(isinstance(exc, Unreadable) for exc in raised), raised)
        self.assertEqual(self.path.read_bytes(), body)

    def test_a_passing_read_error_fails_one_write_and_the_others_keep_every_field(self):
        with mock.patch.object(state, "read_json", Flaky(self.path.name)):
            raised = together(lambda i: butler_settings.update(self.state, subagents="off" if i % 2 else "light"))
        self.assertEqual(sum(isinstance(exc, Unreadable) for exc in raised), 1, raised)
        self.assertEqual([exc for exc in raised if exc is not None and not isinstance(exc, Unreadable)], [])
        after = butler_settings.load(self.state)
        self.assertEqual((after.enabled, after.harness, after.compact_tokens), (True, "codex", 200000))  # Never the defaults.

    def test_the_old_way_would_have_reset_them(self):
        """The bug itself: read_object gives {} for the torn file, so a write of one field resets the rest."""
        self.torn(self.path)
        after = butler_settings.load(self.state)
        self.assertEqual((after.harness, after.compact_tokens), ("", butler_settings.COMPACT_TOKENS))


class LedgerTest(Case):
    """owner-ledger.json (core/owner_ledger.py update and the three that call it)."""

    def setUp(self):
        super().setUp()
        owner_ledger.update(self.state, lambda record: {**record, "totals": {"tries": 7, "ok": 5}, "chats": {"oc_1": {"at": 1}}})
        self.path = owner_ledger.path(self.state)

    def test_torn_and_passing_errors_write_nothing_over_the_counts(self):
        body = self.torn(self.path)
        raised = together(lambda i: owner_ledger.looked(self.state, f"oc_{i}", [], 100.0 + i))
        self.assertTrue(all(isinstance(exc, Unreadable) for exc in raised), raised)
        self.assertEqual(self.path.read_bytes(), body)
        self.path.write_text(json.dumps({"v": 1, "totals": {"tries": 7, "ok": 5}}))
        with mock.patch.object(state, "read_json", Flaky(self.path.name)):
            raised = together(lambda i: owner_ledger.looked(self.state, f"oc_{i}", [], 100.0 + i))
        self.assertEqual(sum(isinstance(exc, Unreadable) for exc in raised), 1, raised)
        record = owner_ledger.read(self.state)
        self.assertEqual(record["totals"], {"tries": 7, "ok": 5})
        self.assertEqual(len(record["chats"]), WRITERS - 1)


class AssistTest(Case):
    """assist-state.json (core/assist.py change): never raises, and never starts the breakers and counts over."""

    def setUp(self):
        super().setUp()
        assist.change(self.state, lambda data: data.setdefault("breakers", {}).update(codex={"until": 9e9, "why": "x"}))
        self.path = assist.path(self.state)

    def test_a_torn_record_is_left_as_it_is_and_change_does_not_raise(self):
        body = self.torn(self.path)
        raised = together(lambda i: assist.change(self.state, lambda data: data.update(n=i)))
        self.assertEqual(raised, [None] * WRITERS)
        self.assertEqual(self.path.read_bytes(), body)

    def test_a_passing_read_error_keeps_the_breaker(self):
        with mock.patch.object(state, "read_json", Flaky(self.path.name)):
            raised = together(lambda i: assist.change(self.state, lambda data: data["recent"].update({f"f{i}": {"at": i}})))
        self.assertEqual(raised, [None] * WRITERS)
        data = assist.read(self.state, fresh=True)
        self.assertIn("codex", data["breakers"])
        self.assertEqual(len(data["recent"]), WRITERS - 1)


class ProjectRecordTest(Case):
    """projects/<id>.json (core/projects.py create and update)."""

    def setUp(self):
        super().setUp()
        self.record = projects.new_record(chat_id="oc_owner", name="上线新版本", goal="灰度后全量")
        projects.create(self.state, self.record)
        self.pid = self.record["id"]
        self.path = projects.record_path(self.state, self.pid)

    def test_create_does_not_write_a_new_record_over_one_it_cannot_read(self):
        body = self.torn(self.path)
        with self.assertRaises(Unreadable):
            projects.create(self.state, {**self.record, "name": "别的"})
        self.assertEqual(self.path.read_bytes(), body)

    def test_update_refuses_and_is_still_a_key_error_for_old_callers(self):
        body = self.torn(self.path)
        raised = together(lambda i: projects.update(self.state, self.pid, lambda record: record.update(note=str(i))))
        self.assertTrue(all(isinstance(exc, Unreadable) and isinstance(exc, KeyError) for exc in raised), raised)
        self.assertEqual(self.path.read_bytes(), body)

    def test_a_passing_read_error_keeps_the_record(self):
        with mock.patch.object(state, "read_json", Flaky(self.path.name)):
            raised = together(lambda i: projects.update(self.state, self.pid,
                                                        lambda record: record.setdefault("seen", []).append(i)))
        self.assertEqual(sum(isinstance(exc, Unreadable) for exc in raised), 1, raised)
        record = projects.read_record(self.state, self.pid)
        self.assertEqual((record["name"], record["goal"], len(record["seen"])), ("上线新版本", "灰度后全量", WRITERS - 1))


class MemoryTest(Case):
    """memory/global.json and memory/codes.json (core/memstore.py), and the short codes read again once the file reads."""

    def setUp(self):
        super().setUp()
        self.made = memstore.add(self.state, kind="rule", scope="global", text="回复主人用中文", now=1.0)
        self.path = memstore.global_path(self.state)

    def test_a_torn_store_takes_no_entry_from_twenty_writers(self):
        body = self.torn(self.path)
        raised = together(lambda i: memstore.add(self.state, kind="rule", scope="global", text=f"规则 {i}", now=2.0 + i))
        self.assertTrue(all(isinstance(exc, OSError) for exc in raised), raised)
        self.assertEqual(self.path.read_bytes(), body)

    def test_a_passing_read_error_keeps_the_entry(self):
        with mock.patch.object(state, "read_json", Flaky(self.path.name)):
            raised = together(lambda i: memstore.add(self.state, kind="rule", scope="global", text=f"规则 {i}", now=2.0 + i))
        self.assertEqual(sum(isinstance(exc, Unreadable) for exc in raised), 1, raised)
        texts = {entry.text for entry in memstore.entries(self.state, scope="global")}
        self.assertIn("回复主人用中文", texts)
        self.assertEqual(len(texts), WRITERS)

    def test_the_short_codes_come_back_as_soon_as_the_file_reads(self):
        """A passing error while the codes are read is not kept for the file (same mtime and size) until it changes."""
        memstore.assign(self.state, [(self.made.ref, "rule")])
        memstore._cache.clear()
        codes_file = memstore.codes_path(self.state)
        with mock.patch.object(memstore, "read_json", side_effect=OSError(errno.EIO, "Input/output error")):
            self.assertEqual(memstore.codes(self.state)["refs"], {})
        self.assertIn(self.made.ref, memstore.codes(self.state)["refs"])
        self.assertTrue(codes_file.exists())


if __name__ == "__main__":
    unittest.main()
