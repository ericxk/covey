"""手上的事 (app/overview.py, cards/overview.py): the card pinned in the owner's private chat, off unless
COVEY_PINNED_OVERVIEW=1; what it lists, when it is redrawn, pinned, replaced."""
from __future__ import annotations

import html
import json
import os
import re
import time
import unittest
from unittest import mock

from support.world import UnifiedCase
from support.records import write_topic
from covey.app import overview
from covey.app.overview import ENV, RECORD
from covey.cards.overview import SHOWN, overview_card, retired_card, when
from covey.core.jobs import upsert_job_record
from covey.core.state import write_private_text

NOW = 1793856000.0  # 2026-11-05T14:40:00+08:00


def plain(content):
    """A Markdown text as it reads: tags, bold marks and the state's dot taken off, entities read, spaces one."""
    text = html.unescape(re.sub(r"</?(font|text_tag)[^>]*>|\*\*|●", "", content))
    return " ".join(text.replace("　", " ").split())


def texts(card):
    """What the card says, one string per element of its body in order (the band, a group's heading, a topic row
    (cards/ui.py topic_row: its state word and head / its line / its figures), a grey note), its Markdown read as
    ``plain``."""
    def contents(node):
        if isinstance(node, dict):
            if node.get("tag") == "markdown":
                yield node["content"]
            for value in node.values():
                yield from contents(value)
        elif isinstance(node, list):
            for value in node:
                yield from contents(value)
    return [" / ".join(part for part in map(plain, contents(element)) if part) for element in card["body"]["elements"]]


def hm(seconds):
    return time.strftime("%H:%M", time.localtime(seconds))


def view(waiting=(), running=(), ended=()):
    return {"waiting": list(waiting), "running": list(running), "ended": list(ended)}


class CardTests(unittest.TestCase):
    def test_when(self):
        """A time of day, not a duration: the text stays the same as the clock runs, until the day changes."""
        self.assertEqual(when(NOW - 180, NOW), hm(NOW - 180))
        self.assertEqual(when(NOW - 180, NOW + 60), when(NOW - 180, NOW))
        self.assertEqual(when(NOW - 86400, NOW), "昨天 " + hm(NOW - 86400))
        self.assertEqual(when(NOW - 3 * 86400, NOW), time.strftime("%m-%d %H:%M", time.localtime(NOW - 3 * 86400)))

    def test_waiting_running_and_ended_in_that_order(self):
        """The 看板's look (T604 #52): the band, then each group's heading and its topic rows."""
        card = overview_card(view(
            waiting=[{"label": "T396", "title": "你为啥没有pin？", "ask": "置顶汇总卡", "since": NOW - 180,
                      "url": "https://applink.example/q"}],
            running=[{"label": "T395", "title": "为啥这么多重复的", "harness": "Claude Code",
                      "workspace": "algo-agent-devskills", "status": "running", "started": NOW - 39 * 60,
                      "phase": "删除分支在补文档"},
                     {"label": "T391", "title": "logid", "harness": "Codex", "workspace": "DataLab",
                      "status": "queued", "started": None, "phase": ""}],
            ended=[{"label": "T394", "title": "群晖公钥", "status": "done", "at": NOW - 60}]), NOW, "14:40")
        lines = texts(card)
        self.assertTrue(lines[0].startswith("要你处理 / 1 件事 / 先看：等你 · T396 你为啥没有pin？ / "), lines[0])
        self.assertEqual(lines[1:], [
            "等你 1", f"等你 T396 你为啥没有pin？ › / 置顶汇总卡 / {hm(NOW - 180)} 问的",  # › : it opens its card.
            "在做 2", f"在做 T395 为啥这么多重复的 / 删除分支在补文档 / {hm(NOW - 39 * 60)} 开始 ｜ Claude Code ｜ "
                    "algo-agent-devskills",
            "在做 T391 logid / 排队中 ｜ Codex ｜ DataLab",
            "刚结束 1", f"有结果 T394 群晖公钥 / {hm(NOW - 60)}",
            "更新于 14:40 · 只列这个私聊里的任务"])
        head = card["header"]
        self.assertEqual((head["title"]["content"], head["subtitle"]["content"], head["template"]),
                         ("手上的事", "私聊里在推进的事", "default"))
        self.assertEqual([tag["text"]["content"] for tag in head["text_tag_list"]], ["1 等你", "2 在做"])
        self.assertEqual(card["config"]["summary"]["content"], "手上的事 · 等你 1 · 在做 2")
        self.assertTrue(card["config"]["update_multi"])  # Redrawn in place for everyone who sees it.
        self.assertIn("band-0", card["config"]["style"]["color"])  # The band's colours (cards/ui.py styled).
        # The question's row opens its card and has 去回答; nothing else is a button.
        shown = json.dumps(card, ensure_ascii=False)
        self.assertEqual(re.findall(r'"text": \{"tag": "plain_text", "content": "([^"]+)"\}, "type"', shown), ["去回答"])
        self.assertEqual(shown.count('"default_url": "https://applink.example/q"'), 3)  # The band, the row, its button.

    def test_a_task_that_may_be_stuck_says_so_first(self):
        card = overview_card(view(running=[{"label": "T395", "title": "跑测试", "status": "running", "started": NOW - 3600,
                                            "phase": "全量", "progress": ["可能卡住了"]}]), NOW)
        lines = texts(card)
        self.assertTrue(lines[0].startswith("要你处理 / 1 件事 / 先看：可能卡住 · T395 跑测试"), lines[0])
        self.assertEqual(lines[2], f"可能卡住 T395 跑测试 / 全量 / {hm(NOW - 3600)} 开始")

    def test_nothing_to_show_and_no_stamp(self):
        card = overview_card(view(), NOW)
        self.assertEqual(texts(card), ["要你处理 / 都在掌握中 / 现在没有在做的任务，也没有在等你的事"])
        self.assertEqual([tag["text"]["content"] for tag in card["header"]["text_tag_list"]], ["都在掌握中"])

    def test_a_first_use_says_what_to_do_first_and_not_that_all_is_in_hand(self):
        """T604 #53: the card of a chat that never had a topic; ``first_use`` is app/overview.py's."""
        for shown in (view(), {**view(), "board": {"counts": {"waiting": 0, "running": 0, "result": 0, "unseen": 0},
                                                   "pinned": 0, "waiting": [], "running": [], "result": [], "stale": []}}):
            card = overview_card({**shown, "first_use": True}, NOW)
            lines = texts(card)
            self.assertEqual(lines[0], "要你处理 / 还没有话题 / 在私聊里发一句要做的事，第一个话题从这里开始")
            self.assertNotIn("都在掌握中", json.dumps(card, ensure_ascii=False))
            self.assertEqual([tag["text"]["content"] for tag in card["header"]["text_tag_list"]], ["还没有话题"])
        # With something asking, the flag changes nothing.
        asking = {"label": "T1", "title": "问", "ask": "？", "since": NOW - 60, "url": "https://x/q"}
        self.assertEqual(texts(overview_card({**view(waiting=[asking]), "first_use": True}, NOW)),
                         texts(overview_card(view(waiting=[asking]), NOW)))

    def test_text_from_a_topic_is_escaped_and_cut_and_long_parts_are_counted(self):
        rows = [{"label": f"T{n}", "title": "a*b_<x>" + "长" * 40, "harness": "", "workspace": "",
                 "status": "cancel_requested", "started": NOW - 10, "phase": ""} for n in range(SHOWN + 3)]
        card = overview_card(view(running=rows), NOW)
        lines = texts(card)
        self.assertEqual(lines[2], "在做 T0 a*b_<x>" + "长" * 22 + "… / 正在停止 / " + hm(NOW - 10) + " 开始")
        self.assertNotIn("<x>", json.dumps(card, ensure_ascii=False))  # Escaped: no tag of the topic's takes effect.
        self.assertEqual(len(lines), 2 + SHOWN + 1)
        self.assertEqual(lines[-1], "还有 3 件没列出")

    def test_questions_waiting_over_a_day_are_one_line_of_topic_numbers(self):
        rows = [{"label": "T394", "title": "群晖", "ask": "要不要装公钥？", "since": NOW - 60},
                *({"label": f"T{n}", "title": "旧", "ask": "补充信息", "since": NOW - 86400 - n} for n in range(12))]
        lines = texts(overview_card(view(waiting=rows), NOW))
        self.assertEqual(lines[1:], ["等你 13", f"等你 T394 群晖 / 要不要装公钥？ / {hm(NOW - 60)} 问的",
                                     "另有 12 个等了一天以上：T0、T1、T2、T3、T4、T5、T6、T7、T8、T9等"])
        lines = texts(overview_card(view(waiting=rows[1:3]), NOW))
        self.assertEqual(lines[1:], ["等你 2", "另有 2 个等了一天以上：T0、T1"])

    def test_a_full_card_lists_fewer_rows_a_group_to_keep_within_what_cardkit_takes(self):
        from covey.cards.elements import CARD_BYTES, CARD_NODES, element_count, json_size

        url = "https://applink.feishu.cn/client/thread/open?open_chat_id=oc_x&open_thread_id=omt_x&thread_position=7"
        card = overview_card(view(
            waiting=[{"label": f"T{n}", "title": "长" * 40, "ask": "问" * 120, "since": NOW - 60 * n, "url": url}
                     for n in range(12)],
            running=[{"label": f"T{n}", "title": "长" * 40, "harness": "Claude Code", "workspace": "algo-agent-devskills",
                      "status": "running", "started": NOW - 600, "phase": "阶" * 80, "progress": ["预计 15:20 左右做完"]}
                     for n in range(12)],
            ended=[{"label": f"T{n}", "title": "长" * 40, "status": "failed", "at": NOW - 30} for n in range(12)]), NOW, "14:40")
        self.assertLessEqual(element_count(card), CARD_NODES)
        self.assertLessEqual(json_size(card), CARD_BYTES)
        lines = texts(card)
        self.assertEqual(lines.count("还有 6 件没列出"), 3)  # Six rows a group, then a line saying how many more.
        self.assertIn("等你 12", lines)

    def test_the_retired_card_says_it_no_longer_changes(self):
        self.assertIn("已不再更新", json.dumps(retired_card(), ensure_ascii=False))


class OverviewCase(UnifiedCase):
    def setUp(self):
        super().setUp()
        self.state = self.ctx.state_dir
        messenger = self.app.messenger
        messenger.create_and_send_card = mock.Mock(return_value=("card1", "om_card1"))
        messenger.put_card = mock.Mock(return_value={"code": 0})
        messenger.pin = mock.Mock(return_value=True)
        messenger.unpin = mock.Mock(return_value=True)
        self.overview = self.app.overview
        self.app.runtime.access.remember_owner_chat("oc_dm")
        self.topic("t1", 395, "为啥这么多重复的\n附件：图片 1 张")

    def topic(self, topic_id, number, first):
        write_topic(self.state, {"topic_number": number}, topic_id=topic_id)
        write_private_text(self.state / "topics" / topic_id / "history.jsonl",
                           json.dumps({"role": "user", "display_content": first}) + "\n"
                           + json.dumps({"role": "assistant", "content": "好"}) + "\n")

    def job(self, key, job_id, status="running", chat="oc_dm", topic="t1", **fields):
        upsert_job_record(self.state, key, job_id=job_id, status=status, chat_id=chat, topic_id=topic,
                          harness_id="claude", workspace_name="algo-agent-devskills", **fields)

    def question(self, token, status="pending", chat="oc_dm", topic="t1", **fields):
        write_private_text(self.state / "questions" / (token + ".json"), json.dumps({
            "token": token, "topic_id": topic, "chat_id": chat, "job_id": 9, "status": status,
            "created_at": NOW - 120, "questions": [{"id": "q", "header": "置顶汇总卡", "question": "要不要做？"}],
            "question_message_id": f"om_{token}", **fields}))

    def record(self):
        return json.loads((self.state / RECORD).read_text())

    def card(self, call):
        """The card a create_and_send_card or put_card call carried."""
        return call.args[2]


class OverviewTests(OverviewCase):
    def test_off_unless_the_switch_is_set(self):
        self.assertNotIn("overview", dict(self.app.worker_targets()))
        with mock.patch.dict(os.environ, {ENV: "1"}):
            self.assertIn("overview", dict(self.app.worker_targets()))
        with mock.patch.dict(os.environ, {ENV: "yes"}):
            self.assertNotIn("overview", dict(self.app.worker_targets()))

    def test_the_card_says_group_when_the_notify_space_is_a_personal_group(self):
        import dataclasses
        access = self.app.runtime.access
        self.assertEqual("私聊", self.overview.where)
        access.spaces = dataclasses.replace(access.spaces, personal_groups=frozenset({"oc_room"}), notify_space="oc_room")
        self.assertEqual("群", self.overview.where)
        self.assertEqual("oc_room", self.overview.chat())

    def test_a_group_that_becomes_the_notify_space_gives_up_its_own_records(self):
        import dataclasses
        access = self.app.runtime.access
        own = {"schema_version": 1, "chat_id": "oc_room", "card_id": "card_old", "message_id": "om_old",
               "sequence": 3, "generation": 1, "sent_at": NOW, "pinned_at": NOW}
        (self.state / "pinned-overview.oc_room.json").write_text(json.dumps(own))
        (self.state / "pinned-topics.oc_room.json").write_text(json.dumps({
            "chat_id": "oc_room", "pins": [{"message_id": "om_pin", "topic_id": "t1"}]}))
        (self.state / "digest.oc_room.json").write_text(json.dumps({"last_slot": 1}))
        access.spaces = dataclasses.replace(access.spaces, personal_groups=frozenset({"oc_room"}), notify_space="oc_room")
        self.overview.tick(self.ctx, NOW)
        self.app.messenger.unpin.assert_any_call("test-bot", "om_pin")
        self.app.messenger.unpin.assert_any_call("test-bot", "om_old")
        self.assertEqual(retired_card(), self.app.messenger.put_card.call_args_list[0].args[2])
        for stem in ("pinned-overview", "pinned-topics", "digest"):
            self.assertFalse((self.state / f"{stem}.oc_room.json").exists())
            self.assertTrue((self.state / f"{stem}.oc_room.json.moved").exists())
        self.assertEqual("oc_room", self.record()["chat_id"])  # The default card is the group's now.

    def test_a_notify_group_that_gets_its_own_board_unpins_the_default_pins(self):
        (self.state / "pinned-topics.json").write_text(json.dumps({
            "chat_id": "oc_room", "pins": [{"message_id": "om_pin", "topic_id": "t1"}]}))
        self.overview.settle(self.ctx, "oc_dm")
        self.app.messenger.unpin.assert_called_once_with("test-bot", "om_pin")
        self.assertTrue((self.state / "pinned-topics.json.moved").exists())

    def test_a_failed_unpin_keeps_the_record_to_try_again(self):
        self.app.messenger.unpin.return_value = False
        (self.state / "pinned-topics.oc_room.json").write_text(json.dumps({
            "chat_id": "oc_room", "pins": [{"message_id": "om_pin", "topic_id": "t1"}]}))
        self.overview.settle(self.ctx, "oc_room")
        self.assertTrue((self.state / "pinned-topics.oc_room.json").exists())

    def test_nothing_is_moved_without_spaces(self):
        self.overview.settle(self.ctx, "oc_dm")
        self.assertEqual([], list(self.state.glob("*.moved")))

    def test_no_card_before_the_owners_private_chat_is_known(self):
        (self.state / "owner-chat.json").unlink()
        self.overview.tick(self.ctx, NOW)
        self.assertFalse(self.app.messenger.create_and_send_card.called)
        self.assertFalse((self.state / RECORD).exists())

    def test_the_first_look_sends_the_card_to_the_private_chat_and_pins_it(self):
        self.job("om_a", 1, phase="删除分支在补文档", timings={"backend_started": NOW - 600})
        self.job("om_other", 2, chat="oc_group")  # Another chat's task.
        self.job("om_launch", 3, status="waiting_workspace")  # A launch card still waiting.
        self.question("q1")
        self.question("q2", display_pending=True)  # Its card is not shown yet.
        self.question("q3", chat="oc_group")
        self.overview.tick(self.ctx, NOW)
        send = self.app.messenger.create_and_send_card
        self.assertEqual(send.call_count, 1)
        (_, chat, card, key), kwargs = send.call_args
        self.assertEqual(chat, "oc_dm")
        self.assertNotIn("reply_to_message_id", kwargs)  # Its own message, in no topic.
        self.assertEqual(key, self.app.voice.key("overview", "oc_dm-1"))
        self.assertEqual(texts(card)[1:5], [
            "等你 1", f"等你 T395 为啥这么多重复的 / 要不要做？ / {hm(NOW - 120)} 问的",
            "在做 1", f"在做 T395 为啥这么多重复的 / 删除分支在补文档 / {hm(NOW - 600)} 开始 ｜ Claude Code ｜ "
                    "algo-agent-devskills"])
        self.app.messenger.pin.assert_called_once_with("test-bot", "om_card1")
        record = self.record()
        self.assertEqual({k: record[k] for k in ("chat_id", "card_id", "message_id", "sequence", "generation",
                                                 "sent_at", "pinned_at")},
                         {"chat_id": "oc_dm", "card_id": "card1", "message_id": "om_card1", "sequence": 0,
                          "generation": 1, "sent_at": NOW, "pinned_at": NOW})
        self.assertFalse(self.app.messenger.put_card.called)
        self.assertEqual(self.said(), [])
        self.assertIn("pinned overview sent generation=1 message=om_card1", self.logs)
        self.assertIn("pinned overview pinned message=om_card1", self.logs)

    def test_redrawn_whole_when_it_changes_gap_apart_and_every_refresh(self):
        self.job("om_a", 1, timings={"backend_started": NOW})
        self.overview.tick(self.ctx, NOW)
        put = self.app.messenger.put_card
        self.overview.tick(self.ctx, NOW + 10)  # Nothing changed.
        self.assertFalse(put.called)
        self.job("om_b", 2)
        self.overview.tick(self.ctx, NOW + 11)  # Changed, but within GAP of the card going out.
        self.assertFalse(put.called)
        self.overview.tick(self.ctx, NOW + 20)
        self.assertEqual(put.call_count, 1)
        self.assertEqual(put.call_args.args[:2], ("test-bot", "card1"))
        self.assertEqual(put.call_args.kwargs["sequence"], 1)
        self.assertEqual(put.call_args.kwargs["key"], "overview-1-1")
        self.assertEqual(texts(self.card(put.call_args))[1], "在做 2")
        self.assertEqual(self.record()["sequence"], 1)
        self.overview.tick(self.ctx, NOW + 50)  # Unchanged: no redraw before REFRESH.
        self.assertEqual(put.call_count, 1)
        self.overview.tick(self.ctx, NOW + 20 + overview.REFRESH)
        self.assertEqual(put.call_count, 2)
        self.assertEqual(put.call_args.kwargs["sequence"], 2)
        self.assertEqual(self.record()["sequence"], 2)
        self.app.messenger.pin.assert_called_once()  # Pinned once.

    def test_the_clock_alone_does_not_redraw_it(self):
        """Its rows say since when, not for how long (2026-11-06: about 60 writes an hour the owner could not see)."""
        self.job("om_a", 1, timings={"backend_started": NOW - 600})
        self.question("q1")
        self.overview.tick(self.ctx, NOW)
        for minute in range(1, 60):
            self.overview.tick(self.ctx, NOW + 60 * minute)
        self.assertFalse(self.app.messenger.put_card.called)
        self.overview.tick(self.ctx, NOW + overview.REFRESH)  # The safety redraw, hourly.
        self.assertEqual(self.app.messenger.put_card.call_count, 1)

    def test_a_task_that_ended_stays_under_recently_ended_for_a_while(self):
        self.job("om_a", 1, timings={"backend_started": NOW})
        self.overview.tick(self.ctx, NOW)
        self.job("om_a", 1, status="done")
        self.overview.tick(self.ctx, NOW + 30)
        card = self.card(self.app.messenger.put_card.call_args)
        self.assertEqual(texts(card)[1:3], ["刚结束 1", f"有结果 T395 为啥这么多重复的 / {hm(NOW + 30)}"])
        self.overview.tick(self.ctx, NOW + 30 + overview.RECENT)
        card = self.card(self.app.messenger.put_card.call_args)
        self.assertEqual(texts(card)[0], "要你处理 / 都在掌握中 / 现在没有在做的任务，也没有在等你的事")

    def test_an_empty_card_is_a_first_use_only_until_the_chat_has_had_a_topic(self):
        self.overview.tick(self.ctx, NOW)
        card = self.card(self.app.messenger.create_and_send_card.call_args)
        self.assertEqual(texts(card)[0], "要你处理 / 还没有话题 / 在私聊里发一句要做的事，第一个话题从这里开始")
        self.job("om_a", 1)
        self.overview.tick(self.ctx, NOW + 30)
        self.job("om_a", 1, status="done")
        self.overview.tick(self.ctx, NOW + 60)
        self.overview.tick(self.ctx, NOW + 60 + overview.RECENT)
        card = self.card(self.app.messenger.put_card.call_args)
        self.assertEqual(texts(card)[0], "要你处理 / 都在掌握中 / 现在没有在做的任务，也没有在等你的事")
        self.assertIs(self.overview.never_had, False)  # For good: not asked again.

    def test_an_ended_task_whose_topic_runs_again_is_not_listed_as_ended(self):
        self.job("om_a", 1)
        self.overview.tick(self.ctx, NOW)
        self.job("om_a", 1, status="done")
        self.job("om_b", 2, phase="第二轮")  # The topic's next turn.
        self.overview.tick(self.ctx, NOW + 30)
        lines = texts(self.card(self.app.messenger.put_card.call_args))
        self.assertEqual(lines[1:3], ["在做 1", "在做 T395 为啥这么多重复的 / 第二轮 / 排队中 ｜ Claude Code ｜ "
                                                "algo-agent-devskills"])
        self.assertNotIn("刚结束 1", lines)

    def test_a_task_that_asked_a_question_is_not_ended(self):
        self.job("om_a", 1)
        self.overview.tick(self.ctx, NOW)
        self.job("om_a", 1, status="waiting_user")
        self.question("q1")
        self.overview.tick(self.ctx, NOW + 30)
        self.assertEqual(texts(self.card(self.app.messenger.put_card.call_args))[1], "等你 1")
        self.assertNotIn("刚结束 1", texts(self.card(self.app.messenger.put_card.call_args)))

    def test_the_latest_question_comes_first(self):
        self.topic("t2", 394, "群晖")
        self.question("q1", created_at=NOW - 600)
        self.question("q2", topic="t2", created_at=NOW - 60)
        self.overview.tick(self.ctx, NOW)
        lines = texts(self.card(self.app.messenger.create_and_send_card.call_args))
        self.assertEqual([line.split()[1] for line in lines[2:4]], ["T394", "T395"])

    def test_a_phase_the_model_did_not_write_is_left_out(self):
        self.job("om_a", 1, phase="准备处理本轮请求", timings={"backend_started": NOW})
        self.overview.tick(self.ctx, NOW)
        line = texts(self.card(self.app.messenger.create_and_send_card.call_args))[2]
        self.assertEqual(line, f"在做 T395 为啥这么多重复的 / {hm(NOW)} 开始 ｜ Claude Code ｜ algo-agent-devskills")

    def test_a_failed_pin_is_tried_again_later_and_a_pinned_card_never_again(self):
        self.app.messenger.pin.return_value = False
        self.overview.tick(self.ctx, NOW)
        self.overview.tick(self.ctx, NOW + 10)
        self.assertEqual(self.app.messenger.pin.call_count, 1)
        self.assertNotIn("pinned_at", self.record())
        self.assertIn("pinned overview pin failed", self.logs)
        self.app.messenger.pin.return_value = True
        self.overview.tick(self.ctx, NOW + overview.PIN_RETRY)
        self.assertEqual(self.app.messenger.pin.call_count, 2)
        self.assertEqual(self.record()["pinned_at"], NOW + overview.PIN_RETRY)
        restarted, _ = self.bridge()  # The next Bridge: the same card, no new pin.
        restarted.overview.messenger = self.app.messenger
        restarted.overview.tick(self.ctx, NOW + 2 * overview.PIN_RETRY)
        self.assertEqual(self.app.messenger.pin.call_count, 2)
        self.assertEqual(self.app.messenger.create_and_send_card.call_count, 1)
        # Redrawn at once (the retry's tick came before REFRESH, with nothing changed: no write).
        self.assertEqual([c.kwargs["sequence"] for c in self.app.messenger.put_card.call_args_list], [1])

    def test_a_send_that_failed_is_tried_again_with_the_same_key(self):
        send = self.app.messenger.create_and_send_card
        send.return_value = ("card0", "")
        self.overview.tick(self.ctx, NOW)
        self.overview.tick(self.ctx, NOW + 10)  # Within SEND_RETRY.
        self.assertEqual(send.call_count, 1)
        self.assertFalse((self.state / RECORD).exists())
        self.assertIn("pinned overview not sent generation=1 card=yes", self.logs)
        send.return_value = ("card1", "om_card1")
        self.overview.tick(self.ctx, NOW + overview.SEND_RETRY)
        self.assertEqual(send.call_count, 2)
        self.assertEqual(send.call_args_list[0].args[3], send.call_args_list[1].args[3])
        self.assertEqual(self.record()["message_id"], "om_card1")

    def test_an_old_card_gives_way_to_a_new_one(self):
        self.overview.tick(self.ctx, NOW)
        self.job("om_a", 1)
        self.overview.tick(self.ctx, NOW + 20)  # A redraw: the old card's sequence is 1.
        send, put = self.app.messenger.create_and_send_card, self.app.messenger.put_card
        send.return_value = ("card2", "om_card2")
        self.overview.tick(self.ctx, NOW + overview.ROTATE)
        self.assertEqual(send.call_count, 2)
        self.assertEqual(send.call_args.args[3], self.app.voice.key("overview", "oc_dm-2"))
        self.app.messenger.unpin.assert_called_once_with("test-bot", "om_card1")
        self.assertEqual(put.call_args.args[1:3], ("card1", retired_card()))
        self.assertEqual(put.call_args.kwargs["sequence"], 2)
        self.assertEqual(self.app.messenger.pin.call_args.args, ("test-bot", "om_card2"))
        record = self.record()
        self.assertEqual((record["card_id"], record["message_id"], record["generation"], record["sequence"]),
                         ("card2", "om_card2", 2, 0))
        self.assertIn("pinned overview retired message=om_card1 unpinned=True redrawn=True", self.logs)

    def test_a_card_that_cannot_be_redrawn_gives_way_too(self):
        self.overview.tick(self.ctx, NOW)
        put = self.app.messenger.put_card
        put.return_value = None
        for n in range(overview.FAILS):
            self.job("om_a", 1, phase=f"阶段 {n}")
            self.overview.tick(self.ctx, NOW + 20 * (n + 1))
        self.assertEqual(put.call_count, overview.FAILS)
        self.assertIn(f"pinned overview update failed failures={overview.FAILS}", self.logs)
        self.app.messenger.create_and_send_card.return_value = ("card2", "om_card2")
        self.overview.tick(self.ctx, NOW + 20 * (overview.FAILS + 1))
        self.assertEqual(self.app.messenger.create_and_send_card.call_count, 2)
        self.assertEqual(self.record()["message_id"], "om_card2")

    def test_a_new_private_chat_gets_its_own_card(self):
        self.overview.tick(self.ctx, NOW)
        (self.state / "owner-chat.json").unlink()
        self.app.runtime.access.remember_owner_chat("oc_dm2")
        self.app.messenger.create_and_send_card.return_value = ("card2", "om_card2")
        self.overview.tick(self.ctx, NOW + 30)
        self.assertEqual(self.app.messenger.create_and_send_card.call_args.args[1], "oc_dm2")
        self.app.messenger.unpin.assert_called_once_with("test-bot", "om_card1")
        self.assertEqual(self.record()["chat_id"], "oc_dm2")

    def test_a_topic_without_a_record_or_history(self):
        self.job("om_a", 1, topic="t-missing")
        self.overview.tick(self.ctx, NOW)
        self.assertIn("（话题未知）", texts(self.card(self.app.messenger.create_and_send_card.call_args))[2])


if __name__ == "__main__":
    unittest.main()


class TopLevelQuestionsTest(OverviewCase):
    """要你定的事 waits under 等你 whoever asks (docs/ui.md 5.3): the butler's question too, under its speaker."""

    def test_the_butlers_question_is_listed_under_its_speaker(self):
        from support.butler import switch_on
        from covey.core import board_read, butler

        switch_on(self)
        top = butler.butler_topic_id("oc_dm")
        self.question("a" * 16)
        self.question("b" * 16, topic=top, created_at=NOW - 30,
                      questions=[{"id": "q", "header": "部署", "question": "现在部署吗？"}])
        view = self.overview.view(self.ctx, "oc_dm", NOW)
        self.assertEqual([row["label"] for row in view["waiting"]], ["管家", "T395"])  # The latest first.
        self.assertEqual(view["waiting"][0]["title"], "")
        self.assertEqual([row["topic_id"] for row in board_read.pending_questions(self.state, "oc_dm")], ["t1"])
        self.assertEqual([row["topic_id"] for row in board_read.top_questions(self.state, "oc_dm")], [top])
        from covey.app.digest import collect
        rows = collect(self.state, "oc_dm", NOW, NOW - 3600, NOW)["waiting"]
        self.assertEqual([row["label"] for row in rows], ["管家", "T395"])


class SpeakerTest(unittest.TestCase):
    def test_the_words_of_each_speaker(self):
        from covey.core import look

        self.assertEqual(look.speaker("butler"), "管家")
        self.assertEqual(look.speaker("lead", name="Covey 体验"), "专项「Covey 体验」· 负责人")
        self.assertEqual(look.speaker("topic", label="T531", title="统一 UI 规范"), "T531 统一 UI 规范")
        self.assertEqual(look.speaker("topic", label="T9", title="一" * 40), "T9 " + "一" * 23 + "…")
        self.assertEqual(look.speaker("topic"), "")


class SpeakerSentenceTest(unittest.TestCase):
    def test_the_speaker_says_what_it_did_with_the_owners_words(self):
        from covey.cards.answer_card import says

        self.assertEqual(says("你问", "answer", "12:31"), "答复你 12:31 的问题")
        self.assertEqual(says("你问", "done", ""), "做完了你交代的事")
        self.assertEqual(says("你答", "waiting", "09:12"), "按你 09:12 的回答接着办，要你定")
        self.assertEqual(says("你问", "unknown", "09:12"), "答复你 09:12 的问题")
        self.assertEqual(says("你问", "report", "09:40"), "主动来汇报 · 09:40")

