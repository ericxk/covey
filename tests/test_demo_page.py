"""The three-step demo page (docs/demo/index.html, scripts/card_sketches.py --demo): the card samples as a clickable static
page, 派到话题 → 回答一道题 → 看答案和产物, its header saying it is demo data. Before T604 the README had only five pictures."""
from __future__ import annotations

import importlib.util
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("card_sketches_script", REPO / "scripts" / "card_sketches.py")
sketches = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sketches)


class DemoPageTest(unittest.TestCase):
    def test_three_steps_of_real_cards_an_answer_for_each_option_and_nothing_from_elsewhere(self):
        page = sketches.demo_page()
        for words in (*sketches.STEPS, sketches.DEMO_NOTE, "要你定：登录后", "有结果", "产物 2 个", "login-page.diff"):
            self.assertIn(words, page)
        self.assertEqual(re.findall(r'id="step-(\d)"', page), ["1", "2", "3"])
        self.assertEqual(sorted(re.findall(r'data-answer="([^"]+)"', page)),
                         sorted(option["label"] for option in sketches.OPTIONS))
        self.assertNotRegex(page, r"<(script|link|img)[^>]+(src|href)=")  # One file: no script, style or font from elsewhere.

    def test_the_committed_page_is_the_one_the_readme_links(self):
        committed = sketches.DEMO_PAGE.read_text(encoding="utf-8")
        for words in (*sketches.STEPS, sketches.DEMO_NOTE):
            self.assertIn(words, committed)
        self.assertIn("(docs/demo/index.html)", (REPO / "README.md").read_text(encoding="utf-8"))
        self.assertIn("(docs/demo/index.html)", (REPO / "README.en.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
