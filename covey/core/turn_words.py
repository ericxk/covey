"""The words the cards, the pinned board and the main thread say about a turn and a topic, in one place (C10,
2026-11-08). Each table keeps the words its readers showed before; which of them should read alike is the owner's
choice (VERIFY-board.md C10), not this module's.

- ``OUTCOME_LINES``, ``outcome_line``: a task card's status line once its run ended (cards/review.py), ✅ 已完成
  among them; ``FOLLOW_UP``: where an answered question went, by its follow-up task's state (cards/question_cards.py);
- ``STATES``: how an ended turn reads on a topic status card and its Pin (app/status_cards.py), 做完 among them,
  已停止 for a stopped one (on the board since 2026-11-09, core/board_read.py BOARD_RESULTS);
  ``answer_head``: the same words with the outcome's mark, atop a 话题区's answer card in its thread (✅ 做完);
  ``BOARD``: how its other rows read there (在做, 排队, 等你回答);
- ``RESULTS``: how one reads in the main thread's snapshot (app/main_thread.py), 完成 among them, 已停止 too
  (core/board_read.py BOARD_RESULTS: the board's results, the snapshot's since 2026-11-10);
- ``OVERVIEW_OUTCOMES``, ``OVERVIEW_GROUPS``: 手上的事's (cards/overview.py);
- ``DIGEST_START``: how 早晚汇总 (cards/digest.py) says a task waits for the owner to start it (waiting_workspace);
- ``GROUP_NAMES``: the main thread's groups of topics, its snapshot's and its tools' (app/main_thread.py,
  app/main_tools.py);
- ``START_PHASE``, ``SEED_PHASE``, ``UNSET_PHASES``: the phases a running task shows before its model wrote one
  (core/longtask.py TaskMonitor), as the status line, the board and the main thread's tools read them;
  ``BLANK_PHASES``, the status card's, also takes an empty phase as none;
- ``request_line``: the round's request in one line (the status card's line, the answer card's "你问" block, a
  topic's title without one of the main thread's: core/topics.py topic_title).

Standard library only, importing nothing of the Bridge's.
"""
from __future__ import annotations

OUTCOME_LINES = {"done": "✅ 已完成", "failed": "⚠️ 执行失败", "cancelled": "⏹ 已停止", "waiting_user": "❓ 需要你回答"}
FOLLOW_UP = {"queued": "排队中", "running": "进行中", "done": OUTCOME_LINES["done"], "failed": OUTCOME_LINES["failed"],
             "cancelled": OUTCOME_LINES["cancelled"], "interrupted": "已中断", "waiting_user": "又提了问题"}
STATES = {"done": "做完", "failed": "失败", "interrupted": "中断", "cancelled": "已停止"}
BOARD = {"running": "在做", "queued": "排队", "waiting": "等你回答"}
RESULTS = {"done": "完成", "failed": "失败", "interrupted": "中断", "cancelled": "已停止"}
OVERVIEW_OUTCOMES = {"done": "✅ 完成", "failed": "❌ 失败", "cancelled": "⏹ 已停止", "interrupted": "⚠️ 中断"}
OVERVIEW_GROUPS = {"waiting": "等你回答", "result": "有结果", "running": "进行中"}
DIGEST_START = "等你开工"
GROUP_NAMES = {"waiting": "等你回答", "result": "有结果未看", "running": "进行中", "recent": "最近", "": "更早"}
START_PHASE = "处理本轮请求"  # TaskMonitor's phase before the checkpoint names one.
# The phase written to a run's checkpoint before its model writes one (app/worker.py): the status line names none.
SEED_PHASE = "准备处理本轮请求"
UNSET_PHASES = frozenset({START_PHASE, SEED_PHASE})
BLANK_PHASES = frozenset({"", *UNSET_PHASES})


def outcome_line(status: str) -> str:
    """The task card's status line once its run ended, by the same status as the answer's title."""
    return OUTCOME_LINES.get(status, OUTCOME_LINES["done"])


def answer_head(status: str) -> str:
    """How a turn that ended with ``status`` reads atop its answer card in a 话题区's thread: ``outcome_line``'s mark
    and STATES's word (✅ 做完, ⚠️ 失败, ⏹ 已停止); a turn waiting for an answer as its status line says it."""
    mark = "⚠️" if status == "interrupted" else outcome_line(status).partition(" ")[0]  # As 执行失败's.
    return f"{mark} {STATES[status]}" if status in STATES else outcome_line(status)


def request_line(text):
    """The round's request in one line: its first line that says something, not a heading the Bridge or a form put
    before the words ("用户补充回答：", a line ending in a colon of a few characters)."""
    for line in str(text or "").split("\n"):
        line = line.strip()
        if line and not (line.endswith(("：", ":")) and len(line) <= 12):
            return line
    return ""


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
    """The Bridge's machine restarted (or its task runner was killed) under a turn on the device."""
    return f"机器人所在机器重启过，{alias} 上这一轮可能还在跑；点检查并继续会先停掉它再接着做。"


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
    return f"任务 #{job_id} 已中断。在原话题{retry_hint()} 检查已有工作后继续。"


def ask_words(record, listed=0) -> str:
    """What a question record (app/questions.py) asks, in a line: its one question (or its header), else "3 个问题";
    with ``listed``, "3 个问题：" and each question cut to that many characters (the main thread's topic_read)."""
    asked = [q for q in record.get("questions") or [] if isinstance(q, dict)]
    if len(asked) == 1:
        return str(asked[0].get("question") or asked[0].get("header") or "")
    if not asked:
        return ""
    if not listed:
        return f"{len(asked)} 个问题"
    return f"{len(asked)} 个问题：" + "；".join(str(q.get("question") or "")[:listed] for q in asked)
