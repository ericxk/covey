"""The 设置 card's 诊断 tab: service/doctor.py's report in a few lines, read-only (app/doctor_panel.py, cards/doctor_card.py)."""
from __future__ import annotations

import json
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_settings_hub import HubCase, MEMBER
from covey.app.doctor_panel import OWNER_ONLY, summarize
from covey.cards.doctor_card import doctor_card, doctor_rows
from covey.cards.elements import CARD_NODES, element_count
from covey.core.process import service_key

NOW = 1_763_700_000.0


def report(key, **changes):
    """A report as service/doctor.py diagnose writes it, the service of ``key`` and one other in the root."""
    service = {"id": key, "desired_state": "running", "status": "ready", "alive": True, "generation": 463,
               "bridge": {"status": "ready", "remote_connection": {"status": "receiving"}},
               "events": {"status": "ready", "owned_required_consumers": True, "dropped": 0}}
    service.update(changes.pop("service", {}))
    found = {"schema_version": 1, "status": "ready", "runtime_status": "ready",
             "boot": {"status": "ready", "mode": "systemd-user", "registered": True},
             "manager": {"process": {"pid": 1}, "lag": 0.42},
             "services": [service, {"id": "other", "desired_state": "running", "status": "ready"}],
             "model_access": "not_probed",
             "harness_probe": {"claude": {"ok": True, "reason": "ok", "detail": "", "checked_at": NOW - 600},
                               "codex": {"ok": False, "reason": "network", "detail": "ECONNRESET",
                                         "checked_at": NOW - 60, "advice": "连不上模型服务（网络或代理问题）"}},
             "events_checked": True}
    found.update(changes)
    return found


class RowsTests(unittest.TestCase):
    def test_a_report_in_words(self):
        data = summarize(report("k1"), "k1", NOW)
        self.assertEqual(data["service"]["generation"], 463)
        self.assertEqual(data["others"], 1)
        self.assertNotIn("detail", data["harnesses"]["codex"])  # No command output kept.
        rows = doctor_rows(data)
        self.assertEqual([(ok, what) for ok, what, _ in rows],
                         [(True, "服务管理器在运行"), (True, "机器人正常"), (True, "开机会自动拉起"),
                          (True, "Claude Code 能调通"), (False, "Codex 调不通"), ("info", "同一服务根下还有 1 个机器人")])
        self.assertIn("第 463 代", rows[1][2])
        self.assertIn("连不上模型服务", rows[4][2])

    def test_what_needs_a_look(self):
        broken = report("k1", manager=None, boot={"status": "ready", "mode": "manager", "autostart": False},
                        harness_probe={}, service={"status": "degraded", "dropped": 3,
                                                   "bridge": {"status": "degraded",
                                                              "remote_connection": {"status": "stalled"}},
                                                   "events": {"status": "degraded", "owned_required_consumers": False,
                                                              "dropped": 3},
                                                   "permission_env": ["LARK_BRIDGE_CODEX_SANDBOX=read-only is ..."]})
        rows = doctor_rows(summarize(broken, "k1", NOW))
        self.assertEqual(rows[0][:2], (False, "服务管理器没有回应"))
        self.assertEqual(rows[1][:2], (False, "机器人有问题"))
        for words in ("和飞书的连接卡住了", "收消息或卡片点击的连接没就绪", "丢了 3 个事件", "权限变量"):
            self.assertIn(words, rows[1][2])
        self.assertEqual(rows[2][:2], (None, "开机不会自动拉起"))
        self.assertEqual(rows[3][:2], (None, "还没测过模型能不能调通"))
        self.assertEqual(doctor_rows(summarize(report("k1"), "not-this-one", NOW))[1][:2],
                         (None, "没找到这个机器人的服务清单"))
        self.assertEqual([row[:2] for row in doctor_rows({"checked_at": NOW, "error": "OSError"})],
                         [(False, "这次没查成")])

    def test_the_card_counts_what_needs_a_look_and_offers_no_button(self):
        card = doctor_card("t" * 24, {"revision": 1, "hub": True}, summarize(report("k1"), "k1", time.time()))
        self.assertEqual(card["header"]["title"]["content"], "诊断")
        self.assertTrue(card["header"]["subtitle"]["content"].startswith("1 项要看｜"))
        self.assertNotIn('"button"', json.dumps(card))
        self.assertLessEqual(element_count(card), CARD_NODES)


class DoctorTabTests(HubCase):
    def setUp(self):
        super().setUp()
        self.reads = 0

        def read():
            self.reads += 1
            return report(service_key(self.ctx.state_dir))
        self.hub.panel("doctor").report = read

    def test_the_tab_reads_the_report_when_turned_to_and_again_when_unfolded(self):
        self.open()
        self.assertEqual(self.reads, 0)  # The 设置 card opens on 新话题: no report read for it.
        self.assertIn("诊断", [label for label, _ in self.tabs()])
        self.assertEqual(self.press(self.tab("诊断"), "ev-t1"), "settings_tab")
        self.assertEqual((self.title(), self.hub_record()["tab"], self.reads), ("诊断", "doctor", 1))
        shown = json.dumps(self.card(), ensure_ascii=False)
        for words in ("服务管理器在运行", "机器人正常", "Codex 调不通", "covey_doctor.py"):
            self.assertIn(words, shown)
        self.assertEqual(self.hub_record()["doctor"]["service"]["generation"], 463)
        self.assertEqual(self.tabs()[-1], ("诊断", "primary_filled"))
        self.assertEqual(self.press(self.tab("诊断"), "ev-t2"), "settings_fold")
        self.assertEqual(self.reads, 1)
        self.assertEqual(self.press(self.tab("诊断"), "ev-t3"), "settings_unfold")
        self.assertEqual(self.reads, 2)  # Unfolded: read anew.
        self.assertEqual(self.press(self.tab("群聊"), "ev-t4"), "settings_tab")
        self.assertEqual(self.title(), "群聊")

    def test_only_the_owner_and_a_report_that_fails_is_said(self):
        self.open()
        click = self.tab("诊断")
        self.assertEqual(self.press(click, "ev-t1", operator=MEMBER), "not_owner")
        self.assertEqual(self.reads, 0)
        self.press(click, "ev-t2")

        def broken():
            raise OSError("unreadable")
        self.hub.panel("doctor").report = broken
        self.press(self.tab("诊断"), "ev-t3")  # Fold.
        self.press(self.tab("诊断"), "ev-t4")  # Unfold, reading anew.
        self.assertIn("这次没查成", json.dumps(self.card(), ensure_ascii=False))
        event = {"event_id": "ev-t5", "chat_id": "dm", "message_id": "om_s1"}
        self.assertEqual(self.hub.click(self.ctx, event, self.tab("新话题"), False), "owner_only")
        self.assertIn(OWNER_ONLY, json.dumps(self.card(), ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
