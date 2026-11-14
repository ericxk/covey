"""./install 的安装器 (service/installer.py, service/install_probe.py, core/qr.py, feishu/install_browser.py 的命令约定):
QR 编码对照 lark-cli 自己画的真实二维码；Harness 探测的分类；步骤编排、默认值、幂等、代理写入、浏览器登录态的清理、
没有浏览器时的退路、扩展钩子、服务注册的参数，全部用假 lark-cli 和假浏览器驱动，不联网。"""
from __future__ import annotations

import io
import json
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import REPO, exec_tempdir

from covey.core import app_manifest, qr
from covey.service import install_probe, installer
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
     for i in $(seq 100); do [ -f "$d/clicked-create" ] && break; sleep 0.05; done
     [ -f "$d/clicked-create" ] || { [ -f "$d/phone" ] || exit 1; }
     touch "$d/profile"; echo "OK: 应用配置成功" ;;
  *"auth status"*) [ -f "$d/authed" ] || exit 1 ;;
  *"auth login"*) echo "  https://accounts.feishu.cn/oauth/v1/device/verify?user_code=CCCC-DDDD"
     for i in $(seq 100); do [ -f "$d/clicked-auth" ] && break; sleep 0.05; done
     [ -f "$d/clicked-auth" ] || { [ -f "$d/phone" ] || exit 1; }
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

    def env(self, **extra):
        return {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.root), "FAKE_DIR": str(self.fake),
                "COVEY_PLAYWRIGHT_PYTHON": sys.executable, "COVEY_EXTENSIONS": "", **extra}

    def survey(self, profile, app_id):
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
                                  app_id_of=lambda profile: "cli_fake" if (self.fake / "profile").exists() else "")
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
    def test_one_run_creates_authorizes_configures_and_leaves_no_login(self):
        self.probes["claude"] = Probe(False, install_probe.NOT_LOGGED_IN, "Not logged in")
        self.lacking = [app_manifest.Need("callback", "card.action.trigger", "卡片按钮")]
        ins = self.make("--no-start")
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
        self.make("--no-start").run()
        (self.fake / "authed").touch()
        FakeBrowser.opened.clear()
        self.assertEqual(0, self.make("--no-start", "--reconfigure").run())
        self.assertEqual(1, len(FakeBrowser.opened))
        self.assertTrue(any("profile remove" in c for c in self.calls()))

    def test_keep_login_leaves_the_browser_folder(self):
        self.lacking.clear()
        self.assertEqual(0, self.make("--no-start", "--keep-login").run())
        self.assertTrue(FakeBrowser.opened[0].home.exists())
        self.assertIn("--keep-login", self.text())

    def test_no_working_harness_stops_before_anything_is_created(self):
        self.probes = {n: Probe(False, install_probe.NOT_LOGGED_IN, "x") for n in ("claude", "codex")}
        self.assertEqual(1, self.make("--no-start").run())
        self.assertEqual([], FakeBrowser.opened)
        self.assertNotIn("config init", "\n".join(self.calls()))
        self.assertIn("没登录", self.text())

    def test_a_missing_cli_is_named_not_probed(self):
        (self.bin / "claude").unlink()
        self.lacking.clear()
        self.make("--no-start").run()
        self.assertEqual(["codex"], [n for n, _ in self.probed])
        self.assertNotIn("claude：能调通", self.text())

    def test_defaults_need_no_questions(self):
        self.lacking.clear()
        self.make("--no-start").run()
        self.assertEqual([], self.asked)
        self.assertTrue((self.data / "tmp").is_dir() and (self.data / "browsers").is_dir())

    def test_the_default_workdir_in_the_repo_is_in_the_data_root(self):
        args = installer.parser().parse_args(["--data-home", str(self.data)])
        ins = installer.Installer(args, env=self.env(), say=self.lines.append, interactive=False)
        with mock.patch.object(Path, "cwd", return_value=REPO):
            ins.check_environment()
        self.assertEqual(self.data / "workspace", ins.workdir)


class ProxyTest(InstallerCase):
    def network_until_proxy(self, env):
        return Probe(True, "ok") if env.get("HTTPS_PROXY") else Probe(False, install_probe.NETWORK, "403")

    def test_a_network_failure_asks_for_a_proxy_and_the_service_gets_it(self):
        self.probes["codex"] = self.network_until_proxy
        self.lacking.clear()
        ins = self.make("--no-start", interactive=True, ask=lambda prompt: "http://127.0.0.1:7890")
        self.assertEqual(0, ins.run())
        self.assertEqual("http://127.0.0.1:7890", ins.env["HTTP_PROXY"])
        self.assertIn(".feishu.cn", ins.env["NO_PROXY"])
        self.assertEqual(2, len([p for p in self.probed if p[0] == "codex"]))
        self.assertIn("代理设置会一起写进服务", self.text())

    def test_non_interactive_says_how_to_set_one_and_enables_the_rest(self):
        self.probes["codex"] = self.network_until_proxy
        self.lacking.clear()
        ins = self.make("--no-start", "--non-interactive")
        self.assertEqual(0, ins.run())
        self.assertEqual([], self.asked)
        self.assertIn("HTTPS_PROXY", self.text())
        self.assertEqual(["claude"], json.loads((self.data / "harnesses.json").read_text())["enabled_harnesses"])

    def test_a_terminal_proxy_is_kept_with_the_feishu_domains_direct(self):
        self.lacking.clear()
        ins = self.make("--no-start", env=self.env(HTTPS_PROXY="http://proxy.example.com:3128", NO_PROXY="corp.local"))
        self.assertEqual(0, ins.run())
        self.assertEqual("corp.local", ins.env["NO_PROXY"].split(",")[0])
        self.assertIn(".larkoffice.com", ins.env["NO_PROXY"])  # privacy: allow 飞书自己的域名

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
        self.lacking.clear()
        self.app_name = "胥可的飞书 CLI"  # The create page's own proposal, kept by the user.
        self.assertEqual(0, self.make("--no-start").run())
        self.assertIn("去飞书私聊机器人「胥可的飞书 CLI」发一句话", self.text())
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
        ins = self.make(env=env)
        ins.prepare_browser = prepare
        self.assertIsNone(ins.ensure_browser())
        self.assertIn("浏览器用不了：Chromium 起不来：缺系统库", self.text())
        self.assertIn("改用手机扫码", self.text())


class ExtensionHookTest(InstallerCase):
    def test_hooks_see_the_old_setup_script_helpers_and_may_add_harnesses(self):
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
    def start(self, up=False, install_code=0, *argv):
        ins = self.make(*argv)
        ins.app_id, ins.workdir, ins.working = "cli_fake", self.root / "work", ["codex"]
        self.data.mkdir(exist_ok=True)
        (self.data / "tmp").mkdir(exist_ok=True)
        ran = []

        def run_logged(name, argv, **kwargs):
            ran.append((name, argv))
            return (install_code if name == "service-install" else 0), "launchd (KeepAlive)"

        patches = (mock.patch.object(installer.Installer, "run_logged", side_effect=run_logged),
                   mock.patch.object(installer.Installer, "service_up", return_value=up))
        with patches[0], patches[1]:
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

    def test_boot_registration_failing_is_said_not_fatal(self):
        ins, _ = self.start(install_code=2)
        self.assertTrue(any("开机自启没注册上" in x for x in ins.leftovers))


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


class EntryTest(unittest.TestCase):
    def test_the_shell_entry_prints_the_options(self):
        done = subprocess.run([str(REPO / "install"), "--help"], capture_output=True, text=True, timeout=60)
        self.assertEqual(0, done.returncode, done.stderr)
        for flag in ("--reconfigure", "--skills", "--non-interactive", "--data-home", "--workdir", "--keep-login",
                     "--no-browser"):
            self.assertIn(flag, done.stdout)


if __name__ == "__main__":
    unittest.main()
