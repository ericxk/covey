"""./install 的安装器 (service/installer.py, service/install_probe.py, core/qr.py, feishu/install_browser.py 的命令约定):
QR 编码对照 lark-cli 自己画的真实二维码；Harness 探测的分类；步骤编排、默认值、幂等、代理写入、浏览器登录态的清理、
没有浏览器时的退路、扩展钩子、服务注册的参数，全部用假 lark-cli 和假浏览器驱动，不联网。"""
from __future__ import annotations

import contextlib
import io
import json
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import REPO, exec_tempdir

from covey.core import app_manifest, butler_settings, home, qr, switches, welcome
from covey.core import env as cenv
from covey.service import install_probe, installer, web_selfhost
from covey.service.install_probe import Probe

# The QR lark-cli printed for "https://open.feishu.cn/page/cli?user_code=LWEY-WV75&lpv=1.0.98&ocv=1.0.98&from=cli"
# (2026-11-13, a real run), rows as hex of their 37 modules.
LARK_CLI_QR = [
    "1fc4c7fc7f",
    "10402c8241",
    "175562c55d",
    "175c597c5d",
    "17568fa55d",
    "1055199341",
    "1fd555557f",
    "1ee1d300",
    "17cd190a7c",
    "18067b002",
    "1b6ab05ba3",
    "39963e6a1",
    "1c47394ed3",
    "6370b29a1",
    "95bb14efb",
    "13b2b3a132",
    "1ac0ae2bf7",
    "114b3ec0e",
    "13668cc373",
    "1322d7f369",
    "456c11cdc",
    "19211abd0b",
    "1ae1e192bb",
    "48fb0a571",
    "1c766a39f6",
    "1a94ee2484",
    "16ddb846d3",
    "143aa0e5e0",
    "12620a7bf1",
    "10172b19",
    "1fc53c4157",
    "105351b31b",
    "175fcb1bf6",
    "175796ebd3",
    "175b2d1d07",
    "1040f0e151",
    "1fdd4a5c53",
]
LARK_CLI_URL = "https://open.feishu.cn/page/cli?user_code=LWEY-WV75&lpv=1.0.98&ocv=1.0.98&from=cli"

FAKE_LARK = """#!/bin/bash
# A lark-cli that keeps its state in $FAKE_DIR; the fake browser touches the files the flows wait for.
d="$FAKE_DIR"; echo "$@" >> "$d/calls"
case "$*" in
  *"config show"*) [ -f "$d/profile" ] && echo '{"appId": "cli_fake"}' || exit 1 ;;
  *"config init"*) echo "  https://open.feishu.cn/page/cli?user_code=AAAA-BBBB"
     for i in $(seq 100); do [ -f "$d/clicked-create" ] || [ -f "$d/phone" ] && break; sleep 0.05; done
     [ -f "$d/clicked-create" ] || [ -f "$d/phone" ] || exit 1
     touch "$d/profile"; echo "OK: 应用配置成功" ;;
  *"auth status"*) [ -f "$d/authed" ] || exit 1 ;;
  *"auth login"*) echo "  https://accounts.feishu.cn/oauth/v1/device/verify?user_code=CCCC-DDDD"
     for i in $(seq 100); do [ -f "$d/clicked-auth" ] || [ -f "$d/phone" ] && break; sleep 0.05; done
     [ -f "$d/clicked-auth" ] || [ -f "$d/phone" ] || exit 1
     touch "$d/authed" ;;
  *"profile remove"*) rm -f "$d/profile" "$d/authed" "$d/clicked-create" "$d/clicked-auth" ;;
esac
exit 0
"""


class FakeBrowser:
    """What feishu/install_browser.py answers, without a browser: the files the fake lark-cli waits for."""
    opened = []

    def __init__(self, python, home, say, env):
        self.home, self.fake = Path(home), Path(env["FAKE_DIR"])
        self.calls, self.last = [], {}
        FakeBrowser.opened.append(self)
        (self.home / "profile").mkdir()  # The login that must be gone at the end.

    def create_app(self, url, name):
        self.calls.append(("create_app", url, name))
        (self.fake / "clicked-create").touch()
        return {"ok": True, "detail": "已点创建"}

    def authorize(self, url):
        self.calls.append(("authorize", url))
        (self.fake / "clicked-auth").touch()
        return {"ok": True, "detail": "已点开通并授权"}

    def configure(self, app_id, plan):
        self.calls.append(("configure", app_id, json.loads(Path(plan).read_text())))
        self.last = {"event": "done", "version": "1.0.1", "status": "published"}
        return {"ok": True, "detail": "done"}

    def close(self):
        self.calls.append(("close",))


class QrTest(unittest.TestCase):
    def test_the_matrix_equals_the_code_lark_cli_drew_for_the_same_link(self):
        rows = qr.matrix(LARK_CLI_URL)
        self.assertEqual([f"{int(r, 16):037b}" for r in LARK_CLI_QR], rows)

    def test_a_png_is_written_and_too_much_text_is_refused(self):
        data = qr.png(qr.matrix("https://example.com"), scale=2, border=1)
        self.assertEqual(b"\x89PNG\r\n\x1a\n", data[:8])
        with self.assertRaises(ValueError):
            qr.matrix("x" * 8000)

    def test_the_terminal_drawing_uses_the_module_rows(self):
        out = io.StringIO()
        from covey.service.app_check import draw
        draw(qr.matrix("a"), out)
        self.assertGreater(len(out.getvalue().splitlines()), 10)


class ProbeTest(unittest.TestCase):
    def test_the_adapter_maps_the_shared_probe_to_the_installer_kinds(self):
        rows = {"ok": (True, "ok"), "not_logged_in": (False, "not_logged_in"), "network": (False, "network"),
                "timeout": (False, "network"), "not_installed": (False, "other"), "other": (False, "other")}
        for reason, (ok, kind) in rows.items():
            cached = mock.Mock(return_value={"ok": ok, "reason": reason, "detail": "x"})
            result = install_probe.probe("codex", {"A": "1"}, cwd="/w", directory="/d", cached=cached)
            self.assertEqual((ok, kind, "x"), (result.ok, result.kind, result.detail))
            cached.assert_called_once_with("/d", "codex", env={"A": "1"}, cwd="/w", timeout=mock.ANY)

    def test_the_cache_directory_defaults_to_the_probe_directory_of_the_data_root(self):
        from covey.core import harness_probe
        cached = mock.Mock(return_value={"ok": True, "reason": "ok"})
        install_probe.probe("claude", {}, cached=cached)
        self.assertEqual(harness_probe.probe_directory(), cached.call_args.args[0])


class InstallerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fake = self.root / "fake"
        self.exec = exec_tempdir()  # The fake CLIs are run directly: not under the noexec test temp.
        self.addCleanup(self.exec.cleanup)
        self.bin = Path(self.exec.name) / "bin"
        self.fake.mkdir()
        self.bin.mkdir()
        for name, body in (("lark-cli", FAKE_LARK), ("claude", "#!/bin/bash\n"), ("codex", "#!/bin/bash\n")):
            path = self.bin / name
            path.write_text(body)
            path.chmod(path.stat().st_mode | stat.S_IEXEC)
        self.data = self.root / "data"
        self.lines = []
        self.probes = {"claude": Probe(True, "ok"), "codex": Probe(True, "ok")}
        self.asked = []
        FakeBrowser.opened = []
        self.lacking = [app_manifest.Need("callback", "card.action.trigger", "卡片按钮")]
        self.app_name = ""  # What the app is really called (the bot's info); "" when unreadable.
        self.slash_sync = mock.Mock(return_value={
            "ok": True, "error": "", "detail": "", "wanted": ["usage", "board", "settings"],
            "synced": ["usage", "board", "settings"], "unchanged": [], "failed": [],
        })

    def env(self, **extra):
        return {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.root), "FAKE_DIR": str(self.fake),
                "COVEY_PLAYWRIGHT_PYTHON": sys.executable, "COVEY_EXTENSIONS": "", **extra}

    def survey(self, profile, app_id, env=None):
        self.surveyed = getattr(self, "surveyed", []) + [dict(env or {})]
        need = list(self.lacking)
        return {"version": "1.0.0"}, need, []

    def probe(self, name, env, **kwargs):
        self.probed = getattr(self, "probed", []) + [(name, dict(env))]
        result = self.probes[name]
        return result(env) if callable(result) else result

    def make(self, *argv, env=None, interactive=False, ask=None):
        args = installer.parser().parse_args(["--data-home", str(self.data), "--workdir", str(self.root / "work"), *argv])
        ins = installer.Installer(args, env=env or self.env(), say=self.lines.append, interactive=interactive,
                                  ask=ask or self.ask, probe=self.probe, browser_factory=FakeBrowser,
                                  survey_app=self.survey, app_name_of=lambda profile: self.app_name,
                                  app_id_of=lambda profile: "cli_fake" if (self.fake / "profile").exists() else "",
                                  sync_slash=self.slash_sync, feishu_sdk=lambda: getattr(self, "sdk_ok", True))
        return ins

    def ask(self, prompt):
        self.asked.append(prompt)
        return ""

    def text(self):
        return "\n".join(self.lines)

    def calls(self):
        path = self.fake / "calls"
        return path.read_text().splitlines() if path.exists() else []


class FlowTest(InstallerCase):
    def test_default_run_uses_phone_qr_and_syncs_slash_commands_with_no_browser(self):
        (self.fake / "phone").touch()
        self.lacking.clear()
        ins = self.make("--no-start")
        self.assertEqual(0, ins.run())
        self.assertEqual([], FakeBrowser.opened)
        self.slash_sync.assert_called_once()
        self.assertEqual(self.slash_sync.call_args.args[0], "covey")
        self.assertIn("斜杠命令已就绪：/usage、/board、/settings", self.text())
        self.assertNotIn("还差：", self.text())

    def test_one_run_creates_authorizes_configures_and_leaves_no_login(self):
        self.probes["claude"] = Probe(False, install_probe.NOT_LOGGED_IN, "Not logged in")
        self.lacking = [app_manifest.Need("callback", "card.action.trigger", "卡片按钮")]
        ins = self.make("--no-start", "--browser")
        with mock.patch.object(installer.Installer, "lacking_after", return_value=[]):
            self.assertEqual(0, ins.run())
        browser = FakeBrowser.opened[0]
        self.assertEqual(["create_app", "authorize", "configure", "close"], [c[0] for c in browser.calls])
        self.assertEqual("Covey", browser.calls[0][2])
        self.assertEqual(1, len(FakeBrowser.opened))  # One browser, one scan.
        self.assertFalse(browser.home.exists(), "the web login is removed")
        config = json.loads((self.data / "harnesses.json").read_text())
        self.assertEqual(["codex"], config["enabled_harnesses"])  # claude is not logged in: named, not enabled
        for n in range(1, 5):
            self.assertIn(f"[{n}/6]", self.text())
        self.assertIn("claude：没登录", self.text())
        self.assertTrue((self.root / "work").is_dir())
        self.assertEqual(str(self.data / "tmp"), ins.env["TMPDIR"])
        self.assertEqual(str(self.data / "browsers"), ins.env["PLAYWRIGHT_BROWSERS_PATH"])

    def test_a_rerun_keeps_what_the_owner_enabled_and_only_names_a_failed_probe(self):
        (self.fake / "phone").touch()
        self.lacking.clear()
        self.assertEqual(0, self.make("--no-start").run())  # First install: by the probes.
        path = self.data / "harnesses.json"
        config = json.loads(path.read_text())
        config.update(enabled_harnesses=["claude", "codex"], spaces={"notify_space": "oc_x"})
        path.write_text(json.dumps(config))
        self.probes["claude"] = Probe(False, install_probe.NOT_LOGGED_IN, "Not logged in")
        (self.fake / "authed").touch()
        self.lines.clear()
        self.assertEqual(0, self.make("--no-start").run())
        kept = json.loads(path.read_text())
        self.assertEqual(["codex", "claude"], kept["enabled_harnesses"])  # The roster order
        self.assertEqual({"notify_space": "oc_x"}, kept["spaces"])
        self.assertIn("还差：claude 没登录", self.text())
        self.assertIn("启动卡上会显示不可用", self.text())

    def test_an_upgrade_without_a_person_lists_the_missing_menu_and_does_not_open_the_browser(self):
        (self.fake / "phone").touch()
        self.lacking.clear()
        self.assertEqual(0, self.make("--no-start").run())
        (self.fake / "authed").touch()
        FakeBrowser.opened.clear()
        self.lines.clear()
        self.lacking.append(app_manifest.Need("menu", "board", "看板"))
        self.assertEqual(0, self.make("--no-start").run())
        self.assertEqual([], FakeBrowser.opened)
        self.assertIn("还差：飞书应用还缺", self.text())
        self.assertIn("看板", self.text())
        self.assertIn("飞书应用待补", self.text())

    def test_a_second_run_reuses_everything_and_opens_no_browser(self):
        (self.fake / "phone").touch()
        self.lacking.clear()
        self.assertEqual(0, self.make("--no-start").run())
        (self.fake / "authed").touch()
        FakeBrowser.opened.clear()
        self.lines.clear()
        self.assertEqual(0, self.make("--no-start").run())
        self.assertEqual([], FakeBrowser.opened)
        self.assertIn("复用已有应用 cli_fake", self.text())

    def test_reconfigure_removes_the_profile_and_scans_again(self):
        self.lacking.clear()
        self.make("--no-start", "--browser").run()
        (self.fake / "authed").touch()
        FakeBrowser.opened.clear()
        self.assertEqual(0, self.make("--no-start", "--browser", "--reconfigure").run())
        self.assertEqual(1, len(FakeBrowser.opened))
        self.assertTrue(any("profile remove" in c for c in self.calls()))

    def test_keep_login_leaves_the_browser_folder(self):
        self.lacking.clear()
        self.assertEqual(0, self.make("--no-start", "--browser", "--keep-login").run())
        self.assertTrue(FakeBrowser.opened[0].home.exists())
        self.assertIn("--keep-login", self.text())

    def test_no_working_harness_stops_before_anything_is_created(self):
        self.probes = {n: Probe(False, install_probe.NOT_LOGGED_IN, "x") for n in ("claude", "codex")}
        self.assertEqual(1, self.make("--no-start").run())
        self.assertEqual([], FakeBrowser.opened)
        self.assertNotIn("config init", "\n".join(self.calls()))
        self.assertIn("没登录", self.text())

    def test_a_missing_cli_is_named_not_probed(self):
        (self.fake / "phone").touch()
        (self.bin / "claude").unlink()
        self.lacking.clear()
        self.make("--no-start").run()
        self.assertEqual(["codex"], [n for n, _ in self.probed])
        self.assertNotIn("claude：能调通", self.text())

    def test_defaults_need_no_questions(self):
        (self.fake / "phone").touch()
        self.lacking.clear()
        self.make("--no-start").run()
        self.assertEqual([], self.asked)
        self.assertTrue((self.data / "tmp").is_dir() and (self.data / "browsers").is_dir())

    def test_the_default_workdir_in_the_repo_is_in_the_data_root(self):
        args = installer.parser().parse_args(["--data-home", str(self.data)])
        ins = installer.Installer(args, env=self.env(), say=self.lines.append, interactive=False)
        with mock.patch.object(Path, "cwd", return_value=REPO):
            ins.check_environment()
        self.assertEqual(self.data / "home", ins.workdir)


class ProxyTest(InstallerCase):
    def network_until_proxy(self, env):
        return Probe(True, "ok") if env.get("https_proxy") else Probe(False, install_probe.NETWORK, "403")

    def test_a_network_failure_asks_for_a_proxy_and_the_service_gets_it(self):
        (self.fake / "phone").touch()
        self.probes["codex"] = self.network_until_proxy
        self.lacking.clear()
        ins = self.make("--no-start", interactive=True, ask=lambda prompt: "http://127.0.0.1:7890")
        self.assertEqual(0, ins.run())
        # Lowercase, as scripts/deploy_bridge.sh's pre-check compares it: the uppercase one it unsets.
        self.assertEqual("http://127.0.0.1:7890", ins.env["https_proxy"])
        self.assertEqual("http://127.0.0.1:7890", ins.env["http_proxy"])
        self.assertNotIn("HTTPS_PROXY", ins.env)
        self.assertIn(".feishu.cn", ins.env["NO_PROXY"])
        self.assertEqual(2, len([p for p in self.probed if p[0] == "codex"]))
        self.assertIn("代理设置会一起写进服务", self.text())

    def test_non_interactive_says_how_to_set_one_and_enables_the_rest(self):
        (self.fake / "phone").touch()
        self.probes["codex"] = self.network_until_proxy
        self.lacking.clear()
        ins = self.make("--no-start", "--non-interactive")
        self.assertEqual(0, ins.run())
        self.assertEqual([], self.asked)
        self.assertIn("HTTPS_PROXY", self.text())
        self.assertEqual(["claude"], json.loads((self.data / "harnesses.json").read_text())["enabled_harnesses"])

    def test_a_terminal_proxy_is_kept_with_the_feishu_domains_direct(self):
        (self.fake / "phone").touch()
        self.lacking.clear()
        ins = self.make("--no-start", env=self.env(HTTPS_PROXY="http://proxy.example.com:3128", NO_PROXY="corp.local"))
        self.assertEqual(0, ins.run())
        self.assertEqual("corp.local", ins.env["NO_PROXY"].split(",")[0])
        self.assertIn(".larkoffice.com", ins.env["NO_PROXY"])  # privacy: allow 飞书自己的域名
        self.assertEqual("http://proxy.example.com:3128", ins.env["https_proxy"])
        self.assertNotIn("HTTPS_PROXY", ins.env)

    def test_a_lowercase_terminal_proxy_wins_over_the_uppercase_one(self):
        (self.fake / "phone").touch()
        self.lacking.clear()
        ins = self.make("--no-start", env=self.env(HTTPS_PROXY="http://upper.example.com:3128",
                                                   https_proxy="http://lower.example.com:3128"))
        self.assertEqual(0, ins.run())
        self.assertEqual("http://lower.example.com:3128", ins.env["https_proxy"])
        self.assertNotIn("HTTPS_PROXY", ins.env)

    def test_a_proxy_with_credentials_is_refused_without_being_printed(self):
        ins = self.make("--no-start", env=self.env(HTTPS_PROXY="http://u:hunter2@" + "proxy.example.com:3128"))
        self.assertEqual(1, ins.run())
        self.assertNotIn("hunter2", self.text())
        self.assertIn("不带凭据", self.text())


class NoBrowserTest(InstallerCase):
    def test_every_link_goes_to_the_phone_and_what_is_left_is_said(self):
        (self.fake / "phone").touch()
        ins = self.make("--no-start", "--no-browser")
        self.assertEqual(0, ins.run())
        self.assertEqual([], FakeBrowser.opened)
        self.assertIn("https://open.feishu.cn/page/cli?user_code=AAAA-BBBB", self.text())
        self.assertIn("https://accounts.feishu.cn/oauth/v1/device/verify", self.text())
        self.assertIn("手机第 1 步：建应用", self.text())
        self.assertIn("手机第 2 步：授权", self.text())
        self.assertIn("手机第 3 步：回调订阅：卡片按钮", self.text())  # The launcher link of the missing callback.
        self.assertIn("https://open.feishu.cn/page/launcher?clientID=cli_fake&addons=", self.text())
        self.assertIn("还差：手机第 3 步（回调订阅：卡片按钮）要在手机上扫上面的码", self.text())
        self.assertIn("建议把名称改成「Covey」", self.text())
        for n in (1, 2, 3):
            self.assertTrue((self.data / "tmp" / f"link-qr-{n}.png").is_file())

    def test_the_closing_line_names_the_app_as_it_is_called_now_or_no_name(self):
        (self.fake / "phone").touch()
        self.lacking.clear()
        self.app_name = "王小明的飞书 CLI"  # The create page's own proposal, kept by the user.
        self.assertEqual(0, self.make("--no-start").run())
        self.assertIn("去飞书私聊机器人「王小明的飞书 CLI」发一句话", self.text())
        self.assertNotIn("「Covey」发一句话", self.text())
        self.lines.clear()
        self.app_name = ""
        ins = self.make("--no-start")
        ins.app_id = "cli_fake"
        self.assertEqual(0, ins.run())
        self.assertIn("去飞书私聊刚建的机器人发一句话", self.text())

    def test_events_and_callbacks_are_one_phone_step(self):
        (self.fake / "phone").touch()
        self.lacking = [app_manifest.Need("callback", "card.action.trigger", "卡片按钮"),
                        app_manifest.Need("event", "application.bot.menu_v6", "菜单")]
        self.assertEqual(0, self.make("--no-start", "--no-browser").run())
        self.assertEqual(1, self.text().count("page/launcher?clientID="))
        self.assertNotIn("手机第 4 步", self.text())

    def test_not_ready_says_which_scan_it_waits_for(self):
        ins = self.make("--no-start", "--no-browser")
        ins.waiting_steps, ins.ready_seconds = [3], 0
        with mock.patch.object(installer.Installer, "service_up", return_value=False):
            ins.wait_ready()
        self.assertTrue(any("在等你扫完手机第 3 步" in x for x in ins.leftovers))
        self.assertFalse(any("服务起来了但还没就绪" in x for x in ins.leftovers))
        ins = self.make("--no-start", "--no-browser")
        ins.ready_seconds = 0
        with mock.patch.object(installer.Installer, "service_up", return_value=False):
            ins.wait_ready()
        self.assertTrue(any("服务起来了但还没就绪" in x for x in ins.leftovers))

    def test_a_missing_menu_is_a_reminder_the_bridge_card_finishes(self):
        (self.fake / "phone").touch()
        self.lacking = [app_manifest.Need("menu", "weekly_usage", "菜单「用量」", label="用量", icon="x")]
        ins = self.make("--no-start", "--no-browser")
        self.assertEqual(0, ins.run())
        self.assertIn("还差：飞书应用还缺 菜单「用量」", self.text())
        self.assertIn("飞书应用待补", self.text())

    def test_an_interactive_run_waits_for_each_scan_by_polling_the_app(self):
        (self.fake / "phone").touch()
        ins = self.make("--no-start", "--no-browser", interactive=True)
        ins.poll_seconds = 0
        with mock.patch.object(installer.Installer, "lacking", side_effect=[[], [], []]) as lacking:
            offer = [app_manifest.Need("callback", "card.action.trigger", "卡片按钮")]
            ins.app_id = "cli_fake"
            ins.configure_by_phone(offer)
        self.assertEqual([], ins.leftovers)
        self.assertGreaterEqual(lacking.call_count, 1)

    def test_an_unavailable_browser_says_why_and_falls_back_to_the_phone(self):
        from covey.service import browser_setup

        def prepare(*a, **k):
            raise browser_setup.Unavailable("Chromium 起不来：缺系统库")

        env = self.env()
        env.pop("COVEY_PLAYWRIGHT_PYTHON")
        ins = self.make("--browser", env=env)
        ins.prepare_browser = prepare
        self.assertIsNone(ins.ensure_browser())
        self.assertIn("浏览器用不了：Chromium 起不来：缺系统库", self.text())
        self.assertIn("改用手机扫码", self.text())

    def test_browser_hitting_conditional_access_falls_back_to_phone_automatically(self):
        class BlockedBrowser(FakeBrowser):
            def create_app(self, url, name):
                self.calls.append(("create_app", url, name))
                return {"ok": False, "code": "conditional_access", "detail": "租户条件访问拦下"}

            def close(self):
                super().close()
                (self.fake / "phone").touch()  # Let the phone fallback's lark-cli flows proceed after aborting init.

        self.lacking = [app_manifest.Need("callback", "card.action.trigger", "卡片按钮")]
        ins = self.make("--no-start", "--browser")
        ins.browser_factory = BlockedBrowser
        self.assertEqual(0, ins.run())
        self.assertEqual(len(FakeBrowser.opened), 1)
        self.assertEqual([c[0] for c in FakeBrowser.opened[0].calls], ["create_app", "close"])
        self.assertIn("条件访问拦下", self.text())
        self.assertIn("手机第 1 步：建应用", self.text())
        self.assertIn("手机第 2 步：授权", self.text())
        self.assertIn("手机第 3 步：回调订阅：卡片按钮", self.text())
        self.slash_sync.assert_called_once()

    def test_slash_sync_failure_becomes_non_fatal_leftover(self):
        (self.fake / "phone").touch()
        self.lacking.clear()
        self.slash_sync.return_value = {"ok": False, "error": "no_scope", "detail": "99991672",
                                        "wanted": ["usage", "board", "settings"], "synced": [], "unchanged": [], "failed": []}
        self.assertEqual(0, self.make("--no-start").run())
        self.assertIn("还差：斜杠命令没注册到输入框面板", self.text())
        self.assertIn("application:app_slash_command", self.text())


class DetectTest(InstallerCase):
    """T766: what step 3 says of each Harness. A CLI off this shell's PATH in a folder its own installer uses is found
    and its folder goes into the service's PATH; the ones not installed are one line that says no one needs them all;
    dsh is enabled where it is installed and its gateway's key is in a file the service reads, and not said otherwise."""

    def setUp(self):
        super().setUp()
        self.home = Path(self.exec.name) / "home"  # Executable: the fake CLIs found there are run.
        self.home.mkdir()
        (self.data / "tmp").mkdir(parents=True)

    def check(self, **extra):
        ins = self.make(env=self.env(HOME=str(self.home), **extra))
        ins.check_harnesses()
        return ins

    def cli(self, folder, name):
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_text("#!/bin/bash\n")
        path.chmod(0o755)
        return path

    def test_a_cli_in_a_folder_its_installer_uses_is_found_and_the_folder_goes_into_the_service_path(self):
        (self.bin / "claude").unlink()
        for relative in installer.COMMON_DIRS:
            with self.subTest(folder=relative):
                shutil.rmtree(self.home / ".local", ignore_errors=True)
                shutil.rmtree(self.home / ".claude", ignore_errors=True)
                self.lines.clear()
                self.cli(self.home / relative, "claude")
                ins = self.check()
                self.assertEqual(["claude", "codex"], ins.working)
                self.assertTrue(ins.env["PATH"].startswith(f"{self.home / relative}:"))
                self.assertIn(f"claude：在 ~/{relative} 找到（这个终端的 PATH 里没有它），服务的 PATH 会加上这个目录", self.text())
                self.assertIn("claude", [name for name, _ in self.probed])
        shutil.rmtree(self.home / ".local", ignore_errors=True)
        shutil.rmtree(self.home / ".claude", ignore_errors=True)
        self.lines.clear()
        ins = self.check()  # Nowhere: not installed, said in the one line.
        self.assertEqual(["codex"], ins.working)
        self.assertEqual("没装（PATH 里没有 claude 命令）", ins.reasons["claude"])
        self.assertIn("没装 Claude Code、Antigravity：不用每个都装，有一个能调通就能用", self.text())

    def test_what_is_not_installed_is_one_line_that_says_not_every_harness_is_needed(self):
        self.check()
        self.assertIn("没装 Antigravity：不用每个都装，有一个能调通就能用；以后装了哪个，重新运行 ./install 会把它加进来",
                      self.text())
        self.assertNotIn("agy：", self.text())
        self.assertNotIn("dsh", self.text())  # Not installed: not said at all.
        self.cli(self.bin, "agy")
        self.probes["agy"] = Probe(True, "ok")
        self.lines.clear()
        self.check()
        self.assertNotIn("没装", self.text())

    def gateway(self, key_file):
        from covey.core.gateways import Gateway
        return Gateway(name="mygw", display="MyGW", base_url="https://gateway.invalid", key_env="MYGW_API_KEY",
                       key_file=str(key_file), harnesses=("codex", "dsh"))

    def test_dsh_is_enabled_where_its_gateway_s_key_is_in_a_file_the_service_reads(self):
        self.cli(self.bin, "dsh")
        key = self.root / "mygw-api-key"
        cases = (("none", None, {}, "dsh：装了，没有给它用的模型网关（docs/harnesses.md「dsh」），先不启用"),
                 ("no key", "", {}, "dsh：装了，没有 MyGW 的密钥：MyGW 个人密钥未配置"),
                 ("only here", "", {"MYGW_API_KEY": "sk-fake"},
                  f"dsh：装了，MyGW 的密钥只在这个终端的 MYGW_API_KEY 里，服务拿不到：写进 {key}（权限 600）"),
                 ("open", "sk-fake", {}, f"MyGW 的密钥文件 {key} 要改成只有自己能读（chmod 600）"),
                 ("ready", "sk-fake", {}, "dsh：装了，MyGW 的密钥也在，启用"))
        for case, body, extra, words in cases:
            with self.subTest(case=case):
                key.unlink(missing_ok=True)
                if body:
                    key.write_text(body)
                    key.chmod(0o644 if case == "open" else 0o600)
                self.lines.clear()
                gateway = None if body is None else self.gateway(key)
                with mock.patch.object(installer.gateways, "dsh_gateway", return_value=gateway):
                    ins = self.check(**extra)
                self.assertIn(words, self.text())
                self.assertEqual(["dsh"] if case == "ready" else [], ins.set_up)
        ins.write_harness_config()
        self.assertEqual(["codex", "claude", "dsh"], json.loads((self.data / "harnesses.json").read_text())["enabled_harnesses"])
        self.assertIn("dsh", ins.fingerprint(self.data / "harnesses.json")["harnesses"])

    def test_dsh_s_command_is_the_one_harnesses_json_names(self):
        self.data.mkdir(parents=True, exist_ok=True)
        dsh = self.cli(self.home / "tools", "dsh-preview")
        (self.data / "harnesses.json").write_text(json.dumps({"harnesses": {"dsh": {"args": ["--dsh-cmd", str(dsh)]}}}))
        with mock.patch.object(installer.gateways, "dsh_gateway", return_value=None):
            self.check()
        self.assertIn("dsh：装了，没有给它用的模型网关", self.text())


class ExtensionHookTest(InstallerCase):
    def test_hooks_see_the_old_setup_script_helpers_and_may_add_harnesses(self):
        (self.fake / "phone").touch()
        ext = self.root / "ext"
        (ext / "scripts").mkdir(parents=True)
        (ext / "scripts" / "setup_hook.sh").write_text(
            f'ext_ensure() {{ have bash && touch "{self.root}/ensured"; }}\n'
            'ext_detect() { AVAILABLE_HARNESSES+=(dsh); }\n'
            f'ext_login() {{ touch "{self.root}/logged"; }}\n')
        self.lacking.clear()
        ins = self.make("--no-start", interactive=True)
        with mock.patch.object(installer.Installer, "hook_dirs", return_value=[ext]):
            self.assertEqual(0, ins.run())
        self.assertTrue((self.root / "ensured").exists() and (self.root / "logged").exists())
        self.assertIn("dsh", json.loads((self.data / "harnesses.json").read_text())["enabled_harnesses"])

    def test_without_extensions_there_are_no_hooks(self):
        ins = self.make()
        self.assertEqual([], ins.hook_dirs())
        self.assertEqual([], ins.run_hooks("ext_ensure"))


class ServiceTest(InstallerCase):
    def start(self, up=False, install_code=0, *argv, working=("claude", "codex")):
        ins = self.make(*argv)
        ins.app_id, ins.workdir, ins.working = "cli_fake", self.root / "work", list(working)
        self.data.mkdir(exist_ok=True)
        (self.data / "tmp").mkdir(exist_ok=True)
        ran = []

        def run_logged(name, argv, **kwargs):
            ran.append((name, argv))
            return (install_code if name == "service-install" else 0), "launchd (KeepAlive)"

        self.precompiled = mock.Mock()
        patches = (mock.patch.object(installer.Installer, "run_logged", side_effect=run_logged),
                   mock.patch.object(installer.Installer, "service_up", return_value=up),
                   mock.patch.object(installer.Installer, "precompile", self.precompiled))
        with patches[0], patches[1], patches[2]:
            ins.start_service(self.data / "harnesses.json")
        return ins, ran

    def test_the_service_is_launched_with_the_profile_app_and_config_then_registered_for_boot(self):
        ins, ran = self.start()
        boot = dict(ran)["bootstrap"]
        self.assertEqual(["--workdir", str(self.root / "work"), "--profile", "covey", "--app-id", "cli_fake",
                          "--harness-config", str(self.data / "harnesses.json")], boot[1:])
        hosting = dict(ran)["service-install"]
        self.assertEqual(["--mode", "auto", "--activate"], hosting[-3:])
        self.assertEqual(str(self.data / "bridge-services"), hosting[hosting.index("--root") + 1])
        self.assertEqual(["service-install", "bootstrap"], [n for n, _ in ran])  # Hosted first, then registered.
        self.assertTrue((self.data / "install.json").is_file())

    def test_an_unchanged_running_service_is_left_alone(self):
        self.start()
        _, ran = self.start(up=True)
        self.assertEqual([], ran)
        _, ran = self.start(True, 0, "--reconfigure")
        self.assertEqual(2, len(ran))

    def test_an_upgrade_into_another_clone_or_with_other_switches_registers_again(self):
        self.start()
        _, ran = self.start(True, 0, "--no-autostart")  # Same fingerprint: nothing changes, even with another flag.
        self.assertEqual([], ran)
        with mock.patch.object(installer, "REPO_ROOT", self.root / "newer-clone"):
            _, ran = self.start(up=True)
        self.assertEqual(2, len(ran))
        _, ran = self.start(up=True)  # The record now names the older clone again: registers back.
        self.assertEqual(2, len(ran))
        ins = self.make(env=self.env(COVEY_PINNED_TOPICS="1"))
        ins.app_id, ins.workdir, ins.working = "cli_fake", self.root / "work", ["codex"]
        self.assertEqual("1", ins.fingerprint(self.data / "harnesses.json")["settings"]["LARK_BRIDGE_PINNED_TOPICS"])  # Fingerprints keep the old spelling (core/env.py canonical).

    def test_a_new_service_gets_the_install_switches_and_showcase_the_demo_set_on_top(self):
        ins, _ = self.start()
        self.assertEqual({key: cenv.get(key, None, ins.env) for key in switches.SHOWCASE},
                         {key: switches.INSTALL.get(key) for key in switches.SHOWCASE})
        self.assertIsNone(cenv.get("COVEY_REVISIT", None, ins.env))  # 到点回来看, 记忆 ... are on unset (T813).
        ins, ran = self.start(False, 0, "--showcase")  # Registered (the service is not up) with the 演示套餐.
        self.assertEqual(2, len(ran))
        self.assertEqual({key: cenv.get(key, None, ins.env) for key in switches.SHOWCASE}, switches.SHOWCASE)
        report = {"changed": {"LARK_BRIDGE_COLLAB": {"from": None, "to": "1"}}}
        with mock.patch.object(installer, "change_settings", return_value=report) as changed:
            ins, ran = self.start(True, 0, "--showcase")  # Running unchanged: only its manifest's switches change.
        self.assertEqual([], ran)
        changed.assert_called_once_with(self.data / "bridge-services", self.data / "harness-cli_fake", {}, preset="showcase")
        self.assertIn("演示套餐打开了 1 个开关", self.text())
        with mock.patch.object(installer, "change_settings", side_effect=ValueError("job #3 (running) would be interrupted")):
            ins, ran = self.start(True, 0, "--showcase")
        self.assertIn("演示套餐没打开（job #3 (running) would be interrupted）", "\n".join(ins.leftovers))
        ins = self.make("--showcase", env=self.env(COVEY_DIGEST_TIMES="10:00", COVEY_COLLAB="0"))
        ins.default_switches()  # What the shell set stays, as with the install switches.
        self.assertEqual(("10:00", "0", "sdk"), tuple(cenv.get(key, None, ins.env)
                                                  for key in ("COVEY_DIGEST_TIMES", "COVEY_COLLAB", "COVEY_CARD_TRANSPORT")))

    def test_the_checkout_is_precompiled_before_registering_even_when_nothing_else_changed(self):
        self.start()
        self.precompiled.assert_called_once_with()
        self.start(up=True)  # A pull since the last run: the stale .pyc files are refreshed, the service is left alone.
        self.precompiled.assert_called_once_with()

    def test_precompile_runs_the_script_with_the_registered_python_and_only_says_a_failure(self):
        said = []
        ins = self.make()
        ins.say = said.append
        ok = subprocess.CompletedProcess([], 0, '{"ok": true, "failed": []}\n', "")
        with mock.patch.object(installer.subprocess, "run", return_value=ok) as run:
            ins.precompile()
        argv = run.call_args.args[0]
        self.assertEqual(["-B", "-I", str(installer.REPO_ROOT / "scripts" / "precompile_release.py"),
                          str(installer.REPO_ROOT)], argv[1:])
        self.assertEqual([], said)
        for broken in (subprocess.CompletedProcess([], 0, '{"ok": false}\n', ""), subprocess.CompletedProcess([], 1, "", "x"),
                       subprocess.CompletedProcess([], 0, "", ""), OSError("no python")):
            said.clear()
            with mock.patch.object(installer.subprocess, "run", **({"side_effect": broken} if isinstance(broken, OSError)
                                                                  else {"return_value": broken})):
                ins.precompile()
            self.assertEqual(1, len(said), broken)
            self.assertIn("预编译", said[0])

    def test_boot_registration_failing_is_said_not_fatal(self):
        ins, _ = self.start(install_code=2)
        self.assertTrue(any("开机自启没注册上" in x for x in ins.leftovers))


class AnswerWaitTest(InstallerCase):
    """docs/install.md 第一个回答: after the service is up, a run in a terminal waits for the first answer in the private
    chat (core/welcome.py first_answer), says each step once, and ends on Ctrl-C or after ANSWER_SECONDS: never a failure."""

    def finish(self, stages, *, interactive=True, ready=True, state=welcome.SENT, seconds=None):
        """run() with every step done and the service ready or not, ``first_answer`` answering ``stages`` (a list, one
        per look, or one stage for every look): its exit code."""
        ins = self.make(interactive=interactive)
        ins.app_id, ins.poll_seconds, ins.answer_seconds = "cli_fake", 0, seconds
        folder = self.data / "harness-cli_fake"
        folder.mkdir(parents=True, exist_ok=True)
        if state:
            (folder / welcome.RECORD).write_text(json.dumps({"state": state}))
        answer = {"side_effect": stages} if isinstance(stages, list) else {"return_value": stages}
        with contextlib.ExitStack() as stack:
            for name in ("check_environment", "ensure_lark_cli", "check_harnesses", "feishu_app", "start_service"):
                stack.enter_context(mock.patch.object(installer.Installer, name))
            stack.enter_context(mock.patch.object(installer.Installer, "write_harness_config",
                                                  return_value=self.data / "harnesses.json"))
            stack.enter_context(mock.patch.object(installer.Installer, "wait_ready",
                                                  lambda self: setattr(self, "ready", ready)))
            self.looked = stack.enter_context(mock.patch.object(installer.welcome, "first_answer", **answer))
            return ins.run()

    def test_a_terminal_run_waits_until_the_first_answer_and_says_each_step_once(self):
        stages = [welcome.NONE, welcome.NONE, welcome.LAUNCH, welcome.LAUNCH, welcome.RECEIVED, welcome.ANSWERED]
        self.assertEqual(0, self.finish(stages))
        self.assertIn("完成。去飞书私聊刚建的机器人发一句话，这里等它回答（最多 10 分钟，Ctrl-C 不等）。", self.text())
        said = [line.strip() for line in self.lines[self.lines.index(next(x for x in self.lines if "这里等它回答" in x)) + 1:]]
        self.assertEqual(said, ["私聊里有一张欢迎卡，点上面的示例也行", installer.ANSWER_WORDS[welcome.LAUNCH],
                                installer.STARTED_WORDS, installer.ANSWER_WORDS[welcome.ANSWERED]])
        self.assertEqual(self.looked.call_args.args[0], self.data / "harness-cli_fake")

    def test_running_out_is_no_failure_and_says_what_to_look_at(self):
        for stage, words in ((welcome.NONE, "扫码后机器人没反应"), (welcome.LAUNCH, "开始任务"), (welcome.RECEIVED, "发消息后没有反应")):
            with self.subTest(stage=stage):
                self.lines.clear()
                self.assertEqual(0, self.finish(stage, seconds=0))
                self.assertEqual(self.lines[-1], installer.TIMEOUT_WORDS[stage])
                self.assertIn(words, self.lines[-1])
                self.assertIn("不影响安装", self.lines[-1])

    def test_ctrl_c_ends_the_wait_and_the_install_is_done(self):
        self.assertEqual(0, self.finish([welcome.NONE, KeyboardInterrupt()]))
        self.assertIn("不等了；机器人照常在运行。", self.text())
        self.assertNotIn("已取消", self.text())

    def test_no_terminal_no_service_or_a_chat_answered_before_waits_for_nothing(self):
        plain = "\n完成。去飞书私聊刚建的机器人发一句话。"
        for case in ({"interactive": False}, {"ready": False}, {"state": welcome.SKIPPED}, {"stages": welcome.ANSWERED}):
            with self.subTest(**{key: str(value) for key, value in case.items()}):
                self.lines.clear()
                self.assertEqual(0, self.finish(**{"stages": welcome.NONE, **case}))
                self.assertEqual(self.lines[-1], plain)
                self.assertNotIn("这里等它回答", self.text())
                if "stages" not in case:
                    self.looked.assert_not_called()


class AppReadyTest(InstallerCase):
    def test_an_app_with_no_user_login_is_not_ready_even_when_the_verify_exits_zero(self):
        ins = self.make()
        ins.app_id_of = lambda profile: "cli_x"
        for out, want in (('{"identities": {"user": {"status": "missing", "available": false}}}', ""),
                          ('{"identities": {"user": {"status": "ready", "available": true}}}', "cli_x"), ("", "cli_x")):
            done = subprocess.CompletedProcess([], 0, out, "")
            with mock.patch.object(installer.subprocess, "run", return_value=done):
                self.assertEqual(want, ins.app_ready(), out)


class LoginCopyTest(InstallerCase):
    def test_the_hidden_login_option_starts_the_browser_from_the_copy_and_is_not_in_help(self):
        source = self.root / "copy"
        (source / "profile").mkdir(parents=True)
        (source / "profile" / "Cookies").write_text("c")
        ins = self.make("--browser-login-dir", str(source))
        (self.data / "tmp").mkdir(parents=True, exist_ok=True)
        ins.browser_factory = lambda *args: object()
        ins.open_browser("python")
        self.assertEqual("c", (ins.browser_home / "profile" / "Cookies").read_text())
        self.assertNotIn("browser-login-dir", installer.parser().format_help())


class SwitchesTest(InstallerCase):
    """The switches are settled before the app's needs are read, and a re-run keeps what the registered service has (T647 P0-9)."""

    def registered(self, environment):
        from covey.service.manager import register
        state = self.data / "harness-cli_fake"
        state.mkdir(parents=True, exist_ok=True)
        register(self.data / "bridge-services", state, [sys.executable, "-B", "/absolute/release/bin/covey_bridge.py"],
                 workdir=self.root, environment=environment, health_timeout=90)

    def settled(self, *argv, **shell):
        ins = self.make(*argv, env=self.env(**shell))
        ins.app_id = "cli_fake"
        ins.settle_switches()
        return lambda name: cenv.get(name, None, ins.env), ins

    def test_the_app_is_checked_against_the_switches_the_new_service_starts_with(self):
        (self.fake / "phone").touch()
        self.lacking.clear()
        self.assertEqual(0, self.make("--no-start", interactive=True).run())
        seen = self.surveyed[0]
        self.assertIsNone(cenv.get("COVEY_PINNED_TOPICS", None, seen))  # On unset: the read event is asked for (T813).
        self.surveyed.clear()
        self.assertEqual(0, self.make("--no-start", "--showcase", interactive=True).run())
        seen = self.surveyed[0]  # The menu 看板, the Pin scopes and the read event are asked for in the one scan.
        self.assertEqual({name: cenv.get(name, None, seen) for name in switches.SHOWCASE}, switches.SHOWCASE)

    def test_a_rerun_keeps_the_registered_settings_adds_no_new_default_and_the_shell_wins(self):
        self.registered({"PATH": "/absolute/bin", "HTTPS_PROXY": "http://proxy.example.invalid:3128",
                         "LARK_BRIDGE_MEMORY": "0", "LARK_BRIDGE_DIGEST_TIMES": "10:00",
                         "LARK_BRIDGE_HISTORY_CMD": "/absolute/kb", "LARK_BRIDGE_PROJECTS": "1"})
        get, ins = self.settled(COVEY_DIGEST_TIMES="08:00")
        self.assertEqual(("08:00", "0", "/absolute/kb", "1"),
                         tuple(get(name) for name in ("COVEY_DIGEST_TIMES", "COVEY_MEMORY", "COVEY_HISTORY_CMD", "COVEY_PROJECTS")))
        self.assertIsNone(get("COVEY_REVISIT"))  # Registered already: no new default.
        self.assertNotIn("HTTPS_PROXY", ins.env)  # The proxy and PATH are this run's own.
        self.assertNotEqual(ins.env["PATH"], "/absolute/bin")
        before = dict(ins.env)
        ins.settle_switches()
        self.assertEqual(ins.env, before)
        get, ins = self.settled("--showcase", COVEY_DIGEST_TIMES="08:00", COVEY_COLLAB="0")
        # The 演示套餐 (the SDK and 协作 since T813) turns on what the manifest lacks or has off; what this shell sets wins,
        # and what the manifest set off of the rest stays off.
        self.assertEqual(("0", "08:00", "0", None, "/absolute/kb"),
                         tuple(get(name) for name in ("COVEY_MEMORY", "COVEY_DIGEST_TIMES", "COVEY_COLLAB",
                                                      "COVEY_PINNED_TOPICS", "COVEY_HISTORY_CMD")))

    def test_registering_again_starts_from_the_registered_settings(self):
        self.registered({"LARK_BRIDGE_HISTORY_CMD": "/absolute/kb", "LARK_BRIDGE_REVISIT": "0"})
        ins = self.make()
        ins.app_id, ins.workdir, ins.working = "cli_fake", self.root / "work", ["codex"]
        (self.data / "tmp").mkdir(parents=True, exist_ok=True)
        with mock.patch.object(installer.Installer, "run_logged", return_value=(0, "")) as ran, \
                mock.patch.object(installer.Installer, "service_up", return_value=False), \
                mock.patch.object(installer.Installer, "precompile"):
            ins.start_service(self.data / "harnesses.json")
        self.assertEqual(["service-install", "bootstrap"], [call.args[0] for call in ran.call_args_list])
        self.assertEqual(("/absolute/kb", "0"), tuple(cenv.get(name, None, ins.env)
                                                for name in ("COVEY_HISTORY_CMD", "COVEY_REVISIT")))


class FeishuSdkTest(InstallerCase):
    """A new service starts on the Feishu SDK's events and card writes (with the events kept on disk until handled) where the
    service's Python has the SDK, installed with pip if need be; else they stay on lark-cli and the closing words say how to turn
    them on later."""

    def settled(self, *argv, **shell):
        ins = self.make(*argv, env=self.env(**shell))
        ins.app_id = "cli_fake"
        ins.settle_switches()
        return ins

    def test_the_install_set_has_the_sdk_switches_and_they_need_the_feishu_sdk_extra(self):
        self.assertEqual({key: switches.INSTALL[key] for key in switches.SDK_SWITCHES},
                         {"COVEY_EVENT_TRANSPORT": "sdk", "COVEY_CARD_TRANSPORT": "sdk"})
        text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
        extra = re.search(r'^feishu-sdk = \[(.*)\]$', text, re.M).group(1)
        self.assertEqual(tuple(re.findall(r'"([^"]+)"', extra)), installer.SDK_REQUIREMENTS)

    def test_a_new_service_is_on_the_sdk_where_it_imports(self):
        ins = self.settled()
        self.assertEqual((cenv.get("COVEY_EVENT_TRANSPORT", None, ins.env), cenv.get("COVEY_CARD_TRANSPORT", None, ins.env)),
                         ("sdk", "sdk"))

    def test_without_the_sdk_the_events_and_cards_stay_on_lark_cli(self):
        self.sdk_ok = False
        ins = self.settled()
        self.assertIsNone(cenv.get("COVEY_EVENT_TRANSPORT", None, ins.env))
        self.assertIsNone(cenv.get("COVEY_CARD_TRANSPORT", None, ins.env))
        self.assertIsNone(cenv.get("COVEY_PROJECTS", None, ins.env))  # On unset: never written (T813).

    def test_the_shell_wins_and_a_registered_service_gets_no_new_default(self):
        ins = self.settled(COVEY_EVENT_TRANSPORT="cli")
        self.assertEqual(cenv.get("COVEY_EVENT_TRANSPORT", None, ins.env), "cli")
        from covey.service.manager import register
        state = self.data / "harness-cli_fake"
        state.mkdir(parents=True, exist_ok=True)
        register(self.data / "bridge-services", state, [sys.executable, "-B", "/absolute/release/bin/covey_bridge.py"],
                 workdir=self.root, environment={"LARK_BRIDGE_PROJECTS": "1"}, health_timeout=90)
        ins = self.settled()
        self.assertIsNone(cenv.get("COVEY_EVENT_TRANSPORT", None, ins.env))

    def fake_python(self, has_after_pip=True):
        """A python3 first on PATH that has the SDK once pip ran (``has_after_pip``); its calls in fake/python-calls."""
        path = self.bin / "python3"
        path.write_text(f"""#!/bin/bash
echo "$*" >> {self.fake}/python-calls
case "$*" in
  *"-m pip install"*) {"touch " + str(self.fake / "sdk") if has_after_pip else "echo 'error: externally-managed-environment'; exit 1"} ;;
  *"sys.prefix"*) echo False ;;
  *"importlib.metadata"*) [ -e {self.fake}/sdk ] || exit 1 ;;
esac
""")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)

    def real_check(self):
        args = installer.parser().parse_args(["--data-home", str(self.data), "--workdir", str(self.root / "work")])
        ins = installer.Installer(args, env=self.env(), say=self.lines.append, interactive=False, ask=self.ask,
                                  probe=self.probe, survey_app=self.survey, sync_slash=self.slash_sync)
        (self.data / "install-logs").mkdir(parents=True, exist_ok=True)
        return ins

    def test_the_sdk_is_installed_with_pip_into_the_users_site_when_missing(self):
        self.fake_python()
        ins = self.real_check()
        self.assertTrue(ins.feishu_sdk())
        self.assertTrue(ins.feishu_sdk())  # Asked once a run.
        calls = (self.fake / "python-calls").read_text().splitlines()
        pip = [line for line in calls if "-m pip install" in line]
        self.assertEqual(len(pip), 1)
        self.assertIn("--user lark-oapi==1.7.4 cryptography>=43", pip[0])

    def test_a_pip_that_cannot_install_leaves_lark_cli_and_says_how_to_turn_it_on(self):
        self.fake_python(has_after_pip=False)
        ins = self.real_check()
        self.assertFalse(ins.feishu_sdk())
        self.assertIn("照旧用 lark-cli", self.text())
        self.assertIn("COVEY_EVENT_TRANSPORT=sdk", self.text())


class UpgraderTest(InstallerCase):
    """A service registered from a git checkout gets <data>/upgrade.json (scripts/upgrade.sh for it): the page's 版本与更新 and
    COVEY_AUTO_UPGRADE use it. Not from a release, and never over an extension installer's."""

    def register(self):
        ins = self.make()
        ins.app_id, ins.workdir, ins.working = "cli_fake", self.root / "work", ["codex"]
        (self.data / "tmp").mkdir(parents=True, exist_ok=True)
        with mock.patch.object(installer.Installer, "run_logged", return_value=(0, "")), \
                mock.patch.object(installer.Installer, "service_up", return_value=False), \
                mock.patch.object(installer.Installer, "precompile"):
            ins.start_service(self.data / "harnesses.json")
        return ins

    def test_a_checkout_writes_the_upgrader_for_its_service(self):
        ins = self.register()
        written = json.loads((self.data / "upgrade.json").read_text())
        script = str(installer.REPO_ROOT / "scripts" / "upgrade.sh")
        state = str(ins.service_state())
        self.assertEqual(written, {"by": "covey", "status": [script, "--status", "--json", "--state-dir", state],
                                   "run": [script, "--state-dir", state]})
        from covey.app import self_upgrade
        self.assertIsNotNone(self_upgrade.config(self.data))  # What the Bridge accepts.

    def test_an_extension_s_upgrader_stays_and_a_release_writes_none(self):
        self.data.mkdir(parents=True, exist_ok=True)
        theirs = {"by": "covey-internal", "status": ["/absolute/x", "status"], "run": ["/absolute/x", "upgrade"]}
        (self.data / "upgrade.json").write_text(json.dumps(theirs))
        self.register()
        self.assertEqual(json.loads((self.data / "upgrade.json").read_text()), theirs)
        (self.data / "upgrade.json").unlink()
        with mock.patch.object(installer, "REPO_ROOT", self.root / "release"):
            self.register()
        self.assertFalse((self.data / "upgrade.json").exists())


class WorkspaceTest(InstallerCase):
    """D1 (T647): in the checkout without --workdir a terminal run asks once where new topics work; Enter (or a directory that
    is not there, twice) keeps <data>/home, which is seeded (core/home.py seed_home) while it is empty; a re-run
    keeps the workspace the last run registered (install.json); a project directory is never written to."""

    def check(self, *, interactive, answers=(), here=REPO, argv=()):
        args = installer.parser().parse_args(["--data-home", str(self.data), *argv])
        replies, asked = iter(answers), []

        def ask(prompt):
            asked.append(prompt)
            return next(replies, "")
        ins = installer.Installer(args, env=self.env(), say=self.lines.append, interactive=interactive, ask=ask)
        with mock.patch.object(Path, "cwd", return_value=here):
            ins.check_environment()
        return ins, asked

    def test_a_terminal_run_in_the_checkout_asks_once_and_takes_a_project_as_it_is(self):
        project = self.root / "myapp"
        project.mkdir()
        ins, asked = self.check(interactive=True, answers=[str(project)])
        self.assertEqual((ins.workdir, len(asked)), (project.resolve(), 1))
        self.assertIn("新话题默认在哪个目录干活", asked[0])
        self.assertEqual(list(project.iterdir()), [])  # Nothing is written into a project.
        self.assertFalse((self.data / "home").exists())

    def test_enter_or_a_missing_directory_twice_keeps_the_new_workspace_with_its_guide(self):
        for number, answers in enumerate(([""], ["/absolute/nowhere", "/absolute/nowhere-either"])):
            with self.subTest(answers=answers):
                self.data = self.root / f"data-{number}"
                self.lines.clear()
                ins, asked = self.check(interactive=True, answers=answers)
                self.assertEqual((ins.workdir, len(asked)), (self.data / "home", len(answers)))
                self.assertTrue(home.seeded(ins.workdir))
                self.assertTrue((ins.workdir / "AGENTS.md").is_file())
                self.assertTrue(welcome.bare_workspace(ins.workdir))
                self.assertIn("「设置」→「工作区」", self.text())
                self.assertIn("--workdir", self.text())
        self.assertIn("没有这个目录：/absolute/nowhere", self.text())

    def test_no_terminal_asks_nothing_and_a_rerun_keeps_the_workspace_it_registered(self):
        ins, asked = self.check(interactive=False)
        self.assertEqual((asked, ins.workdir), ([], self.data / "home"))
        project = self.root / "proj"
        project.mkdir()
        (self.data / "install.json").write_text(json.dumps({"workdir": str(project)}))
        ins, asked = self.check(interactive=True)  # A terminal in the checkout: still no question.
        self.assertEqual((asked, ins.workdir), ([], project))
        ins, _ = self.check(interactive=True, argv=["--workdir", str(self.root / "other")])  # --workdir wins.
        self.assertEqual(ins.workdir, (self.root / "other").resolve())
        (self.data / "install.json").write_text(json.dumps({"workdir": "/absolute/gone"}))  # Not a directory any more.
        ins, asked = self.check(interactive=False, here=project)  # Outside the checkout: the current directory.
        self.assertEqual((asked, ins.workdir), ([], project))

    def test_a_home_with_anything_in_it_is_taken_over_and_written_nothing(self):
        folder = self.data / "home"
        folder.mkdir(parents=True)
        (folder / "notes.txt").write_text("mine")
        self.check(interactive=False)
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["notes.txt"])
        self.assertNotIn("AGENTS.md", self.text())

    def test_an_old_deployment_keeps_its_registered_workspace_unseeded(self):
        old = self.root / "old-workspace"
        old.mkdir()
        (self.data).mkdir(parents=True, exist_ok=True)
        (self.data / "install.json").write_text(json.dumps({"workdir": str(old)}))
        ins, _ = self.check(interactive=False)
        self.assertEqual(ins.workdir, old)
        self.assertEqual(list(old.iterdir()), [])
        self.assertFalse((self.data / "home").exists())

    def test_the_run_writes_the_covey_shim_into_the_data_root(self):
        (self.fake / "phone").touch()
        self.lacking.clear()
        self.assertEqual(0, self.make("--no-start").run(), self.text())
        shim = self.data / "bin" / "covey"
        self.assertTrue(shim.is_file() and shim.stat().st_mode & 0o100)  # Executable (os.access says no on a noexec test directory).
        self.assertIn(str(installer.REPO_ROOT), shim.read_text())


class StopTest(InstallerCase):
    """D7 (T647): where a run stops it says why, what to do next (下一步) and where the logs are; a command that hangs or a
    directory that cannot be made is said the same way, never a traceback."""

    def test_a_data_root_that_cannot_be_made_names_one_that_can(self):
        ins = self.make("--no-start")
        with mock.patch.object(Path, "mkdir", side_effect=PermissionError(13, "Permission denied")):
            self.assertEqual(1, ins.run())
        self.assertIn("安装停在这里：数据根建不了", self.text())
        self.assertIn("Permission denied", self.text())
        self.assertIn("下一步：换一个自己能写的目录：./install --data-home", self.text())

    def test_no_working_harness_says_how_to_get_one_and_where_the_logs_are(self):
        self.probes = {n: Probe(False, install_probe.NOT_LOGGED_IN, "x") for n in ("claude", "codex")}
        (self.data / "install-logs").mkdir(parents=True)
        self.assertEqual(1, self.make("--no-start").run())
        text = self.text()
        self.assertIn("安装停在这里：没有能调通的 Harness", text)
        self.assertIn("    claude：没登录", text)
        self.assertIn("下一步：", text)
        for words in ("codex login", "HTTPS_PROXY", "claude -p"):
            self.assertIn(words, text)
        self.assertIn(f"安装日志在 {self.data / 'install-logs'}", text)
        self.assertLess(text.index("    codex：没登录"), text.index("下一步："))

    def test_a_command_that_hangs_is_said_with_a_next_step_not_a_traceback(self):
        for error in (subprocess.TimeoutExpired(["lark-cli", "auth", "status"], 60), OSError(28, "No space left on device")):
            with self.subTest(error=type(error).__name__):
                self.lines.clear()
                ins = self.make("--no-start")
                with mock.patch.object(installer.Installer, "feishu_app", side_effect=error):
                    self.assertEqual(1, ins.run())
                self.assertIn(f"安装停在这里：{type(error).__name__}", self.text())
                self.assertIn("下一步：", self.text())

    def test_each_step_that_stops_carries_its_next_step(self):
        with self.assertRaises(installer.Fail) as caught:
            installer.Installer.expect({"ok": False, "detail": "按钮没找到"})
        self.assertIn("--no-browser", caught.exception.next)
        ins = self.make()
        ins.app_id, ins.workdir, ins.working = "cli_fake", self.root / "work", ["codex"]
        (self.data / "tmp").mkdir(parents=True)
        with mock.patch.object(installer.Installer, "run_logged", side_effect=lambda name, argv, **kw: (1 if name == "bootstrap" else 0, "boom")), \
                mock.patch.object(installer.Installer, "service_up", return_value=False), \
                mock.patch.object(installer.Installer, "precompile"), self.assertRaises(installer.Fail) as caught:
            ins.start_service(self.data / "harnesses.json")
        self.assertIn("bootstrap.log", caught.exception.next)
        ins = self.make()
        with mock.patch.object(installer.subprocess, "Popen") as popen, self.assertRaises(installer.Fail) as caught:
            popen.return_value.stdout = io.StringIO("Error: denied\n")
            popen.return_value.wait.return_value = 1
            popen.return_value.poll.return_value = 0
            (self.data / "install-logs").mkdir(parents=True, exist_ok=True)
            ins.run_flow(["lark-cli", "config", "init"], lambda link: None, wait=5)
        self.assertIn("--reconfigure", caught.exception.next)

    def test_a_service_not_ready_in_time_says_what_the_doctor_says(self):
        ins = self.make()
        ins.app_id, ins.ready_seconds = "cli_fake", 0
        said = ["结论：有 1 项要看", "✗ 机器人 harness-cli_fake 有问题：进程不在"]
        with mock.patch.object(installer.Installer, "service_up", return_value=False), \
                mock.patch.object(installer.Installer, "doctor_lines", return_value=said):
            ins.wait_ready()
        self.assertIn("诊断这么说", ins.leftovers[-1])
        self.assertIn("    ✗ 机器人 harness-cli_fake 有问题：进程不在", ins.leftovers[-1])
        with mock.patch.object(installer.Installer, "service_up", return_value=False), \
                mock.patch.object(installer.Installer, "doctor_lines", return_value=[]):
            ins.wait_ready()
        self.assertIn("--runtime-only", ins.leftovers[-1])
        self.assertIn(str(self.data / "harness-cli_fake" / "bridge.log"), ins.leftovers[-1])
        done = subprocess.CompletedProcess([], 2, "Covey 诊断\n\n结论：有 1 项要看\n", "")
        with mock.patch.object(installer.subprocess, "run", return_value=done) as run:
            self.assertEqual(ins.doctor_lines(), ["Covey 诊断", "结论：有 1 项要看"])
        argv = run.call_args.args[0]
        self.assertEqual(argv[argv.index("--root") + 1:], [str(self.data / "bridge-services"), "--runtime-only", "--summary"])


class ClosingTest(InstallerCase):
    """D5 (T647): once a service is registered the last lines say where things are, what it runs with (its manifest's
    switches, in words) and the commands for more; then the call to action stays the last line."""

    def registered(self, environment):
        from covey.service.manager import register
        state = self.data / "harness-cli_fake"
        state.mkdir(parents=True, exist_ok=True)
        register(self.data / "bridge-services", state, [sys.executable, "-B", "/absolute/release/bin/covey_bridge.py"],
                 workdir=self.root, environment=environment, health_timeout=90)

    def closing(self, working=("claude", "codex")):
        ins = self.make()
        ins.app_id, ins.workdir, ins.working = "cli_fake", self.root / "work", list(working)
        with mock.patch.object(Path, "cwd", return_value=installer.REPO_ROOT):
            return "\n".join(ins.closing_lines())

    def test_a_new_service_names_its_places_its_features_and_the_commands_for_more(self):
        self.registered({cenv.spellings(key)[-1]: value for key, value in switches.INSTALL.items()})
        text = self.closing()
        state = self.data / "harness-cli_fake"
        for words in (f"数据根      {self.data}", f"状态目录    {state}（日志 {state / 'bridge.log'}）",
                      f"默认工作区  {self.root / 'work'}",
                      "已经开着：管家、专项（叫醒负责人、停住问你、专项记忆、问题分流）、整理专项（只提议）、"
                      "到点回来看、记忆、知识条目、置顶的「手上的事」、话题看板、早晚汇总（09:30、21:30）、管家同时做几件事、派发卡、"
                      "飞书 SDK 收消息（收下先落盘，重启不丢）、飞书 SDK 写卡片",
                      f"演示套餐  python3 -B bin/covey_service.py set --state-dir {state} --preset showcase",
                      "--dry-run COVEY_DIGEST_TIMES=09:30,21:30", "docs/configuration.md「开关速查」", "python3 -B bin/covey_doctor.py",
                      "scripts/upgrade.sh"):
            self.assertIn(words, text)

    def test_the_demo_set_on_is_not_offered_again_and_codex_alone_has_no_butler(self):
        self.registered({cenv.spellings(key)[-1]: value for key, value in switches.SHOWCASE.items()})
        text = self.closing(working=("codex",))
        self.assertNotIn("--preset showcase", text)
        self.assertNotIn("管家、", text)
        for words in ("协作（开发中）", "置顶的「手上的事」", "话题看板", "早晚汇总（09:30、21:30）", "管家同时做几件事", "派发卡"):
            self.assertIn(words, text)
        self.registered({"LARK_BRIDGE_PROJECTS": "0", "LARK_BRIDGE_PROJECT_ASK": "1", "LARK_BRIDGE_MAIN_THREAD": "0",
                         "LARK_BRIDGE_REVISIT": "0"})
        on = next(line for line in self.closing().splitlines() if line.startswith("已经开着："))
        # The butler off, projects off (an extra of theirs alone is no feature, nor is 整理专项), 到点回来看 set off.
        self.assertTrue(on.startswith("已经开着：记忆、"), on)
        self.assertNotIn("专项", on)

    def test_every_switch_the_install_or_the_demo_set_writes_has_its_words(self):
        self.assertEqual(set(switches.SHOWCASE) - set(installer.SWITCH_WORDS), set())
        self.assertEqual(set(installer.PROJECT_PARTS) - set(switches.DEFAULTS), set())

    def test_the_lines_come_after_what_is_left_and_before_the_call_to_action(self):
        self.registered({"LARK_BRIDGE_REVISIT": "1"})
        ins = self.make()
        ins.app_id = "cli_fake"
        ins.leftovers.append("飞书应用还缺 看板")
        with contextlib.ExitStack() as stack:
            for name in ("check_environment", "ensure_lark_cli", "check_harnesses", "feishu_app", "start_service", "wait_ready"):
                stack.enter_context(mock.patch.object(installer.Installer, name))
            stack.enter_context(mock.patch.object(installer.Installer, "write_harness_config",
                                                  return_value=self.data / "harnesses.json"))
            self.assertEqual(0, ins.run())
        self.assertEqual(self.lines[-1], "\n完成。去飞书私聊刚建的机器人发一句话。")
        self.assertLess(self.lines.index("还差：飞书应用还缺 看板"), self.lines.index("装在这里："))
        self.assertTrue([line for line in self.lines if line.startswith("已经开着：") and "、到点回来看、" in line], self.lines)
        self.lines.clear()
        ins = self.make("--no-start")
        ins.app_id = "cli_fake"
        with contextlib.ExitStack() as stack:
            for name in ("check_environment", "ensure_lark_cli", "check_harnesses", "feishu_app"):
                stack.enter_context(mock.patch.object(installer.Installer, name))
            stack.enter_context(mock.patch.object(installer.Installer, "write_harness_config",
                                                  return_value=self.data / "harnesses.json"))
            ins.run()
        self.assertNotIn("装在这里：", self.lines)  # --no-start registers nothing.


class ServiceEnvTest(InstallerCase):
    """D4 (T647): a Harness that answered from this shell, where the shell has a variable it may log in with (an API key, a
    gateway's address), is asked once more in the service's own environment (the manifest's settings over the account's and
    systemd's); a failure names the variables, never a value, and the fix."""

    SHELL = {"ANTHROPIC_AUTH_TOKEN": "fake-token-for-the-test", "ANTHROPIC_BASE_URL": "https://gateway.example.invalid",
             "GITHUB_TOKEN": "ghp_test_not_real", "USER": "someone"}

    def ready(self, **shell):
        from covey.service.manager import register
        state = self.data / "harness-cli_fake"
        state.mkdir(parents=True, exist_ok=True)
        (self.data / "tmp").mkdir(exist_ok=True)
        register(self.data / "bridge-services", state, [sys.executable, "-B", "/absolute/release/bin/covey_bridge.py"],
                 workdir=self.root, environment={"PATH": "/absolute/service/bin", "LARK_BRIDGE_MEMORY": "1"}, health_timeout=90)
        ins = self.make(env=self.env(**shell))
        ins.app_id, ins.workdir, ins.working = "cli_fake", self.root / "work", ["claude", "codex"]
        return ins

    def test_bash_env_and_env_are_not_passed_to_the_scripts_it_runs(self):
        """bash runs the file BASH_ENV names before bootstrap_harness_bridge.sh: the PATH it sets would be registered (T818)."""
        ins = self.make(env=self.env(BASH_ENV=str(self.root / "bashrc"), ENV=str(self.root / "shrc")))
        self.assertNotIn("BASH_ENV", ins.env)
        self.assertNotIn("ENV", ins.env)
        self.assertEqual(ins.env["PATH"], self.env()["PATH"])

    def test_a_key_only_this_shell_has_is_named_with_the_fix_and_never_its_value(self):
        ins = self.ready(**self.SHELL)
        self.probes["claude"] = lambda env: (Probe(True, "ok") if env.get("ANTHROPIC_AUTH_TOKEN")
                                             else Probe(False, install_probe.NOT_LOGGED_IN, "Invalid API key"))
        ins.check_service_env()
        probed = dict(self.probed)
        for name in ("claude", "codex"):  # codex too: this shell has credential-like names the service will not have.
            self.assertNotIn("ANTHROPIC_AUTH_TOKEN", probed[name])
            self.assertNotIn("GITHUB_TOKEN", probed[name])
            self.assertEqual((probed[name]["PATH"], probed[name]["COVEY_MEMORY"], probed[name]["USER"]),
                             ("/absolute/service/bin", "1", "someone"))
        left = "\n".join(ins.leftovers)
        self.assertIn("claude 在服务里调不通（没登录）", left)
        self.assertIn("它可能靠这个终端里的 ANTHROPIC_AUTH_TOKEN、ANTHROPIC_BASE_URL，", left)  # Its own names, not GITHUB_TOKEN.
        self.assertNotIn("GITHUB_TOKEN", left)
        self.assertIn("~/.claude/settings.json 的 \"env\"", left)
        self.assertNotIn("codex 在服务里", left)
        self.assertIn("codex：服务的环境里也能调通", self.text())
        for value in self.SHELL.values():
            if value != "someone":
                self.assertNotIn(value, left + self.text())

    def test_without_such_a_variable_nothing_more_is_probed(self):
        ins = self.ready()
        ins.check_service_env()
        self.assertFalse(hasattr(self, "probed"))
        self.assertEqual(ins.leftovers, [])

    def test_registering_and_an_unchanged_service_both_check_it(self):
        for up in (False, True):  # Registered now, or running with nothing changed.
            with self.subTest(up=up):
                ins = self.ready()
                ins.settle_switches()
                config = self.data / "harnesses.json"
                (self.data / "install.json").write_text(json.dumps(ins.fingerprint(config)) if up else "{}")
                with mock.patch.object(installer.Installer, "run_logged", return_value=(0, "")) as ran, \
                        mock.patch.object(installer.Installer, "service_up", return_value=up), \
                        mock.patch.object(installer.Installer, "precompile"), \
                        mock.patch.object(installer.Installer, "check_service_env") as checked:
                    ins.start_service(config)
                checked.assert_called_once_with()
                self.assertEqual(ran.call_count, 0 if up else 2)

    def test_under_systemd_the_user_manager_s_environment_is_the_base(self):
        ins = self.ready(**self.SHELL)
        (self.data / "bridge-services" / "boot-installation.json").write_text(json.dumps({"mode": "systemd-user"}))
        shown = subprocess.CompletedProcess([], 0, "XDG_RUNTIME_DIR=/run/user/1\nMYGW_API_KEY=from-environment-d\n", "")
        with mock.patch.object(installer.subprocess, "run", return_value=shown) as run:
            env = ins.service_probe_env()
        self.assertEqual(run.call_args.args[0], ["systemctl", "--user", "show-environment"])
        self.assertEqual((env["MYGW_API_KEY"], env["PATH"], env["COVEY_MANAGED"]), ("from-environment-d", "/absolute/service/bin", "1"))
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)
        (self.data / "bridge-services" / "boot-installation.json").write_text(json.dumps({"mode": "cron"}))
        with mock.patch.object(installer.subprocess, "run") as run:
            env = ins.service_probe_env()
        run.assert_not_called()
        self.assertNotIn("MYGW_API_KEY", env)


class WithoutClaudeTest(InstallerCase):
    """D3 (T647 二波, T664): where Claude Code does not answer, a new service gets its butler on the first of Codex and
    Antigravity that does (core/butler_settings.py preset_harness, before its first start) and every switch it would have
    had anyway (专项, 整理专项: their lead and judging run on that Harness too), with --showcase too; a re-run changes
    neither the switches nor the butler's Harness the owner has; Claude Code working later is enabled by a re-run, and the
    closing lines say what the butler runs on and, on a service an older release left without 专项, how to turn it on."""

    def setUp(self):
        super().setUp()
        self.state = self.data / "harness-cli_fake"

    def registered(self, environment):
        from covey.service.manager import register
        self.state.mkdir(parents=True, exist_ok=True)
        register(self.data / "bridge-services", self.state, [sys.executable, "-B", "/absolute/release/bin/covey_bridge.py"],
                 workdir=self.root, environment=environment, health_timeout=90)

    def start(self, *argv, working=("codex",), up=False, changed=None):
        ins = self.make(*argv)
        ins.app_id, ins.workdir, ins.working = "cli_fake", self.root / "work", list(working)
        (self.data / "tmp").mkdir(parents=True, exist_ok=True)
        self.order = []

        def run_logged(name, argv, **kwargs):
            self.order.append((name, butler_settings.path(self.state).exists()))
            return 0, ""
        with mock.patch.object(installer.Installer, "run_logged", side_effect=run_logged), \
                mock.patch.object(installer.Installer, "service_up", return_value=up), \
                mock.patch.object(installer.Installer, "precompile"), \
                mock.patch.object(installer, "change_settings", return_value={"changed": changed or {}}) as self.changed:
            ins.start_service(self.data / "harnesses.json")
        return ins

    def switches_of(self, ins):
        return {key: cenv.get(key, None, ins.env) for key in switches.SHOWCASE}

    def test_a_new_service_without_claude_code_gets_its_butler_on_the_first_that_works_and_every_switch(self):
        for working, butler, title in ((("codex",), "codex", "Codex"), (("agy",), "agy", "Antigravity"),
                                       (("agy", "codex"), "codex", "Codex")):
            with self.subTest(working=working):
                shutil.rmtree(self.data, ignore_errors=True)
                self.lines.clear()
                ins = self.start(working=working)
                self.assertEqual(self.switches_of(ins), {key: switches.INSTALL.get(key) for key in switches.SHOWCASE})
                self.assertEqual(json.loads(butler_settings.path(self.state).read_text()), {"harness": butler})
                self.assertEqual(self.order, [("service-install", False), ("bootstrap", True)])  # Before the first start.
                self.assertIn(f"Claude Code 没调通：管家用 {title}；专项、整理专项照常开，负责人和小模型判断也用 {title}",
                              self.text())
                self.assertEqual(butler_settings.load(self.state).butler_harness, butler)

    def test_with_claude_code_working_nothing_changes(self):
        for working in (("claude", "codex"), ("claude",), ("claude", "agy")):
            with self.subTest(working=working):
                shutil.rmtree(self.data, ignore_errors=True)
                self.lines.clear()
                ins = self.start(working=working)
                self.assertEqual(self.switches_of(ins), {key: switches.INSTALL.get(key) for key in switches.SHOWCASE})
                self.assertFalse(butler_settings.path(self.state).exists())
                self.assertNotIn("Claude Code 没调通", self.text())

    def test_installer_preset_is_the_first_of_codex_and_antigravity_that_answered_without_claude_code(self):
        for working, want in ((("codex",), "codex"), (("agy",), "agy"), (("codex", "agy"), "codex"),
                              (("claude", "codex"), ""), (("claude",), ""), ((), ""), (("dsh",), "")):
            with self.subTest(working=working):
                self.assertEqual(installer.butler_preset(working), want)

    def test_without_claude_code_showcase_turns_on_the_whole_demo_set(self):
        ins = self.start("--showcase")
        self.assertEqual(self.switches_of(ins), dict(switches.SHOWCASE))
        self.registered({"LARK_BRIDGE_REVISIT": "1", "LARK_BRIDGE_PROJECTS": "0"})
        (self.data / "install.json").write_text("{}")
        ins = self.start("--showcase")  # Registered, down: registers again with the demo set; 专项, set off, stays off.
        self.assertEqual((cenv.get("COVEY_PROJECTS", None, ins.env), cenv.get("COVEY_PROJECT_TIDY", None, ins.env),
                          cenv.get("COVEY_COLLAB", None, ins.env)), ("0", None, "1"))
        fingerprint = ins.fingerprint(self.data / "harnesses.json")
        (self.data / "install.json").write_text(json.dumps(fingerprint))
        self.registered({"LARK_BRIDGE_REVISIT": "1", "LARK_BRIDGE_PROJECTS": "0"})
        ins = self.make("--showcase")
        ins.app_id, ins.workdir, ins.working = "cli_fake", self.root / "work", ["codex"]
        with mock.patch.object(installer, "change_settings", return_value={"changed": {}}) as changed:
            ins.showcase_running()  # Running unchanged: the preset in its manifest, nothing kept back.
        changed.assert_called_once_with(self.data / "bridge-services", self.state, {}, preset="showcase")

    def test_a_rerun_keeps_the_switches_and_the_butler_s_harness_the_owner_has(self):
        self.registered({"LARK_BRIDGE_PROJECTS": "1", "LARK_BRIDGE_MEMORY": "0"})
        butler_settings.path(self.state).write_text(json.dumps({"mode": "agent", "harness": "claude"}))
        (self.data / "install.json").write_text("{}")
        ins = self.start()  # Only Codex now, registered already: registers again from what it has.
        self.assertEqual((cenv.get("COVEY_PROJECTS", None, ins.env), cenv.get("COVEY_MEMORY", None, ins.env),
                          cenv.get("COVEY_REVISIT", None, ins.env)), ("1", "0", None))
        self.assertEqual(json.loads(butler_settings.path(self.state).read_text()), {"mode": "agent", "harness": "claude"})
        self.assertNotIn("管家用 Codex", self.text())
        self.assertTrue(butler_settings.preset_harness(self.state, "codex") is False)  # A Harness chosen stays.

    def test_claude_code_working_later_is_enabled_by_a_rerun_but_one_taken_out_stays_out(self):
        path, record = self.data / "harnesses.json", self.data / installer.USABLE_RECORD
        self.data.mkdir(parents=True, exist_ok=True)
        # (what the last run that wrote harnesses.json recorded, the last registration's install.json, enabled, wanted)
        for usable, last, enabled, want in ((None, ["codex"], ["codex"], ["codex", "claude"]),
                                            (None, ["claude", "codex"], ["codex"], ["codex"]),
                                            (None, None, ["codex"], ["codex"]),  # A file the owner wrote by hand.
                                            # T766: the last run stopped before its registration wrote install.json.
                                            (["codex"], None, ["codex"], ["codex", "claude"]),
                                            (["codex"], ["claude", "codex"], ["codex"], ["codex", "claude"]),
                                            (["claude", "codex"], ["codex"], ["codex"], ["codex"])):
            with self.subTest(usable=usable, last=last):
                self.lines.clear()
                path.write_text(json.dumps({"enabled_harnesses": enabled, "spaces": {"notify_space": "oc_x"}}))
                (self.data / "install.json").write_text(json.dumps({"harnesses": last}) if last is not None else "{}")
                record.unlink(missing_ok=True)
                if usable is not None:
                    record.write_text(json.dumps({"usable": usable}))
                ins = self.make()
                ins.working = ["claude", "codex"]
                ins.write_harness_config()
                config = json.loads(path.read_text())
                self.assertEqual((config["enabled_harnesses"], config["spaces"]), (want, {"notify_space": "oc_x"}))
                self.assertEqual("claude：这次能用了，加进启用的 Harness" in self.text(), want != enabled)
                self.assertEqual(json.loads(record.read_text()), {"usable": ["claude", "codex"]})

    def test_a_first_run_that_stops_before_the_registration_still_lets_a_rerun_add_claude_code(self):
        # T766: harnesses.json was written with Codex only, the registration never ran (no install.json); Claude Code
        # logged in since, the re-run adds it, as the closing lines promised.
        (self.fake / "phone").touch()
        self.lacking.clear()
        self.probes["claude"] = Probe(False, install_probe.NOT_LOGGED_IN, "Not logged in")
        self.assertEqual(0, self.make("--no-start").run())
        path = self.data / "harnesses.json"
        self.assertEqual(["codex"], json.loads(path.read_text())["enabled_harnesses"])
        self.assertFalse((self.data / "install.json").exists())
        self.probes["claude"] = Probe(True, "ok")
        (self.fake / "authed").touch()
        self.assertEqual(0, self.make("--no-start").run())
        self.assertEqual(["codex", "claude"], json.loads(path.read_text())["enabled_harnesses"])

    def closing(self, working, environment, butler=None, reasons=None):
        self.registered(environment)
        if butler:
            butler_settings.path(self.state).write_text(json.dumps({"mode": "agent", "harness": butler}))
        else:
            butler_settings.path(self.state).unlink(missing_ok=True)
        ins = self.make()
        ins.app_id, ins.workdir, ins.working = "cli_fake", self.root / "work", list(working)
        ins.reasons = dict(reasons or {})
        with mock.patch.object(Path, "cwd", return_value=installer.REPO_ROOT):
            return "\n".join(ins.closing_lines())

    def test_the_closing_lines_say_what_the_butler_runs_on_and_how_to_turn_projects_on_where_they_are_off(self):
        installed = {cenv.spellings(key)[-1]: value for key, value in switches.INSTALL.items()}
        state = self.state
        turn_on = " ".join(f"{key}={switches.DEFAULTS[key]}" for key in installer.PROJECT_SWITCHES)
        command = f"python3 -B bin/covey_service.py set --state-dir {state} {turn_on}"
        text = self.closing(("codex",), installed, butler="codex")
        for words in ("已经开着：管家（Codex）、专项（", "Claude Code 没调通：管家用 Codex，专项的负责人和整理专项、同时做几件事的"
                      "小模型判断也用它", "装好并登录 Claude Code 以后：重新运行 ./install（它会把 Claude Code 加进启用的 Harness）；"
                      "想让管家用它，在「设置」→「配置」的管家一段换成 Claude Code", "--preset showcase"):
            self.assertIn(words, text)
        self.assertNotIn("（不含专项）", text)
        self.assertNotIn(command, text)  # 专项 is on: nothing to turn on.
        self.assertNotIn("Claude Code 这次：", text)
        text = self.closing(("codex",), installed, butler="codex", reasons={"claude": "没装（PATH 里没有 claude 命令）"})
        self.assertIn("小模型判断也用它\n    Claude Code 这次：没装（PATH 里没有 claude 命令）\n装好并登录 Claude Code 以后", text)
        text = self.closing(("agy",), installed)  # No Harness named: the butler follows what works, Antigravity here.
        self.assertIn("已经开着：管家（Antigravity）", text)
        self.assertIn("Claude Code 没调通：管家用 Antigravity", text)
        older = {**installed, **{cenv.spellings(name)[-1]: "0" for name in installer.PROJECT_SWITCHES}}
        text = self.closing(("codex",), older, butler="codex")  # An older release installed it with Codex only: set off.
        self.assertIn(f"；专项和整理专项没开，要开：{command}", text)
        self.assertNotIn("专项（", text.split("Claude Code 没调通")[0])
        text = self.closing(("claude", "codex"), older, butler="codex")  # Claude Code installed since.
        self.assertIn("Claude Code 能用了：管家用的是 Codex，想换在飞书「设置」→「配置」的管家一段改", text)
        self.assertIn(command, text)
        self.assertIn("--preset showcase", text)
        text = self.closing(("claude", "codex"), installed)  # Claude Code's own butler: nothing said.
        self.assertNotIn("Claude Code 能用了", text)
        self.assertNotIn("Claude Code 没调通", text)
        self.assertIn("已经开着：管家、专项（", text)

    def test_the_project_switches_and_the_butler_file(self):
        self.assertEqual(set(installer.PROJECT_SWITCHES) - set(switches.DEFAULTS), set())
        self.assertFalse(hasattr(switches, "NEEDS_CLAUDE") or hasattr(switches, "without_claude"))
        self.state.mkdir(parents=True)
        butler_settings.path(self.state).write_text(json.dumps({"model": "gpt-x", "topic_defaults": {"run_mode": "direct"}}))
        self.assertTrue(butler_settings.preset_harness(self.state, "codex"))
        self.assertEqual(json.loads(butler_settings.path(self.state).read_text()),
                         {"model": "gpt-x", "topic_defaults": {"run_mode": "direct"}, "harness": "codex"})
        self.assertFalse(butler_settings.preset_harness(self.state, "agy"))
        self.assertEqual(butler_settings.load(self.state).butler_harness, "codex")


class FakeBuild:
    """The page's build process: ends with ``code`` at once, or never (``code`` None) until it is stopped."""
    pid = 4242424

    def __init__(self, code):
        self.code = code

    def poll(self):
        return self.code

    def wait(self, timeout=None):
        return self.code


class WebTest(InstallerCase):
    """T653: the self-hosted page in ./install: chosen by a flag or one question, built in the background, prepared once
    the build is done; nothing happens for a run that does not choose it. Node, the build and prepare are all fakes."""

    def setUp(self):
        super().setUp()
        self.spawned, self.prepared, self.signalled = [], [], []
        self.build_code = 0
        self.suggestion = ""
        self.prepare_error = None
        for name, patch in (("spawn_web_build", self.spawn), ("signal_group", self.signal)):
            patcher = mock.patch.object(installer.Installer, name, autospec=True, side_effect=patch)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.prerequisite_patch = mock.patch.object(installer.Installer, "web_prerequisite", autospec=True)
        self.prerequisite = self.prerequisite_patch.start()
        self.prerequisite.return_value = ""
        self.addCleanup(mock.patch.stopall)

    def spawn(self, ins, argv, log):
        self.spawned.append(argv)
        Path(log).write_text("npm 日志\nbuild 日志 第二行\n")
        return FakeBuild(self.build_code)

    def signal(self, ins, proc, sig):
        self.signalled.append(sig)

    def prepare(self, data, **kwargs):
        if self.prepare_error:
            raise self.prepare_error
        self.prepared.append((data, kwargs))
        names = kwargs.get("public") or []
        result = {"page_url": f"https://{names[0]}:18443/" if names else "http://127.0.0.1:18650/", "config": "x",
                  "switches": ["COVEY_WEB=1", "COVEY_WEB_CONFIG=/absolute/web.json", "COVEY_WEB_SELFHOST=1",
                               f"COVEY_WEB_SELFHOST_DIR={kwargs['folder']}"] + ([f"COVEY_WEB_SELFHOST_PUBLIC={','.join(names)}"] if names else []),
                  "public": {"names": names} if names else None, "open_ports": [18443] if names else [],
                  "fingerprint": "AB:CD:EF" if names else ""}
        return result

    def web(self, *argv, interactive=False, answers=(), env=None):
        replies = list(answers)
        ins = self.make(*argv, interactive=interactive, env=env, ask=lambda prompt: (self.asked.append(prompt), replies.pop(0) if replies else "")[1])
        ins.web_prepare, ins.web_suggest, ins.web_poll = self.prepare, lambda: self.suggestion, 0.01
        (self.data / "tmp").mkdir(parents=True, exist_ok=True)
        return ins

    def folder(self):
        return self.data / "web" / "app"

    def switch_names(self, ins):
        return {key for key in ins.env if "WEB" in key}

    def test_the_page_config_names_the_port_this_run_sets(self):
        ins = self.web("--web", env=self.env(COVEY_WEB_SELFHOST_PORT="18651"))
        ins.choose_web()
        ins.finish_web()
        self.assertEqual(18651, self.prepared[0][1]["port"])  # The Manager starts the page there: web.json must say so too.

    def test_a_terminal_answer_of_yes_builds_in_the_background_and_prepares_the_page_for_the_manifest(self):
        ins = self.web(interactive=True, answers=["y"])
        ins.choose_web()
        self.assertEqual(1, len(self.asked))
        self.assertIn("网页工作台", self.asked[0])
        self.assertEqual(["bash", str(installer.REPO_ROOT / "web/deploy/selfhost/build.sh"), str(self.folder())], self.spawned[0])
        self.assertEqual([], self.prepared)  # Not before the registration step waits for the build.
        ins.finish_web()
        self.assertEqual([(self.data, {"folder": str(self.folder() / "dist-selfhost"), "port": web_selfhost.DEFAULT_PORT})],
                         self.prepared)
        self.assertEqual("1", cenv.get("COVEY_WEB_SELFHOST", None, ins.env))
        self.assertEqual(str(self.folder() / "dist-selfhost"), cenv.get("COVEY_WEB_SELFHOST_DIR", None, ins.env))
        self.assertEqual([], ins.leftovers)
        self.assertIn("web-build.log", self.text())

    def test_the_flag_builds_without_a_question_and_public_asks_for_the_address(self):
        ins = self.web("--web-public", "203.0.113.7")
        ins.choose_web()
        ins.finish_web()
        self.assertEqual([], self.asked)
        self.assertEqual(["203.0.113.7"], self.prepared[0][1]["public"])
        self.assertEqual("203.0.113.7", cenv.get("COVEY_WEB_SELFHOST_PUBLIC", None, ins.env))
        self.assertIn("--web-public", installer.parser().format_help())

    def test_a_terminal_that_says_yes_is_offered_a_suggested_public_address_once_with_no_as_the_default(self):
        self.suggestion = "203.0.113.9"
        ins = self.web(interactive=True, answers=["y", "y"])
        ins.choose_web()
        self.assertEqual(2, len(self.asked))
        ins.finish_web()
        self.assertEqual(["203.0.113.9"], self.prepared[0][1]["public"])
        self.asked.clear()
        self.prepared.clear()
        ins = self.web(interactive=True, answers=["y", ""])
        ins.choose_web()
        ins.finish_web()
        self.assertNotIn("public", self.prepared[0][1])

    def test_auto_takes_the_suggestion_without_a_terminal_and_says_so_when_there_is_none(self):
        self.suggestion = "203.0.113.9"
        ins = self.web("--web-public", "auto")
        ins.choose_web()
        ins.finish_web()
        self.assertEqual(["203.0.113.9"], self.prepared[0][1]["public"])
        self.suggestion, self.prepared[:] = "", []
        ins = self.web("--web-public", "auto")
        ins.choose_web()
        ins.finish_web()
        self.assertNotIn("public", self.prepared[0][1])
        self.assertIn("没找到这台机器的公网地址", "\n".join(ins.leftovers))

    def test_a_run_that_does_not_choose_the_page_does_nothing_for_it(self):
        ins = self.web(interactive=True, answers=[""])  # The question's default is no.
        ins.choose_web()
        ins.finish_web()
        self.assertEqual(([], [], set(), []), (self.spawned, self.prepared, self.switch_names(ins), ins.leftovers))
        self.prerequisite.reset_mock()
        ins = self.web()  # Without a terminal and without a flag: not even a check of Node.
        ins.choose_web()
        ins.finish_web()
        self.assertEqual(([], [], set(), [], 1), (self.spawned, self.prepared, self.switch_names(ins), ins.leftovers, len(self.asked)))
        self.prerequisite.assert_not_called()

    def test_without_node_22_and_npm_nothing_is_asked_and_the_flag_says_it_among_the_leftovers(self):
        self.prerequisite.return_value = "Node 版本太低（v20.1.0，要 22 以上）"
        ins = self.web(interactive=True)
        ins.choose_web()
        self.assertEqual([], self.asked)
        self.assertEqual([], ins.leftovers)
        ins = self.web("--web")
        ins.choose_web()
        ins.finish_web()
        self.assertEqual([], self.spawned)
        self.assertEqual(set(), self.switch_names(ins))
        text = "\n".join(ins.leftovers)
        self.assertIn("Node 版本太低", text)
        self.assertIn(f"bash {ins.path_of('web/deploy/selfhost/build.sh')} {self.folder()} && python3 -B -m covey.service.web_selfhost", text)

    def test_the_real_check_reads_node_s_version_and_npm(self):
        self.prerequisite_patch.stop()
        ins = self.make()
        for name, body, why in (("node", "#!/bin/bash\necho v22.3.0\n", "没有 npm"), ("npm", "#!/bin/bash\n", "")):
            path = self.bin / name
            path.write_text(body)
            path.chmod(path.stat().st_mode | stat.S_IEXEC)
            self.assertEqual(why, ins.web_prerequisite())
        (self.bin / "node").write_text("#!/bin/bash\necho v20.9.0\n")
        self.assertIn("版本太低（v20.9.0", ins.web_prerequisite())
        (self.bin / "node").unlink()
        self.assertEqual("没有 node", ins.web_prerequisite())

    def test_a_failed_build_or_prepare_is_only_said_and_the_service_is_still_registered(self):
        self.build_code = 1
        ins = self.web("--web")
        ins.choose_web()
        ins.finish_web()
        text = "\n".join(ins.leftovers)
        self.assertIn("没构建成功", text)
        self.assertIn("build 日志 第二行", text)
        self.assertIn("build.sh", text)
        self.assertEqual(set(), self.switch_names(ins))
        self.assertEqual([], self.prepared)
        self.build_code, self.prepare_error = 0, ValueError("密钥文件写不了")
        ins = self.web("--web")
        ins.choose_web()
        ins.finish_web()
        self.assertIn("没能备好配置（密钥文件写不了）", "\n".join(ins.leftovers))
        self.assertEqual(set(), self.switch_names(ins))
        ran = self.register(ins)
        self.assertEqual(["service-install", "bootstrap"], [n for n, _ in ran])

    def register(self, ins):
        ins.app_id, ins.workdir, ins.working = "cli_fake", self.root / "work", ["claude"]
        ran = []
        with (mock.patch.object(installer.Installer, "run_logged", side_effect=lambda name, argv, **kw: (ran.append((name, argv)), (0, ""))[1]),
              mock.patch.object(installer.Installer, "service_up", return_value=False),
              mock.patch.object(installer.Installer, "precompile")):
            ins.start_service(self.data / "harnesses.json")
        return ran

    def test_the_switches_are_in_the_environment_the_registration_runs_with(self):
        ins = self.web("--web")
        ins.choose_web()
        ran = self.register(ins)  # start_service waits for the build, then registers.
        self.assertEqual(1, len(self.prepared))
        self.assertEqual("1", cenv.get("COVEY_WEB_SELFHOST", None, ins.env))
        self.assertEqual(["service-install", "bootstrap"], [n for n, _ in ran])
        self.assertIn("1", [v for k, v in ins.fingerprint(self.data / "harnesses.json")["settings"].items() if k.endswith("WEB_SELFHOST")])

    def test_a_build_that_does_not_end_is_stopped_after_the_time_and_one_that_is_interrupted_is_stopped_as_a_group(self):
        import signal
        self.build_code = None
        ins = self.web("--web")
        ins.web_seconds = 0
        ins.choose_web()
        ins.finish_web()
        self.assertEqual([signal.SIGTERM], self.signalled)
        self.assertIn("构建超过", "\n".join(ins.leftovers))
        self.assertEqual([], self.prepared)
        self.signalled.clear()
        ins = self.web("--web")
        with mock.patch.object(installer.Installer, "check_environment"), \
                mock.patch.object(installer.Installer, "ensure_lark_cli", side_effect=KeyboardInterrupt):
            ins.choose_web()  # In run() it comes after check_harnesses; here the build starts, then the run is interrupted.
            self.assertEqual(130, ins.run())
        self.assertEqual([signal.SIGTERM], self.signalled)

    def test_a_rerun_keeps_a_page_the_service_has_on_and_asks_nothing_and_a_web_rerun_upgrades_it(self):
        from covey.service.manager import register
        (self.fake / "profile").touch()
        state = self.data / "harness-cli_fake"
        state.mkdir(parents=True)
        register(self.data / "bridge-services", state, [sys.executable, "-B", "/absolute/release/bin/covey_bridge.py"],
                 workdir=self.root, environment={"LARK_BRIDGE_WEB_SELFHOST": "1", "LARK_BRIDGE_WEB": "1"}, health_timeout=90)
        ins = self.web(interactive=True)
        ins.choose_web()
        self.assertEqual([], self.asked + self.spawned)
        ins = self.web("--web")
        (self.folder()).mkdir(parents=True)
        (self.folder() / installer.WEB_BUILD_MARK).touch()
        ins.choose_web()
        self.assertEqual("--restart", self.spawned[0][-1])
        ins = self.web("--web")  # No build there yet: no --restart.
        shutil.rmtree(self.folder())
        ins.choose_web()
        self.assertNotIn("--restart", self.spawned[1])

    def test_a_service_registered_without_the_page_and_a_run_that_does_not_choose_it_stays_without(self):
        from covey.service.manager import register
        (self.fake / "profile").touch()
        state = self.data / "harness-cli_fake"
        state.mkdir(parents=True)
        register(self.data / "bridge-services", state, [sys.executable, "-B", "/absolute/release/bin/covey_bridge.py"],
                 workdir=self.root, environment={"LARK_BRIDGE_REVISIT": "1"}, health_timeout=90)
        ins = self.web(interactive=False)
        ins.choose_web()
        ins.settle_switches()
        self.assertEqual([], self.spawned)
        self.assertEqual(set(), self.switch_names(ins))

    def test_the_closing_lines_name_the_page_its_open_port_and_certificate_or_offer_it(self):
        from covey.service.manager import register
        state = self.data / "harness-cli_fake"
        state.mkdir(parents=True)

        def closing(environment, result=None):
            register(self.data / "bridge-services", state, [sys.executable, "-B", "/absolute/release/bin/covey_bridge.py"],
                     workdir=self.root, environment=environment, health_timeout=90)
            ins = self.make()
            ins.app_id, ins.workdir, ins.working, ins.web_result = "cli_fake", self.root / "work", ["claude"], result
            with mock.patch.object(Path, "cwd", return_value=installer.REPO_ROOT):
                return "\n".join(ins.closing_lines())

        text = closing({"LARK_BRIDGE_REVISIT": "1"})
        self.assertIn("./install --web", text)
        self.assertNotIn("网页工作台：", text)
        text = closing({"LARK_BRIDGE_WEB_SELFHOST": "1"}, {"page_url": "https://203.0.113.7:18443/", "open_ports": [18443],
                                                          "fingerprint": "AB:CD:EF", "public": {"names": ["203.0.113.7"]}})
        for words in ("网页工作台：https://203.0.113.7:18443/", "TCP 端口 18443", "指纹 AB:CD:EF", "点「允许」"):
            self.assertIn(words, text)
        self.assertRegex(text, "已经开着：.*网页工作台")
        self.assertNotIn("./install --web", text)
        text = closing({"LARK_BRIDGE_WEB_SELFHOST": "1"})  # Kept from an earlier run: no result, the default local address.
        self.assertIn("网页工作台：http://127.0.0.1:18650/", text)
        self.assertNotIn("防火墙", text)

    def test_the_help_names_antigravity_and_the_web_options(self):
        self.assertIn("Claude Code、Codex 或 Antigravity", installer.parser().description)
        args = installer.parser().parse_args(["--web-public", "auto"])
        self.assertEqual("auto", args.web_public)
        self.assertFalse(args.web)


class EntryTest(unittest.TestCase):
    def test_the_shell_entry_prints_the_options(self):
        done = subprocess.run([str(REPO / "install"), "--help"], capture_output=True, text=True, timeout=60)
        self.assertEqual(0, done.returncode, done.stderr)
        for flag in ("--reconfigure", "--skills", "--non-interactive", "--data-home", "--workdir", "--keep-login",
                     "--browser", "--no-browser", "--web", "--web-public"):
            self.assertIn(flag, done.stdout)


if __name__ == "__main__":
    unittest.main()
