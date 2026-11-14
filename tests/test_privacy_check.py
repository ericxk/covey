"""scripts/privacy_check.py：规则命中、行内和整文件豁免、清单文件的读取，以及核心集合要干净。"""
from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

from support import REPO

SPEC = importlib.util.spec_from_file_location("privacy_check", REPO / "scripts" / "privacy_check.py")
check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check)

DENYLIST = REPO / "extensions" / "covey_internal" / "privacy-denylist.txt"


def rules(text, denylist=(), rel=""):
    return [rule for _, rule in check.scan_text(text, denylist, rel)]


class RuleTest(unittest.TestCase):
    def test_each_general_rule_hits(self):
        samples = {
            "飞书 open_id": "ou_" + "a1" * 16,
            "飞书 union_id": "on_" + "b2" * 16,
            "飞书 chat_id": "oc_" + "c3" * 16,
            "飞书消息 id": "om_" + "d4" * 16,
            "飞书 App ID": "cli_" + "a9" * 8,
            "私网 IP": "host 10.1.2.3 up",
            "令牌形状": "key sk-" + "x" * 24,
            "个人或公司路径 /mnt/bn": "/mnt/bn/team/data",
            "个人或公司路径 /opt/tiger": "/opt/tiger/entry.py",
            "个人路径 /home/<人名>": "/home/alice/work",
            "真实邮箱": "mail alice@corp.io now",
        }
        for rule, text in samples.items():
            with self.subTest(rule=rule):
                self.assertIn(rule, rules(text))

    def test_placeholders_and_ordinary_text_pass(self):
        for text in ("ou_xxx and oc_test", "bob@example.com", "git@github.com:me/repo.git", "/home/user/x /home/tiger/y",
                     "pkg@1.5.2 and @scope/name@0.15.0", "http://127.0.0.1:8080", "8.8.8.8", "192.0.2.5"):
            with self.subTest(text=text):
                self.assertEqual(rules(text), [])

    def test_a_line_exemption_needs_a_reason(self):
        self.assertEqual(rules("/mnt/bn/x  # privacy: allow 测试里的挂载点"), [])
        self.assertEqual(rules("/mnt/bn/x  # privacy: allow"), ["个人或公司路径 /mnt/bn"])

    def test_the_file_exemption_only_covers_denylist_terms(self):
        denylist = [("内部名词 acmecorp", check.re.compile(r"acmecorp", check.re.I), ())]
        text = "# privacy: allow-file 测平台接口\nacmecorp and /mnt/bn/x"
        self.assertEqual(rules(text, denylist), ["个人或公司路径 /mnt/bn"])


class DenylistTest(unittest.TestCase):
    def write(self, body):
        handle = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8")
        self.addCleanup(os.unlink, handle.name)
        handle.write(body)
        handle.close()
        return handle.name

    def test_terms_match_whole_words_without_case_and_comments_are_skipped(self):
        denylist = check.load_denylist(self.write("# c\n\nAcmeCorp\nsecret-box | skip=tests/\n"))
        self.assertEqual(rules("see ACMECORP docs", denylist), ["内部名词 AcmeCorp"])
        self.assertEqual(rules("acmecorpx and xacmecorp", denylist), [])
        self.assertEqual(rules("a secret-box", denylist), ["内部名词 secret-box"])
        self.assertEqual(rules("a secret-box", denylist, "tests/test_x.py"), [])

    def test_no_denylist_means_only_the_general_rules(self):
        self.assertEqual(check.load_denylist(None), [])

    def test_run_reports_file_line_and_rule(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "a.md").write_text("ok\nsee acmecorp\n")
            (Path(root) / "docs/history").mkdir(parents=True)
            (Path(root) / "docs/history/old.md").write_text("acmecorp\n")
            deny = self.write("acmecorp\n")
            self.assertEqual(check.run(["a.md", "docs"], deny, Path(root)), ["a.md:2: 内部名词 acmecorp"])
            self.assertEqual(check.run(["a.md"], None, Path(root)), [])


class CoreTest(unittest.TestCase):
    def test_the_core_set_is_clean_by_the_general_rules(self):
        self.assertEqual(check.run(check.CORE, None), [])

    @unittest.skipUnless(DENYLIST.is_file(), "the company extension's denylist is not in this tree")
    def test_the_core_set_is_clean_with_the_extensions_denylist(self):
        self.assertEqual(check.run(check.CORE, str(DENYLIST)), [])


if __name__ == "__main__":
    unittest.main()
