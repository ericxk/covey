"""五分钟上手 (docs/quickstart.md): one page a new owner reads and the butler reads first when asked how to use Covey
(app/prompts.py USAGE_RULE names the running release's own copy). Before T604 the butler had nothing to read for that
and could only search the code or guess."""
from __future__ import annotations

import unittest
from pathlib import Path

from covey.app import prompts

REPO = Path(__file__).resolve().parent.parent
QUICKSTART = REPO / "docs" / "quickstart.md"


class QuickstartTest(unittest.TestCase):
    def test_the_page_the_butler_reads_is_one_short_page_linked_from_the_readme_and_the_usage_guide(self):
        text = prompts.QUICKSTART.read_text(encoding="utf-8")
        self.assertLessEqual(len(text.splitlines()), 150)
        for words in ("看板在哪", "怎么开记忆", "COVEY_MEMORY=0", "install.md"):
            self.assertIn(words, text)
        self.assertIn("docs/quickstart.md", (REPO / "README.md").read_text(encoding="utf-8"))
        self.assertIn("(quickstart.md)", (REPO / "docs" / "usage.md").read_text(encoding="utf-8"))

    def test_it_says_what_is_special_by_kind_without_naming_other_products(self):
        """T647 E5: the butler read this page for 能做什么 and it said nothing of projects, 换配置's handoff or restarts."""
        text = QUICKSTART.read_text(encoding="utf-8")
        part = text[text.index("## 它特别在哪"):text.index("## 日常怎么用")]
        for words in ("管家", "专项", "负责人", "换引擎不断上下文", "交接摘要", "重启不断任务", "提到全局", "回退"):
            self.assertIn(words, part)
        self.assertIn("能做什么", prompts.USAGE_RULE)

    def test_the_butler_reads_it_first_where_it_runs(self):
        self.assertEqual(prompts.QUICKSTART, QUICKSTART)
        self.assertTrue(prompts.QUICKSTART.is_absolute() and prompts.QUICKSTART.is_file())
        self.assertIn(f"先读 {QUICKSTART} 再答", prompts.butler_rules())
        self.assertEqual(1, prompts.butler_rules().count(str(QUICKSTART)))


if __name__ == "__main__":
    unittest.main()
