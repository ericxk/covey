"""Where a new butler message goes when the family already has live tasks: the rule layer of the message router
(a pure function, docs/butler.md 同时做几件事).

``route`` takes one ``Message`` (the text, what it quotes, the family's live tasks, the clock), applies the nine rules in
order, and answers a ``Decision(action, target, why)``; the first rule that matches wins:

1. the switch is off, or the family has no live task: ``legacy`` (today's path: slot 0, no fork, no judge);
2. the whole sentence is a stop word (STOP_WORDS, at most STOP_MAX characters once punctuation is dropped): ``stop`` the
   quoted task's track, else the newest started one;
3. it quotes a message that was merged into a task, and the whole sentence is an ALONE_WORDS phrase: ``new``, the request
   being the quoted message's own text (``text``), the task it went into in ``job_id``;
4. it quotes a card (or question card) of a live task: ``merge`` into that track; a track waiting for an answer
   (``paused``) takes it as the answer (``answers``), not as a supplement;
5. it quotes something that ended (an answer, a relay, the owner's own old message): ``new``, a follow-up on its own track;
6. no quote, exactly one track waits for an answer, and the text is short with no question mark or is an option
   (OPTION_WORDS): ``merge`` into it as the answer;
7. no quote, only attachments or a link (``bare``): ``merge`` into the newest track;
8. no quote, the newest track's last owner message is less than CORRECTION_SECONDS old and the text starts with a
   CORRECTION_PREFIXES word: ``merge`` into the newest track;
9. the rest: ``ask_judge`` (app/track_judge.py asks a small model); with ``judge`` false (switch off, no model) it is ``new``.

Rules 6 to 9 only see the tasks an unquoted message may join (``LiveJob.joins``, core/turn_source.py Behavior.joinable): a
turn the Bridge started for the owner (a handover, "↪ T12 转给管家") takes his words only when he quotes it (rule 4). With
none of those live, the message is ``new`` (why ``UNJOINED``) and the small model is not asked (T957).

Who a quote points at is the caller's work (app/butler_tracks.py reads the topic aliases and the supplement delivery
records); this module reads no file and calls no interface. A quote it cannot place (``unknown``) skips rules 3 to 8:
it is not a plain message, and not what any of them recognises, so only the judge or ``new`` may decide it.

The wordlists are module constants, so tuning is a data change. Standard library only: a process that holds nothing but the
standard library may import this module.
"""
from __future__ import annotations

import dataclasses
import re
import unicodedata

# The five outcomes.
LEGACY, STOP, MERGE, NEW, ASK_JUDGE = "legacy", "stop", "merge", "new", "ask_judge"

# A live task's stage; one that has ended is not in the table the caller builds.
LIVE_PHASES = frozenset({"running", "starting", "queued", "paused"})
PAUSED = "paused"

# What a quote points at (Quote.kind).
Q_NONE = "none"        # no quote
Q_LIVE = "live_card"   # the card, or the question card, of a task that is live
Q_ENDED = "ended"      # an answer, a relay, the card of a task that ended
Q_OWN = "own_message"  # an older message of the owner's that was not merged into a task
Q_MERGED = "merged"    # a message of the owner's that was merged into a task (a supplement delivery record exists)
Q_UNKNOWN = "unknown"  # something the caller cannot place (another person's message, an alias that is gone)
QUOTE_KINDS = frozenset({Q_NONE, Q_LIVE, Q_ENDED, Q_OWN, Q_MERGED, Q_UNKNOWN})

# Rule 2: the whole sentence, lower case, punctuation and spaces dropped.
STOP_WORDS = frozenset({"停止", "停下", "停", "别做了", "算了", "发错了", "不用了", "撤回", "cancel", "stop",
                        "取消", "停掉", "不做了", "不要了", "先停", "先停下"})
STOP_MAX = 8

# Rule 3: "answer this one on its own" (the same sentence rules as STOP_WORDS).
ALONE_WORDS = frozenset({"单独答", "单独回答", "单独回", "单独处理", "单独做", "单独开", "单开", "新开一个", "新开一路",
                         "新问题", "这是新问题", "当新问题", "当成新问题", "别并", "不要并", "不要合并", "不是补充"})

# Rule 6: a short answer to a question that has no question mark, or one of these (the same sentence rules).
ANSWER_MAX = 20
OPTION_WORDS = frozenset({"是", "否", "好", "好的", "好吧", "可以", "行", "不行", "不", "不用", "不要", "要", "对",
                          "不对", "嗯", "确认", "同意", "拒绝", "都行", "随便", "yes", "no", "ok", "okay", "y", "n"})
OPTION_PATTERNS = (
    re.compile(r"选[abcdefgh1-9]"),                  # 选 A, 选2
    re.compile(r"[abcdefgh]"),                       # a bare letter
    re.compile(r"\d{1,2}"),                          # a number
    re.compile(r"第[一二三四五六七八九十\d]+[个项条种]?"),  # 第二个
)
QUESTION_MARKS = ("?", "？")

# Rule 8: a correction of what the owner just said, started within CORRECTION_SECONDS of it.
CORRECTION_SECONDS = 60
CORRECTION_PREFIXES = ("不对", "不是", "错了", "我是说", "我的意思是", "补充", "另外", "改成", "换成", "等一下", "对，", "对,")

# Why a message that quotes nothing is ``new`` when no live task may take it (each is a turn the Bridge started).
UNJOINED = "unjoined"


@dataclasses.dataclass(frozen=True)
class LiveJob:
    """One task of the family that has not ended. ``track`` is its slot (1 to 4 when it ran on a track, 0 the butler
    ), ``request`` the text the owner asked, the times epoch seconds (0 when unknown), ``number`` its number
    on the cards (0 when unknown)."""

    track: int
    job_id: str
    phase: str
    request: str = ""
    started_at: float = 0
    last_owner_at: float = 0
    number: int = 0
    joins: bool = True  # A message that quotes nothing may join it (rules 6 to 9); False: only by a quote (rule 4).


@dataclasses.dataclass(frozen=True)
class Quote:
    """What the message quotes. ``track`` and ``job_id`` name the task a live card or a merged message belongs to;
    ``text`` is the quoted text (the judge reads it; rule 3 makes it the request)."""

    kind: str = Q_NONE
    track: int | None = None
    job_id: str = ""
    text: str = ""


@dataclasses.dataclass(frozen=True)
class Message:
    """What the caller assembles for ``route``. ``live`` is the family's tasks that have not ended, in any order;
    ``now`` epoch seconds; ``bare`` the message is only a forward, images, files or a link (core/butler.py
    bare_message); ``judge`` whether a small model may be asked (the judge switch, a model to call)."""

    text: str
    live: tuple[LiveJob, ...] = ()
    quote: Quote = Quote()
    enabled: bool = True
    now: float = 0
    bare: bool = False
    judge: bool = True


@dataclasses.dataclass(frozen=True)
class Decision:
    """``target`` is a track (a slot number) or None; ``why`` the rule's number ("1" to "9") as a string. ``job_id``
    is the task the decision is about when there is one; ``text`` replaces the message's text as the request (rule 3
    only); ``answers`` marks a merge into a track that waits for an answer."""

    action: str
    target: int | None = None
    why: str = ""
    job_id: str = ""
    text: str = ""
    answers: bool = False


def squash(text: str) -> str:
    """Lower case with every punctuation, symbol and space character dropped."""
    return "".join(ch for ch in text.casefold() if unicodedata.category(ch)[0] not in "PSZC")


def newest(live) -> LiveJob:
    """The task that started last (the larger track when it is a tie)."""
    return max(live, key=lambda job: (job.started_at, job.track))


def quoted_job(quote: Quote, live) -> LiveJob | None:
    """The live task the quote belongs to, by task key first and by track otherwise; None when it is not live."""
    for job in live:
        if job.job_id == quote.job_id if quote.job_id else job.track == quote.track:
            return job
    return None


def is_option(sentence: str) -> bool:
    return sentence in OPTION_WORDS or any(pattern.fullmatch(sentence) for pattern in OPTION_PATTERNS)


def route(message: Message) -> Decision:
    """The decision for ``message``. Never raises on a well-formed message; an unknown quote kind is an unknown quote."""
    live = tuple(job for job in message.live if job.phase in LIVE_PHASES)
    if not message.enabled or not live:
        return Decision(LEGACY, why="1")
    text = (message.text or "").strip()
    sentence = squash(text)
    quote = message.quote if message.quote.kind in QUOTE_KINDS else Quote(Q_UNKNOWN)
    target = quoted_job(quote, live) if quote.kind in (Q_LIVE, Q_MERGED) else None

    if sentence in STOP_WORDS and len(sentence) <= STOP_MAX:
        job = target or newest(live)
        return Decision(STOP, job.track, "2", job.job_id)
    if quote.kind == Q_MERGED and sentence in ALONE_WORDS:
        return Decision(NEW, None, "3", quote.job_id, quote.text.strip() or text)
    if quote.kind == Q_LIVE and target is not None:
        return Decision(MERGE, target.track, "4", target.job_id, answers=target.phase == PAUSED)
    if quote.kind in (Q_LIVE, Q_ENDED, Q_OWN, Q_MERGED):
        # The card's task ended meanwhile, or the owner quotes an old answer or message: a follow-up.
        return Decision(NEW, None, "5")
    if quote.kind == Q_UNKNOWN:
        return Decision(ASK_JUDGE if message.judge else NEW, None, "9")

    joinable = tuple(job for job in live if job.joins)
    if not joinable:
        return Decision(NEW, None, UNJOINED)
    paused = [job for job in joinable if job.phase == PAUSED]
    if len(paused) == 1 and not message.bare and text and not any(mark in text for mark in QUESTION_MARKS) and (
            len(text) <= ANSWER_MAX or is_option(sentence)):
        return Decision(MERGE, paused[0].track, "6", paused[0].job_id, answers=True)
    latest = newest(joinable)
    if message.bare:
        return Decision(MERGE, latest.track, "7", latest.job_id)
    if (latest.last_owner_at and 0 <= message.now - latest.last_owner_at < CORRECTION_SECONDS
            and text.startswith(CORRECTION_PREFIXES)):
        return Decision(MERGE, latest.track, "8", latest.job_id)
    return Decision(ASK_JUDGE if message.judge else NEW, None, "9")
