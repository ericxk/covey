"""网页工作台的设置页 (app/web_settings.py; app/web_ops.py ``settings``; app/web_sync.py's ``settings`` item, COVEY_WEB=1): the page reads the
设置 card's tabs as the card draws them, and the few changes it may make go through the card's own code, so the card shows them too (and a
change on the card shows on the page). Only the owner; the rows the owner kept to Feishu are refused; no redaction term leaves the machine."""
from __future__ import annotations

import json
import os

FIXTURE = os.environ.get("COVEY_WEB_SETTINGS_FIXTURE", "")  # Writes the snapshot there (the page's local mirror uses it).
from unittest import mock

from support.world import UnifiedCase
from covey.app import butler_panel, web_settings
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
        from covey.app.proxy_send import available
        # 设备 only with nodes; 代答 only where a sender is (an extension, covey_internal).
        self.assertEqual([t["label"] for t in data["tabs"]], ["新话题", "管家", "工作区", "脱敏", "群聊"] + (["代答"] if available() else []))
        self.assertNotIn("nodes", data)
        topic = data["topic"]
        self.assertEqual([r["label"] for r in topic["rows"]][-3:], ["脱敏", "工作区", "直接开工"])
        self.assertTrue(all(r["editable"] for r in topic["rows"] + topic["more"]))
        self.assertEqual(topic["more"][0]["label"], "子 Agent")
        butler = data["main"]
        self.assertEqual([r["label"] for r in butler["more"]], ["压缩阈值", "私聊顶层的消息"])
        mode = butler["more"][-1]
        self.assertEqual((mode["text"], mode["editable"]), ("交给管家", True))  # T554 2nd round: with a confirm on the page.
        self.assertEqual(mode["confirm"], {"topics": cards.MODE_CONFIRM})
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
        self.assertEqual(self.op(what="row", kind=cards.MODE, option="topics"), ("failed", "改成每条直接开一个话题要先确认"))
        self.assertEqual(self.op(what="row", kind=cards.ENABLE, option="on"), ("failed", web_settings.READ_ONLY))
        self.assertEqual(self.op(what="row", kind=cards.TOPIC_WORKSPACE, option="nowhere"), ("failed", web_settings.GONE))
        self.assertEqual(self.op(operator="ou_other", what="row", kind=cards.TOPIC_RUN_MODE, option="worktree"),
                         ("failed", web_settings.OWNER_ONLY))
        self.assertEqual(self.op(what="nope", kind=cards.TOPIC_RUN_MODE), ("failed", "不认识的设置改动"))
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

    # -- T554 second round (2026-11-19): what the page could only look at -------------------------------------------------------

    def test_the_private_chat_mode_and_the_butlers_redaction_change_on_the_page(self):
        self.assertEqual(self.op(what="row", kind=cards.MODE, option="topics", confirm=True),
                         ("done", butler_panel.MODE_NOTICES[False]))
        self.assertFalse(settings.load(self.ctx.state_dir).enabled)
        mode, = self.snap()["main"]["rows"]
        self.assertTrue(mode["editable"])  # 纯话题: the row that turns the butler on again.
        self.assertEqual(self.op(what="row", kind=cards.MODE, option="agent"), ("done", butler_panel.MODE_NOTICES[True]))
        self.assertTrue(settings.load(self.ctx.state_dir).enabled)
        self.assertEqual(self.op(what="row", kind=cards.REDACTION, option="off")[0], "done")

    def test_workspaces_added_by_path_removed_and_pruned_as_on_the_card(self):
        extra = self.root / "extra"
        extra.mkdir()
        self.assertEqual(self.op(what="workspace_add", path=str(extra)), ("done", "已添加工作区 extra"))
        self.assertEqual(self.op(what="workspace_add", path=str(extra)), ("done", "该目录已在列表中：extra"))
        self.assertEqual(self.op(what="workspace_add", path=str(self.root / "nowhere"))[0], "failed")
        self.assertEqual(self.op(what="workspace_add", path="relative/dir")[0], "failed")
        data = self.snap()["workspace"]
        self.assertEqual(data["missing"], 1)  # "gone"
        self.assertFalse(next(w for w in data["items"] if w["name"] == "project")["removable"])  # The default.
        self.assertEqual(self.op(what="workspace_remove", workspace="project")[0], "failed")
        self.assertEqual(self.op(what="workspace_remove", workspace="extra"), ("done", "已从列表移除 extra，磁盘目录未删除"))
        self.assertTrue(extra.is_dir())
        self.assertEqual(self.op(what="workspace_prune"), ("done", "已从列表移除 1 项，磁盘目录未删除"))
        self.assertEqual(sorted(w["name"] for w in self.snap()["workspace"]["items"]), ["docs", "project"])

    def test_free_talk_only_tighter_on_the_page(self):
        chat = "oc_group0123456789"
        group_records.save_group(self.ctx.state_dir, chat, lambda record: {**record, "chat_id": chat, "active": True,
                                                                            "external": False, "free_talk": "all"})
        with mock.patch.dict(os.environ, {"COVEY_GROUP_ALL_MESSAGES": "1"}):
            item, = self.snap()["groups"]["items"]
            self.assertEqual(item["free_mode"], "all")
            self.assertEqual([c["value"] for c in item["free_choices"]], ["", "owner"])
            self.assertEqual(self.op(what="free", group=item["key"], mode="all")[0], "failed")
            self.assertEqual(self.op(what="free", group=item["key"], mode="owner")[0], "done")
            self.assertEqual(self.app.runtime.access.group(chat).get("free_talk"), "owner")
        self.assertEqual(self.op(what="free", group=item["key"], mode="")[0], "failed")  # Without 免@ in this deployment.

    def test_nodes_added_by_ssh_off_the_inbox_and_removed(self):
        from covey.app import web_panels
        from covey.core import nodes
        ran = []
        gateway = mock.Mock(tls=mock.Mock(spki="pin"), address="hub:7443")
        panel = mock.Mock(gateway=gateway, ssh_join=lambda alias, args, command: ran.append((alias, args, command)) or f"✅ {alias} 已装好")
        ctx = self.web.context()
        started = []
        with mock.patch.object(nodes, "open_join", return_value="tok") as opened:
            words = web_panels.node_add(ctx, panel, {"name": "box1", "ssh": "me@box1 -p 22"}, start=started.append)
        self.assertIn("正在经 ssh 登上 box1", words)
        opened.assert_called_once_with(ctx.state_dir, "box1", "box1", via="ssh")
        self.assertEqual(web_panels.read_joins(ctx.state_dir)["box1"]["state"], "joining")
        self.assertEqual(ran, [])  # Not in the inbox's thread.
        started[0]()
        self.assertEqual(ran[0][1], ["me@box1", "-p", "22"])
        self.assertEqual(web_panels.read_joins(ctx.state_dir)["box1"]["state"], "done")
        with self.assertRaisesRegex(ValueError, "只能经 ssh"):
            web_panels.node_add(ctx, panel, {"name": "box2", "ssh": ""})
        with self.assertRaisesRegex(ValueError, "本机执行命令"):
            web_panels.node_add(ctx, panel, {"name": "box2", "ssh": "-oProxyCommand=sh me@x"})
        with self.assertRaisesRegex(ValueError, "本机不能移除"):
            web_panels.node_remove(ctx, panel, {"alias": nodes.LOCAL})
        with mock.patch.object(nodes, "revoke", return_value=True):
            self.assertIn("已移除 box1", web_panels.node_remove(ctx, panel, {"alias": "box1"}))
        gateway.revoke_now.assert_called_once_with("box1")
        self.assertEqual(web_panels.node_row({"alias": "box1", "title": "box1", "line": "🟠 box1 · CPU 3% · 内存 90%", "local": False}),
                         {"alias": "box1", "title": "box1", "dot": "tight", "line": "CPU 3% · 内存 90%", "grey": "", "local": False})

    def test_the_proxy_changes_are_the_cards(self):
        from covey.app import web_panels
        from covey.cards import proxy_panel as proxy_cards
        commands = mock.Mock(cmd_disable=lambda *a: "代答已关闭", set_paused=lambda ctx, key, paused: f"{key} paused={paused}")
        proxy = mock.Mock(commands=commands, settings=lambda ctx: {"enabled": True})
        panel = mock.Mock(proxy=proxy, pick=lambda ctx, kind, option: "⚠️ 这个模型已不在可选目录中，请重新选择。")
        ctx = self.web.context()
        self.assertEqual(web_panels.proxy_change(ctx, panel, {"op": "disable"}), "代答已关闭")
        self.assertEqual(web_panels.proxy_change(ctx, panel, {"op": "enable"}), "代答已经是开启的")
        with self.assertRaisesRegex(ValueError, "不在可选目录"):
            web_panels.proxy_change(ctx, panel, {"op": "row", "kind": proxy_cards.TIER_MODEL, "option": "x"})
        with self.assertRaisesRegex(ValueError, "白名单里已经没有"):
            web_panels.proxy_change(ctx, panel, {"op": "pause", "contact": "nobody"})

    def test_the_usage_item_is_the_cards_count(self):
        from covey.app import weekly_usage
        with mock.patch.object(weekly_usage, "summarize", wraps=weekly_usage.summarize) as summarize:
            found = self.web.usage(now=1_800_000_000)
        summarize.assert_called_once()
        self.assertEqual((found["days"], found["tasks"]), (weekly_usage.WINDOW_DAYS, 0))
        json.dumps(found)  # Pushed as JSON: no Counter left.
        with mock.patch.dict(os.environ, {web_sync.ENV: "1"}):
            sync = WebSync(self.app.runtime, self.env)
        self.assertEqual([i["key"] for i in sync.settings_items("oc_dm", 0.0, True)], ["settings", "usage"])
        self.assertEqual([i["key"] for i in sync.settings_items("oc_dm", 1.0, False)], [])

    def test_dismiss_on_the_page_takes_the_topic_off_the_board_as_the_card_does(self):
        from support.records import write_topic
        from covey.app import overview, pins
        topic = "e" * 32
        write_topic(self.ctx.state_dir, topic_id=topic, chat_id="oc_dm", harness_id="claude", topic_number=9)

        def run(**row):
            with mock.patch.object(self.app.runtime.access, "owner_chat", return_value="oc_dm"):
                return self.ops.run({"kind": "dismiss", "op_id": "op9", "payload": {}, **row})

        with mock.patch.object(overview, "dismissable", return_value=False):
            self.assertEqual(run(topic_key=topic)[0], "failed")
        with mock.patch.object(overview, "dismissable", return_value=True):
            self.assertEqual(run(topic_key=topic), ("done", "已从看板拿掉"))
            self.assertEqual(run(topic_key=topic), ("done", "已经拿掉了"))
            self.assertEqual(run(topic_key="f" * 32)[0], "failed")
        record = json.loads(pins.record_path(self.ctx.state_dir, "oc_dm").read_text())
        self.assertIn(topic, record["dismissed"])

    def test_the_proxy_pages_model_rows_are_the_pages_to_change(self):
        """2026-11-19 live: the rows came out read only (the 设置 rows' EDITABLE knows no 代答 kind)."""
        from covey.app import web_panels
        from covey.cards import proxy_panel as proxy_cards
        from covey.cards.launch_config import Setting
        proxy = mock.Mock(settings=lambda ctx: {"enabled": True, "user_profile": "me"}, web_login=mock.Mock(login_seen=(True, 1.0)),
                          web=mock.Mock(problems=lambda ctx, settings: []))
        panel = mock.Mock(proxy=proxy, current=lambda settings, choices: None)
        rows = [Setting("p_model", "模型", proxy_cards.TIER_MODEL, "m1", [("m1", "M1"), ("m2", "M2")])]
        with mock.patch("covey.app.launch_models.available_models", return_value={}), \
                mock.patch("covey.cards.proxy_panel.model_rows", return_value=rows):
            data = web_panels.proxy_tab(self.web.context(), panel, web_settings.row, web_settings.LIMIT)
        self.assertEqual([(r["kind"], r["editable"]) for r in data["rows"]], [(proxy_cards.TIER_MODEL, True)])
        self.assertTrue(data["login_ok"])
