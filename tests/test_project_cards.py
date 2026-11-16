"""The 专项卡 (cards/project_card.py, app/project_cards.py): the card drawn from a project, its single writer (one whole-card
write a tick that changed, a new sequence for each, a failed write backed off), the 14 days, the line it shrinks to, the
buttons (in the table, with a handler, refused on another topic), the board's pin of a project and none of it with
COVEY_PROJECTS unset."""
from __future__ import annotations

import dataclasses
import itertools
import json
import os
import re
from types import SimpleNamespace
from unittest import mock

from support.fake_lark import FakeCardKit
from support.world import UnifiedCase
from test_projects import Case as ProjectsCase

from covey.app import pins, project_cards
from covey.app.card_actions import ACTIONS
from covey.cards import project_card as view
from covey.cards.elements import find_element
from covey.core import board_read, projects
from covey.core.jobs import upsert_job_record
from covey.core.topics import label_title, routed_topic

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


class CardTest(UnifiedCase):
    def test_the_header_names_the_project_its_state_and_the_direction_when_there_is_one(self):
        head = view.project_card(project(direction="数据平台"))["header"]
        self.assertEqual(head["title"]["content"], "把订单库迁到新集群")
        self.assertEqual([tag["text"]["content"] for tag in head["text_tag_list"]], ["在做", "数据平台"])
        self.assertEqual([tag["color"] for tag in head["text_tag_list"]], ["blue", "neutral"])
        plain = view.project_card(project(status=view.WAITING))["header"]
        self.assertEqual([tag["text"]["content"] for tag in plain["text_tag_list"]], ["等你"])
        self.assertEqual(plain["text_tag_list"][0]["color"], "orange")
        for status in (view.RUNNING, view.WAITING, view.PAUSED, view.DONE):
            self.assertEqual(view.project_card(project(status))["header"]["text_tag_list"][0]["text"]["content"], status)

    def test_the_elements_in_order_and_every_id_is_valid_and_there_is_no_input(self):
        card = view.project_card(project(need="T503 按日期还是按租户分区？", need_url=URL, direction="数据平台",
                                         lead_note="先迁小表", triage=("答了 一条",), memory=(("m1", "窗口在周六"),),
                                         library=(("方案.md",),), ask_text="迁库", ask_at="09:10", ask_url=URL))
        found = ids(card["body"]["elements"])
        self.assertEqual([name for name in found if not name.startswith(("pb_", "pq_"))],
                         ["answer_ask", "pj_need", "pj_meta", "pj_lead", "pt_a1b2c3_r", "pj_triage", "pj_mem", "pj_lib", "pj_io",
                          "pj_btns", "pj_steer", "pj_stop", "pj_down"])
        self.assertEqual(len(found), len(set(found)))
        self.assertTrue(all(ELEMENT_ID.match(name) for name in found), found)
        self.assertNotIn('"tag": "input"', text(card))
        self.assertNotIn('"tag": "form"', text(card))
        self.assertIn("打开问题卡 ›", find_element(card, "pj_need")["content"])
        self.assertIn(URL, find_element(card, "pj_need")["content"])
        self.assertIn("话题 1（在做 1） · 用时 3分00秒 · token 33万", find_element(card, "pj_meta")["content"])
        self.assertIn("先迁小表", find_element(card, "pj_lead")["content"])

    def test_what_is_empty_is_not_drawn(self):
        card = view.project_card(project(tokens="", lead_state="", members=()))
        found = ids(card)
        for name in ("pj_need", "answer_ask", "pj_triage", "pj_mem", "pj_lib", "pj_more"):
            self.assertNotIn(name, found)
        self.assertEqual(find_element(card, "pj_meta")["content"], "<font color='grey'>话题 0 · 用时 3分00秒</font>")
        self.assertIn("待命", find_element(card, "pj_lead")["content"])

    def test_threads_at_work_or_waiting_are_open_those_done_shut_and_a_change_of_state_is_a_new_element(self):
        threads = (thread(T1, view.RUNNING), thread(T2, view.DONE, label="T502"), thread(T3, view.WAITING, label="T503"))
        card = view.project_card(project(members=threads))
        folds = [element for element in card["body"]["elements"] if element.get("tag") == "collapsible_panel"
                 and element["element_id"].startswith("pt_")]
        self.assertEqual([(fold["element_id"], fold["expanded"]) for fold in folds],
                         [("pt_c3d4e5_w", True), ("pt_a1b2c3_r", True), ("pt_b2c3d4_d", False)])  # Waiting first, done last.
        ended = view.project_card(project(members=(thread(T1, view.DONE),)))
        self.assertEqual(ids(ended).count("pt_a1b2c3_d"), 1)
        self.assertNotIn("pt_a1b2c3_r", ids(ended))  # The same thread, another state: another id, so the client redraws it.
        self.assertNotIn("pb_a1b2c3_r", ids(ended))

    def test_at_most_eight_threads_are_folds_and_the_rest_is_one_line(self):
        many = tuple(thread(f"{index:02x}" * 3 + "0" * 10, view.DONE if index % 3 else view.RUNNING) for index in range(11))
        card = view.project_card(project(members=many))
        folds = [name for name in ids(card) if name.startswith("pt_")]
        self.assertEqual(len(folds), 8)
        self.assertIn("更早 3 条", find_element(card, "pj_more")["content"])
        self.assertLess(len(json.dumps(card, ensure_ascii=False, separators=(",", ":")).encode()), 14000)

    def test_memory_and_library_rows_carry_their_delete_button_the_triage_none(self):
        card = view.project_card(project(triage=("答了 一条", "转给你 一条"), memory=(("m1", "窗口在周六"), ("m2", "只读到周日")),
                                         library=(("方案.md",), ("表清单.csv",))))
        for name, count in (("pj_triage", 2), ("pj_mem", 2), ("pj_lib", 2)):
            fold = find_element(card, name)
            self.assertFalse(fold["expanded"])
            self.assertIn(f"· {count}", fold["header"]["title"]["content"])
        dels = [button for button in iter_buttons(card) if button.get("text", {}).get("content") == "删"]
        values = [button["behaviors"][0]["value"] for button in dels]
        self.assertEqual([(value["kind"], value.get("entry") or value.get("name")) for value in values],
                         [("bridge_project_mem_del", "m1"), ("bridge_project_mem_del", "m2"),
                          ("bridge_project_lib_del", "方案.md"), ("bridge_project_lib_del", "表清单.csv")])
        self.assertTrue(all(value["project_id"] == PID and value["topic_id"] == LEAD for value in values))

    def test_buttons_follow_the_state(self):
        def names(status):
            card = view.project_card(project(status))
            return [name for name in ("pj_steer", "pj_stop", "pj_down") if find_element(card, name)]
        self.assertEqual(names(view.RUNNING), ["pj_steer", "pj_stop", "pj_down"])
        self.assertEqual(names(view.WAITING), ["pj_steer", "pj_stop", "pj_down"])
        self.assertEqual(names(view.PAUSED), ["pj_steer", "pj_down"])
        self.assertEqual(names(view.DONE), [])
        card = view.project_card(project())
        self.assertEqual(find_element(card, "pj_stop")["value"], {"kind": "bridge_project_stop", "project_id": PID, "topic_id": LEAD})

    def test_the_lines_a_card_shrinks_to(self):
        done = view.settled_card("迁库", url=URL, direction="数据平台")
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
        self.assertIn("pt_a1b2c3_d", ids(self.put.call_args.args[2]))
        self.assertEqual(len({call.kwargs["key"] for call in self.put.call_args_list}), 2)

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

    def test_a_waiting_thread_makes_the_project_wait_for_the_owner_and_a_pause_shows_as_停住(self):
        self.job(T1, 1, "waiting_user", status_message="等你回答：按日期还是按租户分区？")
        self.tick(1010.0)
        card = self.put.call_args.args[2]
        self.assertEqual(card["header"]["text_tag_list"][0]["text"]["content"], "等你")
        self.assertIn("按日期还是按租户分区", find_element(card, "pj_need")["content"])
        projects.set_state(self.state, PID, projects.PAUSED, "主人停止")
        self.tick(1020.0)
        self.assertEqual(self.put.call_args.args[2]["header"]["text_tag_list"][0]["text"]["content"], "停住")
        self.assertIsNone(find_element(self.put.call_args.args[2], "pj_stop"))

    def test_the_card_says_what_the_record_and_the_files_say(self):
        projects.update(self.state, PID, lambda record: record.update(direction="数据平台", lead_note={"text": "先迁小表"},
                                                                   tokens_used=330000, triage=["答了：用哪个版本？"]))
        folder = projects.folder(self.state) / PID
        (folder / "library").mkdir(parents=True)
        (folder / "library" / "方案.md").write_text("x")
        (folder / "memory.json").write_text(json.dumps({"entries": [{"id": "m1", "text": "窗口在周六"}]}))
        self.tick(1010.0)
        card = self.put.call_args.args[2]
        self.assertEqual([tag["text"]["content"] for tag in card["header"]["text_tag_list"]], ["在做", "数据平台"])
        self.assertIn("先迁小表", find_element(card, "pj_lead")["content"])
        self.assertIn("token 33万", find_element(card, "pj_meta")["content"])
        self.assertTrue(all(name in ids(card) for name in ("pj_triage", "pj_mem", "pj_lib")))

    def test_no_thread_without_the_switch_and_no_write(self):
        self.assertEqual([name for name, _ in self.writer.threads()], ["project_cards"])
        with mock.patch.dict(os.environ, {projects.ENV: ""}):
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
        with mock.patch.dict(os.environ, {projects.ENV: ""}):
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
                              ("bridge_project_lib_del", "controls.project_lib_del"),
                              ("bridge_project_triage_wrong", "controls.project_triage_wrong")):
            self.assertEqual((ACTIONS[kind].handler, ACTIONS[kind].bot_only), (handler, False), kind)
        # The correction's submit queues a message in a member topic: bot_only, as a button that starts a task is.
        self.assertEqual((ACTIONS["bridge_project_triage_fix"].handler, ACTIONS["bridge_project_triage_fix"].bot_only),
                         ("controls.project_triage_fix", True))
        self.assertEqual(view.KINDS, {kind for kind in ACTIONS if kind.startswith("bridge_project_")})

    def test_a_click_for_another_topic_or_a_project_not_there_is_refused(self):
        handlers = (self.claude.controls.project_stop, self.claude.controls.project_downgrade, self.claude.steering.project_steer,
                    self.claude.controls.project_mem_del, self.claude.controls.project_lib_del)
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
        self.assertEqual(self.said(), project_cards.NO_LEAD_JOB)
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

    def test_a_memory_entry_and_a_library_file_are_deleted_by_their_own_row_alone(self):
        folder = projects.folder(self.state) / PID
        (folder / "library").mkdir(parents=True)
        for name in ("方案.md", "表清单.csv"):
            (folder / "library" / name).write_text("x")
        (folder / "outside.txt").write_text("keep")
        (folder / "memory.json").write_text(json.dumps(["窗口在周六", "只读到周日"]))
        self.assertTrue(self.claude.controls.project_mem_del(self.lead_ctx, self.event("bridge_project_mem_del", entry="0")))
        self.assertEqual(json.loads((folder / "memory.json").read_text()), ["只读到周日"])
        self.assertTrue(self.claude.controls.project_mem_del(self.lead_ctx, self.event("bridge_project_mem_del", entry="9")))
        self.assertIn("已经不在了", self.said())
        self.assertTrue(self.claude.controls.project_lib_del(self.lead_ctx, self.event("bridge_project_lib_del", name="方案.md")))
        self.assertEqual(sorted(path.name for path in (folder / "library").iterdir()), ["表清单.csv"])
        for name in ("../outside.txt", "/etc/passwd", ".hidden", "不在.md"):
            self.assertTrue(self.claude.controls.project_lib_del(self.lead_ctx, self.event("bridge_project_lib_del", name=name)))
            self.assertIn("已经不在了", self.said())
        self.assertTrue((folder / "outside.txt").exists())


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
        with mock.patch.dict(os.environ, {projects.ENV: ""}):
            self.assertEqual(label_title(self.state, self.lead_id, "（话题未知）"), ("", "（话题未知）"))
            row = self.row("running")
            self.assertEqual(pins.aimed(self.state, "oc_dm", row), row)
