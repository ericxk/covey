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
