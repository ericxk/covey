"""飞书私聊区卡片的头像行 (docs/ui.md 5.3, docs/cards.md): the card at the private chat's top level opens with a row, the speaker's
picture, its name bold and what it did with the owner's words grey (cards/ui.py speaker_row), no coloured title bar; the "你问"
block is gone from the body and its way back is on the 输出 line; 输入 and 输出 are two small lines, none of their parts
left out; a long relayed reply folds under its conclusion; a question's copy leads with the question; a draft waiting for
the owner is a small card with an orange 要你确认; the merged relay has a row and rows that open their topics. The avatars
(core/avatars.py, feishu/avatars.py): a lead's picture is stable and not shared, an upload that fails leaves the card
without its picture; a topic has no picture, its T number is its badge (T638)."""
from __future__ import annotations

import json
import os
from html import unescape
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from covey.cards.answer_card import ask_block, facts_element, figure, input_parts, tail, tail_card
from covey.cards.owner_send import BARS, owner_send_card, top_card
from covey.cards.question_cards import mirror_card, settled_element, settled_line, settled_peek
from covey.cards.relay_card import DONE, STOPPED, WAITING, foot, merged_card, relay_card
from covey.cards.review import answer_card
from covey.cards.ui import ASK_META, speaker_row, split_ask
from covey.core import avatars
from covey.feishu import avatars as uploads
from test_card_golden import question_record

URL = "https://applink.feishu.cn/client/chat/open?openChatId=oc_x&position=7"
TOPIC_URL = "https://applink.feishu.cn/client/thread/open?open_chat_id=oc_x&open_thread_id=omt_x"


def holders(node, found=None):
    """The dicts of ``node`` that carry ASK_META as a key, at any depth."""
    found = [] if found is None else found
    if isinstance(node, dict):
        if ASK_META in node:
            found.append(node)
        for value in node.values():
            holders(value, found)
    elif isinstance(node, list):
        for value in node:
            holders(value, found)
    return found


def unwrap(row):
    """A speaker's row without the block that makes it a link (cards/ui.py speaker_row with a link)."""
    return row["elements"][0] if row["tag"] == "interactive_container" and row.get("element_id") == "speaker_row" else row


def link_of(row):
    """What a click on the row opens; '' when it opens nothing."""
    return (row.get("behaviors") or [{}])[0].get("default_url", "") if row["tag"] == "interactive_container" else ""


def text_of(row):
    """The line of a speaker's row: the markdown of its second column, or the row itself without a picture."""
    row = unwrap(row)
    return row["columns"][1]["elements"][0]["content"] if row["tag"] == "column_set" else row["content"]


def tag(row):
    """(word, colour) of the row's first state tag, None without one."""
    import re

    found = re.search(r"<text_tag color='(\w+)'>(.*?)</text_tag>", text_of(row))
    return (found.group(2), found.group(1)) if found else None


def first(built):
    return built["body"]["elements"][0]


def badge_of_link(row):
    """What a click on the row's badge opens: the badge stops a click, so it carries the row's link."""
    return (unwrap(row)["columns"][0]["elements"][0].get("behaviors") or [{}])[0].get("default_url", "")


def badge_of(row):
    """The badge in a topic's row (cards/ui.py topic_badge): (its words, its colour's name); None without one."""
    row = unwrap(row)
    found = row["columns"][0]["elements"][0] if row["tag"] == "column_set" else None
    if not found or found["tag"] != "interactive_container":
        return None
    return found["elements"][0]["content"], found["background_style"]


class RowTest(unittest.TestCase):
    def meta(self, who, doing, **more):
        return split_ask([ask_block("看板改成彩色块，字大一点，颜色用暖色，再把图例挪到右边去", at="20:41", url=URL, who=who, doing=doing, **more)])[0]

    def test_the_butler_s_row_is_its_picture_its_name_and_the_first_words_of_the_message(self):
        row = speaker_row(self.meta("管家", "answer", avatar="img_butler"))
        self.assertEqual(URL, link_of(row))  # A click anywhere on it goes back to his message (the owner, 2026-11-22).
        self.assertEqual("column_set", unwrap(row)["tag"])
        picture, line = unwrap(row)["columns"]
        self.assertEqual(("auto", "img", "img_butler", "28px 28px", "crop_center"),
                         (picture["width"], picture["elements"][0]["tag"], picture["elements"][0]["img_key"],
                          picture["elements"][0]["size"], picture["elements"][0]["scale_type"]))
        content = text_of(row)
        self.assertTrue(content.startswith("**管家** <font color='grey'>回你 20:41「"))
        self.assertIn("「看板改成彩色块，字大一点", content)
        self.assertNotIn("图例", content)  # Only the first 18 characters, then "…".
        self.assertTrue(content.endswith("…」</font> <font color='grey'>›</font>"))  # What it opens: one grey ›.
        self.assertFalse(picture["elements"][0]["preview"])  # A click on the picture opens the row's link.
        self.assertIsNone(tag(row))  # The butler's answer has no state to tag.

    def test_a_lead_s_row_names_its_project_and_asks_with_an_orange_tag(self):
        row = speaker_row(self.meta("专项「发布」· 负责人", "answer", avatar="img_g1"))
        self.assertTrue(text_of(row).startswith("**发布 · 负责人** <font color='grey'>回你 20:41「"))
        self.assertIsNone(tag(row))
        self.assertTrue(text_of(speaker_row(self.meta("专项「发布」· 负责人", "done"))).startswith(
            "**发布 · 负责人** <font color='grey'>汇报你 20:41 交代的事"))
        self.assertEqual(("等你", "orange"), tag(speaker_row(self.meta("专项「发布」· 负责人", "waiting"))))

    def test_a_topic_s_row_has_its_t_number_as_its_badge_and_its_state_as_the_tag(self):
        for doing, expected in (("done", ("有结果", "green")), ("waiting", ("等你", "orange")), ("stopped", ("停住了", "red"))):
            row = speaker_row(self.meta("T623 工作台私聊密度", doing, avatar="img_topic"))
            self.assertEqual(expected, tag(row))
            # The badge in the picture's place, a picture given or not; the title alone after it.
            self.assertEqual(("<font color='white'>**T623**</font>", "topic-7"), badge_of(row))  # 623 × 5 % 12.
            self.assertTrue(text_of(row).startswith("**工作台私聊密度** <text_tag"))
        self.assertTrue(text_of(speaker_row(self.meta("T623 x", "done"))).endswith("做完了你 20:41 交代的事</font> <font color='grey'>›</font>"))
        self.assertEqual(URL, badge_of_link(speaker_row(self.meta("T623 x", "done"))))  # The badge opens it too.
        self.assertEqual("", link_of(speaker_row(self.meta("T623 x", "done"), url="")))  # In its own thread: no link.

    def test_the_badge_is_as_wide_as_its_number_and_its_colour_is_the_card_s_own(self):
        from covey.cards.elements import card
        from covey.cards.ui import banded, topic_badge
        from covey.core import look

        for label in ("T7", "T638", "T1234", "T12345"):
            badge = topic_badge(label)
            self.assertEqual(("auto", "28px", "notation"), (badge["width"], badge["height"],
                                                            badge["elements"][0]["text_size"]))
            self.assertEqual(f"<font color='white'>**{label}**</font>", badge["elements"][0]["content"])
        self.assertIsNone(topic_badge("话题"))
        # Neighbours never share a colour, and twelve in a row use all twelve.
        places = [look.topic_color(f"T{n}") for n in range(630, 642)]
        self.assertEqual(12, len(set(places)))
        self.assertTrue(all(a != b for a, b in zip(places, places[1:])))
        built = banded(card([speaker_row(self.meta("T638 卡片半遮面", "done"))], "s"))
        self.assertEqual({"topic-10": {"light_mode": "rgba(162,28,175,1)", "dark_mode": "rgba(162,28,175,1)"}},
                         built["config"]["style"]["color"])
        self.assertNotIn("config", banded(card([speaker_row(self.meta("管家", "answer"))], None)))  # No badge, no colour.

    def test_without_a_picture_the_row_is_its_line_alone_and_a_given_one_wins_over_the_block_s(self):
        row = speaker_row(self.meta("管家", "answer"))
        self.assertEqual("markdown", unwrap(row)["tag"])
        self.assertEqual("img_b", unwrap(speaker_row(self.meta("管家", "answer", avatar="img_a"), avatar="img_b"))["columns"][0]
                         ["elements"][0]["img_key"])
        label = speaker_row(None, outcome="stopped", label="T623")  # No block: the T number's badge and the state.
        self.assertEqual((("停住了", "red")), tag(label))
        self.assertEqual("<font color='white'>**T623**</font>", badge_of(label)[0])
        self.assertTrue(text_of(label).startswith("<text_tag"))

    def test_no_row_is_red_and_only_the_owner_s_turn_is_orange(self):
        for who in ("管家", "专项「发布」· 负责人", "T623 x"):
            for doing in ("answer", "done", "waiting", "stopped", "about", "member"):
                found = tag(speaker_row(self.meta(who, doing)))
                if found and found[1] == "orange":
                    self.assertEqual("等你", found[0])

    def test_the_block_keeps_its_look_where_a_card_keeps_it_and_carries_what_the_row_says(self):
        block = ask_block("看板改成彩色块", at="20:41", url=URL, who="管家", avatar="img_b")
        self.assertEqual(URL, block["behaviors"][0]["default_url"])  # Still the grey block, whole of it opening the message.
        self.assertEqual(("管家", "img_b"), (block[ASK_META]["who"], block[ASK_META]["avatar"]))
        self.assertNotIn("avatar", ask_block("看板改成彩色块", url=URL, who="管家")[ASK_META])
        self.assertNotIn(ASK_META, ask_block("看板改成彩色块", url=URL, who="管家", meta=False))
        self.assertNotIn(ASK_META, ask_block("没有说话人", url=URL))  # No row to say it: the block stays one.
        meta, rest = split_ask([block, {"tag": "hr"}])
        self.assertEqual((URL, [{"tag": "hr"}]), (meta["url"], rest))
        legacy = {key: value for key, value in block.items() if key != ASK_META}  # A block sent before the row.
        self.assertEqual((None, [legacy]), split_ask([legacy]))
        self.assertEqual(URL, link_of(speaker_row(meta)))  # The row is the way back.

    def test_an_answer_card_takes_the_block_into_its_row_which_is_the_way_back(self):
        block = ask_block("看板改成彩色块", at="20:41", url=URL, who="管家", doing="answer", avatar="img_b")
        facts = facts_element(input_parts("Claude Code", "Claude 自带", "Opus 5.5", "xhigh", "covey"),
                              [figure("用时", "12分40秒")], link="T9")
        built = answer_card("好了", "管家 · 好了", tail=[facts], head=[block])
        self.assertNotIn("header", built)
        self.assertEqual(("6px", "10px 12px 10px 12px"), (built["body"]["vertical_spacing"], built["body"]["padding"]))
        body = built["body"]["elements"]
        # The row (a link back to his message), the answer, the lines: no grey block, no 看你那句 › at the end, and no
        # line of his words (the butler's row quotes them itself).
        self.assertEqual(["interactive_container", "markdown", "markdown"], [element["tag"] for element in body])
        self.assertEqual(URL, link_of(body[0]))
        self.assertNotIn("看你那句", json.dumps(built, ensure_ascii=False))
        self.assertEqual([], holders(built))
        quiet = answer_card("好了", "s", head=[ask_block("看板改成彩色块", who="管家")])  # No link: nothing to link back.
        self.assertEqual(2, len(quiet["body"]["elements"]))
        self.assertEqual("markdown", quiet["body"]["elements"][0]["tag"])  # The row without a picture.

    def test_a_topic_s_or_a_lead_s_row_has_his_words_one_line_under_it(self):
        block = ask_block("把话题里的卡片和私聊区对齐，任务详情不要了，下载按钮也不要了，换配置做小", at="14:43", url=URL,
                          who="专项「发布」· 负责人", doing="done", full="把话题里的卡片和私聊区对齐，\n任务详情不要了，下载按钮也不要了，换配置做小")
        built = answer_card("好了", "s", head=[block])
        words = built["body"]["elements"][1]
        self.assertEqual(("collapsible_panel", "answer_words", False), (words["tag"], words["element_id"], words["expanded"]))
        title = words["header"]["title"]["content"]
        self.assertTrue(title.startswith("<font color='grey'>你问 14:43 · 把话题里的卡片"))
        self.assertTrue(title.endswith("…</font>"))  # One line: its opening, the fold has all of it.
        # Opened, his words line by line as he wrote them: not one line with a "\\n" in it (2026-11-22, T651).
        self.assertEqual(words["elements"][0]["content"].split("\n"),
                         ["把话题里的卡片和私聊区对齐，", "任务详情不要了，下载按钮也不要了，换配置做小"])

    def test_a_tail_card_after_a_long_answer_has_the_row_too(self):
        block = ask_block("看板改成彩色块", at="20:41", url=URL, who="管家", avatar="img_b")
        built = tail_card([block, *tail(None, ["Claude Code"], [figure("用时", "1分")])], "s", "上面是回答")
        self.assertEqual("column_set", unwrap(first(built))["tag"])
        self.assertEqual(URL, link_of(first(built)))
        self.assertNotIn("header", built)


class FactsTest(unittest.TestCase):
    parts = input_parts("Claude Code", "Claude 自带", "Opus 5.5", "xhigh", "covey")
    out = [figure("用时", "12分40秒"), "**41** <font color='grey'>token/s</font>", figure("token", "76万"),
           figure("缓存命中", "92%")]

    def test_the_two_lines_keep_every_part_and_the_box_is_gone(self):
        element = facts_element(self.parts, self.out)
        self.assertEqual(("markdown", "notation", "answer_facts"), (element["tag"], element["text_size"], element["element_id"]))
        lines = element["content"].split("\n")
        self.assertEqual(2, len(lines))
        self.assertTrue(lines[0].startswith("<font color='grey'>输入</font> "))
        self.assertTrue(lines[1].startswith("<font color='grey'>输出</font> "))
        plain = element["content"].replace(" ", " ").replace("⁠", "")
        for words in ("Claude Code", "Claude 自带", "Opus 5.5", "思考 xhigh", "covey", "用时", "12分40秒", "41", "token/s",
                      "76万", "缓存命中", "92%"):
            self.assertIn(words, plain)
        self.assertNotIn("background", json.dumps(element))

    def test_the_change_is_a_third_line_and_no_link_ends_the_output_line(self):
        element = facts_element(self.parts, self.out, change={"commit": "a6bc148d38ce", "deployed": "第 322 代",
                                                              "verified": "全量通过"})
        lines = element["content"].split("\n")
        self.assertEqual(3, len(lines))
        plain = lines[2].replace(" ", " ").replace("⁠", "")
        for words in ("改动", "a6bc148", "第 322 代", "已自检"):
            self.assertIn(words, plain)
        self.assertNotIn("›", lines[1])  # The card's row opens the topic or the message (the owner, 2026-11-22).

    def test_nothing_to_say_is_no_element(self):
        self.assertIsNone(facts_element())
        self.assertEqual(1, len(facts_element(self.parts, ())["content"].split("\n")))


class RelayTest(unittest.TestCase):
    asked = staticmethod(lambda doing="done": ask_block("发布的事", at="20:33", url=URL, who="T623 密度", doing=doing,
                                                        avatar="img_topic"))

    def test_a_short_reply_stays_open_and_a_long_one_or_a_table_shows_its_opening_and_folds_the_rest(self):
        short, _ = relay_card("T623", DONE, text="## 结论\n好了\n\n细节：改了两处。", ask=self.asked())
        # The row (a link into the topic), his words one line under it, the reply as it is (its "## 结论" left off: no
        # large row over it); no band, no fold, no 进话题 › line at the end (T638); the grey line where a reply goes last.
        self.assertEqual(["interactive_container", "collapsible_panel", "markdown", "markdown"],
                         [element["tag"] for element in short["body"]["elements"]])
        self.assertEqual("relay_hint", short["body"]["elements"][-1]["element_id"])
        self.assertEqual("好了\n\n细节：改了两处。", short["body"]["elements"][2]["content"])
        long, _ = relay_card("T623", DONE, text="## 结论\n好了。\n\n" + "细节很多。" * 160, ask=self.asked())
        _, _, reply, _ = long["body"]["elements"]
        # Whole, as card text (the owner, 2026-11-22: 不折叠，直接展示全文): no fold, no arrow, nothing cut.
        self.assertEqual(("markdown", "relay_reply"), (reply["tag"], reply["element_id"]))
        self.assertEqual("好了。\n\n" + "细节很多。" * 160, reply["content"])

    def test_the_foot_has_the_whole_input_and_output_the_change_line_and_the_links(self):
        change = {"commit": "a6bc148d38ce", "deployed": "第 322 代", "verified": "全量通过", "not_found": ["a6bc148d38ce"]}
        parts = input_parts("Claude Code", "Claude 自带", "Opus 5.5", "xhigh", "covey")
        built, _ = relay_card("T623", DONE, text="## 结论\n好了\n\n" + "细节很多。" * 60, change=change, inputs=parts,
                              outputs=[figure("用时", "1分"), figure("token", "76万"), figure("缓存命中", "92%")],
                              url=TOPIC_URL, ask=self.asked())
        ids = [element.get("element_id") for element in built["body"]["elements"]]
        self.assertNotIn("answer_change", ids)  # No fold: the change is a line.
        self.assertIn("answer_change_check", ids)  # A commit the repository does not have stays in view.
        self.assertNotIn("a6bc148d38ce", json.dumps(built["body"]["elements"][2], ensure_ascii=False))  # Not in the reply's fold.
        facts = built["body"]["elements"][-2]  # Over the line where a reply goes (T647 P1-A3).
        self.assertEqual("answer_facts", facts["element_id"])
        plain = facts["content"].replace(" ", " ").replace("⁠", "")
        for words in ("输入", "Claude Code", "Claude 自带", "Opus 5.5", "思考 xhigh", "covey", "输出", "用时", "token",
                      "缓存命中", "改动", "a6bc148"):
            self.assertIn(words, plain)
        # One way in, the row (the owner, 2026-11-22: 进话题做到卡头 T 号和标题上): no 进话题 › or 看你那句 › at the end.
        self.assertNotIn("›", plain)
        self.assertEqual(TOPIC_URL, link_of(first(built)))

    def test_without_the_topics_link_the_row_opens_the_ask_blocks(self):
        built, _ = relay_card("T623", DONE, text="好了", outputs=[figure("用时", "1分")], ask=self.asked())
        self.assertEqual(URL, link_of(first(built)))
        self.assertNotIn("›", built["body"]["elements"][-1]["content"])
        bare, _ = relay_card("T623", DONE, text="好了", outputs=[figure("用时", "1分")])
        self.assertEqual("", link_of(first(bare)))  # Nowhere to go: the row opens nothing.
        self.assertNotIn("›", json.dumps(bare["body"], ensure_ascii=False))

    def test_a_relay_without_figures_ends_with_no_line(self):
        self.assertIsNone(foot(None, ()))

    def test_a_waiting_and_a_stopped_relay_have_the_row_too(self):
        built, _ = relay_card("T623", WAITING, question="选 A 还是 B？", ask=self.asked("waiting"))
        self.assertEqual(("等你", "orange"), tag(first(built)))
        self.assertNotIn("header", built)
        self.assertEqual(URL, link_of(first(built)))
        self.assertNotIn("进话题", json.dumps(built["body"], ensure_ascii=False))
        stopped, _ = relay_card("T623", STOPPED, reason="到了时间上限", avatar="img_topic")
        self.assertEqual(("停住了", "red"), tag(first(stopped)))
        self.assertTrue(text_of(first(stopped)).startswith("<text_tag"))
        self.assertEqual("<font color='white'>**T623**</font>", badge_of(first(stopped))[0])  # Not the picture given.
        self.assertIn("topic-7", stopped["config"]["style"]["color"])


class MergedTest(unittest.TestCase):
    def test_the_merged_card_has_a_row_and_each_block_opens_its_topic(self):
        built, text = merged_card([{"label": "T1", "title": "改看板", "lead": "好了", "url": TOPIC_URL},
                                   {"label": "T2", "title": "", "lead": "", "url": ""}], avatar="img_topic")
        head, one, two, hint = built["body"]["elements"]
        self.assertEqual("relay_hint", hint["element_id"])  # A reply to it is the butler's: its last line says so.
        self.assertEqual(("有结果", "green"), tag(head))
        self.assertTrue(text_of(head).startswith("**2 个话题做完了**"))
        self.assertEqual("img_topic", head["columns"][0]["elements"][0]["img_key"])
        self.assertEqual({"type": "open_url", "default_url": TOPIC_URL}, one["behaviors"][0])
        self.assertNotIn("behaviors", two)
        self.assertTrue(text.startswith("**2 个话题做完了**"))


class QuestionCopyTest(unittest.TestCase):
    ask = staticmethod(lambda: ask_block("选哪种样式", at="09:12", url=URL, who="T439 改看板", doing="waiting"))

    def test_the_output_comes_first_then_the_question(self):
        built = mirror_card(question_record(), "T439", "上一轮写了很长很长的输出。" * 60, ask=self.ask())
        ids = [element.get("element_id") for element in built["body"]["elements"]]
        # The row, his words one line, the turn's output (its opening, then the fold), then the band and the form (the
        # owner, 2026-11-22).
        self.assertEqual(["speaker_row", "answer_words", "mirror_output"], ids[:3])
        self.assertTrue(ids[4].startswith("q_"))
        output = built["body"]["elements"][2]
        # Whole (the owner, 2026-11-22: 不折叠，直接展示全文), "这一轮输出" grey before it.
        self.assertEqual("markdown", output["tag"])
        self.assertTrue(output["content"].startswith("<font color='grey'>这一轮输出</font> 上一轮写了很长很长的输出。"))
        self.assertEqual(60, output["content"].count("输出。"))
        short = mirror_card(question_record(), "T439", "只有一句", ask=self.ask())["body"]["elements"][2]
        self.assertEqual(("markdown", "<font color='grey'>这一轮输出</font> 只有一句"), (short["tag"], short["content"]))
        self.assertEqual(("等你", "orange"), tag(first(built)))
        self.assertNotIn("header", built)
        self.assertEqual(URL, link_of(first(built)))  # The row opens the topic: no 看你那句 › at the end.
        self.assertNotIn("看你那句", json.dumps(built, ensure_ascii=False))
        self.assertNotIn("answer_ask", ids)

    def test_an_answered_copy_shows_the_question_s_opening_and_what_he_chose_under_it(self):
        answered = question_record("answered", continuation_job_id=5, delivery_state="running")
        line = settled_line(answered)
        self.assertIn("你选了「表格 / 无」 · #5 在做", unescape(line["content"]))  # Where the answer went, at a glance.
        built = mirror_card(answered, "T439", "输出", ask=self.ask())
        self.assertEqual(("已回答", "grey"), tag(first(built)))
        self.assertIn("关于你 09:12 的问题", text_of(first(built)))  # No longer "要你定".
        elements = built["body"]["elements"]
        # The question's element (q_<token>) is its peek (its first lines in view, folded), the answer's line right under it,
        # as on its card in the thread (T647 A4).
        at = next(n for n, element in enumerate(elements) if str(element.get("element_id")).startswith("q_"))
        self.assertEqual(settled_element(answered), elements[at])
        peek, under = elements[at]["columns"][0]["elements"]
        self.assertEqual(settled_peek(answered), peek)
        self.assertEqual(line, under)
        self.assertFalse(peek["expanded"])
        self.assertTrue(peek["header"]["title"]["content"].startswith("**2 道题** "))
        status = unescape(json.dumps(peek["elements"], ensure_ascii=False))
        self.assertIn("#431 的 2 道题 · 已回答 → #5 在做", status)  # The fold's old title, as a grey line inside it.
        self.assertNotIn("❓", status)
        self.assertIn("已取消", settled_line(question_record("cancelled"))["content"])
        for status in ("pending", "superseded"):
            self.assertIsNone(settled_line(question_record(status)))
        self.assertIn("你提了反馈", settled_line(question_record("answered", response_kind="feedback"))["content"])

    def test_one_answered_question_goes_on_from_its_opening_to_the_answer(self):
        long = "按新办法重排：每页把三组草图和参考图放在一起，先看每张草图的配色对不对，再看各组之间是否一致。" * 2 + "用新的评分表来判吗？"
        record = {**question_record("answered", continuation_job_id=5, delivery_state="running"),
                  "questions": [{"id": "q1", "header": "评分", "question": long, "options": [{"label": "用新评分表"}]}],
                  "answers": {"q1": {"answers": ["用新评分表"]}}}
        peek = settled_peek(record)
        head, body = peek["header"]["title"]["content"], peek["elements"][0]["content"]
        self.assertTrue(head.startswith("**评分** 按新办法重排："))
        self.assertTrue(body.endswith("用新的评分表来判吗？\n答：用新评分表"))
        self.assertEqual(2, head.count("按新办法重排") + body.count("按新办法重排"))  # The question once, read together.

    def test_a_block_sent_before_the_row_stays_at_the_top_as_it_was(self):
        legacy = {key: value for key, value in self.ask().items() if key != ASK_META}
        built = mirror_card(question_record(), "T439", "输出", ask=legacy)
        self.assertEqual("answer_ask", built["body"]["elements"][0]["element_id"])
        self.assertNotIn("header", built)


class OwnerSendTest(unittest.TestCase):
    draft = {"id": "d" * 32, "status": "drafted", "topic_label": "T9", "why": "交 key", "text": "key 开好了",
             "recipient": {"name": "张三", "kind": "p2p"}}

    def test_the_draft_waiting_for_the_owner_is_a_small_card_with_an_orange_confirm_tag(self):
        built = top_card("T9", "张三", TOPIC_URL)
        header = built["header"]
        self.assertEqual(("T9 代发草稿", "要你确认", "orange"), (header["title"]["content"], header["text_tag_list"][0]["text"]["content"],
                                                          header["text_tag_list"][0]["color"]))
        self.assertIn("张三", header["subtitle"]["content"])
        self.assertIn(f"[到话题里看 ›]({TOPIC_URL})", built["body"]["elements"][0]["content"])
        self.assertEqual(1, len(top_card("", "", "")["body"]["elements"]))  # No link: still a card, saying where.

    def test_the_card_in_the_topic_says_its_state_in_the_bar_and_the_form_is_unchanged(self):
        built = owner_send_card(self.draft, {"state": "drafted"})
        found = built["header"]["text_tag_list"][0]
        self.assertEqual(("代发草稿", "要你确认", "orange"), (built["header"]["title"]["content"], found["text"]["content"], found["color"]))
        self.assertNotIn("**✉️ 代发草稿", json.dumps(built, ensure_ascii=False))  # The heading is the bar.
        self.assertTrue(any(element.get("tag") == "form" for element in built["body"]["elements"]))
        for state, (_, words, color) in BARS.items():
            drawn = owner_send_card({**self.draft, "status": state, "sent_at_text": "18:59"}, {"state": state})
            self.assertEqual((words, color), (drawn["header"]["text_tag_list"][0]["text"]["content"],
                                              drawn["header"]["text_tag_list"][0]["color"]))


class LinkedTest(unittest.TestCase):
    def test_a_t_number_inside_the_reply_s_fold_is_a_link_and_the_artifacts_panel_is_left_alone(self):
        from covey.app.relay_send import linked
        from covey.cards.answer_card import ARTIFACTS_ID
        from covey.cards.elements import markdown_panel

        files = markdown_panel(ARTIFACTS_ID, "产物", "- T7.md", quiet=True)
        built, _ = relay_card("T9", DONE, text="## 结论\n好了\n\n" + "细节见 T7。" * 40, files=files)
        with mock.patch("covey.app.topic_links.linkify", side_effect=lambda text, *a, **k: text.replace("T7", "[T7](u)")):
            built, _ = linked("/state", "oc_x", built, "")
        self.assertIn("[T7](u)", built["body"]["elements"][1]["content"])  # The reply, whole: it fits.
        self.assertNotIn("[T7](u)", json.dumps(built["body"]["elements"][2], ensure_ascii=False))


class NoLeakTest(unittest.TestCase):
    def test_no_card_sample_sends_the_block_s_notes(self):
        """ASK_META is for the card that takes the block into its row: it is stripped there and never sent (CardKit
        refuses properties it does not know)."""
        from test_card_golden import render

        sent = {name: card for name, card in json.loads(json.dumps(render(), ensure_ascii=False)).items()
                if not name.startswith(("task.ask_speaker", "task.ask_"))}
        leaked = [name for name, card in sent.items() if holders(card)]
        self.assertEqual([], leaked)


class AvatarsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)

    def test_the_pool_is_twenty_files_that_are_there_and_the_butler_has_its_own(self):
        self.assertEqual(20, len(set(avatars.POOL)))
        for name in (*avatars.POOL, avatars.BUTLER):
            self.assertTrue(avatars.asset(name).is_file(), name)
        self.assertIsNone(avatars.asset("../x"))
        self.assertIsNone(avatars.asset("nobody"))
        self.assertIsNone(avatars.asset("topic"))  # A topic's T number is its badge (T638): no picture to upload.
        self.assertEqual({"G": 6, "M": 6, "s": 8}, {k: sum(n[0] == k for n in avatars.POOL) for k in "GMs"})

    def test_a_lead_keeps_its_picture_and_the_leads_of_one_chat_do_not_share_one(self):
        with mock.patch("covey.core.avatars.projects.read_record", return_value={"id": "x"}):
            first_run = {pid: avatars.lead_name(self.state, "oc_1", pid) for pid in (f"p{n:02d}" for n in range(20))}
            self.assertEqual(20, len(set(first_run.values())))  # Twenty projects, twenty pictures: pushed on past a taken one.
            again = {pid: avatars.lead_name(self.state, "oc_1", pid) for pid in first_run}
            self.assertEqual(first_run, again)  # Kept in <state>/avatars.json: the same next time.
            self.assertEqual(first_run, {pid: avatars.lead_name(self.state, "oc_1", pid) for pid in first_run})
            other = avatars.lead_name(self.state, "oc_2", "p00")
            self.assertEqual(avatars.pick("p00"), other)  # Another chat: the id's own place again.
            self.assertEqual(avatars.pick("p00"), avatars.pick("p00"))  # A stable hash, not Python's per-process one.
        self.assertTrue((self.state / "avatars.json").is_file())

    def test_a_project_that_is_gone_lets_go_of_its_picture(self):
        with mock.patch("covey.core.avatars.projects.read_record", return_value={"id": "x"}):
            gone = avatars.lead_name(self.state, "oc_1", "a")
        with mock.patch("covey.core.avatars.projects.read_record", return_value={}):
            self.assertEqual(gone, avatars.pick("a"))
            # "b" wanted the same place: with "a" no longer on record, it has it.
            clash = next(pid for pid in (f"c{n}" for n in range(500)) if avatars.pick(pid) == gone)
            self.assertEqual(gone, avatars.lead_name(self.state, "oc_1", clash))

    def test_nothing_without_a_state_a_chat_or_a_project(self):
        self.assertEqual("", avatars.lead_name(None, "oc_1", "p"))
        self.assertEqual("", avatars.lead_name(self.state, "", "p"))
        self.assertEqual("", avatars.lead_name(self.state, "oc_1", ""))


class UploadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        patch = mock.patch.dict(os.environ, {"COVEY_AVATARS": "1"})
        patch.start()
        self.addCleanup(patch.stop)
        uploads._failed.clear()
        self.addCleanup(uploads._failed.clear)

    def test_the_first_use_uploads_and_the_key_is_kept_by_the_file_s_content(self):
        calls = []
        upload = lambda path, profile: calls.append((path.name, profile)) or "img_v3_butler"
        self.assertEqual("img_v3_butler", uploads.image_key(self.state, "covey", "butler", upload=upload))
        self.assertEqual("img_v3_butler", uploads.image_key(self.state, "covey", "butler", upload=upload))
        self.assertEqual([("butler.png", "covey")], calls)  # Once.
        kept = json.loads((self.state / "avatars.json").read_text())["keys"]["covey"]
        self.assertEqual({avatars.digest("butler"): "img_v3_butler"}, kept)
        self.assertEqual("", avatars.cached_key(self.state, "another-app", "butler"))  # A key belongs to its app.
        self.assertEqual("", avatars.cached_key(self.state, "covey", "G-1"))

    def test_a_failed_upload_leaves_the_card_without_the_picture_and_is_tried_again_later(self):
        logs = []
        results = iter([None, "img_v3_late"])
        upload = lambda path, profile: next(results)
        self.assertEqual("", uploads.image_key(self.state, "covey", "G-1", logs.append, upload=upload))
        self.assertTrue(logs and "G-1" in logs[0])
        self.assertEqual("", uploads.image_key(self.state, "covey", "G-1", upload=upload))  # Not at once: it would wait again.
        with mock.patch("covey.feishu.avatars.time.monotonic", return_value=10 ** 9):
            self.assertEqual("img_v3_late", uploads.image_key(self.state, "covey", "G-1", upload=upload))

    def test_an_upload_that_raises_or_a_missing_picture_is_no_key_and_the_switch_turns_it_all_off(self):
        def boom(path, profile):
            raise OSError("no lark-cli")
        self.assertEqual("", uploads.image_key(self.state, "covey", "butler", upload=boom))
        self.assertEqual("", uploads.image_key(self.state, "covey", "nobody", upload=lambda *_: "img"))
        self.assertEqual("", uploads.image_key(None, "covey", "butler", upload=lambda *_: "img"))
        with mock.patch.dict(os.environ, {"COVEY_AVATARS": "0"}):
            self.assertEqual("", uploads.image_key(self.state, "covey", "butler", upload=lambda *_: "img"))

    def test_the_speaker_s_key_comes_from_who_speaks(self):
        from covey.app import answer_cards

        with mock.patch("covey.core.board_read.avatar_of", return_value="butler"), \
                mock.patch("covey.feishu.avatars.upload_image", return_value="img_b"):
            self.assertEqual("img_b", answer_cards.avatar(self.state, "topic", "oc_1", "covey"))
        with mock.patch("covey.core.board_read.avatar_of", return_value=""):
            self.assertEqual("", answer_cards.avatar(self.state, "topic", "oc_1", "covey"))  # No speaker, no picture.
        with mock.patch("covey.core.board_read.avatar_of", side_effect=OSError):
            self.assertEqual("", answer_cards.avatar(self.state, "topic", "oc_1", "covey"))
        self.assertEqual("", answer_cards.avatar(self.state, "", "oc_1", "covey"))


if __name__ == "__main__":
    unittest.main()
