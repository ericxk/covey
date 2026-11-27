"""The 专项卡 (cards/project_card.py, app/project_cards.py): the card drawn from a project, its single writer (one whole-card
write a tick that changed, a new sequence for each, a failed write backed off), the 14 days, the line it shrinks to, the
buttons (in the table, with a handler, refused on another topic), the board's pin of a project and none of it with
COVEY_PROJECTS unset."""
from __future__ import annotations

import dataclasses
import html
import itertools
import json
import os
import re
import unittest
from types import SimpleNamespace
from unittest import mock

from support.fake_lark import FakeCardKit
from support.world import UnifiedCase
from test_projects import Case as ProjectsCase

from covey.app import pins, project_cards, project_clicks, project_facts
from covey.app.card_actions import ACTIONS, RETIRED_TEXT
from covey.cards import project_card as view
from covey.cards.elements import CARD_BYTES, CARD_NODES, element_count, find_element, json_size
from covey.core import board_read, look, projects
from covey.core.jobs import upsert_job_record
from covey.core.state import write_json
from covey.core.topics import label_title, routed_topic, topic_file

CHAT = "oc_owner_dm"
PID = "a1b2c3d4e5f6"
LEAD = projects.lead_topic_id(CHAT, PID)
ELEMENT_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,19}$")
URL = "https://applink.example/client/thread/open?open_chat_id=oc_x"
T1, T2, T3 = "a1b2c3" + "0" * 10, "b2c3d4" + "0" * 10, "c3d4e5" + "0" * 10


def ids(value):
    found = []
    if isinstance(value, dict):
        if "element_id" in value:
            found.append(value["element_id"])
        for item in value.values():
            found += ids(item)
    elif isinstance(value, list):
        for item in value:
            found += ids(item)
    return found


def thread(topic=T1, status=view.RUNNING, **fields):
    base = dict(topic_id=topic, status=status, label="T501", title="拆分 SQL 迁移脚本", body="第 2/5 步", took="1分00秒", url=URL)
    base.update(fields)
    return view.Member(**base)


def project(status=view.RUNNING, **fields):
    base = dict(project_id=PID, lead_topic=LEAD, name="把订单库迁到新集群", status=status, members=(thread(),), elapsed="3分00秒",
                tokens="33万", lead_state="在做 · 核对", inputs=("Claude Code", "Opus"), outputs=("**开始** 09:12",))
    base.update(fields)
    return view.Project(**base)


def text(card):
    return json.dumps(card, ensure_ascii=False)


def state_tag(card):
    """(text, colour) of the project's state on the lead's speaker row (cards/ui.py speaker_row: the card has no title bar,
    T657 B2); None without one."""
    found = re.search(r"<text_tag color='(\w+)'>(.*?)</text_tag>", text(find_element(card, "speaker_row")))
    return (html.unescape(found.group(2)), found.group(1)) if found else None


def rows(card):
    """The element ids of the member rows (cards/ui.py topic_row), in order."""
    return [element["element_id"] for element in card["body"]["elements"]
            if element.get("tag") == "interactive_container" and str(element.get("element_id") or "").startswith("pt_")]


class CardTest(UnifiedCase):
    def test_the_lead_s_speaker_row_names_the_project_and_its_state(self):
        """B2 (T657): the lead's card has the lead's speaker row first (docs/ui.md 4 说话人), no title bar: its picture, the
        project's name · 负责人, the state as a tag in its colour."""
        card = view.project_card(project())
        self.assertNotIn("header", card)
        self.assertEqual(card["body"]["elements"][0]["element_id"], "speaker_row")
        self.assertIn("**把订单库迁到新集群 · 负责人**", text(card["body"]["elements"][0]))
        self.assertEqual(state_tag(card), ("在做", "blue"))
        self.assertEqual(state_tag(view.project_card(project(status=view.WAITING))), ("等你", "orange"))
        for status in (view.RUNNING, view.WAITING, view.STOPPED, view.CANCELLED, view.DONE):
            self.assertEqual(state_tag(view.project_card(project(status)))[0], look.word(status))
        pictured = view.project_card(project(avatar="img_v3_fake"))["body"]["elements"][0]
        self.assertIn('"img_key": "img_v3_fake"', text(pictured))  # The lead's picture, the one its answers have.
        self.assertNotIn("img_key", text(card["body"]["elements"][0]))  # None uploaded: the name alone.
        asked = view.project_card(project(ask_text="迁库", ask_at="09:10", ask_url=URL))
        row = find_element(asked, "speaker_row")
        self.assertEqual(row["behaviors"][0]["default_url"], URL)  # The row opens the owner's message.
        self.assertIn("在办你 09:10 交代的事", text(row))
        self.assertIn("answer_words", ids(asked))  # His words, one line that opens.

    def test_a_guards_pause_is_停住了_in_red_with_why_and_the_owners_stop_is_已停止(self):
        """P0-3 (T647, docs/ui.md 2.2): 护栏停住的专项是“停住了”的红, why after it in its tag; the owner's own stop is 已停止,
        grey, as every other card says them (core/look.py STATES)."""
        card = view.project_card(project(view.STOPPED, reason="token 用完了"))
        self.assertEqual(state_tag(card), ("停住了 · token 用完了", "red"))
        self.assertEqual(state_tag(view.project_card(project(view.CANCELLED))), ("已停止", "neutral"))
        self.assertEqual(view.project_card(project(view.STOPPED, reason="token 用完了"))["config"]["summary"]["content"],
                         "停住了 · token 用完了 · 把订单库迁到新集群")

    def test_a_member_that_stopped_says_停住了_open_and_first_after_what_waits(self):
        """P0-1 (T647): a member that failed, was interrupted or stopped by the lead is 停住了 (red, open, counted as such),
        not 做完; one the owner stopped is 已停止, shut like one done."""
        members = (thread(T1, view.DONE, label="T501"), thread(T2, view.STOPPED, label="T502", body="这一轮没有成功"),
                   thread(T3, view.CANCELLED, label="T503"), thread("d4e5f6" + "0" * 10, view.WAITING, label="T504"),
                   thread("e5f6a7" + "0" * 10, view.STUCK, label="T505"))
        card = view.project_card(project(members=members))
        # What asks first, a row each as the 看板 draws a topic (T657 B6); the ones done or stopped by the owner one shut fold.
        self.assertEqual(rows(card), ["pt_d4e5f6_w", "pt_b2c3d4_s", "pt_e5f6a7_k"])
        stopped = text(find_element(card, "pt_b2c3d4_s"))
        self.assertIn("<font color='red'>**停住了**</font>", stopped)
        self.assertIn("这一轮没有成功", stopped)
        self.assertIn("<font color='white'>**T502**</font>", stopped)  # Its T number's badge.
        ended = find_element(card, "pj_ended")
        self.assertFalse(ended["expanded"])
        self.assertEqual(ended["header"]["title"]["content"], "做完 1 个 · 已停止 1 个")
        self.assertIn("· 已停止", text(ended))
        self.assertIn("话题 5（等你 1 · 停住了 1 · 可能卡住 1 · 做完 1 · 已停止 1）", find_element(card, "pj_meta")["content"])

    def test_the_elements_in_order_and_every_id_is_valid_and_there_is_no_input(self):
        card = view.project_card(project(need="T503 按日期还是按租户分区？", need_url=URL,
                                         lead_note="先迁小表", triage=("答了 一条",), memory=(("m1", "窗口在周六"),),
                                         library=(("方案.md",),), ask_text="迁库", ask_at="09:10", ask_url=URL))
        found = ids(card["body"]["elements"])
        self.assertEqual(found, ["speaker_row", "answer_words", "pj_need", "pj_meta", "pj_lead", "pt_a1b2c3_r", "pj_triage",
                                 "pj_mem", "pj_lib", "pj_io", "pj_btns", "pj_steer", "pj_switch", "pj_stop", "pj_down"])
        self.assertEqual(len(found), len(set(found)))
        self.assertTrue(all(ELEMENT_ID.match(name) for name in found), found)
        self.assertNotIn('"tag": "input"', text(card))
        self.assertNotIn('"tag": "form"', text(card))
        need = find_element(card, "pj_need")  # The band: what waits, opening the question card.
        self.assertEqual(need["border_color"], "band-line")
        self.assertEqual(need["behaviors"][0]["default_url"], URL)
        self.assertIn("要你定：T503 按日期还是按租户分区？", text(need))
        self.assertIn("话题 1（在做 1） · 用时 3分00秒 · token 33万", find_element(card, "pj_meta")["content"])
        self.assertIn("先迁小表", find_element(card, "pj_lead")["content"])

    def test_what_is_empty_is_not_drawn(self):
        card = view.project_card(project(tokens="", lead_state="", members=()))
        found = ids(card)
        for name in ("pj_need", "answer_ask", "answer_words", "pj_triage", "pj_mem", "pj_lib", "pj_more", "pj_ended"):
            self.assertNotIn(name, found)
        self.assertEqual(find_element(card, "pj_meta")["content"], "<font color='grey'>话题 0 · 用时 3分00秒</font>")
        self.assertIn("待命", find_element(card, "pj_lead")["content"])

    def test_threads_at_work_or_waiting_are_rows_those_done_one_shut_fold_and_a_change_of_state_is_a_new_element(self):
        threads = (thread(T1, view.RUNNING), thread(T2, view.DONE, label="T502"), thread(T3, view.WAITING, label="T503"))
        card = view.project_card(project(members=threads))
        self.assertEqual(rows(card), ["pt_c3d4e5_w", "pt_a1b2c3_r"])  # Waiting first.
        row = find_element(card, "pt_a1b2c3_r")
        self.assertEqual(row["behaviors"][0]["default_url"], URL)  # The whole row opens its thread.
        self.assertIn("第 2/5 步", text(row))
        fold = find_element(card, "pj_ended")
        self.assertFalse(fold["expanded"])
        self.assertIn(f"[T502]({URL})", text(fold))  # Done last, its T number a link.
        waiting = view.project_card(project(members=(thread(T1, view.WAITING),)))
        self.assertEqual(rows(waiting), ["pt_a1b2c3_w"])  # The same thread, another state: another id.
        ended = view.project_card(project(members=(thread(T1, view.DONE),)))
        self.assertEqual(rows(ended), [])
        self.assertIn("pj_ended", ids(ended))

    def test_at_most_eight_threads_are_rows_and_the_rest_is_one_line(self):
        many = tuple(thread(f"{index:02x}" * 3 + "0" * 10, view.RUNNING, label=f"T{600 + index}") for index in range(11))
        card = view.project_card(project(members=many))
        self.assertEqual(len(rows(card)), view.MAX_FOLDS)
        self.assertIn("还有 T608、T609、T610 3 个话题", find_element(card, "pj_more")["content"])
        self.assertLess(len(json.dumps(card, ensure_ascii=False, separators=(",", ":")).encode()), 16000)
        ended = tuple(thread(f"{index:02x}" * 3 + "0" * 10, view.DONE if index % 3 else view.RUNNING) for index in range(11))
        card = view.project_card(project(members=ended))
        self.assertEqual(len(rows(card)), 4)
        self.assertEqual(find_element(card, "pj_ended")["header"]["title"]["content"], "做完 7 个")

    def test_the_library_rows_carry_their_delete_button_the_triage_none_and_the_memory_is_one_line_of_counts(self):
        card = view.project_card(project(triage=("答了 一条", "转给你 一条"), memory=(("m1", "窗口在周六"), ("m2", "只读到周日")),
                                         memory_counts=(3, 1, 2, 4), library=(("方案.md",), ("表清单.csv",))))
        for name, count in (("pj_triage", 2), ("pj_lib", 2)):
            fold = find_element(card, name)
            self.assertFalse(fold["expanded"])
            self.assertIn(f"· {count}", fold["header"]["title"]["content"])
        line = find_element(card, "pj_mem")  # No fold, no rows, no 删除: the counts, 待审核 when some wait.
        self.assertEqual(line["content"], "<font color='grey'>专项记忆 · 规矩 3 · 决定 1 · 资料 2 · 待审核 4</font>")
        self.assertNotIn("bridge_project_mem_del", text(card))
        dels = [button for button in iter_buttons(card) if button.get("text", {}).get("content") == "删除"]
        values = [button["behaviors"][0]["value"] for button in dels]
        self.assertEqual([(value["kind"], value.get("name")) for value in values],
                         [("bridge_project_lib_del", "方案.md"), ("bridge_project_lib_del", "表清单.csv")])
        self.assertTrue(all(value["project_id"] == PID and value["topic_id"] == LEAD for value in values))

    def test_the_memory_line_links_to_the_memory_center_only_with_the_web_page_and_is_left_out_empty(self):
        linked = view.project_card(project(memory_counts=(1, 0, 0, 0), memory_url="https://covey.example.test/memory"))
        self.assertEqual(find_element(linked, "pj_mem")["content"],
                         "<font color='grey'>专项记忆 · 规矩 1 · 决定 0 · 资料 0</font> · [去网页记忆中心](https://covey.example.test/memory)")
        self.assertNotIn("去网页记忆中心", text(view.project_card(project(memory_counts=(1, 0, 0, 0)))))
        self.assertIsNone(find_element(view.project_card(project(memory_counts=(0, 0, 0, 0))), "pj_mem"))

    def test_buttons_follow_the_state(self):
        def names(status):
            card = view.project_card(project(status))
            return [name for name in ("pj_steer", "pj_switch", "pj_stop", "pj_down") if find_element(card, name)]
        self.assertEqual(names(view.RUNNING), ["pj_steer", "pj_switch", "pj_stop", "pj_down"])
        self.assertEqual(names(view.WAITING), ["pj_steer", "pj_switch", "pj_stop", "pj_down"])
        # A paused project's lead can be switched too: a lead stopped on a limit goes on, on another model, once 继续 is clicked.
        self.assertEqual(names(view.STOPPED), ["pj_steer", "pj_switch", "pj_down"])
        self.assertEqual(names(view.CANCELLED), ["pj_steer", "pj_switch", "pj_down"])
        self.assertEqual(names(view.DONE), [])
        card = view.project_card(project())
        self.assertEqual(find_element(card, "pj_stop")["value"], {"kind": "bridge_project_stop", "project_id": PID, "topic_id": LEAD})
        # 换配置 is the 换配置 card's own kind (app/harness_switch.py, matched before the card table), the project named.
        self.assertEqual(find_element(card, "pj_switch")["value"],
                         {"kind": "bridge_switch_open", "topic_id": LEAD, "project_id": PID})
        self.assertEqual(find_element(card, "pj_switch")["text"]["content"], "换配置")

    def test_the_lines_a_card_shrinks_to(self):
        done = view.settled_card("迁库", url=URL)
        self.assertIn("✅ 做完 · 结论在下方　›", text(done))
        self.assertEqual(find_element(done, "pj_line")["behaviors"], [{"type": "open_url", "default_url": URL}])
        self.assertEqual(done["config"]["width_mode"], "compact")
        self.assertNotIn("behaviors", find_element(view.settled_card("迁库"), "pj_line"))
        stopped = view.settled_card("迁库", reason="专项已降为普通话题", url=URL)
        self.assertIn("⏹ 专项已降为普通话题", text(stopped))
        self.assertNotIn("behaviors", find_element(stopped, "pj_line"))
        moved = view.moved_card("迁库", URL)
        self.assertIn("已移到新卡　›", text(moved))
        self.assertEqual(find_element(moved, "pj_line")["behaviors"][0]["default_url"], URL)
        for built in (done, stopped, moved):
            self.assertEqual(ids(built), ["pj_line"])


def iter_buttons(value):
    if isinstance(value, dict):
        if value.get("tag") == "button":
            yield value
        for item in value.values():
            yield from iter_buttons(item)
    elif isinstance(value, list):
        for item in value:
            yield from iter_buttons(item)


class CardCase(UnifiedCase):
    """A Bridge with COVEY_PROJECTS on, a project record and its writer with sends and writes mocked."""

    def setUp(self):
        super().setUp()
        FakeCardKit().start(self)
        patcher = mock.patch.dict(os.environ, {projects.ENV: "1"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.state = self.ctx.state_dir
        self.numbers = itertools.count(1)
        self.sent = mock.Mock(side_effect=self.send_card)
        self.app.messenger.create_and_send_card = self.sent
        self.put = mock.Mock(return_value={"code": 0})
        self.app.messenger.put_card = self.put
        for name, target in (("covey.app.topic_links.topic_thread_link", (URL, "thread")),
                             ("covey.app.topic_links.place_of", {"position": 9})):
            patch = mock.patch(name, return_value=target)
            patch.start()
            self.addCleanup(patch.stop)
        record = projects.new_record(chat_id=CHAT, name="把订单库迁到新集群", project_id=PID, now=1000.0)
        projects.create(self.state, record)
        projects.add_member(self.state, PID, T1)
        projects.update_card(self.state, PID, card_id="card-0", message_id="om_card0", sequence=40, created_at=1000.0)
        self.writer = project_cards.ProjectCards(self.app.runtime, self.app.messenger, self.app.voice, {"claude": "Claude Code"})

    def send_card(self, profile, chat, card, key, **kwargs):
        number = next(self.numbers)
        return kwargs.get("card_id") or f"card-{number}", f"om_card{number}"

    def record(self):
        return projects.read_record(self.state, PID)

    def tick(self, now):
        self.writer.tick(self.ctx, now)

    def job(self, topic, number, status, **fields):
        upsert_job_record(self.state, f"j{number}", topic_id=topic, chat_id=CHAT, job_id=number, status=status, **fields)


class BudgetTest(unittest.TestCase):
    """With everything on at once (the topics' Harness names, the memory and library folds, the 分流记录) and every
    list full of long lines, the card still takes what a card takes (cards/elements.py CARD_NODES, CARD_BYTES)."""

    def card(self, rows, topics):
        text = "长" * 300
        members = tuple(view.Member(topic_id=f"{n:032x}", status=(view.WAITING, view.RUNNING, view.RUNNING, view.DONE)[n % 4],
                                    label=f"T{400 + n}", title=text,
                                    body=text, took="12 分钟", url="https://example.com/t/" + "u" * 60, harness="Claude Code")
                        for n in range(topics))
        return view.project_card(view.Project(
            project_id="a1b2c3d4e5f6", lead_topic="f" * 32, name=text, status=view.WAITING, need=text, need_url="https://example.com/q",
            members=members, elapsed="3 天 4 小时", tokens="1200 万", lead_state=text, lead_note=text,
            triage=tuple((text, "c" * 16) for _ in range(rows)), memory=tuple((f"{n:06x}", text) for n in range(rows)),
            library=tuple((text, f"{n:06x}") for n in range(rows)), inputs=("输入：" + text,), outputs=("输出：" + text,),
            ask_text=text, origin_note=text))

    def test_a_full_card_is_cut_down_to_the_budget_with_a_line_for_what_is_left_out(self):
        built = self.card(40, 12)
        self.assertLessEqual(element_count(built), CARD_NODES)
        self.assertLessEqual(json_size(built), CARD_BYTES)
        self.assertIsNotNone(find_element(built, "pj_more"))  # Topics left out are still said.
        shown = [find_element(built, name) for name in ("pj_triage", "pj_lib")]
        self.assertTrue(all(shown))
        self.assertIsNotNone(find_element(built, "pj_mem"))
        self.assertTrue(all(any("另有" in json.dumps(each, ensure_ascii=False) for each in fold["elements"]) for fold in shown))

    def test_a_card_that_fits_is_drawn_whole_as_before(self):
        built = self.card(3, 3)
        self.assertEqual(len(find_element(built, "pj_lib")["elements"]), 3)
        self.assertIsNone(find_element(built, "pj_more"))

    def test_the_longest_list_the_folds_take_still_leaves_every_button_of_the_card(self):
        self.assertIsNotNone(find_element(self.card(40, 12), "pj_btns"))


class HarnessLabelTest(CardCase):
    """The Harness a card names is the topic's own: the box (输入) is the lead's, each thread's fold says its own (2026-11-16: the
    lead ran on Claude Code, a thread on Codex, and the box was all the card said)."""
    TITLES = {"claude": "Claude Code", "codex": "Codex"}

    def bind(self, topic, harness, **fields):
        write_json(topic_file(self.state, topic), {"harness_id": harness, **fields})

    def facts(self):
        return project_facts.facts(self.state, self.record(), 1100.0, self.TITLES)

    def test_a_threads_fold_names_the_harness_it_runs_on_not_the_leads(self):
        self.job(LEAD, 1, "running", harness_id="claude", model="Opus", effort="high")
        self.job(T1, 2, "running", harness_id="codex")
        shown = self.facts()
        self.assertEqual(shown.members[0].harness, "Codex")
        self.assertEqual(shown.inputs[0], "Claude Code")  # The box is the lead's.
        self.assertIn("Codex", text(find_element(view.project_card(shown), "pt_a1b2c3_r")))  # On its row's figures.

    def test_a_thread_with_no_job_yet_is_named_by_its_binding(self):
        self.bind(T1, "codex")
        self.assertEqual(self.facts().members[0].harness, "Codex")
        self.bind(T1, "claude")
        self.assertEqual(self.facts().members[0].harness, "Claude Code")

    def test_the_job_that_runs_wins_over_a_binding_a_switch_has_changed(self):
        self.bind(T1, "claude")
        self.job(T1, 2, "running", harness_id="codex")
        self.assertEqual(self.facts().members[0].harness, "Codex")

    def test_an_older_job_record_names_its_harness_as_the_backend(self):
        self.job(T1, 2, "running", backend="codex")
        self.assertEqual(self.facts().members[0].harness, "Codex")

    def test_a_lead_without_a_job_is_its_own_binding_not_a_threads_harness(self):
        self.bind(LEAD, "claude", model="opus", effort="high")
        self.job(T1, 2, "running", harness_id="codex", model="gpt", effort="low")
        shown = self.facts()
        self.assertEqual(shown.inputs, ("Claude Code", "opus", "思考 high"))
        self.job(LEAD, 3, "running", harness_id="claude", model="Opus")
        self.assertEqual(self.facts().inputs[0], "Claude Code")

    def test_a_lead_with_neither_job_nor_binding_has_no_harness_in_the_box(self):
        self.job(T1, 2, "running", harness_id="codex")
        self.assertEqual(self.facts().inputs, ())

    def test_a_harness_without_a_title_is_left_out_not_guessed(self):
        self.job(T1, 2, "running", harness_id="neeko")
        self.assertEqual(self.facts().members[0].harness, "")


class RefreshTest(CardCase):
    def test_a_changed_card_is_written_whole_with_a_new_sequence_each_time_and_not_oftener_than_the_gap(self):
        self.tick(1010.0)
        self.assertEqual(self.put.call_count, 1)
        args, kwargs = self.put.call_args
        self.assertEqual((args[1], kwargs["sequence"]), ("card-0", 41))  # Past the 40 the record had.
        self.assertIn("pj_btns", ids(args[2]))
        self.assertEqual(self.record()["card"]["sequence"], 41)  # Saved before the request went out.
        self.tick(1011.0)
        self.assertEqual(self.put.call_count, 1)  # Nothing changed.
        self.job(T1, 1, "running")
        self.tick(1011.5)
        self.assertEqual(self.put.call_count, 1)  # Changed, but inside the gap.
        self.job(T1, 1, "done", status_message="完成了")
        self.tick(1020.0)
        self.assertEqual([call.kwargs["sequence"] for call in self.put.call_args_list], [41, 42])
        self.assertIn("pj_ended", ids(self.put.call_args.args[2]))  # Done: a line of the shut fold, no row.
        self.assertNotIn("pt_a1b2c3_r", ids(self.put.call_args.args[2]))
        self.assertEqual(len({call.kwargs["key"] for call in self.put.call_args_list}), 2)

    def test_the_lead_s_picture_is_had_once_and_is_on_its_speaker_row(self):
        """B2 (T657): the lead's picture (app/answer_cards.py avatar: uploaded the first time, then kept) on the card's speaker
        row, asked for once a project; one not to be had is asked again LINK_RETRY later, the card going with the name alone."""
        calls = []

        def avatar(state, topic, chat, profile=None, log=None):
            calls.append(topic)
            return "" if len(calls) == 1 else "img_v3_lead"
        with mock.patch.object(project_cards, "avatar_of", side_effect=avatar):
            self.tick(1010.0)
            self.assertNotIn("img_key", text(self.put.call_args.args[2]))
            self.tick(1020.0)
            self.assertEqual(len(calls), 1)  # Not again within LINK_RETRY.
            self.tick(1010.0 + project_cards.LINK_RETRY + 1)
            self.assertIn('"img_key": "img_v3_lead"', text(self.put.call_args.args[2]))
            self.tick(1010.0 + 2 * project_cards.LINK_RETRY + 2)
        self.assertEqual(calls, [LEAD, LEAD])

    def test_a_write_that_failed_backs_off_and_the_next_try_has_a_new_sequence(self):
        self.put.return_value = None
        self.tick(1010.0)
        self.tick(1011.0)  # Inside the 2 s back-off.
        self.assertEqual(self.put.call_count, 1)
        self.tick(1013.0)
        self.assertEqual(self.put.call_count, 2)
        self.put.return_value = {"code": 0}
        self.tick(1020.0)
        self.assertEqual([call.kwargs["sequence"] for call in self.put.call_args_list], [41, 42, 43])
        self.assertEqual(self.record()["card"]["sequence"], 43)

    def test_the_digest_is_kept_so_a_restart_does_not_write_the_same_card_again(self):
        self.tick(1010.0)
        self.writer = project_cards.ProjectCards(self.app.runtime, self.app.messenger, self.app.voice, {"claude": "Claude Code"})
        self.tick(1015.0)
        self.assertEqual(self.put.call_count, 1)

    def test_a_waiting_thread_makes_the_project_wait_for_the_owner_and_the_owners_stop_shows_as_已停止(self):
        self.job(T1, 1, "waiting_user", status_message="等你回答：按日期还是按租户分区？")
        self.tick(1010.0)
        card = self.put.call_args.args[2]
        self.assertEqual(state_tag(card), ("等你", "orange"))
        self.assertIn("按日期还是按租户分区", text(find_element(card, "pj_need")))
        projects.set_state(self.state, PID, projects.PAUSED, "主人停止")
        self.tick(1020.0)
        self.assertEqual(state_tag(self.put.call_args.args[2])[0], "已停止")
        self.assertIsNone(find_element(self.put.call_args.args[2], "pj_stop"))
        projects.set_state(self.state, PID, projects.ACTIVE)
        projects.set_state(self.state, PID, projects.PAUSED, "wakes")  # A guard's: 停住了 and why, in red.
        self.tick(1030.0)
        self.assertEqual(state_tag(self.put.call_args.args[2]), ("停住了 · 连续唤醒次数到上限", "red"))

    def test_a_thread_that_failed_or_was_stopped_by_the_lead_is_停住了_on_the_card(self):
        self.job(T1, 1, "failed", status_message="任务异常（transport_error）")
        self.tick(1010.0)
        card = self.put.call_args.args[2]
        self.assertIsNotNone(find_element(card, "pt_a1b2c3_s"))
        self.assertIn("<font color='red'>**停住了**</font>", text(find_element(card, "pt_a1b2c3_s")))
        self.assertIn("停住了 1", find_element(card, "pj_meta")["content"])
        self.job(T1, 2, "cancelled", cancel_requested_by="card")  # The owner's own stop.
        self.tick(1020.0)
        card = self.put.call_args.args[2]
        self.assertEqual(find_element(card, "pj_ended")["header"]["title"]["content"], "已停止 1 个")

    def test_the_card_says_what_the_record_and_the_files_say(self):
        projects.update(self.state, PID, lambda record: record.update(direction="旧的方向", lead_note={"text": "先迁小表"},
                                                                   tokens_used=330000, triage=["答了：用哪个版本？"]))
        folder = projects.folder(self.state) / PID
        (folder / "library").mkdir(parents=True)
        (folder / "library" / "方案.md").write_text("x")
        (folder / "memory.json").write_text(json.dumps({"entries": [{"id": "m1", "text": "窗口在周六"}]}))
        self.tick(1010.0)
        card = self.put.call_args.args[2]
        self.assertEqual(state_tag(card), ("在做", "blue"))
        self.assertIn("先迁小表", find_element(card, "pj_lead")["content"])
        self.assertIn("token 33万", find_element(card, "pj_meta")["content"])
        self.assertTrue(all(name in ids(card) for name in ("pj_triage", "pj_mem", "pj_lib")))

    def test_no_thread_without_the_switch_and_no_write(self):
        self.assertEqual([name for name, _ in self.writer.threads()], ["project_cards"])
        with mock.patch.dict(os.environ, {projects.ENV: "0"}):
            self.assertEqual(self.writer.threads(), ())
            self.tick(1010.0)
        self.assertFalse(self.put.called)


class SendTest(CardCase):
    def env(self):
        from covey.app.butler_env import ButlerEnv
        return ButlerEnv(runtime=self.app.runtime, topics=self.app.topics, messenger=self.app.messenger, voice=self.app.voice,
                       state_dir=self.state, profile="test-bot")

    def test_created_sends_a_message_of_its_own_and_routes_a_quote_of_it_to_the_lead(self):
        projects.update_card(self.state, PID, card_id=None, message_id=None)
        message = project_cards.created(self.env(), self.record())
        self.assertEqual(message, "om_card1")
        (profile, chat, card, key), kwargs = self.sent.call_args
        self.assertEqual(chat, CHAT)
        self.assertFalse(kwargs.get("reply_to_message_id"), kwargs)  # A reply would go into a thread.
        self.assertIn("pj_btns", ids(card))
        record = self.record()
        self.assertEqual((record["card"]["message_id"], record["card"]["sequence"]), ("om_card1", 0))
        self.assertEqual(routed_topic(self.state, CHAT, "om_card1"), LEAD)
        self.assertEqual(project_cards.created(self.env(), record), "om_card1")  # Once.
        self.assertEqual(self.sent.call_count, 1)

    def test_nothing_is_sent_without_the_switch(self):
        with mock.patch.dict(os.environ, {projects.ENV: "0"}):
            self.assertEqual(project_cards.created(self.env(), self.record()), "")
        self.assertFalse(self.sent.called)

    def test_a_dispatch_card_adopted_is_replaced_whole_and_the_sequence_goes_on(self):
        project_cards.adopt_card(self.state, PID, card_id="card-dispatch", message_id="om_dispatch", sequence=77,
                                 created_at=1500.0)
        self.tick(1600.0)
        args, kwargs = self.put.call_args
        self.assertEqual((args[1], kwargs["sequence"]), ("card-dispatch", 78))
        self.assertNotIn("dp_thread", ids(args[2]))
        self.assertIn("pj_meta", ids(args[2]))
        self.assertEqual(self.record()["card"]["message_id"], "om_dispatch")


class FlipTest(CardCase):
    DAYS = 86400

    def age(self, days):
        return 1000.0 + days * self.DAYS

    def test_on_the_twelfth_day_a_new_card_is_sent_the_record_follows_it_and_the_old_one_shrinks(self):
        self.tick(self.age(12) + 1)
        self.assertEqual(self.sent.call_count, 1)
        (profile, chat, card, key), kwargs = self.sent.call_args
        self.assertIn("pj_btns", ids(card))
        self.assertFalse(kwargs.get("reply_to_message_id"))
        record = self.record()
        self.assertEqual((record["card"]["card_id"], record["card"]["message_id"], record["card"]["generation"]),
                         ("card-1", "om_card1", 1))
        self.assertEqual(projects.card_message(record), "om_card1")  # The board's pin follows it (app/pins.py aimed).
        self.assertEqual(routed_topic(self.state, CHAT, "om_card1"), LEAD)  # A click and a quote go to the lead.
        self.assertEqual(record["card"]["old"]["card_id"], "card-0")
        self.tick(self.age(12) + 2)  # The next look shrinks the old one, on its own sequence.
        self.assertNotIn("old", self.record()["card"])
        args, kwargs = self.put.call_args
        self.assertEqual((args[1], kwargs["sequence"]), ("card-0", 41))
        self.assertIn("已移到新卡", text(args[2]))
        self.assertIn("position=9", text(args[2]))
        self.assertEqual(ids(args[2]), ["pj_line"])
        self.assertEqual(record["card"]["created_at"], self.age(12) + 1)

    def test_a_card_that_is_young_is_not_sent_again(self):
        self.tick(self.age(11))
        self.assertFalse(self.sent.called)

    def test_an_old_card_that_cannot_be_written_three_times_is_recalled(self):
        self.put.return_value = None
        now = self.age(12) + 1
        self.tick(now)
        self.tick(now + 1)
        self.assertEqual(self.record()["card"]["old"]["tries"], 1)
        for step in (10, 30):
            self.tick(now + step)
        self.assertNotIn("old", self.record()["card"])
        self.app.messenger.recall.assert_called_once_with(self.ctx.profile, "om_card0")

    def test_a_card_not_sent_is_tried_again_with_the_same_card(self):
        def lost(*args, **kwargs):
            kwargs["on_created"]("card-9")  # The card was created; the message did not go out.
            return "card-9", ""
        self.sent.side_effect = lost
        self.tick(self.age(12) + 1)
        self.assertEqual(self.record()["card"]["card_id"], "card-0")
        self.assertEqual(self.record()["card"]["pending_card_id"], "card-9")
        self.sent.side_effect = self.send_card
        self.tick(self.age(12) + 10)
        self.assertEqual(self.sent.call_args.kwargs["card_id"], "card-9")
        self.assertEqual(self.record()["card"]["message_id"], "om_card1")


class SettleTest(CardCase):
    def test_a_project_that_is_done_shrinks_to_one_line_opening_the_conclusion(self):
        projects.update(self.state, PID, lambda record: record.update(conclusion_message_id="om_conclusion"))
        projects.set_state(self.state, PID, projects.DONE)
        self.tick(1010.0)
        args, kwargs = self.put.call_args
        self.assertEqual(ids(args[2]), ["pj_line"])
        self.assertIn("position=9", text(args[2]))
        card = self.record()["card"]
        self.assertTrue(card["closed"])
        self.tick(1020.0)
        self.assertEqual(self.put.call_count, 1)  # Never refreshed again.

    def test_a_finished_project_the_owner_speaks_in_again_gets_its_whole_card_back_in_place(self):
        from covey.app import project_loop
        projects.update(self.state, PID, lambda record: record.update(conclusion_message_id="om_conclusion"))
        projects.set_state(self.state, PID, projects.DONE)
        self.tick(1010.0)
        settled = self.record()["card"]
        self.assertTrue(settled["closed"])
        projects.update(self.state, PID, project_loop.owner_spoke)  # What his turn in the lead's topic does (owner_turn).
        self.assertEqual(self.record()["state"], projects.ACTIVE)
        self.tick(1100.0)
        self.assertEqual(self.put.call_count, 2)
        self.assertNotEqual(ids(self.put.call_args.args[2]), ["pj_line"])  # The whole card, on the same card.
        card = self.record()["card"]
        self.assertEqual((card["card_id"], card.get("closed")), ("card-0", False))
        self.assertGreater(card["sequence"], settled["sequence"])
        self.assertFalse(self.sent.called)  # No new message.

    def test_without_a_conclusion_it_waits_then_shrinks_without_the_link(self):
        projects.set_state(self.state, PID, projects.DONE)
        self.tick(1010.0)
        self.assertFalse(self.put.called)
        self.tick(1010.0 + project_cards.CONCLUSION_GRACE + 1)
        self.assertEqual(ids(self.put.call_args.args[2]), ["pj_line"])
        self.assertNotIn("default_url", text(self.put.call_args.args[2]))

    def finished_by(self, status, **answer):
        # The lead's turn that called project_finish (app/project_tools.py writes state_job with the state).
        upsert_job_record(self.state, "k-fin", topic_id=LEAD, chat_id=CHAT, job_id=9, status=status,
                          message_id=f"lead-{PID}-2", **answer)
        projects.update(self.state, PID, lambda record: record.update(state_job="k-fin"))
        projects.set_state(self.state, PID, projects.DONE)

    def test_done_by_a_turn_that_sent_its_answer_it_shrinks_at_once_opening_that_answer(self):
        # accept7 (2026-11-16): nothing wrote conclusion_message_id, so each done card waited CONCLUSION_GRACE.
        self.finished_by("done", answer_message_id="om_answer")
        self.tick(1010.0)
        self.assertEqual(ids(self.put.call_args.args[2]), ["pj_line"])
        self.assertIn("position=9", text(self.put.call_args.args[2]))

    def test_done_by_a_turn_that_ended_without_an_answer_it_shrinks_soon_without_the_link(self):
        self.finished_by("done")  # A quiet wake-up turn: its reply was not sent.
        self.tick(1010.0)
        self.assertFalse(self.put.called)  # Its answer id may still be on its way.
        self.tick(1010.0 + project_cards.ANSWER_WAIT + 1)
        self.assertEqual(ids(self.put.call_args.args[2]), ["pj_line"])
        self.assertNotIn("default_url", text(self.put.call_args.args[2]))

    def test_an_answer_id_written_a_moment_after_the_turn_ended_still_gives_the_link(self):
        # accept8 (2026-11-16): the line came out 1-3 s after the turn ended, before its answer_message_id was written.
        self.finished_by("done")
        self.tick(1010.0)
        upsert_job_record(self.state, "k-fin", answer_message_id="om_answer")
        self.tick(1015.0)
        self.assertEqual(ids(self.put.call_args.args[2]), ["pj_line"])
        self.assertIn("position=9", text(self.put.call_args.args[2]))

    def test_done_by_a_turn_still_running_it_waits_for_that_turn(self):
        self.finished_by("running")
        self.tick(1010.0)
        self.assertFalse(self.put.called)
        self.tick(1010.0 + project_cards.CONCLUSION_GRACE + 1)
        self.assertEqual(ids(self.put.call_args.args[2]), ["pj_line"])

    def test_a_project_that_was_cancelled_says_so(self):
        projects.set_state(self.state, PID, projects.CANCELLED, "降级")
        self.tick(1010.0)
        self.assertIn("⏹ 专项已降为普通话题", text(self.put.call_args.args[2]))
        self.assertTrue(self.record()["card"]["closed"])


class ButtonTest(CardCase):
    def setUp(self):
        super().setUp()
        self.claude = self.app.apps["claude"]
        self.lead_ctx = dataclasses.replace(self.ctx, topic_id=LEAD)
        self.claude.messenger.reply = mock.Mock(return_value=True)

    def event(self, kind, **value):
        return {"operator_id": "ou_owner", "chat_id": CHAT, "message_id": "om_card0", "event_id": "ev1",
                "action_value": {"kind": kind, "project_id": PID, "topic_id": LEAD, **value}}

    def said(self):
        return self.claude.messenger.reply.call_args.args[2]

    def test_the_kinds_are_in_the_table_with_a_handler_and_start_no_task(self):
        for kind, handler in (("bridge_project_steer", "steering.project_steer"), ("bridge_project_stop", "controls.project_stop"),
                              ("bridge_project_downgrade", "controls.project_downgrade"),
                              ("bridge_project_mem_del", "controls.project_mem_del"),
                              ("bridge_project_lib_del", "controls.project_lib_del")):
            self.assertEqual((ACTIONS[kind].handler, ACTIONS[kind].bot_only), (handler, False), kind)
        # The 分流记录's 不对 and its form's submit were taken off the cards (T898): kept in the table, answered in words.
        retired = ("bridge_project_triage_wrong", "bridge_project_triage_fix")
        for kind in retired:
            self.assertEqual((ACTIONS[kind].handler, ACTIONS[kind].bot_only), ("controls.retired_action", True), kind)
            self.assertIn(kind, RETIRED_TEXT)
        self.assertEqual(view.KINDS | set(retired), {kind for kind in ACTIONS if kind.startswith("bridge_project_")})

    def test_a_click_for_another_topic_or_a_project_not_there_is_refused(self):
        handlers = (self.claude.controls.project_stop, self.claude.controls.project_downgrade, self.claude.steering.project_steer,
                    self.claude.controls.project_lib_del)
        for handler in handlers:
            with self.assertRaises(ValueError):
                handler(self.lead_ctx, self.event("x", topic_id="other"))
            with self.assertRaises(ValueError):
                handler(self.lead_ctx, self.event("x", project_id="0f9e8d7c6b5a"))
            wrong_chat = {**self.event("x"), "chat_id": "oc_other"}
            with self.assertRaises(ValueError):
                handler(self.lead_ctx, wrong_chat)

    def test_stop_pauses_the_project_and_stops_the_tasks_of_the_lead_and_its_threads(self):
        from covey.core.jobs import BridgeReplyJob

        jobs = []
        for number, topic in ((5, LEAD), (6, T1)):
            job = BridgeReplyJob(job_id=number, user_text="x", chat_id=CHAT, message_id=f"om_job{number}", timeout=0, topic_id=topic)
            job.execution_started = True
            self.app.runtime.jobs.register(job)
            jobs.append(job)
        with mock.patch.object(self.claude.controls, "cancel_reply_job_from_card", return_value=True) as cancel:
            self.assertTrue(self.claude.controls.project_stop(self.lead_ctx, self.event("bridge_project_stop")))
        self.assertEqual(self.record()["state"], "paused")
        self.assertEqual(sorted((call.args[0].topic_id, call.args[1]["job_id"]) for call in cancel.call_args_list),
                         [(LEAD, "5"), (T1, "6")])
        self.assertIn("已请 2 个任务停下", self.said())
        self.assertTrue(self.claude.controls.project_stop(self.lead_ctx, self.event("bridge_project_stop")))
        self.assertIn("已经是停住的", self.said())  # A second click changes nothing.
        self.assertEqual(cancel.call_count, 2)

    def test_stop_reaches_a_task_in_its_task_runner_as_a_click_on_its_card(self):
        # accept8 (2026-11-16): the stop was set only in the Bridge's copy of the runners' jobs, and their commands ran on.
        from covey.app import runner_ipc
        from covey.core.jobs import BridgeReplyJob, JOB_STOP_ACTION_KIND
        from covey.core.topics import message_topic

        for number, topic in ((5, LEAD), (6, T1)):
            job = BridgeReplyJob(job_id=number, user_text="x", chat_id=CHAT, message_id=f"om_job{number}", timeout=0, topic_id=topic)
            job.execution_started = True
            self.app.runtime.jobs.register(job)
        in_runner = lambda runtime, target, click, action: action["job_id"] == "6"  # noqa: E731 - T1's job is a runner's
        with mock.patch.object(runner_ipc, "forward_card_action", side_effect=in_runner) as forward, \
                mock.patch.object(self.claude.controls, "cancel_reply_job_from_card", return_value=True) as cancel:
            self.assertTrue(self.claude.controls.project_stop(self.lead_ctx, self.event("bridge_project_stop")))
        sent = {call.args[3]["job_id"]: call.args for call in forward.call_args_list}
        runtime, target, click, action = sent["6"]
        self.assertEqual((target.topic_id, action["kind"], click["action_value"]), (T1, JOB_STOP_ACTION_KIND, action))
        # The runner routes the click by its message: an alias of T1's own, whatever card the job has.
        self.assertEqual(message_topic(self.state, CHAT, click["message_id"]), T1)
        self.assertEqual([call.args[1]["job_id"] for call in cancel.call_args_list], ["5"])  # The lead's, in this Bridge.
        self.assertIn("已请 2 个任务停下", self.said())

    def test_supplement_opens_the_leads_running_turns_form_and_says_so_when_it_has_none(self):
        self.assertTrue(self.claude.steering.project_steer(self.lead_ctx, self.event("bridge_project_steer")))
        self.assertEqual(self.said(), project_clicks.NO_LEAD_JOB)
        from covey.core.jobs import BridgeReplyJob

        job = BridgeReplyJob(job_id=8, user_text="x", chat_id=CHAT, message_id="om_job8", timeout=0, topic_id=LEAD)
        job.execution_started, job.backend_control = True, SimpleNamespace(remote=False, supports_steer=True)
        self.app.runtime.jobs.register(job)
        with mock.patch.object(self.claude.steering, "steer_action") as steer:
            self.assertTrue(self.claude.steering.project_steer(self.lead_ctx, self.event("bridge_project_steer")))
        args = steer.call_args.args
        self.assertEqual((args[1], args[3], args[5]), (CHAT, {"kind": "bridge_steer", "topic_id": LEAD, "job_id": 8}, "ev1"))

    def test_downgrade_cancels_the_project_and_says_so(self):
        # The whole of it (threads, aliases, the lead's tasks) is tests/test_project_upgrade.py's.
        self.assertTrue(self.claude.controls.project_downgrade(self.lead_ctx, self.event("bridge_project_downgrade")))
        self.assertIn("降回普通话题", self.said())
        self.assertEqual((self.record()["state"], self.record()["state_reason"]), ("cancelled", "downgraded"))

    def test_the_memory_delete_of_a_card_drawn_before_is_offline_and_deletes_nothing(self):
        folder = projects.folder(self.state) / PID
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "memory.json").write_text(json.dumps({"entries": [{"id": "m1", "text": "窗口在周六"}]}))
        before = (folder / "memory.json").read_text()
        self.assertTrue(self.claude.controls.project_mem_del(self.lead_ctx, self.event("bridge_project_mem_del", entry="m1")))
        self.assertEqual(self.said(), "删除已下线：到网页记忆中心收起或作废")
        self.assertEqual((folder / "memory.json").read_text(), before)

    def test_a_library_file_is_deleted_by_its_own_row_alone(self):
        folder = projects.folder(self.state) / PID
        (folder / "library").mkdir(parents=True)
        for name in ("方案.md", "表清单.csv"):
            (folder / "library" / name).write_text("x")
        (folder / "outside.txt").write_text("keep")
        self.assertTrue(self.claude.controls.project_lib_del(self.lead_ctx, self.event("bridge_project_lib_del", name="方案.md")))
        self.assertEqual(sorted(path.name for path in (folder / "library").iterdir()), ["表清单.csv"])
        for name in ("../outside.txt", "/etc/passwd", ".hidden", "不在.md"):
            self.assertTrue(self.claude.controls.project_lib_del(self.lead_ctx, self.event("bridge_project_lib_del", name=name)))
            self.assertIn("已经不在了", self.said())
        self.assertTrue((folder / "outside.txt").exists())


class AskCardTest(CardCase):
    """COVEY_PROJECT_ASK=1: the card a guard's pause sends the owner, what a click on it does, and the line it becomes."""

    def setUp(self):
        super().setUp()
        self.claude = self.app.apps["claude"]
        self.lead_ctx = dataclasses.replace(self.ctx, topic_id=LEAD)
        self.claude.messenger.reply = mock.Mock(return_value=True)
        patcher = mock.patch.dict(os.environ, {projects.ASK_ENV: "1"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def pause(self, reason="tokens", why="token 用到上限 1000 万", **fields):
        def change(record):
            record.update(state=projects.PAUSED, state_reason=reason, **fields)
            record["pause_ask"] = {"reason": reason, "why": why, "at": 1005.0, "choice": "", "halted": 0, "card": {}}
        projects.update(self.state, PID, change)

    def event(self, choice, **fields):
        return {"operator_id": "ou_owner", "chat_id": CHAT, "message_id": "om_card1", "event_id": "ev1",
                "action_value": {"kind": view.ASK_KINDS[choice], "project_id": PID, "topic_id": LEAD}, **fields}

    def said(self):
        return self.claude.messenger.reply.call_args.args[2]

    def test_the_card_says_why_in_a_sentence_and_has_the_three_buttons(self):
        built = view.ask_card("把订单库迁到新集群", "token 用到上限 1000 万", project_id=PID, lead_topic=LEAD, note="在做的 2 个话题已请它们停下。")
        self.assertTrue(all(ELEMENT_ID.match(name) for name in ids(built)), ids(built))
        # 停住了 in red (a guard's stop, docs/ui.md 3): orange says 等你 and 可能卡住 alone (T604 #50).
        tag = built["header"]["text_tag_list"][0]
        self.assertEqual((tag["text"]["content"], tag["color"], built["header"]["icon"]["color"]), ("停住了", "red", "red"))
        self.assertEqual(find_element(built, "pa_why")["content"], "**token 用到上限 1000 万**")
        self.assertIn("2 个话题", find_element(built, "pa_note")["content"])
        buttons = [(name, find_element(built, name)) for name in ("pa_go", "pa_wrap", "pa_stop")]
        self.assertEqual([(button["text"]["content"], button["value"]["kind"]) for _, button in buttons],
                         [("继续", "bridge_project_ask_continue"), ("收尾", "bridge_project_ask_finish"), ("停止", "bridge_project_ask_stop")])
        self.assertTrue(all(button["value"]["project_id"] == PID and button["value"]["topic_id"] == LEAD for _, button in buttons))
        self.assertIsNone(find_element(view.ask_card("x", "y", project_id=PID, lead_topic=LEAD), "pa_note"))

    def test_the_kinds_are_in_the_table_the_two_that_wake_the_lead_bot_only(self):
        for choice, bot_only in (("continue", True), ("finish", True), ("stop", False)):
            action = ACTIONS[view.ASK_KINDS[choice]]
            self.assertEqual((action.handler, action.bot_only), ("controls.project_ask", bot_only), choice)
        self.assertTrue(set(view.ASK_KINDS.values()) <= view.KINDS)

    def test_a_pause_sends_one_card_where_the_project_card_is_and_routes_a_quote_of_it_to_the_lead(self):
        self.pause()
        self.tick(1010.0)
        (profile, chat, card, key), kwargs = self.sent.call_args
        self.assertEqual(chat, CHAT)
        self.assertFalse(kwargs.get("reply_to_message_id"), kwargs)
        self.assertIn("pa_btns", ids(card))
        ask = self.record()["pause_ask"]["card"]
        self.assertEqual((ask["message_id"], ask["card_id"], ask["sequence"]), ("om_card1", "card-1", 0))
        self.assertEqual(routed_topic(self.state, CHAT, "om_card1"), LEAD)
        self.tick(1020.0)
        self.tick(1030.0)
        self.assertEqual(self.sent.call_count, 1)  # Once.

    def test_nothing_is_sent_without_a_question_or_for_a_project_that_moved_on_before_it_went_out(self):
        projects.set_state(self.state, PID, projects.PAUSED, "tokens")  # A guard's pause with the switch off: no pause_ask.
        self.tick(1010.0)
        self.assertFalse(self.sent.called)
        self.pause()
        projects.update(self.state, PID, lambda record: record.update(state=projects.ACTIVE, state_reason=""))
        self.tick(1020.0)
        self.assertFalse(self.sent.called)
        self.assertTrue(self.record()["pause_ask"]["card"]["closed"])

    def test_a_card_that_could_not_be_sent_is_tried_again_after_a_back_off_and_keeps_its_card_id(self):
        self.pause()
        self.sent.side_effect = lambda *args, **kwargs: (kwargs["on_created"]("card-x"), (kwargs["card_id"] or "card-x", ""))[1]
        self.tick(1010.0)
        self.tick(1011.0)  # Inside the 2 s back-off.
        self.assertEqual(self.sent.call_count, 1)
        self.sent.side_effect = self.send_card
        self.tick(1013.0)
        self.assertEqual(self.sent.call_args.kwargs["card_id"], "card-x")
        self.assertEqual(self.record()["pause_ask"]["card"]["message_id"], "om_card1")

    def test_the_tokens_card_says_what_became_of_the_topics_and_the_wakes_card_that_they_go_on(self):
        self.assertEqual(project_cards.ask_note({"reason": "tokens", "halted": 2}), "在做的 2 个话题已请它们停下。")
        self.assertEqual(project_cards.ask_note({"reason": "tokens", "halted": 0}), "")
        self.assertIn("还在继续做", project_cards.ask_note({"reason": "wakes"}))
        self.assertEqual(project_cards.ask_note({"reason": "stalled"}), "")

    def test_after_the_choice_the_card_is_written_as_one_line_saying_what_was_chosen_and_once(self):
        self.pause()
        self.tick(1010.0)
        self.assertTrue(self.claude.controls.project_ask(self.lead_ctx, self.event("continue")))
        self.put.reset_mock()
        self.tick(1020.0)
        asked = [call for call in self.put.call_args_list if call.args[1] == "card-1"]
        self.assertEqual(len(asked), 1)
        self.assertEqual((asked[0].kwargs["sequence"], find_element(asked[0].args[2], "pj_line") is not None), (1, True))
        self.assertIn("你选了继续", text(asked[0].args[2]))
        self.assertNotIn("pa_btns", ids(asked[0].args[2]))
        self.assertTrue(self.record()["pause_ask"]["card"]["closed"])
        self.tick(1030.0)
        self.assertEqual(len([call for call in self.put.call_args_list if call.args[1] == "card-1"]), 1)
        self.assertEqual(self.record()["state"], "active")  # The project's own card says 在做 on its next write.

    def test_a_project_that_moved_on_without_a_click_has_its_card_settled_as_handled_elsewhere(self):
        self.pause()
        self.tick(1010.0)
        projects.update(self.state, PID, lambda record: record.update(state=projects.ACTIVE, state_reason=""))
        self.put.reset_mock()
        self.tick(1020.0)
        line = [call for call in self.put.call_args_list if call.args[1] == "card-1"][0].args[2]
        self.assertIn("已在别处处理", text(line))

    def test_continue_and_finish_leave_the_lead_a_receipt_that_wakes_it_and_say_nothing_in_the_chat(self):
        inbox = lambda: [json.loads(path.read_text()) for path in sorted((projects.folder(self.state) / PID / "inbox").glob("*.json"))]  # noqa: E731

        self.pause(tokens_used=12_000_000)
        self.tick(1010.0)
        self.assertTrue(self.claude.controls.project_ask(self.lead_ctx, self.event("continue")))
        record = self.record()
        self.assertEqual((record["state"], record["token_floor"]), ("active", 12_000_000))
        [item] = inbox()
        self.assertEqual(item["kind"], "owner_choice")
        self.assertIn("选了继续", item["text"])
        self.assertFalse(self.claude.messenger.reply.called)
        self.pause()
        self.assertTrue(self.claude.controls.project_ask(self.lead_ctx, self.event("finish")))
        self.assertEqual(sum("project_finish" in item["text"] for item in inbox()), 1)  # Two files may share a millisecond: no order.

    def test_stop_is_the_project_cards_stop_and_pauses_as_the_owners_stop(self):
        from covey.core.jobs import BridgeReplyJob

        self.pause("wakes")
        self.tick(1010.0)
        for number, topic in ((5, LEAD), (6, T1)):
            job = BridgeReplyJob(job_id=number, user_text="x", chat_id=CHAT, message_id=f"om_job{number}", timeout=0, topic_id=topic)
            job.execution_started = True
            self.app.runtime.jobs.register(job)
        with mock.patch.object(self.claude.controls, "cancel_reply_job_from_card", return_value=True) as cancel:
            self.assertTrue(self.claude.controls.project_ask(self.lead_ctx, self.event("stop")))
        self.assertEqual(sorted((call.args[0].topic_id, call.args[1]["job_id"]) for call in cancel.call_args_list),
                         [(LEAD, "5"), (T1, "6")])
        self.assertEqual((self.record()["state"], self.record()["state_reason"]), ("paused", projects.OWNER_STOP))
        self.assertIn("已请 2 个任务停下", self.said())

    def test_a_second_click_and_a_click_on_a_card_of_an_earlier_pause_change_nothing_and_say_so(self):
        self.pause()
        self.tick(1010.0)
        self.assertTrue(self.claude.controls.project_ask(self.lead_ctx, {**self.event("continue"), "message_id": "om_old"}))
        self.assertIn("过期", self.said())
        self.assertEqual(self.record()["state"], "paused")
        self.assertTrue(self.claude.controls.project_ask(self.lead_ctx, self.event("continue")))
        self.assertTrue(self.claude.controls.project_ask(self.lead_ctx, self.event("stop")))
        self.assertIn("已经做过了", self.said())
        self.assertEqual(self.record()["state"], "active")

    def test_a_click_for_another_topic_or_a_project_not_there_is_refused(self):
        self.pause()
        for value in ({"topic_id": "other"}, {"project_id": "0f9e8d7c6b5a"}):
            event = self.event("stop")
            event["action_value"] = {**event["action_value"], **value}
            with self.assertRaises(ValueError):
                self.claude.controls.project_ask(self.lead_ctx, event)
        event = self.event("stop")
        event["action_value"]["kind"] = "bridge_project_stop"
        with self.assertRaises(ValueError):
            self.claude.controls.project_ask(self.lead_ctx, event)


class PinTest(ProjectsCase):
    """A project is one cell of the board (core/board_read.py groups) and its pin follows the state (app/pins.py aimed)."""

    def setUp(self):
        super().setUp()
        from test_projects import PID as PROJECT, THREAD as THREAD_ID

        self.pid, self.lead_id, self.thread_id = PROJECT, self.lead(), self.thread()
        projects.create(self.state, projects.new_record(chat_id="oc_dm", name="迁库", project_id=PROJECT, now=1000.0))
        projects.add_member(self.state, PROJECT, THREAD_ID)
        projects.update_card(self.state, PROJECT, card_id="card-p", message_id="om_project_card", sequence=3)

    def row(self, group, **record):
        return {"topic": self.lead_id, "group": group, "message_id": "om_thread_task_card", "at": 5.0,
                "record": {"topic_id": self.thread_id, **record}}

    def test_at_work_the_pin_is_the_project_card_waiting_the_question_done_the_answer_else_the_card(self):
        running = pins.aimed(self.state, "oc_dm", self.row("running"))
        self.assertEqual(running["message_id"], "om_project_card")
        self.assertEqual(running["topic"], self.lead_id)
        waiting = pins.aimed(self.state, "oc_dm", self.row("waiting"))
        self.assertEqual(waiting["message_id"], "om_thread_task_card")  # The question card is the row's own.
        done = pins.aimed(self.state, "oc_dm", self.row("result", answer_message_id="om_answer"))
        self.assertEqual(done["message_id"], "om_answer")
        bare = pins.aimed(self.state, "oc_dm", self.row("result"))
        self.assertEqual(bare["message_id"], "om_project_card")

    def test_a_project_with_no_card_yet_keeps_the_threads_own_pin_and_an_ordinary_topic_is_untouched(self):
        projects.update_card(self.state, self.pid, message_id="")
        self.assertEqual(pins.aimed(self.state, "oc_dm", self.row("running"))["message_id"], "om_thread_task_card")
        ordinary = self.topic("cd" * 16)
        row = {"topic": ordinary, "group": "running", "message_id": "om_task", "at": 1.0, "record": {}}
        self.assertEqual(pins.aimed(self.state, "oc_dm", row), row)

    def test_the_cell_is_named_by_the_project_and_with_the_switch_off_by_nothing_new(self):
        self.assertEqual(label_title(self.state, self.lead_id, "（话题未知）"), ("专项", "迁库"))
        self.assertEqual(board_read.topic_label(self.state, self.lead_id), ("专项", "迁库"))
        with mock.patch.dict(os.environ, {projects.ENV: "0"}):
            self.assertEqual(label_title(self.state, self.lead_id, "（话题未知）"), ("", "（话题未知）"))
            row = self.row("running")
            self.assertEqual(pins.aimed(self.state, "oc_dm", row), row)


class FoldedCardTest(CardCase):
    """A project folded away for being idle draws one line (``idle_card``) and the whole card again once it is active."""

    def fold(self, **fields):
        projects.update(self.state, PID, lambda record: record.update(state="paused", state_reason="idle", idle={"at": 1.0, "days": 7}, **fields))

    def test_the_line_says_the_days_and_that_new_work_opens_it_and_is_written_once(self):
        self.fold()
        self.tick(1010.0)
        card = self.put.call_args.args[2]
        self.assertEqual(self.put.call_count, 1)
        self.assertIn("收起 · 7 天没有新的事", text(card))
        self.assertIn("有新的事会自动展开", text(card))
        self.assertIsNone(find_element(card, "pj_btns"))
        self.assertFalse(self.record()["card"].get("closed"))
        self.tick(1020.0)
        self.assertEqual(self.put.call_count, 1)
        self.assertFalse(self.sent.called)  # No pause card: nothing is asked.

    def test_active_again_the_whole_card_is_drawn_over_the_line(self):
        self.fold()
        self.tick(1010.0)
        projects.update(self.state, PID, lambda record: record.update(state="active", state_reason=""))
        self.tick(1020.0)
        self.assertEqual(self.put.call_count, 2)
        self.assertIn("pj_btns", ids(self.put.call_args.args[2]))
        self.assertEqual([call.kwargs["sequence"] for call in self.put.call_args_list], [41, 42])

    def test_a_card_older_than_the_flip_is_not_sent_again_while_folded(self):
        self.fold()
        self.tick(1000.0 + project_cards.FLIP_AFTER + 10)
        self.assertFalse(self.sent.called)
        self.assertIn("收起", text(self.put.call_args.args[2]))

    def test_a_fraction_of_a_day_reads_as_it_is(self):
        projects.update(self.state, PID, lambda record: record.update(state="paused", state_reason="idle", idle={"at": 1.0, "days": 0.001}))
        self.tick(1010.0)
        self.assertIn("收起 · 0.001 天没有新的事", text(self.put.call_args.args[2]))

    def test_an_old_record_without_the_days_uses_the_setting(self):
        projects.update(self.state, PID, lambda record: record.update(state="paused", state_reason="idle"))
        with mock.patch.dict(os.environ, {projects.IDLE_ENV: "3"}):
            self.tick(1010.0)
        self.assertIn("收起 · 3 天没有新的事", text(self.put.call_args.args[2]))
