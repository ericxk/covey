"""Bounded model safety metadata and user-facing stop reasons. Standard library only."""
import re

POLICY_CODES = frozenset({"policy_refusal", "policy_fallback"})


def valid_policy_event(value):
    fields = {"kind", "category", "source_model", "target_model", "scope", "child_id", "response_id", "evidence"}
    return (isinstance(value, dict) and set(value) == fields
            and all(isinstance(v, str) and len(v) <= 160 and re.fullmatch(r"[A-Za-z0-9_.:-]*", v) for v in value.values())
            and value["kind"] in {"refusal", "fallback"} and value["scope"] in {"main", "subagent"}
            and value["category"] in {"cyber", "frontier_llm", "bio", "reasoning_extraction", "general_harms", "unspecified"}
            and value["evidence"] in {"fallback_block", "fallback_iteration", "refusal_stop", "http_refusal", "server_audit"})


def category_words(event):
    category = event.get("category")
    return category if category and category != "unspecified" else "上游未提供类别"


def policy_where(scope, name="", doing=""):
    """Who was refused and what it was doing then, as the stop and the 执行过程 line say it: 子代理「标注第 1 块」在读
    chunk01.txt 第 1201–1600 行时. ``name``: the subagent's description; ``doing``: its last tool calls in words."""
    who = (f"子代理「{name}」" if name else "一个子代理") if scope == "subagent" else "主任务"
    return who + (f"在{doing}时" if doing else "")


def blocked_words(event):
    """被拦 for a refusal, 被降级 for a fallback."""
    return "被降级" if event.get("kind") == "fallback" else "被拦"


def tolerated_line(event, where):
    """The 执行过程 line of a subagent's first refusal, which does not stop the turn (docs/policy-stop.md)."""
    return f"{where}{blocked_words(event)}（{category_words(event)}），主任务继续"


def stop_detail(event, where, earlier=""):
    """What a stop says of the refusal that stopped the turn: ``where`` (policy_where) and, when a subagent's refusal
    was let through before in this task, ``earlier``, where that one was."""
    text = f"{where}{blocked_words(event)}（{category_words(event)}）。"
    if earlier:
        text += f"这是本任务第 2 次被拦（之前{earlier}被拦过一次），连续被拒可能让账号被拦，所以停下。"
    return text


def same_refusal(seen, event):
    """Whether ``event`` is the refusal ``seen`` again (one refusal comes as a stream delta and as its message): a
    subagent's, of the same subagent, and the same response where both name one."""
    return (isinstance(seen, dict) and isinstance(event, dict) and seen.get("scope") == event.get("scope") == "subagent"
            and seen.get("child_id") == event.get("child_id")
            and (not seen.get("response_id") or not event.get("response_id") or seen["response_id"] == event["response_id"]))


def policy_summary(event, *, cleanup=True, detail=""):
    """The stop's first line. ``detail`` (stop_detail) says who was refused doing what; without it the event says
    only whether it was the main task or a subagent."""
    action = "官方模型降级" if event["kind"] == "fallback" else "官方内容安全拒绝"
    where = "一个子代理" if event["scope"] == "subagent" else "主任务"
    transition = (f"\n模型：{event['source_model']} → {event['target_model']}。"
                  if event["source_model"] and event["target_model"] else "")
    state = "本轮已停止" if cleanup else "已请求停止，进程清理尚未确认"
    return f"{state}：" + (detail or f"{where}触发{action}（{category_words(event)}）。") + transition


POLICY_RECOVERY = ("\n\n不会自动重试或自动检查并继续。已产生的文件和过程记录保留；"
                   "本轮部分输出不作为完成结果。请先检查任务内容，再在本话题明确提出调整后的要求。"
                   "已提交到外部系统的作业不随本机进程停止而自动撤销。")


# A refusal of these categories is retried once, on this older model (app/policy_retry.py; the owner approved it on
# 2026-11-10, after Anthropic's guidance): (model id, its name on cards). Fallbacks and other categories never are.
FALLBACK_MODELS = {"cyber": ("claude-opus-4-8", "Opus 4.8"), "bio": ("claude-opus-5", "Opus 5")}


def fallback_model(event):
    """(model id, name) a stop on ``event`` is retried on once, or None: only a refusal of FALLBACK_MODELS' categories."""
    if not isinstance(event, dict) or event.get("kind") != "refusal":
        return None
    return FALLBACK_MODELS.get(event.get("category"))


def retried_line(category, name):
    """What the stopped turn's card and its retry's card say of the retry."""
    return f"已因安全拒绝（{category}）换 {name} 重试"


def retry_recovery(category, name):
    """POLICY_RECOVERY for a stop that is retried on ``name``."""
    return (f"\n\n将因安全拒绝（{category}）换 {name} 在新会话里自动重试一次本轮请求；重试再被拒绝就停止，不再重试。"
            "已产生的文件和过程记录保留；本轮部分输出不作为完成结果。已提交到外部系统的作业不随本机进程停止而自动撤销。")
