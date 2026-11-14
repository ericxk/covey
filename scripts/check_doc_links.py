#!/usr/bin/env python3
"""检查根目录和 docs/ 下 Markdown 里的相对链接（含图片）是否都指向存在的文件。

用法：python3 scripts/check_doc_links.py [仓库根目录]    # 默认是本脚本所在仓库
代码块里的内容不查；带协议的链接（https:、mailto:）、绝对路径和 # 开头的锚点不查，锚点本身也不验证。
有失效链接就逐行列出并退出码 1。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

LINK = re.compile(r"(?<!\!)\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")


def broken_links(root: Path) -> list[str]:
    found = []
    pages = sorted([*root.glob("*.md"), *(root / "docs").rglob("*.md")])
    for page in pages:
        if "history" in page.relative_to(root).parts:
            continue
        in_code = False
        for number, line in enumerate(page.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("```"):
                in_code = not in_code
            if in_code:
                continue
            for match in (*LINK.finditer(line), *IMAGE.finditer(line)):
                raw = match.group(2).split("#", 1)[0]
                if not raw or re.match(r"[a-z]+:", raw) or raw.startswith(("/", "<")):
                    continue
                if not (page.parent / raw).exists():
                    found.append(f"{page.relative_to(root)}:{number}: {match.group(2)}")
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
