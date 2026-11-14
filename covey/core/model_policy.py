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


def policy_summary(event, *, cleanup=True):
    action = "官方模型降级" if event["kind"] == "fallback" else "官方内容安全拒绝"
    category = event["category"] if event["category"] != "unspecified" else "上游未提供类别"
    where = "SubAgent " + event["child_id"] if event["scope"] == "subagent" else "主任务"
    transition = (f"\n模型：{event['source_model']} → {event['target_model']}。"
                  if event["source_model"] and event["target_model"] else "")
    state = "本轮已停止" if cleanup else "已请求停止，进程清理尚未确认"
    return f"{state}：{where}触发{action}（{category}）。" + transition


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
