"""专项问题分流的硬闸（docs/projects.md 专项的问题分流）: the words that keep a project's topic's question from the lead.
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
    # W0-6 (T892 D5, 2026-11-27): the five kinds (core/checkpoint.py ASK_KINDS) in the words the topics' questions used and
    # the list missed: deleting by another word, rolling back, pushing by force, writing over others' work or the live
    # data, sending as the owner or to a group, buying, a training job, a service that keeps running, a big download.
    "删掉", "删了", "删光", "清掉", "回滚", "强推", "覆盖别人", "覆盖他人", "覆盖同事", "线上数据", "生产数据",
    "发到群", "发群里", "群里发", "群公告", "发邮件", "抄送", "@所有人", "买", "续费", "套餐", "采购",
    "训练任务", "提交训练", "常驻", "长期运行", "守护进程", "大量下载", "全量下载", "大量写盘",
)
# ASCII words match whole, in any case; a flag such as ``--force`` or a path ``rm -rf`` holds the word on its own.
ASCII = (
    "quota", "rm", "force", "deploy", "release", "publish", "delete", "drop", "truncate", "purge", "wipe", "destroy", "prod", "production",
    "sudo", "password", "secret", "api_key", "api key", "apikey", "payment", "billing", "irreversible",
    "rollback", "nohup", "daemon",
)
# Asking for machines, cards or quota and making services on them (T722, 2026-11-24: "占 1 台 8 卡 A 型机（扣 pool-a shared）", "再占 2 台",
# "建 1 个普通服务，占 1 台整机（扣 pool-b dedicated）" were held for the lead; none of the words above was in them). A plain word for
# these (卡, 服务, 资源) is in many questions that ask nothing of the kind, so a pattern names the act: a count of machines or cards
# taken, quota charged, a service made, capacity scaled, resources asked for or given back. The word reported is what matched.
PATTERNS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    r"占(?:用)?\s*[0-9一二两三四五六七八九十几]+\s*(?:台|张|块|卡|机|个节点|个实例|个副本)",
    r"扣[^，。,;；\n]{0,16}?(?:shared|dedicated|配额|额度|quota)",
    r"(?:新建|创建|(?<![重修搭])建(?!议))\s*[0-9一二两三四五六七八九十几]*\s*个?[^，。,;；\n]{0,10}?(?:服务|集群|实例|资源池)",
    r"扩缩容|扩容|缩容|缩\s*(?:到|至)?\s*0(?![0-9])",
    r"(?:申请|释放|归还|退还|退掉)[^，。,;；\n]{0,8}?(?:卡|机器|整机|资源|节点|配额)",
    r"配额",
    r"(?<![A-Za-z])scale[\s-]?(?:up|down|out|in)\b",
    # W0-6 (T892 D5): the acts the words above leave out, each named with what it is done to.
    r"(?:删|清理|移除|丢弃|prune|remove)[^，。,;；\n]{0,8}?(?:worktree|分支|branch|检出)",  # Taking away a worktree or a branch.
    r"branch\s+-D\b|push\s+(?:-f|--force)\b",
    r"stable\s*(?:通道|分支|channel|branch)|(?:推|合|发|升|切|上)[^，。,;；\n]{0,6}?stable",  # The stable channel, not a stable sort.
    r"(?:改|动|写|更新|重启|停)[^，。,;；\n]{0,4}?(?:现网|线上)",
    r"(?:通知|告诉|告知|知会)[^，。,;；\n]{0,6}?(?:同事|对方|大家|团队|群|他们|一声)",
    r"以[^，。,;；\n]{0,4}身份",
    r"发(?:到|进|至)[^，。,;；\n]{0,10}?群",
    r"(?:提交|起|启动|发起|拉起)[^，。,;；\n]{0,8}?(?:训练|集群任务|作业)",
    r"占(?:用)?[^，。,;；\n]{0,6}?(?:GPU|显卡|整机|机器|集群)",
    r"下载[^，。,;；\n]{0,10}?[0-9.]+\s*(?:G|T|GB|TB|GiB|TiB)(?![A-Za-z])",
))
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


SAMPLE = "样张"  # A question that shows the owner samples of a card (core/ui_samples.py) asks his taste: always his.


def hits(questions) -> list:
    """The gate words in ``questions``, once each, in the order they come; SAMPLE first when an option carries a sample."""
    asked = questions if isinstance(questions, (list, tuple)) else ()
    found = [SAMPLE] if any(isinstance(q, dict) and any(isinstance(o, dict) and o.get("sample") for o in q.get("options") or ())
                            for q in asked) else []
    for text in texts(questions):
        for word in WORDS:
            if word in text and word not in found:
                found.append(word)
        for match in _ASCII.finditer(text):
            word = match.group(1).lower()
            if word not in found:
                found.append(word)
        for pattern in PATTERNS:
            for match in pattern.finditer(text):
                word = match.group(0)
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
