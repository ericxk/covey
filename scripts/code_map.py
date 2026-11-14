#!/usr/bin/env python3
"""AGENTS.md's module table, generated from each covey module's docstring.

    python3 -B scripts/code_map.py            # print the table
    python3 -B scripts/code_map.py --write    # rewrite it in AGENTS.md between BEGIN and END
    python3 -B scripts/code_map.py --check    # exit 1 when AGENTS.md's table differs from the generated one

One table per package (app, cards, core, feishu, service, and each backends/<name>), one row per
module: its path under covey/ and the first sentence of its docstring, shortened (``summary``).
Each extension under extensions/<package>/ (core/extensions.py) has its tables too: the package, each of its
subpackages and its scripts, with paths under the repository root.
bin/ is one line listing its entry points. tests/test_structure.py runs ``--check``'s comparison, so a
module added, moved or deleted without ``--write`` fails there. To change a row, change the docstring.
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PACKAGE = REPO / "covey"
AGENTS = REPO / "AGENTS.md"
BEGIN = "<!-- code-map:begin (scripts/code_map.py --write) -->"
END = "<!-- code-map:end -->"
PACKAGES = ("app", "cards", "core", "feishu", "service")
LIMIT = 110  # Characters of a summary before it is cut.
SENTENCE_END = re.compile(r"(?<!\be\.g)(?<!\bi\.e)[.;](?=\s|$)")
# A parenthetical naming a path, a doc or a date says where, not what: dropped from the summary.
WHERE = re.compile(r"\s*\((?:[^()]*?(?:/|\.py|\.md|\d{4}-\d\d-\d\d)[^()]*)\)")


def summary(docstring: str) -> str:
    """The first sentence of a docstring's first paragraph, without parentheticals that say where, at most
    LIMIT characters: cut at a ': ' past the 20th character, else at a word, with an ellipsis."""
    text = " ".join((docstring or "").strip().split("\n\n")[0].split())
    if match := SENTENCE_END.search(text):
        text = text[:match.start()]
    text = WHERE.sub("", text).replace("``", "`").replace("|", "\\|").strip().rstrip(":,")
    if len(text) > LIMIT:
        colon = text.find(": ", 20)
        if 0 < colon <= LIMIT:
            text = text[:colon]
        else:
            text = text[:LIMIT].rsplit(" ", 1)[0].rstrip(",:;(") + " …"
    if text.count("`") % 2:
        text = text.replace("`", "")
    return text


def modules(directory: Path):
    return sorted(p for p in directory.glob("*.py") if p.name != "__init__.py")


def table(paths) -> list[str]:
    rows = ["| 文件 | 管什么 |", "| --- | --- |"]
    for path in paths:
        docstring = ast.get_docstring(ast.parse(path.read_text(), str(path))) or ""
        shown = path.relative_to(PACKAGE) if PACKAGE in path.parents else path.relative_to(REPO)
        rows.append(f"| `{shown}` | {summary(docstring)} |")
    return rows


def generate() -> str:
    lines = []
    groups = [(name, PACKAGE / name) for name in PACKAGES]
    groups += [(f"backends/{d.name}", d) for d in sorted((PACKAGE / "backends").iterdir())
               if d.is_dir() and modules(d)]
    extensions = REPO / "extensions"
    for package in sorted(extensions.iterdir()) if extensions.is_dir() else ():
        if (package / "__init__.py").is_file():
            groups += [(f"extensions/{package.name}", package)]
            groups += [(f"extensions/{package.name}/{d.name}", d) for d in sorted(package.iterdir())
                       if d.is_dir() and d.name not in ("tests", "__pycache__", "bin") and modules(d)]
    for name, directory in groups:
        lines += [f"### {name}", "", *table(modules(directory)), ""]
    shells = sorted((REPO / "bin").glob("*.py"))
    forwards = [p for p in shells if "runpy.run_path(" in p.read_text() and "with_name(" in p.read_text()]  # The old names: two-line forwarders.
    entries = "、".join(f"`{p.name}`" for p in shells if p not in forwards)
    old = "、".join(f"`{p.name}`" for p in forwards)
    lines += ["### bin", "", f"入口壳，只解析参数后调 `covey` 里的函数：{entries}。", "",
              f"改名前的入口名留作转发壳，服务清单、开机钩子和各部署的脚本还在用：{old}。", ""]
    return "\n".join(lines)


def current(text: str) -> str:
    start, end = text.index(BEGIN) + len(BEGIN), text.index(END)
    return text[start:end].strip("\n") + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="rewrite the table in AGENTS.md")
    mode.add_argument("--check", action="store_true", help="exit 1 when AGENTS.md's table is stale")
    args = parser.parse_args(argv)
    generated = generate()
    if not (args.write or args.check):
        sys.stdout.write(generated)
        return 0
    text = AGENTS.read_text()
    if BEGIN not in text or END not in text:
        print(f"AGENTS.md lacks the markers {BEGIN!r} … {END!r}", file=sys.stderr)
        return 1
    if args.check:
        if current(text) == generated:
            return 0
        print("AGENTS.md's code map is stale: python3 -B scripts/code_map.py --write", file=sys.stderr)
        return 1
    start, end = text.index(BEGIN) + len(BEGIN), text.index(END)
    AGENTS.write_text(text[:start] + "\n" + generated + text[end:])
    return 0


if __name__ == "__main__":
    sys.exit(main())
