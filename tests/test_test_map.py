"""scripts/test_map.py: diff lines to the functions they are in, the fallback rules, staleness,
the content fingerprint, the ledger that skips repeated runs and the refused full run on a stale
map. The selection tests use a hand-written map, sources and an import scan of files in memory;
the fingerprint and binary-file tests use a throwaway git
repository each under TMPDIR, the child hook one short-lived Python child, the scan cache a directory under TMPDIR;
``patches`` reads sources in memory (and the repo's tests once).
No test suite runs here."""

import importlib.util
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support import REPO

_spec = importlib.util.spec_from_file_location("test_map_script", REPO / "scripts" / "test_map.py")
test_map = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(test_map)

OLD = textwrap.dedent('''\
    import os

    LIMIT = 3


    def helper(x):
        return x + LIMIT


    class Card:
        kind = "plain"

        @property
        def title(self):
            return "t"

        def render(self):
            def line(text):
                return text.strip()
            return [line(self.title)]
    ''')

DIFF = textwrap.dedent('''\
    diff --git a/covey/app/x.py b/covey/app/x.py
    index 1..2 100644
    --- a/covey/app/x.py
    +++ b/covey/app/x.py
    @@ -7 +7 @@ def helper(x):
    -    return x + LIMIT
    +    return x - LIMIT
    @@ -20,0 +21,2 @@ class Card:
    +
    +# a comment
    diff --git a/docs/a.md b/docs/a.md
    --- a/docs/a.md
    +++ b/docs/a.md
    @@ -1,2 +0,0 @@
    --- a heading that starts like a header
    -text
    \\ No newline at end of file
    ''')


def a_map(files, modules=("test_a", "test_b", "test_c")):
    """A version-2 map; files: path -> qualified name -> module names."""
    sets, out = [], {}
    for path, functions in files.items():
        out[path] = {}
        for name, members in functions.items():
            members = sorted(modules.index(member) for member in members)
            if members not in sets:
                sets.append(members)
            out[path][name] = sets.index(members)
    return {"version": 2, "built_at": "abc", "tests": "t", "modules": dict.fromkeys(modules, 1),
            "sources": [], "sets": sets, "files": out}


IMPORTS = textwrap.dedent('''\
    """What x does."""
    from os import path, sep


    def joined(parts):
        """Join."""
        return sep.join(parts)


    def absolute(name):
        return path.abspath(name)


    def main():
        return __doc__
    ''')
IMPORTED = a_map({"covey/app/x.py": {"joined": ["test_a"], "absolute": ["test_b"], "main": ["test_c"]}})

FIELDS = textwrap.dedent('''\
    from dataclasses import asdict, dataclass


    @dataclass
    class Row:
        size: int = 1
        name: str = ""

        def total(self):
            return self.size * 2


    def show(row):
        return row.name


    def dump(row):
        return asdict(row)


    def unrelated():
        return 0
    ''')
MODULES = tuple(f"test_{x}" for x in "abcdefghi")
FIELD_USERS = {"covey/app/y.py": "def use(row):\n    return row.size\n",
               "covey/app/z.py": "from dataclasses import asdict\nfrom covey.app.x import Row\n\n\n"
                                          "def save(row: Row):\n    return asdict(row)\n",
               "covey/app/w.py": "from dataclasses import asdict\n\n\ndef other(x):\n    return asdict(x)\n",
               "tests/test_g.py": "from covey.app import x\n\n\ndef test():\n    assert x.Row(size=2)\n"}
FIELDS_MAP = a_map({"covey/app/x.py": {"Row.total": ["test_a"], "show": ["test_b"], "dump": ["test_c"],
                                                "unrelated": ["test_d", "test_e", "test_f", "test_h"]},
                    "covey/app/y.py": {"use": ["test_d", "test_i"]}, "covey/app/z.py": {"save": ["test_e"]},
                    "covey/app/w.py": {"other": ["test_f"]}}, MODULES)
MAPPED = a_map({"covey/app/x.py": {"helper": ["test_a"], "Card.title": ["test_b"], "Card.render": ["test_c"],
                                            "Card.render.<locals>.line": ["test_c"]},
                "tests/fixtures/f.json": {"": ["test_b"]}})


class DiffTest(unittest.TestCase):
    def test_hunks_give_line_numbers_and_texts_on_both_sides(self):
        found = test_map.parse_diff(DIFF)
        self.assertEqual(found["covey/app/x.py"], ({7: "    return x + LIMIT"},
                                                            {7: "    return x - LIMIT", 21: "", 22: "# a comment"}))
        self.assertEqual(found["docs/a.md"], ({1: "-- a heading that starts like a header", 2: "text"}, {}))

    def test_qualified_names_match_what_the_interpreter_records(self):
        source = OLD + textwrap.dedent('''\
            def outer():
                class Inner:
                    async def go(self):
                        pass
                return Inner
            ''')
        spans = test_map.definitions(source)
        recorded = set()

        def walk(code):
            for const in code.co_consts:
                if hasattr(const, "co_qualname"):
                    if const.co_flags & 2 and not const.co_name.startswith("<"):
                        recorded.add(const.co_qualname)
                    walk(const)

        walk(compile(source, "x.py", "exec"))
        self.assertEqual({name for _, _, name, function in spans if function}, recorded)
        self.assertIn((13, 15, "Card.title", True), spans)  # The decorator line belongs to the function.

    def test_lambdas_and_comprehensions_fold_into_their_function(self):
        self.assertEqual(test_map.folded("f.<locals>.<lambda>"), "f")
        self.assertEqual(test_map.folded("C.m.<locals>.inner.<locals>.<genexpr>"), "C.m.<locals>.inner")
        self.assertEqual(test_map.folded("<lambda>"), "")


def selected(data, found, sources, files=None):
    """affected() with sources {(rev, path): text} ("" when missing; files are the working tree's too) and
    the import scan over files {path: text}."""
    files = files or {}
    with mock.patch.object(test_map, "source", lambda rev, path: sources.get((rev, path), files.get(path, "") if rev is None
                                                                               else "")), \
            mock.patch.object(test_map, "scan", lambda rev: {path: test_map.facts(path, text) for path, text in files.items()}), \
            redirect_stderr(io.StringIO()) as err:
        picked, full = test_map.affected(data, found)
    return picked, full, err.getvalue()


class SelectionTest(unittest.TestCase):
    def pick(self, new_source, old=None, new=None, data=MAPPED, path="covey/app/x.py", files=None, base=OLD):
        change = test_map.Change(path, "HEAD", None, old or {}, new or {})
        return selected(data, {path: change}, {("HEAD", path): base, (None, path): new_source}, files)

    def test_a_change_inside_a_function_selects_the_modules_that_ran_it(self):
        new = OLD.replace("x + LIMIT", "x - LIMIT")
        picked = self.pick(new, {7: "    return x + LIMIT"}, {7: "    return x - LIMIT"})[:2]
        self.assertEqual(picked, ({"test_a"}, []))

    def test_comments_and_blank_lines_select_nothing(self):
        self.assertEqual(self.pick(OLD, {}, {3: "", 4: "# why"})[:2], (set(), []))

    def test_an_assignment_outside_functions_selects_the_functions_that_read_it(self):
        picked = self.pick(OLD.replace("LIMIT = 3", "LIMIT = 4"), {3: "LIMIT = 3"}, {3: "LIMIT = 4"})
        self.assertEqual(picked, ({"test_a"}, [], ""))

    def test_a_statement_outside_functions_selects_the_whole_file_and_the_tests_importing_it(self):
        added = OLD + "print(LIMIT)\n"
        files = {"tests/test_d.py": "from covey.app.x import helper\n",
                 "tests/support/s.py": "from covey.app import x\n", "tests/test_e.py": "import support.s\n"}
        picked, full, err = self.pick(added, {}, {21: "print(LIMIT)"}, files=files)
        self.assertEqual((picked, full), ({"test_a", "test_b", "test_c", "test_d"}, []))  # Not through support.
        self.assertIn("line 21 is outside functions", err)

    def test_a_name_read_outside_functions_or_by_nothing_selects_the_whole_file(self):
        doubled = OLD + "DOUBLE = LIMIT * 2\n"
        picked, _, err = self.pick(doubled.replace("LIMIT = 3", "LIMIT = 4"), {3: "LIMIT = 3"}, {3: "LIMIT = 4"},
                                   base=doubled)
        self.assertEqual(picked, {"test_a", "test_b", "test_c"})
        self.assertIn("LIMIT is read outside functions", err)
        picked, _, err = self.pick(OLD.replace("import os", "import sys"), {1: "import os"}, {1: "import sys"})
        self.assertEqual(picked, {"test_a", "test_b", "test_c"})  # Imports kept for their side effects.
        self.assertIn("nothing reads os", err)
        picked, _, err = self.pick(OLD.replace("import os", "from __future__ import annotations"), {1: "import os"},
                                   {1: "from __future__ import annotations"})
        self.assertEqual(picked, {"test_a", "test_b", "test_c"})
        self.assertIn("line 1 is outside functions", err)

    def test_an_import_counts_for_the_names_it_binds_to_something_else(self):
        old = IMPORTS.replace("path, sep", "path, sep, curdir")
        new = IMPORTS.replace("path, sep", "path, linesep as sep")
        picked = self.pick(new, {2: "from os import path, sep, curdir"}, {2: "from os import path, linesep as sep"},
                           data=IMPORTED, base=old)
        self.assertEqual(picked, ({"test_a"}, [], ""))  # sep; not path; curdir nothing read, os stays imported.
        moved = IMPORTS.replace("from os import path, sep\n", "") + "from os import path, sep\n"
        picked = self.pick(moved, {2: "from os import path, sep"}, {15: "from os import path, sep"}, data=IMPORTED,
                           base=IMPORTS)
        self.assertEqual(picked[:2], ({"test_a", "test_b"}, []))  # Moved, nothing rebound: every name it binds.

    def test_a_name_another_file_takes_selects_that_test_module_or_everything_that_file_reaches(self):
        files = {"tests/test_d.py": "from covey.app.x import LIMIT\n",
                 "tests/test_e.py": "from unittest import mock\nmock.patch('covey.app.x.LIMIT', 1)\n"
                                    "import covey.app.x\n",
                 "covey/app/y.py": "from covey.app import x\n\ndef f():\n    return x.LIMIT\n",
                 "tests/test_f.py": "from covey.app import y\n"}
        data = a_map({"covey/app/x.py": {"helper": ["test_a"]}, "covey/app/y.py": {"f": ["test_b"]}},
                     modules=("test_a", "test_b", "test_c", "test_d", "test_e", "test_f"))
        picked = self.pick(OLD.replace("LIMIT = 3", "LIMIT = 4"), {3: "LIMIT = 3"}, {3: "LIMIT = 4"}, data=data,
                           files=files)
        self.assertEqual(picked, ({"test_a", "test_b", "test_d", "test_e", "test_f"}, [], ""))

    def test_docstrings_select_nothing_unless_a_function_reads_doc(self):
        lines = {n: text for n, text in enumerate(IMPORTS.splitlines(), 1)}
        self.assertEqual(self.pick(IMPORTS, {1: lines[1]}, {1: '"""Something else."""'}, data=IMPORTED,
                                   base=IMPORTS), ({"test_c"}, [], ""))  # main() reads __doc__.
        self.assertEqual(self.pick(IMPORTS, {6: lines[6]}, {6: '    """Or else."""'}, data=IMPORTED, base=IMPORTS),
                         (set(), [], ""))
        undocumented = IMPORTS.replace("    return __doc__", "    return 1")
        self.assertEqual(self.pick(undocumented, {1: lines[1]}, {1: '"""Else."""'}, data=IMPORTED,
                                   base=undocumented), (set(), [], ""))

    def test_a_reader_added_with_the_change_needs_no_map_entry(self):
        added = IMPORTS + "\n\ndef fresh():\n    return sep\n"
        changed = {n: text for n, text in enumerate(added.splitlines(), 1) if n > 15}
        changed[2] = "from os import path, linesep as sep"
        picked = self.pick(added.replace("path, sep", "path, linesep as sep"), {2: "from os import path, sep"},
                           changed, data=IMPORTED, base=IMPORTS)
        self.assertEqual(picked, ({"test_a"}, [], ""))

    def test_a_class_attribute_selects_what_ran_the_class_and_what_uses_the_name(self):
        self.assertEqual(self.pick(OLD, {11: '    kind = "plain"'}, {11: '    kind = "rich"'}), ({"test_b", "test_c"}, [], ""))
        picked, _, err = self.pick(OLD, {10: "class Card:"}, {10: "class Card(object):"})
        self.assertEqual(picked, {"test_a", "test_b", "test_c"})  # The class line itself: the whole file.
        self.assertIn("line 10 is in the body of class Card", err)

    def test_a_field_selects_its_readers_in_any_file_within_the_whole_file(self):
        picked = self.pick(FIELDS.replace("size: int = 1", "size: int = 2"), {6: "    size: int = 1"},
                           {6: "    size: int = 2"}, data=FIELDS_MAP, base=FIELDS, files=FIELD_USERS)
        # total (self.size), dump (asdict), y.use (row.size), z.save (asdict, names Row), test_g (size=...);
        # not show (name), w.other (asdict of something else), unrelated, nor test_i (ran nothing of x).
        self.assertEqual(picked, ({"test_a", "test_c", "test_d", "test_e", "test_g"}, [], ""))

    def test_a_field_falls_back_to_the_whole_file_when_its_readers_are_unknown(self):
        whole = {"test_a", "test_b", "test_c", "test_d", "test_e", "test_f", "test_g", "test_h"}
        for why, base, old, new, data in [
                ("size is read in the body of class Row", FIELDS.replace('name: str = ""', "name: int = size"),
                 {6: "    size: int = 1"}, {6: "    size: int = 2"}, FIELDS_MAP),
                ("show uses name and is not in the map", FIELDS, {7: '    name: str = ""'}, {7: '    name: str = "-"'},
                 a_map({"covey/app/x.py": {"Row.total": ["test_a"], "dump": ["test_c"],
                                                    "unrelated": sorted(whole - {"test_g"})}}, MODULES)),
                ("the fields of class Row changed order", FIELDS, {6: "    size: int = 1", 7: '    name: str = ""'},
                 {6: '    name: str = ""', 7: "    size: int = 1"}, FIELDS_MAP),
                ("line 6 is in the body of class Row", FIELDS.replace("size: int = 1", "__slots__ = ()"),
                 {6: "    __slots__ = ()"}, {6: "    __slots__ = ('size',)"}, FIELDS_MAP)]:
            with self.subTest(why):
                lines = base.splitlines()
                new_source = "\n".join(new.get(n, line) for n, line in enumerate(lines, 1)) + "\n"
                picked, _, err = self.pick(new_source, old, new, data=data, base=base, files=FIELD_USERS)
                self.assertIn(why, err)
                self.assertLessEqual(whole - {"test_g"}, picked)

    def test_a_class_new_on_one_side_counts_as_added_even_when_lines_align(self):
        # git matched the new class's blank lines and its "return" with old ones (commits a13eea3, 3f6b0b0),
        # and the name imported for it is read only by it (b3661a8).
        base = FIELDS.split("@dataclass")[0] + FIELDS.split("    def total(self):\n        return self.size * 2\n")[1]
        base = base.replace("asdict, dataclass", "asdict")
        changed = {n: line for n, line in enumerate(FIELDS.splitlines(), 1) if n == 1 or 4 <= n <= 10 and line}
        data = a_map({"covey/app/x.py": {"show": ["test_b"], "dump": ["test_c"]}})  # Built before Row.
        self.assertEqual(self.pick(FIELDS, {1: "from dataclasses import asdict"}, changed, data=data, base=base,
                                   files=FIELD_USERS), (set(), [], ""))

    def test_a_decorator_counts_as_its_function(self):
        self.assertEqual(self.pick(OLD, {13: "    @property"}, {13: "    @cached_property"})[0], {"test_b"})

    def test_a_function_added_as_a_whole_selects_nothing_by_itself(self):
        added = OLD + "\n\ndef fresh():\n    return 1\n"
        lines = {n: text for n, text in enumerate(added.splitlines(), 1) if n > 21}
        self.assertEqual(self.pick(added, {}, lines)[:2], (set(), []))

    def test_a_function_removed_as_a_whole_selects_the_modules_that_ran_it(self):
        removed = {n: text for n, text in enumerate(OLD.splitlines(), 1) if 6 <= n <= 7}
        without = OLD.replace("def helper(x):\n    return x + LIMIT\n", "")
        self.assertEqual(self.pick(without, removed, {})[0], {"test_a"})

    def test_a_nested_function_the_map_lacks_falls_back_to_the_outer_one(self):
        data = a_map({"covey/app/x.py": {"Card.render": ["test_c"], "helper": ["test_a"]}})
        self.assertEqual(self.pick(OLD, {19: "            return text.strip()"}, {19: "            return text"},
                                   data=data)[0], {"test_c"})

    def test_a_function_the_map_lacks_selects_the_whole_file(self):
        data = a_map({"covey/app/x.py": {"helper": ["test_a"], "Card.render": ["test_c"]}})
        picked, _, err = self.pick(OLD, {15: '        return "t"'}, {15: '        return "u"'}, data=data)
        self.assertEqual(picked, {"test_a", "test_c"})
        self.assertIn("Card.title is not in the map", err)

    def test_paths_without_lines_follow_the_file_rules(self):
        paths = ["covey/app/x.py", "tests/fixtures/f.json", "tests/test_b.py", "docs/a.md", "docs/images/a.png",
                 "scripts/test_map.py"]
        present = {(rev, path): "x = 1\n" for path in paths for rev in (None, "HEAD")}
        picked, full, _ = selected(MAPPED, dict.fromkeys(paths), present)
        self.assertEqual((picked, full), ({"test_a", "test_b", "test_c", "test_test_map"}, []))
        unknown = ["tests/support/world.py", "covey/new.py", "tests/fixtures/new.json"]
        present = {(rev, path): "x = 1\n" for path in unknown for rev in (None, "HEAD")}
        _, full, _ = selected(MAPPED, dict.fromkeys(unknown), present)
        self.assertEqual(full, ["covey/new.py", "tests/fixtures/new.json", "tests/support/world.py"])

    def test_a_support_file_maps_by_function_and_its_whole_file_reaches_through_support(self):
        data = a_map({"tests/support/world.py": {"helper": ["test_a"], "Card.render": ["test_b"]}})
        path = "tests/support/world.py"
        picked = self.pick(OLD.replace("x + LIMIT", "x - LIMIT"), {7: "    return x + LIMIT"},
                           {7: "    return x - LIMIT"}, data=data, path=path)
        self.assertEqual(picked, ({"test_a"}, [], ""))
        files = {"tests/support/other.py": "from support.world import helper\n",
                 "tests/test_c.py": "from support import other\n"}
        picked, full, err = self.pick(OLD, {10: "class Card:"}, {10: "class Card(object):"}, data=data,
                                      path=path, files=files)
        self.assertEqual((picked, full), ({"test_a", "test_b", "test_c"}, []))
        self.assertIn("body of class Card", err)

    def test_a_deleted_file_selects_the_modules_that_ran_or_still_import_it(self):
        data = a_map({"covey/app/x.py": {"helper": ["test_a"]}, "covey/app/y.py": {"f": ["test_b"]}},
                     modules=("test_a", "test_b", "test_c", "test_d"))
        files = {"tests/test_d.py": "def test():\n    from covey.app.x import helper\n",
                 "covey/app/y.py": "from covey.app import x\n"}
        removed = dict(enumerate(OLD.splitlines(), 1))
        change = test_map.Change("covey/app/x.py", "HEAD", None, removed, {})
        found = {"covey/app/x.py": change, "covey/app/gone.py": None, "tests/fixtures/old.json": None}
        picked, full, err = selected(data, found, {("HEAD", "covey/app/x.py"): OLD}, files)
        self.assertEqual((picked, full), ({"test_a", "test_b", "test_d"}, []))
        self.assertIn("covey/app/gone.py: deleted, 0 modules ran or import it", err)

    def test_an_unmapped_file_selects_the_tests_importing_it_or_what_ran_its_importers(self):
        data = a_map({"covey/app/y.py": {"f": ["test_b"]}})
        path, text = "covey/app/new.py", "def f():\n    return 1\n"
        importer = {"covey/app/y.py": "from covey.app.new import f\n", path: text}
        tested = dict(importer, **{"tests/support/s.py": "from covey.app import new\n",
                                   "tests/test_c.py": "from support.s import new\n"})
        added = {(None, path): text}
        self.assertEqual(selected(data, {path: None}, added, tested)[:2], ({"test_c"}, []))  # Through support.
        self.assertEqual(selected(data, {path: None}, added, importer)[:2], ({"test_b"}, []))
        self.assertEqual(selected(data, {path: None}, added, {path: text})[:2], (set(), [path]))
        kept = {**added, ("HEAD", path): text}  # Not new (an import-only module): both.
        self.assertEqual(selected(data, {path: None}, kept, tested)[:2], ({"test_b", "test_c"}, []))

    def test_an_old_format_map_selects_the_full_suite_and_asks_for_a_rebuild(self):
        args = SimpleNamespace(paths=["covey/app/x.py"], diff=None)
        state = {"modules": {"test_a": 1}, "tests": "t", "sources": []}
        with redirect_stderr(io.StringIO()) as err:
            self.assertIsNone(test_map.choose(args, {"built_at": "abc", "modules": {}}, state))
        self.assertIn("format 1, this script writes 2 (rebuild: build)", err.getvalue())


class ScanCacheTest(unittest.TestCase):
    def test_the_import_scan_parses_only_what_changed_since_and_stays_next_to_the_ledger(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            (root / "covey").mkdir()
            a, b = root / "covey" / "a.py", root / "covey" / "b.py"
            a.write_text("import os\n")
            b.write_text("from covey import a\n")
            read, real, ledger = [], test_map.tree_files, [root / "runs" / "ledger.jsonl"]
            listing = {"ls-files": "covey/a.py\ncovey/b.py\n",
                       "ls-tree": "100644 blob 1111\tcovey/a.py\n100644 blob 2222\tcovey/b.py\n"}

            def tree_files(rev, paths):
                read.append((rev, sorted(paths)))
                return real(None, paths)

            with mock.patch.object(test_map, "REPO", root), mock.patch.object(test_map, "tree_files", tree_files), \
                    mock.patch.object(test_map, "git", lambda command, *args: listing[command]), \
                    mock.patch.object(test_map, "ledger_path", lambda value: ledger[0]):
                ledger[0].parent.mkdir()
                first = test_map.scan(None)
                self.assertEqual(test_map.scan(None), first)
                b.write_text("from covey import a as alias\n")
                changed = test_map.scan(None)
                test_map.scan("HEAD")
                self.assertEqual(test_map.scan("HEAD"), changed)  # By blob: nothing read again.
                ledger[0] = None  # No ledger, no cache: scanned each time.
                self.assertEqual(test_map.scan(None), changed)
            self.assertEqual(read, [(None, ["covey/a.py", "covey/b.py"]), (None, ["covey/b.py"]),
                                    ("HEAD", ["covey/a.py", "covey/b.py"]),
                                    (None, ["covey/a.py", "covey/b.py"])])
            self.assertEqual(changed, {path: test_map.facts(path, (root / path).read_text()) for path in changed})
            self.assertIn("covey.a", changed["covey/b.py"].imports)
            self.assertEqual(sorted(path.name for path in (root / "runs").iterdir()), [test_map.SCAN_CACHE])


class RunTest(unittest.TestCase):
    def test_a_full_run_on_a_stale_map_is_refused_and_points_to_build(self):
        state = {"modules": {"test_a": 1}, "tests": "t", "sources": []}
        with tempfile.TemporaryDirectory() as root:
            args = SimpleNamespace(full=True, force=False, ledger=None, why="w", map=Path(root) / "map.json",
                                   test_tmpdir=None)
            for data, force, refused in ((None, False, True), (a_map({}, modules=("test_b",)), False, True),
                                         (a_map({}, modules=("test_b",)), True, False),
                                         (a_map({}, modules=("test_a",)), False, False)):
                if data:
                    args.map.write_text(json.dumps(data))
                args.force = force
                with mock.patch.object(test_map, "prepare"), \
                        mock.patch.object(test_map, "suite_state", return_value=({}, state)), \
                        mock.patch.object(test_map, "ledger_entry", side_effect=LookupError("ran")), \
                        redirect_stderr(io.StringIO()) as err:
                    if refused:
                        self.assertEqual(test_map.run(args), 2)
                        self.assertIn("refused: a full run on a stale map", err.getvalue())
                        self.assertIn("build", err.getvalue())
                    else:
                        self.assertRaisesRegex(LookupError, "ran", test_map.run, args)


class StaleTest(unittest.TestCase):
    def test_a_map_is_stale_when_modules_ids_or_source_files_differ(self):
        data = dict(a_map({}), modules={"test_a": 2, "test_b": 1}, sources=["covey/a.py"])
        state = {"modules": {"test_a": 2, "test_b": 1}, "tests": "t", "sources": ["covey/a.py"]}
        self.assertEqual(test_map.stale(data, state), [])
        state = {"modules": {"test_a": 3, "test_c": 1}, "tests": "u", "sources": ["covey/b.py"]}
        self.assertEqual(test_map.stale(data, state), [
            "test modules -test_b +test_c", "covey/bin files -covey/a.py +covey/b.py",
            "test ids (3 recorded, 4 now)"])
        self.assertEqual(test_map.stale(None, state), ["no map"])


class LedgerTest(unittest.TestCase):
    def entry(self, command="run", scope="full", slow=False, tree="t1", result="pass"):
        return {"command": command, "scope": scope, "slow": slow, "tree": tree, "result": result, "start": "s"}

    def test_what_a_passing_run_on_the_same_content_covers(self):
        covers = test_map.covers
        full, build, mods = self.entry(), self.entry("build", "build", True), self.entry(scope=["test_a", "test_b"])
        self.assertTrue(covers(full, self.entry()))
        self.assertTrue(covers(build, self.entry()))
        self.assertTrue(covers(full, self.entry(scope=["test_a"])))
        self.assertTrue(covers(mods, self.entry(scope=["test_b"])))
        self.assertFalse(covers(mods, self.entry()))
        self.assertFalse(covers(full, self.entry("build", "build", True)))
        self.assertFalse(covers(full, self.entry(slow=True)))
        self.assertFalse(covers(self.entry(result="fail"), self.entry()))
        self.assertFalse(covers(self.entry(result="skipped"), self.entry()))
        self.assertFalse(covers(full, self.entry(tree="t2")))

    def test_a_covered_run_is_skipped_and_logged(self):
        with tempfile.TemporaryDirectory() as root:
            ledger = Path(root) / "ledger.jsonl"
            entry = {"why": "w", "command": "run", "scope": "full", "slow": False, "head": "h", "tree": "t1",
                     "ledger": ledger}
            test_map.append(entry, dict(self.entry(), why="earlier"))
            args = SimpleNamespace(full=True, force=False, ledger=str(ledger), why="again", map=Path(root) / "map.json")
            args.map.write_text(json.dumps(a_map({}, modules=())))
            state = {"modules": {}, "tests": "t", "sources": []}
            with mock.patch.object(test_map, "prepare"), \
                    mock.patch.object(test_map, "suite_state", return_value=({}, state)), \
                    mock.patch.object(test_map, "ledger_entry", return_value=entry), \
                    redirect_stderr(io.StringIO()), mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                self.assertEqual(test_map.run(args), 0)
            self.assertIn('skipped, this tree already passed: {"command": "run"', out.getvalue())
            lines = test_map.read_ledger(ledger)
            self.assertEqual([line["result"] for line in lines], ["pass", "skipped"])
            self.assertNotIn("ledger", lines[1])

    @staticmethod
    def result(failing=0, erring=0, subtests=False):
        """The result of a throwaway suite "test_x.Sample": a passing test, ``failing`` that fail, ``erring``
        that raise, with subtests one whose two subtests fail."""
        def fail(self):
            self.fail("no")

        def error(self):
            raise RuntimeError("boom")

        def sub(self):
            for case in (1, 2):
                with self.subTest(case=case):
                    self.fail(case)
        body = {"__module__": "test_x", "test_ok": lambda self: None, **({"test_sub": sub} if subtests else {}),
                **{f"test_fail_{n:02}": fail for n in range(failing)}, **{f"test_error_{n}": error for n in range(erring)}}
        result = unittest.TestResult()
        # A loader of its own: `unittest -k` filters the default one's names.
        unittest.TestLoader().loadTestsFromTestCase(type("Sample", (unittest.TestCase,), body)).run(result)
        return result

    def test_a_run_with_failures_or_errors_logs_which_tests_had_them(self):
        with tempfile.TemporaryDirectory() as root:
            ledger = Path(root) / "ledger.jsonl"
            entry = {"why": "w", "command": "run", "scope": ["test_x"], "slow": False, "head": "h", "tree": "t1",
                     "ledger": ledger}
            with mock.patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(test_map.finish(None, entry, self.result(1, 1, subtests=True), 0), 1)
                self.assertEqual(test_map.finish(None, entry, self.result(), 0), 0)
                test_map.finish(None, entry, self.result(25), 0)
            failed, passed, many = test_map.read_ledger(ledger)
        self.assertEqual((failed["failures"], failed["errors"], failed["result"]), (3, 1, "fail"))
        # Failures, then errors; the two failed subtests as their one test.
        self.assertEqual(failed["failed"], ["test_x.Sample.test_fail_00", "test_x.Sample.test_sub",
                                            "test_x.Sample.test_error_0"])
        self.assertNotIn("failed", passed)
        self.assertEqual((many["failures"], len(many["failed"]), many["failed"][-1]),
                         (25, test_map.FAILED, "test_x.Sample.test_fail_19"))

    def test_flaky_counts_each_tests_failed_runs_and_reads_old_lines(self):
        with tempfile.TemporaryDirectory() as root:
            ledger = Path(root) / "ledger.jsonl"
            lines = [{"start": "t1", "head": "aaa", "result": "fail", "failures": 1},  # Logged before "failed".
                     {"start": "t2", "head": "aaa", "result": "pass"},
                     {"start": "t3", "head": "aaa", "result": "fail", "failed": ["test_b.B.test_y", "test_a.A.test_x"]},
                     {"start": "t4", "head": "bbb", "result": "skipped", "covered_by": "t2"},
                     {"start": "t5", "head": "bbb", "result": "fail", "failed": ["test_a.A.test_x"]}]
            ledger.write_text("".join(json.dumps(line) + "\n" for line in lines) + "not json\n")
            argv = ["test_map.py", "flaky", "--ledger", str(ledger)]
            with mock.patch.object(test_map, "FORBIDDEN", ()):  # Test temp files may be on /dev/shm, a ledger never.
                with mock.patch.object(sys, "argv", argv), mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                    self.assertEqual(test_map.main(), 0)
                with redirect_stderr(io.StringIO()) as err:
                    self.assertEqual(test_map.flaky(SimpleNamespace(ledger=str(Path(root) / "none.jsonl"))), 2)
        self.assertEqual(out.getvalue().splitlines(), [
            f"{ledger}: 3 failed runs (1 logged before the ledger named tests), 2 tests named",
            "   2  last t5  test_a.A.test_x  (at aaa bbb)",
            "   1  last t3  test_b.B.test_y  (at aaa)"])
        self.assertIn("no ledger at", err.getvalue())

    def test_the_ledger_stays_out_of_the_repo_home_and_system_temp(self):
        place = test_map.ledger_path
        with mock.patch.dict(os.environ, {"TMPDIR": "/mnt/data/run/tmp"}, clear=False):
            os.environ.pop("BRIDGE_TEST_LEDGER", None)
            self.assertEqual(place(None), Path("/mnt/data/run/test-ledger.jsonl"))
            self.assertEqual(place("/mnt/data/l.jsonl"), Path("/mnt/data/l.jsonl"))
            self.assertIsNone(place(str(REPO / "tests" / "l.jsonl")))
            self.assertIsNone(place(str(Path.home() / "l.jsonl")))
        for tmpdir in ("/tmp/x", "/tmp", str(Path.home() / "t")):
            with mock.patch.dict(os.environ, {"TMPDIR": tmpdir}, clear=False):
                os.environ.pop("BRIDGE_TEST_LEDGER", None)
                self.assertIsNone(place(None), tmpdir)


class TestTempTest(unittest.TestCase):
    """Test temp files go to a fresh directory for the run; files to execute stay where temp files were."""

    def test_a_base_gets_a_fresh_directory_that_is_removed_and_the_environment_restored(self):
        with tempfile.TemporaryDirectory() as base, mock.patch.dict(os.environ, {"TMPDIR": base}):
            os.environ.pop("BRIDGE_TEST_EXEC_TMPDIR", None)
            before, previous = dict(os.environ), tempfile.gettempdir()
            with redirect_stderr(io.StringIO()), test_map.test_temp(base) as path:
                self.assertEqual(path.parent, Path(base))
                self.assertEqual((os.environ["TMPDIR"], tempfile.gettempdir()), (str(path), str(path)))
                self.assertEqual(os.environ["BRIDGE_TEST_EXEC_TMPDIR"], previous)
                (path / "left-behind").write_text("x")
            self.assertFalse(path.exists())
            self.assertEqual((dict(os.environ), tempfile.gettempdir()), (before, previous))

    def test_inherit_and_a_missing_tmpfs_keep_tmpdir(self):
        with mock.patch.dict(os.environ, {"BRIDGE_TEST_TMPDIR": "inherit"}):
            with test_map.test_temp(None) as path:
                self.assertIsNone(path)
        with mock.patch.dict(os.environ), mock.patch.object(test_map, "tmpfs", return_value=False):
            os.environ.pop("BRIDGE_TEST_TMPDIR", None)
            with test_map.test_temp(None) as path:
                self.assertIsNone(path)


class ChildHookTest(unittest.TestCase):
    def test_a_child_reports_the_functions_it_ran_and_prints_nothing(self):
        with tempfile.TemporaryDirectory() as root:
            hooks, out = Path(root) / "hooks", Path(root) / "out"
            hooks.mkdir()
            out.mkdir()
            (hooks / "sitecustomize.py").write_text(test_map.CHILD_HOOK)
            env = dict(os.environ, PYTHONPATH=str(hooks), TEST_MAP_CHILD=str(out))
            child = subprocess.run([sys.executable, "-B", "-c", "def f():\n    return sum(x for x in (1,))\nf()\n"],
                                   env=env, capture_output=True, text=True, timeout=60)
            reported = "".join(path.read_text() for path in out.iterdir()).splitlines()
        self.assertEqual((child.returncode, child.stderr), (0, ""))
        self.assertIn("<string>\tf", reported)


JOBS_MODULES = {
    "jobs_pass": '''\
        import unittest


        class P(unittest.TestCase):
            def test_ok(self):
                print("noise")

            @unittest.skip("not here")
            def test_skip(self):
                pass

            def test_sub(self):
                for case in (1, 2):
                    with self.subTest(case=case):
                        self.assertEqual(case, 1)


        class Q(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                raise RuntimeError("no fixture")

            def test_never(self):
                pass
        ''',
    "jobs_crash": '''\
        import os
        import unittest


        class C(unittest.TestCase):
            def test_a(self):
                pass

            def test_b(self):
                print("last words", flush=True)
                os._exit(3)
        ''',
    "jobs_hang": '''\
        import os
        import subprocess
        import time
        import unittest


        class H(unittest.TestCase):
            def test_hang(self):
                sleeper = subprocess.Popen(["sleep", "300"])
                with open(os.environ["JOBS_PID"] + ".new", "w") as f:
                    f.write(str(sleeper.pid))
                os.replace(os.environ["JOBS_PID"] + ".new", os.environ["JOBS_PID"])
                time.sleep(300)
        ''',
    "jobs_record": '''\
        import subprocess
        import sys
        import unittest
        from pathlib import Path

        from covey.core.state import read_json


        class R(unittest.TestCase):
            def test_reads(self):
                self.assertIn("version", read_json(Path("tests/test_map.json")))
                subprocess.run([sys.executable, "-B", "-c", "from covey.core import state\\n"
                                "state.read_json(__import__('pathlib').Path('tests/test_map.json'))"], check=True)
        ''',
}


class ParallelRunTest(unittest.TestCase):
    """run_modules on throwaway modules from PYTHONPATH: one child process per module, results merged."""

    def run_jobs(self, names, root, hooks=None, timeout=600):
        env = {"PYTHONPATH": os.pathsep.join(filter(None, (str(hooks or ""), str(root)))), "JOBS_PID": str(root / "pid")}
        with mock.patch.dict(os.environ, env), mock.patch.object(test_map, "MODULE_TIMEOUT", timeout), \
                redirect_stderr(io.StringIO()) as err:
            result, reports = test_map.run_modules(names, 4, {"ledger": None}, None, hooks)
        return result, reports, err.getvalue()

    def write_modules(self, root):
        for name, text in JOBS_MODULES.items():
            (root / f"{name}.py").write_text(textwrap.dedent(text))

    def test_results_merge_and_a_crash_is_an_error_naming_its_test(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            self.write_modules(root)
            result, reports, err = self.run_jobs(["jobs_pass", "jobs_crash"], root)
        self.assertEqual((result.testsRun, len(result.failures), len(result.skipped)), (5, 1, 1))
        self.assertEqual([(test.id(), str(test)) for test, _ in result.failures],
                         [("jobs_pass.P.test_sub", "test_sub (jobs_pass.P.test_sub) (case=2)")])
        self.assertEqual([(test.id(), str(test)) for test, _ in result.errors], [
            ("setUpClass (jobs_pass.Q)", "setUpClass (jobs_pass.Q)"), ("jobs_crash.C.test_b", "jobs_crash (exit code 3)")])
        crash = result.errors[1][1]
        self.assertIn("(exit code 3 in jobs_crash.C.test_b", crash)
        self.assertIn("last words", crash)
        self.assertEqual(test_map.failed_ids(result), ["jobs_pass.P.test_sub", "setUpClass (jobs_pass.Q)",
                                                       "jobs_crash.C.test_b"])
        self.assertIn("FAIL: test_sub (jobs_pass.P.test_sub) (case=2)\n", err)
        self.assertIn("ERROR: jobs_crash (exit code 3)\n", err)
        self.assertNotIn("noise", err)  # A child's output stays in its log.
        self.assertIsNotNone(reports["jobs_pass"]["seconds"])
        self.assertIsNone(reports["jobs_crash"]["seconds"])

    def test_a_module_past_its_timeout_is_an_error_and_its_process_group_goes(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            self.write_modules(root)
            result, reports, _ = self.run_jobs(["jobs_hang"], root, timeout=5)
            sleeper = int((root / "pid").read_text()) if (root / "pid").exists() else None
        # On a loaded machine the child may still be starting after 5 s: then it was outside a test.
        self.assertEqual([str(test) for test, _ in result.errors], ["jobs_hang (timed out after 5 s)"])
        self.assertIn(result.errors[0][0].id(), ("jobs_hang.H.test_hang", "jobs_hang"))
        self.assertRegex(result.errors[0][1], r"\(timed out after 5 s (in jobs_hang\.H\.test_hang|outside a test)")
        self.assertIsNone(reports["jobs_hang"]["seconds"])
        if sleeper:
            self.assertGone(sleeper)

    def test_sigterm_kills_the_childrens_process_groups(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            self.write_modules(root)

            def terminate():
                deadline = time.monotonic() + 120
                while not (root / "pid").exists() and time.monotonic() < deadline:
                    time.sleep(.05)
                time.sleep(.2)  # The pid written, not yet read.
                os.kill(os.getpid(), signal.SIGTERM)
            threading.Thread(target=terminate, daemon=True).start()
            before = signal.getsignal(signal.SIGTERM)
            with self.assertRaises(KeyboardInterrupt):
                self.run_jobs(["jobs_hang", "jobs_pass"], root)
            self.assertIs(signal.getsignal(signal.SIGTERM), before)
            self.assertGone(int((root / "pid").read_text()))

    def assertGone(self, pid):
        def alive():
            try:
                return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
            except OSError:
                return False
        deadline = time.monotonic() + 10  # Orphaned by the kill: init reaps it.
        while time.monotonic() < deadline and alive():
            time.sleep(.1)
        self.assertFalse(alive())

    def test_a_build_child_records_its_functions_and_files_and_its_children_report_theirs(self):
        with tempfile.TemporaryDirectory() as root:
            root, hooks = Path(root), Path(root) / "hooks"
            self.write_modules(root)
            hooks.mkdir()
            (hooks / "sitecustomize.py").write_text(test_map.CHILD_HOOK)
            result, reports, _ = self.run_jobs(["jobs_record"], root, hooks)
            grandchild = "".join(path.read_text() for path in (hooks / "jobs_record").iterdir()).splitlines()
        self.assertEqual((result.testsRun, result.failures, result.errors), (1, [], []))
        coverage = reports["jobs_record"]["coverage"]
        self.assertIn("covey/core/state.py\tread_json", coverage)
        self.assertIn("tests/test_map.json\t", coverage)  # Opened; build() leaves out what no test reads.
        self.assertIn(str(REPO / "covey" / "core" / "state.py") + "\tread_json", grandchild)


class FingerprintTest(unittest.TestCase):
    def test_the_fingerprint_follows_content_not_commits(self):
        with tempfile.TemporaryDirectory() as root:
            repo = Path(root)

            def git(*args):
                options = ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
                subprocess.run(["git", *options, *args], cwd=repo, check=True, capture_output=True)

            def write(path, text):
                (repo / path).parent.mkdir(parents=True, exist_ok=True)
                (repo / path).write_text(text)

            git("init", "-q")
            write("covey/a.py", "A = 1\n")
            write("tests/test_map.json", "{}\n")
            git("add", "-A")
            git("commit", "-q", "--no-verify", "-m", "one")
            with mock.patch.object(test_map, "REPO", repo):
                first = test_map.fingerprint()
                write("covey/a.py", "A = 2\n")
                edited = test_map.fingerprint()
                git("commit", "-q", "--no-verify", "-am", "two")
                committed = test_map.fingerprint()
                write("tests/test_map.json", '{"x": 1}\n')
                write("notes/todo.md", "n\n")
                unread = test_map.fingerprint()
                write("tests/test_new.py", "\n")
                untracked = test_map.fingerprint()
                write("docs/usage.md", "n\n")  # DocReferenceTest reads the docs.
                doc = test_map.fingerprint()
                write("AGENTS.md", "n\n")
                agents = test_map.fingerprint()
        self.assertNotEqual(first, edited)
        self.assertEqual(edited, committed)
        self.assertEqual(committed, unread)
        self.assertNotEqual(unread, untracked)
        self.assertNotEqual(untracked, doc)
        self.assertNotEqual(doc, agents)


class SourceTest(unittest.TestCase):
    def test_a_binary_file_reads_in_the_tree_and_at_a_commit_without_failing(self):
        png = b"\x89PNG\n\x1a\n\x00\xff"  # Without \r: text mode turns \r\n into \n.
        with tempfile.TemporaryDirectory() as root:
            repo = Path(root)
            (repo / "docs").mkdir()
            (repo / "docs" / "a.png").write_bytes(png)
            options = ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
            for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "--no-verify", "-m", "one"]):
                subprocess.run(["git", *options, *args], cwd=repo, check=True, capture_output=True)
            with mock.patch.object(test_map, "REPO", repo):
                self.assertEqual(test_map.source(None, "docs/a.png"), png.decode(errors="replace"))
                self.assertEqual(test_map.source("HEAD", "docs/a.png"), png.decode(errors="replace"))
                self.assertEqual(test_map.source("HEAD", "docs/gone.png"), "")


PROVE_OLD = textwrap.dedent('''\
    import unittest
    from covey.x import value


    def test_helper():
        pass


    class XTests(unittest.TestCase):
        def test_kept(self):
            self.assertIn(value(), (1, 2))

        def test_changed(self):
            self.assertEqual(value(), 1)
    ''')
PROVE_NEW = textwrap.dedent('''\
    import unittest
    from covey.x import value


    def test_helper():
        pass


    class Mixin:
        def test_inherited(self):
            self.assertEqual(value(), 2)


    class XTests(Mixin, unittest.TestCase):
        def test_kept(self):
            self.assertIn(value(), (1, 2))

        def test_changed(self):
            self.assertEqual(value(), 2)

        def test_guard(self):
            def test_local():
                pass
            self.assertGreater(value(), 0)

        @unittest.skip("not here")
        def test_skipped(self):
            pass
    ''')


class PatchesTest(unittest.TestCase):
    MODULES = {"covey", "covey.app", "covey.app.detached", "covey.app.app"}
    SOURCE = textwrap.dedent('''\
        import importlib
        import unittest
        from unittest import mock

        import covey.app.detached
        from covey.app import detached
        from covey.app import detached as other
        from covey.app.detached import Runner

        TARGET = "covey.app.detached.later"
        loaded = importlib.import_module("covey.app.detached")


        class T(unittest.TestCase):
            @mock.patch("covey.app.detached.alive")
            def test_a(self, alive):
                with mock.patch("covey.app.app.alive"), mock.patch.object(detached, "live_keys"), \\
                        mock.patch.object(Runner, "start"):
                    mock.patch.multiple(other, ACK=1, WAIT=2)
                    mock.patch.dict("covey.app.detached.TABLE", {})
                    setattr(covey.app.detached, "x", 1)
                    loaded.y = 2
                    mock.patch(TARGET)
                for name in ("a", "b"):
                    mock.patch.object(detached, name)
                    setattr(detached, name, 1)
                    mock.patch(f"covey.app.{name}.alive")
                    mock.patch("covey.app.detached." + name)
                    mock.patch.multiple(detached, **{name: 1})
                    mock.patch("covey.core." + name)
        ''')

    def test_each_way_of_patching_a_modules_name_is_listed_and_a_class_attribute_or_another_module_is_not(self):
        sites = test_map.patch_sites("covey.app.detached", {"tests/test_x.py": self.SOURCE}, self.MODULES)
        self.assertEqual([(line, kind, name, where) for _, line, kind, name, where in sites], [
            (10, "string", "later", "<module>"),
            (15, "patch", "alive", "T.test_a"),
            (17, "patch.object", "live_keys", "T.test_a"),
            (19, "patch.multiple", "ACK", "T.test_a"),
            (19, "patch.multiple", "WAIT", "T.test_a"),
            (20, "patch.dict", "TABLE", "T.test_a"),
            (21, "setattr", "x", "T.test_a"),
            (22, "assign", "y", "T.test_a"),
            (25, "dynamic", "name", "T.test_a"),  # Names known at run time only: listed, to be read.
            (26, "dynamic", "name", "T.test_a"),
            (27, "dynamic", "f'covey.app.{name}.alive'", "T.test_a"),
            (28, "dynamic", "'covey.app.detached.' + name", "T.test_a"),
            (29, "dynamic", "**{name: 1}", "T.test_a"),
        ])
        self.assertEqual({path for path, *_ in sites}, {"tests/test_x.py"})
        self.assertEqual([site[1:4] for site in test_map.patch_sites(
            "covey.app", {"tests/test_x.py": self.SOURCE}, self.MODULES)],
            [(27, "dynamic", "f'covey.app.{name}.alive'")])
        self.assertEqual([site[1:4] for site in test_map.patch_sites(
            "covey.app.app", {"tests/test_x.py": self.SOURCE}, self.MODULES)],
            [(17, "patch", "alive"), (27, "dynamic", "f'covey.app.{name}.alive'")])

    def test_the_command_takes_a_dotted_name_or_a_path_and_refuses_an_unknown_module(self):
        for given in ("covey.core.state", "core.state", str(REPO / "covey" / "core" / "state.py")):
            with self.subTest(given=given), mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                self.assertEqual(test_map.patches(SimpleNamespace(module=given)), 0)
                self.assertRegex(out.getvalue().splitlines()[-1],
                                 r"^\d+ places in \d+ files patch names of covey.core.state$")
        with redirect_stderr(io.StringIO()) as err:
            self.assertEqual(test_map.patches(SimpleNamespace(module="no.such.module")), 2)
        self.assertIn("not a module", err.getvalue())


class ProveTest(unittest.TestCase):
    """prove: the test methods new or changed since a commit, run on that commit's covey/ and bin/."""

    def test_new_and_changed_methods_of_classes_count_and_nothing_else(self):
        self.assertEqual(test_map.new_tests(PROVE_OLD, PROVE_NEW),
                         ["Mixin.test_inherited", "XTests.test_changed", "XTests.test_guard", "XTests.test_skipped"])
        self.assertEqual(test_map.new_tests(PROVE_NEW, PROVE_NEW), [])
        self.assertEqual(test_map.new_tests("", "class A:\n    def test_a(self):\n        pass\n"), ["A.test_a"])
        decorated = PROVE_NEW.replace('@unittest.skip("not here")', '@unittest.skip("elsewhere")')
        self.assertEqual(test_map.new_tests(PROVE_NEW, decorated), ["XTests.test_skipped"])
        self.assertEqual(test_map.new_tests("", "class A(:\n"), [])

    def test_the_first_line_is_the_last_exceptions_own(self):
        chained = ("Traceback (most recent call last):\n  File \"a.py\", line 1, in f\n    g()\nKeyError: 'x'\n\n"
                   "During handling of the above exception, another exception occurred:\n\n"
                   "Traceback (most recent call last):\n  File \"a.py\", line 3, in f\n    assert 0\n"
                   "AssertionError: 1 != 2\n- one\n+ two\n")
        self.assertEqual(test_map.first_line(chained), "AssertionError: 1 != 2")
        self.assertEqual(test_map.first_line("no traceback\nat all"), "no traceback")
        self.assertEqual(test_map.first_line(""), "?")

    def test_the_new_tests_run_on_the_old_code_and_each_says_how_it_ended(self):
        with tempfile.TemporaryDirectory() as root:
            repo = Path(root)

            def git(*args):
                options = ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
                subprocess.run(["git", *options, *args], cwd=repo, check=True, capture_output=True)

            def write(path, text):
                (repo / path).parent.mkdir(parents=True, exist_ok=True)
                (repo / path).write_text(text)

            git("init", "-q")
            write("covey/__init__.py", "")
            write("covey/x.py", "def value():\n    return 1\n")
            write("tests/support/__init__.py", "")
            write("tests/test_x.py", PROVE_OLD)
            git("add", "-A")
            git("commit", "-q", "--no-verify", "-m", "base")
            write("covey/x.py", "def value():\n    return 2\n\n\ndef extra():\n    return 3\n")
            write("tests/test_x.py", PROVE_NEW)
            write("tests/test_y.py", "import unittest\nfrom covey.x import extra\n\n\n"
                                     "class YTests(unittest.TestCase):\n    def test_extra(self):\n"
                                     "        self.assertEqual(extra(), 3)\n")
            write("tests/test_z.py", "import unittest\nfrom covey.x import value\n\n\n"
                                     "class ZTests(unittest.TestCase):\n    @classmethod\n    def setUpClass(cls):\n"
                                     "        assert value() == 2, 'set up on the old code'\n\n"
                                     "    def test_after_set_up(self):\n        pass\n")
            outputs = {}
            with mock.patch.object(test_map, "REPO", repo), mock.patch.object(test_map, "TESTS", repo / "tests"):
                for picked in ([], ["test_y", "test_x.XTests.test_changed"], ["test_w"]):
                    args = SimpleNamespace(base="HEAD", tests=picked, jobs=2, keep=False, test_tmpdir="inherit")
                    with redirect_stderr(io.StringIO()), mock.patch("sys.stdout", new=io.StringIO()) as out:
                        outputs[len(picked)] = (test_map.prove(args), out.getvalue())
            self.assertEqual(sorted(repo.iterdir()), [repo / name for name in (".git", "covey", "tests")])
        code, text = outputs[0]
        lines = {line.split()[1]: line.split(None, 2)[0::2] for line in text.splitlines()[1:-1]}
        self.assertEqual(code, 1)
        self.assertEqual(lines, {
            "test_x.XTests.test_changed": ["fail", "AssertionError: 1 != 2"],
            "test_x.XTests.test_inherited": ["fail", "AssertionError: 1 != 2"],
            "test_x.XTests.test_guard": ["pass"],
            "test_x.XTests.test_skipped": ["skip", "not here"],
            "test_y.YTests.test_extra": ["fail", "ImportError: cannot import name 'extra' from 'covey.x' "
                                                 "(covey/x.py)"],
            "test_z.ZTests.test_after_set_up": ["fail", "AssertionError: set up on the old code"]})
        self.assertIn("added or changed since", text.splitlines()[0])
        self.assertEqual(text.splitlines()[-1], "4 of 6 fail on " + text.split("fail on ")[1].split(";")[0]
                         + "; not failing there: test_x.XTests.test_guard test_x.XTests.test_skipped")
        self.assertEqual(outputs[2][0], 0)
        self.assertEqual(len(outputs[2][1].splitlines()), 4)
        self.assertEqual(outputs[1][0], 2)
        self.assertIn("no test added or changed", outputs[1][1])


if __name__ == "__main__":
    unittest.main()
