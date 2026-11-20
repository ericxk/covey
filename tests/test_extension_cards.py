"""核心里留给扩展功能的几处：功能对象认领消息和点击、扩展的卡片 kind；下线功能的旧卡片和环境变量的处理。"""
from __future__ import annotations

import unittest
from unittest import mock

from support.world import UnifiedCase
from covey.app.card_actions import ACTIONS, BOT_ONLY, moved_card_text
from covey.core import extensions


def fake_manifest(**fields):
    base = dict(harnesses={}, providers=(), retired_prefixes={}, retired_harnesses={})
    return extensions.Extension("fake_ext", **{**base, **fields})


class ExtensionTablesTest(unittest.TestCase):
    def test_card_kinds_and_package_settings_come_from_the_enabled_manifests(self):
        found = fake_manifest(card_kinds={"bridge_fake": "假按钮"}, settings={"fake_ext.model": "m1"})
        with mock.patch.object(extensions, "manifests", lambda: (found,)):
            self.assertEqual(extensions.card_kinds(), {"bridge_fake": "假按钮"})
            self.assertEqual(extensions.setting("fake_ext.model"), "m1")
            self.assertEqual(extensions.setting("fake_ext.other", "x"), "x")
            with self.assertRaises(KeyError):
                extensions.setting("unknown")

    def test_a_setting_of_another_package_is_refused_in_a_manifest(self):
        import sys
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as root:
            package = Path(root) / "bad_ext"
            package.mkdir()
            (package / "__init__.py").write_text("")
            (package / "manifest.py").write_text('SETTINGS = {"other_ext.x": "1"}\n')
            sys.path.insert(0, root)
            self.addCleanup(sys.path.remove, root)
            with self.assertRaisesRegex(extensions.ExtensionError, "SETTINGS"):
                extensions.read("bad_ext")

    def test_a_variable_of_a_retired_feature_is_named_and_ignored(self):
        (line,) = extensions.unused_env({"COVEY_PASEO_FLEET": "/absolute/fleet.json"})
        self.assertIn("COVEY_PASEO_FLEET", line)
        self.assertIn("Paseo was taken out", line)
        self.assertEqual(len(extensions.unused_env({"LARK_BRIDGE_PASEO_FLEET": "/absolute/fleet.json"})), 1)
        self.assertEqual(extensions.unused_env({}), [])

    def test_the_text_for_a_retired_card_covers_its_kind_and_its_forms_buttons(self):
        self.assertIn("Paseo 已下线", moved_card_text({"action_value": {"kind": "bridge_paseo"}}))
        self.assertIn("Paseo 已下线", moved_card_text({"action_name": "bridge_paseo_" + "a" * 24}))
        self.assertEqual(moved_card_text({"action_value": {"kind": "bridge_unknown"}}), "")


class ExtensionFeatureRoutesTest(UnifiedCase):
    def test_a_features_message_click_and_close_hooks_are_called(self):
        calls = []

        class Thing:
            def handle_event(self, ctx, event, owner):
                calls.append(("event", owner))
                return True

            def claim(self, ctx, event, raw, owner):
                calls.append(("claim", raw.get("kind")))
                return "fake_claimed" if raw.get("kind") == "bridge_fake" else None

            def close(self):
                calls.append(("close",))

        self.app.extension_features = (Thing(),)
        self.app.handle_event(self.event("m-ext", "hello"), self.ctx)
        self.assertEqual(calls, [("event", True)])
        click = {"operator_id": "owner", "chat_id": "chat", "message_id": "om_card", "event_id": "e1",
                 "action_value": {"kind": "bridge_fake"}}
        self.assertEqual(self.app._handle_card_action_event(click, self.ctx), "fake_claimed")
        self.app.stop_children()
        self.assertEqual(calls[-1], ("close",))

    def test_an_old_card_of_a_retired_feature_gets_a_notice_and_nothing_runs(self):
        self.app.extension_features = ()
        with mock.patch.dict(ACTIONS), mock.patch.dict("covey.app.card_actions.ACTIONS"):
            ACTIONS.pop("bridge_paseo", None)
            self.app.messenger.reply = mock.Mock(return_value=True)
            click = {"operator_id": "owner", "chat_id": "chat", "message_id": "om_card", "event_id": "e2",
                     "action_value": {"kind": "bridge_paseo", "token": "t" * 24}}
            self.assertEqual(self.app._handle_card_action_event(click, self.ctx), "extension_missing")
            form = {"operator_id": "owner", "chat_id": "chat", "message_id": "om_card", "event_id": "e3",
                    "action_name": "bridge_paseo_" + "a" * 24}
            self.assertEqual(self.app._handle_card_action_event(form, self.ctx), "extension_missing")
        self.assertEqual(self.app.messenger.reply.call_count, 2)
        self.assertIn("已下线", self.app.messenger.reply.call_args.args[2])

    def test_the_cores_own_kinds_do_not_depend_on_the_extension_tables(self):
        self.assertNotIn("bridge_fake", BOT_ONLY)


if __name__ == "__main__":
    unittest.main()
