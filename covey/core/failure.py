"""Why a turn did not succeed and what can be done next, in plain words: ``failure_view`` of its job record, one pure
function for the task card's body and buttons (app/worker.py, app/auto_retry.py failed_controls), the recovery card
(app/recovery_cards.py) and the web page's turn (app/web_sync.py, turn.data ``failure``).

``title`` is one sentence: the reason and what happens or can be done next. ``detail`` keeps the internal words (the
record's status line, the error code, the subagent's id) the title leaves out. ``actions`` are the next steps, in
order: continue (检查并继续), retry (现在重试, the same click), diagnose (看原因, a read-only turn that looks into it),
split (拆小再试), switch_model (换配置). The card offers the ones its record allows (core/recovery.py classify).

The record says why through ``failure_code`` (the turn's ExecutionOutcome error code, app/worker.py) and
``failure_detail`` (a route's own words: who was refused doing what); a record written before them is read from its
status and status line.
"""
from __future__ import annotations

import datetime as _dt
import math

from covey.core.model_policy import FALLBACK_MODELS, category_words, policy_where
from covey.core.recovery import RETRY_LABEL, authorized, classify, saved_delivery
from covey.core.result import INCOMPLETE_STATUS, STATUS_MESSAGES

CONTINUE, RETRY, DIAGNOSE, SPLIT, SWITCH_MODEL = "continue", "retry", "diagnose", "split", "switch_model"
ACTIONS = (CONTINUE, RETRY, DIAGNOSE, SPLIT, SWITCH_MODEL)
# The button of each, on cards (cards/recovery.py); continue and retry are the 检查并继续 click (重新投递结果 for a
# result that did not go out).
LABELS = {CONTINUE: RETRY_LABEL, RETRY: "现在重试", DIAGNOSE: "看原因", SPLIT: "拆小再试", SWITCH_MODEL: "换个模型"}

# A task's error went wrong at this step (任务异常: one sentence says which).
BROKE = {
    "transport_error": "和执行后端的连接断了",
    "process_exit": "执行进程没有正常退出",
    "protocol_error": "执行后端返回的数据没通过核对",
    "storage_failed": "结果没能保存下来",
    "identity_mismatch": "这一轮的回复和任务身份对不上",
    "session_guard": "会话没通过续接检查",
    "cleanup_unconfirmed": "执行进程的清理还没确认",
}
# Ended before its work was done; what was done stays above it (原生会话不可续接 in the old words).
UNFINISHED = {
    "settle_timeout": "主任务回答之后，子任务或后续处理没在等待时间内结束",
    "root_incomplete": "主任务没有正常结束",
    "root_failed": "主任务中途出错",
    "output_missing": "没拿到完整的最终回答",
    "child_error": "有子任务失败了",
    "child_aborted": "有子任务中止了",
    "child_unknown": "有子任务的结局没法确认",
    "incomplete": "",  # A record from before failure_code: its status line says no more.
}
# Not error codes: what failure_code says of a record that names none.
PSEUDO = frozenset({"policy", "restart", "interrupted", "incomplete", "failed", "undelivered"})
# Out of a limit of the run: continue, or the same in smaller steps.
LIMITS = {
    "timeout": "到了任务的时间上限",
    "background_timeout": "回答之后后台任务一直没结束，到了任务的时间上限",
    "max_turns": "到了这一轮的轮数上限",
    "budget_exceeded": "到了这一轮的费用上限",
}
# Its own words and next steps.
OWN = {
    "rate_limited": ("上游限流，和任务内容无关", (RETRY, SWITCH_MODEL)),
    "quota_exhausted": ("模型服务的额度用完了，额度恢复前重试不会成功；可以换个模型接着做", (SWITCH_MODEL,)),
    "model_blocked": ("账号因多次安全拒绝被模型服务暂时拦住了（一般次日解除），重试不会成功；可以换个模型接着做",
                      (SWITCH_MODEL,)),
    "authentication_error": ("模型服务不认当前的登录或权限，需要重新登录或检查授权", (DIAGNOSE,)),
    "workspace_routing_timeout": ("连不上模型服务（工作区路由超时），可能是网络或代理的问题", (RETRY, DIAGNOSE)),
    "workdir_missing": ("本话题的工作目录不在了，这一轮没有开始；恢复目录后可以接着做", (CONTINUE,)),
    "redaction_failed": ("本话题开着敏感词脱敏，这一轮没法脱敏，没有发给模型；问题解决后可以重试", (RETRY,)),
}


def clock(at):
    try:
        return _dt.datetime.fromtimestamp(float(at)).astimezone().strftime("%H:%M")
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def failure_code(record):
    """Why ``record``'s turn did not succeed, as an error code (core/result.py ERROR_CODES) or one of "policy",
    "restart", "interrupted", "incomplete", "failed" (PSEUDO); "" when it did, or the owner stopped it."""
    status = record.get("status")
    if record.get("policy_stop"):
        return "policy"
    if saved_delivery(record):  # A reply was saved: only it is sent, never the task run again (core/recovery.py).
        return "" if record.get("delivery_ok") is True or not authorized(record) else "undelivered"
    if status == "interrupted":
        return "restart" if record.get("interrupted_by") in ("restart", "sigterm") else "interrupted"
    code = record.get("failure_code")
    if status not in ("failed", "cancelled", "timed_out") or record.get("cancel_requested_by"):
        return ""
    if status == "failed" and record.get("execution_status") in ("done", "waiting_user"):
        return "undelivered"  # The turn ended well; its result or question did not go out.
    if isinstance(code, str) and code and code != "cancelled":
        return code
    if status == "cancelled":
        return ""
    if record.get("auto_retry_state"):  # app/auto_retry.py KINDS: a retried failure says its kind.
        kind = record.get("auto_retry_kind") or "rate_limited"
        if kind in ("rate_limited", "incomplete"):
            return "rate_limited" if kind == "rate_limited" else "settle_timeout"
    line = str(record.get("status_message") or "")
    found = next((name for name, words in STATUS_MESSAGES.items() if words and line.startswith(words)), "")
    if found and found != "cancelled":
        return found
    return "incomplete" if line.startswith(INCOMPLETE_STATUS) else "failed"


def _pending(record):
    """(time, attempt) of ``record``'s pending automatic retry, else None."""
    if record.get("auto_retry_state") != "pending":
        return None
    return record.get("auto_retry_at"), record.get("auto_retry_attempt")


def _later(record, now, doing, total):
    """"约 3 分钟后（14:05）会自动重试（第 1/2 次）" for a pending automatic retry, else ""."""
    pending = _pending(record)
    if pending is None:
        return ""
    at, attempt = pending
    when = clock(at)
    minutes = ""
    if now is not None and isinstance(at, (int, float)) and at > now:
        minutes = f"约 {max(1, math.ceil((at - now) / 60))} 分钟后"
    when = f"{minutes}（{when}）" if minutes and when else minutes or (when + " " if when else "") or "稍后"
    count = f"（第 {attempt}/{total} 次）" if isinstance(attempt, int) and total else ""
    return f"{when}会{doing}{count}"


def _policy_title(record):
    event = record.get("policy_stop") if isinstance(record.get("policy_stop"), dict) else {}
    said = str(record.get("failure_detail") or "").strip()
    if said:
        return said.rstrip("。") + "；这一轮已停止"
    where = policy_where(event.get("scope") or "main")
    action = "被官方降级" if event.get("kind") == "fallback" else "被官方安全策略拦下"
    return f"{where}{action}（{category_words(event)}），这一轮已停止"


def _detail(record, code):
    parts = [str(record.get("status_message") or "").strip()]
    if code and code not in PSEUDO:
        parts.append(f"错误码 {code}")
    event = record.get("policy_stop")
    if isinstance(event, dict):
        parts.append(" · ".join(filter(None, [
            f"{event.get('kind')}/{event.get('category')}", f"范围 {event.get('scope')}",
            f"子代理标识 {event['child_id']}" if event.get("child_id") else "", f"证据 {event.get('evidence')}"])))
    return "；".join(part for part in parts if part)


def failure_view(record, now=None):
    """{title, detail, actions} for ``record``'s turn that did not succeed, or None (it did, it waits, it runs, or
    the owner stopped it). ``now``: for how long until a pending automatic retry; None leaves that out. ``actions``
    are what the record allows: continue and retry only where 检查并继续 applies, none of a topic without one."""
    code = failure_code(record)
    if not code:
        return None
    total = 2 if code == "rate_limited" else 1
    if code == "policy":
        title, actions = _policy_title(record), (SPLIT, SWITCH_MODEL)
        model = record.get("policy_retry_model")
        if record.get("policy_retry_state") in ("pending", "submitting", "queued") and model:
            name = next((name for found, name in FALLBACK_MODELS.values() if found == model), model)
            title += f"；将换 {name} 自动重试一次"
            actions = ()  # Another turn now would run beside the retry.
    elif code == "restart":
        later = _later(record, now, "自动接着做", 1)
        title = "Covey 重启时打断了这一轮" + (f"，{later}" if later else "；可以接着做")
        actions = () if later else (CONTINUE,)
    elif code == "undelivered":
        title, actions = "这一轮做完了，但结果没能发出去（已保存）；可以重新投递", (CONTINUE,)
    elif code == "interrupted":
        title, actions = "这一轮被打断了；可以接着做，或先看原因", (CONTINUE, DIAGNOSE)
    elif code in OWN:
        title, actions = OWN[code]
        if code == "rate_limited":
            later = _later(record, now, "自动重试", total)
            title += f"；{later}" if later else "；可以稍后重试，着急可以换个模型"
    elif code in LIMITS:
        title, actions = LIMITS[code] + "，这一轮停在这里；可以接着做，或拆小再试", (CONTINUE, SPLIT)
    elif code in BROKE:
        title, actions = f"任务异常：{BROKE[code]}；可以接着做，或先看原因", (CONTINUE, DIAGNOSE)
    elif code in UNFINISHED:
        later = _later(record, now, "自动接着做", 1) if code == "settle_timeout" else ""
        title = ("这一轮没做完" + (f"（{UNFINISHED[code]}）" if UNFINISHED[code] else "") + "，已做的部分保留着；"
                 + (later if later else "可以在新会话里接着做，或先看原因"))
        actions = (CONTINUE, DIAGNOSE)
    else:
        title, actions = "这一轮没有成功；可以接着做，或先看原因", (CONTINUE, DIAGNOSE)
    if not record.get("topic_id"):
        actions = ()
    else:
        label = classify(record).label
        if not (bool(label) if code == "undelivered" else label == RETRY_LABEL):
            actions = tuple(a for a in actions if a not in (CONTINUE, RETRY))
    return {"title": title, "detail": _detail(record, code), "actions": list(actions)}


HEADLINE_CHARS = 40  # docs/ui.md 第 7 节: a sentence over 40 characters is split, its second part on the next line.


def split_title(title):
    """(headline, line) of a ``failure_view`` title for a conclusion band (cards/ui.py conclusion_band): the reason
    before the first "；", what can be done next after it; a reason over HEADLINE_CHARS splits at its last "，" before
    then too, the rest going first on the line."""
    head, _, rest = str(title or "").partition("；")
    if len(head) > HEADLINE_CHARS:
        cut = head.rfind("，", 0, HEADLINE_CHARS)
        if cut > 0:
            head, rest = head[:cut], head[cut + 1:] + ("；" + rest if rest else "")
    return head, rest


def card_body(view):
    """A failed task card's body: the reason and what comes next, in one line (the internal words go to 技术详情)."""
    return f"**{view['title']}**"


def follow_up(name, record, view):
    """(what the topic shows as the owner's message, what the model reads) for the follow-up ``name`` (DIAGNOSE,
    SPLIT) of ``record``'s task, whose ``view`` is failure_view's. 看原因 is a read-only look into why; 拆小再试
    does the same work in smaller steps, the refused part left for last."""
    number = record.get("job_id")
    title = (view or {}).get("title") or "这一轮没有成功"
    detail = (view or {}).get("detail") or ""
    said = f"上一轮（任务 #{number}）的结局：{title}" + (f"\n技术详情：{detail}" if detail else "")
    if name == DIAGNOSE:
        return (f"看原因：任务 #{number} 为什么没成功",
                f"主人点了「看原因」，想知道上一轮为什么没成功。\n\n{said}\n\n"
                "这是一轮只读排查：看日志、进程、检查点、输出和相关文件找原因；不要改文件，不要重跑上一轮的工作，"
                "不要提交、停止或重启任何任务和服务。最后用几句话说清：哪一步出的错、为什么、下一步建议怎么做"
                "（接着做、拆小再试还是换个模型）。")
    return (f"拆小再试：任务 #{number}",
            f"主人点了「拆小再试」：上一轮没做成，把它要做的事拆成更小的几步重新做。\n\n{said}\n\n"
            "先核对上一轮已经做完的部分，不重复做；剩下的拆成小块，一次只处理一块（少读一些文件，一次只读几百行，"
            "派子代理时每个只给一小块），做完一块说一块。上一轮被安全策略拦下的那块先跳过，最后告诉主人跳过了什么、"
            "为什么。")
