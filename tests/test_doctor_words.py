"""covey_doctor.py's short report in Chinese (service/doctor_words.py, D8 of T647): the conclusion, one row a part, each
fault or warning with its next step naming the real state directory; and the entry: a terminal gets the summary, a pipe
and a script the JSON as before, --json and --summary force either; the exit code is the same for both."""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from covey.core.process import service_key
from covey.service import doctor, doctor_words

STATE = "/absolute/data/harness-cli_x"


def report(**service):
    """A report as diagnose makes it: the Manager answering, one running service, hosting by systemd, Claude probed fine."""
    row = {"id": "k1", "desired_state": "running", "switches": [["COVEY_PROJECTS", "1"]], "status": "ready", "alive": True,
           "bridge": {"status": "ready", "remote_connection": {"status": "connected"},
                      "consumers": {"im.message.receive_v1": {"ready": True}, "card.action.trigger": {"ready": True}},
                      "workers": {"jobs": True}},
           "generation": 7, **service}
    return {"schema_version": 1, "status": "ready", "runtime_status": "ready", "manager": {"lag": 0.4},
            "boot": {"status": "ready", "mode": "systemd-user"}, "services": [row], "model_access": "not_probed",
            "harness_probe": {"claude": {"ok": True, "reason": "ok", "checked_at": 0}}, "events_checked": True}


def said(found, **kwargs):
    return "\n".join(doctor_words.lines(found, root="/absolute/data/bridge-services", states={"k1": STATE}, **kwargs))


class SummaryTest(unittest.TestCase):
    def test_a_ready_service_reads_all_fine_and_says_where_the_full_report_is(self):
        text = said(report())
        lines = text.splitlines()
        self.assertEqual(lines[:2], ["Covey 诊断（服务根 /absolute/data/bridge-services）", "结论：都正常"])
        for words in ("✓ 服务管理器在运行（巡检延迟 0.4 秒）", "✓ 机器人 harness-cli_x 正常：第 7 代，收消息和卡片点击都连着",
                      "· 设了 1 个可选开关：COVEY_PROJECTS=1", "✓ 开机会自动拉起（systemd 用户服务）", "✓ Claude Code 能调通",
                      "完整报告（JSON）：python3 -B bin/covey_doctor.py --json"):
            self.assertIn(words, text)
        self.assertNotIn("下一步", text.split("· 上面是上次测的结果")[0])

    def test_the_todo_lines_are_listed_under_one_heading_and_only_when_there_are_some(self):
        none = said(report(todo=[]))
        self.assertNotIn("要你补的", none)
        self.assertEqual(said(report()), none)
        found = said(report(todo=["事件通道是 lark-cli 消费进程；新装默认是飞书 SDK，切换办法见 docs/configuration.md「飞书 SDK 与直连」",
                                  "飞书应用还缺 1 项：权限 im:message（撤回消息）"]))
        self.assertIn("结论：都正常", found)  # only said: no fault, no count
        self.assertIn("要你补的：\n  - 事件通道是 lark-cli 消费进程", found)
        self.assertIn("  - 飞书应用还缺 1 项：权限 im:message（撤回消息）", found)
        self.assertLess(found.index("要你补的"), found.index("完整报告（JSON）"))

    def test_each_fault_says_what_is_wrong_and_the_next_step_with_the_real_state_directory(self):
        found = report(status="degraded", alive=False, bridge=None)
        found.update(status="degraded", runtime_status="degraded")
        text = said(found)
        self.assertIn("结论：有 1 项要看", text)
        self.assertIn("✗ 机器人 harness-cli_x 有问题：第 7 代，进程不在", text)
        self.assertIn(f"    下一步：看日志最后几行：tail -n 50 {STATE}/bridge.log；一直不好就 python3 -B bin/covey_service.py "
                      f"restart --state-dir {STATE}", text)
        bridge = {"status": "degraded", "remote_connection": {"status": "stalled"},
                  "consumers": {"im.message.receive_v1": {"ready": True}, "card.action.trigger": {"ready": False}},
                  "workers": {"jobs": True, "digest": False}}
        text = said(report(status="degraded", bridge=bridge, events={"owned_required_consumers": False, "dropped": 3}))
        self.assertIn("和飞书的连接卡住了；收卡片点击的连接没就绪；后台部件停了：digest；丢了 3 个事件", text)
        self.assertNotIn("收消息或卡片点击的连接没就绪", text)  # Said once, from the Bridge's own consumers.
        text = said(report(status="degraded", permission_env=["LARK_BRIDGE_CODEX_SANDBOX=workspace-write is no longer read"]))
        self.assertIn("清单里留着已不生效的权限变量", text)
        self.assertIn("docs/troubleshooting.md「权限变量让诊断或部署失败」", text)
        text = said(report(status="degraded"))  # Up and ready, the new generation not taken over yet.
        self.assertIn("新的一代还没接上", text)
        self.assertIn("半分钟后再运行一次 python3 -B bin/covey_doctor.py", text)

    def test_a_stopped_service_the_manager_and_no_service_at_all(self):
        found = report(status="stopped", desired_state="stopped")
        found["manager"] = None
        found["runtime_status"] = "degraded"
        text = said(found)
        self.assertIn("✗ 服务管理器没有回应", text)
        self.assertIn(f"start --state-dir {STATE}（顺带拉起服务管理器）", text)
        self.assertIn(f"· 机器人 harness-cli_x 已停用\n    下一步：要启用：python3 -B bin/covey_service.py start --state-dir {STATE}", text)
        found["services"] = []
        text = said(found)
        self.assertIn("✗ 这个服务根下没有注册的机器人\n    下一步：还没装就运行 ./install", text)
        self.assertIn("--state-dir <状态目录>", text)

    def test_boot_claude_sessions_devices_and_probes(self):
        found = report(claude_cleanup={"state": "low", "days": 30, "hint": "Claude Code 只保留最近 30 天的会话，更早的话题不能接着做；"
                                                                         "想留久一点，请自己在 /h/.claude/settings.json 里加一行"},
                       devices={"status": "unverified", "devices": {"box": {"check": {"ok": False}}}})
        found["boot"] = {"status": "ready", "mode": "systemd-user",
                         "hint": "user services stop at logout and do not start at boot until: sudo loginctl enable-linger u"}
        found["harness_probe"] = {"codex": {"ok": False, "reason": "not_logged_in", "detail": "Not logged in",
                                            "advice": "尚未登录，请先在这台机器上登录", "checked_at": 0}}
        text = said(found)
        self.assertIn("! Claude Code 只保留最近 30 天的会话", text)
        self.assertIn("    下一步：想留久一点，请自己在 /h/.claude/settings.json 里加一行", text)
        self.assertIn(f"! 设备自检没通过：box\n    下一步：python3 -B bin/covey_devices.py --state-dir {STATE} check box", text)
        self.assertIn("! 开机自启注册好了，但退出登录后用户服务会停、开机也不起\n    下一步：运行 sudo loginctl enable-linger u", text)
        self.assertIn("✗ Codex 调不通：尚未登录，请先在这台机器上登录：Not logged in", text)
        self.assertIn("codex login；好了再运行 python3 -B bin/covey_doctor.py --probe-harnesses", text)
        self.assertIn("结论：机器人在运行；有 1 项要看", text)
        found["harness_probe"] = {}
        found["boot"] = {"status": "unverified", "mode": "not_installed"}
        text = said(found, root_arg=" --root /absolute/other")
        self.assertIn("· 还没测过模型能不能调通\n    下一步：python3 -B bin/covey_doctor.py --probe-harnesses", text)
        self.assertIn("covey_service_install.py --root /absolute/other --mode auto --activate", text)
        self.assertIn("python3 -B bin/covey_doctor.py --root /absolute/other --json", text)

    def test_the_self_hosted_page_is_one_line_when_well_and_a_row_a_problem_otherwise(self):
        plain = said(report())
        self.assertNotIn("自托管", plain)  # Not on: nothing more than before.
        ok = {"status": "ready", "url": "http://127.0.0.1:18650/", "problems": [],
              "public": {"names": ["2001:db8::1"], "port": 18443, "tls": "self-signed", "not_after": "2027-12-01T00:00:00Z"}}
        text = said(report(web_selfhost=ok))
        self.assertIn("✓ 自托管网页在运行：本机 http://127.0.0.1:18650/，公网 https://[2001:db8::1]:18443/（自签证书到 2027-12-01）", text)
        self.assertIn("结论：都正常", text)
        local = said(report(web_selfhost={**ok, "public": None}))
        self.assertIn("✓ 自托管网页在运行：本机 http://127.0.0.1:18650/\n", local)
        bad = {"status": "degraded", "url": "u", "public": None, "problems": [
            {"what": "node 不在服务的 PATH 里", "next": "装 Node.js 22 以上", "level": "fault"},
            {"what": "证书 20 天后过期", "next": "重启网页进程", "level": "note"}]}
        text = said(report(web_selfhost=bad))
        self.assertIn("✗ node 不在服务的 PATH 里\n    下一步：装 Node.js 22 以上", text)
        self.assertIn("! 证书 20 天后过期\n    下一步：重启网页进程", text)
        self.assertNotIn("自托管网页在运行", text)
        self.assertIn("结论：机器人在运行；有 1 项要看", text)
        note_only = said(report(web_selfhost={**bad, "problems": bad["problems"][1:]}))
        self.assertIn("✓ 自托管网页在运行", note_only)
        self.assertIn("! 证书 20 天后过期", note_only)

    def test_diagnose_has_the_page_row_only_for_a_running_service_that_asked(self):
        asked = {"environment": {"COVEY_WEB_SELFHOST": "1", "COVEY_DATA_HOME": "/absolute/data"}}
        with mock.patch.object(doctor.web_selfhost, "check", return_value={"status": "ready", "problems": []}) as check:
            self.assertEqual(doctor.web_selfhost_row(asked), {"status": "ready", "problems": []})
            self.assertEqual(check.call_args.args[1], Path("/absolute/data"))
            check.reset_mock()
            self.assertIsNone(doctor.web_selfhost_row({"environment": {"COVEY_WEB_SELFHOST": "0"}}))
            self.assertIsNone(doctor.web_selfhost_row({}))
            check.assert_not_called()  # Off: nothing asked of the page.
        with mock.patch.object(doctor.web_selfhost, "check", side_effect=RuntimeError("x")):
            found = doctor.web_selfhost_row(asked)
        self.assertEqual(found["status"], "degraded")
        self.assertIn("RuntimeError", found["problems"][0]["what"])

    def test_an_unreadable_configuration_says_where_to_point_it(self):
        text = "\n".join(doctor_words.unavailable_lines(Path("/absolute/root")))
        self.assertIn("结论：没查成", text)
        self.assertIn("--root 指向 <数据根>/bridge-services", text)


class Terminal(io.StringIO):
    def isatty(self):
        return True


class EntryTest(unittest.TestCase):
    """doctor.main: the summary in a terminal or with --summary, the JSON otherwise or with --json; same exit codes."""

    def run_main(self, argv, out):
        from covey.service.manager import process_identity, register, save
        identity = process_identity(os.getpid())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "harness-cli_x"
            manifest = register(root, state, [sys.executable, "-c", "pass"], workdir=root,
                                environment={"LARK_BRIDGE_REVISIT": "1"})
            save(root / "status" / (service_key(state) + ".json"), {"process": identity, "generation": manifest["generation"]})
            with mock.patch.object(doctor, "manager_health", return_value={"process": identity, "lag": 0}), \
                    mock.patch.object(doctor, "query", return_value={"process": identity, "status": "ready"}), \
                    mock.patch.object(doctor, "boot_report", return_value={"status": "ready", "mode": "systemd-user"}), \
                    mock.patch.object(sys, "argv", ["covey_doctor.py", "--root", str(root), "--skip-events", *argv]), \
                    mock.patch("sys.stdout", out), mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                code = doctor.main()
            return code, out.getvalue(), err.getvalue(), str(state.resolve())

    def test_a_terminal_gets_the_summary_and_a_pipe_the_json(self):
        code, text, err, state = self.run_main([], Terminal())
        self.assertEqual((code, err), (0, ""))
        self.assertIn("结论：都正常", text)
        self.assertIn("机器人 harness-cli_x 正常", text)
        self.assertIn("COVEY_REVISIT=1", text)
        self.assertIn(" --root ", text)  # Not the default root: the commands name it.
        code, text, _, _ = self.run_main([], io.StringIO())
        self.assertEqual((code, json.loads(text)["status"]), (0, "ready"))
        code, text, _, _ = self.run_main(["--json"], Terminal())
        self.assertEqual(json.loads(text)["services"][0]["status"], "ready")
        code, text, _, _ = self.run_main(["--summary"], io.StringIO())
        self.assertTrue(text.startswith("Covey 诊断"))

    def test_both_shapes_is_a_usage_error_and_an_unreadable_root_is_said_in_either(self):
        with mock.patch.object(sys, "argv", ["covey_doctor.py", "--json", "--summary"]), \
                mock.patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit) as caught:
            doctor.main()
        self.assertEqual(caught.exception.code, 2)
        for out, words in ((Terminal(), "结论：没查成"), (io.StringIO(), '"status": "unavailable"')):
            with self.subTest(terminal=out.isatty()), \
                    mock.patch.object(doctor, "diagnose", side_effect=ValueError("broken")), \
                    mock.patch.object(sys, "argv", ["covey_doctor.py", "--root", "/absolute/nowhere"]), \
                    mock.patch("sys.stdout", out):
                self.assertEqual(doctor.main(), 2)
            self.assertIn(words, out.getvalue())


if __name__ == "__main__":
    unittest.main()
