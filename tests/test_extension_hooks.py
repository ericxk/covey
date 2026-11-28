"""The extension hooks and settings (core/extensions.py) and what the core does without them: no 代答 entrance
without a sender, an installer with systemd only, the default web host; a retired hook a manifest still names."""
from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from covey.core import extensions
from support import requires_extension


def manifest_of(**fields):
    return extensions.Extension("x", {}, (), {}, {}, **fields)


class HookTests(unittest.TestCase):
    def test_a_manifest_names_only_known_hooks_and_settings(self):
        fake = SimpleNamespace(HOOKS={"nope": "x.y"})
        with mock.patch.object(extensions, "_import", return_value=fake), self.assertRaises(extensions.ExtensionError):
            extensions.read("x")
        fake = SimpleNamespace(SETTINGS={"web_tenant_domain": 3})
        with mock.patch.object(extensions, "_import", return_value=fake), self.assertRaises(extensions.ExtensionError):
            extensions.read("x")
        fake = SimpleNamespace(HOOKS={"proxy_sender": "x.y"}, SETTINGS={"web_tenant_domain": "t.example.com"})
        with mock.patch.object(extensions, "_import", return_value=fake):
            read = extensions.read("x")
        self.assertEqual((read.hooks, read.settings), ({"proxy_sender": "x.y"}, {"web_tenant_domain": "t.example.com"}))

    def test_a_retired_hook_still_loads_and_is_never_asked_for(self):
        # The web dashboard's fixed entry, taken out 2026-11-19: an extension that still names it starts as before.
        fake = SimpleNamespace(HOOKS={"dashboard_entry": "x.dashboard_entry"})
        with mock.patch.object(extensions, "_import", return_value=fake):
            read = extensions.read("x")
        self.assertNotIn("dashboard_entry", extensions.HOOK_NAMES)
        with mock.patch.object(extensions, "manifests", return_value=(read,)), self.assertRaises(KeyError):
            extensions.hook("dashboard_entry")

    def test_hooks_gives_every_package_that_has_the_hook_in_order(self):
        one, two = extensions.Extension("one", {}, (), {}, {}, hooks={"deploy_check": "json"}), manifest_of()
        three = extensions.Extension("three", {}, (), {}, {}, hooks={"deploy_check": "string"})
        with mock.patch.object(extensions, "manifests", return_value=(one, two, three)):
            self.assertEqual([(package, module.__name__) for package, module in extensions.hooks("deploy_check")],
                             [("one", "json"), ("three", "string")])
            self.assertEqual(extensions.hook("deploy_check").__name__, "json")
            with self.assertRaises(KeyError):
                extensions.hooks("other")
        with mock.patch.object(extensions, "manifests", return_value=()):
            self.assertEqual(extensions.hooks("deploy_check"), [])

    def test_without_an_extension_there_is_no_hook_and_the_default_stands(self):
        with mock.patch.object(extensions, "manifests", return_value=()):
            self.assertIsNone(extensions.hook("proxy_sender"))
            self.assertEqual(extensions.setting("web_tenant_domain", "open.feishu.cn"), "open.feishu.cn")
            with self.assertRaises(KeyError):
                extensions.hook("other")

    def test_the_first_package_that_sets_a_setting_wins(self):
        one, two = manifest_of(settings={"web_tenant_domain": "a.example.com"}), manifest_of(settings={"web_tenant_domain": "b"})
        with mock.patch.object(extensions, "manifests", return_value=(one, two)):
            self.assertEqual(extensions.setting("web_tenant_domain"), "a.example.com")

    @requires_extension()
    def test_the_company_extension_provides_every_hook(self):
        for name in set(extensions.HOOK_NAMES) - OWN_HOOKS:
            self.assertIsNotNone(extensions.hook(name), name)
        self.assertRegex(extensions.setting("web_tenant_domain") or "", r"^[a-z0-9-]+(\.[a-z0-9-]+)+$")  # A tenant's host.


# A deployment's own package gives these (its own model sources, docs/architecture.md 扩展), not the company extension;
# agy_turn too since agy 多账号 moved into the core (backends/agy/accounts.py, T831): the hook stays for other packages.
OWN_HOOKS = {"claude_models", "agy_turn"}


class SearchRulesTests(unittest.TestCase):
    """5.9 rows 12 and 13: the rules are in Chinese and the core says one sentence about searches; a deployment on a
    shared disk gives its full lines through an extension's SETTINGS ``search_rules`` and ``search_reminder``."""

    def test_the_core_says_one_sentence_and_an_extension_its_lines(self):
        from covey.app import prompts
        with mock.patch.object(extensions, "manifests", return_value=()):
            rules = prompts.constraints("unit")
            self.assertIn("\n- 搜索限定目录并设超时。\n", rules)
            for word in ("NAS", "PGID", "PID 1", "rg --files"):
                self.assertNotIn(word, rules)
            self.assertIn("\n- 搜索限定目录并设超时。\n", prompts.proxy_constraints("unit"))
            self.assertEqual(prompts.resume_reminders(), prompts.RESUME_REMINDERS)
            self.assertIn("\n搜索限定目录并设超时。\n", prompts.RESUME_REMINDERS)
        lines = "- 别把整个 NAS 扫一遍\n按进程组停命令（`kill -TERM -- -<PGID>`）\n  接上一条的第二行"
        ext = manifest_of(settings={"search_rules": lines, "search_reminder": "限定目录、设超时；按进程组停。"})
        with mock.patch.object(extensions, "manifests", return_value=(ext,)):
            rules = prompts.constraints("unit")
            self.assertIn("\n- 别把整个 NAS 扫一遍\n- 按进程组停命令（`kill -TERM -- -<PGID>`）\n  接上一条的第二行\n", rules)
            self.assertNotIn(prompts.SEARCH_RULE, rules)
            self.assertIn("\n- 别把整个 NAS 扫一遍\n", prompts.proxy_constraints("unit"))
            self.assertIn("\n限定目录、设超时；按进程组停。\n", prompts.resume_reminders())
            self.assertNotIn(prompts.SEARCH_RULE, prompts.resume_reminders())

    def test_a_manifest_may_set_them(self):
        fake = SimpleNamespace(SETTINGS={"search_rules": "- x", "search_reminder": "y"})
        with mock.patch.object(extensions, "_import", return_value=fake):
            self.assertEqual(extensions.read("x").settings, {"search_rules": "- x", "search_reminder": "y"})

    def test_the_rules_are_in_one_language(self):
        from covey.app import prompts
        from covey.backends.claude.bridge import ClaudeBackend
        with mock.patch.object(extensions, "manifests", return_value=()):
            texts = (prompts.constraints("Claude Code", "Claude Code 权限模式：default", ("说明",)),
                     prompts.proxy_constraints("Claude Code"), prompts.RESUME_REMINDERS,
                     prompts.workspace_block("ws", Path(".")), prompts.first_turn_message("消息", []),
                     prompts.resume_message("消息"))
        for text in texts:
            for english in ("You are", "Constraints for", "Reply only", "Scope every search", "process group",
                            "Current workdir", "Recent conversation", "Latest user message", "Read and follow"):
                self.assertNotIn(english, text)
        backend = ClaudeBackend()
        ctx = SimpleNamespace(skip_permissions=False, options={}, timeout=1800, topic_id="")
        self.assertNotIn("permission", backend.prompt_runtime_note(ctx))
        self.assertNotIn("Background", backend.prompt_notes(ctx)[0])


class WithoutHooksTests(unittest.TestCase):
    """The same checks with the hooks taken away, whether or not an extension is enabled."""

    def test_no_sender_means_no_proxy_entrance(self):
        from covey.app import proxy_send
        from covey.app.menu_panel import hub_tabs
        from covey.app.proxy import Proxy
        from covey.cards.settings_hub import PROXY
        with mock.patch.object(extensions, "hook", return_value=None):
            self.assertFalse(proxy_send.available())
            self.assertNotIn(PROXY, hub_tabs())
            proxy = Proxy.__new__(Proxy)
            self.assertEqual((proxy.threads(), proxy.menu_items(), proxy.menu_folds()), ((), {}, {}))
            web = proxy_send.ProxySend(proxy)
            self.assertEqual(web.web_send(None, {}, {}, {}, "x")["submitted"], False)
            self.assertEqual(web.problems(None, {}), [proxy_send.NO_SENDER])
            login = proxy_send.NullLogin()
            self.assertIsNone(login.check_login(None, {}))

    def test_no_hosting_hook_means_the_core_modes_only(self):
        from covey.service import install
        seen = {}

        def parse(self, *args, **kwargs):
            seen["modes"] = [a for a in self._actions if a.dest == "mode"][0].choices
            raise SystemExit(0)
        with mock.patch.object(extensions, "hook", return_value=None), \
                mock.patch.object(argparse.ArgumentParser, "parse_args", parse), self.assertRaises(SystemExit):
            install.main()
        self.assertEqual(seen["modes"], ["systemd", "launchd", "cron", "manager", "auto"])

    def test_the_web_host_defaults_to_the_open_platform(self):
        import importlib
        from covey.feishu import web_client
        try:
            with mock.patch.object(extensions, "manifests", return_value=()), \
                    mock.patch.dict("os.environ", {web_client.HOST_ENV: ""}):
                importlib.reload(web_client)
                self.assertEqual(web_client.BASE, "https://open.feishu.cn")
                self.assertTrue(web_client.on_messenger("https://acme.feishu.cn/next/messenger/"))
                self.assertFalse(web_client.on_messenger("https://acme.feishu.cn/accounts/login"))
        finally:
            importlib.reload(web_client)

    def test_the_contact_notes_have_no_place_by_default(self):
        from covey.core import proxy as records
        with mock.patch.object(extensions, "manifests", return_value=()):
            self.assertEqual(records.notes_dir(Path("/w"), {"kind": "p2p", "key": "k"}, "20260101"), "")


if __name__ == "__main__":
    unittest.main()
