"""The skills the repository ships (skills/<name>/: SKILL.md and agents/openai.yaml) and that scripts/install_skills.sh syncs."""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SKILLS = sorted(path for path in (REPO / "skills").iterdir() if path.is_dir())


def front_matter(path):
    text = path.read_text(encoding="utf-8")
    match = re.match(r"---\n(.*?)\n---\n", text, re.S)
    return dict(line.split(": ", 1) for line in match.group(1).splitlines()) if match else {}


class ShapeTest(unittest.TestCase):
    def test_each_skill_has_its_name_a_description_and_an_interface(self):
        self.assertIn("change-walkthrough", [path.name for path in SKILLS])
        for folder in SKILLS:
            with self.subTest(skill=folder.name):
                head = front_matter(folder / "SKILL.md")
                self.assertEqual(head.get("name"), folder.name)
                self.assertGreater(len(head.get("description", "")), 80)
                interface = (folder / "agents" / "openai.yaml").read_text(encoding="utf-8")
                self.assertIn(f"${folder.name}", interface)

    def test_the_walkthrough_names_its_four_parts_and_where_it_writes_and_lists_the_files(self):
        text = (REPO / "skills" / "change-walkthrough" / "SKILL.md").read_text(encoding="utf-8")
        for words in ("改了什么、为什么改", "关键流程", "怎么验证", "从哪里接上", "artifacts", "绝对路径", "输出", "单文件静态页"):
            self.assertIn(words, text)

    def test_the_docs_where_skills_are_synced_name_both(self):
        for doc in ("docs/deploy.md", "docs/install.md"):
            text = (REPO / doc).read_text(encoding="utf-8")
            for folder in SKILLS:
                self.assertIn(folder.name, text, f"{doc} does not name {folder.name}")


class InstallTest(unittest.TestCase):
    def test_the_install_script_copies_every_skill_to_both_homes(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "CODEX_HOME": f"{tmp}/codex", "CLAUDE_HOME": f"{tmp}/claude"}
            done = subprocess.run(["bash", str(REPO / "scripts" / "install_skills.sh")], env=env, capture_output=True,
                                  text=True, timeout=60, stdin=subprocess.DEVNULL)
            self.assertEqual(done.returncode, 0, done.stderr)
            for home in ("codex", "claude"):
                for folder in SKILLS:
                    self.assertTrue((Path(tmp) / home / "skills" / folder.name / "SKILL.md").is_file(), (home, folder.name))


if __name__ == "__main__":
    unittest.main()
