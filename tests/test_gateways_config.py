"""模型网关的配置（core/gateways.py）：harnesses.json 的 gateways、校验、冲突，以及没有网关时 dsh 和供应商的样子。"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from covey.backends.dsh.bridge import DshBackend, NO_GATEWAY
from covey.core import gateways, providers
from covey.core.extensions import ExtensionError

OWN = {"display": "MyGW", "prefix": "mygw/", "base_url": "https://gw.example/", "key_env": "MYGW_KEY",
       "headers": {"x-source": "bridge"}, "harnesses": ["codex", "dsh"], "quota_pattern": "over quota",
       "busy_hint": "看一下用量页"}


class ConfiguredGatewayTest(unittest.TestCase):
    def tearDown(self):
        gateways.configure(None)
        gateways.forget()

    def test_a_gateway_from_harnesses_json_becomes_a_provider_and_owns_its_prefix(self):
        before = {g.name for g in gateways.all_gateways()}
        gateways.configure({"mygw": OWN})
        gw = gateways.named("mygw")
        self.assertEqual((gw.display, gw.url(), gw.header_dict()), ("MyGW", "https://gw.example", {"x-source": "bridge"}))
        self.assertIs(gateways.for_model("codex", "mygw/gpt-6").name == "mygw", True)
        self.assertIsNone(gateways.for_model("pi", "mygw/gpt-6"))  # Not declared for Pi.
        self.assertEqual(providers.provider_of("codex", "mygw/gpt-6").name, "MyGW")
        self.assertEqual(providers.menu_name(providers.provider_of("dsh", "x"), "DeepSeek Harness"),
                         "MyGW" if "acme" not in before else providers.menu_name(providers.provider_of("dsh", "x"), ""))
        self.assertTrue(gw.quota_text("403: Over Quota"))
        self.assertTrue(gateways.quota_text("403: Over Quota"))
        self.assertIn("MyGW 个人密钥未配置：写入 ~/.config/harness-bridge/mygw-api-key（权限 600）或设置 MYGW_KEY", gw.missing_key)

    def test_the_key_file_default_is_under_the_config_dir(self):
        gw = gateways.parse("mygw", OWN, "")
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "/xdg"}):
            self.assertEqual(gw.key_path(), Path("/xdg/harness-bridge/mygw-api-key"))
        with mock.patch.dict(os.environ, {"MYGW_FILE": "/k"}):
            self.assertEqual(gateways.parse("g", {**OWN, "key_file_env": "MYGW_FILE"}, "").key_path(), Path("/k"))

    def test_bad_entries_are_refused(self):
        for fields in ({**OWN, "extra": 1}, {**OWN, "base_url": "gw.example"}, {**OWN, "key_env": ""},
                       {**OWN, "prefix": ""}, {**OWN, "harnesses": ["claude"]}, {**OWN, "quota_pattern": "("},
                       {**OWN, "headers": {"a": 1}}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                gateways.parse("mygw", fields, "")
        with self.assertRaises(ValueError):
            gateways.parse("Bad Name", OWN, "")
        with self.assertRaises(ValueError):
            gateways.configure(["not", "an", "object"])

    def test_a_prefix_or_name_the_extension_has_is_refused(self):
        from covey.core.extensions import Extension
        fake = Extension("fake_ext", {}, (), {}, {}, gateways={"taken": {**OWN, "prefix": "taken/"}})
        first = gateways.parse("taken", fake.gateways["taken"], "fake_ext")
        patch = mock.patch.object(gateways, "manifests", return_value=(fake,))
        patch.start()
        self.addCleanup(patch.stop)
        for name, fields in ((first.name, OWN), ("other", {**OWN, "prefix": first.prefix, "harnesses": ["codex"]})):
            with self.subTest(name=name):
                gateways.configure({name: fields})
                with self.assertRaises((ValueError, ExtensionError)):
                    gateways.all_gateways()
                gateways.configure(None)


class NoGatewayTest(unittest.TestCase):
    def setUp(self):
        self.patch = mock.patch.object(gateways, "all_gateways", return_value=())
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_without_a_gateway_there_is_no_gateway_provider_and_dsh_has_no_models(self):
        self.assertEqual([p for p in providers.providers() if p.key == "acme"], [])
        self.assertEqual(providers.provider_of("codex", "acme/gpt-6").prefix, "")  # The Harness's own.
        self.assertIsNone(gateways.dsh_gateway())
        with tempfile.TemporaryDirectory() as root:
            ctx = mock.Mock(state_dir=Path(root))
            self.assertEqual(DshBackend().model_catalog(ctx), {})
            with self.assertRaisesRegex(ValueError, NO_GATEWAY[:12]):
                DshBackend().refresh_model_catalog(ctx)


if __name__ == "__main__":
    unittest.main()
