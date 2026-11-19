"""Storage maintenance (core/maintenance.py) and archives and backups that stay readable (core/backup.py)."""
from __future__ import annotations

import gzip
import json
import os
import tarfile
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from covey.app.artifacts import artifact_key
from covey.core.history_storage import history_lines
from covey.core.checkpoint import read_checkpoint
from covey.core.backup import restore_backup, verify_backup
from covey.core.maintenance import maintain
from covey.core.history_storage import recent_history
from support.records import write_job


def user_questions(path):
    rows = (json.loads(line) for line in history_lines(path) if line.strip())
    return [row["content"] for row in rows if row.get("role") == "user"]


class ArchiveTest(unittest.TestCase):
    def test_archives_remain_readable_and_backup_restores(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            topic = state / "topics" / "topic"
            topic.mkdir(parents=True)
            history = topic / "history.jsonl"
            rows = [{"role": "user", "content": f"问题{i}"} for i in range(20)]
            history.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
            checkpoint = state / "checkpoints" / "one.json"
            checkpoint.parent.mkdir()
            checkpoint.write_text('{"phase":"done"}')
            os.utime(checkpoint, (1, 1))
            jobs = state / "jobs"
            write_job(state, "one", job_id=1, topic_id="topic", status="done", checkpoint_path=str(checkpoint))
            preview = maintain(state, history_bytes=1)
            self.assertTrue(preview["archive"])
            self.assertTrue(history.exists())
            report = maintain(state, apply=True, history_bytes=1)
            self.assertEqual(user_questions(history), [r["content"] for r in rows])
            self.assertEqual(len(recent_history(history)), 12)
            record = json.loads((jobs / "one.json").read_text())
            self.assertEqual(read_checkpoint(Path(record["checkpoint_path"]))["phase"], "done")
            archive = Path(report["backup"])
            self.assertTrue(verify_backup(archive))
            restored = root / "restored"
            restore_backup(archive, restored)
            restored_job = json.loads((restored / "jobs" / "one.json").read_text())
            self.assertTrue(Path(restored_job["checkpoint_path"]).is_relative_to(restored))
            self.assertTrue(Path(restored_job["checkpoint_path"]).exists())
            with self.assertRaises(ValueError):
                restore_backup(archive, restored)

    def test_active_topics_are_not_archived(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            topic = state / "topics" / "a"
            topic.mkdir(parents=True)
            (topic / "history.jsonl").write_text("x" * 100)
            write_job(state, "one", job_id=1, topic_id="a", status="waiting_user")
            self.assertFalse(maintain(state, history_bytes=1)["archive"])

    def test_crash_left_raw_archive_is_read_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.jsonl"
            folder = path.parent / (path.name + ".archive")
            folder.mkdir()
            content = json.dumps({"role": "user", "content": "once"}) + "\n"
            (folder / "001.jsonl").write_text(content)
            with gzip.open(folder / "001.jsonl.gz", "wt") as out:
                out.write(content)
            self.assertEqual(user_questions(path), ["once"])


DAY = 86400


class StorageMaintenanceTest(unittest.TestCase):
    """Daily maintenance: a backup copied without the global lock, and bounded storage."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name).resolve() / "state"
        (self.state / "jobs").mkdir(parents=True)

    def write(self, rel, text):
        # A state tree as maintenance finds it, records of any age and release with their own updated_at and
        # mtimes (upsert_job_record would stamp them now): by hand, not through support/records.py.
        path = self.state / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def test_each_kind_of_stored_data_is_one_rule_the_cleanup_and_the_dry_run_take_once(self):
        """Guard (D4): the plan's keys are the rules' keys in RULES' order; the cleanup carries out every rule
        that acts once, and the dry run lists every rule in the plan once."""
        from covey.core import maintenance_rules
        rules = maintenance_rules.RULES
        self.assertEqual(list(maintain(self.state)), [key for rule in rules for key in rule.keys] + ["backup"])
        acting = [rule for rule in rules if type(rule).apply is not maintenance_rules.Rule.apply]
        cleanup = [rule for rule in maintenance_rules.CLEANUP if rule in rules]
        self.assertEqual((len(cleanup), set(cleanup)), (len(acting), set(acting)))
        self.assertEqual((len(maintenance_rules.DRY_RUN), set(maintenance_rules.DRY_RUN)),
                         (len([r for r in rules if r.keys]), {r for r in rules if r.keys}))

    def test_the_backup_is_copied_without_the_global_lock_and_leaves_out_job_diffs(self):
        from covey.core import backup
        from covey.core.history_storage import HISTORY_LOCK
        from covey.core.locks import LOCK
        self.write("jobs/one.json", json.dumps({"key": "one", "job_id": 1, "topic_id": "t", "status": "done"}))
        # A history being appended to: the copy ends at its last complete line.
        self.write("topics/t/history.jsonl", '{"role": "user", "content": "a"}\n{"role": "us')
        self.write("job-diffs/one/diff.md", "+changed")
        free = []

        def probe():
            if LOCK.acquire(timeout=1):  # Another thread: a submission registering its job.
                LOCK.release()
                with HISTORY_LOCK.shared():  # A history read or append.
                    free.append(True)

        def copy(path):
            thread = threading.Thread(target=probe)
            thread.start()
            thread.join(5)
            return original(path)

        original = backup._backup_bytes
        with mock.patch.object(backup, "_backup_bytes", copy):
            archive = Path(maintain(self.state, apply=True, history_bytes=1 << 30)["backup"])
        self.assertTrue(free)
        self.assertEqual(len(free), len(backup.backup_paths(self.state)))
        manifest = verify_backup(archive)
        self.assertNotIn("job-diffs/one/diff.md", manifest)
        restored = archive.parent.parent.parent / "restored"
        restore_backup(archive, restored)
        self.assertEqual((restored / "topics/t/history.jsonl").read_text(), '{"role": "user", "content": "a"}\n')

    def test_a_job_registered_meanwhile_leaves_the_rest_for_the_next_day(self):
        history = self.write("topics/t/history.jsonl", '{"role": "user", "content": "a"}\n')
        plan = maintain(self.state, apply=True, history_bytes=1, idle=lambda: False, close_stale=mock.Mock())
        self.assertTrue(plan["deferred"] and plan["backup"])
        self.assertTrue(history.exists())  # Not archived while a job may append to it.
        self.assertEqual(plan["closed"], [])
        status = json.loads((self.state / "maintenance-status.json").read_text())
        self.assertTrue(status["last_completed"])  # Today's backup is not repeated every hour.

    def test_proxy_backs_up_only_its_settings_and_ages_out_shots_and_ended_batches(self):
        # 2026-10-27, the owner: back up 代答's settings, whitelist and style note, never its web login;
        # screenshots go after a week, ended batches after 30 days, open ones never.
        import time
        self.write("proxy/settings.json", json.dumps({"enabled": True}))
        self.write("proxy/contacts/a.json", json.dumps({"key": "p2p:ou_a"}))
        self.write("proxy/style.md", "很短")
        self.write("proxy/owner-context.md", "炼化：把人沉淀进 Agent")  # T571: what the owner works on.
        self.write("proxy/web/profile/Default/Cookies", "secret")
        shot = self.write("proxy/shots/old-verify.png", "x")
        old_sent = self.write("proxy/batches/old-sent.json", json.dumps({"id": "old-sent", "status": "sent"}))
        old_held = self.write("proxy/batches/old-held.json", json.dumps({"id": "old-held", "status": "held"}))
        new_sent = self.write("proxy/batches/new-sent.json", json.dumps({"id": "new-sent", "status": "sent"}))
        later = time.time() + 31 * DAY
        os.utime(new_sent, (later, later))
        plan = maintain(self.state, apply=True, now=later)
        self.assertEqual(plan["remove_proxy_batches"], [str(old_sent)])
        self.assertIn(str(shot), plan["remove_temporary"])
        self.assertFalse(old_sent.exists() or shot.exists())
        self.assertTrue(old_held.exists() and new_sent.exists())
        with tarfile.open(plan["backup"], "r:gz") as tar:
            names = sorted(m.name for m in tar.getmembers() if m.name.startswith("proxy/"))
        self.assertEqual(names, ["proxy/contacts/a.json", "proxy/owner-context.md", "proxy/settings.json", "proxy/style.md"])

    def test_temporary_entries_untouched_for_a_week_go_unless_a_process_uses_them(self):
        old = self.write("tmp/old-cache/deep/file", "x")
        held = self.write("tmp/held/file", "x")
        fresh = self.write("tmp/fresh/file", "x")
        seen = self.write("inbound-messages/om_seen", "")
        later = __import__("time").time() + 8 * DAY
        os.utime(fresh, (later, later))
        with held.open() as handle:  # Open in this process.
            plan = maintain(self.state, apply=True, now=later)
            self.assertTrue(handle.readable())
        self.assertEqual(sorted(Path(p).name for p in plan["remove_temporary"]), ["old-cache", "om_seen"])
        self.assertFalse(old.parent.parent.exists() or seen.exists())
        self.assertTrue(held.exists() and fresh.exists())

    def test_each_diff_is_kept_once_and_job_diffs_stay_bounded(self):
        from covey.core import maintenance_rules
        fat = {"files": [{"path": "a.py", "status": "M", "diff_text": "+x" * 100, "additions": 1, "deletions": 0,
                          "inline_text": ""}], "summary": {"file_count": 1}}
        index = self.write("job-diffs/new/diff.json", json.dumps(fat, indent=2))
        self.write("job-diffs/new/diff.md", "+x" * 100)
        gone = self.write("job-diffs/old/diff.md", "+y")
        now = __import__("time").time()
        os.utime(gone, (now - 31 * DAY, now - 31 * DAY))
        plan = maintain(self.state, apply=True, now=now)
        self.assertEqual((plan["remove_diffs"], plan["slim_diffs"]), ([str(gone.parent)], [str(index)]))
        self.assertFalse(gone.parent.exists())
        self.assertEqual(json.loads(index.read_text())["files"],
                         [{"path": "a.py", "status": "M", "additions": 1, "deletions": 0}])
        # Over the total size the oldest uploaded ones go first, but never one written in the last day.
        week = self.write("job-diffs/week/diff.md", "+z" * 100)
        os.utime(week, (now - 3 * DAY, now - 3 * DAY))
        with mock.patch.object(maintenance_rules, "DIFF_BYTES", 10), \
                mock.patch.object(maintenance_rules, "diff_uploaded", lambda state, folder: True):
            self.assertEqual(maintain(self.state, now=now)["remove_diffs"], [str(week.parent)])

    def test_lock_files_of_records_untouched_for_a_week_go_unless_held(self):
        from covey.core.locks import shared_file_lock
        now = __import__("time").time() + 8 * DAY
        record = self.write("jobs/old.json", json.dumps({"key": "old", "status": "done"}))
        held = self.write("questions/held.json", json.dumps({"token": "held", "status": "answered"}))
        with shared_file_lock(record):
            pass
        orphan = self.write("interaction-deliveries/.locks/gone.json.lock", "")
        with shared_file_lock(held):
            plan = maintain(self.state, apply=True, now=now)
        self.assertEqual(len(plan["remove_locks"]), 3)
        self.assertFalse((record.parent / ".locks" / "old.json.lock").exists())
        self.assertFalse(orphan.exists())
        self.assertTrue((held.parent / ".locks" / "held.json.lock").exists())  # Its holder keeps it.
        with shared_file_lock(record):  # Created again on demand.
            self.assertTrue((record.parent / ".locks" / "old.json.lock").exists())

    def test_lock_files_of_cards_go_unless_held(self):
        # Repo review E4: cards/ holds only card lock files, one per card written; none stays once unheld.
        from covey.core import locks
        self.addCleanup(locks.configure_card_locks, locks.configure_card_locks(self.state))
        with locks.resource_lock("card", "c1"):
            pass
        with locks.resource_lock("card", "c2"):
            plan = maintain(self.state, apply=True)
        folder = self.state / "cards" / ".locks"
        self.assertEqual(sorted(Path(name).name for name in plan["remove_locks"]), ["c1", "c2"])
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["c2.lock"])  # Its holder keeps it.

    def test_a_lock_file_removed_while_waiting_for_it_is_not_taken_as_held(self):
        from covey.core import locks
        record = self.write("jobs/r.json", "{}")
        lockfile = record.parent / ".locks" / "r.json.lock"
        real, waited = locks.fcntl.flock, []

        def flock(fd, operation):
            real(fd, operation)
            if not waited:  # Maintenance removed it, and another process made it again, meanwhile.
                waited.append(fd)
                lockfile.unlink()
                lockfile.touch()

        with mock.patch.object(locks.fcntl, "flock", flock), locks.shared_file_lock(record) as lock:
            self.assertEqual(os.fstat(lock.fd).st_ino, lockfile.stat().st_ino)
        self.assertEqual(len(waited), 1)

    def loop_once(self, busy, **kwargs):
        """One pass of the Bridge's maintenance loop; its log lines."""
        from covey.core.maintenance import maintenance_loop
        logs = []
        runtime = mock.Mock(log=logs.append)
        runtime.shutdown.is_set.side_effect = [False, True]
        runtime.shutdown.wait.return_value = True
        runtime.jobs.busy.return_value = busy
        maintenance_loop(runtime, mock.Mock(state_dir=self.state), **kwargs)
        return logs

    def status(self):
        return json.loads((self.state / "maintenance-status.json").read_text())

    def test_the_daily_backup_is_made_while_jobs_run_and_the_cleanup_waits_for_none(self):
        # Repo review E6: maintenance waited for no job, backups included; #1051's 6 hours and the like left
        # 10-26 without one. The backup only reads.
        import time
        history = self.write("topics/t/history.jsonl", '{"role": "user", "content": "a"}\n')
        os.utime(history, (time.time() - 31 * DAY, time.time() - 31 * DAY))  # Due for its archive.
        before = time.time()
        prune = mock.Mock()
        logs = self.loop_once(busy=True, prune_web=prune)
        prune.assert_called_once_with()  # The web login's caches are looked at each pass, jobs or not.
        [backup] = list((self.state / "backups").glob("*.tar.gz"))
        verify_backup(backup)
        self.assertEqual(logs, [f"maintenance backup completed: {backup.name}; jobs are running, the cleanup waits "
                                "until none is"])
        self.assertGreaterEqual(self.status()["last_backup"], before)
        self.assertNotIn("last_completed", self.status())
        self.assertTrue(history.exists())  # Not archived while a job may append to it.
        # Busy again within the day: nothing more.
        self.assertEqual(self.loop_once(busy=True), [])
        # Idle: the cleanup, without a second backup today.
        logs = self.loop_once(busy=False)
        self.assertEqual(logs[0], "idle maintenance archive completed (today's backup was made already)")
        self.assertEqual(len(list((self.state / "backups").glob("*.tar.gz"))), 1)
        self.assertFalse(history.exists())
        status = self.status()
        self.assertGreaterEqual(status["last_completed"], status["last_backup"])
        self.assertIsNone(status["plan"]["backup"])
        # A day later, idle: both again in one run, as before.
        later = status["last_completed"] + DAY + 1
        with mock.patch("covey.core.maintenance.time.time", return_value=later):
            logs = self.loop_once(busy=False)
        self.assertEqual(logs[0], "idle maintenance backup/archive completed")
        self.assertEqual((self.status()["last_backup"], self.status()["last_completed"]), (later, later))

    def test_an_older_status_without_a_backup_time_counts_its_maintenance_as_the_backup(self):
        import time
        self.write("maintenance-status.json", json.dumps({"last_completed": time.time() - 3600, "plan": {}}))
        self.assertEqual(self.loop_once(busy=True), [])
        self.write("maintenance-status.json", json.dumps({"last_completed": time.time() - DAY - 1, "plan": {}}))
        self.assertTrue(self.loop_once(busy=True)[0].startswith("maintenance backup completed: bridge-"))

    def test_the_web_logins_caches_go_past_their_cap_and_nothing_else_does(self):
        # Repo review E6: proxy/web/profile, the owner's only kept web login, grew to 780 MB in a day.
        from covey.core.web_profile import WEB_CACHE_BYTES, prune_web_caches, web_profile
        profile = web_profile(self.state)
        self.assertEqual(profile, self.state / "proxy" / "web" / "profile")
        kept = ["Default/Cookies", "Default/Local Storage/leveldb/000003.log", "Default/IndexedDB/x.leveldb/000005.ldb",
                "Default/Session Storage/LOG", "Default/Preferences", "Local State"]
        caches = ["Default/Cache/Cache_Data/f_000001", "Default/Code Cache/js/abc", "Default/GPUCache/data_0",
                  "Default/Service Worker/CacheStorage/x/y", "Default/Service Worker/Database/LOG",
                  "GrShaderCache/data_1"]
        for rel in kept + caches:
            self.write(f"proxy/web/profile/{rel}", "x" * 1000)
        self.assertEqual(prune_web_caches(profile), 0)  # Below WEB_CACHE_BYTES: nothing goes.
        self.assertTrue(all((profile / rel).exists() for rel in caches))
        self.assertEqual(prune_web_caches(profile, cap=6000), 6000)
        self.assertEqual([rel for rel in kept if not (profile / rel).exists()], [])
        self.assertEqual([rel for rel in caches if (profile / rel).exists()], [])
        self.assertEqual(sorted(p.name for p in (profile / "Default").iterdir()),
                         ["Cookies", "IndexedDB", "Local Storage", "Preferences", "Session Storage"])
        self.assertEqual(WEB_CACHE_BYTES, 200 * 1024 * 1024)

    def test_the_dry_run_says_when_the_backup_is_due_and_what_the_web_login_holds(self):
        from covey.core.maintenance import dry_run_report
        now = 1_800_000_000.0
        self.write("maintenance-status.json", json.dumps({"last_completed": now - 2 * DAY, "last_backup": now - 3600}))
        self.write("proxy/web/profile/Default/Cache/data", "x" * 2048)
        self.write("proxy/web/profile/Default/IndexedDB/a.ldb", "x" * 4096)
        with mock.patch("covey.core.maintenance.WEB_CACHE_BYTES", 1024):
            report = dry_run_report(self.state, now=now).splitlines()
        when = lambda at: __import__("datetime").datetime.fromtimestamp(at).isoformat(timespec="seconds")
        self.assertEqual(report[2], f"上次备份 {when(now - 3600)}，下次 {when(now - 3600 + DAY)}；有任务运行也照做（只读）。")
        self.assertEqual(report[3], f"上次清理 {when(now - 2 * DAY)}；Bridge 在距上次满一天且没有任务时执行，"
                                    "有任务开始时其余清理顺延。")
        self.assertEqual(report[4], "代答网页端登录 proxy/web/profile：缓存类目录共 2.0 KiB（上限 1.0 KiB，超过，Bridge 在"
                                    "没有 sending 批次、浏览器不在用时删除）：Default/Cache 2.0 KiB；其余（cookie、"
                                    "Local Storage、IndexedDB 等）4.0 KiB 不动。")

    def test_a_browser_on_the_web_login_is_seen(self):
        from covey.core.web_profile import browser_on
        profile = self.state / "proxy" / "web" / "profile"
        cookies = self.write("proxy/web/profile/Default/Cookies", "x")
        # The processes of this user as /proc lists them: this machine's own may be sending a 代答 reply now.
        procs = self.state / "proc"
        self.write("proc/101/cmdline", "bash\0-c\0sleep 30; true # covey_feishu_web.py named in a command\0")
        with mock.patch("covey.core.web_profile.PROC", procs):
            self.assertFalse(browser_on(profile))
            with cookies.open():  # Chromium holds its files open (read from the real /proc).
                self.assertTrue(browser_on(profile))
            self.write("proc/102/cmdline", "/venv/bin/python\0-B\0/r/bin/covey_feishu_web.py\0send\0--home\0/w\0")
            self.assertTrue(browser_on(profile))

    def test_backups_are_bounded_by_count_and_total_size_keeping_the_newest(self):
        from covey.core.backup import old_backups
        folder = self.state / "backups"
        folder.mkdir()
        for n, size in enumerate([5, 5, 5, 50]):  # The newest is the largest.
            (folder / f"bridge-{n + 10}.tar.gz").write_bytes(b"x" * size)
        names = lambda paths: [Path(p).name for p in paths]
        self.assertEqual(names(old_backups(self.state, keep=2, max_bytes=1000)), ["bridge-11.tar.gz", "bridge-10.tar.gz"])
        self.assertEqual(names(old_backups(self.state, keep=7, max_bytes=58)),
                         ["bridge-11.tar.gz", "bridge-10.tar.gz"])
        self.assertEqual(names(old_backups(self.state, keep=7, max_bytes=1)),
                         ["bridge-12.tar.gz", "bridge-11.tar.gz", "bridge-10.tar.gz"])

    def test_waiting_records_with_a_week_of_no_activity_are_stale(self):
        from covey.core.stale import stale_records
        now = 1_800_000_000
        old, recent = "2026-10-01T00:00:00Z", "2027-01-15T08:00:00Z"  # now is 2027-01-15 08:00 UTC
        records = [{"key": "launch", "status": "waiting_workspace", "updated_at": old},
                   {"key": "asked", "status": "waiting_user", "updated_at": old},
                   {"key": "recent-q", "status": "waiting_user", "updated_at": old},
                   {"key": "live", "status": "waiting_user", "updated_at": old},
                   {"key": "running", "status": "running", "updated_at": old},
                   {"key": "done", "status": "continued", "updated_at": old},
                   {"key": "fresh", "status": "waiting_workspace", "updated_at": recent}]
        records.append({"key": "unsure", "status": "waiting_user", "updated_at": old})
        for token, key, status, created in (("q1", "asked", "pending", 1), ("q2", "recent-q", "pending", now - 60),
                                            ("q3", "running", "pending", 1), ("q4", "done", "uncertain", 1),
                                            ("q5", "done", "answered", 1), ("q6", "unsure", "uncertain", 1)):
            self.write(f"questions/{token}.json", json.dumps({"token": token, "job_key": key, "status": status,
                                                              "created_at": created}))
        stale = stale_records(self.state, records, {"live"}, now)
        # An answer whose delivery is unconfirmed is never closed as 未处理, nor the job it answers.
        self.assertEqual((stale["jobs"], stale["questions"]), (["launch", "asked"], ["q1"]))
        self.assertEqual(list(stale["since"]["questions"]), ["q1"])
        self.assertLess(stale["since"]["jobs"]["asked"], now - 7 * DAY)

    def test_a_waiting_message_whose_launch_card_never_arrived_is_stale_after_a_day(self):
        from covey.core.maintenance import dry_run_report
        from covey.core.stale import stale_records
        now = 1_800_000_000
        two_days = "2027-01-13T08:00:00Z"  # now is 2027-01-15 08:00 UTC
        error = {"launch_delivery_error": "2200 Something went wrong"}
        records = [{"key": key, "job_id": n, "status": "waiting_workspace", "topic_id": topic, "chat_id": "c",
                    "updated_at": two_days, "created_at": two_days, **extra}
                   for n, (key, topic, extra) in enumerate((
                       ("lost", "t1", error), ("shown", "t2", {}), ("lost-but-startable", "t3", error),
                       ("startable-elsewhere", "t5", error), ("before-the-field", "t6", {}),
                       ("cut-off-publish", "t7", error), ("failed-then-shown", "t9", {})), 1)]
        # t3 has a launch card that did arrive (a later message's): it waits the full week. t5's is in
        # another chat, so it cannot start t5's message. t6 and t9 only have the form's delivery_failed (an
        # older release, live #1774); t9 also has a card that arrived. t7's publish stopped two days ago.
        for name, topic, chat, status in (("f3", "t3", "c", "ready"), ("f5", "t5", "d", "ready"),
                                          ("f6", "t6", "c", "delivery_failed"), ("f7", "t7", "c", "publishing"),
                                          ("f9a", "t9", "c", "delivery_failed"), ("f9b", "t9", "c", "ready")):
            path = self.write(f"launch-forms/{name}.json",
                              json.dumps({"topic_id": topic, "chat_id": chat, "status": status}))
            os.utime(path, (now - 2 * DAY, now - 2 * DAY))
        stale = stale_records(self.state, records, set(), now)
        picked = ["lost", "startable-elsewhere", "before-the-field", "cut-off-publish"]
        self.assertEqual(stale["jobs"], picked)
        self.assertEqual(stale["undelivered"], picked)
        for record in records:
            self.write(f"jobs/{record['key']}.json", json.dumps(record))
        report = dry_run_report(self.state, now=now)
        self.assertIn("任务 #1 waiting_workspace 话题 t1 2.0 天无动静 启动卡未送达（1 天规则） → ", report)

    def test_any_use_of_the_topic_counts_as_activity(self):
        from covey.core.stale import stale_records
        now = __import__("time").time()
        old = now - 30 * DAY
        records = [{"key": key, "status": status, "topic_id": topic, "updated_at": old, "created_at": old}
                   for key, status, topic in (("held", "waiting_user", "t1"), ("edited", "waiting_workspace", "t2"),
                                              ("steered", "waiting_user", "t3"), ("idle", "waiting_user", "t4"))]
        # Yesterday: a note held in t1, the launch card of t2 edited, a 补充 message in t3.
        for rel, topic, when in (("held-notes/n1.json", "t1", now - DAY), ("launch-forms/f2.json", "t2", now - DAY),
                                 ("interaction-deliveries/d3.json", "t3", now - DAY), ("launch-forms/f4.json", "t4", old)):
            path = self.write(rel, json.dumps({"topic_id": topic, "chat_id": "c", "created_at": old}))
            os.utime(path, (when, when))
        self.assertEqual(stale_records(self.state, records, set(), now)["jobs"], ["idle"])

    def test_an_interrupted_backup_never_looks_complete_and_its_leftovers_go(self):
        from covey.core import backup
        self.write("jobs/one.json", json.dumps({"key": "one", "status": "done"}))
        real = Path.replace

        def stop_at_the_archive(path, target):
            if str(target).endswith(".tar.gz"):
                raise KeyboardInterrupt  # The Bridge stopped mid-backup.
            return real(path, target)

        with mock.patch.object(Path, "replace", stop_at_the_archive), self.assertRaises(KeyboardInterrupt):
            backup.create_backup(self.state)
        folder = self.state / "backups"
        self.assertEqual(sorted(p.suffix for p in folder.iterdir()), [".json", ".json", ".tmp"])
        [left] = folder.glob("*.tmp")
        base = left.name.removesuffix(".tmp")
        now = __import__("time").time()
        # While it may still be in progress it is never removed, but its size counts.
        (folder / "bridge-10.tar.gz").write_bytes(b"x" * 5)
        (folder / "bridge-11.tar.gz").write_bytes(b"x" * 5)
        size = sum(p.stat().st_size for p in folder.glob(base + ".*"))
        self.assertEqual([Path(n).name for n in backup.old_backups(self.state, max_bytes=size + 5, now=now)],
                         ["bridge-10.tar.gz"])
        self.assertEqual(backup.old_backups(self.state, now=now), [])
        later = now + 2 * DAY
        self.assertEqual(backup.old_backups(self.state, now=later), [str(folder / base)])
        plan = maintain(self.state, apply=True, now=later)
        self.assertIn(str(folder / base), plan["remove_backups"])
        self.assertFalse(list(folder.glob(base + ".*")))
        newest = Path(plan["backup"])  # Complete: renamed only once its manifest was written and checked.
        self.assertTrue(verify_backup(newest))

    def test_the_size_cap_keeps_a_diff_whose_upload_is_not_confirmed(self):
        import hashlib
        from covey.core import maintenance_rules
        from covey.feishu.delivery import safe_idempotency_key
        now = __import__("time").time()
        long_key = "om_" + "x" * 60  # Its ledger is named by a digest.
        for key, text, upload in (("uploaded", "+a", "same"), (long_key, "+b", "same"), ("failed", "+c", None),
                                  ("changed", "+d", "other"), ("ancient", "+e", None)):
            diff = self.write(f"job-diffs/{key}/diff.md", text)
            age = 31 if key == "ancient" else 3
            os.utime(diff, (now - age * DAY, now - age * DAY))
            self.write(f"jobs/{key}.json", json.dumps({"key": key, "status": "done", "diff_artifact_path": str(diff)}))
            files = {} if upload is None else {artifact_key(str(diff)): {"sha256": hashlib.sha256(
                (text if upload == "same" else "+old").encode()).hexdigest(), "url": "u"}}
            ledger = safe_idempotency_key(key, fallback="codex-lark")
            self.write(f"artifact-drive/{ledger}.json", json.dumps({"status": "done" if files else "failed",
                                                                   "files": files}))
        with mock.patch.object(maintenance_rules, "DIFF_BYTES", 1):
            removed = maintain(self.state, now=now)["remove_diffs"]
        self.assertEqual(sorted(Path(n).name for n in removed), sorted(["ancient", "uploaded", long_key]))

    def test_lock_files_go_whatever_release_a_live_runner_has(self):
        # Repo review R2: lock files stayed while a runner of a release without the re-check (af89642)
        # ran; 10-28 no live runner was one (scripts/compat_report.py lock-recheck), so that wait is gone.
        from covey.core import maintenance_rules
        old = self.write("releases/af89642/covey/core/locks.py", "def _open_locked(self): pass\n").parents[2]
        self.write("jobs/.locks/old.json.lock", "")
        run = self.write("runs/k/runner.json", json.dumps({"key": "k", "release": str(old)})).parent
        later = __import__("time").time() + 8 * DAY
        with mock.patch.object(maintenance_rules, "runner_alive", lambda path: path == run):
            plan = maintain(self.state, now=later)
        self.assertEqual(([Path(n).name for n in plan["remove_locks"]], "old_release_runners" in plan), (["old.json"], False))

    def test_finished_tasks_process_records_a_month_old_are_gzipped_and_still_read(self):
        """REVIEW2 batch 2: traces/ grew 5-10 MB a day. They are task records, so only compressed."""
        import time
        from covey.core import maintenance, maintenance_rules
        from covey.core.trace_store import load_trace, save_trace, trace_path
        later = time.time() + 31 * DAY
        snapshot = {"steps": {"s": {"status": "done", "detail": "读取文件"}}, "started_at": 1.0}
        for key, status in (("done", "done"), ("busy", "running"), ("asked", "waiting_user"), ("fresh", "done"),
                            ("runner", "cancelled")):
            self.write(f"jobs/{key}.json", json.dumps({"key": key, "job_id": 1, "topic_id": key, "status": status}))
            save_trace(self.state, key, snapshot)
        os.utime(trace_path(self.state, "fresh"), (later - DAY, later - DAY))
        self.write("runs/runner/runner.json", json.dumps({"key": "runner"}))
        alive = lambda run: Path(run).name == "runner"
        with mock.patch.object(maintenance_rules, "runner_alive", side_effect=alive):
            planned = maintain(self.state, now=later)["compress_traces"]
            self.assertEqual(planned, [("done", str(trace_path(self.state, "done")))])
            plan = maintain(self.state, apply=True, idle=lambda: True, now=later)
        self.assertEqual(sorted(p.name for p in (self.state / "traces").iterdir()),
                         ["asked.jsonl", "busy.jsonl", "done.jsonl.gz", "fresh.jsonl", "runner.jsonl"])
        self.assertEqual(load_trace(self.state, {"key": "done"}), load_trace(self.state, {"key": "busy"}))
        self.assertFalse(plan["deferred"])
        # A task that became active again after the plan keeps its plain file.
        save_trace(self.state, "again", snapshot)
        self.write("jobs/again.json", json.dumps({"key": "again", "job_id": 2, "topic_id": "a", "status": "done"}))
        planned = maintain(self.state, now=later)["compress_traces"]
        self.write("jobs/again.json", json.dumps({"key": "again", "job_id": 2, "topic_id": "a", "status": "queued"}))
        maintenance._cleanup(self.state, {**maintain(self.state, now=later), "compress_traces": planned},
                             lambda: True, later)
        self.assertTrue(trace_path(self.state, "again").is_file())

    def test_a_dry_run_lists_what_a_run_would_do_and_writes_nothing(self):
        import contextlib
        import hashlib
        import io
        import sys
        from covey.core import maintenance, stale
        later = __import__("time").time() + 31 * DAY  # When the run happens: everything is a month old.
        old = self.write("topics/h/history.jsonl", '{"role": "user", "content": "a"}\n')
        self.write("topics/t/topic.json", json.dumps({"topic_id": "t", "topic_number": 3}))
        checkpoint = self.write("checkpoints/c.json", "{}")
        self.write("jobs/done.json", json.dumps({"key": "done", "job_id": 1, "topic_id": "u", "status": "done",
                                                 "checkpoint_path": str(checkpoint)}))
        self.write("jobs/asked.json", json.dumps({"key": "asked", "job_id": 2, "topic_id": "t", "chat_id": "c",
                                                  "status": "waiting_user", "card_id": "tc",
                                                  "updated_at": "2026-01-01T00:00:00Z", "created_at": 1}))
        self.write("jobs/.locks/done.json.lock", "")
        self.write("traces/done.jsonl", '{"v": 1, "set": [], "full": true}\n')
        self.write("tmp/cache/file", "x" * 10)
        self.write("runs/ended/runner.log", "log")
        self.write("job-diffs/gone/diff.md", "+x")
        for n in range(8):
            self.write(f"backups/bridge-{n + 10}.tar.gz", "x" * 3)
        for folder in [self.state, *self.state.rglob("*")]:  # Not private yet whatever the umask: a chmod shows.
            if folder.is_dir():
                folder.chmod(0o755)

        def content(path):  # Read without touching its access time, which tmp/ cleanup counts as use.
            fd = os.open(path, os.O_RDONLY | os.O_NOATIME)
            with os.fdopen(fd, "rb") as handle:
                return hashlib.sha256(handle.read()).hexdigest()

        def snapshot():
            rows = {}
            for path in [self.state, *self.state.rglob("*")]:
                info = path.lstat()
                rows[str(path)] = (info.st_mode, info.st_size, info.st_mtime_ns, content(path) if path.is_file() else None)
            return rows

        before = snapshot()
        out = io.StringIO()
        argv = ["covey_maintenance.py", "--state-dir", str(self.state), "--dry-run"]
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(out), \
                mock.patch.object(maintenance.time, "time", return_value=later):
            maintenance.main()
        self.assertEqual(snapshot(), before)  # Not a lock file, backup or status file more.
        report = out.getvalue()
        plan = maintain(self.state, now=later)
        self.assertIn("压缩已结束任务的过程记录（traces/ 转 .jsonl.gz）：1 个", report)
        for rel in ("topics/h/history.jsonl", "checkpoints/c.json", "traces/done.jsonl", "tmp/cache", "runs/ended",
                    "job-diffs/gone", "backups/bridge-10.tar.gz", "backups/bridge-11.tar.gz"):
            self.assertIn(rel, report)
        self.assertNotIn("bridge-12.tar.gz", report)  # 7 kept, counting the run's new backup.
        self.assertIn("删除锁文件：1 个", report)
        self.assertIn("任务 #2 asked waiting_user 话题 t（#3）", report)
        self.assertIn(f"任务卡顶部状态行改为“{stale.STALE_NOTE}”", report)
        self.assertEqual((len(plan["archive"]), plan["stale_jobs"]), (1, ["asked"]))
        self.assertTrue(old.exists())
