"""The extension entry (core/extensions.py): which packages are enabled, what their manifests merge into the
Harness roster and the provider table, and the features they add. A fake package in a temp dir; with none enabled
the tables are the core's."""
from __future__ import annotations

import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from support.world import UnifiedCase
from covey.core import extensions, providers, roster

PACKAGE = "fake_covey_ext"
MANIFEST = f'''
HARNESSES = {{
    "fakeh": {{"module": "{__name__}", "store": "FakeStore", "live_questions": True}},
    "fakep": {{"module": "{__name__}", "store": "FakeStore", "store_module": "x.y", "paused_questions": True}},
}}
PROVIDERS = [
    {{"key": "fakeprov", "harness": "fakeh", "name": "", "prefix": "", "access": "fake login"}},
    {{"key": "fakeprov", "harness": "codex", "name": "Fake", "prefix": "fake/", "access": "fake key", "menu": "Fake menu"}},
]
RETIRED_PREFIXES = {{"gone/": "Gone"}}
RETIRED_HARNESSES = {{"oldfake": "Old Fake"}}
'''
FEATURES = '''
class Thing:
    def __init__(self, app):
        self.app = app

    def threads(self):
        return [("fake-thread", lambda ctx: None)]

    def menu_items(self):
        return {"fake_key": lambda ctx, event: None}


def features(app):
    return [Thing(app)]
'''


class FakeStore:
    pass


def make_package(root: Path, name=PACKAGE, manifest=MANIFEST, features=FEATURES) -> Path:
    directory = root / name.replace(".", "/")
    directory.mkdir(parents=True, exist_ok=True)
    for parent in [directory, *[p for p in directory.parents if root in p.parents]]:
        (parent / "__init__.py").touch()
    if manifest is not None:
        (directory / "manifest.py").write_text(textwrap.dedent(manifest))
    if features is not None:
        (directory / "features.py").write_text(textwrap.dedent(features))
    return directory


class ExtensionCase(unittest.TestCase):
    """A temp 'repository root' and a sys.path / sys.modules that are put back afterwards."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "extensions").mkdir()
        path, modules = list(sys.path), set(sys.modules)
        self.addCleanup(lambda: sys.path.__setitem__(slice(None), path))
        self.addCleanup(lambda: [sys.modules.pop(m) for m in set(sys.modules) - modules])

    def enable_file(self, text):
        (self.root / "extensions" / "enabled").write_text(text)

    def load(self, **environ):
        return extensions.load(environ, self.root)


class EnabledTest(ExtensionCase):
    def test_no_file_and_no_variable_means_no_extensions(self):
        self.assertEqual(extensions.enabled({}, self.root), [])
        self.assertEqual(self.load(), ())

    def test_the_file_skips_comments_and_blank_lines(self):
        self.enable_file("# first\n\n  alpha  \nbeta # trailing\n   \n# gamma\nalpha\n")
        self.assertEqual(extensions.enabled({}, self.root), ["alpha", "beta"])

    def test_the_variable_wins_over_the_file(self):
        self.enable_file("alpha\n")
        self.assertEqual(extensions.enabled({"COVEY_EXTENSIONS": "beta, gamma,,"}, self.root), ["beta", "gamma"])

    def test_an_empty_variable_means_none_even_with_a_file(self):
        self.enable_file("alpha\n")
        self.assertEqual(extensions.enabled({"COVEY_EXTENSIONS": ""}, self.root), [])
        self.assertEqual(self.load(COVEY_EXTENSIONS=""), ())

    def test_a_name_that_is_not_a_package_is_refused(self):
        with self.assertRaisesRegex(extensions.ExtensionError, "not a Python package name"):
            extensions.enabled({"COVEY_EXTENSIONS": "a-b"}, self.root)

    def test_the_search_path_is_the_variable_else_the_repository_extensions_directory(self):
        self.assertEqual(extensions.search_path({}, self.root), [str(self.root / "extensions")])
        both = os.pathsep.join(["/one", "/two"])
        self.assertEqual(extensions.search_path({"COVEY_EXTENSION_PATH": both}, self.root), ["/one", "/two"])

    def test_the_path_goes_in_front_of_sys_path_once(self):
        extensions.prepare_path(["/one", "/two"])
        extensions.prepare_path(["/one", "/two"])
        self.assertEqual(sys.path[:2], ["/one", "/two"])
        self.assertEqual(sys.path.count("/one"), 1)

    def test_nothing_enabled_leaves_sys_path_alone(self):
        before = list(sys.path)
        self.load()
        self.assertEqual(sys.path, before)


class ManifestTest(ExtensionCase):
    def test_the_default_directory_and_the_enabled_file_find_a_package(self):
        make_package(self.root / "extensions")
        self.enable_file(f"{PACKAGE}\n")
        (found,) = self.load()
        self.assertEqual(found.package, PACKAGE)
        self.assertEqual(sorted(found.harnesses), ["fakeh", "fakep"])
        self.assertEqual(found.retired_prefixes, {"gone/": "Gone"})
        self.assertEqual(found.retired_harnesses, {"oldfake": "Old Fake"})
        self.assertEqual([p["key"] for p in found.providers], ["fakeprov", "fakeprov"])

    def test_the_path_variable_finds_a_package_elsewhere(self):
        elsewhere = self.root / "elsewhere"
        make_package(elsewhere)
        (found,) = self.load(COVEY_EXTENSIONS=PACKAGE, COVEY_EXTENSION_PATH=str(elsewhere))
        self.assertEqual(found.package, PACKAGE)

    def test_every_field_of_a_manifest_is_optional(self):
        make_package(self.root / "extensions", manifest="X = 1\n", features=None)
        (found,) = self.load(COVEY_EXTENSIONS=PACKAGE)
        self.assertEqual((found.harnesses, found.providers, found.retired_prefixes, found.retired_harnesses),
                         ({}, (), {}, {}))

    def test_the_small_tables_are_read_and_checked(self):
        make_package(self.root / "extensions", features=None, manifest="""
            WORK_MODES = {"m1": ["One", "first"], "m2": ["Two", "second"]}
            DEFAULT_WORK_MODE = "m1"
            AGENT_DIRS = [".agent"]
            IGNORED_ENV = {"OLD_VAR": ["fakeh", "--model"], "OLD_SWITCH": ["fakeh", [None, "--off"]]}
        """)
        (found,) = self.load(COVEY_EXTENSIONS=PACKAGE)
        self.assertEqual((found.work_modes, found.default_work_mode, found.agent_dirs), (
            {"m1": ("One", "first"), "m2": ("Two", "second")}, "m1", (".agent",)))
        self.assertEqual(found.ignored_env, {"OLD_VAR": ("fakeh", "--model"), "OLD_SWITCH": ("fakeh", (None, "--off"))})
        for number, bad in enumerate(('WORK_MODES = {"m": "One"}', 'WORK_MODES = {"m": ["One", "x"]}\nDEFAULT_WORK_MODE = "other"',
                    'AGENT_DIRS = "x"', 'IGNORED_ENV = {"V": "x"}')):
            package = f"bad_ext_{number}"  # A module is imported once: one package for each manifest.
            make_package(self.root / "extensions", name=package, features=None, manifest=bad)
            with self.assertRaises(extensions.ExtensionError):
                self.load(COVEY_EXTENSIONS=package)

    def test_an_enabled_package_that_cannot_be_imported_names_itself(self):
        with self.assertRaisesRegex(extensions.ExtensionError, "extension 'fake_missing_ext'.*cannot import"):
            self.load(COVEY_EXTENSIONS="fake_missing_ext")

    def test_a_manifest_that_raises_names_its_package(self):
        make_package(self.root / "extensions", manifest="raise ValueError('boom')\n")
        with self.assertRaisesRegex(extensions.ExtensionError, f"extension '{PACKAGE}'.*ValueError: boom"):
            self.load(COVEY_EXTENSIONS=PACKAGE)

    def test_a_package_without_a_manifest_is_an_error_not_skipped(self):
        make_package(self.root / "extensions", manifest=None, features=None)
        with self.assertRaisesRegex(extensions.ExtensionError, f"extension '{PACKAGE}'"):
            self.load(COVEY_EXTENSIONS=PACKAGE)

    def test_a_manifest_of_the_wrong_shape_is_refused(self):
        for text in ('HARNESSES = [1]\n', 'HARNESSES = {"a": {"module": "m"}}\n',
                     'HARNESSES = {"a": {"module": "m", "store": "S", "bogus": 1}}\n', 'PROVIDERS = {"a": 1}\n',
                     'PROVIDERS = [{"key": "k"}]\n'):
            with self.subTest(text=text), tempfile.TemporaryDirectory() as temp:
                make_package(Path(temp), name="fake_shape_ext", manifest=text, features=None)
                with self.assertRaisesRegex(extensions.ExtensionError, "fake_shape_ext"):
                    extensions.load({"COVEY_EXTENSIONS": "fake_shape_ext", "COVEY_EXTENSION_PATH": temp}, self.root)
                sys.modules.pop("fake_shape_ext.manifest", None)
                sys.modules.pop("fake_shape_ext", None)


class FeaturesTest(ExtensionCase):
    def test_a_package_with_a_features_module_adds_its_objects(self):
        make_package(self.root / "extensions")
        with mock.patch.object(extensions, "manifests", lambda: self.load(COVEY_EXTENSIONS=PACKAGE)):
            (thing,) = extensions.features("the app")
        self.assertEqual(thing.app, "the app")
        self.assertEqual(thing.threads()[0][0], "fake-thread")
        self.assertEqual(list(thing.menu_items()), ["fake_key"])

    def test_a_package_without_features_adds_none(self):
        make_package(self.root / "extensions", features=None)
        with mock.patch.object(extensions, "manifests", lambda: self.load(COVEY_EXTENSIONS=PACKAGE)):
            self.assertEqual(extensions.features("app"), [])

    def test_a_features_module_that_fails_names_its_package(self):
        make_package(self.root / "extensions", features="def features(app):\n    raise KeyError('x')\n")
        with mock.patch.object(extensions, "manifests", lambda: self.load(COVEY_EXTENSIONS=PACKAGE)):
            with self.assertRaisesRegex(extensions.ExtensionError, f"extension '{PACKAGE}'.*features"):
                extensions.features("app")

    def test_a_features_module_with_a_broken_import_is_an_error_not_skipped(self):
        make_package(self.root / "extensions", features="import fake_not_installed_dependency\n")
        with mock.patch.object(extensions, "manifests", lambda: self.load(COVEY_EXTENSIONS=PACKAGE)):
            with self.assertRaisesRegex(extensions.ExtensionError, f"extension '{PACKAGE}'"):
                extensions.features("app")

    def test_none_enabled_adds_none(self):
        with mock.patch.object(extensions, "manifests", lambda: ()):
            self.assertEqual(extensions.features("app"), [])


class MergeTest(ExtensionCase):
    def fake(self, **replace):
        make_package(self.root / "extensions")
        (found,) = self.load(COVEY_EXTENSIONS=PACKAGE)
        return found if not replace else extensions.Extension(**{**found.__dict__, **replace})

    def other(self, package, **fields):
        base = dict(harnesses={}, providers=(), retired_prefixes={}, retired_harnesses={})
        return extensions.Extension(package, **{**base, **fields})

    def test_without_extensions_the_tables_are_the_core_ones(self):
        harnesses, retired = roster.merged(roster.CORE_HARNESSES, roster.CORE_RETIRED, ())
        self.assertEqual((harnesses, retired), (roster.CORE_HARNESSES, roster.CORE_RETIRED))
        self.assertEqual((list(harnesses), list(retired)), (list(roster.CORE_HARNESSES), list(roster.CORE_RETIRED)))
        known, prefixes = providers.merged(providers.CORE_PROVIDERS, providers.CORE_RETIRED_PREFIXES, ())
        self.assertEqual((known, prefixes), (providers.CORE_PROVIDERS, providers.CORE_RETIRED_PREFIXES))
        # The module constants are what they were, when no extension is enabled here.
        if not extensions.manifests():
            self.assertEqual((roster.HARNESSES, roster.RETIRED), (roster.CORE_HARNESSES, roster.CORE_RETIRED))
            self.assertEqual((providers.PROVIDERS, providers.RETIRED_PREFIXES),
                             (providers.CORE_PROVIDERS, providers.CORE_RETIRED_PREFIXES))
            self.assertEqual(roster.NAMES, tuple(roster.CORE_HARNESSES))

    def test_the_core_comes_first_and_the_extension_s_entries_follow_in_order(self):
        harnesses, retired = roster.merged(roster.CORE_HARNESSES, roster.CORE_RETIRED, (self.fake(),))
        self.assertEqual(list(harnesses), [*roster.CORE_HARNESSES, "fakeh", "fakep"])
        self.assertEqual(list(retired), [*roster.CORE_RETIRED, "oldfake"])
        self.assertEqual(harnesses["fakeh"], roster.RosterEntry(__name__, "FakeStore", live_questions=True))
        self.assertEqual(harnesses["fakep"].store_module, "x.y")
        self.assertTrue(harnesses["fakep"].paused_questions)
        self.assertEqual(list(harnesses)[0], "codex")  # The default route stays the core's first.
        known, prefixes = providers.merged(providers.CORE_PROVIDERS, providers.CORE_RETIRED_PREFIXES, (self.fake(),))
        self.assertEqual(known[:len(providers.CORE_PROVIDERS)], providers.CORE_PROVIDERS)
        self.assertEqual([(p.key, p.harness, p.prefix, p.menu) for p in known[len(providers.CORE_PROVIDERS):]],
                         [("fakeprov", "fakeh", "", ""), ("fakeprov", "codex", "fake/", "Fake menu")])
        self.assertEqual(list(prefixes), [*providers.CORE_RETIRED_PREFIXES, "gone/"])

    def test_the_derived_sets_come_from_the_merged_table(self):
        harnesses, _ = roster.merged(roster.CORE_HARNESSES, roster.CORE_RETIRED, (self.fake(),))
        live = frozenset(n for n, h in harnesses.items() if h.live_questions)
        self.assertIn("fakeh", live)
        self.assertNotIn("fakep", live)

    def test_a_second_extension_follows_the_first(self):
        second = self.other("second_ext", harnesses={"third": {"module": "m", "store": "S"}})
        harnesses, _ = roster.merged(roster.CORE_HARNESSES, roster.CORE_RETIRED, (self.fake(), second))
        self.assertEqual(list(harnesses)[-3:], ["fakeh", "fakep", "third"])

    def test_an_entry_with_after_is_listed_right_after_that_harness(self):
        entry = {"module": "m", "store": "S", "after": "claude"}
        harnesses, _ = roster.merged(roster.CORE_HARNESSES, roster.CORE_RETIRED,
                                     (self.other("late", harnesses={"later": dict(entry)}),
                                      self.other("missing", harnesses={"nowhere": {**entry, "after": "absent"}})))
        self.assertEqual(list(harnesses), ["codex", "claude", "later", "dsh", "pi", "nowhere"])

    def test_a_harness_name_of_the_core_is_refused_with_the_package_name(self):
        clash = self.other("clash_ext", harnesses={"claude": {"module": "m", "store": "S"}})
        with self.assertRaisesRegex(extensions.ExtensionError, "extension 'clash_ext'.*'claude'.*the core"):
            roster.merged(roster.CORE_HARNESSES, roster.CORE_RETIRED, (clash,))

    def test_a_retired_name_of_the_core_is_refused_too(self):
        clash = self.other("clash_ext", harnesses={"kiro": {"module": "m", "store": "S"}})
        with self.assertRaisesRegex(extensions.ExtensionError, "extension 'clash_ext'.*'kiro'"):
            roster.merged(roster.CORE_HARNESSES, roster.CORE_RETIRED, (clash,))

    def test_a_name_another_extension_has_is_refused_naming_both(self):
        with self.assertRaisesRegex(extensions.ExtensionError, f"extension 'second_ext'.*'fakeh'.*{PACKAGE}"):
            roster.merged(roster.CORE_HARNESSES, roster.CORE_RETIRED,
                          (self.fake(), self.other("second_ext", harnesses={"fakeh": {"module": "m", "store": "S"}})))

    def test_a_provider_on_a_harness_and_prefix_that_is_taken_is_refused(self):
        clash = self.other("clash_ext", providers=({"key": "x", "harness": "codex", "name": "L", "prefix": "",
                                                     "access": "a"},))
        with self.assertRaisesRegex(extensions.ExtensionError, "extension 'clash_ext'.*provider 'x'.*the core"):
            providers.merged(providers.CORE_PROVIDERS, providers.CORE_RETIRED_PREFIXES, (clash,))

    def test_a_retired_prefix_that_is_listed_is_refused(self):
        clash = self.other("clash_ext", retired_prefixes={"traex/": "TRAE"})
        with self.assertRaisesRegex(extensions.ExtensionError, "extension 'clash_ext'.*'traex/'"):
            providers.merged(providers.CORE_PROVIDERS, providers.CORE_RETIRED_PREFIXES, (clash,))

    def test_a_merged_provider_is_found_by_its_prefix(self):
        known, prefixes = providers.merged(providers.CORE_PROVIDERS, providers.CORE_RETIRED_PREFIXES, (self.fake(),))
        with mock.patch.object(providers, "PROVIDERS", known), mock.patch.object(providers, "RETIRED_PREFIXES", prefixes):
            self.assertEqual(providers.provider_of("codex", "fake/model").key, "fakeprov")
            self.assertEqual(providers.provider_of("codex", "gpt").key, "openai")
            self.assertEqual(providers.provider_of("fakeh", "m").key, "fakeprov")
            self.assertEqual(providers.retired_provider("gone/x"), "Gone")


class UnifiedFeaturesTest(UnifiedCase):
    def test_the_extensions_features_come_last_and_add_their_threads_and_menu_items(self):
        class Thing:
            def threads(self):
                return [("fake-thread", lambda ctx: None)]

            def menu_items(self):
                return {"fake_key": lambda ctx, event: None}

        thing = Thing()
        with mock.patch.object(extensions, "features", lambda app: [thing]):
            app, _ = self.bridge()
        self.assertIs(app.features[-1], thing)
        self.assertEqual(len(app.features), len(self.app.features) - len(self.app.extension_features) + 1)
        self.assertEqual(app.features[:-1], tuple(f for f in app.features if f is not thing))
        self.assertIn("fake-thread", [name for name, _ in app.worker_targets()])
        self.assertNotIn("fake-thread", [name for name, _ in self.app.worker_targets()])


if __name__ == "__main__":
    unittest.main()
