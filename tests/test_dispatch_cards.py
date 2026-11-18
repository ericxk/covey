"""The 派发卡 (app/dispatch_cards.py, cards/dispatch_card.py, core/dispatch_records.py): sent when the butler's
tools hand work to a topic, kept up to date by one thread (throttled, a new sequence for every write, a failed write
backed off), one line once the conclusion went out, and none of it with COVEY_DISPATCH_CARD off."""
from __future__ import annotations

import itertools
import json
import os
import re
import time
from types import SimpleNamespace
from unittest import mock

from support.fake_lark import FakeCardKit
from support.world import UnifiedCase

from covey.app import dispatch_cards, butler_topics, relay_send, runner_ipc
from covey.app.card_actions import ACTIONS
from covey.app.butler_env import ButlerEnv
from covey.cards import dispatch_card as view
from covey.cards.elements import find_element
from covey.core import dispatch_records as records
from covey.core import butler_settings
from covey.core.jobs import BridgeReplyJob, upsert_job_record
from covey.core.butler_requests import Request
from covey.core.state import read_object, write_json
from covey.core.topics import routed_topic

CHAT = "oc_owner_dm"
OWNER = "om_owner_question"
ASK = "把这块代码提交到 GitHub 上吧，推之前先问我推哪些"
ELEMENT_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,19}$")
URL = "https://applink.example/client/thread/open?open_chat_id=oc_x"
ASK_URL = "https://applink.example/client/chat/open?openChatId=oc_x&position=76"


def ids(value):
    """Every element_id in a card, in order."""
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


def dispatch(status=view.RUNNING, **fields):
    base = dict(token="t" * 16, topic_id="topic1", request=ASK, status=status, label="T453", title="比对公司主干和 GitHub 快照",
                workspace="covey", started="12:41", took="2分00秒", body="第 2/4 步：列出待推提交",
                inputs=("Claude Code", "Sonnet", "思考 high", "covey"), outputs=("**开始** 12:41",), job_id="7", url=URL,
                ask_at="12:31", ask_url=ASK_URL)
    base.update(fields)
    return view.Dispatch(**base)


class CardTest(UnifiedCase):
    def test_the_card_has_the_header_the_line_the_fold_the_box_and_the_buttons(self):
        card = view.dispatch_card(dispatch())
        head = card["header"]
        self.assertEqual(head["title"]["content"], "把这块代码提交到 GitHub 上吧，推之前先问我推哪些")
        longer = view.dispatch_card(dispatch(request="把" * 40))["header"]["title"]["content"]
        self.assertEqual(longer, "把" * 29 + "…")
        self.assertEqual([tag["text"]["content"] for tag in head["text_tag_list"]], ["在做"])
        self.assertEqual(ids(card["body"]["elements"]),
                         ["answer_ask", "dp_meta", "dp_thread", "dp_body", "dp_io", "dp_btns", "dp_steer", "dp_stop", "dp_open"])
        self.assertEqual(find_element(card, "answer_ask")["behaviors"], [{"type": "open_url", "default_url": ASK_URL}])
        self.assertIn("你问 · 12:31", json.dumps(find_element(card, "answer_ask"), ensure_ascii=False))
        meta = find_element(card, "dp_meta")
        self.assertIn("T453 · 12:41 起 · 用时 2分00秒", meta["content"])
        fold = find_element(card, "dp_thread")
        self.assertTrue(fold["expanded"])
        self.assertIn("T453 · 比对公司主干和 GitHub 快照 · covey", fold["header"]["title"]["content"])
        self.assertIn("第 2/4 步：列出待推提交", find_element(card, "dp_body")["content"])
        self.assertEqual(find_element(card, "dp_stop")["value"], {"kind": "bridge_dispatch_stop", "topic_id": "topic1", "job_id": "7"})
        self.assertEqual(find_element(card, "dp_steer")["value"]["kind"], "bridge_dispatch_steer")
        self.assertEqual(find_element(card, "dp_open")["behaviors"], [{"type": "open_url", "default_url": URL}])

    def test_every_element_id_is_valid_and_no_input_is_in_the_card(self):
        for status in (view.RUNNING, view.WAITING, view.DONE, view.FAILED, view.STOPPED):
            card = view.dispatch_card(dispatch(status))
            found = ids(card)
            self.assertEqual(len(found), len(set(found)), status)
            self.assertTrue(all(ELEMENT_ID.match(name) for name in found), found)
            text = json.dumps(card, ensure_ascii=False)
            self.assertNotIn('"tag": "input"', text)
            self.assertNotIn('"tag": "form"', text)
        line = view.settled_card(view.DONE, ASK, url=URL, label="T453")
        self.assertEqual(ids(line), ["dp_line"])

    def test_the_fold_is_open_while_the_job_works_or_waits_and_shut_once_it_ended(self):
        opened = {status: find_element(view.dispatch_card(dispatch(status)), "dp_thread")["expanded"]
                  for status in (view.RUNNING, view.WAITING, view.DONE, view.FAILED, view.STOPPED)}
        self.assertEqual(opened, {"在做": True, "等你": True, "做完": False, "失败": False, "已停": False})

    def test_buttons_are_those_that_can_be_used_now(self):
        def buttons(**fields):
            card = view.dispatch_card(dispatch(**fields))
            return [name for name in ("dp_steer", "dp_stop", "dp_open") if find_element(card, name)]
        self.assertEqual(buttons(), ["dp_steer", "dp_stop", "dp_open"])
        self.assertEqual(buttons(job_id=""), ["dp_open"])  # No job yet: nothing to supplement or stop.
        self.assertEqual(buttons(url=""), ["dp_steer", "dp_stop"])
        self.assertEqual(buttons(status=view.WAITING), ["dp_open"])  # The question is the topic's.
        self.assertEqual(buttons(status=view.DONE), ["dp_open"])
        self.assertEqual(buttons(status=view.DONE, url=""), [])

    def test_the_single_line_says_what_ended_and_opens_the_conclusion_only_for_a_finished_job(self):
        done = view.settled_card(view.DONE, ASK, url=URL, label="T453")
        self.assertIn("✅ 做完 · 结论在下方", json.dumps(done, ensure_ascii=False))
        self.assertEqual(find_element(done, "dp_line")["behaviors"], [{"type": "open_url", "default_url": URL}])
        self.assertEqual(done["config"]["width_mode"], "compact")
        failed = view.settled_card(view.FAILED, ASK, reason="后端退出", label="T453")
        self.assertIn("⚠️ 失败 · 后端退出", json.dumps(failed, ensure_ascii=False))
        self.assertNotIn("behaviors", find_element(failed, "dp_line"))
        self.assertIn("⏹ 已停", json.dumps(view.settled_card(view.STOPPED, ASK), ensure_ascii=False))
        for built in (done, failed):
            self.assertNotIn("dp_thread", ids(built))
            self.assertNotIn("dp_btns", ids(built))


class RecordTest(UnifiedCase):
    def test_a_record_goes_from_live_to_settled_and_never_back(self):
        record = records.new_record(token="a" * 16, chat_id=CHAT, topic_id="t", job_key="main-job", mid="main-a", kind="start",
                                    owner_message=OWNER, request=ASK, starts=True)
        self.assertEqual(records.DISPATCH_TRANSITIONS, {"live": {"settled"}, "settled": set()})
        records.transition(record, records.SETTLED)
        self.assertEqual(record["state"], "settled")
        for state in (records.LIVE, records.SETTLED):
            with self.assertRaises(ValueError):
                records.transition(record, state)

    def test_a_new_record_leaves_the_door_for_projects_open_and_matches_no_word_of_the_owners(self):
        record = records.new_record(token="a" * 16, chat_id=CHAT, topic_id="t", job_key="main-job", mid="main-a", kind="start",
                                    owner_message=OWNER, request="这个长期做，接着做", starts=True, rounds=2)
        self.assertEqual((record["rounds"], record["upgrade_hint"], record["project_id"]), (2, {}, None))  # "长期做" is no hint: the butler decides (``project``).
        plain = records.new_record(token="b" * 16, chat_id=CHAT, topic_id="t", job_key="j", mid="m", kind="send",
                                   owner_message=OWNER, request=ASK, starts=True)
        self.assertEqual((plain["rounds"], plain["upgrade_hint"]), (1, {}))

    def test_a_note_is_only_left_for_a_card_that_exists_and_only_once(self):
        state = self.ctx.state_dir
        self.assertFalse(records.mark_relayed(state, "c" * 16, outcome="done", state="sent", message_id="om_1"))
        records.folder(state).mkdir(parents=True)
        records.record_path(state, "c" * 16).write_text("{}")
        self.assertTrue(records.mark_relayed(state, "c" * 16, outcome="done", state="sent", message_id="om_1"))
        self.assertFalse(records.mark_relayed(state, "c" * 16, outcome="done", state="sent", message_id="om_2"))
        self.assertEqual(records.relay_note(state, "c" * 16)["message_id"], "om_1")
        self.assertEqual(records.live_paths(state), [records.record_path(state, "c" * 16)])  # The note is no record.


class DispatchCase(UnifiedCase):
    def setUp(self):
        super().setUp()
        FakeCardKit().start(self)
        patcher = mock.patch.dict(os.environ, {dispatch_cards.ENV: "1"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(dispatch_cards._stopping.clear)
        self.app.runtime.access.remember_owner_chat(CHAT)
        self.numbers = itertools.count(1)
        self.sent = mock.Mock(side_effect=self.send_card)
        self.app.messenger.create_and_send_card = self.sent
        self.put = mock.Mock(return_value={"code": 0})
        self.app.messenger.put_card = self.put
        self.env = ButlerEnv(runtime=self.app.runtime, topics=self.app.topics, messenger=self.app.messenger,
                           voice=self.app.voice, state_dir=self.ctx.state_dir, profile="test-bot")
        self.ids = itertools.count(1)
        upsert_job_record(self.ctx.state_dir, "main-job", message_id=OWNER, reply_to_message_id=OWNER, chat_id=CHAT,
                          user_text=ASK, job_id=1, status="done", topic_id="main")
        link = mock.patch("covey.app.topic_links.topic_thread_link", return_value=(URL, "thread"))
        link.start()
        self.addCleanup(link.stop)
        asked = mock.patch("covey.app.answer_cards.ask_url", return_value=ASK_URL)
        self.ask_url = asked.start()
        self.addCleanup(asked.stop)
        self.cards = dispatch_cards.DispatchCards(self.app.runtime, self.app.messenger, self.app.voice, {"claude": "Claude Code"})

    def send_card(self, profile, chat, card, key, **kwargs):
        number = next(self.numbers)
        return kwargs.get("card_id") or f"card-{number}", f"om_card{number}"

    def dispatch_sends(self):
        """The calls that sent a 派发卡 (the ones whose card has the 线程 fold; a topic's own root card has none)."""
        return [call for call in self.sent.call_args_list if find_element(call.args[2], "dp_thread")]

    def start(self, request_id="r1", **args):
        fields = dict(title="查日志", prompt="看看昨晚的失败", workspace="project", readonly=True, user_requested=False,
                      why="日志在这个仓库")
        fields.update(args)
        request = Request(request_id, "topic_start", fields, "main-job", 0.0)
        return butler_topics.TOOLS["topic_start"](self.env, request)

    def token(self, request_id="r1"):
        return butler_topics.request_token(SimpleNamespace(id=request_id))

    def record(self, request_id="r1"):
        return read_object(records.record_path(self.ctx.state_dir, self.token(request_id)))

    def job_key(self, request_id="r1"):
        return "main-" + self.token(request_id)

    def set_job(self, request_id="r1", **fields):
        upsert_job_record(self.ctx.state_dir, self.job_key(request_id), **fields)

    def tick(self, now):
        self.cards.tick(self.ctx, now)

    def last_card(self):
        return self.put.call_args.args[2]


class DirectionGoneTest(DispatchCase):
    """The direction layer is gone: the switch and the arguments do nothing, an old record's field is kept and not drawn."""

    def tags(self):
        return [tag["text"]["content"] for tag in self.dispatch_sends()[-1].args[2]["header"]["text_tag_list"]]

    def test_the_old_switch_and_arguments_change_nothing_and_make_no_file(self):
        with mock.patch.dict(os.environ, {"COVEY_DIRECTIONS": "1"}):
            self.start("r1", direction="数据平台", direction_by_owner=True)
        self.assertNotIn("direction", self.record("r1"))
        self.assertEqual(self.tags(), ["在做"])
        self.assertFalse((self.ctx.state_dir / "directions.json").exists())

    def test_an_old_record_with_a_direction_is_read_and_its_card_draws_none(self):
        self.start("r2")
        path = records.record_path(self.ctx.state_dir, self.token("r2"))
        write_json(path, {**self.record("r2"), "direction": {"name": "数据平台", "source": "owner"}})
        self.tick(time.time() + 10)
        self.assertEqual(self.tags(), ["在做"])


class CreatedTest(DispatchCase):
    def test_the_card_is_a_message_of_its_own_in_the_chat_with_a_link_to_the_owners_and_a_quote_of_it_goes_to_the_topic(self):
        # Feishu puts a reply to a message that has a thread already into that thread (the topic hangs under the owner's
        # message), whatever reply_in_thread says: so the card is no reply (the 2026-11-15 check found it in the thread).
        answer = self.start()
        self.assertTrue(answer["ok"], answer)
        topic = answer["data"]["topic_id"]
        (profile, chat, card, key), kwargs = self.dispatch_sends()[-1]
        self.assertEqual(chat, CHAT)
        self.assertFalse(kwargs.get("reply_to_message_id"), kwargs)
        self.assertEqual(card["header"]["title"]["content"], ASK)
        ask = find_element(card, "answer_ask")
        self.assertEqual(ask["behaviors"][0]["default_url"], ASK_URL)  # His message at its place in the chat.
        self.assertIn(ASK, json.dumps(ask, ensure_ascii=False))
        record = self.record()
        message = record["card_message_id"]
        self.assertEqual((record["state"], record["topic_id"], record["owner_message"], record["rounds"],
                          record["project_id"]), ("live", topic, OWNER, 1, None))
        self.assertTrue(message.startswith("om_card"))
        self.assertEqual(routed_topic(self.ctx.state_dir, CHAT, message), topic)

    def test_a_request_asked_twice_sends_one_card(self):
        self.start()
        self.start()
        self.assertEqual(len(self.dispatch_sends()), 1)

    def test_with_the_switch_off_nothing_is_called_and_no_record_is_made(self):
        with mock.patch.dict(os.environ, {dispatch_cards.ENV: ""}):
            self.assertTrue(self.start("off")["ok"])
            self.assertEqual(self.cards.threads(), ())
        self.assertEqual(self.dispatch_sends(), [])
        self.assertEqual(records.live_paths(self.ctx.state_dir), [])

    def test_a_card_that_cannot_be_sent_leaves_the_topic_opened_and_says_so_in_the_log(self):
        self.sent.side_effect = lambda profile, chat, card, key, **kw: (
            ("", "") if find_element(card, "dp_thread") else self.send_card(profile, chat, card, key, **kw))
        self.assertTrue(self.start()["ok"])
        self.assertTrue(any("dispatch card not sent" in line for line in self.logs))

    def test_the_same_question_dispatched_again_counts_its_rounds(self):
        self.start("one")
        self.start("two")
        self.assertEqual((self.record("one")["rounds"], self.record("two")["rounds"]), (1, 1))  # Two topics.
        self.assertEqual(records.rounds_for(self.ctx.state_dir, OWNER, self.record("one")["topic_id"]), 2)

    def send(self, request_id, topic, text):
        request = Request(request_id, "topic_send", {"topic": topic, "text": text}, "main-job", 0.0)
        return butler_topics.TOOLS["topic_send"](self.env, request)

    def test_a_supplement_into_the_job_a_card_follows_sends_no_second_card(self):
        # T525 #4664 (2026-11-18): topic_send started the turn, the next one went in as its 补充 and sent a second card that
        # said the same, and that the conclusion never settled (its note goes to the token of the job's own request).
        topic = self.start()["data"]["topic_id"]
        job = self.app.runtime.jobs.find(0, self.job_key())
        job.execution_started = True
        job.backend_control = SimpleNamespace(steer=mock.Mock(return_value=None), remote=False)
        answer = self.send("r2", topic, "我是指做成插件")
        self.assertTrue(answer["ok"] and answer["data"]["steered"], answer)
        job.backend_control.steer.assert_called_once()
        self.assertEqual(len(self.dispatch_sends()), 1)
        self.assertEqual(self.record("r2"), {})
        self.assertTrue(any("dispatch card kept" in line for line in self.logs))

    def test_a_supplement_after_the_card_settled_sends_a_card_of_its_own(self):
        topic = self.start()["data"]["topic_id"]
        job = self.app.runtime.jobs.find(0, self.job_key())
        job.execution_started = True
        job.backend_control = SimpleNamespace(steer=mock.Mock(return_value=None), remote=False)
        self.assertTrue(records.mark_relayed(self.ctx.state_dir, self.token(), outcome="done", state="sent"))
        self.assertTrue(self.send("r2", topic, "再加一条")["data"]["steered"])
        self.assertEqual(len(self.dispatch_sends()), 2)
        self.assertEqual(self.record("r2")["mid"], self.job_key())

    def test_a_topic_that_waits_on_its_launch_card_says_it_waits_for_the_owner(self):
        butler_settings.update(self.ctx.state_dir, start=())
        self.start(harness="codex")
        self.assertEqual(self.dispatch_sends()[-1].args[2]["header"]["text_tag_list"][0]["text"]["content"], "等你")


class RefreshTest(DispatchCase):
    def setUp(self):
        super().setUp()
        self.assertTrue(self.start()["ok"])
        self.base = self.record()["started_at"]
        self.set_job(status="running", phase="跑测试", workspace_name="project", harness_id="claude", model="Claude · Sonnet", effort="high")
        self.put.reset_mock()

    def test_a_card_is_drawn_again_only_when_what_it_says_changed_and_not_oftener_than_the_gap(self):
        self.tick(self.base + 1)  # The link and the phase are new, but the card was sent a second ago.
        self.assertEqual(self.put.call_count, 0)
        self.tick(self.base + 3)
        self.assertEqual(self.put.call_count, 1)
        self.tick(self.base + 4)  # Nothing changed.
        self.assertEqual(self.put.call_count, 1)
        self.set_job(phase="第二阶段")
        self.tick(self.base + 4.5)  # Changed, but 1.5 s after the last write.
        self.assertEqual(self.put.call_count, 1)
        self.tick(self.base + 5.6)
        self.assertEqual(self.put.call_count, 2)
        self.assertIn("第二阶段", json.dumps(self.last_card(), ensure_ascii=False))
        self.assertEqual(len(self.dispatch_sends()), 1)  # The card was sent once; the rest are whole-card writes.

    def test_each_write_takes_a_new_sequence_and_a_failed_one_is_backed_off_and_not_repeated(self):
        sequences = []
        self.put.side_effect = lambda *a, **kw: sequences.append(kw["sequence"]) or (None if len(sequences) == 1 else {"code": 0})
        self.tick(self.base + 3)
        self.assertEqual(sequences, [1])
        self.tick(self.base + 4)  # Backed off 2 s.
        self.assertEqual(sequences, [1])
        self.tick(self.base + 5.2)
        self.assertEqual(sequences, [1, 2])  # Not sequence 1 again.
        self.assertEqual(self.record()["sequence"], 2)
        self.set_job(phase="again")
        self.tick(self.base + 9)
        self.assertEqual(sequences, [1, 2, 3])
        self.assertEqual(len(set(sequences)), 3)

    def test_the_status_follows_the_job(self):
        def tag(now):
            self.tick(now)
            return self.last_card()["header"]["text_tag_list"][0]["text"]["content"]
        self.assertEqual(tag(self.base + 3), "在做")
        self.set_job(status="waiting_user", status_message="等你选一个")
        self.assertEqual(tag(self.base + 7), "等你")
        self.set_job(status="running")
        self.assertEqual(tag(self.base + 11), "在做")
        self.set_job(status="done", status_message="已完成")
        self.assertEqual(tag(self.base + 15), "做完")
        self.assertFalse(find_element(self.last_card(), "dp_thread")["expanded"])

    def test_a_stop_click_says_stopping_before_the_jobs_record_does(self):
        # T525 #4664 (2026-11-18): the task runner's write of cancel_requested waited minutes on a stalled disk, and the
        # card the owner clicked went on saying the job worked.
        self.tick(self.base + 3)
        self.assertNotIn("正在停止", json.dumps(self.last_card(), ensure_ascii=False))
        number = dispatch_cards.current_job(self.ctx.state_dir, self.record())["job_id"]
        dispatch_cards.stop_clicked(self.record()["topic_id"], str(number), now=self.base + 4)
        self.tick(self.base + 6)
        self.assertIn("正在停止", find_element(self.last_card(), "dp_body")["content"])
        self.set_job(status="cancelled", status_message="用户停止了任务")
        self.tick(self.base + 9)
        self.assertEqual(self.last_card()["header"]["text_tag_list"][0]["text"]["content"], "已停")
        self.assertFalse(dispatch_cards.stopping(self.record()["topic_id"], {"job_id": number},
                                                 self.base + 4 + dispatch_cards.STOP_SHOWN + 1))

    def test_a_step_under_way_is_what_the_fold_says(self):
        steps = [{"title": "读代码", "state": "done"}, {"title": "改接缝", "state": "doing"}, {"title": "跑测试", "state": "todo"}]
        self.set_job(steps=steps)
        self.tick(self.base + 3)
        body = find_element(self.last_card(), "dp_body")["content"]
        self.assertIn("改接缝", body)
        self.assertNotIn("跑测试 ", body.replace("第 2/3 步", ""))

    def test_a_settled_record_is_not_read_or_drawn_again(self):
        self.tick(self.base + 3)
        path = records.record_path(self.ctx.state_dir, self.token())
        record = read_object(path)
        records.transition(record, records.SETTLED)
        path.write_text(json.dumps(record))
        self.put.reset_mock()
        self.set_job(phase="changed after settling")
        self.tick(self.base + 20)
        self.tick(self.base + 40)
        self.put.assert_not_called()
        self.assertIn(path.stem, self.cards.done)

    def test_the_thread_reads_no_lock_and_the_job_records_only(self):
        with mock.patch("covey.app.dispatch_cards.find_job_record", wraps=dispatch_cards.find_job_record) as found:
            self.tick(self.base + 3)
        self.assertTrue(found.called)

    def test_threads_exist_only_with_the_switch_on(self):
        self.assertEqual([name for name, _ in self.cards.threads()], ["dispatch_cards"])
        with mock.patch.dict(os.environ, {dispatch_cards.ENV: ""}):
            self.assertEqual(self.cards.threads(), ())


class SettleTest(DispatchCase):
    def setUp(self):
        super().setUp()
        self.assertTrue(self.start()["ok"])
        self.base = self.record()["started_at"]
        self.set_job(status="running", workspace_name="project")
        self.target = {"token": self.token(), "kind": "start", "topic_id": self.record()["topic_id"]}
        self.tick(self.base + 3)
        self.put.reset_mock()

    def where(self, message_id):
        return {"position": "88"}

    def settle(self, **note):
        dispatch_cards.relayed(self.ctx.state_dir, self.target, **{"outcome": "done", "state": "sent", "message_id": "om_conclusion", **note})
        with mock.patch("covey.app.topic_links.place_of", side_effect=lambda *a, **kw: {"position": "88"}):
            self.tick(self.base + 10)

    def test_a_sent_conclusion_shrinks_the_card_to_one_line_that_opens_it_and_settles_the_record(self):
        self.set_job(status="done", status_message="已完成")
        self.settle()
        card = self.last_card()
        self.assertEqual(ids(card), ["dp_line"])
        self.assertIn("✅ 做完 · 结论在下方", json.dumps(card, ensure_ascii=False))
        self.assertIn("applink.feishu.cn/client/chat/open?openChatId=oc_owner_dm&position=88",
                      find_element(card, "dp_line")["behaviors"][0]["default_url"])
        record = self.record()
        self.assertEqual((record["state"], record["settled_by"]), ("settled", "relay"))
        self.put.reset_mock()
        self.set_job(phase="late")
        self.tick(self.base + 30)
        self.put.assert_not_called()  # Settled: never drawn again.

    def test_a_failure_or_a_stop_says_why_on_one_line(self):
        self.set_job(status="failed", status_message="后端退出")
        self.settle(outcome="stopped")
        text = json.dumps(self.last_card(), ensure_ascii=False)
        self.assertIn("⚠️ 失败 · 后端退出", text)
        self.assertNotIn("behaviors", find_element(self.last_card(), "dp_line"))

    def test_a_job_the_owner_stopped_himself_settles_as_stopped(self):
        self.set_job(status="cancelled")
        self.settle(outcome="stopped", state="skipped", reason="owner_stop")
        self.assertIn("⏹ 已停", json.dumps(self.last_card(), ensure_ascii=False))
        self.assertEqual(self.record()["state"], "settled")

    def test_a_relay_that_did_not_go_out_leaves_the_card_as_it_is(self):
        self.assertFalse(records.relay_note(self.ctx.state_dir, self.token()))
        self.tick(self.base + 10)
        self.assertEqual(self.record()["state"], "live")
        self.assertFalse(self.put.called and "dp_line" in ids(self.last_card()))

    def test_a_relay_that_waits_on_the_owner_does_not_settle_anything(self):
        relay_send.dispatch_note(self.ctx.state_dir, self.target, "waiting", "sent", "om_q")
        self.assertFalse(records.relay_note(self.ctx.state_dir, self.token()))

    def test_a_failed_one_line_write_is_tried_again_and_the_record_stays_live_until_it_goes_out(self):
        self.set_job(status="done", status_message="已完成")
        self.put.return_value = None
        dispatch_cards.relayed(self.ctx.state_dir, self.target, "done", "sent", "om_conclusion")
        with mock.patch("covey.app.topic_links.place_of", return_value={"position": "88"}):
            self.tick(self.base + 10)
            self.assertEqual(self.record()["state"], "live")
            self.put.return_value = {"code": 0}
            self.tick(self.base + 11)  # Backed off.
            self.assertEqual(self.record()["state"], "live")
            self.tick(self.base + 14)
        self.assertEqual(self.record()["state"], "settled")

    def test_a_card_that_ended_and_got_no_conclusion_settles_as_it_is_after_a_while(self):
        self.set_job(status="done", status_message="已完成")
        self.tick(self.base + 100)  # Drawn as it ended.
        self.tick(self.base + 101)  # Unchanged: its end is counted from here.
        self.tick(self.base + 101 + dispatch_cards.TERMINAL_GRACE + 1)
        self.assertEqual((self.record()["state"], self.record()["settled_by"]), ("settled", "ended"))

    def test_the_note_is_not_left_without_a_card_or_with_the_switch_off(self):
        with mock.patch.dict(os.environ, {dispatch_cards.ENV: ""}):
            relay_send.dispatch_note(self.ctx.state_dir, self.target, "done", "sent", "om_x")
        self.assertFalse(records.relay_note(self.ctx.state_dir, self.token()))
        dispatch_cards.relayed(self.ctx.state_dir, {"token": "nothing"}, "done", "sent", "om_x")
        self.assertFalse(records.relay_note(self.ctx.state_dir, "nothing"))


class ButtonTest(DispatchCase):
    def test_the_kinds_are_in_the_table_with_a_handler_and_do_not_start_a_task(self):
        for kind, handler in (("bridge_dispatch_steer", "steering.dispatch_steer"),
                              ("bridge_dispatch_stop", "controls.dispatch_stop")):
            self.assertEqual((ACTIONS[kind].handler, ACTIONS[kind].bot_only), (handler, False))

    def test_a_click_on_another_topic_or_a_job_not_started_is_refused(self):
        codex = self.app.apps["codex"]
        ctx = SimpleNamespace(topic_id="topic1")
        for handler in (codex.controls.dispatch_stop, codex.steering.dispatch_steer):
            with self.assertRaises(ValueError):
                handler(ctx, {"action_value": {"kind": "x", "topic_id": "other", "job_id": "3"}, "chat_id": CHAT})
            with self.assertRaises(ValueError):
                handler(ctx, {"action_value": {"kind": "x", "topic_id": "topic1", "job_id": ""}, "chat_id": CHAT})

    def test_stop_acts_on_the_job_the_card_named_as_the_jobs_own_button_does(self):
        codex = self.app.apps["codex"]
        ctx = SimpleNamespace(topic_id="topic1")
        with mock.patch.object(codex.controls, "cancel_reply_job_from_card", return_value=True) as cancel:
            self.assertTrue(codex.controls.dispatch_stop(
                ctx, {"action_value": {"kind": "bridge_dispatch_stop", "topic_id": "topic1", "job_id": "7"},
                      "chat_id": CHAT, "event_id": "ev1"}))
        cancel.assert_called_once_with(ctx, {"job_id": "7", "message_id": ""}, CHAT, "ev1")

    def test_supplement_opens_the_jobs_own_form_card(self):
        codex = self.app.apps["codex"]
        ctx = SimpleNamespace(topic_id="topic1")
        with mock.patch.object(codex.steering, "steer_action") as steer:
            self.assertTrue(codex.steering.dispatch_steer(
                ctx, {"action_value": {"kind": "bridge_dispatch_steer", "topic_id": "topic1", "job_id": "7"},
                      "chat_id": CHAT, "event_id": "ev1"}))
        args = steer.call_args.args
        self.assertEqual((args[1], args[3], args[5]), (CHAT, {"kind": "bridge_steer", "topic_id": "topic1", "job_id": 7}, "ev1"))


class ClickTest(DispatchCase):
    """The buttons act on what the topic is doing when they are clicked, not on the job the card named when it was drawn
    (the check on 2026-11-15: 补充 on a running task was said to have ended; the stop found it)."""

    def setUp(self):
        super().setUp()
        self.assertTrue(self.start()["ok"])
        self.topic = self.record()["topic_id"]
        self.app.runtime.owner_open_id = "ou_owner"
        self.app.runtime.state_dir = self.ctx.state_dir
        self.app.runtime.jobs.by_id.clear()  # The one the start queued: these tests set the topic's jobs themselves.

    def job(self, number, topic=None, *, running=False, ended=False):
        job = BridgeReplyJob(job_id=number, user_text="x", chat_id=CHAT, message_id=f"om_job{number}", timeout=0,
                             topic_id=topic or self.topic)
        if running:
            job.execution_started, job.backend_control = True, SimpleNamespace(remote=False, supports_steer=True)
        if ended:
            job.cancel_event.set()
        self.app.runtime.jobs.register(job)
        return job

    def click(self, kind, job_id):
        event = {"operator_id": "ou_owner", "chat_id": CHAT, "message_id": self.record()["card_message_id"],
                 "event_id": "ev-click", "action_value": {"kind": kind, "topic_id": self.topic, "job_id": job_id}}
        with mock.patch.object(runner_ipc, "forward_card_action", return_value=True) as forward, \
                mock.patch.object(self.app.apps["claude"].messenger, "reply") as reply:
            self.app.handle_card_action_event(event, self.ctx)
        return forward, reply

    def test_a_button_whose_job_is_over_acts_on_the_one_the_topic_runs_now(self):
        # The card was drawn when #36 was the topic's job; the turn that follows (an answer, a 补充 that started one) is #37.
        self.job(36, ended=True)
        self.job(37, running=True)
        for kind in (view.STEER_KIND, view.STOP_KIND):
            forward, _ = self.click(kind, "36")
            forward.assert_called_once()
            _, target, event, raw = forward.call_args.args
            self.assertEqual((target.topic_id, raw["job_id"], raw["kind"]), (self.topic, "37", kind))
            self.assertEqual(event["action_value"]["job_id"], "37")

    def test_a_job_of_another_topic_is_not_the_one(self):
        self.job(35, topic="topic-of-the-main-thread", running=True)
        self.job(37, running=True)
        forward, _ = self.click(view.STEER_KIND, "35")
        self.assertEqual(forward.call_args.args[3]["job_id"], "37")

    def test_the_running_job_is_the_one_when_a_message_waits_behind_it(self):
        # Check 4 (2026-11-16): 停止 with a message queued behind the running turn cancelled only the queued one.
        self.job(37, running=True)
        self.job(38)  # Queued.
        for kind in (view.STEER_KIND, view.STOP_KIND):
            forward, _ = self.click(kind, "38")
            self.assertEqual(forward.call_args.args[3]["job_id"], "37")

    def test_a_queued_job_is_the_one_when_nothing_has_started(self):
        self.job(38)  # Queued, nothing running.
        forward, _ = self.click(view.STOP_KIND, "38")
        self.assertEqual(forward.call_args.args[3]["job_id"], "38")

    def test_a_topic_with_nothing_running_says_so_and_nothing_is_sent_to_a_task(self):
        self.job(36, ended=True)
        self.set_job(status="done")  # The turn the start queued.
        for kind in (view.STEER_KIND, view.STOP_KIND):
            forward, reply = self.click(kind, "36")
            forward.assert_not_called()
            self.assertEqual(reply.call_args.args[2], dispatch_cards.NO_JOB)

    def test_a_task_runner_this_bridge_has_not_adopted_yet_is_found_in_the_job_records(self):
        self.set_job(status="done")
        upsert_job_record(self.ctx.state_dir, "om_job41", message_id="om_job41", chat_id=CHAT, job_id=41, status="running",
                          topic_id=self.topic)
        forward, _ = self.click(view.STEER_KIND, "36")
        self.assertEqual(forward.call_args.args[3]["job_id"], "41")

    def test_a_stop_reaching_the_bridge_marks_the_card_stopping_at_once(self):
        self.job(37, running=True)
        self.click(view.STOP_KIND, "36")
        self.assertTrue(dispatch_cards.stopping(self.topic, {"job_id": 37}, time.time()))
        self.assertFalse(dispatch_cards.stopping(self.topic, {"job_id": 36}, time.time()))

    def test_the_task_runner_takes_the_click_as_it_is(self):
        self.app.runtime.runner = object()  # In a task runner: the Bridge has named the job already.
        self.job(37, running=True)
        forward, _ = self.click(view.STOP_KIND, "36")
        self.assertEqual(forward.call_args.args[3]["job_id"], "36")
