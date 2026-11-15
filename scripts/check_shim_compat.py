#!/usr/bin/env python3
"""Checks that covey/core/device_shim.py stays runnable on Python 3.9 with the standard library alone.

    python3 scripts/check_shim_compat.py [FILE ...]    # default: covey/core/device_shim.py; exit 1 on a finding

The shim runs on a user's device, where the interpreter can be the 3.9 a Mac's command line tools ship, and there
is no 3.9 on the development machine to run it. Two checks stand in for one: ``ast.parse(feature_version=(3, 9))``
refuses syntax newer than 3.9 (match, except*, and the like), and a deny list names what parses everywhere but
only exists later: ``zip(strict=)``, ``int.bit_count``, ``dataclass(slots=)``, standard library names and
parameters added in 3.10 to 3.13, and a ``X | Y`` in ``isinstance`` (only a type's ``|`` from 3.10). The file must
also keep ``from __future__ import annotations`` (so a ``X | None`` annotation never runs) and import nothing
but the standard library: not covey, not a package from outside.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT = REPO / "covey" / "core" / "device_shim.py"
FEATURE_VERSION = (3, 9)

# Attribute names with no counterpart before 3.10: matched on the attribute alone, whoever the object is, so
# only names that mean one thing (``os.walk`` or ``logger.exception`` are fine, ``Path.walk`` is not seen).
BANNED_ATTRIBUTES = {
    "bit_count": "int.bit_count is 3.10",
    "file_digest": "hashlib.file_digest is 3.11",
    "pairwise": "itertools.pairwise is 3.10",
    "batched": "itertools.batched is 3.12",
    "orig_argv": "sys.orig_argv is 3.10",
    "stdlib_module_names": "sys.stdlib_module_names is 3.10",
    "get_annotations": "inspect.get_annotations is 3.10",
    "aclosing": "contextlib.aclosing is 3.10",
    "cbrt": "math.cbrt is 3.11",
    "exp2": "math.exp2 is 3.11",
    "sumprod": "math.sumprod is 3.12",
    "isjunction": "os.path.isjunction is 3.12",
    "listdrives": "os.listdrives is 3.12",
    "correlation": "statistics.correlation is 3.10",
    "covariance": "statistics.covariance is 3.10",
    "linear_regression": "statistics.linear_regression is 3.10",
    "StrEnum": "enum.StrEnum is 3.11",
    "ReprEnum": "enum.ReprEnum is 3.11",
    "TaskGroup": "asyncio.TaskGroup is 3.11",
    "UTC": "datetime.UTC is 3.11",
    "add_note": "BaseException.add_note is 3.11",
}
BANNED_NAMES = {
    "aiter": "aiter() is 3.10", "anext": "anext() is 3.10",
    "ExceptionGroup": "ExceptionGroup is 3.11", "BaseExceptionGroup": "BaseExceptionGroup is 3.11",
    "EncodingWarning": "EncodingWarning is 3.10", "PythonFinalizationError": "PythonFinalizationError is 3.13",
}
BANNED_MODULES = {"tomllib": "3.11"}
# Names ``from typing import ...`` (or typing.X) must not use: all added after 3.9.
TYPING_AFTER_39 = {
    "Self", "LiteralString", "Never", "assert_never", "assert_type", "reveal_type", "TypeAlias", "ParamSpec",
    "ParamSpecArgs", "ParamSpecKwargs", "Concatenate", "TypeGuard", "Required", "NotRequired", "Unpack",
    "TypeVarTuple", "override", "dataclass_transform", "get_overloads", "clear_overloads", "is_typeddict",
    "TypeAliasType", "ReadOnly", "TypeIs", "NoDefault", "get_protocol_members", "is_protocol", "evaluate_forward_ref",
}
# (function, keyword argument) pairs that exist only later.
BANNED_KEYWORDS = {
    ("zip", "strict"): "zip(strict=) is 3.10",
    ("dataclass", "slots"): "dataclass(slots=) is 3.10",
    ("dataclass", "kw_only"): "dataclass(kw_only=) is 3.10",
    ("dataclass", "match_args"): "dataclass(match_args=) is 3.10",
    ("field", "kw_only"): "dataclasses.field(kw_only=) is 3.10",
    ("Popen", "process_group"): "Popen(process_group=) is 3.11",
    ("run", "process_group"): "subprocess.run(process_group=) is 3.11",
    ("Popen", "pipesize"): "Popen(pipesize=) is 3.10",
    ("run", "pipesize"): "subprocess.run(pipesize=) is 3.10",
    ("rmtree", "onexc"): "shutil.rmtree(onexc=) is 3.12",
    ("rmtree", "dir_fd"): "shutil.rmtree(dir_fd=) is 3.11",
    ("bisect_left", "key"): "bisect(key=) is 3.10",
    ("bisect_right", "key"): "bisect(key=) is 3.10",
    ("insort", "key"): "bisect.insort(key=) is 3.10",
    ("insort_left", "key"): "bisect.insort(key=) is 3.10",
    ("insort_right", "key"): "bisect.insort(key=) is 3.10",
    ("relative_to", "walk_up"): "Path.relative_to(walk_up=) is 3.12",
    ("glob", "case_sensitive"): "Path.glob(case_sensitive=) is 3.12",
    ("rglob", "case_sensitive"): "Path.rglob(case_sensitive=) is 3.12",
    ("TemporaryDirectory", "delete"): "TemporaryDirectory(delete=) is 3.12",
}
# (object name, method) pairs, matched when the call is ``<name>.<method>(...)``.
BANNED_CALL_SHAPES = {
    ("sys", "exception"): "sys.exception() is 3.11",
    ("asyncio", "timeout"): "asyncio.timeout is 3.11",
    ("contextlib", "chdir"): "contextlib.chdir is 3.11",
}
# A traceback function that takes one argument from 3.10 only (the exception itself).
TRACEBACK_ONE_ARGUMENT = {"format_exception", "print_exception", "format_exception_only"}
FUTURE_IMPORT = "from __future__ import annotations"


def stdlib_names() -> set[str] | None:
    """The standard library's top-level module names of the interpreter running this check (3.10 and up)."""
    names = getattr(sys, "stdlib_module_names", None)
    return set(names) if names is not None else None


def check(source: str, filename: str = "<shim>", *, standard=None) -> list[str]:
    """The findings in ``source``, each ``file:line: what``; an empty list means it fits 3.9."""
    try:
        tree = ast.parse(source, filename, feature_version=FEATURE_VERSION)
    except SyntaxError as exc:
        return ["%s:%s: not Python 3.9 syntax: %s" % (filename, exc.lineno, exc.msg)]
    standard = standard if standard is not None else stdlib_names()
    findings: list[str] = []

    def found(node, what):
        findings.append("%s:%s: %s" % (filename, getattr(node, "lineno", "?"), what))

    if FUTURE_IMPORT not in source:
        findings.append("%s:1: missing `%s` (a `X | None` annotation would fail on 3.9)" % (filename, FUTURE_IMPORT))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            check_import(node, standard, found)
        elif isinstance(node, ast.Attribute):
            why = BANNED_ATTRIBUTES.get(node.attr)
            if why:
                found(node, why)
            if isinstance(node.value, ast.Name) and node.value.id == "typing" and node.attr in TYPING_AFTER_39:
                found(node, "typing.%s is after 3.9" % node.attr)
        elif isinstance(node, ast.Name):
            if node.id in BANNED_NAMES:
                found(node, BANNED_NAMES[node.id])
        elif isinstance(node, ast.Call):
            check_call(node, found)
    return findings


def check_import(node, standard, found):
    if isinstance(node, ast.ImportFrom):
        if node.level:
            found(node, "a relative import: the shim is one file")
            return
        modules = [node.module or ""]
        if node.module == "typing":
            for alias in node.names:
                if alias.name in TYPING_AFTER_39:
                    found(node, "typing.%s is after 3.9" % alias.name)
        for alias in node.names:
            if alias.name in BANNED_NAMES or alias.name in ("StrEnum", "UTC", "TaskGroup", "pairwise", "batched", "file_digest"):
                found(node, "%s.%s is after 3.9" % (node.module, alias.name))
    else:
        modules = [alias.name for alias in node.names]
    for module in modules:
        top = module.split(".")[0]
        if top == "covey":
            found(node, "imports covey: the shim must not import anything from the repository")
        elif standard is not None and top not in standard and top != "__future__":
            found(node, "imports %s, which is not the standard library" % top)
        if module in BANNED_MODULES:
            found(node, "module %s is %s" % (module, BANNED_MODULES[module]))


def callee(node) -> tuple[str, str]:
    """(object name, function name) of a call: ``a.b(...)`` is ("a", "b"), ``b(...)`` is ("", "b")."""
    func = node.func
    if isinstance(func, ast.Attribute):
        owner = func.value.id if isinstance(func.value, ast.Name) else ""
        return owner, func.attr
    return "", func.id if isinstance(func, ast.Name) else ""


def check_call(node, found):
    owner, name = callee(node)
    for keyword in node.keywords:
        why = BANNED_KEYWORDS.get((name, keyword.arg))
        if why:
            found(node, why)
    why = BANNED_CALL_SHAPES.get((owner, name))
    if why:
        found(node, why)
    if owner == "traceback" and name in TRACEBACK_ONE_ARGUMENT and len(node.args) == 1 and not node.keywords:
        found(node, "traceback.%s(exc) with one argument is 3.10" % name)
    if name in ("isinstance", "issubclass") and len(node.args) == 2:
        pair = node.args[1]
        if isinstance(pair, ast.BinOp) and isinstance(pair.op, ast.BitOr):
            found(node, "%s(x, A | B) is 3.10: pass a tuple" % name)
    if name == "open" and any(k.arg == "encoding" and isinstance(k.value, ast.Constant) and k.value.value == "locale"
                              for k in node.keywords):
        found(node, 'open(encoding="locale") is 3.10')


def main(argv: list[str]) -> int:
    paths = [Path(arg) for arg in argv[1:]] or [DEFAULT]
    findings = []
    for path in paths:
        findings += check(path.read_text(encoding="utf-8"), str(path))
    for finding in findings:
        print(finding)
    if findings:
        print("%d finding(s): the shim must keep to Python 3.9 and the standard library" % len(findings))
        return 1
    print("ok: %s parse as Python %d.%d and use only what 3.9 has" % (", ".join(str(p) for p in paths), *FEATURE_VERSION))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
