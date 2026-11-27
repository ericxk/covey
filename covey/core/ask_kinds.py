"""照常要问主人的五类事 (部署、删除、花钱、对外发送、占机器) in one place: their short and long names, the words that say a text
is of one of them, and the kind of a checkpoint question with the rule that a declared kind never lowers what its words say.

- ``KINDS``: one ``AskKind`` each, in the order the prompts name them. ``SHORT`` and ``LONG`` are core/checkpoint.py's
  ``ASK_SHORT`` and ``ASK_KINDS`` (they are these, character for character).
- ``classify``: the first of the five a text's words say, '' for none. The words are app/question_dedup.py's five classes as
  they stand (``CATEGORIES``), checked in its order (``ORDER``: deleting first), so a reader moved here sees no change.
- ``question_kind``: a question's kind (``QUESTION_KINDS``). Its words go first: when they say one of the five, that is
  the kind whatever the question declares (``blocked``, ``other``, ``owner_asked``); only otherwise does a declared kind
  count. A declared kind may raise a question to the owner's, never lower it.

Nothing reads it but core/checkpoint.py yet (WAVE12 W1-0: readers come first); the gates and the triage take it up later.
Standard library only: a core module the project's records and an extension's isolated interpreter may import.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class AskKind:
    key: str  # deploy | delete | spend | send | occupy
    short: str  # 部署 / 删除 / 花钱 / 对外发送 / 占机器
    long: str  # Its part of core/checkpoint.py ASK_KINDS.
    zh: tuple[str, ...]  # Chinese words, matched anywhere in the text.
    en: tuple[str, ...]  # Patterns (English stems, which match longer words: deploy in deployment and redeploy; and the one Chinese
    # wording with a gap), matched in any case.


KINDS = (
    AskKind("deploy", "部署", "部署上线和发布", ("部署", "上线", "发布", "发版", "现网"), ("stable", "deploy", "release", "rollout")),
    AskKind("delete", "删除", "删除和其他撤不回的操作（覆盖别人的改动、改线上数据、强推）",
            ("删除", "删掉", "清理", "清掉", "移除", "下掉", "撤掉", "覆盖", "强推", "回滚", "丢弃"), ("force", r"\bdrop\b", r"\brm\b")),
    AskKind("spend", "花钱", "花钱", ("花钱", "付费", "购买", "充值", "预算", "费用", "计费", "开通", "采购"), ()),
    AskKind("send", "对外发送", "对外发送（以主人身份发消息、发到别人看得到的地方）",
            ("对外", "发给", "发送给", "通知", "发消息", "发邮件", "代发", "群里"), (r"以.{0,4}身份",)),
    AskKind("occupy", "占机器", "占机器（长时间占 GPU 或集群、起长期运行的服务、大量下载或写盘）",
            ("占用", "占机器", "显卡", "集群", "长期运行", "常驻", "大量下载", "训练任务"), ("GPU",)),
)
SHORT = "、".join(kind.short for kind in KINDS)
LONG = "、".join(kind.long for kind in KINDS)
BY_KEY = {kind.key: kind for kind in KINDS}
ORDER = ("delete", "deploy", "spend", "send", "occupy")  # Which one a text saying several is of: app/question_dedup.py's order.
QUESTION_KINDS = ("deploy", "delete", "spend", "send", "occupy", "owner_asked", "blocked", "other")
HIGH_RISK = frozenset({"deploy", "delete", "spend", "send", "occupy"})
MUST_OWNER = HIGH_RISK | frozenset({"owner_asked"})
OTHER = "other"

_PATTERNS = {kind.key: re.compile("|".join([*map(re.escape, kind.zh), *kind.en]), re.IGNORECASE) for kind in KINDS}


def classify(text: str) -> str:
    """The key of the first of the five (``ORDER``) whose words ``text`` holds; '' for none (or for no text)."""
    if not isinstance(text, str) or not text.strip():
        return ""
    return next((key for key in ORDER if _PATTERNS[key].search(text)), "")


def _texts(question) -> list:
    """What a question says: its header, its text, each option's label and description (or the option itself, a string), its
    context."""
    if isinstance(question, str):
        return [question]
    if not isinstance(question, Mapping):
        return []
    found = [question.get("header"), question.get("question")]
    options = question.get("options")
    for option in options if isinstance(options, (list, tuple)) else ():
        found += [option.get("label"), option.get("description")] if isinstance(option, Mapping) else [option]
    found.append(question.get("context"))
    return [text for text in found if isinstance(text, str) and text.strip()]


def question_kind(question) -> str:
    """The kind of a checkpoint question (a mapping; a string is its text), one of ``QUESTION_KINDS``. Raised, never
    lowered: when its words (``_texts``, through ``classify``) say one of the five, that one, even if it declares
    ``blocked``, ``other`` or ``owner_asked``; else the kind it declares when that is one of ``QUESTION_KINDS``; else
    ``other``."""
    said = classify("\n".join(_texts(question)))
    if said:
        return said
    declared = question.get("kind") if isinstance(question, Mapping) else None
    declared = declared.strip() if isinstance(declared, str) else ""
    return declared if declared in QUESTION_KINDS else OTHER
