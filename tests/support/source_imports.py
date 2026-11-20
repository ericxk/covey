"""Reading a package's imports from its source: shared by the structure tests of the core (tests/test_structure.py)
and of the company extension (its tests/test_maso_structure.py)."""
from __future__ import annotations

import ast

from support import REPO

PACKAGE = REPO / "covey"


def module_name(path):
    parts = list(path.relative_to(PACKAGE.parent).with_suffix("").parts)
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def imports(path, *, type_checking=False):
    """(module, imported name or None, line) of each absolute import in ``path``, inside functions too;
    without what ``if TYPE_CHECKING:`` imports unless ``type_checking``."""
    tree = ast.parse(path.read_text())
    skipped = set() if type_checking else {id(node) for block in ast.walk(tree) if isinstance(block, ast.If)
                                            and "TYPE_CHECKING" in ast.unparse(block.test) for node in ast.walk(block)}
    found = []
    for node in ast.walk(tree):
        if id(node) in skipped:
            continue
        if isinstance(node, ast.Import):
            found += [(alias.name, None, node.lineno) for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found += [(node.module, alias.name, node.lineno) for alias in node.names]
    return found


def imported_modules(source, package):
    """(module, line) of every import in ``source``, a module of ``package``: relative ones resolved, inside
    functions and under ``if TYPE_CHECKING:`` too; ``from a.b import c`` is a.b.c below the top two levels."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found += [(alias.name, node.lineno) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = ".".join(package.split(".")[:len(package.split(".")) - node.level + 1] if node.level else [])
            module = ".".join(part for part in (base, node.module or "") if part)
            found += [(f"{module}.{alias.name}" if module.count(".") < 2 else module, node.lineno)
                      for alias in node.names]
    return list(dict.fromkeys(found))


GUARDS = ("ImportError", "ModuleNotFoundError", "Exception")


def load_time_imports(path):
    """(module, line) of each absolute import ``path`` runs when it is imported: in its body and the if, with and try
    blocks there, not in functions or classes, not under ``if TYPE_CHECKING:`` and not in a try whose handlers catch
    a failed import (ImportError, ModuleNotFoundError, Exception or a bare except)."""
    found = []

    def walk(body, guarded):
        for node in body:
            if isinstance(node, ast.Import) and not guarded:
                found.extend((alias.name, node.lineno) for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and not guarded and node.level == 0 and node.module:
                found.append((node.module, node.lineno))
            elif isinstance(node, ast.Try):
                catches = any(handler.type is None or any(guard in ast.unparse(handler.type) for guard in GUARDS)
                              for handler in node.handlers)
                walk(node.body, guarded or catches)
                for block in (*(handler.body for handler in node.handlers), node.orelse, node.finalbody):
                    walk(block, guarded)
            elif isinstance(node, ast.If) and "TYPE_CHECKING" not in ast.unparse(node.test):
                walk(node.body, guarded)
                walk(node.orelse, guarded)
            elif isinstance(node, ast.With):
                walk(node.body, guarded)

    walk(ast.parse(path.read_text(encoding="utf-8")).body, False)
    return found
