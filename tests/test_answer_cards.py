"""The answer card (cards/answer_card.py, app/answer_cards.py; the owner, 2026-11-08): the answer, its 产物 folded and
the 输入/输出 box, on the topic's answer card and its relay alike (the relay's 输出 ends in its T number); a reply
too long for a card gets a small card after it; the 产物 there are brought up to date once uploaded, and the task
card leaves them out."""
from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support import make_context, make_job
from covey.app import answer_cards
from covey.app.artifacts import Artifacts, answer_files
from covey.cards.relay_card import DONE, relay_card
from covey.app.turn_delivery import TurnDelivery
from covey.cards.answer_card import (ARTIFACTS_ID, FACTS_ID, facts_element, input_lines, input_parts,
                                              output_parts, tail)
from covey.cards.elements import replace_element
from covey.cards.review import ANSWER_CARD_BYTES, after_lead, answer_card, answer_lead
from covey.core.jobs import job_key, upsert_job_record
from covey.core.state import read_object, write_json

VOICE = SimpleNamespace(safe_key=lambda key: key, key_prefix="p", message_format=lambda: "markdown",
                        chunk_limit=lambda: 3800)
SNAPSHOT = {"started_at": 0, "ended_at": 760, "input_tokens": 750_000, "output_tokens": 10_000,
            "cache_read_tokens": 690_000, "cache_write_tokens": 50_000, "measured_output_tokens": 8_200,
            "generation_seconds": 200}


def ids(built):
    return [element.get("element_id") for element in built["body"]["elements"]]


class PartsTest(unittest.TestCase):
    def test_input_in_the_owner_s_order_and_output_with_the_cache(self):
        self.assertEqual(input_parts("Claude Code", "Claude 自带", "Opus 5.5", "xhigh", True, "algo-agent-devskills"),
                         ["Claude Code", "Claude 自带", "Opus 5.5", "思考 xhigh", "脱敏开", "algo-agent-devskills"])
        # Six parts, none left out (V9, 2026-11-08): a Harness that cannot redact does not; one not known reads "—".
        self.assertEqual(input_parts("Codex", "Acme", "GPT-6", "", None, "w"),
                         ["Codex", "Acme", "GPT-6", "思考 默认", "脱敏关", "w"])
        self.assertEqual(input_parts("Claude Code", "", "", "ultracode", False, ""),
                         ["Claude Code", "—", "默认模型", "思考 ultracode", "脱敏关", "—"])
        out = " · ".join(output_parts(SNAPSHOT, 760))
        for part in ("**12分40秒**", "**41** <font color='grey'>token/s</font>", "**76万**", "缓存命中</font> **92%**"):
            self.assertIn(part, out)
        self.assertEqual(output_parts({}, None), [])

    def test_the_box_has_a_label_column_and_the_relay_s_ends_in_its_t_number(self):
        box = facts_element(["Claude Code", "Opus 5.5"], ["**1**"], link="T439")
        self.assertEqual(box["element_id"], FACTS_ID)
        rows = box["columns"][0]["elements"]
        self.assertEqual([row["columns"][0]["elements"][0]["content"] for row in rows],
                         ["<font color='grey'>输入</font>", "<font color='grey'>输出</font>"])
        self.assertTrue(rows[1]["columns"][1]["elements"][0]["content"].endswith("T439 ›"))
        self.assertIsNone(facts_element())

    def test_a_narrow_card_wraps_only_after_a_separator(self):
        # The topic side panel's chat column split "推理 / xhigh", "缓存命中 / 94%" (2026-11-08): within a part no
        # space breaks and no two characters part; between parts the line breaks after the grey bar (the owner,
        # 2026-11-09: 不用 · 分割，换成明显的分隔符), never before it.
        box = facts_element(input_parts("Claude Code", "Claude 自带", "Opus 5.5", "xhigh", False, "ws"),
                            ["<font color='grey'>缓存命中</font> **94%**"], link="T439")
        rows = [row["columns"][1]["elements"][0]["content"] for row in box["columns"][0]["elements"]]
        shown = [re.sub(r"<[^>]*>|\*\*", "", row) for row in rows]
        self.assertEqual([row.replace("\u2060", "").replace("\u00a0", " ") for row in shown],
                         ["Claude Code ｜ Claude 自带 ｜ Opus 5.5 ｜ 思考 xhigh ｜ 脱敏关 ｜ ws", "缓存命中 94% ｜ T439 ›"])
        self.assertIn("\u00a0<font color='grey'>｜</font> ", rows[1])
        breaks = [[text.replace("\u2060", "") for text in row.split("\u00a0｜ ")] for row in shown]
        self.assertEqual(breaks[0][3], "思考\u00a0xhigh")
        self.assertNotIn(" ", "".join(breaks[0]))
        self.assertNotIn("·", "".join(shown))
        self.assertIn("思\u2060考", shown[0])
        self.assertIn("<font color='grey'>缓\u2060存\u2060命\u2060中</font>\u00a0**94\u2060%**", rows[1])


class LinesTest(unittest.TestCase):
    """The box a line each (the owner, 2026-11-09 13:48: 输入 and 输出 in three short lines each were many), wrapping
    only between parts; before, in short lines (the "E" sample, 2026-11-08: one line each split "脱 / 敏关" and
    "缓存 / 命中" at a phone's width and in a topic's side panel)."""

    def plain(self, text):
        return re.sub(r"<[^>]*>|\*\*", "", text).replace("\u2060", "").replace("\u00a0", " ")

    def rows(self, box):
        return [self.plain(row["columns"][1]["elements"][0]["content"]) for row in box["columns"][0]["elements"]]

    def test_a_saved_reasoning_part_reads_as_the_thinking_budget(self):
        # The owner, 2026-11-09: 推理深度统一叫思考预算. A job's input_parts and a relay's record saved before then say
        # 推理 <budget>; the box shows them as the new ones do, and leaves a part merely starting with 推理 alone.
        old = facts_element(["Claude Code", "Claude 自带", "Opus 5.5", "推理 xhigh", "脱敏关", "ws"], ["x"])
        new = facts_element(input_parts("Claude Code", "Claude 自带", "Opus 5.5", "xhigh", False, "ws"), ["x"])
        self.assertEqual(old, new)
        self.assertEqual(self.rows(old)[0], "Claude Code ｜ Claude 自带 ｜ Opus 5.5 ｜ 思考 xhigh ｜ 脱敏关 ｜ ws")
        self.assertIn("推理器", input_lines(["推理器", "b", "c", "d", "e", "f"])[0].replace("\u2060", ""))

    def test_inputs_and_outputs_a_line_each(self):
        box = facts_element(input_parts("Claude Code", "Claude 自带", "Opus 5.5", "xhigh", True, "algo-agent-devskills"),
                            output_parts(SNAPSHOT, 760), link="T439")
        given, made = self.rows(box)
        self.assertEqual(given, "Claude Code ｜ Claude 自带 ｜ Opus 5.5 ｜ 思考 xhigh ｜ 脱敏开 ｜ algo\u2011agent\u2011devskills")
        self.assertNotIn("\n", made)
        self.assertRegex(made, r"^用时 12分40秒 ｜ 41 token/s ｜ token 76万 ｜ 缓存命中 92% ｜ T439 ›$")

    def test_a_missing_part_leaves_its_line_short_and_an_old_relays_inputs_one_line(self):
        from covey.cards.answer_card import output_lines, RUNNING_LINES, running_figures, with_ran
        self.assertEqual([self.plain(line) for line in output_lines(["<font color='grey'>token</font> **9**"])],
                         ["token 9"])
        self.assertEqual(output_lines([]), [])
        self.assertEqual(self.rows(facts_element(["Claude Code", "Opus 5.5"], ())), ["Claude Code ｜ Opus 5.5"])
        pairs = running_figures({"started_at": 0, "input_tokens": 10, "output_tokens": 5}, None)
        self.assertEqual([label for label, _ in with_ran(pairs, "1分13秒")], ["开始", "已运行", "token"])
        lines = [self.plain(line) for line in output_lines([part for _, part in with_ran(pairs, "1分13秒")],
                                                           RUNNING_LINES)]
        self.assertEqual(len(lines), 1)
        self.assertRegex(lines[0], r"^开始 \d\d:\d\d ｜ 已运行 1分13秒 ｜ token 15$")

    def test_the_tiles_and_the_box_count_the_same_tokens_and_context(self):
        # cards/facts.py: the tiles take a count that is not None, the 输出 box an int; turn_usage hands both ints.
        from covey.cards.answer_card import running_figures
        from covey.cards.facts import context_text, token_total
        from covey.cards.task_figures import figure_cells
        self.assertEqual(token_total({"input_tokens": 1.5, "output_tokens": 2}), "3.5")
        self.assertEqual(token_total({"input_tokens": 1.5, "output_tokens": 2}, ints_only=True), "2")
        self.assertIsNone(token_total({}))
        self.assertEqual([context_text(*pair) for pair in ((5000, 10000), (5000, None), (5000, 100), (0, 9), (1.5, 9))],
                         ["50%", "5,000", "5,000", None, None])
        snapshot = {"started_at": 0, "ended_at": 9, "input_tokens": 1200.0, "output_tokens": 34,
                    "context_tokens": 5000, "context_window": 10000}
        self.assertEqual([value for label, value, _ in figure_cells(snapshot) if label in ("token", "上下文")], ["34", "50%"])
        self.assertEqual([self.plain(part) for label, part in running_figures({**snapshot, "ended_at": None})
                          if label in ("token", "上下文")], ["token 34", "上下文 50%"])


class AskTest(unittest.TestCase):
    """The "你问" block (2026-11-08): which of the owner's messages an answer at the top level answers, a link to it."""

    def test_the_block_is_the_words_when_and_a_link(self):
        from covey.cards.answer_card import ASK_ID, ask_block
        block = ask_block("这个答案区的卡片能不能和私聊区的卡片样式一样？" * 4, at="12:31", url="https://example.invalid/t")
        self.assertEqual((block["tag"], block["element_id"]), ("interactive_container", ASK_ID))
        self.assertEqual(block["behaviors"], [{"type": "open_url", "default_url": "https://example.invalid/t"}])
        head, words = (element["content"] for element in block["elements"])
        self.assertIn("你问 · 12:31　›", head)
        self.assertTrue(words.endswith("…</font>"))
        self.assertNotIn("behaviors", ask_block("短", verb="你答"))  # No link: not clickable, no ›.
        self.assertIsNone(ask_block("  "))
        self.assertNotIn("**", ask_block("**粗体** [链接](x)")["elements"][1]["content"])  # Shown as written.

    def test_an_answer_s_turn_says_question_and_answer_as_its_task_card(self):
        """ACCEPT-330 #1: "你答" read the question's title without the 确认 the owner picked."""
        from covey.app.answers import SKIPPED, UNANSWERED_NOTE, answer_message, answer_title

        def words(record):
            block = answer_cards.asked("/nonexistent", record)
            head, shown = block["header"]["title"]["content"].split("\n")  # The folded block's two lines.
            self.assertIn("你答", head)
            return re.sub(r"<[^>]+>", "", shown)

        shown = "用户补充回答：\n验收：确认结束吗？\n确认"
        # The job's title (the task card's, app/answers.py answer_title) when saved, read back from its words when not.
        self.assertEqual(words({"user_text": shown, "title": "验收 → 确认"}), "验收 → 确认")
        self.assertEqual(words(SimpleNamespace(user_text=shown, title="验收 → 确认")), "验收 → 确认")
        self.assertEqual(words({"user_text": shown}), "验收：确认结束吗？ → 确认")
        # Questions without a header: the read-back says what the task card's title says, skipped or not, with
        # 补充说明 alone when nothing was answered, cut at the title's length.
        questions = [{"id": "a", "question": "验收：确认结束吗？"}, {"id": "b", "question": "用哪个分支合并到主干上去呢"}]
        cases = [({"a": ["确认"], "b": ["主干"]}, ""), ({"a": ["确认"], "b": [SKIPPED]}, ""),
                 ({"a": [SKIPPED], "b": [SKIPPED]}, "先别合，换个做法再说"), ({"a": ["A", "B"], "b": ["很长" * 20]}, "")]
        for given, supplement in cases:
            record = {"questions": questions, "supplement": supplement}
            answers = {key: {"answers": value} for key, value in given.items()}
            text = answer_message(record, answers, "", [])[0]
            with self.subTest(given=given):
                self.assertEqual(words({"user_text": text}), answer_title(record, answers))
        self.assertEqual(words({"user_text": "用户补充回答：\n问\n" + SKIPPED + "\n\n用户" + UNANSWERED_NOTE + "\n不对"}),
                         "补充：不对")
        # Asked words added to a waiting turn and an ordinary turn are as before.
        self.assertEqual(words({"user_text": shown, "title": "x", "asked_text": "另外说的话"}), "另外说的话")
        plain = answer_cards.asked("/nonexistent", {"user_text": "帮我看看\n第二行", "title": "标题"})
        self.assertIn("你问", plain["elements"][0]["content"])
        self.assertIn("帮我看看", plain["elements"][1]["content"])



class AskLinkRulesTest(unittest.TestCase):
    """Where the "你问"/"你答" block opens (the owner, 2026-11-10 16:55: 都能跳转到话题上，除非这个任务不是话题任务，是主
    Agent任务): a topic's turn its topic's thread, the butler's own turn the owner's message in the chat."""

    PLACES = {"om_root": {"thread_id": "omt_topic", "message_position": "40", "thread_message_position": "-1"},
              "om_card": {"thread_id": "omt_topic", "message_position": "41", "thread_message_position": "5"},
              "om_elsewhere": {"thread_id": "omt_other", "message_position": "50", "thread_message_position": "2"},
              "om_owner": {"thread_id": "omt_main", "message_position": "77", "thread_message_position": "-1"},
              "om_flat": {"message_position": "90"}}

    def setUp(self):
        from covey.core.butler import butler_topic_id
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name)
        self.main = butler_topic_id("oc_1")
        write_json(self.state / "topics" / "t1" / "topic.json", {"chat_id": "oc_1", "root_id": "om_root"})
        self.read, self.logs = [], []
        patcher = mock.patch("covey.feishu.message_read.message_item",
                             side_effect=lambda data, mid: data)
        patcher.start()
        self.addCleanup(patcher.stop)

    def fetch(self, message_id, profile):
        self.read.append(message_id)
        if message_id not in self.PLACES:
            raise RuntimeError("not readable")
        return {"message_id": message_id, **self.PLACES[message_id]}

    def asked(self, record, **kwargs):
        return answer_cards.asked(self.state, {"chat_id": "oc_1", "created_at": "2026-11-10T14:43:00", "job_id": 9, **record},
                                  fetch=self.fetch, log=self.logs.append, **kwargs)

    def test_a_topic_turn_opens_its_topics_thread_at_its_card(self):
        block = self.asked({"user_text": "看下日志", "topic_id": "t1", "message_id": "om_owner",
                            "card_message_id": "om_card"})
        url = block["behaviors"][0]["default_url"]
        self.assertIn("open_thread_id=omt_topic", url)
        self.assertIn("thread_position=5", url)
        self.assertIn("　›", block["elements"][0]["content"])
        # Not its owner's message, which is the butler's: the topic's root and the turn's card are read.
        self.assertNotIn("om_owner", self.read)

    def test_without_a_card_in_it_the_thread_itself_and_a_card_elsewhere_is_not_taken(self):
        block = self.asked({"user_text": "看下日志", "topic_id": "t1", "card_message_id": "om_elsewhere"})
        url = block["behaviors"][0]["default_url"]
        self.assertIn("open_thread_id=omt_topic", url)
        self.assertNotIn("thread_position", url)

    def test_a_relay_speaks_for_its_topic_with_the_butlers_words(self):
        butler_record = {"user_text": "A 和 B 两件事", "topic_id": self.main, "message_id": "om_owner"}
        block = self.asked(butler_record, topic_id="t1", at=["om_card"])
        self.assertIn("thread_position=5", block["behaviors"][0]["default_url"])
        self.assertIn("A 和 B 两件事", block["elements"][1]["content"])

    def test_a_root_with_no_thread_falls_back_to_its_place_and_says_so(self):
        write_json(self.state / "topics" / "t2" / "topic.json", {"chat_id": "oc_1", "root_id": "om_flat"})
        block = self.asked({"user_text": "看下日志", "topic_id": "t2"})
        self.assertIn("position=90", block["behaviors"][0]["default_url"])
        self.assertTrue(any("ask link fell back to the chat" in line for line in self.logs), self.logs)

    def test_no_link_at_all_is_said_and_the_block_is_not_clickable(self):
        write_json(self.state / "topics" / "t3" / "topic.json", {"chat_id": "oc_1", "root_id": "om_gone"})
        block = self.asked({"user_text": "看下日志", "topic_id": "t3"})
        self.assertNotIn("behaviors", block)
        self.assertNotIn("›", block["elements"][0]["content"])
        self.assertTrue(any("ask link missing job=#9" in line for line in self.logs), self.logs)

    def test_the_butlers_own_turn_opens_the_owners_message_in_the_chat(self):
        block = self.asked({"user_text": "几点了", "topic_id": self.main, "message_id": "om_owner"})
        url = block["behaviors"][0]["default_url"]
        self.assertIn("position=77", url)
        self.assertNotIn("thread", url)

    def test_an_answer_says_you_answered_and_opens_the_same_place(self):
        answer = {"user_text": "用户补充回答：\n用哪个？\nA", "title": "用哪个？ → A", "message_id": "answer-tok"}
        topic = self.asked({**answer, "topic_id": "t1", "card_message_id": "om_card"})
        self.assertEqual(topic["tag"], "interactive_container")
        self.assertIn("你答 · ", topic["elements"][0]["content"])
        self.assertIn("　›", topic["elements"][0]["content"])
        self.assertIn("thread_position=5", topic["behaviors"][0]["default_url"])
        # The butler's: the message of the turn that asked, at its place in the chat.
        upsert_job_record(self.state, "asking", message_id="om_owner", job_id=8, topic_id=self.main, chat_id="oc_1")
        write_json(self.state / "questions" / "tok.json", {"token": "tok", "job_key": "asking"})
        main = self.asked({**answer, "topic_id": self.main})
        self.assertIn("position=77", main["behaviors"][0]["default_url"])
        self.assertIn("你答 · ", main["elements"][0]["content"])

    def test_an_answer_with_nowhere_to_open_still_folds_out_its_words(self):
        block = self.asked({"user_text": "用户补充回答：\n用哪个？\nA", "message_id": "answer-none",
                            "topic_id": self.main})
        self.assertEqual((block["tag"], block.get("behaviors")), ("collapsible_panel", None))
        self.assertTrue(any("ask link missing" in line for line in self.logs))
        self.assertEqual(answer_cards.ask_link(self.state, "oc_1", "t1", "main-x"), "")  # Not a message: no link.


def shown_text(element):
    """What a card element shows, as read: its texts' tags dropped and literal_text's entities read back."""
    import html

    texts = re.findall(r'"content": "((?:[^"\\]|\\.)*)"', json.dumps(element, ensure_ascii=False))
    return html.unescape(re.sub(r"<[^>]+>", "", "\n".join(json.loads(f'"{text}"') for text in texts)))


class AnswerFoldTest(unittest.TestCase):
    """The owner, 2026-11-09 15:11: the "你答" block could not be opened and cut a long answer with "…"; it folds out
    the whole answer now, the same element wherever the block goes."""

    QUESTIONS = [{"id": "a", "question": "7 种风格里选哪一种（A 极简 / B 杂志 / C 手绘）？"},
                 {"id": "b", "question": "S3（先读后答）按 S3-PLAN.md 的草稿执行到哪一步？"}]
    LONG = ("我重新给你一张图，你把这个图的风格和第 3 种结合起来，颜色用暖色，字体用 **粗一点** 的；人物比例按原图，"
            "背景留白多一些。\n- 先出两张小样给我挑")

    def shown(self, notes=()):
        from covey.app.answers import answer_message
        answers = {"a": {"answers": [self.LONG]}, "b": {"answers": ["全流程（推荐）"]}}
        return answer_message({"questions": self.QUESTIONS, "supplement": "做完后发我截图"}, answers, "",
                              list(notes))[0]

    def test_an_answer_s_block_is_a_folded_panel_holding_the_whole_answer(self):
        from covey.cards.answer_card import ASK_CHARS, ASK_ID
        self.assertGreater(len(self.LONG), ASK_CHARS)
        with tempfile.TemporaryDirectory() as temp:
            write_json(Path(temp) / "questions" / "tok.json", {"token": "tok", "questions": self.QUESTIONS})
            record = {"user_text": self.shown([{"text": "顺便看看 README"}]), "created_at": "2026-11-09T14:43:00",
                      "message_id": "answer-tok", "chat_id": "oc_1", "title": "7 种风格 → 我重新给你…；S3 → 全流程"}
            block = answer_cards.asked(Path(temp), record)
        self.assertEqual((block["tag"], block["element_id"], block["expanded"]), ("collapsible_panel", ASK_ID, False))
        self.assertEqual(block["background_color"], "grey-50")
        self.assertNotIn("behaviors", block)
        head = shown_text(block["header"])
        self.assertRegex(head, r"你答 · 14:43\n7 种风格 → 我重新给你…；S3 → 全流程")
        inside = [shown_text(element) for element in block["elements"]]
        # Each question small and grey, its whole answer as written (no Markdown takes effect), then 补充说明 and
        # 另外补充.
        self.assertEqual(inside, [self.QUESTIONS[0]["question"], self.LONG, self.QUESTIONS[1]["question"],
                                  "全流程（推荐）", "补充说明", "做完后发我截图", "另外补充", "顺便看看 README"])
        self.assertEqual([element["text_size"] for element in block["elements"][:2]], ["notation", "normal_v2"])
        self.assertNotIn("**", json.dumps(block["elements"], ensure_ascii=False))
        self.assertNotIn("\n- ", json.dumps(block["elements"], ensure_ascii=False))

    def test_without_the_question_record_every_word_is_still_there(self):
        from covey.app.answers import answer_parts
        block = answer_cards.asked("/nonexistent", {"user_text": self.shown(), "message_id": "answer-gone"})
        text = shown_text(block["elements"])
        for said in (self.QUESTIONS[0]["question"], *self.LONG.split("\n"), "全流程（推荐）", "做完后发我截图"):
            self.assertIn(said, text)
        # One line each: the pairs, as the title reads them.
        self.assertEqual(answer_parts("用户补充回答：\n问一\n答一\n问二\n答二"),
                         [("question", "问一"), ("answer", "答一"), ("question", "问二"), ("answer", "答二")])
        self.assertEqual(answer_parts("用户对当前提问的反馈（未回答原问题）：\n不对"), [])

    def test_a_huge_answer_is_cut_to_the_card_s_room(self):
        from covey.cards.answer_card import ANSWER_FULL_CHARS, answer_block
        block = answer_block([("question", "问"), ("answer", "很长" * 5000), ("heading", "补充说明"), ("note", "不见了")],
                             "问 → 很长")
        text = shown_text(block["elements"])
        self.assertTrue(text.endswith("…"))
        self.assertLessEqual(len(text), ANSWER_FULL_CHARS + 10)
        self.assertNotIn("不见了", text)
        self.assertLess(len(json.dumps(block, ensure_ascii=False).encode()), 12000)
        self.assertIsNone(answer_block([], ""))

    def test_an_answer_of_escaped_characters_stays_in_its_bytes_and_a_question_is_grey_line_by_line(self):
        from covey.cards.answer_card import ANSWER_FULL_BYTES, answer_block
        # Every character of it written as an entity: the characters' cap alone would let it grow past 10 KB.
        block = answer_block([("question", "第一行\n第二行"), ("answer", "<*_>" * 600)], "问 → 符号")
        opened = sum(len(json.dumps(element["content"], ensure_ascii=False).encode()) for element in block["elements"])
        self.assertLessEqual(opened, ANSWER_FULL_BYTES + 200)
        self.assertTrue(shown_text(block["elements"]).endswith("…"))
        question = block["elements"][0]["content"]
        self.assertEqual(question.count("<font color='grey'>"), 2)
        self.assertTrue(all(line.startswith("<font color='grey'>") and line.endswith("</font>")
                            for line in question.split("\n")))

    def test_feedback_an_ordinary_turn_and_an_old_record_keep_the_ask_block(self):
        feedback = answer_cards.asked("/nonexistent", {"user_text": "用户对当前提问的反馈（未回答原问题，不表示同意或授权，"
                                                       "请重新评估问题和需求）：\n换个做法"})
        self.assertEqual(feedback["tag"], "interactive_container")
        self.assertIn("你问", feedback["elements"][0]["content"])
        self.assertIsNone(answer_cards.asked("/nonexistent", {"title": "旧记录"}))  # As before: no words, no block.
        # A turn answered at the top level after its question's copy there: the copy's block is the same fold.
        from covey.cards.question_cards import mirror_card
        block = answer_cards.asked("/nonexistent", {"user_text": self.shown()})
        record = {"token": "q1", "topic_id": "t", "status": "pending", "blocking": True, "questions": self.QUESTIONS}
        self.assertEqual(mirror_card(record, "T1", "", ask=block)["body"]["elements"][0], block)


class SaidOnceTest(unittest.TestCase):
    """A conclusion band says the answer's first line; the answer under it starts after that line (cards/review.py
    after_lead). The card said it twice, the band's and the first line of the reply under it."""

    def rest(self, text, limit=40):
        return after_lead(text, answer_lead(text, limit))

    def test_a_first_line_that_is_the_lead_goes_and_nothing_else_does(self):
        self.assertEqual(self.rest("登录页加好了，构建和测试都通过。\n\n- 新增 `/login`\n- 补了测试"), "- 新增 `/login`\n- 补了测试")
        self.assertEqual(self.rest("**结论**：登录页加好了。\n- 细节"), "- 细节")
        self.assertEqual(self.rest("## 结论\n\n登录页加好了。\n\n细节：x"), "细节：x")
        self.assertEqual(self.rest("好的，已停。"), "")
        # The band was given the lead without the answer's own "结论：" before it.
        self.assertEqual(after_lead("**结论**：登录页加好了。\n细节", "登录页加好了"), "细节")

    def test_what_the_band_does_not_say_whole_stays(self):
        for text in ("根因是 gateway 超时。另外还有一处。\n细节",  # More in the line than its first sentence.
                     "已合并 [PR](https://x.example/1)。\n细节",  # A link the band shows as words only.
                     "跑了 `pytest`，全过。\n细节", "- 第一条\n- 第二条", "> 引用的话。\n后文", "1. 第一步\n2. 第二步",
                     "```\ncode\n```\n结果在此。\n后文", "| a | b |\n| - | - |\n后文。", "# 改看板\n正文。", ""):
            self.assertEqual(self.rest(text), text, text)
        long = "这一句" + "很" * 60 + "长。\n后文"
        self.assertTrue(answer_lead(long, 40).endswith("…"))
        self.assertEqual(self.rest(long), long)  # A cut lead: the whole sentence is only in the answer.
        self.assertEqual(after_lead("登录页加好了。\n细节", ""), "登录页加好了。\n细节")
        self.assertEqual(after_lead("登录页加好了。\n细节", "别的话"), "登录页加好了。\n细节")

    def test_the_answer_card_under_a_band_and_the_relay_card_say_it_once(self):
        text = "登录页加好了，构建和测试都通过。\n\n- 补了 3 个组件测试"
        band = {"tag": "markdown", "content": "band"}
        plain = json.dumps(answer_card(text, "s"), ensure_ascii=False)
        self.assertEqual(plain.count("登录页加好了"), 1)  # No band: the answer whole.
        headed = answer_card(text, "s", head=[band], said=answer_lead(text, 40))
        self.assertNotIn("登录页加好了", json.dumps(headed["body"], ensure_ascii=False))
        self.assertIn("补了 3 个组件测试", json.dumps(headed, ensure_ascii=False))
        # Without a head there is nothing above the answer that says it: ``said`` alone changes nothing.
        self.assertIn("登录页加好了", json.dumps(answer_card(text, "s", said=answer_lead(text, 40))["body"], ensure_ascii=False))
        # One sentence that the band says whole: no empty Markdown block under it.
        alone = answer_card("好的，已停。", "s", head=[band], said="好的，已停")
        self.assertEqual([e for e in alone["body"]["elements"] if e.get("content") == ""], [])
        self.assertNotIn("好的，已停", json.dumps(alone["body"], ensure_ascii=False))
        relayed, body = relay_card("T7", DONE, text=text)
        self.assertEqual(body, text)  # What goes as Markdown when the card cannot is the reply whole.
        self.assertEqual(json.dumps(relayed["body"], ensure_ascii=False).count("登录页加好了，构建和测试都通过"), 1)
        self.assertIn("补了 3 个组件测试", json.dumps(relayed, ensure_ascii=False))


class DefaultsTest(unittest.TestCase):
    def test_claude_s_model_reaches_the_process_snapshot(self):
        from covey.backends.claude.usage import ClaudeTurnUsage
        from covey.core.trace import ToolTrace as TaskTrace

        events = []
        usage = ClaudeTurnUsage(lambda kind, data: events.append((kind, data)))
        usage.accept({"type": "system", "subtype": "init", "model": "claude-opus-5-5[1m]"}, 1.0)
        usage.accept({"type": "assistant", "message": {"id": "m1", "content": [],
                                                       "usage": {"input_tokens": 3, "output_tokens": 2}}}, 2.0)
        self.assertEqual(events[-1][1]["model"], "claude-opus-5-5[1m]")
        trace = TaskTrace()
        trace.accept(*events[-1])
        self.assertEqual(TaskTrace.restore(trace.snapshot()).snapshot()["model"], "claude-opus-5-5[1m]")
        self.assertNotIn("model", TaskTrace().snapshot())

    def test_no_model_chosen_reads_default_and_a_job_s_time_comes_from_its_timings(self):
        harness = SimpleNamespace(spec=SimpleNamespace(name="claude", title="Claude Code"))
        job = SimpleNamespace(model="", model_label="", effort="", redaction=False, workspace_label="repo.code")
        self.assertEqual(answer_cards.inputs(job, harness),
                         ["Claude Code", "Claude 自带", "默认模型", "思考 默认", "脱敏关", "repo.code"])
        # The model the turn ran (Claude's init row, in its process snapshot) by the launch card's name, the budget
        # its context ran with.
        with tempfile.TemporaryDirectory() as temp:
            write_json(Path(temp) / "launch-models" / "claude.json",
                       {"updated_at": 1, "models": {"claude-opus-5-5": {"display_name": "Opus 5.5"}}})
            ctx = SimpleNamespace(state_dir=Path(temp), effort="xhigh")
            self.assertEqual(answer_cards.inputs(job, harness, {"model": "claude-opus-5-5[1m]"}, ctx),
                             ["Claude Code", "Claude 自带", "Opus 5.5", "思考 xhigh", "脱敏关", "repo.code"])
            self.assertEqual(answer_cards.inputs(job, harness, {"model": "claude-sonnet-5-5"}, ctx)[2], "Sonnet 5.5")
        # A MASO budget preset run as it is (no effort of its own): the budget its name gives, not 默认.
        maso = SimpleNamespace(spec=SimpleNamespace(name="maso", title="MASO"))
        argon = SimpleNamespace(model="gemini-4-argon_reasoning_high", model_label="MASO · Gemini 4 Argon", effort="",
                                redaction=None, workspace_label="w")
        self.assertEqual(answer_cards.inputs(argon, maso)[2:4], ["Gemini 4 Argon", "思考 high"])
        self.assertEqual(answer_cards.inputs(SimpleNamespace(**{**vars(argon), "model": "seed-stable-reasoning"}),
                                             maso)[3], "思考 默认")
        with tempfile.TemporaryDirectory() as temp:
            turn = SimpleNamespace(user_text="1+1？", chat_id="oc_1", topic_id="t", message_id="main-x",
                                   timings={"received": 1794100000.0})
            block = answer_cards.asked(Path(temp), turn)
        self.assertRegex(block["elements"][0]["content"], r"你问 · \d\d:\d\d")


class CardTest(unittest.TestCase):
    files = {"tag": "collapsible_panel", "element_id": ARTIFACTS_ID, "expanded": False, "elements": []}

    def test_the_answer_card_and_the_relay_are_the_same_but_for_the_link(self):
        parts = tail(self.files, ["Claude Code"], ["**1**"])
        built = answer_card("## 结论\n好了", "T439 有结果 · 结论", parts)
        self.assertEqual(ids(built), [None, ARTIFACTS_ID, FACTS_ID])
        self.assertTrue(built["config"]["update_multi"])  # Its 产物 are written in later.
        relay, _ = relay_card("T439", DONE, text="## 结论\n好了。\n\n细节在下面", inputs=["Claude Code"],
                              outputs=["**1**"], files=self.files)
        self.assertEqual(ids(relay), [None, None, ARTIFACTS_ID, FACTS_ID])  # The conclusion band leads the relay.
        whole, _ = relay_card("T439", DONE, text="## 结论\n好了", inputs=["Claude Code"], outputs=["**1**"],
                              files=self.files)
        self.assertEqual(ids(whole), [None, ARTIFACTS_ID, FACTS_ID])  # The band says all of a one-line reply.
        self.assertIn("T439 ›", json.dumps(relay, ensure_ascii=False))
        self.assertNotIn("T439 ›", json.dumps(built, ensure_ascii=False))
        # Without artifacts: the box alone; a plain answer card without a tail as before.
        self.assertEqual(ids(answer_card("好了", "s", tail(None, ["Claude Code"], []))), [None, FACTS_ID])
        self.assertNotIn("update_multi", answer_card("好了", "s").get("config", {}))

    def test_a_reply_too_long_for_a_card_goes_as_markdown_and_a_relay_held_before_is_an_answer_card(self):
        self.assertIsNone(relay_card("T439", DONE, text="字" * 9000, inputs=[], outputs=[])[0])
        # Held by a release before 2026-11-08: at, took and artifacts, no inputs. The reply and its T number's box,
        # no grey line.
        old, text = relay_card("T439", DONE, text="好了", at=1.0, took=60, artifacts=2)
        self.assertEqual(ids(old), [None, FACTS_ID])  # The band (it says the one-line reply whole) and the box.
        self.assertEqual(text, "好了")
        self.assertIn("T439 ›", json.dumps(old, ensure_ascii=False))
        self.assertNotIn("产物 2 个", json.dumps(old, ensure_ascii=False))
        self.assertEqual(text, "好了")

    def test_an_element_is_replaced_where_it_is(self):
        built = answer_card("好了", "s", tail(self.files, ["a"], []))
        self.assertTrue(replace_element(built, ARTIFACTS_ID, {"tag": "markdown", "element_id": ARTIFACTS_ID, "content": "新"}))
        self.assertEqual(built["body"]["elements"][1]["content"], "新")
        self.assertFalse(replace_element(built, "nothing", {}))


class Case(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.ctx = make_context(self.root, topic_id="t1")
        self.state = self.ctx.state_dir
        self.state.mkdir(parents=True, exist_ok=True)
        (self.root / "REPORT.md").write_text("报告")
        point = self.root / "checkpoint.json"
        point.write_text(json.dumps({"artifacts": [str(self.root / "REPORT.md")]}))
        self.key = job_key("om_1", 7)
        self.record = {"key": self.key, "job_id": 7, "checkpoint_path": str(point), "workdir": str(self.root)}

    def ledger(self, status, **more):
        write_json(self.state / "artifact-drive" / f"{self.key}.json", {"status": status, "files": {}, **more})


class RefreshTest(Case):
    def test_each_card_is_redrawn_once_the_upload_says_otherwise(self):
        panel = answer_files(self.state, self.record, "t1", VOICE)
        self.assertIn("上传中", panel["header"]["title"]["content"])
        self.assertFalse(panel["expanded"])  # Folded while it uploads.
        for where, mid in (("answer", "om_a"), ("relay", "om_r")):
            answer_cards.register(self.state, self.key, where, mid, answer_card("好了", "s", tail(panel, ["a"], [])))
        self.assertTrue(answer_cards.holds_files(self.state, self.key))
        self.assertTrue(answer_cards.clicked(self.state, self.key, "om_r"))
        self.ledger("done", folder_url="https://example.invalid/folder")
        messenger = mock.Mock()
        messenger.patch_card.return_value = True
        done = answer_files(self.state, self.record, "t1", VOICE)
        self.assertEqual(answer_cards.refresh(self.state, "p", messenger, self.key, done, "done", print), 2)
        sent = messenger.patch_card.call_args_list[0].args[2]
        self.assertIn("产物 1 个 · REPORT.md", json.dumps(sent, ensure_ascii=False))
        self.assertEqual(answer_cards.refresh(self.state, "p", messenger, self.key, done, "done", print), 0)
        self.assertEqual(answer_cards.refresh(self.state, "p", messenger, self.key, done, "done", print, force=True), 2)
        saved = read_object(answer_cards.record_path(self.state, self.key))
        self.assertEqual({entry["status"] for entry in saved["cards"]}, {"done"})

    def test_a_failed_upload_opens_the_panel_with_its_retry(self):
        self.ledger("failed", error="没有权限")
        panel = answer_files(self.state, self.record, "t1", VOICE)
        self.assertTrue(panel["expanded"])
        self.assertIn("重新上传", json.dumps(panel, ensure_ascii=False))
        self.assertIsNone(answer_files(self.state, {**self.record, "checkpoint_path": ""}, "t1", VOICE))


class AttachTest(Case):
    def artifacts(self):
        cards = mock.Mock()
        cards.set_task_element.return_value = True
        built = Artifacts(SimpleNamespace(voice=VOICE), runtime=mock.Mock(), messenger=mock.Mock(), cards=cards)
        built.in_background = mock.Mock()
        return built, cards

    def test_the_task_card_leaves_them_to_the_answer_card(self):
        artifacts, cards = self.artifacts()
        job = SimpleNamespace(progress=SimpleNamespace(), artifact_controls_attached=False, message_id="om_1")
        with mock.patch("covey.app.artifacts.find_job_record", return_value=self.record):
            answer_cards.register(self.state, self.key, "answer", "om_a", {"body": {"elements": []}})
            artifacts.attach_artifact_controls(self.ctx, job)
        cards.set_task_element.assert_not_called()
        self.assertEqual(artifacts.in_background.call_args.args[-1], False)  # Uploaded, not drawn on the task card.

    def test_without_an_answer_card_they_go_right_under_the_body(self):
        artifacts, cards = self.artifacts()
        job = SimpleNamespace(progress=SimpleNamespace(), artifact_controls_attached=False, message_id="om_1")
        with mock.patch("covey.app.artifacts.find_job_record", return_value=self.record):
            artifacts.attach_artifact_controls(self.ctx, job)
        self.assertTrue(cards.set_task_element.call_args.kwargs["under"])

    def test_a_quiet_turn_with_no_task_card_still_uploads_its_answer_cards_files(self):
        # A butler turn of the 话题区 layout that sent no task card (core/placement.py Row.quiet, B4): its
        # answer card said 上传中 until the owner clicked 重新上传 (2026-11-09 14:25, #3395).
        artifacts, cards = self.artifacts()
        artifacts.in_background = lambda target, *args: target(*args)  # The upload thread, run in place.
        artifacts.messenger.patch_card.return_value = True

        def upload(ctx, record):
            self.ledger("done", folder_url="https://example.invalid/folder")
            return {"status": "done"}
        artifacts.upload_artifacts = mock.Mock(side_effect=upload)
        panel = answer_files(self.state, self.record, "t1", VOICE)
        job = SimpleNamespace(progress=None, artifact_controls_attached=False, message_id="om_1")
        with mock.patch("covey.app.artifacts.find_job_record", return_value=self.record):
            answer_cards.register(self.state, self.key, "answer", "om_a", answer_card("好了", "s", tail(panel, ["a"], [])))
            artifacts.attach_artifact_controls(self.ctx, job)
            artifacts.attach_artifact_controls(self.ctx, job)  # Recorded done again: one upload.
        artifacts.upload_artifacts.assert_called_once()
        cards.set_task_element.assert_not_called()
        self.assertTrue(job.artifact_controls_attached)
        message_id, sent = artifacts.messenger.patch_card.call_args.args[1:3]
        self.assertEqual(message_id, "om_a")
        self.assertNotIn("上传中", json.dumps(sent, ensure_ascii=False))
        self.assertIn("产物 1 个 · REPORT.md", json.dumps(sent, ensure_ascii=False))

    def test_a_turn_with_neither_card_has_nothing_to_draw_on(self):
        artifacts, cards = self.artifacts()
        job = SimpleNamespace(progress=None, artifact_controls_attached=False, message_id="om_1")
        with mock.patch("covey.app.artifacts.find_job_record", return_value=self.record):
            artifacts.attach_artifact_controls(self.ctx, job)
        cards.set_task_element.assert_not_called()
        artifacts.in_background.assert_not_called()
        self.assertFalse(job.artifact_controls_attached)


class DeliveryTest(Case):
    def deliver(self, reply, chat="oc_1", progress=True):
        self.files = []
        job = make_job(job_id=7, chat_id=chat, message_id="om_1", topic_id="t1",
                       progress=SimpleNamespace() if progress else None,
                       checkpoint_path=self.record["checkpoint_path"], model="claude-opus-5-5", effort="xhigh")
        job.timings.update(backend_started=100.0, execution_finished=860.0)
        job.tool_trace = SimpleNamespace(snapshot=lambda: SNAPSHOT)
        verified = SimpleNamespace(text=reply, fingerprint="f1")
        turn = SimpleNamespace(status="done", status_message="", final_text=reply, verified_delivery=verified,
                               verified_outcome=SimpleNamespace(status="completed", notice=lambda: "", error_code=""),
                               outcome=None, allow_resume=True)
        cards = mock.Mock()
        cards.harness = SimpleNamespace(spec=SimpleNamespace(name="claude", title="Claude Code"))
        cards.render_stream_final = lambda job, text, **kwargs: text
        cards.exceeds_stream_card_limit = lambda text: False
        cards.body_cut.return_value = False

        def send(ctx, chat, text, key, card=None, on_card=None):
            if card is not None and on_card is not None:
                on_card("om_answer" if not key.endswith("-tail") else "om_tail")
            return True
        send_text = mock.Mock(side_effect=send)
        runtime = mock.Mock()
        runtime.access.owner_chat.return_value = "oc_1"  # The owner's private chat (app/access.py).
        delivery = TurnDelivery(job, self.ctx, turn, prefix="p", cards=cards, runtime=runtime,
                                artifacts=SimpleNamespace(voice=VOICE, send_result_file=lambda c, j, text: self.files.append(text) or True),
                                questions=mock.Mock(), steering=mock.Mock(),
                                record_job_state=mock.Mock(), send_text=send_text)
        with mock.patch.object(TurnDelivery, "texts", return_value=(reply, reply, False)), \
                mock.patch("covey.app.answer_delivery.find_job_record", return_value=self.record), \
                mock.patch("covey.app.turn_delivery.close_streaming_reply_async"):
            delivery.deliver(None)  # The workdir's snapshot (the diff): not what the 输出 line reads.
        self.delivery = delivery
        return send_text

    def test_the_answer_goes_with_its_files_and_facts_and_is_kept_for_the_upload(self):
        send_text = self.deliver("## 结论\n好了")
        send_text.assert_called_once()
        built = send_text.call_args.kwargs["card"]
        self.assertEqual(ids(built), [None, ARTIFACTS_ID, FACTS_ID])
        box = json.dumps(built["body"]["elements"][2], ensure_ascii=False).replace("\u2060", "").replace("\u00a0", " ")
        self.assertIn("Claude Code", box)
        self.assertIn("token/s", box)
        saved = read_object(answer_cards.record_path(self.state, self.key))
        self.assertEqual([(entry["where"], entry["message_id"]) for entry in saved["cards"]], [("answer", "om_answer")])

    def test_a_quiet_turn_with_no_task_card_keeps_its_answer_card_for_the_upload(self):
        # B4: no task card (job.progress None); the answer card with its 产物 goes to the top level, is kept for the
        # upload, and the turn is recorded delivered, which is what starts it (app/app.py record_job_state).
        from covey.core import placement
        with mock.patch.object(placement, "quiet", return_value=True):
            send_text = self.deliver("## 结论\n好了", progress=False)
            self.assertTrue(self.delivery.bare())
        self.assertEqual(ids(send_text.call_args.kwargs["card"])[-2:], [ARTIFACTS_ID, FACTS_ID])
        self.assertTrue(answer_cards.holds_files(self.state, self.key))
        last = self.delivery.record_job_state.call_args_list[-1]
        self.assertEqual((last.args[2], last.kwargs["delivery_ok"]), ("done", True))

    def test_a_turn_that_committed_has_its_change_folded_under_its_files(self):
        Path(self.record["checkpoint_path"]).write_text(json.dumps({"artifacts": [str(self.root / "REPORT.md")], "delivery": {
            "commit": "951327a0c1d2e3f4", "deployed": "第 331 代", "verified": "全量测试"}}, ensure_ascii=False))
        built = self.deliver("## 结论\n好了").call_args.kwargs["card"]
        self.assertEqual(ids(built), [None, ARTIFACTS_ID, "answer_change", FACTS_ID])
        self.assertIn("改动 · 951327a · 第 331 代 · 已自检", json.dumps(built, ensure_ascii=False))
        # A Q&A turn's checkpoint with a delivery but no commit, and no files changed: none.
        Path(self.record["checkpoint_path"]).write_text(json.dumps({"artifacts": [str(self.root / "REPORT.md")], "delivery": {"verified": "答了"}},
                                                                ensure_ascii=False))
        self.assertEqual(ids(self.deliver("好了").call_args.kwargs["card"]), [None, ARTIFACTS_ID, FACTS_ID])

    def test_the_answer_cards_message_is_saved_on_the_job_for_the_pinned_board(self):
        self.deliver("好了")  # No 产物 to keep up to date: still the card's message, which a 话题区's pin is.
        calls = self.delivery.record_job_state.call_args_list
        self.assertEqual(calls[-1].args[2], "done")
        self.assertEqual(calls[-1].kwargs["answer_message_id"], "om_answer")
        self.assertNotIn("answer_message_id", calls[0].kwargs)  # Not before it went.
        self.deliver("字" * (ANSWER_CARD_BYTES // 2))  # A file: no answer card, the board pins the task card.
        self.assertNotIn("answer_message_id", self.delivery.record_job_state.call_args_list[-1].kwargs)

    def receipts(self):
        """What the task card's body was last written with (Cards.update_job_streaming_reply's content)."""
        return [call.args[2] for call in self.delivery.cards.update_job_streaming_reply.call_args_list]

    def test_a_topic_area_s_answer_card_leads_with_its_status_and_the_task_card_keeps_no_lead(self):
        # D3 (the owner, 2026-11-08): in a 话题区's thread the answer card is the turn's result and its Pin; the
        # task card keeps its status line and 输入/输出, no 结论.
        from covey.core import placement
        from covey.core.topics import topic_file
        write_json(topic_file(self.state, "t1"), {"topic_number": 443, "origin": {"title": "改看板"}})
        with mock.patch.object(placement, "layout", return_value=placement.AREA):
            send_text = self.deliver("## 结论\n好了。\n\n细节在下面")
        built = send_text.call_args.kwargs["card"]
        self.assertEqual(ids(built), [None, None, ARTIFACTS_ID, FACTS_ID])  # A reply with a lead: the conclusion band.
        # The band says the lead; the reply under it starts after it (cards/review.py after_lead).
        self.assertEqual(json.dumps(built["body"], ensure_ascii=False).count("好了"), 1)
        self.assertIn("细节在下面", built["body"]["elements"][1]["content"])
        head = json.dumps(built["body"]["elements"][0], ensure_ascii=False)
        self.assertIn("有结果 · T443 改看板", re.sub(r"<[^>]*>|\*\*", "", head).replace("&#35;", "#"))
        self.assertEqual(self.receipts()[-1], " ")
        for where in (placement.TOPICS,):  # The other layout as before: no line, the lead.
            with self.subTest(where), mock.patch.object(placement, "layout", return_value=where):
                send_text = self.deliver("## 结论\n好了")
                self.assertEqual(ids(send_text.call_args.kwargs["card"]), [None, ARTIFACTS_ID, FACTS_ID])
                self.assertIn("好了", self.receipts()[-1])

    def test_a_group_s_topic_in_the_topic_area_layout_keeps_its_cards_as_before(self):
        # D3 is the owner's private chat's: a group's topic has the same row of the placement table (its Traits
        # are a topic's), yet its answer card has no status line and its task card keeps the 结论 lead.
        from covey.core import placement
        from covey.core.topics import topic_file
        write_json(topic_file(self.state, "t1"), {"topic_number": 443, "origin": {"title": "改看板"}})
        with mock.patch.object(placement, "layout", return_value=placement.AREA):
            self.assertTrue(placement.plan(self.state, make_job(topic_id="t1", chat_id="oc_group")).row.answer_head)
            send_text = self.deliver("## 结论\n好了", chat="oc_group")
            self.assertEqual(ids(send_text.call_args.kwargs["card"]), [None, ARTIFACTS_ID, FACTS_ID])
            self.assertIn("好了", self.receipts()[-1])
        runtime = mock.Mock()  # The private chat not known yet, or not readable: as before too.
        runtime.access.owner_chat.return_value = ""
        delivery = TurnDelivery(make_job(chat_id="oc_1"), self.ctx, None, prefix="p", cards=mock.Mock(),
                                runtime=runtime, artifacts=None, questions=None, steering=None,
                                record_job_state=None, send_text=None)
        self.assertFalse(delivery.headed(placement.TABLE[(placement.AREA, placement.TOPIC_ROLE)]))
        runtime.access.owner_chat.side_effect = OSError("gone")
        self.assertFalse(delivery.headed(placement.TABLE[(placement.AREA, placement.TOPIC_ROLE)]))

    def test_the_status_line_says_how_the_turn_ended(self):
        from covey.cards.answer_card import head_element
        from covey.core.turn_words import answer_head
        self.assertEqual([answer_head(status) for status in ("done", "failed", "cancelled", "interrupted", "waiting_user")],
                         ["有结果", "停住了", "已停止", "停住了", "等你"])
        self.assertEqual(head_element("有结果")["content"], "**有结果**")
        self.assertNotIn("**改**", head_element("有结果", "T1", "**改** <b>看板</b>")["content"])  # Shown as written.

    def test_a_reply_sent_as_a_file_gets_a_small_card_after_it(self):
        send_text = self.deliver("字" * (ANSWER_CARD_BYTES // 2))  # Over 20,000 bytes: a Markdown file.
        self.assertEqual(len(self.files), 1)
        send_text.assert_called_once()
        self.assertTrue(send_text.call_args.args[3].endswith("-tail"))
        self.assertEqual(ids(send_text.call_args.kwargs["card"])[1:], [ARTIFACTS_ID, FACTS_ID])
        saved = read_object(answer_cards.record_path(self.state, self.key))
        self.assertEqual([entry["where"] for entry in saved["cards"]], ["tail"])



class RedeliveryTest(Case):
    """B1 (the owner, 2026-11-10: 重投的答案卡和首发一致): a saved reply whose first send failed goes again (检查并继续,
    the outbox) as the same card under the same key, in the same place: its 产物, 输入/输出 and "你问" block too
    (app/saved_send.py)."""

    def first(self, reply="## 结论\n好了"):
        """The first send of task #7's saved reply, which Feishu refused: (key, context, card) it tried."""
        job = make_job(job_id=7, chat_id="oc_1", message_id="om_1", topic_id="t1", progress=SimpleNamespace(),
                       checkpoint_path=self.record["checkpoint_path"], model="claude-opus-5-5", effort="xhigh")
        job.timings.update(backend_started=100.0, execution_finished=860.0)
        job.tool_trace = SimpleNamespace(snapshot=lambda: SNAPSHOT)
        self.reply = SimpleNamespace(text=reply, fingerprint="f1")
        turn = SimpleNamespace(status="done", status_message="", final_text=reply, verified_delivery=self.reply,
                               verified_outcome=SimpleNamespace(status="completed", notice=lambda: "", error_code=""),
                               outcome=None, allow_resume=True)
        cards = mock.Mock()
        cards.harness = SimpleNamespace(spec=SimpleNamespace(name="claude", title="Claude Code"))
        cards.render_stream_final = lambda job, text, **kwargs: text
        cards.exceeds_stream_card_limit = lambda text: False
        cards.body_cut.return_value = False
        self.harness = cards.harness
        send_text = mock.Mock(return_value=False)
        runtime = mock.Mock()
        runtime.access.owner_chat.return_value = "oc_1"
        delivery = TurnDelivery(job, self.ctx, turn, prefix="p", cards=cards, runtime=runtime,
                                artifacts=SimpleNamespace(voice=VOICE, send_result_file=lambda c, j, text: False),
                                questions=mock.Mock(), steering=mock.Mock(),
                                record_job_state=mock.Mock(), send_text=send_text)
        with mock.patch.object(TurnDelivery, "texts", return_value=(reply, reply, False)), \
                mock.patch("covey.app.answer_delivery.find_job_record", return_value=self.record), \
                mock.patch("covey.app.turn_delivery.close_streaming_reply_async"):
            delivery.deliver(None)
        call = send_text.call_args
        return call.args[3], call.args[0], call.kwargs["card"]

    def again(self, said="已重新投递"):
        """Task #7's saved reply redelivered (app/redelivery.py redeliver_verified_result), its answer ``said``:
        (key, context, card), None when nothing was sent."""
        from covey.app.task_controls import TaskControls
        record = {**self.record, "message_id": "om_1", "chat_id": "oc_1", "topic_id": "t1",
                  "execution_status": "done", "card_message_id": "om_card", "model": "claude-opus-5-5",
                  "effort": "xhigh", "workspace_name": self.ctx.workspace_name,
                  "timings": {"backend_started": 100.0, "execution_finished": 860.0}}
        controls = TaskControls.__new__(TaskControls)
        controls.harness = SimpleNamespace(spec=SimpleNamespace(name="claude", title="Claude Code",
                                                                idempotency_prefix="p"))
        controls.messenger, controls.voice, controls.cards = mock.Mock(), VOICE, mock.Mock()
        controls.messenger.reply.side_effect = lambda ctx, chat, text, key, voice, card=None, on_card=None: (
            on_card is not None and on_card("om_answer" if not key.endswith("-tail") else "om_tail")) or True
        controls.cards.model_label.return_value = ""
        self.refresh = mock.Mock()
        controls.artifacts = SimpleNamespace(voice=VOICE, send_result_file=lambda c, j, text: False,
                                             refresh_answer_cards=self.refresh)
        controls.runtime = mock.Mock()
        controls.runtime.access.owner_chat.return_value = "oc_1"
        authorized = mock.MagicMock()
        authorized.return_value.__enter__.return_value = (self.reply, SimpleNamespace(job_status="completed", notice=str),
                                                          record)
        with mock.patch("covey.app.redelivery.authorized_delivery", authorized), \
                mock.patch("covey.app.redelivery.display_saved_reply", return_value=self.reply.text), \
                mock.patch("covey.app.redelivery.upsert_job_record") as upsert, \
                mock.patch("covey.app.redelivery.needs_question", return_value=False):
            self.assertIn(said, controls.redeliver_verified_result(self.ctx, "oc_1", record))
        self.upsert = upsert
        call = controls.messenger.reply.call_args
        return (call.args[3], call.args[0], call.kwargs["card"]) if call else None

    def same(self, first, again):
        self.assertEqual(again[0], first[0])  # The same key ...
        self.assertEqual(again[1].reply_to_message_id, first[1].reply_to_message_id)  # ... the same place ...
        self.assertEqual(json.dumps(again[2], ensure_ascii=False, sort_keys=True),
                         json.dumps(first[2], ensure_ascii=False, sort_keys=True))  # ... the same card.

    def test_the_card_with_its_files_and_facts_goes_again_as_it_first_went(self):
        first = self.first()
        self.assertEqual(ids(first[2]), [None, ARTIFACTS_ID, FACTS_ID])
        again = self.again()
        self.same(first, again)
        self.assertEqual(self.upsert.call_args.kwargs["answer_message_id"], "om_answer")
        saved = read_object(answer_cards.record_path(self.state, self.key))
        self.assertEqual([entry["where"] for entry in saved["cards"]], ["answer"])  # Kept for the upload.

    def test_the_butlers_answer_goes_again_to_the_top_level_with_its_you_asked_block(self):
        from covey.core import placement
        top = SimpleNamespace(row=SimpleNamespace(answer=placement.TOP, answer_head=False))
        with mock.patch.object(placement, "plan", return_value=top), \
                mock.patch.object(answer_cards, "ask_link", return_value="https://applink.feishu.cn/x"):
            first = self.first()
            self.assertEqual(first[2]["body"]["elements"][0]["element_id"], "answer_ask")
            self.assertEqual(first[1].reply_to_message_id, "")
            again = self.again()
        self.same(first, again)

    def test_it_goes_again_byte_for_byte_once_the_upload_moved_on_then_its_files_are_brought_up(self):
        # The same key never goes with other content (feishu/transport.py idempotency_conflict): the card as kept,
        # then its 产物 as the upload stands (Artifacts.refresh_answer_cards).
        first = self.first()
        self.ledger("done", files={})
        self.same(first, self.again())
        self.refresh.assert_called_once()
        self.assertEqual(self.refresh.call_args.args[1]["key"], self.key)

    def test_a_reply_its_first_send_kept_no_card_of_is_drawn_from_its_record_the_same(self):
        from covey.app import saved_send
        from covey.core.trace_store import save_trace
        first = self.first()
        path = answer_cards.record_path(self.state, self.key)
        saved = read_object(path)
        del saved[saved_send.OUTGOING]
        write_json(path, saved)
        save_trace(self.state, self.key, SNAPSHOT)  # Its process record, as its task runner saved it.
        self.same(first, self.again())

    def outgoing(self, change):
        """The answer-cards record's ``outgoing`` changed by ``change`` (a function of it)."""
        from covey.app import saved_send
        path = answer_cards.record_path(self.state, self.key)
        saved = read_object(path)
        change(saved[saved_send.OUTGOING])
        write_json(path, saved, ensure_ascii=False)

    def drawn_again(self, first):
        """The redelivery drew the card again (``rebuilt``): its record's saved trace gives the first send's card."""
        from covey.app import saved_send
        from covey.core.trace_store import save_trace
        save_trace(self.state, self.key, SNAPSHOT)
        with mock.patch.object(saved_send, "rebuilt", wraps=saved_send.rebuilt) as drawn:
            self.same(first, self.again())
        drawn.assert_called_once()

    def test_a_kept_card_whose_body_was_changed_is_drawn_again(self):
        # r4 review C2 (rule 5): the kept card goes only with the digest of itself and the text authorized now.
        first = self.first()

        def body(outgoing):
            text = json.dumps(outgoing["answer"], ensure_ascii=False)
            self.assertIn("好了", text)
            outgoing["answer"] = json.loads(text.replace("好了", "改过的正文"))
        self.outgoing(body)
        self.drawn_again(first)

    def test_a_kept_card_of_another_text_is_drawn_again(self):
        first = self.first()
        self.outgoing(lambda outgoing: outgoing.update(digest=(outgoing.get("digest") or "0" * 64)[::-1] + "0"))
        self.drawn_again(first)

    def test_a_kept_card_without_a_digest_an_old_record_is_drawn_again(self):
        first = self.first()
        self.outgoing(lambda outgoing: outgoing.pop("digest", None))
        self.drawn_again(first)

    def test_the_kept_card_carries_the_digest_of_the_authorized_text(self):
        from covey.app import saved_send
        first = self.first()
        send_key = first[0]
        kept = saved_send.kept(self.state, self.key, send_key, self.reply.text)
        self.assertIsNotNone(kept)
        self.assertEqual(kept.digest, saved_send.digest_of(kept, self.reply.text))
        self.assertIsNone(saved_send.kept(self.state, self.key, send_key, self.reply.text + "。"))
        with mock.patch.object(saved_send, "rebuilt", side_effect=AssertionError("drawn again")):
            self.same(first, self.again())

    def test_a_card_tried_under_the_key_is_not_drawn_again(self):
        # r4 review P1 (rules 1 and 5): with no card kept, a card already tried under the key (its receipt) may have
        # landed; one drawn again is other content under it: not sent, the outbox told it needs reconciling.
        from covey.app import saved_send
        from covey.feishu import transport
        from covey.feishu.delivery import safe_idempotency_key
        first = self.first()
        path = answer_cards.record_path(self.state, self.key)
        saved = read_object(path)
        del saved[saved_send.OUTGOING]
        write_json(path, saved)
        previous = transport.configure_receipts(self.state)
        self.addCleanup(transport.configure_receipts, previous)
        receipt = transport._receipt_path(safe_idempotency_key(f"{first[0]}-card", fallback=VOICE.key_prefix))
        receipt.parent.mkdir(parents=True)
        write_json(receipt, {"fingerprint": "the first send's card", "uncertain_since": 1.0})
        with mock.patch("covey.app.redelivery.record_attempt") as attempt:
            self.assertIsNone(self.again(said="可能已经送达"))
        self.assertEqual(attempt.call_args.args[2].category, "reconciliation_required")
        self.upsert.assert_not_called()

    def test_the_original_text_goes_back_on_the_card_else_as_a_message_else_as_a_file(self):
        # One way for the first send (which paints its task card first) and a redelivery (no card to paint).
        from covey.app import saved_send
        calls = []
        artifacts = SimpleNamespace(send_result_file=lambda ctx, job, text: calls.append(("file", text)) or True)

        def send(ctx, chat, text, key):
            calls.append(("message", key))
            return False

        def restore(**more):
            calls.clear()
            return saved_send.restore_original(self.ctx, SimpleNamespace(chat_id="oc_1"), "原文", "全文", key="k",
                                               send=send, artifacts=artifacts, log=lambda line: None, **more)
        self.assertTrue(restore(paint=lambda: True))
        self.assertEqual(calls, [])
        self.assertTrue(restore(paint=lambda: False))
        self.assertEqual(calls, [("message", "k"), ("file", "全文")])
        self.assertTrue(restore())
        self.assertEqual(calls, [("message", "k"), ("file", "全文")])
        self.assertFalse(restore(paint=lambda: 1 / 0))  # Never raises: not restored.


if __name__ == "__main__":
    unittest.main()
