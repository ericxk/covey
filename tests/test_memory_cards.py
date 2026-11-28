"""记忆在飞书上的收窄 (M4): the answer card's 记下了 line (no 升为全局, 不对 and 改), the evening and morning digest's 待审核 line, the older
cards' clicks that are offline now (升为全局 on an answer card, 删 on a 专项卡's memory row) and the project card's count of its memory
(app/project_facts.py). T908 M3b: the answer card's 采纳 on the two kinds of 待审核 (the 设置 card's kind), 不对 that closes the item with
the rule, and the lead's "负责人记下了" line. The 设置 card's 记忆 tab is tests/test_memory_panel.py's."""
from __future__ import annotations

import datetime
import json
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import test_digest
from support.records import state_dir, write_job
from support.world import UnifiedCase

from covey.app import memory_collect, project_clicks, project_facts
from covey.app.digest import ZONE, Digest, review_view
from covey.app.task_controls import TaskControls
from covey.cards import answer_card as cards
from covey.cards.digest import digest_card, digest_text
from covey.cards.project_card import memory_line
from covey.core import memory_review, memstore, principles, projects
from covey.feishu.messenger import Voice

URL = "https://covey.example.test"
NOTE = {"kind": "remembered", "text": "选型类的事不用先问侧重点，直接开工", "scope": "管这个专项", "entry_id": "a1b2c3"}


def text_of(value):
    return json.dumps(value, ensure_ascii=False)


def open_urls(value):
    found = []

    def walk(node):
        if isinstance(node, dict):
            for behavior in node.get("behaviors") or ():
                if behavior.get("type") == "open_url":
                    found.append((node["text"]["content"], behavior["default_url"]))
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)
    walk(value)
    return found


class AnswerCardTests(unittest.TestCase):
    def test_the_line_has_no_promote_button_and_edits_only_with_the_web_page(self):
        with mock.patch("covey.core.web_sync.page_url", return_value=""):
            plain = cards.memory_element([NOTE], "t1", 7)
        text = text_of(plain)
        self.assertIn("记下了：选型类的事不用先问侧重点，直接开工 · 管这个专项", text)
        self.assertNotIn("升为全局", text)
        self.assertNotIn(cards.MEMORY_PROMOTE_KIND, text)
        self.assertEqual(text.count('"不对"'), 1)
        self.assertEqual(open_urls(plain), [])  # No page, no 改.
        with mock.patch("covey.core.web_sync.page_url", return_value=URL):
            linked = cards.memory_element([NOTE], "t1", 7)
        self.assertEqual(open_urls(linked), [("改", URL + "/memory")])  # A note without its code: the page's 记忆.
        with mock.patch("covey.core.web_sync.page_url", return_value=URL):
            coded = cards.memory_element([{**NOTE, "code": "R4K2M"}], "t1", 7)
        self.assertEqual(open_urls(coded), [("改", URL + "/memory?code=R4K2M")])  # That entry, opened on the page.
        self.assertIn('"kind": "bridge_memory_wrong"', text_of(linked))  # 不对 is the same button as it was.
        self.assertNotIn("升为全局", text_of(linked))

    def test_the_title_is_said_when_the_note_has_one_else_the_text(self):
        with mock.patch("covey.core.web_sync.page_url", return_value=""):
            titled = text_of(cards.memory_element([{**NOTE, "title": "选型不先问", "code": "R4K2M"}], "t1", 7))
            scoped = text_of(cards.memory_element([{**NOTE, "scope": "管全局"}], "t1", 7))
        self.assertIn("记下了：选型不先问 · 管这个专项", titled)
        self.assertNotIn("直接开工", titled)
        self.assertIn("· 管全局", scoped)

    def test_voided_and_undone_lines_have_no_buttons(self):
        with mock.patch("covey.core.web_sync.page_url", return_value=URL):
            element = cards.memory_element([{**NOTE, "kind": "voided"}, {**NOTE, "kind": "undone"}], "t1", 7)
        self.assertEqual(open_urls(element), [])
        self.assertNotIn("button", text_of(element))


class AcceptLineTests(unittest.TestCase):
    """采纳 and 不对 on the answer card's lines that wait in 待审核 (app/memory_collect.py accept_click, adopt, take_back)."""

    def setUp(self):
        self.state = state_dir(self)
        memstore._cache.clear()
        projects.create(self.state, projects.new_record(chat_id="oc_dm", name="记忆专项", project_id="a1b2c3d4e5f6", now=1.0))
        self.shown = []
        self.ctx = SimpleNamespace(state_dir=self.state, profile="p")
        self.runtime = SimpleNamespace(log=lambda line: None)
        patcher = mock.patch.object(memory_collect, "after_ack", lambda work: work())
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(memory_collect.answer_cards, "put_memory",
                                    lambda state, profile, messenger, key, element, log, **kw: self.shown.append(element))
        patcher.start()
        self.addCleanup(patcher.stop)

    def job(self, notes):
        write_job(self.state, "k-1", topic_id="t1", chat_id="oc_dm", job_id=7, status="done", memory_notes=notes)
        return memory_collect.find_job_record(self.state, "k-1")

    def click(self, entry_id, kind=cards.MEMORY_ACCEPT_KIND):
        event = {"event_id": "ev1", "chat_id": "oc_dm", "message_id": "om_1",
                 "action_value": {"kind": kind, "topic_id": "t1", "job_id": "7", "entry_id": entry_id}}
        return memory_collect.accept_click(self.runtime, object(), self.ctx, event)

    def test_a_rule_held_here_is_one_line_with_accept_and_accept_moves_it_to_global(self):
        here = memstore.add(self.state, kind="rule", scope="topic", owner_key="t1", text="回复都用中文", now=1.0)
        key = memory_review.add(self.state, memory_review.ReviewItem(key="", type="learn", title="", text=here.text, created_at=1.0,
                                                                      suggest={"kind": "rule", "scope": "global"}, items=(here.ref,)))[0]
        note = {"kind": "remembered", "text": here.text, "scope": "管这个话题", "entry_id": memstore.parse_ref(here.ref)[2],
                "ref": here.ref, "code": here.code, "review": key}
        self.job([note])
        with mock.patch("covey.core.web_sync.page_url", return_value=""):
            line = text_of(cards.memory_element([note], "t1", 7))
        self.assertIn("记下了：回复都用中文 · 先管这个话题，要管全局吗", line)
        self.assertEqual((line.count('"采纳"'), line.count('"不对"')), (1, 1))
        self.assertEqual(self.click(note["entry_id"]), "memory_accept")
        moved = memstore.find(self.state, here.code)
        self.assertEqual((moved.scope, moved.said), ("global", "owner"))
        [after] = memory_collect.find_job_record(self.state, "k-1")["memory_notes"]
        self.assertEqual((after["kind"], after["scope"], after["ref"], "review" in after), ("remembered", "管全局", moved.ref, False))
        self.assertIn("记下了：回复都用中文 · 管全局", text_of(self.shown[-1]))
        self.assertNotIn('"采纳"', text_of(self.shown[-1]))
        self.assertEqual(self.click(after["entry_id"]), "memory_accept_done")  # A second click does nothing.
        # 不对 on what was accepted voids it (T908 M3b).
        record = memory_collect.find_job_record(self.state, "k-1")
        memory_collect.take_back(self.state, "t1", record, after["entry_id"])
        self.assertEqual(memstore.find(self.state, moved.ref).status, "void")

    def test_wrong_before_accept_takes_the_rule_back_and_turns_its_item_down(self):
        here = memstore.add(self.state, kind="rule", scope="topic", owner_key="t1", text="改完先跑全量", now=1.0)
        key = memory_review.add(self.state, memory_review.ReviewItem(key="", type="learn", title="", text=here.text, created_at=1.0,
                                                                      suggest={"kind": "rule", "scope": "global"}, items=(here.ref,)))[0]
        note = {"kind": "remembered", "text": here.text, "scope": "管这个话题", "entry_id": memstore.parse_ref(here.ref)[2],
                "ref": here.ref, "review": key}
        record = self.job([note])
        [after] = memory_collect.take_back(self.state, "t1", record, note["entry_id"])
        self.assertEqual(after["kind"], "undone")
        self.assertEqual(memstore.find(self.state, here.ref).status, "void")
        item = next(row for row in memstore.review_items(self.state, states=None) if row["key"] == key)
        self.assertEqual((item["state"], item["decided_by"]), ("rejected", "owner"))

    def test_a_grant_rule_waits_on_its_line_and_holds_once_accepted(self):
        key = memory_review.add(self.state, memory_review.ReviewItem(
            key="", type="learn", title="", text="改完直接部署到本机", created_at=1.0,
            suggest={"kind": "rule", "scope": "topic", "owner_key": "t1", "grant": "deploy"}))[0]
        note = {"kind": "pending", "text": "改完直接部署到本机", "scope": memory_collect.GRANT_WORDS, "entry_id": key}
        self.job([note])
        with mock.patch("covey.core.web_sync.page_url", return_value=""):
            line = text_of(cards.memory_element([note], "t1", 7))
        self.assertIn("待审核：改完直接部署到本机 · " + memory_collect.GRANT_WORDS, line)
        self.assertEqual((line.count('"采纳"'), line.count('"不对"')), (1, 1))
        self.assertEqual(memstore.entries(self.state, kinds=("rule",)), [])
        self.click(key)
        [entry] = memstore.entries(self.state, kinds=("rule",))
        self.assertEqual((entry.text, entry.said, entry.scope), ("改完直接部署到本机", "owner", "topic"))
        [after] = memory_collect.find_job_record(self.state, "k-1")["memory_notes"]
        self.assertEqual((after["kind"], after["ref"], after["scope"]), ("remembered", entry.ref, "管这个话题"))

    def test_an_older_cards_two_lines_become_one_and_an_item_decided_elsewhere_says_so(self):
        here = memstore.add(self.state, kind="rule", scope="topic", owner_key="t1", text="汇报先给结论", now=1.0)
        key = memory_review.add(self.state, memory_review.ReviewItem(key="", type="learn", title="", text=here.text, created_at=1.0,
                                                                      suggest={"kind": "rule", "scope": "global"}, items=(here.ref,)))[0]
        notes = [{"kind": "remembered", "text": here.text, "scope": "管这个话题", "entry_id": memstore.parse_ref(here.ref)[2], "ref": here.ref},
                 {"kind": "pending", "text": here.text, "scope": memory_collect.WIDER_WORDS, "entry_id": key}]
        self.job(notes)
        self.click(key)
        [after] = memory_collect.find_job_record(self.state, "k-1")["memory_notes"]
        self.assertEqual((after["kind"], after["scope"]), ("remembered", "管全局"))
        other = memory_review.add(self.state, memory_review.ReviewItem(key="", type="learn", title="", text="别的", created_at=1.0,
                                                                        suggest={"kind": "rule", "scope": "global"}))[0]
        self.job([{"kind": "pending", "text": "别的", "scope": memory_collect.WIDER_WORDS, "entry_id": other}])
        memory_review.decide(self.state, other, "reject")
        with self.assertRaisesRegex(ValueError, "处理过了"):
            self.click(other)
        with self.assertRaisesRegex(ValueError, "找不到"):
            memory_collect.accept_click(self.runtime, object(), self.ctx, {"chat_id": "oc_other", "action_value": {
                "kind": cards.MEMORY_ACCEPT_KIND, "topic_id": "t1", "job_id": "7", "entry_id": other}})

    def test_the_leads_line_says_who_wrote_it_and_shows_with_the_turns_lines(self):
        lead = {"kind": "remembered", "by": "lead", "text": "发版先灰度", "scope": "管这个专项", "entry_id": "a1", "ref": "project:p:a1"}
        with mock.patch("covey.core.web_sync.page_url", return_value=""):
            line = text_of(cards.memory_element([lead], "t1", 7))
        self.assertIn("负责人记下了：发版先灰度 · 管这个专项", line)
        self.assertEqual((line.count('"不对"'), line.count('"采纳"')), (1, 0))
        write_job(self.state, "k-2", topic_id="t1", chat_id="oc_dm", job_id=8, status="done", message_id="k-2", lead_notes=[lead])
        with mock.patch.object(memory_collect.memory, "enabled", return_value=True), \
                mock.patch.object(memory_collect.memory_observe, "observe"):
            memory_collect.collect(SimpleNamespace(log=lambda line: None), object(), SimpleNamespace(message_id="k-2", job_id=8), self.ctx)
        self.assertIn("负责人记下了：发版先灰度", text_of(self.shown[-1]))


class OfflineClickTests(UnifiedCase):
    def test_promote_on_an_older_answer_card_says_it_is_offline_and_starts_nothing(self):
        principles.save(self.ctx.state_dir, principles.OWNER, "回答先给结论")
        reply = mock.Mock()
        host = SimpleNamespace(messenger=SimpleNamespace(reply=reply), voice=Voice("lark-bridge", "LARK_BRIDGE"))
        event = {"event_id": "ev1", "chat_id": "oc_dm", "message_id": "om_1",
                 "action_value": {"kind": "bridge_memory_promote", "topic_id": "t1", "job_id": "3", "entry_id": "a1b2c3"}}
        self.assertTrue(TaskControls.memory_promote(host, self.ctx, event))
        self.assertEqual(reply.call_args.args[2], "升为全局已下线：到网页记忆中心把它提到更大的范围")
        self.assertEqual([row["text"] for row in principles.entries(self.ctx.state_dir)], ["回答先给结论"])

    def test_the_project_card_delete_says_it_is_offline_in_the_words_asked(self):
        reply = mock.Mock()
        host = SimpleNamespace(messenger=SimpleNamespace(reply=reply), voice=Voice("lark-bridge", "LARK_BRIDGE"))
        event = {"event_id": "ev1", "chat_id": "oc_dm", "message_id": "om_1", "action_value": {"entry": "m1"}}
        self.assertTrue(project_clicks.delete_memory(host, self.ctx, event))
        self.assertEqual(reply.call_args.args[2], "删除已下线：到网页记忆中心收起或作废")


class ProjectMemoryTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        patcher = mock.patch.dict("os.environ", {projects.ENV: "1", projects.MEMORY_ENV: "1"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.pid = "a1b2c3d4e5f6"
        projects.create(self.state, projects.new_record(chat_id="oc_dm", name="网关", goal="发出去", project_id=self.pid, now=1.0))

    def test_the_counts_are_of_the_projects_memory_in_use_and_the_items_waiting_for_it(self):
        add = lambda **kw: memstore.add(self.state, scope="project", owner_key=self.pid, text=kw["title"] + "，照这个做", **kw)  # noqa: E731
        add(kind="rule", title="发布前跑冒烟")
        add(kind="rule", title="不要动 8443")
        add(kind="note", title="网关端口")
        gone = add(kind="rule", title="旧的约定")
        memstore.archive(self.state, gone.ref)
        memstore.add_decision(self.state, scope="project", owner_key=self.pid, question="用哪个版本？", answer="用 2.4", title="版本")
        for scope, key in (("project", self.pid), ("project", "0f9e8d7c6b5a"), ("global", "")):
            memstore.review_put(self.state, {"type": "learn", "title": f"待定 {scope}{key}", "text": "x",
                                             "suggest": {"kind": "rule", "scope": scope, **({"project_key": key} if key else {})}})
        self.assertEqual(project_facts.memory_counts(self.state, self.pid), (2, 1))  # Rules in use, items waiting: no 决定, 资料.

    def test_the_line_is_in_the_projects_facts_with_the_web_page_address(self):
        memstore.add(self.state, kind="rule", scope="project", owner_key=self.pid, title="发布前跑冒烟", text="发布前跑一遍冒烟")
        with mock.patch("covey.core.web_sync.page_url", return_value=URL):
            shown = project_facts.facts(self.state, projects.read_record(self.state, self.pid), 1100.0, {})
        self.assertEqual((shown.memory_counts, shown.memory_url), ((1, 0), f"{URL}/memory?scope=p%3A{self.pid}"))
        line = memory_line(shown)["content"]
        self.assertIn("记忆 · 规矩 1", line)
        self.assertNotIn("决定", line)
        self.assertNotIn("资料", line)  # Not to be read as the 资料库 under it.
        self.assertIn(f"{URL}/memory?scope=p%3A{self.pid}", line)  # This project's group on the page.

    def test_a_memory_that_cannot_be_read_is_no_counts_not_a_failure(self):
        with mock.patch.object(project_facts.memstore, "entries", side_effect=OSError("nfs")):
            self.assertEqual(project_facts.memory_counts(self.state, self.pid), ())


VIEW = {"slot": test_digest.at(8, 9, 30), "since": test_digest.at(7, 23, 30), "waiting": [], "running": [], "done": []}


class DigestReviewTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict("os.environ", {"COVEY_MEMORY": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_memory_off_has_no_line(self):
        with mock.patch.dict("os.environ", {"COVEY_MEMORY": "0"}), \
                mock.patch("covey.app.web_memory.overview", return_value={"counts": {"pending": 2}}) as overview:
            self.assertEqual(review_view("state"), {})
        overview.assert_not_called()

    def test_the_line_says_how_many_wait_and_links_to_the_review_tab(self):
        view = {**VIEW, "review": {"pending": 3, "url": URL + "/memory/review"}}
        self.assertIn("记忆：待审核 3 条 · [去网页处理](" + URL + "/memory/review)", test_digest.words(digest_card(view)))
        self.assertIn("记忆：待审核 3 条 · " + URL + "/memory/review", digest_text(view))
        bare = {**VIEW, "review": {"pending": 3, "url": ""}}
        self.assertIn("记忆：待审核 3 条", test_digest.words(digest_card(bare)))
        self.assertNotIn("去网页处理", text_of(digest_card(bare)))

    def test_no_line_when_nothing_waits_or_the_view_has_none(self):
        for view in (VIEW, {**VIEW, "review": {"pending": 0, "url": URL}}):
            self.assertNotIn("待审核", text_of(digest_card(view)))
            self.assertNotIn("待审核", digest_text(view))

    def test_review_view_reads_the_overview_and_is_empty_when_it_fails(self):
        with mock.patch("covey.core.web_sync.page_url", return_value=URL), \
                mock.patch("covey.app.web_memory.overview", return_value={"counts": {"pending": 2}}):
            self.assertEqual(review_view("state"), {"review": {"pending": 2, "url": URL + "/memory"}})
        with mock.patch("covey.app.web_memory.overview", return_value={"counts": {"pending": 0}}):
            self.assertEqual(review_view("state"), {})
        logs = []
        with mock.patch("covey.app.web_memory.overview", side_effect=OSError("nfs")):
            self.assertEqual(review_view("state", logs.append), {})
        self.assertEqual(logs, ["digest review count unreadable: OSError"])


class DigestTickTests(test_digest.Records, unittest.TestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict("os.environ", {"COVEY_MEMORY": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ctx = SimpleNamespace(state_dir=self.state, profile="test-bot")
        self.logs = []
        self.runtime = SimpleNamespace(log=self.logs.append, shutdown=threading.Event(),
                                       access=SimpleNamespace(owner_chat=lambda: test_digest.CHAT, notify_chat=lambda: test_digest.CHAT))
        self.messenger = test_digest.FakeMessenger()
        self.feature = Digest(self.runtime, self.messenger, Voice("lark-bridge", "LARK_BRIDGE"))
        self.feature.link = lambda ctx, chat, text, cards=None: text
        self.feature.url = lambda ctx, topic, card="": f"https://link/{topic}"
        local = datetime.datetime.fromtimestamp(self.now, ZONE)
        self.slot = local.replace(second=0, microsecond=0).timestamp()
        self.times = [local.hour * 60 + local.minute]
        self.topic("run", 403)
        self.job("j-run", 2, "running", "run", timings={"backend_started": time.time() - 300})

    def test_a_digest_that_goes_out_has_the_line_and_one_that_cannot_read_the_memory_goes_out_without_it(self):
        with mock.patch("covey.core.web_sync.page_url", return_value=URL), \
                mock.patch("covey.app.web_memory.overview", return_value={"counts": {"pending": 2}}):
            self.feature.tick(self.ctx, self.times, now=self.slot + 5)
        self.assertIn("记忆：待审核 2 条 · [去网页处理](" + URL + "/memory)", text_of(self.messenger.sends[0]["card"]))
        self.assertIn("记忆：待审核 2 条", self.messenger.sends[0]["text"])
        self.assertEqual(self.messenger.sends[0]["card"]["body"]["elements"].count(None), 0)

    def test_an_unreadable_memory_leaves_the_line_out_and_the_digest_still_goes(self):
        with mock.patch("covey.app.web_memory.overview", side_effect=OSError("nfs")):
            self.feature.tick(self.ctx, self.times, now=self.slot + 5)
        self.assertEqual(len(self.messenger.sends), 1)
        self.assertNotIn("待审核", text_of(self.messenger.sends[0]["card"]))
        self.assertIn("digest review count unreadable: OSError", self.logs)


if __name__ == "__main__":
    unittest.main()
