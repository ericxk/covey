"""Local runtime locks protect cloud records without locking their filesystem."""
# privacy: allow-file 测托管平台安装模式 merlin-pid1 的锁目录声明
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid

from support import requires_extension, REPO
from covey.core import locks, logs, session_state


# How long a fresh interpreter may take to import from the checkout and finish. Only a wait until: on NFS an
# open of a source file can stick in the kernel in steps of about 10 s, which no CPU margin predicts (6 loops of
# 4 children at load ~440, 2026-11-05: 20 of 1,022 rounds over 20 s, the longest 32 s; 47 s in a build). Four
# tests wait this long, so a hang shared by every child ends at test_map's MODULE_TIMEOUT, as one module error.
CHILD_DEADLINE = 240


CHILD = """
import fcntl, sys
from pathlib import Path
from covey.core.file_lock import file_lock
try:
    with file_lock(Path(sys.argv[1]), operation=fcntl.LOCK_EX | fcntl.LOCK_NB):
        print("held", flush=True)
        input()
except BlockingIOError:
    print("busy", flush=True)
"""


class RuntimeLockTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.cloud = self.home / "cloud"
        self.cloud.mkdir()
        self.runtime = self.home / "runtime"
        patch = mock.patch.dict(os.environ, {"COVEY_LOCK_ROOT": str(self.runtime)})
        patch.start()
        self.addCleanup(patch.stop)

    def child(self, path, code=CHILD):
        child = subprocess.Popen([sys.executable, "-B", "-c", code, str(path)], cwd=REPO,
                                 env=os.environ.copy(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        def stop():
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)
        self.addCleanup(stop)
        return child

    def line(self, child):
        ready, _, _ = select.select([child.stdout], [], [], CHILD_DEADLINE)
        self.assertTrue(ready, "child did not report lock acquisition")
        line = child.stdout.readline().strip()
        if not line:
            self.fail(child.communicate(timeout=10)[1])
        return line

    def mapped(self, path):
        return self.runtime / (hashlib.sha256(os.fsencode(path.resolve())).hexdigest() + ".lock")

    def test_shared_lock_uses_runtime_file_and_never_unlinks_it(self):
        record = self.cloud / "record.json"
        lock = locks.shared_file_lock(record)
        target = self.mapped(lock.path)
        with lock:
            self.assertTrue(target.is_file())
            self.assertFalse(lock.path.exists())
            self.assertFalse(locks.remove_unused_lock(record))
        inode = target.stat().st_ino
        locks.remove_unused_lock(record)
        self.assertEqual(target.stat().st_ino, inode)
        self.assertIsNone(lock.fd)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.runtime.stat().st_mode & 0o777, 0o700)

    def test_canonical_aliases_share_reentrant_lock(self):
        alias = self.home / "alias"
        alias.symlink_to(self.cloud, target_is_directory=True)
        first = locks.shared_file_lock(self.cloud / "r")
        second = locks.shared_file_lock(alias / "r")
        self.assertIs(first, second)
        with first, second:
            self.assertEqual(first.depth, 2)

    def test_crossprocess_contention_release_and_sigkill(self):
        from covey.core.file_lock import file_lock
        path = self.cloud / "shared.lock"
        with file_lock(path):
            contender = self.child(path)
            self.assertEqual(self.line(contender), "busy")
            self.assertEqual(contender.wait(timeout=10), 0)
        holder = self.child(path)
        self.assertEqual(self.line(holder), "held")
        with self.assertRaises(BlockingIOError):
            with file_lock(path, operation=fcntl.LOCK_EX | fcntl.LOCK_NB):
                self.fail("second holder")
        holder.send_signal(signal.SIGKILL)
        self.assertEqual(holder.wait(timeout=10), -signal.SIGKILL)
        with file_lock(path, operation=fcntl.LOCK_EX | fcntl.LOCK_NB):
            pass
        holder = self.child(path)
        self.assertEqual(self.line(holder), "held")
        holder.communicate("\n", timeout=10)
        self.assertEqual(holder.returncode, 0)
        with file_lock(path, operation=fcntl.LOCK_EX | fcntl.LOCK_NB):
            pass

    def test_multiple_processes_serialize_durable_updates(self):
        record = self.cloud / "count"
        record.write_text("0")
        code = """
import sys
from pathlib import Path
from covey.core.locks import shared_file_lock
record = Path(sys.argv[1])
for _ in range(20):
    with shared_file_lock(record):
        record.write_text(str(int(record.read_text()) + 1))
"""
        children = [self.child(record, code) for _ in range(4)]
        for child in children:
            _, error = child.communicate(timeout=CHILD_DEADLINE)
            self.assertEqual(child.returncode, 0, error)
        self.assertEqual(record.read_text(), "80")
        self.assertEqual(list(self.cloud.iterdir()), [record])
        self.assertEqual(len(list(self.runtime.iterdir())), 1)

    @requires_extension()
    def test_all_lock_sites_avoid_flock_on_cloud_files(self):
        from covey_internal.maso.sessions import MasoSessionLease
        from covey.core.saved_results import _locked_results
        from covey.core.session_guard import _locked_guard
        from covey.feishu.web_service import locked
        from covey.service.manager import config_lock
        flock = fcntl.flock
        calls = []
        def local_only(fd, operation):
            number = fd if isinstance(fd, int) else fd.fileno()
            opened = os.fstat(number)
            identities = {(p.stat().st_dev, p.stat().st_ino) for p in self.runtime.glob("*.lock")}
            self.assertIn((opened.st_dev, opened.st_ino), identities, "flock reached cloud")
            calls.append(operation)
            return flock(fd, operation)
        with mock.patch.object(fcntl, "flock", local_only):
            with locks.shared_file_lock(self.cloud / "job"):
                pass
            with session_state._locked_session_state(self.cloud / "session"):
                pass
            with _locked_guard(self.cloud / "guards" / "id"):
                pass
            ctx = SimpleNamespace(state_dir=self.cloud)
            with _locked_results(ctx, create=True):
                pass
            with _locked_results(ctx):
                pass
            with config_lock(self.cloud / "manager"):
                pass
            with locked(self.cloud, wait=0):
                pass
            with MasoSessionLease(self.cloud, "topic", self.cloud, None):
                pass
            log = self.cloud / "bridge.log"
            log.write_text("durable log\n")
            self.assertTrue(logs.rotate_live(log, max_bytes=1))
            self.assertEqual(log.with_name("bridge.log.1").read_text(), "durable log\n")
        self.assertGreaterEqual(len(calls), 10)
        self.assertFalse(list(self.cloud.rglob("*.lock")))

    @requires_extension()
    def test_optional_diagnostics_disabled_even_for_inherited_payload(self):
        from covey_internal.maso.diagnostics import Diagnostic
        with mock.patch.dict(os.environ):
            del os.environ["COVEY_LOCK_ROOT"]
            original = Diagnostic.create(self.cloud, str(uuid.uuid4()), str(uuid.uuid4()))
        self.addCleanup(original.close)
        self.assertIsNotNone(original.fd)
        payload = original.payload({"run_id": original.run_id, "session_id": original.session_id})
        with mock.patch("fcntl.flock", side_effect=AssertionError("cloud diagnostic lock")):
            created = Diagnostic.create(self.cloud, str(uuid.uuid4()), str(uuid.uuid4()))
            attached = Diagnostic.attach(payload, "worker")
        self.addCleanup(created.close)
        self.addCleanup(attached.close)
        self.assertIsNone(created.fd)
        self.assertIsNone(attached.fd)
        self.assertEqual(created.popen_options(), {})
        self.assertEqual(created.payload({"test": True}), {"test": True})
        os.fstat(original.fd)  # Disabling an attachment must not close a caller's fd.

    def test_default_uses_original_flock_and_propagates_enosys(self):
        path = self.cloud / "original.lock"
        probe = """
import fcntl, sys
with open(sys.argv[1], "r+") as handle:
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        print("held", flush=True)
    except BlockingIOError:
        print("busy", flush=True)
"""
        with mock.patch.dict(os.environ):
            del os.environ["COVEY_LOCK_ROOT"]
            with locks.SharedFileLock(path) as lock:
                self.assertEqual(os.fstat(lock.fd).st_ino, path.stat().st_ino)
                child = self.child(path, probe)
                self.assertEqual(self.line(child), "busy")
                child.wait(timeout=10)
            with mock.patch("fcntl.flock", side_effect=OSError(errno.ENOSYS, "unsupported")):
                with self.assertRaises(OSError) as raised:
                    with locks.SharedFileLock(path):
                        pass
            self.assertEqual(raised.exception.errno, errno.ENOSYS)
        self.assertFalse(self.runtime.exists())

    def test_saved_results_lock_recreated_after_runtime_directory_loss(self):
        from covey.core.saved_results import _locked_results
        ctx = SimpleNamespace(state_dir=self.cloud)
        with self.assertRaises(FileNotFoundError):
            with _locked_results(ctx):
                pass
        self.assertFalse((self.cloud / "verified-results").exists())
        with _locked_results(ctx, create=True):
            pass
        # Simulate a reboot only after every holder has exited.
        for path in self.runtime.iterdir():
            path.unlink()
        self.runtime.rmdir()
        with _locked_results(ctx):
            self.assertTrue(self.runtime.is_dir())

    def test_rejects_symlink_or_nonprivate_root_and_files(self):
        from covey.core.file_lock import file_lock
        path = self.cloud / "unsafe.lock"
        self.runtime.mkdir(mode=0o700)
        target = self.mapped(path)
        other = self.home / "other"
        other.write_text("keep")
        for kind in ("symlink", "hardlink", "directory", "fifo", "mode"):
            with self.subTest(kind=kind):
                if kind == "symlink":
                    target.symlink_to(other)
                elif kind == "hardlink":
                    os.link(other, target)
                elif kind == "directory":
                    target.mkdir()
                elif kind == "fifo":
                    os.mkfifo(target, 0o600)
                else:
                    target.touch()
                    target.chmod(0o644)  # Not touch(mode=): a 077 umask would leave it private.
                try:
                    with self.assertRaises((ValueError, OSError)):
                        with file_lock(path):
                            pass
                finally:
                    target.rmdir() if kind == "directory" else target.unlink()
        self.assertEqual(other.read_text(), "keep")
        self.runtime.chmod(0o755)
        with self.assertRaises((ValueError, OSError)):
            with file_lock(path):
                pass
        self.runtime.rmdir()
        self.runtime.symlink_to(self.cloud, target_is_directory=True)
        with self.assertRaises((ValueError, OSError)):
            with file_lock(path):
                pass

    def test_rejects_empty_relative_and_symlink_ancestor_roots(self):
        from covey.core.file_lock import file_lock
        alias = self.home / "alias"
        alias.symlink_to(self.cloud, target_is_directory=True)
        for value in ("", "relative", str(alias / "runtime")):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"COVEY_LOCK_ROOT": value}):
                with self.assertRaises((ValueError, OSError)):
                    with file_lock(self.cloud / "lock"):
                        pass

    def test_rejects_foreign_owned_runtime_root_and_file(self):
        from covey.core.file_lock import file_lock
        path = self.cloud / "owner.lock"
        with file_lock(path):
            pass
        for target in (self.runtime, self.mapped(path)):
            identity = target.stat().st_ino
            fstat = os.fstat
            def foreign_owner(fd):
                info = fstat(fd)
                if info.st_ino == identity:
                    fields = list(info)
                    fields[4] = os.geteuid() + 1
                    return os.stat_result(fields)
                return info
            with self.subTest(target=target), mock.patch("os.fstat", side_effect=foreign_owner):
                with self.assertRaises(ValueError):
                    with file_lock(path):
                        pass

    def test_runtime_descriptors_close_on_success_and_failure(self):
        from covey.core.file_lock import file_lock
        for _ in range(30):
            with file_lock(self.cloud / "fd") as fd:
                self.assertFalse(os.get_inheritable(fd))
            with self.assertRaises(OSError):
                os.fstat(fd)
        opened = []
        real_open = os.open
        def track(*args, **kwargs):
            fd = real_open(*args, **kwargs)
            opened.append(fd)
            return fd
        with mock.patch("os.open", side_effect=track), \
                mock.patch("fcntl.flock", side_effect=OSError(errno.ENOSYS, "unsupported")):
            with self.assertRaises(OSError):
                with file_lock(self.cloud / "failure"):
                    pass
        for fd in opened:
            with self.assertRaises(OSError):
                os.fstat(fd)

    @requires_extension()
    def test_installer_requires_exact_opted_in_persistent_filesystem_and_runtime_locks(self):
        from covey_internal import hosting as install
        entry = "/opt/tiger/arnold/arnold_entrypoint/exec/my_init.py"  # privacy: allow 扩展的 hosting 按平台 PID 1 入口路径认环境
        for fstype, opted_in, runtime, accepted in (
                ("virtio_pfs_010309", False, True, False),
                ("virtio_pfs_010309", True, False, False),
                ("virtio_pfs_010309", True, True, True),
                ("overlay", True, True, False),
                ("tmpfs", True, True, False),
                ("virtio_pfs_other", True, True, False),
                ("nfs4", False, False, True)):
            with self.subTest(fstype=fstype, opted_in=opted_in, runtime=runtime), \
                    mock.patch.dict(os.environ), \
                    mock.patch.object(install, "pid1_context", return_value=(["python", entry], "test-devbox")), \
                    mock.patch.object(install.subprocess, "run", return_value=subprocess.CompletedProcess(
                        [], 0, stdout="/data " + fstype + "\n")), \
                    mock.patch.object(install.subprocess, "Popen") as watchdog, \
                    mock.patch.object(install.site, "ENABLE_USER_SITE", True), \
                    mock.patch.object(install.sys, "executable", str(Path("/proc/1/exe").resolve())):
                if not runtime:
                    del os.environ["COVEY_LOCK_ROOT"]
                kwargs = dict(site_dir=self.home / "site", config_dir=self.home / "config", activate=runtime)
                if opted_in:
                    kwargs["persistent_virtio_pfs"] = True
                if not accepted:
                    with self.assertRaises(ValueError):
                        install.install_pid1(self.cloud / "service", **kwargs)
                    watchdog.assert_not_called()
                    continue
                receipt = install.install_pid1(self.cloud / "service", **kwargs)
                config = json.loads(Path(receipt["config"]).read_text())
                if runtime:
                    self.assertEqual(config["command"][:2],
                                     ["/usr/bin/env", "COVEY_LOCK_ROOT=" + str(self.runtime)])
                    self.assertTrue(receipt["temporary_watchdog_started"])
                    watchdog.assert_called_once_with(
                        [str(Path("/proc/1/exe").resolve()), "-B",
                         str(self.home / "site" / "lark_bridge_boot_hook.py"), receipt["config"]],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, start_new_session=True)
                else:
                    self.assertEqual(config["command"][0], str(Path("/proc/1/exe").resolve()))
                    watchdog.assert_not_called()

    @requires_extension()
    def test_installer_cli_opt_in_and_manifest_environment(self):
        from covey_internal import hosting
        from covey.service import install
        from covey.service.manager import runtime_environment
        with mock.patch.object(sys, "argv", [
                "covey_service_install.py", "--mode", "merlin-pid1", "--root", str(self.cloud),
                "--persistent-virtio-pfs", "--activate"]), \
                mock.patch.object(hosting, "install_pid1", return_value={}) as install_hook, \
                mock.patch("builtins.print"):
            install.main()
        install_hook.assert_called_once_with(self.cloud, activate=True, persistent_virtio_pfs=True)
        self.assertEqual(runtime_environment({"COVEY_LOCK_ROOT": str(self.runtime)}),  # Saved under the old name.
                         {"LARK_BRIDGE_LOCK_ROOT": str(self.runtime)})
