#!/usr/bin/env python3
"""隐私检查：扫描要公开的文件里的个人、公司信息，有命中就退出码 1。

用法：python3 scripts/privacy_check.py [路径 ...]
不给路径时扫核心集合 CORE（不含 extensions/、docs/history/）；
tests/test_map.json 是生成的测试地图，不扫。

通用规则（写在本脚本里，对所有部署成立）：
  open_id / union_id / chat_id / app_id / message_id 的实值形状、飞书 cli_ 开头的 App ID、
  私网与内网 IP、真实邮箱（example.com 等占位除外）、令牌形状、
  /mnt/bn、/opt/tiger、/home/<人名>（user、me、tiger 之外）这类个人或公司路径。

公司域名和内部名词的清单不进仓库：环境变量 COVEY_PRIVACY_DENYLIST 指向一个文本文件，
每行一个词（不分大小写，按单词边界匹配），# 开头是注释。词后可写 ` | skip=前缀,前缀`，
表示这些相对路径前缀下不查这个词（例：`maso | skip=tests/`，核心测试把扩展的 Harness id 当夹具样本）。
没设置时只跑通用规则。

行内豁免：同一行写 `privacy: allow <原因>`，原因必须写。
整个文件豁免内部名词：文件里任何位置写 `privacy: allow-file <原因>`（只管清单里的名词，
通用规则照查）。用于专门测平台、扩展接口的测试，不用于核心代码和文档。
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORE = ("covey", "bin", "scripts", "docs", "tests", "skills", "integrations", ".github", "install",
        "README.md", "README.en.md", "CONTRIBUTING.md", "LICENSE", "AGENTS.md", "pyproject.toml")
# 默认不扫的相对路径前缀：历史文档、生成的测试地图、本脚本自己（规则里有样例）
EXCLUDED = ("docs/history/", "tests/test_map.json", "scripts/privacy_check.py",
            "tests/test_privacy_check.py", "scripts/export_core.py", "extensions/")
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv"}
SKIP_SUFFIX = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".ico", ".pyc", ".xz", ".gz", ".zip", ".woff", ".woff2"}
DENYLIST_ENV = "COVEY_PRIVACY_DENYLIST"

ALLOW = re.compile(r"privacy:\s*allow\s+(\S.*)")
ALLOW_FILE = re.compile(r"privacy:\s*allow-file\s+(\S.*)")
PLACEHOLDER_MAIL_DOMAINS = {"example.com", "example.org", "example.net", "users.noreply.github.com", "noreply.github.com", "anthropic.com", "localhost"}
PLACEHOLDER_HOME = {"user", "me", "tiger", "name", "you", "username", "your-name", "xxx", "<user>", "$user", "home", "runner"}

RULES = (
    ("飞书 open_id", re.compile(r"\bou_[0-9a-f]{16,}\b")),
    ("飞书 union_id", re.compile(r"\bon_[0-9a-f]{16,}\b")),
    ("飞书 chat_id", re.compile(r"\boc_[0-9a-f]{16,}\b")),
    ("飞书消息 id", re.compile(r"\bom_[0-9a-f]{16,}\b")),
    ("飞书 App ID", re.compile(r"\bcli_[0-9a-f]{12,}\b")),
    ("私网 IP", re.compile(r"(?<![\d.])(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3})(?![\d.])")),
    ("令牌形状", re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|xox[abpr]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}|t-[0-9a-f]{20,}|u-[A-Za-z0-9]{20,})\b")),
    ("个人或公司路径 /mnt/bn", re.compile(r"/mnt/bn\b")),
    ("个人或公司路径 /opt/tiger", re.compile(r"/opt/tiger\b")),
)
EMAIL = re.compile(r"(?<![\w.+-])([\w.+-]+)@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})(?![\w-])")
SSH_USERS = {"git", "hg"}  # git@host:path 是 SSH 地址，不是邮箱。
HOME = re.compile(r"/home/([A-Za-z0-9._-]+)")


def load_denylist(path: str | None) -> list[tuple[str, re.Pattern, tuple]]:
    if not path:
        return []
    terms = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        term, _, options = raw.strip().partition("|")
        term = term.strip()
        if not term or term.startswith("#"):
            continue
        skip = ()
        for option in options.split():
            if option.startswith("skip="):
                skip = tuple(p for p in option[5:].split(",") if p)
        pattern = re.compile(r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])", re.IGNORECASE)
        terms.append((f"内部名词 {term}", pattern, skip))
    return terms


def scan_text(text: str, denylist=(), rel: str = "") -> list[tuple[int, str]]:
    """返回 [(行号, 规则)]；有 `privacy: allow 原因` 的行跳过，没写原因的豁免不算。"""
    found = []
    denylist = [] if ALLOW_FILE.search(text) else [(n, p) for n, p, skip in denylist if not rel.startswith(skip)]
    for number, line in enumerate(text.splitlines(), 1):
        if ALLOW.search(line) and not ALLOW_FILE.search(line):
            continue
        hits = [name for name, pat in RULES if pat.search(line)]
        for match in EMAIL.finditer(line):
            domain = match.group(2).lower()
            if match.group(1) in SSH_USERS:
                continue
            if domain not in PLACEHOLDER_MAIL_DOMAINS and not domain.endswith(".example"):
                hits.append("真实邮箱")
        for match in HOME.finditer(line):
            if match.group(1).lower() not in PLACEHOLDER_HOME:
                hits.append("个人路径 /home/<人名>")
        hits += [name for name, pat in denylist if pat.search(line)]
        found += [(number, name) for name in dict.fromkeys(hits)]
    return found


def files(paths, root: Path = ROOT, explicit: bool = False):
    for given in paths:
        base = Path(given)
        if not base.is_absolute():
            base = root / base
        stack = [base]
        while stack:
            path = stack.pop()
            try:
                rel = path.relative_to(root).as_posix()
            except ValueError:
                rel = path.as_posix()
            if not explicit and any(rel == e.rstrip("/") or rel.startswith(e) for e in EXCLUDED):
                continue
            if path.is_dir():
                if path.name in SKIP_DIRS or path.is_symlink():
                    continue
                stack.extend(sorted(path.iterdir()))
            elif path.is_file() and path.suffix.lower() not in SKIP_SUFFIX:
                yield path, rel


def run(paths, denylist_path: str | None, root: Path = ROOT, explicit: bool = False) -> list[str]:
    denylist = load_denylist(denylist_path)
    out = []
    for path, rel in files(paths, root, explicit):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        out += [f"{rel}:{line}: {rule}" for line, rule in scan_text(text, denylist, rel)]
    return out


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    explicit = bool(args)
    denylist = os.environ.get(DENYLIST_ENV) or None
    if denylist and not Path(denylist).is_absolute():
        denylist = str(ROOT / denylist) if (ROOT / denylist).exists() else denylist
    problems = run(args or CORE, denylist, explicit=explicit)
    for line in problems:
        print(line)
    if not denylist:
        print(f"（没设置 {DENYLIST_ENV}，只跑了通用规则）", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
