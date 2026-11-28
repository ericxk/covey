"""Why a turn did not succeed and what can be done next, in plain words: ``failure_view`` of its job record, one pure
function for the task card's body and buttons (app/worker.py, app/auto_retry.py failed_controls), the recovery card
(app/recovery_cards.py), the 停住了 relay at the private chat's top level (app/relay_stops.py) and the web page's turn
(app/web_turns.py failure_of, turn.data ``failure``).

``title`` is one sentence: the reason and what happens or can be done next. ``detail`` keeps the internal words (the
record's status line, the error code, the subagent's id, why its task runner did not start) the title leaves out.
``actions`` are the next steps, in order: continue (检查并继续), retry (现在重试, the same click), diagnose (看原因, a
read-only turn that looks into it), split (拆小再试), switch_model (换配置). The card offers the ones its record allows
(core/recovery.py classify); ``label`` names each as the card's button does.

The record says why through ``failure_code`` (the turn's ExecutionOutcome error code, app/worker.py) and
``failure_detail`` (a route's own words: who was refused doing what); a record written before them is read from its
status and status line.
"""
from __future__ import annotations

import datetime as _dt
import math
import re

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
# Not error codes: what failure_code says of a record that names none. start_failed: its task runner never started
# (app/detached.py not_started, or one that ended before its job began), so nothing of the turn ran.
START_FAILED = "start_failed"
# The status line's opening of a turn its Harness never started (app/job_dispatch.py: its login check, its program, its
# start-up raised); failure_code says "failed" of it.
STARTUP_FAILED = "后端初始化失败："
PSEUDO = frozenset({"policy", "restart", "interrupted", "incomplete", "failed", "undelivered", START_FAILED})
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
    "server_fault": ("模型服务临时故障（过载或连接中断），和任务内容无关", (RETRY, SWITCH_MODEL)),
    "quota_exhausted": ("模型服务的额度用完了，额度恢复前重试不会成功；可以换个模型接着做", (SWITCH_MODEL,)),
    "model_blocked": ("账号因多次安全拒绝被模型服务暂时拦住了（一般次日解除），重试不会成功；可以换个模型接着做",
                      (SWITCH_MODEL,)),
    "authentication_error": ("模型服务不认当前的登录或权限，需要重新登录或检查授权", (DIAGNOSE,)),
    "request_hung": ("模型请求长时间没有回应，Bridge 停下了这一轮，好让你后来的话先跑；可以接着做，或先看原因",
                     (CONTINUE, DIAGNOSE)),
    "workspace_routing_timeout": ("连不上模型服务（工作区路由超时），可能是网络或代理的问题", (RETRY, DIAGNOSE)),
    "workdir_missing": ("本话题的工作目录不在了，这一轮没有开始；恢复目录后可以接着做", (CONTINUE,)),
    # compat: 敏感词脱敏 was taken out (T947); a record from before may still say this.
    "redaction_failed": ("本话题开着敏感词脱敏，这一轮没法脱敏，没有发给模型；问题解决后可以重试", (RETRY,)),
    "read_only_refused": ("这个群的群成员只读，这个模型做不到只读，这一轮没有开始；可以换个模型（Claude 或 Codex）",
                          (SWITCH_MODEL,)),
    START_FAILED: ("任务进程没能启动，这一轮没有开始；可以接着做，或先看原因", (CONTINUE, DIAGNOSE)),
    # A refusal said only by the backend's error (Codex's invalid_prompt, Antigravity's safety block; a Claude Code
    # policy event is "policy" instead): the same words would be refused again (T872).
    "policy_refusal": ("模型服务的安全审查拒了这一轮（不是 Covey 出错，也不是额度）；原样重试多半还会被拒，可以拆小再试或换个模型",
                       (SPLIT, SWITCH_MODEL)),
    "model_unavailable": ("所选模型在当前账号里用不了（没有权限、已下线或不支持所选档位），重试不会成功；换个模型接着做",
                          (SWITCH_MODEL,)),
}
SAID_CHARS = 300  # Of a Harness's own words for a failure (``said_of``), on the card.
LONG_RUN = re.compile(r"(?=[A-Za-z0-9_+/=-]*\d)[A-Za-z0-9_+/=-]{24,}")  # A key's or an id's run: long, with a digit.
HARNESS_NAMES = {"codex": "Codex", "agy": "Antigravity", "claude": "Claude Code"}  # Whose words, on the card.


def said_of(text) -> str:
    """A Harness's own words for a failure as the card may show them (RunResult.backend_said): on one line, what looks
    like a secret (core/text.py redact) and a key, a path, an address, a link or an e-mail (core/quota.py scrub)
    hidden, at most SAID_CHARS. Whatever the error, known or not: the owner reads what the Harness said (T872)."""
    from covey.core.quota import CUT, SCRUB
    from covey.core.text import redact
    text = " ".join(redact(str(text or "")).split())
    # SCRUB's last rule (any long run) would hide an error's own name ("responseStreamConnectionFailed"): a key or an
    # id has digits in it, a name of words has none.
    for pattern in (*SCRUB[:-1], LONG_RUN):
        text = pattern.sub(CUT, text)
    text = re.sub(re.escape(CUT) + r"(?:\s*" + re.escape(CUT) + ")+", CUT, text)
    return text[:SAID_CHARS].strip()


# Where the backend's own words (the record's failure_detail: its kind of error, what it said in its words of record)
# go in the detail too: a failure whose title says no more than that the turn did not finish, or one of the two above.
SAID_TOO = frozenset({"failed", "incomplete", "policy_refusal", "model_unavailable"})


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
    if (status in ("failed", "interrupted") and record.get("runner_start_failed") is True
            and not record.get("cancel_requested_by")):
        return START_FAILED  # Its card is the recovery card's START_FAILED (core/recovery.py), whatever ended it.
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
        if kind in ("rate_limited", "incomplete", "server_fault", "quota_exhausted"):
            return {"incomplete": "settle_timeout"}.get(kind, kind)
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


def _later(record, now, doing, total, cause="限流"):
    """"约 3 分钟后（14:05）会自动重试（第 1/2 次）" for a pending automatic retry, else ""; a limit's known reset is said
    before it (限流预计 16:20 恢复，约 3 分钟后（16:21）会自动重试; ``cause`` 额度 for a spent quota's)."""
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
    reset = clock(record.get("auto_retry_reset_at")) if record.get("auto_retry_reset_at") else ""
    return f"{cause + '预计 ' + reset + ' 恢复，' if reset else ''}{when}会{doing}{count}"


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
    if code == START_FAILED and record.get("runner_start_error"):
        parts.append(f"启动出错 {record['runner_start_error']}")  # The exception's kind (app/detached.py start_error).
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
    total = 3 if code == "server_fault" else 0 if code == "rate_limited" else 1
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
        said = str(record.get("failure_detail") or "").strip()
        if code == "quota_exhausted" and said:
            # In the route's words: a gateway's quota (core/quota_hold.py), the accounts' (backends/agy/accounts.py,
            # app/quota_failure.py): when it is back, where to go.
            title = said.rstrip("。")
        if code == "quota_exhausted":
            later = _later(record, now, "自动接着做", 0, "额度")
            title += f"；{later}" if later else "" if "换" in title else "；可以换个模型接着做"
        if code in ("rate_limited", "server_fault"):
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
    detail = _detail(record, code)
    said = str(record.get("failure_detail") or "").strip()
    if said and (code in SAID_TOO or code in BROKE or code in UNFINISHED) and said.rstrip("。") not in title + detail:
        detail = "；".join(part for part in (detail, said.rstrip("。")) if part)
    own = str(record.get("failure_said") or "").strip()
    if own:
        detail = "；".join(part for part in (detail, f"{HARNESS_NAMES[record['backend']] + ' ' if record.get('backend') in HARNESS_NAMES else '后端'}原话：{own}") if part)
    return {"title": title, "detail": detail, "actions": list(actions)}


def label(record, action) -> str:
    """What ``action``'s button says for ``record``, as its card says it: continue is the record's own 检查并继续
    (重新投递结果 for a result that did not go out, core/recovery.py classify), the others are LABELS'."""
    if action == CONTINUE:
        return classify(record).label or LABELS[CONTINUE]
    return LABELS.get(action, "")


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
