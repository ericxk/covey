"""照常要问主人的五类事 (部署、删除、花钱、对外发送、占机器) in one place: their short and long names, the words that say a text
is of one of them, and the kind of a checkpoint question with the rule that a declared kind never lowers what its words say.

- ``KINDS``: one ``AskKind`` each, in the order the prompts name them. ``SHORT`` and ``LONG`` are core/checkpoint.py's
  ``ASK_SHORT`` and ``ASK_KINDS`` (they are these, character for character).
- The words (WAVE12 W1-A, P1-1): what app/question_dedup.py's five classes and core/project_gates.py's hard gate each listed,
  put together, plus the wordings the Wave 0 review found both missed (restarting the Bridge or a service, a person told by
  name, English verbs in their longer forms: deployment, deleting, released). ``zh`` are matched anywhere; ``en`` are
  patterns (English stems that match longer words, whole short words, and the Chinese wordings with a gap or a word they
  must not be part of: 通知 but not 通知卡片, 买 but not 买家, 覆盖 of others' work but not 覆盖检查, stable the channel but
  not stable sort). A hit costs the owner one question he would have had anyway, a miss lets a turn or the lead decide what
  was his, so the lists lean to hits; a negation (不部署) still hits.
- ``classify``: the first of the five a text's words say, '' for none, checked in app/question_dedup.py's order (``ORDER``:
  deleting first). ``words_in``: each word or pattern of the five a text holds, as it is written there, with its kind.
- ``question_kind``: a question's kind (``QUESTION_KINDS``). Its words go first: when they say one of the five, that is
  the kind whatever the question declares (``blocked``, ``other``, ``owner_asked``); only otherwise does a declared kind
  count. A declared kind may raise a question to the owner's, never lower it.

Read by core/checkpoint.py (the names), core/project_gates.py (the gate's words), app/question_dedup.py (its five classes).
Standard library only: a core module the project's records and an extension's isolated interpreter may import.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

# A gap inside one clause: what may stand between a verb and what it is done to.
_GAP = r"[^，。,;；？?！!\n]"
# An English word's edges: \b does not hold between a Chinese character and a letter (both are word characters), so 要deploy吗
# and 推到main need these.
_W, _E = r"(?<![A-Za-z0-9])", r"(?![A-Za-z0-9])"


@dataclass(frozen=True)
class AskKind:
    key: str  # deploy | delete | spend | send | occupy
    short: str  # 部署 / 删除 / 花钱 / 对外发送 / 占机器
    long: str  # Its part of core/checkpoint.py ASK_KINDS.
    zh: tuple[str, ...]  # Chinese words, matched anywhere in the text.
    en: tuple[str, ...]  # Patterns, matched in any case: English stems (deploy in deployment and redeploy), whole short words
    # (rm), and the Chinese wordings with a gap or a word they must not be part of.


KINDS = (
    AskKind("deploy", "部署", "部署上线和发布",
            ("部署", "上线", "下线", "发布", "发版", "现网", "生产环境", "线上环境", "线上服务", "到线上"),
            (rf"stable\s*(?:通道|分支|channel|branch){_E}", rf"(?:推(?!荐)|合|发|升|切|上){_GAP}{{0,6}}?stable",
             rf"(?:改|动|写|更新|重启|停){_GAP}{{0,4}}?(?:现网|线上)",
             rf"重启{_GAP}{{0,12}}?(?:bridge|服务|进程|网关|集群|节点|机器|生产)",
             r"deploy", rf"{_W}releas(?:e|ed|es|ing){_E}", rf"{_W}publish", rf"{_W}roll[\s-]?out{_E}",
             rf"{_W}ship(?:s|ped|ping)?{_E}", rf"{_W}go[\s-]live{_E}", rf"{_W}prod(?:uction)?{_E}",
             rf"{_W}restart(?:s|ed|ing)?{_E}{_GAP}{{0,20}}?(?:bridge|service|server|prod)")),
    AskKind("delete", "删除", "删除和其他撤不回的操作（覆盖别人的改动、改线上数据、强推）",
            ("删除", "删掉", "删了", "删光", "清理", "清掉", "清空", "移除", "下掉", "撤掉", "强推", "回滚", "丢弃", "干掉",
             "覆盖原", "覆盖掉", "覆盖现有", "覆盖别人", "覆盖他人", "覆盖同事", "覆盖远端", "覆盖线上", "覆盖画板", "覆盖文档",
             "线上数据", "生产数据", "不可逆", "无法恢复", "不能恢复", "撤不回"),
            (rf"{_W}forc(?:e|ed|es|ing){_E}", rf"{_W}drop(?:s|ped|ping)?{_E}", rf"{_W}rm{_E}",
             rf"{_W}delet(?:e|ed|es|ing|ion|ions){_E}", rf"{_W}truncat(?:e|ed|es|ing){_E}", rf"{_W}purg(?:e|ed|es|ing){_E}",
             rf"{_W}wip(?:e|ed|es|ing){_E}", rf"{_W}destroy(?:s|ed|ing)?{_E}", rf"{_W}irreversibl[ey]{_E}",
             rf"{_W}roll[\s-]?back{_E}", r"reset\s+(?:--)?hard", rf"branch\s+-D{_E}", rf"push\s+-f{_E}",
             rf"(?:删|清理|移除|丢弃|prune|remove){_GAP}{{0,8}}?(?:worktree|分支|branch|检出)", r"删(?!减)")),
    AskKind("spend", "花钱", "花钱",
            ("花钱", "付费", "付款", "购买", "充值", "续费", "套餐", "预算", "费用", "计费", "扣费", "开通", "采购", "转账"),
            (r"买(?!家)", rf"{_W}payments?{_E}", rf"{_W}billing{_E}", rf"{_W}purchas(?:e|ed|es|ing){_E}",
             rf"{_W}buy(?:s|ing)?{_E}", rf"{_W}spend(?:s|ing)?{_E}", rf"{_W}pay{_E}")),
    AskKind("send", "对外发送", "对外发送（以主人身份发消息、发到别人看得到的地方）",
            ("对外", "发给", "发送给", "发消息", "发邮件", "写信", "代发", "群里", "发到群", "发群里", "群公告", "抄送", "@所有人"),
            (rf"以{_GAP}{{0,4}}(?:身份|名义)", r"通知(?![卡栏样类方文渠开设的模条中图标声音铃提])",
             rf"(?:告诉|告知|知会){_GAP}{{0,6}}?(?:同事|对方|大家|团队|群|他们|一声)",
             rf"发(?:送)?(?:到|进|至){_GAP}{{0,10}}?群", rf"回复{_GAP}{{0,6}}?(?:评论|邮件|客户|对方|同事|issue|帖)",
             rf"(?:推(?![荐测断理演])|{_W}push){_GAP}{{0,10}}?github",
             rf"{_W}send(?:s|ing)?{_E}{_GAP}{{0,20}}?(?:e-?mail|message|notification)", rf"{_W}notify{_E}",
             rf"{_W}announc(?:e|ed|es|ing){_E}", rf"{_W}on behalf of{_E}", rf"{_W}post{_E}{_GAP}{{0,20}}?(?:channel|group|slack)")),
    AskKind("occupy", "占机器", "占机器（长时间占 GPU 或集群、起长期运行的服务、大量下载或写盘）",
            ("占用", "占机器", "显卡", "集群", "长期运行", "大量下载", "全量下载", "大量写盘", "训练任务", "提交训练", "守护进程",
             "扩缩容", "扩容", "缩容", "退卡"),
            (r"常驻(?!内存)", r"GPU", rf"{_W}nohup{_E}", rf"{_W}daemons?{_E}", rf"{_W}scale[\s-]?(?:up|down|out|in){_E}",
             r"占(?:用)?\s*[0-9一二两三四五六七八九十几]+\s*(?:台|张|块|卡|机|个节点|个实例|个副本)",
             rf"(?:新建|创建|(?<![重修搭])建(?!议))\s*[0-9一二两三四五六七八九十几]*\s*个?{_GAP}{{0,10}}?(?:服务|集群|实例|资源池)",
             r"缩\s*(?:到|至)?\s*0(?![0-9])",
             rf"(?:申请|释放|归还|退还|退掉|租){_GAP}{{0,8}}?(?:卡|机器|整机|资源|节点|GPU)",
             rf"(?:提交|起|启动|发起|拉起){_GAP}{{0,8}}?(?:训练|集群任务|作业)",
             rf"拉起{_GAP}{{0,8}}?(?:卡|机器|整机|节点|实例|集群)",
             rf"占(?:用)?{_GAP}{{0,6}}?(?:显卡|整机|机器|集群)",
             rf"下载{_GAP}{{0,10}}?[0-9.]+\s*(?:G|T|GB|TB|GiB|TiB)(?![A-Za-z])")),
)
SHORT = "、".join(kind.short for kind in KINDS)
LONG = "、".join(kind.long for kind in KINDS)
BY_KEY = {kind.key: kind for kind in KINDS}
ORDER = ("delete", "deploy", "spend", "send", "occupy")  # Which one a text saying several is of: app/question_dedup.py's order.
QUESTION_KINDS = ("deploy", "delete", "spend", "send", "occupy", "owner_asked", "blocked", "other")
HIGH_RISK = frozenset({"deploy", "delete", "spend", "send", "occupy"})
MUST_OWNER = HIGH_RISK | frozenset({"owner_asked"})
OTHER = "other"

# One pattern each (what app/question_dedup.py's CATEGORIES take), and each word on its own (what ``words_in`` reports).
PATTERNS = {kind.key: re.compile("|".join([*map(re.escape, kind.zh), *kind.en]), re.IGNORECASE) for kind in KINDS}
_EACH = {kind.key: tuple(re.compile(pattern, re.IGNORECASE) for pattern in kind.en) for kind in KINDS}


def classify(text: str) -> str:
    """The key of the first of the five (``ORDER``) whose words ``text`` holds; '' for none (or for no text)."""
    if not isinstance(text, str) or not text.strip():
        return ""
    return next((key for key in ORDER if PATTERNS[key].search(text)), "")


def words_in(text: str) -> list:
    """(key, word) for each word of the five ``text`` holds, once each, in ``ORDER`` and each kind's own order (its Chinese
    words, then its patterns): the word as the text writes it, an English one in lower case. [] for no text."""
    if not isinstance(text, str) or not text.strip():
        return []
    found = []
    for key in ORDER:
        words = [word for word in BY_KEY[key].zh if word in text]
        for pattern in _EACH[key]:
            words += [match.group(0) for match in pattern.finditer(text) if match.group(0)]
        for word in words:
            word = word.lower() if word.isascii() else word
            if (key, word) not in found:
                found.append((key, word))
    return found


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


def declared(question) -> str:
    """The kind ``question`` declares (its ``kind``, trimmed) when it is one of ``QUESTION_KINDS``; '' else."""
    kind = question.get("kind") if isinstance(question, Mapping) else None
    kind = kind.strip() if isinstance(kind, str) else ""
    return kind if kind in QUESTION_KINDS else ""


def question_kind(question) -> str:
    """The kind of a checkpoint question (a mapping; a string is its text), one of ``QUESTION_KINDS``. Raised, never
    lowered: when its words (``_texts``, through ``classify``) say one of the five, that one, even if it declares
    ``blocked``, ``other`` or ``owner_asked``; else the kind it declares when that is one of ``QUESTION_KINDS``; else
    ``other``."""
    said = classify("\n".join(_texts(question)))
    return said or declared(question) or OTHER
