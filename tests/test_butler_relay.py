"""A topic turn the butler started or handed on, back at the top level (app/butler_relay.py, docs/butler.md
结论回到顶层): which turns, the card, sent once, and the board that leaves a relayed result out."""
from __future__ import annotations

import json

import time
import unittest
from types import SimpleNamespace
from unittest import mock

from support import make_job
from support.butler import CHAT, BUTLER, ButlerCase
from covey.app import auto_retry, butler_relay, pins, relay_send, relay_stops, relay_targets
from covey.app.butler_relay import relay
from covey.app.relay_targets import relay_target
from covey.cards.relay_card import CARD_TEXT_BYTES, DONE, STOPPED, WAITING, relay_card
from covey.app.turn_delivery import TurnDelivery
from covey.core.jobs import find_job_record, job_file, job_key, topic_job_records, upsert_job_record
from covey.core.state import read_object, write_json
from covey.core.topics import message_topic

TOPIC = "ab" * 16
OWNER = "om_owner"
REPLY = "## 结论\n两种做法都行，先用第一种。"


def unwrap(row):
    """A speaker's row without the block that makes it a link (cards/ui.py speaker_row with a link)."""
    return row["elements"][0] if row["tag"] == "interactive_container" else row


def row_line(row):
    """The words of a speaker's row (cards/ui.py speaker_row): its text column's, or the row itself alone."""
    row = unwrap(row)
    return row["columns"][1]["elements"][0]["content"] if row["tag"] == "column_set" else row["content"]


def badge_words(row):
    """What a topic's badge in a speaker's row says (cards/ui.py topic_badge)."""
    return unwrap(row)["columns"][0]["elements"][0]["elements"][0]["content"]


class Case(ButlerCase):
    context_topic = TOPIC

    def setUp(self):
        super().setUp()
        self.topic(TOPIC, topic_id=TOPIC, topic_number=410)
        # The butler's turn that took the owner's top-level message.
        upsert_job_record(self.state, job_key(OWNER, 5), job_id=5, message_id=OWNER, reply_to_message_id=OWNER,
                          chat_id=CHAT, topic_id=BUTLER, status="done")

    def opened(self, token, at=1.0, **record):
        return super().opened(token, **{"request": token, "job_key": job_key(OWNER, 5), "chat_id": CHAT, "at": at,
                                        **record})

    def job(self, message_id="main-t1", job_id=9, topic=TOPIC, **fields):
        return make_job(job_id=job_id, chat_id=CHAT, message_id=message_id, topic_id=topic, **fields)

    def send(self, result=True):
        def sent(chat, text, key, reply_to, card, on_sent):
            if result is True:
                on_sent("om_relay")
            return result
        return mock.Mock(side_effect=sent)


class TargetTest(Case):
    def test_topic_start_send_and_fork_turns_are_the_butlers(self):
        self.opened("t1", plan={"start": True}, topic_id=TOPIC)
        self.opened("t2", topic_id=TOPIC, answer={"ok": True, "data": {"topic_id": TOPIC, "steered": False}})
        self.opened("t3", fork_topic_id=TOPIC, source="cd" * 16)
        for token, kind in (("t1", "start"), ("t2", "send"), ("t3", "fork")):
            target = relay_target(self.state, self.job(message_id="main-" + token))
            self.assertEqual({"chat_id": CHAT, "reply_to": OWNER, "label": "T410", "token": token, "kind": kind,
                              "topic_id": TOPIC, "main_key": job_key(OWNER, 5)}, target)

    def test_a_supplement_topic_send_gave_a_running_turn(self):
        self.opened("old", at=1.0, answer={"ok": True, "data": {"topic_id": TOPIC, "job_id": 9, "steered": True}})
        upsert_job_record(self.state, job_key("om_later", 6), job_id=6, message_id="om_later", chat_id=CHAT,
                          topic_id=BUTLER)
        self.opened("new", at=2.0, job_key=job_key("om_later", 6),
                    answer={"ok": True, "data": {"topic_id": TOPIC, "job_id": 9, "steered": True}})
        target = relay_target(self.state, self.job(message_id="om_in_topic"))
        self.assertEqual(("steer", "new", "om_later"), (target["kind"], target["token"], target["reply_to"]))
        # Another job of the topic, or a send that was refused, is not.
        self.assertIsNone(relay_target(self.state, self.job(message_id="om_other", job_id=10)))

    def test_a_turn_the_owner_started_in_the_topic_is_not(self):
        self.opened("t1", plan={"start": True}, topic_id=TOPIC)
        self.assertIsNone(relay_target(self.state, self.job(message_id="om_typed_in_topic", job_id=11)))

    def test_the_butlers_own_turn_is_not(self):
        self.opened("t1", plan={"start": True})
        self.assertIsNone(relay_target(self.state, self.job(message_id="main-t1", topic=BUTLER)))

    def test_an_automatic_retry_relays_for_the_turn_it_continues(self):
        self.opened("t1", plan={"start": True}, topic_id=TOPIC)
        upsert_job_record(self.state, job_key("main-t1", 9), job_id=9, message_id="main-t1", auto_retry_state="queued")
        upsert_job_record(self.state, job_key("om_retry", 12), job_id=12, message_id="om_retry",
                          retry_of=job_key("main-t1", 9))
        self.assertEqual("start", relay_target(self.state, self.job(message_id="om_retry", job_id=12))["kind"])
        # A retry the owner clicked is his own.
        upsert_job_record(self.state, job_key("main-t1", 9), auto_retry_state="cancelled")
        self.assertIsNone(relay_target(self.state, self.job(message_id="om_retry", job_id=12)))

    def test_without_the_owners_message_there_is_nowhere_to_quote(self):
        self.opened("t1", plan={"start": True}, job_key="gone")
        self.assertIsNone(relay_target(self.state, self.job()))

    def test_a_topic_the_owner_opened_on_the_web_page_relays_every_turn_quoting_its_topic_card(self):
        """T762 (the owner, 2026-11-25: T753回复的内容没发到私聊区): a topic opened on the web page has its topic card
        at the top level, as one the butler opens at once (app/web_launch.py), but no request of the butler, so none of
        T753's five turns relayed and none of its three questions was copied up."""
        self.topic(TOPIC, topic_id=TOPIC, topic_number=410, chat_id=CHAT, root_id="om_web_root",
                   origin={"kind": "web", "request": "web-op1", "title": "看看视觉"})
        for job_id, message_id in ((9, "web-op1"), (10, "answer-tok"), (11, "om_typed_in_topic")):
            self.assertEqual({"chat_id": CHAT, "reply_to": "om_web_root", "label": "T410", "token": "web-op1",
                              "kind": "follow", "topic_id": TOPIC, "main_key": ""},
                             relay_target(self.state, self.job(message_id=message_id, job_id=job_id)), message_id)
        send = self.send()
        self.assertEqual("sent", relay(self.state, self.job(message_id="answer-tok", job_id=10), DONE, send=send,
                                       text=REPLY))
        chat, _, key, reply_to, _, _ = send.call_args.args
        self.assertEqual((CHAT, job_key("answer-tok", 10), ""), (chat, key, reply_to))  # Its own, at the top level.
        # Once the butler hands it work, its turns relay through that request, as any 话题区's do.
        self.opened("t2", topic_id=TOPIC, answer={"ok": True, "data": {"topic_id": TOPIC, "steered": False}})
        self.assertEqual(("follow", "t2", OWNER),
                         tuple(relay_target(self.state, self.job(message_id="om_later", job_id=12))[k]
                               for k in ("kind", "token", "reply_to")))

    def test_a_topic_from_the_web_page_of_another_chat_or_without_its_card_is_not(self):
        self.topic(TOPIC, topic_id=TOPIC, topic_number=410, chat_id="oc_other", root_id="om_web_root",
                   origin={"kind": "web", "request": "web-op1"})
        self.assertIsNone(relay_target(self.state, self.job(message_id="web-op1")))
        self.topic(TOPIC, topic_id=TOPIC, topic_number=410, chat_id=CHAT, root_id="web-op1",
                   origin={"kind": "web", "request": "web-op1"})
        self.assertIsNone(relay_target(self.state, self.job(message_id="web-op1")))
        self.topic(TOPIC, topic_id=TOPIC, topic_number=410, chat_id=CHAT, root_id="om_root",
                   origin={"kind": "launch"})
        self.assertIsNone(relay_target(self.state, self.job(message_id="om_typed_in_topic")))


class CardTest(unittest.TestCase):
    def test_a_long_reply_is_shown_whole(self):
        reply = "先说结论。\n\n" + "细节。" * 300 + "\n\n## 逐层结果\n\n最后一段。"
        card, text = relay_card("T410", DONE, text=reply, inputs=["Claude Code"], outputs=[], url="https://x/t410")
        # The speaker's row says whose it is: the T number its badge, the state as a tag (cards/ui.py speaker_row).
        row, fold, foot, hint = card["body"]["elements"]
        self.assertNotIn("header", card)
        self.assertEqual("<font color='white'>**T410**</font>", badge_words(row))
        self.assertTrue(row_line(row).startswith("<text_tag color='green'>有结果</text_tag>"))
        self.assertEqual("T410 · 有结果：先说结论", card["config"]["summary"]["content"])
        # No conclusion band (T638) and no fold (the owner, 2026-11-22: 不折叠，直接展示全文): the reply whole, nothing cut.
        self.assertEqual(("relay_reply", "markdown"), (fold["element_id"], fold["tag"]))
        self.assertEqual(reply, fold["content"])
        self.assertEqual(300, fold["content"].count("细节。"))
        self.assertEqual("https://x/t410", row["behaviors"][0]["default_url"])  # The row is the way in, no 进话题 ›.
        self.assertNotIn("进话题", foot["content"])
        self.assertNotIn("全文", text)
        self.assertTrue(text.startswith("先说结论。"))
        # Where a reply to it goes, its last line (T647 P1-A3): grey, small.
        self.assertEqual(("relay_hint", "<font color='grey'>回复这张卡，就是接着跟 T410 说</font>", "notation"),
                         (hint["element_id"], hint["content"], hint["text_size"]))

    def test_a_short_reply_is_the_card_s_body_without_a_heading_over_it(self):
        card, text = relay_card("T410", DONE, text=REPLY, inputs=["Claude Code"], outputs=[])
        row, reply, foot, _ = card["body"]["elements"]
        self.assertEqual("两种做法都行，先用第一种。", reply["content"])  # No "## 结论" row, no band saying it again.
        self.assertEqual(REPLY, text)

    def test_what_the_owner_must_see_is_out_on_the_card_and_the_rest_in_one_fold(self):
        # T647 P1-A2: 没满足 and 提交号对不上 sat folded in grey small print and went unseen.
        from covey.cards.answer_card import criteria_element
        from covey.cards.topic_actions import alone, relay_switch
        from support.cards import walk_elements
        change = {"commit": "951327a", "deployed": "", "verified": "全量过", "not_found": ["951327a"]}
        files = {"tag": "collapsible_panel", "element_id": "answer_artifacts", "elements": [],
                 "header": {"title": {"tag": "markdown", "content": "产物 2 个 · a.md 等"}}}
        card, _ = relay_card("T410", DONE, text=REPLY, inputs=["Claude Code"], outputs=["用时 1分"], files=files,
                             change=change, notes=[criteria_element([("没满足", "全量测试过")])],
                             buttons=alone(relay_switch("t" * 64, 7, "codex", 1)))
        ids = [element.get("element_id") for element in card["body"]["elements"]]
        self.assertEqual(ids[2:], ["answer_change_check", "answer_criteria", "answer_artifacts", "answer_facts",
                                   "relay_switch_row", "relay_hint"])
        self.assertNotIn("relay_details", ids)  # 产物 alone left to look up: no fold around it.
        folded = [e.get("element_id") for e in walk_elements(card["body"]["elements"]) if e.get("tag") == "collapsible_panel"]
        self.assertNotIn("answer_criteria", folded)
        self.assertIn("**提交号对不上**", json.dumps(card["body"]["elements"][2], ensure_ascii=False))
        # Two parts to look up (产物, 验收说明) go into one fold; the alerts stay out, above it.
        acceptance = {"tag": "column_set", "element_id": "answer_acceptance", "columns": []}
        card, _ = relay_card("T410", DONE, text=REPLY, inputs=[], outputs=["用时 1分"], files=files, change=change,
                             notes=[acceptance, criteria_element([("没满足", "全量测试过")])])
        ids = [element.get("element_id") for element in card["body"]["elements"]]
        self.assertEqual(ids[2:], ["answer_change_check", "answer_criteria", "relay_details", "answer_facts", "relay_hint"])

    def test_a_reply_too_large_for_a_card_goes_as_markdown(self):
        reply = "很长的回答。" * (CARD_TEXT_BYTES // 6)
        card, text = relay_card("T410", DONE, text=reply, inputs=[], outputs=[])
        self.assertIsNone(card)
        self.assertEqual(text, reply)  # The Markdown alone: its 产物 and box follow on a small card (send_once).

    def test_a_question_and_a_stop(self):
        card, _ = relay_card("T408", WAITING, question="选 A 还是 B？")
        row, lead, rest = card["body"]["elements"][0], card["body"]["elements"][1], card["body"]["elements"][2:]
        self.assertEqual("<font color='white'>**T408**</font>", badge_words(row))
        self.assertTrue(row_line(row).startswith("<text_tag color='orange'>等你</text_tag>"))
        # No band saying the row's 等你 again (T666): a short question is one bold line under the row, not said twice.
        self.assertEqual(lead, {"tag": "markdown", "content": "**要你定：选 A 还是 B？**"})
        self.assertNotIn("band-line", json.dumps(card))
        self.assertEqual(1, json.dumps(card["body"], ensure_ascii=False).count("选 A 还是 B？"))
        self.assertIn("在 T408 的问题卡里答，或在这里回「T408 …」", rest[0]["content"])
        long = "选 A 还是 B？" + "A 快一些但要改接口，B 慢一些但不动接口。" * 2
        card, text = relay_card("T408", WAITING, question=long)
        self.assertEqual("**要你定**", card["body"]["elements"][1]["content"])  # Then the question whole.
        self.assertEqual(long, card["body"]["elements"][2]["content"])
        self.assertTrue(text.startswith("**T408 等你**"))
        card, text = relay_card("T408", STOPPED, reason="用户停止了任务")
        self.assertIn("<text_tag color='red'>停住了</text_tag>", row_line(card["body"]["elements"][0]))
        self.assertIn("用户停止了任务", str(card["body"]["elements"][1]))
        self.assertTrue(text.startswith("**T408 停住了**：用户停止了任务"))
        self.assertEqual("T408 · 停住了：用户停止了任务", card["config"]["summary"]["content"])


class SendTest(Case):
    def setUp(self):
        super().setUp()
        self.opened("t1", plan={"start": True}, topic_id=TOPIC)

    def test_sent_once_quoting_the_owner_and_kept_as_the_topics(self):
        send = self.send()
        self.assertEqual("sent", relay(self.state, self.job(), DONE, send=send, text=REPLY))
        chat, _, key, reply_to, card, _ = send.call_args.args
        # A message of its own at the top level, quoting no card (butler_settings.threaded, 2026-11-08).
        self.assertEqual((CHAT, job_key("main-t1", 9), ""), (chat, key, reply_to))
        saved = read_object(relay_send.relay_path(self.state, job_key("main-t1", 9)))
        self.assertEqual(("sent", "om_relay", DONE), (saved["state"], saved["message_id"], saved["outcome"]))
        # The topic's: quoting it goes there (the owner, 2026-11-07: 引用直达).
        self.assertEqual(TOPIC, message_topic(self.state, CHAT, "om_relay"))
        self.assertEqual("sent", relay(self.state, self.job(), DONE, send=send, text=REPLY))
        self.assertEqual(1, send.call_count)  # The second call sends nothing.

    def test_its_t_number_opens_the_turns_own_card(self):
        # The owner, 2026-11-08: 点 T439 只能到话题上，到不了具体那个答案卡片上。
        from covey.app import topic_links
        with mock.patch.object(topic_links, "linkify", side_effect=lambda words, *a, **k: words) as linkify:
            relay(self.state, self.job(card_message_id="om_card9"), DONE, send=self.send(), text=REPLY)
        self.assertEqual({k.kwargs["cards"][TOPIC] for k in linkify.call_args_list}, {"om_card9"})

    def test_a_waiting_turn_opens_its_question_card(self):
        job = self.job(card_message_id="om_card9")
        self.assertEqual("om_card9", relay_targets.turn_card(self.state, job, WAITING))
        write_json(self.state / "questions" / "q1.json", {"job_id": 9, "topic_id": TOPIC, "chat_id": CHAT,
                   "status": "pending", "created_at": 1.0, "question_message_id": "om_question"})
        self.assertEqual("om_question", relay_targets.turn_card(self.state, job, WAITING))
        self.assertEqual("om_card9", relay_targets.turn_card(self.state, job, DONE))

    def test_an_unconfirmed_send_is_never_sent_again(self):
        send, log = mock.Mock(side_effect=TimeoutError()), mock.Mock()
        self.assertEqual("uncertain", relay(self.state, self.job(), DONE, send=send, log=log, text=REPLY))
        again = self.send()
        self.assertEqual("uncertain", relay(self.state, self.job(), DONE, send=again, log=log, text=REPLY))
        again.assert_not_called()
        # One line for the one try; the second call tries nothing (its "你问" link lines aside).
        tries = [call.args[0] for call in log.call_args_list if call.args[0].startswith("main relay ")]
        self.assertEqual(len(tries), 1, tries)
        self.assertRegex(tries[0], r"^main relay state=uncertain outcome=done job=\S+ kind=\w+ "
                                   r"message= error=TimeoutError$")
        self.assertFalse(butler_relay.relayed(self.state, {"key": job_key("main-t1", 9)}))

    def refused(self):
        """A send Feishu refused outright (feishu/messenger.py last_outcome NOT_SENT), as messenger_send makes it."""
        send = mock.Mock(return_value=False)
        send.outcome = lambda: relay_send.NOT_SENT
        return send

    def test_a_send_feishu_refused_goes_again_at_the_next_relay_under_its_key_at_most_three_more_times(self):
        """WAVE12 C10 (T904): surely not sent is ``failed``, which a later relay of the job sends again; uncertain never."""
        send, log = self.refused(), mock.Mock()
        path = relay_send.relay_path(self.state, job_key("main-t1", 9))
        self.assertEqual("failed", relay(self.state, self.job(), DONE, send=send, log=log, text=REPLY))
        saved = read_object(path)
        self.assertEqual((saved["state"], saved["tries"], saved["error"]), ("failed", 1, "not sent"))
        self.assertRegex(log.call_args.args[0], r"^main relay state=failed outcome=done job=\S+ kind=\w+ message= "
                                                r"error=not sent$")
        for _ in range(relay_send.RETRIES + 2):
            self.assertEqual("failed", relay(self.state, self.job(), DONE, send=send, text=REPLY))
        self.assertEqual(send.call_count, 1 + relay_send.RETRIES)
        self.assertEqual({call.args[2] for call in send.call_args_list}, {job_key("main-t1", 9)})  # Its own key.
        self.assertFalse(butler_relay.relayed(self.state, {"key": job_key("main-t1", 9)}))

    def test_a_refused_relay_that_goes_the_next_time_is_sent_once(self):
        self.assertEqual("failed", relay(self.state, self.job(), STOPPED, send=self.refused(), reason="超时"))
        send = self.send()
        self.assertEqual("sent", relay(self.state, self.job(), STOPPED, send=send, reason="超时"))
        self.assertEqual("sent", relay(self.state, self.job(), STOPPED, send=send, reason="超时"))
        send.assert_called_once()
        saved = read_object(relay_send.relay_path(self.state, job_key("main-t1", 9)))
        self.assertEqual((saved["state"], saved["message_id"]), ("sent", "om_relay"))

    def test_a_send_that_may_have_gone_is_uncertain_even_when_it_returned(self):
        send = mock.Mock(return_value=False)
        send.outcome = lambda: "uncertain"
        self.assertEqual("uncertain", relay(self.state, self.job(), DONE, send=send, text=REPLY))
        self.assertEqual("uncertain", relay(self.state, self.job(), DONE, send=self.send(), text=REPLY))
        send.assert_called_once()

    def test_a_reply_too_large_for_one_card_goes_on_several_each_with_its_row(self):
        # T663 (the owner, 2026-11-22): it went as Markdown with no row (no T badge, title, 有结果, way into the topic),
        # its 产物 and 输入/输出 on a small card after it.
        from covey.cards.answer_card import FACTS_ID
        from covey.cards.pages import fits
        reply = "## 结论\n先说结论。\n\n" + "\n\n".join(f"### 第{n}节\n" + "细节很长，" * 300 for n in range(1, 12))
        sent, ids = [], iter(f"om_page{n}" for n in range(1, 20))

        def one(chat, text, key, reply_to, card, on_sent):
            sent.append((key, text, card))
            on_sent(next(ids))
            return True
        self.assertEqual("sent", relay(self.state, self.job(), DONE, send=mock.Mock(side_effect=one), text=reply,
                                       inputs=["Claude Code"], outputs=[]))
        key = job_key("main-t1", 9)
        self.assertGreater(len(sent), 1)
        self.assertEqual([each[0] for each in sent], [key] + [f"{key}-p{n}" for n in range(2, len(sent) + 1)])
        for n, (_, text, card) in enumerate(sent, 1):
            row = card["body"]["elements"][0]
            self.assertEqual("<font color='white'>**T410**</font>", badge_words(row))
            self.assertIn("<text_tag color='green'>有结果</text_tag>", row_line(row))
            self.assertIn(f"{n}/{len(sent)}", row_line(row))
            self.assertTrue(fits(card))
            self.assertIn(text.split("\n")[0], json.dumps(card, ensure_ascii=False))  # Its Markdown is its part.
        last = [element.get("element_id") for element in sent[-1][2]["body"]["elements"]]
        self.assertEqual(last[-2:], [FACTS_ID, "relay_hint"])  # No small card after them; where a reply goes, last.
        self.assertEqual("".join(each[1] for each in sent).count("细节很长，"), 11 * 300)
        saved = read_object(relay_send.relay_path(self.state, key))
        self.assertEqual(("sent", "om_page1"), (saved["state"], saved["message_id"]))
        for n in range(1, len(sent) + 1):
            self.assertEqual(TOPIC, message_topic(self.state, CHAT, f"om_page{n}"))  # Each the topic's.

    def test_a_long_relay_s_reports_go_folded_on_cards_that_close_on_their_text(self):
        # T758 (the owner, 2026-11-25): T732's relay was on several cards and its 这一轮中途的汇报 open on the last.
        from covey.app import fold_close
        from covey.cards.pages import Paged
        from covey.core.trace import STAGE_HEADING, with_stage_notes
        reply = "## 结论\n先说结论。\n\n" + "\n\n".join(f"### 第{n}节\n" + "细节很长，" * 300 for n in range(1, 12))
        text = with_stage_notes(reply, [(None, f"第{n}次汇报：" + "进展" * 400) for n in range(1, 41)])
        sent, ids = [], iter(f"om_page{n}" for n in range(1, 30))

        def one(chat, text, key, reply_to, card, on_sent):
            sent.append((key, text, card))
            on_sent(next(ids))
            return True
        self.assertEqual("sent", relay(self.state, self.job(), DONE, send=mock.Mock(side_effect=one), text=text,
                                       inputs=["Claude Code"], outputs=[]))
        self.assertTrue(all(STAGE_HEADING not in words for _, words, _ in sent))
        folded = [n for n, (_, _, card) in enumerate(sent, 1) if fold_close.has_fold(card)]
        self.assertGreater(len(folded), 1)
        for n in folded:  # Each kept, so a click on its text closes it.
            self.assertTrue(fold_close.path_of(self.state, f"om_page{n}").exists())
        self.assertFalse(fold_close.path_of(self.state, "om_page1").exists())  # The reply's first card has none.
        # The cards left the reports to the small card of the parts: it carries them, folded first.
        for path in list((self.state / "main-thread" / "relays").glob("*.json")):
            path.unlink()
        sent.clear()
        fold = {"tag": "collapsible_panel", "element_id": "relay_reply_s0", "expanded": False,
                "elements": [{"tag": "interactive_container", "elements": [],
                              "behaviors": [{"type": "callback", "value": {"kind": "bridge_fold_close", "fold": "relay_reply_s0"}}]}]}
        real = relay_send.relay_pages

        def alone(*args, **kwargs):
            return Paged(real(*args, **kwargs).pages, True, (fold,))
        ids = iter(f"om_next{n}" for n in range(1, 30))
        with mock.patch("covey.app.relay_send.relay_pages", side_effect=alone):
            self.assertEqual("sent", relay(self.state, self.job(), DONE, send=mock.Mock(side_effect=one), text=reply,
                                           inputs=["Claude Code"], outputs=[]))
        key, _, card = sent[-1]
        self.assertTrue(key.endswith("-tail"))
        self.assertIn("relay_reply_s0", [element.get("element_id") for element in card["body"]["elements"]])
        self.assertTrue(fold_close.path_of(self.state, f"om_next{len(sent)}").exists())

    def test_a_sent_relay_is_a_log_line(self):
        log = mock.Mock()
        self.assertEqual("sent", relay(self.state, self.job(), DONE, send=self.send(), log=log, text=REPLY))
        self.assertRegex(log.call_args.args[0], r"^main relay state=sent outcome=done job=\S+ kind=\w+ "
                                                r"message=om_relay$")

    def test_the_record_says_submitting_before_the_send(self):
        def check(*args):
            path = relay_send.relay_path(self.state, job_key("main-t1", 9))
            self.assertEqual("submitting", read_object(path)["state"])
            return True
        relay(self.state, self.job(), STOPPED, send=mock.Mock(side_effect=check), reason="x")


    def test_a_card_that_cannot_be_made_is_unsent_and_never_submitting(self):
        # T441 #3270 (11-08): a part of the card raised before the send, and the relay was lost with no record.
        from covey.app import relay_send
        path = relay_send.relay_path(self.state, job_key("main-t1", 9))
        for name in ("relay_card", "linked"):
            path.unlink(missing_ok=True)
            send, log = self.send(), mock.Mock()
            with mock.patch.object(relay_send, name, side_effect=AttributeError("x")):
                self.assertEqual("unsent", relay(self.state, self.job(), DONE, send=send, log=log, text=REPLY))
            send.assert_not_called()
            saved = read_object(path)
            self.assertEqual(("unsent", "AttributeError", DONE), (saved["state"], saved["error"], saved["outcome"]))
            self.assertRegex(log.call_args.args[0], r"^main relay state=unsent outcome=done job=\S+ kind=start "
                                                    r"error=AttributeError$")
            # Kept as it is: nothing sends it later, the next call included.
            self.assertEqual("unsent", relay(self.state, self.job(), DONE, send=send, text=REPLY))
            send.assert_not_called()
            self.assertFalse(butler_relay.relayed(self.state, {"key": job_key("main-t1", 9)}))


    def test_a_stop_the_owner_pressed_is_recorded_not_sent(self):
        # D4 (the owner, 2026-11-08): he pressed 停止 on its card in the 话题区's thread, looking at it.
        send, log = self.send(), mock.Mock()
        job = self.job(cancel_requested_by="card")
        self.assertEqual("skipped", relay(self.state, job, STOPPED, send=send, log=log, reason="用户停止了任务"))
        send.assert_not_called()
        saved = read_object(relay_send.relay_path(self.state, job_key("main-t1", 9)))
        self.assertEqual(("skipped", "owner_stop", STOPPED), (saved["state"], saved["why"], saved["outcome"]))
        self.assertRegex(log.call_args.args[0], r"^main relay state=skipped outcome=stopped job=\S+ kind=start "
                                                r"why=owner_stop$")
        self.assertEqual("skipped", relay(self.state, job, STOPPED, send=send, reason="x"))
        send.assert_not_called()

    def test_a_failure_still_goes_and_desk_topics_left_true_changes_nothing(self):
        from covey.core import butler_settings
        send = self.send()
        self.assertEqual("sent", relay(self.state, self.job(), STOPPED, send=send, reason="超时"))
        # desk_topics, retired on 2026-11-09, sent the owner's own stops too; read as false now.
        (self.state / butler_settings.FILE).write_text('{"mode": "agent", "desk_topics": true}')
        self.opened("t2", plan={"start": True}, topic_id=TOPIC)
        again = self.send()
        self.assertEqual("skipped", relay(self.state, self.job("main-t2", cancel_requested_by="card"), STOPPED,
                                          send=again, reason="用户停止了任务"))
        again.assert_not_called()


class OriginTest(Case):
    """T911: a topic's own ``origin`` names the request that opened it, so the relay needs no main-thread/opened/ record
    (daily maintenance clears the folder after 7 days)."""

    def origin(self, **fields):
        return {"kind": "main", "request": "t1", "job_key": job_key(OWNER, 5), "title": "查日志", "at": 1.0, **fields}

    def test_without_the_ledger_the_origin_still_relays(self):
        self.topic(TOPIC, topic_id=TOPIC, topic_number=410, chat_id=CHAT, origin=self.origin())
        token = relay_targets._token_of("t1")
        job = self.job(message_id="main-" + token)
        target = relay_target(self.state, job)  # No opened/ record exists.
        self.assertEqual(("start", OWNER, token), (target["kind"], target["reply_to"], target["token"]))
        send = self.send()
        self.assertEqual("sent", relay(self.state, job, DONE, send=send, text=REPLY))
        self.assertEqual(OWNER, send.call_args.args[3] or OWNER)
        # A later turn of the topic (its own words in the thread) follows the same origin.
        follow = relay_target(self.state, self.job(message_id="om_in_topic", job_id=12))
        self.assertEqual(("follow", OWNER), (follow["kind"], follow["reply_to"]))

    def test_the_origin_keeps_the_owners_message_when_the_butlers_job_is_gone(self):
        self.topic(TOPIC, topic_id=TOPIC, topic_number=410, chat_id=CHAT,
                   origin=self.origin(job_key="gone", message_id="om_kept"))
        job = self.job(message_id="main-" + relay_targets._token_of("t1"))
        self.assertEqual("om_kept", relay_target(self.state, job)["reply_to"])

    def test_an_origin_of_another_request_or_an_old_one_falls_back_to_the_ledger(self):
        self.topic(TOPIC, topic_id=TOPIC, topic_number=410, chat_id=CHAT, origin=self.origin(request="other"))
        self.opened("t1", plan={"start": True}, topic_id=TOPIC)
        self.assertEqual("t1", relay_target(self.state, self.job())["token"])
        self.topic(TOPIC, topic_id=TOPIC, topic_number=410, chat_id=CHAT, origin={"title": "老话题"})
        self.assertEqual("t1", relay_target(self.state, self.job())["token"])  # No request in it.

    def test_a_single_topic_relay_card_is_routed_to_its_topic(self):
        from covey.core.topics import routed_topic
        self.opened("t1", plan={"start": True}, topic_id=TOPIC)
        self.assertEqual("sent", relay(self.state, self.job(), DONE, send=self.send(), text=REPLY))
        self.assertEqual(TOPIC, routed_topic(self.state, CHAT, "om_relay"))


class AskedTest(Case):
    """A question asked while the turn runs (relay_question, app/native_questions.py): at the top level once."""

    def setUp(self):
        super().setUp()
        self.opened("t1", plan={"start": True}, topic_id=TOPIC)
        self.question = {"token": "q1", "questions": [{"question": "选 A 还是 B？"}]}

    def test_each_question_goes_once_quoting_the_owner(self):
        send = self.send()
        self.assertEqual("sent", butler_relay.relay_question(self.state, self.job(), self.question, send=send))
        chat, text, key, reply_to, card, _ = send.call_args.args
        self.assertEqual((chat, key, reply_to), (CHAT, job_key("main-t1", 9) + "--q1", ""))
        self.assertIn("T410 等你", text)
        self.assertIn("选 A 还是 B？", text)
        saved = read_object(butler_relay.question_relay_path(self.state, job_key("main-t1", 9), "q1"))
        self.assertEqual((saved["state"], saved["outcome"]), ("sent", WAITING))
        self.assertEqual("sent", butler_relay.relay_question(self.state, self.job(), self.question, send=send))
        other = {"token": "q2", "questions": [{"question": "再问一个"}]}
        butler_relay.relay_question(self.state, self.job(), other, send=send)
        self.assertEqual(2, send.call_count)  # The same question once; another one once more.
        # The turn then ending waiting does not ask again.
        self.assertEqual("asked", relay(self.state, self.job(), WAITING, send=send, question="选 A 还是 B？"))
        self.assertEqual(2, send.call_count)

    def test_unconfirmed_is_never_sent_again(self):
        send = mock.Mock(side_effect=TimeoutError())
        self.assertEqual("uncertain", butler_relay.relay_question(self.state, self.job(), self.question, send=send))
        again = self.send()
        butler_relay.relay_question(self.state, self.job(), self.question, send=again)
        again.assert_not_called()

    def test_refused_goes_again_with_the_next_try_of_the_question(self):
        refused = mock.Mock(return_value=False)
        refused.outcome = lambda: relay_send.NOT_SENT
        self.assertEqual("failed", butler_relay.relay_question(self.state, self.job(), self.question, send=refused))
        send = self.send()
        self.assertEqual("sent", butler_relay.relay_question(self.state, self.job(), self.question, send=send))
        self.assertEqual(send.call_args.args[2], job_key("main-t1", 9) + "--q1")

    def test_not_for_a_turn_the_owner_started(self):
        send = self.send()
        self.assertIsNone(butler_relay.relay_question(self.state, self.job(message_id="om_own"), self.question, send=send))
        send.assert_not_called()


class QuestionTest(Case):
    def test_the_question_from_its_record_else_the_checkpoint(self):
        point = self.root / "checkpoint.json"
        write_json(point, {"requires_user": True, "question": "检查点里的问题？"}, ensure_ascii=False)
        job = self.job(checkpoint_path=str(point))
        self.assertEqual("检查点里的问题？", butler_relay._question(self.state, job))
        write_json(self.state / "questions" / "q1.json", {"job_id": 9, "topic_id": TOPIC, "chat_id": CHAT,
                   "status": "pending", "created_at": 1.0,
                   "questions": [{"id": "a", "header": "补充信息", "question": "选 A 还是 B？"}]}, ensure_ascii=False)
        self.assertEqual("选 A 还是 B？", butler_relay._question(self.state, job))


class SwitchOffTest(Case):
    switch = ""

    def test_nothing_is_sent(self):
        self.opened("t1", plan={"start": True}, topic_id=TOPIC)
        send = self.send()
        self.assertIsNone(relay(self.state, self.job(), DONE, send=send, text=REPLY))
        send.assert_not_called()


class DeliverTest(Case):
    """TurnDelivery.deliver calls the relay after the card's last words, with what came of the turn."""

    def setUp(self):
        super().setUp()
        self.opened("t1", plan={"start": True}, topic_id=TOPIC)

    def deliver(self, job, *, status="done", retry=None, sent=True):
        cards = mock.Mock()
        cards.render_stream_final = lambda job, text, **kwargs: text
        cards.exceeds_stream_card_limit = lambda text: False
        cards.body_cut.return_value = False
        cards.update_job_streaming_reply.return_value = True
        verified = SimpleNamespace(text=REPLY, fingerprint="f1") if status == "done" else None
        turn = SimpleNamespace(status=status, status_message="用户停止了任务" if status == "cancelled" else "任务完成",
                               final_text=REPLY, verified_delivery=verified,
                               verified_outcome=SimpleNamespace(status="completed", notice=lambda: "", error_code=""),
                               outcome=None, allow_resume=True)
        messenger = mock.Mock()
        messenger.send_message.side_effect = lambda *args, on_sent=None, **kwargs: (on_sent("om_relay"), True)[1]
        voice = SimpleNamespace(key=lambda tag, mid: f"bot-{tag}-{mid}")
        delivery = TurnDelivery(job, self.ctx, turn, prefix="p", cards=cards, runtime=mock.Mock(),
                                artifacts=mock.Mock(), questions=mock.Mock(), steering=mock.Mock(),
                                record_job_state=mock.Mock(), send_text=mock.Mock(return_value=True), retry=retry,
                                relay=lambda *args: butler_relay.after_turn(*args, messenger=messenger, profile="unit",
                                                                          voice=voice))
        with mock.patch.object(TurnDelivery, "texts", return_value=(REPLY, REPLY, False)), \
                mock.patch.object(TurnDelivery, "send", return_value=(sent, True)), \
                mock.patch("covey.app.turn_delivery.record_attempt"), \
                mock.patch("covey.app.turn_delivery.close_streaming_reply_async"):
            delivery.deliver(None)
        return messenger.send_message

    def test_a_done_turn_sends_its_conclusion(self):
        send = self.deliver(self.job(progress=SimpleNamespace()))
        send.assert_called_once()
        args, kwargs = send.call_args
        self.assertEqual(("unit", CHAT), args[:2])
        self.assertEqual(f"bot-main-relay-{job_key('main-t1', 9)}", args[3])
        self.assertEqual("", kwargs["reply_to_message_id"])  # Its own message at the top level, quoting no card.
        # Who speaks (docs/ui.md 5.3) is the row at the top: the topic by its T number, one sentence saying what it did with
        # the owner's words after it (样卡 B), not "T410 · 你问", which read as its own; no grey block above the card.
        self.assertNotIn("header", kwargs["card"])
        elements = kwargs["card"]["body"]["elements"]
        self.assertEqual("<font color='white'>**T410**</font>", badge_words(elements[0]))  # Its badge, no picture.
        row = row_line(elements[0])
        self.assertIn("做完了你", row)
        self.assertNotIn("· 你问", row)
        self.assertIn("<text_tag color='green'>有结果</text_tag>", row)
        self.assertNotIn("answer_ask", str(elements[1]))
        # REPLY is "## 结论" over one line: the line alone, no band and no heading saying it again (T638).
        self.assertEqual("两种做法都行，先用第一种。", elements[1]["content"])
        self.assertEqual(json.dumps(kwargs["card"]["body"], ensure_ascii=False).count("两种做法都行"), 1)
        # 输入 and 输出 are two small lines, no grey box (cards/answer_card.py facts_element); then where a reply goes.
        self.assertEqual(["answer_facts", "relay_hint"], [element["element_id"] for element in elements[-2:]])
        self.assertNotIn("background_style", json.dumps(elements[-2]))
        self.assertNotIn("](", elements[-1]["content"])  # Its T number is not a link: a quiet grey line.

    def test_a_done_turn_brings_its_longer_progress_notes_after_the_answer(self):
        """The owner, 2026-11-21 (T620): 13:51's "基线组已经补跑起来了……" stayed in 执行过程; the relay had the last words only."""
        from covey.core.trace import STAGE_HEADING, task_trace
        trace = task_trace()
        trace.note("n1", "我先看下文件。")
        trace.note("n2", "基线组已经补跑起来了。" + "细节" * 200)
        send = self.deliver(self.job(progress=SimpleNamespace(), tool_trace=trace))
        body = json.dumps(send.call_args.kwargs["card"]["body"], ensure_ascii=False)
        self.assertIn("基线组已经补跑起来了", body)
        self.assertIn("这一轮中途的汇报", body)  # In a closed fold under the answer, not open on the card.
        self.assertNotIn(STAGE_HEADING, body)
        self.assertIn('"expanded": false', body)
        self.assertNotIn("我先看下文件", body)
        self.assertLess(body.index("两种做法都行"), body.index("基线组已经补跑起来了"))  # The answer first.
        self.assertIn("两种做法都行，先用第一种", str(send.call_args.kwargs["card"]["body"]["elements"][1]))

    def test_a_turn_that_changed_code_has_its_change_as_a_line_under_input_and_output(self):
        point = self.state / "delivery-checkpoint.json"
        point.write_text(json.dumps({"delivery": {"commit": "a6bc148d38ce", "deployed": "第 322 代"}}, ensure_ascii=False))
        send = self.deliver(self.job(progress=SimpleNamespace(), checkpoint_path=str(point)))
        elements = send.call_args.kwargs["card"]["body"]["elements"]
        ids = [element.get("element_id") for element in elements]
        self.assertNotIn("answer_change", ids)  # No fold: the change is the third line of the facts.
        lines = elements[ids.index("answer_facts")]["content"].replace("\u2060", "").replace("\u00a0", " ").split("\n")
        for words in ("改动", "a6bc148", "第 322 代", "未自检"):
            self.assertIn(words, lines[-1])
        self.assertIn("输入", lines[0])
        relay_send.relay_path(self.state, job_key("main-t1", 9)).unlink()
        # A research turn's delivery without a commit, and no files changed: none.
        point.write_text(json.dumps({"delivery": {"verified": "报告已发群"}}, ensure_ascii=False))
        send = self.deliver(self.job(progress=SimpleNamespace(), checkpoint_path=str(point)))
        self.assertNotIn("改动", str(send.call_args.kwargs["card"]["body"]["elements"]))

    def test_a_done_turn_offers_its_switch_opening_at_the_top_level(self):
        """T669 E1 (skipped by T658): 换配置 on a 做完了 relay, a small bordered button in a row of its own under the
        输入/输出 lines; its value is the task card's with ``relay``, so its card opens at the top level
        (app/harness_switch.py). A turn with no Harness has none (test_a_done_turn_sends_its_conclusion). 独立复核, which it
        stood beside, was taken out (T937): a turn that changed code gets the same row."""
        job = self.job(progress=SimpleNamespace(), harness_id="codex", binding_revision=3)
        elements = self.deliver(job).call_args.kwargs["card"]["body"]["elements"]
        self.assertEqual(["answer_facts", "relay_switch_row", "relay_hint"], [e.get("element_id") for e in elements[-3:]])
        value = {"kind": "bridge_switch_open", "topic_id": TOPIC, "job_id": 9, "harness_id": "codex", "binding_revision": 3,
                 "relay": True}
        self.assertEqual(switches_of(elements[-2]), [("换配置", value)])
        self.assertNotIn("bridge_review_open", json.dumps(elements))

    def test_the_butlers_own_turn_and_a_turn_with_no_harness_offer_no_switch(self):
        self.assertEqual(butler_relay.switch_row(self.job(topic=BUTLER, harness_id="claude")), [])
        self.assertEqual(butler_relay.switch_row(self.job()), [])
        mine = butler_relay.switch_row(self.job(harness_id="codex"))
        self.assertEqual([row.get("element_id") for row in mine], ["relay_switch_row"])

    def test_a_stopped_turn_with_a_retry_planned_sends_nothing(self):
        send = self.deliver(self.job(progress=SimpleNamespace()), status="failed", retry=auto_retry.Planned(time.time() + 60, 1, auto_retry.RATE_LIMITED))
        send.assert_not_called()

    def test_a_stopped_turn_without_one_says_it_stopped(self):
        send = self.deliver(self.job(progress=SimpleNamespace()), status="cancelled")
        card = send.call_args.kwargs["card"]
        self.assertIn("<text_tag color='red'>停住了</text_tag>", row_line(card["body"]["elements"][0]))
        self.assertEqual("<font color='white'>**T410**</font>", badge_words(card["body"]["elements"][0]))
        # A stop no one named has no reason in core/failure.py: plain words, never the status line, and nothing to click.
        self.assertIn("这一轮停了", str(card["body"]["elements"][1]))
        self.assertNotIn("用户停止了任务", json.dumps(card, ensure_ascii=False))
        self.assertNotIn("relay_actions", json.dumps(card))

    def test_a_failed_turn_says_why_in_the_task_cards_words_with_its_buttons(self):
        """T650 (P0-4): the relay said the record's status line ("本轮执行未全部完成，原生会话不可续接", "任务异常（RuntimeError）")
        with no next step; it reads the record the turn's delivery wrote, as the task card does."""
        upsert_job_record(self.state, job_key("main-t1", 9), job_id=9, message_id="main-t1", chat_id=CHAT, topic_id=TOPIC,
                          status="failed", execution_status="failed", failure_code="root_failed",
                          status_message="任务异常（RuntimeError）")
        send = self.deliver(self.job(progress=SimpleNamespace()), status="failed")
        card = send.call_args.kwargs["card"]
        body = json.dumps(card, ensure_ascii=False)
        self.assertIn("这一轮没做完（主任务中途出错）", body)
        self.assertIn("可以在新会话里接着做，或先看原因", body)
        self.assertNotIn("RuntimeError", body)
        self.assertEqual([label for label, _, _ in labels_of(area_of(card))], ["检查并继续", "看原因"])

    def test_an_undelivered_result_sends_nothing(self):
        send = self.deliver(self.job(progress=SimpleNamespace()), sent=False)
        send.assert_not_called()

    def test_a_failing_relay_never_fails_the_delivery(self):
        with mock.patch.object(butler_relay, "relay", side_effect=RuntimeError("boom")):
            self.deliver(self.job(progress=SimpleNamespace()))


    def test_an_answer_part_that_raises_is_left_out_and_the_relay_goes(self):
        from covey.app import answer_cards
        for name in ("inputs", "outputs"):
            relay_send.relay_path(self.state, job_key("main-t1", 9)).unlink(missing_ok=True)
            with mock.patch.object(answer_cards, name, side_effect=AttributeError("x")):
                send = self.deliver(self.job(progress=SimpleNamespace()))
            send.assert_called_once()
            self.assertIn("两种做法都行", json.dumps(send.call_args.kwargs["card"]["body"], ensure_ascii=False))
            saved = read_object(relay_send.relay_path(self.state, job_key("main-t1", 9)))
            self.assertEqual("sent", saved["state"])

    def test_a_failure_before_the_record_leaves_it_unsent(self):
        with mock.patch.object(butler_relay, "relay_target", side_effect=AttributeError("x")):
            send = self.deliver(self.job(progress=SimpleNamespace()))
        send.assert_not_called()
        saved = read_object(relay_send.relay_path(self.state, job_key("main-t1", 9)))
        self.assertEqual(("unsent", "AttributeError", DONE), (saved["state"], saved["error"], saved["outcome"]))


class BoardTest(Case):
    def test_a_conclusion_sent_back_is_seen_by_its_own_read_only(self):
        """LA-10 (T419, 2026-11-08): a top-level read of any message 2 s before the relay went out was its seen. A
        relay sent as a message of its own is the topic's (butler_relay remember_sent): its read is the topic's."""
        t0 = 1793856000.0
        done = {"key": job_key("main-t1", 9), "topic_id": TOPIC, "job_id": 9, "status": "done",
                "card_message_id": "om_card", "timings": {"execution_finished": t0, "delivered": t0 + 1}}
        relayed = lambda record: butler_relay.relayed(self.state, record)  # noqa: E731
        inline = lambda record: butler_relay.inline_at(self.state, record)  # noqa: E731

        def seen(read_top, topic_seen=None, **kwargs):
            rows = pins.board([], [done], topic_seen or {}, {}, t0 + 60, relayed=relayed, inline=inline,
                              read_top=read_top, **kwargs)
            return rows[0]["seen"] if rows else "off"
        write_json(relay_send.relay_path(self.state, done["key"]),
                   {"state": "sent", "outcome": DONE, "message_id": "om_relay", "at": t0 + 5, "sent_at": t0 + 8})
        self.assertIsNone(seen(t0 + 3))  # Read at the top level before the relay went out: not seen.
        self.assertIsNone(seen(t0 + 30))  # Nor after: another message's read is not this one's.
        self.assertEqual(seen(t0 + 3, {TOPIC: t0 + 6}), t0 + 6)  # The relay (or the thread's answer card) read.
        self.assertIsNone(seen(t0 + 30, {TOPIC: t0 - 1}))  # Read before it ended.
        # Written on the butler's card by an older release (INLINE): no read receipt of its own, the top
        # level's once it was there.
        write_json(relay_send.relay_path(self.state, done["key"]),
                   {"state": butler_relay.INLINE, "outcome": DONE, "message_id": "", "at": t0 + 2, "sent_at": t0 + 5})
        self.assertEqual(seen(t0 + 6), t0 + 6)
        self.assertIsNone(seen(t0 + 4))
        # Ended before these rules: as before, a relayed one done is not on the board.
        self.assertEqual(seen(t0 + 30, keep_since=t0 + 1), "off")
        # A question still comes first, relayed or not.
        asked = {"topic_id": TOPIC, "created_at": t0 + 30, "question_message_id": "om_q"}
        self.assertEqual("waiting", pins.board([asked], [done], {}, {}, t0 + 60, relayed=relayed)[0]["group"])
        self.assertIsNone(butler_relay.inline_at(self.state, {"key": "none"}))


if __name__ == "__main__":
    unittest.main()


class AwaitedCase(Case):
    """A project's member topic (COVEY_PROJECTS=1): its turn relays only when its lead handed it on while the owner was there
    (relay_targets.awaited_target, docs/projects.md 主人在等的结果). T601, 2026-11-20: the lead told the owner "T601 出结果后，我把
    精简版和报告路径发给你", T601 ended, the lead's wake-up turn reported a ``thread`` note its style (milestones) held back,
    and the owner had to ask "写完了么？"."""

    PID = "a1b2c3d4e5f6"

    def setUp(self):
        super().setUp()
        from covey.core import projects
        mock.patch.dict("os.environ", {projects.ENV: "1"}).start()
        self.addCleanup(mock.patch.stopall)
        self.lead_topic = projects.lead_topic_id(CHAT, self.PID)
        self.topic(self.lead_topic, chat_id=CHAT, harness_id="claude", project=projects.project_field(self.PID, projects.LEAD))
        self.topic(TOPIC, topic_id=TOPIC, topic_number=410, chat_id=CHAT,
                   project=projects.project_field(self.PID, projects.MEMBER))

    def lead_turn(self, message_id):
        """The lead's turn ``message_id`` that ran project_topic_start: its card at the top level."""
        upsert_job_record(self.state, job_key(message_id, 7), job_id=7, message_id=message_id, chat_id=CHAT,
                          topic_id=self.lead_topic, card_message_id="om_lead_card", status="done",
                          user_text="详细说说两个分支的对比")
        self.opened("t1", plan={"start": True}, topic_id=TOPIC, job_key=job_key(message_id, 7))


class AwaitedTest(AwaitedCase):
    def test_a_turn_the_lead_handed_on_while_the_owner_was_there_relays_its_conclusion_and_its_stop(self):
        self.lead_turn("main-lead1")
        target = relay_targets.awaited_target(self.state, self.job())
        self.assertEqual(("om_lead_card", "T410", job_key("main-lead1", 7)),
                         (target["reply_to"], target["label"], target["main_key"]))
        send = self.send()
        self.assertEqual("sent", relay(self.state, self.job(), DONE, send=send, text=REPLY))
        self.assertTrue(butler_relay.relayed(self.state, {"message_id": "main-t1", "job_id": 9}))
        self.opened("t2", plan={"start": True}, topic_id=TOPIC, job_key=job_key("main-lead1", 7))
        self.assertEqual("sent", relay(self.state, self.job("main-t2", 10), STOPPED, send=send, reason="任务失败"))
        self.assertEqual(2, send.call_count)
        # Its question stays the question flow's (the lead may hold it): not relayed here.
        self.opened("t3", plan={"start": True}, topic_id=TOPIC, job_key=job_key("main-lead1", 7))
        self.assertIsNone(relay(self.state, self.job("main-t3", 11), WAITING, send=send, question="选哪个？"))
        self.assertEqual(2, send.call_count)

    def test_one_a_wake_up_turn_handed_on_is_the_leads_to_tell(self):
        self.lead_turn("lead-a1b2c3d4e5f6-3")
        self.assertIsNone(relay_targets.awaited_target(self.state, self.job()))
        send = self.send()
        self.assertIsNone(relay(self.state, self.job(), DONE, send=send, text=REPLY))
        send.assert_not_called()

    def test_a_turn_the_owner_started_in_the_member_topic_is_not(self):
        self.lead_turn("main-lead1")
        self.assertIsNone(relay_targets.awaited_target(self.state, self.job(message_id="om_typed_in_topic", job_id=12)))

    def test_with_projects_off_nothing(self):
        self.lead_turn("main-lead1")
        from covey.core import projects
        with mock.patch.dict("os.environ", {projects.ENV: "0"}):
            self.assertIsNone(relay_targets.awaited_target(self.state, self.job()))


OWNER_WORDS = "刚发的这个仓库能不能帮上产线？仓库评估的负责人来看吧"
HANDED_WORDS = "管家整理的说明：评估仓库对产线的用处"


class HandedLeadCase(AwaitedCase):
    """T812, 2026-11-26: the owner asked the butler at the top level, its project_send handed his words to the lead (a turn
    of message id "main-<token>", no card of its own and no Feishu message), whose project_topic_start opened T806; T806
    ended and nothing came back to the top level, as the one hop up from it (the lead's turn) named nothing to quote."""

    def setUp(self):
        super().setUp()
        # The butler's turn that took the owner's message, its card at the top level.
        upsert_job_record(self.state, job_key(OWNER, 5), card_message_id="om_butler_card", user_text=OWNER_WORDS)

    def handed_lead_turn(self, token="ps1", job_id=7, butler_key=None, chat=CHAT):
        """The lead's turn ``token`` project_send started (app/butler_projects.py): its record and the request's; its key."""
        mid = "main-" + token
        upsert_job_record(self.state, job_key(mid, job_id), job_id=job_id, message_id=mid, chat_id=chat,
                          topic_id=self.lead_topic, card_message_id="", reply_to_message_id="", status="done",
                          user_text=HANDED_WORDS)
        self.opened(token, job_key=job_key(OWNER, 5) if butler_key is None else butler_key, lead_topic_id=self.lead_topic,
                    project_id=self.PID, answer={"ok": True, "data": {"project_id": self.PID, "steered": False}})
        return job_key(mid, job_id)

    def member_turn(self, lead_key, token="t1"):
        """The member's turn ``token`` the lead's turn ``lead_key`` opened (project_topic_start)."""
        self.opened(token, plan={"start": True}, topic_id=TOPIC, job_key=lead_key)


class HandedLeadTest(HandedLeadCase):
    def test_a_turn_handed_on_in_a_lead_turn_the_butler_handed_on_relays_quoting_the_butlers_card(self):
        lead = self.handed_lead_turn()
        self.member_turn(lead)
        target = relay_targets.awaited_target(self.state, self.job())
        self.assertEqual(("om_butler_card", "T410", "start", "t1", TOPIC, lead),
                         tuple(target[k] for k in ("reply_to", "label", "kind", "token", "topic_id", "main_key")))
        # The lead's turn answers no message of the owner's itself: the topics it opens hang under none.
        self.assertEqual("", relay_targets.butler_job_owner_message(self.state, lead))
        send = self.send()
        self.assertEqual("sent", relay(self.state, self.job(), DONE, send=send, text=REPLY))
        card = json.dumps(send.call_args.args[4], ensure_ascii=False)
        self.assertIn(OWNER_WORDS[:12], card)  # "你问" is what he said to the butler,
        self.assertNotIn("管家整理的说明", card)  # not what the butler handed the lead.
        # Its stop relays too; a second turn of it once.
        self.member_turn(lead, "t2")
        self.assertEqual("sent", relay(self.state, self.job("main-t2", 10), STOPPED, send=send, reason="任务失败"))
        self.assertEqual("sent", relay(self.state, self.job("main-t2", 10), STOPPED, send=send, reason="任务失败"))
        self.assertEqual(2, send.call_count)

    def test_without_the_butlers_card_it_quotes_the_owners_message(self):
        upsert_job_record(self.state, job_key(OWNER, 5), card_message_id="")
        self.member_turn(self.handed_lead_turn())
        self.assertEqual(OWNER, relay_targets.awaited_target(self.state, self.job())["reply_to"])

    def test_an_automatic_retry_of_the_leads_turn_follows_the_turn_it_continues(self):
        lead = self.handed_lead_turn()
        upsert_job_record(self.state, lead, auto_retry_state="queued")
        # Its message id as app/auto_retry.py gives it (task_controls retry_topic), no card (a lead's never has one).
        upsert_job_record(self.state, job_key("auto-retry-7-1", 12), job_id=12, message_id="auto-retry-7-1", chat_id=CHAT,
                          topic_id=self.lead_topic, status="done", retry_of=lead)
        self.member_turn(job_key("auto-retry-7-1", 12))
        self.assertEqual("om_butler_card", relay_targets.awaited_target(self.state, self.job())["reply_to"])

    def test_the_ask_block_is_of_the_turn_the_owner_spoke_in(self):
        lead = self.handed_lead_turn()
        self.member_turn(lead)
        target = relay_targets.awaited_target(self.state, self.job())
        with mock.patch.object(relay_targets.answer_cards, "asked", return_value={"tag": "ask"}) as asked:
            self.assertEqual({"tag": "ask"}, relay_targets.ask_for(self.state, self.job(), target, at="om_card"))
        record = asked.call_args.args[1]
        self.assertEqual((5, OWNER_WORDS), (record["job_id"], record["user_text"]))
        self.assertEqual((TOPIC, "om_card"), (asked.call_args.kwargs["topic_id"], asked.call_args.kwargs["at"][0]))
        # A lead's turn the owner started himself (a reply to the project's card, its message id his): its own words.
        upsert_job_record(self.state, job_key("om_to_card", 8), job_id=8, message_id="om_to_card", chat_id=CHAT,
                          topic_id=self.lead_topic, status="done", user_text="直接对负责人说的")
        with mock.patch.object(relay_targets.answer_cards, "asked", return_value={"tag": "ask"}) as asked:
            relay_targets.ask_for(self.state, self.job(), {**target, "main_key": job_key("om_to_card", 8)})
        self.assertEqual(8, asked.call_args.args[1]["job_id"])
        # An ordinary topic the butler opened: its own turn's words, as before.
        with mock.patch.object(relay_targets.answer_cards, "asked", return_value={"tag": "ask"}) as asked:
            relay_targets.ask_for(self.state, self.job(), {**target, "main_key": job_key(OWNER, 5)})
        self.assertEqual(5, asked.call_args.args[1]["job_id"])

    def test_a_wake_up_turn_still_hands_on_nothing_to_relay(self):
        wake = f"lead-{self.PID}-3"
        upsert_job_record(self.state, job_key(wake, 7), job_id=7, message_id=wake, chat_id=CHAT, topic_id=self.lead_topic,
                          card_message_id="", status="done")
        self.member_turn(job_key(wake, 7))
        self.assertIsNone(relay_targets.awaited_target(self.state, self.job()))
        self.assertEqual("", relay_targets.relay_quote(self.state, job_key(wake, 7)))
        send = self.send()
        self.assertIsNone(relay(self.state, self.job(), DONE, send=send, text=REPLY))
        send.assert_not_called()

    def test_a_loop_of_records_ends_with_nothing_to_quote(self):
        first = self.handed_lead_turn("ps1", 7, butler_key=job_key("main-ps2", 8))
        self.handed_lead_turn("ps2", 8, butler_key=first)
        self.member_turn(first)
        self.assertEqual("", relay_targets.relay_quote(self.state, first))
        self.assertIsNone(relay_targets.awaited_target(self.state, self.job()))
        self.assertEqual(8, relay_targets._asker(self.state, first)["job_id"])  # The loop ends at the last one read.

    def test_a_chain_longer_than_its_hops_is_not_followed_to_its_end(self):
        hops = relay_targets.HAND_HOPS
        above = job_key(OWNER, 5)
        for n in range(hops - 1, 0, -1):  # Jobs 1 .. HAND_HOPS - 1, each handed on by the one above it.
            above = self.handed_lead_turn(f"h{n}", 100 + n, butler_key=above)
        self.assertEqual("om_butler_card", relay_targets.relay_quote(self.state, above))  # HAND_HOPS jobs: within.
        longer = self.handed_lead_turn("h0", 100, butler_key=above)
        self.assertEqual("", relay_targets.relay_quote(self.state, longer))  # One more: not followed.

    def test_a_record_of_another_chat_up_the_chain_is_not_quoted(self):
        upsert_job_record(self.state, job_key(OWNER, 5), chat_id="oc_elsewhere")
        lead = self.handed_lead_turn()
        self.member_turn(lead)
        self.assertEqual("", relay_targets.relay_quote(self.state, lead))
        self.assertIsNone(relay_targets.awaited_target(self.state, self.job()))

    def test_a_topic_moved_out_of_the_project_relays_through_the_same_chain(self):
        # Its turns relay as an ordinary topic's (relay_target), quoting what the turn the owner spoke in has.
        self.topic(TOPIC, topic_id=TOPIC, topic_number=410, chat_id=CHAT)
        self.member_turn(self.handed_lead_turn())
        target = relay_target(self.state, self.job())
        self.assertEqual(("om_butler_card", "start"), (target["reply_to"], target["kind"]))
        self.assertIsNone(relay_targets.awaited_target(self.state, self.job()))  # No member any more.

    def test_the_leads_receipt_says_it_was_sent_so_a_wake_up_that_retells_it_stays_quiet(self):
        from covey.app import project_lead, project_loop
        from covey.core import projects

        projects.create(self.state, projects.new_record(chat_id=CHAT, name="仓库评估", goal="评估", project_id=self.PID,
                                                        now=time.time()))
        projects.add_member(self.state, self.PID, TOPIC)
        self.member_turn(self.handed_lead_turn())
        upsert_job_record(self.state, job_key("main-t1", 9), job_id=9, message_id="main-t1", chat_id=CHAT, topic_id=TOPIC,
                          status="done", final_text="评估完了：有帮助")
        self.assertEqual("sent", relay(self.state, self.job(), DONE, send=self.send(), text=REPLY))
        logs = []
        project_loop.after_runner(logs.append, self.state, self.job())
        [item] = project_loop.unread(self.state, self.PID)
        self.assertEqual(("thread_end", True), (item["kind"], item.get("relayed")))
        self.assertIn(project_lead.RELAYED_NOTE, project_lead.receipt_line(item))
        wake = f"lead-{self.PID}-1"
        project_lead.take_receipts(self.state, self.PID, wake)
        note = {"id": "n1", "at": time.time(), "text": "T410 出结果了", "report": True, "kind": "thread", "job": wake}
        projects.update(self.state, self.PID, lambda record: record.update(style={"report": "each_thread"}, notes=[note]))
        self.assertTrue(project_loop.told_already(self.state, self.PID, wake))
        self.assertFalse(project_loop.reported(self.state, self.lead_topic, wake))


class HandedLeadDeliverTest(HandedLeadCase):
    """TurnDelivery.deliver relays such a turn from its task runner."""

    deliver = DeliverTest.deliver

    def test_a_done_member_turn_sends_its_conclusion(self):
        self.member_turn(self.handed_lead_turn())
        send = self.deliver(self.job(progress=SimpleNamespace()))
        send.assert_called_once()
        self.assertIn("两种做法都行", json.dumps(send.call_args.kwargs["card"]["body"], ensure_ascii=False))
        self.assertEqual("sent", read_object(relay_send.relay_path(self.state, job_key("main-t1", 9))).get("state"))


ASKED_AGAIN = "上一轮说的两个数差这么多，原因能再讲讲吗？"


class AskedOutsideCase(AwaitedCase):
    """T812, 2026-11-26: the owner quoted the relay of a member topic's turn at the top level and asked again (引用直达);
    the turn it started (#7397, its ``top_message`` his message) answered in the topic's thread, his message got DONE,
    and the top level heard nothing: the member's row relays nothing of its own, and no lead handed that turn on."""

    def quoted(self, message_id="om_quoted", job_id=12, **fields):
        """The turn the owner's message ``message_id``, quoting a card of the topic at the top level, started."""
        upsert_job_record(self.state, job_key(message_id, job_id), job_id=job_id, message_id=message_id, chat_id=CHAT,
                          topic_id=TOPIC, status="done", top_message=message_id, user_text=ASKED_AGAIN,
                          **fields)
        return self.job(message_id, job_id, top_message=message_id, user_text=ASKED_AGAIN)

    def steered(self, into, message_id="om_quoted2", job_id=15, status="read", **delivery):
        """The owner's message ``message_id`` quoting a card at the top level, gone into the running task ``into`` as a
        supplement (app/steering.py steer_queued_job): its own turn cancelled, merged into it; its delivery ``status``."""
        from covey.core.deliveries import QUEUED_STEER, delivery_path, save_delivery

        upsert_job_record(self.state, job_key(message_id, job_id), job_id=job_id, message_id=message_id, chat_id=CHAT,
                          topic_id=TOPIC, status="cancelled", merged_into_job_id=into, top_message=message_id,
                          user_text="顺便问下现在到哪一步了")
        save_delivery(delivery_path(self.state, TOPIC, QUEUED_STEER + message_id),
                      {"status": status, "topic_id": TOPIC, "chat_id": CHAT, "job_id": into,
                       "requeue": {"text": "顺便问下现在到哪一步了", "top": message_id}, **delivery})
        return job_key(message_id, job_id)


class AskedOutsideTest(AskedOutsideCase):
    def test_a_turn_he_asked_quoting_its_card_at_the_top_level_relays_its_conclusion_and_its_stop(self):
        job = self.quoted()
        self.assertEqual({"chat_id": CHAT, "reply_to": "om_quoted", "label": "T410", "token": "", "kind": "follow",
                          "topic_id": TOPIC, "main_key": ""}, relay_targets.awaited_target(self.state, job))
        send = self.send()
        self.assertEqual("sent", relay(self.state, job, DONE, send=send, text=REPLY))
        self.assertIn(ASKED_AGAIN[:10], json.dumps(send.call_args.args[4], ensure_ascii=False))  # "你问": his words.
        again = self.quoted("om_quoted_again", 13)
        self.assertEqual("sent", relay(self.state, again, STOPPED, send=send, reason="任务失败"))
        self.assertEqual(2, send.call_count)
        # Its question stays the question flow's.
        self.assertIsNone(relay(self.state, self.quoted("om_quoted_3", 14), WAITING, send=send, question="选哪个？"))
        self.assertEqual(2, send.call_count)

    def test_one_he_started_in_the_topics_own_thread_is_answered_there(self):
        upsert_job_record(self.state, job_key("om_in_thread", 13), job_id=13, message_id="om_in_thread", chat_id=CHAT,
                          topic_id=TOPIC, status="done")
        job = self.job("om_in_thread", 13)
        self.assertIsNone(relay_targets.awaited_target(self.state, job))
        send = self.send()
        self.assertIsNone(relay(self.state, job, DONE, send=send, text=REPLY))
        send.assert_not_called()

    def test_with_projects_off_or_in_an_ordinary_topic_this_is_not_the_way(self):
        from covey.core import projects

        job = self.quoted()
        with mock.patch.dict("os.environ", {projects.ENV: "0"}):
            self.assertIsNone(relay_targets.awaited_target(self.state, job))
        self.topic(TOPIC, topic_id=TOPIC, topic_number=410, chat_id=CHAT)  # No member: relay_target's, as before.
        self.assertIsNone(relay_targets.awaited_target(self.state, job))

    def test_an_automatic_retry_of_it_relays_for_the_turn_it_continues(self):
        self.quoted(auto_retry_state="queued")
        upsert_job_record(self.state, job_key("auto-retry-12-1", 16), job_id=16, message_id="auto-retry-12-1", chat_id=CHAT,
                          topic_id=TOPIC, status="done", retry_of=job_key("om_quoted", 12))
        target = relay_targets.awaited_target(self.state, self.job("auto-retry-12-1", 16))
        self.assertEqual(("om_quoted", "follow"), (target["reply_to"], target["kind"]))
        # A retry that is no automatic one, without a top_message of its own, is not followed back (one the owner clicks
        # keeps the top_message, app/task_controls.py requeue).
        upsert_job_record(self.state, job_key("om_quoted", 12), auto_retry_state="cancelled")
        self.assertIsNone(relay_targets.awaited_target(self.state, self.job("auto-retry-12-1", 16)))

    def test_a_quote_that_went_into_the_running_turn_as_a_supplement_relays_that_turn(self):
        upsert_job_record(self.state, job_key("om_in_thread", 13), job_id=13, message_id="om_in_thread", chat_id=CHAT,
                          topic_id=TOPIC, status="done")
        running = self.job("om_in_thread", 13)
        key = self.steered(13)
        target = relay_targets.awaited_target(self.state, running)
        self.assertEqual(("om_quoted2", "steer", key), (target["reply_to"], target["kind"], target["main_key"]))
        with mock.patch.object(relay_targets.answer_cards, "asked", return_value={"tag": "ask"}) as asked:
            relay_targets.ask_for(self.state, running, target)
        self.assertEqual(15, asked.call_args.args[1]["job_id"])  # "你问": the supplement he sent.
        send = self.send()
        self.assertEqual("sent", relay(self.state, running, DONE, send=send, text=REPLY))
        self.assertIn("顺便问下现在到哪一步了", json.dumps(send.call_args.args[4], ensure_ascii=False))

    def test_a_supplement_the_task_did_not_take_or_one_in_the_thread_is_not(self):
        upsert_job_record(self.state, job_key("om_in_thread", 13), job_id=13, message_id="om_in_thread", chat_id=CHAT,
                          topic_id=TOPIC, status="done")
        running = self.job("om_in_thread", 13)
        # Sent and never read on a Harness that reports reads, ended unread, held, refused: it becomes (or stays) a
        # turn of its own, with its own top_message, which relays itself.
        for n, (status, extra) in enumerate((("sent", {"reads": True}), ("sent", {"target_ended": True}), ("held", {}),
                                             ("refused", {}), ("uncertain", {}))):
            self.steered(13, f"om_q{n}", 20 + n, status, **extra)
        self.assertIsNone(relay_targets.awaited_target(self.state, running))
        # Into another task, or a supplement from the thread (no top_message).
        self.steered(99, "om_other", 30)
        upsert_job_record(self.state, job_key("om_thread_steer", 31), job_id=31, message_id="om_thread_steer", chat_id=CHAT,
                          topic_id=TOPIC, status="cancelled", merged_into_job_id=13)
        self.assertIsNone(relay_targets.awaited_target(self.state, running))
        # Sent on a Harness that reports no reads: taken.
        self.steered(13, "om_codex", 32, "sent")
        self.assertEqual("om_codex", relay_targets.awaited_target(self.state, running)["reply_to"])

    def test_a_turn_the_lead_handed_on_still_relays_as_it_did(self):
        self.lead_turn("main-lead1")
        target = relay_targets.awaited_target(self.state, self.job(top_message="om_quoted"))
        self.assertEqual(("start", "om_lead_card"), (target["kind"], target["reply_to"]))

    def test_the_leads_receipt_says_it_was_sent_so_a_wake_up_that_retells_it_stays_quiet(self):
        from covey.app import project_lead, project_loop
        from covey.core import projects

        projects.create(self.state, projects.new_record(chat_id=CHAT, name="仓库评估", goal="评估", project_id=self.PID,
                                                        now=time.time()))
        projects.add_member(self.state, self.PID, TOPIC)
        job = self.quoted(final_text="两个数差在统计口径上")
        self.assertEqual("sent", relay(self.state, job, DONE, send=self.send(), text=REPLY))
        project_loop.after_runner([].append, self.state, job)
        [item] = project_loop.unread(self.state, self.PID)
        self.assertEqual(("thread_end", True), (item["kind"], item.get("relayed")))
        self.assertIn(project_lead.RELAYED_NOTE, project_lead.receipt_line(item))
        wake = f"lead-{self.PID}-1"
        project_lead.take_receipts(self.state, self.PID, wake)
        note = {"id": "n1", "at": time.time(), "text": "T410 答了", "report": True, "kind": "thread", "job": wake}
        projects.update(self.state, self.PID, lambda record: record.update(style={"report": "each_thread"}, notes=[note]))
        self.assertTrue(project_loop.told_already(self.state, self.PID, wake))
        self.assertFalse(project_loop.reported(self.state, self.lead_topic, wake))


class AskedOutsideDeliverTest(AskedOutsideCase):
    """TurnDelivery.deliver relays such a turn from its task runner."""

    deliver = DeliverTest.deliver

    def test_a_done_turn_he_asked_at_the_top_level_sends_its_conclusion(self):
        job = self.quoted()
        job.progress = SimpleNamespace()
        send = self.deliver(job)
        send.assert_called_once()
        self.assertIn("两种做法都行", json.dumps(send.call_args.kwargs["card"]["body"], ensure_ascii=False))

    def test_one_in_the_thread_sends_nothing(self):
        self.deliver(self.job("om_in_thread", 13, progress=SimpleNamespace())).assert_not_called()


class AwaitedDeliverTest(AwaitedCase):
    """TurnDelivery.deliver relays a member's awaited turn as it does a butler topic's."""

    deliver = DeliverTest.deliver

    def test_a_done_member_turn_the_owner_waits_for_sends_its_conclusion(self):
        self.lead_turn("main-lead1")
        send = self.deliver(self.job(progress=SimpleNamespace()))
        send.assert_called_once()
        self.assertIn("两种做法都行", json.dumps(send.call_args.kwargs["card"]["body"], ensure_ascii=False))

    def test_one_a_wake_up_turn_handed_on_sends_nothing(self):
        self.lead_turn("lead-a1b2c3d4e5f6-3")
        self.deliver(self.job(progress=SimpleNamespace())).assert_not_called()


def area_of(card):
    """The element under a stopped relay's reason (app/relay_stops.py AREA_ID), None without one."""
    from covey.cards.elements import find_element
    return find_element(card, relay_stops.AREA_ID)


def labels_of(element):
    """The labels and values of the buttons in ``element``."""
    found = []

    def walk(value):
        if isinstance(value, dict):
            if value.get("tag") == "button":
                found.append((value["text"]["content"], value.get("type"), value.get("value")))
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(element)
    return found


def switches_of(element):
    """The labels and callback values of the buttons in ``element`` that carry them as ``behaviors`` (cards/elements.py
    topic_button with callback), or else as ``value``."""
    found = []

    def walk(value):
        if isinstance(value, dict):
            if value.get("tag") == "button":
                behaviors = [b.get("value") for b in value.get("behaviors") or () if b.get("type") == "callback"]
                found.append((value["text"]["content"], behaviors[0] if behaviors else value.get("value")))
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(element)
    return found


class StopButtonsTest(Case):
    """停住了 at the top level (app/relay_stops.py, T650): why and what next in core/failure.py's words, never the status line;
    the task card's 检查并继续 and 看原因 under them, each its own kind marked as the relay's; a click redraws the card in place
    from the records, so a second click, a topic that moved on or a stale card starts nothing."""

    KEY = job_key("main-t1", 9)

    def setUp(self):
        super().setUp()
        self.opened("t1", plan={"start": True}, topic_id=TOPIC)

    def record(self, job_id=9, message_id="main-t1", **fields):
        fields = {"status": "failed", "execution_status": "failed", **fields}
        return upsert_job_record(self.state, job_key(message_id, job_id), job_id=job_id, message_id=message_id,
                                 chat_id=CHAT, topic_id=TOPIC, **fields)

    def relayed(self, **fields):
        """The stop relayed for a record with ``fields`` alone (any record and relay of the job before are gone): (its card, its
        relay record)."""
        job_file(self.state, self.KEY).unlink(missing_ok=True)
        relay_send.relay_path(self.state, self.KEY).unlink(missing_ok=True)
        self.record(**fields)
        send = self.send()
        self.assertEqual("sent", relay(self.state, self.job(), STOPPED, send=send,
                                       **relay_stops.facts(self.state, self.job())))
        return send.call_args.args[4], read_object(relay_send.relay_path(self.state, self.KEY))

    def part(self, patched=True):
        """The TaskControls or Submission that took the click: the topic's records, a messenger whose card writes ``patched``."""
        messenger = mock.Mock()
        messenger.patch_card.return_value = patched
        return SimpleNamespace(harness=SimpleNamespace(topic_records=lambda ctx, chat: topic_job_records(self.state, TOPIC, chat_id=chat)),
                               messenger=messenger, voice=SimpleNamespace(key=lambda tag, mid: f"bot-{tag}-{mid}"),
                               runtime=SimpleNamespace(log=self.fail))

    def click(self, part, kind, run):
        event = {"chat_id": CHAT, "event_id": "ev-1", "message_id": "om_relay"}
        raw = {"kind": kind, "topic_id": TOPIC, "job_id": 9, relay_stops.RELAY: True}
        relay_stops.clicked(part, self.ctx, event, raw, run)
        return area_of(part.messenger.patch_card.call_args.args[2]) if part.messenger.patch_card.called else None

    def test_the_card_says_why_and_what_next_with_the_task_cards_buttons(self):
        card, saved = self.relayed(failure_code="root_failed", status_message="本轮执行未全部完成，原生会话不可续接")
        body = json.dumps(card, ensure_ascii=False)
        self.assertIn("这一轮没做完（主任务中途出错），已做的部分保留着", str(card["body"]["elements"][1]))
        self.assertIn("可以在新会话里接着做，或先看原因", str(card["body"]["elements"][1]))  # The next step, the band's second line.
        self.assertNotIn("原生会话", body)
        self.assertNotIn("错误码", body)
        self.assertEqual(labels_of(area_of(card)), [
            ("检查并继续", "primary", {"kind": "bridge_job_retry", "topic_id": TOPIC, "job_id": 9, "relay": True}),
            ("看原因", "default", {"kind": "bridge_job_diagnose", "topic_id": TOPIC, "job_id": 9, "relay": True})])
        self.assertTrue(card["config"]["update_multi"])  # Redrawn in place once clicked.
        self.assertEqual(saved["card"], card)  # Kept for that.

    def test_each_stop_offers_what_its_task_card_offers(self):
        """T669: 换个模型 too (T650 left it out, its card opening in the topic's thread): for a record with a binding, the 换配置
        card's own click with ``relay``, opening at the top level."""
        route = {"harness_id": "codex", "binding_revision": 2}
        cases = {
            "timeout": ({"failure_code": "timeout"}, ["检查并继续", "拆小再试"]),
            "limit": ({"failure_code": "rate_limited", **route}, ["现在重试", "换个模型"]),
            "quota": ({"failure_code": "quota_exhausted", **route}, ["换个模型"]),  # The one next step, the primary.
            "policy": ({"status": "cancelled", "policy_stop": {"kind": "refusal", "category": "cyber", "scope": "main"},
                        **route}, ["拆小再试", "换个模型"]),
            "auth": ({"failure_code": "authentication_error", **route}, ["看原因"]),
            # A refusal whose retry on an older model is scheduled: nothing to click beside it.
            "policy_retry": ({"status": "cancelled", "policy_stop": {"kind": "refusal", "category": "cyber", "scope": "main"},
                              "policy_retry_state": "pending", "policy_retry_model": "claude-opus-4-8", **route}, []),
            # A record with no binding (one from before it, a butler's) cannot open the 换配置 card: as the task card.
            "limit_unbound": ({"failure_code": "rate_limited"}, ["现在重试"]),
            "quota_unbound": ({"failure_code": "quota_exhausted"}, []),
        }
        for name, (fields, labels) in cases.items():
            with self.subTest(name):
                card, _ = self.relayed(**fields)
                self.assertEqual([label for label, _, _ in labels_of(area_of(card))], labels)
                if labels:
                    self.assertEqual([tone for _, tone, _ in labels_of(area_of(card))],
                                     ["primary"] + ["default"] * (len(labels) - 1))  # One a block (docs/ui.md 第 6 节).
                else:
                    self.assertIsNone(area_of(card))
        # Without a button its band's line still says the next step.
        card, _ = self.relayed(failure_code="quota_exhausted")
        self.assertIn("可以换个模型接着做", json.dumps(card["body"]["elements"][1], ensure_ascii=False))

    def test_its_switch_opens_at_the_top_level_preferring_another_harness(self):
        card, _ = self.relayed(failure_code="quota_exhausted", harness_id="codex", binding_revision=2)
        self.assertEqual(labels_of(area_of(card)), [("换个模型", "primary", {
            "kind": "bridge_switch_open", "topic_id": TOPIC, "job_id": 9, "harness_id": "codex", "binding_revision": 2,
            "relay": True, "prefer_other": True})])

    def test_a_switch_from_it_redraws_it_as_one_line_a_reply_goes_on_with(self):
        """Once the owner confirmed the 换配置 its 换个模型 opened (app/harness_switch.py finish): the buttons become one line, the
        new model named; a reply to the card goes to the topic (app/relay_send.py remember_sent), on the new model."""
        from covey.cards.elements import find_element
        from covey.cards.relay_card import NOTE_ID
        self.relayed(failure_code="quota_exhausted", harness_id="codex", binding_revision=1)
        part = self.part()
        form = {"topic_id": TOPIC, "chat_id": CHAT, "job_id": 9, "top_level": True}
        self.assertFalse(relay_stops.switched(self.state, part.messenger, "unit", form, "Claude · Opus 5.5"))  # Not switched.
        part.messenger.patch_card.assert_not_called()
        self.assertNotIn("clicked", read_object(relay_send.relay_path(self.state, self.KEY)))
        self.topic(TOPIC, topic_id=TOPIC, topic_number=410, binding_revision=2, switches=[{"revision": 2, "at": 1.0}])
        self.assertTrue(relay_stops.switched(self.state, part.messenger, "unit", form, "Claude · Opus 5.5"))
        built = part.messenger.patch_card.call_args.args[2]
        self.assertEqual(part.messenger.patch_card.call_args.args[:2], ("unit", "om_relay"))
        self.assertEqual(area_of(built)["content"],
                         "<font color='grey'>↪ 已换配置：Claude · Opus 5.5；回复这张卡说一句，就按新配置接着做</font>")
        self.assertEqual(find_element(built, NOTE_ID)["content"], "<font color='grey'>详情在 T410</font>")
        saved = read_object(relay_send.relay_path(self.state, self.KEY))
        self.assertEqual((saved["clicked"]["action"], saved["clicked"]["to"]), ("switch_model", "Claude · Opus 5.5"))
        self.assertEqual(area_of(saved["card"]), area_of(built))
        self.assertEqual(message_topic(self.state, CHAT, "om_relay"), TOPIC)  # A reply to it is the topic's.

    def test_only_a_stopped_relay_with_its_card_is_redrawn(self):
        part = self.part()
        self.assertFalse(relay_stops.redraw(self.state, part.messenger, "unit", TOPIC, CHAT, 9))  # No record.
        self.record(failure_code="quota_exhausted")
        self.assertFalse(relay_stops.redraw(self.state, part.messenger, "unit", TOPIC, CHAT, 9))  # Not relayed.
        write_json(relay_send.relay_path(self.state, self.KEY), {"state": "sent", "outcome": DONE, "message_id": "om_done"})
        self.assertFalse(relay_stops.redraw(self.state, part.messenger, "unit", TOPIC, CHAT, 9))  # A 做完了 keeps no card.
        part.messenger.patch_card.assert_not_called()
        self.assertFalse(relay_stops.switched(self.state, part.messenger, "unit", {"job_id": "x"}))  # Never raises.

    def test_a_retry_click_redraws_the_card_with_the_task_that_went_on_and_never_retries_twice(self):
        self.relayed(failure_code="root_failed")
        part, runs = self.part(), []

        def retry():
            runs.append(1)
            upsert_job_record(self.state, self.KEY, retried_by="ev-1")
            self.record(job_id=10, message_id="ev-1", status="queued", retry_of=self.KEY)
            return "本话题任务已重新入队，任务 #10。", None
        area = self.click(part, "bridge_job_retry", retry)
        self.assertEqual(area["content"], "<font color='grey'>↪ 已接着做 · 任务 #10</font>")
        self.assertEqual(part.messenger.patch_card.call_args.args[:2], ("unit", "om_relay"))
        part.messenger.reply.assert_not_called()  # Its card says it; nothing under the topic's thread.
        self.assertEqual(self.click(part, "bridge_job_retry", retry)["content"], area["content"])  # A second click.
        self.assertEqual(runs, [1])
        saved = read_object(relay_send.relay_path(self.state, self.KEY))
        self.assertEqual(area_of(saved["card"]), area)

    def test_a_follow_up_click_says_which_task_looks_into_it_and_a_second_one_starts_none(self):
        self.relayed(failure_code="root_failed")
        part, runs = self.part(), []

        def diagnose():
            runs.append(1)
            self.record(job_id=11, message_id="ev-2", status="queued")
            return "", 11
        area = self.click(part, "bridge_job_diagnose", diagnose)
        self.assertEqual(area["content"], "<font color='grey'>↪ 已开一轮只读排查 · 任务 #11</font>")
        self.assertEqual(read_object(relay_send.relay_path(self.state, self.KEY))["clicked"]["job_id"], 11)
        self.assertEqual(self.click(part, "bridge_job_diagnose", diagnose)["content"], area["content"])
        self.assertEqual(runs, [1])

    def test_a_click_refused_for_now_keeps_the_buttons_and_says_why_on_the_card(self):
        from covey.cards.elements import find_element
        from covey.cards.relay_card import NOTE_ID
        self.relayed(failure_code="root_failed")
        part = self.part()
        area = self.click(part, "bridge_job_retry", lambda: ("本话题还有运行或排队的任务，请完成或停止后再重试。", None))
        self.assertEqual([label for label, _, _ in labels_of(area)], ["检查并继续", "看原因"])  # Its task ends: click again.
        note = find_element(part.messenger.patch_card.call_args.args[2], NOTE_ID)["content"]
        self.assertEqual(note, "<font color='grey'>没有开始：本话题还有运行或排队的任务，请完成或停止后再重试 · 详情在 T410</font>")
        part.messenger.reply.assert_not_called()

        def retry():
            upsert_job_record(self.state, self.KEY, retried_by="ev-2")
            self.record(job_id=10, message_id="ev-2", status="queued", retry_of=self.KEY)
            return "本话题任务已重新入队，任务 #10。", None
        self.assertEqual(self.click(part, "bridge_job_retry", retry)["content"], "<font color='grey'>↪ 已接着做 · 任务 #10</font>")
        self.assertEqual(find_element(part.messenger.patch_card.call_args.args[2], NOTE_ID)["content"],
                         "<font color='grey'>详情在 T410</font>")

    def test_a_click_on_a_task_whose_record_is_gone_is_told_in_the_thread(self):
        self.relayed(failure_code="root_failed")
        part = self.part()
        part.harness.topic_records = lambda ctx, chat: []  # Archived by the daily maintenance.
        self.click(part, "bridge_job_retry", lambda: ("本话题没有可重试的失败任务。", None))
        self.assertEqual(part.messenger.reply.call_args.args[2], "本话题没有可重试的失败任务。")
        part.messenger.patch_card.assert_not_called()

    def test_a_topic_that_moved_on_runs_nothing_from_an_old_relay(self):
        self.relayed(failure_code="root_failed")
        self.record(job_id=12, message_id="om_next", status="done")  # The owner's next message in the topic.
        part, runs = self.part(), []
        area = self.click(part, "bridge_job_retry", lambda: runs.append(1) or ("x", None))
        self.assertEqual((runs, area["content"]), ([], f"<font color='grey'>{relay_stops.MOVED_ON}</font>"))

    def test_a_task_from_before_a_switch_offers_nothing_more(self):
        self.relayed(failure_code="root_failed", binding_revision=1)
        self.topic(TOPIC, topic_id=TOPIC, topic_number=410, binding_revision=2,
                   switches=[{"revision": 2, "at": 1.0}])
        record = find_job_record(self.state, self.KEY)
        self.assertTrue(relay_stops.stale(self.state, record))
        part = self.part()
        self.assertTrue(relay_stops.settle(self.state, part.messenger, "unit", self.KEY, record))
        self.assertEqual(area_of(part.messenger.patch_card.call_args.args[2])["content"],
                         f"<font color='grey'>{relay_stops.STALE}</font>")

    def test_a_retry_from_before_a_switch_is_refused_on_its_own_card(self):
        """app/card_routing.py job_route refuses 检查并继续 on a task from before the topic's latest 换配置: a stopped relay's
        click says so on the relay card, redrawn; a task card's still gets the reply in the topic's thread."""
        import dataclasses
        from covey.app.card_routing import CardRouting
        self.relayed(failure_code="root_failed", binding_revision=1)
        self.topic(TOPIC, topic_id=TOPIC, topic_number=410, binding_revision=2, switches=[{"revision": 2, "at": 1.0}])
        target = dataclasses.replace(self.ctx, topic_id=TOPIC, history_path=self.state / "topics" / TOPIC / "history.jsonl")
        for relayed, patched, replied in ((True, 1, 0), (False, 0, 1)):
            with self.subTest(relayed=relayed):
                app = SimpleNamespace(messenger=mock.Mock(), voice=None, key=lambda *parts: "-".join(parts))
                app.messenger.patch_card.return_value = True
                raw = {"kind": "bridge_job_retry", "topic_id": TOPIC, "job_id": 9, **({"relay": True} if relayed else {})}
                event = {"chat_id": CHAT, "event_id": "ev-1", "message_id": "om_relay", "action_value": raw}
                self.assertIsNone(CardRouting.job_route(SimpleNamespace(apps={}), app, target, event, raw))
                self.assertEqual((app.messenger.patch_card.call_count, app.messenger.reply.call_count), (patched, replied))
                if relayed:
                    self.assertEqual(area_of(app.messenger.patch_card.call_args.args[2])["content"],
                                     f"<font color='grey'>{relay_stops.STALE}</font>")

    def test_a_card_that_cannot_be_written_says_it_in_the_thread(self):
        self.relayed(failure_code="root_failed")
        part = self.part(patched=False)
        self.click(part, "bridge_job_retry", lambda: ("本话题还有运行或排队的任务，请完成或停止后再重试。", None))
        part.messenger.reply.assert_called_once()
        self.assertIn("本话题还有运行或排队的任务", part.messenger.reply.call_args.args[2])

    def test_the_task_cards_clicks_hand_a_relays_to_it(self):
        from covey.app.submission import Submission
        from covey.app.task_controls import TaskControls
        event = {"chat_id": CHAT, "event_id": "ev-9", "message_id": "om_relay",
                 "action_value": {"kind": "bridge_job_retry", "topic_id": TOPIC, "job_id": 9, "relay": True}}
        ctx = SimpleNamespace(topic_id=TOPIC)
        controls = SimpleNamespace(retry_topic=mock.Mock(return_value="已重新入队"), messenger=mock.Mock())
        with mock.patch("covey.app.task_controls.after_ack", lambda run: run()), \
                mock.patch.object(relay_stops, "clicked") as clicked:
            TaskControls.task_action(controls, ctx, event)
            self.assertEqual(clicked.call_args.args[4](), ("已重新入队", None))
            controls.messenger.reply.assert_not_called()
        submission = SimpleNamespace(follow_up_job=mock.Mock(return_value=("", 13)), messenger=mock.Mock())
        event["action_value"] = {**event["action_value"], "kind": "bridge_job_diagnose"}
        with mock.patch("covey.app.submission.after_ack", lambda run: run()), \
                mock.patch.object(relay_stops, "clicked") as clicked:
            Submission.follow_up(submission, ctx, event)
            self.assertEqual(clicked.call_args.args[4](), ("", 13))
        submission.follow_up_job.assert_called_once_with(ctx, CHAT, "ev-9", 9, "diagnose")
