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
