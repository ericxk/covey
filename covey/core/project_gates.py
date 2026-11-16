"""专项问题分流的硬闸（docs/butler.md 专项的问题分流）: the words that keep a project's topic's question from the lead.
A question whose text or options (the header, the question, each option's label and description) hold one of them is
the owner's, whatever the lead's memory says: deploying, publishing, sending to others, spending money, deleting, anything
that cannot be taken back. A word is a hint, not a judgement: a hit costs the owner one question card they would have had
without the switch, a miss lets the lead answer something it should not have, so the list leans to hits.

Pure, and only the standard library (AGENTS.md rule 7: a core module the project's records may import). The list is tuned
against the questions of the last 30 days (counts in docs/butler.md)."""
from __future__ import annotations

import re

# Chinese words match anywhere in the text.
WORDS = (
    "部署", "发布", "上线", "下线", "发给", "发送给", "对外", "转账", "付费", "付款", "购买", "充值", "花钱", "额度", "扣费",
    "删除", "清空", "覆盖原", "覆盖掉", "覆盖现有", "覆盖画板", "覆盖文档", "停用", "排空", "批一份", "不可逆", "无法恢复", "不能恢复", "强制", "生产环境", "线上环境", "线上服务",
    "到线上", "密钥", "密码", "令牌",
)
# ASCII words match whole, in any case; a flag such as ``--force`` or a path ``rm -rf`` holds the word on its own.
ASCII = (
    "rm", "force", "deploy", "release", "publish", "delete", "drop", "truncate", "purge", "wipe", "destroy", "prod", "production",
    "sudo", "password", "secret", "api_key", "api key", "apikey", "payment", "billing", "irreversible",
)
_ASCII = re.compile(r"(?<![A-Za-z0-9])(" + "|".join(sorted(map(re.escape, ASCII), key=len, reverse=True)) + r")(?![A-Za-z0-9])",
                    re.IGNORECASE)


def texts(questions) -> list:
    """The strings of ``questions`` a gate looks at: each question's header and text, each option's label and description."""
    found = []
    for question in questions if isinstance(questions, (list, tuple)) else ():
        if not isinstance(question, dict):
            continue
        found += [question.get("header"), question.get("question")]
        for option in question.get("options") or ():
            found += [option.get("label"), option.get("description")] if isinstance(option, dict) else [option]
    return [text for text in found if isinstance(text, str) and text.strip()]


def hits(questions) -> list:
    """The gate words in ``questions``, once each, in the order they come."""
    found = []
    for text in texts(questions):
        for word in WORDS:
            if word in text and word not in found:
                found.append(word)
        for match in _ASCII.finditer(text):
            word = match.group(1).lower()
            if word not in found:
                found.append(word)
    return found


def hit(questions) -> str:
    """The first gate word in ``questions``, '' for none."""
    found = hits(questions)
    return found[0] if found else ""


def choice_only(questions) -> bool:
    """Whether every question has options to pick from (a free-text question is the owner's, always)."""
    return (isinstance(questions, (list, tuple)) and bool(questions) and all(
        isinstance(q, dict) and isinstance(q.get("options"), list) and q["options"] for q in questions))
