"""自动补齐飞书应用 (core/app_manifest.py, feishu/app_config.py, feishu/app_console.py, app/app_setup.py,
cards/app_setup.py, service/app_check.py): what the app needs by the switches, what the online version lacks and what
one run offers (no reviewed scope unless asked, no menu item past five); the official read-back's shape and its
errors; the console's add-only manifest merge, the menu kept with its uploaded icons and new items numbered, the
next version number and the visibility copied; the check sending one card per set of missing things, none for a
reviewed scope alone; the clicks (owner only, once, the card's own, 这些先不补); a run following the console's lines
to the result card, its folder removed; the terminal's QR."""
from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from unittest import mock
from pathlib import Path
from types import SimpleNamespace

from covey.app import app_setup
from covey.app.app_setup import AppSetup, need_dict
from covey.app.card_actions import ACTIONS
from covey.app.unified import UnifiedBridgeApp
from covey.cards import app_setup as cards
from covey.core import app_manifest as manifest
from covey.core.state import read_object, write_json
from covey.feishu import app_config, app_console, slash_commands
from covey.service import app_check

FULL = {"scopes": {name for name, _ in manifest.SCOPES + manifest.PIN_SCOPES + manifest.GROUP_ALL_SCOPES},
        "events": {manifest.MESSAGE_EVENT, manifest.MENU_EVENT, manifest.READ_EVENT},
        "callbacks": {manifest.CARD_CALLBACK}, "bot": True, "menu_enable": True,
        "menu_keys": ["weekly_usage", "digest_board", "proxy_settings", "bridge_settings"], "version": "1.0.11"}
SWITCHES = {"COVEY_PINNED_TOPICS": "1", "COVEY_DIGEST_TIMES": "09:30"}


def ids(needs):
    return [need.id for need in needs]


class ManifestTests(unittest.TestCase):
    def test_switches_add_their_needs(self):
        unset = ids(manifest.required({}))  # The pinned topics and the board are on unless set to 0.
        self.assertIn("event:im.message.message_read_v1", unset)
        self.assertIn("scope:im:message.pins:read", unset)
        bare = ids(manifest.required({"COVEY_PINNED_TOPICS": "0", "COVEY_PINNED_OVERVIEW": "0"}))
        self.assertIn("callback:card.action.trigger", bare)
        self.assertIn("event:im.message.receive_v1", bare)
        # Slash commands replace the legacy bottom custom menu by default: no console menu required.
        self.assertNotIn("event:application.bot.menu_v6", bare)
        self.assertNotIn("menu_enable:menu", bare)
        self.assertNotIn("menu:weekly_usage", bare)
        self.assertNotIn("event:im.message.message_read_v1", bare)
        self.assertNotIn("menu:digest_board", bare)
        self.assertNotIn("scope:im:message.pins:read", bare)
        on = manifest.required({**SWITCHES, "COVEY_GROUP_ALL_MESSAGES": "1"})
        self.assertIn("event:im.message.message_read_v1", ids(on))
        self.assertIn("scope:im:message.pins:write_only", ids(on))
        self.assertEqual([n.name for n in on if n.review], ["im:message.group_msg"])
        # Legacy bottom menu remains available when requested explicitly.
        legacy = ids(manifest.required({**SWITCHES, "COVEY_GROUP_ALL_MESSAGES": "1"}, bottom_menu=True))
        self.assertIn("event:application.bot.menu_v6", legacy)
        self.assertIn("menu_enable:menu", legacy)
        self.assertIn("menu:digest_board", legacy)

    def test_the_private_chat_tab_scope_is_default_and_the_only_one_added(self):
        # 私聊顶栏「工作台」标签（网页的部署脚本 web/deploy 建）：建和更新只要 write_only，不加读权限，也不用审核。
        bare = ids(manifest.required({}))
        self.assertIn("scope:im:chat.tabs:write_only", bare)
        self.assertNotIn("scope:im:chat.tabs:read", bare)
        self.assertEqual([n.review for n in manifest.required({}) if n.name == "im:chat.tabs:write_only"], [False])

    def test_an_old_deployment_lacking_only_the_tab_scope_is_offered_just_that_once(self):
        old = {**FULL, "scopes": FULL["scopes"] - {"im:chat.tabs:write_only"}}
        lacking, full = manifest.missing(SWITCHES, old)
        self.assertEqual(ids(lacking), ["scope:im:chat.tabs:write_only"])
        offer = manifest.offered(lacking, full)
        self.assertEqual(ids(offer), ["scope:im:chat.tabs:write_only"])  # 免审：不用「连同需审核的一起申请」
        self.assertEqual(manifest.plan(offer, remark="r")["scopes"], ["im:chat.tabs:write_only"])
        self.assertEqual(manifest.missing(SWITCHES, FULL), ([], []))  # 补上以后不再缺

    def test_existing_bottom_menu_still_requires_menu_event_but_fresh_app_does_not(self):
        no_menu = {**FULL, "menu_enable": False, "menu_keys": [], "events": {manifest.MESSAGE_EVENT, manifest.READ_EVENT}}
        lacking, full = manifest.missing(SWITCHES, no_menu)
        self.assertEqual((lacking, full), ([], []))
        with_menu = {**no_menu, "menu_enable": True}
        lacking_legacy, _ = manifest.missing(SWITCHES, with_menu)
        self.assertEqual(ids(lacking_legacy), ["event:application.bot.menu_v6"])

    def test_the_proxy_menu_item_only_where_an_extension_has_the_sender(self):
        with mock.patch.object(manifest, "proxy_sender", return_value=False):
            self.assertNotIn("proxy", [c.command for c in manifest.slash_commands({})])
            self.assertNotIn("menu:proxy_settings", ids(manifest.required({}, bottom_menu=True)))
        with mock.patch.object(manifest, "proxy_sender", return_value=True):
            self.assertEqual([c.command for c in manifest.slash_commands({})], ["usage", "board", "proxy", "settings"])
            menu = [n for n in manifest.required({}, bottom_menu=True) if n.kind == "menu"]
        self.assertEqual([n.name for n in menu], ["weekly_usage", "digest_board", "proxy_settings", "bridge_settings"])
        with mock.patch("covey.core.extensions.manifests", side_effect=OSError):
            self.assertFalse(manifest.proxy_sender())

    def test_nothing_missing_but_the_reviewed_scope(self):
        lacking, full = manifest.missing({**SWITCHES, "COVEY_GROUP_ALL_MESSAGES": "1"}, FULL)
        self.assertEqual(ids(lacking), ["scope:im:message.group_msg"])
        self.assertEqual(manifest.offered(lacking, full), [])
        self.assertEqual(ids(manifest.offered(lacking, full, review=True)), ["scope:im:message.group_msg"])

    def test_menu_items_past_five_are_not_offered(self):
        online = {**FULL, "menu_keys": ["a", "b", "c", "d"], "menu_enable": True}
        with mock.patch.object(manifest, "proxy_sender", return_value=False):
            lacking, full = manifest.missing(SWITCHES, online, bottom_menu=True)
        self.assertEqual(ids(full), ["menu:digest_board", "menu:bridge_settings"])
        self.assertEqual(ids(manifest.offered(lacking, full)), ["menu:weekly_usage"])

    def test_the_older_menu_items_are_lacking_until_taken_off_and_free_their_room(self):
        # T898: 仓库, 脱敏 and 群聊 moved onto 设置 tabs; each still on the app's menu is a ``menu_remove`` need (the menu sync
        # drops it), and while it is on it counts against the limit no more than the room it gives back.
        online = {**FULL, "menu_keys": ["a", "manage_workspaces", "redaction_dashboard", "manage_groups", "main_thread_settings"],
                  "menu_enable": True}
        with mock.patch.object(manifest, "proxy_sender", return_value=False):
            lacking, full = manifest.missing(SWITCHES, online, bottom_menu=True)
        self.assertEqual(ids([n for n in lacking if n.kind == "menu_remove"]),
                         ["menu_remove:manage_workspaces", "menu_remove:redaction_dashboard", "menu_remove:manage_groups"])
        self.assertEqual(ids(full), [])  # Five on, three of them to go: the room for the three new ones is there.
        full_menu = {**online, "menu_keys": ["a", "b", "manage_workspaces", "redaction_dashboard", "manage_groups"]}
        with mock.patch.object(manifest, "proxy_sender", return_value=True):
            lacking_full, full_now = manifest.missing(SWITCHES, full_menu, bottom_menu=True)
        self.assertEqual(ids(full_now), ["menu:bridge_settings"])  # Four to add, room for three (the five on, minus the three to go).
        self.assertNotIn("menu_remove:main_thread_settings", ids(lacking))  # The butler's 配置 key stays.
        # Taken off: nothing lacking of that kind.
        clean = {**online, "menu_keys": ["a", "main_thread_settings"]}
        self.assertFalse([n for n in manifest.missing(SWITCHES, clean)[0] if n.kind == "menu_remove"])
        needs = [n for n in lacking if n.kind == "menu_remove"]
        plan = manifest.plan(needs, remark=manifest.remark(needs))
        self.assertEqual(plan["menu_remove"], ["manage_workspaces", "redaction_dashboard", "manage_groups"])
        self.assertIn("去掉菜单旧项 仓库、脱敏、群聊", plan["remark"])
        self.assertEqual(cards.groups([{"kind": n.kind, "name": n.name, "words": n.words} for n in needs])[0][0], "菜单")

    def test_plan_and_remark(self):
        needs = [n for n in manifest.required(SWITCHES, bottom_menu=True)
                 if n.name in ("im:message", "application.bot.menu_v6", "weekly_usage", "bridge_settings")]
        plan = manifest.plan(needs, remark=manifest.remark(needs))
        self.assertEqual(plan["scopes"], ["im:message"])
        self.assertEqual(plan["events"], ["application.bot.menu_v6"])
        self.assertEqual([m["key"] for m in plan["menu"]], ["weekly_usage", "bridge_settings"])
        self.assertTrue(plan["menu_enable"])
        self.assertFalse(plan["bot"])
        self.assertTrue(plan["remark"].startswith(app_console.REMARK_PREFIX))
        self.assertIn("菜单 用量、设置", plan["remark"])
        self.assertEqual(manifest.digest(needs), manifest.digest(list(reversed(needs))))


def fake_cli(responses):
    """A subprocess.run stand-in answering lark-cli api GETs by path."""
    def run(argv, **kwargs):
        path = argv[argv.index("GET") + 1]
        body = responses[path]
        return subprocess.CompletedProcess(argv, 0, json.dumps(body), "")
    return run


class ConfigTests(unittest.TestCase):
    APP = "/open-apis/application/v6/applications/cli_x"

    def test_online_config(self):
        run = fake_cli({
            self.APP: {"ok": True, "data": {"app": {
                "online_version_id": "oav_1", "scopes": [{"scope": "im:message"}, {"scope": "im:resource"}],
                "callback_info": {"subscribed_callbacks": ["card.action.trigger"]}}}},
            self.APP + "/app_versions/oav_1": {"ok": True, "data": {"app_version": {
                "version": "1.0.3", "event_infos": [{"event_type": "im.message.receive_v1"}],
                "ability": {"bot": {"bot_menu_enable": True, "bot_menus": [
                    {"event_key": "weekly_usage", "default_name": "", "i18n_name": {"zh_cn": "用量"}}]}}}}}})
        online = app_config.online_config("p", "cli_x", runner=run)
        self.assertEqual(online["scopes"], {"im:message", "im:resource"})
        self.assertEqual(online["events"], {"im.message.receive_v1"})
        self.assertEqual(online["callbacks"], {"card.action.trigger"})
        self.assertTrue(online["bot"] and online["menu_enable"])
        self.assertEqual(online["menus"], [("weekly_usage", "用量")])
        self.assertEqual(online["version"], "1.0.3")

    def test_errors(self):
        scope = fake_cli({self.APP: {"ok": False, "error": {"code": 99991672, "message": "Access denied. One of the "
                                                                                         "following scopes is required"}}})
        self.assertEqual(app_config.online_config("p", "cli_x", runner=scope)["error"], "no_self_manage")
        unpublished = fake_cli({self.APP: {"ok": True, "data": {"app": {"online_version_id": ""}}}})
        self.assertEqual(app_config.online_config("p", "cli_x", runner=unpublished)["error"], "no_version")
        broken = fake_cli({self.APP: {"ok": False, "error": {"code": 500, "message": "internal"}}})
        self.assertEqual(app_config.online_config("p", "cli_x", runner=broken)["error"], "failed")

    def test_without_self_manage_only_that_scope_is_offered(self):
        read = lambda profile, app_id: {"error": "no_self_manage", "detail": "x"}
        online, lacking, full = app_config.survey("p", "cli_x", {}, read=read)
        self.assertEqual(ids(lacking), ["scope:application:application:self_manage"])


class ConsoleTests(unittest.TestCase):
    def test_merge_adds_only(self):
        current = {"scopes": {"tenant": ["a"], "user": ["offline_access"]},
                   "events": {"items": {"tenant": ["e1"], "user": []}, "subscription_type": "websocket"},
                   "callbacks": {"subscription_type": "websocket"}, "bot": {"enable": False, "menu_config": [{"x": 1}]}}
        plan = {"scopes": ["a", "b"], "events": ["e2"], "callbacks": ["card.action.trigger"], "bot": True}
        merged = app_console.merge(current, plan)
        self.assertEqual(merged["scopes"], {"tenant": ["a", "b"], "user": ["offline_access"]})
        self.assertEqual(merged["events"]["items"]["tenant"], ["e1", "e2"])
        self.assertEqual(merged["callbacks"]["items"], ["card.action.trigger"])
        self.assertTrue(merged["bot"]["enable"])
        self.assertEqual(merged["bot"]["menu_config"], [{"x": 1}])
        self.assertEqual(current["scopes"]["tenant"], ["a"])  # Not changed in place.
        self.assertEqual(app_console.merge(merged, plan), merged)

    def test_menu_keeps_icons_numbers_new_items_and_stops_at_the_limit(self):
        before = {"botMenuEnable": True, "botMenuDisplayStrategy": 3, "maxFloatingMenuCount": 3, "botMenuConfig": [
            {"botMenuID": "old1", "eventKey": "custom", "i18nBotMenuName": {"zh_cn": "帮助"}, "defaultName": "帮助",
             "menuContentType": 2, "iconImageUrl": "img_v3_x", "childNodes": []},
            {"botMenuID": "old2", "i18nBotMenuName": {"zh_cn": "文档"}, "menuContentType": 1,
             "redirectLink": {"pcUrl": "https://example.com", "mobileUrl": ""}, "childNodes": []}]}
        after = {"botMenuConfig": [{"botMenuID": "new1", "eventKey": "custom", "i18nBotMenuName": {"zh_cn": "帮助"},
                                    "menuContentType": 2}]}
        plan = {"menu": [{"key": "weekly_usage", "label": "用量", "icon": "insert-chart_outlined"},
                         {"key": "bridge_settings", "label": "设置", "icon": "setting_outlined"}], "menu_enable": True}
        want = app_console.desired_menu(before, after, plan)
        items = want["botMenuConfig"]
        self.assertEqual([i.get("eventKey") or i["defaultName"] for i in items], ["custom", "文档", "weekly_usage"])
        self.assertEqual(items[0]["botMenuID"], "new1")
        self.assertEqual(items[0]["iconImageUrl"], "img_v3_x")
        self.assertEqual(items[1]["botMenuID"], "1")  # The console lost its ID: numbered like a new one.
        self.assertEqual(items[1]["redirectLink"]["pcUrl"], "https://example.com")
        self.assertNotIn("eventKey", items[1])
        self.assertEqual(items[2]["botMenuID"], "2")
        self.assertEqual(items[2]["udIcon"], {"token": "insert-chart_outlined"})
        self.assertEqual((want["botMenuEnable"], want["botMenuDisplayStrategy"]), (True, 3))
        self.assertNotEqual(app_console.menu_shape(want), app_console.menu_shape(after))
        self.assertEqual(app_console.menu_shape(want), app_console.menu_shape({**want, "botMenuConfig": [
            {**item, "botMenuID": "real"} for item in items]}))

    def test_the_menu_sync_drops_the_stale_items_and_keeps_the_rest(self):
        before = {"botMenuEnable": True, "botMenuDisplayStrategy": 3, "maxFloatingMenuCount": 5, "botMenuConfig": [
            {"botMenuID": "1", "eventKey": "weekly_usage", "i18nBotMenuName": {"zh_cn": "用量"}, "menuContentType": 2},
            {"botMenuID": "2", "eventKey": "manage_workspaces", "i18nBotMenuName": {"zh_cn": "仓库"}, "menuContentType": 2},
            {"botMenuID": "3", "eventKey": "redaction_dashboard", "i18nBotMenuName": {"zh_cn": "脱敏"}, "menuContentType": 2},
            {"botMenuID": "4", "eventKey": "manage_groups", "i18nBotMenuName": {"zh_cn": "群聊"}, "menuContentType": 2},
            {"botMenuID": "5", "eventKey": "bridge_settings", "i18nBotMenuName": {"zh_cn": "设置"}, "menuContentType": 2}]}
        plan = {"menu": [{"key": "digest_board", "label": "看板", "icon": "i"}],
                "menu_remove": ["manage_workspaces", "redaction_dashboard", "manage_groups"], "menu_enable": True}
        want = app_console.desired_menu(before, before, plan)
        self.assertEqual([i["eventKey"] for i in want["botMenuConfig"]], ["weekly_usage", "bridge_settings", "digest_board"])

    def test_menu_off_turns_on_floating(self):
        want = app_console.desired_menu({"botMenuEnable": False, "botMenuConfig": []}, {}, {
            "menu": [{"key": "k", "label": "用量", "icon": "i"}], "menu_display": 3})
        self.assertEqual((want["botMenuEnable"], want["botMenuDisplayStrategy"]), (True, 3))

    def test_versions(self):
        self.assertEqual(app_console.version_after([{"appVersion": "1.0.9"}, {"appVersion": "1.0.11"},
                                                    {"appVersion": "bad"}]), "1.0.12")
        self.assertEqual(app_console.version_after([]), "1.0.0")
        versions = [{"appVersion": "1.0.3", "versionStatus": 0}, {"appVersion": "1.0.2", "versionStatus": 2}]
        self.assertEqual(app_console.pending_version(versions)["appVersion"], "1.0.3")
        self.assertIsNone(app_console.pending_version(versions[1:]))
        self.assertIsNone(app_console.pending_version([{"versionStatus": 2}, {"versionStatus": 0}]))  # Older.
        visible = {"isAll": 0, "members": [{"id": "1", "name": "x"}], "departments": [], "groups": [{"id": "g"}]}
        self.assertEqual(app_console.ids(visible), {"isAll": 0, "departments": [], "members": ["1"], "groups": ["g"]})

    def test_copy_login_skips_caches_and_locks(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "src"
            (source / "Default" / "Cache").mkdir(parents=True)
            (source / "Default" / "Cache" / "big").write_text("x")
            (source / "Default" / "Cookies").write_text("c")
            (source / "SingletonLock").write_text("l")
            target = Path(tmp) / "home" / "profile"
            target.parent.mkdir()
            self.assertTrue(app_console.copy_login(source, target))
            self.assertTrue((target / "Default" / "Cookies").is_file())
            self.assertFalse((target / "Default" / "Cache").exists())
            self.assertFalse((target / "SingletonLock").exists())
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)
            self.assertFalse(app_console.copy_login(Path(tmp) / "none", Path(tmp) / "other"))

    def test_conditional_access_page_fails_fast_instead_of_timing_out(self):
        self.assertTrue(app_console.conditional_access("https://security.feishu.cn/conditionalaccess/deny?foo=1"))
        self.assertTrue(app_console.conditional_access("https://accounts.feishu.cn/x", "继续访问飞书网页版前，需先在客户端完成设备信息授权"))
        self.assertFalse(app_console.conditional_access("https://accounts.feishu.cn/accounts/page/login", "扫码登录"))
        page = SimpleNamespace(
            url="https://security.feishu.cn/conditionalaccess/deny?code=1",
            goto=lambda *a, **k: None,
            evaluate=lambda *a, **k: "继续访问飞书网页版前，需先在客户端完成设备信息授权",
            wait_for_timeout=mock.Mock(),
        )
        console = app_console.Console(page, "https://open.feishu.cn", "cli_x", "/tmp")
        with mock.patch.object(app_console, "emit") as emitted, self.assertRaises(app_console.Stop) as caught:
            console.login(seconds=300)
        self.assertEqual(caught.exception.step, "login")
        self.assertEqual(caught.exception.code, app_console.CONDITIONAL_ACCESS_CODE)
        page.wait_for_timeout.assert_not_called()
        emitted.assert_called_once()
        self.assertEqual(emitted.call_args.kwargs.get("reason"), app_console.CONDITIONAL_ACCESS_CODE)


class Runtime:
    def __init__(self, chat="oc_dm"):
        self.logs = []
        self.chat = chat
        self.access = SimpleNamespace(notify_chat=lambda: self.chat)
        self.shutdown = threading.Event()
        self.script_path = Path("/absolute/bin/covey_bridge.py")

    def log(self, line):
        self.logs.append(line)


class Messenger:
    def __init__(self):
        self.sent, self.patched = [], []

    def send_message(self, profile, chat, text, key, *, voice, card=None, on_card=None, **kwargs):
        self.sent.append((chat, text, key, card))
        if on_card:
            on_card(f"om_{len(self.sent)}")
        return True

    def patch_card(self, profile, message_id, card):
        self.patched.append((message_id, card))
        return True


def lacking_online(*names):
    keys = [k for k in FULL["menu_keys"] if k not in names]
    return {**FULL, "scopes": FULL["scopes"] - set(names), "events": FULL["events"] - set(names), "menu_keys": keys}


class SetupCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        write_json(self.state / "bot-identity.json", {"app_id": "cli_x"})
        self.ctx = SimpleNamespace(state_dir=self.state, profile="p")
        self.runtime, self.messenger = Runtime(), Messenger()
        self.voice = SimpleNamespace(safe_key=lambda key: key)
        self.setup = AppSetup(self.runtime, self.messenger, self.voice)
        env = mock.patch.dict(os.environ, SWITCHES, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for name in ("COVEY_GROUP_ALL_MESSAGES", "LARK_BRIDGE_GROUP_ALL_MESSAGES",
                     "COVEY_PINNED_OVERVIEW", "LARK_BRIDGE_PINNED_OVERVIEW"):
            os.environ.pop(name, None)
        self.online = lacking_online("im:message", "im:resource")
        reader = mock.patch.object(app_config, "online_config", lambda *a, **k: self.online)
        reader.start()
        self.addCleanup(reader.stop)
        self.slash_sync = mock.Mock(return_value={
            "ok": True, "error": "", "detail": "", "wanted": ["usage", "board", "settings"],
            "synced": ["usage", "board", "settings"], "unchanged": [], "failed": [],
        })
        syncer = mock.patch.object(slash_commands, "sync", self.slash_sync)
        syncer.start()
        self.addCleanup(syncer.stop)


class CheckTests(SetupCase):
    def test_one_card_per_set_of_missing_things(self):
        view = self.setup.check(self.ctx)
        self.assertEqual([n["id"] for n in view["offer"]], ["scope:im:message", "scope:im:resource"])
        self.assertEqual(len(self.messenger.sent), 1)
        chat, text, key, card = self.messenger.sent[0]
        self.assertEqual(chat, "oc_dm")
        self.assertIn("缺 2 项", json.dumps(card, ensure_ascii=False))
        record = read_object(self.state / "app-setup" / "state.json")
        self.assertEqual(record["card"]["message_id"], "om_1")
        self.assertTrue(record["slash"]["ok"])
        self.assertEqual(record["slash"]["synced"], ["usage", "board", "settings"])
        self.assertIsNone(self.setup.check(self.ctx))  # The same set: no second card.
        self.online = lacking_online("im:message")
        self.assertIsNotNone(self.setup.check(self.ctx))  # Another set: a new card.
        self.assertEqual(len(self.messenger.sent), 2)

    def test_missing_bottom_menu_alone_syncs_slash_commands_and_sends_no_card(self):
        self.online = {**FULL, "menu_enable": False, "menu_keys": [],
                       "events": {manifest.MESSAGE_EVENT, manifest.READ_EVENT}}
        self.assertIsNone(self.setup.check(self.ctx))
        self.slash_sync.assert_called_once_with("p")
        self.assertEqual(self.messenger.sent, [])
        record = read_object(self.state / "app-setup" / "state.json")
        self.assertEqual(record["lacking"], [])
        self.assertTrue(record["slash"]["ok"])

    def test_no_card_for_a_reviewed_scope_alone_or_without_the_chat(self):
        self.online = FULL
        with mock.patch.dict(os.environ, {"COVEY_GROUP_ALL_MESSAGES": "1"}):
            self.assertIsNone(self.setup.check(self.ctx))
        self.runtime.chat = ""
        self.online = lacking_online("im:message")
        self.assertIsNone(self.setup.check(self.ctx))
        self.assertEqual(self.messenger.sent, [])
        self.assertTrue(any("no private chat" in line for line in self.runtime.logs))

    def test_the_card_comes_after_the_first_message_not_a_day_later(self):
        self.runtime.chat = ""
        self.online = lacking_online("im:message")
        waits = []

        def fake_wait(seconds):
            waits.append(seconds)
            if seconds == app_setup.CHAT_POLL and len(waits) == 3:
                self.runtime.chat = "oc_dm"  # the owner writes during the polling
            return len(self.messenger.sent) == 1  # stop once the card went out

        self.runtime.shutdown = SimpleNamespace(wait=fake_wait)
        self.setup.run(self.ctx)
        self.assertEqual(len(self.messenger.sent), 1)
        self.assertEqual(waits[0], app_setup.FIRST_CHECK)
        self.assertIn(app_setup.CHAT_POLL, waits)
        self.assertNotIn(app_setup.CHECK_EVERY, waits[:-1])

    def test_off_switch(self):
        with mock.patch.dict(os.environ, {app_setup.ENV: "0"}):
            self.assertEqual(self.setup.threads(), ())
        self.assertEqual([name for name, _ in self.setup.threads()], ["app-setup"])


class ClickTests(SetupCase):
    def click(self, kind, *, event="ev1", message="om_1", owner=True, **value):
        return self.setup.claim(self.ctx, {"event_id": event, "message_id": message}, {"kind": kind, **value}, owner)

    def test_table_and_panels(self):
        self.assertIn(cards.START, ACTIONS)
        self.assertIn(cards.SKIP, ACTIONS)
        self.assertIn("app_setup", UnifiedBridgeApp.PANELS)
        self.assertIsNone(self.setup.claim(self.ctx, {}, {"kind": "other"}, True))

    def test_skip_once_by_the_owner_on_its_card(self):
        self.setup.check(self.ctx)
        self.assertEqual(self.click(cards.SKIP, owner=False), "owner_only")
        self.assertEqual(self.click(cards.SKIP, message="om_other"), "app_setup_stale")
        self.assertEqual(self.click(cards.SKIP, event="ev2"), "app_setup_skip")
        self.assertEqual(self.click(cards.SKIP, event="ev2"), "duplicate")
        self.assertIn("先不补", json.dumps(self.messenger.patched[-1][1], ensure_ascii=False))
        record = read_object(self.state / "app-setup" / "state.json")
        self.assertEqual(record["skipped"], record["notified"])
        self.messenger.sent.clear()
        record.pop("notified")
        write_json(self.state / "app-setup" / "state.json", record)
        self.assertIsNone(self.setup.check(self.ctx))  # Skipped: not offered again.

    def test_start_runs_once_at_a_time(self):
        self.setup.check(self.ctx)
        started = threading.Event()
        release = threading.Event()

        def apply(ctx, message_id, view, review):
            started.set()
            release.wait(5)
            self.setup.busy.release()
        with mock.patch.object(self.setup, "apply", apply):
            self.assertEqual(self.click(cards.START, review=False), "app_setup_start")
            started.wait(5)
            self.assertEqual(self.click(cards.START, event="ev2", review=False), "app_setup_busy")
            release.set()


CONSOLE = """
import json, sys, pathlib
args = sys.argv
home = pathlib.Path(args[args.index('--home') + 1]); home.mkdir(parents=True, exist_ok=True)
plan = json.loads(pathlib.Path(args[args.index('--plan') + 1]).read_text())
(home / 'qr-1.png').write_bytes(b'png')
out = [{"event": "qr", "path": str(home / 'qr-1.png'), "number": 1, "matrix": []}, {"event": "logged_in"},
       {"event": "step", "name": "read", "state": "done"}, {"event": "step", "name": "manifest", "state": "done"},
       {"event": "step", "name": "menu", "state": "done"}, {"event": "step", "name": "version", "state": "done"},
       {"event": "done", "version": "1.0.12", "status": "published", "changed": ["manifest", "menu"],
        "plan": plan, "login": '--login-from' in args}]
for line in out:
    print(json.dumps(line), flush=True)
"""


class RunTests(SetupCase):
    def test_a_run_follows_the_console_to_the_result_and_removes_its_folder(self):
        view = self.setup.check(self.ctx)
        with tempfile.TemporaryDirectory() as bin_dir:
            (Path(bin_dir) / "covey_app_console.py").write_text(textwrap.dedent(CONSOLE))
            self.runtime.script_path = Path(bin_dir) / "covey_bridge.py"
            images = []
            reads = iter([self.online, FULL])
            with mock.patch.object(app_setup, "playwright_python", lambda: sys.executable), \
                    mock.patch.object(app_config, "online_config", lambda *a, **k: next(reads)), \
                    mock.patch.object(self.setup, "reply_image", lambda ctx, mid, path, folder: images.append(path.name)):
                self.setup.busy.acquire()
                self.setup.apply(self.ctx, "om_1", view, False)
        self.assertEqual(images, ["qr-1.png"])
        final = json.dumps(self.messenger.patched[-1][1], ensure_ascii=False)
        self.assertIn("飞书应用已补齐", final)
        self.assertIn("版本 1.0.12 已发布", final)
        self.assertIn("线上版本 1.0.11", final)  # The read-back's version (FULL's), not the one before.
        self.assertTrue(any("扫这张卡下面的二维码" in json.dumps(card, ensure_ascii=False)
                            for _, card in self.messenger.patched))
        runs = self.state / "app-setup" / "runs"
        self.assertEqual(list(runs.iterdir()), [])  # The login copy, QR and plan are gone.
        self.assertTrue(self.setup.busy.acquire(blocking=False))
        record = read_object(self.state / "app-setup" / "state.json")
        self.assertEqual(record["last_run"]["outcome"], "done")

    def test_a_failed_step_and_what_is_still_missing(self):
        view = self.setup.check(self.ctx)
        failing = CONSOLE.replace('{"event": "done", "version": "1.0.12", "status": "published", "changed": '
                                  '["manifest", "menu"],\n        "plan": plan, "login": \'--login-from\' in args}',
                                  '{"event": "failed", "step": "menu", "words": "写入菜单", "detail": "报错 9499",'
                                  ' "shot": "", "changed": ["manifest"]}')
        with tempfile.TemporaryDirectory() as bin_dir:
            (Path(bin_dir) / "covey_app_console.py").write_text(textwrap.dedent(failing))
            self.runtime.script_path = Path(bin_dir) / "covey_bridge.py"
            with mock.patch.object(app_setup, "playwright_python", lambda: sys.executable), \
                    mock.patch.object(self.setup, "reply_image", lambda *a: None):
                self.setup.busy.acquire()
                self.setup.apply(self.ctx, "om_1", view, False)
        final = json.dumps(self.messenger.patched[-1][1], ensure_ascii=False)
        self.assertIn("卡在「写入菜单」：报错 9499", final)
        self.assertIn("线上版本仍然缺（2）", final)
        self.assertIn("重新扫码补齐", final)

    def test_a_start_removes_run_folders_left_mid_run(self):
        left = self.state / "app-setup" / "runs" / "20261109-000000" / "home" / "profile"
        left.mkdir(parents=True)
        (left / "Cookies").write_text("c")
        self.runtime.shutdown.set()
        self.setup.run(self.ctx)
        self.assertEqual(list((self.state / "app-setup" / "runs").iterdir()), [])

    def test_groups_are_blocks_of_their_own(self):
        offer = [need_dict(n) for n in manifest.required(SWITCHES, bottom_menu=True) if n.name in ("im:message", "bridge_settings")]
        card = cards.offer_card({"offer": offer})
        blocks = [e["content"] for e in card["body"]["elements"] if e.get("tag") == "markdown"]
        self.assertIn("**权限**\n- 撤回消息 <font color='grey'>im:message</font>", blocks)
        self.assertIn("**菜单**\n- 菜单「设置」", blocks)

    def test_no_browser(self):
        view = self.setup.check(self.ctx)
        with mock.patch.object(app_setup, "playwright_python", lambda: None):
            self.setup.busy.acquire()
            self.setup.apply(self.ctx, "om_1", view, False)
        self.assertIn("没法自动补", json.dumps(self.messenger.patched[-1][1], ensure_ascii=False))


class CardTests(unittest.TestCase):
    def test_review_button_only_with_reviewed_scopes(self):
        needs = [need_dict(n) for n in manifest.required({"COVEY_GROUP_ALL_MESSAGES": "1"})]
        offer = [n for n in needs if not n["review"]][:3]
        plain = json.dumps(cards.offer_card({"offer": offer}), ensure_ascii=False)
        self.assertIn("扫码自动补齐", plain)
        self.assertNotIn("连同需审核的一起申请", plain)
        both = json.dumps(cards.offer_card({"offer": offer, "review": [n for n in needs if n["review"]]}),
                          ensure_ascii=False)
        self.assertIn("连同需审核的一起申请", both)
        self.assertIn("im:message.group_msg", both)


class TerminalTests(unittest.TestCase):
    def test_draw_half_blocks(self):
        out = io.StringIO()
        app_check.draw(["10", "01"], out)
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 3)  # Two margin rows, the two of the QR, two margin rows: two a line.
        self.assertEqual(len(lines[0]), 6)
        self.assertEqual(lines[1][2:4], "▄▀")


if __name__ == "__main__":
    unittest.main()
