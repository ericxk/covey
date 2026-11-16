"""The indexed record store (core/store.py directory_store, one_scan, invalidate_directory) under concurrent
writers, and state recovered from what is on disk."""
from __future__ import annotations

import copy
import json
import shutil
import stat
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support.records import write_topic
from covey.app import outbox
from covey.app.runtime import JobRegistry
from covey.core.jobs import job_file, upsert_job_record
from covey.core.state import write_private_text
from covey.core.history_storage import recent_history
from covey.core.store import directory_store, invalidate_directory, one_scan
from covey.core.topics import workspace_locked


class IndexedStorageTest(unittest.TestCase):
    def test_bad_record_isolated_and_repeat_query_avoids_parsing(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            write_private_text(path/'a.json', json.dumps({'topic_id':'a','job_id':1}))
            write_private_text(path/'b.json', '{broken')
            store = directory_store(path)
            self.assertEqual(len(store.query(topic_id='a')), 1)
            self.assertIn('b.json', store.invalid)
            with mock.patch('covey.core.store.json.loads', side_effect=AssertionError('unnecessary read')):
                self.assertEqual(len(store.query(topic_id='a')), 1)
            write_private_text(path/'a.json', json.dumps({'topic_id':'b','job_id':1}))
            self.assertEqual(store.query(topic_id='a'), [])
            self.assertEqual(len(store.query(topic_id='b')), 1)

    def copies(self):
        """The store's deepcopy, recording the key (or job_id) of each record it copies."""
        copied, real = [], copy.deepcopy
        fake = SimpleNamespace(deepcopy=lambda value: copied.append(value.get('key', value.get('job_id'))) or real(value))
        return copied, mock.patch('covey.core.store.copy', fake)

    def test_a_where_filter_copies_only_the_records_it_accepts(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            for number in (1, 2, 3):
                write_private_text(path / f'{number}.json', json.dumps({'topic_id': 't', 'job_id': number, 'tags': []}))
            store = directory_store(path)
            copied, patched = self.copies()
            with patched:
                found = store.query(lambda record: record['job_id'] != 2, topic_id='t')
            self.assertEqual(([record['job_id'] for _, record in found], copied), ([1, 3], [1, 3]))
            found[0][1]['tags'].append('changed')  # A copy: the cached record stays as it was.
            self.assertEqual(store.query(topic_id='t')[0][1]['tags'], [])

    def test_one_message_scans_a_directory_once_and_sees_this_process_writes(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            write_private_text(path / 'a.json', json.dumps({'topic_id': 't'}))
            store, clock, scans = directory_store(path), [100.0], []
            real_list = type(store)._list

            def listing(self):
                scans.append(self.directory)
                return real_list(self)

            def names():
                return [p.name for p, _ in store.query(topic_id='t')]
            with mock.patch('covey.core.store.time.monotonic', lambda: clock[0]), \
                    mock.patch.object(type(store), '_list', listing):
                with one_scan():
                    self.assertEqual(names(), ['a.json'])
                    (path / 'b.json').write_text(json.dumps({'topic_id': 't'}))  # Another process's write.
                    write_private_text(path / 'c.json', json.dumps({'topic_id': 't'}))  # This process's.
                    clock[0] += 0.5
                    self.assertEqual(names(), ['a.json', 'c.json'])
                    self.assertEqual(len(scans), 1)  # c.json was read by name.
                    clock[0] += 0.6  # The scan is over a second old: redone, as without the block.
                    self.assertEqual(names(), ['a.json', 'b.json', 'c.json'])
                    self.assertEqual(len(scans), 2)
                    invalidate_directory(path)  # A write it cannot name: rescanned at once.
                    self.assertEqual(names(), ['a.json', 'b.json', 'c.json'])
                    self.assertEqual(len(scans), 3)
                write_private_text(path / 'd.json', json.dumps({'topic_id': 't'}))
                self.assertEqual(names(), ['a.json', 'b.json', 'c.json', 'd.json'])
                self.assertEqual(len(scans), 4)  # Outside the block every write still means a rescan.

    def test_a_scan_of_many_files_reads_them_in_parallel_with_the_same_result(self):
        # S35: a task runner's first query read 927 job records one by one from NAS, 2.3 s.
        from covey.core import store as store_module
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            for number in range(3 * store_module.SCAN_THREADS):
                write_private_text(path / f'{number}.json', json.dumps({'topic_id': 't', 'job_id': number}))
            write_private_text(path / 'broken.json', '{broken')
            write_private_text(path / 'list.json', '[]')
            store, threads, real = directory_store(path), set(), store_module.JsonDirectoryStore._read

            def read(self, file):
                threads.add(threading.current_thread().name)
                return real(self, file)
            with mock.patch.object(store_module.JsonDirectoryStore, '_read', read):
                found = store.query(topic_id='t')
            self.assertEqual(sorted(r['job_id'] for _, r in found), list(range(3 * store_module.SCAN_THREADS)))
            self.assertEqual(store.invalid, {'broken.json', 'list.json'})
            self.assertTrue(threads and all(name.startswith('store-scan') for name in threads), threads)
            write_private_text(path / '0.json', json.dumps({'topic_id': 'u', 'job_id': 0}))
            invalidate_directory(path)
            loads, real_loads = [], json.loads
            with mock.patch('covey.core.store.json.loads', lambda text: loads.append(text) or real_loads(text)):
                self.assertEqual([r['job_id'] for _, r in store.query(topic_id='u')], [0])
            self.assertEqual(len(loads), 1)  # Only the changed file: unchanged ones, broken or not, are not read again.

    def test_a_rescan_reads_only_the_files_whose_inode_changed(self):
        # S39: a stat per file cost 0.3-0.9 s for 976 job records on NFS; one listing 1.3 ms.
        from covey.core import store as store_module
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'records'  # Not a JOURNALED name: every look lists it.
            for number in (1, 2, 3):
                write_private_text(path / f'{number}.json', json.dumps({'topic_id': 't', 'job_id': number}))
            store, real = directory_store(path), store_module.JsonDirectoryStore._read
            self.assertEqual(len(store.query(topic_id='t')), 3)
            write_private_text(path / '2.json', json.dumps({'topic_id': 't', 'job_id': 22}))  # A new inode.
            (path / '3.json').unlink()
            write_private_text(path / '4.json', json.dumps({'topic_id': 't', 'job_id': 4}))
            (path / '.5.json.tmp').write_text('{}')  # A writer's temp file: not a record.
            read = []
            with mock.patch.object(store_module.JsonDirectoryStore, '_read',
                                   lambda self, file: read.append(file.name) or real(self, file)):
                self.assertEqual([r['job_id'] for _, r in store.query(topic_id='t')], [1, 22, 4])
            self.assertEqual(sorted(read), ['2.json', '4.json'])
            shutil.rmtree(path)
            invalidate_directory(path)
            self.assertEqual(store.query(), [])

    def test_a_journaled_directory_catches_up_from_its_journal_and_lists_only_when_it_must(self):
        # S45: a changed directory's listing took about 100 ms on NFS with ~1000 job records; the journal 1-3 ms.
        from covey.core import store as store_module
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'jobs'
            write_private_text(path / 'a.json', json.dumps({'topic_id': 't'}))
            write_private_text(Path(root) / 'other' / 'x.json', '{}')
            self.assertEqual((path / '.journal').read_text(), 'a.json\n')
            self.assertFalse((Path(root) / 'other' / '.journal').exists())  # Only the record directories keep one.
            store, clock, listings = directory_store(path), [100.0], []
            real_list = type(store)._list

            def names():
                clock[0] += 2  # Past FRESH_SECONDS: each query catches up.
                return [p.name for p, _ in store.query(topic_id='t')]

            def elsewhere(name, note=True):  # Another process's write_private_text.
                temp = path / f'.{name}.tmp'
                temp.write_text(json.dumps({'topic_id': 't'}))
                temp.replace(path / name)
                if note:
                    store_module.note_change(path, name)
            with mock.patch('covey.core.store.time.monotonic', lambda: clock[0]), \
                    mock.patch.object(type(store), '_list', lambda self: listings.append(1) or real_list(self)):
                self.assertEqual((names(), len(listings)), (['a.json'], 1))
                elsewhere('b.json')
                self.assertEqual((names(), len(listings)), (['a.json', 'b.json'], 1))  # From the new line.
                elsewhere('c.json', note=False)
                with (path / '.journal').open('a') as journal:
                    journal.write('c.js')  # A line still being appended: read next time, whole.
                self.assertEqual((names(), len(listings)), (['a.json', 'b.json'], 1))
                with (path / '.journal').open('a') as journal:
                    journal.write('on\n../escape.json\n.c.json.tmp\n')  # Not record names: ignored.
                self.assertEqual((names(), len(listings)), (['a.json', 'b.json', 'c.json'], 1))
                (path / '.journal').unlink()  # Started again (maintenance): listed once, then read anew.
                elsewhere('d.json')
                self.assertEqual((names(), len(listings)), (['a.json', 'b.json', 'c.json', 'd.json'], 2))
                elsewhere('e.json')
                self.assertEqual((names()[-1], len(listings)), ('e.json', 2))
                elsewhere('f.json', note=False)  # A write the journal missed: seen at the next full listing.
                self.assertEqual((names()[-1], len(listings)), ('e.json', 2))
                clock[0] += store_module.LIST_SECONDS
                self.assertEqual((names()[-1], len(listings)), ('f.json', 3))

    def test_a_new_process_starts_from_the_saved_index_and_reads_only_what_it_asks_for(self):
        # S46: a task runner read all ~1000 job records at its start; parsing them slowed it by 0.3-0.8 s.
        from covey.core import store as store_module
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'jobs'
            for number in range(1, 21):
                write_private_text(path / f'{number}.json', json.dumps({'topic_id': f't{number % 4}', 'job_id': number}))
            bridge = store_module.JsonDirectoryStore(path)  # Not directory_store: each one is another process.
            self.assertEqual(len(bridge.query()), 20)
            self.assertTrue(bridge.save_index(every=0))
            self.assertFalse(bridge.save_index(every=0))  # Nothing changed since.
            write_private_text(path / '21.json', json.dumps({'topic_id': 't1', 'job_id': 21}))  # After .index.
            write_private_text(path / '5.json', json.dumps({'topic_id': 't1', 'job_id': 55}))
            read, real = [], store_module.JsonDirectoryStore._read
            with mock.patch.object(store_module.JsonDirectoryStore, '_read',
                                   lambda self, file: read.append(file.name) or real(self, file)):
                runner = store_module.JsonDirectoryStore(path)
                runner.catch_up()
                self.assertEqual(sorted(read), ['21.json', '5.json'])  # Only what the journal names since .index.
                self.assertEqual(sorted(r['job_id'] for _, r in runner.query(topic_id='t1')), [1, 9, 13, 17, 21, 55])
                self.assertEqual(sorted(read), ['1.json', '13.json', '17.json', '21.json', '5.json', '9.json'])
                self.assertEqual(len(runner.query(topic_id='t2')), 5)
                self.assertEqual(len(read), 11)
                self.assertEqual(len(runner.query()), 21)  # Asking for all reads them all, as before.
            (path / '.index').write_text('{broken')
            fresh = store_module.JsonDirectoryStore(path)
            self.assertEqual(len(fresh.query(topic_id='t3')), 5)  # No usable .index: reads every record.
            self.assertEqual(len(fresh.records), 21)
            bridge.catch_up()  # The Bridge's next look, then its next save.
            self.assertTrue(bridge.save_index(every=0))
            (path / '.journal').unlink()  # Replaced since .index: listed, reading only changed inodes.
            write_private_text(path / '2.json', json.dumps({'topic_id': 't0', 'job_id': 22}))
            read.clear()
            with mock.patch.object(store_module.JsonDirectoryStore, '_read',
                                   lambda self, file: read.append(file.name) or real(self, file)):
                later = store_module.JsonDirectoryStore(path)
                later.catch_up()
            self.assertEqual(read, ['2.json'])
            self.assertEqual(later.fields['2.json']['job_id'], 22)

    def test_a_new_process_finds_jobs_by_a_set_of_statuses_and_the_last_job_number_from_the_index(self):
        # Repo review R4: the Bridge's start and a deploy's status read every job record (2.6 s for 1185 on
        # NFS) for the few running ones and the largest job number.
        from covey.app.detached import restart_blockers
        from covey.core import jobs, store as store_module
        from covey.core.statuses import RUNNING
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            statuses = ['done', 'running', 'queued', 'cancel_requested', 'failed', 'waiting_user', 'continued']
            # By hand: each record with its own updated_at, and one malformed (status 7) that no writer makes.
            for number in range(1, 22):
                write_private_text(job_file(state, f'k{number}'), json.dumps({
                    'key': f'k{number}', 'job_id': number, 'status': statuses[number % 7], 'backend': 'codex',
                    'updated_at': f'2026-10-28T10:{number:02}:00'}))
            write_private_text(job_file(state, 'odd'), json.dumps({'key': 'odd', 'job_id': '30', 'status': 7}))
            bridge = store_module.JsonDirectoryStore(state / 'jobs')
            self.assertEqual(len(bridge.query()), 22)
            self.assertTrue(bridge.save_index(every=0))
            read, real = [], store_module.JsonDirectoryStore._read
            with mock.patch.object(store_module.JsonDirectoryStore, '_read',
                                   lambda self, file: read.append(file.name) or real(self, file)), \
                    mock.patch.dict(store_module._stores, clear=True):
                self.assertEqual(jobs.max_job_id(state), 30)  # The digits of a string job_id count too.
                self.assertEqual(read, [])
                running = jobs.running_job_records(state)
                self.assertEqual([r['job_id'] for r in running], [17, 16, 15, 10, 9, 8, 3, 2, 1])  # Newest first.
                self.assertEqual(sorted(read), sorted(f'k{r["job_id"]}.json' for r in running))
                self.assertEqual(running, [r for r in jobs.list_job_records(state, limit=0)
                                           if r.get('status') in RUNNING])
            read.clear()
            with mock.patch.object(store_module.JsonDirectoryStore, '_read',
                                   lambda self, file: read.append(file.name) or real(self, file)), \
                    mock.patch.dict(store_module._stores, clear=True), \
                    mock.patch('covey.app.detached.live_keys', return_value={'k2'}):
                blockers = restart_blockers(state)  # A deploy's status: another process.
            self.assertEqual([b['job_id'] for b in blockers], [16, 15, 9, 8, 1])  # Not k2's: its runner lives.
            self.assertEqual(len(read), 9)
            self.assertEqual(sorted(bridge.query(status={'failed', 'continued'})),
                             sorted(bridge.query(status='failed') + bridge.query(status='continued')))
            self.assertEqual(bridge.query(status=set()), [])

    def test_the_boards_read_leaves_old_records_unread_and_says_the_same_as_reading_them_all(self):
        # P1: the butler's snapshot read ~3400 job records in a new process for the ~150 written within three
        # days; updated_at rides in .index (store.CARRIED) so board_read.read does not read the rest.
        import datetime
        from covey.core import board_read, store as store_module
        from covey.core.jobs import select_job_records
        from covey.core.butler import butler_topic_id
        now = time.time()

        def stamp(days):
            return datetime.datetime.fromtimestamp(now - days * 86400).isoformat()
        chat = 'chat-1'
        rows = [('old', 'done', 10, 't1'), ('recent', 'done', 1, 't1'), ('running', 'running', 20, 't2'),
                ('old-kept', 'done', 10, 't3'), ('main', 'done', 1, butler_topic_id(chat)), ('other', 'done', 1, 't4'),
                ('no-time', 'done', None, 't5'), ('odd-time', 'done', 7, 't6'), ('queued', 'queued', 2, 't7')]
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            for number, (key, status, days, topic) in enumerate(rows, 1):
                record = {'key': key, 'job_id': number, 'status': status, 'topic_id': topic,
                          'chat_id': 'chat-2' if key == 'other' else chat}
                if days is not None:
                    record['updated_at'] = stamp(days)
                if key == 'odd-time':
                    record['updated_at'] = 12345  # Not an ISO time: ``fresh`` takes it as now.
                write_private_text(job_file(state, key), json.dumps(record))
            bridge = store_module.JsonDirectoryStore(state / 'jobs')
            self.assertEqual(len(bridge.query()), len(rows))
            self.assertTrue(bridge.save_index(every=0))
            saved = json.loads((state / 'jobs' / '.index').read_text())
            self.assertEqual(saved['entries']['recent.json'][1]['updated_at'], stamp(1))
            self.assertEqual(saved['entries']['odd-time.json'][1]['updated_at'], 12345)
            self.assertNotIn('updated_at', saved['entries']['no-time.json'][1])
            self.assertNotIn(('updated_at', stamp(1)), bridge.index)  # Carried, not indexed.

            def run(keep=frozenset()):
                """(keys read, keys board_read.read returns, the same without ``narrow``), in a new process."""
                read, real = [], store_module.JsonDirectoryStore._read
                with mock.patch.object(store_module.JsonDirectoryStore, '_read',
                                       lambda self, file: read.append(file.name) or real(self, file)), \
                        mock.patch.dict(store_module._stores, clear=True):
                    got = board_read.read(state, chat, now, keep=keep).jobs
                    asked = sorted(read)
                with mock.patch.dict(store_module._stores, clear=True):
                    every = select_job_records(state, lambda r: board_read.listed(r.get('topic_id'), chat) and (
                        board_read.fresh(r, now) or r.get('topic_id') in keep), chat_id=chat)
                self.assertEqual(got, every)
                return asked, [r['key'] for r in got]
            asked, keys = run()
            self.assertEqual(sorted(keys), ['no-time', 'odd-time', 'queued', 'recent', 'running'])
            # Not read: the old one, the one of another chat, the butler's, and the not-kept old one.
            self.assertEqual(asked, ['no-time.json', 'odd-time.json', 'queued.json', 'recent.json', 'running.json'])
            asked, keys = run(keep=frozenset({'t3', 't1'}))
            self.assertEqual(sorted(keys), ['no-time', 'odd-time', 'old', 'old-kept', 'queued', 'recent', 'running'])
            self.assertEqual(len(asked), 7)
            # An .index from before updated_at was carried: the records decide, the answer is the same.
            for _, fields in saved['entries'].values():
                fields.pop('updated_at', None)
            (state / 'jobs' / '.index').write_text(json.dumps(saved))
            asked, keys = run()
            self.assertEqual(sorted(keys), ['no-time', 'odd-time', 'queued', 'recent', 'running'])
            self.assertEqual(len(asked), len(rows) - 2)  # All but the other chat's (indexed) and the butler's.
            # A ``narrow`` that cannot tell leaves the record to ``where``.
            def broken(fields):
                raise TypeError('cannot tell')
            with mock.patch.dict(store_module._stores, clear=True):
                self.assertEqual(len(select_job_records(state, None, narrow=broken)), len(rows))

    def test_a_private_write_skips_repeated_steps_but_remakes_a_removed_directory(self):
        # S39: each step of a write is a NAS round trip; about 20 ms a write.
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root) / 'acks'
            write_private_text(folder / 'a.json', '{}')
            shutil.rmtree(folder)
            with mock.patch.object(Path, 'unlink', autospec=True) as unlink:
                write_private_text(folder / 'b.json', '{"ok": true}')
            unlink.assert_not_called()  # The temp file became b.json: nothing to remove.
            self.assertEqual(stat.S_IMODE((folder / 'b.json').stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
            with mock.patch.object(Path, 'replace', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    write_private_text(folder / 'c.json', '{}')
            self.assertEqual(sorted(p.name for p in folder.iterdir()), ['b.json'])

    def test_a_workspace_check_copies_only_its_topics_records(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            upsert_job_record(state, 'mine', job_id=1, topic_id='t', status='waiting_workspace')
            for number in range(2, 12):
                upsert_job_record(state, f'other-{number}', job_id=number, topic_id='u', status='running')
            topic = write_topic(state, topic_id='t').parent
            ctx = SimpleNamespace(state_dir=state, topic_id='t', history_path=topic / 'history.jsonl',
                                  session_path=topic / 'codex_session_id')
            copied, patched = self.copies()
            with patched:
                self.assertFalse(workspace_locked(ctx))  # Another topic's running jobs do not settle this one.
                upsert_job_record(state, 'ran', job_id=12, topic_id='t', status='done')
                self.assertTrue(workspace_locked(ctx))
            self.assertEqual(copied, ['mine', 'mine', 'ran'])

    def test_the_outbox_tick_copies_only_the_results_it_sends_now(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            due = dict(outbox_version=1, delivery_ok=False, outbox_state='pending', outbox_next_at=0, backend='gone',
                       verified_reply_authorized=True, verified_reply_state='authorized')
            upsert_job_record(state, 'due', job_id=1, **due)
            upsert_job_record(state, 'later', job_id=2, **dict(due, outbox_next_at=time.time() + 60))
            upsert_job_record(state, 'paused', job_id=3, **dict(due, outbox_state='paused'))
            for number in range(4, 24):
                upsert_job_record(state, f'sent-{number}', job_id=number, outbox_version=1, delivery_ok=True)
            jobs = JobRegistry()
            copied, patched = self.copies()
            with patched:  # No Harness owns 'gone': the due result is read and left as it is.
                outbox.tick(SimpleNamespace(runtime=SimpleNamespace(jobs=jobs, log=print), apps={}),
                            SimpleNamespace(state_dir=state))
            self.assertEqual(copied, ['due'])

    def test_a_malformed_record_is_skipped_once_logged_and_the_tick_goes_on(self):
        # A field of the wrong type made due() raise: the tick stopped before any record (S21 review).
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            due = dict(outbox_version=1, delivery_ok=False, outbox_state='pending', outbox_next_at=0, backend='gone',
                       verified_reply_authorized=True, verified_reply_state='authorized')
            upsert_job_record(state, 'bad', job_id=1, **dict(due, outbox_next_at='soon'))
            upsert_job_record(state, 'due', job_id=2, **due)
            logs = []
            gateway = SimpleNamespace(runtime=SimpleNamespace(jobs=JobRegistry(),
                                                              log=logs.append), apps={})
            copied, patched = self.copies()
            with patched:
                for _ in range(2):
                    outbox.tick(gateway, SimpleNamespace(state_dir=state))
                self.assertEqual(copied, ['due', 'due'])
                self.assertEqual(logs, ['record jobs/bad.json skipped: TypeError'])  # Once, not every 5 s.
                upsert_job_record(state, 'bad', outbox_next_at='later')
                outbox.tick(gateway, SimpleNamespace(state_dir=state))
            self.assertEqual(logs, ['record jobs/bad.json skipped: TypeError'] * 2)  # Changed since: said again.

    def test_terminal_job_cannot_be_reopened_by_heartbeat(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            upsert_job_record(state, 'job', status='done', status_message='finished')
            record = upsert_job_record(state, 'job', status='running', status_message='heartbeat')
            self.assertEqual(record['status'], 'done')
            self.assertEqual(record['status_message'], 'finished')
            self.assertEqual(record['schema_version'], 1)

    def test_recent_history_does_not_open_old_archive(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'history.jsonl'
            archive = Path(str(path)+'.archive'); archive.mkdir()
            (archive/'old.jsonl.gz').write_bytes(b'not needed')
            path.write_text('\n'.join(json.dumps({'role':'user','content':str(i)}) for i in range(10000)))
            with mock.patch('covey.core.history_storage.gzip.open', side_effect=AssertionError('old archive read')):
                self.assertEqual([r['content'] for r in recent_history(path, 2)], ['9998','9999'])

    def test_reverse_history_preserves_unicode_and_reset(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'history.jsonl'
            values=[{'role':'user','content':'旧'}, {'type':'session_reset'}, {'role':'user','content':'长说明\n'+'中文'*6000}]
            path.write_text('\n'.join(json.dumps(r,ensure_ascii=False) for r in values))
            self.assertEqual(recent_history(path), values[-1:])

    def test_history_reader_does_not_hold_other_topic(self):
        from covey.core.history_storage import history_lines
        with tempfile.TemporaryDirectory() as root:
            a, b = Path(root)/'a.jsonl', Path(root)/'b.jsonl'
            a.write_text('{"role":"user","content":"A"}\n')
            b.write_text('{"role":"user","content":"B"}\n')
            reader = history_lines(a)
            next(reader)
            with ThreadPoolExecutor(1) as pool:
                future = pool.submit(recent_history, b)
                try:
                    self.assertEqual(future.result(1)[0]['content'], 'B')
                finally:
                    reader.close()
