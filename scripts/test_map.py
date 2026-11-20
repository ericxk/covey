#!/usr/bin/env python3
"""Run the tests a change can affect, from a recorded map of what each test module runs.

    python3 -B scripts/test_map.py run --why TEXT [PATH ...]    # the uncommitted change, or PATHs
    python3 -B scripts/test_map.py run --why TEXT --diff REV    # everything that differs from REV
    python3 -B scripts/test_map.py run --why TEXT --full        # the whole default tier
    python3 -B scripts/test_map.py build [tests/test_x.py ...]  # everything incl. the slow tier, rewrites the map
    python3 -B scripts/test_map.py select [--diff REV] [PATH ...]  # only print the unittest command
    python3 -B scripts/test_map.py flaky                         # the tests the ledger's runs failed, how often
    python3 -B scripts/test_map.py prove --base REV [TEST ...]   # do the tests new since REV fail on REV's code?
    python3 -B scripts/test_map.py patches MODULE                # the tests that patch MODULE's names by its path

``run`` and ``build`` run each test module in a child process of its own (``child``), ``--jobs`` at
a time (default JOBS; 1 still works), the slowest of the last run first (TIMES next to the
ledger); the parent merges their results into one, prints failures and errors as unittest does,
and counts a module that crashes, cannot start or exceeds MODULE_TIMEOUT as an error naming the
test it was in. A timeout, Ctrl-C or SIGTERM kills the children's process groups; a finished
module's group goes too (processes its tests left behind). ``build`` has each child record the
functions its tests ran (file and qualified name, ``sys.monitoring``, Python 3.12; the module
loads before recording starts; Python processes they start report theirs through CHILD_HOOK)
and the non-Python files they opened. The map is tests/test_map.json. Without paths, build first
checks whether the map is stale (other test modules, test ids or covey/bin files than
it recorded, or an older format) and stops when it is current.

``select`` and ``run`` map a change to test modules. For a diff (default: uncommitted and
untracked changes; ``--diff REV``, or ``A..B`` between two commits) each changed line counts in
the innermost function around it (decorators included), on the side of the diff it is on: the
modules that ran that function, or the enclosing function when the map does not know a nested
one. A function or class added or removed as a whole (or that the other side does not define) adds
nothing by itself (its changed callers do); comments, blank lines and docstrings add nothing (a
module docstring counts where a function reads ``__doc__``, the others where the file reads
``.__doc__``). An import or an assignment to plain names outside functions counts for the
names whose binding changed: the modules that ran the functions reading them, and the files that
take them from this one (a test module: itself; another file: all it can reach). An assignment in a
class body (a dataclass or NamedTuple field, a class attribute) counts for the modules that ran the
class's methods or a function using the name as an attribute, keyword or string in any file (where
the file names the class, also one listing every field: asdict, vars), at most the whole file. The
whole file counts for any other line outside functions (a class line, other statements), for such
a name read outside functions or in a class body (not one added or removed with the change), by a
function of its file the map does not know or by nothing (an import kept for its side effect), for
class fields that changed order, and for a line in a function the map does not know: every module
that ran anything in it and the test modules that import it (a tests/support/ file: through other
support files too); so does a PATH argument. tests/support/ maps like any other file. A deleted file
selects the modules that ran anything in it or still import it. A Python file the map does not
know (a new one, an import-only module) selects the test modules that import it, through
tests/support/ too, and the modules that ran the files importing it (for a new file only when no
test imports it). Other paths no module maps to and an old-format map select the full suite. A
changed test module selects itself; Markdown selects nothing. ALWAYS runs every time, after a doc
edit too (DOCS are in the fingerprint). The import scan of every file (2-4 s on the NAS) is kept next
to the ledger (SCAN_CACHE) per file, by path, mtime and size (at a commit: its blob), and only files
changed since are parsed again.

``run`` and ``build`` append one JSON line per run to a ledger (``--ledger``, else
$BRIDGE_TEST_LEDGER, else test-ledger.jsonl next to $TMPDIR; never inside the repo, $HOME or the
system temp dirs): why, scope, counts, HEAD and a fingerprint of the content of covey/,
bin/, tests/ and scripts/ (a commit keeps it), and for a run with failures or errors the ids of
the tests that had them (``failed``, FAILED at most; a subtest as its test). ``flaky`` sums those
up per test: in how many runs it failed, the last one, at which HEADs. ``run`` skips what a
passing run on the same content already covered (a full run or a build covers everything);
``--force`` runs anyway.
``run`` refuses a full run on a stale map (exit 2): ``build`` runs the same tests plus the slow
tier and refreshes the map, so run that instead; ``--force`` runs the full suite anyway.
What differs with the machine is not compared: the cases of the modules that skip whole without the
company extension, and of all modules when one cannot be imported here (a package it needs is missing:
``run`` names it and runs it as its import error; ``build`` refuses, exit 2).

``run`` and ``build`` put the tests' temp files in a fresh directory on /dev/shm when it is a
writable tmpfs, and remove it afterwards: most wall time on the NAS is file-system metadata
(a full run ~5 min there, ~65 s on tmpfs). The owner allowed tmpfs for test temp files only;
logs and the ledger stay put. /dev/shm is noexec, so BRIDGE_TEST_EXEC_TMPDIR keeps the previous
temp dir for tests that run files they write. Each module's child gets its own subdirectory as
TMPDIR. ``--test-tmpdir DIR`` or $BRIDGE_TEST_TMPDIR picks another base; ``inherit`` keeps $TMPDIR
(shared by the children).

A changed constant can still affect tests that never ran a function of its file, so the map
narrows what to run while working; each stage still ends with one full check.

``prove --base REV`` checks that the tests a change adds fail without it. It picks every test
method of tests/test*.py that REV lacks or whose text (decorators included) differs from REV's
(the working tree, untracked files too; TEST arguments narrow it to ids starting with one of them),
copies the working tree with REV's covey/ and bin/ to a fresh directory under $TMPDIR,
committed there as a one-commit git repository (``--keep`` leaves it, with each module's log), and runs the picked tests there, one child
process per test module (``--jobs`` at a time), test temp files as for ``run``. It prints each test
as fail or pass (skip: skipped there; missing: not loaded) with the first line of its exception,
and exits 0 when every one failed, 1 when some did not (a guard of what the old code already did
right is expected to pass: name it in the report), 2 when nothing was picked.
"""
from __future__ import annotations

import argparse
import ast
import contextlib
import hashlib
import importlib
import inspect
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from functools import cached_property
from pathlib import Path
from typing import NamedTuple

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from covey.core import extensions  # noqa: E402 - standard library only

TESTS = REPO / "tests"
MAP = TESTS / "test_map.json"
VERSION = 2
FULL = "python3 -B -m unittest discover -s tests"
DIRS = ("covey", "bin", "tests", "scripts", "extensions")  # What the tests read (the map shows nothing else).
# What test_structure's DocReferenceTest also reads (ALWAYS runs it): a doc edit must not pass as tested.
DOCS = ("AGENTS.md", "README.md", "docs", "integrations", "skills")
PYTHON = ("covey", "bin", "tests", "extensions")  # What the import scan reads.
DOTTED = re.compile(r"[A-Za-z_][\w.]*")  # A string that may name a module attribute (mock.patch targets).
# Calls and attributes that read every field of an object: asdict(job), dataclasses.fields(job), row._fields.
LISTS_FIELDS = frozenset({"fields", "asdict", "astuple", "vars", "_asdict", "_fields", "__dict__"})
SCAN_CACHE = "test-map-scan.json"  # Next to the ledger: each file's import scan, see scan().
# Structure tests: they scan the package source (card kinds, sequence writes), start the
# entry scripts in a clean environment or check the backend table. ~3 s.
ALWAYS = ("test_structure",)
UNREAD = ("tests/test_map.json",)  # Changes that no test reads.
# The build machinery runs inside every module, so the map leaves this script out; its tests are:
SELF, SELF_TESTS = "scripts/test_map.py", "test_test_map"
FORBIDDEN = (Path.home(), Path("/tmp"), Path("/var/tmp"), Path("/dev/shm"))  # Never the ledger's place.
FAILED = 20  # Test ids a ledger line names at most, see failed_ids().
SHM = Path("/dev/shm")  # Default base for test temp files, see test_temp().
# Loaded by child Python processes during build (it is first on their PYTHONPATH).
CHILD_HOOK = """\
import atexit, os, sys
out = os.environ.get("TEST_MAP_CHILD")
if out and hasattr(sys, "monitoring"):
    mon, ran = sys.monitoring, set()
    def started(code, _offset):
        if code.co_flags & 2:  # CO_NEWLOCALS: a function, not module or class-body code
            ran.add(code.co_filename + "\\t" + code.co_qualname)
        return mon.DISABLE
    try:
        mon.use_tool_id(mon.COVERAGE_ID, "test_map")
    except ValueError:
        pass
    else:
        mon.register_callback(mon.COVERAGE_ID, mon.events.PY_START, started)
        mon.set_events(mon.COVERAGE_ID, mon.events.PY_START)
        def report():
            lines = sorted(ran)  # A copy: the genexpr below is a new code object and adds itself to ran.
            with open(os.path.join(out, str(os.getpid())), "a") as f:
                f.write("".join(line + "\\n" for line in lines))
        atexit.register(report)
"""
# prove's child, in the copied tests/: python3 -c PROVE_CHILD MODULE OUT QUALNAME...; writes OUT (JSON).
PROVE_CHILD = """\
import importlib, json, sys, traceback, unittest
module, out, wanted = sys.argv[1], sys.argv[2], set(sys.argv[3:])
report = {"tests": {}, "skipped": {}, "fixtures": [], "missing": sorted(wanted)}
try:
    import support  # Scrubs a running Bridge's environment before covey loads.
    loaded = unittest.defaultTestLoader.loadTestsFromModule(importlib.import_module(module))
except BaseException:
    report["import"] = traceback.format_exc()
else:
    def flat(suite):
        for test in suite:
            yield from flat(test) if isinstance(test, unittest.TestSuite) else (test,)
    def owner(test):
        name = getattr(test, "_testMethodName", "")
        cls = next((cls for cls in type(test).__mro__ if name in vars(cls)), None)
        return f"{cls.__qualname__}.{name}" if cls and cls.__module__ == module else None
    picked = [test for test in flat(loaded) if owner(test) in wanted]
    report["missing"] = sorted(wanted - {owner(test) for test in picked})
    class Result(unittest.TestResult):
        def startTest(self, test):
            super().startTest(test)
            report["tests"].setdefault(test.id(), None)
    result = Result()
    unittest.TestSuite(picked).run(result)
    for test, text in result.failures + result.errors:
        name = getattr(test, "test_case", test).id()
        if name in report["tests"]:
            report["tests"][name] = report["tests"][name] or text
        else:
            report["fixtures"].append([str(test), text])
    report["skipped"] = {test.id(): reason for test, reason in result.skipped}
    report["picked"] = [test.id() for test in picked]
with open(out, "w") as f:
    json.dump(report, f)
"""
PROVE_TIMEOUT = 900  # Seconds per test module.
# run and build: test modules at once. On the shared machine (load ~1,400 on 64 cores, 2026-11-03) a full run
# took ~64 s with 4, ~54 s with 8 and ~43 s with 12 (medians, noisy); past 8 the slowest module (~28 s) bounds it.
JOBS = min(8, os.cpu_count() or 1)
MODULE_TIMEOUT = 600  # Seconds per test module in run and build (the slowest takes ~60 s alone, ~185 s on an NFS stall).
TIMES = "test-map-times.json"  # Next to the ledger: each module's last seconds, longest started first.
LIST_KINDS = ("failures", "errors", "skipped", "expectedFailures", "unexpectedSuccesses")  # unittest.TestResult's.


class Change(NamedTuple):
    """The removed (old side, at old_rev) and added lines (new side, at new_rev; None: the
    working tree) of one file, as line number -> text."""
    path: str
    old_rev: str
    new_rev: str | None
    old: dict
    new: dict


def git(*args, stdin=None) -> str:
    return subprocess.run(["git", *args], cwd=REPO, input=stdin, capture_output=True, text=True, check=True).stdout


def note(text) -> None:
    print("# " + text, file=sys.stderr)


def test_roots() -> list[Path]:
    """tests/, then the tests/ of each enabled extension (core/extensions.py; COVEY_EXTENSIONS=''
    leaves them out). Module names are unique across them; an extension's tests/support/ joins the support package."""
    return [TESTS, *(d / "tests" for d in extensions.package_dirs() if (d / "tests").is_dir())]


def test_modules() -> list[str]:
    return sorted(path.stem for root in test_roots() for path in root.glob("test*.py"))


GATED = re.compile(r"^require_extension\(", re.MULTILINE)


def gated_modules() -> list[str]:
    """The core test modules that skip whole without the company extension (a top-level require_extension()):
    their cases depend on whether it is enabled, so the case list a map compares leaves them out and a map built
    with the extension on describes the suite CI runs without it."""
    return sorted(path.stem for path in TESTS.glob("test*.py") if GATED.search(path.read_text(encoding="utf-8")))


TEST_PATH = re.compile(r"(?:tests|extensions/[^/]+/tests)/test")
SUPPORT_PATH = re.compile(r"(?:tests|extensions/[^/]+/tests)/support/")


def is_test(path) -> bool:
    """A test module's path (tests/test_x.py, or an extension's extensions/<package>/tests/test_x.py)."""
    return TEST_PATH.match(path) is not None


def is_support(path) -> bool:
    return SUPPORT_PATH.match(path) is not None


def prepare() -> None:
    os.chdir(REPO)
    for root in reversed(test_roots()):
        sys.path.insert(0, str(root))
    importlib.import_module("support")  # Scrubs a running Bridge's environment before covey loads.


def suite_state():
    """The loaded test modules, and what a map built now would record about the suite."""
    loader = unittest.TestLoader()
    suites, broken = {}, {}
    for name in test_modules():
        errors = len(loader.errors)
        try:
            suites[name] = loader.loadTestsFromName(name)
        except unittest.SkipTest:
            suites[name] = unittest.TestSuite()
        if len(loader.errors) > errors:  # It cannot be imported here: unittest stands one failing case in for its tests.
            lines = [line for line in loader.errors[-1].splitlines() if line.strip()]
            broken[name] = lines[-1].strip() if lines else "import failed"
    ids = {name: sorted(test.id() for test in tests(suite)) for name, suite in suites.items()}
    # An extension checked out beside the core (COVEY_EXTENSION_PATH outside this tree) is not this tree's: the map
    # describes the tree, so those modules are listed apart and left out of the staleness check (and of the case list, which is not compared then).
    outside = sorted(name for root in test_roots() if not root.resolve().is_relative_to(REPO.resolve())
                     for name in (path.stem for path in root.glob("test*.py")))
    gated = set(gated_modules())
    sources = [path for path in git("ls-files", "--cached", "--others", "--exclude-standard", "--", "covey",
                                    "bin", "extensions").splitlines() if "/tests/" not in path]
    return suites, {"modules": {name: len(found) for name, found in ids.items()},
                    "tests": digest(test for name in ids if name not in outside and name not in gated
                                    for test in ids[name]),
                    "outside": outside, "broken": broken,
                    "sources": sorted(path for path in sources if path.endswith(".py"))}


def tests(suite):
    for test in suite:
        yield from tests(test) if isinstance(test, unittest.TestSuite) else (test,)


def digest(lines) -> str:
    return hashlib.sha256("".join(line + "\n" for line in lines).encode()).hexdigest()[:16]


def stale(data, state) -> list[str]:
    """Why the map no longer describes the suite; [] when it does."""
    if not data or data.get("version") != VERSION:
        return ["no map" if not data else f"format {data.get('version', 1)}, this script writes {VERSION}"]
    reasons = []
    now_modules = {name: n for name, n in state["modules"].items() if name not in state.get("outside", ())}
    for what, recorded, now in (("test modules", data["modules"], now_modules),
                                ("covey/bin files", data["sources"], state["sources"])):
        if set(recorded) != set(now):
            moved = [("+" if name in now else "-") + name for name in sorted(set(recorded) ^ set(now))]
            reasons.append(f"{what} {' '.join(moved[:6])}" + (f" and {len(moved) - 6} more" if len(moved) > 6 else ""))
    # With an extension from outside this tree on, core tests that need it run more cases than the map recorded
    # (requires_extension), so the case list cannot be compared; the module and file lists above still are.
    # Nor when a module cannot be imported on this machine (a package it needs is missing): that is no stale map,
    # and the run shows the module's import error as its one failing case (run() says so first).
    if data["tests"] != state["tests"] and not state.get("outside") and not state.get("broken"):
        gated = set(gated_modules())
        count = lambda modules: sum(n for name, n in modules.items() if name not in gated)
        reasons.append(f"test ids ({count(data['modules'])} recorded, {count(now_modules)} now)")
    return reasons


def folded(qualname) -> str:
    """co_qualname without its lambda, comprehension or <locals> tail: "f.<locals>.<lambda>" -> "f"."""
    parts = qualname.split(".")
    while parts and parts[-1].startswith("<"):
        parts.pop()
    return ".".join(parts)


def build(args) -> int:
    os.environ["BRIDGE_TESTS_SLOW"] = "1"  # The slow tier costs ~15 s more and its code belongs in the map.
    prepare()
    suites, state = suite_state()
    if state["broken"]:  # The map would record a stand-in case for each, not its tests.
        print("refused: a map built where test modules cannot be imported: " + "; ".join(
            f"{name} ({error})" for name, error in sorted(state["broken"].items())), file=sys.stderr)
        return 2
    data = json.loads(args.map.read_text()) if args.map.exists() else None
    wanted = {Path(path).stem for path in args.paths}
    if not wanted:
        reasons = stale(data, state)
        if not reasons and not args.force:
            print(f"{args.map}: current (built at {data['built_at']}), nothing to rebuild; a full check of this tree "
                  "is `run --full`, `build --force` rebuilds anyway")
            return 0
        note("building, the map is stale: " + "; ".join(reasons) if reasons else "building (--force)")
    known = set(git("ls-files", "--cached", "--others", "--exclude-standard").splitlines())
    names: dict[str, str | None] = {}
    seen: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))

    def kept(text) -> str | None:
        return text if text in known and not text.startswith("tests/test") and text not in UNREAD and text != SELF else None

    def record(module, path, qualname="") -> None:  # A grandchild's line: its path as the interpreter had it.
        if path not in names:
            names[path] = kept(repo_path(path))
        if names[path]:
            seen[module][names[path]].add(folded(qualname))

    entry = ledger_entry(args, "build", sorted(wanted) or "build", slow=True)
    picked = [name for name in suites if not wanted or name in wanted]
    with test_temp(args.test_tmpdir) as temp:
        hooks = Path(tempfile.mkdtemp(prefix="test_map-"))
        (hooks / "sitecustomize.py").write_text(CHILD_HOOK)
        os.environ["PYTHONPATH"] = os.pathsep.join(filter(None, (str(hooks), os.environ.get("PYTHONPATH"))))
        try:
            start = time.time()
            result, reports = run_modules(picked, args.jobs, entry, temp, hooks)
            for name in picked:
                seen[name]  # A module that runs nothing tracked still gets an entry.
                for line in reports[name]["coverage"]:  # The child resolved its paths in its working directory.
                    text, qualname = line.split("\t", 1)
                    if kept(text):
                        seen[name][text].add(qualname)
                for report in (hooks / name).iterdir():
                    for line in report.read_text().splitlines():
                        record(name, *line.split("\t", 1))
        finally:
            shutil.rmtree(hooks, ignore_errors=True)

    modules = unpack(data) if wanted and data and data.get("version") == VERSION else {}
    modules.update(seen)
    size = write_map(args.map, state, modules)
    print(f"{args.map.relative_to(REPO) if args.map.is_relative_to(REPO) else args.map}: {size}")
    return finish(args, entry, result, start)


def repo_path(path) -> str | None:
    """A path as the interpreter or open() had it -> relative to the repo (posix); None outside it."""
    try:
        return Path(path).resolve().relative_to(REPO).as_posix()
    except (ValueError, OSError):
        return None


def unpack(data) -> dict[str, dict[str, set[str]]]:
    """test module -> path -> qualified names, back from a written map."""
    names, out = list(data["modules"]), defaultdict(lambda: defaultdict(set))
    for name in names:
        out[name]
    for path, functions in data["files"].items():
        for qualname, number in functions.items():
            for index in data["sets"][number]:
                out[names[index]][path].add(qualname)
    return out


def write_map(path, state, modules) -> str:
    """One line per file: qualified name ("" for a file only opened) -> index into the module sets."""
    order = [name for name in state["modules"] if name in modules and name not in state.get("outside", ())]
    position = {name: index for index, name in enumerate(order)}
    files = defaultdict(lambda: defaultdict(set))
    for name in order:
        for file, qualnames in modules[name].items():
            for qualname in qualnames:
                files[file][qualname].add(position[name])
    sets: dict[tuple, int] = {}
    rows = [f"  {json.dumps(file)}: " + json.dumps({qualname: sets.setdefault(tuple(sorted(found)), len(sets))
                                                     for qualname, found in sorted(files[file].items())})
            for file in sorted(files)]
    head = {"version": VERSION, "built_at": git("rev-parse", "--short", "HEAD").strip(), "tests": state["tests"],
            "modules": {name: state["modules"][name] for name in order}, "sources": state["sources"]}
    path.write_text(json.dumps(head)[:-1] + ',\n "sets": [\n' + ",\n".join(f"  {list(found)}" for found in sets)
                    + '],\n "files": {\n' + ",\n".join(rows) + "\n}}\n")
    functions = sum(len(found) for found in files.values())
    return (f"{len(order)} modules, {len(files)} files, {functions} functions, {len(sets)} module sets, "
            f"{path.stat().st_size} bytes")


def parse_diff(text) -> dict[str, tuple[dict, dict]]:
    """``git diff -U0`` -> path -> (removed line number -> text, added line number -> text)."""
    found, path, header, old, new = {}, None, False, 0, 0
    for line in text.splitlines():
        if line.startswith("diff --git "):
            path, header = line.split(" b/", 1)[1], True
            found[path] = ({}, {})
        elif line.startswith("@@ "):
            removed, added = line.split(" ")[1:3]
            old, new, header = int(removed[1:].split(",")[0]), int(added[1:].split(",")[0]), False
        elif header or path is None:
            continue
        elif line.startswith("-"):
            found[path][0][old], old = line[1:], old + 1
        elif line.startswith("+"):
            found[path][1][new], new = line[1:], new + 1
    return found


def changes(args) -> dict[str, Change | None]:
    """Changed path -> its changed lines; None: no line information, the whole file counts."""
    if args.paths:
        paths = [Path(path) for path in args.paths]
        return {path.relative_to(REPO).as_posix() if path.is_relative_to(REPO) else str(path): None for path in paths}
    old, _, new = (args.diff or "HEAD").partition("..")
    diff = git("diff", "-U0", "--no-color", "--no-renames", "--no-ext-diff", old, *filter(None, [new]))
    out = {path: Change(path, old, new or None, *lines) if any(lines) else None
           for path, lines in parse_diff(diff).items()}
    if not new:
        out.update((path, None) for path in git("ls-files", "--others", "--exclude-standard").splitlines())
    return out


def source(rev, path) -> str:
    """The file's text at ``rev`` (None: the working tree), "" when it is not there; a binary file
    (an image under docs/) comes back with its undecodable bytes replaced."""
    if rev is None:
        return (REPO / path).read_text(errors="replace") if (REPO / path).is_file() else ""
    shown = subprocess.run(["git", "show", f"{rev}:{path}"], cwd=REPO, capture_output=True, text=True,
                           errors="replace")
    return shown.stdout if shown.returncode == 0 else ""


def defined(tree) -> list[tuple[ast.AST, str, bool]]:
    """(node, qualified name as in co_qualname, is a function) per def and class, outer ones first."""
    found = []

    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                function = not isinstance(child, ast.ClassDef)
                found.append((child, prefix + child.name, function))
                walk(child, prefix + child.name + (".<locals>." if function else "."))
            else:
                walk(child, prefix)

    walk(tree, "")
    return found


def definitions(text) -> list[tuple[int, int, str, bool]]:
    """(first line with decorators, last line, qualified name as in co_qualname, is a function) per def and class."""
    return [(min([node.lineno] + [decorator.lineno for decorator in node.decorator_list]), node.end_lineno, name,
             function) for node, name, function in defined(ast.parse(text) if isinstance(text, str) else text)]


def docstrings(tree) -> list[tuple[int, int, str]]:
    """(first line, last line, its def or class; "" for the module's) per docstring."""
    owners = [(tree, "")] + [(node, name) for node, name, _ in defined(tree)]
    return [(node.body[0].lineno, node.body[0].end_lineno, name) for node, name in owners
            if node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)]


def bound(node) -> set[str] | None:
    """The names an import or an assignment to plain names binds; None for other statements."""
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        names = {alias.asname or alias.name.split(".")[0] for alias in node.names}
        return None if "*" in names or getattr(node, "module", None) == "__future__" else names
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        items = [item for target in targets for item in (target.elts if isinstance(target, (ast.Tuple, ast.List))
                                                         else [target])]
        return {item.id for item in items} if all(isinstance(item, ast.Name) for item in items) else None
    return None


def statements(tree) -> list[ast.AST]:
    """The statements outside functions and classes, into if/try/with blocks."""
    found, stack = [], list(tree.body)
    while stack:
        node = stack.pop()
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found.append(node)
            for field in ("body", "orelse", "finalbody", "handlers"):
                stack.extend(getattr(node, field, ()))
    return found


def origins(tree) -> dict[str, set[tuple]]:
    """Name -> (module, attribute or None, alias) per import outside functions that binds it."""
    found = defaultdict(set)
    for node in statements(tree):
        for alias in node.names if isinstance(node, (ast.Import, ast.ImportFrom)) else ():
            module = "." * node.level + (node.module or "") if isinstance(node, ast.ImportFrom) else alias.name
            found[alias.asname or alias.name.split(".")[0]].add(
                (module, alias.name if isinstance(node, ast.ImportFrom) else None, alias.asname))
    return found


def readers(tree) -> tuple[dict[str, set[str]], set[str], dict[str, set[str]]]:
    """The names each outermost function (a method counts) reads in its body; the names read outside
    function and class bodies: statements, decorators, defaults and, unless postponed, annotations; and
    those read by each class: its decorators, bases and statements, its methods' decorators and defaults."""
    postponed = any(isinstance(node, ast.ImportFrom) and node.module == "__future__"
                    and any(alias.name == "annotations" for alias in node.names) for node in tree.body)
    functions, top, classes = {}, set(), {}

    def reads(*nodes):
        found = set()
        for child in (child for node in nodes for child in ast.walk(node)):
            if isinstance(child, ast.Name) and not isinstance(child.ctx, ast.Store):
                found.add(child.id)
            elif isinstance(child, (ast.Global, ast.Nonlocal)):
                found.update(child.names)
        return found

    def block(nodes, prefix, sink):
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = node.args
                sink.update(reads(*node.decorator_list, *args.defaults, *filter(None, args.kw_defaults)))
                if not postponed:
                    every = (*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg)
                    sink.update(reads(*(arg.annotation for arg in every if arg and arg.annotation),
                                      *filter(None, [node.returns])))
                functions.setdefault(prefix + node.name, set()).update(reads(*node.body))  # A property setter too.
            elif isinstance(node, ast.ClassDef):
                own = classes.setdefault(prefix + node.name, set())
                own.update(reads(*node.decorator_list, *node.bases, *node.keywords))
                block(node.body, prefix + node.name + ".", own)
            else:
                sink.update(reads(node))

    block(tree.body, "", top)
    return functions, top, classes


def attribute_uses(tree, names, listing=True) -> tuple[dict[str, set[str]], set[str]]:
    """Which of names each outermost function (a method counts) uses as an attribute (``x.name``), a
    keyword (``f(name=...)``) or a string ("name"), and those used outside function bodies; with listing,
    code that lists every field (LISTS_FIELDS: ``asdict(x)``, ``vars(x)``, ``x._fields``) uses them all."""
    functions, top = {}, set()

    def uses(*nodes):
        found = set()
        for child in (child for node in nodes for child in ast.walk(node)):
            word = (child.attr if isinstance(child, ast.Attribute) else child.arg if isinstance(child, ast.keyword)
                    else child.value if isinstance(child, ast.Constant) and isinstance(child.value, str) else None)
            called = child.func if isinstance(child, ast.Call) else None
            listed = (getattr(called, "id", None) or getattr(called, "attr", None) if called is not None
                      else word if isinstance(child, ast.Attribute) and word.startswith("_") else None)
            if listing and listed in LISTS_FIELDS:  # asdict(x), dataclasses.fields(x), x._fields; not a variable.
                found |= names
            elif word in names:
                found.add(word)
        return found

    def block(nodes, prefix):
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                top.update(uses(*node.decorator_list, *node.args.defaults, *filter(None, node.args.kw_defaults)))
                if found := uses(*node.body):
                    functions.setdefault(prefix + node.name, set()).update(found)
            elif isinstance(node, ast.ClassDef):
                top.update(uses(*node.decorator_list, *node.bases, *node.keywords))
                block(node.body, prefix + node.name + ".")
            else:
                top.update(uses(node))

    block(tree.body, "")
    return functions, top


def class_names(tree, qualname, number) -> set[str] | None:
    """The names a statement directly in the body of class qualname binds at line number (a field, a
    class attribute); None for any other line there (the class line, its decorators, another statement)
    and for special names (``__slots__``, ``__hash__``)."""
    node = next((node for node, name, _ in defined(tree) if name == qualname), None)
    statement = next((child for child in node.body if child.lineno <= number <= child.end_lineno), None) if node else None
    names = bound(statement) if statement is not None else None
    return None if not names or any(name.startswith("__") and name.endswith("__") for name in names) else names


def module_of(path) -> str | None:
    """The name the tests import a file by: covey/a/b.py -> covey.a.b (a package's
    __init__.py: the package), tests/support/x.py -> support.x, tests/test_x.py -> test_x; else None."""
    parts = list(Path(path).with_suffix("").parts)
    if not path.endswith(".py"):
        return None
    if parts[0] == "extensions" and len(parts) > 3 and parts[2] == "tests":
        parts = parts[2:]  # An extension's tests: extensions/<package>/tests/... as tests/....
    elif parts[0] == "extensions" and len(parts) > 2 and parts[2] not in ("scripts", "tests"):
        parts = parts[1:]  # An extension's code: extensions/<package>/a/b.py -> <package>.a.b.
        return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)
    if parts[:2] == ["tests", "support"] or (len(parts) == 2 and parts[0] == "tests" and parts[1].startswith("test")):
        parts = parts[1:]
    elif parts[0] != "covey":
        return None
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


class Facts(NamedTuple):
    """What the import scan knows about one Python file."""
    module: str | None
    imports: frozenset  # Modules it may import, at any depth: ``from a.b import c`` -> a, a.b, a.b.c.
    refs: frozenset  # "module.name" it takes: ``from module import name``, an attribute chain on an import.
    strings: frozenset  # Its string constants that may name an attribute ("name", "module.name.x").
    attributes: frozenset  # The attribute and keyword names it uses (x.name, f(name=...)), and LISTS_FIELDS it calls.


def facts(path, text) -> Facts | None:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    module = module_of(path) or ""
    package = module.split(".") if path.endswith("__init__.py") else module.split(".")[:-1]
    imports, refs, strings, attributes, aliases = set(), set(), set(), set(), {}

    def add(name):
        parts = name.split(".")
        imports.update(".".join(parts[:n]) for n in range(1, len(parts) + 1))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                add(alias.name)
                aliases[alias.asname or alias.name.split(".")[0]] = alias.name if alias.asname else alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                base = ".".join(package[:len(package) + 1 - node.level] + ([base] if base else []))
            add(base)
            for alias in node.names:
                imports.add(f"{base}.{alias.name}")
                refs.add(f"{base}.{alias.name}")
                aliases[alias.asname or alias.name] = f"{base}.{alias.name}"
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and DOTTED.fullmatch(node.value):
            strings.add(node.value)
        elif isinstance(node, (ast.Attribute, ast.keyword)) and (word := getattr(node, "attr", None) or node.arg):
            attributes.add(word)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in LISTS_FIELDS:
            attributes.add(node.func.id)
    for node in ast.walk(tree):  # Attribute chains, once every alias is known.
        if isinstance(node, ast.Attribute):
            parts, head = [node.attr], node.value
            while isinstance(head, ast.Attribute):
                parts.append(head.attr)
                head = head.value
            if isinstance(head, ast.Name) and head.id in aliases:
                refs.add(".".join([aliases[head.id], *reversed(parts)]))
    return Facts(module_of(path), frozenset(imports), frozenset(refs), frozenset(strings), frozenset(attributes))


def scan(rev) -> dict[str, Facts | None]:
    """facts() of each Python file under PYTHON at rev (None: the working tree). Parsing them all takes
    2-4 s on the NAS, so each file's facts are kept in SCAN_CACHE next to the ledger, keyed by the file's
    path, mtime and size (at a commit: its blob id); only the files changed since are read and parsed."""
    cache_path = ledger_path(None)
    cache_path = cache_path and cache_path.parent / SCAN_CACHE
    version = digest([inspect.getsource(facts), inspect.getsource(module_of), DOTTED.pattern, *sorted(LISTS_FIELDS)])
    try:
        saved = json.loads(cache_path.read_text()) if cache_path else {}
        cached = saved["entries"] if saved.get("version") == version else {}
    except (OSError, ValueError, KeyError, TypeError):
        cached = {}
    if rev is None:
        paths = [path for path in git("ls-files", "--cached", "--others", "--exclude-standard", "--", *PYTHON).splitlines()
                 if path.endswith(".py") and (REPO / path).is_file()]
        keys = {}
        for path in paths:
            stat = (REPO / path).stat()
            keys[path] = f"{REPO / path}:{stat.st_mtime_ns}:{stat.st_size}"
    else:
        listed = (line.split("\t", 1) for line in git("ls-tree", "-r", rev, "--", *PYTHON).splitlines())
        keys = {path: f"{meta.split()[2]}:{path}" for meta, path in listed if path.endswith(".py")}
    missing = [path for path, key in keys.items() if key not in cached]
    texts = tree_files(rev, missing) if missing else {}
    entries = {key: cached[key] for key in keys.values() if key in cached}
    for path in missing:
        found = facts(path, texts.get(path, ""))
        entries[keys[path]] = found and [found.module, *(sorted(field) for field in found[1:])]
    # Kept besides: the files at commits (a working-tree scan), or at up to as many other blobs (a commit's).
    other = {key: value for key, value in cached.items() if key.startswith("/") != (rev is None)}
    if rev is not None:
        other.update(list((key, value) for key, value in cached.items()
                          if not key.startswith("/") and key not in entries)[-len(keys):])
    if cache_path and (missing or len(other) + len(entries) < len(cached)):
        temp = cache_path.with_name(f".{SCAN_CACHE}.{os.getpid()}")
        try:
            temp.write_text(json.dumps({"version": version, "entries": {**other, **entries}}))
            temp.replace(cache_path)
        except OSError:
            temp.unlink(missing_ok=True)
    return {path: entries[key] and Facts(entries[key][0], *map(frozenset, entries[key][1:])) for path, key in keys.items()}


def tree_files(rev, paths) -> dict[str, str]:
    """Path -> text of each of paths at rev (None: the working tree)."""
    if rev is None:
        return {path: (REPO / path).read_text(errors="replace") for path in paths}
    out = subprocess.run(["git", "cat-file", "--batch"], cwd=REPO, input="".join(f"{rev}:{path}\n" for path in paths).encode(),
                         capture_output=True, check=True).stdout
    texts, at = {}, 0
    for path in paths:  # "<sha> blob <size>\n<content>\n" per path.
        end = out.index(b"\n", at)
        size = int(out[at:end].split()[2])
        texts[path], at = out[end + 1:end + 1 + size].decode(errors="replace"), end + 2 + size
    return texts


def importers(files, module) -> list[str]:
    """The files that may import module."""
    return sorted(path for path, found in files.items() if found and module in found.imports and found.module != module)


def test_importers(files, module, through_support=True) -> set[str]:
    """The test modules that import module; through_support: also those importing a tests/support/ file that does."""
    out, seen, wanted = set(), set(), [module]
    while wanted:
        for path in importers(files, wanted.pop()):
            if is_test(path):
                out.add(Path(path).stem)
            elif through_support and is_support(path) and path not in seen:
                seen.add(path)
                wanted.append(files[path].module)
    return out


def mentions(files, module, name) -> list[str]:
    """The other files that import module and may use its name: ``from module import name`` (or *),
    ``module.name``, or a string naming it (``mock.patch.object(module, "name")``, "module.name.x")."""
    dotted = f"{module}.{name}"
    return sorted(path for path, found in files.items()
                  if found and found.module != module and module in found.imports
                  and (dotted in found.refs or f"{module}.*" in found.refs or name in found.strings
                       or any(text == dotted or text.startswith(dotted + ".") for text in found.strings)))


class Scope:
    """The map's modules per file, and the import scan of the new side of the change (rev None: the working tree)."""

    def __init__(self, data, rev):
        names = list(data["modules"])
        self.data, self.rev = data, rev
        self.sets = [{names[index] for index in members} for members in data["sets"]]

    def functions(self, path) -> dict[str, set[str]]:
        return {name: self.sets[number] for name, number in self.data["files"].get(path, {}).items()}

    def ran(self, path) -> set[str]:
        return set().union(*self.functions(path).values())

    @cached_property
    def files(self) -> dict[str, Facts | None]:
        return scan(self.rev)

    def whole(self, path) -> set[str]:
        """Every module that ran anything in path, and the test modules that import it."""
        module = module_of(path)
        return self.ran(path) | (test_importers(self.files, module, is_support(path)) if module else set())

    def deleted(self, path) -> set[str]:
        """The modules that ran anything in a deleted file or still import it (directly, or a file they ran does)."""
        modules, module = self.ran(path), module_of(path)
        if module:
            modules |= test_importers(self.files, module)
            for other in importers(self.files, module):
                modules |= self.ran(other)
        return modules

    def unmapped(self, path, new) -> set[str] | None:
        """A Python file the map does not know: the test modules that import it (through support files too)
        and the modules that ran the files importing it (a new file: only when no test imports it)."""
        module = module_of(path)
        if not module:
            return None
        tests = test_importers(self.files, module)
        if new and tests:
            return tests
        return tests.union(*(self.ran(other) for other in importers(self.files, module))) or None


def touched(functions, change, scope) -> tuple[set[str], str]:
    """Modules the changed lines can affect, and why the whole file counts too ("" when it does not)."""
    picked, names, imported, trees, members, defined_on = set(), set(), set(), {}, defaultdict(set), {}

    def exists(rev, qualname):
        if rev not in defined_on:
            try:
                defined_on[rev] = {name for *_, name, _ in definitions(source(rev, change.path))}
            except SyntaxError:
                defined_on[rev] = set()
        return qualname in defined_on[rev]

    for rev, lines in ((change.old_rev, change.old), (change.new_rev, change.new)):
        code = sorted(n for n, text in lines.items() if text.strip() and not text.lstrip().startswith("#"))
        if not code:
            continue
        text = source(rev, change.path)
        try:
            trees[rev] = tree = ast.parse(text)
        except SyntaxError:
            return picked, "does not parse"
        spans, docs, outside = definitions(tree), docstrings(tree), statements(tree)
        for number in code:
            doc = next((doc for doc in docs if doc[0] <= number <= doc[1]), None)
            if doc and not doc[2]:
                names.add("__doc__")  # The module docstring: what reads __doc__ (an argparse description).
                continue
            if doc and not re.search(r"\.__doc__|getdoc", text):  # Read as an attribute: like other lines.
                continue
            around = sorted((span for span in spans if span[0] <= number <= span[1]), reverse=True)  # Innermost first.
            if not around:
                inner = min((node for node in outside if node.lineno <= number <= node.end_lineno),
                            key=lambda node: node.end_lineno - node.lineno, default=None)
                if inner is None or bound(inner) is None:
                    return picked, f"line {number} is outside functions"
                (imported if isinstance(inner, (ast.Import, ast.ImportFrom)) else names).update(bound(inner))
                continue
            first, last, name, function = around[0]
            other = change.new_rev if rev == change.old_rev else change.old_rev
            if all(n in lines for n in range(first, last + 1)) or not exists(other, name):
                picked |= functions.get(name, set())  # Added or removed as a whole.
                continue
            if not function:
                if (bound_here := class_names(tree, name, number)) is None:
                    return picked, f"line {number} is in the body of class {name}"
                members[name] |= bound_here  # A field or class attribute: what uses it, below.
                continue
            for _, _, outer, is_function in around:  # A nested function runs only inside its outer one.
                if not is_function:
                    return picked, f"{name} is not in the map"
                if outer in functions:
                    picked |= functions[outer]
                    break
            else:
                return picked, f"{name} is not in the map"
    if not (names or imported or members):
        return picked, ""
    for rev in (change.old_rev, change.new_rev):
        if rev not in trees and (text := source(rev, change.path)):
            try:
                trees[rev] = ast.parse(text)
            except SyntaxError:
                return picked, "does not parse"
    for qualname, attributes in sorted(members.items()):
        modules, why = class_members(functions, change, scope, trees, qualname, attributes)
        picked |= modules
        if why:
            return picked, why
    if not (names or imported):
        return picked, ""
    sides = [origins(trees[rev]) if rev in trees else {} for rev in (change.old_rev, change.new_rev)]
    if imported:  # Only the names an import now binds to something else (all of them for a moved import).
        names |= {name for name in imported if sides[0].get(name) != sides[1].get(name)} or imported
    loaded = [{found[0] for bound in side.values() for found in bound} for side in sides]
    used, module = [], module_of(change.path)
    for rev, tree in trees.items():
        lines = change.old if rev == change.old_rev else change.new
        whole = {name for first, last, name, _ in definitions(tree) if all(n in lines for n in range(first, last + 1))}
        used.append((rev, *readers(tree), whole))
    for name in sorted(names):
        read = False
        for rev, reading, top, classes, whole in used:
            other = change.new_rev if rev == change.old_rev else change.old_rev
            if name in top:
                return picked, f"{name} is read outside functions"
            for qualname in (qualname for qualname, found in classes.items() if name in found):
                if qualname not in whole and exists(other, qualname):
                    return picked, f"{name} is read by class {qualname}"
                read = True  # A class added or removed with this change: its users count.
            for qualname in (qualname for qualname, found in reading.items() if name in found):
                if (qualname in whole or not exists(other, qualname)) and qualname not in functions:  # Added, removed.
                    read = True
                    continue
                if qualname not in functions:
                    return picked, f"{qualname} reads {name} and is not in the map"
                picked |= functions[qualname]
                read = True
        for path in mentions(scope.files, module, name) if module and name != "__doc__" else ():
            picked |= {Path(path).stem} if is_test(path) else scope.whole(path)
            read = True
        modules = {found[0] for side in sides for found in side.get(name, ())}
        if not read and name != "__doc__" and not (modules and all(modules <= side for side in loaded)):
            return picked, f"nothing reads {name}"  # Unless its module is imported before and after.
    return picked, ""


def class_members(functions, change, scope, trees, qualname, attributes) -> tuple[set[str], str]:
    """Modules a change to the fields or class attributes ``attributes`` of class qualname can affect: those
    that ran its methods or a function that uses one of the names (attribute_uses), in this file or another,
    and no more than the whole file would select (the rule before class-level names were followed); and why
    the whole file counts ("" when it does not): a name read in the class body or outside functions, a
    reader in this file the map does not know, fields that changed order."""
    picked = set().union(*(modules for name, modules in functions.items() if name.startswith(qualname + ".")))
    fields = []
    for tree in trees.values():
        node = next((node for node, name, _ in defined(tree) if name == qualname), None)
        if node is None:
            continue
        body = [child for child in node.body if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))]
        decorations = [part for child in node.body if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                       for part in (*child.decorator_list, *child.args.defaults, *filter(None, child.args.kw_defaults))]
        if read := attributes & {child.id for part in (*body, *decorations) for child in ast.walk(part)
                                 if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load)}:
            return picked, f"{min(read)} is read in the body of class {qualname}"
        fields.append([child.target.id for child in node.body
                       if isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name)])
    if len(fields) == 2 and fields[0][:len(fields[1])] != fields[1][:len(fields[0])]:
        return picked, f"the fields of class {qualname} changed order"  # Positional construction, unpacking.
    for rev, tree in trees.items():
        uses, top = attribute_uses(tree, attributes)
        if top:
            return picked, f"{min(top)} is used outside functions"
        lines = change.old if rev == change.old_rev else change.new
        whole = {name for first, last, name, _ in definitions(tree) if all(n in lines for n in range(first, last + 1))}
        for name in uses:
            if name in functions:
                picked |= functions[name]
            elif name not in whole:  # Added or removed with this change: its callers count.
                return picked, f"{name} uses {min(uses[name])} and is not in the map"
    module, simple = module_of(change.path), qualname.split(".")[0]
    for path, found in sorted(scope.files.items()):
        # Listing every field counts where the class is known by name: asdict(self) elsewhere is another class.
        listing = bool(found) and (f"{module}.{simple}" in found.refs or simple in found.attributes | found.strings)
        if path == change.path or not found or not (attributes & (found.attributes | found.strings)
                                                    or listing and LISTS_FIELDS & found.attributes):
            continue
        try:
            uses, top = attribute_uses(ast.parse(source(scope.rev, path)), attributes, listing)
        except SyntaxError:
            uses, top = {}, {"?"}
        known = scope.functions(path)
        if is_test(path):
            picked |= {Path(path).stem} if uses or top else set()
        elif top or (uses and not known):  # Read outside functions, or by a file the map has nothing of.
            picked |= scope.whole(path)
        else:  # A function no test ran has nothing to run.
            picked |= set().union(*(known[name] for name in uses if name in known))
    return picked & scope.whole(change.path), ""


def affected(data, found) -> tuple[set[str], list[str]]:
    """Test modules for the changed paths, and the paths that need the full suite."""
    scope = Scope(data, next((change.new_rev for change in found.values() if change), None))
    picked, full = set(), []
    for path, change in sorted(found.items()):
        lines = list(change.old.values()) + list(change.new.values()) if change else []
        if change and path.endswith(".py") and all(not t.strip() or t.lstrip().startswith("#") for t in lines):
            note(f"{path}: comments and blank lines only")
        elif is_test(path) and path.endswith(".py"):
            picked.add(Path(path).stem)
        elif path == SELF:
            picked.add(SELF_TESTS)
        elif path.endswith(".md") or path.startswith("docs/") or path in UNREAD:
            note(f"{path}: selects no module ({', '.join(ALWAYS)} runs anyway)")
        elif (change is None or not change.new) and not source(change.new_rev if change else None, path):
            modules = scope.deleted(path)
            note(f"{path}: deleted, {len(modules)} modules ran or import it")
            picked |= modules
        elif path in data["files"]:
            if change and path.endswith(".py"):
                modules, why = touched(scope.functions(path), change, scope)
                if why:
                    note(f"{path}: whole file, {why}")
                    modules |= scope.whole(path)
            else:
                modules = scope.whole(path)
            picked |= modules
        elif (modules := scope.unmapped(path, new=not source(change.old_rev if change else "HEAD", path))) is not None:
            note(f"{path}: not in the map, {len(modules)} modules import it or ran its importers")
            picked |= modules
        else:
            full.append(path)
    return picked, full


def choose(args, data, state) -> list[str] | None:
    """The test modules for the change in args; None: the full suite. Reasons go to stderr."""
    if not data or data.get("version") != VERSION:
        note(f"full suite: {stale(data, state)[0]} (rebuild: build)")
        return None
    for reason in stale(data, state):
        note(f"the map is stale (rebuild: build): {reason}")
    new = {name for name in state["modules"] if name not in data["modules"]}
    if new:
        note("not in the map yet: " + " ".join(sorted(new)))
    picked, full = affected(data, changes(args))
    if full:
        note("full suite, no test module maps to: " + " ".join(full))
        return None
    picked = sorted((picked | new | set(ALWAYS)) & set(state["modules"]))
    note(f"{len(picked)} of {len(state['modules'])} modules, {sum(state['modules'][n] for n in picked)} of "
         f"{sum(state['modules'].values())} tests (map built at {data['built_at']})")
    return picked


def select(args) -> int:
    prepare()
    _, state = suite_state()
    picked = choose(args, json.loads(args.map.read_text()) if args.map.exists() else None, state)
    print(FULL + "".join(f" -k '{name}.*'" for name in picked) if picked is not None else FULL)
    return 0


def run(args) -> int:
    prepare()
    suites, state = suite_state()
    data = json.loads(args.map.read_text()) if args.map.exists() else None
    picked = None if args.full else choose(args, data, state)
    reasons = stale(data, state) if picked is None else []
    for name, error in sorted(state.get("broken", {}).items()):
        if picked is None or name in picked:
            print(f"cannot import {name} here ({error}): it runs as one error, its cases are not compared with the map",
                  file=sys.stderr)
    if reasons and not args.force:
        print(f"refused: a full run on a stale map ({'; '.join(reasons)}). `build` runs the same tests plus the slow "
              "tier and refreshes the map: run `python3 -B scripts/test_map.py build --why ...` instead and commit "
              "tests/test_map.json (--force runs the full suite anyway)", file=sys.stderr)
        return 2
    entry = ledger_entry(args, "run", "full" if picked is None else picked,
                         slow=os.environ.get("BRIDGE_TESTS_SLOW") == "1")
    done = [] if args.force else [done for done in read_ledger(entry["ledger"]) if covers(done, entry)]
    hit = done[-1] if done else None
    if hit:
        print(f"skipped, this tree already passed: {json.dumps(hit, ensure_ascii=False)} (--force runs anyway)")
        append(entry, {"start": stamp(time.time()), **entry, "result": "skipped", "covered_by": hit["start"]})
        return 0
    start = time.time()
    with test_temp(args.test_tmpdir) as temp:
        result, _ = run_modules(list(suites) if picked is None else picked, args.jobs, entry, temp)
    return finish(args, entry, result, start)


class Ran(NamedTuple):
    """A test a child ran, back in the parent: ``id()`` the test's (a subtest's test), str the full name."""
    test: str
    shown: str

    def id(self) -> str:
        return self.test

    def __str__(self) -> str:
        return self.shown


def run_modules(names, jobs, entry, temp, hooks=None) -> tuple[unittest.TestResult, dict[str, dict]]:
    """Each test module in its own child process (``child``), ``jobs`` at a time, the slowest last time first
    (TIMES next to the ledger; unknown ones in name order after them); with ``hooks`` (build) each records what it ran. -> the
    children's results merged as one unittest result (failures and errors printed as unittest does), and per
    module its report (see run_module). A module that crashes, fails to start or exceeds MODULE_TIMEOUT is an
    error of its own; an interrupt or SIGTERM kills every child's process group."""
    times_path = entry["ledger"].parent / TIMES if entry["ledger"] else None
    try:
        times = json.loads(times_path.read_text()) if times_path and times_path.exists() else {}
    except ValueError:
        times = {}
    counts = {name: index for index, name in enumerate(test_modules())}
    order = sorted(names, key=lambda name: (-times.get(name, 0), counts.get(name, 0)))
    work = Path(tempfile.mkdtemp(prefix="test_map-jobs-"))  # Under the test temp dir: removed with it.
    live, lock, stop = set(), threading.Lock(), threading.Event()
    reports: dict[str, dict] = {}
    previous = signal.signal(signal.SIGTERM, interrupted) if threading.current_thread() is threading.main_thread() else None
    pool = ThreadPoolExecutor(max(1, jobs))
    try:
        futures = {pool.submit(run_module, name, work, temp, hooks, (live, lock, stop)): name for name in order}
        for future in as_completed(futures):
            reports[futures[future]] = future.result()
    except BaseException:
        with lock:
            stop.set()
            for child in live:
                kill_group(child)
        pool.shutdown(cancel_futures=True)
        raise
    finally:
        pool.shutdown()
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)
        shutil.rmtree(work, ignore_errors=True)
    if times_path:
        times.update((name, report["seconds"]) for name, report in reports.items() if report["seconds"] is not None)
        with contextlib.suppress(OSError):
            times_path.parent.mkdir(parents=True, exist_ok=True)
            times_path.write_text(json.dumps(dict(sorted(times.items())), indent=0) + "\n")
    result = unittest.TestResult()
    for name in names:  # Module order, as a serial run printed them.
        result.testsRun += reports[name]["ran"]
        for kind, items in reports[name]["lists"].items():
            getattr(result, kind).extend(items)
    for flavour, items in (("ERROR", result.errors), ("FAIL", result.failures)):
        for test, text in items:
            print(f"{'=' * 70}\n{flavour}: {test}\n{'-' * 70}\n{text}", file=sys.stderr)
    return result, reports


def interrupted(*_):
    raise KeyboardInterrupt("SIGTERM")


def kill_group(child) -> None:
    with contextlib.suppress(OSError):
        os.killpg(child.pid, signal.SIGKILL)


def run_module(name, work, temp, hooks, control) -> dict:
    """One test module in a child -> {"ran": tests started, "lists": unittest result lists (kind -> [(Ran,
    text)], unexpectedSuccesses [Ran]), "seconds": its time (None when it did not finish), "coverage": with
    hooks the lines "path<TAB>qualname" it recorded}. A child that ends without its report (a crash, a kill,
    MODULE_TIMEOUT) adds an error naming the test it was in and its output's tail."""
    live, lock, stop = control
    out, log = work / f"{name}.jsonl", work / f"{name}.log"
    env = {key: value for key, value in os.environ.items() if key != "TEST_MAP_CHILD"}  # Its own hook stays off.
    if temp:  # Test temp files per module: no two modules share a TMPDIR.
        (temp / name).mkdir(exist_ok=True)
        env["TMPDIR"] = str(temp / name)
    command = [sys.executable, "-B", str(Path(__file__).resolve()), "child", name, str(out)]
    if hooks:
        (hooks / name).mkdir(exist_ok=True)
        command += ["--hooks", str(hooks / name)]
    problem, start, child = None, time.time(), None
    with log.open("w") as sink, lock:
        if stop.is_set():
            raise KeyboardInterrupt("stopped")
        try:
            child = subprocess.Popen(command, cwd=REPO, env=env, stdin=subprocess.DEVNULL, stdout=sink,
                                     stderr=subprocess.STDOUT, start_new_session=True)  # Its own process group.
            live.add(child)
        except OSError as error:  # Out of processes or memory on a loaded machine: this module's error.
            problem = f"could not start: {error}"
    try:
        code = child.wait(timeout=MODULE_TIMEOUT) if child else None
        if code:
            problem = f"exit code {code}" if code > 0 else f"killed by signal {-code}"
    except subprocess.TimeoutExpired:
        problem = f"timed out after {MODULE_TIMEOUT} s"
    finally:
        if child:
            kill_group(child)  # Its leftovers too: a serial run left them to the next module.
            child.wait()
            with lock:
                live.discard(child)
    lists, ran, running, seconds, coverage = {kind: [] for kind in LIST_KINDS}, 0, None, None, []
    for line in out.read_text(errors="replace").splitlines() if out.exists() else ():
        try:
            event = json.loads(line)
        except ValueError:
            continue  # A line cut by the kill.
        if "start" in event:
            ran, running = ran + 1, event["start"]
        elif "kind" in event:
            test = Ran(event["test"], event["shown"])
            lists[event["kind"]].append(test if event["kind"] == "unexpectedSuccesses" else (test, event["text"]))
        elif "done" in event:
            seconds, running, coverage = event["done"], None, event.get("coverage", [])
    if seconds is None or problem:
        tail = log.read_text(errors="replace").splitlines()[-20:] if log.exists() else []
        where = f" in {running}" if running else " outside a test"
        text = (f"{name}: no report from its child process ({problem or 'it ended without one'}{where}, "
                f"after {time.time() - start:.0f} s); its output ends:\n" + "\n".join(tail))
        lists["errors"].append((Ran(running or name, f"{name} ({problem or 'no report'})"), text))
        seconds = None
    return {"ran": ran, "lists": lists, "seconds": seconds, "coverage": coverage}


def child(args) -> int:
    """``child MODULE OUT [--hooks DIR]``: run one test module in this process for run_module, one JSON line
    per event to OUT as it goes (a test started, a result list entry, done with the seconds). With --hooks
    (build) it records each function its tests ran (PY_START once per code object) and each non-Python file
    they opened, as paths relative to the repo, in the done line; its child processes report to DIR through
    CHILD_HOOK. The module loads before recording starts, as build loaded every module first."""
    prepare()
    try:
        suite = unittest.defaultTestLoader.loadTestsFromName(args.module)
    except unittest.SkipTest:  # A module that needs an extension that is not enabled (support.require_extension).
        suite = unittest.TestSuite()
    sink = open(args.out, "a", buffering=1)
    sent = dict.fromkeys(LIST_KINDS, 0)

    def emit(**event) -> None:
        sink.write(json.dumps(event) + "\n")
        sink.flush()

    def flush(result) -> None:  # New entries of the result's lists; fixtures fail outside any test.
        for kind in LIST_KINDS:
            items = getattr(result, kind)
            for item in items[sent[kind]:]:
                test, text = item if isinstance(item, tuple) else (item, "")
                emit(kind=kind, test=getattr(test, "test_case", test).id(), shown=str(test), text=text)
            sent[kind] = len(items)

    class Streamed(unittest.TestResult):
        def startTest(self, test):
            flush(self)
            super().startTest(test)
            emit(start=test.id())

        def stopTest(self, test):
            super().stopTest(test)
            flush(self)

    ran, names, active = set(), {}, [False]

    def record(path, qualname="") -> None:
        if path not in names:
            names[path] = repo_path(path)
        if names[path]:
            ran.add(names[path] + "\t" + folded(qualname))

    def started(code, _offset):
        if code.co_flags & inspect.CO_NEWLOCALS:  # A function, not module or class-body code.
            record(code.co_filename, code.co_qualname)
        return sys.monitoring.DISABLE  # Once per code object.

    def audited(event, arguments):
        if event == "open" and active[0] and isinstance(arguments[0], (str, os.PathLike)):
            path = os.fspath(arguments[0])
            if isinstance(path, str) and not path.endswith(".py"):
                record(path)

    result, start, mon = Streamed(), time.time(), getattr(sys, "monitoring", None)
    if args.hooks and mon is None:  # Only recording which functions ran needs it; running the tests does not.
        sys.exit("test_map build: recording coverage needs Python 3.12 (sys.monitoring)")
    if args.hooks:
        os.environ["TEST_MAP_CHILD"] = args.hooks
        mon.use_tool_id(mon.COVERAGE_ID, "test_map")
        mon.register_callback(mon.COVERAGE_ID, mon.events.PY_START, started)
        sys.addaudithook(audited)
        active[0] = True
        mon.set_events(mon.COVERAGE_ID, mon.events.PY_START)
    try:
        suite.run(result)
    finally:
        active[0] = False
        if args.hooks:
            mon.set_events(mon.COVERAGE_ID, 0)
    flush(result)
    emit(done=round(time.time() - start, 2), coverage=sorted(ran))
    sink.close()
    return 0


def tmpfs(path) -> bool:
    try:
        return any(line.split()[1:3] == [str(path), "tmpfs"] for line in Path("/proc/mounts").read_text().splitlines())
    except OSError:
        return False


@contextlib.contextmanager
def test_temp(value):
    """TMPDIR and tempfile point at a fresh directory for this run's test temp files; removed after."""
    base = value or os.environ.get("BRIDGE_TEST_TMPDIR") or ""
    if base == "inherit" or (not base and not (tmpfs(SHM) and os.access(SHM, os.W_OK))):
        yield None
        return
    previous = tempfile.gettempdir()
    saved = {name: os.environ.get(name) for name in ("TMPDIR", "BRIDGE_TEST_EXEC_TMPDIR")}
    path = Path(tempfile.mkdtemp(prefix=f"bridge-tests-{os.getuid()}-", dir=base or SHM))
    os.environ["TMPDIR"] = tempfile.tempdir = str(path)
    os.environ["BRIDGE_TEST_EXEC_TMPDIR"] = saved["BRIDGE_TEST_EXEC_TMPDIR"] or previous
    note(f"test temp files in {path} (removed afterwards); files to execute in {os.environ['BRIDGE_TEST_EXEC_TMPDIR']}")
    try:
        yield path
    finally:
        tempfile.tempdir = previous
        for name, old in saved.items():
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old
        shutil.rmtree(path, ignore_errors=True)


def ledger_path(value) -> Path | None:
    """--ledger, $BRIDGE_TEST_LEDGER or test-ledger.jsonl next to $TMPDIR; None in the repo, $HOME or system temp."""
    def allowed(path):
        return not any(place.is_relative_to(base) for place in (path, path.resolve())
                       for root in (REPO, *FORBIDDEN) for base in (root, root.resolve()))

    value = value or os.environ.get("BRIDGE_TEST_LEDGER")
    if not value and os.environ.get("TMPDIR") and allowed(Path(os.environ["TMPDIR"]).absolute()):
        value = str(Path(os.environ["TMPDIR"]).absolute().parent / "test-ledger.jsonl")
    path = Path(value).absolute() if value else None
    return path if path and allowed(path) else None


def ledger_entry(args, command, scope, slow) -> dict:
    path = ledger_path(args.ledger)
    if path is None:
        note("no ledger: set BRIDGE_TEST_LEDGER (or a TMPDIR outside the repo, $HOME and /tmp) to log runs "
             "and skip repeats")
    return {"why": args.why, "command": command, "scope": scope, "slow": slow,
            "head": git("rev-parse", "--short", "HEAD").strip(), "tree": fingerprint(), "ledger": path}


def fingerprint() -> str:
    """Hash of the content under DIRS and DOCS, not of HEAD: committing the tested tree keeps it. The map file is
    left out."""
    blobs = {}
    for line in git("ls-tree", "-r", "HEAD", "--", *DIRS, *DOCS).splitlines():
        meta, path = line.split("\t", 1)
        blobs[path] = meta.split()[2]
    changed = (git("diff", "--name-only", "HEAD", "--", *DIRS, *DOCS).splitlines()
               + git("ls-files", "--others", "--exclude-standard", "--", *DIRS, *DOCS).splitlines())
    present = [path for path in changed if (REPO / path).is_file()]
    for path in changed:
        blobs.pop(path, None)
    if present:
        blobs.update(zip(present, git("hash-object", "--stdin-paths", stdin="\n".join(present) + "\n").split()))
    return digest(f"{path} {blob}" for path, blob in sorted(blobs.items()) if path not in UNREAD)


def covers(done, wanted) -> bool:
    """Did the passing run ``done``, on the same content, run every test ``wanted`` runs?"""
    whole = done.get("scope") in ("full", "build")
    return (done.get("result") == "pass" and done.get("tree") == wanted["tree"]
            and (done.get("slow") or not wanted["slow"])
            and (wanted["command"] == "run" or done.get("command") == "build")
            and (whole or (wanted["scope"] not in ("full", "build") and set(wanted["scope"]) <= set(done["scope"]))))


def read_ledger(path) -> list[dict]:
    entries = []
    for line in path.read_text().splitlines() if path and path.exists() else ():
        try:
            entries.append(json.loads(line))
        except ValueError:
            pass
    return entries


def stamp(when) -> str:
    return datetime.fromtimestamp(when, timezone.utc).astimezone().isoformat(timespec="seconds")


def append(entry, record) -> None:
    if entry["ledger"]:
        record = {key: value for key, value in record.items() if key != "ledger"}
        entry["ledger"].parent.mkdir(parents=True, exist_ok=True)
        with entry["ledger"].open("a") as ledger:
            ledger.write(json.dumps(record, ensure_ascii=False) + "\n")


def failed_ids(result) -> list[str]:
    """The ids of the tests with failures or errors, in order, FAILED at most: "test_x.SomeTests.test_y";
    a subtest as its test, a setUpClass or import error as unittest names it."""
    found = []
    for test, _ in result.failures + result.errors:
        name = getattr(test, "test_case", test).id()  # A subtest's id ends with its parameters.
        if name not in found:
            found.append(name)
    return found[:FAILED]


def finish(args, entry, result, start) -> int:
    end, ok, failed = time.time(), result.wasSuccessful(), failed_ids(result)
    append(entry, {"start": stamp(start), "end": stamp(end), "seconds": round(end - start, 1), **entry,
                   "ran": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
                   "skipped": len(result.skipped), "result": "pass" if ok else "fail",
                   **({"failed": failed} if failed else {})})
    scope = entry["scope"] if isinstance(entry["scope"], str) else f"{len(entry['scope'])} modules"
    print(f"{entry['command']} {scope}: ran {result.testsRun} tests in {end - start:.0f}s, failures "
          f"{len(result.failures)}, errors {len(result.errors)}, skipped {len(result.skipped)}: "
          f"{'pass' if ok else 'FAIL'}; ledger {entry['ledger'] or '-'}")
    return 0 if ok else 1


def flaky(args) -> int:
    """Per test id the ledger's runs name in ``failed``, most often first: in how many runs it failed, when
    the last one started, at which HEADs. A failed run logged before ``failed`` was kept only counts."""
    path = ledger_path(args.ledger)
    if path is None or not path.exists():
        note(f"no ledger{f' at {path}' if path else ''}: set --ledger or BRIDGE_TEST_LEDGER (or a TMPDIR outside "
             "the repo, $HOME and /tmp)")
        return 2
    runs = [entry for entry in read_ledger(path) if entry.get("result") == "fail"]
    found: dict[str, dict] = {}
    for order, entry in enumerate(runs):  # The ledger is appended to: a later line is a later run.
        for test in entry.get("failed") or ():
            seen = found.setdefault(test, {"count": 0, "heads": []})
            seen.update(count=seen["count"] + 1, last=entry.get("start", "?"), order=order)
            if entry.get("head") and entry["head"] not in seen["heads"]:
                seen["heads"].append(entry["head"])
    unnamed = sum("failed" not in entry for entry in runs)
    print(f"{path}: {len(runs)} failed runs" + (f" ({unnamed} logged before the ledger named tests)" if unnamed else "")
          + f", {len(found)} tests named")
    for test, seen in sorted(found.items(), key=lambda item: (-item[1]["count"], -item[1]["order"], item[0])):
        heads = seen["heads"]
        shown = " ".join(heads[-6:]) + (f" and {len(heads) - 6} more" if len(heads) > 6 else "")
        print(f"{seen['count']:4}  last {seen['last']}  {test}  (at {shown or '?'})")
    return 0


def dotted_name(node) -> str | None:
    """a.b.c for a Name or an Attribute chain on one; else None."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        head = dotted_name(node.value)
        return f"{head}.{node.attr}" if head else None
    return None


def named_module(node, modules) -> str | None:
    """The module importlib.import_module("m") or sys.modules["m"] gives, when it is one of ``modules``."""
    if isinstance(node, ast.Call) and (dotted_name(node.func) or "").endswith("import_module") and node.args:
        arg = node.args[0]
    elif isinstance(node, ast.Subscript) and dotted_name(node.value) == "sys.modules":
        arg = node.slice
    else:
        return None
    return arg.value if isinstance(arg, ast.Constant) and arg.value in modules else None


def module_aliases(tree, modules) -> dict[str, set[str]]:
    """Name -> the modules a file binds it to anywhere: import a.b as n, from a import b (b a module),
    n = importlib.import_module("a.b") or sys.modules["a.b"]. A plain ``import a.b`` binds ``a``: resolved
    through the dotted chain (see module_objects)."""
    names = defaultdict(set)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname and alias.name in modules:
                    names[alias.asname].add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for alias in node.names:
                if f"{node.module}.{alias.name}" in modules:
                    names[alias.asname or alias.name].add(f"{node.module}.{alias.name}")
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            found = named_module(node.value, modules)
            if found:
                names[node.targets[0].id].add(found)
    return names


def module_objects(node, aliases, modules) -> set[str]:
    """The modules an expression may be: an alias, a dotted chain naming one, import_module(...), sys.modules[...]."""
    found = named_module(node, modules)
    if found:
        return {found}
    name = dotted_name(node)
    if not name:
        return set()
    head, _, rest = name.partition(".")
    result = {name} & modules
    result |= {full for base in aliases.get(head, ()) for full in [base + (f".{rest}" if rest else "")] if full in modules}
    return result


def split_target(target, modules) -> tuple[str, str] | None:
    """'a.b.c.d' -> (the longest prefix that is one of ``modules``, the rest); None without a rest (a module's
    own name, as import_module takes it)."""
    if target in modules:
        return None
    parts = target.split(".")
    for end in range(len(parts) - 1, 0, -1):
        if ".".join(parts[:end]) in modules:
            return ".".join(parts[:end]), ".".join(parts[end:])
    return None


def leading_text(node) -> str | None:
    """The literal text a string expression starts with: a constant, an f-string's text before its first {},
    the left end of a + chain."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        text = ""
        for part in node.values:
            if not isinstance(part, ast.Constant):
                break
            text += part.value
        return text
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return leading_text(node.left)
    return None


def patch_sites(module, sources, modules) -> list[tuple[str, int, str, str, str]]:
    """Each place in ``sources`` (path -> text) that replaces a name of ``module`` by its import path, as
    (path, line, kind, name, enclosing test or function). Kinds: patch (mock.patch("module.name") and its
    decorator), patch.object, patch.multiple and patch.dict on the module object, setattr (builtin or
    monkeypatch-style, on the module object or by a dotted string), assign (``module.name = ...``), string
    (any other "module.name" literal: a patch target kept in a variable, say) and dynamic, a target built at run
    time that may be one of the module's names: patch.object, setattr or patch.multiple(**...) on the module
    object with a name that is not a literal, or an f-string or a concatenation whose literal start may lead to
    the module ("covey.app.{name}.x" for each module under covey.app); its name is the
    expression. Patching an attribute of a class (patch.object(SomeClass, ...)) follows the class when it moves,
    so it is not listed."""
    found = []
    for path, text in sorted(sources.items()):
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        aliases = module_aliases(tree, modules)
        used = set()  # String constants a patch call above took, not listed again as kind string.

        def ours(node):
            return module in module_objects(node, aliases, modules)

        def by_string(node):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                split = split_target(node.value, modules)
                if split and split[0] == module:
                    used.add(id(node))
                    return split[1]
            return None

        def by_dynamic(node):
            """The expression, for an f-string or a concatenation whose literal start may reach ``module``."""
            start = leading_text(node)
            if not start or "." not in start:
                return None
            complete = start.rsplit(".", 1)[0]  # Whole segments; after the last dot the text goes on at run time.
            owner = complete if complete in modules else (split_target(complete, modules) or (None,))[0]
            if not (module.startswith(start) or owner == module):
                return None
            used.update(id(inner) for inner in ast.walk(node))
            return ast.unparse(node)

        def visit(node, where):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                inner = f"{where}.{node.name}" if where else node.name
                for child in [*node.decorator_list, *node.body]:  # A decorator's patch is its test's.
                    visit(child, inner)
                return
            hits = []
            if isinstance(node, ast.Call):
                func = dotted_name(node.func) or ""
                last = func.rsplit(".", 1)[-1]
                args = node.args
                first = args[0] if args else None
                second = args[1].value if len(args) > 1 and isinstance(args[1], ast.Constant) else None
                if last == "patch" and first is not None:
                    name = by_string(first)
                    hits += [("patch", name)] if name else []
                elif func.endswith("patch.object") and first is not None and ours(first) and len(args) > 1:
                    hits.append(("patch.object", second) if isinstance(second, str) else ("dynamic", ast.unparse(args[1])))
                elif func.endswith("patch.multiple") and first is not None:
                    if ours(first) or (isinstance(first, ast.Constant) and first.value == module):
                        used.add(id(first))
                        hits += [("patch.multiple", keyword.arg) if keyword.arg else
                                 ("dynamic", f"**{ast.unparse(keyword.value)}") for keyword in node.keywords]
                elif func.endswith("patch.dict") and first is not None:
                    name = by_string(first)
                    if not name and isinstance(first, ast.Attribute) and ours(first.value):
                        name = first.attr
                    hits += [("patch.dict", name)] if name else []
                elif last == "setattr" and first is not None:
                    name = by_string(first)
                    if not name and ours(first) and len(args) > 1:
                        name = second if isinstance(second, str) else None
                        hits += [] if name else [("dynamic", ast.unparse(args[1]))]
                    hits += [("setattr", name)] if name else []
            elif isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                    if isinstance(target, ast.Attribute) and ours(target.value):
                        hits.append(("assign", target.attr))
            elif isinstance(node, ast.Constant) and id(node) not in used:
                name = by_string(node)
                hits += [("string", name)] if name else []
            elif isinstance(node, (ast.JoinedStr, ast.BinOp)) and id(node) not in used:
                name = by_dynamic(node)
                hits += [("dynamic", name)] if name else []
            for kind, name in hits:
                found.append((path, node.lineno, kind, name, where or "<module>"))
            for child in ast.iter_child_nodes(node):
                visit(child, where)

        visit(tree, "")
    return sorted(found)


def patches(args) -> int:
    """``patches MODULE``: every place in tests/ that patches a name of MODULE by its import path (see
    patch_sites), so a change that moves a name out of MODULE knows which tests would stop pinning it."""
    files = [path for folder in ("covey", "tests/support") for path in (REPO / folder).rglob("*.py")
             if "__pycache__" not in path.parts]
    modules = {module_of(str(path.relative_to(REPO))) for path in files} - {None}
    wanted = args.module
    if wanted.endswith(".py"):
        wanted = module_of(os.path.relpath(os.path.abspath(wanted), REPO)) or wanted
    if wanted not in modules and f"covey.{wanted}" in modules:
        wanted = f"covey.{wanted}"
    if wanted not in modules:
        note(f"{args.module}: not a module under covey/ or tests/support/")
        return 2
    sources = {str(path.relative_to(REPO)): path.read_text() for path in
               [*TESTS.glob("test*.py"), *(TESTS / "support").rglob("*.py")] if "__pycache__" not in path.parts}
    sites = patch_sites(wanted, sources, modules)
    for path, line, kind, name, where in sites:
        print(f"{path}:{line}  {kind}  {name}  ({where})")
    print(f"{len(sites)} places in {len({site[0] for site in sites})} files patch names of {wanted}")
    return 0


def test_methods(text) -> dict[str, str]:
    """Qualified name -> text (decorators included) of each test method of a class in a test module's source."""
    lines = text.splitlines()
    try:
        found = definitions(text)
    except SyntaxError:
        return {}
    return {name: "\n".join(lines[first - 1:last]) for first, last, name, function in found
            if function and "." in name and "<locals>" not in name and name.rsplit(".", 1)[1].startswith("test")}


def new_tests(old, new) -> list[str]:
    """The test methods of ``new`` (source) that ``old`` lacks or has with another text."""
    before = test_methods(old)
    return [name for name, text in test_methods(new).items() if before.get(name) != text]


def first_line(text) -> str:
    """The first line of the last exception a traceback shows (its type and message)."""
    block = text.rsplit("Traceback (most recent call last):", 1)[-1]
    found = [line for line in block.splitlines() if line.strip() and not line[0].isspace()]
    return (found or text.strip().splitlines() or ["?"])[0][:300]


def proof_tree(rev, target) -> None:
    """The working tree's files (tracked and untracked) with REV's covey/ and bin/ under ``target``."""
    for path in git("ls-files", "--cached", "--others", "--exclude-standard", "-z").split("\0"):
        if path and not path.startswith(("covey/", "bin/")) and (REPO / path).is_file():
            (target / path).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / path, target / path)
    present = git("ls-tree", "--name-only", rev, "--", "covey", "bin").split()
    if present:
        archive = subprocess.run(["git", "archive", rev, *present], cwd=REPO, capture_output=True, check=True).stdout
        subprocess.run(["tar", "-x", "-C", str(target)], input=archive, check=True)
    # A checkout of one commit: tests that ask git about the repository fail there for the code, not for no .git.
    identity = ["-c", "user.name=prove", "-c", "user.email=prove@localhost", "-c", "commit.gpgsign=false"]
    for command in (["init", "-q"], ["add", "-A"], ["commit", "-q", "--no-verify", "-m", f"prove on {rev}"]):
        subprocess.run(["git", *identity, *command], cwd=target, capture_output=True, check=True)


def prove_module(target, module, wanted) -> dict[str, tuple[str, str]]:
    """Test id -> (fail, pass, skip or missing; first line) for the ``wanted`` methods of one module in ``target``."""
    out, log = target / f"prove-{module}.json", target / f"prove-{module}.log"
    env = {**os.environ, "PYTHONPATH": str(target)}
    try:
        with log.open("w") as sink:
            subprocess.run([sys.executable, "-B", "-c", PROVE_CHILD, module, str(out), *wanted], cwd=target / "tests",
                           env=env, stdout=sink, stderr=subprocess.STDOUT, timeout=PROVE_TIMEOUT)
        report = json.loads(out.read_text())
    except subprocess.TimeoutExpired:
        return {f"{module}.{name}": ("fail", f"timed out after {PROVE_TIMEOUT} s") for name in wanted}
    except (OSError, ValueError):
        tail = log.read_text(errors="replace").strip().splitlines()[-1:] if log.exists() else []
        return {f"{module}.{name}": ("fail", f"no report from the child: {tail[0] if tail else '?'}")
                for name in wanted}
    if "import" in report:
        return {f"{module}.{name}": ("fail", first_line(report["import"])) for name in wanted}
    fixture = first_line(report["fixtures"][0][1]) if report["fixtures"] else ""
    found = {}
    for test in report["picked"]:
        if test in report["skipped"]:
            found[test] = ("skip", report["skipped"][test])
        elif test not in report["tests"]:  # Never started: a setUpClass or setUpModule failed.
            found[test] = ("fail", fixture or "not run")
        elif report["tests"][test]:
            found[test] = ("fail", first_line(report["tests"][test]))
        else:
            found[test] = ("pass", "")
    found.update({f"{module}.{name}": ("missing", "not loaded as a test") for name in report["missing"]})
    return found


def prove(args) -> int:
    base = git("rev-parse", "--short", args.base).strip()
    picked = {}
    for module in test_modules():
        path = f"tests/{module}.py"
        names = [name for name in new_tests(source(args.base, path), source(None, path))
                 if not args.tests or any(f"{module}.{name}".startswith(test) for test in args.tests)]
        if names:
            picked[module] = names
    count = sum(map(len, picked.values()))
    if not count:
        print(f"prove: no test added or changed since {base}" + (f" among {' '.join(args.tests)}" if args.tests else ""))
        return 2
    target = Path(tempfile.mkdtemp(prefix="bridge-prove-"))
    try:
        proof_tree(args.base, target)
        print(f"prove: {count} tests added or changed since {base} in {len(picked)} modules, on {base}'s "
              f"covey/ and bin/ in {target}", flush=True)
        with test_temp(args.test_tmpdir), ThreadPoolExecutor(max(1, args.jobs)) as pool:
            reports = list(pool.map(lambda item: prove_module(target, *item), picked.items()))
    finally:
        if not args.keep:
            shutil.rmtree(target, ignore_errors=True)
    results = {test: outcome for report in reports for test, outcome in report.items()}
    for test, (state, line) in sorted(results.items()):
        print(f"{state:7}  {test}" + (f"  {line.replace(str(target) + '/', '')}" if line else ""))
    held = sorted(test for test, (state, _) in results.items() if state != "fail")
    print(f"{len(results) - len(held)} of {len(results)} fail on {base}"
          + (f"; not failing there: {' '.join(held)}" if held else "") + (f"; kept {target}" if args.keep else ""))
    return 1 if held else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--map", type=Path, default=MAP, help="the map file (default: tests/test_map.json)")
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run", help="run the modules a change can affect, or --full, and log the run")
    build_parser = commands.add_parser("build", help="record what each test module runs (incl. the slow tier)")
    select_parser = commands.add_parser("select", help="print the command that runs the modules a change can affect")
    flaky_parser = commands.add_parser("flaky", help="the tests the ledger's failed runs name, most often first")
    flaky_parser.set_defaults(paths=[])
    prove_parser = commands.add_parser("prove", help="run the tests added or changed since --base on its source")
    prove_parser.add_argument("--base", required=True, metavar="REV", help="the commit whose covey/ and bin/ to run on")
    prove_parser.add_argument("--jobs", type=int, default=4, help="test modules run at once (default: 4)")
    prove_parser.add_argument("--keep", action="store_true", help="keep the copied tree and the module logs")
    prove_parser.add_argument("tests", nargs="*", help="only the ids starting with one of these (test_x, test_x.Class)")
    prove_parser.set_defaults(paths=[])
    patches_parser = commands.add_parser("patches", help="list the tests that patch a module's names by its import path")
    patches_parser.add_argument("module", help="covey.app.detached, app.detached or covey/app/detached.py")
    patches_parser.set_defaults(paths=[])
    child_parser = commands.add_parser("child", help="internal: one test module in this process, for run and build")
    child_parser.add_argument("module")
    child_parser.add_argument("out", help="JSON lines file to append to")
    child_parser.add_argument("--hooks", help="build: record what the module runs; its child processes report here")
    child_parser.set_defaults(paths=[])
    for sub in (run_parser, build_parser):
        sub.add_argument("--jobs", type=int, default=JOBS, help=f"test modules run at once (default: {JOBS})")
        sub.add_argument("--why", help="what this run checks (goes to the ledger)")
        sub.add_argument("--force", action="store_true", help="run even if the ledger says it is not needed or the map is stale")
    for sub in (run_parser, build_parser, prove_parser):
        sub.add_argument("--test-tmpdir", metavar="DIR", help="base for test temp files (default: $BRIDGE_TEST_TMPDIR, else /dev/shm when it is a tmpfs; inherit: $TMPDIR)")
    for sub in (run_parser, build_parser, flaky_parser):
        sub.add_argument("--ledger", help="ledger file (default: $BRIDGE_TEST_LEDGER, else next to $TMPDIR)")
    run_parser.add_argument("--full", action="store_true", help="the whole default tier")
    for sub in (run_parser, select_parser):
        sub.add_argument("--diff", metavar="REV", help="compare with REV (REV..REV2: two commits) instead of HEAD")
        sub.add_argument("paths", nargs="*", help="changed paths instead of git (the whole file counts)")
    build_parser.add_argument("paths", nargs="*", help="test modules to refresh (default: all, when the map is stale)")
    args = parser.parse_args()
    if args.command == "run" and args.full and (args.diff or args.paths):
        parser.error("--full takes no --diff or paths")
    args.paths = [os.path.abspath(path) for path in args.paths]  # Before prepare() changes into the repo.
    args.map = args.map.absolute()
    return {"run": run, "build": build, "select": select, "flaky": flaky, "prove": prove,
            "patches": patches, "child": child}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
