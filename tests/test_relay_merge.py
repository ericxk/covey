"""连着做完的回带合成一条 (app/relay_send.py send_merged, T604 #49): a 做完了 relay goes at once when nothing else in the
chat is at work, as before; while other topics work, it waits COVEY_RELAY_MERGE_SECONDS for theirs and the ones that came
go as one 「N 个话题做完了」 card. 等你 and 停住了 never wait. Each record still leaves PENDING once (rule 6)."""
from __future__ import annotations

import json
import os
import threading
import time
import unittest
from unittest import mock

from test_butler_relay import OWNER, REPLY, TOPIC, Case
from covey.app import relay_send, runner
from covey.app.butler_relay import relay
from covey.cards.relay_card import DONE, STOPPED, WAITING, merged_card
from covey.core import background
from covey.core.jobs import job_key, upsert_job_record
from covey.core.state import read_object
from covey.core.topics import message_topic
from support.butler import BUTLER, CHAT

OTHER = "cd" * 16


class MergeCase(Case):
    def setUp(self):
        super().setUp()
        self.topic(OTHER, topic_id=OTHER, topic_number=411, origin={"title": "查日志"})
        self.opened("t1", plan={"start": True}, topic_id=TOPIC)
        self.opened("t2", at=2.0, plan={"start": True}, topic_id=OTHER)
        self.later = []  # The windows started, run by hand (``wait``).
        started = mock.patch.object(relay_send.background, "start", side_effect=lambda kind, target: self.later.append((kind, target)))
        started.start()
        self.addCleanup(started.stop)
        paused = mock.patch.object(relay_send, "pause")
        self.pause = paused.start()
        self.addCleanup(paused.stop)

    def working(self, message="main-t2", job_id=10, topic=OTHER, status="running"):
        upsert_job_record(self.state, job_key(message, job_id), job_id=job_id,
                          message_id=message, chat_id=CHAT, topic_id=topic, status=status)

    def wait(self):
        """Each window that was started ends now, in the order they started."""
        while self.later:
            kind, target = self.later.pop(0)
            self.assertEqual(kind, background.RELAY)
            target()

    def saved(self, message, job_id):
        return read_object(relay_send.relay_path(self.state, job_key(message, job_id)))


class AloneTest(MergeCase):
    def test_nothing_else_at_work_goes_at_once_as_before(self):
        send = self.send()
        self.assertEqual("sent", relay(self.state, self.job(), DONE, send=send, text=REPLY))
        self.assertEqual(self.later, [])
        self.assertEqual(TOPIC, message_topic(self.state, CHAT, "om_relay"))  # Its own card, the topic's.
        self.assertFalse((self.state / relay_send.WAITING).exists())

    def test_waiting_and_stopped_never_wait(self):
        self.working()
        send = self.send()
        self.assertEqual("sent", relay(self.state, self.job(), STOPPED, send=send, reason="超时"))
        self.assertEqual("sent", relay(self.state, self.job("main-t2", 10, topic=OTHER), WAITING, send=send,
                                       question="选哪个？"))
        self.assertEqual(self.later, [])
        self.assertEqual(send.call_count, 2)

    def test_off_every_relay_goes_at_once(self):
        self.working()
        with mock.patch.dict(os.environ, {relay_send.MERGE_ENV: "0"}):
            self.assertEqual("sent", relay(self.state, self.job(), DONE, send=self.send(), text=REPLY))
        self.assertEqual(self.later, [])

    def test_the_setting(self):
        self.assertEqual(relay_send.merge_seconds({}), relay_send.MERGE_SECONDS)
        self.assertEqual(relay_send.merge_seconds({relay_send.MERGE_ENV: "5"}), 5)
        for raw in ("x", "-1", str(relay_send.MERGE_MAX + 1)):
            self.assertEqual(relay_send.merge_seconds({relay_send.MERGE_ENV: raw}), relay_send.MERGE_SECONDS)

    def test_one_with_what_the_owner_must_see_goes_at_once_on_its_own_card(self):
        # T647 P1-A2: merged, its 没满足 or 提交号对不上 was gone, and a reply to it went to the butler.
        from covey.cards.answer_card import criteria_element
        self.working()
        send = self.send()
        unmet = [criteria_element([("没满足", "全量测试过")])]
        self.assertEqual("sent", relay(self.state, self.job(), DONE, send=send, text=REPLY, inputs=[], outputs=[],
                                       notes=unmet))
        self.assertEqual(self.later, [])
        self.assertIn("全量测试过", json.dumps(send.call_args.args[4], ensure_ascii=False))
        self.assertEqual(TOPIC, message_topic(self.state, CHAT, "om_relay"))  # A reply to it goes to T410.
        mismatch = {"commit": "951327a", "not_found": ["951327a"]}
        other = self.job("main-t2", 10, topic=OTHER)
        self.working("om_y", 14, topic=TOPIC)
        self.assertEqual("sent", relay(self.state, other, DONE, send=send, text="查完了", inputs=[], outputs=[],
                                       change=mismatch))
        self.assertEqual(self.later, [])

    def test_the_butlers_own_turn_and_its_own_topic_do_not_count(self):
        self.working("om_x", 12, topic=TOPIC)  # The same topic's next message: it relays after this one, not with it.
        upsert_job_record(self.state, job_key(OWNER, 13), job_id=13, message_id=OWNER,
                          chat_id=CHAT, topic_id=BUTLER, status="running")
        self.assertEqual("sent", relay(self.state, self.job(), DONE, send=self.send(), text=REPLY))


class MergedTest(MergeCase):
    def test_two_done_close_together_go_as_one_card(self):
        self.working()  # T411's task is at work while T410's ends.
        send = self.send()
        self.assertEqual(relay_send.PENDING, relay(self.state, self.job(), DONE, send=send, text=REPLY))
        send.assert_not_called()
        self.assertEqual(self.saved("main-t1", 9)["state"], relay_send.PENDING)
        upsert_job_record(self.state, job_key("main-t2", 10), status="done")
        other = self.job("main-t2", 10, topic=OTHER)
        self.assertEqual(relay_send.PENDING, relay(self.state, other, DONE, send=send, text="查完了：没有异常。"))
        self.wait()
        send.assert_called_once()
        chat, text, key, reply_to, card, _ = send.call_args.args
        self.assertEqual((chat, key, reply_to), (CHAT, job_key("main-t1", 9) + "-merged", ""))
        shown = json.dumps(card, ensure_ascii=False)
        for words in ("2 个话题做完了", "有结果", "**T410**", "**T411**", "**查日志**", "两种做法都行", "查完了：没有异常"):
            self.assertIn(words, shown)
        self.assertEqual(card["config"]["summary"]["content"], "2 个话题 · 有结果：T410、T411")
        self.assertTrue(text.startswith("**2 个话题做完了**"))
        first, second = self.saved("main-t1", 9), self.saved("main-t2", 10)
        self.assertEqual((first["state"], second["state"]), ("sent", "sent"))
        self.assertEqual(first["message_id"], second["message_id"])
        self.assertEqual(first["merged"], [job_key("main-t1", 9), job_key("main-t2", 10)])
        self.assertEqual(read_object(self.state / relay_send.WAITING), {})
        self.assertEqual(message_topic(self.state, CHAT, "om_relay"), BUTLER)  # Several topics'.
        self.assertEqual("sent", relay(self.state, other, DONE, send=send, text="又一次"))  # Never again.
        send.assert_called_once()

    def test_a_rows_files_are_counted_on_the_merged_card(self):
        # T647 P1-A2: merged, a turn's 产物 were gone without a word.
        upsert_job_record(self.state, job_key("main-t1", 9), job_id=9, message_id="main-t1",
                          result_file_path="/absolute/out/report.md", diff_artifact_path="/absolute/out/changes.diff")
        self.working()
        send = self.send()
        panel = {"tag": "collapsible_panel", "element_id": "answer_artifacts", "elements": []}
        self.assertEqual(relay_send.PENDING, relay(self.state, self.job(), DONE, send=send, text=REPLY, files=panel))
        self.assertEqual(self.saved("main-t1", 9)["item"]["files"], 2)
        upsert_job_record(self.state, job_key("main-t2", 10), status="done")
        self.assertEqual(relay_send.PENDING, relay(self.state, self.job("main-t2", 10, topic=OTHER), DONE, send=send,
                                                   text="查完了：没有异常。"))
        self.assertNotIn("files", self.saved("main-t2", 10)["item"])
        self.wait()
        shown = json.dumps(send.call_args.args[4], ensure_ascii=False)
        self.assertIn("两种做法都行，先用第一种 · 产物 2 个", shown)
        self.assertIn("回复这张卡是跟管家说", shown)

    def test_one_alone_after_its_window_is_its_own_card(self):
        self.working()
        send = self.send()
        self.assertEqual(relay_send.PENDING, relay(self.state, self.job(), DONE, send=send, text=REPLY))
        self.pause.assert_not_called()
        self.wait()
        self.pause.assert_called_once_with(relay_send.MERGE_SECONDS)
        chat, text, key, reply_to, card, _ = send.call_args.args
        self.assertEqual(key, job_key("main-t1", 9))
        row = card["body"]["elements"][0]  # The T number is its badge (cards/ui.py topic_badge), the state a tag.
        self.assertEqual("<font color='white'>**T410**</font>", row["columns"][0]["elements"][0]["elements"][0]["content"])
        self.assertIn("<text_tag color='green'>有结果</text_tag>", row["columns"][1]["elements"][0]["content"])
        self.assertEqual(self.saved("main-t1", 9)["state"], "sent")
        self.assertNotIn("merged", self.saved("main-t1", 9))
        self.assertEqual(TOPIC, message_topic(self.state, CHAT, "om_relay"))

    def test_a_merged_send_not_confirmed_is_uncertain_for_each_and_never_sent_again(self):
        self.working()
        send = mock.Mock(side_effect=TimeoutError())
        relay(self.state, self.job(), DONE, send=send, text=REPLY)
        relay(self.state, self.job("main-t2", 10, topic=OTHER), DONE, send=send, text="查完了")
        self.wait()
        self.assertEqual(send.call_count, 1)
        self.assertEqual((self.saved("main-t1", 9)["state"], self.saved("main-t2", 10)["state"]), ("uncertain", "uncertain"))
        self.assertEqual("uncertain", relay(self.state, self.job(), DONE, send=send, text=REPLY))
        self.assertEqual(send.call_count, 1)

    def test_one_taken_by_anothers_window_is_not_sent_by_its_own(self):
        self.working()
        send = self.send()
        relay(self.state, self.job(), DONE, send=send, text=REPLY)
        relay(self.state, self.job("main-t2", 10, topic=OTHER), DONE, send=send, text="查完了")
        first, second = self.later
        self.later = [second, first]  # The second's window ends first: it takes both.
        self.wait()
        send.assert_called_once()
        self.assertEqual(send.call_args.args[2], job_key("main-t2", 10) + "-merged")

    def test_a_relay_left_pending_by_a_runner_that_died_goes_with_the_next(self):
        self.working()
        relay(self.state, self.job(), DONE, send=self.send(), text=REPLY)
        self.later.clear()  # Its runner died in its window.
        send = self.send()
        relay(self.state, self.job("main-t2", 10, topic=OTHER), DONE, send=send, text="查完了")
        self.wait()
        self.assertIn("2 个话题", json.dumps(send.call_args.args[4], ensure_ascii=False))
        self.assertEqual(self.saved("main-t1", 9)["state"], "sent")


class CardTest(unittest.TestCase):
    def test_the_merged_card(self):
        built, text = merged_card([{"label": "T12", "title": "改看板", "lead": "按钮改好了", "url": "https://x/t12"},
                                   {"label": "T13", "title": "", "lead": ""}])
        head, first, second, hint = built["body"]["elements"]
        # A reply to it is the butler's (it holds several topics): its last line says so (T647 P1-A2).
        self.assertEqual("<font color='grey'>回复这张卡是跟管家说；要接着跟某个话题说，点那一行进话题</font>", hint["content"])
        self.assertTrue(head["content"].startswith("**2 个话题做完了** <text_tag color='green'>有结果</text_tag>"))
        # Each row is one block, the whole of it opening its topic; one whose link is not known opens nothing.
        self.assertEqual([{"type": "open_url", "default_url": "https://x/t12"}], first["behaviors"])
        self.assertNotIn("behaviors", second)
        # Each row: the topic's T number as its badge (its own link too, a click on it stops there), the title bold
        # and the lead grey beside it.
        badge, words = (column["elements"][0] for column in first["elements"][0]["columns"])
        self.assertEqual(("topic-0", "<font color='white'>**T12**</font>", "https://x/t12"),
                         (badge["background_style"], badge["elements"][0]["content"], badge["behaviors"][0]["default_url"]))
        self.assertEqual("**改看板**\n<font color='grey'>按钮改好了</font>", words["content"])
        badge, words = (column["elements"][0] for column in second["elements"][0]["columns"])
        self.assertNotIn("behaviors", badge)
        self.assertEqual("**T13**", words["content"])  # Nothing else to say: the number again, not an empty line.
        self.assertEqual({"topic-0", "topic-5"}, set(built["config"]["style"]["color"]))  # 12 × 5 % 12, 13 × 5 % 12.
        self.assertEqual(text, "**2 个话题做完了**\n- T12 改看板：按钮改好了\n- T13\n\n回复这张卡是跟管家说；要接着跟某个话题说，点那一行进话题")

    def test_a_row_counts_its_turns_files(self):
        built, text = merged_card([{"label": "T12", "title": "改看板", "lead": "按钮改好了", "files": 3},
                                   {"label": "T13", "title": "查日志", "lead": "", "files": 1}])
        first, second = (row["elements"][0]["columns"][1]["elements"][0]["content"] for row in built["body"]["elements"][1:3])
        self.assertEqual("**改看板**\n<font color='grey'>按钮改好了 · 产物 3 个</font>", first)
        self.assertEqual("**查日志**\n<font color='grey'>产物 1 个</font>", second)
        self.assertIn("- T12 改看板：按钮改好了（产物 3 个）", text)


    def test_too_many_for_one_card_the_last_go_on_one_line(self):
        """T889: 30 topics ending in one window took the card past Feishu's 30 KB and CardKit's 200 nodes."""
        from covey.cards.elements import element_count
        from covey.cards.pages import PAGE_NODES, fits
        from covey.cards.relay_card import MERGED_MORE
        rows = [{"label": f"T{n}", "title": f"第 {n} 个话题", "lead": "做完了，结论在话题里" * 3, "files": 2,
                 "url": f"https://applink.feishu.cn/client/thread/open?open_chat_id=oc_{n:040d}&open_thread_id=omt_{n:040d}"}
                for n in range(100, 130)]
        whole, _ = merged_card(rows)
        self.assertFalse(fits(whole))
        with mock.patch.object(relay_send, "linked", side_effect=lambda state, chat, built, text, profile, cards=None: (built, text)):
            built, text = relay_send.merged_fit("/absolute/state", "oc_x", rows, {}, None)
        self.assertTrue(fits(built))
        self.assertLessEqual(element_count(built), PAGE_NODES)
        shown = json.dumps(built, ensure_ascii=False)
        self.assertIn("30 个话题做完了", shown)
        left = [row["label"] for row in rows if f"**{row['label']}**" not in shown]
        self.assertTrue(left)
        self.assertIn(MERGED_MORE.format(n=len(left), labels="、".join(left)), shown)
        self.assertEqual(text.count("\n- T"), 30)  # The Markdown, sent when the card is refused, lists them all.


    def test_a_card_over_its_nodes_does_not_fit_however_small(self):
        from covey.cards.pages import PAGE_NODES, fits
        small = {"schema": "2.0", "body": {"elements": [{"tag": "hr"} for _ in range(PAGE_NODES)]}}
        self.assertTrue(fits(small))
        small["body"]["elements"].append({"tag": "hr"})
        self.assertFalse(fits(small))  # A few hundred bytes, yet CardKit would refuse it (300305).


class RunnerTest(unittest.TestCase):
    def test_a_relay_window_does_not_hold_the_topic_but_the_exit_waits_for_it(self):
        before = set(threading.enumerate())
        done = threading.Event()
        thread = threading.Thread(target=done.wait, args=(5,), name=background.RELAY, daemon=True)
        thread.start()
        started = time.monotonic()
        runner._wait_background(before, uploads=False)  # Before done.json: not waited for.
        self.assertLess(time.monotonic() - started, 1)
        self.assertIn(background.RELAY, runner.BACKGROUND)
        done.set()
        runner._wait_background(before)
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
