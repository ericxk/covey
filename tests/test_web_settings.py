"""网页工作台的设置页 (app/web_settings.py; app/web_ops.py ``settings``; app/web_sync.py's ``settings`` item, COVEY_WEB=1): the page reads the
设置 card's tabs as the card draws them, and the few changes it may make go through the card's own code, so the card shows them too (and a
change on the card shows on the page). Only the owner; the rows the owner kept to Feishu are refused."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

FIXTURE = os.environ.get("COVEY_WEB_SETTINGS_FIXTURE", "")  # Writes the snapshot there (the page's local mirror uses it).
from unittest import mock

from support.world import UnifiedCase
from support.butler import switch_on
from covey.app import butler_panel, web_settings, web_switch as web_switch_words
from covey.app.butler_env import ButlerEnv
from covey.app.web_ops import Ops
from covey.app import web_sync as web_sync_app
from covey.app.web_sync import WebSync
from covey.cards import butler_panel as cards
from covey.core import butler_settings as settings, groups as group_records, repomap, web_sync
from covey.core.jobs import upsert_job_record
from covey.core.state import read_json, write_json
from covey.core.workspaces import config_path
from covey.core.defaults import RECOMMENDED



class SettingsCase(UnifiedCase):
    def setUp(self):
        super().setUp()
        self.app.runtime.access.remember_owner_chat("dm")
        (self.root / ".git").mkdir(exist_ok=True)  # docs is a checkout: before repos.json the page lists only those (T870 Wave 3.5).
        write_json(config_path(self.ctx.state_dir), {"default": "project", "workspaces": {
            "project": str(self.work), "docs": str(self.root), "gone": str(self.root / "nope")}})
        switch_on(self)
        self.env = ButlerEnv(runtime=self.app.runtime, topics=self.app.topics, messenger=self.app.messenger, voice=self.app.voice,
                             state_dir=self.ctx.state_dir, profile=self.ctx.profile, settings=self.app.settings_hub)
        self.ops = Ops(self.env)
        self.web = self.ops.web_settings

    def snap(self):
        return self.web.snapshot("dm")


class WebSettingsTest(SettingsCase):
    def row(self, tab, kind, data=None):
        data = (data or self.snap())["config"]  # 配置 (v22): its sections topic, main, lead and assist, and 更多.
        found = [r for r in (data[tab]["rows"] if tab in data else []) + data["more"] if r["kind"] == kind]
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
        # T619: the card's 配置 is one page here too, every section open (no card's size to keep to).
        self.assertEqual([t["label"] for t in data["tabs"]], ["配置", "常用路径", "群聊"] + (["代答"] if available() else []))
        # Data contract v34: the operations this Covey does, so the page shows 收尾、停止 and 不对 only where they are done.
        self.assertTrue({"project_finish", "project_stop", "memory_undo", "recover"} <= set(data["ops"]))
        self.assertNotIn("nodes", data)
        config = data["config"]
        self.assertEqual([config[part]["title"] for part in ("topic", "main")], ["新话题", "管家"])
        self.assertNotIn("review", config)  # 独立复核 was taken out (T937).
        self.assertTrue(config["summary"].startswith("新话题 "))
        topic = config["topic"]
        self.assertEqual([r["label"] for r in topic["rows"]][:1], ["Harness"])
        self.assertNotIn("脱敏", [r["label"] for r in topic["rows"] + config["main"]["rows"]])  # Taken out (T947).
        self.assertNotIn("redaction", data)
        for gone in ("工作区", "运行方式"):  # v43: a new topic starts in the home; the repositories are the 仓库 page's.
            self.assertNotIn(gone, [r["label"] for r in topic["rows"]])
        self.assertEqual(sorted(topic), ["note", "rows", "title"])  # v24: no 直接开工 row, start_note or trusted.
        self.assertEqual({r["label"]: r["line"] for r in topic["rows"]}.get("Harness"), 1)
        self.assertTrue(all(r["editable"] for r in topic["rows"] + config["more"]))
        self.assertEqual(config["more"][0]["label"], "子 Agent")
        butler = config["main"]
        self.assertEqual([r["label"] for r in butler["rows"]][0], "Harness")
        self.assertNotIn("工作区", [r["label"] for r in butler["rows"]])
        self.assertEqual([r["label"] for r in butler["rows"]][-1], "压缩阈值")  # T946: no 私聊顶层的消息 row.
        self.assertTrue(butler["on"])
        self.assertFalse(any("confirm" in r for r in butler["rows"]))
        self.assertEqual(data["repos"]["home"], {"tail": web_settings.tail(str(self.work), web_settings.PATH_SIZE)})  # v43: the 仓库 page; the old 工作区 page is still pushed.
        names = [w["name"] for w in data["workspace"]["items"]]
        self.assertEqual(names[0], "project")  # The default first, as on the card.
        gone = next(w for w in data["workspace"]["items"] if w["name"] == "gone")
        self.assertIn({"text": "目录不存在", "tone": "red"}, gone["tags"])
        self.assertEqual(data["groups"]["summary"], "还没有群")

    def test_the_retired_review_section_is_not_pushed_and_its_old_rows_change_nothing(self):
        """独立复核 was taken out (T937): 配置 has no ``review`` and its title bar no 复核, with COVEY_REVIEW=1 left in a
        manifest too; a page drawn before it that changes one of its rows is told so and main-thread.json's ``review``
        stays as it was (an older release reads it)."""
        from covey.app.card_actions import REVIEW_KINDS, REVIEW_RETIRED
        path = self.ctx.state_dir / settings.FILE
        kept = {"harness": "agy", "model": "gemini-4-neon", "effort": "xhigh"}
        path.write_text(json.dumps({**json.loads(path.read_text() if path.exists() else "{}"), "review": kept}))
        with mock.patch.dict(os.environ, {"COVEY_REVIEW": "1"}):
            config = self.snap()["config"]
            self.assertNotIn("review", config)
            self.assertNotIn("复核", config["summary"])
            for kind in REVIEW_KINDS[1:]:  # The 设置 card's rows; the first is the answer card's button.
                with self.subTest(kind):
                    self.assertEqual(self.op(what="row", kind=kind, option="claude"), ("failed", REVIEW_RETIRED))
        self.assertEqual(json.loads(path.read_text())["review"], kept)

    def test_the_lead_section_is_the_cards_and_its_model_changes_on_the_page(self):
        """T700: with COVEY_PROJECTS=1 配置 has 专项负责人 as the card does (data contract v36 ``lead``), changed on the page
        through the card's code; without it there is none."""
        self.assertNotIn("lead", self.snap()["config"])
        with mock.patch.dict(os.environ, {"COVEY_PROJECTS": "1"}):
            lead = self.snap()["config"]["lead"]
            self.assertEqual((lead["title"], lead["note"]), ("专项负责人", web_settings.LEAD_NOTE))
            harness, = lead["rows"]  # Following the butler: the Harness row alone.
            self.assertEqual((harness["kind"], harness["current"], harness["editable"]), (cards.LEAD_HARNESS, "", True))
            from covey.core.roster import CORE_HARNESSES  # An enabled extension's (covey_internal's MASO) may be among them.
            self.assertEqual([o["value"] for o in harness["options"] if not o["value"] or o["value"] in CORE_HARNESSES],
                             ["", "claude", "codex", "dsh", "agy"])
            self.assertEqual(self.op(what="row", kind=cards.LEAD_HARNESS, option="agy"),
                             ("done", butler_panel.SAVED[butler_panel.LEAD]))
            self.assertEqual(settings.load(self.ctx.state_dir).lead.harness, "agy")
            self.assertEqual([r["kind"] for r in self.snap()["config"]["lead"]["rows"]],
                             [cards.LEAD_HARNESS, cards.LEAD_PROVIDER, cards.LEAD_MODEL, cards.LEAD_EFFORT])
            self.assertIn("负责人 ", self.snap()["config"]["summary"])
            # One that cannot take the Bridge's tools is not offered (DeepSeek Harness made one for the test).
            from covey.core.harness import HarnessCapabilities
            with mock.patch("covey.backends.dsh.bridge.DshBackend.capabilities", lambda self, ctx: HarnessCapabilities()):
                self.assertEqual(self.op(what="row", kind=cards.LEAD_HARNESS, option="dsh"), ("failed", web_settings.GONE))
        self.assertEqual(self.op(what="row", kind=cards.LEAD_HARNESS, option="claude"), ("failed", web_settings.GONE))

    def test_the_assist_section_its_row_and_the_recent_calls_with_the_butler_on_or_off(self):
        """T884 辅助模型: 配置 has it whether the butler is on or not (the background calls run without it), its row changed on the
        page through the card's code, ``words`` what it runs on now, ``recent`` the latest outcome of each background feature
        (core/assist.py: who, the reason's code and words, ms, at; nothing the owner or a model said)."""
        from covey.app import assist
        from covey.core import assist as records
        records.settle(self.ctx.state_dir, feature="tidy", harness="codex", model="gpt-6-astra", code="authentication_error",
                       ms=900, manual=True)
        records.settle(self.ctx.state_dir, feature="track", harness="claude", model="haiku", ms=1200)
        assist.forget()
        section = self.snap()["config"]["assist"]
        self.assertEqual((section["title"], section["note"]), ("辅助模型", cards.ASSIST_NOTE))
        row, = section["rows"]
        self.assertEqual((row["kind"], row["current"], row["editable"]), (cards.ASSIST, cards.ASSIST_AUTO, True))
        values = [o["value"] for o in row["options"]]
        self.assertEqual((values[0], values[-1]), (cards.ASSIST_AUTO, cards.ASSIST_NONE))
        self.assertIn("claude", values)
        self.assertTrue(section["words"].startswith("现在：") or section["words"].startswith("现在用不了"), section["words"])
        self.assertEqual([(r["feature"], r["ok"], r["code"], r["words"]) for r in section["recent"]],
                         [("track", True, "", ""), ("tidy", False, "authentication_error", "登录失效或没有权限")])
        self.assertEqual(self.op(what="row", kind=cards.ASSIST, option=cards.ASSIST_NONE),
                         ("done", butler_panel.SAVED_OF[cards.ASSIST]))
        self.assertTrue(settings.load(self.ctx.state_dir).assist.off)
        assist.forget()
        self.assertEqual(self.snap()["config"]["assist"]["words"], "后台活不调模型，只按规则做。")
        self.assertEqual(self.op(what="row", kind=cards.ASSIST, option="claude")[0], "done")
        chosen = settings.load(self.ctx.state_dir).assist  # A Harness named takes its first model and that model's budget (T924).
        self.assertEqual((chosen.harness, bool(chosen.model)), ("claude", True))
        assist.forget()
        keys = [row["key"] for row in self.snap()["config"]["assist"]["rows"]]
        self.assertEqual(keys[0], "assist")
        self.assertIn("a_model", keys)
        self.assertIn("a_effort", keys)
        self.assertEqual(self.op(what="row", kind=cards.ASSIST_EFFORT, option="low")[0], "done")
        self.assertEqual(settings.load(self.ctx.state_dir).assist.effort, "low")
        self.assertEqual(self.op(what="row", kind=cards.ASSIST, option="nope"), ("failed", web_settings.GONE))
        with mock.patch.dict(os.environ, {"COVEY_BUTLER": "0"}):
            self.assertIn("assist", self.snap()["config"])
            self.assertEqual(self.op(what="row", kind=cards.ASSIST, option=cards.ASSIST_AUTO)[0], "done")
        self.assertEqual(settings.load(self.ctx.state_dir).assist, settings.AssistChoice())

    def test_a_background_call_pushes_the_settings_again(self):
        """T884: the 辅助模型's record (assist-state.json) is in the stamps the push watches; the page has no timed full snapshot."""
        from covey.core import assist as records
        before = self.web.stamps()
        records.settle(self.ctx.state_dir, feature="look", harness="claude", ms=10, manual=True)
        self.assertNotEqual(self.web.stamps(), before)

    def test_a_row_changed_on_the_page_is_the_cards_change_and_shows_on_both(self):
        state, words = self.op(what="row", kind=cards.TOPIC_HARNESS, option="codex")
        self.assertEqual((state, words), ("done", "已保存：之后新开的话题照这个来。"))
        self.assertEqual(settings.load(self.ctx.state_dir).topic_defaults.harness, "codex")
        self.assertEqual(self.row("topic", cards.TOPIC_HARNESS)["current"], "codex")
        state, words = self.op(what="row", kind=cards.COMPACT, option="100000")
        self.assertEqual((state, words), ("done", "已保存：管家下一轮起照这个来。"))
        self.assertEqual(settings.load(self.ctx.state_dir).compact_tokens, 100000)
        # Changed on the card: the page's next snapshot says it.
        settings.update(self.ctx.state_dir, compact_tokens=300000)
        self.assertEqual(self.row("main", cards.COMPACT)["current"], "300000")

    def test_rows_kept_to_feishu_an_option_gone_and_anyone_else_are_refused(self):
        for kind, option in ((cards.MODE, "topics"), (cards.MODE, "agent"), (cards.ENABLE, "on")):  # Retired (T946).
            self.assertEqual(self.op(what="row", kind=kind, option=option, confirm=True), ("failed", cards.MODE_RETIRED))
        self.assertEqual(self.op(what="row", kind=cards.TOPIC_HARNESS, option="nowhere"), ("failed", web_settings.GONE))
        for kind in (cards.TOPIC_WORKSPACE, cards.TOPIC_RUN_MODE, cards.SPACE):  # Offline: the rows are gone from the page.
            self.assertEqual(self.op(what="row", kind=kind, option="worktree"), ("failed", cards.OFFLINE_NOTICE))
        self.assertEqual(self.op(operator="ou_other", what="row", kind=cards.TOPIC_HARNESS, option="codex"),
                         ("failed", web_settings.OWNER_ONLY))
        self.assertEqual(self.op(what="nope", kind=cards.TOPIC_HARNESS), ("failed", "不认识的设置改动"))
        self.assertFalse(settings.path(self.ctx.state_dir).exists())  # Nothing written.
        self.assertEqual(settings.load(self.ctx.state_dir).topic_defaults.harness, "")
        self.assertEqual(settings.load(self.ctx.state_dir).topic_defaults.run_mode, "direct")
        switch_on(self, "0")  # COVEY_BUTLER=0: the butler's rows count for nothing, the page has no 管家.
        self.assertEqual(self.op(what="row", kind=cards.COMPACT, option="0")[0], "failed")
        self.assertNotIn("main", self.snap()["config"])

    def test_a_workspaces_use_and_the_retired_untrust_and_redaction_changes(self):
        self.assertEqual(self.op(what="note", workspace="docs", note="  文档 和 手册 "), ("done", "已保存。"))
        self.assertEqual(settings.load(self.ctx.state_dir).note("docs"), "文档 和 手册")
        self.assertEqual(next(w for w in self.snap()["workspace"]["items"] if w["name"] == "docs")["note"], "文档 和 手册")
        self.assertEqual(self.op(what="note", workspace="docs", note=""), ("done", "已保存。"))
        self.assertEqual(settings.load(self.ctx.state_dir).note("docs"), "")
        self.assertEqual(self.op(what="note", workspace="elsewhere", note="x"), ("failed", "没有这个工作区"))
        self.assertEqual(self.op(what="untrust", key="project|edit"), ("failed", "记住的工作区已下线：管家开的话题一律直接开工"))
        # 敏感词脱敏 was taken out (T947): its two changes and its rows, from a page drawn before, are refused and change nothing.
        before = settings.path(self.ctx.state_dir).read_bytes() if settings.path(self.ctx.state_dir).exists() else b""
        retired = ("failed", web_settings.REDACTION_RETIRED)
        self.assertEqual(self.op(what="redaction", name="restore", on=False), retired)
        self.assertEqual(self.op(what="terms", add=["Fine term"], remove=[]), retired)
        self.assertEqual(self.op(what="terms", add="Fine term", remove=[]), retired)
        for kind in ("bridge_main_redaction", "bridge_main_topic_redaction"):
            self.assertEqual(self.op(what="row", kind=kind, option="on"), retired)
        self.assertEqual(self.op(operator="ou_other", what="terms", add=["Fine term"], remove=[]),
                         ("failed", web_settings.OWNER_ONLY))
        after = settings.path(self.ctx.state_dir).read_bytes() if settings.path(self.ctx.state_dir).exists() else b""
        self.assertEqual(before, after)

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

    def test_a_change_from_the_page_is_pushed_before_its_ack_and_one_on_the_card_within_a_second(self):
        """2026-11-19 T554 on the live page: the push thread's full look took minutes, so the page's own change came back 3 min 40 s later
        and a second click on the old option did nothing. The inbox thread posts the settings itself, before the ack."""
        with mock.patch.dict(os.environ, {web_sync.ENV: "1"}):
            sync = WebSync(self.app.runtime, self.env)
        calls, outbox = [], [{"seq": 1, "op_id": "op-1", "kind": "settings", "topic_key": "", "project_key": "",
                              "payload": {"what": "row", "kind": cards.TOPIC_HARNESS, "option": "codex"}}]

        def request(method, path, body=None):
            calls.append((path.split("?")[0], body))
            return {"items": outbox} if path.startswith("/sync/inbox") else {"ok": True}

        sync.client.request = request
        with mock.patch.object(self.app.runtime.access, "owner_chat", return_value="oc_dm"):
            sync.inbox_once()
            paths = [path for path, _ in calls]
            self.assertEqual(paths, ["/sync/inbox", "/sync/upsert", "/sync/ack"])
            pushed, = calls[1][1]["items"]
            harness, = [r for r in pushed["data"]["config"]["topic"]["rows"] if r["kind"] == cards.TOPIC_HARNESS]
            self.assertEqual(harness["current"], "codex")
            outbox.clear()
            calls.clear()
            sync.inbox_once()  # Nothing changed: no post.
            self.assertEqual([path for path, _ in calls], ["/sync/inbox"])
            settings.update(self.ctx.state_dir, subagents="same")  # The card wrote main-thread.json.
            sync.inbox_once()  # An empty poll reads no file now (T673: the inbox stuck 60 s on these stats).
            self.assertEqual([path for path, _ in calls], ["/sync/inbox", "/sync/inbox"])
            sync.last_full = time.time()  # No pass due: the pass thread's step looks at the settings' files alone.
            sync.pass_once()
            self.assertEqual([path for path, _ in calls][2:], ["/sync/upsert"])

    # -- T554 second round (2026-11-19): what the page could only look at -------------------------------------------------------


    def test_a_settings_file_that_cannot_be_read_now_is_not_reset_and_the_page_says_so(self):
        """W1-F (RT-6): read as {} a moment, the write of one row put the defaults over every other setting."""
        settings.update(self.ctx.state_dir, compact_tokens=0)
        path = settings.path(self.ctx.state_dir)
        body = path.read_bytes()
        path.write_bytes(body[: len(body) // 2])
        torn = path.read_bytes()
        self.assertEqual(self.op(what="row", kind=cards.TOPIC_HARNESS, option="codex"), ("failed", settings.UNREADABLE))
        self.assertEqual(path.read_bytes(), torn)
        path.write_bytes(body)
        self.assertEqual(self.op(what="row", kind=cards.TOPIC_HARNESS, option="codex")[0], "done")
        self.assertEqual(settings.load(self.ctx.state_dir).compact_tokens, 0)

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

    # -- 仓库 (data contract v43): the repository map, its changes and the worktrees ----------------------------------------------

    def git(self, *args, cwd):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True, capture_output=True)

    def make_repo(self, path):
        path.mkdir(parents=True)
        self.git("init", "-q", "-b", "main", cwd=path)
        self.git("commit", "-q", "--allow-empty", "-m", "first", cwd=path)
        return path

    def worktrees_root(self):
        root = self.root / "wt-root"
        root.mkdir(exist_ok=True)
        patcher = mock.patch.object(web_settings, "worktree_root", return_value=root)
        patcher.start()
        self.addCleanup(patcher.stop)
        return root

    def repos_file(self):
        return repomap.path(self.ctx.state_dir)

    def test_before_the_map_the_home_by_another_name_and_a_plain_directory_are_no_repositories(self):
        """T870 Wave 3.5 (P1-10, CUA C14): the old list had the home under a link's name and directories topics once ran in."""
        self.worktrees_root()
        plain = self.root / "plain-dir"
        plain.mkdir()
        link = self.root / "home-link"
        link.symlink_to(self.work)
        write_json(config_path(self.ctx.state_dir), {"default": "project", "workspaces": {
            "project": str(self.work), "docs": str(self.root), "plain": str(plain), "again": str(link),
            "gone": str(self.root / "nope")}})
        self.assertEqual([i["name"] for i in self.snap()["repos"]["items"]], ["docs", "gone"])

    def test_the_repos_page_is_the_old_list_read_only_until_the_first_change(self):
        self.worktrees_root()
        page = self.snap()["repos"]
        self.assertEqual(sorted(page), ["devices", "home", "items", "worktrees"])
        self.assertEqual(page["devices"], [])  # Devices off: paths of this machine only.
        self.assertEqual(page["home"], {"tail": web_settings.tail(str(self.work), web_settings.PATH_SIZE)})
        self.assertEqual([i["name"] for i in page["items"]], ["docs", "gone"])  # The home itself is no repository.
        docs = page["items"][0]
        self.assertEqual(sorted(docs), ["branch", "device", "kind", "live_topics", "name", "note", "rules", "status", "tail", "write_mode"])
        self.assertEqual((docs["kind"], docs["device"]), ("git", ""))
        self.assertEqual((docs["status"], docs["write_mode"], docs["note"], docs["live_topics"], docs["rules"]),
                         ("active", "inplace", "", 0, ["AGENTS.md"]))
        self.assertEqual(page["worktrees"], {"total": 0, "prunable": []})
        self.assertFalse(self.repos_file().exists())  # Reading writes nothing.
        before = config_path(self.ctx.state_dir).read_text()
        self.assertEqual(self.op(what="note", workspace="docs", note="文档"), ("done", "已保存。"))  # The butler's 用途 shows on the view.
        self.assertEqual(next(i for i in self.snap()["repos"]["items"] if i["name"] == "docs")["note"], "文档")
        self.assertFalse(self.repos_file().exists())
        self.assertEqual(config_path(self.ctx.state_dir).read_text(), before)

    def test_repos_set_changes_what_is_given_and_the_first_change_makes_the_file(self):
        self.worktrees_root()
        self.assertEqual(self.op(what="repos_set", name="docs", note="  文档\n手册 ", status="readonly", write_mode="worktree"),
                         ("done", "已改 docs：用途、状态、写法"))
        found = {repo.name: repo for repo in repomap.read(self.ctx.state_dir)}
        self.assertEqual(sorted(found), ["docs", "gone"])  # The old list's repositories came along.
        self.assertEqual((found["docs"].note, found["docs"].status, found["docs"].write_mode, found["docs"].added_by),
                         ("文档 手册", "readonly", "worktree", "migrate"))
        self.assertEqual((found["gone"].status, found["gone"].write_mode), ("active", "inplace"))
        docs = next(i for i in self.snap()["repos"]["items"] if i["name"] == "docs")
        self.assertEqual((docs["status"], docs["write_mode"], docs["note"]), ("readonly", "worktree", "文档 手册"))
        self.assertEqual(self.op(what="repos_set", name="docs", status="frozen"), ("done", "已改 docs：状态"))
        for payload, why in (({"name": "nope", "note": "x"}, "没有叫 nope 的常用路径"), ({"name": "docs"}, "没有要改的项"),
                             ({"name": "docs", "status": "gone"}, "状态要是 active、readonly 或 frozen"),
                             ({"name": "docs", "write_mode": "x"}, "写法要是 worktree 或 inplace"),
                             ({"name": "docs", "note": "x" * 201}, "note 要是不超过 200 个字的文字"),
                             ({"name": ""}, "要说明 name（最多 64 字）"), ({}, "要说明 name（最多 64 字）")):
            self.assertEqual(self.op(what="repos_set", **payload), ("failed", why), payload)
        self.assertEqual(self.op(operator="ou_other", what="repos_set", name="docs", note="x"), ("failed", web_settings.OWNER_ONLY))
        self.assertEqual(next(r for r in repomap.read(self.ctx.state_dir) if r.name == "docs").status, "frozen")

    def test_repos_add_takes_an_existing_absolute_directory_a_repository_or_a_plain_one_and_names_it(self):
        self.worktrees_root()
        extra = self.make_repo(self.root / "a" / "extra")
        self.assertEqual(self.op(what="repos_add", path=str(extra), note="新仓库"), ("done", "已加入常用路径：extra（仓库）"))
        added = next(r for r in repomap.read(self.ctx.state_dir) if r.name == "extra")
        self.assertEqual((added.path, added.note, added.status, added.added_by, added.kind, added.device),
                         (str(extra), "新仓库", "active", "owner", "git", ""))
        self.assertEqual(self.op(what="repos_add", path=str(extra)), ("done", "这个路径已在常用路径里：extra"))
        again = self.make_repo(self.root / "b" / "extra")
        self.assertEqual(self.op(what="repos_add", path=str(again)), ("done", "已加入常用路径：extra-2（仓库）"))
        self.assertEqual(self.op(what="repos_add", path=str(self.make_repo(self.root / "c" / "other")), name="extra")[0], "failed")
        self.assertEqual(self.op(what="repos_add", path=str(self.make_repo(self.root / "d" / "other")), name="mine"),
                         ("done", "已加入常用路径：mine（仓库）"))
        plain = self.root / "plain"
        plain.mkdir()
        (plain / "README.md").write_text("怎么用这些脚本\n")
        # No .git: a 目录, written in place; its README.md is its rules when it has no AGENTS.md or CLAUDE.md (T930 5.1).
        self.assertEqual(self.op(what="repos_add", path=str(plain)), ("done", "已加入常用路径：plain（目录）"))
        found = next(r for r in repomap.read(self.ctx.state_dir) if r.name == "plain")
        self.assertEqual((found.kind, found.write_mode, found.rules, found.device), ("dir", "inplace", ("README.md",), ""))
        self.assertEqual(next(i for i in self.snap()["repos"]["items"] if i["name"] == "plain")["kind"], "dir")
        self.assertEqual(self.op(what="repos_set", name="plain", write_mode="worktree"), ("failed", "目录和设备上的路径只能原地改"))
        self.assertEqual(self.op(what="repos_add", path=str(self.root / "nowhere")), ("failed", f"目录不存在：{self.root / 'nowhere'}"))
        self.assertEqual(self.op(what="repos_add", path="relative/dir")[0], "failed")
        self.assertEqual(self.op(what="repos_add", path=str(extra), name="a/b")[0], "failed")  # No path in a name.
        # A device's path with devices off is refused before anything is asked.
        self.assertEqual(self.op(what="repos_add", path="gpu1:/srv/x"), ("failed", "多设备没开（COVEY_DEVICES=1），加不了设备上的路径"))
        self.assertEqual(self.op(what="repos_add", path="/srv/x", device="gpu1")[0], "failed")
        self.assertEqual(self.op(what="repos_add", path="gpu1:~/x"), ("failed", "设备上的路径要写成 设备名:/绝对路径"))
        self.assertEqual(sorted(r.name for r in repomap.read(self.ctx.state_dir)), ["docs", "extra", "extra-2", "gone", "mine", "plain"])

    def test_repos_remove_leaves_the_directory_and_keeps_what_a_newer_release_wrote(self):
        self.worktrees_root()
        write_json(self.repos_file(), {"version": 1, "exclude": ["node_modules/"], "repos": [
            {"name": "docs", "path": str(self.root), "future": {"k": 1}}, {"name": "gone", "path": str(self.root / "nope")},
            {"name": "device", "path": "gpu1:~/x"}]})
        self.assertEqual(self.op(what="repos_set", name="docs", note="n"), ("done", "已改 docs：用途"))
        raw = read_json(self.repos_file())
        self.assertEqual(raw["exclude"], ["node_modules/"])
        self.assertEqual(next(r for r in raw["repos"] if r["name"] == "docs")["future"], {"k": 1})
        self.assertIn({"name": "device", "path": "gpu1:~/x"}, raw["repos"])  # What read() passes over stays as it was.
        raw["repos"][0]["status"] = "archived"  # A newer release's status: set on the page, the page's from then on.
        write_json(self.repos_file(), raw)
        self.assertEqual(self.op(what="repos_set", name="docs", status="active"), ("done", "已改 docs：状态"))
        self.assertEqual(next(r for r in read_json(self.repos_file())["repos"] if r["name"] == "docs")["status"], "active")
        self.assertEqual(self.op(what="repos_remove", name="docs"), ("done", "已从常用路径去掉 docs，目录没有动"))
        self.assertTrue(self.root.is_dir())
        self.assertEqual(self.op(what="repos_remove", name="docs"), ("failed", "没有叫 docs 的常用路径"))
        self.assertEqual(sorted(r.name for r in repomap.read(self.ctx.state_dir)), ["gone"])
        self.assertEqual(self.op(what="repos_remove", name="")[0], "failed")

    def test_a_map_that_cannot_be_read_is_never_written_over(self):
        self.worktrees_root()
        self.repos_file().write_text("{not json")
        self.assertEqual(self.op(what="repos_set", name="docs", note="x"), ("failed", "常用路径（repos.json）读不出来，没有改动"))
        self.assertEqual(self.repos_file().read_text(), "{not json")
        self.repos_file().write_text("[1, 2]")
        self.assertEqual(self.op(what="repos_remove", name="docs"), ("failed", "常用路径（repos.json）的格式不对，没有改动"))
        self.assertEqual(self.repos_file().read_text(), "[1, 2]")
        # A turn's settle leaves it too (core/repomap.py note_used), no error.
        repomap.note_used(self.ctx.state_dir, [SimpleNamespace(name="x", path="/w/x")])
        self.assertEqual(self.repos_file().read_text(), "[1, 2]")

    def test_every_change_of_the_map_is_repomaps_under_its_lock_and_renders_repos_md(self):
        """Seam W2 ↔ W1 (T870): the page's changes and the old workspace ones mirrored into the map are written by core/repomap.py
        ``update`` (its repos.json.lock, the one the turn's note_used holds) and repos.md is rendered after each."""
        self.worktrees_root()
        rendered = repomap.rendered_path(self.ctx.state_dir, self.web.context().default_workdir)
        locked = []
        real = repomap._locked

        def counted(state):
            locked.append(Path(state))
            return real(state)
        extra = self.make_repo(self.root / "r" / "extra")
        mirrored = self.make_repo(self.root / "m" / "mirrored")
        with mock.patch.object(repomap, "_locked", side_effect=counted):
            self.assertEqual(self.op(what="repos_set", name="docs", note="文档"), ("done", "已改 docs：用途"))
            self.assertIn("| docs | `" + str(self.root) + "` | 本机 | 仓库 | 文档 |", rendered.read_text())
            self.assertEqual(self.op(what="repos_add", path=str(extra)), ("done", "已加入常用路径：extra（仓库）"))
            self.assertIn("| extra |", rendered.read_text())
            self.assertEqual(self.op(what="repos_remove", name="extra")[0], "done")
            self.assertNotIn("| extra |", rendered.read_text())
            self.assertEqual(self.op(what="workspace_add", path=str(mirrored))[0], "done")
            self.assertIn("| mirrored |", rendered.read_text())
            self.assertEqual(self.op(what="workspace_remove", workspace="mirrored")[0], "done")
            self.assertNotIn("| mirrored |", rendered.read_text())
        self.assertEqual(locked, [self.ctx.state_dir] * 5)
        self.assertTrue((self.ctx.state_dir / "repos.json.lock").exists())
        self.assertFalse((self.ctx.state_dir / ".locks" / "repos.json.lock").exists())  # No second lock of the same file.

    def test_a_topic_running_in_a_repository_is_counted_and_one_elsewhere_is_not(self):
        self.worktrees_root()
        upsert_job_record(self.ctx.state_dir, "k1", status="running", topic_id="t1", workdir=str(self.root / "sub"))
        upsert_job_record(self.ctx.state_dir, "k2", status="running", topic_id="t2", workdir=str(self.root))
        upsert_job_record(self.ctx.state_dir, "k3", status="running", topic_id="t2", workdir=str(self.root))
        upsert_job_record(self.ctx.state_dir, "k4", status="done", topic_id="t3", workdir=str(self.root))
        upsert_job_record(self.ctx.state_dir, "k5", status="running", topic_id="t4", workdir="/elsewhere/project")
        items = {i["name"]: i for i in self.snap()["repos"]["items"]}
        self.assertEqual((items["docs"]["live_topics"], items["gone"]["live_topics"]), (2, 0))

    def make_worktree(self, root, rel, repo):
        """A real worktree of ``repo`` at ``root/rel`` (the main repository is made once, under the test's root)."""
        main = self.root / "mains" / repo
        if not main.exists():
            self.make_repo(main)
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        self.git("worktree", "add", "-q", "-b", f"topic/{rel.replace('/', '-')}", str(target), cwd=main)
        return target

    def make_topic(self, topic_id, number, status):
        write_json(self.ctx.state_dir / "topics" / topic_id / "topic.json", {"chat_id": "oc_dm", "topic_number": number})
        upsert_job_record(self.ctx.state_dir, "job-" + topic_id[:8], status=status, topic_id=topic_id)

    def test_the_worktree_list_offers_only_those_whose_topic_ended_over_a_day_ago(self):
        root = self.worktrees_root()
        (self.ctx.state_dir / "topics").mkdir(parents=True, exist_ok=True)
        old = self.make_worktree(root, "T12-aaaa1111/covey", "covey")
        self.make_worktree(root, "T13-bbbb2222/covey", "covey")  # Its topic still runs.
        self.make_worktree(root, "T14-cccc3333/covey", "covey")  # No topic by that id: not told.
        legacy = self.make_worktree(root, "covey-20261120-101010-dddd4444", "covey")
        self.make_topic("aaaa1111-1", 12, "done")
        self.make_topic("bbbb2222-2", 13, "running")
        self.make_topic("dddd4444-3", 9, "failed")
        (root / "stray").mkdir()  # Not a worktree, not counted.
        (root / "T99-eeee5555").mkdir()  # A new-layout directory with nothing in it.
        now = time.time()
        page = self.web.snapshot("dm", now=now)["repos"]["worktrees"]
        self.assertEqual((page["total"], page["prunable"]), (4, []))  # Ended just now: not yet a day.
        page = self.web.snapshot("dm", now=now + 2 * 86400 + 60)["repos"]["worktrees"]
        self.assertEqual(page["total"], 4)
        found = {w["tail"]: w for w in page["prunable"]}
        self.assertEqual(sorted(found), ["T12-aaaa1111/covey", "covey-20261120-101010-dddd4444"])
        first = found["T12-aaaa1111/covey"]
        self.assertEqual((first["repo"], first["branch"], first["topic_number"]), ("covey", "topic/T12-aaaa1111-covey", 12))
        self.assertAlmostEqual(first["ended_at"], now, delta=60)
        self.assertEqual(found["covey-20261120-101010-dddd4444"]["topic_number"], 9)
        self.assertTrue(old.is_dir() and legacy.is_dir())  # Listing removes nothing.

    def test_worktrees_prune_removes_only_those_named_and_only_clean_worktrees_under_the_root(self):
        root = self.worktrees_root()
        clean = self.make_worktree(root, "T12-aaaa1111/covey", "covey")
        dirty = self.make_worktree(root, "T13-bbbb2222/covey", "covey")
        (dirty / "work.txt").write_text("unsaved")
        by_tail = self.make_worktree(root, "T14-cccc3333/covey", "covey")
        not_named = self.make_worktree(root, "T15-dddd4444/covey", "covey")
        outside = self.make_worktree(self.root / "elsewhere", "T16-eeee5555/covey", "covey")
        own_git = self.make_repo(root / "plain-repo")  # Under the root, but a repository: its .git is a directory.
        link = root / "link"
        link.symlink_to(not_named.parent)
        state, words = self.op(what="worktrees_prune", paths=[str(clean), str(dirty), "T14-cccc3333/covey", str(outside),
                                                               str(own_git), str(link), "../x", str(root / "T99-zzzz0000/covey")])
        self.assertEqual(state, "done")
        lines = words.splitlines()
        self.assertEqual(lines[0], "要删 8 个 worktree，删掉 2 个：")
        self.assertTrue(lines[1].endswith("：已删除") and "T12-aaaa1111/covey" in lines[1])
        self.assertIn("有未提交的改动或被锁住，没删", lines[2])
        self.assertTrue(lines[3].endswith("：已删除"))
        self.assertTrue(all("不在 worktree 目录下" in line for line in (lines[4], lines[7])))
        self.assertIn("不是 git worktree", lines[5])
        self.assertIn("路径是个链接", lines[6])
        self.assertIn("不是 git worktree", lines[8])
        self.assertFalse(clean.exists() or by_tail.exists())
        for kept in (dirty, not_named, outside, own_git):  # Not named, refused or outside: untouched.
            self.assertTrue(kept.exists(), kept)
        self.assertTrue((dirty / "work.txt").exists())
        self.assertTrue(link.is_symlink() and (not_named / ".git").is_file())
        branches = subprocess.run(["git", "branch", "--list"], cwd=self.root / "mains" / "covey", capture_output=True, text=True).stdout
        self.assertIn("topic/T12-aaaa1111-covey", branches)  # The branch is the owner's: only the directory goes.

    def test_worktrees_prune_with_nothing_removed_is_a_refusal_and_a_bad_list_is_refused(self):
        root = self.worktrees_root()
        dirty = self.make_worktree(root, "T13-bbbb2222/covey", "covey")
        (dirty / "w.txt").write_text("x")
        state, words = self.op(what="worktrees_prune", paths=[str(dirty)])
        self.assertEqual(state, "failed")
        self.assertIn("删掉 0 个", words)
        self.assertTrue(dirty.exists())
        for paths in ("x", [], [1], [""], ["a" * 501], ["a"] * 0 + [f"p{n}" for n in range(51)]):
            self.assertEqual(self.op(what="worktrees_prune", paths=paths)[0], "failed", paths)
        self.assertEqual(self.op(operator="ou_other", what="worktrees_prune", paths=[str(dirty)]), ("failed", web_settings.OWNER_ONLY))
        self.assertTrue(dirty.exists())

    def test_the_old_workspace_changes_still_come_in_and_follow_into_the_map(self):
        self.worktrees_root()
        extra = self.make_repo(self.root / "g" / "extra")
        self.assertEqual(self.op(what="workspace_add", path=str(extra)), ("done", "已添加工作区 extra，也加进了常用路径"))
        self.assertIn("extra", [r.name for r in repomap.read(self.ctx.state_dir)])
        self.assertIn("extra", [w["name"] for w in self.snap()["workspace"]["items"]])  # The old page has it too.
        self.assertEqual(self.op(what="workspace_add", path=str(extra)), ("done", "该目录已在列表中：extra"))
        self.assertEqual([r.name for r in repomap.read(self.ctx.state_dir)].count("extra"), 1)
        self.assertEqual(self.op(what="workspace_remove", workspace="extra"), ("done", "已从列表移除 extra，磁盘目录未删除"))
        self.assertNotIn("extra", [r.name for r in repomap.read(self.ctx.state_dir)])
        self.assertTrue(extra.is_dir())
        self.assertEqual(self.op(what="workspace_prune"), ("done", "已从列表移除 1 项，磁盘目录未删除"))  # 'gone'
        self.assertNotIn("gone", [r.name for r in repomap.read(self.ctx.state_dir)])
        self.assertIn("docs", [r.name for r in repomap.read(self.ctx.state_dir)])

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
        # v24: the same count by local day and by the page's ranges, for its 用量 page.
        self.assertEqual(len(found["history"]["days"]), weekly_usage.HISTORY_DAYS)
        self.assertEqual([span["days"] for span in found["history"]["spans"]], list(weekly_usage.SPANS))
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

    # -- 网页开新话题 (data contract v16, T554 round three) -----------------------------------------------------------

    def test_the_launch_catalog_is_the_launch_cards_reading(self):
        data = self.snap()["launch"]
        if FIXTURE:
            with open(FIXTURE.replace(".json", "-launch.json"), "w") as out:
                json.dump(data, out, ensure_ascii=False, indent=1)
        keys = [m["key"] for m in data["models"]]
        self.assertIn(data["preselect"]["model"], keys)
        for model in data["models"]:
            self.assertIn(model["start_budget"], [b["value"] for b in model["budgets"]], model["key"])
            # The budgets by name, as the 设置 card lists them (T615): no 推荐, no 模型原生默认; the default one is a level too.
            self.assertNotIn(RECOMMENDED, [b["value"] for b in model["budgets"]], model["key"])
            self.assertNotIn("推荐", " ".join(b["text"] for b in model["budgets"]), model["key"])
            self.assertIn(model["default_budget"], [b["value"] for b in model["budgets"]], model["key"])
            self.assertTrue(model["harness_title"])
        chosen = next(m for m in data["models"] if m["key"] == data["preselect"]["model"])
        self.assertIn(data["preselect"]["effort"], [b["value"] for b in chosen["budgets"]])
        # v43: a new topic starts in the home; the catalog has no workspace to preselect and no run modes (app/web_launch.py).
        self.assertNotIn("workspace", data["preselect"])
        self.assertNotIn("run_modes", data)

    def launch(self, op_id="op-l1", **payload):
        access = self.app.runtime.access
        with mock.patch.object(access, "owner_chat", return_value="oc_dm"):
            return self.ops.run({"kind": "launch", "op_id": op_id, "payload": payload})

    def test_a_launch_from_the_page_opens_a_topic_that_starts_at_once_and_is_remembered(self):
        from covey.app.launch import launch_defaults
        from covey.core.jobs import find_job_record
        catalog = self.snap()["launch"]
        model = catalog["models"][-1]
        sent = []
        self.app.messenger.create_and_send_card = mock.Mock(side_effect=lambda *a, **k: (sent.append(a[2]), ("card1", "om_root1"))[1])
        state, words = self.launch(text="把账单整理一下\n第二行", model=model["key"], effort=model["start_budget"], workspace="docs",
                                   run_mode="direct", redaction="off")
        self.assertEqual(state, "done", words)
        self.assertRegex(words, r"已开 .*「把账单整理一下」：直接开工")
        record = json.loads((self.ctx.state_dir / "web" / "launches" / "op-l1.json").read_text())
        topic = json.loads((self.ctx.state_dir / "topics" / record["topic_id"] / "topic.json").read_text())
        self.assertEqual((topic["harness_id"], topic["workspace_name"], topic["origin"]["kind"]),
                         (model["harness"], "docs", "web"))
        job = find_job_record(self.ctx.state_dir, "web-op-l1")
        self.assertEqual(job["topic_id"], record["topic_id"])
        self.assertEqual(launch_defaults(self.ctx.state_dir, "oc_dm")["model_choice"], model["key"])
        self.assertEqual(self.snap()["launch"]["preselect"]["model"], model["key"])  # The next one preselects it.
        # The same operation again: the same topic, nothing sent again.
        self.assertEqual(self.launch(text="把账单整理一下", model=model["key"], effort=model["start_budget"], workspace="docs"),
                         ("done", words))
        self.assertEqual(len(sent), 1)

    def test_a_home_launch_from_the_page_writes_the_topics_start_and_slug_once(self):
        """Seam W2 ↔ W1 (T870): the page's home start goes through app/butler_topics.py bind_topic, which marks it as every home
        start is marked (core/topics.py mark_home): ``home_start``, ``started_at`` the launch's time, ``slug`` of its title."""
        model = self.snap()["launch"]["models"][-1]
        self.app.messenger.create_and_send_card = mock.Mock(return_value=("card2", "om_root2"))
        state, words = self.launch("op-h1", text="fix the README links", model=model["key"], effort=model["start_budget"],
                                   redaction="off")
        self.assertEqual(state, "done", words)
        record = json.loads((self.ctx.state_dir / "web" / "launches" / "op-h1.json").read_text())
        topic = json.loads((self.ctx.state_dir / "topics" / record["topic_id"] / "topic.json").read_text())
        self.assertEqual((topic["home_start"], topic["started_at"], topic["slug"]), (True, record["at"], "fix-the-readme-links"))

    def test_a_launch_with_files_gives_the_first_turn_its_attachments_and_one_that_cannot_be_taken_opens_nothing(self):
        """T616 21:46 (data contract v25): the new-topic page's + and pasted screenshots go with the first turn, as a say's files do."""
        import base64
        from covey.core.jobs import find_job_record
        from covey.app.web_push_client import WebError
        model = self.snap()["launch"]["models"][0]
        uploads = {"a" * 32: ("图.png", "image/png", b"png-bytes"), "b" * 32: ("计划.md", "text/markdown", "- 一\n".encode())}

        def fetch(method, path):
            query = dict(part.split("=", 1) for part in path.split("?", 1)[1].split("&"))
            if query["id"] not in uploads:
                raise WebError("http 404", 404)
            name, mime, data = uploads[query["id"]]
            if path.startswith("/sync/upload/part"):
                return {"n": 0, "data": base64.b64encode(data).decode()}
            return {"id": query["id"], "name": name, "mime": mime, "size": len(data), "parts": 1, "done": True}
        self.ops.fetch = fetch
        sent = []
        self.app.messenger.create_and_send_card = mock.Mock(side_effect=lambda *a, **k: (sent.append(a[2]), ("card1", "om_rootf"))[1])
        base = dict(text="看看这两个", model=model["key"], effort=model["start_budget"], workspace="docs", run_mode="direct", redaction="off")
        state, words = self.launch("op-lf0", **base, files=[{"id": "c" * 32, "name": "过期.pdf", "mime": "application/pdf"}])
        self.assertEqual((state, words), ("failed", "附件 过期.pdf 取不到：已过期或没传完"))
        self.assertEqual(sent, [])  # Nothing opened.
        state, words = self.launch("op-lf1", **base, files=[{"id": "a" * 32, "name": "图.png", "mime": "image/png"},
                                                            {"id": "b" * 32, "name": "计划.md", "mime": "text/markdown"}])
        self.assertEqual(state, "done", words)
        record = json.loads((self.ctx.state_dir / "web" / "launches" / "op-lf1.json").read_text())
        media = self.ctx.state_dir / "topics" / record["topic_id"] / "incoming_media"
        saved = sorted(media.iterdir())
        self.assertEqual([p.read_bytes() for p in saved], [b"png-bytes", "- 一\n".encode()])
        job = find_job_record(self.ctx.state_dir, "web-op-lf1")
        history = (self.ctx.state_dir / "topics" / record["topic_id"] / "history.jsonl").read_text()
        self.assertIn("收到的附件（已下载到本地）", history)
        self.assertIn(str(saved[0]), history)
        self.assertEqual(job["topic_id"], record["topic_id"])
        self.assertFalse((self.ctx.state_dir / "web" / "launches" / "op-lf1.files").exists())  # Moved, and the empty folder gone.

    def test_a_launch_is_checked_as_the_cards_start_checks_it(self):
        model = self.snap()["launch"]["models"][0]
        base = dict(text="x", model=model["key"], effort=model["start_budget"], workspace="docs", run_mode="direct")
        self.assertEqual(self.launch("op-b1", **{**base, "model": "nope:none"}), ("failed", "这个模型已不在可选目录中，请重新选"))
        self.assertEqual(self.launch("op-b2", **{**base, "effort": "nope"}), ("failed", "这个模型没有这一档思考预算，请重新选"))
        self.assertEqual(self.launch("op-b3", **{**base, "workspace": "elsewhere"}), ("failed", "没有登记工作区「elsewhere」，请重新选"))
        self.assertEqual(self.launch("op-b4", **{**base, "text": " "}), ("failed", "要先写这个话题做什么"))
        # 敏感词脱敏 was taken out (T947): the catalog says nothing of it, and an older page's choice of it is ignored.
        self.assertNotIn("redaction", model)
        self.assertNotIn("redaction", self.snap()["launch"]["preselect"])
        self.app.messenger.create_and_send_card = mock.Mock(return_value=("card1", "om_root5"))
        self.assertEqual(self.launch("op-b5", **{**base, "redaction": "on"})[0], "done")

    def test_the_topic_a_launch_opened_is_built_on_the_next_push(self):
        """2026-11-20 live (T576): the page waited over a minute for the topic it had just opened; the inbox now touches it."""
        model = self.snap()["launch"]["models"][0]
        self.app.messenger.create_and_send_card = mock.Mock(return_value=("card1", "om_root9"))
        with mock.patch.dict(os.environ, {web_sync.ENV: "1"}):
            sync = WebSync(self.app.runtime, self.env)
        outbox = [{"seq": 1, "op_id": "op-l9", "kind": "launch", "topic_key": "", "project_key": "",
                   "payload": {"text": "看一眼", "model": model["key"], "effort": model["start_budget"], "workspace": "docs",
                               "run_mode": "direct", "redaction": "off"}}]
        sync.client.request = lambda method, path, body=None: {"items": outbox} if path.startswith("/sync/inbox") else {"ok": True}
        with mock.patch.object(self.app.runtime.access, "owner_chat", return_value="oc_dm"):
            sync.inbox_once()
        record = json.loads((self.ctx.state_dir / "web" / "launches" / "op-l9.json").read_text())
        self.assertIn(record["topic_id"], sync.collector.dirty)

    # -- 网页换配置 (data contract v21, T616): the topic page's 工作区 · Harness · 模型 line is 🔁 换配置 ------------------------

    def opened(self, model, done=True):
        """A topic the page opened with ``model`` in 'docs', its first task done (else queued); (topic id, its topic.json path)."""
        from covey.core.jobs import find_job_record, upsert_job_record
        self.app.messenger.create_and_send_card = mock.Mock(return_value=("card1", "om_root1"))
        state, words = self.launch("op-s0", text="整理账单", model=model["key"], effort=model["start_budget"], workspace="docs",
                                   run_mode="direct", redaction="off")
        self.assertEqual(state, "done", words)
        job = find_job_record(self.ctx.state_dir, "web-op-s0")
        if done:
            upsert_job_record(self.ctx.state_dir, job["key"], status="done")
        topic = job["topic_id"]
        return topic, self.ctx.state_dir / "topics" / topic / "topic.json"

    def switch(self, topic, op_id="op-s1", **payload):
        """The page's ``switch``; the first task's job is no longer in the in-memory queue (it ran)."""
        access = self.app.runtime.access
        with mock.patch.object(access, "owner_chat", return_value="oc_dm"), \
                mock.patch.object(self.app.topics.runtime.jobs, "in_topic", return_value=False):
            return self.ops.run({"kind": "switch", "op_id": op_id, "topic_key": topic, "payload": payload})

    def test_a_switch_from_the_page_rebinds_the_topic_and_says_so_in_feishu(self):
        from covey.app import web_switch
        models = self.snap()["launch"]["models"]
        first = models[0]
        other = next(m for m in models if m["key"] != first["key"])
        topic, path = self.opened(first)
        before = json.loads(path.read_text())
        config = web_switch.topic_config(before)
        self.assertEqual((config["model"], config["workspace"], config["run_mode"]), (first["key"], "docs", "direct"))
        replies = []
        for app in self.app.topics.apps.values():
            app.messenger.reply = mock.Mock(side_effect=lambda *a, **k: replies.append(a[2]))
        self.assertEqual(self.switch(topic, op_id="op-s9", **config), ("failed", "配置没有变化"))
        state, words = self.switch(topic, model=other["key"], effort=other["start_budget"], work_mode="", workspace="project",
                                   run_mode="direct", redaction="off")
        self.assertEqual(state, "done", words)
        self.assertRegex(words, r"^已换配置：.* → .*；工作区 docs → project，下一句话起由新会话接着做$")
        after = json.loads(path.read_text())
        self.assertEqual((after["harness_id"], after["workspace_name"], after["binding_revision"]),
                         (other["harness"], "project", before.get("binding_revision", 1) + 1))
        self.assertEqual(len(replies), 1)
        self.assertIn("🔁 已换配置", replies[0])
        self.assertEqual(web_switch.topic_config(after)["model"].split(":")[0], other["harness"])

    def test_a_switch_names_the_topics_device_and_never_moves_it_to_another(self):
        """T879: the page's 换配置 drew a 执行设备 choice it showed as testbox for a topic on this machine, and the switch dropped
        it in silence. ``config`` says where the topic works ("" for 本机, a device's alias), and a ``device`` other than that
        is refused, the topic unchanged; none or "" (the topic's own) is no change of place."""
        from covey.app import web_switch
        models = self.snap()["launch"]["models"]
        first = models[0]
        other = next(m for m in models if m["key"] != first["key"])
        topic, path = self.opened(first)
        before = json.loads(path.read_text())
        self.assertEqual(web_switch.topic_config(before)["device"], "")
        self.assertEqual(web_switch.topic_config({**before, "workdir": "gpu1:/home/me/work"})["device"], "gpu1")
        choice = dict(model=other["key"], effort=other["start_budget"], work_mode="", redaction="off")
        self.assertEqual(self.switch(topic, "op-d1", device="gpu1", **choice), ("failed", web_switch.DEVICE_FIXED))
        self.assertEqual(json.loads(path.read_text())["harness_id"], before["harness_id"])
        state, words = self.switch(topic, "op-d2", device="", **choice)
        self.assertEqual(state, "done", words)
        self.assertEqual(json.loads(path.read_text())["harness_id"], other["harness"])

    def repos_map(self):
        write_json(self.repos_file(), {"version": 1, "repos": [
            {"name": "covey", "path": str(self.root / "covey")}, {"name": "web", "path": str(self.root / "web"), "status": "readonly"}]})

    def same_config(self, path):
        from covey.app import web_switch
        config = web_switch.topic_config(json.loads(path.read_text()))
        return {key: config[key] for key in ("model", "effort", "work_mode")}

    def test_a_switch_sets_the_topics_repos_it_adds_changes_clears_or_leaves_them(self):
        """Seam W2 ↔ WEB (T870, data contract v43 §4 ``repos?``): ``repos`` sets the topic's repositories, checked as a new topic's
        are; repositories alone are a change (no rebind: the same binding_revision and session); ``[]`` clears them; a payload
        without ``repos`` leaves them as they are."""
        self.repos_map()
        model = self.snap()["launch"]["models"][0]
        topic, path = self.opened(model)
        replies = []
        for app in self.app.topics.apps.values():
            app.messenger.reply = mock.Mock(side_effect=lambda *a, **k: replies.append(a[2]))
        same, before = self.same_config(path), json.loads(path.read_text())
        # Added.
        self.assertEqual(self.switch(topic, "op-p1", **same, repos=[{"name": "covey", "mode": "write"}, {"name": "web", "mode": "read"}]),
                         ("done", "已换配置：仓库 无 → covey（改）、web，会话不变"))
        after = json.loads(path.read_text())
        self.assertEqual([(r["name"], r["mode"], r["source"], r["path"]) for r in after["repos"]],
                         [("covey", "write", "declared", str(self.root / "covey")), ("web", "read", "declared", str(self.root / "web"))])
        self.assertEqual((after.get("binding_revision"), after["harness_id"], after["model"]),
                         (before.get("binding_revision"), before["harness_id"], before["model"]))
        self.assertIn("covey（改）、web", replies[-1])  # Feishu's thread hears of it too.
        # Changed: the one kept keeps what it had (its worktree), with the mode asked; the other goes.
        data = json.loads(path.read_text())
        data["repos"][0]["worktree"] = "/wt/T1-x/covey"
        write_json(path, data)
        self.assertEqual(self.switch(topic, "op-p2", **same, repos=[{"name": "covey", "mode": "read"}]),
                         ("done", "已换配置：仓库 covey（改）、web → covey，会话不变"))
        self.assertEqual([(r["name"], r["mode"], r.get("worktree")) for r in json.loads(path.read_text())["repos"]],
                         [("covey", "read", "/wt/T1-x/covey")])
        self.assertEqual(self.switch(topic, "op-p3", **same, repos=[{"name": "covey", "mode": "read"}]), ("failed", "配置没有变化"))
        # Refused as a new topic's would be: nothing written.
        self.assertEqual(self.switch(topic, "op-p4", **same, repos=[{"name": "web", "mode": "write"}]),
                         ("failed", "仓库「web」只读，不能改；只读就把 mode 写成 read"))
        self.assertEqual(self.switch(topic, "op-p5", **same, repos=[{"name": "nope", "mode": "read"}]),
                         ("failed", "仓库地图里没有「nope」，可选：covey、web"))
        self.assertEqual(self.switch(topic, "op-p6", **same, repos="covey")[0], "failed")
        self.assertEqual([r["name"] for r in json.loads(path.read_text())["repos"]], ["covey"])
        # Left out: a model change alone keeps them.
        other = next(m for m in self.snap()["launch"]["models"] if m["key"] != model["key"])
        state, words = self.switch(topic, "op-p7", model=other["key"], effort=other["start_budget"], work_mode="", redaction="off")
        self.assertEqual(state, "done", words)
        self.assertNotIn("仓库", words)
        moved = json.loads(path.read_text())
        self.assertEqual(([r["name"] for r in moved["repos"]], moved["binding_revision"]), (["covey"], before.get("binding_revision", 1) + 1))
        # With a model change: one rebind, one receipt.
        state, words = self.switch(topic, "op-p8", model=model["key"], effort=model["start_budget"], work_mode="", redaction="off",
                                   repos=[{"name": "covey", "mode": "write"}])
        self.assertEqual(state, "done", words)
        self.assertRegex(words, r"^已换配置：.* → .*；仓库 covey → covey（改），下一句话起由新会话接着做$")
        self.assertEqual(json.loads(path.read_text())["repos"][0]["mode"], "write")
        # Cleared.
        self.assertEqual(self.switch(topic, "op-p9", **self.same_config(path), repos=[]), ("done", "已换配置：仓库 covey（改） → 无，会话不变"))
        self.assertEqual(json.loads(path.read_text())["repos"], [])
        self.assertEqual(self.switch(topic, "op-pa", **self.same_config(path), repos=[]), ("failed", "配置没有变化"))

    def test_the_repos_a_page_sends_back_with_every_switch_are_not_checked_again_and_those_it_never_saw_stay(self):
        """The page sends the topic's repositories back with every switch (web/client/src/cv/launchRepos.ts switchPayload): one
        the map has frozen since, or never had, does not stop a model change; the entries past the REPOS_KEPT the page was
        shown are kept, worktree and all (T870 review)."""
        from covey.app.web_turns import REPOS_KEPT
        self.repos_map()
        model = self.snap()["launch"]["models"][0]
        topic, path = self.opened(model)
        data = json.loads(path.read_text())
        data["repos"] = [{"name": "covey", "path": str(self.root / "covey"), "mode": "write", "worktree": "/wt/T1-x/covey",
                          "source": "declared"},
                         {"name": "notes", "path": "/absolute/notes", "mode": "read", "source": "touched"},
                         *({"name": f"r{i}", "path": f"/absolute/r{i}", "mode": "read", "source": "touched"} for i in range(REPOS_KEPT))]
        write_json(path, data)
        raw = json.loads(self.repos_file().read_text())
        raw["repos"][0]["status"] = "frozen"  # covey, frozen on the settings page since the topic took it to write.
        write_json(self.repos_file(), raw)
        sent = [{"name": r["name"], "mode": r["mode"]} for r in data["repos"][:REPOS_KEPT]]  # What the page was shown.
        other = next(m for m in self.snap()["launch"]["models"] if m["key"] != model["key"])
        state, words = self.switch(topic, "op-k1", model=other["key"], effort=other["start_budget"], work_mode="", redaction="off",
                                   repos=sent)
        self.assertEqual(state, "done", words)
        self.assertNotIn("仓库", words)
        self.assertEqual(json.loads(path.read_text())["repos"], data["repos"])  # Nothing of them changed, the unseen two too.
        # One taken out on the page: it goes; the two it never saw stay, after the ones it sent.
        state, words = self.switch(topic, "op-k2", **self.same_config(path), repos=[r for r in sent if r["name"] != "notes"])
        self.assertEqual(state, "done", words)
        names = [r["name"] for r in json.loads(path.read_text())["repos"]]
        self.assertEqual(names, ["covey", *(f"r{i}" for i in range(REPOS_KEPT))])
        self.assertEqual(json.loads(path.read_text())["repos"][0]["worktree"], "/wt/T1-x/covey")
        # Writing to it again once it was made read only: checked, refused.
        sent = [{"name": "covey", "mode": "read"}, *({"name": f"r{i}", "mode": "read"} for i in range(REPOS_KEPT - 1))]
        self.assertEqual(self.switch(topic, "op-k3", **self.same_config(path), repos=sent)[0], "done")
        self.assertEqual(self.switch(topic, "op-k4", **self.same_config(path), repos=[{"name": "covey", "mode": "write"}, *sent[1:]]),
                         ("failed", "仓库「covey」已冻结，不能改；只读就把 mode 写成 read"))

    def test_a_switch_of_the_repos_alone_waits_for_the_running_task_too(self):
        self.repos_map()
        model = self.snap()["launch"]["models"][0]
        topic, path = self.opened(model, done=False)
        state, words = self.switch(topic, "op-q1", **self.same_config(path), repos=[{"name": "covey", "mode": "read"}])
        self.assertEqual(state, "failed")
        self.assertIn("还有任务在排队或执行，请先停止", words)
        self.assertNotIn("repos", json.loads(path.read_text()))

    def test_a_switch_is_refused_while_a_task_runs_for_a_new_worktree_and_for_choices_not_offered(self):
        model = self.snap()["launch"]["models"][0]
        topic, _ = self.opened(model, done=False)
        base = dict(model=model["key"], effort=model["start_budget"], workspace="docs", run_mode="direct", redaction="off")
        self.assertEqual(self.switch(topic, "op-r1", **{**base, "run_mode": "worktree"}), ("failed", web_switch_words.WORKTREE_ON_CARD))
        self.assertEqual(self.switch(topic, "op-r2", **{**base, "model": "nope:none"}), ("failed", "这个模型已不在可选目录中，请重新选"))
        self.assertEqual(self.switch(topic, "op-r3", **{**base, "workspace": "elsewhere"}), ("failed", "没有登记工作区「elsewhere」，请重新选"))
        self.assertEqual(self.switch("f" * 32, "op-r4", **base)[0], "failed")
        state, words = self.switch(topic, "op-r5", **{**base, "workspace": "project"})
        self.assertEqual(state, "failed")
        self.assertIn("还有任务在排队或执行，请先停止", words)
        with mock.patch.object(self.app.runtime.access, "is_owner", return_value=False):
            self.assertEqual(self.switch(topic, "op-r6", **base), ("failed", "只有主人能换配置"))


class DevicesPageTest(SettingsCase):
    """F1 (T930 第 7 节): the 设置 page's 设备 tab with devices on, even without nodes: devices.json and the node registry as one row a
    machine; 添加 checked first (off the inbox), 改, 移除 (records kept, the node revoked), 体检; and 常用路径 on a device."""

    def setUp(self):
        super().setUp()
        from covey.app import web_panels
        from covey.core import devices
        self.devices, self.panels = devices, web_panels
        self.file = Path(self.root) / "devices.json"
        self.file.write_text(json.dumps({"schema": 1, "devices": {
            "macmini": {"title": "Mac mini", "ssh": {"host": "me@mac", "options": ["-p", "22"]}, "access": "write", "note": "构建"},
            "testbox": {"title": "testbox", "ssh": {"host": "tb"}, "harnesses": ["claude"]}}}))
        patcher = mock.patch.dict(os.environ, {"COVEY_DEVICES": "1", "COVEY_DEVICES_FILE": str(self.file)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.started = []
        start = mock.patch.object(web_panels, "_start", side_effect=lambda start, work, name: self.started.append(work))
        start.start()
        self.addCleanup(start.stop)
        self.raw = lambda: json.loads(self.file.read_text())

    op = WebSettingsTest.op

    def checked(self, ok=True, text="体检通过"):
        return mock.patch("covey.core.device_probe.checkup", return_value=SimpleNamespace(ok=ok, explain=lambda: text))

    def test_the_tab_shows_with_devices_on_and_joins_devices_json_and_the_nodes_by_alias(self):
        from covey.core import nodes
        state = self.ctx.state_dir
        write_json(nodes.nodes_dir(state) / "registry.json", {"schema": 1, "nodes": {"local": {"title": "本机"}, "macmini": {"title": "mac"},
                                                                                     "nas": {"title": "NAS"}}})
        for alias in ("local", "macmini", "nas"):
            nodes.write_link(state, alias, connected=True, heard=time.time())
        write_json(self.devices.device_dir(state, "macmini") / "health.json", {"at": time.time() - 120, "ok": True})
        write_json(self.devices.device_dir(state, "testbox") / "health.json", {"at": time.time(), "ok": False, "error": "Unreachable"})
        write_json(self.devices.device_dir(state, "macmini") / "profile.json",
                   {"check": {"at": time.time(), "ok": True, "text": "macmini 体检通过", "steps": 3}})
        data = self.snap()
        self.assertIn("设备", [t["label"] for t in data["tabs"]])  # No COVEY_NODE_LISTEN: shown for the devices.
        page = data["nodes"]
        rows = {row["alias"]: row for row in page["rows"]}
        self.assertEqual(list(rows), ["local", "macmini", "nas", "testbox"])
        mac = rows["macmini"]
        self.assertEqual((mac["title"], mac["note"], mac["access"], mac["ssh"], mac["runs"], mac["listed"]),
                         ("Mac mini", "构建", "write", "-p 22 me@mac", True, True))
        self.assertEqual(mac["link"]["state"], "ok")
        self.assertIn("连得上", mac["link"]["text"])
        self.assertEqual((mac["check"]["ok"], mac["check"]["steps"]), (True, 3))
        self.assertIsNotNone(mac["node"])
        self.assertEqual((rows["testbox"]["link"]["state"], rows["testbox"]["access"], rows["testbox"]["node"]), ("down", "", None))
        self.assertIn("ssh 连不上", rows["testbox"]["link"]["text"])
        self.assertEqual((rows["nas"]["listed"], rows["nas"]["title"], rows["nas"]["link"]), (False, "NAS", None))
        self.assertTrue(rows["local"]["local"])
        self.assertEqual(page["summary"], "3 台设备 · 可写 1 台 · 监控在线 3 台")
        self.assertTrue(page["can_add_device"])
        self.assertEqual([i["alias"] for i in page["items"]], ["local", "macmini", "nas"])  # An older page's rows stay.
        self.assertEqual(data["repos"]["devices"], [{"alias": "macmini", "title": "Mac mini", "access": "write"},
                                                    {"alias": "testbox", "title": "testbox", "access": ""}])

    def test_a_device_is_added_only_after_its_checkup_passed_read_only_by_default(self):
        self.assertIn("正在体检 box1", self.op(what="device_add", name="box1", title="盒子", ssh="me@box1 -p 2222", note="跑数据")[1])
        self.assertNotIn("box1", self.raw()["devices"])  # Not before the 体检.
        self.assertEqual(self.panels.read_checks(self.ctx.state_dir)["box1"]["state"], "checking")
        self.assertEqual(self.snap()["nodes"]["adding"], [{"alias": "box1", "state": "checking", "message": ""}])
        with self.checked(ok=False, text="连不上 box1：x"):
            self.started.pop()()
        self.assertNotIn("box1", self.raw()["devices"])
        self.assertEqual(self.snap()["nodes"]["adding"][0]["state"], "failed")
        self.op(what="device_add", name="box1", ssh="me@box1 -p 2222", note="跑数据")
        with self.checked() as checkup:
            self.started.pop()()
        config = checkup.call_args.args[0]
        self.assertEqual(config.devices["box1"].host, "me@box1")  # Checked as it will be written, before it is.
        self.assertEqual(self.raw()["devices"]["box1"], {"title": "box1", "access": "read", "note": "跑数据",
                                                         "ssh": {"host": "me@box1", "options": ["-p", "2222"]}})
        self.assertEqual(self.snap()["nodes"]["adding"], [])
        self.assertIn("testbox", self.raw()["devices"])  # The others as they were.
        self.assertEqual(self.raw()["devices"]["testbox"]["harnesses"], ["claude"])
        for payload, why in (({"name": "1box"}, "名字只用小写字母"), ({"name": "macmini"}, "已经有 macmini"),
                             ({"name": "b2", "ssh": "-oProxyCommand=sh me@x"}, "本机执行命令"), ({"name": "b2", "access": "all"}, "write 或 read"),
                             ({"name": "b2", "note": "两\n行"}, "一行"), ({"name": "b2", "ssh": "pw@x", "password": "x"}, None)):
            if why is None:
                continue
            state, words = self.op(what="device_add", **payload)
            self.assertEqual(state, "failed", payload)
            self.assertIn(why, words)

    def test_a_machine_without_ssh_is_written_at_once_as_only_watched(self):
        self.assertEqual(self.op(what="device_add", name="nas", title="NAS"), ("done", "已加入设备名单：nas（没有 ssh：只能看状况，不能执行）"))
        self.assertEqual(self.raw()["devices"]["nas"], {"title": "NAS", "access": "read"})
        self.assertEqual(self.started, [])
        row = next(r for r in self.snap()["nodes"]["rows"] if r["alias"] == "nas")
        self.assertEqual((row["runs"], row["listed"], row["link"]), (False, True, None))
        self.assertEqual(self.op(what="device_check", alias="nas"), ("failed", "nas 没有 ssh，只能看状况，体检不了"))

    def test_a_device_is_changed_checked_and_removed_with_its_records_kept_and_its_node_revoked(self):
        from covey.core import nodes
        state = self.ctx.state_dir
        self.assertEqual(self.op(what="device_set", alias="testbox", access="read", note="同事的机器"), ("done", "已改 testbox：用途、可写还是只读"))
        self.assertEqual(self.raw()["devices"]["testbox"], {"title": "testbox", "ssh": {"host": "tb"}, "harnesses": ["claude"],
                                                            "access": "read", "note": "同事的机器"})
        self.assertEqual(self.op(what="device_set", alias="testbox", ssh="me@tb2 -p 23")[0], "done")
        self.assertEqual(self.raw()["devices"]["testbox"]["ssh"], {"host": "me@tb2", "options": ["-p", "23"]})
        self.assertEqual(self.op(what="device_set", alias="testbox", note="")[0], "done")
        self.assertNotIn("note", self.raw()["devices"]["testbox"])
        self.assertEqual(self.op(what="device_set", alias="nope", note="x"), ("failed", "设备名单里没有 nope"))
        self.assertEqual(self.op(what="device_set", alias="testbox"), ("failed", "没有要改的项"))
        self.assertIn("正在体检 macmini", self.op(what="device_check", alias="macmini")[1])
        self.assertEqual(self.op(what="device_check", alias="macmini"), ("done", "macmini 正在体检"))
        with self.checked(text="macmini 体检通过：…"):
            self.started.pop()()
        self.assertEqual(self.panels.read_checks(state)["macmini"]["state"], "done")
        write_json(self.devices.device_dir(state, "testbox") / "profile.json", {"check": {"at": 1.0, "ok": True}})
        write_json(nodes.nodes_dir(state) / "registry.json", {"schema": 1, "nodes": {"local": {}, "testbox": {"title": "tb"}}})
        with mock.patch.object(nodes, "revoke", return_value=True) as revoked:
            self.assertEqual(self.op(what="device_remove", alias="testbox"),
                             ("done", "testbox：已从设备名单去掉，撤销了它的监控节点；它的记录留着"))
        revoked.assert_called_once_with(state, "testbox")
        self.assertNotIn("testbox", self.raw()["devices"])
        self.assertTrue((self.devices.device_dir(state, "testbox") / "profile.json").exists())
        write_json(nodes.nodes_dir(state) / "registry.json", {"schema": 1, "nodes": {"local": {}}})  # Revoked by now.
        self.assertEqual(self.op(what="device_remove", alias="testbox"), ("failed", "testbox 已经不在了"))

    def test_devices_off_change_nothing_and_a_bad_file_is_never_written_over(self):
        with mock.patch.dict(os.environ, {"COVEY_DEVICES": ""}):
            self.assertEqual(self.op(what="device_add", name="b1")[0], "failed")
            self.assertNotIn("nodes", self.snap())  # No devices, no nodes: no tab.
        self.file.write_text("{not json")
        self.assertEqual(self.op(what="device_set", alias="macmini", note="x")[0], "failed")
        self.assertEqual(self.file.read_text(), "{not json")
        page = self.snap()["nodes"]
        self.assertIn("error", page)

    def test_a_path_on_a_device_is_checked_there_and_kept_as_alias_and_path(self):
        from covey.core import devices
        with mock.patch.object(devices, "inspect_dir", return_value=("ok", "git", "")) as inspected:
            self.assertEqual(self.op(what="repos_add", path="/srv/proj", device="macmini", note="远端"),
                             ("done", "已加入常用路径：proj（设备 macmini 上的仓库）"))
        self.assertEqual(inspected.call_args.args[1:3], ("macmini", "/srv/proj"))
        with mock.patch.object(devices, "inspect_dir", return_value=("ok", "dir", "")):
            self.assertEqual(self.op(what="repos_add", path="testbox:/srv/proj")[1], "已加入常用路径：proj-testbox（设备 testbox 上的目录）")
        with mock.patch.object(devices, "inspect_dir", return_value=("offline", "", "设备 macmini 没有答复（连接超时）")):
            self.assertEqual(self.op(what="repos_add", path="/srv/other", device="macmini"), ("failed", "设备 macmini 没有答复（连接超时）"))
        found = {repo.name: repo for repo in repomap.read(self.ctx.state_dir)}
        self.assertEqual((found["proj"].path, found["proj"].device, found["proj"].kind, found["proj"].write_mode),
                         ("macmini:/srv/proj", "macmini", "git", "inplace"))
        item = next(i for i in self.snap()["repos"]["items"] if i["name"] == "proj")
        self.assertEqual((item["device"], item["kind"], item["tail"]), ("macmini", "git", "/srv/proj"))
        self.assertEqual(self.op(what="repos_set", name="proj", write_mode="worktree"), ("failed", "目录和设备上的路径只能原地改"))


class StampCoverageTest(SettingsCase):
    """T907 W2-D 包 E, gate G5: every state file ``snapshot`` reads is a source of ``stamps`` (the page's settings are pushed again within
    a second of a change), and a change of the groups, the nodes, the 代答 or the 新话题 choices moves the stamps."""

    def setUp(self):
        super().setUp()
        from covey.core import nodes, proxy as proxy_records
        state = self.ctx.state_dir
        self.state = Path(state)
        chat = "oc_group0123456789"
        group_records.save_group(state, chat, lambda record: {**record, "chat_id": chat, "active": True, "external": False})
        repomap_dir = Path(self.work)
        (repomap_dir / ".git").mkdir(exist_ok=True)
        write_json(repomap.path(state), {"schema": 1, "repos": {"project": {"path": str(repomap_dir)}}})
        write_json(nodes.nodes_dir(state) / "registry.json", {"schema": 1, "nodes": {"local": {"title": "本机"}, "box": {"title": "盒子"}}})
        for alias in ("local", "box"):
            nodes.write_link(state, alias, connected=True, heard=time.time())
            write_json(nodes.node_dir(state, alias) / "latest.json", {"t": time.time(), "got": time.time()})
        write_json(proxy_records.proxy_dir(state) / "settings.json", {"enabled": True, "group_name": "代答群"})
        proxy_records.write_contact(state, {"key": "k1", "name": "甲", "kind": "p2p", "added_at": 1.0})
        self.fake_panel = SimpleNamespace(
            proxy=SimpleNamespace(settings=lambda ctx: {**proxy_records.read_settings(ctx.state_dir), "user_profile": "me"},
                                  topics=self.app.topics, web_login=SimpleNamespace(login_seen=(True, 1.0)),
                                  web=SimpleNamespace(problems=lambda ctx, found: [])),
            current=lambda found, choices: None)
        from covey.core import nodes as node_words
        patches = [mock.patch.dict(os.environ, {node_words.LISTEN_ENV: "127.0.0.1:1"}),
                   mock.patch.object(self.web, "proxy_panel", return_value=self.fake_panel)]
        for each in patches:
            each.start()
            self.addCleanup(each.stop)

    def reads(self) -> set:
        """The files under the state directory one ``snapshot`` reads, every part built."""
        seen = self.reads_of(self.snap)
        data = self.last
        for tab in ("config", "repos", "groups", "nodes", "proxy"):
            self.assertIn(tab, data)
            self.assertNotIn("error", data[tab], tab)
        return seen

    def reads_of(self, call) -> set:
        """The files under the state directory ``call`` reads: Path.read_text and read_bytes (core/state.py read_json and
        read_object end in the first) and open() for reading; what it returned is ``last``."""
        import builtins
        seen, state = set(), str(self.state)

        def note(path):
            if isinstance(path, (str, os.PathLike)) and str(path).startswith(state + os.sep):
                seen.add(Path(path))
        read_text, read_bytes, opened = Path.read_text, Path.read_bytes, builtins.open

        def text(path, *args, **kwargs):
            note(path)
            return read_text(path, *args, **kwargs)

        def raw(path, *args, **kwargs):
            note(path)
            return read_bytes(path, *args, **kwargs)

        def open_(file, mode="r", *args, **kwargs):
            if not set(mode) & set("wax+"):
                note(file)
            return opened(file, mode, *args, **kwargs)
        with mock.patch.object(Path, "read_text", text), mock.patch.object(Path, "read_bytes", raw), \
                mock.patch.object(builtins, "open", open_):
            self.last = call()
        return seen

    def uncovered(self, read) -> list:
        """The files read that no source stands for: a source is the file itself or the directory it is in."""
        sources = set(self.web.sources(self.web.context()))
        return sorted(str(path.relative_to(self.state)) for path in read if path not in sources and path.parent not in sources)

    def test_every_file_the_snapshot_reads_is_a_source_of_the_stamps(self):
        read = self.reads()
        names = {str(path.relative_to(self.state)) for path in read}
        # The fixture does read what each tab reads, or the gate would pass for nothing.
        for expected in ("main-thread.json", "workspace-config.json", "assist-state.json", "repos.json",
                         "nodes/registry.json", "nodes/box/link.json", "nodes/box/latest.json", "proxy/settings.json"):
            self.assertIn(expected, names)
        self.assertTrue(any(name.startswith("groups/") for name in names), names)
        self.assertTrue(any(name.startswith("proxy/contacts/") for name in names), names)
        self.assertTrue(any(name.startswith("launch-") for name in names), names)
        self.assertEqual(self.uncovered(read), [])

    def test_each_part_reads_only_files_its_own_sources_stand_for(self):
        """T970: a push between full looks builds again only the parts whose sources moved, so each part's reads must be its own
        sources' (part_sources), not only some tab's."""
        self.snap()
        every = lambda: self.web.snapshot("dm", fresh=False)  # noqa: E731
        common = self.reads_of(every)  # Nothing built again: what every snapshot reads (the tabs, the 代答 panel).
        sources = self.web.part_sources(self.web.context())
        parts = sorted(self.web.built)
        self.assertEqual(set(parts), {"launch", "config", "repos", "groups", "nodes", "proxy", "workspace"})
        for part in parts:
            with self.subTest(part):
                self.web.built.pop(part)
                read = self.reads_of(every) - common
                self.assertIn(part, self.web.built)
                own = set(sources[part])
                self.assertEqual(sorted(str(path.relative_to(self.state)) for path in read
                                        if path not in own and path.parent not in own), [])

    def test_the_gate_names_a_file_a_source_leaves_out(self):
        read = self.reads()
        real = web_settings.WebSettings.tab_sources
        for tab, left in ((web_settings.GROUPS, "groups"), (web_settings.NODES, "nodes/box/link.json"),
                          (web_settings.PROXY_TAB, "proxy/contacts"), (web_settings.CONFIG, "launch-models")):
            def without(self_, ctx, tab=tab, left=left):
                found = real(self_, ctx)
                found[tab] = [path for path in found[tab] if not str(path).endswith(left)]
                return found
            with mock.patch.object(web_settings.WebSettings, "tab_sources", without):
                missing = self.uncovered(read)
            self.assertTrue(missing and all(name.startswith(left) for name in missing), (left, missing))

    def test_a_change_of_a_tab_the_stamps_did_not_cover_moves_them(self):
        from covey.core import nodes, proxy as proxy_records
        state, chat = self.state, "oc_group0123456789"
        steps = {
            "群聊 (a record rewritten in place)": lambda: group_records.save_group(state, chat, lambda r: {**r, "active": False}),
            "群聊 (a new one)": lambda: group_records.save_group(state, "oc_other", lambda r: {**r, "chat_id": "oc_other", "active": True}),
            "设备 (a node's link)": lambda: nodes.write_link(state, "box", connected=False),
            "设备 (the registry)": lambda: write_json(nodes.nodes_dir(state) / "registry.json", {"schema": 1, "nodes": {"local": {}}}),
            "代答 (its settings)": lambda: write_json(proxy_records.proxy_dir(state) / "settings.json", {"enabled": False}),
            "代答 (a contact)": lambda: proxy_records.write_contact(state, {"key": "k2", "name": "乙", "kind": "p2p", "added_at": 2.0}),
            "开话题 (the last launch)": lambda: write_json(state / "launch-defaults.json", {"model": "x"}),
            "开话题 (a model menu)": lambda: write_json(state / "launch-models" / "claude.json", {"updated_at": 1, "models": {}}),
            "开话题 (the recent picks)": lambda: write_json(state / "model-usage.json", {"picks": []}),
        }
        for name, change in steps.items():
            before = self.web.stamps()
            change()
            self.assertNotEqual(self.web.stamps(), before, name)
        before = self.web.stamps()  # Only the mtime: a record a writer on the network disk left with the same size.
        os.utime(state / "groups", ns=(1, 1))
        self.assertNotEqual(self.web.stamps(), before)

    def test_what_changes_all_the_time_moves_the_stamps_once_a_minute(self):
        from covey.core import nodes
        latest = nodes.node_dir(self.state, "box") / "latest.json"
        os.utime(latest, ns=(60 * 10 ** 9 * 1000, 60 * 10 ** 9 * 1000))
        before = self.web.stamps()
        os.utime(latest, ns=(60 * 10 ** 9 * 1000 + 5 * 10 ** 9,) * 2)
        self.assertEqual(self.web.stamps(), before)
        os.utime(latest, ns=(60 * 10 ** 9 * 1001,) * 2)
        self.assertNotEqual(self.web.stamps(), before)

    def test_a_path_that_is_not_there_is_zero_and_nothing_raises(self):
        for path in self.state.glob("nodes/*/latest.json"):
            path.unlink()
        shutil.rmtree(self.state / "groups")
        stamps = self.web.stamps()
        self.assertIn(0, stamps)
        self.assertEqual(len(stamps), len(self.web.sources(self.web.context())) + 1)  # And the worktrees' scan (T970).


class PartsTest(SettingsCase):
    """T970 (2026-11-29 on the live page: a 代答 model picked took 6 to 8 s to show, the push before its ack built every tab again,
    the worktrees' scan 1 to 4 s of it): between full looks a push builds again only the parts whose files moved; the worktrees, once
    scanned, are scanned again in a thread of their own; the push before a 设置 op's ack waits for the other thread's."""

    def counted(self, *names):
        found = {}
        for name in names:
            patch = mock.patch.object(self.web, name, wraps=getattr(self.web, name))
            found[name] = patch.start()
            self.addCleanup(patch.stop)
        return found

    def test_a_push_between_full_looks_builds_only_the_parts_whose_files_moved(self):
        first = self.web.snapshot("dm")
        calls = self.counted("config", "repos", "groups", "workspaces")
        built = lambda: {name: mock_.call_count for name, mock_ in calls.items()}  # noqa: E731
        self.assertEqual(self.web.snapshot("dm", fresh=False), first)  # Nothing moved: nothing built, the same item.
        self.assertEqual(built(), {"config": 0, "repos": 0, "groups": 0, "workspaces": 0})
        settings.update(self.ctx.state_dir, compact_tokens=0)  # main-thread.json: 配置 and the old 工作区 page read it.
        again = self.web.snapshot("dm", fresh=False)
        self.assertEqual(built(), {"config": 1, "repos": 0, "groups": 0, "workspaces": 1})
        self.assertNotEqual(again["config"], first["config"])
        self.assertIs(again["repos"], first["repos"])
        chat = "oc_group0123456789"
        group_records.save_group(self.ctx.state_dir, chat, lambda record: {**record, "chat_id": chat, "active": True})
        self.web.snapshot("dm", fresh=False)
        self.assertEqual(built(), {"config": 1, "repos": 0, "groups": 1, "workspaces": 1})
        self.web.snapshot("dm", fresh=False, background=True)
        self.web.snapshot("dm")  # A full look: every part.
        self.assertEqual(built(), {"config": 2, "repos": 1, "groups": 2, "workspaces": 2})

    def test_a_part_that_cannot_be_read_is_built_again_next_time(self):
        self.web.snapshot("dm")
        with mock.patch.object(self.web, "groups", side_effect=OSError("stuck")):
            self.web.built.pop("groups")
            self.assertIn("error", self.web.snapshot("dm", fresh=False)["groups"])
        self.assertNotIn("groups", self.web.built)
        self.assertNotIn("error", self.web.snapshot("dm", fresh=False)["groups"])

    def test_the_worktrees_are_scanned_again_in_their_own_thread_and_a_new_scan_moves_the_stamps(self):
        scans = []

        def page(state_dir, now):
            scans.append(now)
            return {"total": len(scans), "prunable": []}

        def total(**kwargs):
            return self.web.snapshot("dm", time.time(), **kwargs)["repos"]["worktrees"]["total"]
        with mock.patch.object(web_settings, "worktrees_page", side_effect=page), mock.patch.object(web_settings, "WORKTREE_CACHE", 0.3):
            self.assertEqual(total(background=True), 1)  # The first: in the build.
            before = self.web.stamps()
            time.sleep(0.35)
            self.assertEqual(total(background=True), 1)  # Older: as it was, at once, and scanned again in a thread.
            for _ in range(200):
                if not self.web.scanning:
                    break
                time.sleep(0.01)
            self.assertEqual(len(scans), 2)
            self.assertNotEqual(self.web.stamps(), before)  # Found something new: the next push has it.
            self.assertEqual(total(fresh=False, background=True), 2)
            self.assertEqual(len(scans), 2)
            time.sleep(0.35)
            # Without ``background`` (a caller that wants it now) an older scan is done again in the build, as before.
            self.assertEqual(total(), 3)

    def test_the_push_before_a_settings_ack_waits_for_the_other_threads_push(self):
        import threading
        with mock.patch.dict(os.environ, {web_sync.ENV: "1"}):
            sync = WebSync(self.app.runtime, self.env)
        sync.client.request = mock.Mock(return_value={})
        self.assertTrue(sync.settings_lock.acquire(blocking=False))
        threading.Timer(0.2, sync.settings_lock.release).start()
        self.assertEqual(sync.push_settings("dm", time.time(), wait=5.0), 1)
        self.assertEqual(sync.client.request.call_args.args[:2], ("POST", "/sync/upsert"))
        self.assertTrue(sync.settings_lock.acquire(blocking=False))
        try:
            self.assertEqual(sync.push_settings("dm", time.time()), 0)  # The pass thread's own turn does not wait.
        finally:
            sync.settings_lock.release()
        with mock.patch.object(sync, "push_settings", return_value=0) as push, \
                mock.patch.object(self.app.runtime.access, "owner_chat", return_value="dm"):
            sync.before_ack("settings")
            sync.before_ack("launch")
        self.assertEqual([call.kwargs.get("wait") for call in push.call_args_list], [web_sync_app.SETTINGS_LOCK_WAIT, 0.0])


class LockHeldTest(SettingsCase):
    """T907 W2-D 包 E (P2-2): a push that finds its lock held leaves a mark and logs at most once a minute; the item's next turn pushes
    in full."""

    def setUp(self):
        super().setUp()
        p = mock.patch.dict(os.environ, {web_sync.ENV: "1"})
        p.start()
        self.addCleanup(p.stop)
        self.sync = WebSync(self.app.runtime, self.env)
        log = mock.patch.object(self.app.runtime, "log")
        self.log = log.start()
        self.addCleanup(log.stop)
        self.sync.client.request = mock.Mock(return_value={})
        # name -> (the lock, how to call its push), each with what makes the push go as far as its ``catch_up``.
        self.upgrader = SimpleNamespace(on=lambda: True, stamps=lambda: (1,), view=lambda: {"v": 1})
        self.tidy = SimpleNamespace(web_stamps=lambda: (1,), web_view=lambda now: None)
        for each in (mock.patch.object(self.sync.ops, "_upgrader", self.upgrader),
                     mock.patch.object(self.env, "tidy", self.tidy, create=True)):
            each.start()
            self.addCleanup(each.stop)
        self.pushes = {"settings": (self.sync.settings_lock, lambda: self.sync.push_settings("dm", 0.0)),
                       "tidy": (self.sync.tidy_lock, lambda: self.sync.push_tidy(0.0)),
                       "memory": (self.sync.memory_lock, lambda: self.sync.push_memory()),
                       "memory center": (self.sync.center_lock, lambda: self.sync.push_memory_center()),
                       "upgrade": (self.sync.upgrade_lock, lambda: self.sync.push_upgrade()),
                       "quota": (self.sync.quota_lock, lambda: self.sync.push_quota())}

    def lines(self):
        return [call.args[0] for call in self.log.call_args_list if "its lock is held" in call.args[0]]

    def test_a_lock_that_is_held_leaves_a_mark_and_one_line_a_minute(self):
        for name, (lock, push) in self.pushes.items():
            with self.subTest(name):
                self.log.reset_mock()
                self.assertTrue(lock.acquire(blocking=False))
                try:
                    self.assertEqual(push(), 0)
                    self.assertIn(name, self.sync.held)
                    first = self.sync.held[name]
                    self.assertEqual(push(), 0)  # Within the minute: no second line, and the mark keeps its first time.
                    self.assertEqual(self.sync.held[name], first)
                    lines = self.lines()
                    self.assertEqual(len(lines), 1)
                    self.assertTrue(lines[0].startswith(f"web sync {name} skipped: its lock is held (since "), lines)
                    self.sync.held_logged[name] -= web_sync_app.HELD_LOG_SECONDS + 1  # A minute on.
                    push()
                    self.assertEqual(len(self.lines()), 2)
                finally:
                    lock.release()

    def test_the_next_turn_pushes_in_full_and_clears_the_mark(self):
        seen = []
        real = self.sync.catch_up
        self.sync.catch_up = lambda name, full: seen.append((name, real(name, full))) or seen[-1][1]
        for name, (lock, push) in self.pushes.items():
            with self.subTest(name):
                seen.clear()
                push()
                self.assertEqual(seen, [(name, False)])  # Not held before: not full.
                self.assertTrue(lock.acquire(blocking=False))
                try:
                    push()
                finally:
                    lock.release()
                self.assertIn(name, self.sync.held)
                seen.clear()
                push()
                self.assertEqual(seen, [(name, True)])
                self.assertNotIn(name, self.sync.held)
                seen.clear()
                push()
                self.assertEqual(seen, [(name, False)])  # Once.

    def test_the_settings_item_is_built_in_full_the_turn_after_and_is_due(self):
        self.sync.settings_items("dm", 0.0, False)  # Built: nothing left due.
        self.assertFalse(self.sync.settings_due)
        self.assertTrue(self.sync.settings_lock.acquire(blocking=False))
        try:
            self.sync.push_settings("dm", 1.0)
        finally:
            self.sync.settings_lock.release()
        self.assertTrue(self.sync.settings_due)
        with mock.patch.object(self.sync, "settings_items", wraps=self.sync.settings_items) as items:
            self.sync.push_settings("dm", 2.0)
        self.assertEqual(items.call_args.args[2], True)  # ``full``

    def test_a_forced_memory_center_push_does_not_mark_and_still_waits(self):
        lock = self.sync.center_lock
        self.assertTrue(lock.acquire(blocking=False))
        try:
            with mock.patch.object(web_sync_app, "CENTER_LOCK_WAIT", 0.01):
                self.assertEqual(self.sync.push_memory_center(force=True), 0)
        finally:
            lock.release()
        self.assertNotIn("memory center", self.sync.held)
        self.assertEqual(self.lines(), [])

class SwitchToDeviceTest(unittest.TestCase):
    """主人 D2Y4T: an older page's 换配置 that names a workspace on a device moves only a topic already on a device there; a
    topic of this machine is refused (app/web_switch.py TO_DEVICE)."""

    def place(self, current_device):
        from covey.app import web_switch
        from covey.core import devices
        binding = devices.Binding(Path("/standin"), "gpu1", "/home/me/proj", "gpu1:/home/me/proj")
        env = SimpleNamespace(runtime=SimpleNamespace(access=SimpleNamespace(allows_device=lambda chat, alias: True)))
        ctx = SimpleNamespace(state_dir=Path("/state"), default_workdir=Path("/home"))
        current = {"workspace": "home", "run_mode": "direct", "device": current_device}
        with mock.patch("covey.app.web_launch.workspace_config",
                        return_value=({"workspaces": {"gpu1-proj": binding.value}}, {}, {})), \
                mock.patch("covey.core.workspaces.resolve_binding", return_value=("gpu1-proj", binding)), \
                mock.patch.object(devices, "undeclared_refusal", return_value=""):
            return web_switch._place(env, ctx, "chat", current, "gpu1-proj", "direct", "claude", {"title": "Claude Code"})

    def test_a_topic_here_is_never_moved_onto_a_device(self):
        from covey.app import web_switch
        with self.assertRaisesRegex(ValueError, "执行设备入口已下线"):
            self.place("")
        self.assertIn("covey device", web_switch.TO_DEVICE)

    def test_a_topic_already_on_a_device_may_still_switch_to_a_workspace_there(self):
        self.assertEqual(self.place("gpu1"), {"workspace": "gpu1-proj", "workdir": "gpu1:/home/me/proj", "run_mode": "direct",
                                              "branch": ""})
