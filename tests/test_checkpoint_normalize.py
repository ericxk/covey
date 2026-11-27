"""A checkpoint's questions made into what a question card takes (W1-F step 7, T891 B5; core/checkpoint.py
normalize_questions, question_summary, needs_owner): what a model writes loosely is asked, what is already right comes
back as it was, a secret is never asked."""
from __future__ import annotations

import json
import unittest

from covey.core.checkpoint import needs_owner, normalize_questions, question_summary


class NormalizeTest(unittest.TestCase):
    def test_questions_already_right_come_back_as_they_were(self):
        questions = [{"id": "plan", "header": "方案", "question": "按 A 做？", "kind": "deploy", "context": "背景",
                      "multiple": False, "options": [{"label": "是", "description": "照做", "sample": "/a.png"},
                                                     {"label": "否"}]},
                     {"id": "note", "question": "还有要补充的吗？"}]
        before = json.dumps(questions, ensure_ascii=False)
        made = normalize_questions(questions, "按 A 做？")
        self.assertEqual(made, questions)
        self.assertEqual(json.dumps(made, ensure_ascii=False), before)  # Field for field, in order.
        self.assertEqual(json.dumps(questions, ensure_ascii=False), before)  # The input left as it was.

    def test_ids_a_number_none_or_a_repeat(self):
        made = normalize_questions([{"id": 3, "question": "a"}, {"question": "b"}, {"id": "", "question": "c"},
                                    {"id": "x", "question": "d"}, {"id": "x", "question": "e"}, {"id": True, "question": "f"}], "")
        self.assertEqual([q["id"] for q in made], ["3", "q2", "q3", "x", "x-2", "q6"])

    def test_a_string_is_the_question_and_header_stands_for_empty_words(self):
        made = normalize_questions(["选哪个？", {"id": "h", "header": "范围", "question": " "}, {"id": "gone", "question": ""}], "")
        self.assertEqual([(q["id"], q["question"]) for q in made], [("q1", "选哪个？"), ("h", "范围")])
        self.assertEqual(made[1]["header"], "范围")

    def test_options_a_string_text_or_title_for_the_label_and_none_dropped(self):
        made = normalize_questions([{"id": "a", "question": "?", "options": [
            "甲", {"text": "乙", "description": "第二"}, {"title": "丙"}, {"description": "没名字"}, 7, None, True]}], "")
        self.assertEqual(made[0]["options"], [{"label": "甲"}, {"description": "第二", "label": "乙"}, {"label": "丙"},
                                              {"label": "7"}])
        none_left = normalize_questions([{"id": "b", "question": "?", "options": [{"description": "x"}]}], "")
        self.assertNotIn("options", none_left[0])  # A free answer then.
        self.assertEqual(normalize_questions([{"id": "c", "question": "?", "options": []}], "")[0]["options"], [])

    def test_multi_select_spellings_say_multiple(self):
        made = normalize_questions([{"id": "a", "question": "?", "multiSelect": True},
                                    {"id": "b", "question": "?", "multi_select": True},
                                    {"id": "c", "question": "?", "multiSelect": "yes"},
                                    {"id": "d", "question": "?", "multiple": False, "multiSelect": True}], "")
        self.assertEqual([q.get("multiple") for q in made], [True, True, False, False])
        self.assertFalse(any("multiSelect" in q or "multi_select" in q for q in made))

    def test_a_secret_is_never_asked(self):
        made = normalize_questions([{"id": "pw", "question": "密码？", "isSecret": True}, {"id": "ok", "question": "继续？"}], "")
        self.assertEqual([q["id"] for q in made], ["ok"])
        with self.assertRaises(ValueError):
            normalize_questions([{"id": "pw", "question": "密码？", "isSecret": True}], "要密码")

    def test_no_list_is_one_free_question_of_the_summary(self):
        for questions in (None, [], {"id": "a"}, "选哪个"):
            with self.subTest(questions=questions):
                self.assertEqual(normalize_questions(questions, " 按 A 做？ "),
                                 [{"id": "answer", "header": "补充信息", "question": "按 A 做？"}])
        with self.assertRaises(ValueError):
            normalize_questions([], "")
        with self.assertRaises(ValueError):
            normalize_questions(["", 5, None], "按 A 做？")  # A list with nothing to ask: refused, as before.


class OwnerTest(unittest.TestCase):
    def test_question_summary(self):
        self.assertEqual(question_summary({"question": "按 A 做？", "questions": [{"id": "a", "question": "别的"}]}), "按 A 做？")
        self.assertEqual(question_summary({"questions": [{"id": 1, "header": "范围"}, "继续？"]}), "范围\n继续？")
        self.assertEqual(question_summary({"questions": [{"isSecret": True, "question": "密码"}]}), "")
        self.assertEqual(question_summary({}), "")

    def test_needs_owner(self):
        self.assertTrue(needs_owner({"requires_user": True, "question": "按 A 做？"}))
        self.assertTrue(needs_owner({"requires_user": True, "questions": [{"id": "a", "question": "按 A 做？"}]}))
        self.assertFalse(needs_owner({"requires_user": True}))  # Waited with no card to answer.
        self.assertFalse(needs_owner({"requires_user": True, "question": "  ", "questions": []}))
        self.assertFalse(needs_owner({"requires_user": "true", "question": "按 A 做？"}))
        self.assertFalse(needs_owner({"question": "按 A 做？"}))
        self.assertFalse(needs_owner(None))


if __name__ == "__main__":
    unittest.main()
