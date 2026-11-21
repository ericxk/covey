"""Validated root reply identity, separate from execution and continuation rights.

Only backend completion checks may construct these objects. Progress events and
legacy error strings never establish a verified reply.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import uuid

from covey.core.roster import HARNESSES
from covey.core.model_policy import POLICY_CODES, POLICY_RECOVERY, policy_summary, valid_policy_event

MAX_REPLY_BYTES = 4 * 1024 * 1024
EXECUTION_STATES = frozenset({"completed", "failed", "cancelled", "timed_out", "unknown"})
ERROR_CODES = frozenset({"", "child_error", "child_aborted", "child_unknown", "settle_timeout",
    "root_failed", "root_incomplete", "cancelled", "timeout", "transport_error", "cleanup_unconfirmed",
    "protocol_error", "session_guard", "process_exit", "identity_mismatch", "output_missing", "storage_failed",
    "max_turns", "budget_exceeded", "authentication_error", "rate_limited", "workspace_routing_timeout",
    "workdir_missing", "background_timeout", "quota_exhausted", "model_blocked", "redaction_failed",
    "read_only_refused", "server_fault"}) | POLICY_CODES


# What the user can do next, where a failure has a clear way on. The plain words replace
# "原生会话不可续接": the next message in the topic starts a new native session.
RECOVERY = {
    "rate_limited": ("本轮没有产出回复。\n\n**怎么继续**\n"
                     "- 限流一般几分钟内恢复。稍后在本话题直接发一条消息（例如“继续”）：会开一个新会话，"
                     "带上本话题最近的对话；上一轮做到一半的操作不会自动接着执行，需要时让模型先核对。\n"
                     "- 着急或额度已用完时，点任务卡上的“🔁 换配置”，换一个模型接着做。"),
    "server_fault": ("本轮没有产出回复。\n\n**怎么继续**\n"
                     "- 这类故障一般很快恢复。可以点“现在重试”，或稍后在本话题发一条消息（例如“继续”）：会开一个新会话，"
                     "带上本话题最近的对话；上一轮做到一半的操作不会自动接着执行，需要时让模型先核对。"),
    "quota_exhausted": ("本轮没有产出回复。\n\n**怎么继续**\n"
                        "- 额度恢复前，重试和“继续”都不会成功，机器人也不会自动重试。\n"
                        "- 点任务卡上的“🔁 换配置”，换一个模型接着做。"),
    "model_blocked": ("本轮没有产出回复。\n\n**怎么继续**\n"
                      "- 拦截解除前，重试和“继续”都不会成功，机器人也不会自动重试。一般次日解除，"
                      "先发一条简单消息验证。\n"
                      "- 点任务卡上的“🔁 换配置”，换一个模型接着做。被拒绝的内容照原样再发，换了模型也可能被拒。"),
    "background_timeout": ("本轮没有可以发出的完整回复。需要继续时，在本话题发一条消息：会开一个新会话，"
                           "带上本话题最近的对话；被停止的后台任务不会自动重跑。"),
    "redaction_failed": "问题解决后，在本话题再发一条消息即可接着做。",
    "read_only_refused": "点任务卡上的“🔁 换配置”换到 Claude 或 Codex 的模型，再在本话题发一条消息。",
}
RECOVERY_DEFAULT = "未取得可交付的已验证公开回复，原生会话不可续接。"
# The line before a saved reply sent despite the failure, where the default wording misleads.
NOTICE = {
    "background_timeout": ("以下是本轮最后一条完整回复。需要继续时，在本话题发一条消息：会开一个新会话，"
                           "带上本话题最近的对话；被停止的后台任务不会自动重跑。"),
    "cancelled": "以下是停止前最后一条完整回复。需要继续时，在本话题发一条消息：会开一个新会话，带上本话题最近的对话。",
}
# The job record's status line after a turn that ended without a full reply, by error code; INCOMPLETE_STATUS
# for the others (app/turn_settle.py _settle_status; app/auto_retry.py KINDS says it again once a retry is off).
STATUS_MESSAGES = {
    "rate_limited": "模型服务限流，本轮没有产出回复",
    "server_fault": "模型服务临时故障，本轮没有产出回复",
    "quota_exhausted": "模型服务额度已用完，本轮没有产出回复",
    "model_blocked": "模型服务因安全拒绝拦截了当前账号，本轮没有产出回复",
    "background_timeout": "已到任务的时间上限，后台任务已停止，发出最后一条完整回复",
    "cancelled": "任务已停止，发出停止前最后一条完整回复",
}
INCOMPLETE_STATUS = "本轮执行未全部完成，原生会话不可续接"
# The detail after a failure's label (ExecutionOutcome.summary: Claude Code and the failure card), by error
# code; a route's own words for the error replace it.
DETAILS = {
    "child_error": "至少一个子任务执行失败。",
    "child_aborted": "至少一个子任务已中止。",
    "child_unknown": "部分子任务的结束状态无法确认。",
    "settle_timeout": "主任务结束后的等待时间已到，子任务或后续处理未全部结束。",
    "root_failed": "主任务后续执行失败。",
    "root_incomplete": "主任务的完整结束状态无法确认。",
    "policy_refusal": "模型服务明确拒绝了本轮内容，已停止自动继续。",
    "policy_fallback": "模型服务报告了明确降级，已停止自动继续。",
    "cancelled": "执行已取消。",
    "timeout": "任务执行时间已到。",
    "transport_error": "与执行后端的连接发生异常。",
    "cleanup_unconfirmed": "执行资源的清理尚未确认。",
    "protocol_error": "执行后端返回的信息未通过核对。",
    "session_guard": "原生会话未满足安全续接条件。",
    "process_exit": "执行进程未正常退出。",
    "identity_mismatch": "当前运行与回复身份未通过核对。",
    "output_missing": "未取得完整的最终输出。",
    "storage_failed": "结果保存未完成。",
    "max_turns": "已达到本次运行的轮数上限。",
    "budget_exceeded": "已达到本次运行的费用预算上限。",
    "authentication_error": "模型服务拒绝了当前身份或权限，请检查授权。",
    "rate_limited": "模型服务限流：请求或 token 速率超过了上游限制，或者用量额度不足，与任务内容无关。",
    "server_fault": "模型服务临时故障：过载（529）、服务端错误（5xx）或连接中断，与任务内容无关。",
    "quota_exhausted": "模型服务的用量额度已用完（上游返回 402，或 403 quota exceeded），与任务内容无关。",
    "model_blocked": "模型服务暂停了当前账号使用这个模型：多次触发安全拒绝后被拦截，不是限流。",
    "workspace_routing_timeout": "连接模型服务超时：工作区路由发现失败，请检查服务网络或代理配置。",
    "workdir_missing": "本话题的工作目录不存在，后端没有启动。恢复该目录后可点“检查并继续”，或在主聊天新开话题选择其他目录。",
    "background_timeout": "回复之后仍有后台任务在运行，已到任务的时间上限，机器人已停止执行进程和这些后台任务。",
    "redaction_failed": "本话题开着敏感词脱敏，但本轮没法脱敏，没有发给模型服务。",
    "read_only_refused": "这个群的群成员只读，这个 Harness 做不到只读，后端没有启动。",
}
# The whole failure text of Codex, Pi and dsh by error code (core/reply_state.failure_text), the Harness that
# failed named by {label}. The same codes are worded twice for now, in DETAILS and here.
FAILURES = {
    "root_failed": "{label} 本轮执行失败，原生会话不可续接。",
    "workspace_routing_timeout": "{label} 连接模型服务超时：工作区路由发现失败，请检查服务网络或代理配置。",
    "authentication_error": "{label} 模型服务拒绝了当前身份或权限，请检查授权。",
    "rate_limited": "{label} 模型服务限流或当前用量额度不足。",
    "quota_exhausted": "{label} 模型服务的用量额度已用完，与任务内容无关。",
    "root_incomplete": "{label} 本轮完整回复无法确认，原生会话不可续接。",
    "output_missing": "{label} 未返回本轮有效正文，原生会话不可续接。",
    "identity_mismatch": "{label} 返回的任务身份不匹配，原生会话不可续接。",
    "protocol_error": "{label} 本轮协议数据不完整，原生会话不可续接。",
    "process_exit": "{label} 工作进程异常退出，原生会话不可续接。",
    "transport_error": "{label} 通信失败，原生会话不可续接。",
    "cleanup_unconfirmed": "{label} 进程清理尚未确认，本轮结果暂不能交付。",
    "storage_failed": "{label} 本轮结果保存未确认，请检查本地状态。",
    "cancelled": "{label} 本轮任务已停止，原生会话不可续接。",
    "timeout": "{label} 本轮任务已超时，原生会话不可续接。",
}


def is_identifier(value):
    """A protocol ID: a non-empty string of at most 256 characters with no whitespace."""
    return isinstance(value, str) and 0 < len(value) <= 256 and not any(c.isspace() for c in value)


@dataclass(frozen=True)
class VerifiedRootReply:
    backend: str
    run_id: str
    session_id: str
    boundary: tuple[str, ...]
    message_ids: tuple[str, ...]
    text: str
    generation: int = 1

    def __post_init__(self):
        if type(self.generation) is not int or self.generation < 1:
            raise ValueError("invalid verified reply generation")
        if not isinstance(self.backend, str) or self.backend not in HARNESSES:
            raise ValueError("unsupported verified reply backend")
        try:
            valid_run = str(uuid.UUID(self.run_id)) == self.run_id
        except (ValueError, TypeError, AttributeError):
            valid_run = False
        if not valid_run or not is_identifier(self.session_id):
            raise ValueError("invalid verified reply identity")
        for values in (self.boundary, self.message_ids):
            if (not isinstance(values, tuple) or not values or len(values) > 20000
                    or not all(is_identifier(v) for v in values) or len(set(values)) != len(values)):
                raise ValueError("invalid verified reply boundary")
        if not isinstance(self.text, str) or not self.text.strip() or len(self.text.encode("utf-8")) > MAX_REPLY_BYTES:
            raise ValueError("invalid verified reply text")

    @property
    def sha256(self):
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    @property
    def fingerprint(self):
        return hashlib.sha256(json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True,
                                         separators=(",", ":")).encode("utf-8")).hexdigest()

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) != {"backend", "run_id", "session_id", "boundary", "message_ids", "text", "generation"}:
            raise ValueError("invalid verified reply record")
        if not isinstance(value["boundary"], (tuple, list)) or not isinstance(value["message_ids"], (tuple, list)):
            raise ValueError("invalid verified reply record")
        return cls(**{**value, "boundary": tuple(value["boundary"]), "message_ids": tuple(value["message_ids"])})


@dataclass(frozen=True)
class ExecutionOutcome:
    status: str
    error_code: str = ""
    identity_confirmed: bool = False
    cleanup_confirmed: bool = False
    protection_confirmed: bool = False
    resume_allowed: bool = False
    reply_fingerprint: str = ""
    policy_event: dict | None = None

    def __post_init__(self):
        if self.policy_event is not None and (not valid_policy_event(self.policy_event) or self.resume_allowed
                                             or self.status == "completed"):
            raise ValueError("invalid model policy stop")
        if (not isinstance(self.reply_fingerprint, str) or (self.reply_fingerprint
                and (len(self.reply_fingerprint) != 64 or any(c not in "0123456789abcdef" for c in self.reply_fingerprint)))):
            raise ValueError("invalid outcome reply identity")
        if (not isinstance(self.status, str) or self.status not in EXECUTION_STATES
                or not isinstance(self.error_code, str) or self.error_code not in ERROR_CODES):
            raise ValueError("invalid execution outcome")
        if any(type(v) is not bool for v in (self.identity_confirmed, self.cleanup_confirmed,
                                             self.protection_confirmed, self.resume_allowed)):
            raise ValueError("invalid execution confirmation")
        if self.resume_allowed and (self.status != "completed" or not self.delivery_authorized):
            raise ValueError("incomplete execution cannot authorize continuation")

    @property
    def is_policy_stop(self):
        return self.policy_event is not None

    def to_dict(self):
        # compat(fb0b267): a runner of an older release reads a saved reply without a policy event, so one without
        # it has no such key; retire when no task runner before fb0b267 runs (compat_report: policy-event-runners)
        value = asdict(self)
        if self.policy_event is None:
            value.pop("policy_event")
        return value

    @property
    def delivery_authorized(self):
        return not self.is_policy_stop and self.status != "unknown" and self.identity_confirmed and self.cleanup_confirmed and self.protection_confirmed

    @property
    def job_status(self):
        return {"completed": "done", "cancelled": "cancelled"}.get(self.status, "failed")

    def summary(self, detail=""):
        """The failure's label and detail; ``detail``, a route's own words for the error, replaces the shared detail
        (of a policy stop: who was refused doing what, core/model_policy.py stop_detail)."""
        if self.policy_event is not None:
            return policy_summary(self.policy_event, cleanup=self.cleanup_confirmed, detail=detail)
        if self.status == "completed":
            return ""
        label = {"cancelled": "本轮任务已停止", "timed_out": "本轮任务已超时"}.get(self.status, "本轮任务未全部完成")
        return label + "。" + (detail or DETAILS.get(self.error_code, ""))

    def notice(self):
        if self.status == "completed":
            return ""
        return self.summary() + NOTICE.get(self.error_code, "以下保留主任务在本轮正常生成的公开回复，原生会话不可续接。")

    def failure_reply(self, detail=""):
        """The task card's text when the run produced no reply that may be sent; ``detail`` as in summary."""
        if self.policy_event is not None:
            return self.summary(detail) + POLICY_RECOVERY
        return self.summary(detail) + RECOVERY.get(self.error_code, RECOVERY_DEFAULT)
