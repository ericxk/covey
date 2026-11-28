"""The words the cards, the pinned board and the butler say about a turn and a topic, in one place.
Each table keeps the words its readers showed before; which of them should read alike is the owner's
choice, not this module's.

- ``OUTCOME_LINES``, ``outcome_line``: a task card's status line once its run ended (cards/review.py), 有结果
  among them, and ``TONES``, the state key a card colours a status line's first word by; ``FOLLOW_UP``: where an answered question went, by its follow-up task's state (cards/question_cards.py);
- ``STATES``: how an ended turn reads on a topic status card and its Pin (app/status_cards.py), 做完 among them,
  已停止 for a stopped one (core/board_read.py BOARD_RESULTS);
  ``answer_head``: the status line's word atop a 话题区's answer card in its thread (有结果);
  ``BOARD``: how its other rows read there (在做, 排队, 等你);
- ``RESULTS``: how one reads in the butler's snapshot (app/butler.py), 做完 among them, 停住了 and 已停止 too
  (core/board_read.py BOARD_RESULTS: the board's results, the snapshot's too);
- ``OVERVIEW_OUTCOMES``, ``OVERVIEW_GROUPS``: 手上的事's (cards/overview.py);
- ``DIGEST_START``: how 早晚汇总 (cards/digest.py) says a task waits for the owner to start it (waiting_workspace);
  ``START_LINE`` what such a row says it waits for on the 看板 and 手上的事, the butler's snapshot too, and ``START_ACT``
  its button (core/board_read.py waits: every 等我 opens the card it is acted on);
- ``GROUP_NAMES``: the butler's groups of topics, its snapshot's and its tools' (app/butler.py,
  app/butler_tools.py);
- ``START_PHASE``, ``SEED_PHASE``, ``UNSET_PHASES``: the phases a running task shows before its model wrote one
  (core/longtask.py TaskMonitor), as the status line, the board and the butler's tools read them;
  ``BLANK_PHASES``, the status card's, also takes an empty phase as none;
- ``request_line``: the round's request in one line (the status card's line, the answer card's "你问" block, a
  topic's title without one of the butler's: core/topics.py topic_title).

- ``defers``: a butler reply that says it will come back later ("稍后", "等下", "我去查") and names no topic that would
  bring it back; ``UNFOLLOWED`` the line the Bridge adds under such a reply when its turn handed nothing to a topic
  (app/butler_reply.py unfollowed, #25).

Standard library only, importing nothing of the Bridge's.
"""
from __future__ import annotations

import re

# The words of docs/ui.md 第 3 节 (core/look.py STATES; tests/test_look.py checks they are its words): no mark before
# them, a card colours them (cards/task_status.py, cards/ui.py state_word).
OUTCOME_LINES = {"done": "有结果", "failed": "停住了", "cancelled": "已停止", "waiting_user": "等你"}
# A status line's first word as a key of core/look.py STATES: how a card colours it.
TONES = {"有结果": "result", "停住了": "stopped", "已停止": "cancelled", "等你": "waiting", "在做": "running",
         "可能卡住": "stuck", "排队": "queued", "做完": "done"}
RUNNING, STUCK = "在做", "可能卡住"
FOLLOW_UP = {"queued": "排队", "running": "在做", "done": OUTCOME_LINES["done"], "failed": OUTCOME_LINES["failed"],
             "cancelled": OUTCOME_LINES["cancelled"], "interrupted": "停住了", "waiting_user": "又提了问题"}
STATES = {"done": "做完", "failed": "停住了", "interrupted": "停住了", "cancelled": "已停止"}
BOARD = {"running": "在做", "queued": "排队", "waiting": "等你"}
RESULTS = {"done": "做完", "failed": "停住了", "interrupted": "停住了", "cancelled": "已停止"}
# 手上的事 says what the 看板 and 早晚汇总 say (docs/ui.md 3).
OVERVIEW_OUTCOMES = {"done": "做完", "failed": "停住了", "cancelled": "已停止", "interrupted": "停住了"}
OVERVIEW_GROUPS = {"waiting": "等你", "result": "有结果", "running": "在做"}
DIGEST_START = "等你开工"
START_LINE = "等你在启动卡上点开始"
START_ACT = "去开始"
# The butler's groups say the board's words (docs/ui.md 3, core/look.py STATES and GROUP_WORDS): the words the butler
# reads are the words it says back (#48).
GROUP_NAMES = {"waiting": "等你", "result": "有结果没看", "running": "在做", "recent": "最近", "": "更早"}
START_PHASE = "处理本轮请求"  # TaskMonitor's phase before the checkpoint names one.
# The phase written to a run's checkpoint before its model writes one (app/worker.py): the status line names none.
SEED_PHASE = "准备处理本轮请求"
SEED_NEXT = "检查任务要求与已有状态"  # The seed's next step: a stopped task's card does not say it (app/recovery_cards.py).
UNSET_PHASES = frozenset({START_PHASE, SEED_PHASE})
BLANK_PHASES = frozenset({"", *UNSET_PHASES})


def outcome_line(status: str) -> str:
    """The task card's status line once its run ended, by the same status as the answer's title."""
    return OUTCOME_LINES.get(status, OUTCOME_LINES["done"])


def answer_head(status: str) -> str:
    """How a turn that ended with ``status`` reads atop its answer card in a 话题区's thread: its status line's word
    (有结果, 停住了, 已停止, 等你)."""
    return outcome_line("failed" if status == "interrupted" else status)


# A Markdown code fence's own line (```, ```PLAIN_TEXT, ~~~python): no words of the request, only around them.
_FENCE = re.compile(r"(`{3,}|~{3,})[\w+.\- ]*")


def request_line(text):
    """The round's request in one line: its first line that says something, not a heading the Bridge or a form put
    before the words ("用户补充回答：", a line ending in a colon of a few characters) nor a code fence's own line (a
    message pasted inside ```PLAIN_TEXT read "```PLAIN_TEXT" as T272's title; the words inside the fence are the
    request)."""
    for line in str(text or "").split("\n"):
        line = line.strip()
        if line and not (line.endswith(("：", ":")) and len(line) <= 12) and not _FENCE.fullmatch(line):
            return line
    return ""


def wait_words(seconds) -> str:
    """How long, as the 看板's grey line says it (docs/ui.md 7: whole minutes, a space before the unit): "不到 1 分钟",
    "45 分钟", "2 小时 5 分钟", "3 天 2 小时"."""
    minutes = max(0, int(seconds)) // 60
    if minutes < 1:
        return "不到 1 分钟"
    if minutes < 60:
        return f"{minutes} 分钟"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} 小时" + (f" {minutes} 分钟" if minutes else "")
    days, hours = divmod(hours, 24)
    return f"{days} 天" + (f" {hours} 小时" if hours else "")


# Who stopped a cancelled turn that is not the owner's own (core/jobs.py STOP_SOURCES; core/look.py OWNER_STOPS), as
# the 看板's 停住了 line says it.
STOPPED_BY = {"lead": "专项负责人停了它", "guard": "专项护栏停了它", "proxy": "代答换了批次，停了它"}
# The 看板's button that opens a topic's task card, where 检查并继续 and 看看 are (cards/board.py).
TASK_CARD_ACT = "去任务卡"


def span_words(seconds) -> str:
    """A length of time as the device lines say it: "2 分 10 秒", "5 秒", "3 小时 5 分"."""
    seconds = max(0, int(seconds))
    if seconds >= 3600:
        hours, rest = divmod(seconds, 3600)
        return f"{hours} 小时" + (f" {rest // 60} 分" if rest // 60 else "")
    if seconds >= 60:
        minutes, rest = divmod(seconds, 60)
        return f"{minutes} 分" + (f" {rest} 秒" if rest else "")
    return f"{seconds} 秒"


def limit_words(seconds) -> str:
    """A patience as the card names it: "15 分钟", "6 小时" (whole units), else ``span_words``."""
    seconds = int(seconds)
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{seconds // 3600} 小时"
    if seconds >= 60 and seconds % 60 == 0:
        return f"{seconds // 60} 分钟"
    return span_words(seconds)


def device_name(device) -> str:
    """The device's name on a card: its alias (the same word the workspace carries, ``gpu1:/path``)."""
    return str((device or {}).get("alias") or "设备")


def device_status(device) -> str:
    """The status line of a turn whose device is not reachable now ("" while it is, or for a turn here): the link's
    ``details()``. A turn that waits for the user's answer says so and that the answer will be sent once it is back."""
    if not device or device.get("state") not in ("down", "connecting") or device.get("lost"):
        return ""
    name = device_name(device)
    if device.get("holding"):
        return f"{name} 连接中断。任务停在等你回答，连上后你的回答会送过去。"
    return f"{name} 连接中断，重连中（设备上照常在跑）· 已断 {span_words(device.get('down_seconds') or 0)}"


def device_over(device) -> str:
    """The line under a turn's status when the device's checkpoint grew past what the shim mirrors (1 MiB): the card's
    phase and steps come from the hub's copy, which is no longer the device's."""
    if not device or not device.get("checkpoint_over"):
        return ""
    return f"{device_name(device)} 上的检查点超过 1 MiB，没有同步到本机，上面的阶段和步骤可能不是最新的。"


def device_reconnected(device) -> str:
    """The execution trace's entry once the link is back."""
    return f"{device_name(device)} 已重新连上"


def device_unreachable(alias, why) -> str:
    """A turn that did not start because the device did not answer in time (nothing was queued, nothing ran here)."""
    return f"{alias} 连不上（{why}）。这一轮没有开始，也不会改到本机跑。"


def device_lost_notice(alias, seconds) -> str:
    """The recovery card's text for a turn whose device stayed away past the patience of ``seconds``."""
    after = f"超过 {limit_words(seconds)}" if seconds else "超过设定的耐心值"
    return f"{alias} 失联{after}，这一轮按中断处理；设备上的进程连上后会停掉。"


def device_stop_pending(alias) -> str:
    """The stop button was pressed while the device could not be reached."""
    return f"已请求停止，{alias} 连上后生效"


def device_restarted(alias) -> str:
    """The device lost the task (it restarted, or its supervisor was killed): the turn failed; the session is on the device."""
    return f"{alias} 重启过，这一轮按失败处理；会话还在设备上，检查并继续会接着做。"


def device_hub_restarted(alias) -> str:
    """The turn's task process on the Bridge's machine went away under a turn on the device: the Bridge restarted, or only
    the task process was killed or exited. The record does not tell which (``interrupted_by`` is the same), so the words fit both."""
    return f"上一轮的任务进程中断过（机器人重启或任务进程退出），{alias} 上这一轮可能还在跑；点检查并继续会先停掉它再接着做。"


def device_interrupted(record) -> str:
    """What the recovery card of an interrupted turn on a device leads with ('' for a turn here): the device was lost past the
    patience (``interrupted_by`` device_lost), or the Bridge's machine or the task runner went away under it."""
    alias = str(record.get("device") or "")
    if not alias or record.get("status") != "interrupted":
        return ""
    if record.get("interrupted_by") == "device_lost":
        state = record.get("device_state")
        return device_lost_notice(alias, state.get("limit_seconds") if isinstance(state, dict) else 0)
    return device_hub_restarted(alias)


def retry_hint():
    """Where a task is tried again: its topic's task card (slash commands are retired)."""
    return '点击本话题任务卡的重试按钮'


def interrupted_notice(job_id) -> str:
    """The notice of task #``job_id`` left unfinished (a restart, its task runner gone): report_unfinished's text."""
    return f"任务 #{job_id} 停住了：中途被打断，没有跑完。在原话题{retry_hint()} 检查已有工作后继续。"


def ask_words(record, listed=0) -> str:
    """What a question record (app/questions.py) asks, in a line: its one question (or its header), else "3 个问题";
    with ``listed``, "3 个问题：" and each question cut to that many characters (the butler's topic_read)."""
    asked = [q for q in record.get("questions") or [] if isinstance(q, dict)]
    if len(asked) == 1:
        return str(asked[0].get("question") or asked[0].get("header") or "")
    if not asked:
        return ""
    if not listed:
        return f"{len(asked)} 个问题"
    return f"{len(asked)} 个问题：" + "；".join(str(q.get("question") or "")[:listed] for q in asked)


# A reply that promises to come back: nothing wakes the butler after its turn (app/prompts.py AREA_OPENING), so only a
# topic it opened or handed the matter to brings anything back. 等下载、等下单 are not 等下.
DEFERS = re.compile(r"稍后|等一?下(?![载单面])|待会儿?|一会儿|过会儿|回头(?:再|我|给你)|晚点|晚些时候"
                    r"|我(?:这就|马上)?(?:去|先去|再去)(?:查|看|找|问|试|确认|处理)|我先(?:查|看|找|问|试)(?:一下|看|查)")
TOPIC_NAMED = re.compile(r"(?<![A-Za-z0-9_])T\d{1,6}(?!\d)")
UNFOLLOWED = "（这件事没有交给话题，这一轮结束后不会有人接着做；要做就再说一句。）"


def defers(text) -> bool:
    """Whether a butler reply puts something off for later (DEFERS) without naming a topic (a T number) that would bring
    it back."""
    text = str(text or "")
    return bool(DEFERS.search(text)) and not TOPIC_NAMED.search(text)
