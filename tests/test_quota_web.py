"""账号与额度 on the web page (T831 W1b, docs/web.md 账号与额度, data contract v42): the ``login`` of a snapshot's account, the ``quota``
item built from the snapshots (app/web_quota.py: one row an account, only the model groups really used, the flags the sidebar and the
notices read), the page's 刷新 (the operation: only the owner, its cooldown, its thread), the item pushed, and the two notices to the
owner (app/quota_notice.py: once, held in the quiet hours)."""
from __future__ import annotations

import os
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, mock

from test_web_sync import WebCase
from covey.app import quota_notice, quota_watch, web_quota
from covey.app.web_settings import page_ops
from covey.core import quota, quiet_hours, web_sync

NOW = 1_800_000_000.0  # 16:00 in Asia/Shanghai.
NIGHT = NOW + 8 * 3600  # 00:00.


def row(alias, state="ok", five=0.5, week=0.5, back=None, **fields):
    """A snapshot's account: ``five`` and ``week`` are what is left (None: not read)."""
    windows = [{"kind": kind, "label": label, "used": None if left is None else round(1 - left, 4), "resets_at": NOW + 3600,
                "checked_at": NOW - 60} for kind, label, left in (("5h", "5 小时", five), ("7d", "周", week))]
    return {"alias": alias, "state": state, "available_at": back, "windows": windows, **fields}


def snap(*accounts, **fields):
    return quota.parse({"schema": 1, "collected_at": NOW - 30, "ttl": 600, "accounts": list(accounts), **fields})


class Base(TestCase):
    def setUp(self):
        self.state = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = quota.Store(self.state)

    def agy(self, gemini, third=None, recent=("主号",), recent3=()):
        """The two agy entries as accounts.snapshots writes them; ``third`` defaults to a copy of ``gemini``."""
        for group, accounts, used in (("gemini", gemini, recent), ("3p", third or gemini, recent3)):
            self.store.saved(f"agy.{group}", "agy", snap(*accounts), title=f"agy · {group}", now=NOW, group=group, switched=None,
                             recent=list(used))

    def view(self, now=NOW):
        return web_quota.view(self.state, now)

    def source(self, id_="agy"):
        return next(s for s in self.view()["sources"] if s["id"] == id_)


class LoginField(TestCase):
    def test_login_is_kept_only_when_it_looks_like_an_e_mail_and_is_short(self):
        kept = quota.parse({"schema": 1, "collected_at": NOW, "ttl": 600, "accounts": [
            row("A", login="main@example.com"), row("B", login="not an email"), row("C", login="x" * 80 + "@example.com"),
            row("D", login=5), row("E")]})
        self.assertEqual([a.get("login") for a in kept["accounts"]], ["main@example.com", None, None, None, None])
        self.assertEqual(kept["accounts"][0]["alias"], "A")  # The alias is scrubbed as before; the login is the one text that is not.
        with self.assertRaises(ValueError):  # An e-mail alias is no alias: the login is the field for it.
            quota.parse({"schema": 1, "collected_at": NOW, "ttl": 600, "accounts": [row("a@example.com")]})


class ViewTest(Base):
    def test_one_row_an_account_and_only_the_groups_it_ran_a_turn_in_this_week(self):
        both = [row("主号", five=0.64, week=0.71, active=True, login="main@example.com"), row("副号", five=1.0, week=0.9)]
        self.agy(both, recent=("主号",))
        source = self.source()
        self.assertEqual((source["kind"], source["title"], source["expand"], len(source["accounts"])), ("agy", "agy", True, 2))
        main, spare = source["accounts"]
        self.assertEqual(([p["key"] for p in main["pools"]], main["login"], main.get("active")), (["gemini"], "main@example.com", None))
        self.assertTrue(main["pools"][0]["active"])
        self.assertEqual([p["key"] for p in spare["pools"]], ["gemini"])  # No turn of its own: the group anyone used.
        self.assertNotIn("login", spare)  # Not read from any log: the page shows its name.
        self.assertEqual([(w["kind"], w["left"]) for w in main["pools"][0]["windows"]], [("5h", 0.64), ("7d", 0.71)])
        self.agy(both, recent=("主号",), recent3=("主号", "副号"))
        main, spare = self.source()["accounts"]
        self.assertEqual([p["label"] for p in main["pools"]], ["Gemini", "Claude/GPT"])  # Both used: one cell, two lines.
        self.assertEqual([p["key"] for p in spare["pools"]], ["3p"])
        self.agy(both, recent=(), recent3=())
        self.assertEqual([p["key"] for p in self.source()["accounts"][0]["pools"]], ["gemini"])  # Never used: Gemini.

    def test_an_account_that_needs_a_sign_in_or_is_paused_says_so_for_the_whole_row(self):
        self.agy([row("主号", state="login_required"), row("副号", state="paused"), row("三号", five=0.5)])
        source = self.source()
        self.assertEqual([a["state"] for a in source["accounts"]], ["login_required", "paused", "ok"])
        self.assertEqual(self.view()["relogin"], [{"source": "agy", "name": "主号"}])
        self.assertEqual(source["relogin"], ["主号"])

    def test_low_and_dry_say_when_a_source_is_at_its_bottom(self):
        spent = [row("主号", state="spent", five=0.0, back=NOW + 3600), row("副号", state="cooling", five=0.0, back=NOW + 600)]
        self.agy(spent)
        source = self.source()
        self.assertEqual((source["low"], source["dry"], source["next_available_at"]), (True, True, NOW + 600))
        self.agy([row("主号", five=0.1, week=0.9, active=True), row("副号", state="spent", five=0.0, back=NOW + 600)])
        self.assertEqual([self.source()[k] for k in ("low", "dry")], [True, False])  # Nothing with 20% left, but one can be used.
        self.agy([row("主号", five=0.1), row("副号", five=0.6)])
        self.assertEqual([self.source()[k] for k in ("low", "dry")], [False, False])
        self.agy([row("主号", state="login_required"), row("副号", state="login_required")])
        self.assertEqual([self.source()[k] for k in ("low", "dry")], [False, False])  # A sign-in, not a shortage: the other notice.

    def test_a_gateway_folds_and_a_command_source_opens_when_something_needs_the_owner(self):
        self.store.saved("gateway.x", "gateway", snap(row("A1", five=0.78), row("A2", state="cooling", five=0.0, back=NOW + 900),
                                                     row("A3", five=0.1, week=0.1), link="https://ops.example.com/x",
                                                     pool={"usable": 2, "total": 3, "next_available_at": None}),
                         title="Claude 网关", now=NOW)
        self.store.saved("vps", "command", snap(row("V1", five=None, week=0.57), row("V2", state="login_required")), title="我的 VPS",
                         now=NOW)
        self.store.saved("calm", "url", snap(row("C1")), title="平静", now=NOW)
        self.store.failed("nothing", "command", "boom", title="没读到", now=NOW)
        self.store.failed("gone", "gateway", "404", absent=True, now=NOW)
        self.store.saved("was", "gateway", snap(row("W1")), title="答过的网关", now=NOW - 900)
        self.store.failed("was", "gateway", "HTTP 404", absent=True, now=NOW)  # As an older Covey wrote it (T974).
        sources = {s["id"]: s for s in self.view()["sources"]}
        self.assertNotIn("gone", sources)  # A gateway without the interface is not there.
        self.assertEqual((sources["was"]["failing"], sources["was"]["why"], len(sources["was"]["accounts"])), (True, "HTTP 404", 1))
        self.assertEqual((sources["nothing"]["why"], sources["calm"]["why"]), ("boom", ""))  # What the page says under the title.
        gateway = sources["gateway.x"]
        self.assertEqual((gateway["usable"], gateway["total"], gateway["link"], gateway["expand"]), (2, 3, "https://ops.example.com/x", False))
        self.assertEqual((sources["vps"]["expand"], sources["calm"]["expand"], sources["vps"]["relogin"]), (True, False, ["V2"]))
        self.assertEqual(sources["vps"]["accounts"][0]["pools"][0]["windows"][0]["left"], None)  # Not read: never 0.
        self.assertEqual((sources["nothing"]["accounts"], sources["nothing"]["failing"]), ([], True))
        self.assertEqual([k for k in ("link",) if k in sources["vps"]], [])

    def test_texts_pass_the_whitelist_but_the_login_does_not(self):
        self.store.saved("evil", "command", snap(row("A1", login="me@example.com")), title="https://x.example.com/secret", now=NOW)
        entry = self.store.read("evil")
        entry["title"] = "see /home/tiger/key and 203.0.113.7"
        self.store._write("evil", entry)
        source = self.source("evil")
        self.assertNotIn("/home", source["title"])
        self.assertNotIn("203.0.113.7", source["title"])
        self.assertEqual(source["accounts"][0]["login"], "me@example.com")

    def test_the_item_carries_the_refresh_record(self):
        self.agy([row("主号")])
        self.assertEqual(self.view()["refresh"], {"at": 0, "busy": False, "cooldown": 300})
        (self.state / "quota" / web_quota.REFRESH_FILE).write_text('{"at": %s, "done_at": 0}' % (NOW - 30))
        self.assertTrue(self.view()["refresh"]["busy"])
        self.assertFalse(web_quota.view(self.state, NOW + 5000)["refresh"]["busy"])  # Begun long ago: not still going.
        self.assertEqual(self.view()["read_at"], NOW)
        before = web_quota.stamps(self.state)
        self.assertEqual(before, web_quota.stamps(self.state))
        self.store.saved("agy.gemini", "agy", snap(row("主号")), now=NOW + 1, group="gemini", recent=[])
        self.assertNotEqual(before, web_quota.stamps(self.state))
        self.assertEqual(self.view()["sources"][0]["id"], "agy")

    def test_nothing_to_show_is_an_empty_item(self):
        self.assertEqual(self.view()["sources"], [])


class RefreshTest(Base):
    def refresher(self, clock, **kw):
        self.logs = []
        return web_quota.Refresher(self.state, command=lambda: ["agy"], log=self.logs.append, environ={"HOME": str(self.state)},
                                   clock_=lambda: clock[0], **kw)

    def test_it_reads_every_source_and_asks_every_account_in_a_thread_and_cools_down_five_minutes(self):
        clock = [NOW]
        refresher = self.refresher(clock)
        calls = []
        with mock.patch("covey.core.quota_sources.enabled", return_value=True), \
                mock.patch("covey.core.quota_sources.sources", return_value=(["s"], "")), \
                mock.patch("covey.core.quota_sources.refresh", lambda state, found, **kw: calls.append(("refresh", found, kw))), \
                mock.patch("covey.backends.agy.accounts.configured", return_value=True), \
                mock.patch("covey.backends.agy.accounts.probe_accounts", lambda *a, **kw: calls.append(("probe", a[2:], kw))), \
                mock.patch("covey.backends.agy.accounts.snapshots", lambda *a, **kw: calls.append(("snapshots",))):
            self.assertIn("开始重新读取", refresher.ask())
            refresher.thread.join(10)
            self.assertEqual([c[0] for c in calls], ["refresh", "probe", "snapshots"])
            self.assertEqual(calls[0][2], {"force": True})  # Whatever its time.
            self.assertEqual(calls[1][2], {"which": "all"})  # agy's /usage of every account, the spare ones too.
            self.assertFalse(self.view()["refresh"]["busy"])
            clock[0] = NOW + 120
            self.assertIn("刚查过", refresher.ask())  # Inside the cooldown: nothing is read again.
            self.assertEqual(len(calls), 3)
            clock[0] = NOW + 299
            self.assertIn("刚查过", refresher.ask())
            clock[0] = NOW + 301
            self.assertIn("开始重新读取", refresher.ask())
            refresher.thread.join(10)
            self.assertEqual(len(calls), 6)

    def test_a_reading_still_going_is_not_begun_again_and_one_that_fails_is_logged_and_ends(self):
        clock, gate = [NOW], threading.Event()
        refresher = self.refresher(clock)
        with mock.patch("covey.core.quota_sources.enabled", return_value=True), \
                mock.patch("covey.core.quota_sources.sources", return_value=([], "")), \
                mock.patch("covey.core.quota_sources.refresh", lambda *a, **kw: gate.wait(10) and (_ for _ in ()).throw(OSError("x"))):
            refresher.ask()
            clock[0] = NOW + 400
            self.assertIn("正在重新读取", refresher.ask())
            gate.set()
            refresher.thread.join(10)
        self.assertEqual(self.logs, ["quota refresh failed: OSError"])
        self.assertFalse(self.view()["refresh"]["busy"])  # The record says it ended.

    def test_the_command_is_the_agy_harnesses_own(self):
        apps = {"agy": SimpleNamespace(harness=SimpleNamespace(context=SimpleNamespace(backend_cmd=["/x/agy", "--a"])))}
        self.assertEqual(web_quota.agy_command(apps), ["/x/agy", "--a"])
        self.assertEqual(web_quota.agy_command({}), [])
        self.assertEqual(web_quota.agy_command(None), [])


class PageTest(WebCase):
    """The item pushed on its own, the operation, ``ops``."""

    def setUp(self):
        super().setUp()
        self.runtime.access.is_owner = lambda open_id: open_id == "ou_owner"
        self.quota = quota.Store(self.state)
        self.enterContext(mock.patch.object(quota_watch, "wanted", return_value=True))

    def pushed(self):
        return [each for batch in self.web.upserts for each in batch["items"] if each["key"] == "quota"]

    def put(self, **kw):
        self.quota.saved("agy.gemini", "agy", snap(row("主号", login="main@example.com", active=True), row("副号")), title="agy · Gemini",
                         now=time.time(), group="gemini", recent=["主号"])

    def test_pushed_when_a_snapshot_changed_and_dropped_when_the_sources_are_gone(self):
        self.assertEqual(self.sync.push_quota(full=True), 0)  # Never a source: nothing is pushed.
        self.put()
        self.assertEqual(self.sync.push_quota(), 1)
        first = self.pushed()[-1]
        self.assertEqual(first["type"], "settings")
        self.assertEqual(first["data"]["sources"][0]["accounts"][0]["login"], "main@example.com")
        self.assertEqual(self.sync.push_quota(), 0)  # Nothing changed.
        self.assertEqual(self.sync.push_quota(full=True), 0)  # The same data: the rev is the same, nothing is sent again.
        self.quota.forget("agy.gemini")
        self.assertEqual(self.sync.push_quota(), 1)
        self.assertEqual(self.pushed()[-1]["data"]["sources"], [])  # The page drops its tab.

    def test_a_page_that_refuses_the_key_is_logged_and_tried_again(self):
        self.put()
        with mock.patch.object(self.sync.client, "request", side_effect=OSError("refused")):
            self.assertEqual(self.sync.push_quota(), 0)
        self.assertTrue(any("web quota not pushed (1)" in line for line in self.logs))
        self.assertEqual(self.sync.push_quota(), 1)

    def test_ops_list_the_refresh_only_with_a_source_to_read(self):
        self.assertIn("quota_refresh", page_ops())
        with mock.patch.object(quota_watch, "wanted", return_value=False):
            self.assertNotIn("quota_refresh", page_ops())
            self.assertEqual(self.sync.ops.run({"kind": "quota_refresh", "op_id": "o"})[0], "failed")
        self.assertIn("quota_refresh", web_sync.KINDS)

    def test_the_operation_is_the_owners_alone_and_takes_neither_topic_nor_project(self):
        self.runtime.owner_open_id = "ou_someone_else"
        state, message = self.sync.ops.run({"kind": "quota_refresh", "op_id": "o1", "payload": {}})
        self.assertEqual((state, message), ("failed", "只有主人能刷新额度"))
        self.runtime.owner_open_id = "ou_owner"
        for key in ("topic_key", "project_key"):
            self.assertEqual(self.sync.ops.run({"kind": "quota_refresh", "op_id": "o2", key: "t1", "payload": {}})[0], "failed")

    def test_the_page_is_told_when_the_reading_began_and_when_it_just_did_before_the_ack(self):
        self.put()
        self.sync.push_quota()
        started = threading.Event()
        with mock.patch("covey.core.quota_sources.enabled", return_value=True), \
                mock.patch("covey.core.quota_sources.sources", return_value=([], "")), \
                mock.patch("covey.core.quota_sources.refresh", lambda *a, **kw: started.wait(10)), \
                mock.patch("covey.backends.agy.accounts.configured", return_value=False), \
                mock.patch("covey.backends.agy.accounts.snapshots", lambda *a, **kw: []):
            self.web.op("quota_refresh", 1, "op-q1")
            self.assertEqual(self.sync.inbox_once(), 1)
            self.assertEqual((self.web.acks[-1]["state"], self.web.acks[-1]["message"][:6]), ("done", "开始重新读取"))
            self.assertTrue(self.pushed()[-1]["data"]["refresh"]["busy"])  # The page reads it when it sees done.
            started.set()
            self.sync.ops.quota_refresher.thread.join(10)
            self.web.op("quota_refresh", 2, "op-q2")
            self.sync.inbox_once()
            self.assertIn("刚查过", self.web.acks[-1]["message"])  # Inside the cooldown.
            self.assertFalse(self.pushed()[-1]["data"]["refresh"]["busy"])


class NoticeTest(Base):
    def setUp(self):
        super().setUp()
        self.sent, self.result = [], True
        self.enterContext(mock.patch.dict(os.environ, {quiet_hours.ENV: "23:00-08:30", web_sync.ENV: ""}))
        self.notice = quota_notice.Notice(self.state, self.send, log=lambda text: None)

    def send(self, text):
        self.sent.append(text)
        return self.result

    def step(self, now=NOW):
        return self.notice.step(now)

    def dry(self):
        self.agy([row("主号", state="spent", five=0.0, back=NOW + 3600), row("副号", state="cooling", five=0.0, back=NOW + 600)])

    def fine(self):
        self.agy([row("主号"), row("副号")])

    def test_the_state_a_directory_starts_with_is_not_news(self):
        self.dry()
        self.assertEqual(self.step(), "")
        self.assertEqual(self.sent, [])
        self.assertEqual(self.step(), "")  # Nor the next look.

    def test_a_source_with_no_account_to_use_is_said_once_with_when_the_first_comes_back_and_again_when_it_comes_back_to_it(self):
        self.fine()
        self.step()
        self.dry()
        said = self.step(NOW + 60)
        self.assertEqual(self.sent, [said])
        self.assertIn("agy 现在没有能用的号，最早 ", said)
        self.assertIn(web_quota.clock(NOW + 600), said)
        self.assertEqual(self.step(NOW + 120), "")
        self.assertEqual(self.step(NOW + 180), "")
        self.assertEqual(len(self.sent), 1)  # Not twice for the same thing.
        self.fine()
        self.step(NOW + 240)
        self.dry()
        self.step(NOW + 300)
        self.assertEqual(len(self.sent), 2)  # It stopped being so and is so again: news.

    def test_an_account_that_needs_a_sign_in_is_said_once_by_the_e_mail_or_the_name_and_the_page_is_linked(self):
        self.fine()
        self.step()
        self.agy([row("主号", state="login_required", login="main@example.com"), row("副号", state="login_required")])
        with mock.patch.dict(os.environ, {web_sync.ENV: "1"}), mock.patch.object(web_sync, "page_url", return_value="https://covey.example.com/"):
            said = self.step(NOW + 60)
        self.assertEqual(self.sent, [said])
        self.assertIn("agy 的 main@example.com 要重新登录", said)
        self.assertIn("agy 的 副号 要重新登录", said)
        self.assertTrue(said.endswith("去网页看：https://covey.example.com/settings/quota"), said)
        self.assertEqual(self.step(NOW + 120), "")
        self.assertNotIn("现在没有能用的号", said)  # All of them need a sign-in: one notice, not two.

    def test_at_night_nothing_is_sent_or_recorded_and_the_morning_says_what_is_still_so_in_one_message(self):
        self.fine()
        self.step()
        self.dry()
        self.assertEqual(self.step(NIGHT), "")
        self.assertEqual(self.sent, [])
        self.agy([row("主号", state="login_required"), row("副号", state="cooling", five=0.0, back=NIGHT + 60)])
        self.assertEqual(self.step(NIGHT + 600), "")
        morning = NOW + 17 * 3600  # 09:00.
        self.agy([row("主号", state="login_required"), row("副号", state="cooling", five=0.0, back=morning + 600)])
        said = self.step(morning)
        self.assertEqual(self.sent, [said])
        self.assertIn("现在没有能用的号", said)
        self.assertIn("要重新登录", said)

    def test_a_send_that_failed_or_had_no_chat_is_tried_at_the_next_look(self):
        self.fine()
        self.step()
        self.dry()
        self.result = False
        self.assertEqual(self.step(NOW + 60), "")
        self.result = None
        self.assertEqual(self.step(NOW + 120), "")
        self.result = True
        self.assertIn("没有能用的号", self.step(NOW + 180))
        self.assertEqual(len(self.sent), 3)
        self.assertEqual(self.step(NOW + 240), "")

    def test_the_watch_sends_through_the_bridge_and_without_one_says_nothing(self):
        sent = []
        self.enterContext(mock.patch.dict(os.environ, {quiet_hours.ENV: "off"}))  # The watch reads the real clock.
        runtime = SimpleNamespace(log=lambda text: None, access=SimpleNamespace(notify_chat=lambda: "oc_chat"))
        messenger = SimpleNamespace(send_message=lambda profile, chat, text, key, **kw: sent.append((chat, text)) or True)
        voice = SimpleNamespace(safe_key=lambda key: key)
        ctx = SimpleNamespace(state_dir=self.state, profile="p")
        watch = quota_watch.QuotaWatch(runtime, messenger, voice)
        self.fine()
        with mock.patch("covey.app.pins.remember_notice"):
            self.assertEqual(watch.tell(ctx), "")
            self.dry()
            self.assertIn("没有能用的号", watch.tell(ctx))
        self.assertEqual([chat for chat, _ in sent], ["oc_chat"])
        self.assertEqual(quota_watch.QuotaWatch(runtime).tell(ctx), "")
