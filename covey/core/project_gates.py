"""专项问题分流的硬闸（docs/projects.md 专项的问题分流）: the words that keep a project's topic's question from the lead.
A question whose text, options or context (the header, the question, each option's label and description, its context) hold
one of them is the owner's, whatever the lead's memory says: deploying, publishing, sending to others, spending money,
deleting, anything that cannot be taken back. A word is a hint, not a judgement: a hit costs the owner one question card
they would have had without the switch, a miss lets the lead answer something it should not have, so the list leans to hits.

The five kinds' words are core/ask_kinds.py's (WAVE12 C3: one source; ``question_kind`` first, so a question that declares
one of the five or ``owner_asked`` is the owner's with no word in it); what is not of the five (secrets, quota) is ``EXTRA``
here. Pure, and only the standard library (AGENTS.md rule 7: a core module the project's records may import)."""
from __future__ import annotations

import re

from covey.core import ask_kinds

# Not of the five kinds (core/ask_kinds.py has those): secrets, quota and a few acts on the live service the owner keeps.
# Chinese words match anywhere in the text.
EXTRA_WORDS = ("额度", "停用", "排空", "批一份", "强制", "密钥", "密码", "令牌")
# ASCII words match whole, in any case.
EXTRA_ASCII = ("quota", "sudo", "password", "secret", "api_key", "api key", "apikey")
# Quota charged (T722, 2026-11-24: "占 1 台 8 卡 A 型机（扣 pool-a shared）" was held for the lead). The word reported is what matched.
EXTRA_PATTERNS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    r"扣[^，。,;；\n]{0,16}?(?:shared|dedicated|配额|额度|quota)",
    r"配额",
))
_EXTRA_ASCII = re.compile(r"(?<![A-Za-z0-9])(" + "|".join(sorted(map(re.escape, EXTRA_ASCII), key=len, reverse=True))
                          + r")(?![A-Za-z0-9])", re.IGNORECASE)
# What a question that declares a kind of the owner's and says none of the words hits with (``hits``).
DECLARED = {**{kind.key: kind.short for kind in ask_kinds.KINDS}, "owner_asked": "主人点名要定"}


def _strings(question) -> list:
    """The strings of one question a gate looks at: its header and text, each option's label and description, its context."""
    if not isinstance(question, dict):
        return []
    found = [question.get("header"), question.get("question")]
    for option in question.get("options") or ():
        found += [option.get("label"), option.get("description")] if isinstance(option, dict) else [option]
    found.append(question.get("context"))
    return [text for text in found if isinstance(text, str) and text.strip()]


def texts(questions) -> list:
    """The strings of ``questions`` a gate looks at: each question's header and text, each option's label and description,
    its context."""
    return [text for question in (questions if isinstance(questions, (list, tuple)) else ()) for text in _strings(question)]


def words(text) -> list:
    """The gate words in ``text``, once each: the five kinds' (core/ask_kinds.py words_in), then EXTRA's."""
    found = [word for _, word in ask_kinds.words_in(text)]
    found += [word for word in EXTRA_WORDS if word in text]
    found += [match.group(1).lower() for match in _EXTRA_ASCII.finditer(text)]
    found += [match.group(0) for pattern in EXTRA_PATTERNS for match in pattern.finditer(text)]
    return list(dict.fromkeys(found))


SAMPLE = "样张"  # A question that shows the owner samples of a card (core/ui_samples.py) asks his taste: always his.


def hits(questions) -> list:
    """The gate words in ``questions``, once each, in the order they come; SAMPLE first when an option carries a sample. A
    question with none whose kind is the owner's (core/ask_kinds.py question_kind in MUST_OWNER: it declares one of the five
    or ``owner_asked``) hits with what it declares (``DECLARED``)."""
    asked = questions if isinstance(questions, (list, tuple)) else ()
    found = [SAMPLE] if any(isinstance(q, dict) and any(isinstance(o, dict) and o.get("sample") for o in q.get("options") or ())
                            for q in asked) else []
    for question in asked:
        said = [word for text in _strings(question) for word in words(text)]
        if not said and isinstance(question, dict):
            kind = ask_kinds.question_kind(question)
            said = [DECLARED[kind]] if kind in ask_kinds.MUST_OWNER else []
        found += [word for word in said if word not in found]
    return list(dict.fromkeys(found))


def hit(questions) -> str:
    """The first gate word in ``questions``, '' for none."""
    found = hits(questions)
    return found[0] if found else ""


def choice_only(questions) -> bool:
    """Whether every question has options to pick from (a free-text question is the owner's, always)."""
    return (isinstance(questions, (list, tuple)) and bool(questions) and all(
        isinstance(q, dict) and isinstance(q.get("options"), list) and q["options"] for q in questions))
