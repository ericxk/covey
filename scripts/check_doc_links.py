#!/usr/bin/env python3
"""检查根目录、docs/ 和 skills/ 下 Markdown 里的相对链接（含图片）：指向的文件要存在，带 #锚点 的锚点也要存在。

用法：python3 scripts/check_doc_links.py [仓库根目录]    # 默认是本脚本所在仓库
代码块里的内容不查；带协议的链接（https:、mailto:）和绝对路径不查。锚点按 GitHub 的规则从标题生成（转小写，
去掉标点，空格换成 -，同名标题依次加 -1、-2），也认 <a id="…"> / <a name="…">；只查指向 Markdown 文件的锚点。
docs/history/ 下的旧文档不查，但别的文档可以链到它。有失效链接就逐行列出并退出码 1。
"""
from __future__ import annotations

import re
import sys
import unicodedata
from pathlib import Path

LINK = re.compile(r"(?<!\!)\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")
HTML_SRC = re.compile(r"<img\s[^>]*src=\"([^\"]+)\"")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
HTML_ANCHOR = re.compile(r"<a\s+(?:id|name)=\"([^\"]+)\"")


def slug(heading: str) -> str:
    """GitHub's anchor for a heading: inline code and links reduced to their text, lower-cased, every character that
    is not a letter, digit, space, hyphen or underscore dropped, spaces turned into hyphens."""
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", heading)
    text = re.sub(r"<[^>]+>", "", text).replace("`", "").strip().lower()
    kept = []
    for char in text:
        if char in " -_" or unicodedata.category(char)[0] in "LN":
            kept.append("-" if char == " " else char)
    return "".join(kept)


def anchors(page: Path, cache: dict) -> set:
    if page not in cache:
        found, seen, in_code = set(), {}, False
        for line in page.read_text(encoding="utf-8").splitlines():
            if line.lstrip().startswith("```"):
                in_code = not in_code
            if in_code:
                continue
            found.update(HTML_ANCHOR.findall(line))
            match = HEADING.match(line)
            if not match:
                continue
            base = slug(match.group(2))
            count = seen.get(base, 0)
            seen[base] = count + 1
            found.add(base if count == 0 else f"{base}-{count}")
        cache[page] = found
    return cache[page]


def pages(root: Path) -> list[Path]:
    found = [*root.glob("*.md"), *(root / "docs").rglob("*.md"), *(root / "skills").rglob("*.md")]
    return sorted(p for p in found if "history" not in p.relative_to(root).parts)


def broken_links(root: Path) -> list[str]:
    found, cache = [], {}
    for page in pages(root):
        in_code = False
        for number, line in enumerate(page.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("```"):
                in_code = not in_code
            if in_code:
                continue
            targets = [m.group(2) for m in (*LINK.finditer(line), *IMAGE.finditer(line))] + HTML_SRC.findall(line)
            for target in targets:
                raw, _, fragment = target.partition("#")
                if re.match(r"[a-z]+:", raw) or raw.startswith(("/", "<")):
                    continue
                path = (page.parent / raw) if raw else page
                if not path.exists():
                    found.append(f"{page.relative_to(root)}:{number}: {target}")
                    continue
                if fragment and path.suffix == ".md" and path.is_file() and fragment.lower() not in anchors(path, cache):
                    found.append(f"{page.relative_to(root)}:{number}: {target} (没有这个锚点)")
    return found


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    root = Path(args[0]).resolve() if args else Path(__file__).resolve().parent.parent
    problems = broken_links(root)
    for line in problems:
        print(line)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
