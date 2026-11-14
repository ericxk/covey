"""./install 的浏览器准备 (service/browser_setup.py, core/browser_env.py) 和没有 systemd 时的 crontab 自启
(service/install.py 的 cron 模式、service/doctor.py 的 boot 回执)：下载换源与卡住检测、缺库清单、deb 包名选择、
launch.json 的读写、回调/事件的一键链接、crontab 一行的幂等增删。全部用假进程和假命令，不联网。"""
from __future__ import annotations

import base64
import gzip
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import exec_tempdir

from covey.core import app_manifest, browser_env
from covey.service import browser_setup as bs
from covey.service import doctor, install


def done(out="", code=0, err=""):
    return subprocess.CompletedProcess([], code, out, err)


class DownloadTest(unittest.TestCase):
    def run_plan(self, results, env=None):
        calls, said = [], []

        def runner(python, e, say, live, **kw):
            calls.append({k: e.get(k) for k in ("PLAYWRIGHT_DOWNLOAD_HOST", "HTTPS_PROXY")})
            return results[len(calls) - 1]

        with tempfile.TemporaryDirectory() as tmp:
            try:
                bs.download_chromium("py", env or {"PATH": "/bin"}, said.append, tmp, tmp, runner=runner)
                error = None
            except bs.Unavailable as exc:
                error = str(exc)
        return calls, said, error

    def test_official_first_then_the_mirror_both_direct(self):
        calls, said, error = self.run_plan([(False, "60 秒没有进展"), (True, "")])
        self.assertIsNone(error)
        self.assertEqual([None, bs.MIRROR], [c["PLAYWRIGHT_DOWNLOAD_HOST"] for c in calls])
        self.assertTrue(any("官方源没成功" in x for x in said))

    def test_a_proxy_is_dropped_for_the_direct_tries_and_used_last(self):
        env = {"PATH": "/bin", "HTTPS_PROXY": "http://proxy.invalid:1"}
        calls, _, error = self.run_plan([(False, "x")] * 4, env)
        self.assertEqual([None, None, "http://proxy.invalid:1", "http://proxy.invalid:1"], [c["HTTPS_PROXY"] for c in calls])
        self.assertIn("Chromium 下载失败", error)

    def test_progress_lines_come_every_ten_percent_and_a_stall_kills_it(self):
        said = []
        script = ("import sys,time\nprint('Downloading Chromium 1 (playwright build v1) from x', flush=True)\n"
                  "for p in (0, 4, 10, 14, 20, 100):\n    print('|##| %d%% of 10.0 MiB' % p, flush=True)\n"
                  "time.sleep(30)\n")
        real = subprocess.Popen

        def fake(argv, **kw):
            return real([sys.executable, "-c", script], **kw)

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(subprocess, "Popen", side_effect=fake):
            import time
            ok, why = bs.run_install("py", {"PATH": "/usr/bin:/bin"}, said.append, None, stall=3, deadline=time.monotonic() + 60,
                                     tmp_dirs=[tmp])
        self.assertFalse(ok)
        self.assertIn("3 秒没有进展", why)
        self.assertEqual(["    下载 Chromium", "    0% of 10.0 MiB", "    10% of 10.0 MiB", "    20% of 10.0 MiB", "    100% of 10.0 MiB"], said)

    def test_npm_proxy_spellings_are_dropped_too(self):
        env = {"PATH": "/bin", "npm_config_https_proxy": "http://p.invalid:1", "NPM_CONFIG_PROXY": "http://p.invalid:1"}
        self.assertEqual({"PATH": "/bin"}, bs.direct(env))
        self.assertTrue(bs.has_proxy(env))

    def test_each_source_is_told_its_name_and_only_the_last_may_not_be_switched(self):
        seen = []

        def runner(python, e, say, live, **kw):
            seen.append((kw["source"], kw["slow_eta"]))
            return False, "x"

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(bs.Unavailable):
                bs.download_chromium("py", {"PATH": "/bin", "HTTPS_PROXY": "http://p.invalid:1"}, lambda x: None, tmp, tmp, runner=runner)
        self.assertEqual([("官方源", bs.SLOW_ETA_SECONDS), ("镜像 npmmirror", bs.SLOW_ETA_SECONDS),
                          ("官方源（走代理）", bs.SLOW_ETA_SECONDS), ("镜像（走代理）", None)], seen)


class SlowDownloadTest(unittest.TestCase):
    def test_speed_and_estimate_from_percent_and_size(self):
        rate, eta = bs.speed_eta(10, 100 * 10 ** 6, 50)
        self.assertEqual(200000, rate)
        self.assertEqual(450, eta)
        self.assertIsNone(bs.speed_eta(0, 100, 30))
        self.assertEqual(0, bs.size_bytes("??"))
        self.assertIn("0.2 MB/s", bs.speed_text(200000, 450))
        self.assertIn("7.5 分钟", bs.speed_text(200000, 450))

    def run_script(self, script, slow_eta, clock):
        said = []
        real = subprocess.Popen

        def fake(argv, **kw):
            return real([sys.executable, "-c", script], **kw)

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(subprocess, "Popen", side_effect=fake), \
                mock.patch.object(bs, "SLOW_CHECK_SECONDS", 0), mock.patch.object(bs.time, "monotonic", side_effect=clock):
            ok, why = bs.run_install("py", {"PATH": "/usr/bin:/bin"}, said.append, None, stall=1000, deadline=1e12,
                                     tmp_dirs=[tmp], source="官方源", slow_eta=slow_eta)
        return ok, why, said

    @staticmethod
    def ticking(step):
        state = {"t": 0.0}

        def clock():
            state["t"] += step
            return state["t"]
        return clock

    SCRIPT = ("print('Downloading Chromium 1 (playwright build v1) from x', flush=True)\n"
              "print('|##| 10% of 100.0 MB', flush=True)\nimport time\ntime.sleep(30)\n")

    def test_a_slow_file_is_given_up_when_another_source_exists(self):
        ok, why, said = self.run_script(self.SCRIPT, 180, self.ticking(10))
        self.assertFalse(ok)
        self.assertIn("太慢", why)
        self.assertIn("预计还要", why)

    def test_the_last_source_keeps_going_and_shows_the_source_and_speed(self):
        script = self.SCRIPT.replace("time.sleep(30)", "print('|##| 100% of 100.0 MB', flush=True)")
        ok, why, said = self.run_script(script, None, self.ticking(10))
        self.assertTrue(ok)
        self.assertTrue(any(x.startswith("    [官方源] 10% of 100.0 MB  ") and "MB/s" in x for x in said), said)


class SystemBrowserTest(unittest.TestCase):
    def test_a_named_executable_on_path_is_used(self):
        with exec_tempdir() as tmp:
            exe = Path(tmp) / "google-chrome"
            exe.write_text("#!/bin/sh\n")
            exe.chmod(0o755)
            self.assertEqual(str(exe), bs.system_browser({"PATH": tmp}))
            self.assertEqual("", bs.system_browser({"PATH": "/nonexistent"}))

    def test_the_variable_wins(self):
        self.assertEqual("/bin/sh", bs.system_browser({browser_env.EXECUTABLE: "/bin/sh", "PATH": ""}))


class LibsTest(unittest.TestCase):
    def test_ldd_not_found_lines_are_collected_once(self):
        out = "\tlibc.so.6 => /lib/libc.so.6 (0x1)\n\tlibasound.so.2 => not found\n\tlibcups.so.2 => not found\n"
        with mock.patch.object(bs.shutil, "which", return_value="/usr/bin/ldd"), \
                mock.patch.object(bs.subprocess, "run", return_value=done(out)) as run:
            self.assertEqual(["libasound.so.2", "libcups.so.2"], bs.missing_libs(["a", "b"], ["/x"], {"PATH": "/bin"}))
        self.assertEqual("/x:", run.call_args.kwargs["env"]["LD_LIBRARY_PATH"])

    def test_package_names_are_the_missing_ones_only_libraries_and_their_data(self):
        def run(argv, **kw):
            if argv[:2] == ["apt-cache", "show"]:
                return done("Package: x\n" if argv[-1] != "libasound2" else "")
            if argv[:2] == ["apt-cache", "depends"]:
                return done("libcups2\n  Depends: libavahi-client3\nlibavahi-client3\nfonts-liberation\nlibelogind0\nlibasound2-data\n<virtual>\n")
            return done("libcups2 ii \nlibx11-6 ii \n")  # dpkg-query: these are installed
        names = bs.deb_names(run)
        self.assertIn("libasound2t64", names)  # the t64 name when the plain one is unknown
        self.assertNotIn("libcups2", names)
        self.assertIn("libavahi-client3", names)
        self.assertIn("libasound2-data", names)
        self.assertNotIn("fonts-liberation", names)
        self.assertNotIn("libelogind0", names)

    def test_other_distributions_are_told_to_use_their_package_manager(self):
        with mock.patch.object(bs, "browser_binaries", return_value=["b"]), \
                mock.patch.object(bs, "missing_libs", return_value=["libx.so"]), \
                mock.patch.object(bs, "can_root", return_value=None), mock.patch.object(bs, "debian_like", return_value=False):
            with self.assertRaises(bs.Unavailable) as why:
                bs.ensure_libs("py", {}, lambda s: None, "/nonexistent")
        self.assertIn("不是 Debian/Ubuntu", str(why.exception))

    def test_with_root_the_playwright_dependency_installer_is_used(self):
        ran = []
        lacking = [["libx.so"], []]
        with mock.patch.object(bs, "browser_binaries", return_value=["b"]), \
                mock.patch.object(bs, "missing_libs", side_effect=lambda *a, **k: lacking.pop(0)), \
                mock.patch.object(bs, "can_root", return_value=["sudo", "-n"]), \
                mock.patch.object(bs.subprocess, "run", side_effect=lambda argv, **k: ran.append(argv) or done()):
            self.assertEqual([], bs.ensure_libs("py", {}, lambda s: None, "/f"))
        self.assertEqual(["sudo", "-n", "py", "-m", "playwright", "install-deps", "chromium"], ran[0])


class LaunchFileTest(unittest.TestCase):
    def test_apply_sets_the_executable_and_prepends_the_library_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = Path(tmp) / "lib"
            lib.mkdir()
            exe = Path(tmp) / "chrome"
            exe.write_text("")
            bs.remember(tmp, str(exe), [str(lib)])
            env = {"PLAYWRIGHT_BROWSERS_PATH": tmp, "LD_LIBRARY_PATH": "/old"}
            browser_env.apply(env)
        self.assertEqual(str(exe), env[browser_env.EXECUTABLE])
        self.assertEqual(f"{lib}:/old", env["LD_LIBRARY_PATH"])

    def test_nothing_to_remember_removes_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            bs.remember(tmp, "", ["/x"])
            self.assertTrue((Path(tmp) / browser_env.LAUNCH_FILE).exists())
            bs.remember(tmp, "", [])
            self.assertFalse((Path(tmp) / browser_env.LAUNCH_FILE).exists())


class LauncherLinkTest(unittest.TestCase):
    def test_the_links_carry_gzipped_json_in_the_shape_lark_cli_uses(self):
        needs = [app_manifest.Need("callback", "card.action.trigger", "卡片按钮"),
                 app_manifest.Need("event", "application.bot.menu_v6", "菜单"),
                 app_manifest.Need("scope", "im:message", "撤回")]
        [(words, link)] = app_manifest.launcher_links("cli_x", needs)

        def decode(url):
            addons = url.split("addons=")[1]
            return json.loads(gzip.decompress(base64.urlsafe_b64decode(addons + "=" * (-len(addons) % 4))))

        self.assertTrue(link.startswith("https://open.feishu.cn/page/launcher?clientID=cli_x&addons="))
        # One link, one confirm: the events and the callbacks together.
        self.assertEqual({"events": {"items": {"tenant": ["application.bot.menu_v6"], "user": []}},
                          "callbacks": {"items": ["card.action.trigger"]}}, decode(link))
        self.assertIn("事件订阅：", words)
        self.assertIn("回调订阅：卡片按钮", words)
        [(only, url)] = app_manifest.launcher_links("cli_x", needs[:1])
        self.assertEqual({"callbacks": {"items": ["card.action.trigger"]}}, decode(url))
        self.assertEqual([], app_manifest.launcher_links("cli_x", needs[2:]))

    def test_words_are_said_once(self):
        needs = [app_manifest.Need("scope", "a", "产物上传云空间"), app_manifest.Need("scope", "b", "产物上传云空间")]
        self.assertEqual("产物上传云空间", app_manifest.joined(needs))


class FakeCron:
    """crontab -l / crontab - over a string."""

    def __init__(self, table=""):
        self.table = table

    def __call__(self, argv, **kw):
        if argv == ["crontab", "-l"]:
            return done(self.table, 0 if self.table else 1)
        if argv == ["crontab", "-"]:
            self.table = kw["input"]
            return done()
        raise AssertionError(argv)


class CronTest(unittest.TestCase):
    def test_one_marked_line_is_added_once_kept_apart_from_the_users_and_removed(self):
        cron = FakeCron("0 * * * * mine\n")
        first = install.install_cron("/srv/root", cron)
        install.install_cron("/srv/root", cron)
        self.assertEqual(1, cron.table.count(install.CRON_MARK))
        self.assertTrue(cron.table.startswith("0 * * * * mine\n@reboot "))
        self.assertTrue(first["autostart"])
        self.assertIn("--root /srv/root run", cron.table)
        self.assertEqual({"mode": "cron", "registered": True}, install.cron_status(cron))
        self.assertTrue(install.uninstall_cron(cron)["removed"])
        self.assertEqual("0 * * * * mine\n", cron.table)
        self.assertFalse(install.uninstall_cron(cron)["removed"])

    def test_each_service_root_has_its_own_line_and_uninstall_takes_only_its_own(self):
        cron = FakeCron("0 * * * * mine\n")
        install.install_cron("/srv/a", cron)
        install.install_cron("/srv/b", cron)
        install.install_cron("/srv/a", cron)  # Again: replaced, not doubled.
        self.assertEqual(2, cron.table.count(install.CRON_MARK))
        self.assertEqual(1, cron.table.count(f"{install.CRON_MARK} {install.root_tag('/srv/a')}"))
        self.assertTrue(install.cron_status(cron, root="/srv/a")["registered"])
        self.assertTrue(install.cron_status(cron, root="/srv/b")["registered"])
        self.assertFalse(install.cron_status(cron, root="/srv/c")["registered"])
        self.assertTrue(install.uninstall_cron(cron, root="/srv/a")["removed"])
        self.assertFalse(install.uninstall_cron(cron, root="/srv/a")["removed"])
        self.assertIn("--root /srv/b run", cron.table)
        self.assertNotIn("--root /srv/a run", cron.table)
        self.assertIn("0 * * * * mine", cron.table)

    def test_a_line_from_before_the_tag_belongs_to_the_root_in_its_command_and_stays_for_others(self):
        legacy = f"@reboot cd / && python3 -B /x/covey_service.py --root /srv/old run >> /srv/old/l 2>&1 {install.CRON_MARK}\n"
        cron = FakeCron(legacy)
        install.install_cron("/srv/new", cron)
        self.assertIn("--root /srv/old run", cron.table)  # Another root's line is left alone.
        self.assertTrue(install.cron_status(cron, root="/srv/old")["registered"])
        install.install_cron("/srv/old", cron)  # Its own legacy line is replaced by the tagged one.
        self.assertEqual(2, cron.table.count(install.CRON_MARK))
        self.assertEqual(0, sum(x.endswith(install.CRON_MARK) for x in cron.table.splitlines()))

    def test_the_doctor_looks_for_this_roots_line(self):
        table = f"@reboot x {install.CRON_MARK} {install.root_tag('/srv/a')}\n"
        with mock.patch.object(install, "_crontab", return_value=table):
            self.assertEqual("ready", doctor.hosting_report({"mode": "cron"}, run=FakeCron(), root="/srv/a")["status"])
            self.assertEqual("degraded", doctor.hosting_report({"mode": "cron"}, run=FakeCron(), root="/srv/b")["status"])

    def test_a_table_without_a_trailing_newline_is_not_glued_to_the_line(self):
        cron = FakeCron("0 * * * * mine")
        install.install_cron("/srv/root", cron)
        self.assertTrue(cron.table.startswith("0 * * * * mine\n@reboot "))

    def test_auto_picks_cron_without_systemd_unless_declined(self):
        hosting = mock.Mock(spec=[])
        self.assertEqual("cron", install.choose_mode(hosting, platform="linux", systemd_usable=False, cron_usable=True)[0])
        self.assertEqual("manager", install.choose_mode(hosting, platform="linux", systemd_usable=False, cron_usable=True,
                                                        autostart=False)[0])
        self.assertEqual("manager", install.choose_mode(hosting, platform="linux", systemd_usable=False, cron_usable=False)[0])
        self.assertEqual("systemd", install.choose_mode(hosting, platform="linux", systemd_usable=True, cron_usable=True)[0])

    def test_the_doctor_reads_the_cron_receipt(self):
        with mock.patch.object(install, "_crontab", return_value=f"@reboot x {install.CRON_MARK}\n"):
            row = doctor.hosting_report({"mode": "cron"}, run=FakeCron())
        self.assertEqual("ready", row["status"])
        with mock.patch.object(install, "_crontab", return_value="0 * * * * mine\n"):
            row = doctor.hosting_report({"mode": "cron"}, run=FakeCron())
        self.assertEqual("degraded", row["status"])


class UnsubscribedRetryTest(unittest.TestCase):
    """feishu/events.py: a console without the subscription is asked again soon (a scan fixes it); other failures keep the wait."""

    def delay_of(self, stderr_lines):
        from covey.feishu import events
        seen = {}
        ctx = mock.Mock(profile="p")
        with mock.patch.object(events, "consume_event_stream", lambda **kw: seen.update(kw)):
            events.consume(ctx, events.CARD_ACTION_EVENT_KEY,
                           handle=None, set_process=None, shutdown=None, log=None, retry_seconds=300)
        health = events.consumer(seen["argv"][seen["argv"].index("consume") + 1])
        health.starting(mock.Mock())
        for line in stderr_lines:
            health.stderr(line)
        return seen["restart_delay"], health

    def test_not_subscribed_retries_in_twenty_seconds_and_anything_else_in_three_hundred(self):
        delay, health = self.delay_of(['  "message": "EventKey x requires callbacks not subscribed in console: x",'])
        self.assertTrue(health.unsubscribed)
        self.assertEqual(20, delay(2))
        self.assertEqual(5, delay(0))
        delay, health = self.delay_of(["some other failure"])
        self.assertFalse(health.unsubscribed)
        self.assertEqual(300, delay(2))
        health.starting(mock.Mock())  # A new run starts without the flag.
        health.stderr("requires event types not subscribed in console: y")
        health.starting(mock.Mock())
        self.assertFalse(health.unsubscribed)


class CheckScriptTest(unittest.TestCase):
    def test_script_runs_alone_with_a_stub_playwright(self):
        # The probe runs under the Playwright interpreter with -I: it must not name anything of covey (it once used cenv).
        with tempfile.TemporaryDirectory() as tmp:
            stub = Path(tmp) / "playwright"
            stub.mkdir()
            (stub / "__init__.py").write_text("")
            (stub / "sync_api.py").write_text(
                "import os\n"
                "class _B:\n"
                "    def new_page(self):\n"
                "        class P:\n"
                "            def goto(self, url): pass\n"
                "        return P()\n"
                "    def close(self): pass\n"
                "class _C:\n"
                "    def launch(self, headless, executable_path):\n"
                "        print('exe=' + str(executable_path))\n"
                "        return _B()\n"
                "class _P:\n"
                "    chromium = _C()\n"
                "class sync_playwright:\n"
                "    def __enter__(self): return _P()\n"
                "    def __exit__(self, *a): return False\n")
            env = {"PYTHONPATH": tmp, "PATH": "/usr/bin:/bin"}
            done = subprocess.run([sys.executable, "-c", bs.CHECK_SCRIPT], env={**env, "COVEY_BROWSER_EXECUTABLE": "/x/chrome"},
                                  capture_output=True, text=True, timeout=60)
            self.assertEqual(0, done.returncode, done.stderr)
            self.assertIn("exe=/x/chrome", done.stdout)
            done = subprocess.run([sys.executable, "-c", bs.CHECK_SCRIPT], env={**env, "LARK_BRIDGE_BROWSER_EXECUTABLE": "/y/chrome"},
                                  capture_output=True, text=True, timeout=60)
            self.assertIn("exe=/y/chrome", done.stdout)


if __name__ == "__main__":
    unittest.main()
