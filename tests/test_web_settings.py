"""网页工作台的设置页 (app/web_settings.py; app/web_ops.py ``settings``; app/web_sync.py's ``settings`` item, COVEY_WEB=1): the page reads the
设置 card's tabs as the card draws them, and the few changes it may make go through the card's own code, so the card shows them too (and a
change on the card shows on the page). Only the owner; the rows the owner kept to Feishu are refused; no redaction term leaves the machine."""
from __future__ import annotations

import json
import os

FIXTURE = os.environ.get("COVEY_WEB_SETTINGS_FIXTURE", "")  # Writes the snapshot there (the page's local mirror uses it).
from unittest import mock

from support.world import UnifiedCase
from covey.app import web_settings
from covey.app.butler_env import ButlerEnv
from covey.app.web_ops import Ops
from covey.app.web_sync import WebSync
from covey.cards import butler_panel as cards
from covey.core import butler_settings as settings, groups as group_records, redaction, web_sync
from covey.core.state import write_json
from covey.core.workspaces import config_path



class WebSettingsTest(UnifiedCase):
    def setUp(self):
        super().setUp()
        self.app.runtime.access.remember_owner_chat("dm")
        write_json(config_path(self.ctx.state_dir), {"default": "project", "workspaces": {
            "project": str(self.work), "docs": str(self.root), "gone": str(self.root / "nope")}})
        settings.update(self.ctx.state_dir, enabled=True)
        self.env = ButlerEnv(runtime=self.app.runtime, topics=self.app.topics, messenger=self.app.messenger, voice=self.app.voice,
                             state_dir=self.ctx.state_dir, profile=self.ctx.profile, settings=self.app.settings_hub)
        self.ops = Ops(self.env)
        self.web = self.ops.web_settings

    def snap(self):
        return self.web.snapshot("dm")

    def row(self, tab, kind, data=None):
        data = data or self.snap()
        found = [r for r in data[tab]["rows"] + (data[tab].get("more") or []) if r["kind"] == kind]
        self.assertEqual(len(found), 1, kind)
        return found[0]

    def op(self, operator="", **payload):
        """The page's ``settings`` op as Ops.run does it; ``operator``: someone the Bridge knows instead of the owner."""
        access = self.app.runtime.access
        with mock.patch.object(access, "owner_chat", return_value="oc_dm"), \
                mock.patch.object(self.app.runtime, "owner_open_id", operator or self.app.runtime.owner_open_id), \
                mock.patch.object(access, "is_owner", side_effect=lambda user, real=access.is_owner: real(user) and not operator):
            return self.ops.run({"kind": "settings", "op_id": "op1", "payload": payload})

    def test_the_tabs_are_the_cards_in_its_order_and_say_what_holds(self):
        data = self.snap()
        if FIXTURE:
            with open(FIXTURE, "w") as out:
                json.dump(data, out, ensure_ascii=False, indent=1)
        self.assertEqual([t["label"] for t in data["tabs"]], ["新话题", "管家", "工作区", "脱敏", "群聊"])  # 设备 only with nodes.
        self.assertNotIn("nodes", data)
        topic = data["topic"]
        self.assertEqual([r["label"] for r in topic["rows"]][-3:], ["脱敏", "工作区", "直接开工"])
        self.assertTrue(all(r["editable"] for r in topic["rows"] + topic["more"]))
        self.assertEqual(topic["more"][0]["label"], "子 Agent")
        butler = data["main"]
        self.assertEqual([r["label"] for r in butler["more"]], ["压缩阈值", "私聊顶层的消息"])
        mode = butler["more"][-1]
        self.assertEqual((mode["text"], mode["editable"]), ("交给管家", False))  # The owner kept it to Feishu.
        names = [w["name"] for w in data["workspace"]["items"]]
        self.assertEqual(names[0], "project")  # The default first, as on the card.
        gone = next(w for w in data["workspace"]["items"] if w["name"] == "gone")
        self.assertIn({"text": "目录不存在", "tone": "red"}, gone["tags"])
        self.assertEqual(data["groups"]["summary"], "还没有群")

    def test_a_row_changed_on_the_page_is_the_cards_change_and_shows_on_both(self):
        state, words = self.op(what="row", kind=cards.TOPIC_RUN_MODE, option="worktree")
        self.assertEqual((state, words), ("done", "已保存：之后新开的话题照这个来。"))
        self.assertEqual(settings.load(self.ctx.state_dir).topic_defaults.run_mode, "worktree")
        self.assertEqual(self.row("topic", cards.TOPIC_RUN_MODE)["current"], "worktree")
        state, words = self.op(what="row", kind=cards.COMPACT, option="100000")
        self.assertEqual((state, words), ("done", "已保存：管家下一轮起照这个来。"))
        self.assertEqual(settings.load(self.ctx.state_dir).compact_tokens, 100000)
        # Changed on the card: the page's next snapshot says it.
        settings.update(self.ctx.state_dir, compact_tokens=300000)
        self.assertEqual(self.row("main", cards.COMPACT)["current"], "300000")

    def test_rows_kept_to_feishu_an_option_gone_and_anyone_else_are_refused(self):
        self.assertEqual(self.op(what="row", kind=cards.MODE, option="topics"), ("failed", web_settings.READ_ONLY))
        self.assertEqual(self.op(what="row", kind=cards.REDACTION, option="on"), ("failed", web_settings.READ_ONLY))
        self.assertEqual(self.op(what="row", kind=cards.TOPIC_WORKSPACE, option="nowhere"), ("failed", web_settings.GONE))
        self.assertEqual(self.op(operator="ou_other", what="row", kind=cards.TOPIC_RUN_MODE, option="worktree"),
                         ("failed", web_settings.OWNER_ONLY))
        self.assertEqual(self.op(what="free", kind=cards.TOPIC_RUN_MODE), ("failed", "不认识的设置改动"))
        self.assertTrue(settings.load(self.ctx.state_dir).enabled)
        self.assertEqual(settings.load(self.ctx.state_dir).topic_defaults.run_mode, "direct")
        settings.update(self.ctx.state_dir, enabled=False)  # 纯话题: the butler's rows count for nothing.
        self.assertEqual(self.op(what="row", kind=cards.COMPACT, option="0")[0], "failed")
        self.assertEqual([r["kind"] for r in self.snap()["main"]["rows"]], [cards.MODE])

    def test_a_workspaces_use_trusted_and_the_redaction_switches(self):
        self.assertEqual(self.op(what="note", workspace="docs", note="  文档 和 手册 "), ("done", "已保存。"))
        self.assertEqual(settings.load(self.ctx.state_dir).note("docs"), "文档 和 手册")
        self.assertEqual(next(w for w in self.snap()["workspace"]["items"] if w["name"] == "docs")["note"], "文档 和 手册")
        self.assertEqual(self.op(what="note", workspace="docs", note=""), ("done", "已保存。"))
        self.assertEqual(settings.load(self.ctx.state_dir).note("docs"), "")
        self.assertEqual(self.op(what="note", workspace="elsewhere", note="x"), ("failed", "没有这个工作区"))
        settings.update(self.ctx.state_dir, start=("trusted",), trusted=("project|edit",))
        self.assertEqual(self.snap()["topic"]["trusted"], [{"key": "project|edit", "text": "project · 改文件"}])
        self.assertEqual(self.op(what="untrust", key="project|edit")[0], "done")
        self.assertEqual(settings.load(self.ctx.state_dir).trusted, ())
        redaction.update_config(self.ctx.state_dir, lambda config: config["terms"].append(
            {"term": "Secretproj", "stand_in": "", "anywhere": False}))
        data = self.snap()["redaction"]
        self.assertEqual((data["terms"], data["ignore_case"]), (["Secretproj"], True))  # The owner, T567: the page edits the list.
        self.assertTrue(data["summary"].startswith("1 个词"))
        self.assertEqual(self.op(what="redaction", name="restore", on=False), ("done", "「回复还原」关掉了。"))
        self.assertFalse(redaction.load_config(self.ctx.state_dir)["restore"])
        self.assertFalse(next(s for s in self.snap()["redaction"]["switches"] if s["name"] == "restore")["on"])

    def test_terms_are_added_and_removed_at_once_and_the_receipt_names_none(self):
        def terms():
            return [row["term"] for row in redaction.load_config(self.ctx.state_dir)["terms"]]

        redaction.save_config(self.ctx.state_dir, {"terms": [  # An older version saved a stand-in of the owner's own.
            {"term": "Old Project", "stand_in": "", "anywhere": False}, {"term": "legacy", "stand_in": "ZQ-77", "anywhere": False}]})
        state, words = self.op(what="terms", add=["Blue Harbor 二期", " ACME Corp ", "", "blue harbor 二期", "LEGACY"],
                               remove=["old project", "gone already"])
        self.assertEqual((state, words), ("done", "已保存：加 2 个、删 1 个（2 个已在词表里，1 个已经不在词表里）。下一轮起生效。"))
        self.assertEqual(terms(), ["legacy", "Blue Harbor 二期", "ACME Corp"])
        self.assertNotIn("ZQ-77", json.dumps(self.snap()["redaction"], ensure_ascii=False))  # Never a stand-in.
        # A term written again in another case: removed first, then added.
        self.assertEqual(self.op(what="terms", add=["Acme Corp"], remove=["ACME Corp"])[1], "已保存：加 1 个、删 1 个。下一轮起生效。")
        self.assertEqual(terms(), ["legacy", "Blue Harbor 二期", "Acme Corp"])
        # One term the list cannot take saves nothing, and the words say which by its place, not by itself.
        state, words = self.op(what="terms", add=["Fine term", "X", "a => b", "Y"], remove=["legacy"])
        self.assertEqual(state, "failed")
        self.assertEqual(words, "没有保存：新加的第 2、4 个词原词至少要 2 个字；新加的第 3 个词替身词自动生成，不用写 =>")
        self.assertEqual(terms(), ["legacy", "Blue Harbor 二期", "Acme Corp"])
        self.assertEqual(self.op(what="terms", add=[], remove=[]), ("done", "没有变化"))
        self.assertEqual(self.op(what="terms", add="Fine term", remove=[]), ("failed", web_settings.TERMS_SHAPE))
        self.assertEqual(self.op(operator="ou_other", what="terms", add=["Fine term"], remove=[]),
                         ("failed", web_settings.OWNER_ONLY))
        redaction.config_path(self.ctx.state_dir).write_text('{"terms": [{"term": "Leaky", "stand_in": ""}, '
                                                             '{"term": "leaky", "stand_in": ""}]}')
        self.assertEqual(self.op(what="terms", add=["Fine term"], remove=[]), ("failed", web_settings.TERMS_UNREADABLE))
        data = json.dumps(self.snap()["redaction"], ensure_ascii=False)
        self.assertIn("error", data)
        self.assertNotIn("eaky", data)

    def test_a_group_is_turned_off_and_on_by_a_key_that_is_no_chat_id(self):
        chat = "oc_group0123456789"
        group_records.save_group(self.ctx.state_dir, chat, lambda record: {**record, "chat_id": chat, "active": True, "external": False})
        data = self.snap()["groups"]
        item, = data["items"]
        self.assertNotIn(chat, json.dumps(data))
        self.assertEqual((item["state"]["text"], item["switch"]), ("已启用", "off"))
        self.assertEqual(self.op(what="group", group=item["key"], on=False)[0], "done")
        self.assertFalse(self.app.runtime.access.group(chat).get("active"))
        self.assertEqual(self.snap()["groups"]["items"][0]["switch"], "on")
        self.assertEqual(self.op(what="group", group="nope", on=True), ("failed", "没有这个群的记录了"))

    def test_the_push_sends_the_item_once_and_again_after_a_change(self):
        with mock.patch.dict(os.environ, {web_sync.ENV: "1"}):
            sync = WebSync(self.app.runtime, self.env)
            first = sync.settings_items("dm", 0.0, False)
            self.assertEqual([(i["type"], i["key"]) for i in first], [("settings", "settings")])
            self.assertEqual(sync.settings_items("dm", 1.0, False), [])  # Nothing changed, no full look.
            settings.update(self.ctx.state_dir, compact_tokens=0)  # The card wrote main-thread.json.
            again = sync.settings_items("dm", 2.0, False)
            self.assertNotEqual(again[0]["rev"], first[0]["rev"])
            self.assertEqual(sync.settings_items("dm", 3.0, True)[0]["rev"], again[0]["rev"])  # A full look: the same rev.
            redaction.update_config(self.ctx.state_dir, lambda config: config["terms"].append(  # The card added a term.
                {"term": "Fresh term", "stand_in": "", "anywhere": False}))
            third, = sync.settings_items("dm", 4.0, False)
            self.assertEqual(third["data"]["redaction"]["terms"], ["Fresh term"])

    def test_a_change_from_the_page_is_pushed_before_its_ack_and_one_on_the_card_within_a_second(self):
        """2026-11-19 T554 on the live page: the push thread's full look took minutes, so the page's own change came back 3 min 40 s later
        and a second click on the old option did nothing. The inbox thread posts the settings itself, before the ack."""
        with mock.patch.dict(os.environ, {web_sync.ENV: "1"}):
            sync = WebSync(self.app.runtime, self.env)
        calls, outbox = [], [{"seq": 1, "op_id": "op-1", "kind": "settings", "topic_key": "", "project_key": "",
                              "payload": {"what": "row", "kind": cards.TOPIC_RUN_MODE, "option": "worktree"}}]

        def request(method, path, body=None):
            calls.append((path.split("?")[0], body))
            return {"items": outbox} if path.startswith("/sync/inbox") else {"ok": True}

        sync.client.request = request
        with mock.patch.object(self.app.runtime.access, "owner_chat", return_value="oc_dm"):
            sync.inbox_once()
            paths = [path for path, _ in calls]
            self.assertEqual(paths, ["/sync/inbox", "/sync/upsert", "/sync/ack"])
            pushed, = calls[1][1]["items"]
            run_mode, = [r for r in pushed["data"]["topic"]["rows"] if r["kind"] == cards.TOPIC_RUN_MODE]
            self.assertEqual(run_mode["current"], "worktree")
            outbox.clear()
            calls.clear()
            sync.inbox_once()  # Nothing changed: no post.
            self.assertEqual([path for path, _ in calls], ["/sync/inbox"])
            settings.update(self.ctx.state_dir, subagents="same")  # The card wrote main-thread.json.
            sync.inbox_once()
            self.assertEqual([path for path, _ in calls], ["/sync/inbox", "/sync/inbox", "/sync/upsert"])
