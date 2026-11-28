"""The repository's source files read and parsed once per test process.

tests/test_structure.py walks every module of the package in a few dozen tests; each read and parsed
the whole tree again (about 3 s a time on tmpfs, more on NAS). The trees are shared, so a reader walks
them and never changes them.
"""
from __future__ import annotations

import ast
import functools
from pathlib import Path


@functools.lru_cache(maxsize=None)
def read(path) -> str:
    """The text of the file at ``path``, read once."""
    return Path(path).read_text(encoding="utf-8")


@functools.lru_cache(maxsize=None)
def _parse(source: str, filename: str) -> ast.Module:
    return ast.parse(source, filename=filename)


def parse(source: str, filename: str = "<unknown>") -> ast.Module:
    """``ast.parse(source)``, parsed once per source and file name; the tree must not be changed."""
    return _parse(source, filename)


_WALKED: dict[int, tuple[ast.AST, list[ast.AST]]] = {}


def walk(node: ast.AST):
    """``ast.walk(node)``; a whole module's tree is walked once and its nodes kept (with the tree, so its id is not
    reused), since the structure tests walk every module's tree in many tests."""
    if not isinstance(node, ast.Module):
        return ast.walk(node)
    kept = _WALKED.get(id(node))
    if kept is None or kept[0] is not node:
        kept = _WALKED[id(node)] = (node, list(ast.walk(node)))
    return iter(kept[1])
