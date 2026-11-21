"""The Manager tells the owner when his bot stays down and when it is back (service/down_notice.py, COVEY_DOWN_NOTICE)."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from support import exec_tempdir
from covey.service import down_notice
from covey.service.down_notice import DownNotice
from covey.service.manager import Manager, process_identity, read_json, register, service_key, terminate_owned

OWNER = "ou_owner0001"
FAKE_CLI = """#!{python}
import json, os, sys, time
with open({calls!r}, "a") as out:
    out.write(json.dumps(sys.argv[1:], ensure_ascii=False) + "\\n")
time.sleep(float(os.environ.get("FAKE_SLEEP", "0")))
code = int(os.environ.get("FAKE_RC", "0"))
print(json.dumps({{"ok": True, "data": {{"message_id": "om_1"}}}} if code == 0 else
                 {{"ok": False, "error": {{"type": "network", "code": 99}}}}))
sys.exit(code)
"""


class NoticeCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        bin_dir = exec_tempdir()
        self.addCleanup(bin_dir.cleanup)
        self.bin = Path(bin_dir.name)
        self.calls = self.root / "calls.jsonl"
        fake = self.bin / "lark-cli"
        fake.write_text(FAKE_CLI.format(python=sys.executable, calls=str(self.calls)))
        fake.chmod(0o755)
        self.state = self.root / "state"
        self.state.mkdir()
        (self.state / "bot-identity.json").write_text(json.dumps({"app_id": "cli_test", "owner": OWNER, "profile": "bot"}))
        self.manager_root = self.root / "services"
        self.now = [100_000.0]
        self.lines = []

    def environment(self, **more):
        return {"PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}", **more}

    def sent(self):
        """Each lark-cli call: (its argv, the --text it carried)."""
        if not self.calls.exists():
            return []
        rows = [json.loads(line) for line in self.calls.read_text().splitlines()]
        return [(row, row[row.index("--text") + 1]) for row in rows]

    def notices(self, **env):
        notice = DownNotice(self.manager_root, self.lines.append,
                            lambda manifest: {**os.environ, **manifest["environment"], **env}, clock=lambda: self.now[0])
        return notice

    def manifest(self, **environment):
        return {"id": service_key(self.state), "state_dir": str(self.state), "profile": "bot",
                "desired_state": "running", "environment": self.environment(**environment)}

    def check(self, notice, manifest, status, failed=None, alive=None):
        """One pass, then the message it started (if any) waited for and settled by the next one."""
        alive = status.get("status") == "running" if alive is None else alive
        notice.check(manifest, status, failed, alive=alive)
        notice.wait(30)
        notice.check(manifest, status, failed, alive=alive)


def ready(identity=None, connection="receiving", receiving=True):
    """A Bridge's health snapshot (app/health.py), its message consumer ``receiving`` or not."""
    return {"process": identity, "status": "ready" if receiving and connection == "receiving" else "degraded",
            "consumers": {"im.message.receive_v1": {"ready": receiving}, "card.action.trigger": {"ready": True}},
            "remote_connection": {"status": connection}}


class DownNoticeTests(NoticeCase):
    def test_three_failed_starts_tell_the_owner_once_and_its_return_once(self):
        notice, manifest = self.notices(), self.manifest()
        for failures in (1, 2):
            self.check(notice, manifest, {"status": "backoff", "failures": failures, "returncode": 1})
        self.assertEqual(self.sent(), [])  # Two failed starts are no news yet.
        self.check(notice, manifest, {"status": "backoff", "failures": 3, "returncode": 1})
        [(argv, text)] = self.sent()
        self.assertEqual(argv[:4], ["--profile", "bot", "im", "+messages-send"])
        self.assertEqual(argv[argv.index("--user-id") + 1], OWNER)
        self.assertEqual(argv[argv.index("--as") + 1], "bot")
        key = argv[argv.index("--idempotency-key") + 1]
        self.assertTrue(key.startswith("covey-down-") and len(key) <= 50)
        self.assertIn("连续 3 次", text)
        self.assertIn("退出码 1", text)
        self.assertIn(f"service {manifest['id'][:8]}: told the owner it is down (crash)", self.lines)
        # It goes on failing, comes up, fails again: nothing more while it is down.
        for failures in (4, 5):
            self.now[0] += 60
            self.check(notice, manifest, {"status": "backoff", "failures": failures, "returncode": 1})
            self.check(notice, manifest, {"status": "running", "failures": failures, "health": ready()})
        self.assertEqual(len(self.sent()), 1)
        # Up and ready: told once it has stayed so for BACK_AFTER.
        self.check(notice, manifest, {"status": "running", "failures": 5, "health": ready()})
        self.now[0] += down_notice.BACK_AFTER - 1
        self.check(notice, manifest, {"status": "running", "failures": 5, "health": ready()})
        self.assertEqual(len(self.sent()), 1)
        self.now[0] += 2
        self.check(notice, manifest, {"status": "running", "failures": 0, "health": ready()})
        self.assertEqual(len(self.sent()), 2)
        self.assertIn("恢复了", self.sent()[1][1])
        self.assertIn(f"service {manifest['id'][:8]}: told the owner it is back (crash)", self.lines)
        self.now[0] += 3600
        self.check(notice, manifest, {"status": "running", "failures": 0, "health": ready()})
        self.assertEqual(len(self.sent()), 2)

    def test_messages_cannot_reach_it_for_long(self):
        notice, manifest = self.notices(), self.manifest()
        offline = {"status": "running", "failures": 0, "health": ready(connection="disconnected")}
        self.check(notice, manifest, offline)
        self.now[0] += down_notice.OFFLINE_AFTER - 1
        self.check(notice, manifest, offline)
        self.assertEqual(self.sent(), [])
        self.now[0] += 1
        self.check(notice, manifest, offline)
        self.assertIn("收不到飞书消息", self.sent()[0][1])
        self.assertIn("10 分钟", self.sent()[0][1])
        self.check(notice, manifest, {"status": "running", "health": ready()})
        self.now[0] += down_notice.BACK_AFTER
        self.check(notice, manifest, {"status": "running", "health": ready()})
        self.assertIn("又收得到消息了", self.sent()[1][1])
        # A message consumer that is not ready is the same; within the cooldown the next one waits for it to pass.
        stalled = {"status": "running", "health": ready(receiving=False)}
        self.check(notice, manifest, stalled)
        self.now[0] += down_notice.OFFLINE_AFTER
        self.check(notice, manifest, stalled)
        self.assertEqual(len(self.sent()), 2)
        self.now[0] += down_notice.COOLDOWN
        self.check(notice, manifest, stalled)
        self.assertEqual(len(self.sent()), 3)
        self.assertIn("收不到飞书消息", self.sent()[2][1])

    def test_a_pass_error_tells_only_when_the_bridge_is_not_running(self):
        notice, manifest = self.notices(), self.manifest()
        # 2026-11-18: a full disk failed the status write while the Bridge ran on: it is not down.
        full = "[Errno 122] Disk quota exceeded"
        for _ in range(3):
            self.check(notice, manifest, {"status": "running", "health": ready()}, failed=full, alive=True)
            self.now[0] += down_notice.ERROR_AFTER
        self.assertEqual(self.sent(), [])
        missing = "[Errno 2] No such file or directory: '/absolute/release/bin/python3'"
        self.check(notice, manifest, {"status": "backoff", "failures": 1}, failed=missing, alive=False)
        self.now[0] += down_notice.ERROR_AFTER
        self.check(notice, manifest, {"status": "backoff", "failures": 1}, failed=missing, alive=False)
        [(_, text)] = self.sent()
        self.assertIn("2 分钟启动不了机器人", text)
        self.assertIn("No such file or directory", text)

    def test_switched_off_or_stopped_it_says_nothing(self):
        manager = Manager(self.manager_root, base_delay=0, max_delay=0)
        manager.notices.clock = lambda: self.now[0]
        manifest = self.manifest(LARK_BRIDGE_DOWN_NOTICE="0")
        with mock.patch.object(DownNotice, "check", side_effect=AssertionError("called")) as check:
            manager.tell(manifest, None)
        check.assert_not_called()
        self.assertFalse((self.manager_root / down_notice.FOLDER).exists())
        # Stopped on purpose while the owner was told: no word of it coming back.
        notice, manifest = self.notices(), self.manifest()
        self.check(notice, manifest, {"status": "backoff", "failures": 3})
        self.assertEqual(len(self.sent()), 1)
        self.check(notice, {**manifest, "desired_state": "stopped"}, {"status": "stopped"})
        self.check(notice, manifest, {"status": "running", "health": ready()})
        self.now[0] += down_notice.BACK_AFTER
        self.check(notice, manifest, {"status": "running", "health": ready()})
        self.assertEqual(len(self.sent()), 1)

    def test_no_owner_yet_no_message(self):
        (self.state / "bot-identity.json").unlink()
        notice, manifest = self.notices(), self.manifest()
        for _ in range(2):
            self.check(notice, manifest, {"status": "backoff", "failures": 3})
        self.assertEqual(self.sent(), [])
        self.assertEqual([line for line in self.lines if "owner" in line],
                         [f"service {manifest['id'][:8]}: the owner is not told it is down: "
                          "no owner in bot-identity.json yet"])

    def test_a_send_that_fails_is_tried_again_with_its_key_and_never_holds_up_a_pass(self):
        notice, manifest = self.notices(FAKE_RC="1"), self.manifest()
        self.check(notice, manifest, {"status": "backoff", "failures": 3})
        self.assertTrue(any("did not go out (try 1: type=network code=99)" in line for line in self.lines))
        self.now[0] += down_notice.RETRY_AFTER - 1
        self.check(notice, manifest, {"status": "backoff", "failures": 3})
        self.assertEqual(len(self.sent()), 1)
        notice.environment = lambda manifest: {**os.environ, **manifest["environment"], "FAKE_SLEEP": "3"}
        self.now[0] += 1
        started = time.monotonic()
        notice.check(manifest, {"status": "backoff", "failures": 3}, alive=False)
        self.assertLess(time.monotonic() - started, 2)  # lark-cli runs in its own thread.
        notice.wait(30)
        notice.check(manifest, {"status": "backoff", "failures": 3}, alive=False)
        keys = [argv[argv.index("--idempotency-key") + 1] for argv, _ in self.sent()]
        self.assertEqual(len(keys), 2)
        self.assertEqual(keys[0], keys[1])
        self.assertIn("told the owner it is down (crash)", self.lines[-1])
        # Past the window a message that never went out is given up, and the cooldown holds the next one.
        other, notice = {**self.manifest(), "id": "f" * 64}, self.notices(FAKE_RC="1")
        self.check(notice, other, {"status": "backoff", "failures": 3})
        self.now[0] += down_notice.RETRY_WINDOW + 1
        self.check(notice, other, {"status": "backoff", "failures": 3})
        self.assertTrue(any("the down message to the owner was not sent after 1 tries; given up" in line
                            for line in self.lines))
        self.assertEqual(notice.load(other["id"]).get("last_down"), self.now[0])

    def test_a_manager_that_restarts_finishes_what_its_predecessor_began(self):
        notice, manifest = self.notices(), self.manifest()
        self.check(notice, manifest, {"status": "backoff", "failures": 3})
        successor = self.notices()
        self.check(successor, manifest, {"status": "backoff", "failures": 6})
        self.assertEqual(len(self.sent()), 1)  # Told already: not again.
        self.check(successor, manifest, {"status": "running", "health": ready()})
        self.now[0] += down_notice.BACK_AFTER
        self.check(successor, manifest, {"status": "running", "health": ready()})
        self.assertEqual(len(self.sent()), 2)
        self.assertIn("恢复了", self.sent()[1][1])

    def test_a_record_that_cannot_be_written_holds_no_message_back(self):
        notice, manifest = self.notices(), self.manifest()
        with mock.patch("covey.service.down_notice.write_durable_json", side_effect=OSError(122, "Disk quota exceeded")):
            self.check(notice, manifest, {"status": "backoff", "failures": 3})
        self.assertEqual(len(self.sent()), 1)
        self.assertTrue(any("notice record not written" in line for line in self.lines))


class ManagerNoticeTests(NoticeCase):
    """Through the Manager's own passes: real processes, the fake lark-cli on the service's PATH."""

    def setUp(self):
        super().setUp()
        self.manager = Manager(self.manager_root, base_delay=0, max_delay=0)
        self.manager.notices.clock = lambda: self.now[0]
        self.addCleanup(self.stop_children)

    def stop_children(self):
        for child in self.manager.children.values():
            identity = process_identity(child.pid)
            if identity:
                terminate_owned(identity, timeout=1)
            child.wait(timeout=5)

    def passes(self, count, exits=True):
        """``count`` passes of the Manager, each after its children exited (``exits``) and with the message it started
        waited for; what the Manager said."""
        said = io.StringIO()
        with contextlib.redirect_stdout(said):
            for _ in range(count):
                for child in list(self.manager.children.values()) if exits else ():
                    child.wait(timeout=10)
                self.manager.reconcile_all()
                self.manager.notices.wait(30)
        return said.getvalue()

    def test_a_bridge_that_never_comes_up_is_told_once(self):
        manifest = register(self.manager_root, self.state, [sys.executable, "-B", "-c", "import sys; sys.exit(3)"],
                            workdir=self.root, profile="bot", environment=self.environment())
        said = self.passes(12)
        status = read_json(self.manager_root / "status" / (manifest["id"] + ".json"), {})
        self.assertGreaterEqual(status["failures"], 4)
        [(argv, text)] = self.sent()
        self.assertEqual(argv[argv.index("--user-id") + 1], OWNER)
        self.assertIn("连续 3 次", text)
        self.assertIn(f"service {manifest['id'][:8]}: told the owner it is down (crash)", said)
        saved = read_json(self.manager_root / down_notice.FOLDER / (manifest["id"] + ".json"))
        self.assertEqual(saved["told"]["kind"], "crash")

    def test_switched_off_the_manager_passes_as_before(self):
        manifest = register(self.manager_root, self.state, [sys.executable, "-B", "-c", "import sys; sys.exit(3)"],
                            workdir=self.root, profile="bot", environment=self.environment(LARK_BRIDGE_DOWN_NOTICE="0"))
        said = self.passes(10)
        self.assertEqual(self.sent(), [])
        self.assertFalse((self.manager_root / down_notice.FOLDER).exists())
        self.assertNotIn("owner", said)
        self.assertGreaterEqual(read_json(self.manager_root / "status" / (manifest["id"] + ".json"))["failures"], 3)

    def test_a_notice_that_fails_never_stops_the_pass(self):
        manifest = register(self.manager_root, self.state, [sys.executable, "-B", "-c", "import time; time.sleep(60)"],
                            workdir=self.root, profile="bot", environment=self.environment())
        with mock.patch.object(DownNotice, "check", side_effect=RuntimeError("boom")):
            said = self.passes(2, exits=False)
        status = read_json(self.manager_root / "status" / (manifest["id"] + ".json"))
        self.assertEqual(status["status"], "running")
        self.assertEqual(said.count("notice check failed: RuntimeError: boom"), 1)


if __name__ == "__main__":
    unittest.main()
